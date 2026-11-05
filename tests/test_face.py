"""Face persistence, live wire payloads and bootstrap/identity boundaries."""

import asyncio
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import HARDCODED_DEVICE_CONFIG, settings
from src.router.tts import router
from src.services import face
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.mqtt_service import mqtt_service
from src.services.remote_chat import RemoteChatBrain
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


def test_catalog_matches_openmoxie_labels() -> None:
    labels = [label for layer in face.catalog() for label in layer["labels"]]
    assert len(labels) == len(set(labels)) == 60
    assert all(label.startswith("MX_") for label in labels)
    assert {layer["layer"] for layer in face.catalog() if layer["tested"]} == {"Eyes", "Face_Colors"}


def test_normalize_orders_dedupes_and_rejects_conflicts() -> None:
    assert face.normalize(["MX_120_Glasses_RedCat", "MX_010_Eyes_Purple", "MX_010_Eyes_Purple"]) == [
        "MX_010_Eyes_Purple", "MX_120_Glasses_RedCat",
    ]
    assert face.normalize([]) == []
    with pytest.raises(ValueError, match="unknown"):
        face.normalize(["MX_999_Zebra_Mask"])
    with pytest.raises(ValueError, match="one Eyes"):
        face.normalize(["MX_010_Eyes_Purple", "MX_010_Eyes_Gold"])


def test_post_saves_and_publishes_only_face_options(client, live_device, captured_publish, face_file) -> None:
    resp = client.post("/tts/face", json={"face_options": ["MX_120_Glasses_RedCat", "MX_010_Eyes_Purple"]})
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
    assert client.get("/tts/face").json()["face_options"] == body["face_options"]


@pytest.mark.parametrize("options", [["MX_999_Zebra_Mask"], ["MX_010_Eyes_Purple", "MX_010_Eyes_Gold"]])
def test_invalid_face_changes_nothing(client, live_device, captured_publish, face_file, options) -> None:
    resp = client.post("/tts/face", json={"face_options": options})
    assert resp.status_code == 422
    assert not face_file.exists()
    assert client.get("/tts/face").json()["face_options"] == []
    assert captured_publish == []


async def test_bootstrap_carries_saved_face_but_toggles_do_not(client, known_device, captured_publish) -> None:
    default = await _bootstrap_config(captured_publish)
    # Never-configured face leaves the bootstrap identity exactly as before.
    assert default["child_pii"] == HARDCODED_DEVICE_CONFIG["child_pii"]

    client.post("/tts/face", json={"face_options": ["MX_020_Face_Colors_Teal"]})
    config = await _bootstrap_config(captured_publish)
    assert config["child_pii"]["face_options"] == ["MX_020_Face_Colors_Teal"]
    assert config["child_pii"]["nickname"] == "Pat"

    captured_publish.clear()
    client.post("/tts/cloud-tts", json={"enabled": True})
    (toggle,) = _configs(captured_publish)
    assert "child_pii" not in toggle


async def test_delete_resets_live_and_on_next_bootstrap(client, live_device, captured_publish, face_file) -> None:
    client.post("/tts/face", json={"face_options": ["MX_130_Nose_Dog"]})
    captured_publish.clear()
    resp = client.delete("/tts/face")
    assert resp.status_code == 200
    assert resp.json()["face_options"] == []
    assert json.loads(face_file.read_text())["face_options"] == []
    assert resp.json()["live"]["status"] == "published"
    assert _configs(captured_publish) == [{"child_pii": {"face_options": []}}]
    # An explicit [] clears the robot's previous customization.
    assert (await _bootstrap_config(captured_publish))["child_pii"]["face_options"] == []


