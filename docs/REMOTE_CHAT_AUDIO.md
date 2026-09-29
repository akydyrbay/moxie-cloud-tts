# Remote-chat audio — September 28, 2026

After wake-up and entry into `OPENMOXIE_CHAT`, Moxie said the translated native
line (“Тамаша!”) and then listened silently. It spoke no opener and gave no replies.
This note retains the historical diagnosis. **The later 19:18 run and operator
report establish Kazakh replies to English input.** September 29 input-bridge
and latency repairs are described in [localization repair](LOCALIZATION_FIXES.md).

## Trace of the 17:18 run (`log/debug.log`, lines 674–995)

| Time | Line | Event |
| --- | --- | --- |
| 17:18:09 | 674–677 | WAKEUP `start` `e59a1ebd-…` (injected wake transcript) → our FALLBACK |
| 17:18:09 | 679 | `notify` OPENMOXIE_CHAT “Awesome!” (native line) |
| 17:18:09 | 681–686 | real `prompt` `85834651-…` → our GLOBAL_RESPONSE opener |
| 17:18:09 | 687–722 | **only** TTS request: `e59a1ebd-…`, speakable `Awesome! " "` → “Тамаша!” spoken |
| 17:18:15 | 723 | `notify` with the opener text, plus `response_chunks` and `notify_source` |
| 17:18:32 | 779 | chat FINAL `c44dacc2-…` in 1233 ms, `Мокши жеміссіз туралы сөйлесейік.` |
| 17:19:03 | 843 | chat FINAL `bfad2a9d-…` in 3216 ms, `Мокси маған жемістер туралы сұрақ қойшы.` |
| 17:19:58 | 901–902 | `fa530af3-…` hits the 6 s deadline → error 66 |
| 17:20:23 | 954–956 | FINAL `2b054f07-…` in 5617 ms; robot warns `STTFinal was delayed from EOS by 6252 ms` |
| 17:20:39 | 990 | FINAL `199f0dc1-…` `Бұл` |
| — | — | no `continue`/`reprompt`, no chat completion, no TTS request before shutdown (17:21:11) |

The 16:34 run (lines 293–369) has the same shape: prompt `f25e74d9-…`, a single
wake-ID TTS request (`Cool! " "`), an opener notify, and silent listens. Then
`RobotStateModule: Forcing IDLE state, Waiting has gone too long` (line 368, after
an operator sleep command).

**First missing transition:** audio for the accepted remote opener. Nothing in
either capture requested it. Every later symptom happens after that point.

## Measured facts

1. Neither captured Sep 28 remote opener raised a `CloudTTSRequest`, with the
   bare `<usel variant="2">` wrapper this repo emits. The earlier code comment
   records the same result with plain markup. Native lines did raise requests.
   This observation does not establish the firmware's general remote-TTS contract.
2. `Awesome! " "` / `Cool! " "` is native content, not the opener. In the
   Sep 25 SESSIONDISMOUNT capture, the notify speech omits exactly the `" "` that
   its TTS speakable contains (`remote_chat_handoff.json`, lines 172/181).
3. At 17:17:48–49, two wake-gate FINALs (`bb85bf0c`, `cd6a4244`) overlapped
   pending wake output, and no router turn followed. The later `e59a1ebd` FINAL
   produced `start`. All five chat FINALs also had no following router turn.
   The logs do not reveal why the firmware did not create these turns.
4. Response latency does not explain the first failure. `c44dacc2` returned in
   1.2 s with no robot warning and still produced no turn.
5. The delay warning is measured on receipt, not taken from our `timestamp`. The
   robot clock runs about 3.5 s ahead of the service: a TTS request with
   `timestamp=1790597892521` (12:18:12.5 UTC) was logged at 12:18:09. If the robot
   used the response `timestamp`, it would report about 2.1 s, not 6252 ms. The
   warning matches our 5617 ms plus transit. STT timestamp semantics were left
   unchanged. Confidence, language and original-speech fields are defaults, as in
   OpenMoxie. For the wake shim, those defaults already produced router turns.
6. The protobuf has `RequestSourceType.REMOTECHAT_TTS_REQUEST = 1`, distinct from
   `ROBOT_TTS_REQUEST`. `CloudTTSSupplement` also carries `translation_time` and
   `automarkup_time`, which are cloud-side text-processing stages. The reference
   OpenMoxie never enables `cloud_tts`, so it has no example of either.

## Hypothesis acted on

With `cloud_tts=1`, the firmware expects the cloud to synthesize accepted
remote-module output itself. The robot does not ask for it. The audio is keyed
to the router turn's `event_id` and marked `REMOTECHAT_TTS_REQUEST`. Until that
audio arrives, the robot stays in its output state, and chat FINALs are dropped
there. Facts 1, 3 and 6 support this; it is not proven. The notify at 17:18:15
does not prove playback. Its `source_event_id` was not logged before this change.

## Change

`remote_chat.py`, for an accepted `OPENMOXIE_CHAT` `start`/`prompt`/`continue`/
`reprompt`, when `cloud_tts=1` and puppet is off:

