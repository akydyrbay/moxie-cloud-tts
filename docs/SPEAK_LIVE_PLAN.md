# Speak Live Plan

Status: **IN PROGRESS.** Last updated 2026-09-07 (M1 option A in tree).

Target: run Moxie in **normal (non-puppet) conversational mode** and have it
speak **Russian / Kazakh**, driven by this server: opener via Cloud TTS, then a
listen → chat → TTS loop.

Puppet / TELEHEALTH (`POST /tts/speak`) is **out of scope** for this plan. That
path is a different firmware mode (command-only). Why it was dropped as the
conversation path: `SPEAK_PUPPET_ANALYSIS.md`. This document is the
autonomous conversation path.

---

## 0. Current status (read this first)

### The one-line problem

The robot wakes, stays awake, answers STT, and Cloud TTS plays audio — **but it
never reaches `OPENMOXIE_CHAT`**, the only module whose spoken text this server
fully controls (ru/kk openers + chat replies). Instead it runs onboard English
onboarding:

`WAKEUP` → `ENROLLCONVO` ("Clearance Check": chocolate vs strawberry, fly vs
invisible, …).

Because `cloud_tts=1`, we faithfully synthesize that English. The robot loops
there. `note_conversation_active()` never fires.

**Next work is not “make TTS work”.** TTS already works. Next work is: **prove
on-device that option A skipped `ENROLLCONVO`**, then **M2** (real OpenAI STT).
If ENROLLCONVO still runs after a reboot with `max_enroll=0`, go to option B
(`exit_module` / `launch OPENMOXIE_CHAT`).

### Intended end-to-end flow (what we are building)

```
wakeup
  → pass WAKEUP gate ("Moxie, let's go")
  → skip ENROLLCONVO (max_enroll=0 at bootstrap)  ← IN TREE, not 100% proven on-device
  → OPENMOXIE_CHAT
  → opener (REMOTE_CHAT_OPENERS ru/kk) → Cloud TTS → plays
  → listen (zmqSTT)
  → OpenAI STT (ru/kk)
  → OpenAI chat reply in the same language
  → Cloud TTS → plays
  → listen again → cycle
```

### What already works (do not re-investigate)

| Capability | Where | Status |
|---|---|---|
| Wake + `mode:active`, no stall | firmware + entry-seq / `cmd:` marks | ✅ |
| Entry-sequence `cmd:` marks echoed | `src/services/marks.py` → `speak_service._build_response` | ✅ |
| Config toggle no longer crashes brain (identity stripped on mid-session push) | `tts_service._without_identity_fields` | ✅ |
| STT subscribe + dispatch | `mqtt_service.PROTO_SUBSCRIBE_TYPES`, `register_stt_request_handler`, `_dispatch_stt_request` | ✅ |
| Canned STT reply on `END_OF_SPEECH` (wake-gate only) | `src/services/stt_request.py` | ✅ wake inject; chat inject empty |
| Enrollment skip via bootstrap config | `HARDCODED_DEVICE_CONFIG.max_enroll="0"` | ✅ in tree, prove on boot |
| Cloud TTS of whatever markup the robot requests | `cloud_tts_request.py` → `speak_service.speak` | ✅ |
| Remote-chat brain (modules / schedule / `OPENMOXIE_CHAT` turns) | `remote_chat.py` | ✅ |
| OpenAI STT **bench** (not wired to the robot) | `stt/openai_stt.py`, `POST /tts/transcribe` | ✅ exists, unused on robot path |
| Openers already ru/kk | `config.REMOTE_CHAT_OPENERS` | ✅ (typos aside) |
| Empty schedule while puppet is on | `handle_schedule_query` | ✅ (irrelevant here; puppet stays off) |

### The blocker (evidence: 16:28–16:31 run)

`DEFAULT_SCHEDULE` already asks only for `OPENMOXIE_CHAT`. The robot **does not
honor that until brain-entrance onboarding finishes**. Sequence observed
**before** `max_enroll="0"`:

