from __future__ import annotations


class ASRError(Exception):
    """Base error for the project-level speech recognition boundary."""

    code = "asr_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ASRConfigurationError(ASRError):
    code = "configuration_error"


class ASRAuthenticationError(ASRError):
    code = "authentication_error"


class ASRTimeoutError(ASRError):
    code = "timeout_error"


class ASRProtocolError(ASRError):
    code = "protocol_error"


class ASRProviderError(ASRError):
    code = "provider_error"

    def __init__(self, message: str, *, provider_code: str | None = None) -> None:
        super().__init__(message)
        self.provider_code = provider_code


class ASRBackpressureError(ASRError):
    code = "backpressure_error"


class ASRStateError(ASRError):
    code = "state_error"
