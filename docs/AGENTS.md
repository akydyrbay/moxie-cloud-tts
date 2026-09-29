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

**Invariant:** Moxie only plays cloud audio that answers a request it is already waiting on, matched by `event_id` / `chunk_num`. You cannot push an arbitrary `CloudTTSResponse` and have it play. Two live requests exist:

- Robot lines: a `CloudTTSRequest` → answer with its `event_id`/`chunk_num`, `ROBOT_TTS_REQUEST`.
- Remote-module output (`OPENMOXIE_CHAT` GLOBAL_RESPONSE): firmware raises **no** `CloudTTSRequest` (Sep 28 captures). The server answers that live router turn with one `CloudTTSResponse`, `REMOTECHAT_TTS_REQUEST`, `event_id` = the router `event_id`, `chunk_num=0`, only when `cloud_tts=1` and puppet is off. The later Sep 28 run and operator report confirm Kazakh replies to English input.

**Current milestone (2026-09-29):** wake-up and Kazakh answers to English input
are operator-confirmed. The evening Sep 28 capture shows 13 Cyrillic transcripts
without router turns and 9 English-containing transcripts with them. The English
input bridge, original-text recovery, bounded provider waits and reply validation
are implemented with 322 passing tests. Hardware bridge acceptance remains;
automatic output-language switching is a separate milestone. See
[localization repair](LOCALIZATION_FIXES.md).

`CLOUD_TTS_TRANSLATION_ENABLED=true` translates incoming English/Latin autonomous
TTS through `translation.py` to `DEFAULT_TTS_LANGUAGE=kk`. Cyrillic ru/kk and
puppet speech bypass translation. No wake phrase allowlist or chat-active gate
is used. Removed settings `WAKEUP_TTS_TEXT`, `WAKEUP_TTS_OVERRIDE_ENABLED` and
`REMOTE_CHAT_LANGUAGE_GUARD_ENABLED` must not be restored.

WAKEUP/ENROLLCONVO keep ordinary FALLBACK, preserving native responses/actions.
The remote opener is English source text; with `cloud_tts=1` it is translated
before the remote-chat response and voiced as REMOTECHAT audio (no robot request
ever carries it). Chat replies remain in the configured output language. A nonempty
first `reprompt` now reaches the LLM even without notify history.

Translation preserves original markup, IDs, chunks and command marks; failures
produce correlated silence. STT preserves the original human transcript server-side and sends its faithful
English translation to the robot under the same UUID. The bridge can be disabled
for A/B comparison. `OPENAI_STT_TIMEOUT_S=6` bounds recognition plus translation,
with no SDK retries; failures produce FINAL error 66. STT supports missing START, END audio,
bounded buffers/expiry, deduplication and reset/exit cancellation. See
[STT implementation and verification](STT_WIRING.md) and
[conversation plan](SPEAK_LIVE_PLAN.md).

---

## 2. Component breakdown

### 2.1 Two operating modes (do not mix)

