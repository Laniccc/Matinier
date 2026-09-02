from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from app.text_processing.deepseek_provider import DeepSeekCompletionProvider
from app.text_processing.models import SourceSegmentSnapshot
from app.text_processing.provider import (
    DeepSeekAuthError,
    DeepSeekRequestError,
    StructuredCompletionRequest,
)


def source_segments() -> tuple[SourceSegmentSnapshot, ...]:
    return (
        SourceSegmentSnapshot(
            segment_id="seg-1",
            revision=2,
            language="zh-CN",
            raw_text="大家 好",
            display_text="大家 好",
            audio_start_ms=100,
            audio_end_ms=1_500,
        ),
    )


def completion_body() -> dict:
    content = {
        "title": "整理版",
        "sections": [
            {
                "source_segment_ids": ["seg-1"],
                "start_ms": 100,
                "end_ms": 1_500,
                "clean_text": "大家好。",
                "notes": [],
            }
        ],
        "warnings": [],
    }
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": json.dumps(content, ensure_ascii=False),
                },
            }
        ]
    }


def test_provider_uses_official_json_output_contract_and_sanitizes_key() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=completion_body())

    provider = DeepSeekCompletionProvider(
        api_key="sk-stage5-secret",
        base_url="https://api.deepseek.com/",
        model="deepseek-v4-flash",
        timeout_seconds=12.0,
        temperature=0.2,
        max_output_tokens=2_048,
        transport=httpx.MockTransport(handler),
    )

    result = asyncio.run(provider.complete(source_segments()))

    assert result.finish_reason == "stop"
    assert json.loads(result.content)["title"] == "整理版"
    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["authorization"] == "Bearer sk-stage5-secret"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == "deepseek-v4-flash"
    assert body["response_format"] == {"type": "json_object"}
    assert body["stream"] is False
    assert body["thinking"] == {"type": "disabled"}
    assert body["temperature"] == 0.2
    assert body["max_tokens"] == 2_048
    system_prompt = body["messages"][0]["content"]
    assert "JSON" in system_prompt
    assert "不得翻译" in system_prompt
    assert "不得编造" in system_prompt
    assert "人名、数字和时间" in system_prompt
    assert "source_segment_ids" in system_prompt


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_failures_are_non_retryable_and_do_not_leak_response_or_key(
    status_code: int,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code,
            text="provider body contains sk-stage5-secret",
        )

    provider = DeepSeekCompletionProvider(
        api_key="sk-stage5-secret",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=12.0,
        temperature=0.2,
        max_output_tokens=2_048,
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(DeepSeekAuthError) as exc_info:
        asyncio.run(provider.complete(source_segments()))

    assert "sk-stage5-secret" not in str(exc_info.value)


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_rate_limit_and_server_failures_are_retryable(status_code: int) -> None:
    provider = DeepSeekCompletionProvider(
        api_key="key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-flash",
        timeout_seconds=12.0,
        temperature=0.2,
        max_output_tokens=2_048,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status_code)
        ),
    )

    with pytest.raises(DeepSeekRequestError) as exc_info:
        asyncio.run(provider.complete(source_segments()))

    assert exc_info.value.retryable is True


def test_timeout_is_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret transport detail", request=request)

    provider = DeepSeekCompletionProvider(
        api_key="key",
        base_url="https://api.deepseek.com",
        model="deepseek-v4-pro",
        timeout_seconds=12.0,
        temperature=0.2,
        max_output_tokens=2_048,
        transport=httpx.MockTransport(timeout),
    )

    with pytest.raises(DeepSeekRequestError) as exc_info:
        asyncio.run(provider.complete(source_segments()))

    assert exc_info.value.retryable is True
    assert "secret transport detail" not in str(exc_info.value)


@pytest.mark.parametrize("requested,expected", [(None, 2048), (512, 512), (4096, 2048)])
def test_structured_output_budget_is_forwarded_and_capped(requested, expected) -> None:
    captured: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json=completion_body())

    provider = DeepSeekCompletionProvider(
        api_key="test-key",
        base_url="https://api.deepseek.com",
        model="test-model",
        timeout_seconds=12,
        temperature=0.2,
        max_output_tokens=2048,
        transport=httpx.MockTransport(handler),
    )
    asyncio.run(provider.complete_structured(StructuredCompletionRequest(
        system_prompt="Return JSON.",
        user_prompt="Summarize.",
        input_payload={},
        max_output_tokens=requested,
    )))
    assert captured == [expected]
