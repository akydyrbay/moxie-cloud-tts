"""TTS stage + publish: text -> OpenAI TTS -> framed CloudTTSResponse -> MQTT."""

import pytest

from app.config import settings
from app.protos.embodied.unity.CloudTTS_pb2 import (
    ROBOT_TTS_REQUEST,
    CloudTTSResponse,
)
from app.services.speech import output as speak_service
from app.services.speech.audio import duration_ms
from app.services.speech.marks import VISEME_SILENCE
from app.services.speech.synthesis.base import SynthesisResult
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
    assert resp.audio.buffer == FAKE_PCM
    assert summary["raw_pcm"] is True
    assert [call["response_format"] for call in fake_openai.audio.speech.calls] == ["pcm"]
    # Moxie's mouth is driven by these; an empty list speaks stone-faced.
    assert len(resp.marks) > 0
    assert summary["marks"] == len(resp.marks)
    assert resp.software_version == settings.SOFTWARE_VERSION
    assert resp.module_name == settings.MODULE_NAME


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
    summary = await speak_service.speak("Привет", "ru")
    buffer = _decode_published(captured_publish[0][1]).audio.buffer
    assert_sizes_truthful(buffer)
    assert summary["audio_bytes"] == len(buffer)


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