| Mode | How enabled | What it is | Status |
|---|---|---|---|
| **TELEHEALTH / puppet** | `POST /tts/puppet` or `PUPPET_MODE_AUTO_ENABLE` | Robot stays awake, **schedule empty**, speaks only `PLAY_OUTPUT` from `/tts/speak` | **Proven for first-line TTS. Dropped as the conversation path.** Code kept. **Do not modify this pipeline.** |
| **Autonomous / remote-chat** | `PUPPET_MODE_AUTO_ENABLE=false` (default) | Robot runs `WAKEUP` then `OPENMOXIE_CHAT`; this server supplies opener + chat text; server answers its live router turn with REMOTECHAT audio | **Target path.** Kazakh replies heard; English input bridge awaits hardware acceptance. |

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
  stt_request.py            ears: wake shim + bounded chat audio transcription
  cloud_tts_request.py      mouth inbound: CloudTTSRequest → translation → speak_service
  translation.py           English/Latin → kk/ru, bounded deadline and text cache
  speak_service.py          OpenAI TTS → CloudTTSResponse (shared by both modes)
  telehealth.py             puppet PLAY_OUTPUT / INTERRUPT / wakeup / sleep  [DO NOT TOUCH]
  tts_service.py            config push + puppet toggle (identity strip)
  marks.py                  viseme + cmd: echo so firmware leaves MarkingUp
  audio.py                  PCM duration; WAV repair (never ask OpenAI for wav)
  stt/openai_stt.py         OpenAI STT engine — auto input, no imperative prompt/retries
  stt/input_translation.py  Human kk/ru → English compatibility text
  stt/transcripts.py        Bounded original/wire transcript correlation
  stt/validation.py         Prompt-echo rejection and conservative audio checks
  provider_work.py          Bounded pure provider workers and total deadlines
  llm/reply_text.py         Whole-sentence spoken reply limits
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
| **Ears** | `zmqSTTRequest` / `zmqSTTResponse` | Mic PCM in; faithful English translation of recognized human speech out. Original text retained for GPT. Cannot make Moxie speak. |
| **Mouth** | `CloudTTSRequest` / `CloudTTSResponse` | Robot asks to voice markup; **audio** out. Only way ru/kk is heard. |
| **Brain** | JSON `remote_chat` / `query_result` | Handshake, module, **text Moxie will say** (`output.markup`). |

Spoken ru/kk on the conversation path = `RemoteChatResponse.output.markup` plus a REMOTECHAT `CloudTTSResponse` for that router `event_id` (`remote_chat._voice_remote_output`). The robot does **not** copy remote markup into a `CloudTTSRequest`; native lines still arrive as requests and go through `cloud_tts_request`. Do not put spoken lines in STT `speech`.

### 2.7 What already works (do not re-investigate)

- MQTT connect, device discovery, bootstrap config + ProtoSubscribe.
- `cloud_tts=1` auto-enable when `OPENAI_API_KEY` is set.
- Cloud TTS of whatever markup the robot requests (PCM 24 kHz mono, marks, silence on empty/fail).
- Identity strip on mid-session config toggle.
- Wake-gate STT inject: on `END_OF_SPEECH` before chat, reply `WAKEUP_TRIGGER_LINE` = `"Moxie, let's go"`.
- Remote-chat: modules handshake, schedule (`OPENMOXIE_CHAT/default`), mentor_behaviors `[]`.
- WAKEUP/`ENROLLCONVO`: ordinary `FALLBACK`, leaving native transitions intact. English speech translation happens in Cloud TTS. Do not restore the entrance `GLOBAL_COMMAND`; see the handoff diagnosis.
- `OPENMOXIE_CHAT` `prompt` → English source `REMOTE_CHAT_OPENERS`, translated (cloud_tts=1), wrapped in `<usel variant="2" genre="none">`, then REMOTECHAT audio on the prompt `event_id`.
- `OPENMOXIE_CHAT` `continue`/`reprompt` → `openai_chat.respond`, then REMOTECHAT audio on that turn's `event_id`.
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
5. Firmware still runs onboard **WAKEUP**. STT inject `"Moxie, let's go"`. Entrance volley gets FALLBACK; native actions can transition into chat. Every incoming English TTS line is translated, including Good morning, questions and clarifications.
6. Target: `OPENMOXIE_CHAT` `command=prompt` → opener → Cloud TTS → listen.
7. Disconnect/reboot: `$SYS` connect/disconnect logs → `forget_bootstrap` and `remote_chat_brain.reset_device` on the asyncio loop. Pending recognition and remote output are invalidated immediately; the next session bootstraps again.

### Session state (in-process, not Redis)

