"""TTS stage + publish: text -> OpenAI TTS -> framed CloudTTSResponse -> MQTT."""

import pytest

from src.config import settings
from src.protos.embodied.unity.CloudTTS_pb2 import (
    ROBOT_TTS_REQUEST,
    CloudTTSResponse,
)
from src.services import speak_service
from src.services.audio import duration_ms
from src.services.marks import VISEME_SILENCE
from src.services.speak_service import NoTargetDeviceError
from src.services.synthesis.base import SynthesisResult, UnsupportedLanguageError
from tests.conftest import FAKE_PCM, TEST_DEVICE_ID, make_streaming_wav
from tests.test_audio import assert_sizes_truthful


RESPONSE_PREFIX = b"embodied.unity.CloudTTSResponse:"


def wav_duration_ms(buffer: bytes) -> int:
    return duration_ms(
        buffer, settings.AUDIO_SAMPLE_RATE_HZ, settings.AUDIO_CHANNELS,
    )


def _decode_published(payload: bytes) -> CloudTTSResponse:
    assert payload.startswith(RESPONSE_PREFIX), payload[:40]
    resp = CloudTTSResponse()
    resp.ParseFromString(payload[len(RESPONSE_PREFIX):])
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
    # Moxie's mouth is driven by these; an empty list speaks stone-faced.
    assert len(resp.marks) > 0
    assert summary["marks"] == len(resp.marks)
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


async def test_default_audio_is_raw_pcm(fake_openai, known_device, captured_publish) -> None:
    assert settings.CLOUD_TTS_AUDIO_RAW_PCM is True
    summary = await speak_service.speak("Привет", "ru")
    assert summary["raw_pcm"] is True
    resp = _decode_published(captured_publish[0][1])
    assert not resp.audio.buffer.startswith(b"RIFF")
    assert resp.audio.buffer == FAKE_PCM
    assert resp.audio.sample_rate == settings.AUDIO_SAMPLE_RATE_HZ


async def test_synthesis_never_asks_openai_for_streaming_wav(fake_openai) -> None:
    """`response_format="wav"` returns a 0xFFFFFFFF-length header; never use it."""
    await speak_service.speak("Привет", "ru", device_id=TEST_DEVICE_ID)
    formats = [call.get("response_format") for call in fake_openai.audio.speech.calls]
    assert formats == ["pcm"]


async def test_malformed_engine_audio_is_repaired_before_publishing(
    fake_openai, known_device, captured_publish, monkeypatch,
) -> None:
    """Belt and braces: any engine handing us a WAV with a bogus size is fixed.

    The default path is raw PCM, so this only fires if an engine still emits a
    container. The robot must never see a 0xFFFFFFFF-length header.
    """
    broken = make_streaming_wav(FAKE_PCM)

    def _broken_synthesize(text: str, language: str, timeout: float | None = None) -> SynthesisResult:
        return SynthesisResult(
            audio=broken,
            sample_rate=settings.AUDIO_SAMPLE_RATE_HZ,
            channels=settings.AUDIO_CHANNELS,
            synthesis_time_ms=1,
        )

    monkeypatch.setattr(
        speak_service._engine, "synthesize", _broken_synthesize,  # noqa: SLF001
    )
    monkeypatch.setattr(settings, "CLOUD_TTS_AUDIO_RAW_PCM", False)
    summary = await speak_service.speak("Привет", "ru")
    buffer = _decode_published(captured_publish[0][1]).audio.buffer
    assert_sizes_truthful(buffer)
    assert summary["audio_bytes"] == len(buffer)


async def test_silence_fallback_is_raw_pcm(
    known_device, captured_publish,
) -> None:
    speak_service.publish_silence(TEST_DEVICE_ID, "evt-y")
    buffer = _decode_published(captured_publish[0][1]).audio.buffer
    assert not buffer.startswith(b"RIFF")
    assert buffer == b"\x00\x00" * (settings.AUDIO_SAMPLE_RATE_HZ * 200 // 1000)


async def test_wav_container_toggle(fake_openai, known_device, captured_publish) -> None:
    prev = settings.CLOUD_TTS_AUDIO_RAW_PCM
    settings.CLOUD_TTS_AUDIO_RAW_PCM = False
    try:
        summary = await speak_service.speak("Привет", "ru")
        assert summary["raw_pcm"] is False
        resp = _decode_published(captured_publish[0][1])
        assert resp.audio.buffer.startswith(b"RIFF")
        assert_sizes_truthful(resp.audio.buffer)
    finally:
        settings.CLOUD_TTS_AUDIO_RAW_PCM = prev


async def test_every_repeated_speak_carries_a_terminated_mark_stream(
    fake_openai, known_device, captured_publish,
) -> None:
    """The bug is "line 1 plays, the rest are silent", so N lines must be alike.

    Telehealth lines all arrive with an empty `event_id`, so the Nth response is
    indistinguishable from the first apart from its marks. Each must span its
    own audio and close with `sil`, or the robot is left mid-utterance.
    """
    for _ in range(3):
        await speak_service.speak(
            "Сәлем! Бүгін көңіл-күйің қалай?", "kk", event_id="", chunk_num=0,
        )

    assert len(captured_publish) == 3
    for _, payload in captured_publish:
        resp = _decode_published(payload)
        length_ms = wav_duration_ms(resp.audio.buffer)
        times = [m.time for m in resp.marks]

        assert times == sorted(times)
        assert max(times) <= length_ms, "a mark past the audio outlives the clip"
        assert resp.marks[-1].value == VISEME_SILENCE
        assert resp.marks[-1].time == length_ms


async def test_silence_fallback_still_closes_its_utterance(
    known_device, captured_publish,
) -> None:
    """Nothing to lip-sync, but the robot still needs to know it ended."""
    speak_service.publish_silence(TEST_DEVICE_ID, "evt-z")
    resp = _decode_published(captured_publish[0][1])
    assert [m.value for m in resp.marks] == [VISEME_SILENCE]
    assert resp.marks[0].time == wav_duration_ms(resp.audio.buffer)


async def test_marks_can_be_disabled(
    fake_openai, known_device, captured_publish, monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "CLOUD_TTS_EMIT_MARKS", False)
    await speak_service.speak("Привет", "ru")
    assert len(_decode_published(captured_publish[0][1]).marks) == 0


async def test_publish_silence_is_raw_pcm(
    known_device, captured_publish,
) -> None:
    summary = speak_service.publish_silence(TEST_DEVICE_ID, "evt-x", 2)
    assert summary["status"] == "published_silence"
    resp = _decode_published(captured_publish[0][1])
    assert resp.event_id == "evt-x"
    assert resp.chunk_num == 2
    assert not resp.audio.buffer.startswith(b"RIFF")
    assert resp.audio.sample_rate == settings.AUDIO_SAMPLE_RATE_HZ
