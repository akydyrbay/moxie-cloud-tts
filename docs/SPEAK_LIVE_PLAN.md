# Speak Live Plan — autonomous Kazakh/Russian conversation

Updated: **2026-09-29**

Status: **Kazakh output and English-input conversation are operator-confirmed;
the kk/ru English input bridge and reliability fixes are implemented and locally verified.**

The latest Sep 28 capture has 22 successful transcriptions: 13 Cyrillic-only
inputs fail to become router turns; all nine English-containing inputs become
turns (one is a rejected-by-the-new-code prompt echo). The server now preserves
original input for GPT, sends faithful English to the robot, and bounds response
length and provider waits. **322 tests pass.** Hardware acceptance of the bridge
is still required. See [localization repair](LOCALIZATION_FIXES.md) and
[the evidence/checklist](../prompt.md).

## Goal and completion criteria

Moxie wakes in autonomous mode, greets the user in Kazakh, understands actual
Kazakh or Russian microphone input, gives a relevant spoken reply in the
selected language, and listens again. Kazakh uses the language code **kk**;
Russian uses **ru**.

The operator has now confirmed hearing **“Сәлем! Қалайсың?” after wake-up**.
That completes the earlier task's first audible-line milestone.
It proves that this robot can play our Kazakh cloud audio outside TELEHEALTH.
The later Sep 28 capture additionally proves a remote-chat router turn occurred.
The later evening capture proves real transcription and English-input routing;
kk/ru routing and sustained conversation remain unverified.

The conversation goal is complete only after an operator verifies:

- Five consecutive real question/answer turns in Kazakh and five in Russian.
- Replies address what the person actually said; no canned child transcript.
- Each reply is audible, has mouth movement, and is followed by another listen.
- Silence or an unclear utterance gets a localized clarification and recovery.
- A controlled provider failure does not leave a permanent waiting state.
- A fresh reboot/reconnect can repeat the flow without stale IDs or manual
  recovery between normal turns.

Kazakh should remain the startup language. Support for Russian must be explicit
in the conversation language policy; today's fixed Kazakh default is not an
automatic bilingual implementation. Recognition quality and firmware transitions
still require on-device validation, so full success is not yet guaranteed.

Website-triggered action scripts, arbitrary proactive `tts.speak()` calls, and
rewriting puppet playback are deferred. This plan concerns autonomous conversation.

## Current progress

| Area | Implemented today | Evidence / remaining limit |
| --- | --- | --- |
| Connection and discovery | MQTT/TLS :8883, device discovery, config bootstrap and ProtoSubscribe | Sep 25 log shows a connected, discovered device with `cloud_tts=1 moxie_mode=None`. QR pairing tools also exist. |
| Kazakh speech | Every English/Latin autonomous TTS line is translated to `DEFAULT_TTS_LANGUAGE` before synthesis | Actual OpenAI translation of greeting, clarification and fruit question verified. Fixed greeting and phrase allowlist removed. Operator confirmed translated wake-up speech Sep 28. |
| Cloud audio response | OpenAI PCM, 24 kHz mono, original IDs/chunks, command marks and generated mouth marks | Used by the successful wake-up request. Empty/failed synthesis receives silence. |
| Remote brain | Modules advertisement, only-chat schedule, native entrance preserved with FALLBACK, chat opener and LLM replies | Sep 27 handoff fix matches OpenMoxie and passes log regressions. Sep 28 actual `prompt` establishes hardware entry; later operator feedback confirms Kazakh replies to English input. |
| Language policy | English TTS translates to default `kk` (or configured `ru`); Cyrillic speech passes through | No chat-active TTS guard. The remote opener remains English source text; subsequent LLM replies still use the existing configured-language policy. Sustained kk/ru-input conversation is unproven. |
| Microphone transport | Subscribed `zmqSTTRequest` frames reach the STT handler | Chat audio is accumulated by device/UUID, including START and END; per-frame counts are logged. |
| Recognition engine | `stt/openai_stt.py` provides `transcribe()` | Called off-loop on chat END with a 16 kHz mono PCM16 WAV, auto language and no retries. Actual kk/ru transcripts are logged; routing through the new bridge needs testing. |
| Conversation lifecycle | Router-driven phases; reconnect/exit cancels pending recognition and clears PCM | Chat FINAL carries faithful English input or error 66; original text is retained; other onboard modules still receive an error. UUID deduplication is bounded. |
| Regression coverage | Translation boundary tests and Sep 23/25 replays, plus earlier handoff/STT regressions | See the translation report for the latest full-suite result. Mocked payload checks do not prove live STT or repeated playback. |

