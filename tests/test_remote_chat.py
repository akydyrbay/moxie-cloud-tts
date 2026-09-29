"""Tests for the remote-chat brain (modules / schedule / conversation)."""

import json

import pytest

from src.config import REMOTE_CHAT_OPENERS, settings
from src.services.device_state import device_state
from src.services.mqtt_service import mqtt_service
from src.services.remote_chat import RemoteChatBrain, fallback_line, generation_error_line, remote_chat_brain
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


async def test_prompt_sends_opener_after_wakeup(
    brain: RemoteChatBrain,
) -> None:
    """After WAKEUP, OPENMOXIE_CHAT gets a speakable greeting on its own volley."""
    await brain.handle_router_request(
        TEST_DEVICE_ID,
        {
            "backend": "router",
            "command": "start",
            "event_id": "wake-1",
            "module_id": "WAKEUP",
            "speech": "Moxie, let's go",
        },
    )
    brain._published.clear()  # type: ignore[attr-defined]
    await brain.handle_router_request(
        TEST_DEVICE_ID,
        {
            "backend": "router",
            "command": "prompt",
            "event_id": "prompt-1",
            "module_id": "OPENMOXIE_CHAT",
            "content_id": "default",
        },
    )
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["event_id"] == "prompt-1"
    text = payload["output"]["text"]
    assert text in REMOTE_CHAT_OPENERS["default"].split("|")
    assert text in payload["output"]["markup"]
    assert "<usel" in payload["output"]["markup"]
    assert payload["response_action"]["output_type"] == "GLOBAL_RESPONSE"


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
    assert payload["output"]["text"] == generation_error_line("kk")


async def test_continue_uses_openai(
    brain: RemoteChatBrain, fake_openai,
) -> None:
    fake_openai.chat.completions._reply = "Сәлем! Сені көргеніме қуаныштымын!"
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
    assert payload["output"]["text"] == "Сәлем! Сені көргеніме қуаныштымын!"
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


async def test_openmoxie_notify_does_not_mark_conversation_active(
    brain: RemoteChatBrain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A silent OPENMOXIE_CHAT notify must not stop wake-gate STT inject."""
    marked: list[str] = []
    monkeypatch.setattr(
        "src.services.remote_chat.note_conversation_active",
        lambda device_id: marked.append(device_id),
    )
    await brain.handle_router_request(
        TEST_DEVICE_ID,
        {
            "backend": "router",
            "command": "notify",
            "event_id": "e-notify",
            "module_id": "OPENMOXIE_CHAT",
            "content_id": "default",
            "speech": "",
        },
    )
    assert marked == []

    await brain.handle_router_request(
        TEST_DEVICE_ID,
        {
            "backend": "router",
            "command": "prompt",
            "event_id": "e-prompt",
            "module_id": "OPENMOXIE_CHAT",
            "content_id": "default",
        },
    )
    assert marked == [TEST_DEVICE_ID]


async def test_sleep_tag_keeps_speak_as_primary(
    brain: RemoteChatBrain, fake_openai, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`<sleep>` must not replace the speak block or Cloud TTS is skipped."""
    monkeypatch.setattr(
        "src.services.llm.openai_chat.respond",
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


async def test_request_sleep_is_sent_with_next_chat_turn(
    brain: RemoteChatBrain, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.services import stt_request

    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    turn = {
        "backend": "router", "module_id": "OPENMOXIE_CHAT", "content_id": "default",
    }
    await brain.handle_router_request(
        TEST_DEVICE_ID, {**turn, "command": "prompt", "event_id": "opener"},
    )
    brain._published.clear()  # type: ignore[attr-defined]
    brain.request_sleep(TEST_DEVICE_ID)
    # Nothing is sent on an answered volley, and listening stays in chat.
    assert brain._published == []  # type: ignore[attr-defined]
    assert stt_request._conversation_phase[TEST_DEVICE_ID] == "chat"

    await brain.handle_router_request(
        TEST_DEVICE_ID, {**turn, "command": "reprompt", "event_id": "live", "speech": ""},
    )
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert payload["event_id"] == "live"
    assert payload["output"]["text"] == "Ұйықтайтын уақыт болды. Сау бол!"
    assert payload["response_actions"][0].get("action") is None
    assert payload["response_actions"][1] == {"action": "sleep", "output_type": "GLOBAL_RESPONSE"}

    brain._published.clear()  # type: ignore[attr-defined]
    await brain.handle_router_request(
        TEST_DEVICE_ID, {**turn, "command": "reprompt", "event_id": "after", "speech": ""},
    )
    _topic, payload = brain._published[0]  # type: ignore[attr-defined]
    assert all(a.get("action") != "sleep" for a in payload["response_actions"])


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
