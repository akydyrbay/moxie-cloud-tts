"""Telehealth / puppet-mode control — "make Moxie say this, now".

This is how an HTTP request turns into spoken audio on the robot. It mirrors
OpenMoxie's `send_telehealth_speech()`:

    /devices/{id}/commands/telehealth
    {"command": "telehealth",
     "message": {"action": "PLAY_OUTPUT", "output": {"text": "..."}}}

Why this and not a direct `CloudTTSResponse` push: the robot only plays audio
whose `event_id` matches a request it is currently waiting on. A server-invented
id is always discarded. Pushing PLAY_OUTPUT makes the robot itself decide to
speak, and — with `cloud_tts=1` — issue a real `CloudTTSRequest`, whose
`event_id` we echo back with OpenAI audio.

    POST /tts/speak
      -> PLAY_OUTPUT {text}                      (this module)
      -> robot emits CloudTTSRequest{markup, event_id=E}
      -> cloud_tts_request -> OpenAI TTS
      -> CloudTTSResponse{event_id=E, audio}     -> robot plays it

Puppet mode (`moxie_mode: TELEHEALTH`) keeps the robot awake and stops the
schedule from interrupting, which is what makes this reliable for bench testing.
Per OpenMoxie's docs, `Output.text` is used when `markup` is empty, so plain
text is sent deliberately — no markup tags can leak into synthesis.
"""

import logging

from src.services.mqtt_service import mqtt_service

logger = logging.getLogger(__name__)

TELEHEALTH_COMMAND = "telehealth"


def _send(device_id: str, message: dict) -> None:
    mqtt_service.publish_command_json(
        device_id,
        TELEHEALTH_COMMAND,
        {"command": TELEHEALTH_COMMAND, "message": message},
    )


def play_output(device_id: str, text: str) -> None:
    """Ask the robot to speak `text` (plain text, no markup).

    NB: the robot raises its `CloudTTSRequest` for a telehealth line with an
    *empty* `event_id` regardless of what we put in this payload (confirmed on
    the real robot — stamping `output.event_id` did not change it). So repeated
    telehealth speech cannot be disambiguated by a server-chosen id.
    """
    logger.info(
        "telehealth PLAY_OUTPUT to %s (%d chars): %r",
        device_id,
        len(text),
        text[:80],
    )
    _send(device_id, {"action": "PLAY_OUTPUT", "output": {"text": text}})


def interrupt(device_id: str) -> None:
    """Interrupt whatever the robot is currently saying."""
    logger.info("telehealth INTERRUPT to %s", device_id)
    _send(device_id, {"action": "INTERRUPT"})


def wakeup(device_id: str) -> None:
    """Wake a robot that is asleep with the wake button enabled."""
    logger.info("sending wakeup to %s", device_id)
    mqtt_service.publish_command_json(device_id, "wakeup", {"command": "wakeup"})


__all__ = ["interrupt", "play_output", "wakeup"]
