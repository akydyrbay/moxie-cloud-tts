"""Cloud-TTS + OpenAI speech-pipeline router (Swagger UI at /docs).

Robot-facing:
    GET/POST /tts/cloud-tts   toggle cloud TTS on the device
    GET/POST /tts/puppet      toggle TELEHEALTH (puppet) mode
    POST     /tts/speak       queue ru/kk text on a live chat turn
    POST     /tts/activities  start scripted questions
    GET      /tts/behaviors   list built-in movement labels
    POST     /tts/behavior    queue movement on a live turn
    GET/DELETE /tts/operations/{id}  inspect/cancel queued work
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
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from src.config import settings
from src.services import face, telehealth, tts_service
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.behaviors import Behavior
from src.services.language import detect_language
from src.services.live_actions import ActionConflict, Delivery, Language, Question, QueueFull, live_actions
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
            "Legacy diagnostic mode; /tts/speak requires this to be False."
        ),
    )


class SpeakRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    text: str = Field(
        ...,
        min_length=1,
        max_length=1000,
        description="Text for Moxie to speak (Russian or Kazakh).",
        examples=["Привет! Как дела?", "Сәлем! Қалайсың?"],
    )
    device_id: str | None = Field(
        default=None,
        description="Target robot. Defaults to the most recently seen device.",
    )
    language: Language | None = None
    behavior: Behavior | None = None
    delivery: Delivery = "next_turn"
    ttl_s: float = Field(default=120, ge=1, le=600)

    @field_validator("text")
    @classmethod
    def speakable_text(cls, text: str) -> str:
        if not any(c.isalnum() for c in text):
            raise ValueError("text must contain words or numbers; use /tts/behavior for movement only")
        return text


class BehaviorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    behavior: Behavior
    device_id: str | None = None
    delivery: Delivery = "next_turn"
    ttl_s: float = Field(default=120, ge=1, le=600)


class ActivityRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str | None = None
    language: Language | None = None
    questions: list[Question] = Field(min_length=1, max_length=20)
    max_attempts: int = Field(default=3, ge=1, le=5)
    ttl_s: float = Field(default=900, ge=10, le=3600)
    correct_behavior: Behavior | None = None


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

    Legacy diagnostic mode. Disable it for live speech, scripts, and chat.
    """
    logger.info("received puppet mode request: enabled=%s", body.enabled)
    return await tts_service.set_puppet_mode(body.enabled)


async def _require_live_device(device_id: str | None) -> str:
    target = _require_device(device_id)
    if await device_state.get_puppet_mode() or not await device_state.get_cloud_tts():
        raise HTTPException(status_code=409, detail="Live output requires puppet mode off and cloud TTS on")
    if not device_registry.is_bootstrapped(target):
        raise HTTPException(status_code=409, detail="Wait for this robot to connect and finish bootstrap")
    if not settings.CLOUD_TTS_EMIT_MARKS:
        raise HTTPException(status_code=409, detail="Live output requires CLOUD_TTS_EMIT_MARKS=true")
    return target


def _queue_result(call, *args, **kwargs) -> dict:
    try:
        return call(*args, **kwargs)
    except ActionConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QueueFull as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.post("/speak", status_code=202)
async def speak(body: SpeakRequest) -> dict:
    """Queue exact kk/ru text for a real live-chat turn; never push puppet output.

    next_turn replaces that turn's GPT reply. silence_only waits for a turn
    without user speech. HTTP 202 means queued, not played. Queued speech and
    scripted activities are mutually exclusive on each robot.
    """
    target = await _require_live_device(body.device_id)
    return _queue_result(live_actions.enqueue, target, body.text, body.language or detect_language(body.text),
                         behavior=body.behavior, delivery=body.delivery, ttl_s=body.ttl_s)


@router.get("/behaviors")
async def list_behaviors() -> dict:
    """Built-in behavior labels; physical execution requires robot verification."""
    return {"behaviors": [b.value for b in Behavior]}


@router.post("/behavior", status_code=202)
async def queue_behavior(body: BehaviorRequest) -> dict:
    """Queue a movement without spoken text, on the next eligible live turn."""
    target = await _require_live_device(body.device_id)
    return _queue_result(live_actions.enqueue, target, "", settings.DEFAULT_TTS_LANGUAGE,
                         behavior=body.behavior, delivery=body.delivery, ttl_s=body.ttl_s)


@router.post("/activities", status_code=202)
async def start_activity(body: ActivityRequest) -> dict:
    """Start scripted questions on live turns; short answers need no wake word."""
    target = await _require_live_device(body.device_id)
    return _queue_result(live_actions.start, target, body.questions, body.language or settings.DEFAULT_TTS_LANGUAGE,
                         ttl_s=body.ttl_s, max_attempts=body.max_attempts, behavior=body.correct_behavior)


@router.get("/operations/{operation_id}")
async def operation_status(operation_id: str) -> dict:
    """audio_sent/completed means published, not confirmed robot playback."""
    try:
        return live_actions.get(operation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Operation not found or history expired") from exc


@router.delete("/operations/{operation_id}")
async def cancel_operation(operation_id: str) -> dict:
    """Cancel pending work; cannot retract audio already sent to the robot."""
    try:
        return live_actions.cancel(operation_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Operation not found or history expired") from exc


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
