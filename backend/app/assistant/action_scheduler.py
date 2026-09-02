from __future__ import annotations

import asyncio
import datetime as dt

from app.assistant.action_runner import (
    TERMINAL_ACTION_STATUSES,
    ActionRunResult,
    ActionRunRunner,
)


class ActionRunScheduler:
    """FIFO scheduler that advances independent execution IDs fairly."""

    def __init__(
        self,
        runner: ActionRunRunner,
        *,
        concurrency: int = 3,
        resume_delay_seconds: float = 1.0,
    ) -> None:
        if concurrency < 1:
            raise ValueError("Action Run concurrency must be positive")
        if resume_delay_seconds <= 0:
            raise ValueError("resume delay must be positive")
        self._runner = runner
        self._concurrency = concurrency
        self._resume_delay_seconds = resume_delay_seconds
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        self._queued: set[str] = set()
        self._queued_at: dict[str, dt.datetime] = {}
        self._active: set[str] = set()
        self._workers: list[asyncio.Task[None]] = []
        self._delayed: set[asyncio.Task[None]] = set()
        self._waiters: dict[str, list[asyncio.Future[ActionRunResult]]] = {}
        self._results: dict[str, ActionRunResult] = {}
        self._lock = asyncio.Lock()
        self._started = False
        self._stopping = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def queue_depth(self) -> int:
        return len(self._queued)

    @property
    def active_count(self) -> int:
        return len(self._active)

    @property
    def oldest_queued_age_seconds(self) -> float | None:
        if not self._queued_at:
            return None
        oldest = min(self._queued_at.values())
        return max(0.0, (dt.datetime.now(dt.UTC) - oldest).total_seconds())

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        self._workers = [
            asyncio.create_task(
                self._worker(index),
                name=f"assistant-action-worker-{index}",
            )
            for index in range(self._concurrency)
        ]

    async def stop(self, *, grace_seconds: float = 2.0) -> None:
        if not self._started:
            return
        self._stopping = True
        for task in tuple(self._delayed):
            task.cancel()
        if self._delayed:
            await asyncio.gather(*self._delayed, return_exceptions=True)
        try:
            async with asyncio.timeout(grace_seconds):
                await self._queue.join()
        except TimeoutError:
            # In-flight effects retain their durable requesting/unknown claim.
            # Startup recovery must reconcile, never retry a cancelled request.
            pass
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._queue = asyncio.Queue()
        self._queued.clear()
        self._queued_at.clear()
        for waiters in self._waiters.values():
            for waiter in waiters:
                waiter.cancel()
        self._waiters.clear()
        self._workers.clear()
        self._started = False

    async def enqueue(self, execution_id: str) -> None:
        if not self._started or self._stopping:
            raise RuntimeError("Action Run scheduler is not accepting work")
        async with self._lock:
            if execution_id in self._queued or execution_id in self._active:
                return
            existing = self._results.get(execution_id)
            if existing is not None and (
                existing.status in TERMINAL_ACTION_STATUSES
                or existing.status == "needs_input"
            ):
                return
            self._queued.add(execution_id)
            self._queued_at[execution_id] = dt.datetime.now(dt.UTC)
            await self._queue.put(execution_id)

    async def resume(self, execution_id: str) -> None:
        """Re-open a durable NeedsInput run after input was committed."""

        async with self._lock:
            self._results.pop(execution_id, None)
        await self.enqueue(execution_id)

    async def wait(
        self,
        execution_id: str,
        *,
        timeout: float | None = None,
    ) -> ActionRunResult:
        result = self._results.get(execution_id)
        if result is not None and (
            result.status in TERMINAL_ACTION_STATUSES
            or result.status == "needs_input"
        ):
            return result
        future = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(execution_id, []).append(future)
        awaitable = asyncio.shield(future)
        if timeout is None:
            return await awaitable
        async with asyncio.timeout(timeout):
            return await awaitable

    async def _worker(self, index: int) -> None:
        del index
        while True:
            execution_id = await self._queue.get()
            if execution_id is None:
                self._queue.task_done()
                return
            async with self._lock:
                self._queued.discard(execution_id)
                self._queued_at.pop(execution_id, None)
                self._active.add(execution_id)
            try:
                result = await self._runner.run(execution_id)
                self._results[execution_id] = result
                if (
                    result.status in TERMINAL_ACTION_STATUSES
                    or result.status == "needs_input"
                ):
                    self._resolve_waiters(execution_id, result)
                elif result.status in {"waiting_external", "reconciling"}:
                    self._schedule_resume(execution_id)
            finally:
                async with self._lock:
                    self._active.discard(execution_id)
                self._queue.task_done()

    def _schedule_resume(self, execution_id: str) -> None:
        if self._stopping:
            return
        task = asyncio.create_task(
            self._resume_later(execution_id),
            name=f"assistant-action-resume-{execution_id}",
        )
        self._delayed.add(task)
        task.add_done_callback(self._delayed.discard)

    async def _resume_later(self, execution_id: str) -> None:
        await asyncio.sleep(self._resume_delay_seconds)
        if not self._stopping:
            await self.enqueue(execution_id)

    def _resolve_waiters(
        self,
        execution_id: str,
        result: ActionRunResult,
    ) -> None:
        for future in self._waiters.pop(execution_id, []):
            if not future.done():
                future.set_result(result)


__all__ = ["ActionRunScheduler"]
