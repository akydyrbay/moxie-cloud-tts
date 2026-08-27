# Only the first `/tts/speak` is heard

# Status (2026-08-27, after real-device run 14:03–14:07)

The empty-`event_id` theory is **neither confirmed nor refuted** by the latest
run, and the conversation-transport code written to test it was **reverted**
(see below). What the run *did* deliver is one hard new fact and two dead ends.

**The tree is back to the committed `PLAY_OUTPUT` path.** The flag-gated
`SPEAK_VIA_REMOTE_CHAT` experiment (`push_speech`, the puppet/schedule rewire,
the `device_state` puppet-flag split, the extra tests) was removed — it was
never exercised on the robot, and this run's evidence points at a different,
concrete defect first.

### Headline finding — our config push crashes the robot brain

At 14:05:20, `POST /tts/puppet {enabled:true}` pushed the config
(`moxie_mode=TELEHEALTH`). The robot **immediately** aborted:

```
libc: FORTIFY: pthread_mutex_lock called on a destroyed mutex (0xad90c738)
Fatal signal 6 (SIGABRT) in tid 1860 (Main-bo-brain)
 #05 UserIdModule::SaveUserName(...)
 #06 UserIdModule::AddUser(...)
 #07 UserIdModule::SetMentorName(...)
 #08 UserIdModule::OnCloudSettingsChanged(RobotCloudConfig)
 #14 ZMQEventBroadcaster::SubscriberThread()
```

`OnCloudSettingsChanged` fires on **every** config we publish. It routes into
`UserIdModule::SetMentorName → AddUser → SaveUserName`, which locks a
**destroyed mutex** and SIGABRTs `Main-bo-brain`. The trigger is the
user/mentor identity in our pushed config — `HARDCODED_DEVICE_CONFIG` carries
`pairing_status:"paired"` and `child_pii:{nickname:"Pat"}` (`src/config.py`).
The brain crashed, restarted, and only *then* did line 1 play. So every puppet
enable is currently detonating the native brain and speaking out of a
just-recovered process. This is a firmware bug we cannot patch, but we control
the input that triggers it: **stop pushing user-identity fields mid-session.**

### Line-2 silence reproduced, and two theories killed

`POST /tts/speak` was sent twice (both `PLAY_OUTPUT`, both `kk`, puppet on):

| Line | Time | synth | audio | `event_id` | marks | Heard? |
|---|---|---|---|---|---|---|
| 1 | 14:06:14 | 1900 ms | 160800 B | `''` | 32 | **yes** |
| 2 | 14:07:02 | 2957 ms | 180000 B | `''` | 32 | **no** |

- **Timing theory is dead.** The heard line was the *faster* synth (1900 ms) and
  the silent line the *slower* (2957 ms) — the exact opposite of the earlier
  "the played line is the slowest" note. Latency is not the discriminator.
- **Format/length is dead.** Both raw PCM, both ~3–4 s, both well under any
  timeout, identical marks. The two lines are byte-shape-identical on the wire;
  the only thing that differs is the robot's internal state *after one
  successful playback*.

### What the run confirms about `event_id`

Both lines carried `event_id=''`, so this run cannot separate "empty-id dedup"
from "output slot latches busy after one utterance" — both still fit. It did
confirm the robot emits **non-empty** ids on the conversation path (command-only
`CloudTTSRequest`s `1ec7e89f…`, `ba9f3c1a…` before puppet), but those were all
markup/break commands with no speakable text, and the robot kept drifting to
`sleep`/`standby` — so conversation-driven *speech* remains unobserved.

---

This document is the investigation log.

---

## 0. Latest robot checks (2026-08-26)

Device `d_d05e3c66-dd29-4fe2-9758-7223e31511dc`. Text always
`Сәлем! Бүгін көңіл-күйің қалай?` (kk). Puppet on, `cloud_tts=1`.

### 0.1 Wrap + ssml TALK/NONE (19:30) — not the cause, still in the tree

