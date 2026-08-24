"""Tests for the remote-chat brain (modules / schedule / conversation)."""

import json

import pytest

from src.services.mqtt_service import mqtt_service
from src.services.remote_chat import RemoteChatBrain, fallback_line, remote_chat_brain
from tests.conftest import TEST_DEVICE_ID


@pytest.fixture
def brain(monkeypatch: pytest.MonkeyPatch) -> RemoteChatBrain:
    """Fresh brain with MQTT publish captured; reset singleton state after."""
    b = RemoteChatBrain()
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


async def test_onboard_module_gets_fallback(brain: RemoteChatBrain) -> None:
    req = {
        "backend": "router",
        "command": "continue",
        "event_id": "e1",
        "module_id": "WELCOME",
        "content_id": "",
        "speech": "hi",
    }
    await brain.handle_router_request(TEST_DEVICE_ID, req)
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["response_action"]["output_type"] == "FALLBACK"
    assert payload["output"]["text"]
    assert payload["event_id"] == "e1"


@pytest.mark.parametrize(
    ("speech", "expected"),
    [
        ("Привет как дела", "ru"),
        ("Сәлем қалайсың", "kk"),
    ],
)
async def test_fallback_is_spoken_in_the_childs_language(
    brain: RemoteChatBrain, speech, expected,
) -> None:
    """A Russian/Kazakh child must never hear an English canned line."""
    req = {
        "backend": "router",
        "command": "continue",
        "event_id": "e-fb",
        "module_id": "WELCOME",
        "content_id": "",
        "speech": speech,
    }
    await brain.handle_router_request(TEST_DEVICE_ID, req)
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["output"]["text"] == fallback_line(expected)
    assert not payload["output"]["text"].isascii()


async def test_llm_failure_falls_back_in_language(
    brain: RemoteChatBrain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(*_a, **_k):  # noqa: ANN202
        msg = "openai down"
        raise RuntimeError(msg)

    monkeypatch.setattr("src.services.llm.openai_chat.respond", _boom)
    req = {
        "backend": "router",
        "command": "continue",
        "event_id": "e-err",
        "module_id": "OPENMOXIE_CHAT",
        "content_id": "default",
        "speech": "Сәлем қалайсың",
    }
    await brain.handle_router_request(TEST_DEVICE_ID, req)
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["output"]["text"] == fallback_line("kk")


async def test_prompt_sends_opener(brain: RemoteChatBrain) -> None:
    req = {
        "backend": "router",
        "command": "prompt",
        "event_id": "e-prompt",
        "module_id": "OPENMOXIE_CHAT",
        "content_id": "default",
    }
    await brain.handle_router_request(TEST_DEVICE_ID, req)
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["response_action"]["output_type"] == "GLOBAL_RESPONSE"
    assert payload["output"]["text"]
    assert payload["output"]["markup"] == payload["output"]["text"]


async def test_continue_uses_openai(
    brain: RemoteChatBrain, fake_openai,
) -> None:
    req = {
        "backend": "router",
        "command": "continue",
        "event_id": "e-cont",
        "module_id": "OPENMOXIE_CHAT",
        "content_id": "default",
        "speech": "Привет",
    }
    await brain.handle_router_request(TEST_DEVICE_ID, req)
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["output"]["text"] == "Привет! Рад тебя видеть!"
    assert payload["event_id"] == "e-cont"


async def test_notify_does_not_publish(brain: RemoteChatBrain) -> None:
    req = {
        "backend": "router",
        "command": "notify",
        "event_id": "e-n",
        "module_id": "OPENMOXIE_CHAT",
        "content_id": "default",
        "speech": "Hello there",
        "extra_lines": [{"context_type": "input", "text": "hi"}],
    }
    await brain.handle_router_request(TEST_DEVICE_ID, req)
    assert brain._published == []  # type: ignore[attr-defined]


async def test_mqtt_dispatches_remote_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    received: list[tuple[str, bytes]] = []

    async def handler(device_id: str, payload: bytes) -> None:
        received.append((device_id, payload))

    loop = asyncio.get_running_loop()
    mqtt_service.register_remote_chat_handlers(
        loop, handler, handler, device_state_handler=None,
    )
    try:
        class _Msg:
            topic = f"/devices/{TEST_DEVICE_ID}/events/remote-chat"
            payload = b'{"query":"modules","event_id":"x"}'

        mqtt_service._on_message(None, None, _Msg())  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert len(received) == 1
        assert received[0][0] == TEST_DEVICE_ID
    finally:
        mqtt_service._remote_chat_handler = None  # noqa: SLF001
        mqtt_service._activity_log_handler = None  # noqa: SLF001
        mqtt_service._device_state_handler = None  # noqa: SLF001


assert remote_chat_brain is not None
