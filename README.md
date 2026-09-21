# Moxie Cloud TTS

Replacement cloud backend for the Moxie robot after Embodied Inc. shut down its
servers. It speaks the robot's own MQTT/protobuf protocol, so Moxie boots into a
working session instead of failing authentication, and it voices the robot with
OpenAI text-to-speech — including Russian and Kazakh, which the original service
never supported.

Current milestone: **wake Moxie outside puppet mode and hear “Сәлем! Қалайсың?”
with mouth movement.** The server implementation is ready for on-robot
verification; a published response alone does not prove playback. Real speech
recognition is not wired up yet (see [Roadmap](#roadmap)).

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

## Run: Kazakh after wake

Keep the existing `OPENAI_API_KEY` in `.env`. These are the greeting defaults;
set them explicitly there if you have overridden them previously:

```dotenv
PUPPET_MODE_AUTO_ENABLE=false
CLOUD_TTS_AUTO_ENABLE=true
CLOUD_TTS_AUDIO_RAW_PCM=true
CLOUD_TTS_EMIT_MARKS=true
CLOUD_TTS_REQUEST_SOURCE=robot
WAKEUP_TTS_OVERRIDE_ENABLED=true
WAKEUP_TTS_TEXT="Сәлем! Қалайсың?"
```

```bash
mise start          # MQTT broker + service
```

Swagger UI: <http://localhost:8000/docs>

## Connect Moxie with a QR code

Moxie's QR setup screen appears before it has an MQTT connection, so it cannot
be discovered or bootstrapped yet. This service renders the same QR formats
used by OpenMoxie.

If Moxie is not already on this Wi-Fi network, open Swagger at
<http://localhost:8000/docs>, call `POST /tts/wifi-qr` with the Wi-Fi SSID and
password, and show the returned PNG fullscreen to Moxie's face camera. Use
`band: "2.4ghz"` for a 2.4 GHz-only network, `"5ghz"` for 5 GHz-only, or
`"any"` when both are available. The password is used only to make the image;
it is not returned or logged.

Then open the following URL in a browser on this Mac and show the PNG fullscreen
to Moxie's face camera:

```text
http://localhost:8000/tts/endpoint-qr
```

It configures Moxie to use `192.168.8.71:8883` and disables certificate
verification for this local, self-signed broker. The image contains no OpenAI
key or Wi-Fi password. If you want to show it on another screen, save or
screenshot the PNG first; `mise start` binds the HTTP API to this Mac's
localhost. Confirm the current encoded settings first at:

```text
http://localhost:8000/tts/endpoint-qr/info
```

If this Mac's LAN address changes, set the replacement before `mise start`:

```dotenv
MQTT_PUBLIC_HOST=192.168.8.71
MQTT_PUBLIC_PORT=8883
MQTT_QR_DISABLE_VERIFY=true
```

Do not use `localhost` for `MQTT_PUBLIC_HOST`: from Moxie, that address means
the robot itself. After Moxie scans the Wi-Fi and endpoint codes, the service
should log `robot d_… connected to broker`, `discovered device d_…`, and
`bootstrapping device d_…`.

Start the service, then reboot Moxie for a fresh session. Watch for these lines:

```
robot d_xxxxxxxx-... connected to broker
discovered device d_xxxxxxxx-...
bootstrapping device d_xxxxxxxx-... (cloud_tts=1 moxie_mode=None)
```

Wake with the button/touch, or, once the service has discovered the robot:

```bash
curl -X POST localhost:8000/tts/wakeup
```

This uses **Path A1 + A2** from the task's `prompt.md`:

- The entrance `GLOBAL_COMMAND` and `OPENMOXIE_CHAT` opener contain the configured
  greeting inside `<usel variant="2" genre="none">`.
- If firmware requests its local `Good morning!` or `Excellent!` instead,
  the server synthesizes the greeting **for that existing request**. `Good
  morning!` is included so the first observed wake-up line can be Kazakh before
  the STT gate. The confirmation remains a fallback if that greeting is skipped.
- The response echoes the original `event_id`/`chunk_num`, preserves original
  `cmd:` animation marks, and derives mouth movements from the new spoken text.
  The override is disabled in TELEHEALTH and matches only these complete lines
  (including the captured `Excellent! " "` quote debris).

If Moxie asks you to say “Moxie, let's go”, say it and pause. The existing wake
gate answers `END_OF_SPEECH` with that trigger; the greeting is never used as
an STT transcript. The `/tts/wakeup` command itself only unsleeps the robot.
For this test, keep puppet off and use the wake-up flow above.

To change the greeting, set `WAKEUP_TTS_TEXT` to another plain Cyrillic line
(Russian example: `"Привет! Как у тебя дела?"`), restart the service and reboot
Moxie. Kazakh is logged as `kk`. Set `WAKEUP_TTS_OVERRIDE_ENABLED=false` to
disable the replacement of local English lines; the brain still requests the
configured greeting.

### Verify on the robot

Watch `mise start` or filter the log:

```bash
tail -f log/debug.log | rg --line-buffered 'bootstrapping device|CloudTTSRequest|spoke event_id|SILENT|synthesis failed'
```

For a native Cyrillic request, expect `CloudTTSRequest ... speakable='Сәлем!
Қалайсың?'`, followed by `spoke event_id=... language=kk audio_bytes=...` for
the **same ID and chunk**. With the A2 override the incoming request still
contains English, and the log explicitly distinguishes the replacement:

```text
CloudTTSRequest wakeup override ... event_id='E' chunk=0 requested_text='Good morning!' synthesis_text='Сәлем! Қалайсың?'
handling CloudTTSRequest ... event_id='E' chunk=0 ... language=kk synthesis_text='Сәлем! Қалайсың?'
spoke event_id=E chunk=0 language=kk audio_bytes=... marks=... raw_pcm=True ...
```

These are expected log shapes, not an on-device success claim. For A2, do not
mistake the replacement text for Cyrillic markup received from the robot:
the strict Cyrillic-inbound check in `prompt.md` is met only if A1 is honoured.
The override uses a different route into the same live audio slot.

Success requires **hearing the complete greeting and seeing mouth movement**.
`notify`, `Tx remote_chat`, or a `SILENT fallback` is not success. If only
unmatched English lines appear, record their `speakable=` text and event IDs;
this narrow override does not translate arbitrary onboard scripts. If the FSM
reports an empty module (`/_get_current_content_id`), reboot before retrying.

## Puppet speech (separate operator tool)

The first puppet line is proven; subsequent lines in the same session remain
unreliable. This is not used for the wake-up milestone. To test puppet separately:

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
matched by `event_id` and `chunk_num`. The wake-up path answers that request
directly, with the configured substitution described above. A server cannot
simply push audio at it. In the separate puppet path, `/tts/speak`
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
| `POST /tts/speak` | Puppet PLAY_OUTPUT for Russian/Kazakh text |
| `POST /tts/puppet` | Toggle TELEHEALTH mode (awake, no schedule) |
| `POST /tts/cloud-tts` | Toggle cloud TTS on the robot |
| `POST /tts/interrupt` | Stop current playback |
| `POST /tts/wakeup` | Wake a sleeping robot |
| `POST /tts/sleep` | Put an awake robot to sleep |
| `GET /tts/devices` | Robots seen on the broker |

## Configuration

Everything is environment variables or `.env`; see `src/config.py`.

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | Required. Never hardcode it. |
| `CLOUD_TTS_AUTO_ENABLE` | `true` | Enable cloud TTS at startup. Skipped without a key. |
| `PUPPET_MODE_AUTO_ENABLE` | `false` | Start the robot in TELEHEALTH mode. |
| `WAKEUP_TTS_OVERRIDE_ENABLED` | `true` | Replace known local wake-up lines with the greeting, outside TELEHEALTH only. |
| `WAKEUP_TTS_TEXT` | `Сәлем! Қалайсың?` | Plain Cyrillic ru/kk greeting for entrance, opener, and wake-up override. Restart after changing. |
| `CLOUD_TTS_AUDIO_RAW_PCM` | `true` | `true` sends raw PCM16 (what `AudioBuffer` is); `false` wraps WAV. |
| `DEVICE_ID` | — | Optional override. Normally the device is auto-discovered. |
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `8883` | Broker address. |
| `MQTT_PUBLIC_HOST` | auto-detected LAN IPv4 | Host encoded in `/tts/endpoint-qr`; set it if this Mac has multiple network adapters. |
| `MQTT_PUBLIC_PORT` | `8883` | MQTT port encoded in `/tts/endpoint-qr`. |
| `MQTT_QR_DISABLE_VERIFY` | `true` | Tells Moxie to accept the local self-signed broker certificate. |
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
The current change covers one heard line after wake; completing subsequent
chat listen sessions and solving opener scheduling remain separate work.

## Documents

- [AGENTS.md](docs/AGENTS.md) — architecture, MQTT topics, and remaining Cloud TTS work for development agents.
- [Speak Live Plan](docs/SPEAK_LIVE_PLAN.md) — autonomous (non-puppet) conversation path.
- [Speak Puppet Analysis](docs/SPEAK_PUPPET_ANALYSIS.md) — why TELEHEALTH only plays the first `/tts/speak` line.
