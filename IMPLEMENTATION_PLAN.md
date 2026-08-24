# OpenAI Speech Pipeline — Implementation Plan & Status

> **Milestone 1 (minimal Cloud-TTS pipeline): ✅ IMPLEMENTED.**
> `POST /tts/speak` → OpenAI TTS → audio → `CloudTTSResponse` protobuf →
> published to the robot for playback.
>
> **Milestone 2 (robot-triggered synthesis via `CloudTTSRequest`): ✅ IMPLEMENTED.**
> The robot's own `CloudTTSRequest` (forwarded over MQTT) is decoded and routed
> into the same synthesis pipeline, echoing `event_id`/`chunk_num` back on the
> response.
>
> The single AI provider for the whole speech pipeline is now **OpenAI** (STT +
> chat + TTS). There is **no translation
> stage**: the user speaks Russian or Kazakh and OpenAI responds directly in the
> same language.

Legend: **[CONFIRMED]** verified in code/tests · **[ASSUMPTION]** plausible,
needs a real robot to verify · **[RISK]** compatibility concern.

---

## Architecture (OpenAI-based)

```
Moxie microphone
    │  (user speaks Russian or Kazakh)
    ▼
OpenAI STT            src/services/stt/openai_stt.py
    │  Russian/Kazakh text (no translation)
    ▼
OpenAI response       src/services/llm/openai_chat.py
    │  Russian/Kazakh reply text (same language)
    ▼
OpenAI TTS            src/services/synthesis/openai_tts.py
    │  audio (WAV, or raw PCM16 fallback)
    ▼
CloudTTSResponse      src/services/speak_service.py  (protobuf, unchanged contract)
    │  framed  b"embodied.unity.CloudTTSResponse:" + serialized
    ▼
MQTT /devices/{device}/commands/zmq   src/services/mqtt_service.py
    ▼
Moxie plays the audio
```

Two triggers feed the pipeline:

- **HTTP (manual / testing):** `POST /tts/speak`, `/tts/transcribe`,
  `/tts/respond`, `/tts/converse` — exercise each stage, or the whole loop,
  without the robot.
- **Robot (Milestone 2):** an inbound `CloudTTSRequest` on
  `/devices/{device}/events/zmq` drives synthesis automatically.

Each stage (STT, chat, TTS) is a separate module with its own result type, so
it can be developed and tested in isolation.

---

## PART A — COMPLETED WORK

### A.1 Milestone 1 — text → OpenAI TTS → robot  ✅

```
POST /tts/speak  {"text": "...", "language": "ru"|"kk"}
      │  src/router/tts.py            (SpeakRequest; 200 / 422 / 502 / 503)
      ▼  src/services/speak_service.py::speak()
      ▼  src/services/synthesis/openai_tts.py    OpenAI TTS → WAV (or PCM)
      ▼  build CloudTTSResponse  (audio + event_id + chunk_num + timestamp +
      │                           sample_rate + channels + synthesis_time +
      │                           total_time ; marks empty)
      ▼  src/services/mqtt_service.py::publish_cloud_tts_response()
      ▼  MQTT /devices/{device}/commands/zmq
             framed  b"embodied.unity.CloudTTSResponse:" + serialized
      ▼  Moxie plays the audio
```

### A.2 Milestone 2 — `CloudTTSRequest` handling (robot-triggered)  ✅

```
Robot forwards framed CloudTTSRequest → MQTT /devices/{device}/events/zmq
      │  src/services/mqtt_service.py::_on_message
      │     detects the "embodied.unity.CloudTTSRequest:" wire prefix and
      │     bridges it onto the asyncio loop (run_coroutine_threadsafe)
      ▼  src/services/cloud_tts_request.py::handle_payload
      │     decode CloudTTSRequest → pick language from markup text
      ▼  src/services/speak_service.py::speak(event_id=…, chunk_num=…)
      │     synthesize + echo the request's event_id / chunk_num
      ▼  CloudTTSResponse published back on /commands/zmq  (same as A.1)
```

