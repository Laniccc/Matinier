from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.plugins.capabilities import (
    DeliveryPrepareInput,
    DeliveryQueryInput,
    ModelInvokeInput,
)
from app.plugins.document_contracts import (
    PluginDocumentEvidenceRef,
    PluginDocumentPublishInput,
)
from app.settings import Settings


def test_extended_capability_contracts_accept_the_course_plugin_shapes(tmp_path) -> None:
    model = ModelInvokeInput(
        input_category="session_transcript",
        system_prompt="Return one JSON object.",
        user_prompt="Classify this window.",
        input_payload={"items": [{"item_id": "seg-1", "text": "A definition"}]},
        response_format="json_object",
        max_output_tokens=1_024,
        timeout_seconds=20,
    )
    assert model.input_payload["items"][0]["item_id"] == "seg-1"

    prepared = DeliveryPrepareInput(
        trigger="manual",
        output_language="zh-CN",
        final_sequence=42,
    )
    assert prepared.output_language == "zh-CN"

    query = DeliveryQueryInput(
        package_id="package-1",
        document_kinds=("source_raw", "live_translation", "evidence_index"),
        language=None,
        after_item=0,
        limit=100,
    )
    assert query.limit == 100

    evidence = PluginDocumentEvidenceRef(
        item_id="item-1",
        source_segment_ids=("segment-1",),
        start_ms=1_000,
        end_ms=2_000,
    )
    document = PluginDocumentPublishInput(
        identity_key="course-notes:zh-CN",
        schema_name="matinier.course-notes",
        schema_version="1.0",
        language="zh-CN",
        trigger="manual",
        completeness="interim",
        source_package_id="package-1",
        content={"title": "课程内容整理", "sections": []},
        markdown="# 课程内容整理\n",
        evidence_refs=(evidence,),
    )
    assert document.evidence_refs == (evidence,)

    settings = Settings(
        _env_file=None,
        livekit_url="ws://127.0.0.1:7880",
        livekit_api_key="test-key",
        livekit_api_secret="test-secret-that-is-at-least-32-bytes",
        livekit_room_name="test-room",
        data_dir=tmp_path,
    )
    assert settings.plugin_model_max_concurrency == 2
    assert settings.plugin_model_max_input_chars == 32_000
    assert settings.plugin_model_max_output_tokens == 4_096
    assert settings.plugin_document_max_bytes == 196_608
    assert settings.plugin_delivery_max_page_items == 100


@pytest.mark.parametrize(
    "payload",
    [
        {"system_prompt": "x" * 32_001},
        {"timeout_seconds": 30.01},
        {"input_payload": {"text": "x" * (256 * 1024)}},
        {"unknown": True},
    ],
)
def test_model_invoke_rejects_unbounded_or_unknown_input(payload: dict[str, object]) -> None:
    valid: dict[str, object] = {
        "input_category": "session_transcript",
        "system_prompt": "Return JSON.",
        "user_prompt": "Classify.",
        "input_payload": {"items": []},
        "response_format": "json_object",
        "max_output_tokens": 1_024,
        "timeout_seconds": 20,
    }
    valid.update(payload)
    with pytest.raises(ValidationError):
        ModelInvokeInput.model_validate(valid)


@pytest.mark.parametrize("language", ["", "bad language", "../../zh", "x" * 33])
def test_delivery_contract_rejects_invalid_languages(language: str) -> None:
    with pytest.raises(ValidationError):
        DeliveryPrepareInput(
            trigger="manual",
            output_language=language,
            final_sequence=0,
        )


def test_delivery_query_rejects_invalid_bounds() -> None:
    with pytest.raises(ValidationError):
        DeliveryQueryInput(
            package_id="package-1",
            document_kinds=tuple(f"kind-{index}" for index in range(101)),
            after_item=0,
            limit=100,
        )
    with pytest.raises(ValidationError):
        DeliveryQueryInput(
            package_id="package-1",
            document_kinds=("source_raw",),
            after_item=-1,
            limit=100,
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("identity_key", "bad key"),
        ("schema_name", "Course Notes"),
        ("trigger", "timer"),
        ("completeness", "draft"),
        ("markdown", "<script>alert(1)</script>"),
        ("markdown", "[bad](javascript:alert(1))"),
    ],
)
def test_document_contract_rejects_unsafe_or_unknown_values(
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "identity_key": "course-notes:zh-CN",
        "schema_name": "matinier.course-notes",
        "schema_version": "1.0",
        "language": "zh-CN",
        "trigger": "manual",
        "completeness": "interim",
        "source_package_id": "package-1",
        "content": {"title": "课程内容整理", "sections": []},
        "markdown": "# 课程内容整理\n",
        "evidence_refs": [],
    }
    payload[field] = value
    with pytest.raises(ValidationError):
        PluginDocumentPublishInput.model_validate(payload)


def test_document_contract_rejects_reversed_evidence_and_oversized_content() -> None:
    with pytest.raises(ValidationError):
        PluginDocumentEvidenceRef(
            item_id="item-1",
            source_segment_ids=("segment-1",),
            start_ms=2_000,
            end_ms=1_000,
        )
    with pytest.raises(ValidationError):
        PluginDocumentPublishInput(
            identity_key="course-notes:zh-CN",
            schema_name="matinier.course-notes",
            schema_version="1.0",
            language="zh-CN",
            trigger="manual",
            completeness="interim",
            source_package_id="package-1",
            content={"body": "x" * (256 * 1024)},
            markdown="# Course\n",
            evidence_refs=(),
        )
