# Audit Fixes — what changed and why

Companion to `PIPELINE_AUDIT.md`. Every finding in that audit is resolved,
deferred with a reason, or answered as a question. Nothing was marked fixed
without being exercised: the whole loop was run against a real mosquitto broker
with a simulated robot, and the audio it received was written to disk.

Scope decision from the brief: **speech recognition is out of scope for now.**
The goal is "send Russian/Kazakh text over HTTP, hear it from the robot."

---

## The change that matters most

`POST /tts/speak` previously synthesized audio and published it with a
freshly-generated `event_id`. The robot **discards** any `CloudTTSResponse` whose
`event_id` doesn't match a request it is currently waiting on, so that endpoint
could never produce sound — it only proved that OpenAI and MQTT worked.

Speaking is now a round trip that lets the robot raise the id itself:

```
POST /tts/speak {"text": "Привет!"}
   │
   ├─ telehealth PLAY_OUTPUT ─────────────────► Moxie
   │                                              │  cloud_tts=1, so it asks
   │                                              │  the cloud for the voice
   ◄── CloudTTSRequest{markup, event_id=E} ───────┘
   │
   ├─ strip markup → OpenAI TTS → WAV 24 kHz mono
   │
   └─ CloudTTSResponse{event_id=E, audio} ─────► Moxie plays it
```

`PLAY_OUTPUT` is OpenMoxie's own puppet-mode primitive
(`moxie_server.send_telehealth_speech`), so this is the robot's supported path
for "say this now", not an invention. Only `output.text` is sent, never
`output.markup`: per OpenMoxie's schema `text` is used when `markup` is empty,
which keeps markup tags out of synthesis entirely.

New in `src/services/telehealth.py`, wired up in `src/router/tts.py`.

---

## CRITICAL

### C1 — OpenAI STT not connected → **deferred deliberately**

Not wired, as instructed. What changed is that it is now honest and quiet
instead of misleading:

- `src/services/conversation.py` was **deleted**, along with `POST /tts/converse`.
  It advertised "publishes the spoken reply to the robot" but used a random
  `event_id`, so it could never do that. Keeping a plausible-looking path that
  silently cannot work is worse than not having one.
- Inbound `zmqSTTRequest` frames are dropped without logging (they arrive
  continuously and flooded the log).
- `POST /tts/transcribe` remains, explicitly labelled bench-only.

The robot's unanswered `license` query is now logged (see I8) because that is the
evidence needed when STT is picked up.

### C2 — hardcoded device id → **fixed**

`settings.DEVICE_ID` no longer has a value. Device ids are learned from the
`/devices/{id}/...` topics the robot publishes on, via the new
`src/services/devices.py` registry.

- `register_cloud_tts_request_handler` now takes `(device_id, payload)`, matching
  the remote-chat handlers.
- `speak_service.speak(..., device_id=...)` publishes to the requesting robot.
- Config pushes go to every discovered device.
- With no robot seen and no override, operator endpoints return **409 with a
  readable message** instead of publishing into the void.
- `DEVICE_ID` survives as an optional bench override only.

`GET /tts/devices` shows what the service has discovered.

### C3 — failures published nothing → **fixed**

Every inbound `CloudTTSRequest` now produces exactly one response. Empty markup
and synthesis failures publish a short silent WAV carrying the request's
`event_id`/`chunk_num`, so the robot leaves MarkingUp instead of force-idling and
sleeping — the exact bug this project exists to fix.

Deliberate exception: an unsupported language is a caller error with no robot
waiting, so it is validated *before* publishing and raises without emitting a
frame.

### C4 — audio format → **fixed to WAV**

`CLOUD_TTS_AUDIO_RAW_PCM` now defaults to `false`, so `AudioBuffer.buffer`
carries a real RIFF/WAV container (24 kHz mono, 16-bit) as requested. OpenAI
produces it directly via `response_format="wav"` — no header surgery. The raw
PCM path is kept as a one-flag fallback.

Verified over the wire: the simulated robot received `WAV 28844 bytes 24000 Hz
1ch` and the files play back as normal WAVs. Whether the **real** firmware
prefers WAV or raw PCM still needs the hardware; flipping the flag is the A/B.

---

## IMPORTANT

