"""Stalled OpenAI TTS must not turn native story narration into silence.

October 14 (docs/DEBUG_LOG_ANALYSIS_2026-10-14.md, finding 1): STORY music
played while every narration line received silence. TTS calls either finished
in 1.3–2.7 s or stalled until the 8 s deadline, so a stalled call is hedged
with one parallel attempt inside the same deadline.
"""

import asyncio
import threading
import time

import pytest

from src.config import settings
from src.protos.embodied.unity.CloudTTS_pb2 import ROBOT_TTS_REQUEST
from src.services import cloud_tts_request, speak_service
from src.services.provider_work import ProviderBusyError, ProviderPool
from tests.conftest import FAKE_PCM, TEST_DEVICE_ID
from tests.test_cloud_tts_request import _decode_response, _frame_request

STORY_SOURCE = "Fantastic! This story is called Dr. Wilde Makes a Speech!"
STORY_RU = "Фантастически! Эта история называется «Доктор Уайлд держит речь»!"


@pytest.fixture
def scripted_tts(fake_openai, monkeypatch):
    """Per-call TTS behaviour: "stall" (until teardown), "fail", a delay, or ok."""
    monkeypatch.setattr(settings, "OPENAI_TTS_HEDGE_AFTER_S", 0.05)
    monkeypatch.setattr(settings, "OPENAI_TTS_TIMEOUT_S", 2.0)
    original = speak_service._engine.synthesize
    release = threading.Event()
    lock = threading.Lock()
    behaviours: list = []
    timeouts: list[float | None] = []
    running = [0]

    def synthesize(text, language, timeout=None):
        with lock:
            mode = behaviours[len(timeouts)] if len(timeouts) < len(behaviours) else "ok"
            timeouts.append(timeout)
            running[0] += 1
        try:
            if mode == "stall":
                release.wait(5)
                raise TimeoutError("read timeout")
            if mode == "fail":
                raise ConnectionError("provider reset")
            if isinstance(mode, float):
                time.sleep(mode)
            return original(text, language, timeout)
        finally:
            with lock:
                running[0] -= 1

    monkeypatch.setattr(speak_service._engine, "synthesize", synthesize)
    yield behaviours, timeouts
    # Stalled workers hold shared provider slots; free them before the next test.
    release.set()
    end = time.monotonic() + 5
    while running[0] and time.monotonic() < end:
        time.sleep(0.01)


async def _settle():
    """Let abandoned workers finish so a late result could try to publish."""
    await asyncio.sleep(0.1)


async def test_stalled_story_line_is_hedged_and_spoken(scripted_tts, translated_lines, captured_publish, monkeypatch):
    behaviours, timeouts = scripted_tts
    behaviours.append("stall")
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "ru")
    translated_lines[STORY_SOURCE] = STORY_RU
    markup = f'<mark name="cmd:playback-save"/>{STORY_SOURCE}'

    summary = await cloud_tts_request.handle_payload(
        TEST_DEVICE_ID, _frame_request(markup=markup, event_id="story-1", chunk_num=0),
    )

    assert summary["status"] == "published"
    assert len(timeouts) == 2
    assert all(0 < t <= settings.OPENAI_TTS_TIMEOUT_S for t in timeouts)
    assert len(captured_publish) == 1
    resp = _decode_response(captured_publish[0][1])
    assert (resp.event_id, resp.chunk_num, resp.request_source) == ("story-1", 0, ROBOT_TTS_REQUEST)
    assert resp.audio.buffer == FAKE_PCM
    assert any(mark.value.startswith("cmd:playback-save") for mark in resp.marks)
    await _settle()
    assert len(captured_publish) == 1


async def test_slow_first_attempt_still_wins(scripted_tts, captured_publish):
    behaviours, timeouts = scripted_tts
    behaviours.extend([0.2, "stall"])
    summary = await speak_service.speak("Привет!", "ru", event_id="slow", device_id=TEST_DEVICE_ID)
    assert summary["status"] == "published"
    assert len(timeouts) == 2
    assert _decode_response(captured_publish[0][1]).audio.buffer == FAKE_PCM


async def test_both_attempts_stalled_publish_one_silence_by_deadline(scripted_tts, captured_publish, monkeypatch):
    behaviours, timeouts = scripted_tts
    behaviours.extend(["stall", "stall"])
    monkeypatch.setattr(settings, "OPENAI_TTS_TIMEOUT_S", 0.3)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        await speak_service.speak("Привет!", "ru", event_id="stuck", device_id=TEST_DEVICE_ID)
    assert time.monotonic() - start < 0.6
    assert len(timeouts) == 2
    assert len(captured_publish) == 1
    resp = _decode_response(captured_publish[0][1])
    assert resp.event_id == "stuck" and not any(resp.audio.buffer)


async def test_fast_failure_is_not_retried(scripted_tts, captured_publish):
    behaviours, timeouts = scripted_tts
    behaviours.append("fail")
    with pytest.raises(ConnectionError):
        await speak_service.speak("Привет!", "ru", event_id="fail", device_id=TEST_DEVICE_ID)
    assert len(timeouts) == 1
    assert not any(_decode_response(captured_publish[0][1]).audio.buffer)


async def test_turn_that_lost_is_not_hedged(scripted_tts, captured_publish):
    behaviours, timeouts = scripted_tts
    behaviours.append(0.2)
    current = [True]
    task = asyncio.create_task(speak_service.speak(
        "Привет!", "ru", event_id="lost", device_id=TEST_DEVICE_ID, is_current=lambda: current[0],
    ))
    await asyncio.sleep(0.01)
    current[0] = False
    summary = await task
    assert summary["status"] == "discarded"
    assert len(timeouts) == 1
    assert captured_publish == []


async def test_hedge_never_takes_the_last_provider_slot(scripted_tts, captured_publish, monkeypatch):
    behaviours, timeouts = scripted_tts
    behaviours.append(0.2)
    monkeypatch.setattr(speak_service, "provider_pool", ProviderPool(workers=2))
    summary = await speak_service.speak("Привет!", "ru", event_id="busy", device_id=TEST_DEVICE_ID)
    assert summary["status"] == "published"
    assert len(timeouts) == 1


async def test_short_remaining_budget_is_not_hedged(scripted_tts, captured_publish):
    behaviours, timeouts = scripted_tts
    behaviours.append("stall")
    deadline = time.monotonic() + 0.08  # Less than two hedge delays remain.
    with pytest.raises(TimeoutError):
        await speak_service.speak("Привет!", "ru", event_id="late", device_id=TEST_DEVICE_ID, deadline=deadline)
    assert len(timeouts) == 1
    assert len(captured_publish) == 1


async def test_spare_slots_are_reserved_for_other_work():
    pool = ProviderPool(workers=2)
    release = threading.Event()
    try:
        blocked = asyncio.ensure_future(pool.run(release.wait, 1, timeout=1))
        await asyncio.sleep(0.01)
        with pytest.raises(ProviderBusyError):
            await pool.run(lambda: 1, timeout=1, spare=1)
        assert await pool.run(lambda: 2, timeout=1) == 2
        release.set()
        assert await blocked is True
        assert await pool.run(lambda: 3, timeout=1, spare=1) == 3
    finally:
        release.set()
        pool._executor.shutdown(wait=True)
