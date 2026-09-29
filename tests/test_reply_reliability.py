"""Latency, history ownership, language repair and child-sized spoken replies."""

import asyncio
import json
import threading
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from src.config import settings
from src.protos.embodied.unity.CloudTTS_pb2 import ROBOT_TTS_REQUEST, REMOTECHAT_TTS_REQUEST
from src.services import speak_service, remote_chat
from src.services.device_state import device_state
from src.services.language import matches_output_language
from src.services.llm import openai_chat
from src.services.llm.reply_text import short_reply
from src.services.provider_work import ProviderBusyError, ProviderPool
from src.services.remote_chat import RemoteChatBrain, generation_error_line
from src.services.stt import transcripts
from tests.conftest import TEST_DEVICE_ID
from tests.test_cloud_tts_request import _decode_response
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


async def test_empty_reprompts_are_bounded_without_invented_input(fake_openai, captured_publish):
    await device_state.set_cloud_tts(True)
    brain = RemoteChatBrain()
    for i in range(4):
        await brain.handle_router_request(TEST_DEVICE_ID, request(f"empty-{i}", command="reprompt"))
    chats, audio = _split(captured_publish)
    assert len(chats) == len(audio) == 4
    assert fake_openai.chat.completions.calls == []
    assert len(fake_openai.audio.speech.calls) == 2
    assert chats[2]["output"]["text"] == chats[3]["output"]["text"] == ""
    assert not any(audio[2].audio.buffer)
    fake_openai.chat.completions._reply = "Алма өте дәмді!"
    await brain.handle_router_request(TEST_DEVICE_ID, request("real", "Apple", "reprompt"))
    assert fake_openai.chat.completions.calls[-1]["messages"][-1]["content"] == "Apple"
    assert brain._sessions[TEST_DEVICE_ID].empty_reprompts == 0


@pytest.mark.parametrize("text,language,valid", [
    ("Алма.", "kk", True), ("Мен роботпын.", "kk", True),
    ("Көліктердің тарихы XIX ғасырда басталған.", "kk", True),
    ("Маған Apple туралы сұрақ қойшы.", "kk", True),
    ("Это машина BMW.", "ru", True),
    ("Hello, Сәлем!", "kk", False), ("Hello!", "ru", False),
    ("Привет! Как тебя зовут?", "kk", False), ("Сәлем!", "ru", False),
])
def test_language_validation_handles_ambiguous_and_mixed_tokens(text, language, valid):
    assert matches_output_language(text, language) is valid


@pytest.mark.parametrize("text,expected", [
    ("Алма. Алмұрт. Өрік. Жүзім.", "Алма. Алмұрт. Өрік."),
    ("Алма!Алмұрт!Өрік!Жүзім!", "Алма! Алмұрт! Өрік!"),
    ("Қалайсың? Не істедің?", "Қалайсың?"),
    ("1. Алма.\n2. Алмұрт.\n3. Өрік.\n4. Жүзім.", "Алма. Алмұрт. Өрік."),
    ("Бұл 3.5 саны. Келесі сан 4.5.", "Бұл 3.5 саны. Келесі сан 4.5."),
    ("Алма. " + "сөз " * 36 + ".", "Алма."),
])
def test_reply_caps_keep_whole_sentences(text, expected):
    assert short_reply(text) == expected
    assert len(short_reply(text).split()) <= 35


def test_one_overlong_sentence_is_not_cut_mid_sentence():
    with pytest.raises(ValueError):
        short_reply("сөз " * 36 + ".")


async def test_markup_special_characters_are_escaped(fake_openai, captured_publish):
    fake_openai.chat.completions._reply = "Алма & алмұрт: 2 < 3, 5 > 4."
    await RemoteChatBrain().handle_router_request(TEST_DEVICE_ID, request("xml", "Count fruit"))
    chat = _split(captured_publish)[0][0]
    assert ElementTree.fromstring(chat["output"]["markup"]).text == chat["output"]["text"]
    assert "&amp;" in chat["output"]["markup"]


@pytest.mark.parametrize("kind", ["no-choices", "empty", "refusal", "incomplete"])
def test_invalid_chat_completion_is_not_treated_as_spoken_answer(kind, fake_openai, monkeypatch):
    completion = SimpleNamespace(choices=[] if kind == "no-choices" else [SimpleNamespace(
        finish_reason="length" if kind == "incomplete" else "stop",
        message=SimpleNamespace(content="" if kind == "empty" else "Сәлем!", refusal="refused" if kind == "refusal" else None),
    )])
    monkeypatch.setattr(fake_openai.chat.completions, "create", lambda **kwargs: completion)
    with pytest.raises(ValueError):
        openai_chat.respond("Сәлем!", "kk")


async def test_wrong_language_gets_one_bounded_rewrite(fake_openai, captured_publish):
    fake_openai.chat.completions._reply = "Hello!"
    fake_openai.chat.completions.translations["Hello!"] = "Сәлем!"
    await RemoteChatBrain().handle_router_request(TEST_DEVICE_ID, request("fix", "hello"))
    assert len(fake_openai.chat.completions.calls) == 2
    assert _split(captured_publish)[0][0]["output"]["text"] == "Сәлем!"


async def test_punctuation_only_output_is_not_rewritten_as_an_answer(fake_openai, captured_publish):
    fake_openai.chat.completions._reply = "..."
    await RemoteChatBrain().handle_router_request(TEST_DEVICE_ID, request("punctuation", "Hello"))
    assert len(fake_openai.chat.completions.calls) == 1
    assert _split(captured_publish)[0][0]["output"]["text"] == generation_error_line("kk")