1. Opener: the English source is translated first (bounded, cached), so the
   response text, markup and later notify match the audio. If translation fails,
   a localized generation-error text is sent with correlated silence.
2. The RemoteChatResponse is published first, unchanged in shape.
3. Then exactly one `CloudTTSResponse` is published: `request_source=REMOTECHAT`,
   `event_id` = router `event_id`, `chunk_num=0`, OpenAI PCM and marks. If
   synthesis fails, correlated silence is sent. A turn without an `event_id` gets
   no audio.

Unchanged: robot-originated requests (still `ROBOT_TTS_REQUEST` via
`cloud_tts_request.py`), FALLBACK/entrance/onboard turns (no pushed audio),
`cloud_tts=0` (onboard TTS speaks the markup), puppet/TELEHEALTH, STT, protos.
Inbound robot requests explicitly use source 0; remote turns explicitly use source 1.
`CLOUD_TTS_REQUEST_SOURCE` is only a fallback for shared helper callers that omit it.

New logs: `remote-chat audio device=… event_id=… chunk=0 source=REMOTECHAT` before
synthesis, and `remote-chat notify … source_event_id=… notify_source=…
response_chunks=…` for every remote-module notify. No audio or secrets are logged.

## Local verification

`tests/test_remote_chat_audio.py` replays the 17:18 IDs, text and transcript. The
wake-ID TTS markup is reconstructed, and the `continue` is synthetic because none
was logged. Assertions:

- The prompt yields the Kazakh opener JSON, then REMOTECHAT audio on `85834651-…`.
- The native `e59a1ebd-…` request is still answered with ROBOT audio (“Тамаша!”).
- The chat FINAL carries an English translation under the original listen UUID.
- A synthetic `continue` on that UUID recovers the original Kazakh for GPT; its reply is voiced
  on that turn's ID.
- There is no pushed audio with `cloud_tts=0`, in puppet mode, or for an onboard
  FALLBACK.
- Translation or synthesis failure produces correlated REMOTECHAT silence.

Two existing tests encoded the previous request-only design and were updated:
`test_cloud_tts_request.py::test_wakeup_to_audio_even_when_chat_prompt_arrives_first`
and `test_remote_chat_handoff.py::test_logged_handoff_sequences`.

## Review fixes and verification

The first review found **205 passed, 5 failed**. The failures were in the new
test helper: bootstrap JSON is a string, while protobuf payloads are bytes.
The helper now checks the payload type before checking a protobuf prefix.

The review also reproduced stale remote speech after a module exit or a switch
to puppet mode during translation. Remote output now checks the original
session, the current turn and a revision of the speech settings before sending
JSON, synthesized audio or fallback silence. A new turn, module exit, sleep,
rebootstrap, robot broker disconnect/reconnect or service shutdown invalidates
old work. Cloud-TTS and puppet toggles advance the revision, so switching off
and back on cannot revive an old result. Unchanged settings, history-only
notifications and another device's session do not invalidate a live turn.

`speak_service.speak` accepts an optional validity check for remote callers and
checks it again after the provider returns, including on failure. A provider
call already running in a thread may finish; its stale result is discarded
without publishing audio or silence. New logs distinguish
`remote-chat discarded stale response` and `discarded stale speech` from output
actually published. The robot-originated and puppet callers use the existing
behavior without this optional guard.

Added 33 concurrency cases that hold translation, chat generation or TTS while
state changes. Thirty cases reproduced stale output before the production fix;
the other three verify that unrelated activity preserves a live turn.
After that historical fix, **243 tests passed**. September 29 adds the input
bridge and reliability regressions: **322 passed**. STT-to-router acceptance
of the new English bridge remains a hardware check.

## Hardware acceptance (operator)

Restart the service, reboot Moxie, keep puppet off, wake it, and say “Moxie, let's go”.

1. After “Тамаша!”, the Kazakh opener must be **audible**, with mouth movement.
   The log should show `remote-chat audio … event_id=<prompt>` → `spoke
   event_id=<prompt>`. The opener notify's `source_event_id` should equal `<prompt>`.
2. Say “жемістер туралы сөйлесейік”. Expect `STT FINAL … speech=…` →
   `Rx remote-chat … command='continue'` (or `reprompt`) with that speech →
   `openai chat replied` → REMOTECHAT audio on that turn → audible reply → another listen.

If the opener plays but Kazakh transcripts still produce no router turn, try an
English question in the same chat state and preserve both captures. English
forwarding with Kazakh failing would support investigating an English transcript
bridge at the robot boundary. The September 29 repair implements that bridge by default; FINAL carries
English while GPT recovers the original transcript. Compare the exact UUIDs
using the new [operator procedure](LOCALIZATION_FIXES.md#operator-acceptance).

If step 1 is still silent, keep the capture. Check for robot warnings near the
REMOTECHAT publish, and whether the notify `source_event_id` differs from the
router `event_id`. Either would reject this hypothesis, or its key or chunk.
Latency remains open: one listen missed the 6 s STT deadline. The robot warned
at about 6.25 s but not for the faster samples. Its exact warning threshold and
whether it rejects a late FINAL remain unmeasured.
