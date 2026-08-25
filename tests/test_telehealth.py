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


async def test_speak_line_publishes_play_output_only(
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
    assert actions == ["PLAY_OUTPUT"]
    play = json.loads(captured_publish[-1][1])["message"]["output"]
    assert play == {"text": "Привет", "markup": "Привет"}


def test_interrupt_does_not_fabricate_a_robot_state() -> None:
    """Only the robot may set telehealth state.

    The robot reports READY on entering TELEHEALTH and nothing per line, so a
    locally invented "INTERRUPTING" would never be corrected and would make
    every later speak wait out the full READY timeout.
    """
    telehealth.note_puppet_state(TEST_DEVICE_ID, "READY")
    telehealth.interrupt(TEST_DEVICE_ID)
    assert telehealth.get_puppet_state(TEST_DEVICE_ID) == "READY"


async def test_repeated_speaks_do_not_wait_for_a_second_ready(
    known_device, captured_publish,
) -> None:
    """Once READY, the Nth line must go out as promptly as the first."""
    telehealth.note_puppet_state(TEST_DEVICE_ID, "READY")

    first = await telehealth.speak_line(TEST_DEVICE_ID, "один")
    second = await telehealth.speak_line(TEST_DEVICE_ID, "два")
    third = await telehealth.speak_line(TEST_DEVICE_ID, "три")

    for result in (first, second, third):
        assert result["ready"] is True
        assert result["puppet_state"] == "READY"

    spoken = [
        json.loads(payload)["message"]["output"]["text"]
        for topic, payload in captured_publish
        if topic.endswith("/commands/telehealth")
        and json.loads(payload)["message"]["action"] == "PLAY_OUTPUT"
    ]
    assert spoken == ["один", "два", "три"]
