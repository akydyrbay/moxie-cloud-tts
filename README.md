# APRIL Moxie agent

Python 3.12 service for Moxie's MQTT/protobuf protocol, with Russian/Kazakh
conversation, native speech localization, live speech/actions and face changes.
One agent controls the protocol alongside one MQTT broker; do not run the old
`moxie-cloud-tts` or another OpenMoxie controller against the same robot topics.
Cloud recognition, translation, chat and synthesis currently require internet.
APRIL catalog/session integration remains incomplete.

## Start

Requirements: Python 3.12, `uv`, a reachable MQTT/TLS broker, and an OpenAI API
key. Docker Compose is needed only for the bundled broker. Run commands from
the project root.

```bash
uv sync --frozen
```

Create `.env` with installation settings (replace the sample key and address):

```dotenv
OPENAI_API_KEY=your-api-key
DEFAULT_TTS_LANGUAGE=kk
STT_INPUT_LANGUAGE=auto
MQTT_HOST=localhost
MQTT_PORT=8883
MQTT_PUBLIC_HOST=192.168.1.50
```

`MQTT_HOST` is the broker address used by the agent. `MQTT_PUBLIC_HOST` is the
LAN address the robot can reach and that the endpoint QR advertises. It must
not be localhost. Output defaults to `ru`; recognition currently defaults to
`ru`. Set `auto` to avoid forcing a recognition language hint.

For the bundled broker, supply installation-specific, broker-readable TLS files:
`keys/ca.crt`, `keys/mosquitto.crt`, `keys/mosquitto.key`. These are not supplied
with this agent. The local configuration permits anonymous clients and uses
`MQTT_QR_DISABLE_VERIFY=true` for the robot's self-signed-certificate setup.
Then start the broker and agent:

```bash
docker compose up -d --build mqtt
uv run --frozen python -m app
```

For an existing broker, configure its address and run only the second command.
The agent listens on **5053** (`HTTP_PORT` overrides it); MQTT/TLS uses **8883**.
Use one HTTP worker. The agent acquires a local controller lock, connects MQTT
in the background, and keeps HTTP diagnostics available while the broker is
unreachable. The lock does not prevent a controller on another machine.

Use the commands above for this checkout: the older `mise dev`/`mise start`
helpers still launch FastAPI's development default port, and `mise sim` points
to a simulator that is not included.

## Pair Moxie

Open Swagger at **http://localhost:5053/docs**. If Wi-Fi must be configured,
use `POST /moxie/wifi-qr` there and display its returned PNG to the robot. Then
show the broker endpoint QR:

```bash
curl http://localhost:5053/moxie/endpoint-qr/info
curl http://localhost:5053/moxie/endpoint-qr -o endpoint-qr.png
curl http://localhost:5053/moxie/devices
```

Display `endpoint-qr.png` for the robot to scan. The robot must be able to reach
the broker on port 8883. Once it connects, the agent discovers its real device
ID and sends configuration, subscriptions, module information and a schedule.
Cloud TTS enables when a provider key is present; puppet mode starts disabled.
Wake Moxie and let its entrance sequence finish before beginning chat.

## Check and stop

```bash
curl http://localhost:5053/health
curl http://localhost:5053/status
curl http://localhost:5053/ready
```

`/health` checks HTTP liveness. `/status` separates broker connectivity, device
freshness, bootstrap publication and provider configuration. `/ready` can return
503 while setup is incomplete. None is a physical playback confirmation.
Press Ctrl+C to stop the agent; `docker compose stop mqtt` stops the bundled
broker. Pending operations are in memory and do not survive a restart.

Logs go to the console and `log/debug.log`. Set `ROBOT_LOG_CAPTURE=false` to
omit forwarded firmware log lines; status and service diagnostics remain.
Broker logs: `docker compose logs mqtt`.

## Documentation

- [Live mode](docs/live_mode.md): listening-to-speech pipeline and module ownership.
- [Puppet mode](docs/puppet_mode.md): entering TELEHEALTH, remaining controls and the historical speech failure.
- [Capabilities](docs/capabilities.md): face, speech, behavior and activity commands.
- [Native content](docs/native_content.md): observed content, firmware catalog and support limits.
- [Future work](docs/future_work.md): remaining features and investigations.

## Structure

```text
app/
  main.py, __main__.py    HTTP app and single-worker launcher
  config.py              Environment settings and robot defaults
  core/                  Runtime scope, lifecycle and controller lock
  router/                HTTP request models and routes
  services/
    transport/           MQTT connection, framing and dispatch
    devices/             Registry, state, settings and face persistence
    speech/              Audio, marks, language, localization and output
      recognition/       Microphone handling and English input bridge
      synthesis/         Native speech handler and TTS engine
    conversation/        Remote routing, dialogue ownership and llm/
    actions/             Live queues, behaviors and remaining TELEHEALTH controls
    providers/           Provider client and bounded worker pool
    setup/               Pairing QR generation
  protos/                Generated protocol bindings; do not hand-edit
openmoxie/               Vendored protocol documentation and upstream license
tests/                   Essential regressions with mocked MQTT/providers
docs/                    Five focused guides
```

## Tests and provenance

```bash
DEFAULT_TTS_LANGUAGE=kk uv run --frozen pytest tests/ -q
```

The language override supports historical Kazakh fixtures; it does not alter
installation settings. Tests do not require hardware or a real provider key and
do not prove audible playback or firmware completion. Retest on the robot after
changes to protocol timing, ownership, localization or native activity handling.

Retained upstream material is
covered by the [OpenMoxie MIT license](openmoxie/LICENSE). Generated bindings
remain unchanged; only part of their original schema set is included here.
