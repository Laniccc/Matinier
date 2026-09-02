from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol


class AudioFrameSink(Protocol):
    def send_frame(self, pcm: bytes | bytearray | memoryview) -> None: ...


FailureHandler = Callable[[BaseException], Awaitable[None]]
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _OptionalRoute:
    name: str
    sink: AudioFrameSink
    on_failure: FailureHandler
    active: bool = True


class AudioRouter:
    """Fan one PCM frame into a required source route and isolated auxiliaries."""

    def __init__(self, source: AudioFrameSink) -> None:
        self._source = source
        self._optional: list[_OptionalRoute] = []

    def add_optional(
        self,
        name: str,
        sink: AudioFrameSink,
        *,
        on_failure: FailureHandler,
    ) -> None:
        if not name:
            raise ValueError("route name is required")
        self._optional.append(
            _OptionalRoute(
                name=name,
                sink=sink,
                on_failure=on_failure,
            )
        )

    async def route(
        self,
        pcm: bytes | bytearray | memoryview,
    ) -> None:
        # Source ASR is authoritative: its backpressure or transport failure is
        # fatal to the caption Session and must remain visible to the caller.
        self._source.send_frame(pcm)
        for route in self._optional:
            if not route.active:
                continue
            try:
                route.sink.send_frame(pcm)
            except asyncio.CancelledError:
                raise
            except BaseException as error:
                route.active = False
                try:
                    await route.on_failure(error)
                except asyncio.CancelledError:
                    raise
                except BaseException as report_error:
                    logger.error(
                        "optional audio route failure reporting failed",
                        extra={
                            "process_name": "worker",
                            "event": "optional_audio_route_report_failed",
                            "route_name": route.name,
                            "internal_error_type": type(report_error).__name__,
                        },
                    )

    def is_optional_active(self, name: str) -> bool:
        return any(
            route.name == name and route.active
            for route in self._optional
        )
