from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from app.worker.health import WorkerHealthStore


def test_worker_health_store_tracks_lifecycle_and_jobs(tmp_path) -> None:
    now = [dt.datetime(2026, 8, 3, 12, 0, tzinfo=dt.UTC)]
    store = WorkerHealthStore(
        tmp_path,
        clock=lambda: now[0],
        process_alive=lambda process_id: process_id == 4242,
    )

    assert store.snapshot().worker_alive is False
    store.mark_worker_started(4242)
    store.mark_livekit_connected()
    now[0] += dt.timedelta(seconds=1)
    store.mark_job_started("job-sensitive-id")

    running = store.snapshot()
    assert running.worker_alive is True
    assert running.livekit_connected is True
    assert running.active_jobs == 1
    assert running.last_job_event_at == now[0]

    now[0] += dt.timedelta(seconds=1)
    store.mark_job_finished("job-sensitive-id")
    assert store.snapshot().active_jobs == 0
    assert store.snapshot().last_job_event_at == now[0]
    store.mark_worker_stopped()
    assert store.snapshot().worker_alive is False
    assert not list((tmp_path / "jobs").glob("*job-sensitive-id*"))


def test_worker_health_is_available_only_on_internal_boundary(
    client: TestClient,
) -> None:
    unauthorized = client.get("/internal/worker/health")
    assert unauthorized.status_code == 401

    response = client.get(
        "/internal/worker/health",
        headers={
            "X-Internal-Control-Token": "dev-internal-control-token",
        },
    )
    assert response.status_code == 200
    assert response.json() == {
        "worker_alive": False,
        "livekit_connected": False,
        "active_jobs": 0,
        "last_job_event_at": None,
    }