OpenMoxie puppet never sends bare text. Every line is wrapped with
`AUTO_GESTURE_TALK` … `AUTO_GESTURE_NONE`, and those tags are echoed as Polly
`ssml` marks on the response (`src/services/moxie_markup.py`).

Restart was correct: `PLAY_OUTPUT` ~1113 bytes, `marks=36`, inbound markup
starts with `cmd:playback-mood`. Line 1 heard, line 2 silent.
`CloudTTSRequest.event_id` stayed `''` on every PLAY_OUTPUT line.

The wrap stays for gestures / OpenMoxie parity. It does not unblock line 2.

### 0.2 `START_SESSION` + `session_id` + `output.event_id` (19:49–19:50) — useless, reverted

Hypothesis: extra fields on the telehealth JSON, or opening a new telehealth
session before line 2, would copy a UUID into `CloudTTSRequest.event_id`.

What went on the wire:

- First `/tts/speak` (19:49:53): PLAY_OUTPUT only. CloudTTSRequest
  `event_id=''`. OpenAI 141600 B. Heard (first line of that visit).
- Second `/tts/speak` (19:50:35): `START_SESSION` then PLAY_OUTPUT with a
  unique `session_id` / `output.event_id`. Robot started **its own** session
  (`4d7bae33-…`), `mode: active`, telehealth `READY -> IN_SESSION`.
  CloudTTSRequest **still** `event_id=''`. OpenAI 189600 B. Silent.
- Side effect: `zmqSTT: UUID has gone too long without a response` — STT
  started because we opened a session.

TelehealthMessage has `session_id`. PLAY_OUTPUT `Output` is still only
`text`/`markup` on the documented schema. The firmware did not copy our extras
into `CloudTTSRequest.event_id`. Those extras are gone; `/tts/speak` is
PLAY_OUTPUT-only again.

### 0.3 The robot *can* send a non-empty `event_id`

Same visit, 19:49:22, entering TELEHEALTH (not PLAY_OUTPUT): a command-only
`CloudTTSRequest` had `event_id='994ca4f8-74f5-45de-8634-54f29581edc6'`.
Boot warmup uses `CereprocWarmup-ignore-me`. Conversation `remote_chat`
responses carry `event_id` and the robot echoes it on later requests.

PLAY_OUTPUT is the path that always produces `''`.

---

## 1. The problem

### What the operator does

1. Robot connects to this service's MQTT broker.
2. `POST /tts/puppet` with `{"enabled": true}` — config push sets
   `cloud_tts=1` and `moxie_mode=TELEHEALTH`.
3. Robot reports telehealth `READY`.
4. `POST /tts/speak` with `{"text": "Сәлем! Бүгін көңіл-күйің қалай?"}`
   (or any other line). **Heard.** Mouth moves.
5. The same `POST /tts/speak` again (same text, same device, same session).
   **Silent.** Mouth does not speak that line.
6. Repeat: still silent. Restarting the service: the *next* first line is heard
   again.

### What this service does on every line (heard and silent)

The HTTP handler does not synthesize. It publishes a telehealth `PLAY_OUTPUT`.
The robot then asks the cloud for audio, and we answer. For **every** line,
including the silent ones, the log shows the same shape:

| Step | Happens on line 1? | Happens on line 2+? |
|---|---|---|
| HTTP `POST /tts/speak` returns 200, `ready: true`, `waited_ms ≈ 0` | yes | yes |
| `PLAY_OUTPUT` published on `/devices/{id}/commands/telehealth` | yes | yes |
| Robot immediately emits `CloudTTSRequest` on `events/zmq` | yes | yes |
| Request fields: `event_id=''`, `chunk_num=0`, unique `timestamp`, same markup | yes | yes |
| OpenAI `/v1/audio/speech` `200`, `response_format=pcm` | yes | yes |
| `CloudTTSResponse` published on `/devices/{id}/commands/zmq` (~150–190 KB) | yes | yes |
| `marks=36` (visemes + `ssml` TALK/NONE) | yes | yes |