- `PROTO_SUBSCRIBE_TYPES` now correctly requests the **inbound**
  `embodied.unity.CloudTTSRequest` (previously it wrongly listed the outbound
  `CloudTTSResponse`).
- The response echoes the request's `event_id`/`chunk_num` so the robot can
  correlate the audio with the request that triggered it.

### A.3 Files created

| File | Purpose |
|------|---------|
| `src/services/openai_client.py` | Shared, lazily-constructed OpenAI client; API key from `OPENAI_API_KEY` env (never hardcoded/logged); raises `OpenAIConfigError` if missing. |
| `src/services/synthesis/openai_tts.py` | `OpenAITTSEngine` (implements the same `TTSEngine` interface Google used): WAV by default, raw PCM16 fallback. |
| `src/services/stt/openai_stt.py` | `transcribe()` — OpenAI STT → `TranscriptResult{text, language, stt_time_ms}`. |
| `src/services/llm/openai_chat.py` | `respond()` — OpenAI chat → `ChatResult`; steered to reply in the same language, never English. |
| `src/services/language.py` | `detect_language()` — Kazakh-specific-letter heuristic to pick ru/kk when a request carries no language (e.g. `CloudTTSRequest`). |
| `src/services/cloud_tts_request.py` | Milestone 2: decode a framed `CloudTTSRequest` and route it into `speak_service`. |
| `src/services/conversation.py` | Full STT → chat → TTS composition used by `POST /tts/converse`. |
| `tests/` | pytest suite (26 tests) covering every stage, the robot-triggered path, MQTT framing/dispatch, and the HTTP endpoints — all with a fake OpenAI client (no key / robot needed). |

### A.4 Files modified

| File | Change |
|------|--------|
| `src/config.py` | Removed all Google config (`GOOGLE_TTS_LANGUAGE_VOICES`, `CLOUD_TTS_USE_MP3`). Added `OPENAI_API_KEY`, `OPENAI_STT_MODEL`, `OPENAI_CHAT_MODEL`, `OPENAI_TTS_MODEL`, `OPENAI_TTS_VOICE`, `OPENAI_REQUEST_TIMEOUT_S`, `OPENAI_SYSTEM_PROMPT`, `SUPPORTED_LANGUAGES`, `DEFAULT_TTS_LANGUAGE`, `AUDIO_SAMPLE_RATE_HZ`, `AUDIO_CHANNELS`, `CLOUD_TTS_AUDIO_RAW_PCM`. |
| `src/services/speak_service.py` | Points `_engine` at `openai_tts_engine`; `speak()` gained `event_id`/`chunk_num`/`request_source` params so the robot-triggered path can echo them. |
| `src/services/synthesis/base.py` | Unchanged interface (kept provider-agnostic). |
| `src/services/mqtt_service.py` | `PROTO_SUBSCRIBE_TYPES` → `CloudTTSRequest`; added `register_cloud_tts_request_handler()` + `_dispatch_cloud_tts_request()` that bridge inbound `CloudTTSRequest` from paho's thread onto the asyncio loop. Existing filters kept verbatim. |
| `src/router/tts.py` | `/tts/speak` now OpenAI; added `/tts/transcribe`, `/tts/respond`, `/tts/converse`; unified error mapping (422/502/503). |
| `src/main.py` | Registers the inbound `CloudTTSRequest` handler with the running loop during lifespan startup. |
| `pyproject.toml` / `uv.lock` | Removed `google-cloud-texttospeech`; added `openai`; added a `dev` group (`pytest`, `pytest-asyncio`) and pytest config. |
| `src/services/synthesis/google_tts.py` | **Deleted.** |

### A.5 Design decisions

- **Single provider behind the existing seam.** The pipeline still depends only
  on `TTSEngine`/`SynthesisResult`; swapping Google → OpenAI was a one-line
  change to `speak_service._engine`. STT and chat are separate modules with
  their own result dataclasses so each stage is independently testable.
