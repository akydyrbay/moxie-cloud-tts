# Agent prompt — hear one Russian or Kazakh line after wake (non-puppet)

Copy everything below the line into a new agent session. Prefer this prompt plus the code over `README.md`. If this prompt conflicts with older plans on **current** code, trust the code and `docs/AGENTS.md`.

---

You are implementing a **narrow, on-device goal** in the repo `moxie-cloud-tts`.

## Goal (done when this is true)

After Moxie **wakes** (button / touch / `POST /tts/wakeup`) while **not** in TELEHEALTH/puppet (`moxie_mode` absent, `PUPPET_MODE_AUTO_ENABLE=false`, `cloud_tts=1`):

**A human hears Moxie speak any one complete line in Russian or Kazakh**, synthesized by this server’s OpenAI Cloud TTS, with the mouth moving.

That is the whole milestone. Not a conversation loop. Not STT. Not a second line. Not “notify says we said it.” **Heard audio.**

Acceptable spoken lines (pick one, keep it short):

- Russian: `Привет! Как у тебя дела?`
- Kazakh: `Сәлем! Қалайсың?`
- Existing confirmation already in code: `Отлично!`
- Existing opener in `REMOTE_CHAT_OPENERS`

An optional HTTP trigger **is allowed** if it is easier than waiting for `OPENMOXIE_CHAT` `command=prompt`, **as long as it is not puppet**. See “HTTP that is allowed” below.

## Success log (required)

In `log/debug.log` / `mise start` output, in one session after wake:

1. `bootstrapping device d_… (cloud_tts=1 moxie_mode=None)` — **not** `moxie_mode=TELEHEALTH`.
2. A `CloudTTSRequest` whose **speakable** markup (after stripping `<mark>` / `<usel>`) contains Cyrillic ru or kk (not `Excellent!`, not `Good morning!`, not `Moxie, let's go`).
3. Matching `spoke event_id=… language=ru` or `language=kk` with non-tiny `audio_bytes`.
4. Operator confirmation: they **heard** that line.

`notify` with Russian text **without** a matching `CloudTTSRequest` for that markup is **failure**. The 12:28 run did exactly that: brain accepted `Привет!…`, mouth voiced leftover `Excellent!`.

## What this robot already proved (do not re-investigate)

- MQTT/TLS :8883, device discovery, bootstrap config, ProtoSubscribe.
- `cloud_tts=1` → Unity sends `CloudTTSRequest`; we must answer with one `CloudTTSResponse` (PCM 24 kHz mono + viseme/`cmd:` marks) echoing `event_id`/`chunk_num`.
- **Mouth works.** TELEHEALTH first `/tts/speak` in ru/kk **plays** with lip sync. WAKEUP English lines (`Good morning!`, `Moxie, let's go!`, `Excellent!`) also play via the **same** `cloud_tts_request.py` → `speak_service.py` path.
- Language is in the **PCM**, not a robot language pack. Unity will carry non-ASCII markup (puppet Kazakh proved it). Do not run OpenMoxie `automarkup`/`unidecode` (it would turn `Привет` into `Privet`).
- Wake-gate STT inject: before chat, on `END_OF_SPEECH`, reply `zmqSTTResponse.speech = "Moxie, let's go"` (`WAKEUP_TRIGGER_LINE`).
- Modules handshake advertises `OPENMOXIE_CHAT`; schedule is `only_chat` (`DEFAULT_SCHEDULE`).
- `max_enroll="0"` + `child_pii` at bootstrap (OpenMoxie: disable `ENROLLCONVO`). Firmware may still run WAKEUP.
- `OPENMOXIE_CHAT` `prompt` **has already happened** at least once (12:28): opener was `Tx`’d; **no** `CloudTTSRequest` for that opener; Excellent used the WAKEUP STT uuid.

**TTS is not the bug.** The bug is **which markup gets a live `CloudTTSRequest` after wake**, and not colliding with WAKEUP’s `Excellent!`.

## Architecture (do not mix channels)

| Channel | Messages | Can make Moxie speak? |
|---|---|---|
| **Ears** | `zmqSTTRequest` / `zmqSTTResponse` | **No.** `speech` = what the child said. |
| **Mouth** | `CloudTTSRequest` / `CloudTTSResponse` | **Yes — only this.** Robot must be waiting on that `event_id`. |
| **Brain** | JSON `remote_chat` / `query_result` | Sets `output.markup` (text Moxie *intends* to say). Does not play audio by itself. |

Invariant: **you cannot push audio.** Unsolicited `CloudTTSResponse` (including `request_source=REMOTECHAT_TTS_REQUEST` without a Unity request) is discarded or leaves MarkingUp. OpenMoxie never sends CloudTTS; this robot **asks** first.