1. `module=WAKEUP` `notify` — English “Here we go again!” spoken via our Cloud TTS.
2. STT injects `WAKEUP_TRIGGER_LINE` = `"Moxie, let's go"` → robot `continue`s WAKEUP.
3. Robot enters **`ENROLLCONVO`**. Scripted English Q&A. We synthesize it.
4. `ENROLLCONVO` is **not** a remote module → `handle_router_request` FALLBACK
   (`Извини, я не расслышала…`). Injection never answers enrollment questions.
5. `OPENMOXIE_CHAT` never launches → STT stays in `wake-gate` forever.

Enrollment is **not** the same as “has `child_pii` in config”. Bootstrap already
sends PII; `ENROLLCONVO` still ran at `max_enroll="2"`. Completing (or skipping)
the **script**, not just nicknaming the child, appears to be the gate.

**Option A is now in tree:** bootstrap `max_enroll="0"` (OpenMoxie: `"0"` to
disable ENROLLCONVO) plus paired `child_pii`. Prove on a real boot: after WAKEUP
+ `"Moxie, let's go"`, next router module must be `OPENMOXIE_CHAT`. If not, B.

---

## 1. Architecture

Keep two channels separate. Mixing them is how earlier plans went wrong.

| Channel | Proto | Direction | Meaning |
|---|---|---|---|
| **Ears** | `zmqSTTRequest` / `zmqSTTResponse` | robot → server → robot | mic PCM in; **recognized text** back. `speech` = *what the child said*. Cannot make Moxie *speak* a line. |
| **Mouth** | `CloudTTSRequest` / `CloudTTSResponse` | robot → server → robot | robot asks to voice markup; **audio** back. This is the only way ru/kk is heard. |
| **Brain** | JSON `remote_chat` / `query_result` | both | handshake, which module runs, and the **text** Moxie will say next (`output.markup`). |

Spoken ru/kk is **never** posted as `/tts/speak` on this path and **never** put
in `zmqSTTResponse.speech`. It is set as `RemoteChatResponse.output.markup`
(opener or chat reply). The robot copies that into `CloudTTSRequest.markup`.
`cloud_tts_request.py` only synthesizes what the robot already decided to say.

```
                    ┌──────── ROBOT ────────┐
 wakeup cmd          │                      │
 (optional)          │  WAKEUP gate         │
                     │    zmqSTT ───────────┼──► stt_request  (inject "Moxie, let's go")
                     │    ◄─────────────────┼──  zmqSTTResponse
                     │                      │
                     │  ENROLLCONVO         │  ← skip / exit / complete  [M1]
                     │                      │
                     │  OPENMOXIE_CHAT      │
                     │    prompt ───────────┼──► remote_chat opener (ru/kk)
                     │    CloudTTSRequest ──┼──► OpenAI TTS
                     │    ◄ audio ──────────┤  ▶ plays opener
                     │                      │
                     │    listen zmqSTT ────┼──► OpenAI STT (ru/kk)     [M2]
                     │    ◄ transcript ─────┤
                     │    reprompt(speech) ─┼──► OpenAI chat
                     │    CloudTTSRequest ──┼──► OpenAI TTS
                     │    ◄ audio ──────────┤  ▶ plays reply
                     │    (loop)            │
                     └──────────────────────┘
```

`POST /tts/wakeup` only unsleeps the robot. It does not start chat, skip
onboarding, or send ru/kk text.

`PUPPET_MODE_AUTO_ENABLE` must stay **false**. Puppet empties the schedule and
parks the robot in TELEHEALTH so `OPENMOXIE_CHAT` never runs.

---

## 2. Target conversation loop (after OPENMOXIE_CHAT)

Once `handle_router_request` sees `module_id` in `REMOTE_CHAT_MODULE_IDS`:

1. `note_conversation_active(device_id)` — STT leaves wake-gate.
2. First `command=prompt` → `_opener_for()` → `REMOTE_CHAT_OPENERS` (ru/kk) →
   MQTT `/devices/{id}/commands/remote_chat`.
3. Robot emits `CloudTTSRequest` with that markup → OpenAI TTS → playback.
4. Robot listens. VAD streams `zmqSTTRequest` (`START` / `SPEECH` / `END`,
   16 kHz PCM, one `uuid` per utterance).
5. **Today:** no canned chat STT (`INJECTED_STT_LINE=""`). **Target:** accumulate
   PCM, OpenAI STT, `zmqSTTResponse.speech = transcript` (same `uuid`, `type=FINAL`).
