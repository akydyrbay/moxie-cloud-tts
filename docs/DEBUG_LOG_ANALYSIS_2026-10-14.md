# October 14 robot log analysis: free chat and native content

The October 14 run shows working native-response arbitration, but unreliable
speech delivery and microphone interpretation. **19 of 23 native requests
containing speech received silence.** The robot entered STORY and selected a
story, but no story narration received successful speech audio in this capture.
Repeated joke requests reached EXIT confirmation; no JOKES entry was recorded.
Sustained free chat and a normal activity-completion return to chat remain
unverified.

**Operator observations added after the initial analysis:** asking for a story
starts the native STORY activity and audible background music, but Moxie does
not voice the story text visible in the logs. The operator also confirms that
unwanted noises trigger listening and requests stricter acceptance of clear,
sufficiently loud speech. These are direct observations from the robot, in
addition to the log evidence below. The requested sensitivity change is a
proposed implementation fix, not an already-applied runtime change.

This report analyzes **only the October 14 entries in `log/debug.log`**. Earlier
documents establish implementation progress; their older incidents are not
counted as October 14 failures. No runtime code or configuration was changed.

## Evidence and implementation baseline

- Capture: [log/debug.log](../log/debug.log), 3,058 lines, all dated
  **2026-10-14**, from **19:40:17 to 20:19:58**, using timestamps as logged.
  It includes an early service start/stop, the main run, and a restart at 20:19.
- Capture SHA-256:
  `a7fee054b13468d0e8fe036e08f361803ba500b67d31a82b557e6d92d2d0469a`.
- Checkout reviewed: `c1e2eec` (`fix moxie native content`, October 13).
  The capture does not record a service commit, so its exact running revision
  cannot independently be established. Its routing behavior matches this code.
- Robot: ID ending `7223e31511dc`; firmware reports `24.10.803`, API `rchat_api=10`,
  `cloud_tts=1`, `local_stt=on`, `cloud_translate=en`. Output in this run is Russian;
  STT calls log `auto`. This capture does not establish Kazakh acceptance.
- Reviewed first: [README](../README.md), [development guide](AGENTS.md),
  [native content policy](NATIVE_CONTENT_POLICY.md),
  [live plan](SPEAK_LIVE_PLAN.md), and the separate
  [puppet history](SPEAK_PUPPET_ANALYSIS.md). Code review covered MQTT dispatch,
  module arbitration, microphone buffering/validation, input translation,
  localization context, TTS, provider capacity, and the relevant tests.

The current design is appropriate: `OPENMOXIE_CHAT` generates short GPT replies
and correlated REMOTECHAT audio; native modules receive FALLBACK while their
CloudTTSRequests receive localized ROBOT audio. Native answers bypass the chat
wake-word requirement. Original human transcripts remain correlated by UUID.
Puppet is a separate operator path.

The log supports several parts of that implementation:

- **Native ownership works for the captured chat handoff.** At 20:08:04,
  `5a703f5b` requests a story. EXIT selects its confirmation, GPT output is
  discarded, and the server sends an empty FALLBACK. The subsequent failure is
  native TTS timing out, not GPT replacing the confirmation
  ([lines 367–449](../log/debug.log#L367)).
- **Native recognition remains enabled.** At 20:09:16,
  “Могси, расскажи первую историю” becomes “Moxie, tell the first story.”
  The firmware changes from `STORY/the_robot_olympics` to
  `STORY/dr_wilde_makes_a_speech`, the first offered title
  ([lines 657–675](../log/debug.log#L657)). The original content ID was not proof
  that the second story had already been selected.
- **The earlier microphone-capacity symptom does not recur here.** There are no
  `Too many pending microphone streams` errors. Provider-worker saturation does
  occur and is a different limit.
- **Interim animation does not cancel GPT.** `[animation:curious]` at 20:19:09
  is followed by remote audio publication at 20:19:13
  ([lines 2620–2670](../log/debug.log#L2620)). The input for that answer was
  incorrectly injected by the server, as discussed below.

## Counts and sequence

Counts distinguish native requests, remote audio, and expected command-only
silence. `spoke` means the server published PCM; it is not a playback receipt.

| Measurement | October 14 result |
| --- | --- |
| Native CloudTTSRequests | 31: 23 with speech, 8 command-only |
| Native requests with speech | 4 published speech audio; 19 returned silence |
| Causes of those 19 silent speech requests | 14 TTS timeouts; 3 translation-stage `ProviderBusyError`; 1 translation timeout; 1 `TranslationError` |
| Successful speech publications across both paths | 7: 4 ROBOT and 3 REMOTECHAT |
| All TTS failure records with an `error=` field | 18 `TimeoutError`, 3 `ProviderBusyError`; excludes duplicate “silence already sent” summaries |
| All silent publications | 34: 8 command-only native, 19 failed native speech, 7 remote failures |
| Microphone END events / FINAL responses | 108 / 106; two listens retired at the WAKEUP transition |
| FINAL phases | 67 chat, 36 native, 3 wake-gate |
| Chat inputs ignored for missing wake word | 65 of 67 chat FINALs; the other two contain story requests |
| Explicit STT errors | One error 66: deadline exceeded at 20:11:37 |
| Firmware forced-IDLE warnings | 9 |

The 23 speech requests include `CereprocWarmup-ignore-me`. Excluding that
warmup gives 18 failures out of 22. All captured native chunks are numbered 0,
often with a new event ID for the next story segment; this run does not validate
nonzero chunk handling. Some event IDs have both a remote fallback response and
a native response. Counting those without request source would incorrectly
label them duplicate native responses.

| Time | Observed sequence | Result |
| --- | --- | --- |
| 20:02:24–25 | Robot logger crashes during QR-related handling and restarts | Separate firmware issue |
| 20:07:15–50 | Native greeting, WAKEUP transition, remote opener and reprompt | Four speech publications |
| 20:07:52 | First recognized story request finishes after an empty reprompt | No router turn for that input UUID is recorded |
| 20:08:04–38 | Second story request → EXIT → STORY choice prompts | GPT cancellation works; confirmations/options receive silence |
| 20:09:16–20:11:59 | First story selected; several narration/repeat requests | Narration and recognition-warning audio fail |
| 20:12:40–20:14:14 | Repeated joke requests → EXIT; later return to STORY | One joke confirmation publishes audio; no JOKES entry |
| 20:14:49–20:16:18 | Sleep/stop, sleep status, wake, OPENMOXIE_CHAT prompt | Stale native listening phase; new opener also times out |
| 20:19:08–13 | Service rejoins a live chat and injects the wake phrase | GPT answers the fabricated input |

## Findings and corrective work

### 1. P1 — STORY runs with background music but no spoken narration

**Confirmed by operator observation and speech-failure logs; underlying
provider/network cause unresolved.**

The reported behavior is specifically: request a story → native STORY starts →
background music plays → story text appears in the service logs → the robot
does not speak that text. Reaching STORY and producing text are therefore
insufficient acceptance checks.

The log records story text in incoming `CloudTTSRequest` and translated
`synthesis_text` before synthesis succeeds. Those entries describe intended
speech. For example, `83e614f3` contains the story introduction at 20:09:17,
its Russian translation at 20:09:18, and a TTS timeout with silence at 20:09:26
([lines 671–683](../log/debug.log#L671)). There is no successful narration PCM
publication for that event. The firmware separately reports that
`moxie_mu_kumanaisland_theme_loop_v1` is already playing on the **Background**
channel at 20:11:49 and 20:14:04
([line 907](../log/debug.log#L907), [line 1362](../log/debug.log#L1362)).

Native background sound and cloud-generated speech follow separate paths.
Preserved native commands can continue the activity and its music while the
speech response contains silence. This explains how the observed music and
missing narration can coexist; the log does not establish a music-volume or
speaker-mixing defect. The operator's observation confirms the audible symptom,
while the logs identify missing generated speech as a concrete failure to fix.

The initial story confirmation `5a703f5b` arrives at 20:08:04 and translates
successfully at 20:08:05. TTS reaches its 8,002 ms deadline at 20:08:13 and
returns 9,600 bytes of silence. The firmware also reports forced IDLE at that
time ([lines 377–449](../log/debug.log#L377)).

This repeats throughout the story. Examples include the story introduction
`83e614f3` at 20:09:26, dialogue `b7d5c5d1` at 20:10:08, narration `ba1ac9fd`
at 20:10:42, and “We have to help Z” (`08d226d0`) at 20:14:14. Even “Sure!”
(`547f15d6`) and “Great!” (`b20e6af2`) time out. Long story text alone therefore
does not explain the failure. The chat opener `9695f677` also fails at 20:16:17.

[synthesis/openai_tts.py:49](../src/services/synthesis/openai_tts.py#L49)
waits for a complete PCM response. [speak_service.py:236](../src/services/speak_service.py#L236)
applies the total synthesis deadline and publishes silence on failure.
[cloud_tts_request.py:105](../src/services/cloud_tts_request.py#L105) separately
handles translation failures the same way. Silence preserves correlation and
command marks, but cannot communicate a confirmation, story choice, or narration.
Consequently, the native activity can continue without the child receiving its
dialogue.

**Next work:** instrument provider start, response/body completion, deadline,
and late worker completion with event ID, source, and safe error categories.
Establish why complete PCM misses the budget, including for short lines. Then
measure a bounded recovery strategy against actual firmware waiting behavior.
Do not treat a larger timeout or a silent response as successful speech. Preserve
native actions and request correlation while improving audio reliability.

### 2. P1 — Speculative native fallback synthesis consumes capacity needed by native speech

**Confirmed local resource failures; code and ordering explain a contributor.**

At 20:08:13, :15, and :16, the server starts synthesizing the same canned apology
for native router turns `62bd1a1b`, `2efeaf9d`, and `0fb501df`. At :17 the robot
selects the story-choice prompt for `0fb501df`, but its translation immediately
fails with `ProviderBusyError`. Another native prompt fails the same way at :18.
The three obsolete fallback jobs only report discarded output at :21, :23,
and :24 ([lines 438–542](../log/debug.log#L438)). Saturation recurs at 20:14:04,
when the selected story-resumption line also receives silence
([lines 1313–1364](../log/debug.log#L1313)).

[remote_chat.py:825](../src/services/remote_chat.py#L825) prepares fallback audio
for every native router turn. After the 0.15-second grace, synthesis proceeds
without explicit remote selection. [provider_work.py:19](../src/services/provider_work.py#L19)
shares four workers across STT, both translations, chat, and TTS; acquisition
fails immediately when all four are busy. Native takeover prevents publication
but does not stop an already-running synchronous provider call or release its
slot. Holding the slot until the worker actually ends is necessary to retain
the concurrency bound.

The exact worker occupancy is not logged, so the capture cannot assign every
occupied slot to a specific job. It does establish that obsolete fallback work
overlaps selected native failures and has no priority separation from them.

**Next work:** reserve or prioritize bounded capacity for selected native speech
and recognition; avoid repeated live synthesis of the identical fallback, for
example through bounded reusable PCM with fresh response IDs/marks. Preserve
late-notify support: making all remote audio wait indefinitely for a selection
notification would reintroduce another known failure. Add a replay with several
slow fallback jobs, a late native winner, and overlapping recognition.

### 3. P1 — Suspicious recognition results can drive native confirmations and choices

**Observed recognition-quality problem; the acoustic cause is not proven.**

The operator additionally reports listening triggered by other noises. This
confirms unwanted activation as a user-visible issue, although it does not
identify the acoustic source of each individual transcript below. The requested
noise/sensitivity fix is specified in finding 10.

Native mode forwards a stream of short, unrelated multilingual results:

| Time / UUID prefix | Recognized original | Wire input / consequence |
| --- | --- | --- |
| 20:08:15 / `2efeaf9d` | `敲门` | `Knock on the door.` reaches EXIT |
| 20:08:16 / `0fb501df` | `好的。` | `Okay.` reaches EXIT; next selection is STORY |
| 20:10:31 / `7dbc9c27` | `Iskreno.` | Forwarded unchanged into STORY |
| 20:13:58 / `5c551529` | `결과를` | `result` reaches EXIT |
| 20:14:03 / `dc939a7b` | `Не смею.` | `I don't dare.`; firmware cancels the joke confirmation and resumes STORY |

See [lines 474–520](../log/debug.log#L474),
[803–835](../log/debug.log#L803), and [1229–1359](../log/debug.log#L1229).
The `Okay` recording is only 960 ms and arrives while the first confirmation's
TTS is failing. The log proves that this recognized affirmation was forwarded
and followed by a native transition. It does **not** prove the person actually
agreed, or that every foreign-language result is hallucinated: raw microphone
audio and a reference transcript are absent.

[stt/openai_stt.py:68](../src/services/stt/openai_stt.py#L68) sends auto-language
audio without a recognition hint in this run.
[stt/validation.py](../src/services/stt/validation.py) checks empty output,
prompt echoes, and grossly excessive length. The audio gate at
[stt_request.py:315](../src/services/stt_request.py#L315) rejects only near-digital
silence (`rms < 2`). There is no calibrated speech/addressing check for native
mode, where wake-word bypass is intentional.

**Next work:** obtain a short, controlled microphone capture with known spoken
answers, silence, room speech, and robot/background audio. Check framing,
recognition language, and calibrate the stricter speech gate in finding 10
against that evidence. For a
controlled Russian run, evaluate the existing `STT_INPUT_LANGUAGE=ru` setting;
evaluate `kk` separately. Keep valid one-word native answers working and avoid
an arbitrary duration/RMS blacklist. Translation must not turn uncertain audio
into presumed consent or a story selection.

### 4. P1 — The English bridge mistakes ASCII text for English

**Confirmed code defect, reproduced offline.**

At 20:08:08, the bridge reports
`original='Zdecydowanie.' wire='Zdecydowanie.' translated=False wire_language=en`.
At 20:10:28 it does the same for `Taktykos namai.`, and at 20:10:31 for
`Iskreno.` ([line 401](../log/debug.log#L401),
[lines 807–815](../log/debug.log#L807)). These are not English translations.

[stt/input_translation.py:47](../src/services/stt/input_translation.py#L47)
returns any text without non-ASCII letters unchanged. STT nevertheless labels
the wire language `en` whenever the bridge is enabled. Script membership cannot
establish language. This is independent of whether the recognition itself was
accurate.

**Next work:** bypass translation only when English is actually established,
or use bounded translation/validation for uncertain textual inputs. Keep a
safe numeric-input path. Add the three captured examples to bridge regressions
and make logged language claims reflect what was established.

### 5. P1 — Restarting the service fabricates a wake phrase in an existing chat

**Confirmed lifecycle defect, reproduced offline.**

At 20:19:08 the service receives microphone UUID `c1c934fb` before a module
request. It defaults to `phase=wake-gate`, injects `Moxie, let's go`, and returns
a successful FINAL without recognition. The robot then sends a **continue in
OPENMOXIE_CHAT**, not a WAKEUP turn. GPT answers “Куда мы идём? Это будет весело!”
and audio publishes at 20:19:13
([lines 2597–2619](../log/debug.log#L2597),
[2649–2670](../log/debug.log#L2649)). This is not evidence of successful
recognition or a meaningful free-chat exchange.

[stt_request.py:291](../src/services/stt_request.py#L291) treats absent phase
state as `wake-gate`. [reset_conversation_state](../src/services/stt_request.py#L122)
also removes phase state, restoring that default. Injection happens before
audio-quality validation. An offline call with unknown phase and silent PCM
likewise returned the canned phrase with error code 0.

**Next work:** distinguish unknown/reconnecting state from an observed entrance.
Arm the shim only from trustworthy current WAKEUP/ENROLLCONVO evidence. Establish
the current module after service restart before interpreting microphone input.
Test restart during free chat, during native content, and while asleep, alongside
the legitimate cold-start entrance path.

### 6. P1 — Sleep and wake status do not reliably update the listening phase

**Confirmed state mismatch; a related notification case reproduces offline.**

The robot reports `mode=sleep` at 20:15:06. After touch/operator wake it emits
a WAKEUP notification at 20:15:52, but microphone processing still says
`phase=native`. This persists through UUID `b20e6af2` at 20:16:09, which returns
`Take me for a meal.`. Only the subsequent WAKEUP router `start` corrects phase
and retires two in-flight listens, `f04acfe2` and `23410ef8`. The firmware then
warns that those UUIDs have received no response
([lines 1467–1533](../log/debug.log#L1467),
[1557–1636](../log/debug.log#L1557)).

[mqtt_service.py:321](../src/services/mqtt_service.py#L321) logs robot status but
does not apply lifecycle transitions; normal `/state` handling is bootstrap-only
at [line 436](../src/services/mqtt_service.py#L436).
[remote_chat.py:467](../src/services/remote_chat.py#L467) can observe a boundary
notification while native mode is active, but applies the boundary transition
only when `current` matches a chat-session record. Native routing has already
cleared that chat session. Sending a correlated WAKEUP notification to a brain
in native mode reproduced `phase=native`.

**Next work:** reconcile trustworthy sleep/session/wake signals with dialogue
state and invalidate work at the actual boundary. Preserve protection against
delayed old notifications; do not simply let every WAKEUP notify override a newer
chat turn. Define how still-live microphone waiters are closed at this transition
without publishing stale transcripts, and replay the two captured UUIDs.

### 7. P2 — Successful STT can still be too late for the robot's turn

**Confirmed latency and missing-routing evidence; exact firmware cutoff unknown.**

The first story request `d96d090d` ends at 20:07:48 and receives a correct FINAL
at 20:07:52 after 3,759 ms of server processing. Meanwhile, the robot has already
requested and received an empty-input reprompt. It reports an EOS delay of about
4.18 seconds; no router request for `d96d090d` appears in the capture. The next
story request `5a703f5b` does reach routing
([lines 305–371](../log/debug.log#L305)).

Other recognized turns take 5,003 and 5,675 ms server-side; firmware reports
roughly 5.57 and 6.11 seconds from EOS. At 20:11:37, `aed36e76` exceeds the
6-second deadline and returns error 66. Its successor finishes after 4,695 ms
([lines 863–888](../log/debug.log#L863)). The robot repeatedly warns near three
seconds without a response, but that warning alone does not establish a hard
three-second rejection threshold. Some later FINALs are demonstrably accepted.

[stt_request.py:331](../src/services/stt_request.py#L331) serializes recognition
and bridging per device, including queue wait in the END deadline. That bounds
work but can delay later listens behind slow recognition. Increasing the pending
listen count solved a capacity pattern; it did not establish acceptable latency.

**Next work:** measure END → queue admission → recognition → bridge → FINAL →
router acceptance by UUID, and compare it with firmware timing. Reduce or
prioritize work within bounded deadlines. Keep absent router acceptance visible
as a separate outcome from successful transcription. Include reprompt races in
the replay; do not assume every FINAL is consumed.

### 8. P2 — Wake-word matching drops a plausible addressed request

At 20:16:59, `720785f7` recognizes “Окси, расскажи шутку.” and returns empty
speech because `Окси` is not an accepted first token
([lines 2013–2014](../log/debug.log#L2013)). At 20:19:46, “Вообще иди спать.”
is also ignored because it contains no recognized wake word
([lines 2920–2921](../log/debug.log#L2920)). The latter is current configured
behavior, not evidence by itself that matching is broken.

[stt/validation.py:23](../src/services/stt/validation.py#L23) requires an exact
first-word match against [config.py](../src/config.py). The bridge cannot repair
a name rejected before translation. The log does not reveal whether the speaker
actually said `Окси` or recognition lost the initial consonant.

**Next work:** evaluate captured name variants against recorded addressed and
unaddressed speech. Add justified normalization/aliases or an explicit
conversational addressing policy without opening free chat to all room audio.
Keep native short answers independent of the wake word. The 65 ignored chat
FINALs should not all be counted as bugs: most contain unrelated speech and
their rejection is useful.

### 9. P2 — Localization and failure diagnostics still need acceptance work

Russian source/target text shows inconsistent narrator gender: the confirmation
uses “рассказала” at 20:08:05, the choice reprompt uses “говорил” at 20:08:30,
and narration uses “остановился” at 20:11:40. Zaygo becomes “Заыго” in the
introduction and “Зайго” later. These are generated-text quality defects; the
affected requests mostly failed synthesis, so the log does not establish that
the child heard them ([lines 380](../log/debug.log#L380),
[608](../log/debug.log#L608), [675](../log/debug.log#L675),
[837–899](../log/debug.log#L837)).

Context instructions exist in [translation.py](../src/services/translation.py),
but names and narrator voice are not enforced by a curated glossary. The recent
localized history is updated only after audio publication at
[cloud_tts_request.py:151](../src/services/cloud_tts_request.py#L151), and
[localization_context.py:62](../src/services/localization_context.py#L62) rejects
an update if its snapshot has changed. Failed speech therefore cannot provide
the same continuity as successfully published speech. This is a limitation to
account for, not a reason to pretend failed speech was heard.

The recognition-warning line at 20:19:11 additionally fails with the generic
`TranslationError` ([line 2651](../log/debug.log#L2651)). Its validator can reject
several conditions, including incomplete output, refusal, Latin text, or markup;
the log does not identify which occurred. A claim that a specific word such as
`GRL` caused it would be speculation.

**Next work:** review Russian and Kazakh titles, character names, narrator voice,
and answer choices; maintain terminology separately from heard-dialogue history
if needed. Log safe validation reason codes and correlate provider work with
event/source. Native notify logs currently omit the `source_event_id` value,
and timestamps have only second precision, limiting analysis of the 150 ms
selection grace. Keep transcripts/audio publication distinct from confirmed
playback in both diagnostics and acceptance results.

### 10. P1 — Reduce STT sensitivity with a clear, sufficiently loud speech gate

**Requested fix; implementation and hardware calibration pending.**

The desired behavior is to accept intentional speech only when it is
sufficiently loud and distinguishable from background noise. Quiet incidental
sounds, handling/click noises, and the robot's own music or voice must not become
human transcripts or drive activity choices. This applies to both free chat and
native activities; native answers such as “Да”, “Нет”, “Иә”, and “Екінші” must
still work without a wake word when spoken clearly above the configured gate.

The current `rms < 2` check only rejects almost silent PCM. The `active_ms`
measurement is diagnostic, not a speech acceptance condition. Consequently,
ordinary noise can reach the recognition provider. Wake-word filtering happens
after recognition and is intentionally absent for native answers, so it cannot
serve as the acoustic filter.

**Proposed implementation:**

1. Add a configurable minimum speech level and a required margin above a
   measured background-noise level. Evaluate short audio frames as well as the
   utterance; a single loud click must not make a mostly noisy recording pass.
   Calibrate defaults on this robot at the intended speaking distance. Do not
   choose a fixed higher RMS value solely from the transcript log.
2. Combine loudness with speech detection. A loud fan, tap, or music passage
   must fail even if it exceeds the volume threshold. Use stable speech
   evidence and different start/stop thresholds to avoid repeatedly opening
   and closing on marginal noise. Preserve the beginning of accepted words in
   a small bounded buffer, including brief yes/no answers.
3. Reject unwanted audio before starting provider recognition/translation.
   Complete the live microphone UUID once with a defined ignored/no-speech
   result and retain the correlation needed to keep its remote turn silent.
   Routine noise rejection must not become invented input, a conversational
   apology, or a provider-failure error 66. Ensure rejected audio also cannot
   activate the wake-phrase shim.
4. Account for the robot's background music and speech when calibrating noise
   rejection or echo suppression. Do not disable listening for an entire story:
   the child must still answer native questions and issue intentional commands.
5. Log compact rejection reasons and measured levels so sensitivity can be
   tuned: below minimum volume, insufficient separation from background,
   non-speech, or accepted speech. Keep the existing buffering, deadline, and
   per-device resource bounds.

The service currently receives START/SPEECH/END events already produced by the
robot's VAD. A server-side gate can prevent noise from reaching STT and dialogue,
but cannot by itself prevent the firmware from opening a microphone stream or
displaying a listening indicator. If that visible activation must also stop,
investigate a verified robot-side VAD/sensitivity control separately; the current
code does not establish such a control.

**Acceptance checks for this fix:**

| Input at the intended robot distance | Required behavior |
| --- | --- |
| Quiet room noise, fan hum, rustling, or handling noise | No provider transcription, dialogue response, or native selection |
| Loud isolated tap/click | Rejected as non-speech despite its peak volume |
| Background story music or Moxie's own speech, with no person talking | No false human transcript or self-triggered choice |
| Voice below the configured minimum or obscured by noise | Ignored; no fabricated words or repeated error/apology loop |
| Clear, sufficiently loud addressed chat question | Recognized and answered; existing chat wake-word policy applies |
| Clear native yes/no/first/second answer | Recognized without a wake word; correct native branch |
| Clear human speech over the normal story soundtrack | Accepted when it meets the speech/volume criteria; listening remains available |

## Other robot issues observed

| Issue | Evidence and relevance |
| --- | --- |
| BoLogger SIGABRT | At 20:02:24–25, `Main-bo-logger` / `com.embodied.bo_unity:bologger` aborts; a `regex_error`, teardown stack, and `RightPoint::on_QRCommand` appear. The OS restarts BoLogger. This is a real firmware failure, distinct from the historical `Main-bo-brain` identity/config crash. The exact trigger is not established. See [lines 43–83](../log/debug.log#L43). |
| Unsupported service queries | `license` at 20:02:21 and `remote_lines` at 20:06:26 / 20:15:37 are explicitly unanswered. The [activity-query handler](../src/services/remote_chat.py#L920) lacks verified contracts. Track compatibility, but this run reaches chat and STORY, so these warnings do not prove the cause of speech timeouts. |
| Assets and background behavior | Missing `I_001_01_Idles_Sleep` animation, duplicate assets, `LizardErrorEvent BugID: 17`, and duplicate story-background sound warnings are present ([lines 89–143](../log/debug.log#L89), [907](../log/debug.log#L907), [1362](../log/debug.log#L1362)). No demonstrated causal link to provider timeouts; visual/audio behavior still needs robot review. |
| Power and unavailable legacy cloud | Battery falls from 26% to 19%; a localized low-battery line successfully publishes at 20:12:48. `BOBackup` fails at 20:15:38. Keep power state in the reproduction record. Neither observation establishes the TTS failure cause. |

## Verification performed and completion criteria

Existing tests were run against the reviewed checkout:

```text
DEFAULT_TTS_LANGUAGE=kk .venv/bin/python -m pytest tests/ -q
344 passed in 4.87s
```

The language override matches historical fixtures; it did not change runtime
configuration. Providers and MQTT are mocked. Separate offline calls, with no
provider access or robot publications, reproduced ASCII-language bypass,
unknown-state wake injection, and native-phase retention after WAKEUP notify.
`git diff --check` was also run for this documentation change.

The current suite includes native arbitration, late notification, queue limits,
native short answers, and a synthetic return-to-chat request. It does not prove
that this firmware naturally finishes a story and returns to chat. In this
capture chat reappears after sleep/wake, followed by a failed opener. There is
no completed story, no actual JOKES module entry, and no sustained sequence of
GPT answers to recognized human questions.

Prioritize the next work in this order:

1. **Restore speech delivery and protect selected native work.** Replay the
   October 14 timeout/saturation sequence with slow workers, then verify audible
   confirmation, both story options, and several narration segments on hardware.
   Reproduce the reported music-without-narration case and require intelligible
   story speech alongside the background music. Count failure silence separately
   from audio success; logged story text or music alone is not a pass.
2. **Reduce noise-triggered listening and recognition.** Implement and calibrate
   finding 10 using quiet/loud noises, story music, and clear human speech.
   Verify both rejection of unwanted audio and acceptance of brief native
   answers; measure firmware listening activation separately from server STT
   acceptance.
3. **Repair lifecycle and input interpretation.** Cover service restart in chat
   and STORY, sleep/status boundaries, the two retired microphone UUIDs, the
   ASCII bridge examples, and controlled native yes/no/first/second answers.
4. **Verify both module cycles.** In separate ru and kk configurations, run
   ordinary chat → native story → native completion/cancel → actual chat reentry,
   and ordinary chat → joke confirmation → JOKES → chat. Hear every required
   question before answering it; verify that speech matches native visuals.
5. **Confirm repeated free chat and recovery.** Ask at least five ordinary
   questions per configured language, verify a relevant audible reply and next
   listen, then repeat after restart and sleep/wake. The injected wake phrase
   must never count as recognized human input.

Preserve the working ownership split, native command marks, UUID/event/chunk
correlation, bounded workers, and late-notify support while addressing these
failures. Custom modules, puppet changes, and automatic output-language switching
are not prerequisites for this acceptance work.
