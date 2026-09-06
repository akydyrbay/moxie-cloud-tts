# Making Moxie Speak Without Puppet Mode — Implementation Plan

Status: **IN PROGRESS.** Last updated 2026-09-04. Target: run Moxie in its
normal (non-puppet) conversational mode and have it speak Russian/Kazakh.

---

## 0. CURRENT STATUS — read this first (2026-09-04)

### The one-line problem

The robot now wakes, stays awake, and speaks — **but it speaks its own onboard
English onboarding modules (`WAKEUP` → `ENROLLCONVO` "Clearance Check" / getting-
to-know-you questions) and never reaches `OPENMOXIE_CHAT`**, the one module where
this server controls the language (ru/kk). We synthesize that English text
faithfully (English in → English audio out). **The task is now: get the robot
past its onboard onboarding into `OPENMOXIE_CHAT`, or otherwise suppress/replace
the English onboard content with ru/kk.**

### What already works (do not re-investigate — see files below)

| Capability | Where | Status |
|---|---|---|
| Wake + reach `mode:active`, no stall | firmware + entry-seq fix | ✅ |
| **Entry-sequence completes** (loading bubbles resolve) — robot's `cmd:` marks echoed back | `src/services/marks.py::command_marks` (line 103), threaded through `src/services/speak_service.py::_build_response` | ✅ (this unblocked everything) |
| **Config push no longer crashes the brain** — identity stripped from toggle pushes | `src/services/tts_service.py::_without_identity_fields` (line 35), `_IDENTITY_FIELDS` (line 32) | ✅ verified |
| **STT injection** — reply to the robot's `zmqSTTRequest` stream with a canned line | `src/services/stt_request.py` (whole file; `handle_stt_request` line 93) | ✅ (fires every session) |
| Cloud TTS synthesis + playback of a normal turn | `src/services/cloud_tts_request.py` → `src/services/speak_service.py::speak` | ✅ (robot audibly speaks) |
| Inbound remote-chat visibility | `remote_chat.py::handle_remote_chat_event` (line 372) logs every `Rx remote-chat …` | ✅ diagnostic |

### The blocker, precisely (evidence: 16:28–16:31 run)

On wake the robot runs its **onboard** module flow, which this server does not
schedule and cannot fully drive:

1. `Rx remote-chat backend='router' command='notify' module=WAKEUP speech='Here
   we go again! …'` → robot speaks the WAKEUP line (English) via our cloud TTS.
2. Our STT handler injects `WAKEUP_TRIGGER_LINE` = `"Moxie, let's go"` (phase
   `wake-gate`). The robot forwards it as `module=WAKEUP command=continue`.
3. Robot advances to **`ENROLLCONVO`** ("Clearance Check" / mentor enrollment):
   `speech='Excellent! Activating clearance check for pat! … say: I am your Moxie
   Mentor!'`, then scripted questions (`'Do you prefer chocolate or strawberry?'`,
   `'fly or become invisible?'`). All English; all synthesized by us.
4. `ENROLLCONVO` is **not** a remote module (`is_remote_module` is True only for
   `OPENMOXIE_CHAT`), so every `command=continue/start` for it hits the
   **FALLBACK** branch (`remote_chat.py::handle_router_request` line ~356) →
   `'Извини, я не расслышала…'`. Our injected `"Moxie, let's go"` never answers
   the enrollment questions, so the robot loops in `ENROLLCONVO` and re-asks.
5. **`OPENMOXIE_CHAT` is never launched**, so `note_conversation_active` never
   fires and STT injection stays stuck in `wake-gate` forever (never switches to
   `INJECTED_STT_LINE = "Привет, как дела?"`).

Net: the robot is stuck in the onboard English **enrollment** flow and never
reaches the ru/kk chat module.

### Why the robot runs `ENROLLCONVO` at all

