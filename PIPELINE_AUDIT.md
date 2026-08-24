# End-to-End Pipeline Audit — Human Speech → Moxie Audio Playback

Audit date: 2026-08-23
Scope: full trace of the speech pipeline against actual code, protobuf definitions,
MQTT topics, serialization, audio formats and configuration.
No code was modified as part of this audit.

> **This audit is historical.** Its findings have since been addressed — see
> `AUDIT_FIXES.md` for what each one turned into. The descriptions below reflect
> the code *before* those fixes, so read them as the rationale for the current
> design rather than as a description of it. Notably: C1 (OpenAI STT) was
> deferred on purpose, C2/C3/C4 are fixed, and the `openmoxie/` reference clone
> is available again, which resolved several UNKNOWNs.

---

## 1. What the pipeline actually does (verified by tracing code)

The requested/assumed pipeline was:

```
Human speech → OpenAI STT → text → OpenAI chat → text → OpenAI TTS → audio → Moxie
```

The **implemented** pipeline is different. Traced through
`mqtt_service.py` → `remote_chat.py` → `openai_chat.py` → robot → `cloud_tts_request.py`
→ `speak_service.py` → `openai_tts.py` → `mqtt_service.publish_cloud_tts_response`:

```
Human speech
  → Moxie ONBOARD speech recognition            (on-robot, not ours)
  → RemoteChatRequest{speech:"..."} on /events/remote-chat
  → remote_chat.RemoteChatBrain                 (ours)
  → OpenAI chat                                 (ours)
  → remote_chat response JSON on /commands/remote_chat
  → Moxie decides to speak the markup
  → CloudTTSRequest{markup, event_id} on /events/zmq
  → cloud_tts_request.handle_payload            (ours)
  → OpenAI TTS                                  (ours)
  → CloudTTSResponse on /commands/zmq
  → Moxie playback
```

`src/services/stt/openai_stt.py` **is never invoked by the robot path.** It is only
reachable via `POST /tts/transcribe` and `POST /tts/converse` (manual uploads).

---

## 2. Stage-by-stage verification

| Stage | Verified? | Result |
|---|---|---|
| Microphone audio → OpenAI STT | **NO** | Not wired. No `zmqSTTRequest` handler exists. See C1. |
| Recognized text → OpenAI chat | Yes | `remote_chat._remote_module_response` passes `request["speech"]` into `openai_chat.respond`. Correct. |
| OpenAI instructed to reply in ru/kk | Partially | System prompt pins the language, but the language is **guessed** from the robot's text. See I11. |
| Reply text → OpenAI TTS | Yes, indirectly | Reply goes to robot as `output.markup`; robot returns it as `CloudTTSRequest.markup`, which is fed to TTS. See I6. |
| TTS output format accepted by Moxie | **UNVERIFIABLE** | Raw PCM16 @ 24 kHz mono assumed. See C4. |
| Audio bytes into `CloudTTSResponse` | Yes | `speak_service._build_response` sets `AudioBuffer.buffer/channels/sample_rate`. Correct. |
| Required protobuf fields populated | Yes (see note) | `event_id`, `chunk_num`, `timestamp`, `total_time`, `synthesis_time`, `software_version`, `module_name` all set. `marks` empty (I10), `request_source` omitted on wire (I9). |
| `event_id` / `chunk_num` echoed | Yes | `cloud_tts_request` passes both through to `speak_service`. Verified correct. |
| Serialization / framing | Yes | Verified: `b"embodied.unity.CloudTTSResponse:" + SerializeToString()`. Matches inbound framing convention. |
| Published to correct topic | **Conditionally NO** | Always published to hardcoded `settings.DEVICE_ID`. See C2. |
| Moxie enabled for Cloud TTS | Yes | `CLOUD_TTS_AUTO_ENABLE=True` → `settings.props.cloud_tts="1"` pushed on `/config`. But see I1. |
| Moxie decodes and plays | **UNKNOWN** | Cannot be verified without the robot/firmware. |
| Error handling can't silently break pipeline | **NO** | Failures publish nothing at all. See C3. |
| `OPENAI_API_KEY` config correct | Partially | Env var works; `.env` is silently ignored. See I2. |

