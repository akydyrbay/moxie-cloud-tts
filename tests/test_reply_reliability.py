"""Latency, history ownership, language repair and child-sized spoken replies."""

import asyncio
import threading

import pytest

pytestmark = pytest.mark.usefixtures("robot_selection")

from app.config import settings
from app.protos.embodied.unity.CloudTTS_pb2 import REMOTECHAT_TTS_REQUEST
from app.services.speech import output as speak_service
from app.services.devices.state import device_state
from app.services.providers.pool import ProviderBusyError, ProviderPool
from app.services.conversation.remote_chat import RemoteChatBrain
from app.services.speech.recognition import transcripts
from tests.conftest import TEST_DEVICE_ID
from tests.test_remote_chat_audio import _split


def request(event_id, speech="", command="continue", **extra):
    return {"backend": "router", "command": command, "module_id": "OPENMOXIE_CHAT",
            "content_id": "default", "event_id": event_id, "speech": speech, **extra}


@pytest.mark.parametrize("language,original,wire,reply", [
    ("kk", "Сенің атың кім?", "What is your name?", "Менің атым Мокси."),
    ("ru", "Как тебя зовут?", "What is your name?", "Меня зовут Мокси."),
])
async def test_original_input_recovery_keeps_protocol_echo(language, original, wire, reply, fake_openai, captured_publish, monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", language)
    await device_state.set_cloud_tts(True)
    transcripts.remember(TEST_DEVICE_ID, "u", original, wire, language)
    fake_openai.chat.completions._reply = reply
    brain = RemoteChatBrain()
    await brain.handle_router_request(TEST_DEVICE_ID, request("u", wire))
    messages = fake_openai.chat.completions.calls[0]["messages"]
    assert messages[-1] == {"role": "user", "content": original}
    chats, audio = _split(captured_publish)
    assert chats[0]["input_speech"] == wire
    assert audio[0].event_id == "u" and audio[0].request_source == REMOTECHAT_TTS_REQUEST
    assert chats[0]["output"]["text"] == reply
    assert brain._sessions[TEST_DEVICE_ID].history == [
        {"role": "user", "content": original}, {"role": "assistant", "content": reply},
    ]


async def test_missing_correlation_never_uses_latest_other_transcript(fake_openai, captured_publish):
    transcripts.remember(TEST_DEVICE_ID, "old", "Ескі сұрақ", "Old question", "kk")
    transcripts.remember("different-robot", "new", "Басқа сұрақ", "Other question", "kk")
    fake_openai.chat.completions._reply = "Менің атым Мокси."
    await RemoteChatBrain().handle_router_request(TEST_DEVICE_ID, request("new", "What is your name?"))
    assert fake_openai.chat.completions.calls[0]["messages"][-1]["content"] == "What is your name?"


async def test_notify_and_duplicate_turn_do_not_duplicate_original_history(fake_openai, captured_publish):
    await device_state.set_cloud_tts(True)
    brain = RemoteChatBrain()
    original = "Сенің атың кім?"
    transcripts.remember(TEST_DEVICE_ID, "u", original, "What is your name?", "kk")
    fake_openai.chat.completions._reply = "Менің атым Мокси."
    await brain.handle_router_request(TEST_DEVICE_ID, request("u", "What is your name?"))
    for _ in range(2):
        await brain.handle_router_request(TEST_DEVICE_ID, request("u", "What is your name?"))
        await brain.handle_router_request(TEST_DEVICE_ID, request(
            "notify-id", "Менің атым Мокси.", "notify", source_event_id="u", response_chunks=[0],
            extra_lines=[{"context_type": "input", "text": "What is your name?"}],
        ))
    assert len(fake_openai.chat.completions.calls) == 1
    assert len(fake_openai.audio.speech.calls) == 1
    history = brain._sessions[TEST_DEVICE_ID].history
    assert history == [{"role": "user", "content": original}, {"role": "assistant", "content": "Менің атым Мокси."}]


async def test_wrong_language_gets_one_bounded_rewrite(fake_openai, captured_publish):
    fake_openai.chat.completions._reply = "Hello!"
    fake_openai.chat.completions.translations["Hello!"] = "Сәлем!"
    await RemoteChatBrain().handle_router_request(TEST_DEVICE_ID, request("fix", "hello"))
    assert len(fake_openai.chat.completions.calls) == 2
    assert _split(captured_publish)[0][0]["output"]["text"] == "Сәлем!"


@pytest.fixture
async def blocked_synthesis(monkeypatch):
    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    original = speak_service._engine.synthesize
    fail = [False]

    def blocked(*args):
        loop.call_soon_threadsafe(entered.set)
        release.wait(1)
        try:
            if fail[0]:
                raise TimeoutError("body read failed after HTTP 200")
            return original(*args)
        finally:
            loop.call_soon_threadsafe(finished.set)

    monkeypatch.setattr(speak_service._engine, "synthesize", blocked)
    yield entered, release, finished, fail
    release.set()


async def test_timed_out_provider_keeps_its_capacity_until_worker_finishes():
    pool = ProviderPool(workers=1)
    release, done = threading.Event(), threading.Event()
    def blocked():
        release.wait(1)
        done.set()
        return 1
    try:
        with pytest.raises(TimeoutError):
            await pool.run(blocked, timeout=0.03)
        with pytest.raises(ProviderBusyError):
            await pool.run(lambda: 2, timeout=1)
        release.set()
        await asyncio.to_thread(done.wait, 1)
        # join proves the first worker actually released its slot.
        await asyncio.to_thread(pool._executor.shutdown, wait=True)
    finally:
        release.set()
        pool._executor.shutdown(wait=True)


async def test_full_reply_deadline_limits_tts_to_time_left_after_chat(fake_openai, captured_publish, blocked_synthesis, monkeypatch):
    import time
    entered, release, finished, fail = blocked_synthesis
    original = fake_openai.chat.completions.create
    def slow_chat(**kwargs):
        time.sleep(0.03)
        return original(**kwargs)
    monkeypatch.setattr(fake_openai.chat.completions, "create", slow_chat)
    monkeypatch.setattr(settings, "REMOTE_REPLY_TIMEOUT_S", 0.08)
    fake_openai.chat.completions._reply = "Менің атым Мокси."
    await device_state.set_cloud_tts(True)
    start = time.monotonic()
    await RemoteChatBrain().handle_router_request(TEST_DEVICE_ID, request("total", "What is your name?"))
    assert time.monotonic() - start < 0.3
    assert entered.is_set()
    assert len(_split(captured_publish)[1]) == 1
    assert not any(_split(captured_publish)[1][0].audio.buffer)
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    await asyncio.sleep(0)
    assert len(_split(captured_publish)[1]) == 1
