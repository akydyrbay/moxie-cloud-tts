"""Configuration for the Moxie Cloud-TTS service.

Topics and payload shapes below are the ones OpenMoxie's `moxie_server.py` uses,
which is what the robot firmware expects:

- `/devices/{device}/config`             plain JSON device config (cloud_tts lives here)
- `/devices/{device}/commands/zmq`       framed protobuf (ProtoSubscribe, CloudTTSResponse)
- `/devices/{device}/commands/{command}` plain JSON commands (remote_chat, query_result,
                                          telehealth, wakeup, sleep)
- `/devices/{device}/events/...`         robot -> server

The device id is NOT hardcoded: it is learned from the MQTT topics the robot
publishes on (see `src/services/devices.py`). `DEVICE_ID` remains only as an
optional manual override for bench testing without a robot.
"""

from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Load a local .env if present. Without this pydantic-settings ignores .env
    # entirely and OPENAI_API_KEY silently stays empty.
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- MQTT ---------------------------------------------------------------
    # Split into scalars so each can be overridden with a plain env var
    # (a dict setting would require JSON in the environment).
    MQTT_HOST: str = "localhost"
    MQTT_PORT: int = 8883

    # Moxie's MQTT host is set by scanning an OpenMoxie-compatible endpoint QR.
    # Empty means discover this Mac's LAN IPv4 address when the QR is requested.
    # Set this when the computer has multiple LAN adapters or the broker is on
    # another machine; never use localhost because Moxie resolves it to itself.
    MQTT_PUBLIC_HOST: str = ""
    MQTT_PUBLIC_PORT: int = 8883
    MQTT_QR_PROJECT: str = "openmoxie"
    # The bundled broker uses a self-signed certificate. The relocation QR must
    # therefore tell Moxie not to verify the expired/public CA chain.
    MQTT_QR_DISABLE_VERIFY: bool = True

    # Optional manual override. Leave empty in normal operation — the service
    # discovers the real device id from `/devices/{id}/...` topics.
    DEVICE_ID: str = ""

    # Framed-protobuf channel (ProtoSubscribe, CloudTTSResponse).
    COMMAND_TOPIC_TEMPLATE: str = "/devices/{device}/commands/zmq"

    # Device settings/config push (plain JSON).
    CONFIG_TOPIC_TEMPLATE: str = "/devices/{device}/config"

    # JSON command topic (remote_chat, query_result, telehealth, wakeup, sleep).
    COMMAND_JSON_TOPIC_TEMPLATE: str = "/devices/{device}/commands/{command}"

    # Lets this service run alongside a real OpenMoxie backend (which then owns
    # config pushes) instead of standalone.
    OPENMOXIE_ENABLED: bool = False

    SOFTWARE_VERSION: str = "0.1.0"
    MODULE_NAME: str = "moxie-tts-service"

    # --- OpenAI speech pipeline --------------------------------------------
    # Single provider for every stage. Autonomous chat replies in the configured
    # output language, even when firmware supplies an English wake transcript.
    #
    # The API key is read from the OPENAI_API_KEY environment variable (or .env)
    # — never hardcoded, never logged.
    OPENAI_API_KEY: str = ""

    OPENAI_STT_MODEL: str = "gpt-4o-transcribe"
    OPENAI_CHAT_MODEL: str = "gpt-4o-mini"
    OPENAI_TTS_MODEL: str = "gpt-4o-mini-tts"
    OPENAI_TTS_VOICE: str = "alloy"

    # Per-call timeout. Must stay well under the robot's MarkingUp timeout: once
    # the robot gives up waiting, late audio is discarded and it may force-idle.
    OPENAI_REQUEST_TIMEOUT_S: float = 8.0
    # Extra translation stage has its own deadline and no SDK retries.
    OPENAI_TRANSLATION_TIMEOUT_S: float = Field(default=4.0, gt=0)
    # Total STT wait after END, including worker scheduling; no SDK retries.
    OPENAI_STT_TIMEOUT_S: float = Field(default=6.0, gt=0, le=8.0)
    # Recognition language, robot transport language and reply language differ.
    STT_INPUT_LANGUAGE: Literal["auto", "kk", "ru", "en"] = "auto"
    STT_ENGLISH_BRIDGE_ENABLED: bool = True
    # Optional protobuf metadata is off until its firmware behavior is verified.
    STT_BRIDGE_METADATA_ENABLED: bool = False
    # Chat turns count only when the child starts with the wake word; other
    # speech (TV, adults, noise) is answered with an empty FINAL and silence.
    STT_WAKE_WORD_REQUIRED: bool = True
    STT_WAKE_WORDS: list[str] = ["moxie", "moxy", "мокси", "моқси", "моксі", "моксы", "моқши", "макси", "мукси", "moksi", "moksy", "мокши", "мокщи", "моқщи"]  # noqa: E501
    OPENAI_INPUT_TRANSLATION_TIMEOUT_S: float = Field(default=2.0, gt=0, le=6.0)
    OPENAI_CHAT_TIMEOUT_S: float = Field(default=4.0, gt=0)
    OPENAI_TTS_TIMEOUT_S: float = Field(default=8.0, gt=0)
    REMOTE_REPLY_TIMEOUT_S: float = Field(default=12.0, gt=0, le=20.0)
    OPENAI_CHAT_MAX_TOKENS: int = Field(default=256, ge=64, le=512)

    # Persona only. The reply language is appended per turn from
    # DEFAULT_TTS_LANGUAGE (see `openai_chat._language_instruction`).
    OPENAI_SYSTEM_PROMPT: str = (
        "You are Moxie, a kind physical robot talking face to face with a child. "
        "Never call yourself a virtual robot and never invent your location or abilities."
        "Be warm, natural and age-appropriate."
    )

    SUPPORTED_LANGUAGES: list[str] = ["ru", "kk"]
    DEFAULT_TTS_LANGUAGE: Literal["ru", "kk"] = "ru"
    # Translate every English/Latin autonomous TTS line to DEFAULT_TTS_LANGUAGE.
    # No phrase allowlist, fixed greeting, or dependency on chat entry.
    CLOUD_TTS_TRANSLATION_ENABLED: bool = True
    # Briefly admit immediate native selections; never require a notify before
    # audio, since this firmware also notifies after remote audio publication.
    REMOTE_SELECTION_GRACE_S: float = Field(default=0.15, ge=0, le=1)

    # --- STT injection (Goal 1: speak without puppet mode) ------------------
    # On wake the robot first enters its onboard WAKEUP module ("say 'Moxie,
    # let's go!' to start the day") and will not reach OPENMOXIE_CHAT until it
    # hears that trigger. Inject THIS phrase during the entrance phase only.
    # Chat and native activities use real transcription plus the English bridge.
    # Session boundaries keep recognition disabled.
    WAKEUP_TRIGGER_LINE: str = "Moxie, let's go"

    # --- Audio --------------------------------------------------------------
    # OpenAI TTS returns mono 24 kHz audio; the rate is recorded explicitly so
    # AudioBuffer.sample_rate is always truthful about the bytes we send.
    AUDIO_SAMPLE_RATE_HZ: int = 24000
    AUDIO_CHANNELS: int = 1

    # AudioBuffer.buffer content selector.
    #   True  (default) -> raw little-endian PCM16 samples
    #   False           -> locally built WAV (truthful RIFF sizes; never OpenAI's
    #                      streamed `response_format=wav`, which writes 0xFFFFFFFF)
    CLOUD_TTS_AUDIO_RAW_PCM: bool = True

    # Populate CloudTTSResponse.marks with derived Polly-style viseme/word
    # timings. These drive lip movement on the first (heard) line.
    CLOUD_TTS_EMIT_MARKS: bool = True

    # CloudTTSResponse.request_source. "robot" = ROBOT_TTS_REQUEST (0),
    # "remotechat" = REMOTECHAT_TTS_REQUEST (1). Note proto3 omits 0 from the
    # wire, so "robot" means the field is absent and the robot uses its default.
    CLOUD_TTS_REQUEST_SOURCE: str = "robot"

    # --- Startup behaviour --------------------------------------------------
    # Enable cloud_tts automatically so the robot's first config push already
    # has cloud_tts="1". Ignored when no OPENAI_API_KEY is configured, because
    # enabling cloud TTS with nothing able to synthesize stalls the robot in
    # MarkingUp and puts it to sleep.
    CLOUD_TTS_AUTO_ENABLE: bool = True

    # Put the robot into TELEHEALTH ("puppet") mode at startup. In this mode the
    # robot stays awake, ignores the schedule, and speaks only what we push via
    # POST /tts/speak — which is the simplest way to verify cloud TTS audio.
    # Set false to run the conversational remote-chat brain instead.
    PUPPET_MODE_AUTO_ENABLE: bool = False

    # --- /tts/speak sequencing (puppet + cloud_tts) -------------------------
    # After INTERRUPT, pause briefly so the robot can leave its previous output
    # state before we send PLAY_OUTPUT.
    SPEAK_INTERRUPT_PAUSE_S: float = 0.35
    # How long to wait for a telehealth READY report before sending PLAY_OUTPUT
    # anyway. Only applies while the TELEHEALTH session is still coming up: the
    # robot reports READY on entering the session and does not report anything
    # per line, so once it is READY every speak proceeds immediately.
    SPEAK_READY_TIMEOUT_S: float = 4.0
    # When the robot has never reported a telehealth state (first speak / quiet
    # firmware), settle this long instead of blocking on READY.
    SPEAK_SETTLE_S: float = 0.5

    # --- Robot-side logging -------------------------------------------------
    # The robot streams its own log to `events/device-logs`. Re-emitting it is
    # the only way to see what the firmware does with a CloudTTSResponse, since
    # nothing else it publishes reports playback.
    ROBOT_LOG_CAPTURE: bool = True
    # Pushed as the `default_loglevel` device prop, i.e. the level the ROBOT
    # filters at before sending. OpenMoxie documents {info, warning, error,
    # fatal}; "warning" hides most of the speech pipeline. "info" is the most
    # verbose value that set lists. "debug"/"verbose" are not documented and
    # may be ignored by the firmware.
    ROBOT_LOG_LEVEL: str = "info"