6. Robot sends `RemoteChatRequest(command=reprompt, speech=transcript)`.
7. `_remote_module_response` → `openai_chat.respond` (same language) →
   `output.markup` = reply.
8. Same Cloud TTS path as the opener. Then listen again.

Chat and TTS already implement steps 6–8. They will not run in ru/kk until
step 1 happens (robot actually in `OPENMOXIE_CHAT`).

English as a **spoken** conversation language is not in `SUPPORTED_LANGUAGES`
(`ru`, `kk` only). OpenAI STT can return English; chat/TTS will not accept `en`
until that list (and the system prompt) is extended. Do not block M2 on English.

---

## 3. Milestones

Order matters: **M1 (leave onboarding) before M2 (real STT)**. Real STT during
`WAKEUP`/`ENROLLCONVO` will replace `"Moxie, let's go"` with whatever the child
said and can keep the robot stuck in the gate.

### Goal 1 — Canned STT injection — **DONE**

Answer each completed listen session with a fixed `zmqSTTResponse` instead of
transcribing. Wires ears so the robot does not STT-timeout / force-idle.

**In tree:** `stt_request.py`, MQTT subscribe/dispatch, `main.py` registration,
`tests/test_stt_request.py`, `WAKEUP_TRIGGER_LINE` / `INJECTED_STT_LINE`.

Keep wake-gate injection until `OPENMOXIE_CHAT` is reached. Chat-phase canned
injection is **off** (`INJECTED_STT_LINE=""`); the opener is
`REMOTE_CHAT_OPENERS`. After M2, transcribe only while
`device_id in _conversation_active`.

---

### M1 — Skip or finish onboard onboarding → reach `OPENMOXIE_CHAT` — **OPTION A IN TREE**

**Why:** Schedule already says `only_chat`. Firmware still runs `WAKEUP` →
`ENROLLCONVO` first. FALLBACK does not eject those modules; it tells the robot
“this is not a cloud module, play local,” which is how English Q&A continues.

**What has to change (current logic):**

| Current behavior | Problem | Change |
|---|---|---|
| `handle_router_request` FALLBACK for any non-`OPENMOXIE_CHAT` module | `ENROLLCONVO` stays active; we reply «не расслышала» | Intercept `WAKEUP` / `ENROLLCONVO` (see §4) instead of generic FALLBACK |
| `_strip_action_tags` / `exit_module` / `launch` only used on `OPENMOXIE_CHAT` replies | Cannot tell firmware to leave a **local** module | Optionally send those actions on onboard router requests |
| STT always injects wake trigger until chat | During ENROLLCONVO the trigger is the wrong “heard” line | After a successful skip, `note_conversation_active` must fire; until then wake-gate injection is OK for WAKEUP only |
| `cloud_tts_request` synthesizes every markup | English enrollment is voiced | Optional stopgap: silence / skip synth when request is clearly onboard English — **does not** advance the FSM |
| `HARDCODED_DEVICE_CONFIG` / bootstrap PII | Identity on bootstrap is OK; mid-session identity push still crashes the brain | **Done (option A):** `max_enroll="0"` + paired `child_pii` at bootstrap only. Do not set `brain_entrances_available="0"` (risks blocking chat). Use `_without_identity_fields` for live toggles. If ENROLLCONVO still runs on-device, go to option B. |

**Success:** log shows `Rx remote-chat … module=OPENMOXIE_CHAT … command=prompt`,
then `Tx remote_chat` with opener text, then Cloud TTS of that opener (ru/kk).
No further `ENROLLCONVO` chocolate/strawberry lines.

**Not success:** English enrollment still speaking, even if STT injection fires.

Detailed skip options: **§4**.

---

### M2 — Real OpenAI STT in the conversation loop — **AFTER M1**

Replace canned `INJECTED_STT_LINE` **once `OPENMOXIE_CHAT` is active**.

**Current logic to change** (`stt_request.handle_stt_request`):

