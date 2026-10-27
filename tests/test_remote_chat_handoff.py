"""Module exit/re-entry keeps remote-chat ownership and STT phase in sync."""

import pytest

from src.config import settings
from src.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest, zmqSTTResponse
from src.services import stt_request
from src.services.remote_chat import RemoteChatBrain
from tests.conftest import TEST_DEVICE_ID

DEVICE = TEST_DEVICE_ID


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


async def test_session_dismount_answer_is_transcribed(brain, fake_openai, captured_publish):
    """SESSIONDISMOUNT asks "keep playing?"; its answer must reach the robot."""
    await brain.handle_router_request(DEVICE, {
        "backend": "router", "command": "prompt", "module_id": "OPENMOXIE_CHAT",
        "content_id": "default", "event_id": "chat-prompt",
    })
    await brain.handle_router_request(DEVICE, {
        "backend": "router", "command": "start", "module_id": "SESSIONDISMOUNT",
        "event_id": "dismount-start",
    })
    assert brain.output_language(DEVICE) is None
    fake_openai.audio.transcriptions._text = "Yes"
    captured_publish.clear()
    await stt_request.handle_stt_request(DEVICE, zmqSTTRequest(
        uuid="dismount-answer", vad=zmqSTTRequest.VADState.END_OF_SPEECH,
        audio_content=b"\x01\x02" * 1600,
    ))
    prefix = zmqSTTResponse.DESCRIPTOR.full_name.encode() + b":"
    response = zmqSTTResponse.FromString(captured_publish[-1][1][len(prefix):])
    assert (response.uuid, response.speech, response.error_code) == ("dismount-answer", "Yes", 0)
    assert len(fake_openai.audio.transcriptions.calls) == 1
