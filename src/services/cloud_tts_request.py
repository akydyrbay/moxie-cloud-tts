"""Robot-triggered synthesis via inbound `CloudTTSRequest`.

The robot forwards a framed `CloudTTSRequest` on `/devices/{id}/events/zmq`
whenever it wants the cloud to voice a line. This module decodes it, strips any
markup, synthesizes with OpenAI, and replies with a `CloudTTSResponse` carrying
the request's `event_id`/`chunk_num` — the correlation the robot requires before
it will play anything.

Every autonomous English speech line is translated to the configured output
language before synthesis. Original markup is still used for command marks;
translation never changes module actions or the request's correlation IDs.

Every inbound request produces exactly one response. An empty markup or a failed
synthesis publishes a short silence instead of nothing, because with cloud_tts
enabled a missing response leaves the robot stuck in MarkingUp until it
force-idles and sleeps.
"""

import logging
import re
import time
from html import unescape

from src.config import settings
from src.protos.embodied.unity.CloudTTS_pb2 import CloudTTSRequest, ROBOT_TTS_REQUEST
from src.services import localization_context, speak_service, translation
from src.services.device_state import device_state
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
    text = unescape(_MARKUP_TAG_RE.sub(" ", markup))
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
    deadline = time.monotonic() + settings.REMOTE_REPLY_TIMEOUT_S

    # Echo the id verbatim: substituting a random one guarantees the robot
    # discards the audio. Telehealth PLAY_OUTPUT lines often arrive with an
    # empty event_id — that is expected; still echo it exactly.
    event_id = request.event_id
    puppet = await device_state.get_puppet_mode()
    context = "" if puppet else localization_context.snapshot(device_id)

    logger.info(
        "CloudTTSRequest fields device=%s event_id=%r chunk_num=%s "
        "timestamp=%s markup[:80]=%r speakable=%r",
        device_id,
        event_id,
        request.chunk_num,
        request.timestamp,
        (request.markup or "")[:80],
        text,
    )

    if not any(char.isalnum() for char in text):
        logger.warning(
            "CloudTTSRequest from %s has no speakable text (event_id=%r markup=%r); "
            "publishing silence",
            device_id,
            event_id,
            (request.markup or "")[:80],
        )
        return speak_service.publish_silence(
            device_id, event_id, request.chunk_num, markup=request.markup or "",
            request_source=ROBOT_TTS_REQUEST,
        )

    if not puppet:
        from src.services.remote_chat import remote_chat_brain

        remote_chat_brain.observe_native_audio(device_id, event_id)
    language = detect_language(text)
    if (
        settings.CLOUD_TTS_TRANSLATION_ENABLED
        and not puppet
        and translation.needs_translation(text)
    ):
        requested_text = text
        language = settings.DEFAULT_TTS_LANGUAGE
        try:
            text = await translation.translate(requested_text, language, deadline=deadline,
                                               context=context)
        except Exception as exc:
            logger.warning(
                "CloudTTSRequest translation failed device=%s event_id=%r chunk=%d "
                "target=%s error=%s; publishing silence", device_id, event_id,
                request.chunk_num, language, type(exc).__name__,
            )
            summary = speak_service.publish_silence(
                device_id, event_id, request.chunk_num, markup=request.markup or "",
                request_source=ROBOT_TTS_REQUEST,
            )
            return {**summary, "reason": "translation_failed"}
        logger.info(
            "CloudTTSRequest translation device=%s event_id=%r chunk=%d "
            "target=%s requested_text=%r synthesis_text=%r",
            device_id, event_id, request.chunk_num, language, requested_text, text,
        )
    logger.info(
        "handling CloudTTSRequest from %s event_id=%r chunk=%d chars=%d "
        "language=%s synthesis_text=%r",
        device_id,
        event_id,
        request.chunk_num,
        len(text),
        language,
        text,
    )

    try:
        result = await speak_service.speak(
            text,
            language,
            event_id=event_id,
            chunk_num=request.chunk_num,
            device_id=device_id,
            markup=request.markup or "",
            request_source=ROBOT_TTS_REQUEST,
            deadline=deadline,
        )
        if not puppet and result["status"] == "published":
            localization_context.remember(device_id, strip_markup(request.markup), text,
                                          language, context=context)
        return result
    except Exception:
        # speak() already published silence; swallow so the MQTT dispatcher logs
        # a single clear error rather than an unhandled task exception.
        logger.info(
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
