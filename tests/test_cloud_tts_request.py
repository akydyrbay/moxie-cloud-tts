"""Inbound CloudTTSRequest -> synthesis -> CloudTTSResponse.

Verifies the robot-triggered path at the message boundary: a framed
CloudTTSRequest is decoded, markup-stripped, synthesized, and published back to
the *requesting* device with its event_id/chunk_num echoed.
"""

import json
import logging

import pytest

from app.config import settings
from app.protos.embodied.perception.audio.zmqSTT_pb2 import (
    zmqSTTRequest,
    zmqSTTResponse,
)
from app.protos.embodied.unity.CloudTTS_pb2 import (
    REMOTECHAT_TTS_REQUEST,
    ROBOT_TTS_REQUEST,
    CloudTTSRequest,
    CloudTTSResponse,
)
from app.services.speech.synthesis import handler as cloud_tts_request
from app.services.speech.recognition import handler as stt_request
from app.services.devices.state import device_state
from app.services.conversation.remote_chat import RemoteChatBrain
from tests.conftest import TEST_DEVICE_ID

OTHER_DEVICE = "some-other-robot"


RESPONSE_PREFIX = b"embodied.unity.CloudTTSResponse:"


def _frame_request(**kwargs) -> bytes:  # noqa: ANN003
    req = CloudTTSRequest(**kwargs)
    return (
        CloudTTSRequest.DESCRIPTOR.full_name.encode()
        + b":"
        + req.SerializeToString()
    )


def _decode_response(payload: bytes) -> CloudTTSResponse:
    assert payload.startswith(RESPONSE_PREFIX)
    resp = CloudTTSResponse()
    resp.ParseFromString(payload[len(RESPONSE_PREFIX):])
    return resp


async def test_handle_payload_replies_to_requesting_device(
    fake_openai, captured_publish,
) -> None:
    """The response must go to the robot that asked, not a configured default."""
    payload = _frame_request(
        markup="Сәлем! Қалайсың?", event_id="evt-9", chunk_num=3, user_id="u1",
    )
    summary = await cloud_tts_request.handle_payload(OTHER_DEVICE, payload)

    assert summary is not None
    assert summary["status"] == "published"
    assert summary["event_id"] == "evt-9"
    assert summary["chunk_num"] == 3
    assert summary["language"] == "kk"
    assert summary["device_id"] == OTHER_DEVICE

    assert len(captured_publish) == 1
    topic, out = captured_publish[0]
    assert topic == f"/devices/{OTHER_DEVICE}/commands/zmq"
    resp = _decode_response(out)
    assert resp.event_id == "evt-9"
    assert resp.chunk_num == 3
    assert len(resp.audio.buffer) > 0
    # WAV is never the default: AudioBuffer is a raw-PCM payload.
    assert not resp.audio.buffer.startswith(b"RIFF")


@pytest.fixture
def translation_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make regression tests independent of the operator's .env choices."""
    monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", True)
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")


@pytest.mark.parametrize(
    ("local_text", "translated", "language"),
    [
        ("Good morning!", "Қайырлы таң!", "kk"),
        ("Good morning.", "Доброе утро.", "ru"),
    ],
)
async def test_translation_preserves_slot_commands_and_lip_sync(
    fake_openai, captured_publish, translation_settings, monkeypatch, caplog,
    local_text, translated, language,
) -> None:
    """Use real response assembly: only text changes, not the live audio slot."""
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", language)
    fake_openai.chat.completions._reply = translated
    # One second of mock PCM, long enough to distinguish it from 200 ms silence.
    pcm = b"\x00\x01" * 24000
    monkeypatch.setattr(fake_openai.audio.speech, "_pcm", pcm)
    markup = (
        '<mark name="cmd:behaviour-tree,wake"/>'
        f'<usel variant="2" genre="none">{local_text}</usel>'
        '<mark name="cmd:attachment-animator,stop"/>'
    )
    req = CloudTTSRequest(markup=markup, event_id="wake-slot", chunk_num=3)
    with caplog.at_level(logging.INFO):
        summary = await cloud_tts_request.handle_cloud_tts_request(OTHER_DEVICE, req)

    assert req.markup == markup
    assert len(fake_openai.audio.speech.calls) == 1
    assert fake_openai.chat.completions.calls[0]["messages"][-1]["content"] == local_text
    assert fake_openai.audio.speech.calls[0]["input"] == translated
    assert fake_openai.audio.speech.calls[0]["response_format"] == "pcm"
    assert summary["language"] == language
    assert summary["audio_bytes"] == len(pcm)
    assert len(captured_publish) == 1
    topic, payload = captured_publish[0]
    assert topic == f"/devices/{OTHER_DEVICE}/commands/zmq"
    resp = _decode_response(payload)
    assert (resp.event_id, resp.chunk_num) == ("wake-slot", 3)
    assert resp.request_source == ROBOT_TTS_REQUEST
    assert resp.audio.buffer == pcm
    assert (resp.audio.sample_rate, resp.audio.channels) == (24000, 1)
    commands = [(m.value, m.time) for m in resp.marks if m.type == "ssml"]
    assert commands == [
        ("cmd:behaviour-tree,wake", 0),
        ("cmd:attachment-animator,stop", 1000),
    ]
    assert any(m.type == "viseme" and m.value != "sil" for m in resp.marks)
    assert any(m.type == "word" and m.value == translated.split()[0] for m in resp.marks)
    assert resp.marks[-1].value == "sil"
    assert "CloudTTSRequest translation" in caplog.text
    assert f"requested_text={local_text!r}" in caplog.text
    assert f"synthesis_text={translated!r}" in caplog.text
    assert f"spoke event_id=wake-slot chunk=3 language={language}" in caplog.text


