"""Cloud-TTS + OpenAI speech-pipeline router (Swagger UI at /docs).

Robot-facing:
    GET/POST /tts/cloud-tts   toggle cloud TTS on the device
    GET/POST /tts/puppet      toggle TELEHEALTH (puppet) mode
    POST     /tts/speak       make Moxie speak ru/kk text
    POST     /tts/interrupt   stop current playback
    POST     /tts/wakeup      wake a sleeping robot
    POST     /tts/sleep       put an awake robot to sleep
    GET      /tts/devices     which robots this service has seen
    GET      /tts/endpoint-qr OpenMoxie endpoint QR for Moxie's camera
    POST     /tts/wifi-qr     Wi-Fi credentials QR for Moxie's camera
"""

import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, SecretStr

from src.config import settings
from src.services import telehealth, tts_service
from src.services.devices import device_registry
from src.services.pairing_qr import (
    endpoint_qr_config,
    endpoint_qr_payload,
    render_qr,
    wifi_qr_payload,
)
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


class WifiQrRequest(BaseModel):
    """Credentials used once to render a Moxie-readable Wi-Fi QR image."""

    ssid: str = Field(..., min_length=1, max_length=32)
    password: SecretStr = Field(..., max_length=63)
    hidden: bool = False
    band: str = Field(default="any", pattern="^(any|2.4ghz|5ghz)$")


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


@router.get("/endpoint-qr")
async def endpoint_qr() -> Response:
    """Render a QR that relocates Moxie to this service's MQTT broker.

    Show the returned PNG fullscreen on a phone, tablet, or another display for
    Moxie to scan. The image carries only the MQTT endpoint, not API keys.
    """
    try:
        config = endpoint_qr_config()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    logger.info(
        "rendering Moxie endpoint QR host=%s port=%s disable_verify=%s",
        config.host,
        config.port,
        config.disable_verify,
    )
    return Response(
        content=render_qr(endpoint_qr_payload(config)),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/endpoint-qr/info")
async def endpoint_qr_info() -> dict:
    """Show the non-secret endpoint encoded by ``GET /tts/endpoint-qr``."""
    try:
        config = endpoint_qr_config()
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {
        "mqtt_host": config.host,
        "mqtt_port": config.port,
        "disable_verify": config.disable_verify,
        "project": config.project,
    }


@router.post("/wifi-qr")
async def wifi_qr(body: WifiQrRequest) -> Response:
    """Render a Wi-Fi QR for Moxie; credentials are never logged or returned."""
    bands = {
        "any": 0,
        "5ghz": 1,
        "2.4ghz": 2,
    }
    logger.info("rendering Moxie Wi-Fi QR ssid=%r band=%s hidden=%s", body.ssid, body.band, body.hidden)
    payload = wifi_qr_payload(
        ssid=body.ssid,
        password=body.password.get_secret_value(),
        hidden=body.hidden,
        band=bands[body.band],
    )
    return Response(
        content=render_qr(payload),
        media_type="image/png",
        headers={"Cache-Control": "no-store"},
    )


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
