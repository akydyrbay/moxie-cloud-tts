"""Provider-agnostic TTS engine interface.

Origin: new for the minimal Cloud-TTS pipeline. The pipeline
(`speak_service.py`) depends only on this interface + `SynthesisResult`, so the
openai implementation can be swapped for another provider later without any
changes upstream.
"""

from dataclasses import dataclass
from typing import Protocol


class UnsupportedLanguageError(ValueError):
    """Raised when a requested language has no configured voice."""


@dataclass(frozen=True)
class SynthesisResult:
    """Everything the pipeline needs to fill an `AudioBuffer` + timings.

    `audio` is the bytes that go straight into `AudioBuffer.buffer` — either a
    full WAV container or raw PCM16, depending on the engine/config. `channels`
    and `sample_rate` describe how to interpret those bytes.
    """

    audio: bytes
    sample_rate: int
    channels: int
    synthesis_time_ms: int


class TTSEngine(Protocol):
    """Minimal contract a synthesis provider must satisfy."""

    def synthesize(self, text: str, language: str) -> SynthesisResult:
        """Synthesize `text` in `language` (short code, e.g. "ru"/"kk")."""
        ...
