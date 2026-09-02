from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import psutil


@dataclass(frozen=True, slots=True)
class WorkerHealthSnapshot:
    worker_alive: bool
    livekit_connected: bool
    active_jobs: int
    last_job_event_at: dt.datetime | None


class WorkerHealthStore:
    """Small file-backed bridge between Worker processes and FastAPI."""

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], dt.datetime] | None = None,
        process_alive: Callable[[int], bool] | None = None,
    ) -> None:
        self.root = Path(root)
        self._jobs_dir = self.root / "jobs"
        self._worker_state_path = self.root / "worker.json"
        self._last_job_event_path = self.root / "last-job-event.txt"
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._process_alive = process_alive or psutil.pid_exists

    def mark_worker_started(self, process_id: int | None = None) -> None:
        self._ensure_directories()
        for marker in self._jobs_dir.glob("*.json"):
            marker.unlink(missing_ok=True)
        self._last_job_event_path.unlink(missing_ok=True)
        self._write_json(
            self._worker_state_path,
            {
                "process_id": process_id or os.getpid(),
                "livekit_connected": False,
                "started_at": self._now().isoformat(),
            },
        )

    def mark_livekit_connected(self) -> None:
        state = self._read_json(self._worker_state_path)
        if state is None:
            return
        state["livekit_connected"] = True
        state["connected_at"] = self._now().isoformat()
        self._write_json(self._worker_state_path, state)

    def mark_worker_stopped(self) -> None:
        state = self._read_json(self._worker_state_path) or {}
        state["livekit_connected"] = False
        state["stopped_at"] = self._now().isoformat()
        self._write_json(self._worker_state_path, state)

    def mark_job_started(self, job_id: str) -> None:
        self._ensure_directories()
        occurred_at = self._now()
        self._write_json(
            self._job_path(job_id),
            {"job_id": job_id, "started_at": occurred_at.isoformat()},
        )
        self._write_text(self._last_job_event_path, occurred_at.isoformat())

    def mark_job_finished(self, job_id: str) -> None:
        self._ensure_directories()
        self._job_path(job_id).unlink(missing_ok=True)
        self._write_text(
            self._last_job_event_path,
            self._now().isoformat(),
        )

    def snapshot(self) -> WorkerHealthSnapshot:
        state = self._read_json(self._worker_state_path) or {}
        process_id = state.get("process_id")
        worker_alive = (
            isinstance(process_id, int)
            and process_id > 0
            and self._process_alive(process_id)
            and "stopped_at" not in state
        )
        active_jobs = (
            sum(1 for _ in self._jobs_dir.glob("*.json"))
            if worker_alive and self._jobs_dir.is_dir()
            else 0
        )
        return WorkerHealthSnapshot(
            worker_alive=worker_alive,
            livekit_connected=(
                worker_alive and state.get("livekit_connected") is True
            ),
            active_jobs=active_jobs,
            last_job_event_at=self._read_last_job_event(),
        )

    def _job_path(self, job_id: str) -> Path:
        digest = hashlib.sha256(job_id.encode("utf-8")).hexdigest()
        return self._jobs_dir / f"{digest}.json"

    def _read_last_job_event(self) -> dt.datetime | None:
        try:
            raw_value = self._last_job_event_path.read_text(
                encoding="utf-8"
            ).strip()
            return dt.datetime.fromisoformat(raw_value)
        except (OSError, ValueError):
            return None

    def _ensure_directories(self) -> None:
        self._jobs_dir.mkdir(parents=True, exist_ok=True)

    def _now(self) -> dt.datetime:
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)

    @staticmethod
    def _read_json(path: Path) -> dict[str, object] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _write_json(self, path: Path, payload: dict[str, object]) -> None:
        self._write_text(
            path,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    def _write_text(self, path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        temporary.write_text(value, encoding="utf-8")
        os.replace(temporary, path)
