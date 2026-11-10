"""HTTP endpoints (Swagger surface), driven with FastAPI TestClient.

Uses a minimal app that mounts only the router, so we don't trigger the app
lifespan (which would try to reach a real MQTT broker).
"""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.router.moxie import router
from app.services.devices.state import device_state
from app.services.devices.registry import device_registry

DEVICE = "test-device-1"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_speak_queues_live_output(client, known_device, captured_publish) -> None:
    device_state._config["settings"]["props"]["cloud_tts"] = "1"
    device_registry.mark_bootstrapped(DEVICE)
    resp = client.post("/moxie/speak", json={"text": "Привет, как дела?"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["device_id"] == DEVICE
    assert captured_publish == []
    assert client.get(f'/moxie/operations/{body["id"]}').json()["status"] == "queued"


def test_speak_without_a_robot_is_a_clear_error(client, captured_publish) -> None:
    resp = client.post("/moxie/speak", json={"text": "Привет"})
    assert resp.status_code == 409
    assert "no robot" in resp.json()["detail"]
    assert captured_publish == []


def test_cloud_tts_toggle_pushes_config(client, known_device, captured_publish) -> None:
    resp = client.post("/moxie/cloud-tts", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["settings"]["props"]["cloud_tts"] == "1"

    topic, payload = captured_publish[0]
    assert topic == f"/devices/{DEVICE}/config"
    assert json.loads(payload)["settings"]["props"]["cloud_tts"] == "1"

    client.post("/moxie/cloud-tts", json={"enabled": False})
    assert json.loads(captured_publish[-1][1])["settings"]["props"]["cloud_tts"] == "0"


def test_puppet_toggle_pushes_config(client, known_device, captured_publish) -> None:
    resp = client.post("/moxie/puppet", json={"enabled": True})
    assert resp.status_code == 200
    assert resp.json()["enabled"] is True
    assert resp.json()["config"]["moxie_mode"] == "TELEHEALTH"

    topic, payload = captured_publish[0]
    assert topic == f"/devices/{DEVICE}/config"
    assert json.loads(payload)["moxie_mode"] == "TELEHEALTH"

    # Idempotent: second enable must not re-push (mode flips crash the robot).
    before = len(captured_publish)
    again = client.post("/moxie/puppet", json={"enabled": True})
    assert again.json().get("unchanged") is True
    assert len(captured_publish) == before

    client.post("/moxie/puppet", json={"enabled": False})
    assert "moxie_mode" not in json.loads(captured_publish[-1][1])


def test_endpoint_qr_returns_a_png_for_the_configured_lan_host(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MQTT_PUBLIC_HOST", "192.168.8.71")
    response = client.get("/moxie/endpoint-qr")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(response.content) > 100
    assert client.get("/moxie/endpoint-qr/info").json() == {
        "mqtt_host": "192.168.8.71",
        "mqtt_port": 8883,
        "disable_verify": True,
        "project": "openmoxie",
    }


def test_wifi_qr_returns_png_without_echoing_credentials(client) -> None:
    response = client.post("/moxie/wifi-qr", json={
        "ssid": "Moxie-network",
        "password": "not-logged-here",
        "hidden": True,
        "band": "2.4ghz",
    })
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert b"not-logged-here" not in response.content
