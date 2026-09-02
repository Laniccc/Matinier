from __future__ import annotations

from app.media.projector import MediaEventProjector, ProjectorConfig
from app.persistence.database import Database
from app.settings import Settings


def build_media_event_projector(
    settings: Settings,
    database: Database,
) -> MediaEventProjector | None:
    """Build the model-free sidecar when the plugin framework is enabled."""

    if not settings.plugin_framework_enabled:
        return None
    return MediaEventProjector(
        database,
        config=ProjectorConfig(
            poll_interval_ms=settings.media_event_projector_poll_interval_ms,
            batch_size=settings.media_event_projector_batch_size,
            scan_session_limit=settings.media_event_projector_scan_session_limit,
        ),
    )

