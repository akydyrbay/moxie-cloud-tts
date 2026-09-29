"""Robot microphone chunks → bounded OpenAI transcription → correlated FINAL.

OpenMoxie's zmq_stt_handler accumulates incremental 16 kHz mono PCM16 on all
VAD frames. Only an actual remote-chat turn enables recognition here. WAKEUP
keeps its entrance shim; other onboard modules never get fabricated transcripts.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass, field
import logging
import time
from typing import Literal

from google.protobuf.message import DecodeError

from src.config import settings
from src.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest, zmqSTTResponse
from src.services.audio import pcm_to_wav
from src.services.device_state import device_state
from src.services.mqtt_service import mqtt_service
from src.services.provider_work import provider_pool
from src.services.stt import input_translation, openai_stt, transcripts
from src.services.stt.validation import InvalidTranscript, pcm_energy, validate_transcript

logger = logging.getLogger(__name__)
STT_REQUEST_PREFIX = zmqSTTRequest.DESCRIPTOR.full_name.encode() + b":"
STT_SAMPLE_RATE_HZ = 16000
STT_CHANNELS = 1
STT_SAMPLE_WIDTH = 2
# Bound both recorded audio and abandoned streams, independently of provider wait.
MAX_AUDIO_BYTES = STT_SAMPLE_RATE_HZ * STT_SAMPLE_WIDTH * 30
SESSION_MAX_AGE_S = 30.0
MAX_PENDING_SESSIONS = 16
MAX_DEVICE_SESSIONS = 2
COMPLETED_TTL_S = 120.0
MAX_COMPLETED_SESSIONS = 1024

SessionKey = tuple[str, str]
Phase = Literal["wake-gate", "chat", "onboard"]


@dataclass
class _Listen:
    start_ms: int
    end_ms: int
    audio: bytearray = field(default_factory=bytearray)
    audio_bytes: int = 0
    frames: int = 0
    processing: bool = False
    expiry: asyncio.TimerHandle | None = None
    worker: asyncio.Task | None = None
    revision: int = field(default_factory=lambda: device_state.speech_revision)


# Accessed only on the asyncio loop. Paho schedules resets onto that same loop.
_sessions: dict[SessionKey, _Listen] = {}
_completed: OrderedDict[SessionKey, float] = OrderedDict()
_conversation_phase: dict[str, Phase] = {}


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _remember(key: SessionKey) -> None:
    now = time.monotonic()
    while _completed and next(iter(_completed.values())) <= now:
        _completed.popitem(last=False)
    _completed[key] = now + COMPLETED_TTL_S
    _completed.move_to_end(key)
    while len(_completed) > MAX_COMPLETED_SESSIONS:
        _completed.popitem(last=False)


def _discard_listens(device_id: str) -> None:
    transcripts.clear(device_id)
    for key, session in list(_sessions.items()):
        if key[0] != device_id:
            continue
        del _sessions[key]
        session.audio.clear()
        if session.expiry:
            session.expiry.cancel()
        if session.worker:
            session.worker.cancel()
        # Keep only a bounded, short-lived UUID tombstone. A late END must not
        # resurrect old audio or receive a wake phrase after exit/reconnect.
        _remember(key)
        logger.info("STT discarded stale listen device=%s uuid=%s", *key)


def note_conversation_active(device_id: str) -> None:
    """Only real remote router turns enter chat; notify is history-only."""
    _conversation_phase[device_id] = "chat"


def note_onboard_module(device_id: str, *, wake_gate: bool) -> None:
    """Discard chat audio/work on exit; re-arm injection only for entrances."""
    phase: Phase = "wake-gate" if wake_gate else "onboard"
    if _conversation_phase.get(device_id, "wake-gate") != phase:
        _discard_listens(device_id)
    _conversation_phase[device_id] = phase


def reset_conversation_state(device_id: str) -> None:
    """Invalidate this robot's audio and pending results on reconnect/reboot."""
    _discard_listens(device_id)
    _conversation_phase.pop(device_id, None)


def invalidate_mode(*, puppet: bool = False) -> None:
    """Settings are global in this service; invalidate every affected listen."""
    for device in set(_conversation_phase) | {key[0] for key in _sessions}:
        _discard_listens(device)
        if puppet:
            _conversation_phase[device] = "onboard"
    transcripts.clear()


def parse_stt_request(payload: bytes) -> zmqSTTRequest | None:
    """Decode a framed request; malformed frames cannot start a live listen."""
    if not payload.startswith(STT_REQUEST_PREFIX):
        return None
    request = zmqSTTRequest()
    try:
        request.ParseFromString(payload[len(STT_REQUEST_PREFIX):])
    except DecodeError:
        logger.warning("invalid zmqSTTRequest protobuf")
        return None
    return request


