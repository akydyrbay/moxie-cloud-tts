"""Cloud-TTS + OpenAI speech-pipeline router (Swagger UI at /docs).

Robot-facing:
    GET/POST /tts/cloud-tts   toggle cloud TTS on the device
    GET/POST /tts/puppet      toggle TELEHEALTH (puppet) mode
    POST     /tts/speak       make Moxie speak ru/kk text
    POST     /tts/interrupt   stop current playback
    POST     /tts/wakeup      wake a sleeping robot
    POST     /tts/sleep       put an awake robot to sleep
    GET      /tts/devices     which robots this service has seen
"""

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from src.config import settings
from src.services import telehealth, tts_service
from src.services.devices import device_registry
from src.services.remote_chat import remote_chat_brain

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tts", tags=["cloud-tts"])


class CloudTtsToggleRequest(BaseModel):
    enabled: bool = Field(..., description="True to enable cloud TTS, False to disable.")


class PuppetToggleRequest(BaseModel):
    enabled: bool = Field(
        ...,
        description=(
            "True puts Moxie in TELEHEALTH (puppet) mode: awake, no schedule, "
            "speaks only what you push with /tts/speak."
        ),
    )


class SpeakRequest(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        description="Text for Moxie to speak (Russian or Kazakh).",
        examples=["Привет! Как дела?", "Сәлем! Қалайсың?"],
    )
    device_id: str | None = Field(
        default=None,
        description="Target robot. Defaults to the most recently seen device.",
    )


def _require_device(device_id: str | None) -> str:
    target = device_id or device_registry.primary()
    if not target:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "no robot has connected to this broker yet; power on Moxie and "
                "check the logs for 'discovered device'"
            ),
        )
    return target


@router.get("/devices")
async def list_devices() -> dict:
    """Robots seen on the broker this run (most recently active first)."""
    return {
        "devices": device_registry.known(),
        "primary": device_registry.primary(),
        "device_id_override": settings.DEVICE_ID or None,
    }


@router.get("/cloud-tts")
async def get_cloud_tts_status() -> dict:
    """Return the current device config, including the live cloud_tts flag."""
    return await tts_service.get_status()


@router.post("/cloud-tts")
async def set_cloud_tts_status(body: CloudTtsToggleRequest) -> dict:
    """Toggle cloud_tts live, without rebooting the robot.

    Updates in-memory state and pushes the config to connected robots over MQTT.
    """
    logger.info("received cloud_tts toggle request: enabled=%s", body.enabled)
    return await tts_service.request_toggle(body.enabled)


@router.get("/puppet")
async def get_puppet_status() -> dict:
    """Return whether TELEHEALTH (puppet) mode is currently enabled."""
    return {"enabled": await tts_service.get_puppet_mode()}


@router.post("/puppet")
async def set_puppet_status(body: PuppetToggleRequest) -> dict:
    """Enable/disable TELEHEALTH mode and push the config to the robot.

    Puppet mode keeps Moxie awake and stops the schedule from interrupting, so
    `/tts/speak` plays reliably. Disable it to run normal conversation.
    """
    logger.info("received puppet mode request: enabled=%s", body.enabled)
    return await tts_service.set_puppet_mode(body.enabled)


@router.post("/speak")
async def speak(body: SpeakRequest) -> dict:
    """Make Moxie speak `text` out loud (Russian or Kazakh).

    Waits until the robot has reported telehealth READY (or a short settle),
    then sends PLAY_OUTPUT. The robot emits a CloudTTSRequest; this service
    answers with OpenAI audio. Matches OpenMoxie's puppet speak path — do not
    INTERRUPT before every line; that is `POST /tts/interrupt`.

    Calls for the same device are serialized. Prefer leaving puppet mode
    enabled for the whole session — flipping it mid-session can crash the
    native stack.
    """
    target = _require_device(body.device_id)
    logger.info("received /tts/speak for %s: chars=%d", target, len(body.text))
    return await telehealth.speak_line(target, body.text)


@router.post("/interrupt")
async def interrupt(device_id: str | None = None) -> dict:
    """Interrupt whatever Moxie is currently saying."""
    target = _require_device(device_id)
    telehealth.interrupt(target)
    return {"status": "interrupted", "device_id": target}


@router.post("/wakeup")
async def wakeup(device_id: str | None = None) -> dict:
    """Wake a sleeping Moxie (requires wake button support enabled)."""
    target = _require_device(device_id)
    telehealth.wakeup(target)
    return {"status": "wakeup_sent", "device_id": target}


@router.post("/sleep")
async def sleep(device_id: str | None = None) -> dict:
    """Put an awake Moxie to sleep.

    Publishes the MQTT `sleep` command (mirror of `/tts/wakeup`) and a
    remote-chat `sleep` action on the last in-flight volley, which is the
    documented firmware path (`<sleep>` / `action: sleep`).
    """
    target = _require_device(device_id)
    telehealth.sleep(target)
    remote_chat_brain.request_sleep(target)
    return {"status": "sleep_sent", "device_id": target}
