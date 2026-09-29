"""Language policy across module entry, model context and the live TTS waiter."""

import json

import pytest
from pydantic import ValidationError

from src.config import REMOTE_CHAT_OPENERS, Settings, settings
from src.services import cloud_tts_request
from src.services.device_state import device_state
from src.services.remote_chat import RemoteChatBrain, fallback_line, generation_error_line
from tests.conftest import TEST_DEVICE_ID
from tests.test_cloud_tts_request import _decode_response, _frame_request


@pytest.fixture
def brain(monkeypatch):
    brain = RemoteChatBrain()
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", True)
    return brain


async def turn(brain, command, speech="", **extra):
    await brain.handle_router_request(TEST_DEVICE_ID, {
        "backend": "router", "module_id": "OPENMOXIE_CHAT", "content_id": "default",
        "command": command, "event_id": "chat-turn", "speech": speech, **extra,
    })


def test_language_defaults_and_validation():
    defaults = Settings(_env_file=None)
    assert defaults.DEFAULT_TTS_LANGUAGE == "kk"
    assert defaults.CLOUD_TTS_TRANSLATION_ENABLED is True
    assert defaults.OPENAI_SYSTEM_PROMPT.startswith("You are Moxie")
    assert "Kazakh" not in defaults.OPENAI_SYSTEM_PROMPT
    assert "SAME language" not in defaults.OPENAI_SYSTEM_PROMPT
    with pytest.raises(ValidationError, match="DEFAULT_TTS_LANGUAGE"):
        Settings(_env_file=None, DEFAULT_TTS_LANGUAGE="kz")


@pytest.mark.parametrize("command", ["start", "prompt"])
async def test_entry_preserves_source_opener_until_robot_requests_audio(
    brain, fake_openai, captured_publish, command,
):
    await turn(brain, command, "")
    assert brain.output_language(TEST_DEVICE_ID) == "kk"
    assert fake_openai.chat.completions.calls == []
    assert fake_openai.audio.speech.calls == []
    assert len(captured_publish) == 1
    response = json.loads(captured_publish[0][1])
    assert response["output"]["text"] == REMOTE_CHAT_OPENERS["default"]
    assert response["response_actions"] == [{"output_type": "GLOBAL_RESPONSE"}]


@pytest.mark.parametrize("speech", ["Moxie, let's go", "Привет", "Сәлем!"])
async def test_input_and_notify_history_cannot_change_response_language(
    brain, fake_openai, captured_publish, speech,
):
    await turn(brain, "notify", "Good morning!", extra_lines=[
        {"context_type": "input", "text": "hello"},
    ])
    assert brain.output_language(TEST_DEVICE_ID) is None
    assert captured_publish == []
    fake_openai.chat.completions._reply = "Сені көргеніме қуаныштымын!"
    await turn(brain, "continue", speech)
    messages = fake_openai.chat.completions.calls[0]["messages"]
    assert "Reply only in Kazakh" in messages[0]["content"]
    assert "Russian" in messages[0]["content"]  # anti-drift line for kk
    assert all(m["content"] != "Good morning!" for m in messages)
    assert {"role": "user", "content": "hello"} in messages
    assert messages[-1]["content"] == (speech or fallback_line("kk"))
    response = json.loads(captured_publish[0][1])
    assert response["output"]["text"] == fake_openai.chat.completions._reply

    await cloud_tts_request.handle_payload(TEST_DEVICE_ID, _frame_request(
        markup=response["output"]["markup"], event_id="live-tts", chunk_num=2,
    ))
    assert fake_openai.audio.speech.calls[0]["input"] == response["output"]["text"]
    audio = _decode_response(captured_publish[-1][1])
    assert (audio.event_id, audio.chunk_num) == ("live-tts", 2)


@pytest.mark.parametrize("reply", ["Hello!", "Привет!", "Hello, Сәлем!", "", "<sleep>"])
async def test_invalid_model_output_becomes_kazakh_without_side_effects(
    brain, fake_openai, captured_publish, reply,
):
    fake_openai.chat.completions._reply = reply
    await turn(brain, "continue", "Привет")
    response = json.loads(captured_publish[0][1])
    assert response["output"]["text"] == generation_error_line("kk")
    assert response["response_actions"] == [{"output_type": "GLOBAL_RESPONSE"}]


async def test_model_failure_with_english_input_stays_kazakh(
    brain, fake_openai, captured_publish, monkeypatch,
):
    def fail(**kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(fake_openai.chat.completions, "create", fail)
    await turn(brain, "continue", "hello")
    assert json.loads(captured_publish[0][1])["output"]["text"] == generation_error_line("kk")


@pytest.mark.parametrize("mode", ["active", "notify", "puppet", "disabled", "other-device"])
async def test_translation_is_independent_of_chat_entry_and_preserves_waiter(
    brain, fake_openai, translated_lines, captured_publish, monkeypatch, mode,
):
    await turn(brain, "notify" if mode == "notify" else "prompt")
    if mode == "puppet":
        await device_state.set_puppet_mode(True)
    if mode == "disabled":
        monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", False)
    captured_publish.clear()
    device = "another-device" if mode == "other-device" else TEST_DEVICE_ID
    summary = await cloud_tts_request.handle_payload(device, _frame_request(
        markup='<mark name="cmd:idlestate,data:{+idleState+:5}"/><usel>Hello!</usel>',
        event_id="", chunk_num=3,
    ))
    expected = "Hello!" if mode in {"puppet", "disabled"} else translated_lines["Hello!"]
    assert fake_openai.audio.speech.calls[0]["input"] == expected
    assert summary["status"] == "published"
    assert len(captured_publish) == 1
    audio = _decode_response(captured_publish[0][1])
    assert (audio.event_id, audio.chunk_num) == ("", 3)
    assert [m.value for m in audio.marks if m.type == "ssml"] == [
        "cmd:idlestate,data:{+idleState+:5}",
    ]


async def test_delayed_notify_does_not_clear_language_but_reentry_and_bootstrap_do(
    brain, captured_publish,
):
    await turn(brain, "prompt")
    await turn(brain, "notify", "Good morning.", module_id="WAKEUP")
    assert brain.output_language(TEST_DEVICE_ID) == "kk"
    await turn(brain, "start", module_id="WAKEUP")
    assert brain.output_language(TEST_DEVICE_ID) is None
    await turn(brain, "prompt")
    await brain.ensure_device_bootstrapped(TEST_DEVICE_ID)
    assert brain.output_language(TEST_DEVICE_ID) is None


@pytest.mark.parametrize("content_id", ["", "short"])
async def test_notify_content_alias_does_not_discard_active_language(
    brain, captured_publish, content_id,
):
    await turn(brain, "prompt")
    await turn(brain, "notify", "Сәлем!", content_id=content_id)
    assert brain.output_language(TEST_DEVICE_ID) == "kk"


async def test_russian_can_be_explicitly_selected_for_chat(
    brain, fake_openai, captured_publish, monkeypatch,
):
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "ru")
    fake_openai.chat.completions._reply = "Привет! Как дела?"
    await turn(brain, "continue", "Сәлем!")
    messages = fake_openai.chat.completions.calls[0]["messages"]
    assert "Reply only in Russian" in messages[0]["content"]
    assert "Kazakh" not in messages[0]["content"]
    assert json.loads(captured_publish[0][1])["output"]["text"] == "Привет! Как дела?"
