"""Remote-module speech is pushed as REMOTECHAT audio for the live router turn.

Sep 28 17:18 (`log/debug.log`): the prompt `85834651-…` was answered and later
notified, but the robot raised no CloudTTSRequest for it. The only request was
the native WAKEUP line on the wake ID `e59a1ebd-…`. Every chat FINAL was then
ignored. IDs, text and transcripts below come from that capture; the TTS markup
is reconstructed and the `continue` turn is synthetic (none was logged).
Mocked providers and MQTT verify the server envelopes, not robot playback.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.usefixtures("robot_selection")

from src.config import settings
from src.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest, zmqSTTResponse
from src.protos.embodied.unity.CloudTTS_pb2 import (
    REMOTECHAT_TTS_REQUEST,
    ROBOT_TTS_REQUEST,
)
from src.services import cloud_tts_request, remote_chat, stt_request
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.mqtt_service import mqtt_service
from src.services.remote_chat import RemoteChatBrain
from tests.test_cloud_tts_request import _decode_response, _frame_request

DEVICE = "d_d05e3c66-dd29-4fe2-9758-7223e31511dc"
WAKE_ID = "e59a1ebd-97bd-4c49-a964-34a9868d630a"
PROMPT_ID = "85834651-30d4-43c6-b2b1-e77263037d0e"
LISTEN_ID = "bfad2a9d-0e88-486d-aba6-45badb27bb3e"
TRANSCRIPT = "Мокси маған жемістер туралы сұрақ қойшы."
OPENER_KK = "Сәлем! Не туралы сөйлескің келеді?"
REPLY_KK = "Жарайды! Саған қандай жеміс ұнайды?"
STT_PREFIX = zmqSTTResponse.DESCRIPTOR.full_name.encode() + b":"


@pytest.fixture
def brain(monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", True)
    return RemoteChatBrain()


def _router(command, module, event_id, speech=None):
    request = {
        "backend": "router", "command": command, "module_id": module,
        "content_id": "default" if module == "OPENMOXIE_CHAT" else "",
        "event_id": event_id,
    }
    if speech is not None:
        request["speech"] = speech
    return json.dumps(request)


def _split(published):
    """Return (remote_chat JSON payloads, CloudTTSResponses) in publish order."""
    chats, audio = [], []
    for topic, data in published:
        if topic.endswith("/commands/remote_chat"):
            chats.append(json.loads(data))
        elif isinstance(data, bytes) and data.startswith(b"embodied.unity.CloudTTSResponse:"):
            audio.append(_decode_response(data))
    return chats, audio


async def test_sep28_prompt_audio_and_transcript_turn(
    brain, fake_openai, translated_lines, captured_publish,
):
    await device_state.set_cloud_tts(True)
    await brain.ensure_device_bootstrapped(DEVICE)

    # Entrance: native response wins; no pushed audio competes with it.
    captured_publish.clear()
    await brain.handle_remote_chat_event(
        DEVICE, _router("start", "WAKEUP", WAKE_ID, "Moxie, let's go"),
    )
    chats, audio = _split(captured_publish)
    assert [c["response_action"]["output_type"] for c in chats] == ["FALLBACK"]
    assert audio == []

    captured_publish.clear()
    await brain.handle_remote_chat_event(
        DEVICE, _router("notify", "OPENMOXIE_CHAT", "", "Awesome!"),
    )
    assert captured_publish == []

    # Prompt: localized opener, then REMOTECHAT audio keyed by the prompt ID.
    captured_publish.clear()
    await brain.handle_remote_chat_event(
        DEVICE, _router("prompt", "OPENMOXIE_CHAT", PROMPT_ID),
    )
    assert captured_publish[0][0].endswith("/commands/remote_chat")
    chats, audio = _split(captured_publish)
    assert len(chats) == 1 and len(audio) == 1
    assert chats[0]["event_id"] == PROMPT_ID
    assert chats[0]["output"]["text"] == OPENER_KK
    assert chats[0]["output"]["markup"] == f'<usel variant="2" genre="none">{OPENER_KK}</usel>'
    assert chats[0]["response_action"] == {"output_type": "GLOBAL_RESPONSE"}
    assert audio[0].request_source == REMOTECHAT_TTS_REQUEST
    assert (audio[0].event_id, audio[0].chunk_num) == (PROMPT_ID, 0)
    assert audio[0].audio.buffer
    assert fake_openai.audio.speech.calls[-1]["input"] == OPENER_KK

    # The native wake line still gets its ordinary robot-sourced answer.
    captured_publish.clear()
    await cloud_tts_request.handle_payload(DEVICE, _frame_request(
        markup='<usel variant="1" genre="excited"> Awesome!</usel> " "',
        event_id=WAKE_ID, chunk_num=0,
    ))
    _chats, audio = _split(captured_publish)
    assert len(audio) == 1
    assert audio[0].request_source == ROBOT_TTS_REQUEST
    assert (audio[0].event_id, audio[0].chunk_num) == (WAKE_ID, 0)
    assert fake_openai.audio.speech.calls[-1]["input"] == "Тамаша!"

    # Chat listen: the faithful English bridge retains the original UUID.
    captured_publish.clear()
    fake_openai.audio.transcriptions._text = TRANSCRIPT
    bridged = "Moxie, ask me a question about fruit."
    fake_openai.chat.completions.translations[TRANSCRIPT] = bridged
    for vad, audio_bytes in (
        (zmqSTTRequest.VADState.START_OF_SPEECH, b""),
        (zmqSTTRequest.VADState.SPEECH, b"\x00\x01" * 1920),
        (zmqSTTRequest.VADState.END_OF_SPEECH, b""),
    ):
        await stt_request.handle_stt_request(DEVICE, zmqSTTRequest(
            uuid=LISTEN_ID, vad=vad, audio_content=audio_bytes, timestamp=1790597939000,
        ))
    assert len(captured_publish) == 1
    final = zmqSTTResponse.FromString(captured_publish[0][1][len(STT_PREFIX):])
    assert (final.uuid, final.speech, final.error_code) == (LISTEN_ID, bridged, 0)

    # Synthetic router turn carrying that transcript reaches the LLM intact.
    captured_publish.clear()
    fake_openai.chat.completions._reply = REPLY_KK
    turn_id = LISTEN_ID
    await brain.handle_remote_chat_event(
        DEVICE, _router("continue", "OPENMOXIE_CHAT", turn_id, bridged),
    )
    assert fake_openai.chat.completions.calls[-1]["messages"][-1] == {
        "role": "user", "content": TRANSCRIPT,
    }
    chats, audio = _split(captured_publish)
    assert chats[0]["event_id"] == turn_id
    assert chats[0]["output"]["text"] == REPLY_KK
    assert len(audio) == 1
    assert audio[0].request_source == REMOTECHAT_TTS_REQUEST
    assert (audio[0].event_id, audio[0].chunk_num) == (turn_id, 0)
    assert fake_openai.audio.speech.calls[-1]["input"] == REPLY_KK


async def test_no_pushed_audio_without_cloud_tts(brain, fake_openai, captured_publish):
    """Onboard TTS speaks the markup itself; English source stays untranslated."""
    await device_state.set_cloud_tts(False)
    await brain.handle_remote_chat_event(
        DEVICE, _router("prompt", "OPENMOXIE_CHAT", PROMPT_ID),
    )
    chats, audio = _split(captured_publish)
    assert chats[0]["output"]["text"] == "Hi! What would you like to talk about?"
    assert audio == []
    assert fake_openai.audio.speech.calls == []


async def test_no_pushed_audio_in_puppet_mode(brain, fake_openai, captured_publish):
    await device_state.set_cloud_tts(True)
    await device_state.set_puppet_mode(True)
    await brain.handle_remote_chat_event(
        DEVICE, _router("prompt", "OPENMOXIE_CHAT", PROMPT_ID),
    )
    _chats, audio = _split(captured_publish)
    assert audio == []


async def test_onboard_fallback_is_not_voiced(brain, fake_openai, captured_publish):
    """A FALLBACK loses arbitration; the local line raises its own request."""
    await device_state.set_cloud_tts(True)
    await brain.handle_remote_chat_event(
        DEVICE, _router("continue", "WELCOME", "onboard-1", "Сәлем"),
    )
    chats, audio = _split(captured_publish)
    assert chats[0]["response_action"]["output_type"] == "FALLBACK"
    assert audio == []


async def test_opener_translation_failure_publishes_correlated_silence(
    brain, fake_openai, captured_publish, monkeypatch,
):
    async def _fail(*_args, **_kwargs):
        raise TimeoutError

    monkeypatch.setattr("src.services.translation.translate", _fail)
    await device_state.set_cloud_tts(True)
    await brain.handle_remote_chat_event(
        DEVICE, _router("prompt", "OPENMOXIE_CHAT", PROMPT_ID),
    )
    chats, audio = _split(captured_publish)
    assert chats[0]["output"]["text"] == remote_chat.generation_error_line("kk")
    assert len(audio) == 1
    assert audio[0].request_source == REMOTECHAT_TTS_REQUEST
    assert (audio[0].event_id, audio[0].chunk_num) == (PROMPT_ID, 0)
    assert not any(audio[0].audio.buffer)
    assert fake_openai.audio.speech.calls == []


async def test_synthesis_failure_publishes_correlated_silence(
    brain, fake_openai, captured_publish, monkeypatch,
):
    def _boom(*_args, **_kwargs):
        raise RuntimeError("tts down")

    monkeypatch.setattr(fake_openai.audio.speech, "create", _boom)
    fake_openai.chat.completions._reply = REPLY_KK
    await device_state.set_cloud_tts(True)
    await brain.handle_remote_chat_event(
        DEVICE, _router("continue", "OPENMOXIE_CHAT", "turn-1", TRANSCRIPT),
    )
    chats, audio = _split(captured_publish)
    assert chats[0]["output"]["text"] == REPLY_KK
    assert len(audio) == 1
    assert (audio[0].request_source, audio[0].event_id) == (REMOTECHAT_TTS_REQUEST, "turn-1")
    assert not any(audio[0].audio.buffer)


@pytest.fixture
async def blocked_stage(request, fake_openai, translated_lines, monkeypatch):
    """Hold one real provider boundary while the robot changes state."""
    stage = request.param
    loop = asyncio.get_running_loop()
    gate = SimpleNamespace(
        stage=stage, entered=asyncio.Event(), release=threading.Event(), fail=False,
    )
    fake_openai.chat.completions._reply = REPLY_KK
    provider = (
        fake_openai.audio.speech if stage == "tts" else fake_openai.chat.completions
    )
    create = provider.create
    first = True

    def blocked_create(**kwargs):
        nonlocal first
        if first:
            first = False
            loop.call_soon_threadsafe(gate.entered.set)
            assert gate.release.wait(3), "test did not release the provider"
            if gate.fail:
                raise RuntimeError("late provider failure")
        return create(**kwargs)

    monkeypatch.setattr(provider, "create", blocked_create)
    yield gate
    gate.release.set()


async def _start_pending(brain, gate, published):
    await device_state.set_cloud_tts(True)
    await brain.ensure_device_bootstrapped(DEVICE)
    published.clear()
    command = "prompt" if gate.stage == "translation" else "continue"
    pending = asyncio.create_task(brain.handle_remote_chat_event(
        DEVICE, _router(command, "OPENMOXIE_CHAT", PROMPT_ID, TRANSCRIPT),
    ))
    await asyncio.wait_for(gate.entered.wait(), 1)
    # At the TTS boundary the JSON response has already been sent. The test
    # checks that nothing else is sent for this turn after it is invalidated.
    published.clear()
    return pending


async def _change_state(brain, transition, monkeypatch):
    if transition == "module_exit":
        await brain.handle_remote_chat_event(
            DEVICE, _router("start", "WELCOME", "new-module"),
        )
    elif transition == "new_prompt":
        await brain.handle_remote_chat_event(
            DEVICE, _router("prompt", "OPENMOXIE_CHAT", "new-prompt"),
        )
    elif transition.startswith("puppet"):
        await device_state.set_puppet_mode(True)
        if transition.endswith("round_trip"):
            await device_state.set_puppet_mode(False)
    elif transition.startswith("cloud_tts"):
        await device_state.set_cloud_tts(False)
        if transition.endswith("round_trip"):
            await device_state.set_cloud_tts(True)
    elif transition == "bootstrap":
        device_registry.forget_bootstrap(DEVICE)
        await brain.ensure_device_bootstrapped(DEVICE)
    elif transition == "broker_disconnect":
        monkeypatch.setattr(remote_chat, "remote_chat_brain", brain)
        monkeypatch.setattr(mqtt_service, "_loop", asyncio.get_running_loop())
        mqtt_service._on_broker_log(SimpleNamespace(
            payload=f"Client {DEVICE} disconnected.".encode(),
        ))
        await asyncio.sleep(0)  # Run the reset scheduled onto the asyncio loop.
    else:
        raise AssertionError(transition)


@pytest.mark.parametrize("blocked_stage", ["translation", "chat", "tts"], indirect=True)
@pytest.mark.parametrize("transition", [
    "module_exit", "new_prompt", "puppet", "cloud_tts", "bootstrap",
    "broker_disconnect", "puppet_round_trip", "cloud_tts_round_trip",
])
async def test_state_change_discards_pending_remote_output(
    brain, blocked_stage, captured_publish, monkeypatch, transition,
):
    pending = await _start_pending(brain, blocked_stage, captured_publish)
    try:
        await _change_state(brain, transition, monkeypatch)
        if transition == "new_prompt":
            chats, audio = _split(captured_publish)
            assert [c["event_id"] for c in chats] == ["new-prompt"]
            assert [a.event_id for a in audio] == ["new-prompt"]
        captured_publish.clear()
    finally:
        blocked_stage.release.set()
        await asyncio.wait_for(pending, 1)
    assert captured_publish == []


@pytest.mark.parametrize("blocked_stage", ["translation", "chat", "tts"], indirect=True)
async def test_stale_provider_failure_does_not_publish_fallback(
    brain, blocked_stage, captured_publish, monkeypatch,
):
    pending = await _start_pending(brain, blocked_stage, captured_publish)
    try:
        await _change_state(brain, "module_exit", monkeypatch)
        captured_publish.clear()
        blocked_stage.fail = True
    finally:
        blocked_stage.release.set()
        await asyncio.wait_for(pending, 1)
    assert captured_publish == []


@pytest.mark.parametrize("blocked_stage", ["translation", "chat", "tts"], indirect=True)
async def test_notify_other_device_and_unchanged_settings_keep_turn_live(
    brain, blocked_stage, captured_publish,
):
    pending = await _start_pending(brain, blocked_stage, captured_publish)
    try:
        await device_state.set_cloud_tts(True)
        await device_state.set_puppet_mode(False)
        await brain.handle_remote_chat_event(
            DEVICE, _router("notify", "OPENMOXIE_CHAT", "notify-event", OPENER_KK),
        )
        await brain.handle_remote_chat_event(
            "other-device", _router("start", "WELCOME", "other-exit"),
        )
        captured_publish.clear()
    finally:
        blocked_stage.release.set()
        await asyncio.wait_for(pending, 1)
    chats, audio = _split(captured_publish)
    assert [a.event_id for a in audio] == [PROMPT_ID]
    assert audio[0].audio.buffer
    assert [c["event_id"] for c in chats] == (
        [] if blocked_stage.stage == "tts" else [PROMPT_ID]
    )
