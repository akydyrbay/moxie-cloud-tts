"""Robot interrupt preserves observed TELEHEALTH state."""

import json


from app.services.actions import telehealth
from tests.conftest import TEST_DEVICE_ID


def test_interrupt_does_not_fabricate_a_robot_state(captured_publish) -> None:
    """Sending an interrupt must preserve the last state reported by the robot."""
    telehealth.note_puppet_state(TEST_DEVICE_ID, "READY")
    telehealth.interrupt(TEST_DEVICE_ID)
    assert telehealth.get_puppet_state(TEST_DEVICE_ID) == "READY"
    assert [(topic, json.loads(payload)) for topic, payload in captured_publish] == [
        (f"/devices/{TEST_DEVICE_ID}/commands/telehealth",
         {"command": "telehealth", "message": {"action": "INTERRUPT"}}),
    ]
