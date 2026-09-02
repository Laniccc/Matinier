from __future__ import annotations

from app.meeting_state.extractor import MeetingStateExtractor
from app.assistant.plugin_policy import MeetingPluginPolicy
from app.meeting_state.projector import MeetingStateProjector, ProjectorConfig
from app.persistence.database import Database
from app.settings import Settings
from app.text_processing.deepseek_provider import DeepSeekCompletionProvider
from app.text_processing.provider import StructuredTextProvider


def build_meeting_state_projector(
    settings: Settings,
    database: Database,
    *,
    provider: StructuredTextProvider | None = None,
    policy: MeetingPluginPolicy | None = None,
) -> MeetingStateProjector | None:
    """Build the sidecar only when the explicit Assistant feature flag is on."""

    if not settings.assistant_enabled:
        return None
    structured_provider = provider or DeepSeekCompletionProvider(
        api_key=settings.deepseek_api_key,
        base_url=str(settings.deepseek_base_url),
        model=settings.deepseek_model,
        timeout_seconds=settings.deepseek_request_timeout_seconds,
        temperature=settings.deepseek_temperature,
        max_output_tokens=settings.deepseek_max_output_tokens,
    )
    return MeetingStateProjector(
        database,
        MeetingStateExtractor(structured_provider),
        policy=policy or MeetingPluginPolicy(database, enabled=settings.plugin_framework_enabled),
        config=ProjectorConfig(
            poll_interval_ms=settings.meeting_state_poll_interval_ms,
            batch_segments=settings.meeting_state_batch_segments,
            batch_trigger_chars=settings.meeting_state_batch_trigger_chars,
            max_input_chars=settings.meeting_state_max_input_chars,
            max_batch_wait_seconds=settings.meeting_state_max_batch_wait_seconds,
            scan_overlap_seconds=settings.meeting_state_scan_overlap_seconds,
            stale_after_seconds=settings.meeting_state_stale_after_seconds,
            concurrency=settings.meeting_state_concurrency,
            priority_burst=settings.meeting_state_priority_burst,
            retry_delays_seconds=settings.meeting_state_retry_delay_list,
        ),
    )
