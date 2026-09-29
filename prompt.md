# Moxie kk/ru conversation: diagnosis and implementation plan

Updated: 2026-09-29. The September 28 diagnosis is retained below; implementation status follows.

## 1. Task and current conclusion

Implement reliable autonomous Kazakh/Russian conversation while preserving the working MQTT, wake-up, native-line translation and remote-chat audio paths.

The backend repair is implemented and covered by **322 passing tests**. See [implementation, settings and robot checks](docs/LOCALIZATION_FIXES.md). Hardware acceptance remains necessary; neither translated output nor successful transcription alone proves a complete conversation loop.

### Implementation status — September 29

- Steps 1–4: captured-log fixtures, sanitized correlation logs, prompt-echo rejection, bounded English input translation and original-text recovery are implemented. The bridge defaults on; optional protobuf language/original fields default off until firmware behavior is verified.
- Steps 5–7: provider deadlines and capacity, one publication owner, source-enum correction, output validation, a hard three-sentence/35-word cap, escaped markup, bounded empty reprompts and event-based history are implemented. Reply language remains fixed kk or explicitly configured ru, as planned.
- Step 8: reference contracts were investigated. Unsupported queries are rate-limited compatibility diagnostics. No verified remote_lines or warm-up bypass contract was found; native assets/BugID 17 require firmware evidence. These are documented, not claimed fixed.
- Automated regression and full-suite verification passed. Real kk/ru FINAL-to-router acceptance and sustained audible conversation still require the operator run in section 7.

Sections 2–4 describe the pre-repair capture and code findings; section 5 retains the implementation checklist for review.

The latest capture establishes a strong input-language boundary:

- OpenAI transcription produces Kazakh and Russian text successfully.
- The service publishes that original text in `zmqSTTResponse.speech`.
- None of the 13 Cyrillic-only chat transcripts has a matching remote-chat `continue`.
- All 9 transcripts containing Latin/English text have a matching `continue` with the same event ID. One of those 9 is an ASR prompt echo, not genuine evidence of a correctly recognized English utterance.
- Once a real turn reaches the server, the LLM is explicitly instructed to answer in Kazakh. The operator reports hearing Kazakh responses.

**Primary diagnosis:** the unadapted non-English transcript is lost between the STT response and the robot's remote-chat routing. An English-language normalization/dialogue gate inside the robot is the leading explanation. The exact closed-source firmware component cannot be identified from this capture alone. Mixed utterances containing Cyrillic names pass, so this is not evidence of a blanket UTF-8/Cyrillic transport failure.

The first repair to validate is a faithful kk/ru-to-English **input compatibility translation**, while retaining the original transcript for the server's LLM and history. Do not change the recognizer to English or the robot's spoken output to English.

There are also independently confirmed ASR hallucination, output-validation, empty-reprompt and TTS-latency failures. Fixing only the input bridge will not resolve every observed silence or inappropriate apology.

## 2. Evidence snapshot and confidence

Analyzed file: `log/debug.log`, 2,792 lines, logged interval 2026-09-28 19:18:24–19:34:23.
SHA-256: `c023e0f4a9e60ccc245d70322cf229b26c02f637990f4ed09297ca98968f63fb`.

Firmware reported in this capture: `24.10.803`. Line references below apply to this snapshot, not a subsequently appended or rotated file.

There are 22 successful chat STT FINALs, with handler elapsed times of 945–1,922 ms, zero STT FINAL errors, and no delayed-STT SpeechFuser warning in this run. This differs from older captures; do not carry their failure diagnosis forward without checking dates.

### Input/output correlation

| Log lines | Human transcript | STT UUID / router event ID | Observed result |
| --- | --- | --- | --- |
| 360 | Жемістер туралы сөйлесейік. | `75004190-7ed0-4f29-9036-7cb3ebb8f69b` | Successful STT; no matching continue |
| 447 | Маған қазақша сұрақ қойшы. | `979d263c-050b-40c1-9b52-fc2b4cba3b00` | Successful STT; no matching continue |
| 544–545 | Ask me some questions in Kazakh language | `111a19ab-05a8-4bec-8184-6b2793df1275` | Immediate matching continue; Kazakh answer generated |
| 806 | Привет. | `8b8beb9f-d97d-49b0-a985-f965db1136f4` | Russian STT also fails to become a continue |
| 892–916 | Tell me something about cars | `ba05582f-4d71-47f9-91a6-e692dc7302fc` | Continue and valid Kazakh answer; answer rejected by language guard |
| 1488 | Сенің атың кім? | `77c664e2-fec5-4c8d-92f7-e35a2a4106dc` | No matching continue |
| 1533–1534 | What's your name? | `5963441e-3da1-4d14-95d8-ecbb1edb5976` | Same question in English reaches continue; later TTS fails |
| 2288–2289 | ASR instruction prompt copied into transcript | `94afdf9d-bea0-4274-862f-60672e488512` | Erroneously accepted as a conversation turn |
| 2408–2409 | Моқси, what will be five plus five? | `9843358a-098a-4a7b-908f-5f1f40c56aa3` | Mixed-script input reaches continue; later TTS fails |

