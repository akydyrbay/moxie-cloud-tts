"""HTTP endpoints (Swagger surface), driven with FastAPI TestClient.

Uses a minimal app that mounts only the router, so we don't trigger the app
lifespan (which would try to reach a real MQTT broker).
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.router.tts import router
from src.services.telehealth import note_puppet_state
from tests.conftest import make_wav

DEVICE = "test-device-1"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _telehealth_actions(captured_publish: list) -> list[dict]:
    actions = []
    for topic, payload in captured_publish:
        if not topic.endswith("/commands/telehealth"):
            continue
        actions.append(json.loads(payload)["message"])
    return actions


def test_speak_sends_play_output(client, known_device, captured_publish) -> None:
    """OpenMoxie's puppet speak path is PLAY_OUTPUT, not INTERRUPT+PLAY."""
    resp = client.post("/tts/speak", json={"text": "Привет, как дела?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "requested"
    assert body["device_id"] == DEVICE

    actions = _telehealth_actions(captured_publish)
    assert [a["action"] for a in actions] == ["PLAY_OUTPUT"]
    output = actions[0]["output"]
    assert output["text"] == "Привет, как дела?"
    # OpenMoxie always sends markup; text alone is the fallback path.
    assert output["markup"] == "Привет, как дела?"


def test_second_speak_is_also_just_play_output(client, known_device, captured_publish) -> None:
    """The Nth speak must not INTERRUPT — that cancelled the cloud output path."""
    note_puppet_state(DEVICE, "READY")
    client.post("/tts/speak", json={"text": "один"})
    note_puppet_state(DEVICE, "READY")
    client.post("/tts/speak", json={"text": "два"})

    actions = _telehealth_actions(captured_publish)
    assert [a["action"] for a in actions] == ["PLAY_OUTPUT", "PLAY_OUTPUT"]
    assert actions[0]["output"]["text"] == "один"
    assert actions[1]["output"]["text"] == "два"


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


def test_cloud_tts_toggle_pushes_config(client, known_device, captured_publish) -> None:
    resp = client.post("/tts/cloud-tts", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["settings"]["props"]["cloud_tts"] == "1"

    topic, payload = captured_publish[0]
    assert topic == f"/devices/{DEVICE}/config"
    assert json.loads(payload)["settings"]["props"]["cloud_tts"] == "1"

    client.post("/tts/cloud-tts", json={"enabled": False})
    assert json.loads(captured_publish[-1][1])["settings"]["props"]["cloud_tts"] == "0"


def test_puppet_toggle_pushes_config(client, known_device, captured_publish) -> None:
    resp = client.post("/tts/puppet", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert resp.json()["config"]["moxie_mode"] == "TELEHEALTH"

    topic, payload = captured_publish[0]
    assert topic == f"/devices/{DEVICE}/config"
    assert json.loads(payload)["moxie_mode"] == "TELEHEALTH"

    # Idempotent: second enable must not re-push (mode flips crash the robot).
    before = len(captured_publish)
    again = client.post("/tts/puppet", json={"enabled": True})
    assert again.json().get("unchanged") is True
    assert len(captured_publish) == before

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