---

## 3. CRITICAL issues (pipeline cannot work / will break)

### C1 — OpenAI STT is not connected to the robot's microphone path

**What is wrong.** The audited requirement "STT correctly receives/processes
microphone audio and produces Russian/Kazakh text" is not met. No component
consumes robot microphone audio.

**Why it is wrong.** `PROTO_SUBSCRIBE_TYPES` requests only
`embodied.unity.CloudTTSRequest`. The vendored proto
`src/protos/embodied/perception/audio/zmqSTT_pb2.py` is never imported. In
`mqtt_service._on_message`, inbound audio is explicitly discarded:

```python
if msg.topic.endswith("events/zmq"):
    if msg.payload.startswith(b"embodied.perception.audio.zmqSTTRequest"):
        logger.info("Audio received")
        return
```

Speech recognition therefore depends entirely on Moxie's onboard recognizer
(config sets `local_stt: "on"`). Whether that recognizer supports Russian or
Kazakh is **UNKNOWN** and is the single largest risk to the whole feature.

**Where.** `src/services/mqtt_service.py` (~lines 198–205, and
`PROTO_SUBSCRIBE_TYPES` ~line 55); `src/services/stt/openai_stt.py` (orphaned
from the robot path).

**How to fix.**
1. Add `"embodied.perception.audio.zmqSTTRequest"` to `PROTO_SUBSCRIBE_TYPES`.
2. Add a handler module (e.g. `src/services/stt_request.py`) that decodes the
   framed `zmqSTTRequest`, accumulates the audio buffer, and calls
   `openai_stt.transcribe(...)`.
3. Feed the resulting text into the brain as a synthetic conversation turn
   instead of relying on `request["speech"]`.
4. Confirm the actual `zmqSTT` message/field names from a live capture before
   implementing — the proto is vendored but its runtime semantics are **UNKNOWN**.

If the intent is to keep using the robot's onboard recognizer, then remove the
claim that OpenAI STT is part of the pipeline and treat C1 as accepted scope.

---

### C2 — CloudTTSResponse is always published to a hardcoded device id

**What is wrong.** The audio reply is published to `settings.DEVICE_ID`, not to
the device that actually sent the `CloudTTSRequest`.

**Why it is wrong.** The inbound dispatcher discards the topic's device id.
`register_cloud_tts_request_handler` uses the signature
`Callable[[bytes], Awaitable]` — payload only — while the remote-chat handlers
correctly receive `(device_id, payload)`. `speak_service.speak` then hardcodes:

```python
mqtt_service.publish_cloud_tts_response(settings.DEVICE_ID, response)
```

**Verified experimentally.** A simulated `CloudTTSRequest` was dispatched and the
response was published to
`/devices/d_d05e3c66-dd29-4fe2-9758-7223e31511dc/commands/zmq` regardless of
which device originated it.

**Impact.** If the robot's real id ever differs from the hardcoded default (new
unit, factory reset, second robot, typo in env override), the audio is published
to a topic nobody is listening on. Symptom is identical to the original bug:
silence plus MarkingUp stall. The brain still answers correctly, which makes this
especially confusing to debug.

**Where.** `src/services/mqtt_service.py` (`CloudTtsRequestHandler`,
`_dispatch_cloud_tts_request`); `src/services/cloud_tts_request.py`
(`handle_payload`); `src/services/speak_service.py` (line ~103).

**How to fix.** Thread the device id through, exactly as the remote-chat path
already does:
1. Change `CloudTtsRequestHandler` to `Callable[[str, bytes], Awaitable[Any]]`.
2. In `_dispatch_cloud_tts_request`, parse the id with the existing
   `_parse_device_id(msg.topic)` and pass it to the handler.
