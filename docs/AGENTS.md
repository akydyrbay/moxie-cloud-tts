# Development guide — current autonomous pipeline

Updated: **2026-10-13**. This document describes the current implementation.
Use [NATIVE_CONTENT_POLICY.md](NATIVE_CONTENT_POLICY.md) for the log evidence and
[SPEAK_LIVE_PLAN.md](SPEAK_LIVE_PLAN.md) for acceptance criteria and remaining work.

## Scope and ownership

This is a replacement cloud backend using Moxie's MQTT/protobuf protocol.
FastAPI is the operator/configuration API; it does not transport autonomous
microphone or speech audio. Device IDs are discovered from MQTT, not hardcoded.

There are two autonomous dialogue owners:

| Robot module/state | Server dialogue behavior | STT behavior |
| --- | --- | --- |
| WAKEUP / ENROLLCONVO | FALLBACK preserves native entrance actions; native lines are localized | `wake-gate`: existing END-of-speech shim returns `Moxie, let's go` |
| OPENMOXIE_CHAT/default or /short | GPT supplies short replies with GLOBAL_RESPONSE | `chat`: real recognition and English input bridge; configured wake-word rule applies |
| EXIT / JOKES / STORY and other non-boundary onboard modules | Native activity controls questions, choices, animations and progression; server returns FALLBACK without conversational GPT | `native`: real recognition and English bridge; short answers need no wake word |
| WELCOME / SESSIONDISMOUNT / SLEEP / TELEHEALTH, or missing module | Session boundary/unknown state; no GPT conversation | `onboard`: recognition disabled; no fabricated transcript |

Puppet/TELEHEALTH is a separate operator path. It is not used to implement
conversation or native activities. Custom kk/ru modules have not been added.

## End-to-end flow

1. Moxie connects to Mosquitto using MQTT/TLS on port 8883. Discovery and
   bootstrap push configuration and subscriptions for CloudTTSRequest and
   zmqSTTRequest. Identity fields are bootstrap-only.
2. The modules handshake advertises OPENMOXIE_CHAT/default and /short.
   DEFAULT_SCHEDULE schedules /default and sets it as `chat_request`.
   Native global commands remain enabled; the schedule does not delete native
   stories, games or firmware entrance behavior.
3. Native wake-up lines arrive as CloudTTSRequest. Their English text is
   localized to DEFAULT_TTS_LANGUAGE, synthesized, and returned on the same
   event/chunk with source ROBOT. Original command marks remain intact.
4. A real OPENMOXIE_CHAT `start`, `prompt`, `continue` or `reprompt` establishes
   free conversation. The opener is English source translated for this live
   router turn; subsequent replies are generated directly in the configured
   language. Native wake-up speech is not replaced by a fixed opener.
5. Microphone PCM is accumulated at 16 kHz mono PCM16. OpenAI recognizes the
   child, and a separate translation produces faithful English for the robot's
   native input matcher. The original kk/ru transcript is retained by device
   and UUID for GPT. STT responses contain human input, never assistant speech.
6. During free chat, GPT output is sent in RemoteChatResponse with normal
   GLOBAL_RESPONSE priority. REMOTECHAT audio answers that live event directly:
   this firmware does **not** raise CloudTTSRequest for ordinary remote replies.
7. If native content wins the same turn, its speech notification or speakable
   CloudTTSRequest cancels pending GPT output. The real native speech goes
   through localization/TTS. Follow-up native turns receive FALLBACK, not a
   competing GPT answer. A story request can first enter EXIT to ask for
   confirmation; the child must hear and answer that native question.
8. Native activities retain their own sequence. Their kk/ru answers are bridged
   into English using the activity context. An unrelated question does not
   automatically make GPT speak over an active story. Native completion/exit
   must produce a real OPENMOXIE_CHAT turn before free chat resumes.
9. Reset/reconnect, true session boundaries and speech-mode revisions discard
   obsolete remote output and microphone work. Chat/native handoffs preserve
   in-flight recognition and original/wire transcript correlation.

## Arbitration details that must be preserved

Remote notification ordering is not fixed. The October 12 prompt `f439b0c9`
published remote audio at 18:37:44 and received REMOTE_RESPONSE notify at
18:37:45. Another turn notified before audio. Requiring notify before audio
would risk silence. `REMOTE_SELECTION_GRACE_S=0.15` admits immediate native
selection before synthesis; afterwards a live remote turn proceeds, checking
for native takeover through synthesis and immediately before publication.