| Store | Lifetime |
|---|---|
| `device_registry._last_seen` / `_bootstrapped` | until disconnect or process restart |
| `device_state._config` | process lifetime; overlay `cloud_tts`, `moxie_mode` |
| `device_state.speech_revision` | advances on actual cloud-TTS/puppet mode changes; prevents pending remote output surviving an off/on toggle |
| `stt_request._conversation_phase` | entrance/chat/onboard from actual router turns; reset on reconnect bootstrap |
| `stt_request._sessions` / `_completed` | bounded PCM/pending worker per device+UUID; 120-second bounded tombstones prevent duplicate or stale replies |
| `remote_chat._sessions` | per device, output language/active flag, current turn token and bounded event-based input/audio-outcome history; removed on module exit, sleep, reset or shutdown |
| `stt.transcripts._entries` | original/wire text by device+UUID, 256 entries, 120-second TTL, reset and mode-revision invalidation |
| `remote_chat._last_event_id` | last router event (for HTTP sleep) |
| `telehealth._puppet_state` | READY/IN_SESSION/EXITING from activity-log **session** boundaries only |

There is no durable robot session store. Restarting this service forgets devices until they publish again.

Remote replies check session identity, turn identity and the speech revision
before publishing text or audio. The optional `speak_service.speak(is_current=...)`
guard also suppresses stale failure silence after synthesis. Background provider
threads can finish, but invalidated results cannot publish. History-only notify
messages do not replace the current turn.

---

## 4. Roadmap — remaining Cloud TTS conversation pipeline

**Do not add HTTP endpoints to make the robot speak on this path.** Native requests and accepted remote turns already use their respective correlated CloudTTSResponse sources. Remaining work is hardware acceptance of the input bridge and sustained conversation.

### 4.1 M1 — Prove `OPENMOXIE_CHAT` on hardware

**Goal:** after wakeup + `"Moxie, let's go"`, logs show:

```
Rx remote-chat … module=OPENMOXIE_CHAT … command=prompt event_id=<P>
remote-chat entered … module=OPENMOXIE_CHAT/default
translated speech … target=kk
Tx remote_chat … event_id=<P> output_type=GLOBAL_RESPONSE text='<Kazakh opener>'
remote-chat audio … event_id=<P> chunk=0 source=REMOTECHAT …
spoke event_id=<P> chunk=0 language=kk …
remote-chat notify … source_event_id='<P>' …
```

Entry and subsequent Kazakh speech were observed Sep 28. Remote output receives
source-1 audio directly; it raises no native TTS request. The server fix restores FALLBACK for entrances; English speech uses the
translation stage. Notify/SCHEDULED alone does not establish entry. Translation
works in other native modules even if this handoff has not completed.

If entry still fails, preserve the fresh capture and diagnose it before testing
secondary launch/exit actions. Do **not** set `brain_entrances_available="0"`.
Those actions are experiments, not an established fix. The primary response
must stay speak-only. Tests: `test_remote_chat.py`, `test_remote_chat_handoff.py`.

**Do not** mark conversation active on `OPENMOXIE_CHAT` `notify`.

### 4.2 M2 — OpenAI STT wired; verify the microphone

`stt_request.py` reuses `stt/openai_stt.py` and `audio.pcm_to_wav`. Reference
OpenMoxie accumulates incremental chunks at 16 kHz mono PCM16. Our handler:

- Keeps the wake shim only in the entrance phase, transcribes only in chat,
  and completes other onboard listens with FINAL error 66.
- Includes START, SPEECH and END audio, including missing START / END-only
  payloads. Caps each recording at 30 seconds, two sessions/device, 16 globally,
  and expires streams lacking END after 30 seconds.
- Runs recognition off-loop without the former imperative prompt. Original
  human text is validated and retained; a separate English translation is sent
  to the robot by default, never an assistant answer. Auto-mode language is
  unknown. An explicit STT input hint is separate from the response language.
- Limits total recognition plus bridge wait with `OPENAI_STT_TIMEOUT_S=6`
  (maximum 8); provider retries are disabled. Input translation uses at most
  2 seconds and only the remaining total budget. Timeout/failure,
  empty recognition or invalid audio returns the existing FINAL error 66.
- Echoes UUID and valid start/end timestamps from the robot's clock. Duplicate
  END yields one response. Reset/exit/reconnect discards audio and cancels the
  awaiting task. Bounded short-lived UUID tombstones deliberately survive the
  reset so late frames cannot reopen old listens. Synchronous worker completion
  after cancellation cannot publish anything.

