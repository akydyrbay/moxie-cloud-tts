"""Lightweight language detection for ru/kk.

Inbound text (a `CloudTTSRequest.markup`, or recognized speech in a remote-chat
turn) carries no language field, so when a caller doesn't specify one we pick
between the two supported languages.

Heuristic: Kazakh uses nine Cyrillic letters absent from the Russian alphabet;
if any appear, treat the text as Kazakh. Text with no Cyrillic at all is not
evidence for either language, so it resolves to the configured default. This
only ever picks between supported languages — it never adds a translation stage.
"""

from src.config import settings

# Kazakh-specific Cyrillic letters absent from the Russian alphabet.
_KAZAKH_ONLY = set("әғқңөұүһіӘҒҚҢӨҰҮҺІ")


def has_cyrillic(text: str) -> bool:
    """True if `text` contains any Cyrillic character."""
    return any("\u0400" <= ch <= "\u04ff" for ch in text)


def detect_language(text: str) -> str:
    """Infer kk/ru from Cyrillic; use the default only for ambiguous input."""
    if any(ch in _KAZAKH_ONLY for ch in text):
        return "kk"
    if has_cyrillic(text):
        return "ru"
    return settings.DEFAULT_TTS_LANGUAGE


def matches_output_language(text: str, language: str) -> bool:
    """Conservative script check for autonomous replies, not a translator.

    For this one-line milestone prefer a known localized fallback over English,
    mixed Latin/Cyrillic output, or an ambiguous Kazakh line without distinctive
    letters. Explicit Russian speech outside autonomous chat remains supported.
    """
    return (
        has_cyrillic(text)
        and not any("a" <= ch.lower() <= "z" for ch in text)
        and detect_language(text) == language
    )


def fallback_language(text: str) -> str:
    """Language for a canned line responding to `text`.

    Same as `detect_language`, but non-Cyrillic input (e.g. an English STT
    result) is reported explicitly as the default rather than implying the user
    actually spoke it.
    """
    if not has_cyrillic(text):
        return settings.DEFAULT_TTS_LANGUAGE
    return detect_language(text)