- **No translation stage.** STT returns text in the spoken language; chat is
  instructed (system prompt + per-call language reinforcement) to reply in that
  same language; TTS voices whatever text it is given. Russian stays Russian,
  Kazakh stays Kazakh.
- **Reused the existing transport — no new one invented.** Responses go out on
  the same `/devices/{device}/commands/zmq` channel with the confirmed
  `TypeName:bytes` framing; inbound `CloudTTSRequest` is consumed on
  `…/events/zmq`. MQTT/ZMQ/protobuf infra and the `CloudTTSResponse` contract
  are unchanged.
- **Secrets stay out of code.** `OPENAI_API_KEY` is read from the environment,
  never hardcoded, and never logged (the client-construction log line omits it).
  The client is built lazily so the app boots and non-OpenAI routes work without
  a key.
- **WAV by default, PCM fallback ready.** OpenAI TTS emits `response_format=wav`
  by default (24 kHz mono); `CLOUD_TTS_AUDIO_RAW_PCM=true` switches it to `pcm`
  (raw 24 kHz mono signed-16 LE) — no header surgery needed.
- **Thread bridge for robot triggers.** paho delivers messages on its network
  thread; `run_coroutine_threadsafe` hands `CloudTTSRequest` handling to the
  FastAPI event loop, keeping synthesis async and the loop responsive.
- **Errors handled.** Per-call OpenAI timeout (`OPENAI_REQUEST_TIMEOUT_S`);
  unsupported language → 422; missing key/config → 503; any API/transport
  failure → 502 with detail; empty `markup` in a `CloudTTSRequest` is ignored.

### A.6 Verification performed (in this environment)

Tested with a **fake OpenAI client** (canned audio/text) and a **captured MQTT
publish** — no real key or robot required. `pytest`: **26 passed**.

- ✅ App imports and boots past all imports; `openapi()` exposes `/tts/speak`,
  `/tts/transcribe`, `/tts/respond`, `/tts/converse`, `/tts/cloud-tts`.
- ✅ **TTS + publish (ru & kk):** `speak()` publishes exactly one frame on
  `/devices/{device}/commands/zmq`, decodable as `embodied.unity.CloudTTSResponse`
  with `request_source=ROBOT_TTS_REQUEST`, matching `event_id`, `chunk_num`,
  `timestamp>0`, `sample_rate=24000`, `channels=1`, non-empty audio, empty
  `marks`, and `synthesis_time`/`total_time` set.
- ✅ **Milestone 2 (robot-triggered):** a framed `CloudTTSRequest` (markup, ru or
  kk) → decode → synthesize → published `CloudTTSResponse` echoes the request's
  `event_id`/`chunk_num`; empty markup is ignored; non-`CloudTTSRequest` payloads
  pass through untouched; `_on_message` dispatches a real `CloudTTSRequest` onto
  the loop and ignores unrelated payloads.
- ✅ **STT / chat / TTS stages** each tested standalone; unsupported language →
  `UnsupportedLanguageError`.
- ✅ **Full `/tts/converse` loop:** audio → recognized text → reply → published
  `CloudTTSResponse` reaches the robot transport.
- ✅ **HTTP:** every endpoint returns 200 on the happy path; unsupported
  language → 422.
- ✅ **Raw-PCM toggle:** `CLOUD_TTS_AUDIO_RAW_PCM=true` yields a buffer with no
  `RIFF` header.
- ✅ `ruff check src/` clean apart from the pre-existing repo-wide baseline
  (`CPY001` + the mutually-exclusive `D2xx` docstring rules present in every
  existing file too).

### A.7 Not yet verified — needs the real robot + a real OpenAI key

None are code-blocked; they gate the "real playback on the robot" criterion:

1. **[ASSUMPTION]** `/devices/{device}/commands/zmq` is the topic the robot's
   audio player actually consumes `CloudTTSResponse` on (confirmed as the
   service→robot proto channel; confirm end-to-end playback).
