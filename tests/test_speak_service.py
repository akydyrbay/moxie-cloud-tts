"""TTS stage + publish: text -> OpenAI TTS -> framed CloudTTSResponse -> MQTT."""

import pytest

from src.config import settings
from src.protos.embodied.unity.CloudTTS_pb2 import (
    ROBOT_TTS_REQUEST,
    CloudTTSResponse,
)
from src.services import speak_service
from src.services.speak_service import NoTargetDeviceError
from src.services.synthesis.base import UnsupportedLanguageError
from tests.conftest import TEST_DEVICE_ID


def _decode_published(payload: bytes) -> CloudTTSResponse:
    prefix = b"embodied.unity.CloudTTSResponse:"
    assert payload.startswith(prefix), payload[:40]
    resp = CloudTTSResponse()
    resp.ParseFromString(payload[len(prefix):])
    return resp


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Привет! Как дела?", "ru"),
        ("Сәлем! Қалайсың?", "kk"),
    ],
)
async def test_speak_publishes_framed_response(
    fake_openai, known_device, captured_publish, text, language,
) -> None:
    summary = await speak_service.speak(text, language)

    assert summary["status"] == "published"
    assert summary["language"] == language
    assert summary["audio_bytes"] > 0

    # Exactly one frame published, on the confirmed command topic, addressed to
    # the discovered robot rather than a hardcoded id.
    assert len(captured_publish) == 1
    topic, payload = captured_publish[0]
    assert topic == f"/devices/{TEST_DEVICE_ID}/commands/zmq"

    resp = _decode_published(payload)
    assert resp.request_source == ROBOT_TTS_REQUEST
    assert resp.event_id == summary["event_id"]
    assert resp.chunk_num == 0
    assert resp.timestamp > 0
    assert resp.audio.sample_rate == settings.AUDIO_SAMPLE_RATE_HZ
    assert resp.audio.channels == settings.AUDIO_CHANNELS
    assert len(resp.audio.buffer) > 0
    assert len(resp.marks) == 0  # no visemes in this milestone
    assert resp.software_version == settings.SOFTWARE_VERSION
    assert resp.module_name == settings.MODULE_NAME


async def test_speak_echoes_event_and_chunk(
    fake_openai, known_device, captured_publish,
) -> None:
    summary = await speak_service.speak(
        "Привет", "ru", event_id="evt-123", chunk_num=7,
    )
    assert summary["event_id"] == "evt-123"
    assert summary["chunk_num"] == 7
    resp = _decode_published(captured_publish[0][1])
    assert resp.event_id == "evt-123"
    assert resp.chunk_num == 7


async def test_speak_targets_explicit_device(
    fake_openai, known_device, captured_publish,
) -> None:
    await speak_service.speak("Привет", "ru", device_id="robot-b")
    assert captured_publish[0][0] == "/devices/robot-b/commands/zmq"


async def test_speak_without_device_raises(fake_openai, captured_publish) -> None:
    with pytest.raises(NoTargetDeviceError):
        await speak_service.speak("Привет", "ru")
    assert captured_publish == []


async def test_speak_rejects_unsupported_language(
    fake_openai, known_device, captured_publish,
) -> None:
    with pytest.raises(UnsupportedLanguageError):
        await speak_service.speak("hello", "en")
    # A caller error must not emit a silent fallback frame.
    assert captured_publish == []


async def test_default_audio_is_wav(fake_openai, known_device, captured_publish) -> None:
    assert settings.CLOUD_TTS_AUDIO_RAW_PCM is False
    summary = await speak_service.speak("Привет", "ru")
    assert summary["raw_pcm"] is False
    resp = _decode_published(captured_publish[0][1])
    assert resp.audio.buffer.startswith(b"RIFF")
    assert resp.audio.sample_rate == settings.AUDIO_SAMPLE_RATE_HZ


async def test_speak_raw_pcm_toggle(fake_openai, known_device, captured_publish) -> None:
    prev = settings.CLOUD_TTS_AUDIO_RAW_PCM
    settings.CLOUD_TTS_AUDIO_RAW_PCM = True
    try:
        summary = await speak_service.speak("Привет", "ru")
        assert summary["raw_pcm"] is True
        resp = _decode_published(captured_publish[0][1])
        # PCM path returns raw samples (no RIFF header).
        assert not resp.audio.buffer.startswith(b"RIFF")
    finally:
        settings.CLOUD_TTS_AUDIO_RAW_PCM = prev


async def test_publish_silence_is_valid_playable_wav(
    known_device, captured_publish,
) -> None:
    summary = speak_service.publish_silence(TEST_DEVICE_ID, "evt-x", 2)
    assert summary["status"] == "published_silence"
    resp = _decode_published(captured_publish[0][1])
    assert resp.event_id == "evt-x"
    assert resp.chunk_num == 2
    assert resp.audio.buffer.startswith(b"RIFF")
    assert resp.audio.sample_rate == settings.AUDIO_SAMPLE_RATE_HZ
