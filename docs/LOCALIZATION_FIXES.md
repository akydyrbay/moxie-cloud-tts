# Autonomous localization repair — 2026-09-29

The September 28 capture contains 22 successful chat transcripts: 13 Cyrillic-only
inputs have no matching router continue; nine English-containing inputs do.
One of the latter is the recognizer's own prompt echoed as speech. The operator
has confirmed Kazakh replies to English input. This identifies the input-to-router
boundary as the next hardware experiment, independently of successful Kazakh TTS.

The server changes below are implemented. Mocked tests verify their behavior;
they cannot establish how closed-source firmware will process the new FINAL.

## Implemented pipeline

1. Accumulate 16 kHz mono PCM16 under the robot's listen UUID.
2. Transcribe with the existing `gpt-4o-transcribe`, no imperative prompt, auto
   language unless `STT_INPUT_LANGUAGE` explicitly selects kk/ru/en.
3. Reject empty/near-zero audio, the captured instruction echo, and grossly
   implausible transcript length. Valid short answers are retained. RMS and
   active-sample duration are diagnostic; no general noise classifier is claimed.
4. Translate non-ASCII-language input, including mixed kk/ru/English, faithfully
   into English. ASCII English/numbers pass through. The input translator has
   its own prompt and validator; it never answers the human's question.
5. Keep the original text server-side and send the English text in FINAL with
   the same UUID/timestamps. A bounded 256-entry, 120-second store correlates
   device/UUID; reset/exit/mode revision invalidates old entries and workers.
6. Wait for an actual OPENMOXIE_CHAT request. Recover original text by exact
   event ID for GPT/history, preserving the received `input_speech` protocol echo.
   A correlation miss uses that request's speech, never another recent transcript.
7. Generate in the configured session language, kk by default. Require complete
   model output and enforce at most three sentences, 35 words and one question.
   Overlong output is shortened at sentence boundaries, or gets a localized
   generation-error line if no usable short sentence exists. Numeric-only answers
   receive a localized prefix to give synthesis a language cue.
8. Publish remote text, then source-1 REMOTECHAT PCM on the same event ID/chunk 0.
   Native CloudTTSRequest responses retain source 0 and their original chunks.

Automatic output-language switching is still a separate milestone. For this
repair, kk/ru **input** works through the same bridge while **output** stays at
`DEFAULT_TTS_LANGUAGE`. Use `ru` and restart for a fixed Russian reply session.

## Timing and ownership

| Setting | Default | Scope |
| --- | --- | --- |
| `STT_ENGLISH_BRIDGE_ENABLED` | true | New input compatibility path; false provides a controlled original-text A/B comparison |
| `STT_INPUT_LANGUAGE` | auto | Recognition hint only; auto, kk, ru or en |
| `STT_BRIDGE_METADATA_ENABLED` | false | Optional `language`/`original_*` protobuf fields; firmware semantics remain unverified |
| `OPENAI_STT_TIMEOUT_S` | 6 s | Total END processing, recognition and input translation |
| `OPENAI_INPUT_TRANSLATION_TIMEOUT_S` | 2 s | Further limited by the remaining STT budget |
| `OPENAI_CHAT_TIMEOUT_S` | 4 s | Chat generation |
| `OPENAI_TRANSLATION_TIMEOUT_S` | 4 s | Outgoing translation / one wrong-language rewrite |
| `OPENAI_TTS_TIMEOUT_S` | 8 s | Full synthesis/body read as observed by its async caller |
| `REMOTE_REPLY_TIMEOUT_S` | 12 s | Combined reply stages; also bounds native translation plus synthesis |
| `OPENAI_CHAT_MAX_TOKENS` | 256 | Model output cap, followed by sentence/word validation |

These are engineering limits, not measured firmware tolerances. Relevant provider
calls use zero SDK retries. Four shared provider workers are admitted without an
unbounded queue. A timed-out/cancelled synchronous HTTP call holds its worker slot
until it actually finishes; excess work fails promptly. Such workers only return
data. The asyncio owner alone publishes, so late success/failure cannot send a
second packet. A stale remote turn publishes neither audio nor fallback silence.

The STT metadata flag defaults off deliberately: this first experiment changes
the robot's transcript text while preserving the previously working metadata
defaults. Server-side original-text recovery does not depend on firmware returning
`original_speech`. Enabling the flag sets wire language en and supplies original
text plus an input language only when explicitly known. Test it separately.

## Other corrected behavior

