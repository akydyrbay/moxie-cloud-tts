"""Lightweight language detection for ru/kk.

Inbound text (a `CloudTTSRequest.markup`, or recognized speech in a remote-chat
turn) carries no language field, so when a caller doesn't specify one we pick
between the two supported languages.

Heuristic: Kazakh uses nine Cyrillic letters absent from the Russian alphabet;
if any appear, treat the text as Kazakh. Text with no Cyrillic at all is not
evidence for either language, so it resolves to the configured default. This
only ever picks between supported languages — it never adds a translation stage.
"""

import re

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
    """Conservatively check generated assistant replies against their target.

    This validates remote-chat output; English source speech is translated in
    `translation.py` at the separate incoming-audio boundary.
    """
    if language not in {"kk", "ru"} or not any(c.isalnum() for c in text):
        return False
    if not any(c.isalpha() for c in text):
        return True  # e.g. an arithmetic answer
    if not has_cyrillic(text):
        return False
    latin = re.findall(r"[A-Za-z]+", text)
    cyrillic = re.findall(r"[\u0400-\u04ff]+", text)
    # Permit Roman numerals, acronyms and occasional proper names in otherwise
    # Cyrillic speech; reject ordinary English phrases, including mixed ones.
    english = {"hello", "hi", "sorry", "the", "you", "your", "is", "are", "and", "i", "my", "name", "thanks"}
    for token in latin:
        if re.fullmatch(r"[IVXLCDM]+", token) and (token != "I" or len(cyrillic) >= 2):
            continue
        if token.lower() in english:
            return False
        if len(cyrillic) < 2 or len(latin) > 2 or not token[0].isupper():
            return False
    if language == "ru":
        return not any(c in _KAZAKH_ONLY for c in text)
    if any(c in _KAZAKH_ONLY for c in text):
        return True
    # Shared Cyrillic does not identify Russian. Reject clear Russian signals,
    # but let the requested target govern ambiguous lines such as "Алма.".
    russian = {"привет", "здравствуй", "здравствуйте", "спасибо", "пожалуйста",
               "меня", "тебя", "зовут", "это", "очень", "почему", "который",
               "люблю", "хорошо", "мой", "мне", "тебе", "да", "нет"}
    return not russian.intersection(word.lower() for word in cyrillic)


def fallback_language(text: str) -> str:
    """Language for a canned line responding to `text`.

    Same as `detect_language`, but non-Cyrillic input (e.g. an English STT
    result) is reported explicitly as the default rather than implying the user
    actually spoke it.
    """
    if not has_cyrillic(text):
        return settings.DEFAULT_TTS_LANGUAGE
    return detect_language(text)
