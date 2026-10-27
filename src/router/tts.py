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
    GET      /tts/face        saved face customization + asset catalog
    POST     /tts/face        save face assets (applied on next robot connect)
    DELETE   /tts/face        back to the default face (on next robot connect)
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import Response
from pydantic import BaseModel, Field, SecretStr

from src.config import settings
from src.services import face, telehealth, tts_service
from src.services.device_state import device_state
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


class FaceRequest(BaseModel):
    face_options: list[str] = Field(
        ...,
        max_length=len(face.FACE_LAYERS),
        description="Asset labels from GET /tts/face, at most one per layer. [] is the default face.",
        examples=[["MX_010_Eyes_Purple", "MX_120_Glasses_RedCat"]],
    )
    reset_child_id: bool = Field(
        default=False,
        description=(
            "OpenMoxie's emergency recovery: a new child_pii.id makes Moxie drop "
            "its cached face textures. Use only if a face change leaves Moxie broken."
        ),
    )


# Serializes save-to-file + in-memory update so they cannot disagree.
_face_lock = asyncio.Lock()

_FACE_APPLY_NOTE = (
    "Saved. Moxie receives it with its config the next time it connects to the "
    "broker (reboot Moxie). It is never pushed mid-session: face options live "
    "in child_pii, whose live re-push crashes Moxie's brain."
)


async def _store_face(options: list[str], reset_child_id: bool) -> dict:
    async with _face_lock:
        child_id = (await device_state.get_face())["child_id"]
        if reset_child_id:
            child_id = face.new_child_id()
        try:
            face.save(options, child_id)
        except OSError as exc:
            logger.error("could not save face file %s: %s", settings.MOXIE_FACE_FILE, exc)
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="could not save the face; nothing was changed",
            ) from exc
        await device_state.set_face(options, child_id)
    logger.info("saved Moxie face options %s (reset_child_id=%s); applies on next robot connect",
                options, reset_child_id)
    return {
        "face_options": options,
        "child_id_reset": reset_child_id,
        "untested": face.untested(options),
        "applies": "next robot connect",
        "note": _FACE_APPLY_NOTE,
    }


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
    """Put an awake Moxie to sleep at its next chat turn.

    Firmware runs `action: sleep` only in the response to a live turn, so the
    action is queued and sent with the reply to the child's next utterance or
    the robot's silence reprompt. The MQTT `sleep` command is also published;
    this firmware has not been seen to act on it.
    """
    target = _require_device(device_id)
    telehealth.sleep(target)
    remote_chat_brain.request_sleep(target)
    return {"status": "sleep_queued", "device_id": target}


@router.get("/face")
async def get_face() -> dict:
    """Saved face options and every asset label you can choose from."""
    saved = await device_state.get_face()
    return {
        "face_options": saved["face_options"],
        "applies": "next robot connect",
        "layers": face.catalog(),
    }


@router.post("/face")
async def set_face(body: FaceRequest) -> dict:
    """Save Moxie's face; it is applied the next time Moxie connects.

    Replaces the whole face: layers you leave out return to default. Only Eyes
    and Face_Colors are well tested (OpenMoxie); other layers are returned in
    `untested` and may make Moxie unstable.
    """
    try:
        options = face.normalize(body.face_options)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return await _store_face(options, body.reset_child_id)


@router.delete("/face")
async def reset_face() -> dict:
    """Return to the default face on the next robot connect."""
    return await _store_face([], reset_child_id=False)