settings = Settings()

# Robot "mode" value that enables telehealth/puppet control (OpenMoxie:
# device.robot_config["moxie_mode"] = "TELEHEALTH").
MOXIE_MODE_TELEHEALTH = "TELEHEALTH"

# Remote modules advertised on the modules handshake. Schema matches OpenMoxie
# `RemoteChat.get_modules_info()` — source=REMOTE_CHAT, rules=RANDOM.
DEFAULT_REMOTE_MODULES_INFO: dict = {
    "modules": [
        {
            "info": {"id": "OPENMOXIE_CHAT"},
            "rules": "RANDOM",
            "source": "REMOTE_CHAT",
            "content_infos": [
                {"info": {"id": "default"}},
                {"info": {"id": "short"}},
            ],
        },
    ],
    "version": "openmoxie_v1",
}

# Module/content pairs this brain hosts (OpenMoxie OPENMOXIE_CHAT defaults).
REMOTE_CHAT_MODULE_IDS: frozenset[str] = frozenset(
    {
        "OPENMOXIE_CHAT/default",
        "OPENMOXIE_CHAT/short",
    },
)

# Remote module source text. Translate for the live cloud-enabled router turn;
# these do not replace native wake-up lines with a fixed Kazakh greeting.
REMOTE_CHAT_OPENERS: dict[str, str] = {
    "default": "Hi! What would you like to talk about?",
    "short": "Hi! What would you like to talk about?",
}

