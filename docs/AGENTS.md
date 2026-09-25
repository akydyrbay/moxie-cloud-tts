# AGENTS.md — Moxie Cloud TTS

Instructions for a development agent finishing this repo. Prefer this file over `README.md` for architecture and remaining work. Do not invent WebSockets, queues, or extra HTTP speak paths.

---

## 1. Global objective

This repository is a **replacement cloud backend** for the Moxie robot after Embodied Inc. shut down its servers.

**FastAPI** (`src/main.py`) is the operator/control plane (Swagger at `/docs`). It does **not** carry robot audio. The robot talks **MQTT over TLS TCP :8883** to a local Mosquitto broker. This service is the MQTT client that:

1. Speaks the robot’s OpenMoxie protocol (JSON commands + framed protobuf).
2. Bootstraps config (`cloud_tts=1`) so speech is synthesized here instead of onboard.
3. Voices **Russian / Kazakh** via OpenAI TTS.
4. Eventually runs an autonomous listen → chat → speak loop **without** TELEHEALTH (puppet) mode.

Device ids look like `d_<uuid>`. They are **discovered** from MQTT topics, never hardcoded.

**Invariant:** Moxie only plays cloud audio that answers a request it is already waiting on, matched by `event_id` / `chunk_num`. You cannot push a `CloudTTSResponse` and have it play.