### What the Sep 25 log actually shows

Source: `log/debug.log`, run approximately 14:00–14:10 on Sep 25.
Line numbers below refer to that capture and can change when logs rotate.

| Time / log line | Event | Conclusion |
| --- | --- | --- |
| 14:01:16 / 16 | Bootstrap with `cloud_tts=1 moxie_mode=None` | Autonomous cloud audio is enabled. |
| 14:05:27 / 62–70 | Schedule and modules queries answered | Discovery alone is not the remaining blocker; handshake replies are being sent. |
| 14:06:06 / 112–113 | `Good morning.` request overridden with `Сәлем! Қалайсың?` | A real robot speech request provides the playable slot. |
| 14:06:10 / 139 | Same event, `language=kk audio_bytes=139200 marks=28` | Audio was published; the operator confirms it was heard. |
| 14:06:19–20 / 142–148 | Canned wake transcript, WAKEUP `continue`, Kazakh GLOBAL_COMMAND, chat activity marked SCHEDULED | The intended chat activity is recorded, but there is no actual chat router prompt. |
| 14:06:20–21 / 149–155 | SESSIONDISMOUNT starts; “Cool! You're a terrific mentor!” reaches TTS unchanged | Firmware proceeds into its local end-of-session content. |
| 14:06:36 / 188–191 | BO requests “Great!” | Another local line bypasses the narrow wake-up replacement. |
| 14:08:32 onward / 225 onward | Empty module ID with `_get_current_content_id ()`; English clarification requests | Routing has reached a broken/unknown module state, not our working conversation loop. |

The successful greeting used event ID
`c0cfb174-449c-4029-a30d-ae47f1dba9ea`. It was a replacement for an English
wake-up request, not proof that an `OPENMOXIE_CHAT` opener reached playback.

### Why English returned in the old implementation

The old wake-up override recognized only a small set of complete local phrases.
It did not translate every firmware line. Its broader TTS language guard applied
only after an actual remote-chat router turn established an active chat session.
A SCHEDULED activity and a `notify` do not establish that session.

In this capture, the robot advanced into SESSIONDISMOUNT and later an empty
module. Their English requests were synthesized unchanged. A log label
of `language=kk` is only the default label for text without Cyrillic; it does
not make English synthesis input become Kazakh.

The Sep 27 investigation found a concrete protocol mismatch: entrance replies
used `GLOBAL_COMMAND`, which overrides the local response and its actions.
OpenMoxie returns `FALLBACK` for onboard modules. The Sep 7 archive records that
FALLBACK reaching an actual chat prompt; the Sep 25 GLOBAL_COMMAND runs do not.
The handoff fix restores that priority. The new translation stage uses the same
audio response path for all English lines and no longer needs actual chat entry.
This is strong evidence for a discarded native transition, but the local
firmware action itself is not in the capture. A fresh robot run must confirm it.

The latest capture does not show the earlier ENROLLCONVO question sequence.
English apologies do not establish a recognition failure; no real recognition ran.

## Architecture and protocol constraints

FastAPI is the operator interface. Robot audio and conversation traffic use
MQTT, with framed protobuf for microphone/audio messages and JSON for the brain.

