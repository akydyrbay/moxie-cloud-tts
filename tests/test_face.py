"""Face persistence, live wire payloads and bootstrap/identity boundaries."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import HARDCODED_DEVICE_CONFIG, settings
from app.router.moxie import router
from app.services.devices import face
from app.services.devices.registry import device_registry
from app.services.transport.mqtt import mqtt_service
from app.services.conversation.remote_chat import RemoteChatBrain
from tests.conftest import TEST_DEVICE_ID

CONFIG_TOPIC = settings.CONFIG_TOPIC_TEMPLATE.format(device=TEST_DEVICE_ID)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
def live_device(known_device) -> str:
    device_registry.mark_bootstrapped(known_device)
    return known_device


def _configs(captured_publish) -> list[dict]:
    return [json.loads(payload) for topic, payload in captured_publish if topic == CONFIG_TOPIC]


async def _bootstrap_config(captured_publish) -> dict:
    captured_publish.clear()
    device_registry.forget_bootstrap(TEST_DEVICE_ID)  # as on a broker reconnect
    await RemoteChatBrain().ensure_device_bootstrapped(TEST_DEVICE_ID)
    (config,) = _configs(captured_publish)
    return config


def test_post_saves_and_publishes_only_face_options(client, live_device, captured_publish, face_file) -> None:
    resp = client.post("/moxie/face", json={"face_options": ["MX_120_Glasses_RedCat", "MX_010_Eyes_Purple"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["face_options"] == ["MX_010_Eyes_Purple", "MX_120_Glasses_RedCat"]
    assert body["untested"] == ["MX_120_Glasses_RedCat"]
    assert body["saved"] is True
    assert body["applies"] == "live update requested"
    assert body["live"] == {"status": "published", "devices": [TEST_DEVICE_ID],
                            "failed_devices": [], "deferred_devices": [], "reason": None}
    # No nickname, ID, pairing, mode, settings, wake/sleep or chat traffic.
    assert len(captured_publish) == 1
    assert _configs(captured_publish) == [{"child_pii": {"face_options": body["face_options"]}}]
    assert json.loads(face_file.read_text()) == {
        "face_options": ["MX_010_Eyes_Purple", "MX_120_Glasses_RedCat"], "child_id": None,
    }
    assert client.get("/moxie/face").json()["face_options"] == body["face_options"]


async def test_bootstrap_carries_saved_face_but_toggles_do_not(client, known_device, captured_publish) -> None:
    default = await _bootstrap_config(captured_publish)
    # Never-configured face leaves the bootstrap identity exactly as before.
    assert default["child_pii"] == HARDCODED_DEVICE_CONFIG["child_pii"]

    client.post("/moxie/face", json={"face_options": ["MX_020_Face_Colors_Teal"]})
    config = await _bootstrap_config(captured_publish)
    assert config["child_pii"]["face_options"] == ["MX_020_Face_Colors_Teal"]
    assert config["child_pii"]["nickname"] == "Pat"

    captured_publish.clear()
    client.post("/moxie/cloud-tts", json={"enabled": True})
    (toggle,) = _configs(captured_publish)
    assert "child_pii" not in toggle


def test_save_failure_leaves_previous_face_and_sends_nothing(
    client, live_device, captured_publish, face_file, monkeypatch,
) -> None:
    options = ["MX_010_Eyes_Gold"]
    client.post("/moxie/face", json={"face_options": options})
    captured_publish.clear()

    def cannot_save(*args):
        raise OSError("disk full")

    monkeypatch.setattr(face, "save", cannot_save)
    assert client.delete("/moxie/face").status_code == 500
    assert captured_publish == []
    assert json.loads(face_file.read_text())["face_options"] == options
    assert client.get("/moxie/face").json()["face_options"] == options


@pytest.mark.parametrize("failure", ["rc", "exception"])
def test_failed_publish_keeps_saved_face_and_can_be_retried(
    client, live_device, captured_publish, face_file, monkeypatch, failure,
) -> None:
    options = ["MX_010_Eyes_Gold"]

    def fail_publish(*args):
        if failure == "exception":
            raise OSError("connection lost")
        return type("Info", (), {"rc": 4})()

    with monkeypatch.context() as patch:
        patch.setattr(mqtt_service.client, "publish", fail_publish)
        response = client.post("/moxie/face", json={"face_options": options})
    assert response.status_code == 200  # Persistence succeeded; delivery is separate.
    body = response.json()
    assert body["saved"] is True
    assert body["live"]["status"] == "failed"
    assert body["live"]["devices"] == []
    assert body["live"]["failed_devices"] == [TEST_DEVICE_ID]
    assert json.loads(face_file.read_text())["face_options"] == options
    assert client.get("/moxie/face").json()["face_options"] == options
    retry = client.post("/moxie/face", json={"face_options": options}).json()
    assert retry["live"]["status"] == "published"
    assert _configs(captured_publish) == [{"child_pii": {"face_options": options}}]
