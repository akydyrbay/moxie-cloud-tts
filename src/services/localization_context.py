"""Bounded native-activity context shared by speech and input translation.

Only observed robot prompts and successfully localized lines belong here.
Pending GPT answers must never become a native activity's context.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
import json

from src.services.device_state import device_state


@dataclass
class _Activity:
    module: str = ""
    content: str = ""
    prompt: str = ""
    lines: list[dict[str, str]] = field(default_factory=list)
    revision: int = field(default_factory=lambda: device_state.speech_revision)


_activities: OrderedDict[str, _Activity] = OrderedDict()


def clear(device: str) -> None:
    _activities.pop(device, None)


def _get(device: str) -> _Activity | None:
    state = _activities.get(device)
    if state and state.revision != device_state.speech_revision:
        clear(device)
        return None
    return state


def select(device: str, module: str, content: str = "") -> None:
    state = _get(device)
    if (state is None or state.module != module
            or (content and state.content != content)):
        _activities[device] = _Activity(module=module, content=content)
    _activities.move_to_end(device)
    while len(_activities) > 64:
        _activities.popitem(last=False)


def observe_prompt(device: str, text: str) -> None:
    state = _get(device)
    if state:
        state.prompt = text[:2000]


def snapshot(device: str) -> str:
    state = _get(device)
    if state is None:
        return ""
    return json.dumps({"module": state.module, "content": state.content,
                       "robot_prompt": state.prompt, "recent_lines": state.lines},
                      ensure_ascii=False)


def remember(device: str, source: str, translated: str, language: str,
             *, context: str) -> None:
    # A slow translation from an old module cannot pollute a newer activity.
    state = _get(device)
    if state is None or snapshot(device) != context:
        return
    state.lines.append({"source": source[:2000], "localized": translated[:2000],
                        "language": language})
    state.lines = state.lines[-4:]
    state.prompt = source[:2000]
