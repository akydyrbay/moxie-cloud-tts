"""Microphone accumulation, correlated FINAL, deadlines and session lifecycle."""

import pytest

from src.config import settings
from src.protos.embodied.perception.audio.zmqSTT_pb2 import (
    zmqSTTRequest,
    zmqSTTResponse,
)
from src.services import stt_request
from src.services.mqtt_service import mqtt_service
from tests.conftest import TEST_DEVICE_ID


@pytest.fixture(autouse=True)
def original_transcript_mode(monkeypatch):
    """Exercise recognition's A/B bypass; bridge-on cases have their own suite."""
    monkeypatch.setattr(settings, "STT_ENGLISH_BRIDGE_ENABLED", False)


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
    """Capture publish_proto (topic, message); conftest isolates STT state."""
    published: list[tuple[str, object]] = []
    monkeypatch.setattr(
        mqtt_service,
        "publish_proto",
        lambda topic, message: published.append((topic, message)),
    )
    return published


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


async def test_chat_listen_completes_without_canned_child_speech(
    captured_protos: list[tuple[str, object]],
) -> None:
    """After OPENMOXIE_CHAT, do not inject a canned child line.

    The opener is spoken via remote-chat REMOTE_CHAT_OPENERS, not STT.
    """
    # A listen can start during WAKEUP and finish after the chat prompt.
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request("sess-2", zmqSTTRequest.VADState.START_OF_SPEECH, timestamp=1000)),
    )
    assert captured_protos == []
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request("sess-2", zmqSTTRequest.VADState.END_OF_SPEECH)),
    )
    assert len(captured_protos) == 1
    response = captured_protos[0][1]
    assert response.type == zmqSTTResponse.ResponseType.FINAL
    assert response.uuid == "sess-2"
    assert response.start_timestamp == 1000
    assert response.end_timestamp >= response.start_timestamp
    assert response.speech == ""
    assert response.error_code == 66
    assert "transcription" in response.error_message
    captured_protos.clear()

    # A reconnect re-arms the wake trigger.
    stt_request.reset_conversation_state(TEST_DEVICE_ID)
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request("sess-3", zmqSTTRequest.VADState.END_OF_SPEECH)),
    )
    assert len(captured_protos) == 1
    assert captured_protos[0][1].speech == settings.WAKEUP_TRIGGER_LINE


async def test_reconnect_discards_only_that_devices_pending_listens(
    captured_protos, monkeypatch,
):
    """A new robot session must not reuse an earlier STT start timestamp."""
    monkeypatch.setattr(stt_request, "_now_ms", lambda: 2000)
    for device in [TEST_DEVICE_ID, "other-robot"]:
        stt_request.note_conversation_active(device)
        await stt_request.handle_stt_request(
            device, _request("same-uuid", zmqSTTRequest.VADState.START_OF_SPEECH, timestamp=1000),
        )
    stt_request.reset_conversation_state(TEST_DEVICE_ID)
    for device in [TEST_DEVICE_ID, "other-robot"]:
        await stt_request.handle_stt_request(
            device, _request("new-uuid" if device == TEST_DEVICE_ID else "same-uuid",
                             zmqSTTRequest.VADState.END_OF_SPEECH),
        )
    restarted = captured_protos[0][1]
    other = captured_protos[1][1]
    assert restarted.start_timestamp == 2000
    assert restarted.speech == settings.WAKEUP_TRIGGER_LINE
    assert other.start_timestamp == 1000
    assert other.speech == ""
    assert other.error_code == 66