It is the robot's **onboarding / mentor "Clearance Check"** gate. The robot runs
`WAKEUP` → `ENROLLCONVO` as a brain-entrance sequence **before** it will run the
cloud-provided schedule (our `DEFAULT_SCHEDULE` is `only_chat` = just
`OPENMOXIE_CHAT`, `config.py::DEFAULT_SCHEDULE` line 227). Enrollment is gated on
mentor/PII state and completing the scripted Q&A — neither of which our injected
line satisfies. Note the tension with the crash fix: stripping `child_pii`
(§ crash fix) can push the robot toward "not enrolled," which is exactly the
`ENROLLCONVO` path. In this run PII was present at bootstrap (bootstrap does not
strip — only mid-session toggles do), yet `ENROLLCONVO` still ran, so completing
enrollment — not just having PII — appears to be the gate.

### Where to look (file/function map)

- **Schedule answer** (what module the robot is told to run):
  `src/services/remote_chat.py::handle_schedule_query` (line 212) →
  `src/config.py::DEFAULT_SCHEDULE` (line 227, currently `only_chat`).
- **Modules handshake** (what modules exist): `handle_modules_query` (line 190)
  → `src/config.py::DEFAULT_REMOTE_MODULES_INFO` (line 176). Only advertises
  `OPENMOXIE_CHAT`.
- **Onboard-module requests → FALLBACK** (this is where `WAKEUP`/`ENROLLCONVO`
  land, and the natural place to intercept): `handle_router_request` (line 331);
  remote vs onboard split via `is_remote_module` (line 161); FALLBACK at ~356.
- **Action tags** (`<exit>`/`<sleep>`/`<launch:…>` → response actions) —
  candidate lever to make the robot LEAVE an onboard module:
  `remote_chat.py::_strip_action_tags` (line 88); consumed in
  `_remote_module_response` (line 275). Currently only applied to `OPENMOXIE_CHAT`
  replies, NOT to the onboard FALLBACK.
- **STT injection + phase switch**: `src/services/stt_request.py::_injected_line`
  (line 59, `wake-gate` vs `chat`), `note_conversation_active` (line 49, called
  from `handle_router_request` line 343 only when `OPENMOXIE_CHAT` is reached),
  config `WAKEUP_TRIGGER_LINE` (line 102) / `INJECTED_STT_LINE` (line 92).
- **Openers** (ru/kk lines once `OPENMOXIE_CHAT` starts): `config.py::REMOTE_CHAT_OPENERS`
  (line 201).

### What to try next (directions, unverified)

Goal: reach `OPENMOXIE_CHAT` (ru/kk) instead of looping in English `ENROLLCONVO`.

1. **Make the robot skip onboarding / mark it already enrolled.** Investigate the
   device props that gate brain entrances / onboarding (`brain_entrances_available`,
   `schedule_sensitive`, enrollment/PII state) in the pushed config
   (`config.py::HARDCODED_DEVICE_CONFIG`) and how OpenMoxie sets a robot as
   already-enrolled. If the robot believes onboarding is done, it should run the
   scheduled `OPENMOXIE_CHAT` directly. (Compare OpenMoxie's `no_onboarding`
   schedule and its enrollment/PII handling — cloned at `~/Desktop/hri-lab/openmoxie`.)
2. **Force-exit the onboard modules.** For `module=WAKEUP`/`ENROLLCONVO` router
   requests, instead of FALLBACK, reply with an **exit/skip action** so the robot
   leaves the onboard module and advances to the next scheduled item
   (`OPENMOXIE_CHAT`). Extend `handle_router_request` to send an `exit_module`
   action for onboard modules (see `_strip_action_tags` for the action shape).
   Unknown: whether the firmware honors a cloud-sent exit for a LOCAL module.
3. **Suppress the English audio meanwhile.** If skipping proves hard, detect
   onboard-module CloudTTSRequests and publish silence instead of English speech
   (in `cloud_tts_request.py`), so at least Moxie is not talking English — but
   this leaves it stuck, so it is a stopgap, not a fix.
4. **Answer the enrollment to completion.** Feed the specific answers
   `ENROLLCONVO` expects (a scripted Q&A) rather than the generic
   `WAKEUP_TRIGGER_LINE`, to drive enrollment to done so the schedule proceeds.
   Brittle (depends on exact expected phrases) but would confirm the "complete
   enrollment → reach OPENMOXIE_CHAT" theory.

Recommended first step: **option 1** (skip/short-circuit onboarding via config),
because it addresses the root cause; fall back to **option 2** (force-exit) if the
robot cannot be told it is already onboarded.

