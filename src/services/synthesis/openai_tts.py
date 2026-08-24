"""OpenAI Text-to-Speech engine.

Origin: new for the OpenAI speech pipeline; replaces the removed Google Cloud
TTS engine behind the same provider-agnostic `TTSEngine` interface in
`base.py`, so `speak_service` is unchanged apart from which engine it points at.

OpenAI TTS returns mono 24 kHz audio. We request `wav` (a RIFF container) by
default so `AudioBuffer.buffer` carries WAV, or `pcm` (raw little-endian PCM16)
when `CLOUD_TTS_AUDIO_RAW_PCM` is set — the documented fallback for a robot
player that expects bare samples. No language stage is applied: OpenAI voices
the text in whatever language it is written (Russian or Kazakh), so no
translation happens here.
"""

import logging
import time

from src.config import settings
from src.services.openai_client import get_client
from src.services.synthesis.base import (
    SynthesisResult,
    UnsupportedLanguageError,
)

logger = logging.getLogger(__name__)


class OpenAITTSEngine:
    """`TTSEngine` backed by OpenAI Text-to-Speech."""

    def synthesize(self, text: str, language: str) -> SynthesisResult:
        """Synthesize `text` (Russian/Kazakh) to audio via OpenAI TTS.

        `language` is validated against the supported set and used for logging;
        OpenAI infers the spoken language from the text itself, so no separate
        language/voice mapping is required.
        """
        if language not in settings.SUPPORTED_LANGUAGES:
            supported = ", ".join(settings.SUPPORTED_LANGUAGES)
            msg = f"unsupported language {language!r}; supported: {supported}"
            raise UnsupportedLanguageError(msg)

        # wav -> RIFF container; pcm -> raw 24 kHz mono signed 16-bit LE samples.
        response_format = "pcm" if settings.CLOUD_TTS_AUDIO_RAW_PCM else "wav"

        client = get_client()
        start = time.monotonic()
        result = client.audio.speech.create(
            model=settings.OPENAI_TTS_MODEL,
            voice=settings.OPENAI_TTS_VOICE,
            input=text,
            response_format=response_format,
        )
        # The SDK returns a binary response; `.content` is the raw audio bytes.
        audio = result.content
        synthesis_time_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "openai tts synthesized %d chars (%s) -> %d bytes in %d ms (format=%s)",
            len(text),
            language,
            len(audio),
            synthesis_time_ms,
            response_format,
        )
        return SynthesisResult(
            audio=audio,
            sample_rate=settings.AUDIO_SAMPLE_RATE_HZ,
            channels=settings.AUDIO_CHANNELS,
            synthesis_time_ms=synthesis_time_ms,
        )


# Module-level singleton — mirrors the `mqtt_service` / `zmq_service` pattern.
openai_tts_engine = OpenAITTSEngine()
