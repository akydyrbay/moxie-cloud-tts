# Speak Puppet Analysis

Why TELEHEALTH / puppet mode (`POST /tts/speak`) could not be the conversation
path. This is the investigation log, rewritten so the **conclusion is first**,
the two bugs are separated, and later on-robot retries are folded in.

Historical puppet findings are retained below. For the current autonomous
pipeline and module handling, see [the development guide](AGENTS.md) and
[the live conversation plan](SPEAK_LIVE_PLAN.md).

Device: `d_d05e3c66-dd29-4fe2-9758-7223e31511dc`. Sources restored from git:
`PUPPET_SPEAK_SILENCE_ANALYSIS.md` (last full version, 2026-08-27) and
`PUPPET_LINE2_SLOT_WORKAROUND.md` (2026-09-04 retries). Puppet HTTP is **kept**
as an operator tool. **Do not modify** `telehealth.py` speak/interrupt sequencing
unless asked. Conversation work is `SPEAK_LIVE_PLAN.md`.

---

## Verdict (read this first)

| Fact | Status |
|---|---|
| First `POST /tts/speak` in a TELEHEALTH session is heard | Proven |
| Second and later `/tts/speak` in the **same** session are silent | **Unsolved** |
| The cloud still answers line 2 (`PLAY_OUTPUT` → `CloudTTSRequest` → OpenAI PCM → `CloudTTSResponse`) | Proven — failure is **after** we publish audio |
| Mid-session identity config push SIGABRTs `Main-bo-brain` | **Fixed** (`tts_service._without_identity_fields`); **not** the line-2 cause |
| Prepending `INTERRUPT` to each `PLAY_OUTPUT` | Ruled out — cancels the **new** line |
| `INTERRUPT` after line 1 drains, then `PLAY_OUTPUT` | Tried 2026-09-04, **failed** (collapses to INTERRUPT-then-play) |
| `CloudTTSSupplement` as end-of-utterance | Tried (bundled 2026-08-25 and isolated 2026-09-04), **failed** |
| TELEHEALTH empty schedule parks the brain — no `OPENMOXIE_CHAT` | By design of puppet |

**Product decision:** drop puppet as the ru/kk listen→chat→speak path. Current
autonomous free chat uses a live RemoteChatResponse and correlated REMOTECHAT
audio directly; ordinary remote replies do not raise CloudTTSRequest. Native
activities use CloudTTSRequest → localized ROBOT audio. Neither path uses
`/tts/speak`. Keep `/tts/puppet`, `/tts/speak`, `/tts/interrupt` as operator tools.

**Working model of line 2:** the TELEHEALTH output path has **one speech slot**.
Line 1 acquires it and plays. Nothing we send is recognized as “utterance done”,
so line 2’s audio is dropped even though the MQTT round-trip looks identical.

---

## Two independent bugs

Do not collapse these. Fixing one does not fix the other.

### Bug A — config-push SIGABRT (fixed 2026-09-04)

At 2026-08-27 14:05:20, `POST /tts/puppet {enabled:true}` published full config
(`moxie_mode=TELEHEALTH` plus identity). The robot aborted immediately:

```
libc: FORTIFY: pthread_mutex_lock called on a destroyed mutex (0xad90c738)
Fatal signal 6 (SIGABRT) in tid 1860 (Main-bo-brain)
 #05 UserIdModule::SaveUserName(...)
 #06 UserIdModule::AddUser(...)
 #07 UserIdModule::SetMentorName(...)
 #08 UserIdModule::OnCloudSettingsChanged(RobotCloudConfig)
 #14 ZMQEventBroadcaster::SubscriberThread()
```

`OnCloudSettingsChanged` runs on **every** config publish. Identity in
`HARDCODED_DEVICE_CONFIG` (`pairing_status:"paired"`, `child_pii.nickname`)
drives `SetMentorName → AddUser → SaveUserName`, which locks a **destroyed
mutex**. Firmware bug; we only control the payload.

**Fix (in tree, verified on-robot 2026-09-04 13:11:47):** mid-session toggles
strip `child_pii` and `pairing_status` (`tts_service._without_identity_fields`).
Identity is bootstrap-only. That enable produced no SIGABRT, only a benign
`[MISSING_PII] Child PII has not been filled in`. **Line 2 was still silent**
in the same visit (13:15:22). Keep the strip; do not re-push identity mid-session.

### Bug B — only the first puppet line is heard (unsolved)

Operator sequence:

1. MQTT connect.
2. `POST /tts/puppet` `{"enabled": true}` → `cloud_tts=1`, `moxie_mode=TELEHEALTH`.
3. Telehealth `READY`.
4. `POST /tts/speak` with any line (e.g. `Сәлем! Бүгін көңіл-күйің қалай?`).
   **Heard.** Mouth moves.
