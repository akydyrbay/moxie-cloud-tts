# Fix Explanation — OpenAI Cloud TTS for Moxie

## What was broken

With only the MQTT broker running (no OpenMoxie Django “hive”), the robot:

1. Published `remote-chat` modules queries repeatedly and got **no answer**.
2. Never entered a real conversation turn, so it **never emitted a `CloudTTSRequest`**.
3. If `cloud_tts` was flipped on anyway, it waited in **MarkingUp** for audio that never arrived, then force-idled and slept (~5s wake-and-crash loop).

`POST /tts/speak` could publish a valid `CloudTTSResponse`, but with a **random `event_id`**. The robot only plays audio whose `event_id` matches an outstanding request, so those messages were silently dropped. That endpoint now pushes a telehealth `PLAY_OUTPUT` so the robot raises a real request first — see `AUDIT_FIXES.md`.

## What we implemented

A **remote-chat brain** in this service that reimplements OpenMoxie’s *protocol* (not its AI providers), wired into the existing OpenAI STT/chat/TTS stack.

| Piece | Role |
|---|---|
| `src/services/remote_chat.py` | Answers modules handshake, schedule/mentor queries, conversation turns; FALLBACK for onboard modules |
| `src/services/mqtt_service.py` | Routes `/events/remote-chat` and `/events/client-service-activity-log`; publishes JSON on `/commands/{command}` |
| `src/config.py` | `OPENMOXIE_CHAT` module catalog, `only_chat`-style schedule, opener lines |
| `src/main.py` | Registers brain handlers on startup |
| `cloud_tts_request.py` + `speak_service.py` | When the robot sends `CloudTTSRequest`, echo `event_id`/`chunk_num` and publish OpenAI audio back to that device |
| `src/services/telehealth.py` | Puppet-mode control: push arbitrary speech, interrupt, wake |
| `src/services/devices.py` | Learns device ids from MQTT topics (no hardcoded `DEVICE_ID`) |

### Protocol details (matched to OpenMoxie)

1. **Modules query** (`backend=data` + nested `query.query=modules`, or flat `query=modules`)  
   → `/commands/remote_chat` with `query_data` listing `OPENMOXIE_CHAT` (`source: REMOTE_CHAT`).

2. **Schedule query** (`client-service-activity-log`, `query=schedule`)  
   → `/commands/query_result` with a schedule that launches `OPENMOXIE_CHAT/default` immediately (skips Embodied onboarding modules).

3. **Router turns** (`backend=router`)  
   - Our module: `prompt` → opener; `continue`/`reprompt` → OpenAI chat; `notify` → history only (OpenMoxie notify-tracking).  
   - Other/onboard modules: **FALLBACK** response so the robot does not wait forever for a cloud reply.

4. **Device bootstrap** when a device first appears, and again whenever it
   reconnects or starts a new session  
   → push config + `ProtoSubscribe` for `CloudTTSRequest`.

5. **Cloud TTS path (pull)**  
   By default (`CLOUD_TTS_AUTO_ENABLE=true`, requires `OPENAI_API_KEY`), cloud
   TTS is enabled in memory at startup, so the robot gets `cloud_tts="1"` on its
   first config push. It then sends `CloudTTSRequest` with the turn’s
   `event_id`; `cloud_tts_request.py` synthesizes and replies with a matching
   `CloudTTSResponse`. Failures publish a correlated silence rather than
   nothing, so the robot can never be stranded in MarkingUp.

## How to run

See `README.md` for setup, endpoints, and the robot simulator. Short version:

```bash
echo 'OPENAI_API_KEY=sk-...' > .env
mise start          # docker mqtt + FastAPI (auto-enables cloud_tts)
```

Look for `CLOUD_TTS_AUTO_ENABLE: cloud_tts set to 1`, then when Moxie connects,
`bootstrapping device ... (cloud_tts=1)`.

To disable auto-enable: `CLOUD_TTS_AUTO_ENABLE=false mise dev`, then use
`POST /tts/cloud-tts {"enabled": true}` manually after the brain is answering.
