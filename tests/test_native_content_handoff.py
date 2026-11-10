"""Replay native selection from October 12 without replacing its speech.

MQTT/providers are fake; arbitration notifications are sent explicitly in the
observed order. These tests do not claim physical playback or translation quality.
"""

import asyncio
import json

import pytest

from app.config import settings
from app.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest
from app.protos.embodied.unity.CloudTTS_pb2 import CloudTTSRequest, ROBOT_TTS_REQUEST, REMOTECHAT_TTS_REQUEST
from app.services.speech.synthesis import handler as cloud_tts_request
from app.services.speech import localization_context
from app.services.conversation import remote_chat
from app.services.speech.recognition import handler as stt_request
from app.services.devices.state import device_state
from app.services.conversation.llm import openai_chat
from app.services.speech.recognition import transcripts
from tests.test_remote_chat_audio import _split

DEVICE = "d_d05e3c66-dd29-4fe2-9758-7223e31511dc"
JOKE_EVENT = "7547550f-2606-4ad6-8f01-d6b3e7b2f24a"
STORY_EVENT = "ce704e3d-2b08-47be-a572-6892f2e8e342"
CONFIRM = "Oh! Do you want me to tell you a story?"
CHOICES = "Would you like to hear the first story I mentioned, or the second one?"


def router(event, module="OPENMOXIE_CHAT", command="continue", speech="Moxie, tell a story"):
    return {"backend": "router", "command": command, "event_id": event,
            "module_id": module, "content_id": "default" if module == "OPENMOXIE_CHAT" else "",
            "speech": speech}


def notification(event, module="EXIT", speech=CONFIRM, *, remote=False):
    return {**router("", module, "notify", speech), "source_event_id": event,
            **({"notify_source": "REMOTE_RESPONSE", "response_chunks": [0]} if remote else {})}


async def until(predicate):
    async with asyncio.timeout(1):
        while not predicate():
            await asyncio.sleep(0)


@pytest.fixture
async def brain(monkeypatch):
    brain = remote_chat.RemoteChatBrain()
    monkeypatch.setattr(remote_chat, "remote_chat_brain", brain)
    monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", True)
    await device_state.set_cloud_tts(True)
    return brain


async def native_audio(event, text, chunk=0):
    return await cloud_tts_request.handle_cloud_tts_request(DEVICE, CloudTTSRequest(
        event_id=event, chunk_num=chunk,
        markup=f'<mark name="cmd:playback-save"/><usel>{text}</usel><mark name="cmd:done"/>',
    ))


@pytest.mark.parametrize("language,reply,localized", [
    ("kk", "Саған бір ертегі айтып берейін.", "Саған ертегі айтып берейін бе?"),
    ("ru", "Я расскажу тебе историю.", "Хочешь, я расскажу тебе историю?"),
])
@pytest.mark.parametrize("first", ["notify", "tts", "during_generation"])
async def test_native_winner_gets_only_localized_native_audio(
    brain, fake_openai, captured_publish, monkeypatch, language, reply, localized, first,
):
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", language)
    fake_openai.chat.completions._reply = reply
    fake_openai.chat.completions.translations[CONFIRM] = localized
    entered, release = asyncio.Event(), asyncio.Event()
    run = remote_chat.provider_pool.run

    async def provider(function, *args, **kwargs):
        if function is openai_chat.respond and first == "during_generation":
            entered.set()
            await release.wait()
        return await run(function, *args, **kwargs)

    monkeypatch.setattr(remote_chat.provider_pool, "run", provider)
    task = asyncio.create_task(brain.handle_router_request(DEVICE, router(JOKE_EVENT)))
    if first == "during_generation":
        await asyncio.wait_for(entered.wait(), 1)
    else:
        await until(lambda: bool(_split(captured_publish)[0]))
        assert _split(captured_publish)[0][0]["response_action"]["output_type"] == "GLOBAL_RESPONSE"
        assert not _split(captured_publish)[1]  # JSON alone never proves selection.
    if first != "tts":
        await brain.handle_router_request(DEVICE, notification(JOKE_EVENT))
    await native_audio(JOKE_EVENT, CONFIRM)
    if first == "tts":
        await brain.handle_router_request(DEVICE, notification(JOKE_EVENT))
    release.set()
    await asyncio.wait_for(task, 1)
    chats, audio = _split(captured_publish)
    assert len(audio) == 1
    assert (audio[0].event_id, audio[0].chunk_num, audio[0].request_source) == (JOKE_EVENT, 0, ROBOT_TTS_REQUEST)
    assert any(audio[0].audio.buffer)
    assert [m.value for m in audio[0].marks if m.type == "ssml"] == ["cmd:playback-save", "cmd:done"]
    assert [call["input"] for call in fake_openai.audio.speech.calls] == [localized]
    assert reply not in [m["content"] for m in brain._sessions[DEVICE].history if m["role"] == "assistant"]
    assert stt_request._conversation_phase[DEVICE] == "native"
    if first == "during_generation":
        assert chats[-1]["response_action"]["output_type"] == "FALLBACK"


