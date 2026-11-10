# Moxie capabilities

Commands below use the agent on port 5053. Start and pair it using the
[README](../README.md). Discover IDs with `GET /moxie/devices`; include `device_id`
in live-action bodies or control query parameters when targeting a robot.
Omitting it uses the most recently seen device or configured `DEVICE_ID`.
Configuration and face updates currently apply globally to eligible devices.

## Face customization

Get the saved selection and full asset catalog:

```bash
curl http://localhost:5053/moxie/face
```

The catalog has 60 labels across Eyes, Face_Colors, Eye_Designs, Face_Designs,
Eyelid_Designs, Mouth, Head_Hair, Facial_Hair, Brows, Glasses and Nose. Choose at
most one asset per layer. Eyes and Face_Colors carry `tested: true`; other
layers are less tested and may be unstable on some firmware.

```bash
curl -X POST http://localhost:5053/moxie/face \
  -H 'Content-Type: application/json' \
  -d '{"face_options":["MX_010_Eyes_Purple","MX_020_Face_Colors_Teal"]}'
```

Each request replaces the entire face selection; omitted layers revert to their
defaults. Changes are saved atomically to `local/work/moxie_face.json` (or
`MOXIE_FACE_FILE`). Bootstrapped devices receive a live face-only update:
`{"child_pii":{"face_options":[...]}}`. Saved options also apply at the next
bootstrap. Live updates have been operator-confirmed on the source robot;
that does not validate every asset or combination.

```bash
# Save without requesting a live change.
curl -X POST http://localhost:5053/moxie/face \
  -H 'Content-Type: application/json' \
  -d '{"face_options":["MX_010_Eyes_Gold"],"apply_live":false}'

# Restore the default face.
curl -X DELETE http://localhost:5053/moxie/face

# Recover from cached textures; applies only on the next robot reconnect.
curl -X POST http://localhost:5053/moxie/face \
  -H 'Content-Type: application/json' \
  -d '{"face_options":[],"reset_child_id":true}'
```

Response `live.status` is `published`, `partial`, `deferred` or `failed`.
`devices`, `failed_devices` and `deferred_devices` report publication outcomes;
`published` means MQTT acceptance, not confirmed display. A failed live update
keeps the saved selection. Face changes do not resend nickname, pairing, mode
or speech settings.

## Provided speech

Speech, behaviors and scripted activities require a bootstrapped robot in live
mode with cloud TTS enabled. They run on genuine `OPENMOXIE_CHAT` turns:

```bash
curl -X POST http://localhost:5053/moxie/speak \
  -H 'Content-Type: application/json' \
  -d '{"text":"Сәлем! Қалайсың?","language":"kk","delivery":"next_turn","ttl_s":120}'
```

The response is HTTP 202 with an operation `id`. `next_turn` replaces the next
eligible chat reply. `silence_only` waits for an eligible turn without child
speech; it still needs a robot turn. Text is limited to 1,000 characters and
language to `kk`/`ru`. Pass language explicitly for predictable output.

## Behaviors

```bash
curl http://localhost:5053/moxie/behaviors
curl -X POST http://localhost:5053/moxie/behavior \
  -H 'Content-Type: application/json' -d '{"behavior":"bht_back_and_forth_arm_wave"}'
```

| Behavior | Request value |
| --- | --- |
| Spin | `bht_spin_360` |
| Turn left / right | `bht_180_turn_left` / `bht_180_turn_right` |
| Arms in / out | `bht_arms_in` / `bht_arms_out` |
| Arms up then open | `bht_arms_up_then_open` |
| Wave | `bht_back_and_forth_arm_wave` |
| Celebrate | `bht_celebration_dance_01` |

Only these allowlisted assets can be requested; raw command markup is not
accepted. Behavior-only requests use correlated silent audio carrying command
marks. Add `behavior` to `/moxie/speak` to combine text and one gesture. Firmware
controls execution; audio publication does not confirm movement completion.
Continuous behavior sequences are [future work](future_work.md).

## Scripted questions

```bash
curl -X POST http://localhost:5053/moxie/activities \
  -H 'Content-Type: application/json' \
  -d '{"language":"ru","questions":[{"text":"Сколько будет пять плюс пять?","answers":["10","десять"]}],"max_attempts":3,"correct_behavior":"bht_celebration_dance_01"}'
```

Activities contain 1–20 questions and use real microphone recognition. Answer
matching normalizes case, punctuation and common number names; it does not use
semantic grading. Each question can specify its own `behavior`. Wrong or empty
answers get a bounded retry; at the attempt limit the first accepted answer is
spoken before advancing. Stop words end the activity. A native takeover or mode
change can cancel it. Scripted questions are agent-owned live actions, not new
native firmware modules.

## Operation status and stopping

Replace `OPERATION_ID` with the ID returned by a queued request:

```bash
curl http://localhost:5053/moxie/operations/OPERATION_ID
curl -X DELETE http://localhost:5053/moxie/operations/OPERATION_ID
curl -X POST http://localhost:5053/moxie/interrupt
```

Important statuses are `queued`, `dispatching`, `awaiting_answer`, `audio_sent`,
`completed`, `cancelled`, `expired`, `interrupted` and `failed`. `audio_sent` and
`completed` describe service progress, not firmware-confirmed playback.
Cancellation blocks pending work but cannot retract audio already sent.
`/moxie/interrupt` submits a separate robot command; its optimistic response is
not confirmation of stopping and does not cancel the service queue.

Queues allow eight active operations per device and 64 globally. Speech and a
scripted activity cannot be mixed on the same device: conflicts return 409,
capacity exhaustion 429. Operations/history are in memory and disappear on
restart; terminal history is bounded to 256 records and a ten-minute lifetime.

## Wake, sleep and diagnostics

```bash
curl -X POST http://localhost:5053/moxie/wakeup
curl -X POST http://localhost:5053/moxie/sleep
curl http://localhost:5053/moxie/cloud-tts
curl -X POST http://localhost:5053/moxie/cloud-tts \
  -H 'Content-Type: application/json' -d '{"enabled":true}'
```

Wake requires firmware wake support. Sleep queues a `sleep` action for a future
live-chat turn and also submits a standalone MQTT sleep command, whose effect
has not been established. It is not immediate or confirmed. Cloud TTS normally
enables at startup when a provider key is present; the toggle is diagnostic.
See [puppet mode](puppet_mode.md) for the separate TELEHEALTH toggle.

Recorded-audio uploads, arbitrary motor control, continuous choreography,
volume/battery product endpoints and durable APRIL session/action callbacks
are not implemented here.
