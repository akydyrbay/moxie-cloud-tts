"""Tests for the remote-chat brain (modules / schedule / conversation)."""

import json

import pytest

from app.config import settings
from app.services.devices.state import device_state
from app.services.transport.mqtt import mqtt_service
from app.services.conversation.remote_chat import RemoteChatBrain, remote_chat_brain
from tests.conftest import TEST_DEVICE_ID


@pytest.fixture
def brain(monkeypatch: pytest.MonkeyPatch) -> RemoteChatBrain:
    """Fresh brain with MQTT publish captured; reset singleton state after."""
    b = RemoteChatBrain()
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    published: list[tuple[str, dict]] = []

    def _publish_command_json(device_id: str, command: str, payload: dict) -> None:
        published.append((
            f"/devices/{device_id}/commands/{command}",
            payload,
        ))

    monkeypatch.setattr(mqtt_service, "publish_command_json", _publish_command_json)
    monkeypatch.setattr(mqtt_service, "publish_settings_update", lambda *a, **k: None)
    monkeypatch.setattr(mqtt_service, "send_proto_subscribe", lambda *a, **k: None)
    b._published = published  # type: ignore[attr-defined]
    return b


async def test_modules_query_nested_form(brain: RemoteChatBrain) -> None:
    device = TEST_DEVICE_ID
    req = {
        "backend": "data",
        "event_id": "evt-modules-1",
        "query": {"query": "modules"},
    }
    await brain.handle_remote_chat_event(device, json.dumps(req))
    assert len(brain._published) == 1  # type: ignore[attr-defined]
    topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert topic.endswith("/commands/remote_chat")
    assert payload["event_id"] == "evt-modules-1"
    assert payload["result"] == 0
    assert payload["query_data"]["version"] == "openmoxie_v1"
    assert payload["query_data"]["modules"][0]["info"]["id"] == "OPENMOXIE_CHAT"


async def test_modules_query_flat_form(brain: RemoteChatBrain) -> None:
    """Flat `{\"query\":\"modules\"}` form seen in captures must also be answered."""
    await brain.handle_remote_chat_event(
        TEST_DEVICE_ID,
        json.dumps({"query": "modules", "event_id": "flat-1"}),
    )
    topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert topic.endswith("/commands/remote_chat")
    assert payload["event_id"] == "flat-1"
    assert "query_data" in payload


async def test_schedule_query(brain: RemoteChatBrain) -> None:
    req = {
        "subtopic": "query",
        "query": "schedule",
        "request_id": "sched-1",
    }
    await brain.handle_activity_log_event(TEST_DEVICE_ID, json.dumps(req))
    topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert topic.endswith("/commands/query_result")
    assert payload["query"] == "schedule"
    assert payload["request_id"] == "sched-1"
    assert payload["schedule"]["provided_schedule"][0]["module_id"] == "OPENMOXIE_CHAT"


async def test_schedule_query_is_empty_in_puppet_mode(brain: RemoteChatBrain) -> None:
    """A chat-launching schedule in TELEHEALTH steals the speech-output slot."""
    await device_state.set_puppet_mode(True)
    await brain.handle_activity_log_event(
        TEST_DEVICE_ID,
        json.dumps({
            "subtopic": "query",
            "query": "schedule",
            "request_id": "sched-puppet",
        }),
    )
    payload = brain._published[0][1]  # type: ignore[attr-defined]
    assert payload["schedule"] == {"provided_schedule": []}


@pytest.mark.parametrize("module_id", ["WAKEUP", "ENROLLCONVO"])
@pytest.mark.parametrize("command", ["start", "continue"])
async def test_entrance_defers_to_native_transition_without_launch(
    brain: RemoteChatBrain, monkeypatch: pytest.MonkeyPatch, module_id, command,
) -> None:
    """Do not outrank WAKEUP's response: its accepted action enters chat."""
    req = {
        "backend": "router",
        "command": command,
        "event_id": "wake-stt-uuid",
        "module_id": module_id,
        "content_id": "",
        "speech": "Moxie, let's go",
    }
    await brain.handle_router_request(TEST_DEVICE_ID, req)
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["event_id"] == "wake-stt-uuid"
    assert payload["response_action"] == {"output_type": "FALLBACK"}
    assert payload["response_actions"] == [{"output_type": "FALLBACK"}]
    # Match OpenMoxie's Volley: priority is output_type, not this legacy flag.
    assert payload["fallback"] is False
    assert payload["output"]["text"] == "I'm sorry. Can you repeat that?"
    assert payload["output"]["markup"] == (
        '<usel variant="2" genre="none">I\'m sorry. Can you repeat that?</usel>'
    )
    assert payload["response_action"].get("action") is None
    assert all("action" not in a for a in payload["response_actions"])


async def test_sleep_tag_keeps_speak_as_primary(
    brain: RemoteChatBrain, fake_openai, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`<sleep>` must not replace the speak block or Cloud TTS is skipped."""
    monkeypatch.setattr(
        "app.services.conversation.llm.openai_chat.respond",
        lambda *_a, **_k: type("R", (), {"text": "Қайырлы түн! <sleep>"})(),
    )
    await brain.handle_router_request(
        TEST_DEVICE_ID,
        {
            "backend": "router",
            "command": "continue",
            "event_id": "e-sleep",
            "module_id": "OPENMOXIE_CHAT",
            "content_id": "default",
            "speech": "Спокойной ночи",
        },
    )
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["output"]["text"] == "Қайырлы түн!"
    assert payload["response_action"].get("action") is None
    assert payload["response_actions"][0].get("action") is None
    assert payload["response_actions"][1]["action"] == "sleep"


assert remote_chat_brain is not None
