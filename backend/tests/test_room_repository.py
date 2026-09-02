from __future__ import annotations

from app.persistence.database import Database
from app.persistence.rooms import RoomRepository
from app.persistence.sessions import SessionRepository


def test_room_repository_manages_room_and_reusable_runs() -> None:
    database = Database("sqlite://")
    database.create_schema()
    try:
        with next(database.sessions()) as db_session:
            rooms = RoomRepository(db_session)
            sessions = SessionRepository(db_session)
            room = rooms.create(
                room_id="room-id",
                room_name="test-room-managed",
                display_name="直播间 A",
            )
            first = sessions.create(
                session_id="first-run",
                room_id=room.id,
                room_name=room.room_name,
                source_type="microphone",
                source_name="浏览器麦克风",
                language="zh-CN",
            )
            sessions.cancel(first.id)
            second = sessions.create(
                session_id="second-run",
                room_id=room.id,
                room_name=room.room_name,
                source_type="file",
                source_name="sample.mp3",
                language="zh-CN",
            )
            db_session.commit()

            assert rooms.list_newest_first() == [room]
            assert sessions.list_for_room(room.id) == [second, first]
            assert sessions.get_active_for_room(room.id) == second
            assert rooms.rename(room.id, "主直播间").display_name == "主直播间"
            closed = rooms.close(room.id)
            assert closed.status == "closed"
            assert closed.closed_at is not None
    finally:
        database.dispose()