def _finish(
    key: SessionKey, session: _Listen, *, speech: str = "", error: str = "",
    elapsed_ms: int = 0,
    original: str = "", original_language: str = "", wire_language: str = "",
) -> dict | None:
    # Reset/exit may have invalidated the utterance while OpenAI was running.
    if _sessions.get(key) is not session:
        return None
    if session.revision != device_state.speech_revision:
        _discard_listens(key[0])
        return None
    del _sessions[key]
    if session.expiry:
        session.expiry.cancel()
    session.audio.clear()
    _remember(key)

    device_id, uuid = key
    phase = _conversation_phase.get(device_id, "wake-gate")
    response = zmqSTTResponse(
        uuid=uuid, type=zmqSTTResponse.ResponseType.FINAL, speech="" if error else speech,
        timestamp=_now_ms(), start_timestamp=session.start_ms,
        end_timestamp=max(session.start_ms, session.end_ms),
    )
    if error:
        # OpenMoxie's existing provider failure contract; never an assistant
        # apology masquerading as the child's recognized speech.
        response.error_code = 66
        response.error_message = error
        logger.warning(
            "STT FINAL error device=%s uuid=%s phase=%s error_code=66 "
            "audio_bytes=%d elapsed_ms=%d reason=%s",
            device_id, uuid, phase, session.audio_bytes, elapsed_ms, error,
        )
    else:
        if phase == "chat":
            # No await between remembering and publication: an immediate router
            # turn can recover the original text using exactly this UUID.
            transcripts.remember(device_id, uuid, original, speech, original_language)
            if settings.STT_BRIDGE_METADATA_ENABLED:
                response.language = wire_language
                response.original_speech = original
                response.original_language = original_language
        logger.info(
            "STT FINAL device=%s uuid=%s phase=%s audio_bytes=%d "
            "elapsed_ms=%d speech=%r",
            device_id, uuid, phase, session.audio_bytes, elapsed_ms, speech,
        )
    mqtt_service.publish_proto(
        settings.COMMAND_TOPIC_TEMPLATE.format(device=device_id), response,
    )
    return {"device_id": device_id, "uuid": uuid, "speech": speech,
            "error_code": response.error_code}


async def _recognize_and_bridge(wav: bytes, audio_ms: int, deadline: float):
    hint = settings.STT_INPUT_LANGUAGE
    result = await provider_pool.run(
        openai_stt.transcribe, wav, None if hint == "auto" else hint, "stt.wav",
        timeout=deadline - time.monotonic(),
    )
    original = result.text.strip()
    validate_transcript(original, audio_ms)
    wire = original
    if settings.STT_ENGLISH_BRIDGE_ENABLED:
        wire = await input_translation.to_english(original, remaining_s=deadline - time.monotonic())
    return original, wire, result.language


def _expire(key: SessionKey, session: _Listen) -> None:
    _finish(key, session, error="Microphone stream timed out before END_OF_SPEECH")


