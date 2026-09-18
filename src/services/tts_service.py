"""Cloud-TTS and puppet-mode toggle orchestration.

Conditional logic for the two deployment shapes:

    OpenMoxie running     -> it owns the robot's config; don't push (avoids
                             racing its own `config_update_live`).
    OpenMoxie not running -> this service emulates the live push over MQTT.

`settings.OPENMOXIE_ENABLED` selects the branch.

Config is pushed to every device seen on the broker (or the `DEVICE_ID`
override), so a toggle reaches whichever robot is actually connected.
"""

import logging
from copy import deepcopy

from src.config import settings
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.mqtt_service import mqtt_service

logger = logging.getLogger(__name__)

# Mentor/user identity fields. Re-pushing them mid-session crashes the native
# brain: OnCloudSettingsChanged -> SetMentorName -> AddUser -> SaveUserName locks
# a destroyed mutex and SIGABRTs (backtrace in docs/SPEAK_PUPPET_ANALYSIS.md
# Bug A; reproduced on every puppet enable before the strip). A cloud_tts /
# moxie_mode toggle does not need identity, so it is stripped from toggle
# pushes — the change then carries no mentor/user delta for the handler to act
# on. Identity is still sent once, at bootstrap
# (remote_chat.ensure_device_bootstrapped).
_IDENTITY_FIELDS = ("child_pii", "pairing_status")


def _without_identity_fields(config: dict) -> dict:
    """Return a copy of `config` with mentor/user identity fields removed."""
    stripped = deepcopy(config)
    for field in _IDENTITY_FIELDS:
        stripped.pop(field, None)
    return stripped


async def get_status() -> dict:
    return await device_state.get_config()


async def get_puppet_mode() -> bool:
    return await device_state.get_puppet_mode()


async def request_toggle(enabled: bool) -> dict:
    """Toggle cloud_tts and push the updated config to connected robots over MQTT."""
    config = await device_state.set_cloud_tts(enabled)
    pushed = push_config(config)
    if pushed:
        logger.info("published cloud_tts=%s to %s", enabled, pushed)
    return config


async def set_puppet_mode(enabled: bool) -> dict:
    """Enable/disable TELEHEALTH mode and push the config to the robot(s).

    Idempotent: if the mode is already the requested value, do not re-push.
    Mid-session moxie_mode flips are a known native crash trigger on this
    firmware, so avoid no-op pushes.
    """
    current = await device_state.get_puppet_mode()
    if current == enabled:
        config = await device_state.get_config()
        logger.info(
            "puppet mode already %s; skipping config push",
            enabled,
        )
        return {
            "enabled": enabled,
            "devices": [],
            "config": config,
            "unchanged": True,
        }

    config = await device_state.set_puppet_mode(enabled)
    pushed = push_config(config)
    logger.info("puppet mode=%s pushed to %s", enabled, pushed or "no devices")
    return {"enabled": enabled, "devices": pushed, "config": config}


def push_config(config: dict) -> list[str]:
    """Push `config` to every known device; return the ids actually targeted."""
    if settings.OPENMOXIE_ENABLED:
        logger.info("OPENMOXIE_ENABLED=true: deferring config push to OpenMoxie")
        return []
    targets = device_registry.targets()
    if not targets:
        logger.warning(
            "no device to push config to yet; it will be applied when a robot "
            "connects and is bootstrapped",
        )
        return []
    # Strip identity so a mid-session toggle cannot re-trigger SetMentorName and
    # crash the brain (see _IDENTITY_FIELDS).
    payload = _without_identity_fields(config)
    for device_id in targets:
        mqtt_service.publish_settings_update(device_id, payload)
    return targets
