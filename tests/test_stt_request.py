"""Tests for the STT-channel handler (wake-gate inject; no canned chat line).

Feeds framed `zmqSTTRequest` frames (START/SPEECH/END) into the handler and
asserts that exactly one framed `zmqSTTResponse` carrying the wake trigger is
published on END_OF_SPEECH before OPENMOXIE_CHAT, with the session `uuid`
echoed. After conversation is active, nothing is injected by default. No robot
or OpenAI client is needed — the handler does not transcribe.
"""

import pytest

from src.config import settings
from src.protos.embodied.perception.audio.zmqSTT_pb2 import (
    zmqSTTRequest,
    zmqSTTResponse,
)
from src.services import stt_request
from src.services.mqtt_service import mqtt_service
from tests.conftest import TEST_DEVICE_ID


def _frame(req: zmqSTTRequest) -> bytes:
    """Wire form: `<full.name>:` prefix + serialized proto."""
    return stt_request.STT_REQUEST_PREFIX + req.SerializeToString()


def _request(
    uuid: str,
    vad: int,
    *,
    audio: bytes = b"",
    timestamp: int = 0,
) -> zmqSTTRequest:
    req = zmqSTTRequest()
    req.uuid = uuid
    req.vad = vad
    if audio:
        req.audio_content = audio
    if timestamp:
        req.timestamp = timestamp
    return req


@pytest.fixture
def captured_protos(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, object]]:
    """Capture publish_proto (topic, message); reset per-device state each test."""
    stt_request._session_start_ms.clear()  # noqa: SLF001
    stt_request._conversation_active.clear()  # noqa: SLF001
    published: list[tuple[str, object]] = []
    monkeypatch.setattr(
        mqtt_service,
        "publish_proto",
        lambda topic, message: published.append((topic, message)),
    )
    yield published
    stt_request._session_start_ms.clear()  # noqa: SLF001
    stt_request._conversation_active.clear()  # noqa: SLF001


async def test_injects_wake_trigger_before_conversation_active(
    captured_protos: list[tuple[str, object]],
) -> None:
    """Before OPENMOXIE_CHAT is reached, inject the WAKEUP trigger phrase."""
    uuid = "sess-1"

    # Mid-stream frames must NOT produce a reply.
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request(uuid, zmqSTTRequest.VADState.START_OF_SPEECH, timestamp=1000)),
    )
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request(uuid, zmqSTTRequest.VADState.SPEECH, audio=b"\x00\x01")),
    )
    assert captured_protos == []

    # END_OF_SPEECH triggers exactly one zmqSTTResponse with the wake trigger.
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request(uuid, zmqSTTRequest.VADState.END_OF_SPEECH)),
    )

    assert len(captured_protos) == 1
    topic, message = captured_protos[0]
    assert topic == f"/devices/{TEST_DEVICE_ID}/commands/zmq"
    assert isinstance(message, zmqSTTResponse)
    assert message.uuid == uuid
    assert message.type == zmqSTTResponse.ResponseType.FINAL
    assert message.speech == settings.WAKEUP_TRIGGER_LINE
    # start_timestamp echoes the session's first-frame timestamp.
    assert message.start_timestamp == 1000


async def test_no_canned_chat_line_once_conversation_active(
    captured_protos: list[tuple[str, object]],
) -> None:
    """After OPENMOXIE_CHAT, do not inject a canned child line.

    The opener is spoken via remote-chat REMOTE_CHAT_OPENERS, not STT.
    """
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request("sess-2", zmqSTTRequest.VADState.END_OF_SPEECH)),
    )
    assert captured_protos == []

    # A reconnect re-arms the wake trigger.
    stt_request.reset_conversation_state(TEST_DEVICE_ID)
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request("sess-3", zmqSTTRequest.VADState.END_OF_SPEECH)),
    )
    assert len(captured_protos) == 1
    assert captured_protos[0][1].speech == settings.WAKEUP_TRIGGER_LINE


async def test_no_line_to_inject_sends_nothing(
    captured_protos: list[tuple[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no wake trigger, inject nothing."""
    monkeypatch.setattr(settings, "WAKEUP_TRIGGER_LINE", "")
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request("sess-4", zmqSTTRequest.VADState.END_OF_SPEECH)),
    )
    assert captured_protos == []


async def test_non_stt_payload_is_ignored(
    captured_protos: list[tuple[str, object]],
) -> None:
    result = await stt_request.handle_payload(
        TEST_DEVICE_ID,
        b"embodied.unity.CloudTTSRequest:not-an-stt-frame",
    )
    assert result is None
    assert captured_protos == []


def test_parse_stt_request_roundtrip() -> None:
    req = _request("abc", zmqSTTRequest.VADState.SPEECH, audio=b"\x01\x02\x03")
    parsed = stt_request.parse_stt_request(_frame(req))
    assert parsed is not None
    assert parsed.uuid == "abc"
    assert parsed.audio_content == b"\x01\x02\x03"
    assert stt_request.parse_stt_request(b"other:payload") is None
