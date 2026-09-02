from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from app.text_processing.models import SourceSegmentSnapshot


class DeepSeekError(RuntimeError):
    """Base class for sanitized DeepSeek failures."""


class DeepSeekConfigurationError(DeepSeekError):
    pass


class DeepSeekRequestError(DeepSeekError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class DeepSeekAuthError(DeepSeekRequestError):
    def __init__(self, message: str = "DeepSeek authentication failed") -> None:
        super().__init__(message, retryable=False)


@dataclass(frozen=True, slots=True)
class StructuredCompletionRequest:
    system_prompt: str
    user_prompt: str
    input_payload: Mapping[str, Any]
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class StructuredCompletionResult:
    content: str
    finish_reason: str | None
    output_tokens: int | None = None


ScriptCompletionResult = StructuredCompletionResult


class StructuredTextProvider(Protocol):
    provider_name: str
    model: str

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult: ...


class ScriptCompletionProvider(Protocol):
    provider_name: str
    model: str

    async def complete(
        self,
        segments: Sequence[SourceSegmentSnapshot],
    ) -> ScriptCompletionResult: ...