5. Same endpoint again, same session. **Silent.** Mouth does not move.
6. Restart this service: the *next* first line is heard again.

HTTP always returns 200 / `ready: true`. The robot always emits
`CloudTTSRequest`. We always answer. Playback does not.

Example run (2026-08-27 14:06–14:07), both `PLAY_OUTPUT`, both `kk`, puppet on,
both `event_id=''`:

| Line | Time | synth | audio | Heard? |
|---|---|---|---|---|
| 1 | 14:06:14 | 1900 ms | 160800 B | **yes** |
| 2 | 14:07:02 | 2957 ms | 180000 B | **no** |

The heard line was the *faster* synth. Latency is not the discriminator.
Format/length is not either: both raw PCM, both ~3–4 s, identical mark shape.

---

## What the wire proves

Every line, heard and silent, has the same shape:

| Step | Line 1 | Line 2+ |
|---|---|---|
| HTTP `/tts/speak` 200, `waited_ms ≈ 0` | yes | yes |
| `PLAY_OUTPUT` on `/devices/{id}/commands/telehealth` | yes | yes |
| Robot `CloudTTSRequest` on `events/zmq` | yes | yes |
| Request: `event_id=''`, `chunk_num=0`, unique `timestamp` | yes | yes |
| OpenAI `/v1/audio/speech` 200, `pcm` | yes | yes |
| `CloudTTSResponse` on `commands/zmq` (~150–190 KB) | yes | yes |
| Viseme / `ssml` marks present | yes | yes |

So this is **not** a dropped HTTP call, missing `PLAY_OUTPUT`, ignored command,
synthesis failure, MQTT type-name bug, or missing first-line marks (the mouth
**does** move on line 1).

`device-logs` at `info` show almost no speech pipeline (`EBUploader` only).
Absence of a “playing / busy / dropped” line does **not** mean the robot did
nothing — firmware may not upload those levels.

`openmoxie` never sets `cloud_tts` and has no `CloudTTS` handler. Its puppet
page is **onboard** TTS: `PLAY_OUTPUT` only, Interrupt as a separate button.
Embodied’s own cloud is the only historical `CloudTTSResponse` producer. We
never captured a Polly-era frame.

---

## Ruled out — do not retry

Each item was implemented and run on the robot. Several are still in the tree
as **real other bugs**; they are not the line-2 fix.

| Attempt | Result | Still in tree? |
|---|---|---|
| Repair OpenAI streaming WAV `0xFFFFFFFF` (~24.8 h header); fetch `pcm` instead | Line 1 only, even with truthful WAV | **Yes** — real header defect (`audio.py`) |
| Stop fabricating telehealth `INTERRUPTING` | Endpoint no longer waits 4 s; line 2 still silent | **Yes** — latency bug, not silence |
| Pre-warm OpenAI client | Line 1 (slower) is the one that plays | **Yes** — first-request delay |
| Polly-style viseme / `sil` marks | Mouth moves on line 1; line 2 still silent | **Yes** — lip-sync |
| Raw PCM vs truthful WAV | Both play line 1 only | PCM is default |
| `INTERRUPT` immediately before every `PLAY_OUTPUT` | Cancels the **new** line’s cloud path; request still fires | No auto-interrupt; `/tts/interrupt` stays |
| Empty schedule while puppet is on | Stops `OPENMOXIE_CHAT` stealing the slot; does not explain line 2 | **Yes** |
| Echo request `timestamp` + `CloudTTSSupplement` after every frame (2026-08-25 13:34) | Line 1 heard, line 2 silent | Reverted |
| Fake a **response** `event_id` while the request is `''` | Wrong pairing; would break conversation-path UUIDs | Never shipped |
| Automarkup `AUTO_GESTURE_TALK`/`NONE` + `ssml` marks | Gestures; not line 2 | Historical wrapper removed; current marks are handled in `marks.py` |
| Telehealth `START_SESSION` / `session_id` / `output.event_id` | Robot `IN_SESSION`, STT timeouts, request `event_id` still `''` | Reverted |
| Strip identity from mid-session config (Bug A) | Crash gone; line 2 still silent (2026-09-04) | **Yes** |
| Drain → standalone `INTERRUPT` → settle → `PLAY_OUTPUT` | Failed 2026-09-04: operator sends line 2 **after** line 1 finished, so drain is elapsed and this **is** INTERRUPT-then-play | Reverted |
| Isolated `CloudTTSSupplement` after each response | Both lines published it; line 2 still silent (2026-09-04) | Reverted |
| Invented `SPEAK_VIA_REMOTE_CHAT` / `push_speech` | Written, reverted, never robot-tested | Gone |

