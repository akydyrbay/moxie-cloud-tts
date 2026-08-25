# Only the first `/tts/speak` is heard

**Status: unsolved.** Puppet (TELEHEALTH) mode with `cloud_tts=1`. After
entering puppet mode, the first `POST /tts/speak` is voiced (and the mouth
moves). Every later `/tts/speak` in the **same** session is silent, even though
this service completes a full, successful round trip for each one. Toggling
puppet off and on (a new TELEHEALTH session) makes the next line play once,
then the silence repeats.

This document is the investigation log. It describes the problem from on-robot
evidence, every fix that was tried and why it was wrong, and what is still
worth trying. Speculative code that did not change the symptom has been
reverted.

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
6. Repeat: still silent. Disable/enable puppet: the *next* first line is heard
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
| `CloudTTSResponse` published on `/devices/{id}/commands/zmq` (~150–170 KB) | yes | yes |
| `marks=32` (Polly-style visemes + closing `sil`) | yes | yes |

Played and silent lines are indistinguishable on the wire except for OpenAI
latency and a few kilobytes of audio length. The line that **plays** is often
the *slowest* (4–5 s to synthesize). The silent lines are faster (2–3 s). So
this is not a timeout, not a dropped HTTP call, not a missing `PLAY_OUTPUT`,
and not a missing `CloudTTSRequest`.

Latest confirming run: **2026-08-25 13:34–13:35**, device
`d_d05e3c66-dd29-4fe2-9758-7223e31511dc`, after a process restart and a fresh
puppet enable.

| | line 1 (heard) | line 2 (silent) |
|---|---|---|
| `PLAY_OUTPUT` | 13:34:56 | 13:35:34 |
| `CloudTTSRequest` timestamp | `1787646896346` | `1787646934340` |
| OpenAI PCM | 156000 B ≈ 3.25 s | 158400 B ≈ 3.30 s |
| `CloudTTSResponse` | published | published |
| `CloudTTSSupplement` (since reverted) | published, same timestamp | published, same timestamp |
| synth time | 4300 ms | 2441 ms |

### What the robot does *not* tell us (yet)

Between speaks, `events/device-logs` at `default_loglevel=info` shows almost
nothing about speech: `EBUploader` work cycles, occasional power/XMOS lines.
It does **not** report “playing”, “busy”, “dropped CloudTTSResponse”, or
“waiting for another chunk”. Telehealth state is `READY` once on entering
TELEHEALTH and then never again — no per-utterance `IN_SESSION` / `READY`.

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
- Correlation by a *non-empty* `event_id` (telehealth always sends `''` and we
  echo `''`; line 1 still plays)
- Wall-clock latency / MarkingUp give-up on the silent lines
- Lip-sync absence (mouth **does** move on the heard line)

The failure is on the robot, after it has asked for audio and after this
service has published a `CloudTTSResponse`. Something about **having already
completed one cloud-TTS utterance in this TELEHEALTH session** makes the next
response inaudible.

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

**Why it was not the cause.** Runs with `marks=32` on every line still only
play line 1 (12:47, 13:14, 13:34). The mouth **does** move on the heard line,
so marks are consumed at least once. They do not free the slot for line 2.

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

### 2.9 `event_id` correlation in general — not the discriminator

All telehealth `CloudTTSRequest`s carry `event_id=''`. We echo `''`. Line 1
plays with that pair, so empty-empty is accepted at least once. Inventing a
random response `event_id` when the request has a **real** UUID is known to
make the robot discard audio (conversation path). Doing that for empty
telehealth ids was not shipped, because it would risk breaking the one line
that works.

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
`activity-log` lines around line 1 vs line 2. That is the first new evidence
this investigation has not had.

---

## 4. Remaining possible solutions

These are not implemented. They are ordered by how well they fit the evidence
(robot asks, we answer, only the first utterance in a TELEHEALTH session is
voiced).

### 4.1 Read the new robot-status / activity-log dump (do this first)

The next puppet session should show whether line 2 leaves the robot in
SPEAKING / MarkingUp / a busy output slot, or whether it returns to IDLE and
still does not play. That chooses among 4.2–4.7 instead of guessing.

### 4.2 TELEHEALTH + `cloud_tts=1` may be a one-shot path

OpenMoxie puppet never enables `cloud_tts`; onboard TTS plays every
`PLAY_OUTPUT`. Embodied cloud TTS may have been built for **conversation**
(unique `event_id`s, remote-chat) and only accidentally play the first
telehealth line. If robot-status after line 1 never returns to an idle output
slot, the firmware may not support a second cloud-TTS clip in TELEHEALTH.

