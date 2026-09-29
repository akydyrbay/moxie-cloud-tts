# Remote-chat handoff — September 27, 2026

Historical verification of the handoff fix. The later user-requested
[translation implementation](TTS_TRANSLATION.md) replaces the fixed Kazakh
greeting discussed below; FALLBACK priority and lifecycle fixes remain.

The entrance response used the wrong arbitration priority. We returned a
speak-only `GLOBAL_COMMAND` for WAKEUP/ENROLLCONVO, replacing the robot's local
response. OpenMoxie's protocol executes actions only on the accepted response;
discarding the local response can also discard the transition into scheduled
chat. Returning `FALLBACK` preserves that transition. Kazakh speech remains in
the proven Cloud TTS override, which changes audio without replacing brain actions.

This fixes a demonstrated difference from OpenMoxie. The captures strongly
support it as the cause of the handoff failure; they do not expose the native
action or prove the patched firmware interaction. A fresh robot run is required.

## Evidence

| Capture | Observed sequence |
| --- | --- |
| `log/debug.log.2026-09-07`, 12:28:40–41, lines 232–244 | WAKEUP start → server FALLBACK → chat SCHEDULED → OPENMOXIE_CHAT notify → actual prompt `12283584-6239-4642-bd20-ef62906c5704`. The later native Excellent TTS request uses wake ID `5c491ac0-73f6-41a9-b2f5-b1afe3585198`. |
| `log/debug.log`, Sep 25 14:06:19–14:08:32, lines 142–229 | WAKEUP continue `7ef747c8-a387-4441-aef9-8ee1050420db` → GLOBAL_COMMAND greeting → chat SCHEDULED → WAKEUP notify → SESSIONDISMOUNT → empty module. No real chat turn. |
| Same file, 17:14:50–17:15:03, lines 481–493 | WAKEUP start `5c85bcb9-fb15-4795-8007-ce4c2edee14d` → GLOBAL_COMMAND greeting → chat SCHEDULED → WAKEUP notify. No chat prompt; firmware later reports waiting too long and forces IDLE. |

The operator-confirmed Kazakh audio at 14:06:10 used a different live request,
`c0cfb174-449c-4029-a30d-ae47f1dba9ea`. It demonstrates playable Kazakh cloud audio,
not acceptance of the later remote-chat transition.

The reference checkout inspected was `/Users/bake.72/Desktop/hri-lab/openmoxie`:

- `doc/RemoteModuleAPI.md`: output priority arbitration; actions run only on an accepted response.
- `site/hive/mqtt/moxie_remote_chat.py`: FALLBACK for onboard modules.
- `site/hive/mqtt/volley.py`: `fallback=false` remains unchanged even when output type is FALLBACK.
- `site/hive/mqtt/moxie_server.py` and `site/data/default_schedules.json`: our modules envelope and only-chat schedule already match.
- `site/hive/mqtt/zmq_stt_handler.py`: FINAL error code 66 on transcription failure, still sent to the robot.

## Changes

- WAKEUP/ENROLLCONVO now return FALLBACK in both action fields. The legacy
  fallback flag matches Volley. No launch or unsolicited TTS response is added.
- At the time of this handoff fix, the fixed Kazakh greeting was still used as
  the fallback and opener. The later translation change replaces it; native
  wake-up requests now translate their own line in its live audio slot.
- Actual router turns synchronize STT phase: entrance, chat, or other onboard
  content. Dismount/unknown turns stop the fake wake transcript; a later entrance
  re-arms it. Notify/SCHEDULED alone never establish or clear conversation.
- Bootstrap clears that device's pending STT timestamps as well as chat state.
- Completed listens without the wake shim receive a correlated FINAL error 66,
  with empty speech and an explicit transcription-unavailable message. Audio
  recognition is still unimplemented; the error prevents an unanswered request.
- Logs distinguish `entrance deferred to local response`, `remote-chat entered`,
  `remote-chat left`, and `remote-chat missing module`. “Entered” means a real
  router turn reached the server, not a playback receipt.

## Verification

Before changing production code, the new/updated regression expectations
produced **15 failures** for entrance priority and unanswered listens.
After the fix, `mise test` reports **174 passed**.

`tests/fixtures/remote_chat_handoff.json` preserves **21 selected events** from
the three sequences above. Their timestamps, IDs, text and activity fields were
checked against the original log lines. Complete packets were not logged; TTS
markup in the replay is reconstructed from logged speakable text. The failed
captures remain failed-entry inputs: tests never invent a chat prompt for them.

Coverage includes native transition priority, repeated entrance, local/unknown
exit, delayed notifications, per-device isolation, STT response correlation and
reconnect timestamps. The earlier Excellent/prompt race and Sep 23 audio replay
still pass, including Kazakh replacement, original TTS IDs/chunks and command marks.

A direct comparison using the local OpenMoxie Volley class also verified exact
equality of FALLBACK and GLOBAL_RESPONSE envelopes for identical request/text/markup.
Providers and MQTT are mocked in tests; this verifies server behavior, not
firmware arbitration, audible playback, or recognition quality.

## Next robot run

Restart the service, reboot Moxie, keep puppet off, and wake it. Follow
[README's handoff verification](../README.md#verify-the-remote-chat-handoff).
Look for the entrance FALLBACK followed by a real OPENMOXIE_CHAT router turn,
Kazakh reply markup, a live robot TTS request and its correlated response.
Confirm audible playback and the next listen. A scheduled activity alone fails
this check. Preserve the capture if SESSIONDISMOUNT or an empty module returns.

The next listen currently ends with `STT FINAL error ... error_code=66` because
real STT is the next milestone. Recognition, input-driven kk/ru selection and
stable repeated conversation remain in [the live plan](SPEAK_LIVE_PLAN.md).
