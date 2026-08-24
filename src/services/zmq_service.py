"""ZMQ service."""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable

import zmq
import zmq.asyncio

from src.config import settings

logger = logging.getLogger(__name__)


class ZmqService:
    def __init__(self) -> None:
        # Move context and socket initialization here, but DO NOT bind yet
        self._ctx = zmq.asyncio.Context()
        self._pub = self._ctx.socket(zmq.PUB)
        self._sub_task: asyncio.Task | None = None
        self._is_bound = False

    def start(self) -> None:
        """Bind the PUB socket to the configured address.

        Called during the FastAPI lifespan startup event to avoid port conflicts
        during hot-reloads.
        """
        if not self._is_bound:
            logger.info("Binding ZMQ PUB socket to %s", settings.ZMQ_PUB_ADDR)
            self._pub.bind(settings.ZMQ_PUB_ADDR)
            self._is_bound = True

    async def publish_cloud_tts_command(self, enabled: bool) -> None:
        """Publish a set_cloud_tts command onto the internal ZMQ bus."""
        if not self._is_bound:
            msg = "ZmqService must be started before publishing messages."
            raise RuntimeError(msg)

        message = json.dumps({"command": "set_cloud_tts", "enabled": enabled})
        await self._pub.send_string(message)
        logger.debug("zmq published: %s", message)

    def start_consumer(self, handler: Callable[[bool], Awaitable[None]]) -> None:
        """Start the background consumer task."""
        self._sub_task = asyncio.create_task(self._consume(handler))

    async def _consume(self, handler: Callable[[bool], Awaitable[None]]) -> None:
        # Use the class instance context instead of a global one
        sub = self._ctx.socket(zmq.SUB)
        sub.connect(settings.ZMQ_PUB_ADDR)
        sub.setsockopt_string(zmq.SUBSCRIBE, "")
        try:
            while True:
                raw = await sub.recv_string()
                data = json.loads(raw)
                if data.get("command") == "set_cloud_tts":
                    await handler(bool(data["enabled"]))
        finally:
            sub.close()

    async def stop(self) -> None:
        """Cancel the consumer task and close the PUB socket (used on app shutdown)."""
        if self._sub_task:
            self._sub_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sub_task
        self._pub.close()
        self._ctx.term()
        self._is_bound = False


# Instantiation stays clean; no side-effects happen until .start() is explicitly called
zmq_service = ZmqService()
