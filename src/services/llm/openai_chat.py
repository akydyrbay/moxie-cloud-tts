"""OpenAI response-generation stage.

Origin: new for the OpenAI speech pipeline. Takes the recognized Russian/Kazakh
text and asks OpenAI for a reply — directly in the same language, with NO
translation to English (steered by `OPENAI_SYSTEM_PROMPT`, reinforced per-call
with the detected language).

Isolated in its own module (+ `ChatResult`) so it can be tested on its own:
feed it text, assert a non-empty reply — no STT/TTS or robot required.
"""

import logging
import time
from dataclasses import dataclass

from src.config import settings
from src.services.openai_client import get_client
from src.services.synthesis.base import UnsupportedLanguageError

logger = logging.getLogger(__name__)

# Human-readable language names to reinforce the "reply in this language" hint.
_LANGUAGE_NAMES = {"ru": "Russian", "kk": "Kazakh"}


@dataclass(frozen=True)
class ChatResult:
    """Generated reply text plus the language it is in."""

    text: str
    language: str
    chat_time_ms: int


def respond(
    text: str,
    language: str,
    history: list[dict[str, str]] | None = None,
) -> ChatResult:
    """Generate a reply to `text`, in the same `language` (short code).

    `history` is an optional prior turn list of `{"role","content"}` messages
    (OpenAI chat format) used by the remote-chat brain for multi-turn context.
    Raises `UnsupportedLanguageError` for unknown languages. The reply is
    produced directly in `language`; there is no translation stage.
    """
    if language not in settings.SUPPORTED_LANGUAGES:
        supported = ", ".join(settings.SUPPORTED_LANGUAGES)
        msg = f"unsupported language {language!r}; supported: {supported}"
        raise UnsupportedLanguageError(msg)

    language_name = _LANGUAGE_NAMES.get(language, language)
    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                f"{settings.OPENAI_SYSTEM_PROMPT}\n"
                f"The user is speaking {language_name}. "
                f"You MUST reply only in {language_name}."
            ),
        },
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})

    client = get_client()
    start = time.monotonic()
    completion = client.chat.completions.create(
        model=settings.OPENAI_CHAT_MODEL,
        messages=messages,
    )
    chat_time_ms = int((time.monotonic() - start) * 1000)

    reply = (completion.choices[0].message.content or "").strip()

    logger.info(
        "openai chat replied to %d chars (%s) -> %d chars in %d ms",
        len(text),
        language,
        len(reply),
        chat_time_ms,
    )
    return ChatResult(text=reply, language=language, chat_time_ms=chat_time_ms)
