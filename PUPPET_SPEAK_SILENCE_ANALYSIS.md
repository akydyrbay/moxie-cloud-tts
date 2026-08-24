# Why repeated `/tts/speak` goes silent — analysis & candidate fixes

> Scope: puppet (TELEHEALTH) mode. The **first** `/tts/speak` plays on the robot;
> a **later** one produces no audio and the robot appears to idle / sleep.
> Written from the live logs of 2026‑08‑24 (device `d_d05e3c66‑…`) plus the
> current source. Confidence markers: **[CONFIRMED]** = shown in the logs/code;
> **[LIKELY]** = strong circumstantial evidence; **[TO VERIFY]** = plausible,
> needs one more experiment.

---

## 1. What is now ruled OUT

- **`event_id` correlation is NOT the cause. [CONFIRMED]**
  The line that *played* (`14:01:28`, `markup='Сәлем! …'`) carried
  `event_id=''`, and so does every other telehealth line. A line that plays and
  a line that stays silent are indistinguishable by `event_id`. Stamping an id
  into `PLAY_OUTPUT.output` did not change it either — the firmware always emits
  an empty `event_id` for a telehealth‑initiated `CloudTTSRequest`. Drop this
  thread entirely.

- **Puppet mode DID engage. [CONFIRMED]**
  After `POST /tts/puppet {enabled:true}` at `13:59:46`, the robot's
  conversation handshake (`modules` / `schedule` / `mentor_behaviors` /
  `remote_lines` queries) **stopped** — the log is quiet from `13:59:49` to the
  next `/tts/speak` at `14:01:26`. Those queries **resumed within one second**
  of disabling puppet at `14:01:50`. So `moxie_mode=TELEHEALTH` reached the robot
  and it left conversation mode. The earlier theory "puppet never engaged" is
  wrong for this run.

- **Live puppet‑enable is not fatally crashing the robot in this run.**
  The native `libc`/`DEBUG` tombstones seen in the very first session are absent
  here.

---

## 2. Key facts the log establishes

| Time | Event | Note |
|---|---|---|
| 13:59:26 | robot connects | first config push is `moxie_mode=None` (puppet not yet on) |
| 13:59:28–44 | modules/schedule/mentor queries | robot is in **conversation mode** |
| 13:59:44 | `CloudTTSRequest event_id='f4e62005‑…' markup='<break time="0.5s"/>'` | a **conversation‑mode** turn *does* carry a real `event_id`; it had no speakable text → we sent silence |
| 13:59:46 | puppet enabled → `moxie_mode=TELEHEALTH` pushed | conversation loop goes quiet after this |
| 14:01:26 | `/tts/speak` → `wakeup` sent | robot had been idle ~95 s |
| 14:01:28 | `PLAY_OUTPUT`; inbound `CloudTTSRequest event_id='' … markup='Сәлем! …'` | telehealth line, empty id |
| 14:01:30 | **`constructed OpenAI client`** | client built *lazily, mid‑request* (~2 s cost on the first call) |
| 14:01:34 | `spoke … total_ms=6301` | **played.** End‑to‑end ~6 s from `PLAY_OUTPUT` to audio |
| 14:01:50 | puppet disabled | conversation loop resumes at 14:01:51 |

Two numbers matter downstream: the robot sat **idle ~95 s** before the speak,
and the round trip was **~6.3 s** with a ~2 s one‑off client‑build tax inside it.

> Note: this particular capture contains only **one** `/tts/speak` (then puppet
> was turned off), so the silent‑second‑line itself is not in *this* log. The
> hypotheses below are ranked against everything we do see plus the reproducible
> report that any second line is silent.

---

## 3. Candidate causes, ranked

### H1 — The robot dozes when idle, and a telehealth line only fires if it is awake *at that moment*. **[LIKELY]**
Puppet mode keeps the robot from running the schedule, but it does **not** keep
the screen/audio awake indefinitely — after a line it drifts back to idle and, on
this firmware, to sleep. The first `/tts/speak` in the log worked **only because
it was immediately preceded by a `wakeup`** (14:01:26) after a 95 s idle. A
second line lands after another idle gap; if the single `wakeup` + 1.5 s delay
doesn't fully wake it (or it re‑sleeps before `PLAY_OUTPUT`), no audio.