The necessary integration changes include the provider retry override, config
validation, immediate STT reset scheduled from the broker thread, shutdown
cleanup, and forwarding nonempty first-reprompt input. No MQTT framing or
puppet changes are needed. Never send unsolicited TTS or introduce an HTTP
transcription path. See [STT_WIRING](STT_WIRING.md) for regression/live checks
and the remaining physical microphone acceptance.

### 4.3 M3/M4 — Bilingual replies and repeated conversation

Input bridging, nonempty first reprompts, silence handling, event-based history,
short replies and deadlines are implemented. Automatic kk/ru output selection
and sustained on-device conversation acceptance remain.
Follow the detailed milestones in `docs/SPEAK_LIVE_PLAN.md`.

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

### 4.5 Integration verification

Tests mock MQTT + OpenAI (`tests/conftest.py`). Current STT coverage includes:

| Test | Assert |
|---|---|
| `tests/test_stt_request.py` | wake-gate still injects `"Moxie, let's go"` |
| same | after `note_conversation_active`, accumulated PCM → fake `openai_stt.transcribe` → FINAL with echoed `uuid` |
| same | WAV passed to STT is **16 kHz** |
| same | STT failure still publishes a response |
| `tests/test_remote_chat.py` | notify does not call `note_conversation_active` (already) |
| `tests/test_stt_log_replay.py` | Sep 28 UUID/phase replay with synthetic kk/ru input → brain → matching TTS |
| On-device (not pytest) | log sequence in §4.1; then speak ru/kk; `openai stt transcribed`; English FINAL matches the router UUID; original text is recovered; reply TTS uses the configured output language |

Do not require a live robot in pytest.

---

## 5. Hard rules (do not break)

1. **Do not modify** `src/services/telehealth.py` speak/interrupt sequencing, or the puppet `/tts/speak` path, unless the operator explicitly asks.
2. `PUPPET_MODE_AUTO_ENABLE` stays **false** for conversation work.
3. Do not mix ears / mouth / brain. Spoken ru/kk is only a correlated `CloudTTSResponse`: to a robot `CloudTTSRequest`, or REMOTECHAT for an accepted remote-module router turn (never for FALLBACK/onboard turns, never from HTTP).
4. Do not put spoken lines in `zmqSTTResponse.speech`.
5. Do not re-push `child_pii` / `pairing_status` on live toggles.
6. Do not set `brain_entrances_available="0"`.
7. Do not put `launch` / `sleep` as the **primary** `response_action` (skips Cloud TTS). Speak-only primary, side-effect as later `response_actions[]`.
8. Echo `event_id` and STT `uuid` verbatim. Empty telehealth `event_id` is valid — still echo it.
9. Every `CloudTTSRequest` gets exactly one `CloudTTSResponse` (audio or silence).
10. Never ask OpenAI TTS for `response_format=wav` (0xFFFFFFFF RIFF).
11. Do not subscribe to MQTT `#`.
12. Do not transcribe STT until the device's conversation phase is `chat`.
13. Do not hand-edit `src/protos/`.
14. Output `SUPPORTED_LANGUAGES` remains ru/kk. Recognition can accept kk/ru/en;
    the English input bridge does not require enabling English assistant output.

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
remote-chat entered … module=OPENMOXIE_CHAT/default
Tx remote_chat … output_type=GLOBAL_RESPONSE text='<Kazakh opener>'
remote-chat audio … source=REMOTECHAT …
spoke event_id=<prompt id> chunk=0 language=kk
```

Then: `STT END`, `openai stt transcribed`, `STT FINAL ... speech=`, `command='continue'` / `reprompt` with child speech, and REMOTECHAT audio for that turn's `event_id`.

Current conversation roadmap: `docs/SPEAK_LIVE_PLAN.md`. Puppet line-2 / identity-crash evidence:
`docs/SPEAK_PUPPET_ANALYSIS.md`. The current task brief is `prompt.md`.
