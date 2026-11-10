"""MQTT transport.

Topic/framing conventions follow OpenMoxie's `moxie_server.py`, which is what
the robot firmware expects:

- framed protobuf  : `b"<fully.qualified.TypeName>:" + serialized`
                     on `/devices/{device}/commands/zmq`
- JSON commands    : `/devices/{device}/commands/{command}`
- device config    : `/devices/{device}/config`

Inbound messages are routed by topic to async handlers. paho delivers on its own
network thread, so handlers are bridged onto the asyncio loop with
`run_coroutine_threadsafe`. Every handler receives the device id parsed from the
topic — nothing targets a hardcoded device.
"""

from app.core.context import Local, current_scope

import asyncio
import json
import logging
import re
import ssl
import time
import uuid
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from contextvars import ContextVar
from threading import BoundedSemaphore, Lock
from typing import Any

from google.protobuf.message import Message
from paho.mqtt.client import CallbackAPIVersion, Client, MQTTMessage

from app.config import settings
from app.protos.embodied.logging.Log_pb2 import ProtoSubscribe
from app.services.devices.registry import device_registry

logger = logging.getLogger(__name__)

# Proto message types this service asks the robot to forward to it.
# `CloudTTSRequest` drives synthesis; `zmqSTTRequest` streams the robot's mic
# audio + VAD state so the STT handler can answer each listen session (Goal 1
# injects a canned recognized line; real transcription is a later milestone).
PROTO_SUBSCRIBE_TYPES = [
    "embodied.unity.CloudTTSRequest",
    "embodied.perception.audio.zmqSTTRequest",
]

# Async handler receiving (device_id, raw_payload).
DeviceEventHandler = Callable[[str, bytes], Awaitable[Any]]

# Payload bytes logged for unrecognized traffic. Full payloads must never be
# logged: a single CloudTTSResponse is tens of KB of audio and produced ~190 KB
# of log output per reply. 512 bytes is enough to see a JSON envelope or proto
# type name; 64 bytes used to cut off inside `events/device-logs` envelopes.
_MAX_LOGGED_PAYLOAD = 512

# `events/device-logs` carries the robot's own log stream as a JSON envelope:
# {"timestamp": "...", "level": "I", "tag": "Unity   ", "pid": ..., <message>}.
# It is the only window into what the firmware actually does with what we
# publish — whether it plays a CloudTTSResponse, drops it, or is still busy from
# a previous line. It used to fall through to the generic dump above, which cut
# every entry off at 64 bytes, i.e. inside the envelope and before the message.
_ROBOT_LOG_SUFFIX = "events/device-logs"

# Envelope keys that are metadata rather than the log text itself.
_ROBOT_LOG_META_KEYS = frozenset({"timestamp", "level", "tag", "pid", "tid"})

# mosquitto publishes its log to `$SYS/broker/log/#` (enabled by `log_dest topic`
# in mosquitto.conf). Parsing it is how OpenMoxie notices a robot connecting —
# and the only way to know a robot REBOOTED, which means its config must be
# pushed again. A Moxie's MQTT client id is its device id.
_CONNECT_RE = re.compile(r"connected from (.*) as (d_[a-f0-9-]+)")
_DISCONNECT_RE = re.compile(
    r"Client (d_[a-f0-9-]+) (?:closed its connection|disconnected)",
)

_output_generation = ContextVar("mqtt_output_generation", default=None)


def _parse_robot_log_entries(raw: str) -> list[object]:
    """Split a device-logs payload into one or more log records."""
    stripped = raw.strip()
    if not stripped:
        return [raw]
    try:
        parsed = json.loads(stripped)
    except ValueError:
        entries = []
        for line in stripped.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except ValueError:
                entries.append(line)
        return entries or [raw]
    if isinstance(parsed, list):
        return parsed
    return [parsed]