- Evidence for: robot idle 95 s then needed a wakeup; "goes to sleep" reported.
- Evidence against: the *first* wakeup worked, so wakeup is at least sometimes enough.
- Fix candidates (in order):
  1. **Increase `SPEAK_WAKEUP_DELAY_S`** (e.g. 1.5 → 3.0 s) so a deep‑idle robot
     is fully listening before `PLAY_OUTPUT`. Cheap; already wired
     (`src/config.py`).
  2. **Interrupt + wakeup before speak**: send `INTERRUPT` then `wakeup` then
     `PLAY_OUTPUT`, to clear any half‑finished playback/markup state.
     `telehealth.interrupt()` already exists.
  3. **Keep‑alive while in puppet mode**: a background task that sends `wakeup`
     every N seconds so the robot never reaches sleep. Most robust; more code.
- How to verify: two speaks 30 s apart in puppet mode; check whether the second
  produces an **`inbound CloudTTSRequest`** line at all (see §4).

### H2 — Synthesis latency is close to the robot's "waiting for audio" timeout. **[LIKELY]**
With `cloud_tts=1` the robot enters *MarkingUp* and waits for the cloud audio; if
it waits too long it force‑idles (and, per the project's own notes, can sleep).
The played line took **~6.3 s**, inflated by a **~2 s lazy OpenAI‑client build on
the first call** (`14:01:30`). `OPENAI_REQUEST_TIMEOUT_S=8.0` is *larger* than a
plausible ~5 s robot timeout, so a slow synth can overrun the robot's patience —
intermittently, which matches "sometimes it just doesn't play."

- Evidence for: 6.3 s round trip; 2 s client tax; 8 s client timeout > robot budget.
- Evidence against: the first line still played at 6.3 s (so the true robot budget may be >6 s, or puppet relaxes it).
- Fix candidates:
  1. **Pre‑warm the OpenAI client at startup** (call `get_client()` in the
     lifespan) so no request pays the ~2 s build. `src/services/openai_client.py`
     is already a lazy singleton — just touch it once on boot.
  2. **Lower `OPENAI_REQUEST_TIMEOUT_S`** to ~4 s so a slow call fails fast and
     we publish the correlated silence *before* the robot gives up on its own
     (keeps it out of a bad idle path), rather than delivering audio it has
     already stopped waiting for.
  3. **Cut synthesis time**: try `response_format=pcm` (no WAV assembly), a
     faster model, or streaming. Measure `total_ms` and aim < 3 s.
- How to verify: watch `openai tts synthesized … in <N> ms` and `total_ms`;
  correlate slow calls (>4–5 s) with the silent lines.

### H3 — We answer `schedule` with a chat‑launching schedule even in puppet mode. **[TO VERIFY]**
`handle_schedule_query` always returns `DEFAULT_SCHEDULE`, which launches
`OPENMOXIE_CHAT/default` (`src/services/remote_chat.py:204`,
`src/config.py:173`). In puppet mode the robot mostly stops asking, but any
schedule/`modules` poll that slips through is told to enter a conversation
module. With no real conversation (STT isn't wired) that turn stalls and the
robot idles → sleeps, which would make the *next* telehealth line land on a
sleeping robot.

- Fix: when `moxie_mode == TELEHEALTH`, answer `schedule` (and `modules`) with an
  **empty / no‑launch** payload so the robot stays parked and awake in puppet
  mode instead of being nudged toward chat.
- How to verify: after a line, do the modules/schedule queries resume (robot
  drifting back to conversation)? If yes, this is contributing.

### H4 — Bootstrap is re‑pushed on **every** `modules` query. **[CONFIRMED behaviour; impact TO VERIFY]**
`handle_modules_query` calls `forget_bootstrap()` then `ensure_device_bootstrapped()`
every time (`src/services/remote_chat.py:186‑187`), re‑publishing the full
`config` **and** `ProtoSubscribe`. That is a lot of config churn while the robot
is cycling, and a mid‑session `config` push can reset robot state. Probably not
the primary cause (the loop is quiet during puppet), but it is unnecessary
thrash and a candidate aggravator when puppet is off or flapping.

- Fix: only re‑bootstrap on an actual reconnect (broker‑log connect event),
  not on every `modules` query; or re‑push `ProtoSubscribe` only, not the full
  config, when already bootstrapped.

### H5 — A single `wakeup` wakes the screen but the robot re‑sleeps fast. **[TO VERIFY]**
Wake config is present (`wake_button/audio_wake/touch_wake = 1`), and the one
wakeup in the log worked. But "wakeup" may only bring it up briefly; without
sustained engagement it can nod off again before/after a line. This overlaps
H1 and points to the keep‑alive fix as the durable answer.

---

## 4. The one experiment that separates these

Run this in puppet mode and capture the **full** server log:

1. `POST /tts/puppet {enabled:true}` and wait ~5 s.
2. `POST /tts/speak` (line A). Confirm audio.
3. Wait ~30 s (let it idle).
4. `POST /tts/speak` (line B, any text).

Then read the log around step 4 and classify:

- **No `inbound CloudTTSRequest` after line B's `PLAY_OUTPUT`** → the robot never
  even asked for audio ⇒ it was **asleep/not listening** ⇒ H1/H5 (wake / keep‑alive).
- **`inbound CloudTTSRequest` present, `spoke …` present, but nothing heard** →
  audio produced but robot **discarded/played nothing** ⇒ H2 (timeout) or a
  playback/state issue; check `total_ms` and whether it exceeded ~5 s.
- **Modules/schedule queries reappear between A and B** → robot **drifted back to
  conversation** ⇒ H3 (schedule) is contributing.

The new full‑field log line already in place tells you which branch you're in:

```
CloudTTSRequest fields from <dev>: event_id='' chunk_num=0 user_id='…' timestamp=… markup='…'
```

Its **presence/absence** for line B is the single most decisive signal.

---

## 5. Recommended fix order (highest value first)

1. **Pre‑warm the OpenAI client at startup** and **lower the timeout to ~4 s**
   (H2). Removes the ~2 s first‑call tax and keeps every call inside the robot's
   waiting window. Small, safe, and independently worthwhile.
2. **Keep the robot awake in puppet mode** — either raise `SPEAK_WAKEUP_DELAY_S`
   and add `INTERRUPT`+`wakeup` before each line, or add a periodic `wakeup`
   keep‑alive while `moxie_mode=TELEHEALTH` (H1/H5).
3. **Serve an empty schedule (and skip the chat‑launch) while in puppet mode**
   so the robot doesn't get pulled back toward conversation (H3).
4. **Stop re‑bootstrapping on every `modules` query** (H4) to cut config churn.

Items 1 and 4 are low‑risk cleanups regardless of the root cause. Items 2 and 3
are the ones most likely to fix the silent second line; confirm with the §4
experiment before committing to one.

---

## 6. Pointers (files & lines)

- `/tts/speak` wake‑then‑speak: `src/router/tts.py` (`speak`, uses
  `SPEAK_AUTO_WAKEUP` / `SPEAK_WAKEUP_DELAY_S`).
- Telehealth primitives (`PLAY_OUTPUT`, `INTERRUPT`, `wakeup`):
  `src/services/telehealth.py`.
- Inbound request handling + the new diagnostic dump:
  `src/services/cloud_tts_request.py`.
- Synthesis + robot‑waiting notes: `src/services/speak_service.py`.
- Lazy OpenAI client (pre‑warm target): `src/services/openai_client.py`.
- Schedule answered regardless of puppet mode:
  `src/services/remote_chat.py` (`handle_schedule_query`, `DEFAULT_SCHEDULE` in
  `src/config.py`).
- Re‑bootstrap on every modules query:
  `src/services/remote_chat.py` (`handle_modules_query`).
- Timeout / auto‑enable / puppet knobs: `src/config.py`.
