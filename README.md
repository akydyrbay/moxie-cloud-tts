# Moxie Cloud TTS

Replacement cloud backend for the Moxie robot after Embodied Inc. shut down its
servers. It speaks the robot's own MQTT/protobuf protocol, so Moxie boots into a
working session instead of failing authentication, and it voices the robot with
OpenAI text-to-speech — including Russian and Kazakh, which the original service
never supported.

Current milestone: **push text over HTTP, hear it from the robot.** Speech
recognition is deliberately not wired up yet (see [Roadmap](#roadmap)).

## Requirements

- Docker (for the MQTT broker)
- [uv](https://docs.astral.sh/uv/) and Python 3.12
- An OpenAI API key
- A Moxie on the same network, pointed at this machine's broker

Works on macOS (Intel and Apple Silicon) and Linux — everything is pure Python
plus a multi-arch mosquitto image.

## Setup

```bash
uv sync
echo 'OPENAI_API_KEY=sk-...' > .env
```

## Run

```bash
mise start          # MQTT broker + service
```

Swagger UI: <http://localhost:8000/docs>

Power on Moxie and watch for these lines:

```
robot d_xxxxxxxx-... connected to broker
discovered device d_xxxxxxxx-...
bootstrapping device d_xxxxxxxx-... (cloud_tts=1 moxie_mode=None)
```

Put it in puppet mode so it stays awake and only says what you send:

```bash
curl -X POST localhost:8000/tts/puppet \
  -H 'content-type: application/json' -d '{"enabled": true}'
```

Then make it talk:

```bash
curl -X POST localhost:8000/tts/speak \
  -H 'content-type: application/json' \
  -d '{"text": "Привет! Как у тебя дела сегодня?"}'

curl -X POST localhost:8000/tts/speak \
  -H 'content-type: application/json' \
  -d '{"text": "Сәлем! Бүгін көңіл-күйің қалай?"}'
```

## No robot handy?

`scripts/simulate_robot.py` impersonates the robot side of the protocol and
writes the audio it receives to `local/work/*.wav`, so the whole loop can be
verified (and the audio played back) on the laptop alone:

```bash
mise start     # terminal 1
mise sim       # terminal 2
curl -X POST localhost:8000/tts/speak \
  -H 'content-type: application/json' -d '{"text": "Привет!"}'
```

## How speaking works

Moxie only plays cloud audio that answers a request it is currently waiting on,
matched by `event_id`. A server cannot simply push audio at it. So `/tts/speak`
asks the robot to speak, and answers the request the robot raises in return:

```
POST /tts/speak {"text": "Привет!"}
   │
   ├─ telehealth PLAY_OUTPUT ─────────────────► Moxie
   │                                              │  cloud_tts=1, so it asks
   │                                              │  the cloud for the voice
   ◄── CloudTTSRequest{markup, event_id=E} ───────┘
   │
   ├─ strip markup → OpenAI TTS → raw PCM 24 kHz mono
   │
   └─ CloudTTSResponse{event_id=E, audio} ─────► Moxie plays it
```

Every request gets exactly one response. If synthesis fails, a short silence is
published instead of nothing — with `cloud_tts` on, a missing response leaves
the robot stuck waiting until it force-idles and sleeps.

## Endpoints

| Endpoint | Purpose |
| --- | --- |
| `POST /tts/speak` | Make Moxie say Russian/Kazakh text |
| `POST /tts/puppet` | Toggle TELEHEALTH mode (awake, no schedule) |
| `POST /tts/cloud-tts` | Toggle cloud TTS on the robot |
| `POST /tts/interrupt` | Stop current playback |
| `POST /tts/wakeup` | Wake a sleeping robot |
| `GET /tts/devices` | Robots seen on the broker |
| `POST /tts/speak-direct` | Bench test of synthesis + publish only |
| `POST /tts/transcribe` | Bench test of OpenAI STT |
| `POST /tts/respond` | Bench test of OpenAI chat |

## Configuration

Everything is environment variables or `.env`; see `src/config.py`.

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | Required. Never hardcode it. |
| `CLOUD_TTS_AUTO_ENABLE` | `true` | Enable cloud TTS at startup. Skipped without a key. |
| `PUPPET_MODE_AUTO_ENABLE` | `false` | Start the robot in TELEHEALTH mode. |
| `CLOUD_TTS_AUDIO_RAW_PCM` | `true` | `true` sends raw PCM16 (what `AudioBuffer` is); `false` wraps WAV. |
| `DEVICE_ID` | — | Optional override. Normally the device is auto-discovered. |
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `8883` | Broker address. |
| `OPENAI_REQUEST_TIMEOUT_S` | `8.0` | Must stay under the robot's wait timeout. |

## Tests

```bash
mise test
```

## Roadmap

Speech recognition is next. Moxie already does the hard part: it runs voice
activity detection on-device and streams microphone audio as `zmqSTTRequest`
frames (16 kHz mono PCM16), flagging `END_OF_SPEECH` itself, then expects a
`zmqSTTResponse` with the transcript. Wiring that to OpenAI is the remaining
work, plus confirming the robot's recognizer settings accept Russian/Kazakh.

## Documents
