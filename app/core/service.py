"""Own the migrated protocol runtime, diagnostics and bounded shutdown."""

import asyncio
from contextvars import copy_context
import logging
import threading

from app.config import Settings
from app.core.context import Scope
from app.core.controller_lock import ControllerLock

logger = logging.getLogger(__name__)


async def bounded_call(function, timeout):
    """Bound blocking cleanup without retaining the loop's default executor."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    context = copy_context()

    def finish(error):
        if not future.done():
            if error is None:
                future.set_result(None)
            else:
                future.set_exception(error)

    def run():
        error = None
        try:
            context.run(function)
        except Exception as exc:
            error = exc
        try:
            loop.call_soon_threadsafe(finish, error)
        except RuntimeError:
            pass

    threading.Thread(target=run, daemon=True, name="moxie-cleanup").start()
    await asyncio.wait_for(future, max(0.001, timeout))


class AgentService:
    def __init__(self, settings: Settings | None = None, *, registry=None,
                 transport_factory=None, state=None, supervisor=None,
                 providers=None, storage=None):
        self.settings = settings if settings is not None else Settings()
        self.scope = Scope(settings=self.settings)
        for name, component in {"registry": registry, "state": state,
                                "supervisor": supervisor, "providers": providers}.items():
            if component is not None:
                self.scope.components[name] = component
        self.storage = storage
        self.lock = ControllerLock(self.settings.CONTROLLER_LOCK_FILE)
        self.started = False
        self.stopping = False
        self.shutdown_report = {}
        with self.scope.activate():
            from app.services.devices.registry import device_registry
            from app.services.devices.state import device_state
            from app.services.actions.live_actions import live_actions
            from app.services.providers.pool import provider_pool
            from app.services.conversation.remote_chat import remote_chat_brain
            from app.services.transport.mqtt import mqtt_service
            self.registry = device_registry.resolve()
            self.state = device_state.resolve()
            self.supervisor = live_actions.resolve()
            self.providers = provider_pool.resolve()
            self.brain = remote_chat_brain.resolve()
            if transport_factory:
                self.scope.components["transport"] = transport_factory()
            self.transport = mqtt_service.resolve()

    async def start(self):
        if self.started or self.stopping:
            raise RuntimeError("Create a new AgentService for each lifespan")
        self.lock.acquire()
        self.started = True
        try:
            with self.scope.activate():
                from app.services.devices import face
                from app.services.speech.synthesis.handler import CLOUD_TTS_REQUEST_PREFIX, handle_payload
                from app.services.speech.recognition.handler import STT_REQUEST_PREFIX, handle_payload as stt_payload
                from app.services.conversation.remote_chat import handle_remote_chat_payload, handle_activity_log_payload

                loop = asyncio.get_running_loop()
                self.transport.register_cloud_tts_request_handler(loop, handle_payload, CLOUD_TTS_REQUEST_PREFIX)
                self.transport.register_stt_request_handler(loop, stt_payload, STT_REQUEST_PREFIX)

                async def bootstrap(device, _payload):
                    await self.brain.ensure_device_bootstrapped(device)

                self.transport.register_remote_chat_handlers(loop, handle_remote_chat_payload,
                    handle_activity_log_payload, device_state_handler=bootstrap)
                # Cloud speech is always enabled when synthesis is configured.
                # Without a key, preserve onboard speech instead of muting it.
                await self.state.set_cloud_tts(bool(self.settings.OPENAI_API_KEY))
                await self.state.set_puppet_mode(False)
                saved = face.load()
                if saved:
                    await self.state.set_face(saved["face_options"], saved["child_id"])
                try:
                    self.transport.connect()
                except Exception as exc:
                    self.transport.last_error = type(exc).__name__
                    logger.exception("MQTT startup failed; HTTP diagnostics remain available")
        except BaseException:
            await self.stop()
            raise

    def status(self):
        devices = self.registry.snapshot(self.settings.DEVICE_FRESHNESS_S)
        broker_ready = self.transport.connected and self.transport.subscribed
        bootstrapped = any(d["fresh"] and d["bootstrap_sent"] for d in devices)
        configured = bool(self.settings.OPENAI_API_KEY)
        ready = self.started and not self.stopping and broker_ready and bootstrapped and configured
        return {
            "ready": ready,
            "accepting": self.started and not self.stopping,
            "broker": {"connected": self.transport.connected,
                       "subscribed": self.transport.subscribed, "error": self.transport.last_error},
            "devices": devices,
            "providers": {"configured": configured, "availability": "unverified" if configured else "unconfigured",
                          "requires_internet": True, "active_workers": self.providers.active},
            "dispatch": {"pending": self.transport.pending, "limit": self.settings.MQTT_MAX_PENDING,
                         "rejected": self.transport.rejected},
            "bootstrap": "sent" if bootstrapped else "pending",
            "robot_ready": None,
            "april_actions_available": False,
        }

    async def stop(self):
        if self.stopping:
            return
        self.stopping = self.scope.closing = True
        deadline = asyncio.get_running_loop().time() + self.settings.SHUTDOWN_TIMEOUT_S

        def remaining():
            return max(0.001, deadline - asyncio.get_running_loop().time())

        with self.scope.activate():
            from app.services.providers.openai_client import close_client
            self.transport.stop_admission()
            self.providers.stop_admission()
            for device in self.registry.known():
                self.brain.reset_device(device)
            tasks = list(self.scope.tasks)
            for task in tasks:
                task.cancel()
            if tasks:
                _, pending = await asyncio.wait(tasks, timeout=remaining())
                self.shutdown_report["pending_tasks"] = len(pending)
            await self.transport.settle(remaining())
            if self.storage is not None:
                try:
                    await asyncio.wait_for(self.storage.flush(), remaining())
                except Exception as exc:
                    self.shutdown_report["storage_error"] = type(exc).__name__
                    logger.exception("Execution/outbox flush failed")

            async def close_transport():
                await bounded_call(self.transport.disconnect, remaining())

            async def close_providers():
                self.shutdown_report["provider_workers_remaining"] = await self.providers.close(remaining())
                await bounded_call(close_client, remaining())

            results = await asyncio.gather(close_transport(), close_providers(), return_exceptions=True)
            self.shutdown_report["cleanup_errors"] = [type(r).__name__ for r in results if isinstance(r, BaseException)]
            if self.shutdown_report["cleanup_errors"] or self.shutdown_report.get("provider_workers_remaining"):
                logger.warning("Shutdown deadline/resource report: %s", self.shutdown_report)
            self.lock.release()
            self.started = False
