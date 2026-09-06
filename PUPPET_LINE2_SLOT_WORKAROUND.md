# Puppet Mode: "Only the First Line Is Spoken" — Problem & Workaround

Status: **UNSOLVED.** Written 2026-09-04. Scope: TELEHEALTH / puppet mode
(`POST /tts/speak` → `PLAY_OUTPUT`), where the **first** cloud-TTS line is heard
and **every later line in the same session is silent**. Grounded in the prior
investigation (`PUPPET_SPEAK_SILENCE_ANALYSIS.md`), which ruled out many causes
and left the "output slot latches busy after one utterance" model as the
strongest surviving hypothesis. Two workarounds were then implemented and
**tested on the real robot; both failed** (§0). The bug is not yet fixed.

---

## 0. Ruled out on the real robot (read this first)

For future models: do **not** re-try these. Each was implemented, run on the
device, and confirmed not to fix line-2 silence.

- **INTERRUPT to "clear the slot" (workaround A, 2026-09-04).** Drain-until-
  line-1-finishes → standalone `INTERRUPT` → settle → `PLAY_OUTPUT`. On the robot
  line 2 was still wrong. **Why it cannot work:** when line 2 is sent after line
  1 has finished (the normal case) the drain is already elapsed, so the sequence
  collapses to `INTERRUPT` immediately before `PLAY_OUTPUT` — the exact pattern
  ruled out in `PUPPET_SPEAK_SILENCE_ANALYSIS.md` §2.6, where `INTERRUPT` cancels
  the *new* line's cloud output. It also mis-fired on line 1 because the boot
  `CereprocWarmup` line had set the "previous audio finished" timestamp.
  **Conclusion: `INTERRUPT` in any timing adjacent to the next `PLAY_OUTPUT`
  poisons that line. Stop trying to free the slot with `INTERRUPT`.**

- **`CloudTTSSupplement` as the turn-close signal (workaround C, 2026-09-04).**
  After each `CloudTTSResponse`, published a matching `CloudTTSSupplement`
  (`event_id`/`chunk_num` + text/markup/timings), puppet-mode only. On the robot
  both lines published the supplement (confirmed in the log) and **line 2 was
  still silent**. **Conclusion: the `CloudTTSSupplement` is NOT the
  end-of-utterance signal that frees the slot.** (This was the isolated retry of
  §2.8's supplement; it is now independently ruled out.)

- **Config-push crash — FIXED & VERIFIED 2026-09-04, but NOT the cause of
  line-2 silence.** `tts_service.push_config` now strips identity fields
  (`child_pii`, `pairing_status`) from mid-session toggle pushes, so a
  puppet/cloud_tts toggle carries no `SetMentorName` delta (identity is still
  sent once at bootstrap). On-robot this **eliminated the crash**: the 13:11:47
  puppet enable produced no `pthread_mutex_lock … destroyed mutex` SIGABRT, only
  a benign `[MISSING_PII] Child PII has not been filled in` warning confirming
  the strip. **However, line-2 silence persisted** in the same run (line at
  13:15:22 silent despite a clean, crash-free brain). **Conclusion: the crash
  and the line-2 silence are independent — removing the crash does not un-silence
  line 2.** Keep the fix (the brain crashing on every puppet enable was a real
  defect and a confound), but it is not the line-2 answer.

**Still untried (ranked), after crash ruled out:** (1) **pace-only** — wait for
line 1 to drain before sending line 2 with **no `INTERRUPT`** (workaround B;
never tested in isolation, and it lacks the `INTERRUPT` poison that killed A);
(2) a terminal `cmd:` mark or `chunk_num` final-chunk semantics (§5C
alternatives); (3) capture a real Embodied Polly `CloudTTSResponse` to learn the
actual turn-close contract (§4.3). Note: the working **non-puppet** conversation
path voices consecutive turns fine, so if puppet's one-way `PLAY_OUTPUT` cannot
be made to speak line 2, driving speech through the remote-chat path instead is
the pragmatic escape.

---

## 1. Symptom

In puppet mode with `cloud_tts=1`:

1. First `POST /tts/speak` → `PLAY_OUTPUT` → robot emits `CloudTTSRequest` → we
   synthesize (OpenAI) → `CloudTTSResponse` → **Moxie speaks. Heard.**
2. Second (and every later) `POST /tts/speak` in the same TELEHEALTH session →
   same wire shape, robot still emits `CloudTTSRequest`, we still answer with
   audio → **silent.** The mouth does not move.
3. Restarting the service makes the *next* first line audible again, then the
   same silence returns.

## 2. What is confirmed (so the model is well-constrained)

From the prior on-robot runs:

- Line 1 and line 2 are **byte-shape identical** on the wire: both
  `event_id=''`, `chunk_num=0`, same markup, valid marks, valid audio. The
  *silent* line is often the **faster** synth, so it is **not** a timeout, not a
  dropped HTTP call, not a missing `PLAY_OUTPUT`, not a synthesis failure.
- The mouth **moves** on line 1, so marks/visemes are consumed at least once.
- The failure is **on the robot, after** it asked for audio and after we
  published a valid `CloudTTSResponse`. The only thing that differs between the
  heard line and the silent line is **the robot's internal state after one
  successful utterance**.

That last point is the whole ballgame: **something on the robot is consumed by
the first utterance and is not replenished/cleared for the second.** The user's
framing — "Moxie has a slot; after the first request it fills and does not get
removed" — is precisely this model.

## 3. The hypothesis: a single output slot that latches busy

Model: the TELEHEALTH output path has one "speech output slot." Line 1 acquires
it, plays, and **completes without releasing it** (no end-of-utterance signal
the robot recognizes, or the release only happens in a state puppet mode never
re-enters). Line 2 acquires nothing, so its audio is dropped even though the
request/response round-trip looks perfect.

Two levers follow directly, and both are what the user proposed:

- **Release the slot** after each utterance (clear it), or
- **Enlarge / recycle the slot** so it can hold more than one utterance.

## 4. Important: the naive "clear before each request" is already ruled out

Sending `INTERRUPT` **immediately before** each `PLAY_OUTPUT` was tried
(`PUPPET_SPEAK_SILENCE_ANALYSIS.md` §2.6). It does **not** work and is actively
harmful: the `INTERRUPT` cancels the **new** line's cloud-output path — the
robot still emits the `CloudTTSRequest`, we still answer, and nothing is voiced.
So "clean the slot before each request" in the obvious form makes things worse,
not better.

The fix therefore has to clear the slot **from the previous utterance**, timed
so it does not race the next line's output — i.e. **after line 1 finishes**, as
a distinct step, not fused to line 2's send.

## 5. Workarounds, ordered by expected value

### A. Free the slot *after* line 1 completes — TRIED 2026-09-04, FAILED

**Result: reverted.** Implemented as drain → standalone INTERRUPT → settle →
PLAY_OUTPUT, gated on a recorded previous-line audio duration. On the robot both
lines still came out wrong. Why it cannot work: in the normal case the operator
sends line 2 *after* line 1 has finished, so the drain is already elapsed
(`remaining ≤ 0`) and the sequence collapses to **INTERRUPT immediately before
PLAY_OUTPUT** — exactly the ruled-out §2.6 pattern that cancels the new line's
cloud output. It also mis-fired on line 1 because the boot `CereprocWarmup` line
set the "previous audio" timestamp. A only differs from §2.6 while line 1 is
still playing, which is precisely when line 2 is not sent. Do not retry.

### A′ (original, kept for reference). Free the slot *after* line 1 completes

Send a standalone `INTERRUPT` once line 1's audio has actually finished playing,
then let line 2's `PLAY_OUTPUT` land in a freed slot. Timing is the crux:

- We know line 1's audio duration — the `CloudTTSResponse` carries it (the
  `spoke … total_ms` / the PCM byte count ÷ sample rate). Wait
  `audio_duration + margin` (e.g. +300–500 ms) after line 1's audio was
  published, **then** `INTERRUPT`.
