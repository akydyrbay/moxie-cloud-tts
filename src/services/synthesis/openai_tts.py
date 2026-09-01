"""OpenAI Text-to-Speech engine.

Origin: new for the OpenAI speech pipeline; replaces the removed Google Cloud
TTS engine behind the same provider-agnostic `TTSEngine` interface in
`base.py`, so `speak_service` is unchanged apart from which engine it points at.

OpenAI TTS returns mono 24 kHz audio. We always ask for `pcm` (raw
little-endian PCM16) and build the WAV container ourselves in
`src/services/audio.py`, publishing bare samples instead when
`CLOUD_TTS_AUDIO_RAW_PCM` is set.

Asking OpenAI for `wav` directly writes a 0xFFFFFFFF unknown-length RIFF
header (see `src/services/audio.py`). We always fetch `pcm` and optionally wrap
it locally. That header is a real defect; it is not what silences puppet line 2.

No language stage is applied: OpenAI voices the text in whatever language it is
written (Russian or Kazakh), so no translation happens here.
"""

import logging
import time

from src.config import settings
from src.services.audio import pcm_to_wav
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

        client = get_client()
        start = time.monotonic()
        result = client.audio.speech.create(
            model=settings.OPENAI_TTS_MODEL,
            voice=settings.OPENAI_TTS_VOICE,
            input=text,
            # Never "wav": OpenAI's streaming RIFF header declares an unknown
            # (0xFFFFFFFF) length that wedges the robot's speech output.
            response_format="pcm",
        )
        # The SDK returns a binary response; `.content` is the raw audio bytes —
        # here 24 kHz mono signed 16-bit little-endian samples, no header.
        pcm = result.content
        synthesis_time_ms = int((time.monotonic() - start) * 1000)

        if settings.CLOUD_TTS_AUDIO_RAW_PCM:
            audio = pcm
            container = "pcm"
        else:
            audio = pcm_to_wav(
                pcm,
                settings.AUDIO_SAMPLE_RATE_HZ,
                settings.AUDIO_CHANNELS,
            )
            container = "wav"

        logger.info(
            "openai tts synthesized %d chars (%s) -> %d bytes in %d ms "
            "(container=%s, %d pcm bytes ≈ %.2fs)",
            len(text),
            language,
            len(audio),
            synthesis_time_ms,
            container,
            len(pcm),
            len(pcm) / (settings.AUDIO_SAMPLE_RATE_HZ * settings.AUDIO_CHANNELS * 2),
        )
        return SynthesisResult(
            text=text,
            audio=audio,
            sample_rate=settings.AUDIO_SAMPLE_RATE_HZ,
            channels=settings.AUDIO_CHANNELS,
            synthesis_time_ms=synthesis_time_ms,
        )


# Module-level singleton — mirrors the `mqtt_service` pattern.
openai_tts_engine = OpenAITTSEngine()
