from __future__ import annotations


class TranslationError(Exception):
    code = "translation_stream_error"


class TranslationConfigurationError(TranslationError):
    code = "translation_configuration_error"


class TranslationAuthenticationError(TranslationError):
    code = "translation_auth_error"


class TranslationProtocolError(TranslationError):
    code = "translation_protocol_error"


class TranslationProviderError(TranslationError):
    code = "translation_stream_error"

    def __init__(self, message: str, *, provider_code: str | None = None) -> None:
        super().__init__(message)
        self.provider_code = provider_code


class TranslationStateError(TranslationError):
    code = "translation_state_error"


class TranslationTimeoutError(TranslationError):
    code = "translation_timeout_error"


class TranslationBackpressureError(TranslationError):
    code = "translation_backpressure_error"