- Today: ignore `audio_content`; on `END_OF_SPEECH` publish canned `speech`.
- Needed:
  1. Accumulate `audio_content` per `(device_id, uuid)` on START/SPEECH/END.
  2. On END: `audio.pcm_to_wav(buffer, sample_rate=16000)` — **not**
     `settings.AUDIO_SAMPLE_RATE_HZ` (that is 24 kHz TTS).
  3. `await run_in_threadpool(openai_stt.transcribe, wav, None, "stt.wav")`
     with `language=None` so OpenAI auto-detects ru vs kk.
  4. `zmqSTTResponse.speech = result.text`; echo `uuid`; `type=FINAL`.
  5. On failure still reply (`error_code` / fallback line) so the robot is not
     left waiting.
  6. Cap buffer size; drop sessions if `END_OF_SPEECH` never arrives.
  7. Stay under the robot STT wait (`OPENAI_REQUEST_TIMEOUT_S` is 8s).

**Do not** transcribe during wake-gate. Branch:

```
if device_id not in _conversation_active:
    inject WAKEUP_TRIGGER_LINE   # unchanged
else:
    transcribe accumulated PCM   # M2
```

Empty `INJECTED_STT_LINE` can mean “no canned chat stimulus”; wake trigger stays
its own setting.

**Reuse, do not rewrite:** `src/services/stt/openai_stt.py`, `audio.pcm_to_wav`.

**Tests:** extend `tests/test_stt_request.py` — fake transcribe, assert WAV is
16 kHz, assert wake-gate still injects, chat phase uses transcript.

**Verification:** speak ru/kk to the robot in `OPENMOXIE_CHAT`; log
`openai stt transcribed …`; remote-chat `speech=` matches; reply TTS in the
same language.

---

### M3 — Opener ru/kk — **mostly done, polish**

`REMOTE_CHAT_OPENERS["default"]` is already Russian. Fix copy-paste typos
(«чём» / «Что тебя на уме»). Optional Kazakh opener variant.

This only plays **after M1**. No new channel.

**Verification:** first `command=prompt` on `OPENMOXIE_CHAT` →
`Tx remote_chat … text='Привет!…'` → `spoke … language=ru` (or kk) with no
child speech required.

---

### M4 — `/tts/speak` during autonomous mode — **later, optional**

Not required for the loop above. `PLAY_OUTPUT` while listening can fight the
output slot (same class of bug as puppet line-2). Prefer putting the line in
`remote_chat` `output.markup` (reactive). Proactive PLAY_OUTPUT only when idle.

---

## 4. Skipping onboarding (`ENROLLCONVO`) — options in detail

Firmware runs `WAKEUP` → `ENROLLCONVO` as a **brain-entrance** before the cloud
schedule. We cannot delete those modules from the robot. We can try to make the
robot believe they are done, eject them, or walk the script.

**Recommended order:** A (config / enrolled) → B (cloud exit/launch) → D
(answer the script) → C (silence English) only as a stopgap.

None of A–D is proven on-device except that **today’s FALLBACK + canned STT
does not skip enrollment**.

### A. Mark onboarding already done (config / enrollment state)

**Idea:** if the robot thinks mentor enrollment finished, it should skip
Clearance Check and take `DEFAULT_SCHEDULE` (`OPENMOXIE_CHAT`).

**Where to look:**

- `src/config.py` — `HARDCODED_DEVICE_CONFIG`
  (`brain_entrances_available`, `child_pii`, `pairing_status`,
  `cloud_schedule_reset_threshold`, …).
- `src/services/remote_chat.py` — `ensure_device_bootstrapped` (the one place
  a **full** config including identity is allowed).
- `src/services/tts_service.py` — never re-push `child_pii` / `pairing_status`
  mid-session (native SIGABRT in `UserIdModule::SaveUserName`).
- OpenMoxie clone (`~/Desktop/hri-lab/openmoxie`): `no_onboarding` schedule,
  how it sets enrolled/PII, any `schedule_sensitive` / entrance flags.

**Levers tried / remaining (bootstrap only):**

1. ~~Set `brain_entrances_available` to `"0"`~~ **Not used.** That flag is
   “ALLOW GLOBAL LAUNCH PATTERNS”; turning it off may block chat entry and is
   not the enrollment disable.
2. **Done:** `max_enroll="0"` (OpenMoxie: disable ENROLLCONVO) + paired
   `child_pii` / `pairing_status` at bootstrap. Aligning with a post-Clearance
   Check snapshot is still available if `"0"` is ignored.