`notify_source=REMOTE_RESPONSE`, or legacy `response_chunks`, records remote
selection. A native speech notification or speakable native TTS request records
native selection for that exact event. An old event must not invalidate a newer
chat turn. Selection entries are bounded to 256 with a 120-second lifetime and
speech-mode revision checks.

`[animation:curious]`, `[animation:thinking]`, `[silent:reprompt]` and empty
notifications are interim signals, not native speech winners. Command-only
CloudTTSRequest receives its normal silent response with command marks but does
not cancel GPT. Native dialogue, including later story chunks, is never muted
or replaced with GPT. The old REMOTE_CHAT_SUPPRESS_NATIVE_CONTENT setting and
ReplyAudio replacement path are removed.

A native module's remote fallback normally loses to local content. Its audio
is prepared with the same grace and native-takeover guards, correlated as
REMOTECHAT; it is a canned localized clarification, not conversational GPT.
It also does not require an explicit selection notification. Duplicate native
router requests are recognized so they cannot replay the fallback audio.

The firmware performs final response arbitration. The server cannot retract
already played audio if a subsequent activity interrupts it. Do not describe
mocked publication checks as proof of physical playback or perfect arbitration.

## Language and localization

- DEFAULT_TTS_LANGUAGE chooses output `kk` or `ru`; the checked-in default is
  `ru`. The operator can set `kk` in `.env`. Input language does not currently
  switch output language automatically.
- STT_INPUT_LANGUAGE is a separate recognition hint (`auto`, `kk`, `ru`, `en`).
  STT_ENGLISH_BRIDGE_ENABLED defaults true; original text remains server-side.
  Optional protobuf original/language metadata defaults off.
- GPT free-chat replies are limited to three short sentences and 35 words.
  Silence/ignored speech follows bounded reprompt behavior, not invented input.
- Native English/Latin speech is localized before TTS. Already Cyrillic native
  lines bypass this stage; puppet uses its explicit text/language behavior.
- Native translation receives module/content, observed prompt and up to four
  recent successfully localized source/target lines. System instructions ask
  for natural child-friendly kk/ru while preserving questions, options and their
  order, negation, numbers, names, story facts and setup/punchline. Native story
  chunks are not truncated to the free-chat limit.
- Input translation uses the activity context captured at utterance END, before
  waiting in the recognition queue. It preserves yes/no and first/second
  choices, and maps explicitly mentioned localized titles to their source
  names. It must not infer agreement, invent choices or answer for the child.
- Context is bounded to 64 devices and cleared on activity/reset/mode changes.
  Successful text translations have a 256-entry cache including context,
  language and model. Never cache or reuse another request's audio IDs.
- Translation quality, especially English wordplay, still needs live review.
  Prompts do not guarantee a natural adaptation of every native script.

## Time and resource limits

| Stage/resource | Current default or bound |
| --- | --- |
| Recognition plus input bridge | 6 seconds from END, including queue wait; configurable maximum 8 |
| Input translation | 2 seconds, also limited by remaining STT budget |
| Output translation | 4 seconds |
| GPT generation | 4 seconds |
| TTS synthesis | 8 seconds |
| Combined remote reply/native localization + audio | 12 seconds |
| Immediate native selection grace | 0.15 seconds; never an indefinite notify gate |
| Recorded PCM | 30 seconds per UUID; streams without END expire after 30 seconds |
| Pending listens | 6 per device, at most 2 still recording; 16 globally |
| Recognition | One recognition/bridge coroutine per device; queued work shares the END deadline |
| Provider workers | 4 globally; a timed-out worker retains its slot until it actually finishes |
| Completed STT UUIDs | 1024 bounded tombstones, 120-second lifetime |
| Original/wire transcripts | 256 entries, 120-second lifetime, revision-aware |

The October 12 capacity error came from a third listen arriving while two
finished recordings were still processing. The bounded recognition queue
addresses that pattern. Error 66 remains the protocol response for genuine
provider failures, deadline expiry, invalid audio, disabled recognition and
capacity overflow; do not hide those failures or promise that they cannot occur.

## Protocol and code map

The robot publishes to `/devices/{id}/events/#` and `/devices/{id}/state`.
The service also subscribes to `$SYS/broker/log/#` for connection lifecycle.
Do not subscribe to `#`, which echoes server publications.

