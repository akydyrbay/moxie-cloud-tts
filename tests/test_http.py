"""HTTP endpoints (Swagger surface), driven with FastAPI TestClient.

Uses a minimal app that mounts only the router, so we don't trigger the app
lifespan (which would try to reach a real MQTT broker).
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import settings
from src.router.tts import router
from tests.conftest import make_wav

DEVICE = "test-device-1"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def no_wakeup_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the auto-wakeup behaviour but drop its sleep so tests stay fast."""
    monkeypatch.setattr(settings, "SPEAK_WAKEUP_DELAY_S", 0.0)


def _play_output_messages(captured_publish) -> list[dict]:
    return [
        json.loads(payload)
        for topic, payload in captured_publish
        if topic == f"/devices/{DEVICE}/commands/telehealth"
        and json.loads(payload)["message"].get("action") == "PLAY_OUTPUT"
    ]


def test_speak_sends_telehealth_play_output(
    client, known_device, captured_publish, no_wakeup_delay,
) -> None:
    """/tts/speak must push PLAY_OUTPUT so the robot raises a real CloudTTSRequest."""
    resp = client.post("/tts/speak", json={"text": "Привет, как дела?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "requested"
    assert body["device_id"] == DEVICE

    plays = _play_output_messages(captured_publish)
    assert len(plays) == 1
    output = plays[0]["message"]["output"]
    assert output["text"] == "Привет, как дела?"
    # Sending markup would risk tags being read aloud; text-only is deliberate.
    assert "markup" not in output


def test_speak_wakes_the_robot_before_speaking(
    client, known_device, captured_publish, no_wakeup_delay,
) -> None:
    """The robot dozes after a line, so /tts/speak wakes it (before PLAY_OUTPUT)."""
    resp = client.post("/tts/speak", json={"text": "Привет"})
    assert resp.status_code == 200

    topics = [t for t, _ in captured_publish]
    assert f"/devices/{DEVICE}/commands/wakeup" in topics
    # Wakeup must come first, so the robot is listening when the speech arrives.
    play_topic = f"/devices/{DEVICE}/commands/telehealth"
    assert topics.index(f"/devices/{DEVICE}/commands/wakeup") < topics.index(play_topic)


def test_speak_without_a_robot_is_a_clear_error(client, captured_publish) -> None:
    resp = client.post("/tts/speak", json={"text": "Привет"})
    assert resp.status_code == 409
    assert "no robot" in resp.json()["detail"]
    assert captured_publish == []


def test_speak_direct_publishes_audio(client, fake_openai, known_device, captured_publish) -> None:
    resp = client.post(
        "/tts/speak-direct",
        json={"text": "Сәлем!", "language": "kk", "event_id": "evt-1"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "published"
    assert captured_publish[0][0] == f"/devices/{DEVICE}/commands/zmq"


def test_speak_direct_unsupported_language(client, fake_openai, known_device, captured_publish) -> None:
    resp = client.post("/tts/speak-direct", json={"text": "hi", "language": "en"})
    assert resp.status_code == 422
    assert captured_publish == []


def test_puppet_toggle_pushes_config(client, known_device, captured_publish) -> None:
    resp = client.post("/tts/puppet", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert resp.json()["config"]["moxie_mode"] == "TELEHEALTH"

    topic, payload = captured_publish[0]
    assert topic == f"/devices/{DEVICE}/config"
    assert json.loads(payload)["moxie_mode"] == "TELEHEALTH"

    client.post("/tts/puppet", json={"enabled": False})
    assert "moxie_mode" not in json.loads(captured_publish[-1][1])


def test_devices_endpoint_reports_discovered_robot(client, known_device) -> None:
    body = client.get("/tts/devices").json()
    assert body["devices"] == [DEVICE]
    assert body["primary"] == DEVICE


def test_transcribe_endpoint(client, fake_openai) -> None:
    files = {"file": ("speech.wav", make_wav(), "audio/wav")}
    resp = client.post("/tts/transcribe", files=files, data={"language": "ru"})
    assert resp.status_code == 200
    assert resp.json()["text"] == "Привет, меня зовут Мокси"


def test_respond_endpoint(client, fake_openai) -> None:
    resp = client.post("/tts/respond", json={"text": "Привет", "language": "ru"})
    assert resp.status_code == 200
    assert resp.json()["reply_text"] == "Привет! Рад тебя видеть!"