**Current narrow milestone (2026-09-25):** `prompt.md` Path A1 + A2 is implemented,
pending operator playback confirmation. `WAKEUP_TTS_TEXT` defaults to
`Сәлем! Қалайсың?` for entrance responses and chat openers. With
`WAKEUP_TTS_OVERRIDE_ENABLED=true`, known local `Good morning.` / `Good morning!`
/ `Excellent!` and captured day-start instructions are voiced as that line outside
TELEHEALTH, preserving live IDs and original command marks. Matching ignores
boundary punctuation, including captured `Excellent! " "` text.
Do not gate the override on conversation-active state: chat prompt can arrive
before the wake-up confirmation's Cloud TTS request. Logs show incoming
`speakable` and effective `synthesis_text` separately. An overridden English
request is not a native Cyrillic request; only operator-heard audio confirms
playback. See the current [README verification steps](../README.md#verify-on-the-robot).
`DEFAULT_TTS_LANGUAGE=kk` now fixes autonomous chat output language at entry,
independent of input/history language. `start` and `prompt` return the greeting
without calling the LLM. Invalid-language LLM replies get a localized fallback;
English assistant notifications are excluded from history. The separate
`REMOTE_CHAT_LANGUAGE_GUARD_ENABLED` flag protects TTS payloads only after an
actual chat router turn. A delayed onboard notify must not clear that state.
Neither schedule activity nor notify activates chat. Russian remains available
through explicit configuration and puppet speech. See
[the Sep 23 diagnosis and replay](KAZAKH_WAKEUP_ANALYSIS.md).
The conversation roadmap below is later work, beyond this one-line milestone.

---

## 2. Component breakdown

### 2.1 Two operating modes (do not mix)

| Mode | How enabled | What it is | Status |
|---|---|---|---|
| **TELEHEALTH / puppet** | `POST /tts/puppet` or `PUPPET_MODE_AUTO_ENABLE` | Robot stays awake, **schedule empty**, speaks only `PLAY_OUTPUT` from `/tts/speak` | **Proven for first-line TTS. Dropped as the conversation path.** Code kept. **Do not modify this pipeline.** |
| **Autonomous / remote-chat** | `PUPPET_MODE_AUTO_ENABLE=false` (default) | Robot runs `WAKEUP` then `OPENMOXIE_CHAT`; this server supplies opener + chat text; robot raises `CloudTTSRequest` | **Target path.** Mouth TTS works. Real STT not wired. Chat entry still needs on-device proof. |

### 2.2 Why puppet was dropped (conversation)

Puppet was the first way to prove Cloud TTS:

- `POST /tts/speak` → MQTT `commands/telehealth` `PLAY_OUTPUT` → robot emits `CloudTTSRequest` → OpenAI PCM → `CloudTTSResponse` with the **same** `event_id`.
- First line plays. Lip marks (`src/services/marks.py`) work.

It was **abandoned as the product path** because:

1. **TELEHEALTH parks the brain.** Schedule is emptied (`handle_schedule_query` returns `{"provided_schedule": []}`). `OPENMOXIE_CHAT` never runs, so there is no ru/kk conversation loop.
2. **Second puppet line often silent** in the same TELEHEALTH session. Prepending `INTERRUPT` to every line made it worse (cancels the cloud output path while a `CloudTTSRequest` still arrives).
3. **Mid-session identity push SIGABRTs** the native brain (`OnCloudSettingsChanged` → `SetMentorName` → destroyed mutex). Live toggles must strip `child_pii` / `pairing_status` (`tts_service._without_identity_fields`). Full identity is bootstrap-only.
4. Spoken ru/kk on the conversation path must be `remote_chat.output.markup`, **never** `/tts/speak` and **never** `zmqSTTResponse.speech`.

Keep `/tts/speak`, `/tts/puppet`, `/tts/interrupt`, `telehealth.py`, and `tts_service.set_puppet_mode` as-is. `PUPPET_MODE_AUTO_ENABLE` must stay **false** while finishing Cloud TTS conversation.

Full on-robot evidence (line-2 silence vs identity SIGABRT): `docs/SPEAK_PUPPET_ANALYSIS.md`.

### 2.3 Service layout

```
src/main.py                 FastAPI app + lifespan (register MQTT handlers, connect)
src/router/tts.py           HTTP /tts/* (operator; not the robot audio path)
src/config.py               Settings + HARDCODED_DEVICE_CONFIG + openers/schedule
src/services/
  mqtt_service.py           paho client, topic routing, proto framing
  devices.py                device_registry (seen / bootstrapped / primary)
  device_state.py           in-memory config overlay (cloud_tts, moxie_mode)
  remote_chat.py            brain: modules, schedule, OPENMOXIE_CHAT, WAKEUP
  stt_request.py            ears: wake-gate inject; M2 will transcribe here
  cloud_tts_request.py      mouth inbound: CloudTTSRequest → speak_service
  speak_service.py          OpenAI TTS → CloudTTSResponse (shared by both modes)
  telehealth.py             puppet PLAY_OUTPUT / INTERRUPT / wakeup / sleep  [DO NOT TOUCH]
  tts_service.py            config push + puppet toggle (identity strip)
  marks.py                  viseme + cmd: echo so firmware leaves MarkingUp
  audio.py                  PCM duration; WAV repair (never ask OpenAI for wav)
  stt/openai_stt.py         OpenAI STT engine — exists, not wired to robot
  llm/openai_chat.py        OpenAI chat — wired from remote_chat continue/reprompt
  synthesis/openai_tts.py   OpenAI TTS engine
src/protos/                 generated protobuf (do not hand-edit)
mosquitto.conf              TLS :8883, anonymous, $SYS/broker/log/#
```

HTTP is **not** the robot session. MQTT is.

### 2.4 Routing mechanics (MQTT inbound)

paho runs on its own thread. Handlers are scheduled onto the FastAPI asyncio loop via `run_coroutine_threadsafe`.

On connect this service **subscribes only** to:

- `/devices/+/events/#`
- `/devices/+/state`
- `$SYS/broker/log/#`

Do **not** subscribe to `#` (that echoed our own audio publishes).

Dispatch order in `_on_message`:

1. `$SYS/broker/log/#` → connect/disconnect parse (`d_[uuid]`). Forget bootstrap on reconnect.
2. Parse device id from `/devices/{id}/...`; `device_registry.seen`.
3. Framed `embodied.unity.CloudTTSRequest:` on `events/zmq` → `cloud_tts_request.handle_payload`.
4. Framed `embodied.perception.audio.zmqSTTRequest:` on `events/zmq` → `stt_request.handle_payload`.
5. `events/remote-chat` (and `-staging`) → `remote_chat.handle_remote_chat_event`.
6. `events/client-service-activity-log` → schedule / mentor queries + telehealth READY.
7. `/devices/{id}/state` → bootstrap once if not yet bootstrapped.
8. `events/device-logs` → re-log robot Unity logs.
9. `client-service-robot-status` → log on change only (heartbeat).
10. Ignore `embodied.power*` and `ASRAnalytics`.

### 2.5 Broker configuration (topics — there are no queues)

Mosquitto: TLS port **8883**, `allow_anonymous true`, `log_dest topic` so `$SYS/broker/log/#` carries `connected from … as d_…`.

**Robot → server (events)**

| Topic | Payload |
|---|---|
| `/devices/{id}/events/zmq` | framed protobuf: `CloudTTSRequest`, `zmqSTTRequest`, other zmq types |
| `/devices/{id}/events/remote-chat` | JSON RemoteChatRequest (modules query + router turns) |
| `/devices/{id}/events/client-service-activity-log` | JSON: `query=schedule\|mentor_behaviors`, telehealth state |
| `/devices/{id}/events/device-logs` | JSON robot log lines |
| `/devices/{id}/state` | device state (bootstrap trigger) |
| `/devices/{id}/…/client-service-robot-status` | JSON heartbeat (`mode`, battery, settings) |

**Server → robot (commands / config)**

| Topic | Payload |
|---|---|
| `/devices/{id}/config` | JSON device config (`cloud_tts`, optional `moxie_mode`, props) |
| `/devices/{id}/commands/zmq` | framed protobuf: `ProtoSubscribe`, `CloudTTSResponse`, `zmqSTTResponse` |
| `/devices/{id}/commands/remote_chat` | JSON RemoteChatResponse |
| `/devices/{id}/commands/query_result` | JSON schedule / mentor_behaviors |
| `/devices/{id}/commands/telehealth` | JSON PLAY_OUTPUT / INTERRUPT (**puppet only**) |
| `/devices/{id}/commands/wakeup` | `{"command":"wakeup"}` |
| `/devices/{id}/commands/sleep` | `{"command":"sleep"}` plus remote-chat `action:sleep` |

**Framing:** `b"<fully.qualified.TypeName>:" + SerializeToString()`. Templates live in `src/config.py` (`COMMAND_TOPIC_TEMPLATE`, `COMMAND_JSON_TOPIC_TEMPLATE`, `CONFIG_TOPIC_TEMPLATE`).

**ProtoSubscribe** (bootstrap): robot is told to forward `embodied.unity.CloudTTSRequest` and `embodied.perception.audio.zmqSTTRequest`.

### 2.6 Three channels (keep separate)

| Channel | Messages | Meaning |
|---|---|---|
| **Ears** | `zmqSTTRequest` / `zmqSTTResponse` | Mic PCM in; **recognized text** out. Cannot make Moxie speak. |
| **Mouth** | `CloudTTSRequest` / `CloudTTSResponse` | Robot asks to voice markup; **audio** out. Only way ru/kk is heard. |
| **Brain** | JSON `remote_chat` / `query_result` | Handshake, module, **text Moxie will say** (`output.markup`). |

Spoken ru/kk = `RemoteChatResponse.output.markup` → robot copies it into `CloudTTSRequest.markup` → `cloud_tts_request` synthesizes. Do not put spoken lines in STT `speech`.

### 2.7 What already works (do not re-investigate)

- MQTT connect, device discovery, bootstrap config + ProtoSubscribe.
- `cloud_tts=1` auto-enable when `OPENAI_API_KEY` is set.
- Cloud TTS of whatever markup the robot requests (PCM 24 kHz mono, marks, silence on empty/fail).
- Identity strip on mid-session config toggle.
- Wake-gate STT inject: on `END_OF_SPEECH` before chat, reply `WAKEUP_TRIGGER_LINE` = `"Moxie, let's go"`.
- Remote-chat: modules handshake, schedule (`OPENMOXIE_CHAT/default`), mentor_behaviors `[]`.
- WAKEUP/`ENROLLCONVO` `start|continue`: `GLOBAL_COMMAND` with `WAKEUP_TTS_TEXT` (no `launch` on primary). Empty GLOBAL_COMMAND left WAKEUP looping English; `launch` as primary skipped Cloud TTS.
- `OPENMOXIE_CHAT` `prompt` → `REMOTE_CHAT_OPENERS` (configured greeting, kk by default) wrapped in `<usel variant="2" genre="none">`.
- `OPENMOXIE_CHAT` `continue`/`reprompt` → `openai_chat.respond`.
- `note_conversation_active` only on remote `prompt|continue|reprompt|start`, **not** `notify`.
- HTTP: devices, cloud-tts toggle, puppet toggle, speak, interrupt, wakeup, sleep.
- `max_enroll="0"` + paired `child_pii` at bootstrap (enrollment skip **in tree**; prove on boot).

---

## 3. Connection lifecycle

**Not WebSockets. Not HTTP streaming.** The robot is an MQTT client (TLS :8883). This FastAPI process is a second MQTT client (`moxie-tts-<random>`).

```
Moxie  ──MQTT/TLS──►  Mosquitto :8883  ◄──MQTT/TLS──  FastAPI (paho loop_start)
Operator ──HTTP──► FastAPI /tts/*  (control only)
```

### Sequence

1. `mise start` brings up Mosquitto + uvicorn. Lifespan registers handlers, optionally sets `cloud_tts=1`, pre-warms OpenAI, then `mqtt_service.connect()`.
2. Robot connects; broker log `$SYS` matches `connected from … as (d_[a-f0-9-]+)`.
3. Service `forget_bootstrap` + `seen`. Next `/state` or `remote-chat`/`activity-log` calls `ensure_device_bootstrapped` once:
   - reset STT conversation phase + chat session
   - `publish_settings_update` (full config **including** identity)
   - `send_proto_subscribe`
4. Robot queries `modules` → we advertise `OPENMOXIE_CHAT`. Queries `schedule` → `DEFAULT_SCHEDULE` unless puppet (empty).
5. Firmware still runs onboard **WAKEUP**. STT inject `"Moxie, let's go"`. Entrance volley gets `WAKEUP_TTS_TEXT` GLOBAL_COMMAND; known local English TTS requests can use the same greeting via the guarded override.
6. Target: `OPENMOXIE_CHAT` `command=prompt` → opener → Cloud TTS → listen.
7. Disconnect/reboot: `$SYS` disconnect regex → `forget_bootstrap`. Next session bootstraps again.

### Session state (in-process, not Redis)

| Store | Lifetime |
|---|---|
| `device_registry._last_seen` / `_bootstrapped` | until disconnect or process restart |
| `device_state._config` | process lifetime; overlay `cloud_tts`, `moxie_mode` |
| `stt_request._conversation_active` | until reconnect bootstrap |
| `stt_request._session_start_ms` | per STT `uuid` until END or answer |
| `remote_chat._sessions` | per device, output language/active flag plus 20-turn history from `notify` |
| `remote_chat._last_event_id` | last router event (for HTTP sleep) |
| `telehealth._puppet_state` | READY/IN_SESSION/EXITING from activity-log **session** boundaries only |

There is no durable robot session store. Restarting this service forgets devices until they publish again.

---

## 4. Roadmap — remaining Cloud TTS conversation pipeline

**Do not add HTTP endpoints to make the robot speak on this path.** Mouth is already `CloudTTSRequest` → `speak_service`. Remaining work is ears + proving chat entry.

### 4.1 M1 — Prove `OPENMOXIE_CHAT` (blocker)

**Goal:** after wakeup + `"Moxie, let's go"`, logs show:

```
Rx remote-chat … module=OPENMOXIE_CHAT … command=prompt
Tx remote_chat … text='Привет!…'
spoke … language=ru
```

No chocolate/strawberry `ENROLLCONVO`. One English WAKEUP “let’s go” **before** inject is expected.

**If ENROLLCONVO still runs:** do **not** set `brain_entrances_available="0"`. Next lever is option B in `handle_router_request` for `ENROLLCONVO`: `exit_module` and/or `launch OPENMOXIE_CHAT` as a **second** `response_actions` entry (primary must stay speak-only). Tests: `tests/test_remote_chat.py`.

**Do not** mark conversation active on `OPENMOXIE_CHAT` `notify`.

### 4.2 M2 — Wire OpenAI STT (after M1)

**Change only** `src/services/stt_request.py`. Reuse `src/services/stt/openai_stt.py` and `audio.pcm_to_wav`.

Branch:

```
if device_id not in _conversation_active:
    inject WAKEUP_TRIGGER_LINE          # already implemented
else:
    transcribe accumulated PCM          # build this
```

**Payload scheme — inbound `zmqSTTRequest` (already):**

- Topic: `/devices/{id}/events/zmq`
- Prefix: `embodied.perception.audio.zmqSTTRequest:`
- `vad`: START / SPEECH / END_OF_SPEECH
- `audio_content`: 16 kHz mono PCM16 (not 24 kHz TTS)
- `uuid`: one per utterance

**Payload scheme — outbound `zmqSTTResponse` (already shaped):**

```python
resp.uuid = req.uuid                 # MUST echo
resp.type = FINAL
resp.speech = transcript             # what the child said, not what Moxie should say
resp.timestamp = now_ms()
resp.start_timestamp = session_start
resp.end_timestamp = now_ms()
# on failure still publish (error_code / fallback speech) so the robot does not wait
```

Publish framed on `/devices/{id}/commands/zmq` via `mqtt_service.publish_proto`.

**Implementation steps:**

1. Accumulate `audio_content` per `(device_id, uuid)` on START/SPEECH/END.
2. On END: `pcm_to_wav(buffer, sample_rate=16000)` — **not** `AUDIO_SAMPLE_RATE_HZ` (24 kHz).
3. `await run_in_threadpool(openai_stt.transcribe, wav, None, "stt.wav")` (`language=None` → auto ru/kk).
4. Stay under robot wait (`OPENAI_REQUEST_TIMEOUT_S` is 8s). Cap buffer; drop sessions with no END.
5. Never transcribe during wake-gate.

**Retry / timeout policy (no new HTTP retries):**

| Failure | Required behavior |
|---|---|
| OpenAI STT timeout / error | Still send a FINAL `zmqSTTResponse` (fallback speech or `error_code`) |
| Late FINAL | Robot logs `zmqSTT: UUID has gone too long` and may force-idle — keep STT < 8s |
| OpenAI TTS fail | Already handled: `speak_service` publishes silence |
| Missing `CloudTTSResponse` | Robot stalls in MarkingUp — never skip the reply |
| MQTT reconnect | `forget_bootstrap`; do not reuse STT uuid maps across sessions |

No new retry loop around MQTT publish. One response per STT uuid, one response per CloudTTS `event_id`.

### 4.3 M3 — Opener copy (polish)

`REMOTE_CHAT_OPENERS["default"]` is already Russian. Fix typos («чём» / «Что тебя на уме»). Optional Kazakh variant. Plays only after M1 `prompt`.

### 4.4 HTTP — do not add

Do **not** add `/tts/transcribe`, `/tts/respond`, `/tts/speak-direct`, or a new “conversation speak” endpoint. Puppet `/tts/speak` stays for operators only (M4 later, optional, idle-only PLAY_OUTPUT).

Existing endpoints to keep:

| Endpoint | Role |
|---|---|
| `GET/POST /tts/cloud-tts` | live `cloud_tts` flag |
| `GET/POST /tts/puppet` | TELEHEALTH — do not use for conversation work |
| `POST /tts/speak` | puppet PLAY_OUTPUT — do not use for OPENMOXIE_CHAT |
| `POST /tts/interrupt` | puppet interrupt |
| `POST /tts/wakeup` | unsleep only |
| `POST /tts/sleep` | MQTT sleep + remote-chat `action:sleep` |
| `GET /tts/devices` | discovered ids |

### 4.5 Integration tests still needed

Existing unit tests mock MQTT + OpenAI (`tests/conftest.py`). Add:

| Test | Assert |
|---|---|
| `tests/test_stt_request.py` | wake-gate still injects `"Moxie, let's go"` |
| same | after `note_conversation_active`, accumulated PCM → fake `openai_stt.transcribe` → FINAL with echoed `uuid` |
| same | WAV passed to STT is **16 kHz** |
| same | STT failure still publishes a response |
| `tests/test_remote_chat.py` | notify does not call `note_conversation_active` (already) |
| same | if option B: ENROLLCONVO continue launches/exits without primary `action` |
| On-device (not pytest) | log sequence in §4.1; then speak ru/kk; `openai stt transcribed`; remote-chat `speech=` matches; reply TTS same language |

Do not require a live robot in pytest.

---

## 5. Hard rules (do not break)

1. **Do not modify** `src/services/telehealth.py` speak/interrupt sequencing, or the puppet `/tts/speak` path, unless the operator explicitly asks.
2. `PUPPET_MODE_AUTO_ENABLE` stays **false** for conversation work.
3. Do not mix ears / mouth / brain. Spoken ru/kk is only `output.markup` → `CloudTTSRequest`.
4. Do not put spoken lines in `zmqSTTResponse.speech`.
5. Do not re-push `child_pii` / `pairing_status` on live toggles.
6. Do not set `brain_entrances_available="0"`.
7. Do not put `launch` / `sleep` as the **primary** `response_action` (skips Cloud TTS). Speak-only primary, side-effect as later `response_actions[]`.
8. Echo `event_id` and STT `uuid` verbatim. Empty telehealth `event_id` is valid — still echo it.
9. Every `CloudTTSRequest` gets exactly one `CloudTTSResponse` (audio or silence).
10. Never ask OpenAI TTS for `response_format=wav` (0xFFFFFFFF RIFF).
11. Do not subscribe to MQTT `#`.
12. Do not transcribe STT until `_conversation_active`.
13. Do not hand-edit `src/protos/`.
14. `SUPPORTED_LANGUAGES` is `ru`, `kk` only. English transcripts will fail chat/TTS until that list is extended on purpose.

---

## 6. Run / verify

```bash
uv sync
echo 'OPENAI_API_KEY=sk-...' > .env
mise start          # broker + service
mise test           # pytest
```

On-device success logs:

```
robot d_… connected to broker
discovered device d_…
bootstrapping device d_… (cloud_tts=1 moxie_mode=None)
injecting canned STT line … phase=wake-gate speech="Moxie, let's go"
Rx remote-chat … command='prompt' module=OPENMOXIE_CHAT/default
Tx remote_chat … text='Привет!
spoke … language=ru
```

Then (after M2): `openai stt transcribed` and `command='continue'` / `reprompt` with child speech.

Reference plan with skip options A–D: `docs/SPEAK_LIVE_PLAN.md`. Puppet line-2 / identity-crash evidence: `docs/SPEAK_PUPPET_ANALYSIS.md`. If either conflicts with this file on current code (e.g. removed bench HTTP, removed `INJECTED_STT_LINE`), **this file wins**.
