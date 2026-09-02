from __future__ import annotations


# Shared language set supported by the current Fun-ASR stable model and usable
# as text targets by Qwen3.5 LiveTranslate. Locale-shaped browser values remain
# accepted for compatibility with sessions created before bilingual captions.
SUPPORTED_LANGUAGE_CODES = frozenset(
    {
        "ar",
        "bg",
        "cs",
        "da",
        "de",
        "el",
        "en",
        "es",
        "fi",
        "fr",
        "hi",
        "hr",
        "hu",
        "id",
        "it",
        "ja",
        "ko",
        "ms",
        "nl",
        "no",
        "pl",
        "pt",
        "ro",
        "ru",
        "sk",
        "sv",
        "th",
        "tl",
        "vi",
        "zh",
    }
)

_ALIASES = {
    "ar-sa": "ar",
    "de-de": "de",
    "en-gb": "en",
    "en-us": "en",
    "es-es": "es",
    "fr-fr": "fr",
    "hi-in": "hi",
    "id-id": "id",
    "it-it": "it",
    "ja-jp": "ja",
    "ko-kr": "ko",
    "pt-br": "pt",
    "pt-pt": "pt",
    "ru-ru": "ru",
    "th-th": "th",
    "vi-vn": "vi",
    "zh-cn": "zh",
    "zh-hans": "zh",
    "zh-hant": "zh",
    "zh-hk": "zh",
    "zh-tw": "zh",
}


def provider_language_code(
    value: str,
    *,
    allow_auto: bool,
) -> str | None:
    """Return the provider language code while preserving API compatibility."""

    normalized = value.strip().lower().replace("_", "-")
    if allow_auto and normalized == "auto":
        return None
    code = _ALIASES.get(normalized, normalized)
    if code not in SUPPORTED_LANGUAGE_CODES:
        raise ValueError(f"Unsupported language: {value}")
    return code


def validate_translation_pair(
    source_language: str,
    target_language: str | None,
) -> tuple[str | None, str | None]:
    source_code = provider_language_code(source_language, allow_auto=True)
    if target_language is None:
        return source_code, None
    target_code = provider_language_code(target_language, allow_auto=False)
    if source_code is None:
        raise ValueError("Translation requires an explicit source language")
    if source_code == target_code:
        raise ValueError("Source and target languages must be different")
    return source_code, target_code