Five separate empty `reprompt` requests occur. They use different IDs from the rejected Kazakh utterances. For example, the empty reprompt at line 366 is `47f3d78c-a089-4231-8c48-6076782a13d1`, not the fruit-question UUID at line 360. Do not count that reprompt as successful delivery of the fruit question.

### What is already working

- Discovery, bootstrap, MQTT subscriptions and module entry work in this run. An actual `OPENMOXIE_CHAT/default` prompt arrives at line 235.
- Native English `CloudTTSRequest` lines are translated and answered with correlated robot-source audio.
- The remote opener is translated to Kazakh at line 268 and voiced using `REMOTECHAT_TTS_REQUEST` at lines 270–307. Its notify references the same event ID at line 310.
- English input reaches the existing remote module and produces Kazakh LLM output. A new fruit bundle is not required to repair basic conversation routing.
- Existing stale-output protection works: line 2303 discards the previous reprompt's audio after a newer turn replaces it. Preserve this protection.
- The log has no evidence of a general MQTT disconnect, publish error, authentication rejection or rate-limit rejection causing the language split.

A server “spoke” log means audio was published, not that the operator heard it. A remote notify can arrive before audio finishes synthesizing, including on turns whose synthesis later fails. Preserve this distinction in diagnostics and acceptance claims.

## 3. Actual pipeline and language ownership

Current failing path:

~~~text
Robot microphone: 16 kHz mono PCM16
  -> cumulative zmqSTTRequest frames / END_OF_SPEECH
  -> OpenAI transcription in the spoken language
  -> FINAL speech="Сенің атың кім?", original UUID
  -> robot input processing
  -> no matching RemoteChatRequest observed
  -> no LLM answer or answer audio for that utterance
~~~

Current successful input path, subject to independent TTS failures:

~~~text
English or English-containing transcript
  -> FINAL with original UUID
  -> OPENMOXIE_CHAT continue with the same event_id
  -> OpenAI chat, configured output language kk
  -> remote-chat GLOBAL_RESPONSE with Kazakh markup
  -> CloudTTSResponse source=REMOTECHAT, same router event_id, chunk=0
  -> robot audio handling
~~~

Treat these as three independent language settings:

| Concern | Required policy |
| --- | --- |
| Recognition | Preserve the actually spoken kk/ru/en; auto by default, optional explicit input hint |
| Robot-compatible STT text | Faithful English translation where needed, if the bridge experiment confirms it |
| Assistant text and TTS | Explicit session output language: kk by default; ru when deliberately configured |

Use ISO code `kk`, not `kz`. Kazakhstan's country abbreviation is not the STT/TTS language code.

Current local configuration is consistent with Kazakh output:

- `OPENAI_STT_MODEL=gpt-4o-transcribe`; `OPENAI_CHAT_MODEL=gpt-4o-mini`.
- `OPENAI_TTS_MODEL=gpt-4o-mini-tts`; voice `alloy`.
- `DEFAULT_TTS_LANGUAGE=kk`; output `SUPPORTED_LANGUAGES=[ru, kk]`.
- `CLOUD_TTS_TRANSLATION_ENABLED=true`; `PUPPET_MODE_AUTO_ENABLE=false`.
- `OPENAI_REQUEST_TIMEOUT_S=8`, transcription deadline 6 s, outgoing translation deadline 4 s.

Robot status also reports `cloud_translate=en`, `app_language_support=0`, `local_stt=on`, `cloud_tts=1`, `cloud_tts_use_mp3=0`, `cloud_tts_engine=polly` and `rchat_api=10`. Some status previews are truncated.

These are not proof that the server calls Polly or that flipping one firmware flag enables Kazakh. The service actually calls OpenAI. Bootstrap does not explicitly establish most of these inherited firmware properties. The local OpenMoxie reference documents `local_stt=on` for wake phrases; disabling it blindly could break wake-up.