Played and silent lines are indistinguishable on the wire except for OpenAI
latency and a few kilobytes of audio length. The line that **plays** is often
the *slowest* (4–5 s to synthesize). The silent lines are faster (2–3 s). So
this is not a timeout, not a dropped HTTP call, not a missing `PLAY_OUTPUT`,
and not a missing `CloudTTSRequest`.

Confirming runs:

- **2026-08-25 13:34–13:35** (before wrap): line 1 heard, line 2 silent,
  `event_id=''` both times.
- **2026-08-26 19:30** (wrap + ssml): same, `marks=36`.
- **2026-08-26 19:49–19:50** (START_SESSION extras): same empty `event_id`.

### What the robot does *not* tell us (yet)

Between speaks, `events/device-logs` at `default_loglevel=info` shows almost
nothing about speech: `EBUploader` work cycles, occasional power/XMOS lines.
It does **not** report “playing”, “busy”, “dropped CloudTTSResponse”, or
“waiting for another chunk”. Telehealth state is `READY` once on entering
TELEHEALTH and then never again — unless we send `START_SESSION`, which
flips `IN_SESSION` and starts STT without filling `event_id`.

At connect, before puppet, the robot often logs:

- `RobotStateModule: Forcing IDLE state, MarkingUp has gone too long without transitioning`
- sleep → touch wakeup → `POWER_STATE_STANDBY`

That MarkingUp timeout is a **conversation-mode** stall (cloud_tts on, no
speech yet), not the puppet-line-2 bug. Puppet then wakes it (`POWER_STATE_ACTIVE`).

### What this is not (ruled out by the logs above)

- HTTP / telehealth command path
- Robot ignoring later `PLAY_OUTPUT`s (it still emits `CloudTTSRequest`)
- Synthesis failure
- MQTT topic / protobuf type name (`embodied.unity.CloudTTSResponse`)
- Wall-clock latency / MarkingUp give-up on the silent lines
- Lip-sync absence (mouth **does** move on the heard line)
- Missing TALK/NONE wrap / `ssml` marks (19:30)
- Telehealth `session_id` / `output.event_id` / `START_SESSION` (19:50)

The failure is on the robot, after it has asked for audio and after this
service has published a `CloudTTSResponse` whose `event_id` is still `''`.
**Having already completed one empty-id cloud-TTS utterance in this TELEHEALTH
visit** makes the next empty-id response inaudible.

There is no reference implementation. `openmoxie` never sets `cloud_tts` and
has no `CloudTTS` handling. Its puppet page uses **onboard** TTS: `PLAY_OUTPUT`
only, Interrupt as a separate button. Embodied's own cloud is the only thing
that historically answered `CloudTTSRequest`.

---

## 2. Approaches that were applied, and why they were wrong

Each item below was a real change, tested on the robot, and **did not make
line 2 play**. Several were still genuine bugs and stay in the tree; they are
marked **kept**. The rest were hypotheses about *this* symptom and have been
**reverted**.

### 2.1 OpenAI streaming WAV header (`0xFFFFFFFF`) — kept, not the cause

**Hypothesis.** OpenAI `response_format=wav` streams the body, so both RIFF
size fields are `0xFFFFFFFF` (~24.8 hours of 24 kHz audio). The robot plays
the first few seconds, then believes it is still speaking for the rest of the
session.

**What we did.** Always request `pcm` from OpenAI. If a WAV container is
wanted, build it locally with truthful sizes (`src/services/audio.py`). Repair
any RIFF that still arrives with the placeholder.

**Why it looked right.** The predicted symptom is exactly “line 1 plays, later
lines are silent while CloudTTSRequest still fires.”

**Why it was not the cause.** After the repair, a 3.3 s WAV with
`RIFF=156036` / `data=156000` matching the file still only played line 1
(run 12:19). Raw PCM (no RIFF at all) still only played line 1 (runs 13:14,
13:34).