| Channel | Request → response | Meaning |
| --- | --- | --- |
| Ears | `zmqSTTRequest` → `zmqSTTResponse` | Microphone audio → faithful English translation of human speech, with original retained |
| Brain | `RemoteChatRequest` → `RemoteChatResponse` | A conversation turn → what Moxie should say |
| Mouth | `CloudTTSRequest` → `CloudTTSResponse` | Robot-selected spoken markup → synthesized audio |

Target flow:

```text
wake → Kazakh wake-up greeting → complete WAKEUP → actual OPENMOXIE_CHAT turn
     → translated chat opener → correlated REMOTECHAT audio → playback
     → microphone/VAD → original transcription → English bridge → same-UUID FINAL
     → real remote-chat turn → original text recovered for GPT → kk/ru reply markup
     → correlated REMOTECHAT audio → playback → listen again
```

Native robot output stage (remote-module text uses its live router turn instead):

```text
robot-selected English TTS markup → strip tags → OpenAI translation to kk
    → OpenAI PCM synthesis → original request ID/chunk + original command marks
```

Preserve these constraints throughout implementation:

1. Moxie originates a native speech request or an accepted remote-module turn.
   Never push unsolicited CloudTTS audio,
   invent a replacement event ID, or reuse an old volley as if it were live.
2. Echo each TTS request's `event_id` and `chunk_num`, including an empty ID.
   Every inbound TTS request needs one response: audio or short silence.
3. Keep original `cmd:` marks even when replacing spoken text or returning
   silence. Generate mouth marks from the text/audio actually synthesized.
4. Request OpenAI raw PCM. TTS is 24 kHz mono PCM16; wrap WAV locally only when
   configured. Never request the provider's streamed WAV for robot playback.
5. STT audio is expected to be 16 kHz mono PCM16. Verify real frame contents;
   do not wrap microphone bytes using the 24 kHz TTS setting.
6. STT `speech` is a faithful English translation of human input by default
   (original text in bridge-off mode). Do not put a robot greeting, apology,
   or LLM response there. The existing wake trigger is a limited entrance shim.
7. Do not suppress the STT protocol to disable listening. An unanswered request
   can time out, force-idle or sleep. Turning off transcription is not equivalent
   to completing the firmware's listening state.
8. `notify` is history information, not an audio request or playback receipt.
   SCHEDULED is not proof that a remote module started.
9. Keep spoken output as `<usel variant="2" genre="none">…</usel>`. Preserve
   Cyrillic; do not transliterate through automarkup/unidecode. Remote-module
   output raises no robot TTS request; voice it once as REMOTECHAT audio on the
   router `event_id` (see the invariant in [AGENTS.md](AGENTS.md)).
10. Keep the primary speech response free of launch/sleep side effects.
    If a supported transition action is tested, place it after the speak action.
11. Push identity fields only during bootstrap. Preserve identity stripping on
    live config toggles; do not set `brain_entrances_available="0"`.
12. Keep puppet off and the autonomous schedule populated. Do not change puppet
    sequencing, generated protobufs, or MQTT transport to solve chat routing.

## Remaining work, in order

### Completed priority — Translate English speech before synthesis

`translation.py` now translates all English/Latin speech, with no phrase list or
chat-entry condition. It caches successful text, rejects unusable provider
output, and bounds translation with a deadline. Timeout/failure returns silence
with original request correlation and commands. Already Cyrillic lines and
puppet speech are unchanged. This output milestone is complete enough to proceed to M2; STT is described below.

The old fixed greeting settings and chat-active TTS guard are removed. Native
modules retain their text/actions; the remote opener is an ordinary English
source line that follows the same translation path when its audio is requested.

Sep 28 acceptance: the operator heard translated Kazakh wake-up speech; logs
show multiple successful translations and cloud audio. Continue checking
activity and clarification lines during longer sessions. Translation does not resolve native
English-only answer matching; after recognition, those activities may need
input translation/intent mapping. Our own remote modules can consume kk/ru text.

### M1 — Establish ownership of a real conversation turn

**Status: actual remote-chat entry observed Sep 28; repeat playback remains an operator check.**