async def test_chat_native_choices_and_return_to_chat(
    brain, fake_openai, captured_publish, monkeypatch,
):
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "ru")
    monkeypatch.setattr(settings, "STT_WAKE_WORD_REQUIRED", True)
    fake_openai.chat.completions._reply = "О чём поговорим?"
    free = asyncio.create_task(brain.handle_router_request(DEVICE, router("free", speech="Moxie, hello")))
    await until(lambda: bool(_split(captured_publish)[0]))
    await brain.handle_router_request(DEVICE, notification("free", "OPENMOXIE_CHAT", remote=True))
    await free
    assert _split(captured_publish)[1][-1].request_source == REMOTECHAT_TTS_REQUEST

    # Actual native EXIT -> STORY progression, no independent GPT answer.
    for event, module, speech in [("confirm", "EXIT", "Moxie, tell a story"),
                                  (STORY_EVENT, "STORY", "Yes"),
                                  ("choice", "STORY", "The second one")]:
        await brain.handle_router_request(DEVICE, notification(event, module, CHOICES))
        before = len(fake_openai.chat.completions.calls)
        await brain.handle_router_request(DEVICE, router(event, module, speech=speech))
        assert len(fake_openai.chat.completions.calls) == before
        assert _split(captured_publish)[0][-1]["response_action"]["output_type"] == "FALLBACK"
        assert stt_request._conversation_phase[DEVICE] == "native"

    fake_openai.chat.completions.translations[CHOICES] = "Хочешь первую историю или вторую?"
    await native_audio("choice-prompt", CHOICES)
    fake_openai.audio.transcriptions._text = "Екінші ертегі."
    fake_openai.chat.completions.translations["Екінші ертегі."] = "The second story."
    outcome = await stt_request.handle_stt_request(DEVICE, zmqSTTRequest(
        uuid="child-choice", vad=zmqSTTRequest.END_OF_SPEECH, audio_content=b"\x10\x02" * 16000,
    ))
    assert outcome["error_code"] == 0 and outcome["speech"] == "The second story."
    assert transcripts.find(DEVICE, "child-choice").original == "Екінші ертегі."
    bridge_messages = fake_openai.chat.completions.calls[-1]["messages"]
    assert "STORY" in bridge_messages[1]["content"]
    assert "Хочешь первую историю" in bridge_messages[1]["content"]
    assert bridge_messages[-1]["content"] == "Екінші ертегі."

    fake_openai.chat.completions.translations.update({
        "Good morning! How are you feeling?": "Привет! Как дела?",
    })
    monkeypatch.setattr(brain, "_opener_for", lambda content: "Good morning! How are you feeling?")
    returning = asyncio.create_task(brain.handle_router_request(DEVICE, router("return", command="prompt", speech="")))
    await until(lambda: any(c["event_id"] == "return" for c in _split(captured_publish)[0]))
    await brain.handle_router_request(DEVICE, notification("return", "OPENMOXIE_CHAT", remote=True))
    await returning
    assert stt_request._conversation_phase[DEVICE] == "chat"
    assert not localization_context.snapshot(DEVICE)
    assert _split(captured_publish)[1][-1].event_id == "return"


async def test_oct12_remote_notify_after_audio_does_not_deadlock_or_replay(
    brain, fake_openai, captured_publish, monkeypatch,
):
    monkeypatch.setattr(settings, "REMOTE_SELECTION_GRACE_S", 0.01)
    fake_openai.chat.completions._reply = "Мен роботпын."
    # 18:37:44 audio preceded 18:37:45 remote notification for this real UUID.
    event = "f439b0c9-2b07-4f9b-a470-cf3e9daf5a95"
    await brain.handle_router_request(DEVICE, router(event))
    assert len(_split(captured_publish)[1]) == 1
    assert _split(captured_publish)[1][0].request_source == REMOTECHAT_TTS_REQUEST
    await brain.handle_router_request(DEVICE, notification(event, "OPENMOXIE_CHAT", remote=True))
    assert len(_split(captured_publish)[1]) == 1


async def test_three_listens_overlap_without_capacity_error_66(brain, fake_openai, captured_publish, monkeypatch):
    # Oct 12 18:38:10/11/12: two completed recordings were still processing
    # when a third UUID started. Recognition is serialized, recording is not.
    stt_request.note_native_activity(DEVICE)
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def recognize(*args, **kwargs):
        calls.append(len(calls))
        entered.set()
        await release.wait()
        return "Иә", "Yes", "kk"

    monkeypatch.setattr(stt_request, "_recognize_and_bridge", recognize)
    tasks = []
    try:
        for event in ["636b835f-3dbf-4e2e-af5d-51944397e574",
                      "70f59ac9-4197-4f19-a8da-301139ca8819",
                      "1c51569f-9b0b-4881-bdb0-d04c519fcddc"]:
            tasks.append(asyncio.create_task(stt_request.handle_stt_request(DEVICE, zmqSTTRequest(
                uuid=event, vad=zmqSTTRequest.END_OF_SPEECH, audio_content=b"\x10\x02" * 1600,
            ))))
            await asyncio.sleep(0)
        await asyncio.wait_for(entered.wait(), 1)
        assert len(stt_request._sessions) == 3 and len(calls) == 1
    finally:
        release.set()
        outcomes = await asyncio.gather(*tasks)
    assert all(o["error_code"] == 0 and o["speech"] == "Yes" for o in outcomes)
    assert len(calls) == 3 and not stt_request._sessions


