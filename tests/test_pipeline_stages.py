"""Independent-stage tests for the OpenAI STT, chat, and TTS stages.

STT is exercised here only as a standalone stage: the robot performs its own
speech recognition today, so nothing feeds these bytes automatically yet.
"""

import pytest

from src.config import settings
from src.services.llm import openai_chat
from src.services.stt import openai_stt
from src.services.synthesis import openai_tts
from src.services.synthesis.base import UnsupportedLanguageError
from tests.conftest import FAKE_PCM


def test_stt_stage(fake_openai) -> None:
    result = openai_stt.transcribe(b"fake-audio-bytes", language="ru")
    assert result.text == "Привет, меня зовут Мокси"
    assert result.language == "ru"
    assert result.stt_time_ms >= 0


def test_stt_rejects_unsupported_language(fake_openai) -> None:
    with pytest.raises(UnsupportedLanguageError):
        openai_stt.transcribe(b"x", language="en")


def test_chat_stage(fake_openai) -> None:
    result = openai_chat.respond("Привет", "ru")
    assert result.text == "Привет! Рад тебя видеть!"
    assert result.language == "ru"


def test_chat_rejects_unsupported_language(fake_openai) -> None:
    with pytest.raises(UnsupportedLanguageError):
        openai_chat.respond("hi", "en")


def test_tts_stage(fake_openai) -> None:
    result = openai_tts.openai_tts_engine.synthesize("Привет", "ru")
    assert len(result.audio) > 0
    assert result.sample_rate == settings.AUDIO_SAMPLE_RATE_HZ
    assert result.channels == settings.AUDIO_CHANNELS


def test_tts_requests_pcm_by_default(fake_openai) -> None:
    """AudioBuffer.buffer is raw PCM; channels+sample_rate describe it."""
    result = openai_tts.openai_tts_engine.synthesize("Сәлем", "kk")
    assert not result.audio.startswith(b"RIFF")
    assert result.audio == FAKE_PCM
