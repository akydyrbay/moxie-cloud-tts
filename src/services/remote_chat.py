"""Remote-chat brain — the missing piece that lets Moxie enter conversation.

Mirrors OpenMoxie's `moxie_remote_chat.py` / `moxie_server.py` *protocol*
(modules handshake, schedule query, RemoteChatRequest/Response, FALLBACK for
onboard modules) but drives replies through this repo's OpenAI chat stage.

Without this brain the robot loops `{"query":"modules"}` forever, never emits
a `CloudTTSRequest`, and enabling `cloud_tts` alone stalls it in MarkingUp.
"""

from __future__ import annotations

import copy
import json
import logging
import random
import re
from typing import Any

from fastapi.concurrency import run_in_threadpool

from src.config import (
    DEFAULT_REMOTE_MODULES_INFO,
    DEFAULT_SCHEDULE,
    FALLBACK_LINES,
    REMOTE_CHAT_MODULE_IDS,
    REMOTE_CHAT_OPENERS,
    settings,
)
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.language import detect_language, fallback_language
from src.services.llm import openai_chat
from src.services.mqtt_service import mqtt_service
from src.services.stt_request import (
    note_conversation_active,
    reset_conversation_state,
)
from src.services.telehealth import handle_activity_log_telehealth

logger = logging.getLogger(__name__)

_ACTION_TAG_RE = re.compile(r"<[^>]*>")
_MAX_HISTORY = 20

# Local brain-entrance modules. FALLBACK loses to the local scripted line
# ("Excellent!"), which Cloud TTS then voices in English. An empty
# GLOBAL_COMMAND beats that line but leaves WAKEUP looping "Moxie, let's go!"
# because the module never hears a completed confirmation. A `launch` action
# on the *primary* response skips Cloud TTS (the robot jumps modules before
# the renderer runs). Replace Excellent with the configured ru/kk greeting and
# do not launch — DEFAULT_SCHEDULE already starts OPENMOXIE_CHAT after WAKEUP.
_ENTRANCE_MODULES = frozenset({"WAKEUP", "ENROLLCONVO"})


def fallback_line(language: str) -> str:
    """Canned line in the child's language (never English)."""
    return FALLBACK_LINES.get(language) or FALLBACK_LINES[settings.DEFAULT_TTS_LANGUAGE]


def _build_response(
    request: dict,
    *,
    text: str,
    markup: str | None = None,
    output_type: str = "GLOBAL_RESPONSE",
    actions: list[dict] | None = None,
) -> dict:
    """Build a RemoteChatResponse matching OpenMoxie's Volley schema."""
    speak_only = {"output_type": output_type}
    if actions:
        response_actions = [
            {**action, "output_type": action.get("output_type", output_type)}
            for action in actions
        ]
        # Side-effect actions (launch/sleep) as the primary block skip Cloud
        # TTS: the robot leaves the current module before it emits a request.
        # Keep a speak-only primary so `output` is voiced first.
        if "action" in response_actions[0]:
            response_actions.insert(0, speak_only)
    else:
        response_actions = [speak_only]
    primary = response_actions[0]

    response: dict[str, Any] = {
        "command": "remote_chat",
        "result": 0,
        "backend": request.get("backend", "router"),
        "event_id": request.get("event_id", ""),
        "output": {
            "text": text,
            "markup": text if markup is None else markup,
        },
        "response_actions": response_actions,
        "fallback": output_type == "FALLBACK",
        "response_action": primary,
    }
    if "speech" in request:
        response["input_speech"] = request["speech"]
    return response


