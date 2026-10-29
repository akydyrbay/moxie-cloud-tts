"""Bounded live-turn output and scripted Q&A. All mutation is on the asyncio loop.

No timers fabricate turns. A claim is committed only after correlated audio is
published. Audio publication is NOT evidence of physical playback.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import logging
import re
import time
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.config import settings
from src.services.behaviors import Behavior
from src.services.device_state import device_state

Language = Literal["kk", "ru"]
Delivery = Literal["next_turn", "silence_only"]
TERMINAL = frozenset({"audio_sent", "completed", "failed", "cancelled", "expired", "interrupted"})
MAX_ACTIVE = 64
MAX_DEVICE_QUEUE = 8
MAX_HISTORY = 256
HISTORY_TTL_S = 600
logger = logging.getLogger(__name__)


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(min_length=1, max_length=500)
    answers: list[str] = Field(min_length=1, max_length=12)
    behavior: Behavior | None = None

    @field_validator("text")
    @classmethod
    def speakable_text(cls, text: str) -> str:
        if not any(c.isalnum() for c in text):
            raise ValueError("question must contain words or numbers")
        return text

    @field_validator("answers")
    @classmethod
    def valid_answers(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values]
        if any(not value or len(value) > 80 or not any(c.isalnum() for c in value) for value in cleaned):
            raise ValueError("answers must contain 1–80 characters of answer text")
        return cleaned


class ActionConflict(ValueError):
    pass


class QueueFull(ValueError):
    pass


# Exact number names, not substring matches: '10 or 11' must never pass as 10.
_NUMBER_NAMES: dict[str, str] = {}
for units, teens, tens in (
    ("ноль один два три четыре пять шесть семь восемь девять".split(),
     "десять одиннадцать двенадцать тринадцать четырнадцать пятнадцать шестнадцать семнадцать восемнадцать девятнадцать".split(),
     "двадцать тридцать сорок пятьдесят шестьдесят семьдесят восемьдесят девяносто".split()),
    ("нөл бір екі үш төрт бес алты жеті сегіз тоғыз".split(),
     ["он"] + [f"он {u}" for u in "бір екі үш төрт бес алты жеті сегіз тоғыз".split()],
     "жиырма отыз қырық елу алпыс жетпіс сексен тоқсан".split()),
    ("zero one two three four five six seven eight nine".split(),
     "ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split(),
     "twenty thirty forty fifty sixty seventy eighty ninety".split()),
):
    for n, name in enumerate(units + teens):
        _NUMBER_NAMES[name] = str(n)
    for n, name in enumerate(tens, 2):
        _NUMBER_NAMES[name] = str(n * 10)
        for digit in range(1, 10):
            _NUMBER_NAMES[f"{name} {units[digit]}"] = str(n * 10 + digit)
_NUMBER_NAMES.update({"сто": "100", "жүз": "100", "one hundred": "100", "нуль": "0"})


def normalized_answer(text: str) -> str:
    text = re.sub(r"(?<=[^\W\d_])-(?=[^\W\d_])", " ", text)
    words = re.findall(r"[\w]+|[-−]", text.casefold().replace("ё", "е"))
    if words and words[0] in {w.casefold() for w in settings.STT_WAKE_WORDS}:
        words.pop(0)
    value = " ".join(words)
    for prefix in ("ответ будет ", "ответ ", "это ", "будет ", "the answer is ", "it is ", "жауабы "):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    for suffix in (" болады",):
        if value.endswith(suffix):
            value = value[:-len(suffix)]
    if re.fullmatch(r"[0-9]{1,6}", value):
        return str(int(value))
    return _NUMBER_NAMES.get(value, value)


@dataclass
class Operation:
    id: str
    device_id: str
    kind: str
    language: str
    expires: float
    revision: int
    text: str = ""
    behavior: Behavior | None = None
    delivery: Delivery = "next_turn"
    questions: list[Question] = field(default_factory=list)
    max_attempts: int = 3
    status: str = "queued"
    reason: str = ""
    event_id: str | None = None
    index: int = 0
    attempts: int = 0
    token: object | None = None
    ended: float | None = None


@dataclass(frozen=True)
class TurnOutput:
    operation: Operation
    token: object
    text: str
    behavior: Behavior | None
    index: int
    attempts: int
    next_status: str

    @property
    def language(self) -> str:
        return self.operation.language


class LiveActions:
    def __init__(self) -> None:
        self._operations: OrderedDict[str, Operation] = OrderedDict()
        self._used_turns: OrderedDict[tuple[str, str], float] = OrderedDict()

    @staticmethod
    def _end(op: Operation, status: str, reason: str = "") -> None:
        op.status, op.reason, op.token = status, reason, None
        op.ended = time.monotonic()
        logger.info("live action id=%s device=%s status=%s event_id=%s reason=%s",
                    op.id, op.device_id, status, op.event_id, reason)

    def _prune(self) -> None:
        now = time.monotonic()
        for key, expires in list(self._used_turns.items()):
            if now >= expires:
                del self._used_turns[key]
        for key, op in list(self._operations.items()):
            if op.status not in TERMINAL:
                if op.revision != device_state.speech_revision:
                    self._end(op, "cancelled", "speech_mode_changed")
                elif now >= op.expires:
                    self._end(op, "expired", "deadline_elapsed")
            if op.ended is not None and now - op.ended >= HISTORY_TTL_S:
                del self._operations[key]
        terminal = [key for key, op in self._operations.items() if op.status in TERMINAL]
        for key in terminal[:-MAX_HISTORY]:
            del self._operations[key]

    def active(self, device_id: str | None = None) -> list[Operation]:
        self._prune()
        return [op for op in self._operations.values()
                if op.status not in TERMINAL and (device_id is None or op.device_id == device_id)]

    def _add(self, device_id: str, kind: str, language: str, ttl_s: float, **kwargs) -> dict:
        active = self.active()
        local = [op for op in active if op.device_id == device_id]
        if local and (kind == "activity" or any(op.kind == "activity" for op in local)):
            raise ActionConflict("Finish or cancel existing work before mixing speech and an activity")
        if len(active) >= MAX_ACTIVE or len(local) >= MAX_DEVICE_QUEUE:
            raise QueueFull("Live output queue is full")
        op = Operation(str(uuid4()), device_id, kind, language,
                       time.monotonic() + ttl_s, device_state.speech_revision, **kwargs)
        self._operations[op.id] = op
        logger.info("live action queued id=%s device=%s kind=%s ttl_s=%.1f", op.id, device_id, kind, ttl_s)
        return self.get(op.id)

    def enqueue(self, device_id: str, text: str, language: str, *,
                behavior: Behavior | None = None, delivery: Delivery = "next_turn",
                ttl_s: float = 120) -> dict:
        return self._add(device_id, "speech", language, ttl_s,
                         text=text, behavior=behavior, delivery=delivery)

    def start(self, device_id: str, questions: list[Question], language: str, *,
              ttl_s: float = 900, max_attempts: int = 3,
              behavior: Behavior | None = None) -> dict:
        return self._add(device_id, "activity", language, ttl_s,
                         questions=[q.model_copy(deep=True) for q in questions],
                         max_attempts=max_attempts, behavior=behavior)

    def get(self, operation_id: str) -> dict:
        self._prune()
        op = self._operations[operation_id]
        return {
            "id": op.id, "device_id": op.device_id, "kind": op.kind,
            "status": op.status, "reason": op.reason, "language": op.language,
            "event_id": op.event_id, "remaining_seconds": max(0, round(op.expires - time.monotonic(), 1)),
            "question_index": op.index if op.kind == "activity" else None,
            "question_count": len(op.questions), "attempts": op.attempts,
            "delivery": op.delivery,
        }

    def cancel(self, operation_id: str) -> dict:
        self._prune()
        op = self._operations[operation_id]
        if op.status not in TERMINAL:
            self._end(op, "cancelled", "operator_cancelled")
        return self.get(operation_id)

    def reset(self, device_id: str, reason: str = "session_reset") -> None:
        for op in self.active(device_id):
            self._end(op, "cancelled", reason)

    def native_selected(self, device_id: str, event_id: str) -> None:
        # A late native selection can follow publication of remote audio.
        for op in self._operations.values():
            if op.device_id == device_id and op.event_id == event_id and op.status not in {
                "cancelled", "expired", "failed", "interrupted",
            }:
                self._end(op, "interrupted", "native_content_selected")

    def leave_chat(self, device_id: str, *, boundary: bool) -> None:
        for op in self.active(device_id):
            if boundary or op.status != "queued":
                self._end(op, "cancelled", "left_live_chat")

    def supersede(self, device_id: str, event_id: str) -> None:
        for op in self.active(device_id):
            if op.status == "dispatching" and op.event_id != event_id:
                self._end(op, "interrupted", "newer_turn")

    def awaiting_answer(self, device_id: str) -> bool:
        return self.answer_key(device_id) is not None

    def answer_key(self, device_id: str) -> tuple[str, int] | None:
        for op in self.active(device_id):
            if op.kind == "activity" and op.status == "awaiting_answer":
                return op.id, op.index
        return None

    def answer_context(self, device_id: str) -> str:
        for op in self.active(device_id):
            if op.kind == "activity" and op.status == "awaiting_answer":
                return f"Moxie asked: {op.questions[op.index].text}"
        return ""

    def used_turn(self, device_id: str, event_id: str) -> bool:
        self._prune()
        return (device_id, event_id) in self._used_turns

    def claim(self, device_id: str, event_id: str, speech: str, *, ignored: bool = False) -> TurnOutput | None:
        if not event_id or ignored:
            return None
        active = self.active(device_id)
        if not active or (device_id, event_id) in self._used_turns:
            return None
        op = active[0]
        if op.status == "dispatching":
            return None
        if op.kind == "speech":
            if op.delivery == "silence_only" and speech.strip():
                return None
            text, behavior, index, attempts, next_status = op.text, op.behavior, 0, 0, "audio_sent"
        else:
            text, behavior, index, attempts, next_status = self._activity_output(op, speech)
        token = object()
        op.status, op.token, op.event_id = "dispatching", token, event_id
        logger.info("live action dispatch id=%s device=%s event_id=%s question=%d",
                    op.id, device_id, event_id, op.index)
        self._used_turns[device_id, event_id] = time.monotonic() + 3600
        while len(self._used_turns) > 1024:
            self._used_turns.popitem(last=False)
        return TurnOutput(op, token, text, behavior, index, attempts, next_status)

    @staticmethod
    def _activity_output(op: Operation, speech: str) -> tuple:
        kk = op.language == "kk"
        index, attempts = op.index, op.attempts
        question = op.questions[index]
        if op.status == "queued":
            return question.text, question.behavior, index, 0, "awaiting_answer"
        answer = normalized_answer(speech)
        if answer in {"стоп", "остановись", "тоқта", "stop"}:
            return ("Ойынды тоқтаттық." if kk else "Игра остановлена."), None, index, attempts, "cancelled"
        correct = bool(answer) and answer in {normalized_answer(a) for a in question.answers}
        if correct:
            prefix = "Жарайсың!" if kk else "Молодец!"
        else:
            attempts += 1
            if attempts < op.max_attempts:
                prefix = ("Тағы бір рет байқап көр." if kk else "Попробуй ещё раз.") if answer else (
                    "Жауабыңды айта ғой." if kk else "Скажи свой ответ.")
                return f"{prefix} {question.text}", None, index, attempts, "awaiting_answer"
            prefix = (f"Жауабы: {question.answers[0]}." if kk else f"Ответ: {question.answers[0]}.")
        index += 1
        if index == len(op.questions):
            ending = "Ойын аяқталды!" if kk else "Игра закончена!"
            return f"{prefix} {ending}", op.behavior if correct else None, index, 0, "completed"
        next_question = op.questions[index]
        return (f"{prefix} {next_question.text}", next_question.behavior or (op.behavior if correct else None),
                index, 0, "awaiting_answer")

    def current(self, output: TurnOutput) -> bool:
        self._prune()
        op = output.operation
        return op.status == "dispatching" and op.token is output.token

    def finish(self, output: TurnOutput, status: str) -> None:
        if not self.current(output):
            return
        op = output.operation
        if status != "published":
            self._end(op, "failed" if status == "failed" else "interrupted", status)
            return
        op.index, op.attempts, op.token = output.index, output.attempts, None
        if output.next_status in TERMINAL:
            self._end(op, output.next_status)
        else:
            op.status = output.next_status


live_actions = LiveActions()
