"""OpenAI response-generation stage.

Origin: new for the OpenAI speech pipeline. Takes the recognized Russian/Kazakh
text and asks OpenAI for a reply directly in the requested output language.
Input and history language do not select the response language.

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

_LANGUAGE_NAMES = {"kk": "Kazakh (қазақ тілі)", "ru": "Russian (русский язык)"}
# Models drift from Kazakh into Russian; say so explicitly for kk.
_LANGUAGE_EXTRAS = {
    "kk": " Do not use Russian words or phrases; use Kazakh vocabulary.",
    "ru": "",
}


def _language_instruction(language: str) -> str:
    return (
        f"Reply only in {_LANGUAGE_NAMES[language]}, in Cyrillic script, "
        "regardless of the language of the child's message or earlier turns. "
        "Do not switch languages and do not repeat English or other-language "
        f"sentences.{_LANGUAGE_EXTRAS[language]}"
    )


# Keep the spoken-response format even when OPENAI_SYSTEM_PROMPT is overridden
# in the environment. This applies to both supported output languages.
_RESPONSE_STYLE_INSTRUCTIONS = (
    "You are speaking aloud to a child. Every reply must contain at most 3 "
    "short sentences and at most 35 words in total; prefer 1 or 2 sentences. "
    "Use simple, familiar words and explain one idea at a time. "
    "Be warm and natural. Ask at most one short question per reply. "
    "Do not use lists, Markdown, technical jargon, or long explanations. "
    "Return only the words to speak in the selected response language."
)


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
    """Generate a reply to `text` in the requested output `language` (short code).

    `history` is an optional prior turn list of `{"role","content"}` messages
    (OpenAI chat format) used by the remote-chat brain for multi-turn context.
    Raises `UnsupportedLanguageError` for unknown languages. The reply is
    produced directly in `language`; there is no translation stage.
    """
    if language not in settings.SUPPORTED_LANGUAGES:
        supported = ", ".join(settings.SUPPORTED_LANGUAGES)
        msg = f"unsupported language {language!r}; supported: {supported}"
        raise UnsupportedLanguageError(msg)

    messages: list[dict[str, str]] = [
        {
            "role": "system",
            "content": (
                f"{settings.OPENAI_SYSTEM_PROMPT}\n"
                f"{_language_instruction(language)}\n"
                f"{_RESPONSE_STYLE_INSTRUCTIONS}"
            ),
        },
    ]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": text})

    client = get_client().with_options(max_retries=0, timeout=settings.OPENAI_CHAT_TIMEOUT_S)
    start = time.monotonic()
    completion = client.chat.completions.create(
        model=settings.OPENAI_CHAT_MODEL,
        messages=messages,
        max_completion_tokens=settings.OPENAI_CHAT_MAX_TOKENS,
    )
    chat_time_ms = int((time.monotonic() - start) * 1000)

    if not completion.choices or completion.choices[0].finish_reason != "stop":
        raise ValueError("Chat response was incomplete")
    message = completion.choices[0].message
    reply = (message.content or "").strip()
    if getattr(message, "refusal", None) or not reply:
        raise ValueError("Chat response was empty or refused")

    logger.info(
        "openai chat replied to %d chars (%s) -> %d chars in %d ms",
        len(text),
        language,
        len(reply),
        chat_time_ms,
    )
    return ChatResult(text=reply, language=language, chat_time_ms=chat_time_ms)
