"""Robot-triggered synthesis via inbound `CloudTTSRequest`.

The robot forwards a framed `CloudTTSRequest` on `/devices/{id}/events/zmq`
whenever it wants the cloud to voice a line. This module decodes it, strips any
markup, synthesizes with OpenAI, and replies with a `CloudTTSResponse` carrying
the request's `event_id`/`chunk_num` — the correlation the robot requires before
it will play anything.

Every inbound request produces exactly one response. An empty markup or a failed
synthesis publishes a short silence instead of nothing, because with cloud_tts
enabled a missing response leaves the robot stuck in MarkingUp until it
force-idles and sleeps.
"""

import logging
import re

from src.protos.embodied.unity.CloudTTS_pb2 import CloudTTSRequest
from src.services import speak_service
from src.services.language import detect_language

logger = logging.getLogger(__name__)

# The framing prefix an inbound CloudTTSRequest carries on the wire.
CLOUD_TTS_REQUEST_PREFIX = CloudTTSRequest.DESCRIPTOR.full_name.encode() + b":"

# Moxie speaks a markup language; anything tag-like must not be read aloud.
_MARKUP_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")


def strip_markup(markup: str) -> str:
    """Return speakable plain text from Moxie markup.

    Removes xml-like tags (`<mark .../>`, `<break/>`, mood/animation tags) and
    collapses the whitespace they leave behind. Passing raw markup to a TTS
    engine makes it read the tags out loud.
    """
    text = _MARKUP_TAG_RE.sub(" ", markup)
    return _WHITESPACE_RE.sub(" ", text).strip()


def parse_cloud_tts_request(payload: bytes) -> CloudTTSRequest | None:
    """Decode a framed `CloudTTSRequest`, or return None if it isn't one."""
    if not payload.startswith(CLOUD_TTS_REQUEST_PREFIX):
        return None
    serialized = payload[len(CLOUD_TTS_REQUEST_PREFIX):]
    request = CloudTTSRequest()
    request.ParseFromString(serialized)
    return request


async def handle_cloud_tts_request(
    device_id: str, request: CloudTTSRequest,
) -> dict:
    """Synthesize the request's markup and publish the response back to `device_id`."""
    text = strip_markup(request.markup or "")

    # Echo the id verbatim: substituting a random one guarantees the robot
    # discards the audio. Telehealth PLAY_OUTPUT lines often arrive with an
    # empty event_id — that is expected; still echo it exactly.
    event_id = request.event_id

    logger.info(
        "CloudTTSRequest fields device=%s event_id=%r chunk_num=%s "
        "timestamp=%s user_id=%r markup[:80]=%r",
        device_id,
        event_id,
        request.chunk_num,
        request.timestamp,
        request.user_id,
        (request.markup or "")[:80],
    )

    if not text:
        logger.warning(
            "CloudTTSRequest from %s has no speakable text (event_id=%r markup=%r); "
            "publishing silence",
            device_id,
            event_id,
            (request.markup or "")[:80],
        )
        return speak_service.publish_silence(
            device_id, event_id, request.chunk_num,
        )

    language = detect_language(text)
    logger.info(
        "handling CloudTTSRequest from %s event_id=%r chunk=%d chars=%d language=%s",
        device_id,
        event_id,
        request.chunk_num,
        len(text),
        language,
    )

    try:
        return await speak_service.speak(
            text,
            language,
            event_id=event_id,
            chunk_num=request.chunk_num,
            device_id=device_id,
        )
    except Exception:
        # speak() already published silence; swallow so the MQTT dispatcher logs
        # a single clear error rather than an unhandled task exception.
        logger.exception(
            "synthesis failed for event_id=%r (silence already sent)", event_id,
        )
        return {
            "status": "failed",
            "event_id": event_id,
            "chunk_num": request.chunk_num,
            "device_id": device_id,
        }


async def handle_payload(device_id: str, payload: bytes) -> dict | None:
    """Decode + handle a framed payload if it is a CloudTTSRequest."""
    request = parse_cloud_tts_request(payload)
    if request is None:
        return None
    return await handle_cloud_tts_request(device_id, request)


__all__ = [
    "CLOUD_TTS_REQUEST_PREFIX",
    "handle_cloud_tts_request",
    "handle_payload",
    "parse_cloud_tts_request",
    "strip_markup",
]
