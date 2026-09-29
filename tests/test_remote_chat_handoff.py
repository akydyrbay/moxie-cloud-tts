"""Replay logged handoffs; supplied robot inputs do not prove firmware acceptance."""

import json
import logging
from pathlib import Path

import pytest

from src.config import settings
from src.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest, zmqSTTResponse
from src.protos.embodied.unity.CloudTTS_pb2 import REMOTECHAT_TTS_REQUEST
from src.services import cloud_tts_request, stt_request
from src.services.device_state import device_state
from src.services.remote_chat import RemoteChatBrain
from tests.test_cloud_tts_request import _decode_response, _frame_request

CAPTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "remote_chat_handoff.json").read_text(),
)
DEVICE = CAPTURE["device_id"]


@pytest.fixture
def brain(monkeypatch):
    instance = RemoteChatBrain()
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", True)
    return instance


async def listen(device, uuid, published):
    """Finish a firmware listen and decode the single correlated reply."""
    published.clear()
    req = zmqSTTRequest(uuid=uuid, vad=zmqSTTRequest.VADState.END_OF_SPEECH)
    await stt_request.handle_stt_request(device, req)
    assert len(published) == 1
    topic, framed = published.pop()
    prefix = zmqSTTResponse.DESCRIPTOR.full_name.encode() + b":"
    assert topic == f"/devices/{device}/commands/zmq"
    assert framed.startswith(prefix)
    response = zmqSTTResponse.FromString(framed[len(prefix):])
    assert response.uuid == uuid
    assert response.type == zmqSTTResponse.ResponseType.FINAL
    return response


@pytest.mark.parametrize("run", CAPTURE["runs"], ids=lambda run: run["name"])
async def test_logged_handoff_sequences(
    run, brain, fake_openai, translated_lines, captured_publish, caplog,
):
    await device_state.set_cloud_tts(True)
    await brain.ensure_device_bootstrapped(DEVICE)
    caplog.set_level(logging.INFO)
    entered = False
    for event in run["events"]:
        captured_publish.clear()
        if event["kind"] == "activity":
            await brain.handle_activity_log_event(DEVICE, json.dumps(event["request"]))
            assert not captured_publish
        elif event["kind"] == "router":
            request = event["request"]
            await brain.handle_remote_chat_event(DEVICE, json.dumps(request))
            if request["command"] == "notify":
                assert not captured_publish
            else:
                remote = request["module_id"] == "OPENMOXIE_CHAT"
                # An accepted remote turn is followed by its REMOTECHAT audio;
                # native entrances keep their robot-originated TTS request.
                assert len(captured_publish) == (2 if remote else 1)
                topic, data = captured_publish[0]
                response = json.loads(data)
                assert topic.endswith("/commands/remote_chat")
                assert response["event_id"] == request["event_id"]
                assert response["input_speech"] == request["speech"]
                if remote:
                    audio = _decode_response(captured_publish[1][1])
                    assert audio.request_source == REMOTECHAT_TTS_REQUEST
                    assert (audio.event_id, audio.chunk_num) == (request["event_id"], 0)
                    assert audio.audio.buffer
                expected = "GLOBAL_RESPONSE" if remote else "FALLBACK"
                assert response["response_action"] == {"output_type": expected}
                assert response["response_actions"] == [{"output_type": expected}]
                assert response["fallback"] is False
                entered = remote
                # Use a distinct synthetic UUID: these END frames were not logged.
                stt = await listen(DEVICE, f"probe-{request['event_id']}", captured_publish)
                entrance = request["module_id"] in {"WAKEUP", "ENROLLCONVO"}
                assert stt.speech == (settings.WAKEUP_TRIGGER_LINE if entrance else "")
                assert stt.error_code == (0 if entrance else 66)
        elif event["kind"] == "tts":
            # Logs preserve speakable text, not complete original markup.
            await cloud_tts_request.handle_payload(DEVICE, _frame_request(
                markup=event["text"], event_id=event["event_id"],
                chunk_num=event["chunk_num"],
            ))
            assert len(captured_publish) == 1
            response = _decode_response(captured_publish[0][1])
            assert (response.event_id, response.chunk_num) == (
                event["event_id"], event["chunk_num"],
            )
            assert response.audio.buffer
            assert fake_openai.audio.speech.calls[-1]["input"] == translated_lines[event["text"]]
        assert brain.output_language(DEVICE) == ("kk" if entered else None)

    assert entered is run["observed_chat_entry"]
    assert "entrance deferred to local response" in caplog.text
    assert ("remote-chat entered" in caplog.text) is entered
    assert "output_type=GLOBAL_COMMAND" not in caplog.text
    if run["name"] == "scheduled_then_dismount":
        assert "remote-chat missing module" in caplog.text


@pytest.mark.parametrize("exit_module", ["WAKEUP", "ENROLLCONVO", "SESSIONDISMOUNT", ""])
async def test_exit_reentry_and_delayed_notify_keep_brain_and_stt_in_sync(
    exit_module, brain, captured_publish,
):
    async def route(command, module, device=DEVICE):
        await brain.handle_router_request(device, {
            "backend": "router", "command": command, "module_id": module,
            "content_id": "default" if module == "OPENMOXIE_CHAT" else "",
            "event_id": f"{module}-{command}",
        })

    await route("prompt", "OPENMOXIE_CHAT")
    await route("prompt", "OPENMOXIE_CHAT", "other-robot")
    # Neither history nor scheduled activity changes ownership.
    await route("notify", exit_module)
    assert brain.output_language(DEVICE) == "kk"
    assert (await listen(DEVICE, "chat-listen", captured_publish)).speech == ""
    await route("start", exit_module)
    assert brain.output_language(DEVICE) is None
    result = await listen(DEVICE, "after-exit", captured_publish)
    assert result.speech == (
        settings.WAKEUP_TRIGGER_LINE if exit_module in {"WAKEUP", "ENROLLCONVO"} else ""
    )
    assert brain.output_language("other-robot") == "kk"
    assert (await listen("other-robot", "other-listen", captured_publish)).speech == ""

    await route("start", "WAKEUP")
    assert (await listen(DEVICE, "second-wake", captured_publish)).speech == settings.WAKEUP_TRIGGER_LINE
    await route("prompt", "OPENMOXIE_CHAT")
    assert brain.output_language(DEVICE) == "kk"
    assert (await listen(DEVICE, "second-chat", captured_publish)).speech == ""
