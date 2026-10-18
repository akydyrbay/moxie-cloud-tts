"""Cloud-TTS synthesis: text -> OpenAI TTS -> CloudTTSResponse -> robot.

    text + language
      -> OpenAITTSEngine.synthesize()              (audio + synthesis time)
      -> build a valid CloudTTSResponse            (audio + ids + timings)
      -> mqtt_service.publish_cloud_tts_response() (framed, to the requesting device)

The robot only plays a response whose `event_id`/`chunk_num` match a request it
is currently waiting on, so both are echoed verbatim.

For a current request, a response is published *even when synthesis fails*. With cloud_tts
enabled the robot suppresses its onboard TTS and waits in "MarkingUp"; if
nothing ever arrives it force-idles and goes to sleep. Publishing a short silent
buffer releases that wait instead of hanging the robot.

Every buffer goes through `audio.repair_wav()` first, so a container can never
declare more audio than it carries (see `src/services/audio.py`). `marks`
drive Moxie's mouth (see `src/services/marks.py`).
"""

import asyncio
from collections.abc import Callable
import logging
import time
import uuid

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
from src.services.marks import build_marks, command_marks
from src.services.mqtt_service import mqtt_service
from src.services.provider_work import provider_pool
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
    markup: str = "",
) -> CloudTTSResponse:
    """Assemble a CloudTTSResponse for playback.

    The buffer is passed through `repair_wav()` so the container can never claim
    to be longer than the samples it carries.

    `marks` carries Polly-style viseme/word timings derived from `result.text`
    plus any `ssml` command marks echoed from `markup` — the robot needs those
    `cmd:` marks back to fire its behaviour/animation and leave MarkingUp (see
    `src/services/marks.py`).
    """
    audio = repair_wav(result.audio)
    if settings.CLOUD_TTS_EMIT_MARKS:
        dur = duration_ms(audio, result.sample_rate, result.channels)
        marks = [*command_marks(markup, dur), *build_marks(result.text, dur)]
        # Time order; stable so a command mark keeps its place ahead of a viseme
        # emitted at the same instant.
        marks.sort(key=lambda mark: mark.time)
    else:
        marks = []
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


def _discard_stale_output(device_id: str, event_id: str, chunk_num: int) -> dict:
    logger.info(
        "discarded stale speech device=%s event_id=%s chunk=%d",
        device_id, event_id, chunk_num,
    )
    return {
        "status": "discarded", "device_id": device_id,
        "event_id": event_id, "chunk_num": chunk_num,
    }


def _consume_attempt(task: asyncio.Task) -> None:
    """Retrieve a finished attempt's exception so a losing attempt never warns."""
    if not task.cancelled():
        task.exception()


def _start_attempt(text: str, language: str, deadline: float, *, spare: int = 0) -> asyncio.Task:
    timeout = deadline - time.monotonic()
    task = asyncio.ensure_future(
        provider_pool.run(_engine.synthesize, text, language, timeout, timeout=timeout, spare=spare),
    )
    task.add_done_callback(_consume_attempt)
    return task


async def _synthesize(
    text: str,
    language: str,
    deadline: float,
    *,
    event_id: str,
    may_hedge: Callable[[], bool],
) -> SynthesisResult:
    """Synthesize within `deadline`, hedging a stalled first attempt once.

    OpenAI TTS either answers within a few seconds or stalls until the deadline.
    If the first attempt is still running after OPENAI_TTS_HEDGE_AFTER_S, one
    more attempt starts beside it and the first successful audio wins; the
    first attempt is never abandoned, because a slow call can still succeed.
    Both attempts share the original deadline, so the robot waits no longer.

    The hedge needs at least OPENAI_TTS_HEDGE_AFTER_S of remaining budget and
    keeps one provider slot free for recognition and translation; otherwise it
    is skipped. Fast failures are raised unchanged: only a stall is hedged. If
    both attempts fail, the first attempt's error is raised.
    """
    start = time.monotonic()
    first = _start_attempt(text, language, deadline)
    attempts = [first]
    try:
        hedge_at = start + settings.OPENAI_TTS_HEDGE_AFTER_S
        # A hedge gets at least as long as the first attempt had before it was
        # judged stalled; a shorter remaining budget cannot usefully finish.
        if deadline - hedge_at < settings.OPENAI_TTS_HEDGE_AFTER_S:
            return await first
        await asyncio.wait([first], timeout=hedge_at - time.monotonic())
        if first.done() or not may_hedge():
            return await first

        logger.warning(
            "tts attempt still running event_id=%s after %d ms; starting hedged attempt",
            event_id, int((time.monotonic() - start) * 1000),
        )
        attempts.append(_start_attempt(text, language, deadline, spare=1))
        pending = set(attempts)
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                if task.exception() is None:
                    logger.info(
                        "tts hedge result event_id=%s winner=attempt%d elapsed_ms=%d",
                        event_id, attempts.index(task) + 1,
                        int((time.monotonic() - start) * 1000),
                    )
                    return task.result()
        logger.warning(
            "tts hedge result event_id=%s winner=none errors=%s",
            event_id, ",".join(type(task.exception()).__name__ for task in attempts),
        )
        return first.result()
    finally:
        # A losing worker keeps its provider slot until its HTTP call ends;
        # cancelling only stops awaiting it, and it can never publish.
        for task in attempts:
            if not task.done():
                task.cancel()