- Line 2's `PLAY_OUTPUT` must come **after** that `INTERRUPT` has settled
  (`SPEAK_INTERRUPT_PAUSE_S`, currently 0.35 s), so the interrupt clears the
  *old* utterance and does not cancel the *new* one.

This is the untested variant explicitly called out in §4.2 of the analysis
("`INTERRUPT` after line 1's audio duration, not before line 2"). It is the most
direct implementation of "clear the slot," done at the only time that can work.

### B. Let the slot drain naturally: pace the requests

If the slot self-releases but only after the utterance fully drains, then simply
**not sending line 2 until line 1 has finished** may be enough. Enforce a
per-device minimum gap of `last_audio_duration + margin` before the next
`PLAY_OUTPUT`. Cheaper than (A) — no `INTERRUPT` — and worth trying first as an
A/B, because if it works it means the slot was fine and we were just talking
over it.

### C. Send a real end-of-utterance signal so the slot self-frees

**`CloudTTSSupplement` variant: TRIED 2026-09-04, FAILED, reverted.** The proto
has no explicit "final" flag, so the `CloudTTSSupplement` companion was the
candidate. After each `CloudTTSResponse` a matching `CloudTTSSupplement`
(`event_id`/`chunk_num` + text/markup/timings) was published, puppet-mode only.
On the robot both lines published it and line 2 was still silent — so the
supplement is not the turn-close signal. Do not retry the supplement.

**Still-open variants of C** (a real turn-close signal may still exist, just not
the supplement): a terminal `cmd:` mark appended to the response `marks` (we send
a closing `sil` viseme, but the robot may want an `ssml`/`cmd` "output done"
marker), or correct `chunk_num` final-chunk semantics so the robot knows the turn
is complete. Both are unverified without a captured reference frame (§4.3).

Original candidates:

The slot may stay busy because the first cloud-TTS turn is **never explicitly
closed**. Candidates the robot's Polly-era protocol may expect:

- A terminal marker in `marks` (we already append a closing `sil` viseme; the
  robot may want an `ssml`/`cmd` end marker instead or in addition).