# Spoken when a turn cannot be answered (onboard module, or LLM failure).
# Keyed by language so a Russian/Kazakh child never hears an English line.
FALLBACK_LINES: dict[str, str] = {
    "ru": "Извини, я не расслышала. Можешь повторить?",
    "kk": "Кешір, мен естімедім. Қайталай аласың?",
}

# Session schedule. `only_chat` style — skip onboard onboarding modules that
# need Embodied cloud content and go straight into our remote chat module.
DEFAULT_SCHEDULE: dict = {
    "provided_schedule": [
        {"module_id": "OPENMOXIE_CHAT", "content_id": "default"},
    ],
    "chat_request": {
        "module_id": "OPENMOXIE_CHAT",
        "content_id": "default",
    },
}

# Device config pushed on `/devices/{device}/config`. Mirrors OpenMoxie's
# DEFAULT_ROBOT_CONFIG + DEFAULT_ROBOT_SETTINGS. `cloud_tts` and `moxie_mode`
# are deliberately absent here — `device_state.py` overlays them live.
#
# Enrollment skip (docs/SPEAK_LIVE_PLAN.md §4 A): identity is a paired,
# nicknamed mentor, and max_enroll="0" is OpenMoxie's documented disable of
# ENROLLCONVO ("Clearance Check": chocolate vs strawberry). Firmware still
# runs that as a brain-entrance even when DEFAULT_SCHEDULE is only_chat, so
# the prop — not the schedule — is the skip. Full identity is bootstrap-only;
# mid-session toggles must strip child_pii / pairing_status.
HARDCODED_DEVICE_CONFIG: dict = {
    "pairing_status": "paired",
    "audio_volume": 1.0,
    "screen_brightness": 1.0,
    "audio_wake_set": "off",
    "timezone_id": "America/Los_Angeles",
    "child_pii": {"nickname": "Pat", "input_speed": 0.0},
    "settings": {
        "props": {
            "touch_wake": "1",
            "wake_alarms": "1",
            "wake_button": "1",
            "doa_range": "80",
            "target_all": "1",
            "gcp_upload_disable": "1",
            "local_stt": "on",
            "max_enroll": "0",
            "audio_wake": "1",
            "cloud_schedule_reset_threshold": "5",
            "debug_whiteboard": "0",
            "brain_entrances_available": "1",
            "mqtt_files": "0",
            "file_sync_wait": "0",
            "default_loglevel": settings.ROBOT_LOG_LEVEL,
        },
    },
}