Spoken ru/kk = markup the robot copies into `CloudTTSRequest` → we synthesize **that** request. Optionally steal an **existing** English request’s slot by synthesizing ru/kk **for that `event_id`**.

HTTP `/tts/speak` is **TELEHEALTH PLAY_OUTPUT**. It is puppet. **Out of scope for this goal.**

## Recommended order 

### Path A — Steal the WAKEUP Cloud TTS slot (fastest “any line”)

After `"Moxie, let's go"`, Unity often requests TTS of local **`Excellent!`** with `event_id` = the STT/WAKEUP volley uuid (12:28: `5c491ac0-…`). The robot **will** play whatever PCM we put on that response.

**A1 (brain, already in tree):** `WAKEUP`/`ENROLLCONVO` `start|continue` → `GLOBAL_COMMAND` «Отлично!» with `<usel variant="2" genre="none">Отлично!</usel>`, **no `launch` on the primary action**. Prove on a **fresh boot** with current `remote_chat.py`. The 12:28 log still showed FALLBACK + Excellent — that was the old path.

**A2 (mouth steal):** In `cloud_tts_request.handle_cloud_tts_request`, if stripped markup is onboard English confirmation (`Excellent!`, maybe `Good morning!` only if you must), synthesize a fixed ru/kk line **for that `event_id`**. Still echo `cmd:` marks from the original markup so firmware leaves MarkingUp.

This is crude and is allowed **only** to satisfy “hear any line.” Log clearly that you overrode markup. Do not steal every English line forever without a flag.

### Path B — `OPENMOXIE_CHAT` opener owns its own `CloudTTSRequest`

Intended OpenMoxie enter path: **schedule** after entrance (`DEFAULT_SCHEDULE` already `OPENMOXIE_CHAT/default`). `max_enroll="0"` skips enroll. WAKEUP is firmware; docs offer `wake_module: WAKEUP_LAUNCHER` to skip it.

Then:

```
Rx remote-chat … module=OPENMOXIE_CHAT/default command=prompt
Tx remote_chat … markup with <usel> and ru/kk
inbound CloudTTSRequest … markup contains that same ru/kk (new event_id, not Excellent’s)
spoke … language=ru|kk
```

If opener `Tx` happens but Cloud TTS is still Excellent: **do not send the opener as a second push.** The robot already `notify`’d the opener and opened the mic. Options:

- Delay sending the opener until Excellent’s `CloudTTSRequest` is answered (hard; prompt `event_id` may already be consumed).
- Make WAKEUP confirmation ru (Path A) so the slot is not English, then opener can request next — **or** skip opener until a **new** volley.
- After listen starts, answer `END_OF_SPEECH` with a canned STT line so `reprompt` creates a **new** remote-chat turn and a **new** Cloud TTS request (after Excellent is done). Today, once `note_conversation_active`, STT is **unanswered** → `UUID has gone too long`. Answering END is required to not hang; empty FINAL is “heard nothing,” not “don’t listen.”

`launch OPENMOXIE_CHAT` / `exit_module`: **second** `response_actions` entry only. Speak-only primary. Primary `launch` skipped Cloud TTS on this robot. Option B is unproven vs firmware ignoring cloud launch on a local module.

`WAKEUP_LAUNCHER`: advertise as remote module with content ids `ftue|more_10|less_10|first_time_today|scheduled`, add `"wake_module": {"module_id": "WAKEUP_LAUNCHER"}` to the schedule JSON. It is a stub that should fall into the first scheduled item. Implement only if A/B schedule entry still fails.

**Do not** set `brain_entrances_available="0"`.

### Path C — HTTP trigger without puppet (allowed)

`POST /tts/wakeup` only unsleeps. It does **not** speak ru/kk.

You **may** add something like `POST /tts/say-live` that, **only if** `moxie_mode` is not TELEHEALTH:

- Reuses a **live** router `event_id` (`remote_chat._last_event_id`) to send `RemoteChatResponse` with ru/kk `<usel>` markup (`GLOBAL_COMMAND` if you must beat a local line), **or**
- Waits for the next `CloudTTSRequest` and fills it with ru/kk (steal), **or**
- After `OPENMOXIE_CHAT` is active, puts the line in `output.markup` on the next `prompt`/`reprompt`/`continue`.

You **must not**:

- Call `telehealth.play_output` / `/tts/speak`.
- Set `moxie_mode=TELEHEALTH` or `PUPPET_MODE_AUTO_ENABLE=true`.
- Empty the schedule.
- Publish `CloudTTSResponse` with a made-up `event_id`.
- Prepend `INTERRUPT` to “help” playback (cancels the cloud output path).