3. Change `handle_payload(device_id, payload)` and
   `handle_cloud_tts_request(device_id, request)`.
4. Add a `device_id: str | None = None` parameter to `speak_service.speak` and
   publish to `device_id or settings.DEVICE_ID`.
5. Update `src/main.py` registration and `tests/test_cloud_tts_request.py`.

---

### C3 — Any synthesis failure publishes nothing, re-creating the original stall

**What is wrong.** When TTS fails, or when `markup` is empty, the service sends
**no** `CloudTTSResponse` at all.

**Why it is wrong.** With `cloud_tts=1` the robot suppresses its onboard TTS and
waits in MarkingUp for a correlated response. No response means the documented
failure mode: force IDLE → sleep → screen off ~5 s. Because cloud TTS is now
auto-enabled at startup (I1), a missing/invalid `OPENAI_API_KEY`, a quota error,
or a network blip reproduces the exact bug this project set out to fix.

**Verified experimentally.**
- TTS raising an exception → `messages published to robot on failure: []`
  (exception propagates to a `logger.exception` in the done-callback only).
- `markup="   "` → `{'status': 'ignored', 'reason': 'empty markup'}` and
  **zero** messages published.

**Where.** `src/services/cloud_tts_request.py` (empty-markup branch ~lines 52–58);
`src/services/speak_service.py` (no try/except around synthesis);
`src/services/mqtt_service.py` `_log_result` (logs and swallows).

**How to fix.**
1. In `handle_cloud_tts_request`, on empty markup publish a valid
   `CloudTTSResponse` with an empty/short silent `AudioBuffer` echoing
   `event_id`/`chunk_num`, instead of returning early.
2. Wrap synthesis in `try/except`; on failure publish the same
   correlated silent response so the robot can leave MarkingUp.
3. Bound synthesis with `asyncio.wait_for(...)` shorter than the robot's
   MarkingUp timeout (see I4).
4. Consider auto-disabling `cloud_tts` after N consecutive synthesis failures so
   the robot falls back to onboard speech rather than sleeping.

---

### C4 — Audio format accepted by Moxie is assumed, not verified (UNKNOWN)

**What is wrong.** The service ships raw headerless PCM by default and there is no
evidence in the repository that Moxie accepts it.

**Why it is wrong.** `CLOUD_TTS_AUDIO_RAW_PCM: bool = True` selects
`response_format="pcm"`, which OpenAI documents as 24 kHz mono signed 16-bit
little-endian **without a container**. `AudioBuffer` carries only
`buffer`/`channels`/`sample_rate` — there is no format/encoding field, so the
robot must already know the expected encoding. If Moxie's player expects a RIFF
WAV container, or a different sample rate (16 kHz / 48 kHz are common), the
message decodes as protobuf but plays as silence or noise. Verified that OpenAI
SDK 3.2.0 accepts both `"pcm"` and `"wav"`, so switching is a config change only.

**UNKNOWN.** Moxie's expected container, sample rate, bit depth and endianness are
not derivable from this repository.

**Where.** `src/config.py` (`CLOUD_TTS_AUDIO_RAW_PCM`, `AUDIO_SAMPLE_RATE_HZ`,
`AUDIO_CHANNELS`); `src/services/synthesis/openai_tts.py` (~line 44).

**How to fix.** Treat this as an explicit A/B experiment on hardware before
trusting any other result:
1. Run one turn with `CLOUD_TTS_AUDIO_RAW_PCM=true` (current default).
2. Run the same turn with `CLOUD_TTS_AUDIO_RAW_PCM=false` (WAV).
3. If both are silent, try resampling to 16 kHz and 48 kHz while keeping
   `AudioBuffer.sample_rate` truthful.
4. Record the working combination in config comments and stop guessing.

---

## 4. IMPORTANT issues (likely to cause failures)

