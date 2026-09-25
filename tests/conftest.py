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
from src.services.telehealth import clear_puppet_state

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


def make_streaming_wav(pcm: bytes, sample_rate: int = 24000) -> bytes:
    """A WAV shaped exactly like OpenAI's streamed `response_format="wav"`.

    Both the RIFF and data chunk sizes carry the 0xFFFFFFFF "length unknown"
    placeholder — the header that silences Moxie for the rest of a session.
    """
    header = (
        b"RIFF" + b"\xff\xff\xff\xff" + b"WAVEfmt "
        + (16).to_bytes(4, "little")
        + (1).to_bytes(2, "little")          # PCM
        + (1).to_bytes(2, "little")          # mono
        + sample_rate.to_bytes(4, "little")
        + (sample_rate * 2).to_bytes(4, "little")
        + (2).to_bytes(2, "little")          # block align
        + (16).to_bytes(2, "little")         # bits per sample
        + b"data" + b"\xff\xff\xff\xff"
    )
    return header + pcm


# 1200 frames of non-silent PCM16, i.e. what `response_format="pcm"` returns.
FAKE_PCM = b"\x00\x01" * 1200


class _FakeSpeech:
    """Stands in for `client.audio.speech`, honouring `response_format`."""

    def __init__(self, pcm: bytes) -> None:
        self._pcm = pcm
        self.calls: list[dict] = []

    def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        fmt = kwargs.get("response_format", "wav")
        # Mirror the real API: "wav" streams a placeholder-size RIFF header.
        content = self._pcm if fmt == "pcm" else make_streaming_wav(self._pcm)
        return type("Resp", (), {"content": content})()


class _FakeTranscriptions:
    def __init__(self, text: str) -> None:
        self._text = text

    def create(self, **kwargs):  # noqa: ANN003, ANN201
        return type("Tr", (), {"text": self._text})()


class _FakeCompletions:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    def create(self, **kwargs):  # noqa: ANN003, ANN201
        self.calls.append(kwargs)
        message = type("M", (), {"content": self._reply})()
        choice = type("C", (), {"message": message})()
        return type("Comp", (), {"choices": [choice]})()


class FakeOpenAI:
    """Minimal stand-in for the OpenAI client used across the pipeline."""

    def __init__(self, pcm: bytes, transcript: str, reply: str) -> None:
        self.audio = type("Audio", (), {})()
        self.audio.speech = _FakeSpeech(pcm)
        self.audio.transcriptions = _FakeTranscriptions(transcript)
        self.chat = type("Chat", (), {})()
        self.chat.completions = _FakeCompletions(reply)


@pytest.fixture
def fake_openai() -> FakeOpenAI:
    """Install a fake OpenAI client and a test API key; restore afterwards."""
    client = FakeOpenAI(
        pcm=FAKE_PCM,
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
    """Reset shared config / telehealth state so they don't leak across tests."""
    clear_puppet_state()
    # Keep speak tests fast: no real robot to wait on.
    prev_pause = settings.SPEAK_INTERRUPT_PAUSE_S
    prev_settle = settings.SPEAK_SETTLE_S
    prev_ready = settings.SPEAK_READY_TIMEOUT_S
    settings.SPEAK_INTERRUPT_PAUSE_S = 0.0
    settings.SPEAK_SETTLE_S = 0.0
    settings.SPEAK_READY_TIMEOUT_S = 0.05
    yield
    settings.SPEAK_INTERRUPT_PAUSE_S = prev_pause
    settings.SPEAK_SETTLE_S = prev_settle
    settings.SPEAK_READY_TIMEOUT_S = prev_ready
    clear_puppet_state()
    device_state._config["settings"]["props"]["cloud_tts"] = "0"  # noqa: SLF001
    device_state._config.pop("moxie_mode", None)  # noqa: SLF001
