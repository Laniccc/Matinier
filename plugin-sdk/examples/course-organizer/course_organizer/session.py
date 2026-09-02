from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from .finalizer import CourseFinalizer, FinalizationResult
from .language import localize_notes, validate_language
from .models import RealtimeNote, TranscriptFragment
from .realtime import (
    RealtimeConfig,
    bound_notes,
    build_window_evidence,
    fallback_notes,
    model_input,
    parse_model_response,
    window_ready,
)
from .view import build_course_view
from .version import PLUGIN_VERSION


class CapabilityClient(Protocol):
    async def capability(
        self,
        *,
        name: str,
        session_scope: str,
        input_value: dict[str, object],
        idempotency_key: str | None = None,
    ) -> dict[str, object]: ...


TaskFactory = Callable[..., asyncio.Task[Any]]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class PendingEvent:
    stream_key: str
    event_type: str
    logical_id: str
    sequence: int
    revision: int
    text: str
    start_ms: int
    end_ms: int
    language: str
    confidence: float | None
    source_segment_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.stream_key or not self.logical_id or not self.text:
            raise ValueError("pending event identity and text are required")
        if self.event_type not in {"transcript.final", "translation.final"}:
            raise ValueError("unsupported pending event type")
        if self.sequence < 1 or self.revision < 1:
            raise ValueError("pending event sequence/revision must be positive")
        if self.start_ms < 0 or self.end_ms < self.start_ms:
            raise ValueError("invalid pending event time range")
        if (any(not isinstance(item, str) or not item for item in self.source_segment_ids)
                or len(set(self.source_segment_ids)) != len(self.source_segment_ids)):
            raise ValueError("invalid source Segment IDs")

    def to_dict(self) -> dict[str, object]:
        return {
            "stream_key": self.stream_key,
            "event_type": self.event_type,
            "logical_id": self.logical_id,
            "sequence": self.sequence,
            "revision": self.revision,
            "text": self.text,
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "language": self.language,
            "confidence": self.confidence,
            "source_segment_ids": list(self.source_segment_ids),
        }

    @classmethod
    def from_dict(cls, value: object) -> "PendingEvent":
        if not isinstance(value, dict):
            raise ValueError("invalid pending event state")
        return cls(
            stream_key=str(value["stream_key"]),
            event_type=str(value["event_type"]),
            logical_id=str(value["logical_id"]),
            sequence=int(value["sequence"]),
            revision=int(value["revision"]),
            text=str(value["text"]),
            start_ms=int(value["start_ms"]),
            end_ms=int(value["end_ms"]),
            language=str(value["language"]),
            confidence=(
                float(value["confidence"])
                if value.get("confidence") is not None
                else None
            ),
            source_segment_ids=tuple(value.get("source_segment_ids", _legacy_source_ids(
                str(value["event_type"]), str(value["logical_id"])
            ))),
        )


