"""Registry of Moxie devices seen on the broker.

Replaces the previously hardcoded `settings.DEVICE_ID`. Device ids are learned
from the `/devices/{id}/...` topics the robot publishes on, so responses always
go back to the robot that actually asked, and a different/replacement unit works
without editing configuration.

Populated from paho's network thread and read from asyncio handlers, so access
is guarded by a plain threading lock (never held across an await).
"""

import logging
import threading
import time

from src.config import settings

logger = logging.getLogger(__name__)


class DeviceRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_seen: dict[str, float] = {}
        self._bootstrapped: set[str] = set()

    def seen(self, device_id: str) -> bool:
        """Record traffic from `device_id`; return True the first time it appears."""
        if not device_id:
            return False
        with self._lock:
            is_new = device_id not in self._last_seen
            self._last_seen[device_id] = time.time()
        if is_new:
            logger.info("discovered device %s", device_id)
        return is_new

    def mark_bootstrapped(self, device_id: str) -> bool:
        """Return True if this call claimed the one-time bootstrap for the device."""
        with self._lock:
            if device_id in self._bootstrapped:
                return False
            self._bootstrapped.add(device_id)
            return True

    def forget_bootstrap(self, device_id: str) -> None:
        """Allow a device to be bootstrapped again (e.g. after reconnect)."""
        with self._lock:
            self._bootstrapped.discard(device_id)

    def is_bootstrapped(self, device_id: str) -> bool:
        with self._lock:
            return device_id in self._bootstrapped

    def known(self) -> list[str]:
        """All device ids seen this run, most recently active first."""
        with self._lock:
            items = sorted(self._last_seen.items(), key=lambda kv: kv[1], reverse=True)
        return [device_id for device_id, _ in items]

    def primary(self) -> str | None:
        """Best target for an operator-initiated command.

        Prefers an explicit `DEVICE_ID` override, else the most recently active
        device. Returns None when no robot has been seen and no override is set,
        so callers can report that instead of publishing into the void.
        """
        if settings.DEVICE_ID:
            return settings.DEVICE_ID
        known = self.known()
        return known[0] if known else None

    def targets(self) -> list[str]:
        """Devices a config push should go to."""
        known = self.known()
        if known:
            return known
        return [settings.DEVICE_ID] if settings.DEVICE_ID else []

    def reset(self) -> None:
        with self._lock:
            self._last_seen.clear()
            self._bootstrapped.clear()


device_registry = DeviceRegistry()
