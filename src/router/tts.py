"""Cloud-TTS + OpenAI speech-pipeline router (Swagger UI at /docs).

Robot-facing:
    GET/POST /tts/cloud-tts   toggle cloud TTS on the device
    GET/POST /tts/puppet      toggle TELEHEALTH (puppet) mode
    POST     /tts/speak       make Moxie speak ru/kk text  <- the real path
    POST     /tts/interrupt   stop current playback
    POST     /tts/wakeup      wake a sleeping robot
    GET      /tts/devices     which robots this service has seen

Bench-only (no robot required):
    POST /tts/transcribe  audio upload -> OpenAI STT  -> text
    POST /tts/respond     text         -> OpenAI chat -> reply text
    POST /tts/speak-direct text        -> OpenAI TTS  -> published immediately
"""

import logging
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel, Field

from src.config import settings
from src.services import speak_service, telehealth, tts_service
from src.services.devices import device_registry
from src.services.llm import openai_chat
from src.services.openai_client import OpenAIConfigError
from src.services.speak_service import NoTargetDeviceError
from src.services.stt import openai_stt
from src.services.synthesis.base import UnsupportedLanguageError

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


class SpeakDirectRequest(BaseModel):
    text: str = Field(..., min_length=1, examples=["Привет! Как дела?"])
    language: str = Field(default=settings.DEFAULT_TTS_LANGUAGE, examples=["ru", "kk"])
    event_id: str | None = Field(
        default=None,
        description=(
            "Must match an outstanding request on the robot or the audio is "
            "discarded. Leave empty only to test the publish path itself."
        ),
    )
    device_id: str | None = Field(default=None)


class RespondRequest(BaseModel):
    text: str = Field(
        ...,
        min_length=1,
        description="User utterance to respond to (Russian or Kazakh).",
        examples=["Привет! Как тебя зовут?", "Сәлем! Сенің атың кім?"],
    )
    language: str = Field(default=settings.DEFAULT_TTS_LANGUAGE, examples=["ru", "kk"])


def _to_http_error(exc: Exception) -> HTTPException:
    """Map pipeline errors to HTTP responses with useful detail."""
    if isinstance(exc, UnsupportedLanguageError):
        return HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc),
        )
    if isinstance(exc, NoTargetDeviceError):
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc),
        )
    if isinstance(exc, OpenAIConfigError):
        return HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc),
        )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=f"openai pipeline failed: {exc}",
    )


def _require_device(device_id: str | None) -> str:
    target = device_id or device_registry.primary()
    if not target:
        raise _to_http_error(
            NoTargetDeviceError(
                "no robot has connected to this broker yet; power on Moxie and "
                "check the logs for 'discovered device'",
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


@router.post("/speak-direct")
async def speak_direct(body: SpeakDirectRequest) -> dict:
    """Bench test of the synthesis + publish path only.

    Publishes a CloudTTSResponse immediately. Unless `event_id` matches a request
    the robot is actively waiting on, the robot WILL discard the audio — this
    verifies OpenAI TTS and MQTT framing, not playback. Use `/tts/speak` to make
    Moxie actually talk.
    """
    logger.info(
        "received /tts/speak-direct: language=%s chars=%d event_id=%s",
        body.language,
        len(body.text),
        body.event_id,
    )
    try:
        return await speak_service.speak(
            body.text,
            body.language,
            event_id=body.event_id,
            device_id=body.device_id,
        )
    except Exception as exc:
        logger.exception("speak-direct failed")
        raise _to_http_error(exc) from exc


@router.post("/transcribe")
async def transcribe(
    file: Annotated[
        UploadFile, File(description="Audio file (e.g. WAV) to transcribe."),
    ],
    language: Annotated[
        str | None, Form(description='Optional language hint, "ru" or "kk".'),
    ] = None,
) -> dict:
    """Bench test of the STT stage: audio upload -> OpenAI STT -> text.

    Not part of the robot pipeline — Moxie currently performs its own speech
    recognition. Consuming the robot's microphone stream is a later milestone.
    """
    audio = await file.read()
    logger.info(
        "received /tts/transcribe: %d bytes, filename=%s, language=%s",
        len(audio),
        file.filename,
        language,
    )
    try:
        result = openai_stt.transcribe(audio, language, file.filename or "audio.wav")
    except Exception as exc:
        logger.exception("transcribe failed")
        raise _to_http_error(exc) from exc
    return {
        "text": result.text,
        "language": result.language,
        "stt_time_ms": result.stt_time_ms,
    }


@router.post("/respond")
async def respond(body: RespondRequest) -> dict:
    """Bench test of the LLM stage: text -> OpenAI chat -> reply text."""
    logger.info(
        "received /tts/respond: language=%s chars=%d", body.language, len(body.text),
    )
    try:
        result = openai_chat.respond(body.text, body.language)
    except Exception as exc:
        logger.exception("respond failed")
        raise _to_http_error(exc) from exc
    return {
        "reply_text": result.text,
        "language": result.language,
        "chat_time_ms": result.chat_time_ms,
    }
