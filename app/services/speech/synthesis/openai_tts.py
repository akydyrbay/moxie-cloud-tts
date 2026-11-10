"""OpenAI Text-to-Speech engine.

Origin: new for the OpenAI speech pipeline; replaces the removed Google Cloud
TTS engine behind the same provider-agnostic `TTSEngine` interface in
`base.py`, so `speak_service` is unchanged apart from which engine it points at.

OpenAI TTS returns mono 24 kHz audio. We always ask for `pcm` (raw
little-endian PCM16) and publish those bare samples.

Asking OpenAI for `wav` directly writes a 0xFFFFFFFF unknown-length RIFF
header (see `app/services/speech/audio.py`). We always fetch `pcm`.

No language stage is applied: OpenAI voices the text in whatever language it is
written (Russian or Kazakh), so no translation happens here.
"""

import logging
import time

from app.config import settings
from app.services.providers.openai_client import get_client
from app.services.speech.synthesis.base import (
    SynthesisResult,
    UnsupportedLanguageError,
)

logger = logging.getLogger(__name__)


class OpenAITTSEngine:
    """`TTSEngine` backed by OpenAI Text-to-Speech."""

    def synthesize(self, text: str, language: str, timeout: float | None = None) -> SynthesisResult:
        """Synthesize `text` (Russian/Kazakh) to audio via OpenAI TTS.

        `language` is validated against the supported set and used for logging;
        OpenAI infers the spoken language from the text itself, so no separate
        language/voice mapping is required.

        `timeout` caps the HTTP timeouts to the caller's remaining budget, so a
        stalled request releases its provider worker by the speech deadline
        instead of holding it for a full OPENAI_TTS_TIMEOUT_S after it.
        """
        if language not in settings.SUPPORTED_LANGUAGES:
            supported = ", ".join(settings.SUPPORTED_LANGUAGES)
            msg = f"unsupported language {language!r}; supported: {supported}"
            raise UnsupportedLanguageError(msg)

        if timeout is None:
            timeout = settings.OPENAI_TTS_TIMEOUT_S
        timeout = min(timeout, settings.OPENAI_TTS_TIMEOUT_S)
        client = get_client().with_options(max_retries=0, timeout=timeout)
        start = time.monotonic()
        try:
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
            if not pcm or len(pcm) % 2:
                raise ValueError("TTS returned empty or incomplete PCM16 audio")
        except Exception as exc:
            # Logged here because a worker the caller stopped awaiting would
            # otherwise end silently; the error type is safe to log, text is not.
            logger.warning(
                "openai tts failed %d chars (%s) after %d ms (timeout=%.2fs) error=%s",
                len(text), language, int((time.monotonic() - start) * 1000),
                timeout, type(exc).__name__,
            )
            raise
        synthesis_time_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "openai tts synthesized %d chars (%s) -> %d bytes in %d ms "
            "(container=pcm, %d pcm bytes ≈ %.2fs)",
            len(text),
            language,
            len(pcm),
            synthesis_time_ms,
            len(pcm),
            len(pcm) / (settings.AUDIO_SAMPLE_RATE_HZ * settings.AUDIO_CHANNELS * 2),
        )
        return SynthesisResult(
            text=text,
            audio=pcm,
            sample_rate=settings.AUDIO_SAMPLE_RATE_HZ,
            channels=settings.AUDIO_CHANNELS,
            synthesis_time_ms=synthesis_time_ms,
        )


# Module-level singleton — mirrors the `mqtt_service` pattern.
openai_tts_engine = OpenAITTSEngine()
