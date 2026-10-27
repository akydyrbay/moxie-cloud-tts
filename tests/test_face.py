"""Face customization is saved, validated and only reaches the robot at bootstrap."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import HARDCODED_DEVICE_CONFIG, settings
from src.router.tts import router
from src.services import face
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.remote_chat import RemoteChatBrain
from tests.conftest import TEST_DEVICE_ID

CONFIG_TOPIC = settings.CONFIG_TOPIC_TEMPLATE.format(device=TEST_DEVICE_ID)


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


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


def test_post_saves_without_publishing_mid_session(client, known_device, captured_publish, face_file) -> None:
    resp = client.post("/tts/face", json={"face_options": ["MX_120_Glasses_RedCat", "MX_010_Eyes_Purple"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["face_options"] == ["MX_010_Eyes_Purple", "MX_120_Glasses_RedCat"]
    assert body["untested"] == ["MX_120_Glasses_RedCat"]
    assert body["applies"] == "next robot connect"
    # child_pii must never be pushed to a live robot (Bug A).
    assert captured_publish == []
    assert json.loads(face_file.read_text()) == {
        "face_options": ["MX_010_Eyes_Purple", "MX_120_Glasses_RedCat"], "child_id": None,
    }
    assert client.get("/tts/face").json()["face_options"] == body["face_options"]


@pytest.mark.parametrize("options", [["MX_999_Zebra_Mask"], ["MX_010_Eyes_Purple", "MX_010_Eyes_Gold"]])
def test_invalid_face_changes_nothing(client, face_file, options) -> None:
    resp = client.post("/tts/face", json={"face_options": options})
    assert resp.status_code == 422
    assert not face_file.exists()
    assert client.get("/tts/face").json()["face_options"] == []


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


async def test_delete_sends_default_face_on_next_bootstrap(client, known_device, captured_publish, face_file) -> None:
    client.post("/tts/face", json={"face_options": ["MX_130_Nose_Dog"]})
    resp = client.delete("/tts/face")
    assert resp.status_code == 200
    assert resp.json()["face_options"] == []
    assert json.loads(face_file.read_text())["face_options"] == []
    # An explicit [] clears the robot's previous customization.
    assert (await _bootstrap_config(captured_publish))["child_pii"]["face_options"] == []


async def test_reset_child_id_is_kept_until_reset_again(client, known_device, captured_publish) -> None:
    first = client.post("/tts/face", json={"face_options": [], "reset_child_id": True}).json()
    assert first["child_id_reset"] is True
    child_id = (await device_state.get_face())["child_id"]
    assert child_id
    client.post("/tts/face", json={"face_options": ["MX_010_Eyes_Gold"]})
    assert (await device_state.get_face())["child_id"] == child_id
    assert (await _bootstrap_config(captured_publish))["child_pii"]["id"] == child_id


def test_load_round_trip_and_bad_files(face_file) -> None:
    assert face.load() is None
    face.save(["MX_010_Eyes_Hazel"], "abc")
    assert face.load() == {"face_options": ["MX_010_Eyes_Hazel"], "child_id": "abc"}
    for bad in ("not json", '{"face_options": ["MX_999_Nope"]}', '{"face_options": "x"}',
                '{"face_options": [], "child_id": 5}', "[]"):
        face_file.write_text(bad)
        assert face.load() is None