async def test_disabled_wake_trigger_still_completes_listen(
    captured_protos: list[tuple[str, object]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty trigger disables fabricated speech, not the STT protocol."""
    monkeypatch.setattr(settings, "WAKEUP_TRIGGER_LINE", "")
    await stt_request.handle_payload(
        TEST_DEVICE_ID,
        _frame(_request("sess-4", zmqSTTRequest.VADState.END_OF_SPEECH)),
    )
    assert len(captured_protos) == 1
    response = captured_protos[0][1]
    assert response.uuid == "sess-4"
    assert response.type == zmqSTTResponse.ResponseType.FINAL
    assert response.speech == ""
    assert response.error_code == 66


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


@pytest.mark.parametrize("transcript", ["Менің сүйікті жемісім — алма.", "Мой любимый фрукт — яблоко."])
async def test_chat_accumulates_every_frame_and_preserves_transcript(
    transcript, fake_openai, captured_protos,
):
    import io
    import wave

    fake_openai.audio.transcriptions._text = transcript
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    chunks = [b"\x01\x02" * 800, b"\x03\x04" * 1600, b"\x05\x06" * 800]
    for index, (vad, chunk) in enumerate(zip([1, 2, 3], chunks)):
        await stt_request.handle_payload(TEST_DEVICE_ID, _frame(_request(
            "actual-speech", vad, audio=chunk, timestamp=1000 + index * 100,
        )))
        assert len(captured_protos) == (1 if vad == 3 else 0)
    assert len(fake_openai.audio.transcriptions.calls) == 1
    request = fake_openai.audio.transcriptions.calls[0]
    assert request["model"] == settings.OPENAI_STT_MODEL
    assert "language" not in request  # Detect from audio; never force kk onto ru.
    filename, wav = request["file"]
    assert filename == "stt.wav"
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) == (16000, 1, 2)
        assert reader.readframes(reader.getnframes()) == b"".join(chunks)
    response = captured_protos[0][1]
    assert (response.uuid, response.type, response.speech, response.error_code) == (
        "actual-speech", zmqSTTResponse.ResponseType.FINAL, transcript, 0,
    )
    assert (response.start_timestamp, response.end_timestamp) == (1000, 1200)
    assert response.timestamp > 0
    assert response.language == ""  # No invented detected-language label.
    assert fake_openai.options == [{"max_retries": 0, "timeout": settings.OPENAI_STT_TIMEOUT_S}]


@pytest.mark.parametrize("initial_vad", [2, 3])
async def test_missing_start_and_end_only_audio(fake_openai, captured_protos, initial_vad):
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("missing-start", initial_vad, audio=b"\x01\x02" * 1600))
    if initial_vad != 3:
        await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("missing-start", 3))
    assert len(captured_protos) == 1
    assert captured_protos[0][1].speech == fake_openai.audio.transcriptions._text


@pytest.mark.parametrize("mode", ["empty", "silent", "odd", "provider-error", "empty-result"])
async def test_recognition_failure_completes_once_and_next_listen_recovers(
    mode, fake_openai, captured_protos, monkeypatch,
):
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    transcribe = fake_openai.audio.transcriptions.create
    audio = {"empty": b"", "silent": b"\x00\x00" * 100, "odd": b"\x01"}.get(mode, b"\x01\x02" * 1600)
    if mode == "provider-error":
        def fail(**kwargs):
            raise RuntimeError("secret provider error body")
        monkeypatch.setattr(fake_openai.audio.transcriptions, "create", fail)
    if mode == "empty-result":
        fake_openai.audio.transcriptions._text = "  "
    end = _request("failed-uuid", 3, audio=audio)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, end)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, end)
    assert len(captured_protos) == 1
    response = captured_protos[0][1]
    assert (response.uuid, response.type, response.speech, response.error_code) == (
        "failed-uuid", zmqSTTResponse.ResponseType.FINAL, "", 66,
    )
    assert response.error_message
    assert "secret" not in response.error_message
    if mode in {"empty", "silent", "odd"}:
        assert fake_openai.audio.transcriptions.calls == []
    monkeypatch.setattr(fake_openai.audio.transcriptions, "create", transcribe)
    fake_openai.audio.transcriptions._text = "Сәлем!"
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("next-uuid", 3, audio=b"\x01\x02" * 1600))
    assert captured_protos[-1][1].speech == "Сәлем!"
    assert len(captured_protos) == 2


async def test_wake_and_onboard_never_call_provider(fake_openai, captured_protos):
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("wake", 3, audio=b"\x01\x02" * 1600))
    assert captured_protos[-1][1].speech == settings.WAKEUP_TRIGGER_LINE
    stt_request.note_onboard_module(TEST_DEVICE_ID, wake_gate=False)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("onboard", 3, audio=b"\x01\x02" * 1600))
    assert captured_protos[-1][1].error_code == 66
    assert fake_openai.audio.transcriptions.calls == []


@pytest.fixture
async def blocked_provider(fake_openai, monkeypatch):
    """Hold a synchronous provider call while the asyncio handler can progress."""
    import asyncio
    import threading

    loop = asyncio.get_running_loop()
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    def blocked(**kwargs):
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=2)
        loop.call_soon_threadsafe(finished.set)
        return type("Result", (), {"text": "Сәлем!"})()

    monkeypatch.setattr(fake_openai.audio.transcriptions, "create", blocked)
    yield entered, release, finished
    release.set()


async def test_duplicate_end_while_pending_and_after_final(blocked_provider, captured_protos):
    import asyncio

    entered, release, finished = blocked_provider
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    end = _request("duplicate", 3, audio=b"\x01\x02" * 1600)
    pending = asyncio.create_task(stt_request.handle_stt_request(TEST_DEVICE_ID, end))
    await asyncio.wait_for(entered.wait(), 1)
    assert await stt_request.handle_stt_request(TEST_DEVICE_ID, end) is None
    assert captured_protos == []
    release.set()
    await pending
    await stt_request.handle_stt_request(TEST_DEVICE_ID, end)
    assert len(captured_protos) == 1
    assert captured_protos[0][1].speech == "Сәлем!"


async def test_total_deadline_discards_late_worker_result(blocked_provider, captured_protos, monkeypatch):
    import asyncio
    import time

    entered, release, finished = blocked_provider
    monkeypatch.setattr(settings, "OPENAI_STT_TIMEOUT_S", 0.03)
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    start = time.monotonic()
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("timeout", 3, audio=b"\x01\x02" * 1600))
    assert time.monotonic() - start < 0.5
    assert len(captured_protos) == 1
    assert captured_protos[0][1].error_code == 66
    assert "deadline" in captured_protos[0][1].error_message
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    await asyncio.sleep(0)
    assert len(captured_protos) == 1


@pytest.mark.parametrize("transition", ["reset", "onboard", "wake"])
async def test_pending_result_is_discarded_after_exit_or_reset(
    transition, blocked_provider, captured_protos,
):
    import asyncio

    entered, release, finished = blocked_provider
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    end = _request("old-session", 3, audio=b"\x01\x02" * 1600)
    pending = asyncio.create_task(stt_request.handle_stt_request(TEST_DEVICE_ID, end))
    await asyncio.wait_for(entered.wait(), 1)
    if transition == "reset":
        stt_request.reset_conversation_state(TEST_DEVICE_ID)
    else:
        stt_request.note_onboard_module(TEST_DEVICE_ID, wake_gate=transition == "wake")
    assert await pending is None
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, end)
    assert captured_protos == []
    assert not stt_request._sessions


async def test_two_devices_same_uuid_have_separate_audio(fake_openai, captured_protos):
    import io
    import wave

    for device, audio in [(TEST_DEVICE_ID, b"\x01\x02"), ("other", b"\x03\x04")]:
        stt_request.note_conversation_active(device)
        await stt_request.handle_stt_request(device, _request("same", 1, audio=audio * 1600))
    for device in ["other", TEST_DEVICE_ID]:
        await stt_request.handle_stt_request(device, _request("same", 3))
    assert len(captured_protos) == 2
    for call, expected in zip(fake_openai.audio.transcriptions.calls, [b"\x03\x04", b"\x01\x02"]):
        with wave.open(io.BytesIO(call["file"][1]), "rb") as reader:
            assert reader.readframes(1600) == expected * 1600


async def test_oversized_and_abandoned_audio_are_bounded(fake_openai, captured_protos, monkeypatch):
    import asyncio

    stt_request.note_conversation_active(TEST_DEVICE_ID)
    monkeypatch.setattr(stt_request, "MAX_AUDIO_BYTES", 4)
    monkeypatch.setattr(stt_request, "SESSION_MAX_AGE_S", 0.02)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("oversize", 1, audio=b"\x01\x02" * 3))
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("oversize", 3))
    assert len(captured_protos) == 1
    assert captured_protos[-1][1].error_code == 66
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("abandoned", 1, audio=b"\x01\x02"))
    await asyncio.sleep(0.05)
    assert len(captured_protos) == 2
    assert "before END_OF_SPEECH" in captured_protos[-1][1].error_message
    assert not stt_request._sessions
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("abandoned", 3))
    assert len(captured_protos) == 2
    assert fake_openai.audio.transcriptions.calls == []


async def test_pending_and_completed_maps_are_bounded(captured_protos, monkeypatch):
    monkeypatch.setattr(stt_request, "MAX_DEVICE_SESSIONS", 1)
    monkeypatch.setattr(stt_request, "MAX_PENDING_SESSIONS", 2)
    monkeypatch.setattr(stt_request, "MAX_COMPLETED_SESSIONS", 3)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("first", 1))
    for i in range(5):
        await stt_request.handle_stt_request(TEST_DEVICE_ID, _request(f"overflow-{i}", 1))
    await stt_request.handle_stt_request("other", _request("second", 1))
    await stt_request.handle_stt_request("third", _request("global-overflow", 1))
    assert len(stt_request._sessions) == 2
    assert len(stt_request._completed) == 3
    assert all(message.error_code == 66 for _, message in captured_protos)


async def test_malformed_frames_are_ignored(captured_protos):
    assert await stt_request.handle_payload(TEST_DEVICE_ID, stt_request.STT_REQUEST_PREFIX + b"\xff") is None
    for req in [_request("", 3), _request("unknown-vad", 0)]:
        assert await stt_request.handle_stt_request(TEST_DEVICE_ID, req) is None
    assert captured_protos == []


async def test_broker_disconnect_invalidates_pending_recognition(blocked_provider, captured_protos, monkeypatch):
    import asyncio
    from tests.test_mqtt import _FakeMsg

    device = "d_01234567-0123-0123-0123-0123456789ab"
    entered, release, finished = blocked_provider
    monkeypatch.setattr(mqtt_service, "_loop", asyncio.get_running_loop())
    stt_request.note_conversation_active(device)
    pending = asyncio.create_task(stt_request.handle_stt_request(device, _request("broker", 3, audio=b"\x01\x02" * 1600)))
    await asyncio.wait_for(entered.wait(), 1)
    mqtt_service._on_broker_log(_FakeMsg("$SYS/broker/log/N", f"Client {device} disconnected.".encode()))
    await asyncio.wait_for(pending, 1)
    release.set()
    await asyncio.wait_for(finished.wait(), 1)
    assert captured_protos == []
    assert device not in stt_request._conversation_phase


async def test_mqtt_thread_dispatches_audio_frames_in_order(fake_openai, captured_publish, monkeypatch):
    import asyncio
    import io
    import wave
    from tests.test_mqtt import _FakeMsg

    finished = asyncio.Event()

    async def handler(device, payload):
        result = await stt_request.handle_payload(device, payload)
        if result is not None:
            finished.set()

    monkeypatch.setattr(mqtt_service, "_loop", asyncio.get_running_loop())
    monkeypatch.setattr(mqtt_service, "_stt_request_handler", handler)
    monkeypatch.setattr(mqtt_service, "_stt_request_prefix", stt_request.STT_REQUEST_PREFIX)
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    chunks = [b"\x01\x02" * 1600, b"\x03\x04" * 1600, b"\x05\x06" * 1600]

    def network_thread():
        for vad, audio in zip([1, 2, 3], chunks):
            mqtt_service._on_message(None, None, _FakeMsg(
                f"/devices/{TEST_DEVICE_ID}/events/zmq",
                _frame(_request("mqtt-live", vad, audio=audio)),
            ))

    await asyncio.to_thread(network_thread)
    await asyncio.wait_for(finished.wait(), 1)
    assert len(captured_publish) == 1
    topic, wire = captured_publish[0]
    prefix = zmqSTTResponse.DESCRIPTOR.full_name.encode() + b":"
    assert topic == f"/devices/{TEST_DEVICE_ID}/commands/zmq"
    assert wire.startswith(prefix)
    result = zmqSTTResponse.FromString(wire[len(prefix):])
    assert result.uuid == "mqtt-live"
    assert result.speech == fake_openai.audio.transcriptions._text
    with wave.open(io.BytesIO(fake_openai.audio.transcriptions.calls[0]["file"][1]), "rb") as reader:
        assert reader.readframes(reader.getnframes()) == b"".join(chunks)