- `CloudTTSSupplement` — a companion proto (`text`, `markup`, timings) that
  exists in the schema and was historically sent alongside the audio. An earlier
  experiment sent it after every frame (§2.8) but bundled with other reverted
  changes; sending **only** a correct terminal supplement is untried in
  isolation.
- Correct `chunk_num` semantics: mark the utterance's **final** chunk so the
  robot knows the turn is complete (see E).

This is lower-confidence without a captured reference frame (§4.3 of the
analysis), but it is the "release the slot the way the firmware expects" option
and would fix the root cause rather than paper over it.

### D. Keep-awake nudge before line 2

Between lines the robot drifts toward `sleep`/`standby`. A `wakeup` (or a
lightweight keep-alive) immediately before line 2's `PLAY_OUTPUT` removes power
state as a confound. Combine with (A) or (B); alone it is unlikely to be
sufficient but it is cheap and removes noise.

### E. Collapse N lines into one multi-chunk utterance

Instead of N separate `/tts/speak` requests (N separate slot acquisitions), send
one utterance the robot treats as a single turn, using `chunk_num` to stream
parts and a final-chunk marker to close it. If the slot is per-**turn**, one
turn with many chunks never needs a second acquisition. This changes the API
shape (a "speak these lines as one turn" endpoint) but sidesteps the slot
entirely for the common "say a few sentences" case.

### F. "Enlarge the slot" — speculative, no known config knob

There is **no documented device prop** that sets output-slot depth. The robot's
exposed `cloud_tts_*` props are voice/engine/gain/rate only
(`cloud_tts_engine=polly`, `cloud_tts_voice_id=Joanna`, `cloud_tts_gain_db`,
`cloud_tts_speech_rate`, `cloud_tts_use_mp3`) — none is a queue length. So
"increase the slot so it holds more than one request" is not actionable via
known configuration today. If a firmware/props reference surfaces a queue/slot
setting, it would belong here; until then, treat enlarging as blocked and pursue
**releasing** (A/B/C) instead.

## 6. Recommended implementation sketch (A, with B as the cheaper A/B)

All of this lives in the puppet speak path (`src/services/telehealth.py`
`speak_line` / `play_output` and the `/tts/speak` route), plus knowing line 1's
audio duration (already computed in `speak_service` and reported as
`total_ms` / derivable from the published PCM length).

1. **Record, per device, when the current utterance's audio will finish.**
   After publishing a `CloudTTSResponse`, store
   `busy_until = now + audio_duration_ms`.
2. **Before the next `PLAY_OUTPUT` for that device:**
   - **(B)** if `now < busy_until`, wait until `busy_until + margin` (drain).
   - **(A)** then send a standalone `INTERRUPT`, wait `SPEAK_INTERRUPT_PAUSE_S`,
     and only then `PLAY_OUTPUT` the new line.
3. **Serialize per device** (there is already a `_speak_lock` per device in
   `telehealth.py`) so two `/tts/speak` calls can't interleave the drain +
   interrupt + play sequence.
4. Gate the whole behaviour behind a setting (e.g.
   `PUPPET_FREE_SLOT_AFTER_LINE: bool` and a `PUPPET_SLOT_MARGIN_MS: int`) so it
   can be A/B-tested on the robot and reverted cleanly, matching how prior
   hypotheses were flag-gated.

Do **not** fuse the `INTERRUPT` to the same instant as `PLAY_OUTPUT` (that is the
ruled-out §2.6 path). The `INTERRUPT` targets the *finished* line; the settle
protects the *new* line.

## 7. How to test / what to capture

Run on the robot (puppet on, `cloud_tts=1`, charged so it does not sleep):

1. Baseline: two `/tts/speak` lines, confirm line 2 silent (reproduce).
2. Enable (B) only: two lines paced by `audio_duration + margin`. Heard twice?
   → the slot self-drains and pacing is the whole fix.
3. If (B) alone still silent, enable (A): drain + post-line `INTERRUPT` + settle
   + line 2. Heard twice? → the slot needed an explicit release.
4. Capture `client-service-robot-status` and `client-service-activity-log`
   around line 1 vs line 2 (both are now logged) — look for any
   output-slot / speaking / MarkingUp transition that flips after line 1 and
   never resets. That log is what will confirm the slot model directly.

Success = line 2 (and 3, 4, …) audible within one TELEHEALTH session, no service
restart needed.

## 8. Do-not-repeat list (already tested, did not fix line 2)

From `PUPPET_SPEAK_SILENCE_ANALYSIS.md`, so no cycles are wasted: WAV header
repair / raw PCM (§2.1, 2.5), fabricated `INTERRUPTING` state (§2.2), client
pre-warm (§2.3), marks/visemes (§2.4), **`INTERRUPT` before every `PLAY_OUTPUT`**
(§2.6), empty puppet schedule (§2.7), timestamp echo + `CloudTTSSupplement`
after every frame (§2.8), faking a response-only `event_id` (§2.9), automarkup
`ssml` TALK/NONE wrap (§2.10), `START_SESSION`/`session_id`/`output.event_id`
(§2.11). The open, untried levers are exactly §5 A–E of this doc.