class MqttService:
    def __init__(self, client=None) -> None:
        self.scope = current_scope()
        # Unique client id: a fixed id makes the broker evict the previous
        # session, which caused connect/disconnect flapping under `--reload`.
        client_id = f"april-moxie-{uuid.uuid4().hex[:8]}"
        self.client = client or Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            transport="tcp",
        )
        self.client.tls_set(cert_reqs=ssl.CERT_NONE)
        self.client.on_connect = self._callback(self._on_connect)
        self.client.on_message = self._callback(self._on_message)
        self.client.on_disconnect = self._callback(self._on_disconnect)
        self.client.on_connect_fail = self._callback(self._on_connect_fail)
        self.client.on_subscribe = self._callback(self._on_subscribe)
        self.client.reconnect_delay_set(settings.MQTT_RECONNECT_MIN_S, settings.MQTT_RECONNECT_MAX_S)
        self.client.connect_timeout = 3.0
        self.client.max_queued_messages_set(settings.MQTT_MAX_PENDING)
        self.connected = False
        self.subscribed = False
        self.last_error = None
        self._subscriptions = set()
        self._generation = 0
        self._device_generations = {}
        self._accepting = True
        self._closed = False
        self._started = False
        self._pending = set()
        self._pending_lock = Lock()
        self._resets_pending = set()
        self._slots = BoundedSemaphore(settings.MQTT_MAX_PENDING)
        self.rejected = 0
        logger.info("mqtt client id: %s", client_id)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._cloud_tts_request_handler: DeviceEventHandler | None = None
        self._cloud_tts_request_prefix: bytes | None = None
        self._stt_request_handler: DeviceEventHandler | None = None
        self._stt_request_prefix: bytes | None = None
        self._remote_chat_handler: DeviceEventHandler | None = None
        self._activity_log_handler: DeviceEventHandler | None = None
        self._device_state_handler: DeviceEventHandler | None = None
        # Last `client-service-robot-status` JSON per device, so heartbeats
        # that did not change are not logged every few hundred ms.
        self._last_robot_status: dict[str, str] = {}

    def _callback(self, callback):
        def scoped(*args, **kwargs):
            with self.scope.activate():
                return callback(*args, **kwargs)
        return scoped

    @property
    def pending(self):
        with self._pending_lock:
            return len(self._pending)

    def _stamp(self, device_id):
        return self._generation, self._device_generations.get(device_id, 0)

    def _can_publish(self, device_id=None):
        stamp = _output_generation.get()
        return not self._closed and (stamp is None or stamp == self._stamp(device_id))

    # --- registration -------------------------------------------------------

    def register_cloud_tts_request_handler(
        self,
        loop: asyncio.AbstractEventLoop,
        handler: DeviceEventHandler,
        match_prefix: bytes,
    ) -> None:
        """Register the handler for inbound framed `CloudTTSRequest` payloads."""
        self._loop = loop
        self._cloud_tts_request_handler = handler
        self._cloud_tts_request_prefix = match_prefix
        logger.info(
            "registered inbound CloudTTSRequest handler (prefix=%r)",
            match_prefix,
        )

    def register_stt_request_handler(
        self,
        loop: asyncio.AbstractEventLoop,
        handler: DeviceEventHandler,
        match_prefix: bytes,
    ) -> None:
        """Register the handler for inbound framed `zmqSTTRequest` payloads."""
        self._loop = loop
        self._stt_request_handler = handler
        self._stt_request_prefix = match_prefix
        logger.info(
            "registered inbound zmqSTTRequest handler (prefix=%r)",
            match_prefix,
        )

    def register_remote_chat_handlers(
        self,
        loop: asyncio.AbstractEventLoop,
        remote_chat_handler: DeviceEventHandler,
        activity_log_handler: DeviceEventHandler,
        device_state_handler: DeviceEventHandler | None = None,
    ) -> None:
        """Register remote-chat brain handlers (modules, schedule, first state)."""
        self._loop = loop
        self._remote_chat_handler = remote_chat_handler
        self._activity_log_handler = activity_log_handler
        self._device_state_handler = device_state_handler
        logger.info("registered remote-chat + activity-log MQTT handlers")

    # --- connection ---------------------------------------------------------

    def connect(self) -> None:
        """Connect to the broker and start paho's background network loop."""
        logger.info("connecting to mqtt %s:%s", settings.MQTT_HOST, settings.MQTT_PORT)
        self.client.connect_async(settings.MQTT_HOST, settings.MQTT_PORT, 60)
        self.client.loop_start()
        self._started = True

    def disconnect(self) -> None:
        """Stop the background loop and disconnect from the broker."""
        self._closed = True
        self._accepting = False
        self.connected = self.subscribed = False
        self.client.disconnect()
        if self._started:
            self.client.loop_stop()
        self._started = False

    def stop_admission(self):
        self._accepting = False
        self._closed = True
        self._generation += 1

    async def settle(self, timeout):
        with self._pending_lock:
            pending = list(self._pending)
        for future in pending:
            future.cancel()
        if pending:
            await asyncio.wait([asyncio.wrap_future(f) for f in pending], timeout=timeout)

    def _invalidate_connection(self):
        self._generation += 1
        for device_id in device_registry.known():
            device_registry.forget_bootstrap(device_id)
            self._reset_robot_conversation(device_id)

    def _on_connect_fail(self, *_args):
        self.connected = self.subscribed = False
        self.last_error = "broker_unreachable"
        logger.warning("MQTT connection failed; retrying with capped backoff")

    def _on_disconnect(self, _client, _userdata, _flags, reason_code, _properties=None):
        self.connected = self.subscribed = False
        self.last_error = None if self._closed else f"disconnected: {reason_code}"
        self._subscriptions.clear()
        self._invalidate_connection()

    def _on_subscribe(self, _client, _userdata, mid, reason_codes, _properties=None):
        if any(getattr(code, "is_failure", False) or (isinstance(code, int) and code >= 128)
               for code in reason_codes):
            self.last_error = "subscription_rejected"
            self.subscribed = False
            return
        self._subscriptions.discard(mid)
        self.subscribed = self.connected and not self._subscriptions and self.last_error is None

    def _on_connect(
        self,
        client: Client,
        _userdata: Any,
        _flags: Any,
        reason_code: Any,
        _properties: Any = None,
    ) -> None:
        logger.info("mqtt connected (reason_code=%s)", reason_code)
        if self._closed:
            client.disconnect()
            return
        if reason_code != 0:
            self.connected = self.subscribed = False
            self.last_error = f"connection_rejected: {reason_code}"
            return
        self.connected = True
        self.subscribed = False
        self.last_error = None
        self._subscriptions.clear()
        self._invalidate_connection()
        # Only robot->server traffic. Subscribing to "#" also echoed back every
        # message this service published, including full audio frames.
        for topic in ("/devices/+/events/#", "/devices/+/state", "$SYS/broker/log/#"):
            rc, mid = client.subscribe(topic)
            if rc != 0:
                self.last_error = "subscription_failed"
            else:
                self._subscriptions.add(mid)

    # --- inbound routing ----------------------------------------------------

    def _on_message(self, _client: Client, _userdata: Any, msg: MQTTMessage) -> None:
        if not self._accepting:
            return
        if msg.topic.startswith("$SYS/broker/log/"):
            self._on_broker_log(msg)
            return

        device_id = self._parse_device_id(msg.topic)
        if device_id:
            device_registry.seen(device_id)

        if self._dispatch_cloud_tts_request(device_id, msg):
            return

        if self._dispatch_stt_request(device_id, msg):
            return

        if self._dispatch_device_event(
            device_id,
            msg,
            event_suffixes=("events/remote-chat", "events/remote-chat-staging"),
            handler=self._remote_chat_handler,
            label="remote-chat",
        ):
            return

        if self._dispatch_device_event(
            device_id,
            msg,
            event_suffixes=("events/client-service-activity-log",),
            handler=self._activity_log_handler,
            label="activity-log",
        ):
            return

        if self._dispatch_device_state(device_id, msg):
            return

        if msg.topic.endswith(_ROBOT_LOG_SUFFIX):
            self._on_device_log(device_id, msg)
            return

        if msg.topic.endswith("client-service-robot-status"):
            self._on_robot_status(device_id, msg)
            return

        # High-volume battery/thermal protobuf; nothing about speech.
        if msg.payload.startswith(b"embodied.power"):
            return
        if msg.topic.endswith("events/zmq"):
            if msg.payload.startswith(b"embodied.perception.audio.zmqSTTRequest"):
                # Normally consumed by _dispatch_stt_request above. This quiet
                # fallback only runs when no STT handler is registered, so a
                # stream of mic-audio frames can't flood the log.
                return
            if msg.payload.startswith(b"embodied.perception.audio.ASRAnalytics"):
                # Per-session ASR analytics the robot emits alongside each STT
                # session (one per utterance). We don't consume it; ignore
                # quietly so it can't flood the log.
                return
            logger.info(
                "unhandled zmq proto on %s: %r",
                msg.topic,
                msg.payload[:_MAX_LOGGED_PAYLOAD],
            )
            return

        logger.info(
            "topic: %s payload[:%d]: %r (%d bytes)",
            msg.topic,
            _MAX_LOGGED_PAYLOAD,
            msg.payload[:_MAX_LOGGED_PAYLOAD],
            len(msg.payload),
        )

    def _on_device_log(self, device_id: str | None, msg: MQTTMessage) -> None:
        """Re-emit the robot's own log stream, in full.

        The firmware filters before sending (`default_loglevel`). What it does
        send is re-logged at INFO so a DEBUG/VERBOSE robot line cannot be
        swallowed by Python's logger even if a handler is set to INFO.

        Payloads may be a single JSON object, a JSON array, or NDJSON.
        """
        if not settings.ROBOT_LOG_CAPTURE:
            return

        raw = msg.payload.decode("utf-8", errors="replace")
        for entry in _parse_robot_log_entries(raw):
            self._emit_robot_log(device_id, entry)

    def _emit_robot_log(self, device_id: str | None, entry: object) -> None:
        if not isinstance(entry, dict):
            logger.info("robot-log %s: %s", device_id, entry)
            return

        level = str(entry.get("level", "I")).upper()[:1]
        tag = str(entry.get("tag", "")).strip()
        text = entry.get("message")
        if text is None:
            body = {k: v for k, v in entry.items() if k not in _ROBOT_LOG_META_KEYS}
            text = next(iter(body.values())) if len(body) == 1 else body

        extra = []
        for key in ("module", "logger", "file", "func"):
            value = entry.get(key)
            if value:
                extra.append(f"{key}={value}")
        suffix = f" ({', '.join(extra)})" if extra else ""

        # Always INFO: the original robot level is in the line (`[D]`, `[W]`).
        logger.info(
            "robot-log [%s] %s: %s%s",
            level,
            tag or "-",
            text,
            suffix,
        )

    def _on_robot_status(self, device_id: str | None, msg: MQTTMessage) -> None:
        """Log `client-service-robot-status` when the payload actually changes.

        This topic was previously dropped as heartbeat noise. It is the closest
        thing to a live FSM dump (output slot, MarkingUp, power). Identical
        repeats are skipped so a 2 Hz heartbeat does not drown speech logs.
        """
        raw = msg.payload.decode("utf-8", errors="replace")
        key = device_id or ""
        if self._last_robot_status.get(key) == raw:
            return
        self._last_robot_status[key] = raw
        try:
            status = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("robot-status %s: invalid JSON", device_id)
            return
        allowed = {"mode", "state", "slot", "battery_level", "robot_firmware_version", "software_version",
                   "local_stt", "cloud_translate", "app_language_support", "cloud_tts",
                   "cloud_tts_use_mp3", "cloud_tts_engine", "rchat_api", "remote_transition"}
        safe = {}

        def collect(value, depth=0):
            if not isinstance(value, dict) or depth > 6:
                return
            for name, item in value.items():
                if name in allowed and isinstance(item, (str, int, float, bool)):
                    safe[name] = item[:128] if isinstance(item, str) else item
                elif isinstance(item, dict):
                    collect(item, depth + 1)

        collect(status)
        logger.info("robot-status %s: %s", device_id, json.dumps(safe, sort_keys=True))

    def _on_broker_log(self, msg: MQTTMessage) -> None:
        """Track robot connects/disconnects from the mosquitto log topic.

        A reconnect means the robot restarted its session and lost whatever we
        pushed before, so its bootstrap is cleared and the config (cloud_tts,
        moxie_mode) plus ProtoSubscribe are sent again on its next message.
        """
        try:
            line = msg.payload.decode("utf-8", errors="replace")
        except Exception:
            return

        connected = _CONNECT_RE.search(line)
        if connected:
            device_id = connected.group(2)
            logger.info("robot %s connected to broker", device_id)
            device_registry.forget_bootstrap(device_id)
            self._reset_robot_conversation(device_id)
            device_registry.seen(device_id)
            return

        disconnected = _DISCONNECT_RE.search(line)
        if disconnected:
            device_id = disconnected.group(1)
            logger.info("robot %s disconnected from broker", device_id)
            device_registry.forget_bootstrap(device_id)
            self._reset_robot_conversation(device_id)

    def _reset_robot_conversation(self, device_id: str) -> None:
        """Invalidate pending recognition and remote output at a broker boundary."""
        # This callback runs on paho's thread; conversation state belongs to
        # asyncio. Import lazily because the brain publishes through this service.
        from app.services.conversation.remote_chat import remote_chat_brain

        self._device_generations[device_id] = self._device_generations.get(device_id, 0) + 1

        if self._loop is not None and not self._loop.is_closed():
            with self._pending_lock:
                if device_id in self._resets_pending:
                    return
                self._resets_pending.add(device_id)

            def reset():
                with self.scope.activate():
                    try:
                        remote_chat_brain.reset_device(device_id)
                    finally:
                        with self._pending_lock:
                            self._resets_pending.discard(device_id)

            self._loop.call_soon_threadsafe(reset)

    @staticmethod
    def _parse_device_id(topic: str) -> str | None:
        """Extract the device id from a `/devices/{id}/...` topic."""
        parts = topic.split("/")
        if len(parts) < 4 or parts[1] != "devices":
            return None
        return parts[2] or None

    def _schedule(
        self,
        handler: DeviceEventHandler,
        device_id: str,
        payload: bytes,
        label: str,
    ) -> None:
        """Run an async handler on the asyncio loop from paho's thread."""
        loop = self._loop
        if loop is None or loop.is_closed() or not self._accepting:
            return
        if not self._slots.acquire(blocking=False):
            self.rejected += 1
            logger.error("MQTT capacity exceeded device=%s event=%s", device_id, label)
            self._reject(device_id, payload, label)
            return
        stamp = self._stamp(device_id)

        async def dispatch():
            with self.scope.activate():
                task = asyncio.current_task()
                self.scope.tasks.add(task)
                token = _output_generation.set(stamp)
                try:
                    if self._accepting and stamp == self._stamp(device_id):
                        await handler(device_id, payload)
                finally:
                    _output_generation.reset(token)
                    self.scope.tasks.discard(task)

        coroutine = dispatch()
        try:
            fut = asyncio.run_coroutine_threadsafe(coroutine, loop)
        except RuntimeError:
            coroutine.close()
            self._slots.release()
            return
        with self._pending_lock:
            self._pending.add(fut)

        def _log_result(f: Future) -> None:
            with self._pending_lock:
                self._pending.discard(f)
            self._slots.release()
            if f.cancelled():
                return
            try:
                f.result()
            except Exception:
                logger.exception("%s handling failed for %s", label, device_id)

        fut.add_done_callback(_log_result)

    def _reject(self, device_id, payload, label):
        """Return correlated failure without creating another queued task.

        STT overflow ends the stream and resets its buffered recognition on the
        event loop. Other rejected events are visible in logs and status.
        """
        try:
            if label == "CloudTTSRequest":
                from app.services.speech.synthesis.handler import parse_cloud_tts_request
                from app.services.speech.output import publish_silence
                request = parse_cloud_tts_request(payload)
                publish_silence(device_id, request.event_id, request.chunk_num,
                                markup=request.markup, request_source=0)
            elif label == "zmqSTTRequest":
                from app.services.speech.recognition.handler import parse_stt_request
                from app.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTResponse
                request = parse_stt_request(payload)
                response = zmqSTTResponse(uuid=request.uuid, type=zmqSTTResponse.FINAL,
                    timestamp=int(time.time() * 1000), error_code=66,
                    error_message="MQTT admission capacity exceeded")
                self.publish_proto(settings.COMMAND_TOPIC_TEMPLATE.format(device=device_id), response)
                self._reset_robot_conversation(device_id)
        except Exception:
            logger.exception("Could not return overload failure device=%s event=%s", device_id, label)

    def _dispatch_device_event(
        self,
        device_id: str | None,
        msg: MQTTMessage,
        *,
        event_suffixes: tuple[str, ...],
        handler: DeviceEventHandler | None,
        label: str,
    ) -> bool:
        if handler is None or self._loop is None or not device_id:
            return False
        if not any(msg.topic.endswith(suffix) for suffix in event_suffixes):
            return False
        self._schedule(handler, device_id, bytes(msg.payload), label)
        return True

    def _dispatch_device_state(
        self, device_id: str | None, msg: MQTTMessage,
    ) -> bool:
        """Route `/devices/{id}/state`, but only until the device is bootstrapped."""
        handler = self._device_state_handler
        if handler is None or self._loop is None or not device_id:
            return False
        parts = msg.topic.split("/")
        if len(parts) != 4 or parts[1] != "devices" or parts[3] != "state":
            return False
        # State is published continuously; once bootstrapped there is nothing to
        # do, so don't schedule a coroutine for every heartbeat.
        if device_registry.is_bootstrapped(device_id):
            return True
        self._schedule(handler, device_id, bytes(msg.payload), "device-state")
        return True

    def _dispatch_cloud_tts_request(
        self, device_id: str | None, msg: MQTTMessage,
    ) -> bool:
        """Route a framed `CloudTTSRequest` to the synthesis handler."""
        handler = self._cloud_tts_request_handler
        prefix = self._cloud_tts_request_prefix
        if handler is None or prefix is None or self._loop is None or not device_id:
            return False
        if not msg.payload.startswith(prefix):
            return False

        payload = bytes(msg.payload)
        logger.info(
            "inbound CloudTTSRequest from %s on %s (%d bytes)",
            device_id,
            msg.topic,
            len(payload),
        )
        self._schedule(handler, device_id, payload, "CloudTTSRequest")
        return True

    def _dispatch_stt_request(
        self, device_id: str | None, msg: MQTTMessage,
    ) -> bool:
        """Route a framed `zmqSTTRequest` (streamed mic audio) to the STT handler.

        These frames are high-volume (many per utterance), so unlike the
        CloudTTSRequest path dispatch stays quiet. The handler logs frame byte
        counts at DEBUG and utterance completion/transcription at INFO.
        """
        handler = self._stt_request_handler
        prefix = self._stt_request_prefix
        if handler is None or prefix is None or self._loop is None or not device_id:
            return False
        if not msg.payload.startswith(prefix):
            return False
        self._schedule(handler, device_id, bytes(msg.payload), "zmqSTTRequest")
        return True

    # --- outbound -----------------------------------------------------------

    @staticmethod
    def _frame_proto(message: Message) -> bytes:
        """Frame a proto as `b"<fully.qualified.TypeName>:" + serialized`."""
        prefix = message.DESCRIPTOR.full_name.encode() + b":"
        return prefix + message.SerializeToString()

    def publish_proto(self, topic: str, message: Message) -> bool:
        """Publish a protobuf message using the robot's framing convention."""
        if not self._can_publish(self._parse_device_id(topic)):
            return False
        payload = self._frame_proto(message)
        info = self.client.publish(topic, payload)
        rc = getattr(info, "rc", 0)
        if rc not in (0, None):
            logger.error(
                "mqtt publish failed rc=%s topic=%s type=%s bytes=%d",
                rc,
                topic,
                message.DESCRIPTOR.full_name,
                len(payload),
            )
            return False
        return True

    def send_proto_subscribe(self, device_id: str) -> None:
        """Tell the robot which proto message types to forward to this service."""
        topic = settings.COMMAND_TOPIC_TEMPLATE.format(device=device_id)
        sub = ProtoSubscribe()
        sub.timestamp = int(time.time() * 1000)
        sub.protos.extend(PROTO_SUBSCRIBE_TYPES)
        sub.software_version = settings.SOFTWARE_VERSION
        sub.module_name = settings.MODULE_NAME
        logger.info("publishing ProtoSubscribe to %s: %s", topic, list(sub.protos))
        return self.publish_proto(topic, sub)

    def publish_cloud_tts_response(self, device_id: str, response: Message) -> None:
        """Publish a CloudTTSResponse to `device_id` for playback."""
        topic = settings.COMMAND_TOPIC_TEMPLATE.format(device=device_id)
        logger.info(
            "publishing %s to %s (event_id=%s chunk=%d, %d audio bytes)",
            response.DESCRIPTOR.full_name,
            topic,
            response.event_id,
            response.chunk_num,
            len(response.audio.buffer),
        )
        self.publish_proto(topic, response)

    def publish_command_json(
        self, device_id: str, command: str, payload: dict,
    ) -> None:
        """Publish a JSON command (OpenMoxie's `send_command_to_bot_json`)."""
        if not self._can_publish(device_id):
            return False
        topic = settings.COMMAND_JSON_TOPIC_TEMPLATE.format(
            device=device_id, command=command,
        )
        body = json.dumps(payload)
        logger.info("publishing JSON command to %s (%d bytes)", topic, len(body))
        info = self.client.publish(topic, body)
        return getattr(info, "rc", 0) in (0, None)

    def publish_settings_update(self, device_id: str, config: dict) -> bool:
        """Publish config JSON; return local MQTT acceptance, not robot ACK."""
        if not self._can_publish(device_id):
            return False
        topic = settings.CONFIG_TOPIC_TEMPLATE.format(device=device_id)
        props = config.get("settings", {}).get("props", {})
        logger.info(
            "publishing config to %s (cloud_tts=%s moxie_mode=%s)",
            topic,
            props.get("cloud_tts"),
            config.get("moxie_mode"),
        )
        info = self.client.publish(topic, json.dumps(config))
        rc = getattr(info, "rc", 0)
        if rc not in (0, None):
            logger.error("mqtt config publish failed rc=%s topic=%s", rc, topic)
            return False
        return True


mqtt_service = Local("transport", MqttService)