Audit these properties and their protocol meaning, but do not change `cloud_translate`, `app_language_support`, `rchat_api` or local wake recognition based only on their names.

## 4. Confirmed failures and additional structural risks

### A. Non-English STT has no compatibility adapter

Location: `src/services/stt_request.py`, `handle_stt_request` and `_finish`.

The returned transcript is stripped and copied directly into `zmqSTTResponse.speech`. No inbound translation, original-transcript correlation store, or separate robot transport language exists. The response's language/original-language/original-speech fields remain unset.

The protobuf descriptor already exposes `language`, `original_language`, `original_speech`, and alternatives. Their exact interpretation by this firmware is not established. Do not hand-edit generated protobuf files or assume setting those fields alone fixes routing.

Confidence: the missing adaptation and loss of router turns are proven; the proposed English bridge needs on-robot validation. Equal UUID handling, timing and default confidence for English and Kazakh make those shared fields weaker explanations for the language-specific split.

### B. ASR prompt is emitted as fake human speech

Location: `src/services/stt/openai_stt.py`, current transcription prompt.

At line 2288, only 30,720 bytes of PCM, approximately 0.96 seconds, yield:

~~~text
###
The speaker uses Kazakh (қазақ тілі, Қазақстан) or Russian (русский язык). Preserve the spoken language and use Cyrillic.
###
~~~

That is the recognizer's own prompt, surrounded by delimiters. It is forwarded to Moxie and reaches the LLM at line 2289. The generated answer then discusses speaking Kazakh instead of answering a human question. This false turn also replaces the previous pending answer.

The exact prompt match and implausible duration strongly indicate prompt echo/hallucination. Original PCM was not retained for listening, so do not claim every short “Қазақстан” or “қазақ тілі” transcript is also false.

Current audio validation checks empty/all-zero buffers and PCM alignment. It does not reject low-energy nonzero noise, prompt echo or implausible transcript length.

### C. Output-language guard rejects valid Kazakh

Locations: `src/services/language.py:matches_output_language` and `remote_chat.py:_remote_module_response`.

At lines 912–914, the model generates a 632-character Kazakh answer about cars. The guard rejects it because the answer contains Roman numeral `XIX`. It is replaced with “Кешір, мен естімедім. Қайталай аласың?” despite successful recognition and answer generation.

Local reproduction: the logged answer fails validation; replacing `XIX` with `19` passes. The same heuristic also rejects valid short Kazakh such as “Алма.” or “Мен роботпын.” because they contain no Kazakh-exclusive letter and are classified as Russian.

This is not an STT failure. The fallback incorrectly tells the user that the robot did not hear them.

### D. Empty reprompts become invented user messages

Location: `remote_chat.py:_remote_module_response`.

For an empty reprompt with history, the service calls the LLM with `speech or fallback_line(language)`. Thus the robot's own Kazakh apology is supplied as a user utterance.

Line 366 has empty input; line 379 reports responding to 38 characters, matching the fallback sentence. Later empty reprompts generate new questions or repeat lists. A silence/timeout event must not masquerade as recognized human speech.

The existing fix for a first **nonempty** reprompt is correct and must remain.

### E. TTS socket timeouts and retries exceed conversational deadlines

Locations: `openai_client.py`, `synthesis/openai_tts.py`, `speak_service.py`.

The shared client uses an 8-second request timeout but leaves SDK retries enabled. The installed SDK default is two retries. TTS also buffers the full PCM body before publishing. Socket/read timeouts do not impose an 8-second wall-clock limit across retries, queueing and body reads.

Observed failures:

| Router event ID | TTS start / silent fallback | Consequence |
| --- | --- | --- |
| `5c6e3b69-8781-4047-96d7-d98e891e1bea` | 19:29:36 / 19:30:08 | About 32 s waiting, then silence; line 1311 |
| `5963441e-3da1-4d14-95d8-ecbb1edb5976` | 19:30:49 / 19:31:21 | About 32 s waiting for a name answer, then silence; line 1773 |
| `9843358a-098a-4a7b-908f-5f1f40c56aa3` | 19:33:30 / 19:34:09 | About 39 s waiting for an arithmetic answer, then silence; line 2607 |

Line 1935 reports: `Forcing IDLE state, Waiting has gone too long without transitioning.` Its timing is consistent with the preceding long wait; the log does not establish the exact internal timer or exclusive cause.

