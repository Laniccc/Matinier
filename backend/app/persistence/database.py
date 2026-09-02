from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.persistence.models import Base


logger = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str) -> None:
        parsed_url = make_url(url)
        self.is_sqlite = parsed_url.get_backend_name() == "sqlite"
        self.database_location = (
            parsed_url.database
            if self.is_sqlite and parsed_url.database not in {None, ""}
            else f"<{parsed_url.drivername} database>"
        )
        connect_args = {"check_same_thread": False} if self.is_sqlite else {}
        engine_options = {"poolclass": StaticPool} if url == "sqlite://" else {}
        self.engine: Engine = create_engine(
            url,
            connect_args=connect_args,
            **engine_options,
        )
        if self.is_sqlite:
            use_wal = parsed_url.database not in {None, "", ":memory:"}

            @event.listens_for(self.engine, "connect")
            def configure_sqlite(dbapi_connection, _connection_record) -> None:
                cursor = dbapi_connection.cursor()
                try:
                    cursor.execute("PRAGMA foreign_keys=ON")
                    cursor.execute("PRAGMA busy_timeout=5000")
                    if use_wal:
                        current_row = cursor.execute(
                            "PRAGMA journal_mode"
                        ).fetchone()
                        current_mode = current_row[0] if current_row else None
                        try:
                            if str(current_mode).lower() == "wal":
                                journal_mode = current_mode
                            else:
                                row = cursor.execute(
                                    "PRAGMA journal_mode=WAL"
                                ).fetchone()
                                journal_mode = row[0] if row else None
                            if str(journal_mode).lower() != "wal":
                                logger.warning(
                                    "SQLite WAL mode was not enabled",
                                    extra={
                                        "event": "sqlite_wal_unavailable",
                                        "database_path": self.database_location,
                                        "journal_mode": journal_mode,
                                    },
                                )
                        except Exception as error:
                            logger.warning(
                                "SQLite WAL mode could not be enabled",
                                extra={
                                    "event": "sqlite_wal_unavailable",
                                    "database_path": self.database_location,
                                    "internal_error_type": type(error).__name__,
                                },
                            )
                    cursor.execute("PRAGMA synchronous=NORMAL")
                finally:
                    cursor.close()
        self._session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
        )

    def sessions(self) -> Generator[Session, None, None]:
        with self.session() as db_session:
            yield db_session

    @contextmanager
    def session(self) -> Iterator[Session]:
        """Open one configured unit of work for background services."""

        db_session = self._session_factory()
        try:
            yield db_session
        finally:
            db_session.close()

    def check_connection(self) -> dict[str, str | int | None]:
        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            settings = self._sqlite_runtime_settings(connection)
        logger.info(
            "database connection ready",
            extra={
                "event": "database_connection_ready",
                "database_path": self.database_location,
                **settings,
            },
        )
        return settings

    def check_read_write(self) -> None:
        """Verify reads and the ability to acquire a write transaction."""

        with self.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            connection.rollback()
            if not self.is_sqlite:
                return
            try:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            finally:
                if connection.in_transaction():
                    connection.rollback()

    def sqlite_runtime_settings(self) -> dict[str, str | int | None]:
        """Return the effective connection policy for diagnostics and tests."""

        with self.engine.connect() as connection:
            return self._sqlite_runtime_settings(connection)

    def _sqlite_runtime_settings(
        self,
        connection,
    ) -> dict[str, str | int | None]:
        if not self.is_sqlite:
            return {}
        return {
            "journal_mode": connection.exec_driver_sql(
                "PRAGMA journal_mode"
            ).scalar_one(),
            "synchronous": connection.exec_driver_sql(
                "PRAGMA synchronous"
            ).scalar_one(),
            "foreign_keys": connection.exec_driver_sql(
                "PRAGMA foreign_keys"
            ).scalar_one(),
            "busy_timeout_ms": connection.exec_driver_sql(
                "PRAGMA busy_timeout"
            ).scalar_one(),
        }

    def create_schema(self) -> None:
        """Create tables for isolated tests; deployed databases use Alembic."""
        Base.metadata.create_all(self.engine)

    def dispose(self) -> None:
        self.engine.dispose()
