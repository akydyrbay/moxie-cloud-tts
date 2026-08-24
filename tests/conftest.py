"""Shared test fixtures.

These tests exercise the OpenAI speech pipeline end-to-end WITHOUT a real
OpenAI API key or a physical robot, by:
  * injecting a fake OpenAI client (so STT/chat/TTS return canned bytes/text),
  * capturing what the MQTT service would publish to the robot.

This mirrors how Milestone 1 was verified (mock the provider, capture the
published frame) and lets every stage be checked independently.
"""

import io
import wave

import pytest

from src.config import settings
from src.services import openai_client
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.mqtt_service import mqtt_service

TEST_DEVICE_ID = "test-device-1"


def make_wav(sample_rate: int = 24000, channels: int = 1, frames: int = 2400) -> bytes:
    """Build a tiny valid WAV container of silence for use as fake audio."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"\x00\x00" * frames * channels)
    return buf.getvalue()


class _FakeSpeech:
    def __init__(self, audio: bytes) -> None:
        self._audio = audio

    def create(self, **kwargs):  # noqa: ANN003, ANN201
        fmt = kwargs.get("response_format", "wav")
        content = self._audio if fmt == "wav" else b"\x00\x01" * 1200
        return type("Resp", (), {"content": content})()


class _FakeTranscriptions:
    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, **kwargs):  # noqa: ANN003, ANN201
        return type("Tr", (), {"text": self._text})()


class _FakeCompletions:
    def __init__(self, reply: str) -> None:
        self._reply = reply

    def create(self, **kwargs):  # noqa: ANN003, ANN201
        message = type("M", (), {"content": self._reply})()
        choice = type("C", (), {"message": message})()
        return type("Comp", (), {"choices": [choice]})()


class FakeOpenAI:
    """Minimal stand-in for the OpenAI client used across the pipeline."""

    def __init__(self, audio: bytes, transcript: str, reply: str) -> None:
        self.audio = type("Audio", (), {})()
        self.audio.speech = _FakeSpeech(audio)
        self.audio.transcriptions = _FakeTranscriptions(transcript)
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(reply)


@pytest.fixture
def fake_openai() -> FakeOpenAI:
    """Install a fake OpenAI client and a test API key; restore afterwards."""
    client = FakeOpenAI(
        audio=make_wav(),
        transcript="Привет, меня зовут Мокси",
        reply="Привет! Рад тебя видеть!",
    )
    prev_key = settings.OPENAI_API_KEY
    settings.OPENAI_API_KEY = "test-key"
    openai_client._client = client  # noqa: SLF001 (test injection)
    yield client
    openai_client.reset_client()
    settings.OPENAI_API_KEY = prev_key


@pytest.fixture
def captured_publish(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, bytes]]:
    """Capture everything the MQTT service would publish, without a broker."""
    published: list[tuple[str, bytes]] = []

    def _publish(topic: str, payload: bytes, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        published.append((topic, payload))

    monkeypatch.setattr(mqtt_service.client, "publish", _publish)
    return published


@pytest.fixture(autouse=True)
def clean_device_registry() -> None:
    """Isolate device discovery between tests (the registry is a singleton)."""
    device_registry.reset()
    yield
    device_registry.reset()


@pytest.fixture
def known_device() -> str:
    """Simulate a robot having published on the broker."""
    device_registry.seen(TEST_DEVICE_ID)
    return TEST_DEVICE_ID


@pytest.fixture(autouse=True)
def clean_device_state() -> None:
    """Reset the shared config so cloud_tts/moxie_mode don't leak across tests."""
    yield
    device_state._config["settings"]["props"]["cloud_tts"] = "0"  # noqa: SLF001
    device_state._config.pop("moxie_mode", None)  # noqa: SLF001
