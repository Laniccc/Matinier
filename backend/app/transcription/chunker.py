from __future__ import annotations


class AudioChunker:
    """Aggregate aligned PCM16 frames into provider-sized chunks."""

    def __init__(
        self,
        *,
        sample_rate: int = 16_000,
        channels: int = 1,
        sample_width_bytes: int = 2,
        chunk_duration_ms: int = 100,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if channels <= 0:
            raise ValueError("channels must be positive")
        if sample_width_bytes != 2:
            raise ValueError("only PCM16 (2-byte samples) is supported")
        if chunk_duration_ms <= 0:
            raise ValueError("chunk_duration_ms must be positive")

        byte_milliseconds = (
            sample_rate * channels * sample_width_bytes * chunk_duration_ms
        )
        if byte_milliseconds % 1_000:
            raise ValueError("chunk duration must produce a whole number of bytes")

        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width_bytes = sample_width_bytes
        self.chunk_duration_ms = chunk_duration_ms
        self.bytes_per_chunk = byte_milliseconds // 1_000
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, pcm: bytes | bytearray | memoryview) -> list[bytes]:
        data = bytes(pcm)
        if len(data) % self.sample_width_bytes:
            raise ValueError("PCM16 input must contain complete 2-byte samples")
        if not data:
            return []

        self._buffer.extend(data)
        chunks: list[bytes] = []
        while len(self._buffer) >= self.bytes_per_chunk:
            chunks.append(bytes(self._buffer[: self.bytes_per_chunk]))
            del self._buffer[: self.bytes_per_chunk]
        return chunks

    def flush(self) -> bytes | None:
        if not self._buffer:
            return None
        tail = bytes(self._buffer)
        self._buffer.clear()
        return tail
