"""Telehealth / puppet-mode control — "make Moxie say this, now".

Mirrors OpenMoxie's `send_telehealth_speech()` / puppet page:

    /devices/{id}/commands/telehealth
    {"command": "telehealth",
     "message": {"action": "PLAY_OUTPUT",
                 "output": {"text": "...", "markup": "..."}}}

Why this and not a direct `CloudTTSResponse` push: the robot only plays audio
whose `event_id` matches a request it is currently waiting on. Pushing
PLAY_OUTPUT makes the robot itself decide to speak, and — with `cloud_tts=1` —
issue a `CloudTTSRequest` that we answer with OpenAI audio.

Sequential speaks need an explicit reset. After a cloud line the robot may stay
out of READY (or keep an empty-`event_id` output slot) even though it still
emits a fresh CloudTTSRequest. OpenMoxie's puppet UI exposes Interrupt for this;
we INTERRUPT and wait for READY before every PLAY_OUTPUT so the Nth line is as
clean as the first.

Per OpenMoxie, `Output.markup` is the default speak path and `text` is only used
when markup is empty. We therefore send both (markup == text when no mood markup
is available), matching `send_telehealth_speech`.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.config import settings
from src.services.mqtt_service import mqtt_service

logger = logging.getLogger(__name__)

TELEHEALTH_COMMAND = "telehealth"

# RobotState names from OpenMoxie's TelehealthMessage schema.
STATE_READY = "READY"
STATE_IN_SESSION = "IN_SESSION"
STATE_EXITING = "EXITING"

# Per-device telehealth state as last reported by the robot.
_puppet_state: dict[str, str] = {}
# Waiters woken when a device reports READY.
_ready_waiters: dict[str, list[asyncio.Event]] = {}
# Serialize speak sequences per device so concurrent /tts/speak calls cannot
# interleave INTERRUPT/PLAY_OUTPUT.
_speak_locks: dict[str, asyncio.Lock] = {}


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
    if state == STATE_READY:
        for event in _ready_waiters.pop(device_id, []):
            event.set()


def get_puppet_state(device_id: str) -> str | None:
    return _puppet_state.get(device_id)


def clear_puppet_state(device_id: str | None = None) -> None:
    """Drop cached state (tests / device disconnect)."""
    if device_id is None:
        _puppet_state.clear()
        _ready_waiters.clear()
        _speak_locks.clear()
        return
    _puppet_state.pop(device_id, None)
    for event in _ready_waiters.pop(device_id, []):
        event.set()
    _speak_locks.pop(device_id, None)


def _speak_lock(device_id: str) -> asyncio.Lock:
    lock = _speak_locks.get(device_id)
    if lock is None:
        lock = asyncio.Lock()
        _speak_locks[device_id] = lock
    return lock


def play_output(device_id: str, text: str, markup: str | None = None) -> None:
    """Ask the robot to speak. Shape matches OpenMoxie `send_telehealth_speech`."""
    speak_markup = text if markup is None else markup
    logger.info(
        "telehealth PLAY_OUTPUT to %s (%d chars): %r",
        device_id,
        len(text),
        text[:80],
    )
    _send(
        device_id,
        {
            "action": "PLAY_OUTPUT",
            "output": {"text": text, "markup": speak_markup},
        },
    )


def interrupt(device_id: str) -> None:
    """Interrupt whatever the robot is currently saying / marking up."""
    logger.info("telehealth INTERRUPT to %s", device_id)
    # After interrupt the robot is no longer READY until it says so.
    if _puppet_state.get(device_id) == STATE_READY:
        _puppet_state[device_id] = "INTERRUPTING"
    _send(device_id, {"action": "INTERRUPT"})


def wakeup(device_id: str) -> None:
    """Wake a robot that is asleep with the wake button enabled."""
    logger.info("sending wakeup to %s", device_id)
    mqtt_service.publish_command_json(device_id, "wakeup", {"command": "wakeup"})


async def wait_until_ready(
    device_id: str,
    timeout_s: float | None = None,
) -> bool:
    """Block until the robot reports READY, or until timeout.

    Returns True if READY was observed, False on timeout. When we have never
    seen a state for this device (firmware quiet / first speak), returns True
    after a short settle so we do not deadlock bring-up.
    """
    timeout = (
        settings.SPEAK_READY_TIMEOUT_S if timeout_s is None else timeout_s
    )
    if _puppet_state.get(device_id) == STATE_READY:
        return True

    if device_id not in _puppet_state:
        # No telehealth state yet — don't block forever on first speak.
        settle = min(timeout, settings.SPEAK_SETTLE_S)
        logger.info(
            "no telehealth state for %s yet; settling %.1fs before PLAY_OUTPUT",
            device_id,
            settle,
        )
        await asyncio.sleep(settle)
        return True

    event = asyncio.Event()
    _ready_waiters.setdefault(device_id, []).append(event)
    # Re-check after registering: state may have flipped READY on another task.
    if _puppet_state.get(device_id) == STATE_READY:
        event.set()

    try:
        await asyncio.wait_for(event.wait(), timeout=timeout)
        return True
    except TimeoutError:
        logger.warning(
            "timed out after %.1fs waiting for telehealth READY on %s "
            "(last_state=%s); sending PLAY_OUTPUT anyway",
            timeout,
            device_id,
            _puppet_state.get(device_id),
        )
        return False
    finally:
        waiters = _ready_waiters.get(device_id)
        if waiters and event in waiters:
            waiters.remove(event)
        if waiters == []:
            _ready_waiters.pop(device_id, None)


async def speak_line(device_id: str, text: str) -> dict:
    """INTERRUPT → wait READY → PLAY_OUTPUT. Safe for repeated /tts/speak.

    One speak at a time per device. This is the fix for "first line plays, later
    lines are silent": leftover telehealth/cloud-TTS output state is cleared
    before each new line, and we do not fire PLAY_OUTPUT until the robot says
    it is READY (or a settle/timeout fires).
    """
    async with _speak_lock(device_id):
        started = time.monotonic()
        interrupt(device_id)
        # Brief pause so the INTERRUPT is processed before we wait/poll READY.
        await asyncio.sleep(settings.SPEAK_INTERRUPT_PAUSE_S)
        ready = await wait_until_ready(device_id)
        play_output(device_id, text)
        return {
            "status": "requested",
            "device_id": device_id,
            "chars": len(text),
            "ready": ready,
            "puppet_state": get_puppet_state(device_id),
            "waited_ms": int((time.monotonic() - started) * 1000),
            "detail": (
                "INTERRUPT + PLAY_OUTPUT sent. Moxie should emit a "
                "CloudTTSRequest which this service answers with OpenAI audio."
            ),
        }


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
    "play_output",
    "speak_line",
    "wait_until_ready",
    "wakeup",
]
