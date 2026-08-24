"""Telehealth state tracking and speak_line sequencing."""

import asyncio
import json

import pytest

from src.services import telehealth
from src.services.remote_chat import remote_chat_brain
from tests.conftest import TEST_DEVICE_ID


async def test_activity_log_telehealth_updates_ready_state() -> None:
    payload = json.dumps({
        "subtopic": "telehealth",
        "message": {"state": "READY"},
    })
    await remote_chat_brain.handle_activity_log_event(TEST_DEVICE_ID, payload)
    assert telehealth.get_puppet_state(TEST_DEVICE_ID) == "READY"


async def test_wait_until_ready_unblocks_on_state() -> None:
    telehealth.note_puppet_state(TEST_DEVICE_ID, "IN_SESSION")

    async def _flip() -> None:
        await asyncio.sleep(0.02)
        telehealth.note_puppet_state(TEST_DEVICE_ID, "READY")

    asyncio.create_task(_flip())
    assert await telehealth.wait_until_ready(TEST_DEVICE_ID, timeout_s=1.0) is True


async def test_speak_line_publishes_interrupt_then_play(
    known_device, captured_publish,
) -> None:
    telehealth.note_puppet_state(TEST_DEVICE_ID, "READY")
    result = await telehealth.speak_line(TEST_DEVICE_ID, "Привет")
    assert result["status"] == "requested"

    actions = [
        json.loads(payload)["message"]["action"]
        for topic, payload in captured_publish
        if topic.endswith("/commands/telehealth")
    ]
    assert actions == ["INTERRUPT", "PLAY_OUTPUT"]
    play = json.loads(captured_publish[-1][1])["message"]["output"]
    assert play == {"text": "Привет", "markup": "Привет"}
