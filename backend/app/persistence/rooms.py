from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.persistence.models import RoomRecord, utc_now


class RoomRepository:
    def __init__(self, db_session: Session) -> None:
        self._db_session = db_session

    def create(
        self,
        *,
        room_name: str,
        display_name: str,
        room_id: str | None = None,
        created_at: dt.datetime | None = None,
    ) -> RoomRecord:
        if not room_name:
            raise ValueError("room_name is required")
        if not display_name:
            raise ValueError("display_name is required")
        timestamp = created_at or utc_now()
        record = RoomRecord(
            id=room_id or str(uuid.uuid4()),
            room_name=room_name,
            display_name=display_name,
            status="ready",
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._db_session.add(record)
        self._db_session.flush()
        return record

    def get(self, room_id: str) -> RoomRecord | None:
        return self._db_session.get(RoomRecord, room_id)

    def get_required(self, room_id: str) -> RoomRecord:
        record = self.get(room_id)
        if record is None:
            raise LookupError(f"Room not found: {room_id}")
        return record

    def list_newest_first(self) -> list[RoomRecord]:
        return list(
            self._db_session.scalars(
                select(RoomRecord).order_by(
                    RoomRecord.created_at.desc(),
                    RoomRecord.id.desc(),
                )
            )
        )

    def rename(self, room_id: str, display_name: str) -> RoomRecord:
        if not display_name:
            raise ValueError("display_name is required")
        record = self.get_required(room_id)
        record.display_name = display_name
        record.updated_at = utc_now()
        self._db_session.flush()
        return record

    def close(self, room_id: str) -> RoomRecord:
        record = self.get_required(room_id)
        if record.status == "closed":
            return record
        timestamp = utc_now()
        record.status = "closed"
        record.closed_at = timestamp
        record.updated_at = timestamp
        self._db_session.flush()
        return record