def _strip_action_tags(text: str) -> tuple[str, list[dict]]:
    """Remove `<exit>` / `<sleep>` style tags and turn them into response actions."""
    actions: list[dict] = []
    matches = _ACTION_TAG_RE.findall(text)
    for raw in matches:
        parts = raw[1:-1].split(":")
        tag = parts[0]
        if tag == "exit":
            actions.append({"action": "exit_module", "output_type": "GLOBAL_RESPONSE"})
        elif tag == "sleep":
            actions.append({"action": "sleep", "output_type": "GLOBAL_RESPONSE"})
        elif tag == "launch" and len(parts) >= 2:
            action = {
                "action": "launch",
                "module_id": parts[1],
                "output_type": "GLOBAL_RESPONSE",
            }
            if len(parts) >= 3:
                action["content_id"] = parts[2]
            actions.append(action)
    clean = _ACTION_TAG_RE.sub("", text).strip()
    return clean, actions


def _speech_markup(text: str) -> str:
    """Wrap spoken text the way local lines arrive on CloudTTSRequest.

    Plain markup is accepted in remote-chat history (notify) but the speech
    renderer does not raise CloudTTSRequest for it. Local content uses
    `<usel variant ...>`; matching that shape is what gets voiced.
    """
    return f'<usel variant="2" genre="none">{text}</usel>'


def _is_modules_query(payload: dict) -> bool:
    """True for OpenMoxie's nested form and the flat capture form."""
    query = payload.get("query")
    if payload.get("backend") == "data" and isinstance(query, dict):
        return query.get("query") == "modules"
    return query == "modules"


class _ChatSession:
    """Per-device conversation history (notify-tracking, like OpenMoxie)."""

    def __init__(self, module_key: str) -> None:
        self.module_key = module_key
        self.history: list[dict[str, str]] = []

    def add(self, role: str, message: str) -> None:
        if not message:
            return
        if self.history and self.history[-1]["role"] == role:
            self.history[-1]["content"] += " " + message
        else:
            self.history.append({"role": role, "content": message})
            if len(self.history) > _MAX_HISTORY:
                self.history = self.history[-_MAX_HISTORY:]

    def ingest_notify(self, request: dict) -> None:
        for line in request.get("extra_lines", []):
            if line.get("context_type") == "input":
                self.add("user", line.get("text", ""))
        speech = request.get("speech") or ""
        if speech and "animation:" not in speech and "silent:" not in speech:
            self.add("assistant", speech)

    def reset(self) -> None:
        self.history = []


