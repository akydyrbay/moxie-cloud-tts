#!/usr/bin/env python3
"""Fake Moxie for verifying the cloud-TTS loop without the real robot.

Impersonates the robot side of the protocol: announces itself, accepts the
config push, honours ProtoSubscribe, and — when told to speak via telehealth —
emits a real `CloudTTSRequest` and waits for the matching `CloudTTSResponse`.

    Terminal 1:  mise start
    Terminal 2:  uv run python scripts/simulate_robot.py
    Terminal 3:  curl -X POST localhost:8000/tts/speak \
                   -H 'content-type: application/json' \
                   -d '{"text": "Привет! Как дела?"}'

The received audio is written to `local/work/moxie_sim_<event_id>.wav` so it can
be played back on the laptop, which answers the one question a log line cannot:
does the service produce audio a player actually accepts?

Exit code is 0 only if at least one response was received and correlated.
"""

import argparse
import json
import logging
import pathlib
import ssl
import sys
import time
import uuid

from paho.mqtt.client import CallbackAPIVersion, Client

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from src.protos.embodied.unity.CloudTTS_pb2 import (  # noqa: E402
    CloudTTSRequest,
    CloudTTSResponse,
)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s",
)
logger = logging.getLogger("moxie-sim")

OUT_DIR = pathlib.Path(__file__).resolve().parent.parent / "local" / "work"
RESPONSE_PREFIX = b"embodied.unity.CloudTTSResponse:"