### Note on `[FIRMWARE_BUG] BugID: 17` and `Forcing IDLE … Waiting`

Both appear in the run but are **not** blockers here: `BugID: 17` is a benign
firmware event on `POWER_STATE_ACTIVE`; the `Waiting has gone too long` force-idle
is the robot momentarily idling between onboarding turns, not the entry-sequence
stall we already fixed. Do not chase these.

---

**Goal 1 (original framing, achieved):** inject a canned recognized line over the
STT channel so the robot believes it *heard* it and drives a normal turn (STT →
chat → TTS → playback). This works; the remaining problem is purely that the
robot's *onboard* modules occupy the conversation before `OPENMOXIE_CHAT`.

---

## 0. Read this first — one correction that changes the whole plan

The milestone as originally framed was:

> "send one line in russian/kazakh **as text over zmqSTT** which the robot
> expects … it should play the welcoming audio … which should be sent over
> zmqSTT to the OpenAI TTS."

There are **two different audio channels** on this robot, and the plan only
works if we keep them apart:

| Channel | Proto | Direction | Meaning |
|---|---|---|---|
| **Ears** | `zmqSTTRequest` / `zmqSTTResponse` | robot → server → robot | mic audio in; **recognized text** back. `zmqSTTResponse.speech` = *"what the child said."* |
| **Mouth** | `CloudTTSRequest` / `CloudTTSResponse` | robot → server → robot | robot asks for speech; **synthesized audio** back. `CloudTTSResponse` = *"what Moxie says."* |

Consequences:

- **The welcome line is NOT sent over `zmqSTT`.** It is spoken over the
  **CloudTTS** channel. `zmqSTT` cannot make Moxie say a specific line — putting
  text in `zmqSTTResponse.speech` only makes the robot *believe it heard* that
  text, which then drives the brain to *reply* to it.
- **The assumption "ru/kk must be played as audio" is correct** — and it applies
  to the **mouth** (CloudTTS), not the ears. Onboard TTS is English Polly
  (`cloud_tts_engine=polly`, `cloud_tts_voice_id=Joanna` in the latest
  `robot-status`), so any ru/kk line must be synthesized externally (OpenAI TTS)
  and delivered as `CloudTTSResponse` audio. That path already exists and works
  (see the 14:40 log: `/tts/speak` → PLAY_OUTPUT → CloudTTSRequest → OpenAI
  audio → `spoke … audio_bytes=201600`).
- **`zmqSTT` still must be answered** — not to make Moxie speak, but because the
  robot is streaming mic audio and waiting for a reply. When no reply comes we
  get the log you saw:
  `zmqSTT: UUID has gone too long without a response`, and the state machine
  eventually fires `RobotStateModule: Forcing IDLE state, Waiting has gone too
  long without transitioning`. Answering `zmqSTT` keeps the robot awake and
  unlocks the *listening* half of the conversation.

So the corrected goal decomposes into a **mouth** track (welcome + `/tts/speak`)
and an **ears** track (`zmqSTT`), joined by the **brain** (`remote_chat`).

---

## 1. Target architecture — the full loop, no puppet mode

```
        ┌─────────────────────────── ROBOT ───────────────────────────┐
        │  wake → schedule query ─────────────────────────────────────┼──► server: handle_schedule_query
        │                                                             ◄┼──  DEFAULT_SCHEDULE (launch OPENMOXIE_CHAT)
        │  launch module → command=prompt ────────────────────────────┼──► server: _remote_module_response
        │                                                             ◄┼──  RemoteChatResponse(text = welcome opener)
        │  cloud_tts=1 → CloudTTSRequest(markup) ──────────────────────┼──► server: cloud_tts_request handler
        │                                                             ◄┼──  CloudTTSResponse(OpenAI audio)   ◄── MOUTH
        │  ▶ plays welcome audio                                        │
        │                                                              │
        │  child speaks → VAD → zmqSTTRequest (16 kHz PCM, streamed) ──┼──► server: STT handler  [NEW]        ◄── EARS
        │                                                             ◄┼──  zmqSTTResponse(speech = recognized text)
        │  brain → RemoteChatRequest(speech=recognized) ──────────────┼──► server: handle_router_request
        │                                                             ◄┼──  RemoteChatResponse(text = reply)
        │  cloud_tts=1 → CloudTTSRequest ─────────────────────────────┼──► server (OpenAI TTS)
        │                                                             ◄┼──  CloudTTSResponse(audio)          ◄── MOUTH
        │  ▶ plays reply audio                                         │
        └──────────────────────────────────────────────────────────────┘
```

