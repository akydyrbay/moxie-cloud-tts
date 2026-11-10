# Puppet mode

Puppet mode sets `moxie_mode=TELEHEALTH`. It parks autonomous scheduling and
free chat so a controller can issue diagnostic commands. It never auto-enables.
For normal Russian/Kazakh interaction, use [live mode](live_mode.md).

## Enter and leave

Start the agent and pair the robot using the [README](../README.md). Discover
its ID, then enable puppet mode:

```bash
curl http://localhost:5053/moxie/devices
curl -X POST http://localhost:5053/moxie/puppet \
  -H 'Content-Type: application/json' -d '{"enabled":true}'
curl http://localhost:5053/moxie/puppet
```

The toggle updates configuration on known devices. It does not select an
exclusive device binding or acknowledge that firmware entered TELEHEALTH.
When the robot asks for a schedule in this mode, the agent returns an empty one.
Repeated requests for the already-selected mode do not resend configuration.

```bash
curl -X POST http://localhost:5053/moxie/puppet \
  -H 'Content-Type: application/json' -d '{"enabled":false}'
```

After leaving, wait for a real `OPENMOXIE_CHAT` turn before expecting live output.
Mode changes invalidate pending microphone and speech work.

## Current capabilities

| Control | Meaning |
| --- | --- |
| `GET/POST /moxie/puppet` | Inspect/change desired puppet configuration. |
| `POST /moxie/interrupt?device_id=DEVICE_ID` | Submit a TELEHEALTH INTERRUPT command; physical stopping is not confirmed. |
| `POST /moxie/wakeup?device_id=DEVICE_ID` | Submit a wake command. |
| Robot TELEHEALTH activity-log events | Record observed session states such as `READY`; these are not per-line playback acknowledgements. |

There is **no puppet speech endpoint**. The legacy `play_output()`,
`wait_until_ready()` and `speak_line()` helpers were removed. `/moxie/speak`,
`/moxie/behavior` and `/moxie/activities` require puppet mode off and cloud TTS on;
requests in puppet mode return 409. `/moxie/sleep` relies on a future live-chat
turn, so it is not a reliable puppet-mode stop command.

## Why repeated Kazakh/Russian speech did not work

Earlier on-robot investigations in August–September 2026 used this path:

```text
PLAY_OUTPUT → robot CloudTTSRequest → cloud synthesis → CloudTTSResponse
```

The first line in a TELEHEALTH session was audible with mouth movement. Later
lines in the same session were silent even though requests, synthesized audio
and MQTT responses were recorded. Restarting the service could make the next
first line audible again. This was a repeated-playback failure, not proof that
the synthesizer could not generate Russian or Kazakh.

Two separate issues were identified:

- **Configuration crash — fixed:** sending full identity during a live mode
  toggle triggered a firmware SIGABRT in `SetMentorName`/`SaveUserName`. Live
  toggle payloads now omit `child_pii` and `pairing_status`; full identity is
  bootstrap-only. Dedicated face updates send only `child_pii.face_options`.
- **Later lines silent — unresolved:** the cloud completed both the audible and
  silent lines' request/response paths. The suspected cause was firmware output
  ownership or an unrecognized utterance-completion signal. The precise cause
  was never confirmed.

Raw PCM versus corrected WAV, mouth/closing-silence marks, identity stripping,
interrupt-before-play and `CloudTTSSupplement` experiments did not fix repeated
playback. A successful publish or TELEHEALTH `READY` must not be presented as
successful playback. The live remote-chat path was adopted for conversation;
the puppet defect is not claimed fixed.

Implementation: [mode configuration](../app/services/devices/settings.py) and
[remaining TELEHEALTH controls](../app/services/actions/telehealth.py).
