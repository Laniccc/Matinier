from __future__ import annotations

import json
from collections.abc import Mapping

from app.media.contracts import validate_bounded_json
from app.plugins.broker import CapabilityExecutionContext
from app.plugins.capabilities import ModelInvokeInput, ModelInvokeOutput
from app.text_processing.provider import (
    DeepSeekAuthError,
    DeepSeekConfigurationError,
    DeepSeekRequestError,
    StructuredCompletionRequest,
    StructuredTextProvider,
)


class PluginModelCapabilityError(RuntimeError):
    _MESSAGES = {
        "model_input_too_large": "Model input exceeds the Host limit.",
        "model_output_limit_exceeded": "Requested model output exceeds the Host limit.",
        "model_unavailable": "The Host model is unavailable.",
        "model_request_failed": "The Host model request failed.",
        "model_output_truncated": "The Host model output was truncated.",
        "model_output_too_large": "The Host model output exceeds the byte limit.",
        "model_invalid_output": "The Host model returned an invalid JSON object.",
    }

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(self._MESSAGES.get(code, "The Host model call failed."))


class PluginModelCapabilityAdapter:
    def __init__(
        self,
        provider: StructuredTextProvider,
        *,
        max_input_chars: int,
        max_output_tokens: int,
    ) -> None:
        if max_input_chars < 1 or max_output_tokens < 1:
            raise ValueError("model adapter limits must be positive")
        self._provider = provider
        self._max_input_chars = max_input_chars
        self._max_output_tokens = max_output_tokens

    async def __call__(
        self,
        _context: CapabilityExecutionContext,
        value: ModelInvokeInput,
    ) -> ModelInvokeOutput:
        serialized_payload = json.dumps(
            value.input_payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        total_input_chars = (
            len(value.system_prompt)
            + len(value.user_prompt)
            + len(serialized_payload)
        )
        if total_input_chars > self._max_input_chars:
            raise PluginModelCapabilityError("model_input_too_large")
        if value.max_output_tokens > self._max_output_tokens:
            raise PluginModelCapabilityError("model_output_limit_exceeded")

        try:
            result = await self._provider.complete_structured(
                StructuredCompletionRequest(
                    system_prompt=value.system_prompt,
                    user_prompt=value.user_prompt,
                    input_payload=value.input_payload,
                    max_output_tokens=value.max_output_tokens,
                )
            )
        except (DeepSeekConfigurationError, DeepSeekAuthError):
            raise PluginModelCapabilityError("model_unavailable") from None
        except DeepSeekRequestError:
            raise PluginModelCapabilityError("model_request_failed") from None
        except Exception:
            raise PluginModelCapabilityError("model_request_failed") from None

        if result.finish_reason == "length":
            raise PluginModelCapabilityError("model_output_truncated")
        if len(result.content.encode("utf-8")) > 192 * 1024:
            raise PluginModelCapabilityError("model_output_too_large")
        try:
            parsed = json.loads(result.content)
        except (TypeError, ValueError):
            raise PluginModelCapabilityError("model_invalid_output") from None
        if not isinstance(parsed, Mapping):
            raise PluginModelCapabilityError("model_invalid_output")
        output = dict(parsed)
        try:
            validate_bounded_json(output, max_bytes=192 * 1024)
        except ValueError:
            raise PluginModelCapabilityError("model_output_too_large") from None
        output_tokens = result.output_tokens or 0
        if output_tokens > value.max_output_tokens:
            raise PluginModelCapabilityError("model_invalid_output")
        return ModelInvokeOutput(
            output=output,
            provider=self._provider.provider_name,
            model=self._provider.model,
            finish_reason=result.finish_reason,
            output_tokens=output_tokens,
        )


__all__ = ["PluginModelCapabilityAdapter", "PluginModelCapabilityError"]