### I1 — Cloud TTS auto-enables before the pipeline is proven healthy

**What/why.** `main.py` sets `cloud_tts=1` at startup purely because the brain was
*registered* — not because it can actually answer or synthesize. The project's own
rule was "never enable `cloud_tts` before the brain is answering". With no API key
this now guarantees the 5-second power-off loop instead of degrading to onboard
speech.
**Where.** `src/main.py` (~lines 80–86); `src/config.py` (`CLOUD_TTS_AUTO_ENABLE`).
**Fix.** Gate auto-enable on a real readiness check: require `OPENAI_API_KEY` to be
present, and preferably only flip to `"1"` after the first successful
`remote_chat` reply for that device (enable inside the brain, then re-push config).
Keep `CLOUD_TTS_AUTO_ENABLE=false` as the safe default for first bring-up.

### I2 — `.env` files are silently ignored

**What/why.** `Settings` has no `SettingsConfigDict(env_file=".env")`. Verified
experimentally: with a `.env` containing `OPENAI_API_KEY`, the loaded value was
`''`. A developer who creates `.env` gets a service that starts fine and then
fails at the first OpenAI call — combined with C3/I1, that means a sleeping robot.
**Where.** `src/config.py` (`class Settings`).
**Fix.** Add `model_config = SettingsConfigDict(env_file=".env", extra="ignore")`,
and fail fast at startup with a clear log line when the key is missing.

### I3 — Outbound audio is echoed back and logged in full (~192 KB per reply)

**What/why.** The client subscribes to `#`, so it receives its own publishes. A
`CloudTTSResponse` on `/commands/zmq` matches none of the mute filters and falls
through to `logger.info("payload: %s", msg.payload)`. Measured: **192,138
characters of log output for a single 48 KB audio message**, written to console
*and* `log/debug.log` (already 5.1 MB). This burns CPU during the latency-critical
window and makes logs unusable for debugging.
**Where.** `src/services/mqtt_service.py` `_on_message` (~lines 198–208) and the
`client.subscribe("#")` diagnostic (~line 145).
**Fix.** Drop `#` (the itemized subscriptions already cover what is needed), skip
messages published to `/commands/`, and truncate payload logging (e.g. first 64
bytes plus length).

### I4 — 30 s OpenAI timeout exceeds the robot's response window

**What/why.** `OPENAI_REQUEST_TIMEOUT_S = 30.0` applies to chat and TTS. A
conversational turn that takes 30 s is already lost: the robot will have
force-idled (MarkingUp) or fallen back locally. The service will then publish
stale audio for an `event_id` the robot no longer awaits.
**UNKNOWN.** The exact MarkingUp timeout is not in this repo.
**Where.** `src/config.py`; `src/services/openai_client.py`.
**Fix.** Reduce to ~5–8 s, add separate chat/TTS budgets, and pair with the
fallback response from C3 so a timeout still frees the robot.

### I5 — Fallback line is English in a Russian/Kazakh deployment

**What/why.** `_FALLBACK_LINE = "I'm sorry. Can you repeat that?"` is used for
onboard-module FALLBACK responses *and* whenever the LLM call fails. With cloud
TTS on, OpenAI will voice this English sentence to a Russian/Kazakh-speaking child.
**Where.** `src/services/remote_chat.py` (~line 36, used ~lines 282–285, ~320).
**Fix.** Move the fallback text into `src/config.py` per language and select it
using the same detected language used for the turn.

### I6 — Markup is passed to TTS without stripping tags

**What/why.** `handle_cloud_tts_request` feeds `request.markup` straight into
OpenAI TTS. The brain currently sends plain text as `markup`, but Moxie is
documented to work in a markup language (mood/animation tags), and the robot may
add its own before requesting synthesis. Any tag that survives is read aloud
literally.
**Where.** `src/services/cloud_tts_request.py` (~line 52);
`src/services/remote_chat.py` (`markup=text`).
**Fix.** Strip tag-like spans (reuse/extend `_ACTION_TAG_RE`) before synthesis, and
log when stripping changes the string so real markup usage becomes visible.
**UNKNOWN.** Whether this firmware echoes markup verbatim — confirm from a capture
of a real `CloudTTSRequest`.

