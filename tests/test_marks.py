"""Speech marks — Moxie's lip-sync and utterance boundaries.

Publishing an empty `marks` list made Moxie speak its first telehealth line with
a motionless mouth. The rules that matter to the firmware: marks are ordered by
time, no mark may point past the audio, and every buffer ends with a closing
`sil` so the utterance has an explicit end.
"""

from app.services.speech.marks import MARK_WORD, build_marks

RU = "Привет! Как дела?"
KK = "Сәлем! Бүгін көңіл-күйің қалай?"

# The robot's entry-animation markup: command marks only, no speakable text.
ENTRY_MARKUP = (
    '<mark name="cmd:idlestate,data:{+idleState+:7}"/>'
    '<mark name="cmd:behaviour-tree,data:{+transition+:0.5}"/>'
)


def of_type(marks, kind):
    return [m for m in marks if m.type == kind]


def test_offsets_are_utf8_byte_positions() -> None:
    """Polly reports byte offsets, and Cyrillic is two bytes per character."""
    marks = build_marks(RU, 2000)
    first = of_type(marks, MARK_WORD)[0]
    assert first.start == 0
    assert first.end == len("Привет!".encode())
    assert RU.encode()[first.start:first.end].decode() == "Привет!"