| ID | Status | What changed |
| --- | --- | --- |
| I1 | Fixed | Auto-enable now requires `OPENAI_API_KEY`. Without one it logs an error and leaves `cloud_tts=0`, so Moxie keeps its own voice instead of going mute. |
| I2 | Fixed | `SettingsConfigDict(env_file=".env")` — `.env` is actually loaded now. |
| I3 | Fixed | Dropped the `#` subscription that echoed our own audio back; payload logging truncated to 64 bytes plus length. |
| I4 | Fixed | OpenAI timeout 30 s → 8 s, so a slow call cannot outlive the robot's wait. |
| I5 | Fixed | Fallback lines moved to `FALLBACK_LINES` in config, one per language, selected by the same detection used for the turn. A Russian/Kazakh child no longer hears an English sentence. |
| I6 | Fixed | `strip_markup()` removes tag-like spans before synthesis, so tags are never read aloud. |
| I7 | Fixed | MQTT client id is now `moxie-tts-<random>`; a fixed id made the broker evict the previous session on every reload. |
| I8 | Fixed (logging) | Unhandled activity-log queries — notably `license` — are logged at INFO with their `request_id`. |
| I9 | Fixed (configurable) | `CLOUD_TTS_REQUEST_SOURCE` selects `robot` or `remotechat`. |
| I10 | Open — needs hardware | `marks` stays empty. The mark schema can't be invented; it needs a capture from a real robot. |
| I11 | Improved | Detection is unchanged in spirit but now honest: text with no Cyrillic is not treated as evidence of Russian, and `fallback_language()` is used for canned lines. Detected language is logged per turn. |

---

## MINOR

M1 (stale comment), M2 (bench-only wording), M3 (empty `event_id` no longer
replaced with a random UUID), M5 (paho `CallbackAPIVersion.VERSION2`), M6
(`/state` stops scheduling work once bootstrapped), M7 (`MQTT_HOST`/`MQTT_PORT`
as plain scalars), M8 (`README.md` written), M9 (regression tests added) are all
fixed. M4 (`CloudTTSSupplement`) remains unknown pending a capture.

---

## Two problems found while testing the fixes

Neither was in the audit; both were found by actually running the loop.

**The broker never re-configured a rebooted robot.** Bootstrap was once-per-
process, so a Moxie that restarted came back with no `cloud_tts` and no
`ProtoSubscribe`, and stayed silent until the service was restarted. Fixed two
ways: the service now parses `$SYS/broker/log/#` for connect/disconnect (the same
mechanism OpenMoxie uses, keyed on the robot's `d_<uuid>` client id), and a
`modules` query — which only happens at session start — also forces a re-push.
Verified by killing and restarting the simulated robot: config was pushed again
within a second.

**The broker's own logging was broken.** `mosquitto.conf` wrote to
`/mosquitto/log/mosquitto.log`, a host bind mount the `mosquitto` user cannot
write to. Beyond losing the log, the failed open tore down the `log_dest topic`
destination, so `$SYS/broker/log/#` published nothing and connect detection could
not work at all. The config now logs to stdout (`docker compose logs mqtt`) plus
the topic.

---

## Your question about recording and end-of-speech

Yes on both counts, confirmed in the OpenMoxie source rather than assumed.

Moxie runs voice activity detection on-device. It streams microphone audio to the
server as `zmqSTTRequest` frames, each carrying `audio_content` plus a `vad`
field, and it decides for itself when the person stopped talking — it sets
`vad == END_OF_SPEECH` on the final frame. The server just accumulates frames
until that flag, then transcribes. Audio is **16 kHz mono PCM16** (note: not the
24 kHz used for playback). The reply is a `zmqSTTResponse` with the transcript.

This is enabled by the setting `stt: "4"` ("use ZMQ via Moxie server"); `stt: "0"`
means the robot uses Google directly with its own credentials. The separate
`local_stt: "on"` setting is only for on-device wake-phrase detection.

So when STT is picked up, we do not need to solve recording or endpointing — the
robot hands over cleanly delimited utterances. The open question is purely
whether the recognizer path accepts Russian and Kazakh, which the `license` query
logging is there to help answer.

---

## Verification performed

Full loop against a real mosquitto broker with `scripts/simulate_robot.py`, a new
tool that impersonates the robot side of the protocol and saves received audio to
`local/work/*.wav`:

| Check | Result |
| --- | --- |
| Robot discovery + config push | `bootstrapping ... (cloud_tts=1)` within ~1 s of the robot appearing |
| Russian `POST /tts/speak` | PLAY_OUTPUT → CloudTTSRequest → correlated WAV, 24 kHz mono, 49 ms |
| Kazakh `POST /tts/speak` | Same, detected as `kk`, 52 ms |
| `event_id` correlation | Response id matched the request id every time |
| Robot reboot | Reconnect detected, config re-pushed automatically |
| Puppet mode toggle | `moxie_mode=TELEHEALTH` received by the robot |
| Interrupt | Received by the robot |
| No API key | `cloud_tts` correctly left at `0` with an explanatory error |
| Test suite | 53 passed |

What this does **not** prove: that the real firmware accepts this WAV, that
`marks` are unnecessary, and that its recognizer handles Russian or Kazakh. Those
need the robot, and the simulator exists to make everything else cheap to
re-check when you get there.