2. **[RISK]** `AudioBuffer.buffer` format the player expects — WAV vs raw PCM16,
   and the 24 kHz sample rate. If WAV doesn't play, set
   `CLOUD_TTS_AUDIO_RAW_PCM=true` (already implemented).
3. **[RISK/ASSUMPTION]** Inbound `CloudTTSRequest` really arrives on
   `…/events/zmq` with the `TypeName:bytes` framing (no captured
   `CloudTTSRequest` exists in-repo yet — dispatch is prefix-matched, so a
   different framing would need the prefix adjusted).
4. **[ASSUMPTION]** OpenAI STT/TTS quality on Kazakh is acceptable for the
   target users (verify with real audio).

---

## PART B — HOW TO RUN & TEST

### B.1 Configuration (environment variables)

| Variable | Default | Purpose |
|----------|---------|---------|
| `OPENAI_API_KEY` | *(required)* | OpenAI key. **Never hardcode.** |
| `OPENAI_STT_MODEL` | `gpt-4o-transcribe` | Speech-to-text model. |
| `OPENAI_CHAT_MODEL` | `gpt-4o-mini` | Response-generation model. |
| `OPENAI_TTS_MODEL` | `gpt-4o-mini-tts` | Text-to-speech model. |
| `OPENAI_TTS_VOICE` | `alloy` | TTS voice. |
| `OPENAI_REQUEST_TIMEOUT_S` | `30` | Per-call OpenAI timeout (s). |
| `SUPPORTED_LANGUAGES` | `["ru","kk"]` | Allowed languages. |
| `DEFAULT_TTS_LANGUAGE` | `ru` | Fallback when none given/detected. |
| `AUDIO_SAMPLE_RATE_HZ` | `24000` | Output sample rate. |
| `CLOUD_TTS_AUDIO_RAW_PCM` | `false` | `true` → raw PCM16 instead of WAV. |
| `DEVICE_ID` | *(real robot id)* | Target device for topics. |
| `OPENMOXIE_ENABLED` | `false` | Defer config pushes to OpenMoxie if true. |

### B.2 Run the service

```bash
export OPENAI_API_KEY=sk-...          # from your secret store — never commit it
docker compose up mqtt                 # broker on :8883
mise dev                               # or: uv run fastapi dev src/main.py
```

Swagger UI: <http://localhost:8000/docs>.

### B.3 Test each stage without the robot (HTTP)

**TTS only** — text → OpenAI TTS → published to Moxie:

```bash
curl -X POST localhost:8000/tts/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"Сәлем! Қалайсың?","language":"kk"}'
# → {"status":"published","event_id":"…","language":"kk","audio_bytes":…,
#    "sample_rate":24000,"channels":1,"synthesis_time_ms":…,"total_time_ms":…}
```

**STT only** — upload a WAV of speech → recognized text:

```bash
curl -X POST localhost:8000/tts/transcribe \
  -F 'file=@russian_hello.wav' -F 'language=ru'
# → {"text":"Привет, как дела","language":"ru","stt_time_ms":…}
```

**Chat only** — user text → reply text (same language):

```bash
curl -X POST localhost:8000/tts/respond \
  -H 'Content-Type: application/json' \
  -d '{"text":"Привет! Как тебя зовут?","language":"ru"}'
# → {"reply_text":"Привет! Меня зовут Мокси…","language":"ru","chat_time_ms":…}
```

**Full loop** — speech → STT → chat → TTS → published to Moxie:

```bash
curl -X POST localhost:8000/tts/converse \
  -F 'file=@kazakh_question.wav' -F 'language=kk'
# → {"status":"published","language":"kk",
#    "recognized_text":"…","reply_text":"…",
#    "stt_time_ms":…,"chat_time_ms":…,"synthesis_time_ms":…,
#    "audio_bytes":…,"event_id":"…","device_id":"…"}
```

**Robot-triggered path (Milestone 2)** — normally the robot sends the
`CloudTTSRequest`; to exercise the decoder without hardware, publish a framed
request to the events topic (or see `tests/test_cloud_tts_request.py` for a
pure-Python example):