Even successful audio takes 28,115 ms and 30,890 ms on two turns. Those responses contain roughly 24.9 and 20.4 seconds of audio. The model produces long lists despite the general “short reply” instruction.

There are 15 remote-audio attempts: 11 nonempty published responses, 3 failure silences and 1 stale discard. HTTP 200 headers occur before some body-read failures: HTTP status alone is not a successful synthesis metric.

No evidence identifies whether provider, network or connection behavior is the underlying source of individual read stalls. Retries and missing overall deadlines demonstrably amplify their effect.

### F. Additional code risks, not all proven as runtime failures here

- `llm/openai_chat.py` also inherits shared retries, has no overall conversational deadline or completion-length cap, and assumes a usable first choice without explicit finish/refusal handling.
- `_speech_markup` inserts model text into XML without escaping `&`, `<` and `>`. Plain generated text can corrupt markup. The present capture does not show that happening.
- `_ChatSession.ingest_notify` adds history based on text notifications, which can precede audio publication. Known failed audio may therefore remain in context as if spoken. Repeated notify input entries also lack event-based deduplication; this capture does not expose enough `extra_lines` content to prove duplication.
- The model at one point describes itself as a virtual robot. Tighten the physical-Moxie persona without inventing facts about its location or capabilities.
- Request-source selection uses truthiness in shared speech helpers. `ROBOT_TTS_REQUEST` is enum value 0, so an explicit robot source could be replaced by a non-default configured source. Current configuration is `robot`, so this does not explain this capture; use explicit `is None` selection when hardening the routing contract.
- Current logs truncate key status and request fields, obscure stage ownership, and produce duplicate long exception traces plus HTTP transport headers. A successful publish is not a device playback acknowledgment.

### G. Other messages requiring triage

| Evidence | Meaning / planned handling |
| --- | --- |
| Lines 21, 48: unanswered `license` and `remote_lines` queries | Protocol coverage gaps. Research exact supported reply schema; do not invent responses. Successful English chat shows these are not universally blocking remote chat. |
| Lines 46–103: `CereprocWarmup-ignore-me` translation timeout | Technical warm-up text went through the translation API, exhausted its 4 s deadline, and received correlated silence. Identify this exact warm-up contract before optionally bypassing translation while preserving command marks. |
| Lines 30–35: duplicate bundle assets | Native bundle warnings involving behavior, icons and sound files. Inventory bundle versions/overlaps separately; no demonstrated connection to dropping Kazakh turns. |
| Line 36: missing `I_001_01_Idles_Sleep` animation | Native animation reference mismatch; investigate the corresponding asset/firmware version. |
| Line 124: firmware `BugID: 17` | Insufficient stack/context to diagnose. Preserve surrounding logs; do not claim the language bridge fixes it. |
| Lines 111, 122, 129: command-only silent responses | Expected behavior for markup containing actions but no speech. Preserve command marks; these are not provider failures. |
| `EBUploader [NO UPLOAD]` | Expected absence of the original upload backend; not evidence of the localization failure. |
| Line 2303: stale speech discarded | Correct behavior after a newer turn. Fix false input upstream rather than removing cancellation. |

## 5. Implementation sequence

### Step 1 — Preserve a reproducible baseline and add stage correlation

1. Read `docs/AGENTS.md`, `docs/STT_WIRING.md`, `docs/REMOTE_CHAT_AUDIO.md`, `docs/TTS_TRANSLATION.md` and `docs/SPEAK_LIVE_PLAN.md`. Some older status prose still says remote audio is unconfirmed or implies remote text raises native TTS requests; use the latest capture and operator report above to resolve those contradictions.
2. Extract small sanitized fixtures from this snapshot: Kazakh and English name questions, Russian greeting, fruit request, `XIX` answer, prompt echo, empty reprompt, TTS timeouts and stale discard.
3. Record the real negative behavior: the fixture must not fabricate an observed continue for a dropped Cyrillic transcript. Synthetic post-fix router events belong in separately labeled test scenarios.
4. Add structured stage logs using device ID, session generation, listen UUID/router event ID, phase, input/output language, bytes/duration and monotonic elapsed time. Distinguish ASR returned, transcript validated, bridge translated, FINAL enqueued, router accepted, chat generated, audio published, audio timed out and turn discarded.
5. Log a safe allowlist of actual language/audio firmware properties without the current 800-character truncation. Never log API keys, raw PCM, child identity or full HTTP headers. Reduce noisy transport logger levels and duplicate tracebacks while retaining request IDs and useful failure categories.

