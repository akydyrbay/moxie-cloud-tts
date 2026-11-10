"""Allowlisted default assets from OpenMoxie's AssetBundleMasterManifest.csv."""

from enum import StrEnum
from html import escape
import json


class Behavior(StrEnum):
    SPIN = "bht_spin_360"
    TURN_LEFT = "bht_180_turn_left"
    TURN_RIGHT = "bht_180_turn_right"
    ARMS_IN = "bht_arms_in"
    ARMS_OUT = "bht_arms_out"
    ARMS_UP = "bht_arms_up_then_open"
    WAVE = "bht_back_and_forth_arm_wave"
    CELEBRATE = "bht_celebration_dance_01"


def output_markup(text: str, behavior: Behavior | None = None) -> str:
    """Only generated commands are executable; caller text is always escaped."""
    mark = ""
    if behavior is not None:
        data = {
            "transition": 0.5, "duration": 1.0, "repeat": 1,
            "layerBlendInTime": 0.5, "layerBlendOutTime": 0.5,
            "blocking": False, "action": 0, "variableName": "",
            "variableValue": "", "eventName": "Gesture_None", "lifetime": 0,
            "category": "None", "behaviour": Behavior(behavior).value, "Track": "",
        }
        encoded = json.dumps(data, separators=(",", ":")).replace('"', '+')
        mark = f'<mark name="cmd:behaviour-tree,data:{encoded}"/>'
    return f'<usel variant="2" genre="none">{mark}{escape(text, quote=False)}</usel>'