```python
from src.protos.embodied.unity.CloudTTS_pb2 import CloudTTSRequest
req = CloudTTSRequest(markup="Сәлем!", event_id="e1", chunk_num=0)
framed = b"embodied.unity.CloudTTSRequest:" + req.SerializeToString()
# publish `framed` to /devices/{DEVICE_ID}/events/zmq
```

### B.4 Run the automated tests

```bash
uv sync                 # installs deps incl. the dev group
uv run pytest -q        # 26 passed
```

### B.5 Running on the real Moxie robot

1. **Power on Moxie:** press and hold the button on the back until the screen
   lights up; wait for it to finish booting and connect to Wi-Fi (it must be on
   the same network / paired to your OpenMoxie server so its MQTT traffic
   reaches the broker this service connects to).
2. **Start the broker + service** (B.2) with a valid `OPENAI_API_KEY` and the
   real `DEVICE_ID` for your robot.
3. **Enable cloud TTS on the device** (so it routes speech through this service):
   ```bash
   curl -X POST localhost:8000/tts/cloud-tts \
     -H 'Content-Type: application/json' -d '{"enabled": true}'
   ```
4. **Speak to Moxie** in Russian or Kazakh. The robot forwards a
   `CloudTTSRequest`/STT event; the service runs STT → chat → TTS and publishes
   a `CloudTTSResponse`, which Moxie plays back.
   - Or drive TTS directly with `POST /tts/speak` and confirm the robot speaks.
5. **Watch the logs** (`log/`) for `publishing embodied.unity.CloudTTSResponse
   to /devices/{id}/commands/zmq (N audio bytes)` to confirm audio left the
   service.
6. If Moxie receives the message but plays nothing, flip
   `CLOUD_TTS_AUDIO_RAW_PCM=true` and retry (WAV vs raw-PCM player expectation).

---

## PART C — REMAINING WORK (future milestones)

> Milestones 1 and 2 are done on OpenAI. What remains is lip-sync, richer
> streaming, and consuming the robot's STT stream directly.

### Milestone 3 — STT event ingestion (`zmqSTTResponse`)
Today `/tts/converse` takes an uploaded audio file. Next, consume the robot's
own `zmqSTTResponse` (proto already vendored) so recognized speech drives the
conversation automatically on-device, feeding chat → TTS without an HTTP upload.
- **[RISK]** confirm the inbound topic/framing for `zmqSTTResponse` with a live
  capture.

### Milestone 4 — Lip-sync: visemes / `TTSMark`s
- Derive word/phoneme timings and populate `CloudTTSResponse.marks`
  (`TTSMark{time,start,end,type,value}`). OpenAI TTS does not return timepoints,
  so this needs an alignment step (e.g. forced alignment) or a provider that
  emits marks.
- **[RISK]** learn the exact `type`/`value` viseme vocabulary Moxie expects from
  a real capture first.

### Milestone 5 — `CloudTTSSupplement`
- Emit the supplement (`text`, `markup`, `tts_engine`, and the timing breakdown)
  alongside the response.

### Milestone 6 — Chunking & streaming
- Split long replies into ordered chunks; synthesize/publish per `chunk_num` to
  cut latency; add back-pressure/ordering.

### Milestone 7 — RemoteChat support
- Wire `REMOTECHAT_TTS_REQUEST` end-to-end (pass that `request_source` through
  `speak_service` for remote-chat-originated speech).

### Milestone 8 — Hardening
- Retries/metrics; narrow the diagnostic `#` MQTT subscribe to confirmed topics;
  expand the regression suite; on-robot latency budget.

### Cross-cutting (deferred) risks
- **Audio format / sample rate** (§A.7.2) — validate on-robot early.
- **Latency budget** — STT + chat + TTS in series; chunking (M6) mitigates.
- **Secrets** — `OPENAI_API_KEY` from env/secret store only, never in code or
  `HARDCODED_DEVICE_CONFIG`.
- **`cloud_tts` toggle interlock** — enabling the toggle must make the robot
  route its speech to this service.
