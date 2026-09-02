from pathlib import Path

import sqlalchemy as sa

from app.persistence.database import Database
from meeting_plugin_fakes import seed_meeting_history
from test_plugin_document_migration import _migrate


TABLES = ("sessions", "media_sessions", "segments", "meeting_marks", "action_candidates", "action_candidate_revisions", "assistant_executions", "assistant_events", "external_action_claims")
NEW_TABLES = {"meeting_plugin_sessions", "meeting_plugin_operations", "assistant_action_intents"}


def snapshot(engine):
    with engine.connect() as connection:
        return {name: sorted(str(tuple(row)) for row in connection.execute(sa.text(f'SELECT * FROM "{name}"'))) for name in TABLES}


def test_incremental_migration_preserves_history_and_constraints(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'upgrade.db').as_posix()}"
    _migrate(url, "upgrade", "20260828_0026")
    db = Database(url)
    try:
        seed_meeting_history(db)
        before = snapshot(db.engine)
        _migrate(url, "upgrade", "20260831_0027")
        _migrate(url, "upgrade", "20260831_0027")
        assert snapshot(db.engine) == before
        inspector = sa.inspect(db.engine)
        assert NEW_TABLES.issubset(inspector.get_table_names())
        unique = {tuple(item["column_names"]) for item in inspector.get_unique_constraints("meeting_plugin_operations")}
        assert ("plugin_id", "plugin_version", "media_session_id", "client_request_id") in unique
        for table in NEW_TABLES:
            assert not any(fk["referred_table"] == "plugin_installations" for fk in inspector.get_foreign_keys(table))
        with db.engine.connect() as connection:
            assert connection.execute(sa.text("PRAGMA foreign_key_check")).all() == []
            assert connection.scalar(sa.text("PRAGMA integrity_check")) == "ok"
            for table in NEW_TABLES:
                assert connection.scalar(sa.text(f'SELECT COUNT(*) FROM "{table}"')) == 0
        _migrate(url, "downgrade", "20260828_0026")
        assert snapshot(db.engine) == before
        assert not NEW_TABLES.intersection(sa.inspect(db.engine).get_table_names())
    finally:
        db.dispose()
