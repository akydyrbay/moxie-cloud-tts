"""Speech marks — Moxie's lip-sync and utterance boundaries.

Publishing an empty `marks` list made Moxie speak its first telehealth line with
a motionless mouth. The rules that matter to the firmware: marks are ordered by
time, no mark may point past the audio, and every buffer ends with a closing
`sil` so the utterance has an explicit end.
"""

from src.services.marks import (
    MARK_SENTENCE,
    MARK_SSML,
    MARK_VISEME,
    MARK_WORD,
    VISEME_SILENCE,
    build_marks,
    command_marks,
    viseme_for,
)

RU = "Привет! Как дела?"
KK = "Сәлем! Бүгін көңіл-күйің қалай?"

# The robot's entry-animation markup: command marks only, no speakable text.
ENTRY_MARKUP = (
    '<mark name="cmd:idlestate,data:{+idleState+:7}"/>'
    '<mark name="cmd:behaviour-tree,data:{+transition+:0.5}"/>'
)


def test_command_marks_echoes_command_only_markup_at_time_zero() -> None:
    """Command-only entry markup yields ssml marks the robot needs, at time 0."""
    marks = command_marks(ENTRY_MARKUP, 200)
    assert [m.type for m in marks] == [MARK_SSML, MARK_SSML]
    assert [m.time for m in marks] == [0, 0]
    assert marks[0].value == "cmd:idlestate,data:{+idleState+:7}"
    assert marks[1].value == "cmd:behaviour-tree,data:{+transition+:0.5}"


def test_command_marks_time_tracks_position_in_speech() -> None:
    """A mark after the text fires later than one before it."""
    marks = command_marks('<mark name="cmd:a"/>Привет<mark name="cmd:b"/>', 1000)
    assert [m.value for m in marks] == ["cmd:a", "cmd:b"]
    assert marks[0].time == 0
    assert marks[1].time > marks[0].time


def test_command_marks_ignores_plain_and_non_mark_tags() -> None:
    assert command_marks("Привет, как дела?", 1000) == []
    assert command_marks('<break time="0.5s"/>', 1000) == []
    assert command_marks("", 1000) == []


def of_type(marks, kind):
    return [m for m in marks if m.type == kind]


def test_marks_are_ordered_and_bounded() -> None:
    marks = build_marks(KK, 3400)
    times = [m.time for m in marks]
    assert times == sorted(times), "the robot consumes marks in time order"
    assert max(times) <= 3400, "no mark may fall past the end of the audio"


def test_utterance_always_ends_with_silence() -> None:
    for text in (RU, KK, "", "   ", "!!!", "one"):
        marks = build_marks(text, 1000)
        assert marks, f"{text!r} produced no marks at all"
        assert marks[-1].type == MARK_VISEME
        assert marks[-1].value == VISEME_SILENCE
        assert marks[-1].time == 1000


def test_silence_fallback_gets_only_the_closing_mark() -> None:
    """Nothing speakable, but the utterance must still be terminated."""
    assert len(build_marks("", 200)) == 1


def test_zero_length_audio_has_no_marks() -> None:
    assert build_marks(RU, 0) == []


def test_words_and_visemes_cover_the_text() -> None:
    marks = build_marks(RU, 2000)
    words = of_type(marks, MARK_WORD)
    assert [m.value for m in words] == ["Привет!", "Как", "дела?"]
    # Every letter that makes a mouth shape gets a viseme.
    expected = sum(1 for ch in RU if viseme_for(ch))
    assert len(of_type(marks, MARK_VISEME)) == expected + 1  # +1 closing sil


def test_sentences_are_marked_when_they_start() -> None:
    marks = build_marks(RU, 2000)
    sentences = of_type(marks, MARK_SENTENCE)
    assert [m.value for m in sentences] == ["Привет!", "Как дела?"]
    # The second sentence cannot begin at zero; it follows the first one's audio.
    assert sentences[0].time == 0
    assert sentences[1].time > 0


def test_offsets_are_utf8_byte_positions() -> None:
    """Polly reports byte offsets, and Cyrillic is two bytes per character."""
    marks = build_marks(RU, 2000)
    first = of_type(marks, MARK_WORD)[0]
    assert first.start == 0
    assert first.end == len("Привет!".encode())
    assert RU.encode()[first.start:first.end].decode() == "Привет!"


def test_timings_spread_across_the_whole_clip() -> None:
    """A mouth that finishes early (or runs long) is worse than none."""
    marks = build_marks(KK, 3400)
    visemes = of_type(marks, MARK_VISEME)
    assert visemes[0].time == 0
    # The last real viseme lands near the end, not bunched at the start.
    assert visemes[-2].time > 3400 * 0.9


def test_unpronounced_characters_get_no_viseme() -> None:
    for char in "ьъ .,!?-":
        assert viseme_for(char) is None, char


def test_viseme_codes_are_from_the_polly_set() -> None:
    polly = {"p", "f", "T", "t", "s", "S", "k", "r", "i", "e", "E",
             "a", "@", "o", "O", "u", "sil"}
    values = {m.value for m in build_marks(KK, 3400) if m.type == MARK_VISEME}
    assert values <= polly, values - polly
