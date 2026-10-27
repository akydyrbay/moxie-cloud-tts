"""Service entrypoint.

Origin: the `lifespan` pattern (connect MQTT on startup, disconnect on
shutdown, `loop_start()`/`loop_stop()`) is the same shape as your existing
`main.py` / `moxie/src/main.py`. Extended here to mount the `router/tts.py`
routes so they show up in Swagger at /docs.

Also registers the remote-chat brain so the robot's modules/schedule handshake
and conversation turns are answered (required before cloud TTS can work).

With `CLOUD_TTS_AUTO_ENABLE=true` (default) cloud_tts is turned on in memory
before MQTT connects, so the robot's first config push already has
cloud_tts="1" — no separate HTTP toggle needed. It is skipped when no OpenAI key
is configured, since enabling cloud TTS with nothing able to synthesize would
leave the robot mute.

Run with:
    mise start   # broker + service (recommended)
    mise dev     # service only (broker already running)
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from src.config import settings
from src.logging_conf import configure_logging
from src.router.tts import router as tts_router
from src.services import face
from src.services.cloud_tts_request import (
    CLOUD_TTS_REQUEST_PREFIX,
    handle_payload,
)
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.mqtt_service import mqtt_service
from src.services.openai_client import get_client
from src.services.remote_chat import (
    handle_activity_log_payload,
    handle_remote_chat_payload,
    remote_chat_brain,
)
from src.services.stt_request import (
    STT_REQUEST_PREFIX,
    handle_payload as handle_stt_payload,
)

configure_logging()
logger = logging.getLogger(__name__)


async def _on_device_state(device_id: str, _payload: bytes) -> None:
    """Bootstrap config/ProtoSubscribe the first time we see device state."""
    await remote_chat_brain.ensure_device_bootstrapped(device_id)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    loop = asyncio.get_running_loop()

    # 1. Milestone 2: let the MQTT service route inbound CloudTTSRequest
    # messages (delivered on paho's network thread) into the async synthesis
    # pipeline on THIS running event loop.
    mqtt_service.register_cloud_tts_request_handler(
        loop,
        handle_payload,
        CLOUD_TTS_REQUEST_PREFIX,
    )

    # 1b. Answer the robot's zmqSTT stream. Until OPENMOXIE_CHAT is active,
    # inject WAKEUP_TRIGGER_LINE so the onboard "start the day" gate opens.
    # After chat entry, accumulate microphone PCM and transcribe it with OpenAI.
    # Robot replies still travel via remote-chat + Cloud TTS.
    mqtt_service.register_stt_request_handler(
        loop,
        handle_stt_payload,
        STT_REQUEST_PREFIX,
    )

    # 2. Remote-chat brain: answer modules/schedule and conversation turns so
    # the robot can enter a real turn and later emit CloudTTSRequest.
    mqtt_service.register_remote_chat_handlers(
        loop,
        handle_remote_chat_payload,
        handle_activity_log_payload,
        device_state_handler=_on_device_state,
    )

    # 3. Auto-enable cloud_tts in memory once the brain is ready. The robot
    # gets this on its first bootstrap config push (no manual HTTP needed).
    if settings.CLOUD_TTS_AUTO_ENABLE and not settings.OPENAI_API_KEY:
        logger.error(
            "CLOUD_TTS_AUTO_ENABLE is set but OPENAI_API_KEY is empty — leaving "
            "cloud_tts=0 so Moxie keeps its onboard voice instead of going mute. "
            "Set OPENAI_API_KEY in .env, then POST /tts/cloud-tts {enabled:true}.",
        )
    elif settings.CLOUD_TTS_AUTO_ENABLE:
        await device_state.set_cloud_tts(True)
        logger.info(
            "CLOUD_TTS_AUTO_ENABLE: cloud_tts set to 1 "
            "(will push when the robot connects)",
        )

    # 3b. Build the OpenAI client now rather than inside the first
    # CloudTTSRequest. Constructing it costs ~2 s (import + TLS setup), and the
    # robot is already waiting for audio by then.
    if settings.OPENAI_API_KEY:
        try:
            await run_in_threadpool(get_client)
        except Exception:
            logger.exception("could not pre-warm the OpenAI client")

    # 3c. Restore the saved face so the robot's bootstrap config carries it.
    saved_face = face.load()
    if saved_face:
        await device_state.set_face(saved_face["face_options"], saved_face["child_id"])
        logger.info("restored Moxie face options %s", saved_face["face_options"])

    if settings.PUPPET_MODE_AUTO_ENABLE:
        await device_state.set_puppet_mode(True)
        logger.info(
            "PUPPET_MODE_AUTO_ENABLE: moxie_mode=TELEHEALTH "
            "(robot stays awake and speaks only via POST /tts/speak)",
        )

    # 4. Connect MQTT and start paho's background network loop
    mqtt_service.connect()

    yield

    # 5. Clean up MQTT on app shutdown
    for device_id in device_registry.known():
        remote_chat_brain.reset_device(device_id)
    mqtt_service.disconnect()


app = FastAPI(
    title="Moxie Cloud-TTS Toggle Service",
    lifespan=lifespan,
)
app.include_router(tts_router)