If HTTP is used, document the curl in the PR/commit message and still require the success log above.

## Hard caveats (this firmware — do not “fix” by ignoring them)

1. **Locked OS.** You cannot add a firmware mode. `OPENMOXIE_CHAT` is a **cloud** module; `WAKEUP`/`ENROLLCONVO`/`TELEHEALTH` are onboard. A new `module_id` does not skip WAKEUP.
2. **One Cloud TTS waiter.** Match `event_id`/`chunk_num` exactly. Puppet line 2: request+response exist, **nothing plays**. Do not chase that. Do not use PLAY_OUTPUT during autonomous listen (same slot fight).
3. **Empty puppet `event_id` is valid** — echo `''`. Conversation volleys use real UUIDs. Onboard Excellent used the **RemoteChatRequest/STT uuid**. Chat `prompt` used a **different** uuid (`12283584-…`) that never got a Cloud TTS request. Echo Unity; never invent an id only on the response.
4. **Every `CloudTTSRequest` gets exactly one response** (audio or ~200 ms silence). Missing reply → MarkingUp → sleep.
5. **Never** OpenAI TTS `response_format=wav` (0xFFFFFFFF RIFF). PCM 24 kHz; wrap WAV locally only if `CLOUD_TTS_AUDIO_RAW_PCM=false`.
6. **Echo `cmd:` marks** from request markup (`marks.command_marks`). Empty speakable text still needs those marks (entry animation / CereprocWarmup).
7. Wrap speakable remote-chat text as `<usel variant="2" genre="none">{text}</usel>`. Plain markup can `notify` without raising `CloudTTSRequest`.
8. **Do not** put spoken lines in `zmqSTTResponse.speech`.
9. **Do not** mark conversation active on `OPENMOXIE_CHAT` `notify` (stops wake inject too early).
10. Identity (`child_pii`, `pairing_status`) **bootstrap only**. Mid-session push SIGABRTs (`UserIdModule::SaveUserName`). `tts_service._without_identity_fields` on live toggles.
11. Do not subscribe MQTT `#`. Do not hand-edit `src/protos/`.
12. `SUPPORTED_LANGUAGES` is `ru`,`kk`. `detect_language` labels English as `ru` (no Kazakh letters); OpenAI still speaks the **characters**. Do not add English as a first milestone.
13. STT PCM is **16 kHz**; TTS is **24 kHz**. Do not mix. STT must stay under ~8 s (`OPENAI_REQUEST_TIMEOUT_S`).
14. Do not modify `telehealth.py` speak/interrupt sequencing unless the operator explicitly asks to change puppet.
15. Global voice “moxie let’s chat” is firmware `chat_request` **after** session; it does not skip WAKEUP. There is no “start chatting mode” command.
16. `notify` is not playback. `module=/_get_current_content_id ()` (empty `module_id`) is a broken FSM — FALLBACK loop; reboot rather than piling more FALLBACK Russian.

## Files you are allowed to change

Prefer the smallest diff that produces a heard line:

- `src/services/remote_chat.py` — entrance confirmation, schedule/`wake_module`, delayed opener, option B **secondary** launch.
- `src/services/cloud_tts_request.py` — optional English-markup override (Path A2), behind a setting.
- `src/services/stt_request.py` — always FINAL on chat-phase `END_OF_SPEECH` if the robot hangs after opener; canned line only if you need a second volley.
- `src/config.py` — flags, `WAKEUP_LAUNCHER` in modules/schedule, opener copy.
- `src/router/tts.py` — **only** if adding a non-puppet HTTP say endpoint.
- Tests: `tests/test_remote_chat.py`, `tests/test_cloud_tts_request.py`, `tests/test_stt_request.py`, `tests/test_http.py`.

Do **not** rewrite MQTT, puppet, proto, or OpenAI engines for this goal.

## Verify

```bash
mise test
mise start    # MQTT + API
```

Power on or `curl -X POST localhost:8000/tts/wakeup`. Confirm `moxie_mode=None`. Watch logs for the success sequence. If you added HTTP, curl it **after** wake, still with puppet **off**.

Pytest must not require a live robot. On-device: one heard ru/kk line beats any unit test.

## Out of scope (do not do in this pass)

- Full listen → OpenAI STT → chat loop (M2).
- Fixing TELEHEALTH second `/tts/speak`.
- Parent UI, Control Center, copying an OpenMoxie fork.
- Switching openers to English “to make TTS easier” (mouth already plays ru/kk; English vs Excellent is harder to tell apart).
