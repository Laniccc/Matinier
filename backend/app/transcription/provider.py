from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator

from app.transcription.models import ASREvent


class SpeechRecognitionProvider(ABC):
    """Asynchronous vendor boundary for one real-time ASR stream."""

    @abstractmethod
    async def start(self) -> None:
        """Open the provider stream and wait until it accepts audio."""

    @abstractmethod
    async def send_audio(self, pcm: bytes) -> None:
        """Send one ordered PCM16 audio chunk."""

    @abstractmethod
    async def finish(self) -> None:
        """Finish input and wait for the provider's terminal response."""

    @abstractmethod
    def events(self) -> AsyncIterator[ASREvent]:
        """Iterate normalized events until one terminal event is emitted."""

    @abstractmethod
    async def aclose(self) -> None:
        """Release transport and background tasks. Must be idempotent."""