Everything except the **[NEW] STT handler** already exists in this repo.

---

## 2. Current state in this repo

| Piece | Where | State |
|---|---|---|
| Wake → schedule → launch chat module | `remote_chat.py:handle_schedule_query` + `config.DEFAULT_SCHEDULE` | ✅ works (non-puppet) |
| Welcome/opener line | `remote_chat.py:_opener_for` + `config.REMOTE_CHAT_OPENERS` | ✅ works; currently set to an English test line — must be set to the ru/kk welcome |
| CloudTTS synthesis (mouth) | `cloud_tts_request.py`, `synthesis/openai_tts.py`, `mqtt_service.publish_cloud_tts_response` | ✅ works |
| `/tts/speak` on-demand line | `router/tts.py` → `telehealth.play_output` | ✅ works (note the `play_output(markup=…)` signature must stay intact — a broken edit that dropped it makes every speak `NameError`) |
| OpenAI STT stage | `stt/openai_stt.py:transcribe()` | ✅ exists, unused by the robot path |
| PCM→WAV helper | `audio.pcm_to_wav(pcm, sample_rate)` | ✅ exists |
| **Consume `zmqSTTRequest` (ears)** | `mqtt_service.py:228-232` | ❌ **explicitly ignored** ("later milestone") |
| **Subscribe to `zmqSTTRequest`** | `mqtt_service.PROTO_SUBSCRIBE_TYPES` | ❌ only `CloudTTSRequest` is requested |
| **Reply with `zmqSTTResponse`** | — | ❌ not implemented |

---

## 3. Reference: OpenMoxie's STT handler (canonical shape)

The cloned repo at `~/Desktop/hri-lab/openmoxie` has cloud TTS *unimplemented*,
but it **does** implement STT, and its handler is the contract the firmware
expects. Key file: `site/hive/mqtt/zmq_stt_handler.py`.

What it establishes (verified against the proto in
`src/protos/embodied/perception/audio/zmqSTT_pb2.py`):

- The robot streams `zmqSTTRequest` frames, each with:
  - `uuid` — the **STT session id** (one per utterance).
  - `vad` — `START_OF_SPEECH` / `SPEECH` / `END_OF_SPEECH`.
  - `audio_content` — a chunk of **16 kHz mono PCM16** mic audio.
- The server **accumulates** `audio_content` per `(device_id, uuid)` and, on
  `vad == END_OF_SPEECH`, transcribes the whole buffer.
