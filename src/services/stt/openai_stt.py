"""OpenAI Speech-to-Text stage.

Origin: new for the OpenAI speech pipeline. Converts a user's Russian/Kazakh
speech (audio bytes) into text with OpenAI transcription. This stage is
deliberately isolated (its own module + `TranscriptResult`) so it can be tested
on its own — feed it a WAV and assert the recognized text — without the chat or
TTS stages or a physical robot.

No translation is performed: the recognized text stays in the language spoken.
The detected language is returned so downstream stages (chat, TTS) can preserve
it.
"""

import logging
import time
from dataclasses import dataclass

from src.config import settings
from src.services.openai_client import get_client
from src.services.synthesis.base import UnsupportedLanguageError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptResult:
    """Recognized text plus the language it was recognized in."""

    text: str
    language: str
    stt_time_ms: int


def transcribe(
    audio: bytes,
    language: str | None = None,
    filename: str = "audio.wav",
) -> TranscriptResult:
    """Transcribe `audio` to text with OpenAI STT.

    `language` is an optional short code (e.g. "ru"/"kk"). When provided it is
    validated and passed to OpenAI as a recognition hint and echoed back as the
    result language; when omitted OpenAI auto-detects and we fall back to the
    configured default for our records.
    """
    if language is not None and language not in settings.SUPPORTED_LANGUAGES:
        supported = ", ".join(settings.SUPPORTED_LANGUAGES)
        msg = f"unsupported language {language!r}; supported: {supported}"
        raise UnsupportedLanguageError(msg)

    client = get_client()
    # The SDK wants a file-like tuple: (filename, bytes). filename extension
    # only hints the container type.
    request_kwargs: dict = {
        "model": settings.OPENAI_STT_MODEL,
        "file": (filename, audio),
    }
    if language is not None:
        request_kwargs["language"] = language

    start = time.monotonic()
    result = client.audio.transcriptions.create(**request_kwargs)
    stt_time_ms = int((time.monotonic() - start) * 1000)

    text = (result.text or "").strip()
    resolved_language = language or settings.DEFAULT_TTS_LANGUAGE

    logger.info(
        "openai stt transcribed %d audio bytes -> %d chars (%s) in %d ms",
        len(audio),
        len(text),
        resolved_language,
        stt_time_ms,
    )
    return TranscriptResult(
        text=text,
        language=resolved_language,
        stt_time_ms=stt_time_ms,
    )