async def test_reset_child_id_is_bootstrap_only_and_kept(client, live_device, captured_publish) -> None:
    first = client.post("/tts/face", json={"face_options": [], "reset_child_id": True}).json()
    assert first["child_id_reset"] is True
    assert first["live"]["reason"] == "child_id_reset"
    assert captured_publish == []
    child_id = (await device_state.get_face())["child_id"]
    assert child_id
    client.post("/tts/face", json={"face_options": ["MX_010_Eyes_Gold"]})
    assert _configs(captured_publish) == [{"child_pii": {"face_options": ["MX_010_Eyes_Gold"]}}]
    assert (await device_state.get_face())["child_id"] == child_id
    assert (await _bootstrap_config(captured_publish))["child_pii"]["id"] == child_id


@pytest.mark.parametrize("method", ["post", "delete"])
def test_save_only(client, live_device, captured_publish, face_file, method) -> None:
    if method == "post":
        options = ["MX_010_Eyes_Gold"]
        resp = client.post("/tts/face", json={"face_options": options, "apply_live": False})
    else:
        options = []
        resp = client.delete("/tts/face?apply_live=false")
    assert resp.status_code == 200
    assert resp.json()["live"]["reason"] == "save_only"
    assert resp.json()["applies"] == "next robot connect"
    assert json.loads(face_file.read_text())["face_options"] == options
    assert captured_publish == []


@pytest.mark.parametrize("state", ["unknown", "discovered", "disconnected", "override"])
async def test_no_live_push_before_bootstrap(client, captured_publish, monkeypatch, state) -> None:
    monkeypatch.setattr(settings, "DEVICE_ID", "")
    if state in {"discovered", "disconnected"}:
        device_registry.seen(TEST_DEVICE_ID)
    if state == "disconnected":
        device_registry.mark_bootstrapped(TEST_DEVICE_ID)
        device_registry.forget_bootstrap(TEST_DEVICE_ID)
    if state == "override":
        monkeypatch.setattr(settings, "DEVICE_ID", TEST_DEVICE_ID)
    body = client.post("/tts/face", json={"face_options": ["MX_010_Eyes_Gold"]}).json()
    assert body["live"]["reason"] == "no_bootstrapped_devices"
    assert body["live"]["devices"] == []
    assert body["live"]["deferred_devices"] == ([] if state == "unknown" else [TEST_DEVICE_ID])
    assert captured_publish == []
    assert (await _bootstrap_config(captured_publish))["child_pii"]["face_options"] == body["face_options"]


def test_openmoxie_config_ownership(client, live_device, captured_publish, face_file, monkeypatch) -> None:
    monkeypatch.setattr(settings, "OPENMOXIE_ENABLED", True)
    body = client.post("/tts/face", json={"face_options": ["MX_010_Eyes_Gold"]}).json()
    assert body["live"]["reason"] == "openmoxie_owns_config"
    assert body["applies"] == "OpenMoxie managed"
    assert captured_publish == []
    assert json.loads(face_file.read_text())["face_options"] == body["face_options"]


def test_save_failure_leaves_previous_face_and_sends_nothing(
    client, live_device, captured_publish, face_file, monkeypatch,
) -> None:
    options = ["MX_010_Eyes_Gold"]
    client.post("/tts/face", json={"face_options": options})
    captured_publish.clear()

    def cannot_save(*args):
        raise OSError("disk full")

    monkeypatch.setattr(face, "save", cannot_save)
    assert client.delete("/tts/face").status_code == 500
    assert captured_publish == []
    assert json.loads(face_file.read_text())["face_options"] == options
    assert client.get("/tts/face").json()["face_options"] == options


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
        response = client.post("/tts/face", json={"face_options": options})
    assert response.status_code == 200  # Persistence succeeded; delivery is separate.
    body = response.json()
    assert body["saved"] is True
    assert body["live"]["status"] == "failed"
    assert body["live"]["devices"] == []
    assert body["live"]["failed_devices"] == [TEST_DEVICE_ID]
    assert json.loads(face_file.read_text())["face_options"] == options
    assert client.get("/tts/face").json()["face_options"] == options
    retry = client.post("/tts/face", json={"face_options": options}).json()
    assert retry["live"]["status"] == "published"
    assert _configs(captured_publish) == [{"child_pii": {"face_options": options}}]


