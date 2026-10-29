"""HTTP endpoints (Swagger surface), driven with FastAPI TestClient.

Uses a minimal app that mounts only the router, so we don't trigger the app
lifespan (which would try to reach a real MQTT broker).
"""

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import settings
from src.protos.embodied.logging.Cloud2_pb2 import ServiceConfiguration2
from src.protos.embodied.wifiapp.QRCommands_pb2 import StartPairingQR
from src.router.tts import router
from src.services.pairing_qr import EndpointQrConfig, endpoint_qr_payload, wifi_qr_payload
from src.services.device_state import device_state
from src.services.devices import device_registry

DEVICE = "test-device-1"


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_speak_queues_live_output(client, known_device, captured_publish) -> None:
    device_state._config["settings"]["props"]["cloud_tts"] = "1"
    device_registry.mark_bootstrapped(DEVICE)
    resp = client.post("/tts/speak", json={"text": "Привет, как дела?"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "queued"
    assert body["device_id"] == DEVICE
    assert captured_publish == []
    assert client.get(f'/tts/operations/{body["id"]}').json()["status"] == "queued"


def test_second_speak_is_a_separate_queued_operation(client, known_device, captured_publish) -> None:
    device_state._config["settings"]["props"]["cloud_tts"] = "1"
    device_registry.mark_bootstrapped(DEVICE)
    first = client.post("/tts/speak", json={"text": "один"}).json()
    second = client.post("/tts/speak", json={"text": "два"}).json()
    assert first["id"] != second["id"]
    assert first["status"] == second["status"] == "queued"
    assert captured_publish == []


def test_speak_without_a_robot_is_a_clear_error(client, captured_publish) -> None:
    resp = client.post("/tts/speak", json={"text": "Привет"})
    assert resp.status_code == 409
    assert "no robot" in resp.json()["detail"]
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


def test_endpoint_qr_payload_matches_openmoxie_service_configuration() -> None:
    payload = endpoint_qr_payload(
        EndpointQrConfig("192.168.8.71", 8883, True, "openmoxie"),
    )
    encoded = json.loads(payload)["debug"]
    assert encoded["command"] == "om"
    service = ServiceConfiguration2()
    service.ParseFromString(base64.b64decode(encoded["param"]))
    assert service.gcp_project == "openmoxie"
    assert service.mqtt_host == "192.168.8.71"
    assert service.override_port == 8883
    assert service.disable_verify is True


def test_wifi_qr_payload_matches_openmoxie_pairing_format() -> None:
    payload = wifi_qr_payload(
        ssid="Moxie-network",
        password="not-logged-here",
        hidden=True,
        band=StartPairingQR.WifiBandSelect.ONLY_24G,
    )
    assert payload.startswith("PA")
    creds = StartPairingQR()
    creds.ParseFromString(base64.b64decode(payload[2:]))
    assert creds.wifi_only is True
    assert creds.ssid == "Moxie-network"
    assert creds.password == "not-logged-here"
    assert creds.is_hidden is True
    assert creds.band_select == StartPairingQR.WifiBandSelect.ONLY_24G


def test_endpoint_qr_returns_a_png_for_the_configured_lan_host(
    client, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "MQTT_PUBLIC_HOST", "192.168.8.71")
    response = client.get("/tts/endpoint-qr")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert response.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(response.content) > 100
    assert client.get("/tts/endpoint-qr/info").json() == {
        "mqtt_host": "192.168.8.71",
        "mqtt_port": 8883,
        "disable_verify": True,
        "project": "openmoxie",
    }


def test_wifi_qr_returns_png_without_echoing_credentials(client) -> None:
    response = client.post("/tts/wifi-qr", json={
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


def test_wifi_qr_rejects_an_unknown_band(client) -> None:
    response = client.post("/tts/wifi-qr", json={
        "ssid": "network", "password": "password", "band": "6ghz",
    })
    assert response.status_code == 422


def test_wakeup_publishes_mqtt_command(client, known_device, captured_publish) -> None:
    resp = client.post("/tts/wakeup")
    assert resp.status_code == 200
    assert resp.json() == {"status": "wakeup_sent", "device_id": DEVICE}
    topic, payload = captured_publish[0]
    assert topic == f"/devices/{DEVICE}/commands/wakeup"
    assert json.loads(payload) == {"command": "wakeup"}


def test_sleep_publishes_mqtt_and_queues_chat_action(
    client, known_device, captured_publish,
) -> None:
    resp = client.post("/tts/sleep")
    assert resp.status_code == 200
    assert resp.json() == {"status": "sleep_queued", "device_id": DEVICE}
    commands = {topic.rsplit("/", 1)[-1]: json.loads(payload)
                for topic, payload in captured_publish}
    # The remote-chat action waits for the next live turn.
    assert commands == {"sleep": {"command": "sleep"}}


def test_sleep_without_a_robot_is_a_clear_error(client, captured_publish) -> None:
    resp = client.post("/tts/sleep")
    assert resp.status_code == 409
    assert "no robot" in resp.json()["detail"]
    assert captured_publish == []
