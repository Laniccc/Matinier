from __future__ import annotations

import os
import pytest


os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "test-key")
os.environ.setdefault("LIVEKIT_API_SECRET", "test-secret-that-is-at-least-32-bytes")
os.environ.setdefault("LIVEKIT_ROOM_NAME", "test-room")


@pytest.fixture
def meeting_history(tmp_path):
    from app.persistence.database import Database
    from meeting_plugin_fakes import seed_meeting_history

    database = Database(f"sqlite:///{(tmp_path / 'meeting-history.db').as_posix()}")
    database.create_schema()
    try:
        yield seed_meeting_history(database)
    finally:
        database.dispose()


@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.persistence.database import Database
    from app.settings import Settings

    database_url = "sqlite://"
    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        database_url=database_url,
        data_dir=tmp_path / "data",
        log_level="DEBUG",
    )
    database = Database(database_url)
    database.create_schema()

    with TestClient(create_app(settings=settings, database=database)) as test_client:
        yield test_client

    database.dispose()