The immediate target is an actual `OPENMOXIE_CHAT` router request, an accepted
response, and a matching robot TTS request. Extending the English replacement
list can mask the symptom without advancing this milestone.

Implemented and verified locally:

1. Preserved 21 selected log events from the Sep 7 successful entry and both
   Sep 25 failures as a replay fixture (removed Sep 29 once the handoff was
   confirmed on hardware; recoverable from git history).
2. Compared modules, schedule and response shapes with the copied OpenMoxie.
   Registration and schedule already matched. Restored entrance `FALLBACK` and
   the reference legacy `fallback=false` flag; both normal response envelopes
   now compare equal to OpenMoxie's Volley for identical inputs.
3. Preserved native entrance actions without adding a speculative launcher.
   Kazakh audio now comes from translation of the chosen English speech line.
4. Synchronized brain/STT phase on real router turns. Local dismount/unknown
   turns disable the wake shim; actual entrance turns re-arm it. Notifications
   and SCHEDULED activities do not activate or clear chat. Bootstrap also
   clears pending STT timestamps for that device.
5. Completed listens outside the wake shim now receive a correlated FINAL
   error using OpenMoxie's existing code 66 contract. This fixes the unanswered
   request, not recognition. M2 below now replaces the placeholder for active chat.
6. Added entry/exit/missing-module log markers and regressions covering repeat
   wake, device isolation, delayed notify, the native Excellent/prompt race,
   original TTS correlation, and disabled wake injection after local exit.

