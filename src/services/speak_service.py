"""Cloud-TTS synthesis: text -> OpenAI TTS -> CloudTTSResponse -> robot.

    text + language
      -> OpenAITTSEngine.synthesize()              (audio + synthesis time)
      -> build a valid CloudTTSResponse            (audio + ids + timings)
      -> mqtt_service.publish_cloud_tts_response() (framed, to the requesting device)

The robot only plays a response whose `event_id`/`chunk_num` match a request it
is currently waiting on, so both are echoed verbatim.

Critically, a response is published *even when synthesis fails*. With cloud_tts
enabled the robot suppresses its onboard TTS and waits in "MarkingUp"; if
nothing ever arrives it force-idles and goes to sleep. Publishing a short silent
buffer releases that wait instead of hanging the robot.

Every buffer goes through `audio.repair_wav()` first, so a container can never
declare more audio than it carries (see `src/services/audio.py`). `marks`
drive Moxie's mouth (see `src/services/marks.py`).
"""

import logging
import time
import uuid

from fastapi.concurrency import run_in_threadpool

from src.config import settings
from src.protos.embodied.unity.CloudTTS_pb2 import (
    REMOTECHAT_TTS_REQUEST,
    ROBOT_TTS_REQUEST,
    AudioBuffer,
    CloudTTSResponse,
    RequestSourceType,
)
from src.services.audio import duration_ms, pcm_to_wav, repair_wav
from src.services.devices import device_registry
from src.services.marks import build_marks
from src.services.mqtt_service import mqtt_service
from src.services.synthesis.base import (
    SynthesisResult,
    TTSEngine,
    UnsupportedLanguageError,
)
from src.services.synthesis.openai_tts import openai_tts_engine

logger = logging.getLogger(__name__)

_engine: TTSEngine = openai_tts_engine

# Silence published when synthesis fails, so the robot can leave MarkingUp.
_FALLBACK_SILENCE_MS = 200


class NoTargetDeviceError(RuntimeError):
    """Raised when no robot has been seen and no DEVICE_ID override is set."""


def configured_request_source() -> RequestSourceType:
    """Resolve `CLOUD_TTS_REQUEST_SOURCE` to the protobuf enum value."""
    if settings.CLOUD_TTS_REQUEST_SOURCE.strip().lower() == "remotechat":
        return REMOTECHAT_TTS_REQUEST
    return ROBOT_TTS_REQUEST


def _silence(duration_ms: int = _FALLBACK_SILENCE_MS) -> bytes:
    """Silent audio in whichever container the robot is configured to receive."""
    frames = int(settings.AUDIO_SAMPLE_RATE_HZ * duration_ms / 1000)
    pcm = b"\x00\x00" * frames * settings.AUDIO_CHANNELS
    if settings.CLOUD_TTS_AUDIO_RAW_PCM:
        return pcm
    return pcm_to_wav(
        pcm,
        settings.AUDIO_SAMPLE_RATE_HZ,
        settings.AUDIO_CHANNELS,
    )


def _build_response(
    result: SynthesisResult,
    *,
    event_id: str,
    chunk_num: int,
    request_source: RequestSourceType,
    total_time_ms: int,
) -> CloudTTSResponse:
    """Assemble a CloudTTSResponse for playback.

    The buffer is passed through `repair_wav()` so the container can never claim
    to be longer than the samples it carries.

    `marks` carries Polly-style viseme/word timings derived from `result.text`
    and the measured audio length (see `src/services/marks.py`).
    """
    audio = repair_wav(result.audio)
    marks = (
        build_marks(
            result.text,
            duration_ms(audio, result.sample_rate, result.channels),
        )
        if settings.CLOUD_TTS_EMIT_MARKS
        else []
    )
    return CloudTTSResponse(
        request_source=request_source,
        audio=AudioBuffer(
            buffer=audio,
            channels=result.channels,
            sample_rate=result.sample_rate,
        ),
        marks=marks,
        event_id=event_id,
        chunk_num=chunk_num,
        timestamp=int(time.time() * 1000),
        synthesis_time=result.synthesis_time_ms,
        total_time=total_time_ms,
        software_version=settings.SOFTWARE_VERSION,
        module_name=settings.MODULE_NAME,
    )


