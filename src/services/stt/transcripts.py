"""Bounded original/wire transcript correlation, owned by the asyncio loop."""

from collections import OrderedDict
from dataclasses import dataclass
import time

from src.services.device_state import device_state

TTL_S = 120.0
MAX_ENTRIES = 256


@dataclass(frozen=True)
class Transcript:
    original: str
    wire: str
    language: str
    revision: int
    expires: float


_entries: OrderedDict[tuple[str, str], Transcript] = OrderedDict()


def clear(device_id: str | None = None) -> None:
    for key in list(_entries):
        if device_id is None or key[0] == device_id:
            del _entries[key]


def _prune() -> None:
    now = time.monotonic()
    for key, entry in list(_entries.items()):
        if entry.expires <= now or entry.revision != device_state.speech_revision:
            del _entries[key]


def remember(device_id: str, uuid: str, original: str, wire: str, language: str) -> None:
    _prune()
    _entries[device_id, uuid] = Transcript(
        original, wire, language, device_state.speech_revision, time.monotonic() + TTL_S,
    )
    _entries.move_to_end((device_id, uuid))
    while len(_entries) > MAX_ENTRIES:
        _entries.popitem(last=False)


def find(device_id: str, uuid: str) -> Transcript | None:
    _prune()
    return _entries.get((device_id, uuid))
