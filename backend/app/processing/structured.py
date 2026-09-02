from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import TypeVar

from app.text_processing.parser import ScriptOutputError
from app.text_processing.provider import (
    DeepSeekRequestError,
    StructuredCompletionRequest,
    StructuredTextProvider,
)


ItemT = TypeVar("ItemT")
ResultT = TypeVar("ResultT")


def chunk_items(
    items: Sequence[ItemT],
    *,
    text_of: Callable[[ItemT], str],
    max_items: int,
    max_chars: int,
) -> tuple[tuple[ItemT, ...], ...]:
    """Split ordered items without rewriting or dropping an oversized item."""

    if max_items <= 0 or max_chars <= 0:
        raise ValueError("structured workflow chunk limits must be positive")
    chunks: list[tuple[ItemT, ...]] = []
    current: list[ItemT] = []
    current_chars = 0
    for item in items:
        item_chars = len(text_of(item))
        exceeds_count = len(current) >= max_items
        exceeds_chars = bool(current) and current_chars + item_chars > max_chars
        if exceeds_count or exceeds_chars:
            chunks.append(tuple(current))
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


async def complete_and_parse(
    provider: StructuredTextProvider,
    request: StructuredCompletionRequest,
    *,
    parse: Callable[[str], ResultT],
    max_retries: int,
    retry_delay_seconds: float,
) -> ResultT:
    """Apply the shared retry contract to provider and strict parse failures."""

    if max_retries < 0 or retry_delay_seconds < 0:
        raise ValueError("structured workflow retry settings must be non-negative")
    for attempt in range(max_retries + 1):
        try:
            completion = await provider.complete_structured(request)
            if completion.finish_reason == "length":
                raise ScriptOutputError(
                    "structured provider JSON output was truncated"
                )
            return parse(completion.content)
        except ScriptOutputError:
            if attempt >= max_retries:
                raise
        except DeepSeekRequestError as error:
            if not error.retryable or attempt >= max_retries:
                raise
        if retry_delay_seconds:
            await asyncio.sleep(retry_delay_seconds * (2**attempt))
    raise AssertionError("unreachable structured completion retry state")
