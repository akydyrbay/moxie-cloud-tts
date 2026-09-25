"""Robot-triggered synthesis via inbound `CloudTTSRequest`.

The robot forwards a framed `CloudTTSRequest` on `/devices/{id}/events/zmq`
whenever it wants the cloud to voice a line. This module decodes it, strips any
markup, synthesizes with OpenAI, and replies with a `CloudTTSResponse` carrying
the request's `event_id`/`chunk_num` — the correlation the robot requires before
it will play anything.

For the wake-up milestone, a guarded override can replace the known local
English greeting/confirmation with WAKEUP_TTS_TEXT. Original request markup is
still passed through for command marks; logs distinguish requested/spoken text.

Every inbound request produces exactly one response. An empty markup or a failed
synthesis publishes a short silence instead of nothing, because with cloud_tts
enabled a missing response leaves the robot stuck in MarkingUp until it
force-idles and sleeps.
"""

import logging
import re

from src.config import settings
from src.protos.embodied.unity.CloudTTS_pb2 import CloudTTSRequest
from src.services import speak_service
from src.services.device_state import device_state
from src.services.language import detect_language, matches_output_language
from src.services.remote_chat import fallback_line, remote_chat_brain

logger = logging.getLogger(__name__)

# The framing prefix an inbound CloudTTSRequest carries on the wire.
CLOUD_TTS_REQUEST_PREFIX = CloudTTSRequest.DESCRIPTOR.full_name.encode() + b":"

# Moxie speaks a markup language; anything tag-like must not be read aloud.
_MARKUP_TAG_RE = re.compile(r"<[^>]*>")
_WHITESPACE_RE = re.compile(r"\s+")

# These complete local lines have live Cloud TTS slots in the robot logs.
# Match the whole speakable line, never arbitrary English. Do not gate this on
# the chat phase: OPENMOXIE_CHAT prompt can arrive BEFORE Excellent!'s request.
_DAY_START_LINE = "when you're ready to start the day, say, moxie, let's go"
_WAKEUP_LOCAL_LINES = frozenset({
    "good morning",
    "excellent",
    _DAY_START_LINE,
    f"that's better. {_DAY_START_LINE}",
    f"i'm back on solid ground. that's better. {_DAY_START_LINE}",
})


def _is_wakeup_line(text: str) -> bool:
    # Sep 23 firmware says `Good morning.`; older firmware used `!` and left
    # quote debris outside <usel>. Normalize only boundary punctuation so a
    # different sentence with the same prefix is never accidentally matched.
    normalized = text.replace("’", "'").casefold().strip(" \"'“”‘.!?…")
    return normalized in _WAKEUP_LOCAL_LINES


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
        "timestamp=%s user_id=%r markup[:80]=%r speakable=%r",
        device_id,
        event_id,
        request.chunk_num,
        request.timestamp,
        request.user_id,
        (request.markup or "")[:80],
        text,
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
            device_id, event_id, request.chunk_num, markup=request.markup or "",
        )

    puppet = await device_state.get_puppet_mode()
    module_language = remote_chat_brain.output_language(device_id)
    if (
        settings.WAKEUP_TTS_OVERRIDE_ENABLED
        and _is_wakeup_line(text)
        and not puppet
    ):
        logger.info(
            "CloudTTSRequest wakeup override device=%s event_id=%r chunk=%d "
            "requested_text=%r synthesis_text=%r",
            device_id,
            event_id,
            request.chunk_num,
            text,
            settings.WAKEUP_TTS_TEXT,
        )
        text = settings.WAKEUP_TTS_TEXT

    if (
        settings.REMOTE_CHAT_LANGUAGE_GUARD_ENABLED
        and module_language
        and not puppet
        and not matches_output_language(text, module_language)
    ):
        replacement = settings.WAKEUP_TTS_TEXT
        if not matches_output_language(replacement, module_language):
            replacement = fallback_line(module_language)
        logger.info(
            "CloudTTSRequest chat language override device=%s event_id=%r chunk=%d "
            "language=%s requested_text=%r synthesis_text=%r",
            device_id, event_id, request.chunk_num, module_language, text, replacement,
        )
        text = replacement

    language = detect_language(text)
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
        return await speak_service.speak(
            text,
            language,
            event_id=event_id,
            chunk_num=request.chunk_num,
            device_id=device_id,
            markup=request.markup or "",
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