Acceptance: a single utterance can be followed through every available boundary without treating server publication or remote text notify as confirmed playback.

### Step 2 — Reject ASR prompt echo before enabling an input bridge

1. Remove the long English imperative from the auto-detection transcription prompt. Start with no prompt; add only a short, justified vocabulary/context hint if recorded evaluation shows a benefit.
2. Keep auto recognition for mixed kk/ru/en. If adding an explicit input-language hint, configure it separately from output language. Do not force `language=en` merely because the robot needs an English transcript.
3. Keep `TranscriptResult.text` as the original recognized text. A missing detected-language label stays unknown; do not label it Kazakh solely because output defaults to kk.
4. Reject normalized copies of the known provider instruction, including surrounding `###`. Add measured PCM energy/voiced-duration diagnostics and conservative checks for implausible transcript length. Treat duration/confidence thresholds as calibrated signals, not universal language rules.
5. Do not blacklist legitimate short words, country names or answers such as “Иә”, “Жоқ”, “Да”, “Нет” and “Алма”. Evaluate suspicious repeated seed words with real audio before filtering them.
6. Invalid/empty recognition must complete the live listen once using the existing error contract: FINAL error 66, empty speech, original UUID. It must not create a chat turn or synthetic wake phrase after chat entry.
7. Optionally evaluate transcription log probabilities as one confidence signal; do not invent calibrated confidence values from them.