async def handle_stt_request(device_id: str, request: zmqSTTRequest) -> dict | None:
    """Accumulate START/SPEECH/END and answer each live UUID at most once."""
    if not request.uuid or request.vad not in {
        zmqSTTRequest.VADState.START_OF_SPEECH,
        zmqSTTRequest.VADState.SPEECH,
        zmqSTTRequest.VADState.END_OF_SPEECH,
    }:
        logger.warning("ignoring invalid STT identity/VAD device=%s uuid=%r vad=%s",
                       device_id, request.uuid, request.vad)
        return None

    key = (device_id, request.uuid)
    now = time.monotonic()
    while _completed and next(iter(_completed.values())) <= now:
        _completed.popitem(last=False)
    if key in _completed:
        logger.debug("STT ignored completed/retired uuid device=%s uuid=%s", *key)
        return None
    session = _sessions.get(key)
    if session and session.processing:
        logger.debug("STT ignored frame during transcription device=%s uuid=%s", *key)
        return None
    if session is None:
        full = (
            len(_sessions) >= MAX_PENDING_SESSIONS
            or sum(k[0] == device_id for k in _sessions) >= MAX_DEVICE_SESSIONS
        )
        timestamp = request.timestamp or _now_ms()
        session = _Listen(start_ms=timestamp, end_ms=timestamp)
        _sessions[key] = session
        if full:
            return _finish(key, session, error="Too many pending microphone streams")
        session.expiry = asyncio.get_running_loop().call_later(
            SESSION_MAX_AGE_S, _expire, key, session,
        )
        logger.info(
            "STT audio started device=%s uuid=%s phase=%s "
            "sample_rate=%d channels=%d sample_width=%d",
            device_id, request.uuid, _conversation_phase.get(device_id, "wake-gate"),
            STT_SAMPLE_RATE_HZ, STT_CHANNELS, STT_SAMPLE_WIDTH,
        )

    session.frames += 1
    session.audio_bytes += len(request.audio_content)
    session.end_ms = max(session.end_ms, request.timestamp or session.start_ms)
    if session.audio_bytes > MAX_AUDIO_BYTES:
        return _finish(key, session, error="Microphone audio exceeded 30-second limit")
    session.audio.extend(request.audio_content)
    logger.debug(
        "STT frame device=%s uuid=%s vad=%s frame_bytes=%d total_bytes=%d frames=%d",
        device_id, request.uuid, zmqSTTRequest.VADState.Name(request.vad),
        len(request.audio_content), session.audio_bytes, session.frames,
    )
    if request.vad != zmqSTTRequest.VADState.END_OF_SPEECH:
        return None

    session.processing = True
    started = time.monotonic()
    session.expiry.cancel()
    phase = _conversation_phase.get(device_id, "wake-gate")
    audio_ms = session.audio_bytes * 1000 // (STT_SAMPLE_RATE_HZ * STT_SAMPLE_WIDTH)
    # Keep audio timestamps in the robot's clock domain, even with clock skew.
    if not request.timestamp:
        session.end_ms = max(session.end_ms, session.start_ms + audio_ms)
    logger.info(
        "STT END device=%s uuid=%s phase=%s frames=%d audio_bytes=%d audio_ms=%d",
        device_id, request.uuid, phase, session.frames, session.audio_bytes, audio_ms,
    )
    if phase == "wake-gate" and settings.WAKEUP_TRIGGER_LINE:
        logger.info(
            "injecting canned STT line for %s uuid=%s phase=%s speech=%r",
            device_id, request.uuid, phase, settings.WAKEUP_TRIGGER_LINE,
        )
        return _finish(key, session, speech=settings.WAKEUP_TRIGGER_LINE)
    if phase != "chat":
        return _finish(key, session, error="Microphone transcription requires active remote chat")
    if await device_state.get_puppet_mode():
        return _finish(key, session, error="Microphone transcription is disabled in puppet mode")
    if not session.audio or not any(session.audio):
        return _finish(key, session, error="Microphone transcription received empty or silent audio")
    if len(session.audio) % STT_SAMPLE_WIDTH:
        return _finish(key, session, error="Microphone transcription received incomplete PCM16 sample")

    rms, active_ms = pcm_energy(bytes(session.audio))
    logger.info("STT audio quality device=%s uuid=%s rms=%.2f active_ms=%d audio_ms=%d",
                device_id, request.uuid, rms, active_ms, audio_ms)
    # Near-zero digital noise is not a useful recording. Do not gate ordinary
    # quiet speech on an uncalibrated VAD score.
    if rms < 2:
        return _finish(key, session, error="Microphone transcription received near-silent audio")

    speech, error, original, original_language = "", "", "", ""
    try:
        # Deliberately independent of the 24 kHz TTS configuration.
        wav = pcm_to_wav(bytes(session.audio), sample_rate=STT_SAMPLE_RATE_HZ,
                         channels=STT_CHANNELS, sample_width=STT_SAMPLE_WIDTH)
        session.audio.clear()
        # asyncio.to_thread lets the total deadline include worker queue wait.
        # A running sync HTTP call cannot be killed; it cannot publish either.
        session.worker = asyncio.create_task(_recognize_and_bridge(
            wav, audio_ms, started + settings.OPENAI_STT_TIMEOUT_S,
        ))
        original, speech, original_language = await asyncio.wait_for(
            session.worker, timeout=max(0.0, started + settings.OPENAI_STT_TIMEOUT_S - time.monotonic()),
        )
        logger.info(
            "STT validated/bridge device=%s uuid=%s revision=%d original_language=%s "
            "wire_language=%s original_chars=%d wire_chars=%d translated=%s original=%.160r wire=%.160r",
            device_id, request.uuid, session.revision, original_language or "unknown",
            "en" if settings.STT_ENGLISH_BRIDGE_ENABLED else original_language or "unknown",
            len(original), len(speech), original != speech, original, speech,
        )
    except TimeoutError:
        error = "Microphone transcription deadline exceeded"
    except asyncio.CancelledError:
        if _sessions.get(key) is session:
            _discard_listens(device_id)
            raise
        return None
    except InvalidTranscript as exc:
        error = str(exc)
    except Exception as exc:
        # No raw provider error bodies/credentials in the robot response.
        error = f"Microphone transcription failed ({type(exc).__name__})"
    return _finish(key, session, speech="" if error else speech, error=error,
                   original=original, original_language=original_language,
                   wire_language="en" if settings.STT_ENGLISH_BRIDGE_ENABLED else original_language,
                   elapsed_ms=int((time.monotonic() - started) * 1000))


async def handle_payload(device_id: str, payload: bytes) -> dict | None:
    request = parse_stt_request(payload)
    return None if request is None else await handle_stt_request(device_id, request)


__all__ = [
    "STT_REQUEST_PREFIX", "handle_payload", "handle_stt_request",
    "note_conversation_active", "note_onboard_module", "parse_stt_request",
    "reset_conversation_state",
]
