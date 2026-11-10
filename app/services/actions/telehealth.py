"""Robot interrupt, wake/sleep controls and observed TELEHEALTH state.

Normal speech uses the live action queue. This module retains the MQTT control
commands used by the HTTP API and records session state reported by the robot.
Sending a command does not confirm playback interruption or a mode transition.
"""


from __future__ import annotations

from app.core.context import Local

import logging

from app.services.transport.mqtt import mqtt_service

logger = logging.getLogger(__name__)

TELEHEALTH_COMMAND = "telehealth"

# RobotState names from OpenMoxie's TelehealthMessage schema.
STATE_READY = "READY"
STATE_IN_SESSION = "IN_SESSION"
STATE_EXITING = "EXITING"

# Per-device telehealth state as last reported by the robot.
_puppet_state = Local("puppet_state", dict)


def _send(device_id: str, message: dict) -> None:
    mqtt_service.publish_command_json(
        device_id,
        TELEHEALTH_COMMAND,
        {"command": TELEHEALTH_COMMAND, "message": message},
    )


def note_puppet_state(device_id: str, state: str | None) -> None:
    """Record a telehealth state update from the robot (activity-log)."""
    if not device_id or not state:
        return
    previous = _puppet_state.get(device_id)
    _puppet_state[device_id] = state
    if state != previous:
        logger.info("telehealth state %s: %s -> %s", device_id, previous, state)


def get_puppet_state(device_id: str) -> str | None:
    return _puppet_state.get(device_id)


def clear_puppet_state(device_id: str | None = None) -> None:
    """Drop cached state (tests / device disconnect)."""
    if device_id is None:
        _puppet_state.clear()
        return
    _puppet_state.pop(device_id, None)


def interrupt(device_id: str) -> None:
    """Request an interrupt without changing the robot-reported state."""
    logger.info("telehealth INTERRUPT to %s", device_id)
    _send(device_id, {"action": "INTERRUPT"})


def wakeup(device_id: str) -> None:
    """Wake a robot that is asleep with the wake button enabled."""
    logger.info("sending wakeup to %s", device_id)
    mqtt_service.publish_command_json(device_id, "wakeup", {"command": "wakeup"})


def sleep(device_id: str) -> None:
    """Put an awake robot to sleep (MQTT counterpart of `wakeup`)."""
    logger.info("sending sleep to %s", device_id)
    mqtt_service.publish_command_json(device_id, "sleep", {"command": "sleep"})


def handle_activity_log_telehealth(device_id: str, request: dict) -> bool:
    """Consume a telehealth activity-log payload. Returns True if handled."""
    if request.get("subtopic") != "telehealth":
        return False
    message = request.get("message") or {}
    state = message.get("state")
    if isinstance(state, str):
        note_puppet_state(device_id, state)
    else:
        logger.info("telehealth activity from %s: %s", device_id, message)
    return True


__all__ = [
    "STATE_EXITING",
    "STATE_IN_SESSION",
    "STATE_READY",
    "clear_puppet_state",
    "get_puppet_state",
    "handle_activity_log_telehealth",
    "interrupt",
    "note_puppet_state",
    "sleep",
    "wakeup",
]