### I7 — Fixed MQTT `client_id` causes connection takeover under `--reload`

**What/why.** `Client(client_id="client_id")` is a constant. `mise dev` runs
`fastapi dev` with reload; during a reload two processes briefly share the id and
the broker disconnects one. Two developers or a stale process produce a permanent
connect/disconnect flap, and messages are lost while disconnected.
**Where.** `src/services/mqtt_service.py` (~line 67).
**Fix.** Use a unique id, e.g. `f"moxie-tts-{uuid.uuid4().hex[:8]}"`, optionally
overridable via settings.

### I8 — The robot's `license` query is never answered

**What/why.** OpenMoxie answered a `client-service-activity-log` query with
`query == "license"` by returning Google speech credentials, which is how a robot
obtains cloud speech recognition. Our `handle_activity_log_event` handles only
`schedule` and `mentor_behaviors`; a `license` query falls through silently. If the
robot needs that response to enable non-English recognition, Russian/Kazakh input
may never be recognized (compounding C1).
**Where.** `src/services/remote_chat.py` (`handle_activity_log_event`, ~lines 366–372).
**UNKNOWN.** Whether this firmware requires a license response, and whether onboard
`local_stt` covers ru/kk.
**Fix.** Log unhandled query types explicitly at INFO so a real capture shows
whether `license` is requested, then decide whether to answer it.

### I9 — `request_source` is always ROBOT_TTS_REQUEST and never appears on the wire

**What/why.** `cloud_tts_request` hardcodes `ROBOT_TTS_REQUEST`. Verified that in
proto3 the zero value is **not serialized**, so the field is absent and the robot
falls back to its own default. Speech that originates from a remote-chat module may
be expected to carry `REMOTECHAT_TTS_REQUEST (1)`, which *would* be serialized.
**Where.** `src/services/cloud_tts_request.py` (~line 76); `speak_service.speak`
default argument.
**UNKNOWN.** Which value this firmware requires.
**Fix.** Make it configurable and test both values on hardware; the plumbing
already accepts `request_source` as a parameter.

### I10 — `marks` is always empty (MarkingUp risk)

**What/why.** `CloudTTSResponse.marks` is never populated. The failure state named
in the original bug report is literally "MarkingUp". If the robot requires at least
one `TTSMark` to advance its lip-sync state machine, correct audio still may not
play.
**Where.** `src/services/speak_service.py` `_build_response`.
**UNKNOWN.** Whether marks are mandatory, and the expected `type`/`value` vocabulary.
**Fix.** Capture a genuine `CloudTTSResponse` from real hardware to learn the mark
schema; until then, test whether a single synthetic mark spanning the utterance
changes playback behaviour.

### I11 — Language detection is a heuristic that defaults to Russian

**What/why.** `detect_language` returns `kk` only if a Kazakh-specific Cyrillic
letter appears, otherwise `ru`. A Kazakh sentence without those letters is treated
as Russian and OpenAI is explicitly instructed "You MUST reply only in Russian".
If the robot's recognizer emits Latin/English text, it is also classified as
Russian.
**Where.** `src/services/language.py`; used in `remote_chat` (~line 269) and
`cloud_tts_request` (~line 60).
**Fix.** Prefer an explicit language signal when the robot provides one; otherwise
relax the system prompt to "reply in the user's language" rather than asserting a
possibly-wrong language, and log the detected language per turn.

---

## 5. MINOR issues