API guidance: transcription `language` is an input-language hint, and a transcription `prompt` should match the audio language. The API reference documents optional log probabilities for supported transcription models. Check support for the configured `gpt-4o-transcribe`; do not copy parameters restricted to newer models or migrate models as part of this fix. [Official transcription API reference](https://developers.openai.com/api/reference/python/resources/audio/subresources/transcriptions/methods/create).

Acceptance: the exact prompt-echo fixture never reaches translation, remote chat or TTS; valid short kk/ru/en inputs still pass.

### Step 3 — Add a bounded English input compatibility bridge

Implement this separately from outbound robot-speech translation.

1. Add a configurable bridge mode for controlled A/B testing, with the deployment choice documented explicitly. Use a separate helper/module, for example `src/services/stt/input_translation.py`, or a carefully separated translation interface.
2. After ASR validation, translate non-English kk/ru and mixed-language utterances faithfully into English. Ordinary English can pass through. Do not classify “contains any Latin letter” as “already English”; Kazakh sentences can contain brand names or Latin words.
3. The translation prompt must treat the transcript as data: preserve questions, commands, names, numbers and meaning; never answer the question or add an assistant reply. Do not transliterate Kazakh or prepend an English wake phrase to trick routing.
4. Do not call the existing `translation.translate(text, "en")` unchanged. Its targets are kk/ru and its validator requires Cyrillic and rejects Latin. Keep existing English-to-kk/ru robot-output behavior intact.
5. Use the same listen UUID and audio timestamps. Publish a real FINAL whose `speech` is the English translation. The intended language field is `en`; validate this field's firmware contract independently from the text adaptation.
6. Preserve the original transcript in server state. If verified compatible, also populate `original_speech` and an honestly known `original_language`; do not depend on firmware forwarding these fields.
7. Bound ASR plus translation together by the current listen deadline, initially 6 seconds from processing END, including worker scheduling. Give input translation only the remaining budget, disable SDK retries and fail once if that budget expires. Do not silently create a 6 + 4 second listen deadline.
8. A provider thread may finish after cancellation, but it must never publish. Retain existing listen identity checks, completed UUID tombstones, size limits, expiry and reset behavior.
9. Log original-vs-wire text only in sanitized diagnostic fixtures/previews, with distinct language roles and stage timings.

Expected example:

~~~text
original human text:  Сенің атың кім?
robot FINAL speech:  What is your name?
same listen UUID:    preserved
router event:        real OPENMOXIE_CHAT continue, same UUID
LLM user text:       Сенің атың кім? (recovered from server correlation)
LLM output language: kk
spoken answer:       Менің атым Мокси.
~~~

Acceptance: the same Kazakh question that previously produced no continue now produces a real matched continue on the robot. If it does not, compare text-only and verified metadata variants, capture relevant robot input logs and revisit the gate hypothesis. Do not claim the bridge is guaranteed before this experiment.

### Step 4 — Restore original human language at the remote-chat boundary

1. Store original and wire transcripts before publishing the FINAL, keyed by device, conversation generation and exact listen UUID.
2. Keep the store bounded by count and TTL. Clear it on reconnect, reboot, sleep, module exit and relevant mode changes; prevent old results repopulating a reset session.
3. On a real nonempty router turn, correlate the event ID to that exact listen. The latest run shows matching IDs on all nine continues, but missing/mismatched IDs must be handled explicitly.
4. Give `openai_chat.respond` the original text when correlation succeeds. If it misses, use the actual router speech and log the miss; never use the “latest transcript” from a different listen.
5. Keep the protocol's `input_speech` echo based on the received router request. Original-text recovery is for model context/history and must not rewrite event IDs or required wire echoes.
6. Retain enough correlation data for subsequent notify/history handling without re-processing the same input as a new turn. Deduplicate by event identity, not by text; a person may legitimately repeat the same sentence.
7. Do not attach a dropped Kazakh utterance to an unrelated empty reprompt. Do not invoke a fabricated router turn directly from the STT handler or synthesize remote audio without a live router request.

Acceptance: two robots, overlapping listens, repeated phrases and reconnects never exchange transcripts; English transport text does not leak into the stored original-language conversation when a matching transcript exists.

### Step 5 — Make audio completion timely and exactly once

1. Disable automatic retries for latency-sensitive TTS and chat calls through operation-scoped client options. Preserve the already bounded STT and outgoing translation behavior.
2. Add explicit configurable wall-clock deadlines that cover scheduling, provider work and full audio body reads. Start TTS with an 8-second total budget as a provisional engineering setting, then tune against measured hardware tolerance; it is not an established firmware timeout.
3. Give the complete reply path an explicit budget too. Independent translation/chat/TTS limits must not accidentally sum to another 30-second robot wait.
4. Separate pure synthesis from publishing. One owner on the asyncio loop must choose exactly one terminal outcome for a live request: audio or correlated silence. A stale request publishes neither.
5. Preserve `is_current`, session/turn identity and `speech_revision` checks immediately before publication. A timeout followed by a late provider success/failure must not produce a second packet.
6. Do not merely wrap the current publish-capable `speak` coroutine in a timeout while allowing a background copy to continue publishing. Do not catch the same exception at multiple layers and publish duplicate fallback buffers.
7. Bound provider concurrency/queue wait so abandoned synchronous workers cannot exhaust capacity indefinitely. Provider workers may return data but never directly publish.
8. Preserve native `ROBOT_TTS_REQUEST` IDs/chunks and remote `REMOTECHAT_TTS_REQUEST` router IDs/chunk 0. Select enum defaults with an explicit None check, not truthiness.
9. Keep raw mono 24 kHz PCM16 output and required command/viseme marks. Do not invent multi-chunk firmware streaming, switch to MP3/WAV or route this through puppet.
10. Record provider failure separately from the final audio outcome. A short silent fallback releases the robot's wait but does not count as a spoken answer.

Acceptance: controlled timeout tests prove a bounded response, no duplicate/late publication and no leak across mode/session changes. Hardware retries no longer spend 30–39 seconds waiting before silence.

### Step 6 — Fix output validation, reply length and markup

1. Replace the “any Latin character is invalid” and “must contain a Kazakh-only letter” rules. The selected target language is authoritative when text is ambiguous.
2. Accept valid short Kazakh and Russian, numbers, Roman numerals and legitimate names. Normalize names/numerals for speech where useful without discarding the entire answer.
3. Keep a meaningful check against genuinely wrong-language sentences. If a correction is needed, allow at most one bounded rewrite within the remaining reply budget; otherwise return an appropriate localized generation-error line.
4. Do not report “I did not hear you” for an output-language validation or synthesis failure.
5. Every conversational GPT reply must use at most **3 short sentences**, preferably 1–2, with at most one short question. Use simple, familiar words suitable for a child; explain one idea at a time without technical jargon, lists or Markdown. Add a model-appropriate completion cap and validate the result, including empty choices, refusal and incomplete generation. Prefer complete short sentences rather than arbitrary character truncation.
6. Limit ordinary conversational replies to **35 words total**. Avoid the observed 250–630 character lists. As of 2026-09-29, the live chat prompt includes these instructions for both kk and ru, including when OPENAI_SYSTEM_PROMPT is overridden. The remote reply validator also enforces sentence/word limits before synthesis, keeping whole sentences or using a localized generation-error line. Outgoing native-line translation remains faithful to the robot's source text.
7. Make the persona a physical Moxie robot conversing with its user, with no invented location or unsupported capabilities.
8. Escape plain text before embedding it in `<usel>`. Continue handling intentional action tags and command marks through their existing dedicated paths.

Acceptance: the logged cars answer is not replaced with a hearing apology because of `XIX`; “Алма.” passes for kk; real English output is still corrected/rejected under kk policy; XML remains valid for special characters.

### Step 7 — Treat silence, history and language switching explicitly

1. An empty reprompt is a timeout/no-input event. Return a short localized clarification or repeat the last question using explicit state. Do not supply the robot's apology as a new user message to the LLM.
2. Bound repeated empty reprompts and clear that counter on genuine input. Preserve nonempty reprompt processing, including before any history notify.
3. Distinguish response text accepted by firmware, audio published and confirmed playback when available. Exclude or annotate known failed/cancelled output rather than storing it as certainly heard.
4. Deduplicate repeated notify input/history using source event ID and chunk/context identity. Preserve ordering when old notifications arrive after a newer turn.
5. Keep default output kk for the first repair. Verify ru through an explicit session/configuration choice. Automatic switching is a separate policy and must use original human input or an explicit request, never the English compatibility transcript.
6. For short ambiguous human text, keep the established session language rather than treating the absence of Kazakh-specific letters as proof of Russian.

Acceptance: silence does not invent human speech, multiple reprompts do not produce five-question monologues, and the bridge does not force English replies or silently change the output language.

### Step 8 — Resolve secondary protocol and firmware findings

1. Inspect the local reference checkout at `/Users/bake.72/Desktop/hri-lab/openmoxie` for documented `license` and `remote_lines` contracts. The initial source search found no usable `remote_lines` implementation; investigate protocol/version support rather than guessing.
2. Implement a supported empty/unavailable response only when its schema and semantics are established. Do not fabricate licenses or cloud credentials. If unsupported, log an explicit, rate-limited compatibility gap.
3. Confirm the exact technical warm-up event contract. If safe, answer that event locally with correlated silence and its required command marks rather than translating warm-up prose. Do not generalize this bypass to normal English content.
4. Collect asset manifest/version details for duplicate assets and the missing animation. Keep these firmware/bundle issues separate from server localization until evidence connects them.
5. Preserve context around BugID 17 and any future forced-IDLE event; determine whether it remains after reply deadlines are enforced.
6. Reconcile stale docs with the verified two-source audio design and final language policy. Do not claim every firmware warning is fixed by backend changes.

## 6. File map for implementation

| File / area | Required work |
| --- | --- |
| `src/config.py` | Separate input hint, bridge policy, reply output language and stage/total deadlines; document defaults |
| `src/services/stt/openai_stt.py` | Remove problematic prompt; preserve original transcription; optional verified confidence signals |
| `src/services/stt_request.py` | Validate ASR; bounded input bridge; same-UUID FINAL; no late/duplicate completion |
| New input translation/correlation helper(s) | Direction-specific English translation; bounded per-device/session transcript mapping |
| `src/services/remote_chat.py` | Exact original-text recovery, empty-reprompt handling, history outcomes/deduplication, escaped markup |
| `src/services/language.py` | Handle ambiguous Cyrillic and legitimate mixed tokens without false language rejection |
| `src/services/llm/openai_chat.py` | Short physical-robot responses, bounded inference, valid completion handling |
| `src/services/openai_client.py`, `synthesis/openai_tts.py`, `speak_service.py` | Explicit retry/deadline policy, pure synthesis, one publication owner, enum-safe source selection |
| `mqtt_service.py`, `device_state.py`, `main.py` | Integrate new correlation cleanup with existing lifecycle and mode invalidation |
| `src/logging_conf.py` | Useful stage diagnostics, restrained provider logs, no duplicate error floods |
| `tests/`, sanitized fixtures, conversation docs | Regression coverage and reproducible hardware procedure |

Do not modify generated protobufs. Leave puppet behavior and its endpoints intact. Retain native WAKEUP/ENROLLCONVO FALLBACK transitions, the wake-only shim and translation of incoming native English TTS.

## 7. Verification requirements

### Automated checks

Run focused tests while implementing, then the complete `mise test` suite. September 29 verification: **322 passed**, including captured-log regressions and the existing protocol/lifecycle tests.

Required meaningful scenarios:

1. Reproduce the captured 13 unmatched Cyrillic versus 9 matched Latin/mixed transcripts in a read-only log audit.
2. Test kk and ru input translation, English pass-through, mixed-language text, translation refusal/empty result and deadline exhaustion.
3. Assert one FINAL with the original UUID, timestamps and error contract; no canned wake phrase after chat entry.
4. Verify original-text recovery, missing correlation, repeated utterances with distinct IDs, two-device isolation, overlapping listens, TTL eviction and every reset path.
5. Reject the captured prompt echo; keep short legitimate kk/ru responses; avoid a blanket country-name blacklist.
6. Reproduce the `XIX` false rejection and short-Kazakh ambiguity; verify corrected language validation without accepting whole English replies.
7. Empty reprompts must not enter the LLM as synthetic user apologies. Nonempty first reprompts must still work.
8. TTS timeout, chat timeout, body-read failure after HTTP 200, stale success and stale failure must not publish duplicate or cancelled audio. Use controllable workers/fake clocks, not 30-second test sleeps.
9. Cover explicit enum value 0 with a different configured default; enforce native/remote source and ID/chunk invariants.
10. Parse escaped markup and preserve action/command marks. Verify incomplete/empty model completions have bounded localized handling.
11. Verify history deduplication and treatment of notify-before-audio-success/failure.
12. Retain existing wake-up, translation, raw-PCM, cancellation and puppet regression tests.

Do not write a fake-robot test that merely assumes English bridging will fix the real firmware, then present it as hardware proof.

### Operator test sequence

1. Start the service using the existing `mise start` workflow; confirm discovered device, bootstrap, modules and schedule.
2. Wake Moxie using the existing wake phrase. Confirm a real chat prompt and a Kazakh opener.
3. As an English control, ask “What is your name?” Confirm the entire correlated path and audible answer.
4. Ask “Сенің атың кім?” and “Жемістер туралы сөйлесейік.” Confirm original Kazakh ASR, faithful English FINAL, real matching continue, original input at the LLM, Kazakh output and audible answer.
5. Ask “Как тебя зовут?” with output still configured kk to isolate input recognition from output choice; then explicitly select ru and repeat to verify Russian output.
6. Test short answers, a mixed-language sentence, silence and background noise. No provider-prompt echo may become a chat turn.
7. Continue for at least ten sequential human/robot turns. Check factual relevance, one question at a time, latency and sustained listening.
8. Reconnect, exit/re-enter the module and change permitted modes during an in-flight request. Confirm that old transcripts/audio cannot appear in the new session.
9. Exercise a controlled provider failure in tests or a designated diagnostic run; verify bounded silence/error handling without claiming an audible answer.
10. Inspect the fresh log by exact UUID, not just by adjacent timestamps. Record operator-observed playback alongside server evidence.

Expected successful trace, with new log names illustrative rather than already implemented:

~~~text
STT END uuid=U phase=chat audio_ms=...
STT validated uuid=U original="Сенің атың кім?"
STT bridge uuid=U wire_language=en speech="What is your name?"
STT FINAL uuid=U
Rx remote-chat command=continue event_id=U module=OPENMOXIE_CHAT/default
remote-chat input event_id=U original_recovered=true output_language=kk
remote-chat audio event_id=U source=REMOTECHAT chunk=0 language=kk
audio published event_id=U bytes>0 elapsed_ms=<configured deadline>
operator: corresponding Kazakh answer heard
~~~

Completion requires real matching router turns for kk/ru, audible sequential answers, no prompt echoes, no valid-Kazakh language rejection, no artificial apology-as-user turns, and no 30-second waits or late/duplicate audio. Secondary native warnings must be either resolved or explicitly documented with remaining evidence needed. Do not promise that this backend can eliminate an unexplained firmware bug.

## 8. Boundaries and decisions

- Do not return to puppet mode or add a separate HTTP speak path for autonomous replies.
- Do not create a fruit content module to work around input that never reaches the existing module. Content modules can follow once ordinary conversation works.
- Do not bypass the live request/event-ID requirement, fabricate remote turns or synthesize an unrelated English trigger on every listen.
- Do not treat the Kazakh TTS success as proof that onboard NLU supports Kazakh.
- Do not blindly change inherited firmware language properties or disable local wake detection.
- Do not remove stale-output guards to make old audio play; fix prompt hallucination and timing upstream.
- The English bridge is the best-supported next experiment, not a guarantee about closed-source firmware. Keep original text and a controlled comparison path so its effect can be verified.