- It replies with a `zmqSTTResponse`:
  - `uuid` = the session id (must echo the request's uuid).
  - `type = ResponseType.FINAL`.
  - `speech` = recognized text.
  - `timestamp`, `start_timestamp`, `end_timestamp` = ms clock values.
  - on failure: `error_code` + `error_message`.
- Reply framing/topic (identical to this repo's `publish_proto`):
  `"<full.proto.Name>:" + SerializeToString()` published to
  `/devices/{device_id}/commands/zmq`. OpenMoxie: `send_zmq_to_bot`
  (`moxie_server.py:308`). This repo: `mqtt_service.publish_proto`
  (`mqtt_service.py:424`).
- To *receive* the packets, OpenMoxie appends `zmqSTTRequest` to its
  ProtoSubscribe list (`moxie_server.py:263`). This repo must do the same.

The robot then takes the recognized `speech` and, on its own, issues a
`RemoteChatRequest(speech=…)` — which this repo already answers in
`remote_chat.handle_router_request`. We do **not** wire STT→chat on the server;
the robot does that.

---

## 4. Milestones

### Goal 1 (this task) — Inject a canned recognized line → chat → TTS → spoken reply.

We answer the robot's STT stream with a **fixed** recognized line instead of
transcribing the real audio. The robot believes it heard that line and runs its
normal turn, which the server completes with the existing chat + TTS stages. Net
effect: **Moxie speaks the OpenAI chat reply to `"Привет, как дела?"`**, with no
microphone transcription and no puppet mode.

**Exact chain (what each component does):**

```
1. robot awake, in OPENMOXIE_CHAT (schedule already launches it), listening
2. robot streams zmqSTTRequest frames (mic audio, one uuid per utterance)
3. server [NEW]: on END_OF_SPEECH, IGNORE the audio and reply
       zmqSTTResponse(uuid=<same>, type=FINAL, speech="Привет, как дела?")
4. robot: "I heard 'Привет, как дела?'" → sends
       RemoteChatRequest(speech="Привет, как дела?")           (robot does this)
5. server: remote_chat.handle_router_request → _remote_module_response
       → openai_chat.respond("Привет, как дела?", "ru", history)  [OpenAI CHAT]
       → RemoteChatResponse(text=<reply>)                       (already implemented)
6. robot (cloud_tts=1): CloudTTSRequest(markup=<reply>)
7. server: synthesis/openai_tts → CloudTTSResponse(audio)        [OpenAI TTS]
8. robot: ▶ plays the reply audio
```

Steps 4–8 already work in this repo. The **only new code is step 3** — answering
`zmqSTT` with the canned line. Everything downstream is the existing conversation
path, so injecting the line is enough to exercise chat→TTS→playback end to end.

**Implementation of step 3:**

**a. Subscribe to the proto.** In `mqtt_service.PROTO_SUBSCRIBE_TYPES` add:
```python
"embodied.perception.audio.zmqSTTRequest",
```
(This is what makes the robot forward mic packets to us.)

**b. New handler module** `src/services/stt_request.py` (mirrors
`cloud_tts_request.py`):
   - Parse the framed payload into `zmqSTTRequest` (`ParseFromString` after
     stripping the `"embodied.perception.audio.zmqSTTRequest:"` prefix — reuse
     the same de-framing the CloudTTSRequest path uses).
   - Track sessions as `dict[(device_id, uuid)]` (for Goal 1 we don't even need
     the audio bytes — just the uuid; keep a set/dict so we know when a session
     ends). Real audio accumulation arrives in M2.
   - On `vad == zmqSTTRequest.VADState.END_OF_SPEECH`:
     - Build `zmqSTTResponse(uuid=req.uuid, type=FINAL,
       speech=settings.INJECTED_STT_LINE, timestamp=now_ms())` — see §5 for the
       full field contract. **Do not transcribe.**
     - Drop the session key.
   - Reply via `mqtt_service.publish_proto(f"/devices/{device_id}/commands/zmq",
     resp)` (already frames + publishes correctly).
   - Add a config knob `INJECTED_STT_LINE: str = "Привет, как дела?"` in
     `config.Settings` so the canned line is easy to change (and can later be set
     `""` to mean "use real STT" once M2 lands).

**c. Register + dispatch.**
   - Add `register_stt_request_handler(handler)` to `mqtt_service` (mirror
     `register_cloud_tts_request_handler`, `mqtt_service.py:124`).
   - In `_on_message`, replace the *ignore* at `mqtt_service.py:228-232` with a
     dispatch to the registered STT handler (schedule it on the asyncio loop the
     same way CloudTTSRequest is via `_schedule`, `mqtt_service.py:398-413`).
   - Wire it in `main.py` next to the other `register_*` calls
     (`main.py:62-74`).

**Preconditions / how to trigger.** The robot must be awake and in
`OPENMOXIE_CHAT` (the schedule already launches it; `PUPPET_MODE_AUTO_ENABLE=False`,
`CLOUD_TTS_AUTO_ENABLE=True`, `OPENAI_API_KEY` set). The robot only streams
`zmqSTTRequest` while it is listening for a turn, and `END_OF_SPEECH` is
VAD-driven — so **make any sound near the robot** (speak anything, tap the mic)
to close a session. Whatever was actually said is discarded; we always return the
canned line.

**Verification of Goal 1:**
   - `zmqSTT: UUID has gone too long without a response` warnings stop.
   - Server log shows a `RemoteChatRequest`/router turn with
     `speech='Привет, как дела?'`, then `Tx remote_chat … text='<reply>'`.
   - Then `inbound CloudTTSRequest` → `openai tts synthesized … (ru)` →
     `spoke … language=ru`.
   - Moxie audibly plays the reply; `Forcing IDLE` no longer fires.

> Note on unsolicited injection: it is cleaner to reply *to a real STT session*
> (matching its `uuid`) than to fabricate a `zmqSTTResponse` out of nowhere — the
> firmware correlates responses to pending request uuids, so an unknown-uuid
> response is likely dropped. Hijacking a live session (return canned text
> instead of the transcript) is the reliable injection point.

### M2 — Real transcription (replace the canned line).

Set `INJECTED_STT_LINE=""` and transcribe for real:
   - Wrap the accumulated PCM in a WAV before handing it to OpenAI:
     `wav = audio.pcm_to_wav(buffer, sample_rate=16000)` — **note the 16 kHz mic
     rate**, which differs from the 24 kHz TTS rate in
     `settings.AUDIO_SAMPLE_RATE_HZ`; do **not** reuse that constant here.
   - `result = stt.openai_stt.transcribe(wav, language=None, filename="stt.wav")`
     — `language=None` lets OpenAI auto-detect ru vs kk. (Optionally pass a hint
     if you constrain the child's language.)
   - `resp.speech = result.text`; if `transcribe` raises, set `resp.error_code`
     / `resp.error_message` (like OpenMoxie) and still reply, so the robot is
     never left waiting.
   - Run the blocking OpenAI call off the event loop
     (`run_in_threadpool`, as `remote_chat` already does) or a thread pool like
     OpenMoxie's `ThreadPoolExecutor`, so concurrent utterances/devices don't
     block each other.
   - Consider a max-buffer guard and a per-session timeout so a missing
     `END_OF_SPEECH` can't leak memory.

**Verification of M2:** speak ru/kk to the robot; the log shows
`openai stt transcribed …`, then Moxie replies in the same language.

### M3 — Proactive wake welcome line (mouth), optional.

Independent of the STT loop: have Moxie greet on wake without waiting for an
injected/real turn. This is the CloudTTS/opener path.

1. Set the wake opener to the ru/kk welcome. In `config.REMOTE_CHAT_OPENERS`,
   `"default"` should be the welcome line, e.g.
   `"Привет! Давай поговорим. О чём ты хочешь поболтать?"` (this is the original
   opener; the current English line was a one-off test — restore ru here).
2. On wake the robot launches `OPENMOXIE_CHAT` from the schedule and sends
   `command=prompt`; `_opener_for` returns the welcome, which is synthesized and
   played. Then Goal 1's injected turn (or M2's real STT) drives the conversation.

**Verification of M3:** wake → `Tx remote_chat … text='Привет!...'` →
`openai tts synthesized … (ru)` → `spoke …` with no input needed.

### M4 — On-demand ru/kk lines via `/tts/speak` (mouth), during autonomous mode.

`/tts/speak` already works (it is the mouth channel via `telehealth.play_output`
→ `PLAY_OUTPUT`). What changes without puppet mode is **timing/coexistence**:

- Pushing `PLAY_OUTPUT` while the robot is in an active chat/listening turn can
  clash with STT and local output state (lip-sync desync, audio cut-outs). This
  is the caveat in the task description, and it is real.
- Safer patterns for arbitrary lines in autonomous mode:
  - **Reactive:** return the desired line as the `remote_chat` response to the
    current turn (no PLAY_OUTPUT injection; rides the normal output slot).
  - **Proactive:** send `PLAY_OUTPUT` only when the robot is idle/not
    mid-utterance; optionally precede with `INTERRUPT` and a short settle
    (`SPEAK_INTERRUPT_PAUSE_S`). Expect occasional glitches; this is a firmware
    constraint, not a server bug.

**Verification of M4:** with the robot awake and not mid-turn,
`POST /tts/speak {"text":"Сәлем! Қалайсың?"}` → `spoke … language=kk`.

---

## 5. `zmqSTTResponse` field contract (what to set)

Minimum for the robot to accept a FINAL result (from the proto + OpenMoxie):

```python
resp = zmqSTTResponse()
resp.uuid = req.uuid                 # MUST echo the session id
resp.type = resp.ResponseType.FINAL  # PARTIAL=0, FINAL=1
resp.speech = recognized_text        # Goal 1: the canned INJECTED_STT_LINE
resp.timestamp = now_ms()
resp.start_timestamp = session_start_ms          # best-effort
resp.end_timestamp = session_start_ms + dur_ms   # best-effort
# on failure instead of speech:
# resp.error_code = 66; resp.error_message = str(e)
```

`language`, `confidence`, `alternatives`, `speaker_id` are optional; leave unset
unless a need appears.

---

## 6. Risks & caveats

1. **Config-push native crash.** Pushing config that changes user/identity
   fields mid-session can crash the robot brain
   (`pthread_mutex_lock … destroyed mutex` in `UserIdModule::SaveUserName`, seen
   at 14:38). Keep config pushes minimal and avoid re-pushing identity fields.
   Bootstrapping is already once-per-session
   (`remote_chat.ensure_device_bootstrapped`); do not add per-turn pushes for
   STT.
2. **Sample-rate mismatch.** Mic audio is **16 kHz**; TTS audio is 24 kHz. Wrap
   STT PCM with `sample_rate=16000`, not `settings.AUDIO_SAMPLE_RATE_HZ`.
3. **Force-idle timing.** Before Goal 1 lands, an unanswered STT stream can
   force-idle the robot. Answering `zmqSTT` (Goal 1) is what removes this.
4. **PLAY_OUTPUT vs active listening (M4).** Injecting audio mid-turn can glitch;
   prefer the reactive path.
5. **Language of the opener (M3).** The wake opener currently holds an English
   test line; for the welcome set it to the ru/kk line.
6. **STT enablement on the robot.** OpenMoxie notes the robot must be in a cloud
   STT mode (e.g. `stt:4`) to stream audio; `stt:0` sends nothing. The latest
   `robot-status` shows the robot actively streaming (the timeout warnings), so
   this is already on — but keep it in mind if no `zmqSTTRequest` arrives after
   subscribing.

---

## 7. File-change checklist

**Goal 1 (this task):**
- [ ] `src/config.py` — add `INJECTED_STT_LINE: str = "Привет, как дела?"`.
- [ ] `src/services/mqtt_service.py` — add `zmqSTTRequest` to
      `PROTO_SUBSCRIBE_TYPES`; add `register_stt_request_handler`; replace the
      ignore at `:228-232` with a dispatch to it.
- [ ] `src/services/stt_request.py` — **new**: track sessions by `(device, uuid)`;
      on `END_OF_SPEECH` reply `zmqSTTResponse(speech=INJECTED_STT_LINE)` (no
      transcription) via `mqtt_service.publish_proto`.
- [ ] `src/main.py` — register the STT handler alongside the others
      (`main.py:62-74`).
- [ ] Tests — mirror `tests/test_remote_chat.py`: feed framed `zmqSTTRequest`
      frames (START/SPEECH/END), assert one framed `zmqSTTResponse` with the
      right `uuid`/`type`/`speech=INJECTED_STT_LINE` is published.

**Later:**
- [ ] M2 — accumulate `audio_content`, `pcm_to_wav(…, 16000)` → `stt.openai_stt`;
      gate on `INJECTED_STT_LINE == ""`.
- [ ] M3 — `REMOTE_CHAT_OPENERS["default"]` → ru/kk welcome.
- [ ] M4 — verify `telehealth.play_output` keeps its `markup` parameter so
      `/tts/speak` is not broken.

---

## 8. TL;DR

- **Goal 1:** answer the robot's `zmqSTT` stream with a **canned** line
  (`"Привет, как дела?"`) instead of transcribing. The robot thinks it heard it
  → forwards it → server runs **OpenAI chat** → gets a reply → runs **OpenAI
  TTS** → the robot **plays the reply**. Only new code is the STT reply handler;
  the chat→TTS→playback tail already exists. OpenMoxie's `zmq_stt_handler.py` is
  the exact template (minus the transcription — we hardcode the line).
- `zmqSTT` is the **ears**, `CloudTTS` is the **mouth**; the injected line is a
  *stimulus*, and what Moxie says is the *chat reply* to it.
- The robot (not the server) turns recognized speech into the `remote_chat`
  turn, so injecting the line is enough to drive the whole loop.
- Later: real STT (M2), a wake welcome (M3), on-demand `/tts/speak` (M4).
