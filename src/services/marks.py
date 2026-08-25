"""Speech marks for `CloudTTSResponse.marks` — lip-sync and utterance timing.

`TTSMark {time, start, end, type, value}` is field-for-field Amazon Polly's
speech-mark schema, so that is the contract we implement:

- `time`        milliseconds from the start of the audio
- `start`/`end` byte offsets into the UTF-8 spoken text (words/sentences only)
- `type`        `sentence` | `word` | `viseme` (Polly also has `ssml`, emitted
                for `<mark name="..."/>` tags; telehealth lines carry none)
- `value`       the sentence/word text, or the viseme code

Why this module exists: we were publishing an empty `marks` list, and Moxie
spoke the first telehealth line **with a motionless mouth**. Marks drive
lip-sync. They do **not** make the second puppet line play — that was tested
on-robot (`marks=32` on every line, only line 1 heard).

OpenAI's speech API returns audio only, with no marks, so these are *derived*
from the text and the measured audio duration. Timings are estimates: time is
spread evenly across viseme-bearing characters rather than following real
phoneme boundaries. Good enough for the mouth to move roughly in time.
"""

import re

from src.protos.embodied.unity.CloudTTS_pb2 import TTSMark

MARK_SENTENCE = "sentence"
MARK_WORD = "word"
MARK_VISEME = "viseme"

# Polly's "mouth at rest" viseme. Emitted last so the utterance has an explicit
# end rather than trailing off after the final sound.
VISEME_SILENCE = "sil"

_WORD_RE = re.compile(r"\S+")
# Keeps the terminator with the sentence it ends.
_SENTENCE_RE = re.compile(r"[^.!?…]+[.!?…]*", re.UNICODE)

# Grapheme -> Polly viseme, grouped by place of articulation (which is what a
# viseme represents, so letters that sound different but look the same on the
# mouth share a code). Cyrillic covers Russian plus the extra Kazakh letters
# (ә ғ қ ң ө ұ ү һ і); Latin is included so mixed text still animates.
# Every character appears in exactly one group.
_VISEME_GROUPS: dict[str, str] = {
    "p": "пбмpbm",          # bilabial
    "f": "фвfv",            # labiodental
    "s": "сзsxzц",          # sibilant
    "S": "шщчжj",           # postalveolar
    "t": "тднлtdnl",        # alveolar
    "k": "кгхқғңһkghqc",    # velar / glottal
    "r": "рr",              # rhotic
    "i": "иійыi",           # close front
    "e": "еэe",             # mid front
    "a": "аяәa",            # open
    "o": "оөёo",            # mid back rounded
    "u": "уұүюwu",          # close back rounded
    # Soft/hard signs (ь ъ) and 'y' are deliberately absent: they are not
    # pronounced on their own, so giving them a mouth shape would push every
    # following viseme out of sync.
}

_VISEMES: dict[str, str] = {
    char: viseme
    for viseme, chars in _VISEME_GROUPS.items()
    for char in chars
}


def viseme_for(char: str) -> str | None:
    """Polly viseme code for a character, or None if it makes no mouth shape.

    Soft/hard signs (ь ъ) and punctuation deliberately map to nothing: they are
    not pronounced, so giving them a mouth shape would desynchronise the rest.
    """
    return _VISEMES.get(char.lower())


def _byte_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Character slice -> UTF-8 byte offsets, which is what Polly reports."""
    return (
        len(text[:start].encode("utf-8")),
        len(text[:end].encode("utf-8")),
    )


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """Character spans of each sentence, terminator included."""
    return [
        (m.start(), m.end())
        for m in _SENTENCE_RE.finditer(text)
        if m.group().strip()
    ]


def build_marks(text: str, duration_ms: int) -> list[TTSMark]:
    """Derive Polly-style speech marks for `text` spoken over `duration_ms`.

    Marks come out in time order: each sentence at the moment its first word
    starts, each word, the visemes inside it, and a closing `sil` at the end of
    the audio.

    Audio with nothing speakable in it (the silence fallback, punctuation-only
    text) still gets that closing `sil` on its own, so every buffer we publish
    carries a terminated utterance. Only a zero-length buffer yields no marks.
    """
    if duration_ms <= 0:
        return []

    closing = TTSMark(time=duration_ms, type=MARK_VISEME, value=VISEME_SILENCE)

    words = [(m.group(), m.start(), m.end()) for m in _WORD_RE.finditer(text)]
    # Time is apportioned per viseme-bearing character, so a long word holds the
    # mouth longer than a short one.
    units = sum(1 for word, _, _ in words for ch in word if viseme_for(ch))
    if not units:
        return [closing]
    per_unit = duration_ms / units

    sentences = _sentence_spans(text)
    next_sentence = 0
    marks: list[TTSMark] = []
    elapsed = 0.0

    for word, char_start, char_end in words:
        # Open any sentence this word is the first word of, at the word's time.
        while (
            next_sentence < len(sentences)
            and sentences[next_sentence][0] <= char_start
        ):
            span_start, span_end = sentences[next_sentence]
            start, end = _byte_span(text, span_start, span_end)
            marks.append(
                TTSMark(
                    time=int(elapsed),
                    type=MARK_SENTENCE,
                    value=text[span_start:span_end].strip(),
                    start=start,
                    end=end,
                ),
            )
            next_sentence += 1

        start, end = _byte_span(text, char_start, char_end)
        marks.append(
            TTSMark(
                time=int(elapsed),
                type=MARK_WORD,
                value=word,
                start=start,
                end=end,
            ),
        )
        for char in word:
            viseme = viseme_for(char)
            if viseme is None:
                continue
            marks.append(
                TTSMark(time=int(elapsed), type=MARK_VISEME, value=viseme),
            )
            elapsed += per_unit

    marks.append(closing)
    return marks


__all__ = [
    "MARK_SENTENCE",
    "MARK_VISEME",
    "MARK_WORD",
    "VISEME_SILENCE",
    "build_marks",
    "viseme_for",
]
