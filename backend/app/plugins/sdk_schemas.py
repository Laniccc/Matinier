from __future__ import annotations

import argparse
import json
from pathlib import Path

from pydantic import TypeAdapter

from app.media.contracts import MediaEvent
from app.assistant.plugin_contracts import (
    MeetingAskInput, MeetingExecuteInput, MeetingMarkInput, MeetingInputInput, MeetingCancelInput,
    MeetingStateQueryInput, MeetingOperationQueryInput, MeetingOperationAccepted, MeetingQueryOutput,
)
from app.plugins.capabilities import (
    ActionExecuteInput,
    ActionExecuteOutput,
    DeliveryPrepareInput,
    DeliveryPrepareOutput,
    DeliveryQueryInput,
    DeliveryQueryOutput,
    MediaQueryInput,
    MediaQueryOutput,
    ModelInvokeInput,
    ModelInvokeOutput,
    NetworkFetchInput,
    NetworkFetchOutput,
    StateGetInput,
    StateGetOutput,
    StatePutInput,
    StatePutOutput,
    UIViewPublishInput,
    UIViewPublishOutput,
)
from app.plugins.contracts import (
    JsonRpcErrorResponse,
    JsonRpcRequest,
    JsonRpcResponse,
    PluginManifest,
)
from app.plugins.document_contracts import (
    PluginDocumentPublishInput,
    PluginDocumentPublishOutput,
)
from app.plugins.ui_schema import PluginUIViewDocument


JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _published(schema: dict[str, object]) -> dict[str, object]:
    return {"$schema": JSON_SCHEMA_DIALECT, **schema}


def generated_sdk_schemas() -> dict[str, dict[str, object]]:
    """Generate public SDK schemas directly from the enforced host models."""

    rpc_envelope = TypeAdapter(
        JsonRpcRequest | JsonRpcResponse | JsonRpcErrorResponse
    )
    capability_contract = TypeAdapter(
        MediaQueryInput
        | MediaQueryOutput
        | ModelInvokeInput
        | ModelInvokeOutput
        | DeliveryPrepareInput
        | DeliveryPrepareOutput
        | DeliveryQueryInput
        | DeliveryQueryOutput
        | NetworkFetchInput
        | NetworkFetchOutput
        | StateGetInput
        | StateGetOutput
        | StatePutInput
        | StatePutOutput
        | UIViewPublishInput
        | UIViewPublishOutput
        | ActionExecuteInput
        | ActionExecuteOutput
        | PluginDocumentPublishInput
        | PluginDocumentPublishOutput
        | MeetingAskInput | MeetingExecuteInput | MeetingMarkInput | MeetingInputInput | MeetingCancelInput
        | MeetingStateQueryInput | MeetingOperationQueryInput | MeetingOperationAccepted | MeetingQueryOutput
    )
    return {
        "json-rpc-envelope.schema.json": _published(rpc_envelope.json_schema()),
        "media-event.schema.json": _published(MediaEvent.model_json_schema()),
        "plugin-capabilities.schema.json": _published(
            capability_contract.json_schema()
        ),
        "plugin-manifest.schema.json": _published(
            PluginManifest.model_json_schema()
        ),
        "ui-view-document.schema.json": _published(
            PluginUIViewDocument.model_json_schema()
        ),
    }


def write_sdk_schemas(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for filename, schema in generated_sdk_schemas().items():
        (output_dir / filename).write_text(
            json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate Matinier plugin SDK JSON Schemas from host models."
    )
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    write_sdk_schemas(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["generated_sdk_schemas", "write_sdk_schemas"]