@pytest.mark.parametrize("fails", [False, True])
async def test_native_takeover_during_remote_synthesis_discards_audio_and_failure_silence(
    brain, fake_openai, captured_publish, monkeypatch, fails,
):
    fake_openai.chat.completions._reply = "Мен роботпын."
    fake_openai.chat.completions.translations[CONFIRM] = "Саған ертегі айтып берейін бе?"
    entered, release = asyncio.Event(), asyncio.Event()
    run = remote_chat.provider_pool.run

    async def provider(function, *args, **kwargs):
        if function == remote_chat.speak_service._engine.synthesize and args[0] == "Мен роботпын.":
            entered.set()
            await release.wait()
            if fails:
                raise RuntimeError("late TTS failure")
        return await run(function, *args, **kwargs)

    monkeypatch.setattr(remote_chat.provider_pool, "run", provider)
    task = asyncio.create_task(brain.handle_router_request(DEVICE, router(JOKE_EVENT)))
    await until(lambda: bool(_split(captured_publish)[0]))
    await brain.handle_router_request(DEVICE, notification(JOKE_EVENT, "OPENMOXIE_CHAT", remote=True))
    await asyncio.wait_for(entered.wait(), 1)
    await brain.handle_router_request(DEVICE, notification(JOKE_EVENT))
    await native_audio(JOKE_EVENT, CONFIRM)
    release.set()
    await task
    audio = _split(captured_publish)[1]
    assert len(audio) == 1 and audio[0].request_source == ROBOT_TTS_REQUEST
    assert any(audio[0].audio.buffer)


@pytest.mark.parametrize("signal", ["[animation:curious]", "[animation:thinking]", "[silent:reprompt]"])
async def test_oct12_interim_notifications_do_not_cancel_gpt(
    brain, fake_openai, captured_publish, monkeypatch, signal,
):
    event = "e0c05b0d-9c6f-4a82-a62b-0cc94813ee96"
    fake_openai.chat.completions._reply = "Мен роботпын."
    entered, release = asyncio.Event(), asyncio.Event()
    run = remote_chat.provider_pool.run

    async def provider(function, *args, **kwargs):
        if function is openai_chat.respond:
            entered.set()
            await release.wait()
        return await run(function, *args, **kwargs)

    monkeypatch.setattr(remote_chat.provider_pool, "run", provider)
    task = asyncio.create_task(brain.handle_router_request(DEVICE, router(event)))
    await asyncio.wait_for(entered.wait(), 1)
    await brain.handle_router_request(DEVICE, notification(event, "OPENMOXIE_CHAT", signal))
    assert stt_request._conversation_phase[DEVICE] == "chat"
    release.set()
    await task  # Also works with no remote selection notification yet.
    assert len(_split(captured_publish)[1]) == 1
    assert _split(captured_publish)[1][0].request_source == REMOTECHAT_TTS_REQUEST


async def test_queued_stt_keeps_context_from_utterance_end(brain, captured_publish, monkeypatch):
    stt_request.note_native_activity(DEVICE)
    localization_context.select(DEVICE, "STORY", "the_robot_olympics")
    localization_context.observe_prompt(DEVICE, CHOICES)
    entered, release = asyncio.Event(), asyncio.Event()
    contexts = []

    async def recognize(*args, context, **kwargs):
        contexts.append(json.loads(context))
        entered.set()
        await release.wait()
        return "Екінші", "Second", "kk"

    monkeypatch.setattr(stt_request, "_recognize_and_bridge", recognize)
    tasks = []
    try:
        for event in ["context-first", "context-queued"]:
            tasks.append(asyncio.create_task(stt_request.handle_stt_request(DEVICE, zmqSTTRequest(
                uuid=event, vad=zmqSTTRequest.END_OF_SPEECH, audio_content=b"\x10\x02" * 1600,
            ))))
            await asyncio.sleep(0)
        await asyncio.wait_for(entered.wait(), 1)
        assert len(contexts) == 1
        localization_context.select(DEVICE, "JOKES")
        localization_context.observe_prompt(DEVICE, "Would you like another joke?")
    finally:
        release.set()
        outcomes = await asyncio.gather(*tasks)
    assert all(o["error_code"] == 0 for o in outcomes)
    assert [context["module"] for context in contexts] == ["STORY", "STORY"]
    assert all(context["robot_prompt"] == CHOICES for context in contexts)