| Channel | Robot → server | Server → robot |
| --- | --- | --- |
| Microphone | framed zmqSTTRequest on events/zmq | framed zmqSTTResponse on commands/zmq, exact UUID |
| Native speech | framed CloudTTSRequest on events/zmq | CloudTTSResponse, exact event/chunk, ROBOT source |
| Remote dialogue | JSON events/remote-chat | JSON commands/remote_chat plus correlated REMOTECHAT audio |
| Configuration/schedule | state, modules/activity queries | config, query_result, ProtoSubscribe |

Frames use `fully.qualified.TypeName:` followed by protobuf serialization.

| File | Responsibility |
| --- | --- |
| src/main.py | FastAPI lifecycle, handler registration, startup/shutdown |
| src/services/mqtt_service.py | MQTT dispatch, protobuf framing, broker resets |
| src/services/devices.py | Discovery/bootstrap registry |
| src/services/device_state.py | Cloud-TTS/puppet overlay and speech revision |
| src/services/remote_chat.py | Handshake, schedule, module routing, response ownership, GPT turns |
| src/services/stt_request.py | PCM accumulation, phases, bounded recognition queue, correlated FINAL |
| src/services/stt/input_translation.py | Faithful human input → English |
| src/services/stt/transcripts.py | Original/wire text correlation |
| src/services/cloud_tts_request.py | Native speech → localization → correlated audio |
| src/services/localization_context.py | Bounded activity prompts and source/localized history |
| src/services/translation.py | Context-aware native localization and successful-text cache |
| src/services/llm/openai_chat.py | GPT free-chat generation |
| src/services/llm/reply_text.py | Spoken reply length bounds |
| src/services/speak_service.py | Shared TTS, stale remote-output guard, correlated failure silence |
| src/services/marks.py | Estimated word/viseme timing and native command marks |
| src/services/provider_work.py | Bounded synchronous provider work |
| src/services/telehealth.py | Separate puppet protocol; leave unchanged for this task |
| src/config.py | Settings, module advertisement, schedule, source openers |

## Hard rules

1. Keep puppet sequencing and `/tts/speak` separate. Do not add an HTTP speak or
   transcription endpoint for autonomous conversation.
2. Keep PUPPET_MODE_AUTO_ENABLE false for autonomous testing. Never set
   brain_entrances_available to zero to suppress native content.
3. Preserve native actions and command marks. Localization changes speech, not
   native branching, choices or animation assets.
4. Echo event_id/chunk_num/UUID verbatim, including a valid empty puppet ID.
   Every native CloudTTSRequest gets its correlated audio or failure silence.
5. Remote audio must belong to a live router turn. Never invent an unsolicited
   audio waiter, fake remote prompt or assistant text in STT speech.
6. Do not gate remote audio on a notification that may arrive after playback
   starts. Do not treat interim animation/silence signals as native speech.
7. Keep response actions speak-first: launch/sleep as a primary action can skip
   the audio. Preserve WAKEUP/ENROLLCONVO FALLBACK entrance handling.
8. Never re-push child_pii/pairing_status on live config toggles. Full identity
   belongs only in bootstrap.
9. Never request OpenAI WAV output; use PCM and truthful format metadata.
   Word/viseme/command timing is estimated, not forced phoneme alignment.
10. Do not hand-edit generated files in src/protos. Do not log credentials.
11. Preserve bounds, deadlines, reset guards and cross-device isolation.
    Native speech suppression must not be reintroduced as a side effect.
12. SUPPORTED_LANGUAGES remains kk/ru. English compatibility input is not an
    additional assistant output language.

## Verification

Run `DEFAULT_TTS_LANGUAGE=kk mise test`, or
`DEFAULT_TTS_LANGUAGE=kk .venv/bin/python -m pytest tests/ -q` with dependencies
already installed. The override serves historical Kazakh test fixtures and does
not alter runtime configuration. Native handoff tests explicitly exercise ru/kk.
Run `git diff --check` as well.

Providers and MQTT are mocked. Tests cover the actual October 12 event IDs and
request order, pending GPT cancellation, late notify, interim animation signals,
native STT/choices, burst capacity, context across queues and module transitions,
reset, timeout, duplicate delivery and separate puppet behavior. Tests must not
require a robot or modify captured logs. Hardware acceptance remains necessary;
use the checklist in NATIVE_CONTENT_POLICY.md and keep fresh logs for diagnosis.