class FakeMoxie:
    def __init__(self, device_id: str, host: str, port: int) -> None:
        self.device_id = device_id
        self.host = host
        self.port = port
        self.cloud_tts: str | None = None
        self.moxie_mode: str | None = None
        self.subscribed_protos: list[str] = []
        self.pending: dict[str, float] = {}
        self.received: list[tuple[str, bytes]] = []

        # A real Moxie uses its device id as the MQTT client id, which is how
        # the service spots it in the broker log and re-pushes config after a
        # reboot. Matching that here means only one simulator may run at a time:
        # two clients sharing an id evict each other in an endless reconnect
        # loop that silently drops messages.
        self.client = Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=device_id,
        )
        self.client.tls_set(cert_reqs=ssl.CERT_NONE)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

    # --- robot -> server ----------------------------------------------------

    def _topic(self, suffix: str) -> str:
        return f"/devices/{self.device_id}/{suffix}"

    def announce(self) -> None:
        """Publish state, which is what makes the service bootstrap us."""
        self.client.publish(
            self._topic("state"),
            json.dumps({"battery_level": 88, "sim": True}),
        )
        logger.info("announced state as %s", self.device_id)

    def send_cloud_tts_request(self, markup: str) -> str:
        """Ask the cloud to voice `markup`, exactly as the robot would."""
        event_id = str(uuid.uuid4())
        req = CloudTTSRequest(
            markup=markup,
            event_id=event_id,
            chunk_num=0,
            user_id="sim-user",
        )
        framed = (
            CloudTTSRequest.DESCRIPTOR.full_name.encode()
            + b":"
            + req.SerializeToString()
        )
        self.pending[event_id] = time.monotonic()
        self.client.publish(self._topic("events/zmq"), framed)
        logger.info("-> CloudTTSRequest event_id=%s markup=%r", event_id, markup[:60])
        return event_id

    # --- server -> robot ----------------------------------------------------

    def _on_connect(self, client, _u, _f, reason_code, _p=None) -> None:  # noqa: ANN001
        logger.info("connected to broker (%s)", reason_code)
        client.subscribe(self._topic("commands/#"))
        client.subscribe(self._topic("config"))

    def _on_message(self, _c, _u, msg) -> None:  # noqa: ANN001
        if msg.topic.endswith("/config"):
            self._on_config(msg.payload)
        elif msg.topic.endswith("/commands/zmq"):
            self._on_zmq(msg.payload)
        elif msg.topic.endswith("/commands/telehealth"):
            self._on_telehealth(msg.payload)
        else:
            logger.info("<- %s: %s", msg.topic, msg.payload[:120])

    def _on_config(self, payload: bytes) -> None:
        cfg = json.loads(payload)
        self.cloud_tts = cfg.get("settings", {}).get("props", {}).get("cloud_tts")
        self.moxie_mode = cfg.get("moxie_mode")
        logger.info(
            "<- config: cloud_tts=%s moxie_mode=%s",
            self.cloud_tts,
            self.moxie_mode,
        )

    def _on_zmq(self, payload: bytes) -> None:
        if payload.startswith(b"embodied.logging.ProtoSubscribe:"):
            logger.info("<- ProtoSubscribe")
            self.subscribed_protos.append("ok")
            return
        if payload.startswith(RESPONSE_PREFIX):
            self._on_tts_response(payload)
            return
        logger.info("<- unknown zmq frame: %s", payload[:60])

    def _on_tts_response(self, payload: bytes) -> None:
        resp = CloudTTSResponse()
        resp.ParseFromString(payload[len(RESPONSE_PREFIX):])
        started = self.pending.pop(resp.event_id, None)
        audio = resp.audio.buffer
        container = "WAV" if audio[:4] == b"RIFF" else "raw/other"

        if started is None:
            logger.error(
                "<- CloudTTSResponse for UNKNOWN event_id=%s — the real robot "
                "would DISCARD this audio",
                resp.event_id,
            )
            return

        logger.info(
            "<- CloudTTSResponse event_id=%s chunk=%d %s %d bytes "
            "%d Hz %dch after %d ms",
            resp.event_id,
            resp.chunk_num,
            container,
            len(audio),
            resp.audio.sample_rate,
            resp.audio.channels,
            int((time.monotonic() - started) * 1000),
        )
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        ext = "wav" if container == "WAV" else "pcm"
        out = OUT_DIR / f"moxie_sim_{resp.event_id[:8]}.{ext}"
        out.write_bytes(audio)
        logger.info("   saved audio to %s", out)
        self.received.append((resp.event_id, audio))

    def _on_telehealth(self, payload: bytes) -> None:
        msg = json.loads(payload)
        body = msg.get("message", {})
        action = body.get("action")
        logger.info("<- telehealth %s", action)
        if action != "PLAY_OUTPUT":
            return
        output = body.get("output", {})
        speech = output.get("markup") or output.get("text") or ""
        if self.cloud_tts != "1":
            logger.warning(
                "cloud_tts is %r — a real robot would speak this with its own "
                "voice and never call the cloud",
                self.cloud_tts,
            )
            return
        # This is what the firmware does with cloud_tts enabled.
        self.send_cloud_tts_request(speech)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # Real Moxie device ids look like `d_<uuid>` (hex only) and double as the
    # MQTT client id; the service's broker-log connect detection keys off that.
    parser.add_argument(
        "--device-id", default="d_51ada1ed-0000-4000-8000-00000000d001",
    )
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8883)
    parser.add_argument(
        "--say",
        help="Immediately request synthesis of this text, then exit.",
    )
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    bot = FakeMoxie(args.device_id, args.host, args.port)
    bot.client.connect(args.host, args.port, 60)
    bot.client.loop_start()
    time.sleep(1.0)
    bot.announce()
    time.sleep(1.5)

    if args.say:
        bot.send_cloud_tts_request(args.say)
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline and not bot.received:
            time.sleep(0.2)
        bot.client.loop_stop()
        if not bot.received:
            logger.error("no CloudTTSResponse within %.0fs", args.timeout)
            return 1
        return 0

    logger.info(
        "waiting for telehealth PLAY_OUTPUT — now run: "
        "curl -X POST localhost:8000/tts/speak -H 'content-type: application/json' "
        "-d '{\"text\": \"Привет!\"}'",
    )
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("received %d responses", len(bot.received))
        bot.client.loop_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