| ID | Issue | Where | Fix |
|---|---|---|---|
| M1 | Comment says "Default False => full WAV bytes" but the value is `True` (PCM) | `src/config.py` | Correct the comment to match the value |
| M2 | `/tts/speak` and `/tts/converse` cannot drive playback (random `event_id`), but `/converse`'s docstring says it "publishes the spoken reply to the robot" | `src/router/tts.py`, `src/services/conversation.py` | Reword docstrings as bench-only |
| M3 | `event_id or None` turns an empty id into a random UUID the robot will reject | `src/services/cloud_tts_request.py` (~line 74) | Echo the empty value or skip publishing explicitly |
| M4 | `CloudTTSSupplement` is defined in the proto but never sent | `src/services/speak_service.py` | Determine necessity from a capture (**UNKNOWN**) |
| M5 | paho callback API v1 deprecation warning on every run | `src/services/mqtt_service.py` | Migrate to `CallbackAPIVersion.VERSION2` |
| M6 | Every `/state` message schedules a coroutine even after bootstrap | `src/services/mqtt_service.py` `_dispatch_device_event` | Short-circuit once the device is initialized |
| M7 | `MQTT_ENDPOINT` is a dict, so env override requires JSON | `src/config.py` | Split into `MQTT_HOST` / `MQTT_PORT` |
| M8 | `README.md` is empty; run instructions live only in `FIX_EXPLANATION.md` | `README.md` | Add setup/run/verify instructions |
| M9 | Tests never cover the wrong-device-id or failure-publish paths | `tests/` | Add regression tests for C2 and C3 |

---

## 6. Assumptions that cannot be verified from the code (UNKNOWN)