3. Copy OpenMoxie’s enrolled-device config rather than inventing fields.
4. Do **not** strip PII at bootstrap to “avoid ENROLLCONVO” — missing PII is
   more likely to **force** enrollment. Strip identity only on later toggles.

**How to know it worked:** after WAKEUP + `"Moxie, let's go"`, next router
module is `OPENMOXIE_CHAT`, not `ENROLLCONVO`.

**Unknown:** which exact prop/bit is the gate. PII-on-bootstrap was **not**
enough in the 16:28 run.

### B. Force-exit / launch chat from the cloud (change FALLBACK)

**Idea:** when `handle_router_request` sees `module_id` in `{WAKEUP, ENROLLCONVO}`
(and not a `notify` we should ignore), do **not** send `output_type=FALLBACK`.
Send a RemoteChatResponse with `response_actions` that leave the module:

- `{"action": "exit_module", "output_type": "GLOBAL_RESPONSE"}`
- and/or `{"action": "launch", "module_id": "OPENMOXIE_CHAT", "content_id": "default", "output_type": "GLOBAL_RESPONSE"}`

Shape already exists in `_strip_action_tags` / `_build_response`, but is only
applied to **our** chat replies today.

**Where to change:** `remote_chat.handle_router_request` onboard branch
(~FALLBACK). Possibly a small helper `skip_onboard_module(device_id, request)`.

**Tests:** `tests/test_remote_chat.py` — ENROLLCONVO `command=start|continue`
publishes `exit_module` and/or `launch OPENMOXIE_CHAT`, not FALLBACK text.

**Unknown:** whether firmware honors a **cloud** exit/launch while the **local**
module owns the stack. If it ignores the action, you will see another
ENROLLCONVO turn immediately.

**Do not** `notify` with FALLBACK; `notify` is already a no-reply path.

### C. Suppress English audio (stopgap only)

**Idea:** in `cloud_tts_request.handle_cloud_tts_request`, if markup is clearly
onboard English (or if we track “current module is ENROLLCONVO” from the last
router request), `publish_silence` instead of OpenAI TTS.

**Where:** `cloud_tts_request.py`; optional module hint stored from
`handle_router_request`.

**Effect:** robot stops talking English. **FSM still stuck.** Use only while
debugging skip A/B so operators are not drowned in enrollment audio.

### D. Complete the enrollment Q&A via STT injection

**Idea:** `ENROLLCONVO` expects specific phrases (e.g. “I am your Moxie Mentor”,
chocolate/strawberry). Drive STT injection from a **per-module script** instead
of always `"Moxie, let's go"`.

**Where:** `stt_request._injected_line` needs the current onboard module (pass
from `remote_chat` when it sees `module=ENROLLCONVO`), plus an ordered list of
answers.

**Cost:** brittle; firmware copy can change. Useful as an **experiment** to
prove “finished enrollment → schedule runs `OPENMOXIE_CHAT`”. If that proof
lands, prefer encoding the same end-state via A rather than living on a Q&A
script.

---

## 5. File map — update vs create

### Change these

| File | Why |
|---|---|
| `src/services/remote_chat.py` | **M1:** replace onboard FALLBACK with skip/exit/launch; keep `note_conversation_active` only on `OPENMOXIE_CHAT`. Bootstrap stays the only full identity push. |
| `src/config.py` | **M1:** entrance / enrollment props; **M3:** opener copy; **M2:** maybe `STT_SAMPLE_RATE_HZ = 16000` so 16 kHz is not a magic number. |
| `src/services/stt_request.py` | **M2:** accumulate PCM; transcribe after chat-active; keep wake-gate inject. |
| `src/services/cloud_tts_request.py` | **M1-C optional:** silence onboard English. |
| `src/services/tts_service.py` | Do not regress identity-strip on toggles if M1 adds bootstrap-only enrollment fields. |
| `src/services/language.py` / `llm/openai_chat.py` / `synthesis/openai_tts.py` | Only if adding `en` later — not required for ru/kk. |
| `tests/test_remote_chat.py` | M1 skip/exit actions. |
| `tests/test_stt_request.py` | M2 accumulate + transcribe; wake-gate unchanged. |
| `tests/test_cloud_tts_request.py` | Only if silence-on-onboard is implemented. |

