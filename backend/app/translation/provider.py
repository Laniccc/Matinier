from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.translation.models import TranslationEvent


class SpeechTranslationProvider(ABC):
    @abstractmethod
    async def start(self) -> None:
        """Open and configure the provider stream."""

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Send one ordered PCM16 audio chunk."""

    @abstractmethod
    async def finish(self) -> None:
        """Flush the last speech segment and wait for completion."""

    @abstractmethod
    def events(self) -> AsyncIterator[TranslationEvent]:
        """Iterate normalized translation events."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release the transport and background tasks idempotently."""