@dataclass(slots=True)
class CourseSessionState:
    last_sequence: int = 0
    state_version: int = 0
    view_version: int = 1
    output_language: str = "zh-CN"
    status: str = "ready"
    pending: tuple[PendingEvent, ...] = ()
    notes: tuple[RealtimeNote, ...] = ()
    final_document_markdown: str = ""
    history: tuple[dict[str, object], ...] = ()
    final_status: str = "idle"
    final_error: str | None = None
    terminal_trigger: str | None = None
    terminal_job_id: str | None = None
    terminal_final_sequence: int = 0
    language_explicit: bool = False
    source_language: str | None = None
    translation_languages: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "last_sequence": self.last_sequence,
            "view_version": self.view_version,
            "output_language": self.output_language,
            "status": self.status,
            "pending": [item.to_dict() for item in self.pending],
            "notes": [_note_to_dict(item) for item in self.notes],
            "final_document_markdown": self.final_document_markdown,
            "history": list(self.history[-50:]),
            "final_status": self.final_status,
            "final_error": self.final_error,
            "terminal_trigger": self.terminal_trigger,
            "terminal_job_id": self.terminal_job_id,
            "terminal_final_sequence": self.terminal_final_sequence,
            "language_explicit": self.language_explicit,
            "source_language": self.source_language,
            "translation_languages": list(self.translation_languages),
        }

    @classmethod
    def from_dict(
        cls,
        value: object,
        *,
        state_version: int,
        max_notes: int,
    ) -> "CourseSessionState":
        if value is None:
            return cls(state_version=state_version)
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("unsupported course session state")
        pending_raw = value.get("pending", [])
        notes_raw = value.get("notes", [])
        history_raw = value.get("history", [])
        if not isinstance(pending_raw, list) or not isinstance(notes_raw, list):
            raise ValueError("invalid course session collection state")
        if not isinstance(history_raw, list):
            raise ValueError("invalid course history state")
        return cls(
            last_sequence=int(value.get("last_sequence", 0)),
            state_version=state_version,
            view_version=max(1, int(value.get("view_version", 1))),
            output_language=str(value.get("output_language", "zh-CN")),
            status=str(value.get("status", "ready")),
            pending=tuple(PendingEvent.from_dict(item) for item in pending_raw),
            notes=tuple(_note_from_dict(item) for item in notes_raw[-max_notes:]),
            final_document_markdown=str(value.get("final_document_markdown", "")),
            history=tuple(dict(item) for item in history_raw[-50:] if isinstance(item, dict)),
            final_status=str(value.get("final_status", "idle")),
            final_error=(
                str(value["final_error"])
                if value.get("final_error") is not None
                else None
            ),
            terminal_trigger=(
                str(value["terminal_trigger"])
                if value.get("terminal_trigger") is not None
                else None
            ),
            terminal_job_id=(
                str(value["terminal_job_id"])
                if value.get("terminal_job_id") is not None
                else None
            ),
            terminal_final_sequence=int(value.get("terminal_final_sequence", 0)),
            language_explicit=bool(value.get("language_explicit", False)),
            source_language=(
                str(value["source_language"])
                if value.get("source_language") is not None
                else None
            ),
            translation_languages=tuple(
                str(item) for item in value.get("translation_languages", [])
            ),
        )