**Kept because** publishing a 24.8-hour header is still a defect. It is just
not *this* defect.

### 2.2 Fabricated telehealth `INTERRUPTING` state — kept, not the cause

**Hypothesis.** Later speaks were delayed or dropped because we thought the
robot was not `READY`.

**What we did.** `interrupt()` used to write `"INTERRUPTING"` into a local
cache. The robot only reports READY/EXITING at **session** boundaries, so
nothing ever cleared it and every later `/tts/speak` waited the full 4 s
timeout.

**Why it was not the cause.** After the fix, the endpoint returns
`ready: true`, `waited_ms ≈ 0`, and `PLAY_OUTPUT` still goes out immediately.
Line 2 is still silent. Latency bug, not a silence bug.

### 2.3 OpenAI client constructed on the first request — kept, not the cause

**Hypothesis.** ~2 s of client import/TLS on line 1 vs faster line 2 explained
the difference.

**What we did.** Pre-warm the client at process start.

**Why it was not the cause.** Line 1 (slower) is the one that **plays**. Line 2
is faster and silent.

### 2.4 Speech marks / visemes — kept for lip-sync, not the cause

**Hypothesis.** An empty `marks` list left the utterance un-terminated, so the
output slot never went idle. Also the mouth was motionless on line 1.

**What we did.** Derive Polly-style `sentence` / `word` / `viseme` marks with a
closing `sil` at the audio duration (`src/services/marks.py`).

**Why it was not the cause.** Runs with `marks=32` then `marks=36` still only
play line 1. The mouth **does** move on the heard line, so marks are consumed
at least once. They do not free the slot for line 2.

**Kept because** lip movement on the first line is required, and the user
confirmed it works.

### 2.5 Raw PCM instead of WAV in `AudioBuffer.buffer` — kept as default, not the cause

**Hypothesis.** `AudioBuffer` has `channels` + `sample_rate` beside `buffer`,
so the payload is PCM. Unity sniffs RIFF once; later WAVs in the same session
are ignored.

**What we did.** Default `CLOUD_TTS_AUDIO_RAW_PCM=true`.

**Why it was not the cause.** Truthful WAV and raw PCM both play line 1 only.

### 2.6 Auto-`INTERRUPT` before every speak — reverted earlier, not the cause

**Hypothesis.** Leftover output state blocked line 2. OpenMoxie only sends
`PLAY_OUTPUT`; Interrupt is a separate button.

**What we did.** Stopped prepending `INTERRUPT` to every `/tts/speak`.

**Why it was not the cause.** PLAY_OUTPUT-only runs (13:14, 13:34) still silent
after line 1. Prepending INTERRUPT can also cancel the cloud output path while
leaving markup intact (request still fires, nothing is voiced) — so it was the
wrong protocol *and* it did not help.

`POST /tts/interrupt` remains for an explicit stop.

### 2.7 Empty schedule in puppet mode — kept, not the cause

**Hypothesis.** Answering schedule with `OPENMOXIE_CHAT/default` launched chat,
which stole MarkingUp / the output slot.

**What we did.** `handle_schedule_query` returns `{"provided_schedule": []}`
when puppet is on.

**Why it was not the cause.** In the 13:13 and 13:34 runs, schedule was
answered **before** puppet enable (`puppet=False`), so chat schedule still went
out at bootstrap. After puppet, line 1 still played and line 2 did not. Empty
schedule is still the right answer *during* puppet so a later schedule query
does not start chat; it does not explain line 2.

### 2.8 Echo request `timestamp` + send `CloudTTSSupplement` — reverted

**Hypothesis.** Telehealth requests all have `event_id=''`. The request
timestamp is the only unique id. The response used `now()` instead, so the
robot could not match line 2. Separately, `CloudTTSSupplement` exists in the
proto as a companion (`text`, `markup`, timings) and was never sent, so the
first cloud-TTS turn never closed.

