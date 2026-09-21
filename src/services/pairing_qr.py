"""OpenMoxie-compatible endpoint and Wi-Fi QR payloads for Moxie.

Moxie reads these values with its face camera while it is in its QR setup
screen. The formats intentionally mirror OpenMoxie's `moxie_server.py`:

* endpoint: JSON debug envelope carrying base64 `ServiceConfiguration2`
* Wi-Fi: ``PA`` followed by base64 `StartPairingQR`

They configure Moxie's own network client; no MQTT connection exists until it
has successfully scanned the relevant code.
"""

from __future__ import annotations

import base64
import json
import socket
from dataclasses import dataclass
from io import BytesIO

import qrcode

from src.config import settings
from src.protos.embodied.logging.Cloud2_pb2 import ServiceConfiguration2
from src.protos.embodied.wifiapp.QRCommands_pb2 import StartPairingQR


@dataclass(frozen=True)
class EndpointQrConfig:
    """Non-secret endpoint settings encoded into an endpoint QR."""

    host: str
    port: int
    disable_verify: bool
    project: str


def _local_lan_ipv4() -> str:
    """Find the IPv4 source address used by this Mac's default route.

    Connecting a UDP socket selects an interface locally without transmitting a
    packet. A QR code is only useful with a LAN-reachable address; loopback is
    explicitly rejected so Moxie is not pointed at itself.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect(("192.0.2.1", 9))
        address = probe.getsockname()[0]
    if address.startswith("127.") or address == "0.0.0.0":
        msg = "could not determine a LAN IPv4 address; set MQTT_PUBLIC_HOST"
        raise RuntimeError(msg)
    return address


def endpoint_qr_config() -> EndpointQrConfig:
    """Resolve the server endpoint that Moxie should use after scanning."""
    host = settings.MQTT_PUBLIC_HOST.strip() or _local_lan_ipv4()
    if host.lower() == "localhost" or host.startswith("127."):
        msg = "MQTT_PUBLIC_HOST must be a LAN address or hostname, not localhost"
        raise ValueError(msg)
    return EndpointQrConfig(
        host=host,
        port=settings.MQTT_PUBLIC_PORT,
        disable_verify=settings.MQTT_QR_DISABLE_VERIFY,
        project=settings.MQTT_QR_PROJECT,
    )


def endpoint_qr_payload(config: EndpointQrConfig | None = None) -> str:
    """Return the exact OpenMoxie endpoint-relocation QR payload string."""
    config = config or endpoint_qr_config()
    service = ServiceConfiguration2()
    service.gcp_project = config.project
    service.mqtt_host = config.host
    service.override_port = config.port
    service.disable_verify = config.disable_verify
    encoded = base64.b64encode(service.SerializeToString()).decode("ascii")
    return json.dumps({"debug": {"command": "om", "param": encoded}})


def wifi_qr_payload(
    *,
    ssid: str,
    password: str,
    hidden: bool = False,
    band: int = StartPairingQR.WifiBandSelect.ANY,
) -> str:
    """Return the OpenMoxie Wi-Fi QR payload without retaining credentials."""
    credentials = StartPairingQR()
    credentials.wifi_only = True
    credentials.ssid = ssid
    credentials.password = password
    credentials.is_hidden = hidden
    credentials.band_select = band
    return "PA" + base64.b64encode(credentials.SerializeToString()).decode("ascii")


def render_qr(payload: str) -> bytes:
    """Encode a payload as a high-contrast PNG for Moxie's face camera."""
    image = qrcode.make(payload)
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


__all__ = [
    "EndpointQrConfig",
    "endpoint_qr_config",
    "endpoint_qr_payload",
    "render_qr",
    "wifi_qr_payload",
]
