"""STT-channel replies (Goal 1 / M1: speak without puppet).

The robot streams framed `zmqSTTRequest` frames (mic audio + VAD state) on
`/devices/{id}/events/zmq` and waits for a `zmqSTTResponse` carrying the
recognized text. Rather than transcribe, this handler waits for
`END_OF_SPEECH` and, while the robot is still in onboard WAKEUP, replies with
`WAKEUP_TRIGGER_LINE` ("Moxie, let's go") so the day-start gate opens.

Once `OPENMOXIE_CHAT` is active, canned chat injection is off. The first spoken
line is the `REMOTE_CHAT_OPENERS` opener from remote-chat `command=prompt`,
voiced by Cloud TTS — not an STT "the child said this" stimulus. Real
transcription (accumulate `audio_content`, wrap as 16 kHz WAV, run OpenAI STT)
is M2; see SPEAK_WITHOUT_PUPPET_PLAN.md.
"""

import logging
import time

from src.config import settings
from src.protos.embodied.perception.audio.zmqSTT_pb2 import (
    zmqSTTRequest,
    zmqSTTResponse,
)
from src.services.mqtt_service import mqtt_service

logger = logging.getLogger(__name__)

# The framing prefix an inbound zmqSTTRequest carries on the wire.
STT_REQUEST_PREFIX = zmqSTTRequest.DESCRIPTOR.full_name.encode() + b":"

# Per (device_id, uuid) session start time in ms, so a reply can carry a
# best-effort start_timestamp. A session is dropped once it is answered. All
# access is from async handlers on the single event loop, so no lock is needed.
_session_start_ms: dict[tuple[str, str], int] = {}

# Devices that have entered OPENMOXIE_CHAT (past the onboard WAKEUP gate). Until
# a device is in this set we inject WAKEUP_TRIGGER_LINE to open the gate; after,
# listen sessions are left unanswered. remote_chat marks entry via
# note_conversation_active(); a reconnect clears it via reset_conversation_state().
_conversation_active: set[str] = set()


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def note_conversation_active(device_id: str) -> None:
    """Mark that `device_id` has entered OPENMOXIE_CHAT (past the WAKEUP gate)."""
    _conversation_active.add(device_id)


def reset_conversation_state(device_id: str) -> None:
    """Forget conversation phase for `device_id` (e.g. on reconnect/reboot)."""
    _conversation_active.discard(device_id)


def _injected_line(device_id: str) -> str:
    """The phrase to inject for `device_id`, given its conversation phase.

    Before OPENMOXIE_CHAT is active, inject the WAKEUP trigger so the robot
    leaves its onboard "start the day" gate. After that, inject nothing so the
    opener from remote-chat is what Moxie speaks next.
    """
    if device_id in _conversation_active:
        return ""
    return settings.WAKEUP_TRIGGER_LINE


def parse_stt_request(payload: bytes) -> zmqSTTRequest | None:
    """Decode a framed `zmqSTTRequest`, or return None if it isn't one."""
    if not payload.startswith(STT_REQUEST_PREFIX):
        return None
    request = zmqSTTRequest()
    request.ParseFromString(payload[len(STT_REQUEST_PREFIX):])
    return request


def _build_response(uuid: str, speech: str, start_ms: int) -> zmqSTTResponse:
    """Build a FINAL zmqSTTResponse echoing the session `uuid`."""
    resp = zmqSTTResponse()
    resp.uuid = uuid
    resp.type = zmqSTTResponse.ResponseType.FINAL
    resp.speech = speech
    now = _now_ms()
    resp.timestamp = now
    resp.start_timestamp = start_ms
    resp.end_timestamp = now
    return resp


async def handle_stt_request(
    device_id: str, request: zmqSTTRequest,
) -> dict | None:
    """Answer a completed listen session with the canned recognized line.

    Mid-stream frames (START/SPEECH) only record the session start and return
    None. On END_OF_SPEECH exactly one `zmqSTTResponse` is published back to the
    robot. When the chosen line is empty (default after OPENMOXIE_CHAT: no
    canned chat stimulus), nothing is sent.
    """
    key = (device_id, request.uuid)
    if key not in _session_start_ms:
        _session_start_ms[key] = request.timestamp or _now_ms()

    if request.vad != zmqSTTRequest.VADState.END_OF_SPEECH:
        return None

    start_ms = _session_start_ms.pop(key, _now_ms())

    line = _injected_line(device_id)
    if not line:
        logger.debug(
            "STT session %s ended for %s but no line to inject; not replying",
            request.uuid,
            device_id,
        )
        return None

    resp = _build_response(request.uuid, line, start_ms)
    topic = settings.COMMAND_TOPIC_TEMPLATE.format(device=device_id)
    phase = "chat" if device_id in _conversation_active else "wake-gate"
    logger.info(
        "injecting canned STT line for %s uuid=%s phase=%s speech=%r",
        device_id,
        request.uuid,
        phase,
        line,
    )
    mqtt_service.publish_proto(topic, resp)
    return {"device_id": device_id, "uuid": request.uuid, "speech": line}


async def handle_payload(device_id: str, payload: bytes) -> dict | None:
    """Decode + handle a framed payload if it is a zmqSTTRequest."""
    request = parse_stt_request(payload)
    if request is None:
        return None
    return await handle_stt_request(device_id, request)


__all__ = [
    "STT_REQUEST_PREFIX",
    "handle_payload",
    "handle_stt_request",
    "note_conversation_active",
    "parse_stt_request",
    "reset_conversation_state",
]