@pytest.mark.parametrize("markup", [
    '<mark name="cmd:CereprocWarmup"/>',
    '<mark name="cmd:CereprocWarmup"/><usel>Excellent!</usel>',
])
async def test_empty_or_failed_synthesis_still_echoes_commands_once(
    fake_openai, translated_lines, captured_publish, translation_settings, monkeypatch, markup,
) -> None:
    def fail_synthesis(**kwargs):
        raise RuntimeError("TTS unavailable")

    monkeypatch.setattr(fake_openai.audio.speech, "create", fail_synthesis)
    summary = await cloud_tts_request.handle_payload(
        TEST_DEVICE_ID, _frame_request(markup=markup, event_id="wake-fail", chunk_num=2),
    )
    assert summary["status"] in {"failed", "published_silence"}
    assert len(captured_publish) == 1
    resp = _decode_response(captured_publish[0][1])
    assert (resp.event_id, resp.chunk_num) == ("wake-fail", 2)
    assert resp.audio.buffer == b"\x00\x00" * 4800
    assert [m.value for m in resp.marks if m.type == "ssml"] == ["cmd:CereprocWarmup"]


@pytest.mark.parametrize("firmware_uses_local_line", [True, False])
@pytest.mark.usefixtures("robot_selection")
async def test_wakeup_to_audio_even_when_chat_prompt_arrives_first(
    fake_openai, translated_lines, captured_publish, translation_settings, caplog, firmware_uses_local_line,
) -> None:
    """Translate the live line, regardless of a later remote-chat prompt's ID.

    Robot requests are simulated; this proves server correlation, not playback.
    """
    brain = RemoteChatBrain()
    await device_state.set_cloud_tts(True)
    with caplog.at_level(logging.INFO):
        await brain.ensure_device_bootstrapped(TEST_DEVICE_ID)
    config = json.loads(captured_publish[0][1])
    assert config["settings"]["props"]["cloud_tts"] == "1"
    assert "moxie_mode" not in config
    assert "cloud_tts=1 moxie_mode=None" in caplog.text
    captured_publish.clear()

    wake_id = "5c491ac0-73f6-41a9-b2f5-b1afe3585198"
    prompt_id = "12283584-6239-4642-bd20-ef62906c5704"
    req = zmqSTTRequest(uuid=wake_id, vad=zmqSTTRequest.VADState.END_OF_SPEECH)
    await stt_request.handle_payload(
        TEST_DEVICE_ID, stt_request.STT_REQUEST_PREFIX + req.SerializeToString(),
    )
    prefix = zmqSTTResponse.DESCRIPTOR.full_name.encode() + b":"
    stt = zmqSTTResponse.FromString(captured_publish.pop()[1][len(prefix):])
    assert stt.uuid == wake_id
    assert stt.speech == "Moxie, let's go"
    await brain.handle_router_request(TEST_DEVICE_ID, {
        "backend": "router", "module_id": "WAKEUP", "command": "start",
        "event_id": wake_id, "speech": stt.speech,
    })
    entrance = json.loads(captured_publish[-1][1])
    assert entrance["event_id"] == wake_id
    assert entrance["output"]["text"] == "I'm sorry. Can you repeat that?"
    assert entrance["response_actions"] == [{"output_type": "FALLBACK"}]
    await brain.handle_router_request(TEST_DEVICE_ID, {
        "backend": "router", "module_id": "OPENMOXIE_CHAT", "content_id": "default",
        "command": "prompt", "event_id": prompt_id,
    })
    # Firmware raises no CloudTTSRequest for remote output (Sep 28): the prompt
    # reply is voiced as REMOTECHAT on prompt_id, never on the wake request.
    assert len(captured_publish) == 3
    assert all(t.endswith("/commands/remote_chat") for t, _ in captured_publish[:2])
    opener_audio = _decode_response(captured_publish[2][1])
    assert opener_audio.request_source == REMOTECHAT_TTS_REQUEST
    assert (opener_audio.event_id, opener_audio.chunk_num) == (prompt_id, 0)
    captured_publish.clear()
    markup = (
        '<usel variant="2" genre="none">Excellent!</usel> " "'
        if firmware_uses_local_line else entrance["output"]["markup"]
    )
    try:
        await cloud_tts_request.handle_payload(
            TEST_DEVICE_ID, _frame_request(markup=markup, event_id=wake_id),
        )
    finally:
        stt_request.reset_conversation_state(TEST_DEVICE_ID)
    assert len(captured_publish) == 1
    resp = _decode_response(captured_publish[0][1])
    assert resp.event_id == wake_id
    assert resp.request_source == ROBOT_TTS_REQUEST
    expected_source = 'Excellent! " "' if firmware_uses_local_line else entrance["output"]["text"]
    assert fake_openai.audio.speech.calls[-1]["input"] == translated_lines[expected_source]