**What we did.** Echo `CloudTTSRequest.timestamp` on the response. Publish
`CloudTTSSupplement` on the same zmq topic after every audio frame. MQTT QoS 1
for those frames.

**Why it was not the cause.** Run 13:34–13:35 published, for both lines:

```
publishing embodied.unity.CloudTTSResponse ... timestamp=<request ts>
publishing embodied.unity.CloudTTSSupplement ... timestamp=<request ts>
```

Line 1 heard, line 2 silent. Reverted: supplement, timestamp echo, QoS 1.

### 2.9 Inventing a *response* `event_id` while the request stays `''` — not shipped

All telehealth `CloudTTSRequest`s carry `event_id=''`. We echo `''`. Line 1
plays with that pair, so empty-empty is accepted **once**. Putting a random
id on the **response** while the request is still `''` is the wrong pairing:
when the request has a real UUID (conversation path), a mismatched response
id makes the robot discard audio.

The fix is a non-empty id on the **request**, then echo it. Do not fake it
only on the response.

### 2.10 Automarkup wrap + `ssml` TALK/NONE — kept, not the cause

**Hypothesis.** Line 1 started talk; nothing sent `AUTO_GESTURE_NONE`; Unity
kept the output slot busy.

**What we did.** `wrap_speech` on PLAY_OUTPUT and Polly `ssml` marks on the
response (19:30 run: `marks=36`, markup starts with `cmd:playback-mood`).

**Why it was not the cause.** Line 2 still silent, `event_id` still `''`.

**Kept because** OpenMoxie never sends bare text; gestures still need the wrap.

### 2.11 Telehealth `START_SESSION` / `session_id` / `output.event_id` — reverted

**Hypothesis.** Those JSON fields would become `CloudTTSRequest.event_id`.

**What we did.** Unique UUID per speak; `START_SESSION` before line 2+.

**Why it was not the cause.** 19:50: robot `IN_SESSION`, zmqSTT timeouts,
`CloudTTSRequest.event_id` still `''`. PLAY_OUTPUT does not copy them.

---

## 3. What the logs do and do not show

### Previously missing

- `events/device-logs` was truncated at 64 bytes (inside the JSON envelope).
  Now re-emitted in full as `robot-log [I] Tag: …`.
- `default_loglevel` was `warning`, which is what OpenMoxie ships. The robot
  then never sends info-level speech logs. We push `info` (the most verbose
  value OpenMoxie documents: `{info, warning, error, fatal}`).
- `events/client-service-robot-status` was **dropped entirely** as heartbeat
  noise. If the firmware reports output-slot / MarkingUp / speaking there, we
  never saw it.
- `events/client-service-activity-log` entries that were not `telehealth` or
  `query` were **dropped**. Any other subtopic (speech, output, cloud-tts)
  never reached the log.

### What “info” still does not give us

During speaks, `device-logs` at info is almost only `EBUploader`. The speech
pipeline likely logs at debug/verbose, and those levels are **not** in
OpenMoxie's documented `default_loglevel` set — the firmware may ignore
`debug`. So absence of a speech line in our log does **not** mean the robot
did nothing; it may mean it did not *upload* that line.

### What we changed so the next run is actually diagnostic

- Re-emit every `device-logs` record at INFO (original letter still in the
  line: `[D]`, `[W]`), including JSON arrays and NDJSON batches.
- Log `client-service-robot-status` **when the payload changes** (identical
  heartbeats skipped).
- Log every activity-log envelope (`subtopic=…`), not only telehealth/query.
- Unknown event topics: 512-byte preview instead of 64.
- MQTT publish `rc` is logged on failure (QoS remains 0, matching OpenMoxie).

After restart, a useful repro log should contain `robot-status` and
`activity-log` lines around line 1 vs line 2.

---

## 4. Remaining possible solutions

