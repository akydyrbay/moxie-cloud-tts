"""In-memory device configuration state.

Holds the hardcoded device config plus the two dynamic fields:

- `settings.props.cloud_tts` — "0"/"1", routes the robot's speech through this
  service instead of its onboard TTS.
- `moxie_mode` — "TELEHEALTH" puts the robot into puppet mode: awake, ignoring
  the schedule, speaking only what we push. Absent means normal operation.

Both start disabled; `main.py` may enable them at startup. It also restores
the saved face (`child_pii.face_options`), which only bootstrap sends. An asyncio.Lock
guards mutation since HTTP handlers and MQTT callbacks both touch this.
"""

import asyncio
from copy import deepcopy

from src.config import HARDCODED_DEVICE_CONFIG, MOXIE_MODE_TELEHEALTH


class DeviceState:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._config: dict = deepcopy(HARDCODED_DEVICE_CONFIG)
        self._config["settings"]["props"]["cloud_tts"] = "0"
        self._speech_revision = 0

    @property
    def speech_revision(self) -> int:
        """Read on the asyncio loop to invalidate output across mode changes.

        A counter also detects switching off and back on while a provider is
        running. Setters update it without yielding inside the config lock.
        """
        return self._speech_revision

    async def get_config(self) -> dict:
        """Return a copy of the full current device config."""
        async with self._lock:
            return deepcopy(self._config)

    async def get_cloud_tts(self) -> bool:
        async with self._lock:
            return self._config["settings"]["props"]["cloud_tts"] == "1"

    async def set_cloud_tts(self, enabled: bool) -> dict:
        """Update cloud_tts and return the full resulting config."""
        async with self._lock:
            if (self._config["settings"]["props"]["cloud_tts"] == "1") != enabled:
                self._speech_revision += 1
                from src.services.stt_request import invalidate_mode
                invalidate_mode()
            self._config["settings"]["props"]["cloud_tts"] = "1" if enabled else "0"
            return deepcopy(self._config)

    async def get_puppet_mode(self) -> bool:
        async with self._lock:
            return self._config.get("moxie_mode") == MOXIE_MODE_TELEHEALTH

    async def set_puppet_mode(self, enabled: bool) -> dict:
        """Enable/disable TELEHEALTH mode and return the full resulting config."""
        async with self._lock:
            if (self._config.get("moxie_mode") == MOXIE_MODE_TELEHEALTH) != enabled:
                self._speech_revision += 1
                from src.services.stt_request import invalidate_mode
                invalidate_mode(puppet=enabled)
            if enabled:
                self._config["moxie_mode"] = MOXIE_MODE_TELEHEALTH
            else:
                self._config.pop("moxie_mode", None)
            return deepcopy(self._config)

    async def get_face(self) -> dict:
        """Face options/child id the next bootstrap will send."""
        async with self._lock:
            child = self._config["child_pii"]
            return {"face_options": list(child.get("face_options", [])), "child_id": child.get("id")}

    async def set_face(self, options: list[str], child_id: str | None) -> None:
        """Store validated face options for the next bootstrap only.

        child_pii is never pushed mid-session (tts_service strips it), so this
        does not touch the live robot or the speech revision.
        """
        async with self._lock:
            child = self._config["child_pii"]
            child["face_options"] = list(options)
            if child_id is None:
                child.pop("id", None)
            else:
                child["id"] = child_id


device_state = DeviceState()
