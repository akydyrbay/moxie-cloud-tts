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

from src.config import settings
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.mqtt_service import mqtt_service
from src.services.zmq_service import zmq_service

logger = logging.getLogger(__name__)


async def get_status() -> dict:
    return await device_state.get_config()


async def get_puppet_mode() -> bool:
    return await device_state.get_puppet_mode()


async def request_toggle(enabled: bool) -> dict:
    """Entry point for the Swagger route.

    Publishes onto the internal ZMQ bus rather than mutating state directly; the
    state change + MQTT push happen in `_apply_toggle`, driven by the ZMQ
    consumer started in `main.py`.
    """
    await zmq_service.publish_cloud_tts_command(enabled)
    return await device_state.get_config()


async def apply_toggle(enabled: bool) -> None:
    """Public entry point for the ZMQ consumer (see main.py) to apply a toggle."""
    await _apply_toggle(enabled)


async def set_puppet_mode(enabled: bool) -> dict:
    """Enable/disable TELEHEALTH mode and push the config to the robot(s)."""
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
    for device_id in targets:
        mqtt_service.publish_settings_update(device_id, config)
    return targets


async def _apply_toggle(enabled: bool) -> None:
    config = await device_state.set_cloud_tts(enabled)
    pushed = push_config(config)
    if pushed:
        logger.info("published cloud_tts=%s to %s", enabled, pushed)