@pytest.mark.parametrize("language,prefix", [("kk", "Жауабы: "), ("ru", "Ответ: ")])
async def test_numeric_reply_gets_a_spoken_language_cue(language, prefix, fake_openai, captured_publish, monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", language)
    fake_openai.chat.completions._reply = "10."
    await device_state.set_cloud_tts(True)
    await RemoteChatBrain().handle_router_request(TEST_DEVICE_ID, request("number", "What is five plus five?"))
    assert fake_openai.audio.speech.calls[0]["input"] == prefix + "10."


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


@pytest.mark.parametrize("late_failure", [False, True])
async def test_tts_timeout_publishes_once_and_late_worker_cannot_publish(late_failure, fake_openai, captured_publish, blocked_synthesis, monkeypatch):
    entered, release, finished, fail = blocked_synthesis
    fail[0] = late_failure
    monkeypatch.setattr(settings, "OPENAI_TTS_TIMEOUT_S", 0.03)
    with pytest.raises(TimeoutError):
        await speak_service.speak("Сәлем!", "kk", event_id="timeout", device_id=TEST_DEVICE_ID)
    assert entered.is_set() and len(captured_publish) == 1
    audio = _decode_response(captured_publish[0][1])
    assert audio.event_id == "timeout" and not any(audio.audio.buffer)
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    await asyncio.sleep(0)
    assert len(captured_publish) == 1


async def test_early_notify_then_failed_audio_does_not_enter_history(fake_openai, captured_publish, blocked_synthesis, monkeypatch):
    entered, release, finished, fail = blocked_synthesis
    fail[0] = True
    fake_openai.chat.completions._reply = "Менің атым Мокси."
    await device_state.set_cloud_tts(True)
    brain = RemoteChatBrain()
    pending = asyncio.create_task(brain.handle_router_request(TEST_DEVICE_ID, request("answer", "What is your name?")))
    await asyncio.wait_for(entered.wait(), 1)
    await brain.handle_router_request(TEST_DEVICE_ID, request(
        "notify", "Менің атым Мокси.", "notify", source_event_id="answer", response_chunks=[0],
    ))
    assert brain._sessions[TEST_DEVICE_ID].history == [{"role": "user", "content": "What is your name?"}]
    release.set()
    await asyncio.wait_for(pending, 1)
    assert brain._sessions[TEST_DEVICE_ID].history == [{"role": "user", "content": "What is your name?"}]
    assert not any(_split(captured_publish)[1][0].audio.buffer)


async def test_reply_budget_bounds_chat_and_following_audio(fake_openai, captured_publish, monkeypatch):
    async def expire(*args, **kwargs):
        raise TimeoutError
    monkeypatch.setattr(remote_chat.provider_pool, "run", expire)
    await device_state.set_cloud_tts(True)
    await RemoteChatBrain().handle_router_request(TEST_DEVICE_ID, request("budget", "Hello"))
    chats, audio = _split(captured_publish)
    assert chats[0]["output"]["text"] == generation_error_line("kk")
    assert len(audio) == 1 and not any(audio[0].audio.buffer)


async def test_explicit_robot_enum_zero_is_preserved(fake_openai, captured_publish, monkeypatch):
    monkeypatch.setattr(settings, "CLOUD_TTS_REQUEST_SOURCE", "remotechat")
    await speak_service.speak("Сәлем!", "kk", event_id="robot", device_id=TEST_DEVICE_ID, request_source=ROBOT_TTS_REQUEST)
    speak_service.publish_silence(TEST_DEVICE_ID, "silence", request_source=ROBOT_TTS_REQUEST)
    assert all(_decode_response(data).request_source == ROBOT_TTS_REQUEST for _, data in captured_publish)


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


async def test_http_success_headers_do_not_mask_body_failure(fake_openai, captured_publish, monkeypatch):
    class HeaderOnlyResponse:
        status_code = 200
        @property
        def content(self):
            raise TimeoutError("body stalled")
    monkeypatch.setattr(fake_openai.audio.speech, "create", lambda **kwargs: HeaderOnlyResponse())
    with pytest.raises(TimeoutError):
        await speak_service.speak("Сәлем!", "kk", event_id="headers", device_id=TEST_DEVICE_ID)
    assert len(captured_publish) == 1
    assert not any(_decode_response(captured_publish[0][1]).audio.buffer)


def test_late_history_completion_cannot_overwrite_replacement_record():
    session = remote_chat._ChatSession("OPENMOXIE_CHAT/default")
    session.begin("same", "prompt", "", "")
    old = session.records["same"]
    session.begin("same", "continue", "Hello", "Сәлем")
    new = session.records["same"]
    session.finish("same", "Жаңа жауап.", "published", owner=new)
    session.finish("same", "Ескі жауап.", "discarded", owner=old)
    assert session.history[-1]["content"] == "Жаңа жауап."


async def test_status_logs_keep_language_properties_but_omit_identity(caplog):
    import logging
    from src.services.mqtt_service import mqtt_service
    from tests.test_mqtt import _FakeMsg
    payload = {"state": "LISTENING", "child_pii": {"name": "PRIVATE_CHILD"},
               "settings": {"props": {"cloud_translate": "en", "local_stt": "on", "cloud_tts": "1"}},
               "public_key": "PRIVATE_KEY"}
    with caplog.at_level(logging.INFO):
        mqtt_service._on_robot_status("status-test", _FakeMsg("unused", json.dumps(payload).encode()))
    assert "cloud_translate" in caplog.text and "LISTENING" in caplog.text
    assert "PRIVATE_CHILD" not in caplog.text and "PRIVATE_KEY" not in caplog.text