**Possible moves:** speak puppet lines with onboard TTS (`cloud_tts=0`) to
confirm PLAY_OUTPUT itself can repeat; or drive cloud TTS from remote-chat
(unique `event_id`) even while the face is in TELEHEALTH; or leave TELEHEALTH
and use conversation mode for multi-line cloud TTS.

### 4.3 Unique `event_id` on the *request*, not the response

Empty `event_id` is accepted once. If the robot keys “already played” by
`event_id` and keeps `''` in that set, every later empty-id response is a
duplicate. We cannot change the request unless the robot copies an id out of
`PLAY_OUTPUT`. Things to try only with a fresh puppet session, and only if
line 1 still plays:

- Extra JSON fields on `PLAY_OUTPUT` (`event_id`, `session_id`) in case the
  parser is loose.
- Speak via `remote_chat` output (which already carries `event_id`) while
  `moxie_mode=TELEHEALTH`.

Do **not** put a random id on the response while the request stays `''`
without a line-1 retest — that pairing discards audio when the request has a
real UUID.

### 4.4 Capture a real Embodied `CloudTTSResponse`

The viseme `type`/`value` vocabulary, whether `buffer` is PCM or WAV, sample
rate, `request_source`, presence/absence of `CloudTTSSupplement`, and whether
marks `time` is ms vs samples are all inferred. IMPLEMENTATION_PLAN already
flagged “learn viseme vocabulary from a capture first.” A single captured
frame from Embodied cloud would end several remaining guesses.

### 4.5 Automarkup

OpenMoxie's `send_telehealth_speech()` runs `automarkup_process()` so `markup`
carries mood/behaviour tags. We send the same string as `text` and `markup`.
Unlikely to explain “only the first clip”, but it is a real protocol
difference on every line.

### 4.6 Sample rate / PCM interpretation after the first clip

OpenAI is 24 kHz mono 16-bit LE. Robot STT is 16 kHz. Line 1 sounding correct
argues 24 kHz is accepted. A second clip could still be fed to a mixer left
in 16 kHz or “WAV” mode after the first decode. Resampling to 16 kHz is a
cheap A/B if 4.1 shows the second buffer is accepted but played wrong (chipmunk
/ silence / truncated).

### 4.7 Free the output slot after line 1 *finishes*

If the mixer is busy for the rest of the session, an `INTERRUPT` **after**
line 1's duration (not before line 2's PLAY_OUTPUT) — or a wait of
`audio_duration + margin` before the next speak — might release it. Auto-
INTERRUPT *before* PLAY_OUTPUT was already tried and can cancel the cloud
path. This would be a different sequence: play → wait → interrupt → play.

### 4.8 `request_source=REMOTECHAT_TTS_REQUEST`

Proto default is `ROBOT_TTS_REQUEST` (0, omitted on the wire). Telehealth
speech might be classified as remote-chat on the robot. Easy A/B via
`CLOUD_TTS_REQUEST_SOURCE=remotechat`. Risk: breaks the line that currently
works.

### 4.9 What not to try again

- Streaming OpenAI WAV / `0xFFFFFFFF` headers
- Fabricating telehealth states
- Marks as the fix for line 2 (keep them for the mouth)
- PCM vs truthful WAV as the fix for line 2
- INTERRUPT immediately before every PLAY_OUTPUT
- `CloudTTSSupplement` + echoing request timestamp (tested 13:34)
- MQTT QoS 1 on the audio frame (same run)

---

## 5. Code that remains, on purpose

| Piece | Why it stays |
|---|---|
| `src/services/audio.py` (truthful WAV / repair) | Real OpenAI streaming-header defect |
| `src/services/marks.py`, `CLOUD_TTS_EMIT_MARKS=true` | Lip-sync on the heard line |
| `CLOUD_TTS_AUDIO_RAW_PCM=true` | Matches `AudioBuffer` shape; both formats fail line 2 equally |
| PLAY_OUTPUT only on `/tts/speak` | Matches OpenMoxie puppet |
| Empty schedule while puppet is on | Stops chat from launching mid-session |
| No synthetic `INTERRUPTING` | Real latency bug |
| OpenAI client pre-warm | Real first-request delay |
| Robot log / status / activity-log capture | Only window into firmware playback |

---

## 6. Files

- Response assembly: `src/services/speak_service.py`
- Inbound request: `src/services/cloud_tts_request.py`
- Telehealth: `src/services/telehealth.py`
- Marks: `src/services/marks.py`
- Audio containers: `src/services/audio.py`
- Synthesis: `src/services/synthesis/openai_tts.py`
- MQTT + robot logs/status: `src/services/mqtt_service.py`
- Activity-log: `src/services/remote_chat.py`
- Log level pushed to the robot: `ROBOT_LOG_LEVEL` in `src/config.py`