**`event_id` note.** PLAY_OUTPUT requests always carry `event_id=''`. Line 1
plays with empty-empty, so empty is accepted **once**. Conversation-path
`CloudTTSRequest`s have real UUIDs (command-only warmup too). Filling the
request id from telehealth JSON extras does not work. Echo the request id
verbatim; never invent one only on the response.

---

## Surviving hypotheses (after 2026-09-04)

Ranked. Conversation path already voices consecutive turns without puppet, which
is why product work moved there rather than spending more robot time here.

1. **Pace-only (never tested in isolation).** Wait `audio_duration + margin`
   before the next `PLAY_OUTPUT`, **with no INTERRUPT**. If the slot
   self-releases after drain, this is the whole fix. Do **not** add INTERRUPT
   next to the new line.

2. **A terminal signal that is not `CloudTTSSupplement`.** Closing `sil` viseme
   may be the wrong “output done” marker. A `cmd:` / `ssml` end mark, or
   `chunk_num` final-chunk semantics, is unverified without a captured Embodied
   Polly `CloudTTSResponse`.

3. **Capture a real Embodied `CloudTTSResponse`.** Robot props say
   `cloud_tts_engine=polly`, `cloud_tts_use_mp3=0`, `cloud_tts_voice_id=Joanna`.
   One reference frame would settle marks `time` units, whether a companion
   proto is required, and how a turn closes.

4. **A/B with `cloud_tts=0`.** Onboard Polly `PLAY_OUTPUT`: if line 2 repeats,
   the defect is the cloud-TTS answer; if it also stops after line 1, the
   defect is the TELEHEALTH output FSM. Diagnostic only (ru/kk will
   mispronounce).

5. **Keep-awake before line 2.** Robot drifts to `sleep`/`standby`. Cheap
   confound removal, unlikely sufficient alone.

6. **Collapse N HTTP lines into one multi-chunk utterance** if the slot is
   per-turn. Changes the API; not needed for conversation (remote-chat already
   does consecutive turns).

There is **no** documented prop for output-slot depth (`cloud_tts_*` is
voice/engine/gain/rate only). “Enlarge the slot” is not an available knob.

Do not retry `request_source=REMOTECHAT_TTS_REQUEST` until (1)–(3) have
narrowed things — it can break the line that currently works.

---

## Why this is not the conversation path

Even if line 2 were fixed:

- Puppet **empties the schedule**. The robot never launches `OPENMOXIE_CHAT`.
- TELEHEALTH is command-only. There is no ru/kk listen → chat loop on this
  path.
- `/tts/speak` during autonomous chat can steal the same output slot.

Keep puppet code for operator first-line TTS. Finish speech in
`SPEAK_LIVE_PLAN.md` with `PUPPET_MODE_AUTO_ENABLE=false`.

---

## Code that remains on purpose

| Piece | Why |
|---|---|
| `src/services/audio.py` (truthful WAV / never ask OpenAI for `wav`) | Real `0xFFFFFFFF` header defect |
| `src/services/marks.py` | Lip-sync on the heard line |
| `CLOUD_TTS_AUDIO_RAW_PCM=true` | Matches `AudioBuffer`; both formats fail line 2 equally |
| `/tts/speak` → `PLAY_OUTPUT` | Operator path; empty `event_id` |
| Puppet `moxie_mode=TELEHEALTH` | Keeps the robot awake |
| Empty schedule while puppet is on | Stops chat stealing the slot |
| No synthetic `INTERRUPTING` | Real latency bug |
| Echo request `event_id` on `CloudTTSResponse` | Required pairing (echoes `''` on PLAY_OUTPUT) |
| Strip identity on mid-session config | Bug A; bootstrap still sends full identity |
| Robot log / status / activity-log capture | Only window into firmware playback |

---

## Files

- Telehealth PLAY_OUTPUT / INTERRUPT / wakeup / sleep: `src/services/telehealth.py` (**do not retune speak sequencing**)
- HTTP: `src/router/tts.py`
- Response assembly: `src/services/speak_service.py`
- Inbound `CloudTTSRequest`: `src/services/cloud_tts_request.py`
- Config push + identity strip: `src/services/tts_service.py`, `HARDCODED_DEVICE_CONFIG` in `src/config.py`
- Marks / markup / audio: `src/services/marks.py`, `src/services/cloud_tts_request.py`, `src/services/audio.py`
- MQTT + robot logs: `src/services/mqtt_service.py`
- Conversation plan (not this mode): `SPEAK_LIVE_PLAN.md`