### Already in place (do not recreate)

| File | Role |
|---|---|
| `src/services/mqtt_service.py` | STT + CloudTTS subscribe, framing, `_schedule` onto asyncio. |
| `src/main.py` | Registers STT + CloudTTS + remote-chat handlers. |
| `src/services/speak_service.py` | Mouth: OpenAI TTS → `CloudTTSResponse`. |
| `src/services/stt/openai_stt.py` | Mouth-adjacent ears engine; wire from `stt_request` in M2. |
| `src/services/audio.py` | `pcm_to_wav` for 16 kHz STT buffers. |
| `src/services/llm/openai_chat.py` | Reply text once robot is in chat. |
| `src/router/tts.py` | `/tts/wakeup` only unsleeps; `/tts/speak` is puppet. |
| `src/protos/embodied/perception/audio/zmqSTT_pb2.py` | Request/response schema. |
| `src/protos/embodied/unity/CloudTTS_pb2.py` | Mouth schema. |

### Create only if a skip option needs a new home

| File | When |
|---|---|
| e.g. `src/services/onboard_skip.py` | If ENROLLCONVO intercept + exit/launch + enrollment script would clutter `remote_chat.py`. Not required if M1 stays a short branch in `handle_router_request`. |

No new MQTT service. No new HTTP speak path for this plan.

### Reference (read-only)

- OpenMoxie STT contract: `~/Desktop/hri-lab/openmoxie/site/hive/mqtt/zmq_stt_handler.py`
- OpenMoxie proto subscribe / `send_zmq_to_bot`: `moxie_server.py`
- OpenMoxie `no_onboarding` / enrollment config: same clone
- Puppet line-2 / config-crash: `SPEAK_PUPPET_ANALYSIS.md` (do not
  re-push identity mid-session while implementing M1)

---

## 6. `zmqSTTResponse` contract (unchanged)

```python
resp = zmqSTTResponse()
resp.uuid = req.uuid                 # MUST echo session id
resp.type = resp.ResponseType.FINAL
resp.speech = recognized_text        # wake inject, canned chat, or OpenAI STT
resp.timestamp = now_ms()
resp.start_timestamp = session_start_ms
resp.end_timestamp = now_ms()
# on STT failure, still reply:
# resp.error_code = 66; resp.error_message = str(e)
```

Publish framed via `mqtt_service.publish_proto` on
`/devices/{device}/commands/zmq`.

---

## 7. Risks

1. **M1 is unverified.** Config skip and cloud `exit_module` may be ignored.
   Time-box A then B; do not assume 100%.
2. **Identity config crash.** Full `child_pii` / `pairing_status` only at
   bootstrap.
3. **16 kHz vs 24 kHz.** STT WAV must be 16 kHz.
4. **STT latency.** Late FINAL → `zmqSTT: UUID has gone too long` → idle.
5. **Real STT too early.** Transcribing before `OPENMOXIE_CHAT` breaks WAKEUP.
6. **English STT vs ru/kk chat.** Transcript in English with current allowlist
   will fail chat/TTS; keep M2 scoped to ru/kk.
7. **`/tts/speak` in this mode** (M4) can steal the output slot.

`BugID: 17` and brief `Forcing IDLE … Waiting` between entrance turns were
benign in the 16:28 run. Do not chase them unless they persist after M1.

---

## 8. TL;DR

Intended path:

**wakeup → `"Moxie, let's go"` on STT → leave ENROLLCONVO → `OPENMOXIE_CHAT` →
ru/kk opener via Cloud TTS → listen → OpenAI STT → chat → Cloud TTS → loop.**

Already built: ears wake-gate injection, mouth TTS, brain for `OPENMOXIE_CHAT`,
ru/kk openers, bench STT, bootstrap `max_enroll="0"`.

Missing: **on-device proof that option A skipped ENROLLCONVO**, then **M2 wire
`openai_stt.transcribe` into `stt_request` after chat is active**. If A fails
on boot, option B (`exit_module` / `launch`).

Spoken ru/kk is always `remote_chat` `output.markup` → robot `CloudTTSRequest`.
Never STT `speech`, never `/tts/speak`, for this plan.