class CourseSession:
    def __init__(
        self,
        capabilities: CapabilityClient,
        *,
        create_task: TaskFactory | None = None,
        sleep: Sleep = asyncio.sleep,
        config: RealtimeConfig | None = None,
        final_retry_delays: tuple[int, ...] = (2, 10, 30),
    ) -> None:
        self._capabilities = capabilities
        self._create_task = create_task or _default_task_factory
        self._sleep = sleep
        self.config = config or RealtimeConfig()
        if any(item <= 0 for item in final_retry_delays):
            raise ValueError("final retry delays must be positive")
        self._final_retry_delays = final_retry_delays
        self.state = CourseSessionState()
        self._scope: str | None = None
        self._media_session_id: str | None = None
        self._lock = asyncio.Lock()
        self._background_task: asyncio.Task[Any] | None = None
        self._final_tasks: dict[str, asyncio.Task[Any]] = {}
        self._final_job_ids: dict[str, str] = {}
        self._terminal_task: asyncio.Task[Any] | None = None
        self._language_task: asyncio.Task[Any] | None = None
        self._closed = False

    @property
    def background_done(self) -> bool:
        return self._background_task is None or self._background_task.done()

    async def open(self, params: dict[str, object]) -> dict[str, object]:
        scope = params.get("session_scope")
        media_session_id = params.get("media_session_id")
        after_sequence = params.get("after_sequence", 0)
        if not isinstance(scope, str) or not isinstance(media_session_id, str):
            raise ValueError("invalid session.open parameters")
        if not isinstance(after_sequence, int) or after_sequence < 0:
            raise ValueError("invalid session cursor")
        self._scope = scope
        self._media_session_id = media_session_id
        restored = await self._capabilities.capability(
            name="state.get",
            session_scope=scope,
            input_value={"key": "course-session"},
        )
        version = restored.get("version", 0)
        if not isinstance(version, int):
            raise ValueError("Host returned an invalid state version")
        self.state = CourseSessionState.from_dict(
            restored.get("value"),
            state_version=version,
            max_notes=self.config.max_notes,
        )
        self.state.last_sequence = max(self.state.last_sequence, after_sequence)
        if self.state.status == "processing":
            self.state.status = "degraded"
        await self._publish_view()
        if self.state.terminal_job_id is not None and self.state.terminal_trigger is not None:
            self._schedule_terminal()
        elif self.state.pending and self._window_ready() and self._background_task is None:
            self.state.status = "processing"
            await self._put_state()
            self._schedule(tuple(self.state.pending))
        return {"opened": True}

    async def event_batch(self, params: dict[str, object]) -> dict[str, object]:
        self._require_scope(params.get("session_scope"))
        events = params.get("events")
        if not isinstance(events, list):
            raise ValueError("event.batch events must be an array")
        schedule: tuple[PendingEvent, ...] | None = None
        schedule_terminal = False
        async with self._lock:
            changed = False
            for raw_event in sorted(events, key=_event_sequence):
                if not isinstance(raw_event, dict):
                    raise ValueError("invalid MediaEvent")
                sequence = _event_sequence(raw_event)
                if sequence <= self.state.last_sequence:
                    continue
                self.state.last_sequence = sequence
                changed = True
                if raw_event.get("event_type") in {
                    "transcript.final",
                    "translation.final",
                }:
                    pending = _pending_event(raw_event)
                    self._merge_pending(pending)
                    self._observe_language(pending)
                if raw_event.get("event_type") in {
                    "session.completed",
                    "session.failed",
                    "session.cancelled",
                }:
                    trigger = str(raw_event["event_type"]).replace(".", "_")
                    job_id = _terminal_job_id(
                        self._media_session_id or "unknown",
                        trigger,
                        self.state.last_sequence,
                        self.state.output_language,
                    )
                    if self.state.terminal_job_id is None:
                        self.state.terminal_trigger = trigger
                        self.state.terminal_job_id = job_id
                        self.state.terminal_final_sequence = self.state.last_sequence
                        self.state.final_status = "generating"
                        self.state.final_error = None
                    schedule_terminal = self._terminal_task is None
            should_schedule = (
                changed
                and self._background_task is None
                and self._window_ready()
                and not schedule_terminal
            )
            if should_schedule:
                self.state.status = "processing"
                schedule = tuple(self.state.pending)
            if changed:
                await self._put_state()
        if schedule is not None:
            self._schedule(schedule)
        if schedule_terminal:
            self._schedule_terminal()
        return {"acknowledged_sequence": self.state.last_sequence}

    async def heartbeat(self, _params: dict[str, object]) -> dict[str, object]:
        return {"ok": True, "status": self.state.status}

    async def wait_for_idle(self, *, timeout: float = 2.0) -> None:
        task = self._background_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def wait_for_final_idle(self, *, timeout: float = 3.0) -> None:
        tasks = [item for item in self._final_tasks.values() if not item.done()]
        if tasks:
            await asyncio.wait_for(
                asyncio.gather(*(asyncio.shield(item) for item in tasks)),
                timeout=timeout,
            )

    async def wait_for_terminal_idle(self, *, timeout: float = 3.0) -> None:
        task = self._terminal_task
        if task is not None:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout)

    async def command(self, params: dict[str, object]) -> dict[str, object]:
        self._require_scope(params.get("session_scope"))
        command = params.get("command")
        command_id = params.get("command_id")
        values = params.get("values", {})
        if not isinstance(command, str) or not isinstance(command_id, str):
            raise ValueError("invalid course command")
        if not isinstance(values, dict):
            raise ValueError("invalid course command values")
        if command == "retry_final" and self.state.terminal_trigger is not None:
            if self._terminal_task is not None and not self._terminal_task.done():
                return {"accepted": True, "job_id": self.state.terminal_job_id}
            self.state.terminal_job_id = command_id
            self.state.final_status = "generating"
            self.state.final_error = None
            self.state.view_version += 1
            await self._put_state()
            await self._publish_view()
            self._schedule_terminal()
            return {"accepted": True, "job_id": command_id}
        if command in {"generate_final", "retry_final"}:
            return await self._start_manual_final(command_id)
        if command == "set_language":
            language = values.get("output_language")
            if not isinstance(language, str):
                raise ValueError("output language is required")
            return await self._start_language_change(command_id, language)
        if command == "select_version":
            return {"accepted": True, "job_id": command_id}
        raise ValueError("unknown course command")

    async def close(self) -> None:
        self._closed = True
        tasks = {
            item
            for item in (
                self._background_task,
                self._terminal_task,
                self._language_task,
                *self._final_tasks.values(),
            )
            if item is not None and not item.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _schedule(self, snapshot: tuple[PendingEvent, ...]) -> None:
        task = self._create_task(
            self._process_window(snapshot),
            name=f"course-realtime-{self._media_session_id}",
        )
        self._background_task = task
        task.add_done_callback(self._clear_background_task)

    def _clear_background_task(self, task: asyncio.Task[Any]) -> None:
        if self._background_task is task:
            self._background_task = None

    def _schedule_terminal(self) -> None:
        if self._terminal_task is not None and not self._terminal_task.done():
            return
        self._terminal_task = self._create_task(
            self._run_terminal(),
            name=f"course-terminal-{self._media_session_id}",
        )

    async def _start_manual_final(self, command_id: str) -> dict[str, object]:
        language = self.state.output_language
        current = self._final_tasks.get(language)
        if current is not None and not current.done():
            return {"accepted": True, "job_id": self._final_job_ids[language]}
        self.state.final_status = "generating"
        self.state.final_error = None
        self.state.view_version += 1
        await self._put_state()
        await self._publish_view()
        self._final_job_ids[language] = command_id
        self._final_tasks[language] = self._create_task(
            self._run_finalization(
                job_id=command_id,
                trigger="manual",
                final_sequence=self.state.last_sequence,
                language=language,
            ),
            name=f"course-final-{self._media_session_id}-{language}",
        )
        return {"accepted": True, "job_id": command_id}

    async def _start_language_change(
        self,
        command_id: str,
        language: str,
    ) -> dict[str, object]:
        if language == "source":
            if self.state.source_language is None:
                raise ValueError("source language is not available yet")
            target = validate_language(self.state.source_language)
        else:
            target = validate_language(language)
        if self._language_task is not None and not self._language_task.done():
            return {"accepted": True, "job_id": self._language_task.get_name()}
        self._language_task = self._create_task(
            self._run_language_change(target),
            name=command_id,
        )
        return {"accepted": True, "job_id": command_id}

    async def _run_language_change(self, target: str) -> None:
        try:
            localized = await localize_notes(
                self._capabilities,
                session_scope=self._scope_value(),
                notes=self.state.notes,
                target_language=target,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            return
        async with self._lock:
            self.state.notes = localized
            self.state.output_language = target
            self.state.language_explicit = True
            self.state.view_version += 1
            await self._put_state()
            await self._publish_view()

    async def _run_terminal(self) -> None:
        realtime_task = self._background_task
        if realtime_task is not None and not realtime_task.done():
            await realtime_task
        if self.state.pending:
            await self._process_window(tuple(self.state.pending))
        job_id = self.state.terminal_job_id
        trigger = self.state.terminal_trigger
        if job_id is None or trigger is None:
            return
        await self._run_finalization(
            job_id=job_id,
            trigger=trigger,
            final_sequence=self.state.terminal_final_sequence,
            language=self.state.output_language,
        )

    async def _run_finalization(
        self,
        *,
        job_id: str,
        trigger: str,
        final_sequence: int,
        language: str,
    ) -> None:
        delays: tuple[int | None, ...] = (None, *self._final_retry_delays)
        for attempt, delay in enumerate(delays):
            if delay is not None:
                await self._sleep(delay)
            try:
                result = await CourseFinalizer(
                    self._capabilities,
                    session_scope=self._scope_value(),
                    output_language=language,
                    realtime_notes=self.state.notes,
                ).run(
                    job_id=job_id,
                    trigger=trigger,
                    final_sequence=final_sequence,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                await self._record_final_failure(error)
                if attempt + 1 < len(delays):
                    continue
                return
            await self._record_final_success(result, terminal_job_id=job_id)
            return

    async def _record_final_failure(self, error: Exception) -> None:
        async with self._lock:
            self.state.final_status = "waiting_retry"
            self.state.final_error = (
                "当前课程没有可用于最终整理的字幕。"
                if "no Final captions" in str(error)
                else "最终整理暂时失败，可稍后重试。"
            )
            self.state.view_version += 1
            await self._put_state()
            await self._publish_view()

    async def _record_final_success(
        self,
        result: FinalizationResult,
        *,
        terminal_job_id: str,
    ) -> None:
        async with self._lock:
            entry = {
                "document_id": result.document_id,
                "document_version": result.document_version,
                "identity_key": result.identity_key,
                "language": result.language,
                "trigger": result.trigger,
                "completeness": result.completeness,
                "content_hash": result.content_hash,
                "label": (
                    f"{result.language} · v{result.document_version} · "
                    f"{result.completeness}"
                ),
            }
            self.state.history = (*self.state.history, entry)[-50:]
            self.state.final_document_markdown = result.markdown
            self.state.final_status = "ready"
            self.state.final_error = None
            if self.state.terminal_job_id == terminal_job_id:
                self.state.terminal_job_id = None
                self.state.terminal_trigger = None
            self.state.view_version += 1
            await self._put_state()
            await self._publish_view()

    async def _process_window(self, snapshot: tuple[PendingEvent, ...]) -> None:
        fragments = _preferred_fragments(snapshot, self.state.output_language)
        evidence = build_window_evidence(fragments)
        fallback_ids: frozenset[str] = frozenset()
        try:
            notes = await self._invoke_model(evidence)
        except Exception:
            notes = fallback_notes(evidence, output_language=self.state.output_language)
            fallback_ids = frozenset(item.id for item in notes)
            await self._commit_window(
                snapshot,
                notes,
                status="degraded",
                replace_ids=frozenset(),
            )
            for delay in self.config.retry_delays_seconds:
                try:
                    await self._sleep(delay)
                    repaired = await self._invoke_model(evidence)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    continue
                await self._commit_window(
                    (),
                    repaired,
                    status="ready",
                    replace_ids=fallback_ids,
                )
                return
            return
        await self._commit_window(
            snapshot,
            notes,
            status="ready",
            replace_ids=frozenset(),
        )

    async def _invoke_model(
        self,
        evidence,
    ) -> tuple[RealtimeNote, ...]:
        if not evidence:
            return ()
        response = await self._capabilities.capability(
            name="model.invoke",
            session_scope=self._scope_value(),
            input_value=model_input(evidence, output_language=self.state.output_language),
        )
        return bound_notes(parse_model_response(response, evidence=evidence))

    async def _commit_window(
        self,
        snapshot: tuple[PendingEvent, ...],
        notes: tuple[RealtimeNote, ...],
        *,
        status: str,
        replace_ids: frozenset[str],
    ) -> None:
        async with self._lock:
            snapshot_revisions = {item.stream_key: item.revision for item in snapshot}
            self.state.pending = tuple(
                item
                for item in self.state.pending
                if item.stream_key not in snapshot_revisions
                or item.revision > snapshot_revisions[item.stream_key]
            )
            combined = [item for item in self.state.notes if item.id not in replace_ids]
            by_id = {item.id: item for item in combined}
            for item in notes:
                by_id[item.id] = item
            ordered = sorted(by_id.values(), key=lambda item: (item.start_ms, item.end_ms, item.id))
            self.state.notes = tuple(ordered[-self.config.max_notes :])
            self.state.status = status
            self.state.view_version += 1
            await self._put_state()
            await self._publish_view()

    def _merge_pending(self, item: PendingEvent) -> None:
        by_key = {value.stream_key: value for value in self.state.pending}
        existing = by_key.get(item.stream_key)
        if existing is None or item.revision > existing.revision:
            by_key[item.stream_key] = item
        self.state.pending = tuple(
            sorted(by_key.values(), key=lambda value: (value.start_ms, value.stream_key))
        )

    def _observe_language(self, item: PendingEvent) -> None:
        if item.event_type == "transcript.final":
            self.state.source_language = item.language
            if not self.state.language_explicit and not self.state.translation_languages:
                self.state.output_language = item.language
            return
        languages = list(self.state.translation_languages)
        if item.language not in languages:
            languages.append(item.language)
            self.state.translation_languages = tuple(languages)
        if not self.state.language_explicit:
            self.state.output_language = self.state.translation_languages[0]

    def _window_ready(self) -> bool:
        return window_ready(
            _preferred_fragments(self.state.pending, self.state.output_language),
            self.config,
        )

    async def _put_state(self) -> None:
        result = await self._capabilities.capability(
            name="state.put",
            session_scope=self._scope_value(),
            input_value={
                "key": "course-session",
                "value": self.state.to_dict(),
                "expected_version": self.state.state_version,
            },
        )
        version = result.get("version")
        if not isinstance(version, int) or version <= self.state.state_version:
            raise ValueError("Host returned an invalid state version")
        self.state.state_version = version

    async def _publish_view(self) -> None:
        view = build_course_view(self.state)
        await self._capabilities.capability(
            name="ui.publish",
            session_scope=self._scope_value(),
            input_value={
                "surface": view["surface"],
                "view_id": view["view_id"],
                "view_version": view["view_version"],
                "view": view["root"],
                "actions": view["actions"],
            },
        )

    def _require_scope(self, scope: object) -> None:
        if scope != self._scope:
            raise ValueError("session scope mismatch")

    def _scope_value(self) -> str:
        if self._scope is None:
            raise RuntimeError("course session is not open")
        return self._scope


def _event_sequence(value: object) -> int:
    if not isinstance(value, dict):
        raise ValueError("invalid MediaEvent")
    sequence = value.get("sequence")
    if not isinstance(sequence, int) or sequence < 1:
        raise ValueError("invalid MediaEvent sequence")
    return sequence


def _pending_event(value: dict[str, object]) -> PendingEvent:
    event_type = value.get("event_type")
    logical_id = value.get("logical_id")
    revision = value.get("revision")
    start_ms = value.get("media_time_ms")
    duration_ms = value.get("duration_ms", 0)
    payload = value.get("payload")
    if (
        event_type not in {"transcript.final", "translation.final"}
        or not isinstance(logical_id, str)
        or not isinstance(revision, int)
        or not isinstance(start_ms, int)
        or not isinstance(duration_ms, int)
        or not isinstance(payload, dict)
    ):
        raise ValueError("invalid Final MediaEvent")
    text = payload.get("text")
    language = payload.get("language")
    confidence = payload.get("confidence")
    if not isinstance(text, str) or not text.strip() or not isinstance(language, str):
        raise ValueError("Final MediaEvent has no text/language")
    if confidence is not None and not isinstance(confidence, int | float):
        raise ValueError("Final MediaEvent confidence is invalid")
    stream_key = f"{event_type}:{logical_id}:{language}"
    source_ids = payload.get("source_segment_ids")
    if source_ids is None:
        evidence = payload.get("evidence")
        source_id = evidence.get("segment_id") if isinstance(evidence, dict) else None
        source_ids = ([source_id] if event_type == "transcript.final" and isinstance(source_id, str)
                      else list(_legacy_source_ids(event_type, logical_id)))
    if not isinstance(source_ids, list):
        raise ValueError("Final MediaEvent source Segment IDs are invalid")
    return PendingEvent(
        stream_key=stream_key,
        event_type=event_type,
        logical_id=logical_id,
        sequence=_event_sequence(value),
        revision=revision,
        text=text.strip()[:12_000],
        start_ms=max(0, start_ms),
        end_ms=max(0, start_ms) + max(0, duration_ms),
        language=language,
        confidence=float(confidence) if confidence is not None else None,
        source_segment_ids=tuple(source_ids),
    )


def _legacy_source_ids(event_type: str, logical_id: str) -> tuple[str, ...]:
    if event_type == "transcript.final":
        return (logical_id.removeprefix("transcript:"),)
    if logical_id.startswith("translation:"):
        return ()
    return (logical_id,)


def _preferred_fragments(
    pending: tuple[PendingEvent, ...],
    output_language: str,
) -> tuple[TranscriptFragment, ...]:
    grouped: dict[tuple[str, ...], list[PendingEvent]] = {}
    for item in pending:
        if item.source_segment_ids:
            grouped.setdefault(item.source_segment_ids, []).append(item)
    selected: list[PendingEvent] = []
    for values in grouped.values():
        translations = [
            item
            for item in values
            if item.event_type == "translation.final"
            and item.language == output_language
        ]
        sources = [item for item in values if item.event_type == "transcript.final"]
        candidates = translations or sources or values
        selected.append(max(candidates, key=lambda item: (item.revision, item.sequence)))
    translated_sources = {source_id for item in selected
                          if item.event_type == "translation.final" and item.language == output_language
                          for source_id in item.source_segment_ids}
    selected = [item for item in selected if item.event_type != "transcript.final"
                or not set(item.source_segment_ids).issubset(translated_sources)]
    return tuple(
        TranscriptFragment(
            segment_id=item.logical_id,
            revision=item.revision,
            text=item.text,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            language=item.language,
            confidence=item.confidence,
            source_segment_ids=item.source_segment_ids,
        )
        for item in sorted(selected, key=lambda value: (value.start_ms, value.logical_id))
    )


def _note_to_dict(item: RealtimeNote) -> dict[str, object]:
    return {
        "id": item.id,
        "note_type": item.note_type,
        "title": item.title,
        "body": item.body,
        "start_ms": item.start_ms,
        "end_ms": item.end_ms,
        "source_segment_ids": list(item.source_segment_ids),
        "evidence_item_ids": list(item.evidence_item_ids),
        "confidence_status": item.confidence_status,
        "language": item.language,
        "related_note_ids": list(item.related_note_ids),
    }


def _note_from_dict(value: object) -> RealtimeNote:
    if not isinstance(value, dict):
        raise ValueError("invalid realtime note state")
    return RealtimeNote(
        id=str(value["id"]),
        note_type=str(value["note_type"]),  # type: ignore[arg-type]
        title=str(value["title"]),
        body=str(value["body"]),
        start_ms=int(value["start_ms"]),
        end_ms=int(value["end_ms"]),
        source_segment_ids=tuple(str(item) for item in value["source_segment_ids"]),
        evidence_item_ids=tuple(str(item) for item in value["evidence_item_ids"]),
        confidence_status=str(value["confidence_status"]),  # type: ignore[arg-type]
        language=str(value["language"]),
        related_note_ids=tuple(str(item) for item in value["related_note_ids"]),
    )


def _default_task_factory(awaitable, *, name: str):
    return asyncio.create_task(awaitable, name=name)


def _terminal_job_id(
    media_session_id: str,
    trigger: str,
    final_sequence: int,
    language: str,
) -> str:
    digest = hashlib.sha256(
        f"{PLUGIN_VERSION}\x1f{media_session_id}\x1f{trigger}\x1f{final_sequence}\x1f{language}".encode(
            "utf-8"
        )
    ).hexdigest()[:24]
    return f"terminal-{digest}"


__all__ = ["CourseSession", "CourseSessionState", "PendingEvent"]