def _resolve_device(device_id: str | None) -> str:
    target = device_id or device_registry.primary()
    if not target:
        msg = (
            "no target device: no robot has published on this broker yet and "
            "DEVICE_ID is not set"
        )
        raise NoTargetDeviceError(msg)
    return target


def publish_silence(
    device_id: str,
    event_id: str,
    chunk_num: int = 0,
    request_source: RequestSourceType | None = None,
) -> dict:
    """Publish a correlated silent response so the robot stops waiting."""
    audio = _silence()
    result = SynthesisResult(
        audio=audio,
        sample_rate=settings.AUDIO_SAMPLE_RATE_HZ,
        channels=settings.AUDIO_CHANNELS,
        synthesis_time_ms=0,
    )
    # `result.text` is empty: no speech, so no visemes — `build_marks` still
    # closes the utterance with a trailing `sil`.
    response = _build_response(
        result,
        event_id=event_id,
        chunk_num=chunk_num,
        request_source=request_source or configured_request_source(),
        total_time_ms=0,
    )
    mqtt_service.publish_cloud_tts_response(device_id, response)
    logger.warning(
        "published SILENT fallback to %s event_id=%s chunk=%d (%d bytes)",
        device_id,
        event_id,
        chunk_num,
        len(audio),
    )
    return {
        "status": "published_silence",
        "event_id": event_id,
        "chunk_num": chunk_num,
        "audio_bytes": len(audio),
        "device_id": device_id,
    }


async def speak(
    text: str,
    language: str,
    event_id: str | None = None,
    chunk_num: int = 0,
    request_source: RequestSourceType | None = None,
    device_id: str | None = None,
) -> dict:
    """Synthesize `text` and publish it to the robot; return a result summary.

    `event_id`/`chunk_num` echo the triggering request. `device_id` is the robot
    that made the request; when omitted the most recently seen device is used.

    Raises `UnsupportedLanguageError` for unknown languages, `NoTargetDeviceError`
    when there is nothing to publish to, and propagates OpenAI errors after
    publishing a silent fallback so the robot is never left waiting.
    """
    # Validated before anything is published: a bad language is a caller error,
    # not a robot stall, so it must not emit a silent fallback.
    if language not in settings.SUPPORTED_LANGUAGES:
        supported = ", ".join(settings.SUPPORTED_LANGUAGES)
        msg = f"unsupported language {language!r}; supported: {supported}"
        raise UnsupportedLanguageError(msg)

    target = _resolve_device(device_id)
    if event_id is None:
        event_id = str(uuid.uuid4())
    source = request_source or configured_request_source()
    start = time.monotonic()

    try:
        # The OpenAI client call is blocking; keep the event loop responsive.
        result = await run_in_threadpool(_engine.synthesize, text, language)
    except Exception:
        logger.exception(
            "synthesis failed for event_id=%s; publishing silence so %s "
            "does not stall in MarkingUp",
            event_id,
            target,
        )
        publish_silence(target, event_id, chunk_num, source)
        raise

    total_time_ms = int((time.monotonic() - start) * 1000)
    response = _build_response(
        result,
        event_id=event_id,
        chunk_num=chunk_num,
        request_source=source,
        total_time_ms=total_time_ms,
    )
    mqtt_service.publish_cloud_tts_response(target, response)

    # Report what actually went on the wire, which is the repaired buffer.
    audio_bytes = len(response.audio.buffer)
    logger.info(
        "spoke event_id=%s chunk=%d language=%s audio_bytes=%d marks=%d "
        "raw_pcm=%s total_ms=%d device=%s",
        event_id,
        chunk_num,
        language,
        audio_bytes,
        len(response.marks),
        settings.CLOUD_TTS_AUDIO_RAW_PCM,
        total_time_ms,
        target,
    )
    return {
        "status": "published",
        "event_id": event_id,
        "chunk_num": chunk_num,
        "language": language,
        "audio_bytes": audio_bytes,
        "marks": len(response.marks),
        "sample_rate": result.sample_rate,
        "channels": result.channels,
        "raw_pcm": settings.CLOUD_TTS_AUDIO_RAW_PCM,
        "synthesis_time_ms": result.synthesis_time_ms,
        "total_time_ms": total_time_ms,
        "device_id": target,
    }