class RemoteChatBrain:
    """Answers remote-chat / schedule / mentor queries for connected Moxies."""

    def __init__(self) -> None:
        self._sessions: dict[str, _ChatSession] = {}
        # Last router event_id per device, so an HTTP sleep can attach a
        # `sleep` action to a volley the robot still has in flight.
        self._last_event_id: dict[str, str] = {}

    def get_modules_info(self) -> dict:
        return copy.deepcopy(DEFAULT_REMOTE_MODULES_INFO)

    def get_schedule(self) -> dict:
        return copy.deepcopy(DEFAULT_SCHEDULE)

    def is_remote_module(self, module_id: str, content_id: str) -> bool:
        key = f"{module_id}/{content_id}"
        if key in REMOTE_CHAT_MODULE_IDS:
            return True
        # Content id may be one of several aliases under the same module.
        return module_id in {k.split("/")[0] for k in REMOTE_CHAT_MODULE_IDS}

    async def ensure_device_bootstrapped(self, device_id: str) -> None:
        """Push config + ProtoSubscribe once per robot session.

        Cleared when the broker reports the robot reconnecting, so a rebooted
        Moxie is reconfigured instead of running without cloud_tts.
        """
        if not device_registry.mark_bootstrapped(device_id):
            return
        # New session (fresh connect or reboot): the robot is back at the WAKEUP
        # gate, so re-arm wake-trigger injection until it re-enters chat.
        reset_conversation_state(device_id)
        self._sessions.pop(device_id, None)
        self._last_event_id.pop(device_id, None)
        config = await device_state.get_config()
        props = config.get("settings", {}).get("props", {})
        logger.info(
            "bootstrapping device %s (cloud_tts=%s moxie_mode=%s)",
            device_id,
            props.get("cloud_tts"),
            config.get("moxie_mode"),
        )
        mqtt_service.publish_settings_update(device_id, config)
        mqtt_service.send_proto_subscribe(device_id)

    async def handle_modules_query(self, device_id: str, request: dict) -> None:
        # Bootstrap only if this device has not been configured yet. Re-pushing
        # the full config on every modules query (they can repeat) was thrashing
        # the robot mid-session and contributing to native crashes on mode
        # transitions. Reconnects are already handled by the broker-log watcher.
        await self.ensure_device_bootstrapped(device_id)

        req_id = request.get("event_id", "")
        payload = {
            "command": "remote_chat",
            "result": 0,
            "event_id": req_id,
            "query_data": self.get_modules_info(),
        }
        logger.info(
            "answering modules query for %s event_id=%s (%d modules)",
            device_id,
            req_id,
            len(payload["query_data"]["modules"]),
        )
        mqtt_service.publish_command_json(device_id, "remote_chat", payload)

    async def handle_schedule_query(self, device_id: str, request: dict) -> None:
        req_id = request.get("request_id", "")
        puppet = await device_state.get_puppet_mode()
        # TELEHEALTH already parks the robot. Handing it DEFAULT_SCHEDULE still
        # launches OPENMOXIE_CHAT, which with cloud_tts=1 enters MarkingUp and
        # can steal the speech-output slot from later PLAY_OUTPUT lines.
        schedule: dict = (
            {"provided_schedule": []} if puppet else self.get_schedule()
        )
        payload = {
            "command": "query_result",
            "query": "schedule",
            "request_id": req_id,
            "schedule": schedule,
        }
        logger.info(
            "answering schedule query for %s request_id=%s puppet=%s",
            device_id,
            req_id,
            puppet,
        )
        mqtt_service.publish_command_json(device_id, "query_result", payload)

    def handle_mentor_behaviors_query(self, device_id: str, request: dict) -> None:
        req_id = request.get("request_id", "")
        payload = {
            "command": "query_result",
            "query": "mentor_behaviors",
            "request_id": req_id,
            "mentor_behaviors": [],
        }
        logger.info(
            "answering mentor_behaviors query for %s request_id=%s",
            device_id,
            req_id,
        )
        mqtt_service.publish_command_json(device_id, "query_result", payload)

    def _session_for(self, device_id: str, module_key: str) -> _ChatSession:
        existing = self._sessions.get(device_id)
        if existing is not None and existing.module_key == module_key:
            return existing
        session = _ChatSession(module_key)
        self._sessions[device_id] = session
        return session

    def _clear_session(self, device_id: str) -> None:
        self._sessions.pop(device_id, None)

    def _send_remote_chat(self, device_id: str, response: dict) -> None:
        logger.info(
            "Tx remote_chat to %s event_id=%s output_type=%s text=%r",
            device_id,
            response.get("event_id"),
            response.get("response_action", {}).get("output_type"),
            (response.get("output") or {}).get("text", "")[:80],
        )
        mqtt_service.publish_command_json(device_id, "remote_chat", response)

    def _opener_for(self, content_id: str) -> str:
        openers = REMOTE_CHAT_OPENERS.get(content_id) or REMOTE_CHAT_OPENERS["default"]
        return random.choice(openers.split("|"))

    def _complete_entrance(self, device_id: str, request: dict) -> None:
        """Replace local "Excellent!" so WAKEUP can finish and the opener can run.

        Empty GLOBAL_COMMAND won the slot but did not complete WAKEUP, so the
        robot re-asked for "Moxie, let's go!" and never sent `command=prompt`.
        """
        self._send_remote_chat(
            device_id,
            _build_response(
                request,
                text=settings.WAKEUP_TTS_TEXT,
                markup=_speech_markup(settings.WAKEUP_TTS_TEXT),
                output_type="GLOBAL_COMMAND",
            ),
        )

    def request_sleep(self, device_id: str) -> None:
        """Ask the robot to sleep via a remote-chat `sleep` action.

        Firmware only honours this on an accepted volley, so we reuse the last
        router `event_id` when we have one. The MQTT `sleep` command (see
        `telehealth.sleep`) is sent alongside this from the HTTP handler.
        """
        event_id = self._last_event_id.get(device_id, "")
        payload = {
            "command": "remote_chat",
            "result": 0,
            "backend": "router",
            "event_id": event_id,
            "output": {"text": "", "markup": '<break time="0.01s"/>'},
            "response_actions": [
                {"action": "sleep", "output_type": "GLOBAL_COMMAND"},
            ],
            "response_action": {
                "action": "sleep",
                "output_type": "GLOBAL_COMMAND",
            },
            "fallback": False,
        }
        logger.info(
            "Tx remote_chat sleep to %s event_id=%r",
            device_id,
            event_id,
        )
        mqtt_service.publish_command_json(device_id, "remote_chat", payload)

    async def _remote_module_response(
        self,
        device_id: str,
        request: dict,
        module_id: str,
        content_id: str,
    ) -> None:
        cmd = request.get("command")
        module_key = f"{module_id}/{content_id}"
        session = self._session_for(device_id, module_key)

        if cmd == "notify":
            session.ingest_notify(request)
            return

        if cmd == "prompt" or (cmd == "reprompt" and not session.history):
            if cmd == "prompt":
                session.reset()
            text = self._opener_for(content_id or "default")
            self._send_remote_chat(
                device_id,
                _build_response(
                    request,
                    text=text,
                    markup=_speech_markup(text),
                ),
            )
            return

        speech = request.get("speech") or ""
        if cmd == "reprompt":
            speech = speech or "hm"

        language = detect_language(speech) if speech else settings.DEFAULT_TTS_LANGUAGE
        # Tentative history for this turn; official history arrives via notify.
        history_for_inference = copy.deepcopy(session.history)

        try:
            chat = await run_in_threadpool(
                openai_chat.respond,
                speech or "hello",
                language,
                history_for_inference,
            )
            text, actions = _strip_action_tags(chat.text)
            if not text:
                text = fallback_line(language)
        except Exception:
            logger.exception("remote-chat LLM failed for %s", device_id)
            text = fallback_line(fallback_language(speech))
            actions = []

        response = _build_response(
            request,
            text=text,
            markup=_speech_markup(text),
            actions=actions or None,
        )
        self._send_remote_chat(device_id, response)

    async def handle_router_request(self, device_id: str, request: dict) -> None:
        """Handle backend=router RemoteChatRequest (conversation turns)."""
        module_id = request.get("module_id") or ""
        content_id = request.get("content_id") or ""
        cmd = request.get("command")
        module_key = f"{module_id}/{content_id}"

        event_id = request.get("event_id") or ""
        if event_id:
            self._last_event_id[device_id] = event_id

        if self.is_remote_module(module_id, content_id):
            logger.debug("RCR:%s for remote module %s", cmd, module_key)
            # Notify is history-only and can fire while WAKEUP is still looping.
            # Marking conversation active on that notify stopped wake-gate STT
            # inject, so the robot re-prompted English and never reached prompt.
            if cmd in {"prompt", "continue", "reprompt", "start"}:
                note_conversation_active(device_id)
            await self._remote_module_response(
                device_id, request, module_id, content_id,
            )
            return

        # Onboard / unknown module: drop any remote session. Notify is a
        # no-reply path. Entrance modules must not FALLBACK — that loses to the
        # local script and Cloud TTS voices English instead of the opener.
        if device_id in self._sessions:
            self._clear_session(device_id)
        if cmd == "notify":
            return
        if module_id in _ENTRANCE_MODULES:
            logger.info(
                "entrance %s cmd=%s device=%s — replace Excellent with "
                "configured greeting GLOBAL_COMMAND (no launch)",
                module_key,
                cmd,
                device_id,
            )
            self._complete_entrance(device_id, request)
            return
        logger.debug(
            "FALLBACK for non-remote module %s cmd=%s device=%s",
            module_key,
            cmd,
            device_id,
        )
        line = fallback_line(fallback_language(request.get("speech") or ""))
        self._send_remote_chat(
            device_id,
            _build_response(
                request,
                text=line,
                markup=line,
                output_type="FALLBACK",
            ),
        )

    async def handle_remote_chat_event(
        self, device_id: str, payload: bytes | str,
    ) -> None:
        """Entry point for `/devices/{id}/events/remote-chat` payloads."""
        await self.ensure_device_bootstrapped(device_id)
        try:
            raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            request = json.loads(raw)
        except Exception:
            logger.exception("invalid remote-chat JSON from %s", device_id)
            return

        # Log every inbound remote-chat message so conversation-entry is visible:
        # after the modules handshake the robot should send a backend=router turn
        # (command=prompt, then the child's speech). If only modules queries ever
        # appear here, the robot never entered a conversation.
        logger.info(
            "Rx remote-chat from %s backend=%r command=%r module=%s/%s "
            "event_id=%r speech=%r keys=%s",
            device_id,
            request.get("backend"),
            request.get("command"),
            request.get("module_id") or "",
            request.get("content_id") or "",
            request.get("event_id", ""),
            (request.get("speech") or "")[:80],
            list(request.keys()),
        )

        if _is_modules_query(request):
            await self.handle_modules_query(device_id, request)
            return

        if request.get("backend") == "router":
            await self.handle_router_request(device_id, request)
            return

        logger.debug(
            "ignoring remote-chat from %s backend=%r keys=%s",
            device_id,
            request.get("backend"),
            list(request.keys()),
        )

    async def handle_activity_log_event(
        self, device_id: str, payload: bytes | str,
    ) -> None:
        """Entry point for schedule / mentor_behaviors queries."""
        await self.ensure_device_bootstrapped(device_id)
        try:
            raw = payload.decode("utf-8") if isinstance(payload, bytes) else payload
            request = json.loads(raw)
        except Exception:
            logger.exception("invalid activity-log JSON from %s", device_id)
            return

        subtopic = request.get("subtopic")
        # Always print the envelope. Previously anything that was not
        # `telehealth` or `query` was dropped, which hid speech/output FSM
        # reports if the firmware used another subtopic.
        logger.info(
            "activity-log %s subtopic=%s: %s",
            device_id,
            subtopic,
            request.get("message") or request,
        )
        if subtopic == "telehealth":
            # Robot reports READY / IN_SESSION / EXITING here.
            handle_activity_log_telehealth(device_id, request)
            return
        if subtopic != "query":
            return
        query = request.get("query")
        if query == "schedule":
            await self.handle_schedule_query(device_id, request)
        elif query == "mentor_behaviors":
            self.handle_mentor_behaviors_query(device_id, request)
        else:
            # Notably `license`: OpenMoxie answers this with Google speech
            # credentials, which is how the robot obtains cloud recognition.
            # We don't provide one, so log it rather than dropping it silently —
            # this is the evidence needed when wiring STT later.
            logger.info(
                "unanswered query %r from %s (request_id=%s)",
                query,
                device_id,
                request.get("request_id"),
            )


remote_chat_brain = RemoteChatBrain()


async def handle_remote_chat_payload(device_id: str, payload: bytes) -> None:
    await remote_chat_brain.handle_remote_chat_event(device_id, payload)


async def handle_activity_log_payload(device_id: str, payload: bytes) -> None:
    await remote_chat_brain.handle_activity_log_event(device_id, payload)


__all__ = [
    "RemoteChatBrain",
    "handle_activity_log_payload",
    "handle_remote_chat_payload",
    "remote_chat_brain",
]
