#!/usr/bin/env bash
# Start MQTT broker + FastAPI service for Moxie cloud TTS.
# Usage (from project root):  ./scripts/start.sh   or   mise start
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# .env is loaded by the app itself (pydantic-settings); this check only warns
# early, so the key can live in either place.
if [[ -z "${OPENAI_API_KEY:-}" ]] && ! grep -qs '^OPENAI_API_KEY=.' .env; then
  echo "warning: OPENAI_API_KEY is not set (env or .env)." >&2
  echo "         cloud_tts will stay OFF so Moxie keeps its own voice." >&2
fi

echo "==> starting MQTT broker (docker compose up mqtt -d)"
docker compose up mqtt -d --build

echo "==> waiting for broker on localhost:8883"
ready=0
for _ in $(seq 1 30); do
  if python3 -c 'import socket; s=socket.create_connection(("127.0.0.1",8883),1); s.close()' 2>/dev/null; then
    ready=1
    break
  fi
  sleep 1
done
if [[ "$ready" -ne 1 ]]; then
  echo "error: MQTT broker did not become ready on :8883" >&2
  exit 1
fi
echo "==> broker is up"

echo "==> starting FastAPI service (cloud_tts auto-enables when a key is set)"
echo "    Swagger:   http://localhost:8000/docs"
echo "    No robot?  run 'mise sim' in another terminal to fake one"
exec uv run fastapi dev src/main.py --reload-dir src