Re-ordered after the 14:03–14:07 run. `event_id` is no longer assumed to be
*the* cause; it is one of two surviving hypotheses (§4.3). The config-push
crash (§4.1) is the one thing the run made concrete, so it goes first.

### 4.1 Stop the config push from crashing the brain — do this first

Confirmed defect (backtrace above): `OnCloudSettingsChanged(RobotCloudConfig)`
→ `UserIdModule::SetMentorName → AddUser → SaveUserName` SIGABRTs on a destroyed
mutex whenever we publish config. Our payload carries user/mentor identity
(`pairing_status`, `child_pii.nickname`) that drives `SetMentorName`.

Actions to try, cheapest first:
- **Never re-push identity fields.** Send `cloud_tts` / `moxie_mode` in the
  *smallest* possible config, without `pairing_status` / `child_pii`, so
  `OnCloudSettingsChanged` has no mentor/user delta to act on.
- **Push identity once, at bootstrap, then never again.** Enable puppet and
  cloud_tts up front; do not flip `moxie_mode` mid-session (each flip re-pushes
  the whole config and re-enters the crash).
- Verify the next enable produces **no** `pthread_mutex_lock ... destroyed
  mutex` / `Main-bo-brain` SIGABRT.

This may not by itself un-silence line 2, but the robot is currently speaking
out of a just-crashed-and-restarted brain; removing that instability is a
prerequisite for trusting any line-2 experiment.

### 4.2 Free / recycle the output slot after line 1

The strongest surviving line-2 hypothesis. Both lines were byte-shape-identical
(`event_id=''`, `chunk_num=0`, 32 marks, raw PCM); the only variable is robot
state after one successful utterance. Try, as an A/B on `PLAY_OUTPUT`:
- `INTERRUPT` **after** line 1's audio duration (not before line 2).
- Or wait `audio_duration + margin` before the next `/tts/speak`.
- Or a keep-awake / wakeup nudge before line 2 (the robot drifts to
  `sleep`/`standby` between lines).

Auto-INTERRUPT *before* `PLAY_OUTPUT` is already ruled out (§2.6) — it cancels
the cloud path. This is the after-the-fact variant, never tested.

### 4.3 Capture a real Embodied `CloudTTSResponse` — highest information

The robot's own props expose its native voice: `cloud_tts_engine=polly`,
`cloud_tts_use_mp3=0`, `cloud_tts_voice_id=Joanna`, `cloud_tts_gain_db=3`,
`cloud_tts_speech_rate=100`. So its reference cloud TTS is **Polly**, and our
viseme vocabulary, whether a terminal marker / `CloudTTSSupplement` is required
to close an utterance, and whether marks `time` is ms vs samples are all still
inferred. A single captured Polly-era frame would settle whether a **missing
end-of-utterance signal** is what latches the slot busy after line 1 (§4.2).

### 4.4 A/B with `cloud_tts=0` to isolate the FSM from the cloud path

With cloud_tts off, `PLAY_OUTPUT` uses onboard Polly. If onboard PLAY_OUTPUT
**repeats** line 2, the defect is specific to the cloud-TTS answer (missing
terminal/format), pointing at §4.3. If onboard **also** stops after line 1, the
defect is in the telehealth/output FSM itself, pointing at §4.2. Either result
halves the search space. (kk/ru will mispronounce on Polly — this is a
diagnostic, not a deliverable.)

### 4.5 Conversation-driven `event_id` — deprioritised, not deleted

Still the only way to a non-empty *request* id (`PLAY_OUTPUT` structurally
cannot: `session_id` / `output.event_id` / `START_SESSION` all failed, §2.11).
The flag-gated `push_speech` implementation was written and then **reverted**:
this run shows why it is not yet worth shipping — the robot sleeps without
TELEHEALTH, keeping it in `OPENMOXIE_CHAT` and voicing *unsolicited* turns is
unproven, and the config pushes that would arrange all that are exactly what
crashes the brain (§4.1). Revisit only after §4.1 makes pushes safe and §4.3
confirms the response contract.

