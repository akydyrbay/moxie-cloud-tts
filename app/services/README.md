# Service organization

| Folder | Responsibility |
| --- | --- |
| `transport/` | MQTT connections, protobuf framing, event dispatch and reconnect handling. |
| `devices/` | Device registry, configuration state, settings updates and saved face options. |
| `speech/` | Audio helpers, speech marks, language rules, localization and correlated output. |
| `speech/recognition/` | Microphone request handling, transcription, English input bridging and original transcripts. |
| `speech/synthesis/` | Native cloud speech request handling, synthesis contracts and the TTS engine. |
| `conversation/` | Remote module handshake, schedules, dialogue ownership and chat history. |
| `conversation/llm/` | Chat generation and spoken reply length rules. |
| `actions/` | Live action queues, scripted questions, behavior markup and legacy puppet commands. |
| `providers/` | Shared provider client lifecycle and bounded worker capacity. |
| `setup/` | Broker endpoint and Wi-Fi pairing QR generation. |

The application lifecycle and dependency scope remain in `app/core/`. HTTP
routes remain in `app/router/`, and generated protocol bindings in `app/protos/`.

Import concrete modules from their responsibility packages. Package initializers
contain documentation only, so importing a package does not construct a runtime
or register protocol handlers. Keep cross-service imports that are deliberately
inside functions lazy; they break cycles between device state, speech and the
conversation engine.
