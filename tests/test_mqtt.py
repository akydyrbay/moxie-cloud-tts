"""MQTT framing, ProtoSubscribe contents, and inbound dispatch/routing."""

import asyncio
import json
import logging

from src.config import HARDCODED_DEVICE_CONFIG, settings
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


class TestRobotLogCapture:
    """`events/device-logs` is the robot's own log stream.

    It is the only signal that reports what the firmware does with a published
    CloudTTSResponse, so it must reach the log intact. Truncating it (as the
    generic payload dump did, at 64 bytes) cuts every entry off inside the JSON
    envelope, before the message.
    """

    topic = "/devices/robot-1/events/device-logs"

    def _emit(self, caplog, payload: bytes) -> list:
        caplog.clear()
        with caplog.at_level(logging.DEBUG, logger="src.services.mqtt_service"):
            mqtt_service._on_message(None, None, _FakeMsg(self.topic, payload))  # noqa: SLF001
        return caplog.records

    def test_message_survives_in_full(self, caplog) -> None:
        message = (
            "SpeechOutput: finished utterance, releasing slot after "
            + "x" * 300
        )
        payload = json.dumps({
            "timestamp": "1787642385.758",
            "level": "I",
            "tag": "Unity   ",
            "pid": 4242,
            "message": message,
        }).encode()

        (record,) = self._emit(caplog, payload)
        assert message in record.getMessage()
        assert "Unity" in record.getMessage()

    def test_robot_level_is_kept_in_the_line(self, caplog) -> None:
        """Every robot line is re-emitted at INFO; the original level is in the text."""
        for robot_level in ("I", "W", "E", "F", "D"):
            payload = json.dumps(
                {"level": robot_level, "tag": "t", "message": "m"},
            ).encode()
            (record,) = self._emit(caplog, payload)
            assert record.levelno == logging.INFO, robot_level
            assert f"[{robot_level}]" in record.getMessage()

    def test_json_array_payload_emits_every_entry(self, caplog) -> None:
        payload = json.dumps([
            {"level": "I", "tag": "Unity", "message": "first"},
            {"level": "W", "tag": "Unity", "message": "second"},
        ]).encode()
        records = self._emit(caplog, payload)
        texts = [r.getMessage() for r in records]
        assert any("first" in t for t in texts)
        assert any("second" in t for t in texts)

    def test_unparsable_payload_is_still_reported(self, caplog) -> None:
        (record,) = self._emit(caplog, b"not json at all")
        assert "not json at all" in record.getMessage()

    def test_capture_can_be_switched_off(self, caplog, monkeypatch) -> None:
        monkeypatch.setattr(settings, "ROBOT_LOG_CAPTURE", False)
        payload = json.dumps({"level": "I", "tag": "t", "message": "m"}).encode()
        assert self._emit(caplog, payload) == []


class TestRobotStatusCapture:
    topic = "/devices/robot-1/events/client-service-robot-status"

    def test_status_is_logged_on_change(self, caplog) -> None:
        mqtt_service._last_robot_status.clear()  # noqa: SLF001
        payload = b'{"state":"SPEAKING","slot":1}'
        with caplog.at_level(logging.INFO, logger="src.services.mqtt_service"):
            mqtt_service._on_message(  # noqa: SLF001
                None, None, _FakeMsg(self.topic, payload),
            )
            mqtt_service._on_message(  # noqa: SLF001
                None, None, _FakeMsg(self.topic, payload),
            )
            mqtt_service._on_message(  # noqa: SLF001
                None, None, _FakeMsg(self.topic, b'{"state":"IDLE","slot":0}'),
            )
        lines = [r.getMessage() for r in caplog.records if "robot-status" in r.getMessage()]
        assert len(lines) == 2
        assert "SPEAKING" in lines[0]
        assert "IDLE" in lines[1]


def test_robot_log_level_is_pushed_to_the_device() -> None:
    """The robot filters before sending; at "warning" its speech logs never ship."""
    props = HARDCODED_DEVICE_CONFIG["settings"]["props"]
    assert props["default_loglevel"] == settings.ROBOT_LOG_LEVEL


def test_bootstrap_config_marks_enrollment_done() -> None:
    """ENROLLCONVO is skipped by firmware when max_enroll is 0 (OpenMoxie docs)."""
    props = HARDCODED_DEVICE_CONFIG["settings"]["props"]
    assert props["max_enroll"] == "0"
    assert HARDCODED_DEVICE_CONFIG["pairing_status"] == "paired"
    assert HARDCODED_DEVICE_CONFIG["child_pii"]["nickname"]


assert mqtt_module  # module import sanity