def test_multiple_devices_report_partial_delivery(client, live_device, monkeypatch) -> None:
    for device in ("failed-robot", "offline-robot"):
        device_registry.seen(device)
    device_registry.mark_bootstrapped("failed-robot")
    attempted = []

    def publish(device, config):
        attempted.append(device)
        if device == "failed-robot":
            raise OSError("publish failed")
        return True

    monkeypatch.setattr(mqtt_service, "publish_settings_update", publish)
    body = client.post("/tts/face", json={"face_options": []}).json()
    assert body["live"] == {
        "status": "partial", "devices": [TEST_DEVICE_ID], "failed_devices": ["failed-robot"],
        "deferred_devices": ["offline-robot"], "reason": None,
    }
    assert set(attempted) == {TEST_DEVICE_ID, "failed-robot"}


@pytest.mark.parametrize("puppet", [False, True])
async def test_face_preserves_speech_mode_and_active_conversation(
    client, live_device, captured_publish, puppet,
) -> None:
    from src.services import stt_request
    from src.services.live_actions import live_actions

    await device_state.set_cloud_tts(True)
    await device_state.set_puppet_mode(puppet)
    revision = device_state.speech_revision
    config = await device_state.get_config()
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    # An active operation must survive a face refresh, including DELETE.
    operation = live_actions.enqueue(TEST_DEVICE_ID, "Привет!", "ru")
    for method in ("post", "delete"):
        if method == "post":
            response = client.post("/tts/face", json={"face_options": ["MX_010_Eyes_Gold"]})
        else:
            response = client.delete("/tts/face")
        assert response.json()["live"]["status"] == "published"
        current = await device_state.get_config()
        current["child_pii"].pop("face_options")
        assert current == config
        assert device_state.speech_revision == revision
        assert stt_request._conversation_phase[TEST_DEVICE_ID] == "chat"
        assert live_actions.get(operation["id"])["status"] == "queued"
        assert len(live_actions.active(TEST_DEVICE_ID)) == 1
    assert len(captured_publish) == 2


async def test_concurrent_updates_preserve_save_and_publish_order(
    live_device, captured_publish, face_file, monkeypatch,
) -> None:
    from src.router import tts

    # Suspend request one after its save. Request two must not save or publish
    # until request one's in-memory update and publish have both completed.
    monkeypatch.setattr(tts, "_face_lock", asyncio.Lock())
    entered = asyncio.Event()
    release = asyncio.Event()
    original_set_face = device_state.set_face
    first_options = ["MX_010_Eyes_Gold"]
    second_options = ["MX_010_Eyes_Purple"]

    async def delayed_set_face(options, child_id):
        if options == first_options:
            entered.set()
            await release.wait()
        await original_set_face(options, child_id)

    monkeypatch.setattr(device_state, "set_face", delayed_set_face)
    first = asyncio.create_task(tts.set_face(tts.FaceRequest(face_options=first_options)))
    second = None
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        second = asyncio.create_task(tts.set_face(tts.FaceRequest(face_options=second_options)))
        await asyncio.sleep(0)  # Let request two contend for the save/publish lock.
        assert json.loads(face_file.read_text())["face_options"] == first_options
        assert captured_publish == []
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=1)
    finally:
        release.set()
        tasks = [task for task in (first, second) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    assert _configs(captured_publish) == [
        {"child_pii": {"face_options": first_options}},
        {"child_pii": {"face_options": second_options}},
    ]
    assert json.loads(face_file.read_text())["face_options"] == second_options
    assert (await device_state.get_face())["face_options"] == second_options


def test_load_round_trip_and_bad_files(face_file) -> None:
    assert face.load() is None
    face.save(["MX_010_Eyes_Hazel"], "abc")
    assert face.load() == {"face_options": ["MX_010_Eyes_Hazel"], "child_id": "abc"}
    for bad in ("not json", '{"face_options": ["MX_999_Nope"]}', '{"face_options": "x"}',
                '{"face_options": [], "child_id": 5}', "[]"):
        face_file.write_text(bad)
        assert face.load() is None
