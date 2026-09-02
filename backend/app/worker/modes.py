from __future__ import annotations

from enum import StrEnum


class WorkerRuntimeMode(StrEnum):
    """Explicit Worker data-plane modes; production remains the default."""

    PRODUCTION = "production"
    TRANSPORT_ONLY = "transport-only"
    FAKE_PROVIDER = "fake-provider"

    @property
    def uses_real_provider(self) -> bool:
        return self is WorkerRuntimeMode.PRODUCTION

    @property
    def provider_name(self) -> str:
        if self is WorkerRuntimeMode.TRANSPORT_ONLY:
            return "transport-only"
        if self is WorkerRuntimeMode.FAKE_PROVIDER:
            return "fake"
        return "bailian"