1. **Audio encoding Moxie accepts** — container (raw PCM vs WAV), sample rate, bit depth, endianness.
2. **Whether `marks` are required** to leave the MarkingUp state.
3. **Whether `request_source` must be `REMOTECHAT_TTS_REQUEST`** for remote-chat speech.
4. **Whether Moxie's onboard recognizer supports Russian/Kazakh at all.**
5. **Whether the robot requires a `license` response** to enable cloud speech recognition.
6. **The robot's MarkingUp timeout duration** (drives the latency budget).
7. **Whether `CloudTTSSupplement` is expected** alongside the response.
8. **Whether `ProtoSubscribe` is required** for `CloudTTSRequest` delivery, or whether `cloud_tts=1` alone suffices.
9. **Whether the robot echoes `markup` verbatim** or adds its own tags.
10. **Whether the schedule payload is sufficient** — the shape is minimal and unvalidated.
11. **Config value types** (e.g. `audio_volume: 1.0` float vs OpenMoxie's `"0.6"` string).
12. **OpenAI STT quality for Kazakh** with `gpt-4o-transcribe`.

All twelve require a live capture against real hardware (ideally alongside a
working OpenMoxie instance) to resolve. Guessing at them is what produced the
original failures.

---

## 7. Verdict and priorities

### End-to-end verdict

**The pipeline should NOT be expected to work end to end as currently implemented.**

The conversational half is sound: the remote-chat brain answers the modules and
schedule handshake, generates replies via OpenAI, and the synthesis/framing/echo
logic for `event_id`/`chunk_num` is correct. That is a real improvement over the
original state.

But the pipeline as *specified* is broken in two structural ways and unverified in
a third:
- OpenAI STT is not in the robot path at all (**C1**).
- Audio is published to a hardcoded device id (**C2**).
- The accepted audio format is unverified while cloud TTS auto-enables (**C4**),
  and any failure publishes nothing, which reproduces the original wake-and-sleep
  bug (**C3**).

Best case — the hardcoded device id happens to be correct, the robot's onboard
recognizer handles Russian, and Moxie accepts raw 24 kHz PCM — it can work. None
of those three conditions is verified.

### Critical to fix first
1. **C3** — always publish a correlated response, even on failure/empty markup.
2. **C2** — route the response to the requesting device id.
3. **C4** — determine the real audio format on hardware (PCM vs WAV).
4. **C1** — decide explicitly: wire OpenAI STT to `zmqSTTRequest`, or drop it from the stated pipeline.

### Important to fix next
5. **I1** — do not auto-enable cloud TTS without a readiness check.
6. **I2** — load `.env` and fail fast on a missing key.
7. **I3** — stop echoing/logging full audio payloads.
8. **I4** — cut the OpenAI timeout to fit the turn window.
9. **I5** — localize the fallback line.
10. **I6** — strip markup before synthesis.
11. **I7** — unique MQTT client id.
12. **I8/I9/I10/I11** — resolve via capture, then implement.

### Files that need modification

| File | For |
|---|---|
| `src/services/mqtt_service.py` | C1, C2, I3, I7, M5, M6 |
| `src/services/cloud_tts_request.py` | C2, C3, I6, I9, M3 |
| `src/services/speak_service.py` | C2, C3, I10 |
| `src/config.py` | C4, I1, I2, I4, I5, M1, M7 |
| `src/main.py` | C1, C2, I1 |
| `src/services/remote_chat.py` | I5, I8, I11 |
| `src/services/synthesis/openai_tts.py` | C4 |
| `src/services/language.py` | I11 |
| `src/services/stt/openai_stt.py` + new `src/services/stt_request.py` | C1 |
| `src/router/tts.py`, `src/services/conversation.py` | M2 |
| `tests/` | M9 |
| `README.md` | M8 |

---

## 8. Recommended end-to-end test procedure (Russian + Kazakh)

**Phase 0 — bench, no robot**
1. `export OPENAI_API_KEY=...` (env var, not `.env`, until I2 is fixed).
2. `uv run pytest -q` → expect 34 passing.
3. `POST /tts/respond` `{"text":"Привет, как тебя зовут?","language":"ru"}` → Russian reply.
4. `POST /tts/respond` `{"text":"Сәлем! Сенің атың кім?","language":"kk"}` → Kazakh reply.
   *If this returns Russian, I11 is confirmed on real data.*
5. `POST /tts/speak` for both languages → confirm a `CloudTTSResponse` is published
   and note `audio_bytes`. Playback is not expected (random `event_id`).

**Phase 1 — brain only, cloud TTS OFF**
6. Start with `CLOUD_TTS_AUTO_ENABLE=false mise start`.
7. Power on Moxie. Confirm in logs:
   - `bootstrapping device ... (cloud_tts=0)`
   - `answering modules query ...` (a few times, then stops)
   - `answering schedule query ...`
8. Speak Russian, then Kazakh. Confirm `Tx remote_chat ... output_type=GLOBAL_RESPONSE`
   with sensible reply text, and that the robot speaks using onboard TTS.
   **Record what `speech` text the robot actually sent** — this answers UNKNOWN 4
   (does onboard recognition handle ru/kk at all).
9. Watch for any unanswered `license` query (I8).

**Phase 2 — cloud TTS ON, format experiment**
10. Only after Phase 1 is clean: `POST /tts/cloud-tts {"enabled": true}`; confirm
    `/state` reports `cloud_tts:"1"`.
11. Speak one Russian sentence. Expect:
    - `inbound CloudTTSRequest ... event_id=<E>`
    - `spoke event_id=<E>` with the **same** id
    - `publishing embodied.unity.CloudTTSResponse ...`
    - audible OpenAI audio, and **no** "Forcing IDLE / MarkingUp" on the robot.
12. If silent: restart with `CLOUD_TTS_AUDIO_RAW_PCM=false` and repeat (C4).
13. Repeat the working configuration in Kazakh.
14. Negative test: temporarily unset `OPENAI_API_KEY`, speak once, and confirm
    whether the robot stalls and sleeps — this demonstrates C3 until it is fixed.

**Phase 3 — regression**
15. Leave the robot idle 10 minutes with cloud TTS on; confirm it stays awake.
16. Power-cycle the robot and confirm bootstrap + a working turn without
    restarting the service.

---

## 9. Corrected architecture diagram (actual data flow)

```
┌──────────────────────────── MOXIE ROBOT ────────────────────────────┐
│  microphone                                                          │
│      │                                                               │
│      ▼                                                               │
│  ONBOARD speech recognition        ← ru/kk support UNKNOWN (C1)      │
│      │  recognized text                                              │
└──────┼───────────────────────────────────────────────────────────────┘
       │  RemoteChatRequest {backend:"router", command, speech, event_id}
       │  topic: /devices/{id}/events/remote-chat        (JSON)
       ▼
┌──────────────────── THIS SERVICE (FastAPI) ─────────────────────────┐
│  mqtt_service._on_message → _dispatch_device_event                   │
│      ▼                                                               │
│  remote_chat.RemoteChatBrain                                         │
│      ├─ modules query   → /commands/remote_chat  {query_data}        │
│      ├─ schedule query  → /commands/query_result {schedule}          │
│      └─ router turn:                                                 │
│            language = detect_language(speech)     ← heuristic (I11)  │
│            openai_chat.respond(speech, language, history)            │
│                    │ reply text (ru/kk)                              │
│                    ▼                                                 │
│            response JSON {output:{text, markup}}                     │
└──────┬───────────────────────────────────────────────────────────────┘
       │  topic: /devices/{id}/commands/remote_chat      (JSON)
       ▼
┌──────────────────────────── MOXIE ROBOT ────────────────────────────┐
│  accepts response → prepares to speak → enters MarkingUp             │
│  cloud_tts = "1"  ⇒ onboard TTS suppressed                           │
└──────┬───────────────────────────────────────────────────────────────┘
       │  CloudTTSRequest {markup, event_id=E, chunk_num=N}
       │  framed: b"embodied.unity.CloudTTSRequest:" + bytes
       │  topic: /devices/{id}/events/zmq
       ▼
┌──────────────────── THIS SERVICE (FastAPI) ─────────────────────────┐
│  mqtt_service._dispatch_cloud_tts_request                            │
│        ⚠ device id from topic is DISCARDED (C2)                      │
│      ▼                                                               │
│  cloud_tts_request.handle_payload                                    │
│        ⚠ markup not tag-stripped (I6)                                │
│        ⚠ empty markup ⇒ NOTHING published (C3)                       │
│      ▼                                                               │
│  speak_service.speak(text, language, event_id=E, chunk_num=N)        │
│      ▼                                                               │
│  openai_tts.synthesize  → PCM16 24 kHz mono   ← accepted? UNKNOWN(C4)│
│        ⚠ exception ⇒ NOTHING published (C3)                          │
│      ▼                                                               │
│  CloudTTSResponse{                                                   │
│      audio{buffer, channels=1, sample_rate=24000},                   │
│      event_id=E, chunk_num=N,          ← correctly echoed ✅          │
│      timestamp, synthesis_time, total_time,                          │
│      software_version, module_name,                                  │
│      marks=[]                          ← empty (I10)                 │
│      request_source=0                  ← absent on wire (I9)         │
│  }                                                                   │
│      ▼                                                               │
│  framed: b"embodied.unity.CloudTTSResponse:" + bytes   ✅            │
│  published to /devices/{settings.DEVICE_ID}/commands/zmq             │
│        ⚠ hardcoded id, not the requester (C2)                        │
└──────┬───────────────────────────────────────────────────────────────┘
       ▼
┌──────────────────────────── MOXIE ROBOT ────────────────────────────┐
│  matches event_id ⇒ plays audio ✅                                    │
│  no match / no response ⇒ MarkingUp timeout → force IDLE → sleep     │
└──────────────────────────────────────────────────────────────────────┘

SIDE PATH (bench only — never used by the robot):
   WAV upload → POST /tts/transcribe|/tts/converse → openai_stt.transcribe
   → openai_chat → speak_service (random event_id ⇒ robot discards)
```
