# Moxie Cloud TTS

Replacement cloud backend for the Moxie robot after Embodied Inc. shut down its
servers. It speaks the robot's own MQTT/protobuf protocol, so Moxie boots into a
working session instead of failing authentication, and it voices the robot with
OpenAI text-to-speech — including Russian and Kazakh, which the original service
never supported.

Current milestone: **verify the kk/ru input compatibility bridge on the robot.**
Kazakh wake-up and replies to English input have been heard. The server now
translates recognized kk/ru to English for Moxie, recovers the original text for
GPT, and bounds reply length and provider waits. Reply language remains fixed
kk or explicitly configured ru. See [localization repair](docs/LOCALIZATION_FIXES.md)
for the implementation, passing regressions and remaining hardware checks.

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

Keep the existing `OPENAI_API_KEY` in `.env`. These are the translation defaults;
set them explicitly there if you have overridden them previously:

```dotenv
PUPPET_MODE_AUTO_ENABLE=false
CLOUD_TTS_AUTO_ENABLE=true
CLOUD_TTS_AUDIO_RAW_PCM=true
CLOUD_TTS_EMIT_MARKS=true
CLOUD_TTS_REQUEST_SOURCE=robot
DEFAULT_TTS_LANGUAGE=kk
CLOUD_TTS_TRANSLATION_ENABLED=true
OPENAI_TRANSLATION_TIMEOUT_S=4
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

All autonomous English speech now follows the translation path:

- WAKEUP/ENROLLCONVO receive a low-priority `FALLBACK`, letting the robot accept
  its native response and transition into scheduled chat. The previous
  `GLOBAL_COMMAND` greeting could replace that transition along with its text.
- Every English/Latin line in `CloudTTSRequest` goes to OpenAI for translation
  into `DEFAULT_TTS_LANGUAGE` (`kk`), then to TTS. This includes wake-up lines,
  activity questions, end-of-session content, and clarification requests.
- The fixed Kazakh opener is gone. Remote chat retains ordinary English opener
  text, which is translated only if the robot requests its audio. Native
  `Good morning.` becomes a translation such as `Қайырлы таң.`.
- The response echoes the original `event_id`/`chunk_num`, preserves original
  `cmd:` animation marks, and derives mouth movements from the new spoken text.
- Translation applies before and after chat entry; there is no phrase allowlist
  or active-chat greeting replacement. Cyrillic ru/kk lines pass through.
  Puppet speech keeps its existing explicit text/language behavior.
- Successful translations are cached in memory (up to 256 entries). Failures,
  malformed translations and deadline expiry produce one correlated silence
  response with the original command marks, never English or a canned opener.

If Moxie asks you to say “Moxie, let's go”, say it and pause. The existing wake
gate answers `END_OF_SPEECH` with that trigger; the greeting is never used as
an STT transcript. The `/tts/wakeup` command itself only unsleeps the robot.
For this test, keep puppet off and use the wake-up flow above.

To translate English into Russian, set `DEFAULT_TTS_LANGUAGE=ru` and restart.
Kazakh's language code is `kk`, not `kz`. `CLOUD_TTS_TRANSLATION_ENABLED=false`
disables translation for diagnostics. Old `WAKEUP_TTS_TEXT`,
`WAKEUP_TTS_OVERRIDE_ENABLED` and `REMOTE_CHAT_LANGUAGE_GUARD_ENABLED` settings
are removed and ignored if left in `.env`; they cannot restore the old greeting.

### Verify on the robot

Watch `mise start` or filter the log:

```bash
tail -f log/debug.log | rg --line-buffered 'bootstrapping device|entrance deferred|remote-chat|Tx remote_chat|CloudTTSRequest|spoke event_id|STT audio|STT END|STT FINAL|openai stt|SILENT|synthesis failed'
```

For an English request, expect its original text followed by translated
`synthesis_text` and audio for the **same ID and chunk**:

```text
CloudTTSRequest translation ... event_id='E' chunk=0 target=kk requested_text='Good morning.' synthesis_text='Қайырлы таң.'
handling CloudTTSRequest ... event_id='E' chunk=0 ... language=kk synthesis_text='Қайырлы таң.'
spoke event_id=E chunk=0 language=kk audio_bytes=... marks=... raw_pcm=True ...
```

These are expected log shapes; wording can vary. The incoming markup remains
English, while the audio speaks its translation. Commands are never translated.

Success requires **hearing translations of several different lines and seeing
mouth movement**. `notify`, `Tx remote_chat`, or `SILENT fallback` alone is not
success. Check translation timeouts/errors if a line is silent. If the FSM
reports an empty module (`/_get_current_content_id`), reboot before retrying;
translation changes speech, not module routing or speech recognition.

### Verify the remote-chat handoff

The operator confirmed translated Kazakh wake-up audio on Sep 28. That run
also logged an actual `OPENMOXIE_CHAT` prompt and two chat listen sessions.
Restart the service, reboot Moxie, wake it with puppet off, and look for:

```text
entrance deferred to local response ... module=WAKEUP/ ... output_type=FALLBACK
Rx remote-chat ... command='prompt' module=OPENMOXIE_CHAT/default event_id='P'
remote-chat entered ... module=OPENMOXIE_CHAT/default ... event_id=P
remote-chat language ... language=kk
Tx remote_chat ... event_id=P output_type=GLOBAL_RESPONSE text='Hi! What would you like to talk about?'
```

`start`, `continue` or `reprompt` can also establish real entry. A SCHEDULED
activity or `notify` alone is insufficient. Confirm a subsequent robot-originated
TTS request and its matching audio response, then audible playback. The native
`Excellent!` request can arrive after the chat prompt using the earlier wake ID;
its translation must still answer that original request.

### Verify microphone transcription

Restart with `mise start`, wake Moxie, and say “Moxie, let's go”. Wait for the
chat opener to finish, then speak a short sentence such as “Мен алманы жақсы
көремін” or “Я люблю яблоки”. The existing entrance shim remains in place;
real transcription begins only after an actual `OPENMOXIE_CHAT` router turn.

Expected sequence (UUID `U` must stay the same for the listen and its FINAL):

```text
STT audio started ... uuid=U phase=chat sample_rate=16000 channels=1 sample_width=2
STT END ... uuid=U phase=chat frames=... audio_bytes=... audio_ms=...
openai stt transcribed ... (auto) in ... ms
STT validated/bridge ... uuid=U original='Мен алманы жақсы көремін' wire='I like apples'
STT FINAL ... uuid=U phase=chat ... speech='I like apples'
Rx remote-chat ... command='continue' event_id='U' speech='I like apples'
remote-chat input ... event_id=U original_recovered=True output_language=kk
Tx remote_chat ... event_id=U output_type=GLOBAL_RESPONSE ...
remote-chat audio ... event_id=U source=REMOTECHAT
spoke event_id=U language=kk ...
```

`reprompt` is also valid and now forwards nonempty first-turn speech to the LLM.
Check that the transcript matches what you said, the reply answers it, and a
new UUID appears when you speak again. Try at least five turns per language.
Recognition auto-detects the spoken language; **reply language remains fixed**
by `DEFAULT_TTS_LANGUAGE` (`kk` by default, `ru` for a Russian test). Automatic
reply-language switching is the next milestone.

`OPENAI_STT_TIMEOUT_S=6` bounds recognition plus input translation after END;
SDK retries are disabled. Empty/silent or malformed audio, no recognized speech,
provider failure, and deadline expiry produce one FINAL error 66 with a reason.
The old “Robot transcription is not implemented” placeholder should no longer
appear for chat. Onboard modules outside chat still receive an explicit error.
Never-ending streams expire after 30 seconds; audio is capped at 30 seconds,
with at most two pending listens per device and 16 globally. A reconnect or
module exit discards pending work and rejects late results.

The Sep 28 log contains UUIDs/errors but no microphone bytes. Accumulation and
16 kHz format follow the copied OpenMoxie implementation; real microphone
quality and firmware acceptance still require this operator test. See
[STT verification](docs/STT_WIRING.md).

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
| `POST /tts/sleep` | Queue sleep; sent with the reply to the robot's next chat turn (speech or silence reprompt) |
| `GET /tts/devices` | Robots seen on the broker |

## Configuration

Everything is environment variables or `.env`; see `src/config.py`.

| Variable | Default | Notes |
| --- | --- | --- |
| `OPENAI_API_KEY` | — | Required. Never hardcode it. |
| `CLOUD_TTS_AUTO_ENABLE` | `true` | Enable cloud TTS at startup. Skipped without a key. |
| `PUPPET_MODE_AUTO_ENABLE` | `false` | Start the robot in TELEHEALTH mode. |
| `DEFAULT_TTS_LANGUAGE` | `kk` | Autonomous chat output language; `kk` or `ru`. Input language does not change it. |
| `CLOUD_TTS_TRANSLATION_ENABLED` | `true` | Translate every English/Latin autonomous speech request; puppet bypasses this. |
| `OPENAI_CHAT_MODEL` | `gpt-4o-mini` | Existing model used for translation as well as remote chat, with separate prompts. |
| `OPENAI_TRANSLATION_TIMEOUT_S` | `4.0` | Translation deadline, including worker wait; SDK retries disabled for this stage. |
| `CLOUD_TTS_AUDIO_RAW_PCM` | `true` | `true` sends raw PCM16 (what `AudioBuffer` is); `false` wraps WAV. |
| `DEVICE_ID` | — | Optional override. Normally the device is auto-discovered. |
| `MQTT_HOST` / `MQTT_PORT` | `localhost` / `8883` | Broker address. |
| `MQTT_PUBLIC_HOST` | auto-detected LAN IPv4 | Host encoded in `/tts/endpoint-qr`; set it if this Mac has multiple network adapters. |
| `MQTT_PUBLIC_PORT` | `8883` | MQTT port encoded in `/tts/endpoint-qr`. |
| `MQTT_QR_DISABLE_VERIFY` | `true` | Tells Moxie to accept the local self-signed broker certificate. |
| `OPENAI_REQUEST_TIMEOUT_S` | `8.0` | Shared socket timeout; operation deadlines below also bound total waits. |
| `STT_INPUT_LANGUAGE` | `auto` | Recognition hint: auto, kk, ru or en; separate from reply language. |
| `STT_ENGLISH_BRIDGE_ENABLED` | `true` | Translate human kk/ru to English for robot routing; preserve original text for GPT. |
| `STT_BRIDGE_METADATA_ENABLED` | `false` | Optional protobuf language/original fields; test separately on firmware. |
| `STT_WAKE_WORD_REQUIRED` | `true` | In chat, answer only speech that starts with a wake word; other speech gets an empty FINAL and a silent turn. |
| `STT_WAKE_WORDS` | `moxie, moxy, мокси, моқси, моксі, моксы` | First-word matches, case-insensitive (JSON list in `.env`). |
| `OPENAI_STT_TIMEOUT_S` | `6.0` | Total recognition plus bridge deadline. |
| `OPENAI_INPUT_TRANSLATION_TIMEOUT_S` | `2.0` | Bounded by remaining STT time. |
| `OPENAI_CHAT_TIMEOUT_S` | `4.0` | Chat generation deadline, no retries. |
| `OPENAI_TTS_TIMEOUT_S` | `8.0` | Full synthesis deadline, no retries. |
| `REMOTE_REPLY_TIMEOUT_S` | `12.0` | Combined translation/chat/audio budget. |
| `OPENAI_CHAT_MAX_TOKENS` | `256` | Completion cap; spoken chat is also limited to 3 sentences/35 words. |

## Tests

```bash
mise test
```

Providers and MQTT are mocked. Tests do not read or overwrite `log/debug.log`
and do not prove playback on the robot.

## Roadmap

Microphone STT is wired for active remote chat. Next: validate real Kazakh and
Russian recordings on the robot, then implement automatic reply-language
switching and sustained conversation recovery. Native English activities may
still require translated input or intent mapping; this integration transcribes
only active remote chat and preserves the wake shim.

## Documents

- [Localization repair](docs/LOCALIZATION_FIXES.md) — input bridge, deadlines, regression evidence and robot checklist.
- [AGENTS.md](docs/AGENTS.md) — architecture, MQTT topics, and remaining Cloud TTS work for development agents.
- [Speak Live Plan](docs/SPEAK_LIVE_PLAN.md) — autonomous (non-puppet) conversation path.
- [Speak Puppet Analysis](docs/SPEAK_PUPPET_ANALYSIS.md) — why TELEHEALTH only plays the first `/tts/speak` line.