### 4.6 `request_source=REMOTECHAT_TTS_REQUEST`

Proto default is `ROBOT_TTS_REQUEST` (0, omitted on the wire). Easy A/B via
`CLOUD_TTS_REQUEST_SOURCE=remotechat`. Risk: breaks the line that currently
works. Only worth trying once §4.2/§4.3 have narrowed things.

### 4.7 What not to try again

- Pushing the **full** config (esp. `pairing_status` / `child_pii` / any
  user-identity field) mid-session — it SIGABRTs `Main-bo-brain` (14:05:20).
- Reasoning from **synthesis latency** — the heard line was the *faster* one.
- Streaming OpenAI WAV / `0xFFFFFFFF` headers.
- Fabricating telehealth states.
- Marks / TALK-NONE wrap as the fix for line 2 (keep them for the mouth).
- PCM vs truthful WAV as the fix for line 2.
- INTERRUPT immediately before every PLAY_OUTPUT.
- `CloudTTSSupplement` + echoing request timestamp (tested 13:34).
- MQTT QoS 1 on the audio frame (same run).
- Telehealth `START_SESSION`, `session_id`, or `output.event_id` as a way to
  fill `CloudTTSRequest.event_id` (tested 19:50).
- A random `CloudTTSResponse.event_id` while the request is still `''`.
- Unsolicited `remote_chat` speech (`push_speech`) before §4.1/§4.3 — written,
  reverted, never robot-tested.

---

## 5. Code that remains, on purpose

| Piece | Why it stays |
|---|---|
| `src/services/audio.py` (truthful WAV / repair) | Real OpenAI streaming-header defect |
| `src/services/marks.py`, `CLOUD_TTS_EMIT_MARKS=true` | Lip-sync on the heard line |
| `src/services/moxie_markup.py` wrap + ssml | Gestures / OpenMoxie parity; not the line-2 fix |
| `CLOUD_TTS_AUDIO_RAW_PCM=true` | Matches `AudioBuffer` shape; both formats fail line 2 equally |
| `/tts/speak` → telehealth `PLAY_OUTPUT` | Committed path; empty `event_id`; line 1 heard, line 2 silent |
| Puppet sets `moxie_mode=TELEHEALTH` | Committed; keeps the robot awake — but the config push crashes the brain (§4.1) |
| Empty schedule while puppet is on | Committed; stops OPENMOXIE_CHAT stealing the output slot |
| No synthetic `INTERRUPTING` | Real latency bug |
| OpenAI client pre-warm | Real first-request delay |
| Echo request `event_id` on `CloudTTSResponse` | Required pairing; echoes `''` verbatim on the PLAY_OUTPUT path |
| Robot log / status / activity-log capture | Only window into firmware playback (surfaced the crash) |

**Reverted after the 14:03–14:07 run:** the flag-gated `SPEAK_VIA_REMOTE_CHAT`
transport — `push_speech`, the `device_state` puppet-flag split, the
puppet/schedule rewire, and its tests. Never robot-tested; see §4.5.

---

## 6. Files

- Response assembly: `src/services/speak_service.py`
- Inbound request: `src/services/cloud_tts_request.py`
- Telehealth (READY / interrupt / wakeup): `src/services/telehealth.py`
- Remote-chat brain (modules / schedule / conversation): `src/services/remote_chat.py`
- Pushed device config (crash trigger — `child_pii`, `pairing_status`): `HARDCODED_DEVICE_CONFIG` in `src/config.py`
- Marks: `src/services/marks.py`
- Markup / ssml commands: `src/services/moxie_markup.py`
- Audio containers: `src/services/audio.py`
- Synthesis: `src/services/synthesis/openai_tts.py`
- MQTT + robot logs/status: `src/services/mqtt_service.py`
- Log level pushed to the robot: `ROBOT_LOG_LEVEL` in `src/config.py`