Sep 28 shows the real chat prompt at 16:34:44 and chat listens at 16:35:12/20.
The notify opener is not a playback receipt. On the next run, verify the sequence
in [README](../README.md#verify-the-remote-chat-handoff). If it still dismounts
without a real chat turn, preserve that new capture before trying any secondary
launch/exit or wake-module experiment. An empty module must be logged and the
robot rebooted for a controlled retry, not treated as an active chat session.

**Acceptance:** after a fresh autonomous wake, the log shows a real
`OPENMOXIE_CHAT` turn, localized response markup, a corresponding live TTS
request, and an audible reply. The robot then opens a listen session instead of
immediately moving to SESSIONDISMOUNT. Complete M2 before claiming a stable
conversation: M2 still requires microphone acceptance on hardware.

### M2 — Verify the English input compatibility bridge

**Status: server implementation and regression tests complete; hardware acceptance pending.**

Recognition preserves actual kk/ru/en. The new default-enabled bridge translates
non-English input into English for Moxie while retaining original text under
(device, listen UUID) with lifecycle invalidation and bounded storage. A real
router request on that UUID recovers the original for GPT. Optional protobuf
language/original metadata defaults off until firmware semantics are established.

The entire END processing/recognition/translation path has a six-second deadline.
The former imperative recognition prompt is removed; exact prompt echo,
near-zero noise and grossly implausible output are rejected. Short valid words
remain eligible. Errors return FINAL 66, never invented human speech.

Hardware acceptance: compare English and Kazakh versions of the same question.
Require original ASR → faithful English FINAL → real router turn with the same
UUID → original_recovered=True → relevant audible kk reply → another listen.
Repeat with Russian input, then a configured ru reply session. Follow the
[operator sequence](LOCALIZATION_FIXES.md#operator-acceptance). If the English
FINAL still yields no turn, investigate the firmware boundary; tests do not
prove the assumed acceptance behavior.

### M3 — Make language and reply behavior consistently bilingual

**Status: fixed-language Kazakh policy implemented; bilingual turns remaining.**

Today's `_ChatSession.language` is fixed from `DEFAULT_TTS_LANGUAGE`.
`openai_stt.transcribe(language=None)` leaves its language label unknown;
it does not use the configured TTS default as detected-language evidence. The Cyrillic heuristic
also cannot reliably distinguish every short Russian/Kazakh phrase.

Target policy: start in Kazakh, answer clear Kazakh input in Kazakh and clear
Russian input in Russian, and retain the last selected language for silence,
names, mixed or ambiguous input. An explicit user request to switch between the
two supported languages should take precedence. Validate fixed-language kk and
ru sessions first, then add switching without destabilizing the loop.

Tasks:

1. Separate recognized input language, chosen response language and rendered
   TTS language. Use trustworthy recognizer metadata where available, otherwise
   an evaluated text-language policy. Do not report a default as detected.
2. Keep the selected language consistent across the chat session, system
   instructions, fallback, assistant history and active-chat TTS guard.
   Already Cyrillic Russian replies now bypass English translation. Define
   any broader ru-to-kk policy explicitly rather than silently changing them.
3. Implemented: language validation accepts ambiguous short Cyrillic, Roman
   numerals and occasional names. It remains heuristic; evaluate it with real
   conversation before introducing automatic switching.
4. Nonempty first-turn `reprompt` speech now reaches the LLM (fixed with M2).
   Keep this regression while improving multilingual history.
5. Implemented: empty reprompts use bounded localized clarification/repeated
   questions and never become a synthetic user apology.
6. Implemented: event-based input/output history deduplicates notifications and
   excludes known pending/failed/cancelled audio from assistant context.
7. Pin each generated reply's language while it is in flight so a later language
   switch cannot rewrite an earlier TTS request incorrectly. Keep LLM failures
   bounded and respond with a short fallback in the selected language.

**Acceptance:** replies reflect actual input and retain context over several
turns in each language. An explicit language switch works; ambiguous input does
not cause random switching. No English model output or repeated opener replaces
a valid user turn.

### M4 — Prove repeated turns and recovery on the robot

**Status: not implemented/verified as a complete loop.**

Tasks:

1. Coordinate listening, transcription, response generation, TTS and return to
   listening with per-device session state. Reconcile the separate brain
   session and STT phase. Entry/exit phase changes are synchronized. STT buffers
   have lifecycle handling; remote output now discards stale translation, chat
   and TTS results after session/turn or speech-mode changes. Hardware recovery
   still needs verification.
2. Track each channel's own correlation ID. Do not assume the STT UUID,
   remote-chat event ID and Cloud TTS event ID are always identical.
3. Verify the implemented stale-result guards on hardware after a module exit,
   reconnect, sleep, newer turn or mode toggle. Local concurrency regressions
   now cover remote text/audio and failure silence as well as STT cancellation.
4. Handle empty input, model failure, TTS failure and network delay through
   bounded protocol responses. Avoid both infinite clarification loops and
   silent hangs. Observe firmware timing rather than assuming publication
   means playback has completed.
5. Log concise transition, correlation, language, duration and response-status
   evidence. Distinguish requested text, synthesis text, published audio and
   operator-confirmed playback. Do not log raw audio or credentials.
6. Add regressions for actual failed runs, then perform the completion tests
   listed above across reboot/reconnect and both languages.

**Acceptance:** the operator completes the multi-turn tests without firmware
English taking over, STT UUID timeouts, stuck MarkingUp, unexpected sleep or
manual resets between normal turns.

## Implementation and test map

| Files | Planned work |
| --- | --- |
| [remote_chat.py](../src/services/remote_chat.py) | Investigate entry/exit, preserve real user turns, language selection, history and stale-response handling. |
| [config.py](../src/config.py) | Keep proven defaults; introduce only settings required by verified routing, audio and language behavior. |
| [stt_request.py](../src/services/stt_request.py) | Frame handling, bounded audio accumulation, real transcription, FINAL/error handling and lifecycle cleanup. |
| [stt/openai_stt.py](../src/services/stt/openai_stt.py) | Reuse transcription; correct language reporting and evaluate kk/ru recognition. |
| [language.py](../src/services/language.py), [llm/openai_chat.py](../src/services/llm/openai_chat.py) | Bilingual output policy and meaningful localized responses. |
| [cloud_tts_request.py](../src/services/cloud_tts_request.py) | Preserve working response path; align language guard with each intended reply. |
| [speak_service.py](../src/services/speak_service.py), [marks.py](../src/services/marks.py), [audio.py](../src/services/audio.py) | Reuse proven synthesis/PCM/marks; change only for a demonstrated defect. |
| [openai_client.py](../src/services/openai_client.py) | Audit whole-operation timeout/retry behavior when wiring recognition. |
| [tests/test_remote_chat.py](../tests/test_remote_chat.py), [tests/test_chat_language.py](../tests/test_chat_language.py) | Entry/exit, first real reprompt, bilingual history, language guard and stale turns. |
| [tests/test_stt_request.py](../tests/test_stt_request.py) | Real audio assembly, 16 kHz WAV, per-device UUIDs, empty/error/duplicate END, timeout and reconnect. Replace the temporary transcription-unavailable FINAL with actual recognition. |
| [tests/test_cloud_tts_request.py](../tests/test_cloud_tts_request.py) | Preserve original correlation, command marks, mouth marks, PCM and single-response failure handling. |
| [tests/test_remote_chat_handoff.py](../tests/test_remote_chat_handoff.py) | Module exit/re-entry lifecycle; add a later full-turn capture. |

Transport, bootstrap, pairing and puppet code already exist. Reuse them rather
than creating a second MQTT service or new HTTP conversation endpoints.

## Verification procedure

Keep the existing API key in `.env`. Baseline autonomous settings:

```dotenv
PUPPET_MODE_AUTO_ENABLE=false
CLOUD_TTS_AUTO_ENABLE=true
CLOUD_TTS_AUDIO_RAW_PCM=true
CLOUD_TTS_EMIT_MARKS=true
CLOUD_TTS_REQUEST_SOURCE=robot
DEFAULT_TTS_LANGUAGE=kk
CLOUD_TTS_TRANSLATION_ENABLED=true
OPENAI_TRANSLATION_TIMEOUT_S=4
OPENAI_STT_TIMEOUT_S=6
```

For a fixed Russian translation target, use `DEFAULT_TTS_LANGUAGE=ru`, then
restart. Automatic switching is M3, not an existing
environment option.

Run relevant mocked tests during implementation, then the repository suite:

```bash
mise test
mise start
```

Preserve each hardware capture before log rotation. Reboot Moxie for an entry
experiment; confirm `cloud_tts=1 moxie_mode=None`, wake it, then test the current
milestone. Keep it on a stable surface so pickup responses do not obscure the
conversation transition. The operator performs the physical listening tests.

Capture this evidence for every completed conversation turn:

| Stage | Required evidence |
| --- | --- |
| Entry | Actual `OPENMOXIE_CHAT` router request and session language, beyond SCHEDULED/notify |
| Listen | Device/utterance UUID, VAD completion, nonempty correctly formatted microphone audio |
| Recognition | Real transcript, language decision and processing duration; one matching FINAL |
| Brain | Remote turn containing that transcript; relevant localized response with spoken markup |
| Mouth | Robot-originated TTS request; same ID/chunk on the audio response, correct synthesis text and marks |
| Playback/repeat | Operator hears the reply and observes mouth movement; a new listen/utterance follows |

Failures should retain enough context to identify which stage stopped. A
`spoke` log means audio was published, not that the firmware played it.
A canned wake trigger, scheduled activity, or overridden local English line
does not count as a real conversation turn.

## References and handoff

- [AGENTS.md](AGENTS.md): architecture, protocol invariants and current translation status.
- [Puppet analysis](SPEAK_PUPPET_ANALYSIS.md): first-line success, subsequent-line
  problem and identity-update crash history.
- [README](../README.md): setup and operator commands.

The root [prompt.md](../prompt.md) preserves the September 28 evening diagnosis
and the implementation checklist. The September 29
[localization repair report](LOCALIZATION_FIXES.md) records current behavior,
322 passing tests, defaults, retained firmware limitations and operator checks.

Next: validate the English input bridge with real Kazakh/Russian questions,
then prove sustained turns and recovery. Automatic output-language switching
remains a separate policy change after fixed-language sessions work.