- Valid Kazakh containing `XIX`, short shared-Cyrillic words such as `Алма`, and
  occasional Latin names/acronyms no longer fail solely because of their script.
  This remains a lightweight heuristic, not a complete language classifier.
  Clear wrong-language output gets one bounded rewrite or a generation-error line.
- Empty reprompts repeat the last question or give a short clarification. At most
  two consecutive empty reprompts are voiced; further ones receive correlated
  silence until real input resets the counter. No robot apology becomes user input.
- History is recorded per event in turn order. Known failed/cancelled/pending audio
  is excluded from assistant context, and repeated notifications do not duplicate
  input. Published audio is still not proof that the whole line was heard.
- XML special characters in spoken text are escaped. Recognized action tags remain
  separate from plain text; actions attached to omitted text are not executed.
- Source enum 0 is preserved explicitly. HTTP 200 headers do not count as success
  if reading the PCM body fails.
- Robot status logs retain relevant language/audio/state fields without identity
  fields or arbitrary payload dumps. Noisy HTTP transport debug logs are muted.
  Stage logs carry UUID, language roles, bounded transcript previews and outcomes.

## Verification

`mise test`: **322 passed** (mocked MQTT and OpenAI).

New suites cover input translation, recognition/translation deadlines, rejected
ASR, correlation isolation/expiry, mode resets, actual language-guard regressions,
short answers, invalid model completions, XML, empty reprompts, history deduplication,
notify before synthesis failure, timeout followed by late success/failure, provider
capacity, total reply deadlines and source enum zero. Existing wake-up, native TTS,
puppet, raw PCM/marks and stale-output regressions remain passing.

`tests/fixtures/localization_2026_09_28.json` preserves the observed 13/9 routing
split and exact rejected answer/prompt echo. Its snapshot hash is
`c023e0f4a9e60ccc245d70322cf229b26c02f637990f4ed09297ca98968f63fb`.
Tests explicitly label their post-fix router requests as synthetic. No live provider
call, robot restart or physical acceptance test was performed for this repair.

The configured models were retained. Completion limits and result status checks
follow the [official Chat Completions API](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create).
Recognition hints remain separate from output language; see the
[official transcription API](https://developers.openai.com/api/reference/python/resources/audio/subresources/transcriptions/methods/create).

## Operator acceptance

Restart the service with the existing `mise start` workflow. Keep puppet off and
the default bridge enabled. Wake Moxie, wait for its opener, and compare:

1. “What is your name?” as an English control.
2. “Сенің атың кім?” and “Жемістер туралы сөйлесейік.”
3. “Как тебя зовут?” while output remains kk, then repeat after selecting ru.

For each utterance, the expected chain is:

```text
STT END uuid=U phase=chat
STT validated/bridge uuid=U ... original='Сенің атың кім?' wire='What is your name?'
STT FINAL uuid=U speech='What is your name?'
Rx remote-chat command='continue' event_id='U'
remote-chat input event_id=U original_recovered=True output_language=kk
remote-chat audio event_id=U source=REMOTECHAT
spoke event_id=U ...
remote-chat completed event_id=U audio_status=published elapsed_ms=...
```

Record whether the corresponding answer was actually heard. Repeat at least ten
sequential turns, then silence, short answers and a reconnect. Confirm no prompt
echo, valid-Kazakh rejection, old response after reset, or 30-second provider wait.
If the English FINAL still produces no router turn, preserve both language traces
and compare with bridge disabled. Do not fabricate a remote turn as a workaround.

## Firmware/protocol findings still requiring evidence

The local reference checkout was searched for `license`, `remote_lines`, the warm-up
event, duplicate asset names, missing animation and BugID 17. Its `moxie_server.py`
lines 194–205 replies to `license` only when real Google speech credentials exist;
otherwise it sends no response. There is no verified empty-license contract here.
No `remote_lines` implementation or warm-up bypass contract was found. The service
now reports unsupported-query gaps at most once per device/query per minute,
without invented credentials or payload schemas.

`CereprocWarmup-ignore-me` still receives the normal bounded translation/TTS handling
and correlated silence on failure, preserving command marks. No unverified bypass
was added. Duplicate bundle assets, missing `I_001_01_Idles_Sleep` and BugID 17 remain
firmware/asset findings: the service has neither the relevant asset manifests nor
a native stack trace establishing a repair. Preserve their context if they recur.
Inherited firmware language properties were not changed. Full conversation success
and disappearance of the forced-IDLE warning require the new hardware run.
