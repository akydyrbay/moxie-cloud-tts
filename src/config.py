"""Configuration for the Moxie Cloud-TTS service.

Topics and payload shapes below are the ones OpenMoxie's `moxie_server.py` uses,
which is what the robot firmware expects:

- `/devices/{device}/config`             plain JSON device config (cloud_tts lives here)
- `/devices/{device}/commands/zmq`       framed protobuf (ProtoSubscribe, CloudTTSResponse)
- `/devices/{device}/commands/{command}` plain JSON commands (remote_chat, query_result,
                                          telehealth, wakeup)
- `/devices/{device}/events/...`         robot -> server

The device id is NOT hardcoded: it is learned from the MQTT topics the robot
publishes on (see `src/services/devices.py`). `DEVICE_ID` remains only as an
optional manual override for bench testing without a robot.
"""

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
    MQTT_PROJECT: str = "openmoxie"

    # Optional manual override. Leave empty in normal operation — the service
    # discovers the real device id from `/devices/{id}/...` topics. Setting this
    # only forces a target for bench commands when no robot has been seen yet.
    DEVICE_ID: str = ""

    # Framed-protobuf channel (ProtoSubscribe, CloudTTSResponse).
    COMMAND_TOPIC_TEMPLATE: str = "/devices/{device}/commands/zmq"

    # Device settings/config push (plain JSON).
    CONFIG_TOPIC_TEMPLATE: str = "/devices/{device}/config"

    # JSON command topic (remote_chat, query_result, telehealth, wakeup).
    COMMAND_JSON_TOPIC_TEMPLATE: str = "/devices/{device}/commands/{command}"

    # Lets this service run alongside a real OpenMoxie backend (which then owns
    # config pushes) instead of standalone.
    OPENMOXIE_ENABLED: bool = False

    # In-process ZMQ PUB address used to decouple "the Swagger endpoint received
    # a toggle" from "the message actually goes out over MQTT".
    ZMQ_PUB_ADDR: str = "tcp://127.0.0.1:5560"

    SOFTWARE_VERSION: str = "0.1.0"
    MODULE_NAME: str = "moxie-tts-service"

    # --- OpenAI speech pipeline --------------------------------------------
    # Single provider for every stage. No translation stage: the user speaks
    # Russian or Kazakh and OpenAI replies in the same language.
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

    OPENAI_SYSTEM_PROMPT: str = (
        "You are Moxie, a friendly companion robot talking with a child. "
        "Always reply in the SAME language the user used (Russian or Kazakh). "
        "Never translate to English. Keep replies short, warm, natural and "
        "age-appropriate."
    )

    SUPPORTED_LANGUAGES: list[str] = ["ru", "kk"]
    DEFAULT_TTS_LANGUAGE: str = "ru"

    # --- Audio --------------------------------------------------------------
    # OpenAI TTS returns mono 24 kHz audio; the rate is recorded explicitly so
    # AudioBuffer.sample_rate is always truthful about the bytes we send.
    AUDIO_SAMPLE_RATE_HZ: int = 24000
    AUDIO_CHANNELS: int = 1

    # AudioBuffer.buffer content selector.
    #   False (default) -> full WAV bytes (RIFF container)   <- what Moxie gets
    #   True            -> raw little-endian PCM16 samples   <- fallback
    # OpenAI produces either directly via `response_format` (wav vs pcm), so no
    # header surgery is needed. Flip to True only if WAV turns out to be wrong.
    CLOUD_TTS_AUDIO_RAW_PCM: bool = False

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
    # state before we wait for READY / send PLAY_OUTPUT.
    SPEAK_INTERRUPT_PAUSE_S: float = 0.35
    # How long to wait for a telehealth READY report before sending PLAY_OUTPUT
    # anyway. Covers the gap after a prior cloud line finishes (or is interrupted).
    SPEAK_READY_TIMEOUT_S: float = 4.0
    # When the robot has never reported a telehealth state (first speak / quiet
    # firmware), settle this long instead of blocking on READY.
    SPEAK_SETTLE_S: float = 0.5


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

# Opener lines when the robot sends command=prompt for a remote module.
# `|` separates alternatives; one is picked at random.
REMOTE_CHAT_OPENERS: dict[str, str] = {
    "default": (
        "Привет! Давай поговорим. О чём ты хочешь поболтать?"
        "|Привет! Что у тебя на уме сегодня?"
    ),
    "short": (
        "Привет! О чём подумаем сегодня?"
        "|Давай поболтаем! Что интересного?"
    ),
}

# Spoken when a turn cannot be answered (onboard module, or LLM failure).
# Keyed by language so a Russian/Kazakh child never hears an English line.
FALLBACK_LINES: dict[str, str] = {
    "ru": "Извини, я не расслышала. Можешь повторить?",
    "kk": "Кешір, мен естімедім. Қайталай аласың ба?",
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
            "max_enroll": "2",
            "audio_wake": "1",
            "cloud_schedule_reset_threshold": "5",
            "debug_whiteboard": "0",
            "brain_entrances_available": "1",
            "mqtt_files": "0",
            "file_sync_wait": "0",
            "default_loglevel": "warning",
        },
    },
}
