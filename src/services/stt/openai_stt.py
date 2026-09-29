"""OpenAI Speech-to-Text stage.

Origin: new for the OpenAI speech pipeline. Converts a user's Russian/Kazakh
speech (audio bytes) into text with OpenAI transcription. This stage is
deliberately isolated (its own module + `TranscriptResult`) so it can be tested
on its own — feed it a WAV and assert the recognized text — without the chat or
TTS stages or a physical robot.

No translation is performed: the recognized text stays in the language spoken.
An explicit language hint is returned when supplied. Auto-detection does not
invent a language label from the configured TTS default.
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
    """Recognized text; language is empty when the API supplies no language label."""

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
    result language; when omitted OpenAI auto-detects. The default JSON result
    supplies text, not a reliable detected-language label, so ours stays empty.
    """
    if language is not None and language not in {"kk", "ru", "en"}:
        supported = "kk, ru, en"
        msg = f"unsupported language {language!r}; supported: {supported}"
        raise UnsupportedLanguageError(msg)

    # A socket timeout alone is not a total deadline. The robot handler also
    # bounds worker scheduling + this call, and discards late worker results.
    client = get_client().with_options(
        max_retries=0, timeout=settings.OPENAI_STT_TIMEOUT_S,
    )
    # The SDK wants a file-like tuple: (filename, bytes). filename extension
    # only hints the container type.
    request_kwargs: dict = {
        "model": settings.OPENAI_STT_MODEL,
        "file": (filename, audio),
        # No imperative prompt: the Sep 28 capture echoed it as human speech.
        "response_format": "json",
    }
    if language is not None:
        request_kwargs["language"] = language

    start = time.monotonic()
    result = client.audio.transcriptions.create(**request_kwargs)
    stt_time_ms = int((time.monotonic() - start) * 1000)

    text = (result.text or "").strip()
    resolved_language = language or ""

    logger.info(
        "openai stt transcribed %d audio bytes -> %d chars (%s) in %d ms",
        len(audio),
        len(text),
        resolved_language or "auto",
        stt_time_ms,
    )
    return TranscriptResult(
        text=text,
        language=resolved_language,
        stt_time_ms=stt_time_ms,
    )
