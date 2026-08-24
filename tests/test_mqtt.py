"""MQTT framing, ProtoSubscribe contents, and inbound dispatch/routing."""

import asyncio

from src.protos.embodied.unity.CloudTTS_pb2 import CloudTTSRequest
from src.services import mqtt_service as mqtt_module
from src.services.devices import device_registry
from src.services.mqtt_service import PROTO_SUBSCRIBE_TYPES, mqtt_service


def test_proto_subscribe_requests_inbound_request_type() -> None:
    # We must ask the robot to forward the INBOUND request, not the outbound
    # response.
    assert "embodied.unity.CloudTTSRequest" in PROTO_SUBSCRIBE_TYPES
    assert "embodied.unity.CloudTTSResponse" not in PROTO_SUBSCRIBE_TYPES


def test_frame_proto_uses_descriptor_name() -> None:
    req = CloudTTSRequest(markup="hi")
    framed = mqtt_service._frame_proto(req)  # noqa: SLF001
    assert framed.startswith(b"embodied.unity.CloudTTSRequest:")


def test_parse_device_id() -> None:
    parse = mqtt_service._parse_device_id  # noqa: SLF001
    assert parse("/devices/abc123/events/zmq") == "abc123"
    assert parse("/devices/abc123/state") == "abc123"
    assert parse("/nonsense") is None


class _FakeMsg:
    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = topic
        self.payload = payload


async def test_on_message_dispatches_with_device_id() -> None:
    received: list[tuple[str, bytes]] = []

    async def handler(device_id: str, payload: bytes) -> None:
        received.append((device_id, payload))

    loop = asyncio.get_running_loop()
    prefix = CloudTTSRequest.DESCRIPTOR.full_name.encode() + b":"
    mqtt_service.register_cloud_tts_request_handler(loop, handler, prefix)
    try:
        framed = prefix + CloudTTSRequest(markup="Привет").SerializeToString()
        msg = _FakeMsg("/devices/robot-77/events/zmq", framed)
        mqtt_service._on_message(None, None, msg)  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert received == [("robot-77", framed)]
        # Traffic from a robot registers it as a routable target.
        assert device_registry.primary() == "robot-77"
    finally:
        mqtt_service._loop = None  # noqa: SLF001
        mqtt_service._cloud_tts_request_handler = None  # noqa: SLF001
        mqtt_service._cloud_tts_request_prefix = None  # noqa: SLF001


async def test_on_message_ignores_unrelated_payloads() -> None:
    called: list[bytes] = []

    async def handler(device_id: str, payload: bytes) -> None:
        called.append(payload)

    loop = asyncio.get_running_loop()
    prefix = CloudTTSRequest.DESCRIPTOR.full_name.encode() + b":"
    mqtt_service.register_cloud_tts_request_handler(loop, handler, prefix)
    try:
        msg = _FakeMsg("/devices/x/state", b"embodied.power.something")
        mqtt_service._on_message(None, None, msg)  # noqa: SLF001
        await asyncio.sleep(0.02)
        assert called == []
    finally:
        mqtt_service._loop = None  # noqa: SLF001
        mqtt_service._cloud_tts_request_handler = None  # noqa: SLF001
        mqtt_service._cloud_tts_request_prefix = None  # noqa: SLF001


async def test_device_state_dispatch_stops_after_bootstrap() -> None:
    """State is a heartbeat; scheduling work for every one is wasteful."""
    calls: list[str] = []

    async def state_handler(device_id: str, _payload: bytes) -> None:
        calls.append(device_id)
        device_registry.mark_bootstrapped(device_id)

    loop = asyncio.get_running_loop()
    mqtt_service.register_remote_chat_handlers(
        loop,
        remote_chat_handler=state_handler,
        activity_log_handler=state_handler,
        device_state_handler=state_handler,
    )
    try:
        msg = _FakeMsg("/devices/robot-9/state", b'{"battery": 90}')
        mqtt_service._on_message(None, None, msg)  # noqa: SLF001
        await asyncio.sleep(0.05)
        mqtt_service._on_message(None, None, msg)  # noqa: SLF001
        await asyncio.sleep(0.05)
        assert calls == ["robot-9"]
    finally:
        mqtt_service._loop = None  # noqa: SLF001
        mqtt_service._remote_chat_handler = None  # noqa: SLF001
        mqtt_service._activity_log_handler = None  # noqa: SLF001
        mqtt_service._device_state_handler = None  # noqa: SLF001


assert mqtt_module  # module import sanity
