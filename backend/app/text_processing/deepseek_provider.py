from __future__ import annotations

import json
from collections.abc import Sequence

import httpx

from app.text_processing.models import SourceSegmentSnapshot
from app.text_processing.provider import (
    DeepSeekAuthError,
    DeepSeekConfigurationError,
    DeepSeekRequestError,
    ScriptCompletionResult,
    StructuredCompletionRequest,
    StructuredCompletionResult,
)


_SYSTEM_PROMPT = """你是实时字幕的轻度整理器。
请只输出一个符合指定结构的 JSON 对象，不要输出 Markdown 代码围栏或解释。

允许：补充合理标点、合并过碎字幕、删除明显无意义的重复口头语、统一术语写法。
禁止：不得翻译；不得编造原文没有的事实；不得删除人名、数字和时间等关键信息；
不得修改、遗漏、重复或伪造 source_segment_ids。

输出 JSON 必须严格使用以下字段，不能增加字段：
{
  "title": "简短标题",
  "sections": [
    {
      "source_segment_ids": ["输入中的 segment_id"],
      "start_ms": 0,
      "end_ms": 1000,
      "clean_text": "保持原意的整理文本",
      "notes": []
    }
  ],
  "warnings": []
}

每个输入 segment_id 必须且只能出现一次。每个 section 的 start_ms 必须等于其引用片段的最小
start_ms，end_ms 必须等于其引用片段的最大 end_ms。"""


class DeepSeekCompletionProvider:
    provider_name = "deepseek"

    def __init__(
        self,
        *,
        api_key: str | None,
        base_url: str,
        model: str,
        timeout_seconds: float,
        temperature: float,
        max_output_tokens: int,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self.model = model
        self._timeout_seconds = timeout_seconds
        self._temperature = temperature
        self._max_output_tokens = max_output_tokens
        self._transport = transport

    async def complete(
        self,
        segments: Sequence[SourceSegmentSnapshot],
    ) -> ScriptCompletionResult:
        if not segments:
            raise ValueError("at least one source segment is required")

        source_payload = [
            {
                "segment_id": segment.segment_id,
                "start_ms": segment.audio_start_ms,
                "end_ms": segment.audio_end_ms,
                "text": segment.display_text,
            }
            for segment in segments
        ]
        return await self.complete_structured(
            StructuredCompletionRequest(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt="请把以下 Final 字幕整理为上述 JSON。输入 JSON：",
                input_payload={"segments": source_payload},
            )
        )

    async def complete_structured(
        self,
        request: StructuredCompletionRequest,
    ) -> StructuredCompletionResult:
        if not self._api_key:
            raise DeepSeekConfigurationError("DeepSeek API key is not configured")
        output_limit = self._max_output_tokens
        if request.max_output_tokens is not None:
            if type(request.max_output_tokens) is not int or request.max_output_tokens < 1:
                raise ValueError("requested output token limit must be positive")
            output_limit = min(output_limit, request.max_output_tokens)
        request_payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {
                    "role": "user",
                    "content": (
                        request.user_prompt
                        + "\n"
                        + json.dumps(
                            request.input_payload,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    ),
                },
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "temperature": self._temperature,
            "max_tokens": output_limit,
            "stream": False,
        }

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout_seconds,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=request_payload,
                )
        except httpx.RequestError as error:
            raise DeepSeekRequestError(
                "DeepSeek request failed",
                retryable=True,
            ) from error

        if response.status_code in {401, 403}:
            raise DeepSeekAuthError()
        if response.status_code >= 400:
            raise DeepSeekRequestError(
                f"DeepSeek returned HTTP {response.status_code}",
                retryable=response.status_code == 429
                or response.status_code >= 500,
            )

        try:
            response_payload = response.json()
            choice = response_payload["choices"][0]
            message = choice["message"]
            content = message.get("content") or ""
            finish_reason = choice.get("finish_reason")
            if not isinstance(content, str):
                raise TypeError("message content must be text")
            if finish_reason is not None and not isinstance(finish_reason, str):
                raise TypeError("finish reason must be text")
            output_tokens = None
            usage = response_payload.get("usage")
            if isinstance(usage, dict):
                completion_tokens = usage.get("completion_tokens")
                if (
                    isinstance(completion_tokens, int)
                    and not isinstance(completion_tokens, bool)
                    and completion_tokens >= 0
                ):
                    output_tokens = completion_tokens
        except (KeyError, IndexError, TypeError, ValueError) as error:
            raise DeepSeekRequestError(
                "DeepSeek returned an invalid response envelope",
                retryable=True,
            ) from error

        return StructuredCompletionResult(
            content=content,
            finish_reason=finish_reason,
            output_tokens=output_tokens,
        )
