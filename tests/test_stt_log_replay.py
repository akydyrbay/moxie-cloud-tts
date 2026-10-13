"""Replay Sep 28 phase/UUIDs, with synthetic audio and explicit robot next turns."""

import json
import logging
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures("robot_selection")

from src.config import settings
from src.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest, zmqSTTResponse
from src.services import cloud_tts_request, stt_request
from src.services.remote_chat import RemoteChatBrain
from tests.test_cloud_tts_request import _decode_response, _frame_request

CAPTURE = json.loads((Path(__file__).parent / "fixtures/stt_2026_09_28.json").read_text())


@pytest.mark.parametrize("command", ["continue", "reprompt"])
async def test_failed_logged_chat_listens_now_reach_brain_and_cloud_tts(
    command, fake_openai, captured_publish, caplog, monkeypatch,
):
    device = CAPTURE["device_id"]
    brain = RemoteChatBrain()
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    monkeypatch.setattr(settings, "STT_ENGLISH_BRIDGE_ENABLED", False)
    caplog.set_level(logging.INFO)
    await brain.ensure_device_bootstrapped(device)
    captured_publish.clear()
    # The original wake shim still opens the gate.
    await stt_request.handle_stt_request(device, zmqSTTRequest(
        uuid=CAPTURE["wake_uuid"], vad=3,
    ))
    prefix = zmqSTTResponse.DESCRIPTOR.full_name.encode() + b":"
    wake = zmqSTTResponse.FromString(captured_publish.pop()[1][len(prefix):])
    assert wake.speech == settings.WAKEUP_TRIGGER_LINE
    await brain.handle_remote_chat_event(device, json.dumps({
        "backend": "router", "command": "continue", "module_id": "WAKEUP",
        "event_id": CAPTURE["wake_uuid"], "speech": wake.speech,
    }))
    assert json.loads(captured_publish.pop()[1])["response_action"]["output_type"] == "FALLBACK"
    await brain.handle_remote_chat_event(device, json.dumps({
        "backend": "router", "command": "prompt", "module_id": "OPENMOXIE_CHAT",
        "content_id": "default", "event_id": CAPTURE["chat_prompt_event_id"], "speech": "",
    }))
    captured_publish.clear()
    for listen in CAPTURE["chat_listens"]:
        fake_openai.audio.transcriptions._text = listen["test_transcript"]
        for vad in [1, 2, 3]:
            request = zmqSTTRequest(uuid=listen["uuid"], vad=vad, audio_content=b"\x01\x02" * 1600)
            await stt_request.handle_payload(device, stt_request.STT_REQUEST_PREFIX + request.SerializeToString())
        assert len(captured_publish) == 1
        topic, wire = captured_publish.pop()
        assert topic == f"/devices/{device}/commands/zmq"
        assert wire.startswith(prefix)
        result = zmqSTTResponse.FromString(wire[len(prefix):])
        assert (result.uuid, result.type, result.error_code, result.speech) == (
            listen["uuid"], zmqSTTResponse.ResponseType.FINAL, 0, listen["test_transcript"],
        )
        # Simulate the firmware delivering the transcript as a remote-chat turn.
        # There is deliberately no notify history before the first reprompt.
        fake_openai.chat.completions._reply = "Алма өте дәмді! Басқа қандай жемісті ұнатасың?"
        await brain.handle_remote_chat_event(device, json.dumps({
            "backend": "router", "command": command, "module_id": "OPENMOXIE_CHAT",
            "content_id": "default", "event_id": result.uuid, "speech": result.speech,
        }))
        messages = fake_openai.chat.completions.calls[-1]["messages"]
        assert messages[-1] == {"role": "user", "content": result.speech}
        response = json.loads(captured_publish.pop()[1])
        assert response["event_id"] == result.uuid
        assert response["output"]["text"] == fake_openai.chat.completions._reply
        # cloud_tts is off here, so the brain pushes no REMOTECHAT audio (see
        # test_remote_chat_audio.py). This checks the robot-sourced path only.
        await cloud_tts_request.handle_payload(device, _frame_request(
            markup=response["output"]["markup"], event_id=result.uuid, chunk_num=0,
        ))
        audio = _decode_response(captured_publish.pop()[1])
        assert (audio.event_id, audio.chunk_num) == (result.uuid, 0)
        assert audio.audio.buffer
    assert "phase=wake-gate" in caplog.text
    assert "remote-chat entered" in caplog.text
    assert "openai stt transcribed" in caplog.text
    assert "STT FINAL device=" in caplog.text
    assert "phase=chat" in caplog.text
    assert "STT FINAL error" not in caplog.text
    assert "Robot transcription is not implemented" not in caplog.text
