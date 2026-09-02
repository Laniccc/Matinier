from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Mapping

from app.assistant.grants import (
    action_grant_from_record,
    authorize_tool_use,
)
from app.assistant.models import ActionGrant
from app.assistant.repository import (
    AssistantIdempotencyConflictError,
    AssistantRepository,
)
from app.assistant.tools.contracts import (
    ToolExecutionContext,
    ToolInvocation,
    ToolResult,
    ToolSpec,
)
from app.assistant.tools.registry import RegisteredTool, ToolRegistry
from app.logging import log_assistant_lifecycle
from app.persistence.database import Database
from app.persistence.models import (
    AssistantExecutionRecord,
    AssistantToolCallRecord,
    ExternalActionClaimRecord,
    utc_now,
)


def canonical_arguments_hash(arguments: Mapping[str, object]) -> str:
    payload = json.dumps(
        arguments,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stored_result(record: AssistantToolCallRecord) -> ToolResult:
    if record.result_json is not None:
        return ToolResult.model_validate(record.result_json)
    if record.status in {"prepared", "requesting", "pending"}:
        return ToolResult(
            status="pending",
            external_reference=record.external_reference_json,
        )
    if record.status in {"unknown", "reconciling"}:
        return ToolResult(
            status="unknown",
            external_reference=record.external_reference_json,
            error_code=record.error_code or "tool_outcome_unknown",
            error_message=(
                record.error_message
                or "The tool outcome is unknown and requires reconciliation."
            ),
        )
    if record.status == "failed":
        return ToolResult(
            status="failed",
            external_reference=record.external_reference_json,
            error_code=record.error_code or "tool_failed",
            error_message=record.error_message or "The tool did not complete.",
        )
    raise RuntimeError("succeeded ToolCall is missing its persisted result")


def _validate_result(spec: ToolSpec, value: ToolResult | Mapping[str, object]) -> ToolResult:
    result = ToolResult.model_validate(value)
    if result.output is None:
        return result
    output = spec.output_model.model_validate(result.output)
    return result.model_copy(update={"output": output.model_dump(mode="json")})


class ToolExecutor:
    """Runs one typed tool without keeping a database Session across awaits."""

    def __init__(
        self,
        database: Database,
        registry: ToolRegistry,
        *,
        claim_lease_seconds: float = 60.0,
        execution_guard=None,
    ) -> None:
        if claim_lease_seconds <= 0:
            raise ValueError("claim_lease_seconds must be positive")
        self._database = database
        self._registry = registry
        self._claim_lease_seconds = claim_lease_seconds
        self._execution_guard = execution_guard

    async def execute(
        self,
        invocation: ToolInvocation | Mapping[str, object],
    ) -> ToolResult:
        request = ToolInvocation.model_validate(invocation)
        registered = self._registry.get(request.tool_name)
        spec = registered.spec
        arguments_model = spec.input_model.model_validate(request.arguments)
        arguments = arguments_model.model_dump(mode="json")
        arguments_hash = canonical_arguments_hash(arguments)

        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            execution = repository.get_execution_required(request.execution_id)
            if execution.profile not in spec.allowed_profiles:
                return ToolResult(
                    status="failed",
                    error_code="tool_profile_denied",
                    error_message="This tool is unavailable for the execution profile.",
                    confirmed_side_effects=0,
                )
            self._validate_invocation_keys(request, spec)
            if self._execution_guard is not None:
                self._execution_guard.authorize_tool(db_session, request, spec)
            replay = self._find_replay(
                repository,
                request=request,
                registered=registered,
                arguments_hash=arguments_hash,
            )
            if replay is not None:
                return replay

            grant = self._authorize(
                repository,
                execution=execution,
                request=request,
                spec=spec,
            )
            tool_call, claim = self._prepare_request(
                repository,
                execution=execution,
                registered=registered,
                request=request,
                arguments=arguments,
                arguments_hash=arguments_hash,
            )
            if tool_call.execution_id != execution.id:
                return _validate_result(spec, _stored_result(tool_call))
            if tool_call.status != "prepared":
                return _validate_result(spec, _stored_result(tool_call))
            if claim is not None and (
                claim.holder_execution_id != execution.id
                or claim.tool_call_id != tool_call.id
            ):
                source_call = (
                    repository.get_tool_call(claim.tool_call_id)
                    if claim.tool_call_id is not None
                    else None
                )
                if source_call is None:
                    return ToolResult(status="pending")
                return _validate_result(spec, _stored_result(source_call))

            if spec.effect == "external_write":
                if grant is None or claim is None:
                    raise RuntimeError("authorized external write is missing state")
                repository.consume_grant(grant.grant_id, consumed_at=utc_now())
                repository.transition_external_action_claim(
                    claim.id,
                    holder_execution_id=execution.id,
                    expected_status="reserved",
                    target_status="requesting",
                )
            tool_call = repository.update_tool_call(
                tool_call.id,
                expected_status="prepared",
                status="requesting",
                increment_attempt=True,
            )
            context = self._execution_context(
                execution=execution,
                tool_call=tool_call,
                arguments=arguments_model,
                grant=grant,
                request=request,
                reconciling=False,
            )
            claim_id = claim.id if claim is not None else None
            db_session.commit()

        result = await self._invoke_adapter(registered, context)
        result = _validate_result(spec, result)
        return self._persist_result(
            registered=registered,
            tool_call_id=context.tool_call_id,
            execution_id=context.execution_id,
            evidence_refs=context.evidence_refs,
            result=result,
            claim_id=claim_id,
            expected_claim_status="requesting" if claim_id is not None else None,
            phase="execute",
        )

    async def reconcile(
        self,
        tool_call_id: str,
        *,
        evidence_refs: tuple[str, ...] = (),
    ) -> ToolResult:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            tool_call = repository.get_tool_call(tool_call_id)
            if tool_call is None:
                raise LookupError(f"Assistant ToolCall not found: {tool_call_id}")
            registered = self._registry.get(tool_call.tool_name)
            spec = registered.spec
            if tool_call.tool_version != spec.version:
                raise RuntimeError("registered tool version no longer matches ToolCall")
            if not spec.supports_reconciliation:
                raise ValueError(f"tool does not support reconciliation: {spec.name}")
            if tool_call.status in {"succeeded", "failed"}:
                return _validate_result(spec, _stored_result(tool_call))
            if tool_call.status not in {"pending", "unknown", "reconciling"}:
                raise ValueError("only pending or unknown ToolCalls can reconcile")

            execution = repository.get_execution_required(tool_call.execution_id)
            if execution.profile not in spec.allowed_profiles:
                return ToolResult(
                    status="failed",
                    error_code="tool_profile_denied",
                    error_message="This tool is unavailable for the execution profile.",
                    confirmed_side_effects=0,
                )
            arguments_model = spec.input_model.model_validate(tool_call.arguments_json)
            grant = self._load_execution_grant(repository, execution)
            claim = self._claim_for_call(repository, registered, tool_call)
            expected_claim_status = None
            if claim is not None:
                expected_claim_status = claim.status
                if expected_claim_status not in {"requesting", "unknown"}:
                    return _validate_result(spec, _stored_result(tool_call))
            tool_call = repository.update_tool_call(
                tool_call.id,
                expected_status=tool_call.status,
                status="reconciling",
                external_reference=tool_call.external_reference_json,
                increment_attempt=True,
            )
            context = ToolExecutionContext(
                execution_id=execution.id,
                session_id=execution.session_id,
                tool_call_id=tool_call.id,
                arguments=arguments_model,
                grant=grant,
                candidate_id=None,
                logical_action_key=tool_call.logical_action_key,
                idempotency_key=tool_call.idempotency_key,
                evidence_refs=evidence_refs,
                external_reference=tool_call.external_reference_json,
                attempt=tool_call.attempt_count,
                reconciling=True,
            )
            claim_id = claim.id if claim is not None else None
            db_session.commit()

        result = await self._reconcile_adapter(registered, context)
        result = _validate_result(spec, result)
        return self._persist_result(
            registered=registered,
            tool_call_id=tool_call_id,
            execution_id=context.execution_id,
            evidence_refs=context.evidence_refs,
            result=result,
            claim_id=claim_id,
            expected_claim_status=expected_claim_status,
            phase="reconcile",
        )

    @staticmethod
    def _validate_invocation_keys(request: ToolInvocation, spec: ToolSpec) -> None:
        if spec.effect == "external_write" and (
            request.logical_action_key is None or request.idempotency_key is None
        ):
            raise ValueError(
                "external-write tools require logical-action and idempotency keys"
            )
        if spec.supports_idempotency and request.idempotency_key is None:
            raise ValueError("this tool requires an idempotency key")

    @staticmethod
    def _validate_call_identity(
        record: AssistantToolCallRecord,
        *,
        request: ToolInvocation,
        spec: ToolSpec,
    ) -> None:
        identity = (
            record.tool_name,
            record.tool_version,
            record.capability,
            record.effect,
            record.logical_action_key,
            record.idempotency_key,
        )
        requested = (
            spec.name,
            spec.version,
            spec.capability,
            spec.effect,
            request.logical_action_key,
            request.idempotency_key,
        )
        if identity != requested:
            raise AssistantIdempotencyConflictError(
                "persisted ToolCall identity differs from the requested tool"
            )

    def _find_replay(
        self,
        repository: AssistantRepository,
        *,
        request: ToolInvocation,
        registered: RegisteredTool,
        arguments_hash: str,
    ) -> ToolResult | None:
        if request.prepared_tool_call_id is not None or request.idempotency_key is None:
            return None
        existing = repository.get_tool_call_by_idempotency_key(
            request.idempotency_key
        )
        if existing is None:
            return None
        self._validate_call_identity(
            existing,
            request=request,
            spec=registered.spec,
        )
        if existing.arguments_hash != arguments_hash:
            raise AssistantIdempotencyConflictError(
                "idempotency key is already bound to different arguments"
            )
        if existing.status == "prepared" and existing.execution_id == request.execution_id:
            return None
        return _validate_result(registered.spec, _stored_result(existing))

    def _authorize(
        self,
        repository: AssistantRepository,
        *,
        execution: AssistantExecutionRecord,
        request: ToolInvocation,
        spec: ToolSpec,
    ) -> ActionGrant | None:
        resolved_grant_id = request.grant_id or execution.grant_id
        if (
            request.grant_id is not None
            and execution.grant_id is not None
            and request.grant_id != execution.grant_id
        ):
            raise ValueError("invocation Grant does not match the execution Grant")
        grant = None
        if resolved_grant_id is not None:
            record = repository.get_grant(resolved_grant_id)
            if record is None:
                raise LookupError(f"Action Grant not found: {resolved_grant_id}")
            grant = action_grant_from_record(record)
        return authorize_tool_use(
            grant,
            effect=spec.effect,
            capability=spec.capability,
            session_id=execution.session_id,
            execution_goal=execution.goal,
            candidate_id=request.candidate_id,
            requested_resource_scope=request.requested_resource_scope,
        )

    def _prepare_request(
        self,
        repository: AssistantRepository,
        *,
        execution: AssistantExecutionRecord,
        registered: RegisteredTool,
        request: ToolInvocation,
        arguments: Mapping[str, object],
        arguments_hash: str,
    ) -> tuple[AssistantToolCallRecord, ExternalActionClaimRecord | None]:
        spec = registered.spec
        tool_call: AssistantToolCallRecord
        claim: ExternalActionClaimRecord | None = None
        if request.prepared_tool_call_id is not None:
            tool_call = repository.get_tool_call(request.prepared_tool_call_id)
            if tool_call is None:
                raise LookupError(
                    f"Assistant ToolCall not found: {request.prepared_tool_call_id}"
                )
            self._validate_call_identity(tool_call, request=request, spec=spec)
            if tool_call.execution_id != execution.id:
                raise ValueError("prepared ToolCall belongs to another execution")
            if tool_call.status != "prepared":
                return tool_call, self._claim_for_call(
                    repository,
                    registered,
                    tool_call,
                )
            if spec.effect == "external_write":
                claim = self._claim_for_call(repository, registered, tool_call)
                if claim is None or claim.status != "reserved":
                    raise ValueError(
                        "external ToolCall arguments require a reserved claim"
                    )
            if tool_call.arguments_hash != arguments_hash:
                old_hash = tool_call.arguments_hash
                tool_call = repository.replace_prepared_tool_call_arguments(
                    tool_call.id,
                    execution_id=execution.id,
                    expected_arguments_hash=old_hash,
                    arguments=arguments,
                    arguments_hash=arguments_hash,
                )
                if claim is not None:
                    claim = repository.replace_reserved_claim_arguments(
                        claim.id,
                        holder_execution_id=execution.id,
                        tool_call_id=tool_call.id,
                        expected_arguments_hash=old_hash,
                        arguments_hash=arguments_hash,
                    )
        else:
            requested_tool_call_id = str(uuid.uuid4())
            tool_call = repository.prepare_tool_call(
                execution_id=execution.id,
                tool_name=spec.name,
                tool_version=spec.version,
                capability=spec.capability,
                effect=spec.effect,
                arguments=arguments,
                arguments_hash=arguments_hash,
                logical_action_key=request.logical_action_key,
                idempotency_key=request.idempotency_key,
                step_id=request.step_id,
                tool_call_id=requested_tool_call_id,
            )
            self._validate_call_identity(tool_call, request=request, spec=spec)
            if tool_call.arguments_hash != arguments_hash:
                raise AssistantIdempotencyConflictError(
                    "persisted ToolCall arguments differ from the request"
                )

        if spec.effect == "external_write" and claim is None:
            if request.logical_action_key is None:
                raise RuntimeError("external ToolCall has no logical action key")
            lease_seconds = max(
                self._claim_lease_seconds,
                spec.timeout_seconds + 5,
            )
            claim = repository.reserve_external_action_claim(
                provider=registered.adapter.provider_name,
                capability=spec.capability,
                logical_action_key=request.logical_action_key,
                holder_execution_id=execution.id,
                arguments_hash=arguments_hash,
                lease_expires_at=utc_now() + dt.timedelta(seconds=lease_seconds),
                tool_call_id=tool_call.id,
            )
        return tool_call, claim

    @staticmethod
    def _load_execution_grant(
        repository: AssistantRepository,
        execution: AssistantExecutionRecord,
    ) -> ActionGrant | None:
        if execution.grant_id is None:
            return None
        record = repository.get_grant(execution.grant_id)
        return action_grant_from_record(record) if record is not None else None

    @staticmethod
    def _claim_for_call(
        repository: AssistantRepository,
        registered: RegisteredTool,
        tool_call: AssistantToolCallRecord,
    ) -> ExternalActionClaimRecord | None:
        if tool_call.effect != "external_write" or tool_call.logical_action_key is None:
            return None
        return repository.observe_external_action_claim(
            provider=registered.adapter.provider_name,
            capability=registered.spec.capability,
            logical_action_key=tool_call.logical_action_key,
        )

    @staticmethod
    def _execution_context(
        *,
        execution: AssistantExecutionRecord,
        tool_call: AssistantToolCallRecord,
        arguments,
        grant: ActionGrant | None,
        request: ToolInvocation,
        reconciling: bool,
    ) -> ToolExecutionContext:
        return ToolExecutionContext(
            execution_id=execution.id,
            session_id=execution.session_id,
            tool_call_id=tool_call.id,
            arguments=arguments,
            grant=grant,
            candidate_id=request.candidate_id,
            logical_action_key=tool_call.logical_action_key,
            idempotency_key=tool_call.idempotency_key,
            evidence_refs=request.evidence_refs,
            external_reference=tool_call.external_reference_json,
            attempt=tool_call.attempt_count,
            reconciling=reconciling,
        )

    @staticmethod
    async def _invoke_adapter(
        registered: RegisteredTool,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            async with asyncio.timeout(registered.spec.timeout_seconds):
                return await registered.adapter.execute(context)
        except TimeoutError:
            return ToolResult(
                status="unknown",
                error_code="tool_timeout_unknown",
                error_message=(
                    "The tool timed out without a confirmed outcome; "
                    "reconciliation is required."
                ),
            )
        except Exception:
            if registered.spec.effect == "external_write":
                return ToolResult(
                    status="unknown",
                    error_code="tool_response_unknown",
                    error_message=(
                        "The external action returned no confirmed outcome; "
                        "reconciliation is required."
                    ),
                )
            return ToolResult(
                status="failed",
                error_code="tool_execution_failed",
                error_message="The tool could not complete this request.",
                retryable=True,
            )

    @staticmethod
    async def _reconcile_adapter(
        registered: RegisteredTool,
        context: ToolExecutionContext,
    ) -> ToolResult:
        try:
            async with asyncio.timeout(registered.spec.timeout_seconds):
                return await registered.adapter.reconcile(context)
        except TimeoutError:
            return ToolResult(
                status="unknown",
                external_reference=context.external_reference,
                error_code="tool_reconciliation_pending",
                error_message="The external outcome is still being reconciled.",
            )
        except Exception:
            return ToolResult(
                status="unknown",
                external_reference=context.external_reference,
                error_code="tool_reconciliation_unknown",
                error_message="The external outcome could not yet be confirmed.",
            )

    def _persist_result(
        self,
        *,
        registered: RegisteredTool,
        tool_call_id: str,
        execution_id: str,
        evidence_refs: tuple[str, ...],
        result: ToolResult,
        claim_id: str | None,
        expected_claim_status: str | None,
        phase: str,
    ) -> ToolResult:
        with self._database.session() as db_session:
            repository = AssistantRepository(db_session)
            tool_call = repository.get_tool_call(tool_call_id)
            if tool_call is None:
                raise LookupError(f"Assistant ToolCall not found: {tool_call_id}")
            if tool_call.status in {"succeeded", "failed"}:
                return _validate_result(registered.spec, _stored_result(tool_call))

            tool_status = result.status
            tool_call = repository.update_tool_call(
                tool_call_id,
                expected_status=tool_call.status,
                status=tool_status,
                result=result.model_dump(mode="json"),
                external_reference=result.external_reference,
                error_code=result.error_code,
                error_message=result.error_message,
            )
            if claim_id is not None and expected_claim_status is not None:
                if result.status == "succeeded":
                    target_claim_status = "succeeded"
                elif result.status == "failed":
                    target_claim_status = "failed_safe"
                elif result.status == "unknown":
                    target_claim_status = "unknown"
                else:
                    target_claim_status = expected_claim_status
                repository.transition_external_action_claim(
                    claim_id,
                    holder_execution_id=execution_id,
                    expected_status=expected_claim_status,
                    target_status=target_claim_status,
                    external_reference=result.external_reference,
                )
            if (
                registered.spec.effect == "external_write"
                and result.confirmed_side_effects == 0
            ):
                execution = repository.get_execution_required(execution_id)
                if execution.grant_id is not None:
                    repository.release_grant_side_effect(execution.grant_id)
            repository.append_observation(
                execution_id=execution_id,
                source="tool",
                source_ref=tool_call_id,
                observation={
                    "tool_name": registered.spec.name,
                    "tool_version": registered.spec.version,
                    "status": result.status,
                    "output": result.output,
                    "external_reference": result.external_reference,
                    "error_code": result.error_code,
                    "retryable": result.retryable,
                    "confirmed_side_effects": result.confirmed_side_effects,
                },
                evidence_refs=evidence_refs,
            )
            execution = repository.get_execution_required(execution_id)
            db_session.commit()
            log_assistant_lifecycle(
                "assistant_tool_result",
                session_id=execution.session_id,
                execution_id=execution.id,
                root_execution_id=execution.root_execution_id,
                status=result.status,
                elapsed_ms=_tool_duration_ms(tool_call),
                phase=phase,
                profile=execution.profile,
                snapshot_id=execution.snapshot_id,
                tool_call_id=tool_call.id,
                tool_name=tool_call.tool_name,
                capability=tool_call.capability,
                attempt_count=tool_call.attempt_count,
                retry_count=max(0, tool_call.attempt_count - 1),
                error_code=result.error_code,
            )
        return result


def _tool_duration_ms(tool_call: AssistantToolCallRecord) -> int:
    started = tool_call.requested_at or tool_call.created_at
    ended = tool_call.completed_at or tool_call.updated_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=dt.UTC)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=dt.UTC)
    return max(0, int((ended - started).total_seconds() * 1000))


__all__ = ["ToolExecutor", "canonical_arguments_hash"]
