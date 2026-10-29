# Live speech, movement, and scripted questions

Keep `PUPPET_MODE_AUTO_ENABLE=false`, cloud TTS enabled, and
`CLOUD_TTS_EMIT_MARKS=true`. The robot must have connected and completed
bootstrap. These APIs do not enable puppet mode, wake the robot, invent a turn,
or interrupt native content. `/tts/speak` is now a live queue: existing clients
must handle HTTP **202**, replacing the former puppet response.

## Speak a provided line

```bash
curl -X POST localhost:8000/tts/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"Сәлем! Қалайсың?","language":"kk","ttl_s":120}'
```

The response contains `id`, `device_id`, and `status:"queued"`. The text is
spoken verbatim, without translation or GPT rewriting. The optional language
is `kk` or `ru`; omitted speech language uses the existing text heuristic.
Text is limited to 1,000 characters. An optional `device_id` targets a specific
bootstrapped robot; otherwise the configured/most recently seen robot is used.

Delivery choices:

- `"delivery":"next_turn"` (default): replace the next eligible live-chat
  reply with your line, even if the child spoke on that turn.
- `"delivery":"silence_only"`: answer child speech normally and wait for a
  genuine empty live-chat turn. Speech ignored for lacking a wake word does
  not count as silence for delivery.

Delivery has no guaranteed latency. The queue cannot make the firmware start
a turn. Native activities leave queued speech waiting; sleep, dismount, puppet
entry, disconnect/rebootstrap or a speech-mode change cancel it. Expiration is
checked at status reads, selection and publication, so expired work cannot play
later. The default expiration is 120 seconds; the allowed range is 1–600.

## Movement

```bash
curl localhost:8000/tts/behaviors

curl -X POST localhost:8000/tts/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"Молодец!","language":"ru","behavior":"bht_arms_out"}'

curl -X POST localhost:8000/tts/behavior \
  -H 'Content-Type: application/json' \
  -d '{"behavior":"bht_spin_360","ttl_s":60}'
```

Movement-only output uses silent audio with the behavior command mark. Both
routes wait for a live turn. Allowed labels are `bht_spin_360`,
`bht_180_turn_left`, `bht_180_turn_right`, `bht_arms_in`, `bht_arms_out`,
`bht_arms_up_then_open`, `bht_back_and_forth_arm_wave`, and
`bht_celebration_dance_01`. They are default assets in the copied OpenMoxie
manifest. The implementation uses its documented behavior-tree template;
physical movement and timing remain subject to firmware testing.

Raw markup is not accepted as a command interface. Speech text is escaped;
only the typed `behavior` field adds executable markup. Speech and movement
begin together; exact animation duration/completion is not reported by this API.

## A math activity

```bash
curl -X POST localhost:8000/tts/activities \
  -H 'Content-Type: application/json' \
  -d '{
    "language":"kk",
    "max_attempts":3,
    "ttl_s":900,
    "correct_behavior":"bht_arms_out",
    "questions":[
      {"text":"Беске бесті қосқанда қанша болады?","answers":["10"]},
      {"text":"Екіге үшті қосқанда қанша болады?","answers":["5"]}
    ]
  }'
```

For Russian, set `language:"ru"` and provide Russian question text, for example
`"Сколько будет пять плюс пять?"`. Question text and accepted answers are
provided by the caller; the service does not generate problems or evaluate
arbitrary arithmetic expressions. The language defaults to
`DEFAULT_TTS_LANGUAGE`. An individual question may also have a `behavior`,
which takes precedence over the success gesture when asking that question.

The first eligible turn asks question one; it never grades the speech that
started the activity. Subsequent child answers are checked against `answers`.
Exact case/punctuation-normalized aliases are supported, together with numeric
equivalence for integer number names 0–100 in Russian, Kazakh and English
(the English input bridge remains in use). For example `10`, `десять`, and `он`
match. Ambiguous answers such as “10 or 11” do not match. Supply additional
aliases explicitly for other wording; no LLM guesses whether an answer is right.

A correct answer produces “Молодец!” or “Жарайсың!” and the next question.
Wrong answers and silence repeat the question, up to `max_attempts` (1–5,
default 3). At that limit, Moxie gives the first accepted answer and moves on.
The final response ends the activity. “стоп”, “тоқта”, or “stop” also ends it.
Afterward, normal GPT chat resumes on the next spoken turn. Script history is
not used to automatically restart a finished question on silence.

While a question is awaiting an answer, short microphone responses need no
wake word. Outside that state, normal chat's configured wake-word rule applies.
Input translation gets the question text, but not the expected answer. Original
STT text is preferred for grading. Question progress commits only after audio
publication; a failed or interrupted question ends the operation without
advancing. Native activity takeover or a new live-chat opener cancels an
already-started script rather than treating another conversation's answer as
a math answer. Recognized answers retain their activity/question identity;
late answers cannot advance a different question.

Activities contain 1–20 questions (500 characters each) and 1–12 accepted
answer strings per question (80 characters each). The activity's total lifetime
is 10–3,600 seconds, default 900; this is not a per-child-answer countdown.
Retries happen only on real turns. The existing 12-second remote processing
deadline and 8-second TTS synthesis deadline still apply to each response.

## Status and cancellation

Use the operation ID returned by any of the three POST endpoints:

```bash
curl localhost:8000/tts/operations/OPERATION_ID
curl -X DELETE localhost:8000/tts/operations/OPERATION_ID
```

Possible statuses: `queued`, `dispatching`, `awaiting_answer`, `audio_sent`,
`completed`, `cancelled`, `expired`, `interrupted`, `failed`. Status includes
the last `event_id`, zero-based question index, attempt count and reason.
**`audio_sent`, `awaiting_answer`, and `completed` describe server progress,
not confirmed audible playback.** Native selection can still interrupt audio.
Cancellation prevents future scripted audio; it cannot retract audio already
published. If necessary, a still-current waiter receives silence without
movement marks after cancellation. The old telehealth `/tts/interrupt` is not
a live queue cancellation API.

Speech is FIFO, up to eight active operations per robot and 64 globally. A
silence-only item at the front holds later items until eligible or expired.
An activity and queued/in-flight speech cannot coexist on one robot (HTTP 409);
finish/cancel existing work first. A full queue returns 429. Terminal history
retains at most 256 operations for up to ten minutes; unknown/evicted IDs return
404. Everything is in memory and is lost on service restart.

No hardware playback claim follows from mocked tests. See
[the implementation plan and robot checklist](LIVE_ACTION_PLAN.md).
