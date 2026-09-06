"""Config-push identity stripping — native-brain crash workaround.

Re-pushing mentor/user identity mid-session SIGABRTs the robot brain
(OnCloudSettingsChanged -> SetMentorName -> SaveUserName on a destroyed mutex).
Toggle pushes must therefore omit identity fields.
"""

import pytest

from src.services import tts_service
from src.services.mqtt_service import mqtt_service
from tests.conftest import TEST_DEVICE_ID

_FULL_CONFIG = {
    "pairing_status": "paired",
    "child_pii": {"nickname": "Pat", "input_speed": 0.0},
    "moxie_mode": "TELEHEALTH",
    "settings": {"props": {"cloud_tts": "1"}},
}


def test_without_identity_fields_strips_only_identity() -> None:
    stripped = tts_service._without_identity_fields(_FULL_CONFIG)  # noqa: SLF001
    assert "child_pii" not in stripped
    assert "pairing_status" not in stripped
    # Control + settings survive so the toggle still lands.
    assert stripped["moxie_mode"] == "TELEHEALTH"
    assert stripped["settings"]["props"]["cloud_tts"] == "1"
    # Deep copy: the source config is untouched.
    assert "child_pii" in _FULL_CONFIG


def test_push_config_omits_identity(
    known_device: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    pushed: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        mqtt_service,
        "publish_settings_update",
        lambda device_id, config: pushed.append((device_id, config)),
    )

    targets = tts_service.push_config(_FULL_CONFIG)

    assert targets == [TEST_DEVICE_ID]
    assert len(pushed) == 1
    _, sent = pushed[0]
    assert "child_pii" not in sent
    assert "pairing_status" not in sent
    assert sent["moxie_mode"] == "TELEHEALTH"