def publish_silence(
    device_id: str,
    event_id: str,
    chunk_num: int = 0,
    request_source: RequestSourceType | None = None,
    markup: str = "",
) -> dict:
    """Publish a correlated silent response so the robot stops waiting.

    `markup` is the original request markup: command-only lines (the robot's
    entry animation) carry no speakable text but DO carry `cmd:` marks that must
    be echoed, or the robot stalls in MarkingUp. Passing it here lets the silent
    response still carry those commands.
    """
    audio = _silence()
    result = SynthesisResult(
        audio=audio,
        sample_rate=settings.AUDIO_SAMPLE_RATE_HZ,
        channels=settings.AUDIO_CHANNELS,
        synthesis_time_ms=0,
    )
    # `result.text` is empty: no speech, so no visemes — `build_marks` still
    # closes the utterance with a trailing `sil`, and any `cmd:` marks in
    # `markup` are echoed as `ssml` marks.
    response = _build_response(
        result,
        event_id=event_id,
        chunk_num=chunk_num,
        request_source=configured_request_source() if request_source is None else request_source,
        total_time_ms=0,
        markup=markup,
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
    markup: str | None = None,
    *,
    is_current: Callable[[], bool] | None = None,
    deadline: float | None = None,
) -> dict:
    """Synthesize `text` and publish it to the robot; return a result summary.

    `event_id`/`chunk_num` echo the triggering request. `device_id` is the robot
    that made the request; when omitted the most recently seen device is used.

    Raises `UnsupportedLanguageError` for unknown languages, `NoTargetDeviceError`
    when there is nothing to publish to, and propagates OpenAI errors after
    publishing a silent fallback so the robot is never left waiting.

    Remote chat supplies `is_current` to discard expired turns, including
    failures: neither audio nor silence may complete an invalidated request.
    The guard runs on the asyncio loop, immediately before a publish, with no
    await between the check and the publish. Other callers retain their usual
    one-response behavior.
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
    if is_current is not None and not is_current():
        return _discard_stale_output(target, event_id, chunk_num)
    source = configured_request_source() if request_source is None else request_source
    start = time.monotonic()

    try:
        # The OpenAI client call is blocking; keep the event loop responsive.
        tts_deadline = start + settings.OPENAI_TTS_TIMEOUT_S
        if deadline is not None:
            tts_deadline = min(tts_deadline, deadline)
        result = await _synthesize(
            text, language, tts_deadline, event_id=event_id,
            # Never hedge a turn that has already lost, e.g. a remote
            # fallback after native content took over.
            may_hedge=lambda: is_current is None or is_current(),
        )
    except Exception as exc:
        if is_current is not None and not is_current():
            return _discard_stale_output(target, event_id, chunk_num)
        logger.warning(
            "synthesis failed for event_id=%s device=%s error=%s elapsed_ms=%d; publishing silence",
            event_id,
            target,
            type(exc).__name__, int((time.monotonic() - start) * 1000),
        )
        publish_silence(target, event_id, chunk_num, source, markup=markup or "")
        raise

    if is_current is not None and not is_current():
        return _discard_stale_output(target, event_id, chunk_num)
    total_time_ms = int((time.monotonic() - start) * 1000)
    response = _build_response(
        result,
        event_id=event_id,
        chunk_num=chunk_num,
        request_source=source,
        total_time_ms=total_time_ms,
        markup=markup or "",
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
