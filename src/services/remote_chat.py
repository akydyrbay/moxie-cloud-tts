"""Remote-chat brain — the missing piece that lets Moxie enter conversation.

Mirrors OpenMoxie's `moxie_remote_chat.py` / `moxie_server.py` *protocol*
(modules handshake, schedule query, RemoteChatRequest/Response, FALLBACK for
onboard modules) but drives replies through this repo's OpenAI chat stage.

Without this brain the robot loops `{"query":"modules"}` forever, never emits
a `CloudTTSRequest`, and enabling `cloud_tts` alone stalls it in MarkingUp.
"""

from __future__ import annotations

from collections.abc import Callable
from collections import OrderedDict
import asyncio
import copy
from dataclasses import dataclass, field
from html import escape
import json
import logging
import random
import re
import time
from typing import Any

from src.config import (
    DEFAULT_REMOTE_MODULES_INFO,
    DEFAULT_SCHEDULE,
    FALLBACK_LINES,
    REMOTE_CHAT_MODULE_IDS,
    REMOTE_CHAT_OPENERS,
    settings,
)
from src.protos.embodied.unity.CloudTTS_pb2 import REMOTECHAT_TTS_REQUEST
from src.services import localization_context, speak_service, translation
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.language import fallback_language, matches_output_language
from src.services.llm import openai_chat
from src.services.llm.reply_text import short_reply
from src.services.mqtt_service import mqtt_service
from src.services.provider_work import provider_pool
from src.services.stt import transcripts
from src.services.stt_request import (
    native_activity_active,
    note_conversation_active,
    note_native_activity,
    note_onboard_module,
    reset_conversation_state,
)
from src.services.telehealth import handle_activity_log_telehealth

logger = logging.getLogger(__name__)

_ACTION_TAG_RE = re.compile(r"<(?:exit|sleep|launch:[^<>]+)>")
_MAX_HISTORY = 20

# Let native entrances finish with their own response/actions, as in OpenMoxie.
# GLOBAL_COMMAND can discard the local transition along with "Excellent!":
# Sep 25 scheduled chat but never entered it; Sep 7 FALLBACK reached prompt.
# Translate English speech only in its live CloudTTSRequest.
_ENTRANCE_MODULES = frozenset({"WAKEUP", "ENROLLCONVO"})
_SESSION_BOUNDARIES = _ENTRANCE_MODULES | {"WELCOME", "SESSIONDISMOUNT", "SLEEP", "TELEHEALTH"}
_NON_SPEECH_NOTIFY = re.compile(r"(?:\[(?:animation|silent):[^\]]*\]\s*)+", re.IGNORECASE)


@dataclass
class _Selection:
    owner: str = ""
    native_request: tuple[str, str, str, str] | None = None
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    created: float = field(default_factory=time.monotonic)
    revision: int = field(default_factory=lambda: device_state.speech_revision)


def fallback_line(language: str) -> str:
    """Canned line in the child's language (never English)."""
    return FALLBACK_LINES.get(language) or FALLBACK_LINES[settings.DEFAULT_TTS_LANGUAGE]


def sleep_line(language: str) -> str:
    return {"kk": "Ұйықтайтын уақыт болды. Сау бол!", "ru": "Пора спать. Пока!"}[language]


def generation_error_line(language: str) -> str:
    return {
        "kk": "Жауап дайындау қиын болып тұр. Тағы бір рет сұрап көрші.",
        "ru": "Не получается подготовить ответ. Попробуй спросить ещё раз.",
    }[language]


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
        # OpenMoxie's Volley leaves this legacy flag false even for FALLBACK.
        # Arbitration priority is carried in both response action blocks.
        "fallback": False,
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

    Neither plain nor `<usel>` remote markup raises a CloudTTSRequest on this
    firmware (Sep 28 captures); its audio is pushed as REMOTECHAT instead.
    """
    return f'<usel variant="2" genre="none">{escape(text, quote=False)}</usel>'


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
        self.language = settings.DEFAULT_TTS_LANGUAGE
        self.active = False
        self._prior_history: list[dict[str, str]] = []
        self.records: OrderedDict[str, dict] = OrderedDict()
        self._seen_notify: OrderedDict[tuple, None] = OrderedDict()
        self.empty_reprompts = 0
        self.turn: object | None = None

    @property
    def history(self) -> list[dict[str, str]]:
        return self.context()

    def context(self, *, exclude: str = "") -> list[dict[str, str]]:
        history = copy.deepcopy(self._prior_history)
        for event_id, record in self.records.items():
            if event_id == exclude:
                continue
            if record["input"]:
                history.append({"role": "user", "content": record["input"]})
            # Text notify alone cannot establish that audio was even sent.
            if record["status"] in {"published", "text_only"} and record["output"]:
                history.append({"role": "assistant", "content": record["output"]})
        return history[-_MAX_HISTORY:]

    def begin(self, event_id: str, command: str, wire: str, original: str) -> bool:
        previous = self.records.get(event_id)
        if previous and (previous["command"], previous["wire"]) == (command, wire):
            return False
        self.records[event_id] = {
            "command": command, "wire": wire, "input": original,
            "output": "", "status": "pending", "notified": False,
        }
        self.records.move_to_end(event_id)
        while len(self.records) > _MAX_HISTORY:
            self.records.popitem(last=False)
        return True

    def finish(self, event_id: str, text: str, status: str, *, owner: dict) -> None:
        record = self.records.get(event_id)
        if record is owner:
            record.update(output=text, status=status)

    def reprompt(self) -> str:
        self.empty_reprompts += 1
        if self.empty_reprompts > 2:
            return ""  # Complete the live turn silently; no endless monologue.
        for message in reversed(self.context()):
            if message["role"] == "assistant":
                questions = re.findall(r"[^.!?]+\?", message["content"])
                if questions:
                    return short_reply(questions[-1].strip())
        return fallback_line(self.language)

    def add(self, role: str, message: str) -> None:
        if not message:
            return
        self._prior_history.append({"role": role, "content": message[:4000]})
        self._prior_history = self._prior_history[-_MAX_HISTORY:]

    def ingest_notify(self, request: dict) -> None:
        # `source_event_id` should name the volley whose output was notified;
        # compare it with the REMOTECHAT audio event_id on the next robot run.
        logger.info(
            "remote-chat notify module=%s source_event_id=%r notify_source=%r "
            "response_chunks=%.200r speech=%r",
            self.module_key, request.get("source_event_id", ""),
            request.get("notify_source"), request.get("response_chunks"),
            (request.get("speech") or "")[:80],
        )
        source = request.get("source_event_id") or ""
        if source in self.records:
            self.records[source]["notified"] = True
            return
        if self.active:
            logger.info("remote-chat ignored uncorrelated history source_event_id=%r", source)
            return
        for index, line in enumerate(request.get("extra_lines", [])):
            key = (source or request.get("event_id", ""), index, line.get("context_type"), line.get("text", ""))
            if key in self._seen_notify:
                continue
            self._seen_notify[key] = None
            while len(self._seen_notify) > 64:
                self._seen_notify.popitem(last=False)
            if line.get("context_type") == "input":
                self.add("user", line.get("text", ""))

    def reset(self) -> None:
        self._prior_history = []
        self.records.clear()
        self._seen_notify.clear()
        self.empty_reprompts = 0


class RemoteChatBrain:
    """Answers remote-chat / schedule / mentor queries for connected Moxies."""

    def __init__(self) -> None:
        self._sessions: dict[str, _ChatSession] = {}
        # Devices whose next live chat turn is answered with a `sleep` action.
        self._pending_sleep: set[str] = set()
        self._unsupported_queries: OrderedDict[tuple[str, str], float] = OrderedDict()
        self._selections: OrderedDict[tuple[str, str], _Selection] = OrderedDict()
        self._native_turns: dict[str, object] = {}

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
        self.reset_device(device_id)
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

    def _session_for(
        self, device_id: str, module_key: str, *, history_only: bool = False,
    ) -> _ChatSession:
        existing = self._sessions.get(device_id)
        if existing is not None:
            same_module = existing.module_key.split("/")[0] == module_key.split("/")[0]
            if existing.module_key == module_key or (history_only and same_module):
                # Notifications can omit content_id or use an alias. They do
                # not replace the active session selected by a router turn.
                return existing
        session = _ChatSession(module_key)
        if existing is not None and not history_only:
            self._clear_session(device_id)
        self._sessions[device_id] = session
        return session

    def _clear_session(self, device_id: str) -> None:
        session = self._sessions.pop(device_id, None)
        if session:
            for event_id in session.records:
                selection = self._selections.get((device_id, event_id))
                if selection:
                    selection.ready.set()

    def reset_device(self, device_id: str) -> None:
        """Invalidate microphone work and remote output at a session boundary."""
        self._clear_session(device_id)
        self._pending_sleep.discard(device_id)
        self._native_turns.pop(device_id, None)
        for key in list(self._selections):
            if key[0] == device_id:
                self._selections.pop(key).ready.set()
        localization_context.clear(device_id)
        reset_conversation_state(device_id)

    def _selection_for(self, device_id: str, event_id: str) -> _Selection:
        now = time.monotonic()
        for key, selection in list(self._selections.items()):
            if now - selection.created > 120 or selection.revision != device_state.speech_revision:
                self._selections.pop(key).ready.set()
        key = (device_id, event_id)
        if key not in self._selections:
            self._selections[key] = _Selection()
        while len(self._selections) > 256:
            _, retired = self._selections.popitem(last=False)
            retired.ready.set()
        return self._selections[key]

    def observe_native_audio(self, device_id: str, event_id: str) -> None:
        """A real ROBOT audio waiter proves native selection, even before notify."""
        if not event_id:
            return
        selection = self._selection_for(device_id, event_id)
        selection.owner = "native"
        selection.ready.set()
        session = self._sessions.get(device_id)
        if session and event_id in session.records:
            session.records[event_id]["status"] = "native_selected"
            # Keep recognition alive until the actual native module arrives.
            if event_id == next(reversed(session.records)):
                note_native_activity(device_id)
            logger.info("native owns turn device=%s event_id=%s; GPT audio cancelled",
                        device_id, event_id)

    def _observe_selection(self, device_id: str, request: dict) -> None:
        event_id = request.get("source_event_id") or ""
        if not event_id:
            return  # Uncorrelated or delayed notify cannot change ownership.
        if (request.get("notify_source") == "REMOTE_RESPONSE"
                or (not request.get("notify_source") and request.get("response_chunks"))):
            selection = self._selection_for(device_id, event_id)
            if selection.owner != "native":
                selection.owner = "remote"
                selection.ready.set()
            return
        speech = (request.get("speech") or "").strip()
        if not speech or _NON_SPEECH_NOTIFY.fullmatch(speech):
            # October 12: curious/thinking are interim signals for the SAME
            # UUID as the eventual GPT reply, not native speech selection.
            return
        module = request.get("module_id") or ""
        content = request.get("content_id") or ""
        session = self._sessions.get(device_id)
        current = bool(session and session.records and event_id == next(reversed(session.records)))
        # A local fallback within OPENMOXIE_CHAT can also beat a remote reply.
        self.observe_native_audio(device_id, event_id)
        if module and (current or native_activity_active(device_id)):
            localization_context.select(device_id, module, content)
            localization_context.observe_prompt(device_id, request.get("speech") or "")
            if module not in _SESSION_BOUNDARIES:
                note_native_activity(device_id)
            elif current:
                self._clear_session(device_id)
                note_onboard_module(device_id, wake_gate=module in _ENTRANCE_MODULES)

    def output_language(self, device_id: str) -> str | None:
        """Language of an entered chat module; notify alone is not entry."""
        session = self._sessions.get(device_id)
        return session.language if session and session.active else None

    def _send_remote_chat(
        self, device_id: str, response: dict, *,
        is_current: Callable[[], bool] | None = None,
    ) -> bool:
        if is_current is not None and not is_current():
            logger.info(
                "remote-chat discarded stale response device=%s event_id=%s",
                device_id, response.get("event_id"),
            )
            return False
        logger.info(
            "Tx remote_chat to %s event_id=%s output_type=%s text=%r",
            device_id,
            response.get("event_id"),
            response.get("response_action", {}).get("output_type"),
            (response.get("output") or {}).get("text", "")[:80],
        )
        mqtt_service.publish_command_json(device_id, "remote_chat", response)
        return True

    async def _remote_audio_enabled(self) -> bool:
        """Cloud TTS owns remote-module speech only outside TELEHEALTH."""
        return (
            await device_state.get_cloud_tts()
            and not await device_state.get_puppet_mode()
        )

    async def _voice_remote_output(
        self, device_id: str, request: dict, text: str | None, language: str,
        markup: str, *, is_current: Callable[[], bool],
        deadline: float | None = None,
        selection: _Selection | None = None,
    ) -> str:
        """Answer an accepted remote-module turn with REMOTECHAT audio.

        The robot raises no native CloudTTSRequest for remote text. Use its
        live router event_id and REMOTECHAT source; later Sep 28 operation
        confirmed audible replies to English input. Text notify is not proof
        of completed playback. `text=None` publishes correlated silence.
        """
        if not is_current():
            return "discarded"
        event_id = request.get("event_id") or ""
        if not event_id:
            logger.warning(
                "remote-chat audio skipped device=%s: router turn has no event_id",
                device_id,
            )
            return "failed"
        if selection is not None and not selection.owner:
            grace = settings.REMOTE_SELECTION_GRACE_S
            if deadline is not None:
                grace = min(grace, max(0, deadline - time.monotonic()))
            try:
                await asyncio.wait_for(selection.ready.wait(), timeout=grace)
            except TimeoutError:
                # Actual prompt f439b0c9: remote audio at 18:37:44, notify at
                # 18:37:45. Waiting for that notify would risk a silent opener.
                # Keep the live remote request and guard throughout synthesis.
                logger.debug("remote-chat selection pending device=%s event_id=%s; using live remote turn",
                             device_id, event_id)
        if not is_current() or (selection is not None and selection.owner == "native"):
            return "discarded"
        logger.info(
            "remote-chat audio device=%s event_id=%s chunk=0 source=REMOTECHAT "
            "language=%s synthesis_text=%r",
            device_id, event_id, language, text,
        )
        if not text or not any(char.isalnum() for char in text):
            speak_service.publish_silence(
                device_id, event_id, 0, REMOTECHAT_TTS_REQUEST, markup=markup,
            )
            return "published_silence"
        try:
            result = await speak_service.speak(
                text,
                language,
                event_id=event_id,
                chunk_num=0,
                request_source=REMOTECHAT_TTS_REQUEST,
                device_id=device_id,
                markup=markup,
                is_current=is_current,
                deadline=deadline,
            )
            return result["status"]
        except Exception as exc:
            # speak() has already published correlated silence.
            logger.info(
                "remote-chat audio outcome device=%s event_id=%s status=failed error=%s",
                device_id, event_id, type(exc).__name__,
            )
            return "failed"

    def _opener_for(self, content_id: str) -> str:
        openers = REMOTE_CHAT_OPENERS.get(content_id) or REMOTE_CHAT_OPENERS["default"]
        return random.choice(openers.split("|"))

    def _defer_entrance(self, device_id: str, request: dict) -> None:
        """Preserve the native transition; Cloud TTS translates its chosen line.

        Response actions execute only if their response wins arbitration.
        A high-priority speak-only greeting replaces the native entrance action
        as well as its text. Use the ordinary fallback if no local reply wins.
        """
        self._send_remote_chat(
            device_id,
            _build_response(
                request,
                text="I'm sorry. Can you repeat that?",
                markup=_speech_markup("I'm sorry. Can you repeat that?"),
                output_type="FALLBACK",
            ),
        )

    def request_sleep(self, device_id: str) -> None:
        """Queue a `sleep` action for this robot's next live chat turn.

        Firmware executes response actions only on the response to a turn it
        is still waiting for; a response on an already-answered event_id is
        dropped (Sep 23/25/29 captures). Chat state is left alone until the
        robot actually changes module, so listening keeps working meanwhile.
        """
        self._pending_sleep.add(device_id)
        logger.info("sleep queued for next chat turn device=%s", device_id)

    async def _remote_module_response(
        self, device_id: str, request: dict, module_id: str, content_id: str,
    ) -> None:
        cmd = request.get("command")
        module_key = f"{module_id}/{content_id}"
        session = self._session_for(device_id, module_key, history_only=cmd == "notify")
        if cmd == "notify":
            session.ingest_notify(request)
            return
        if cmd not in {"start", "prompt", "continue", "reprompt"}:
            return

        event_id = request.get("event_id") or ""
        wire_speech = (request.get("speech") or "").strip()
        original = transcripts.find(device_id, event_id) if wire_speech else None
        selection = self._selection_for(device_id, event_id)
        if selection.owner == "native":
            self._send_remote_chat(device_id, _build_response(request, text="", output_type="FALLBACK"))
            return
        if not session.active:
            logger.info(
                "remote-chat entered device=%s module=%s command=%s event_id=%s",
                device_id, module_key, cmd, event_id,
            )
        if not session.active or cmd in {"start", "prompt"}:
            session.language = settings.DEFAULT_TTS_LANGUAGE
            session.active = True
            logger.info(
                "remote-chat language device=%s module=%s command=%s language=%s",
                device_id, module_key, cmd, session.language,
            )
        if cmd in {"start", "prompt"}:
            # Suppress an exact redelivery without replaying audio or resetting
            # history. A genuinely new opener starts a fresh dialogue.
            prior = session.records.get(event_id)
            if prior and (prior["command"], prior["wire"]) == (cmd, wire_speech):
                return
            session.reset()
        localization_context.clear(device_id)
        self._native_turns.pop(device_id, None)
        note_conversation_active(device_id)

        speech = original.original if original else wire_speech
        if not session.begin(event_id, cmd, wire_speech, speech):
            logger.info("remote-chat ignored duplicate turn device=%s event_id=%s", device_id, event_id)
            return
        record = session.records[event_id]
        logger.info(
            "remote-chat input device=%s event_id=%s original_recovered=%s "
            "original_language=%s output_language=%s",
            device_id, event_id, original is not None,
            original.language or "unknown" if original else "unknown", session.language,
        )
        turn = object()
        session.turn = turn
        revision = device_state.speech_revision

        def is_current() -> bool:
            return (self._sessions.get(device_id) is session
                    and session.turn is turn
                    and device_state.speech_revision == revision
                    and selection.owner != "native")

        language = session.language
        started = time.monotonic()
        deadline = started + settings.REMOTE_REPLY_TIMEOUT_S
        voiced = await self._remote_audio_enabled()
        actions = []
        failed_translation = False
        if device_id in self._pending_sleep:
            self._pending_sleep.discard(device_id)
            logger.info("remote-chat sleep device=%s event_id=%s command=%s",
                        device_id, event_id, cmd)
            text = sleep_line(language)
            actions = [{"action": "sleep", "output_type": "GLOBAL_RESPONSE"}]
        elif cmd in {"start", "prompt"}:
            text = self._opener_for(content_id or "default")
            if voiced and settings.CLOUD_TTS_TRANSLATION_ENABLED and translation.needs_translation(text):
                try:
                    text = await translation.translate(text, language, deadline=deadline)
                except Exception as exc:
                    logger.warning(
                        "remote-chat opener translation failed device=%s event_id=%s error=%s",
                        device_id, event_id, type(exc).__name__,
                    )
                    text = generation_error_line(language)
                    failed_translation = True
        elif not speech and transcripts.was_ignored(device_id, event_id):
            # Speech without the wake word: complete the turn silently.
            text = ""
        elif not speech:
            # Silence is an event, not the robot's apology spoken by the child.
            text = session.reprompt()
        else:
            session.empty_reprompts = 0
            try:
                chat = await provider_pool.run(
                    openai_chat.respond, speech, language, session.context(exclude=event_id),
                    timeout=min(settings.OPENAI_CHAT_TIMEOUT_S, deadline - time.monotonic()),
                )
                text, actions = _strip_action_tags(chat.text)
                if not any(c.isalnum() for c in text):
                    raise ValueError("Chat response contains no spoken content")
                if not any(c.isalpha() for c in text):
                    text = ("Жауабы: " if language == "kk" else "Ответ: ") + text
                if not matches_output_language(text, language):
                    logger.warning(
                        "remote-chat repairing output language device=%s event_id=%s language=%s",
                        device_id, event_id, language,
                    )
                    # One faithful rewrite, bounded by the same reply budget.
                    text = await translation.translate(text, language, deadline=deadline)
                    actions = []
                    if not matches_output_language(text, language):
                        raise ValueError("Wrong-language reply after correction")
                bounded = short_reply(text)
                if bounded != text:
                    actions = []  # Do not execute actions attached to omitted speech.
                text = bounded
            except Exception as exc:
                logger.warning(
                    "remote-chat generation failed device=%s event_id=%s error=%s",
                    device_id, event_id, type(exc).__name__,
                )
                text, actions = generation_error_line(language), []

        markup = _speech_markup(text)
        response = _build_response(request, text=text, markup=markup, actions=actions or None)
        if not self._send_remote_chat(device_id, response, is_current=is_current):
            if selection.owner == "native" and device_state.speech_revision == revision:
                # The router may still be awaiting our side of arbitration.
                self._send_remote_chat(device_id, _build_response(request, text="", output_type="FALLBACK"))
            session.finish(event_id, text, "discarded", owner=record)
            return
        session.finish(event_id, text, "pending", owner=record)
        if voiced:
            status = await self._voice_remote_output(
                device_id, request, None if failed_translation else text, language, markup,
                is_current=is_current, deadline=deadline, selection=selection,
            )
        else:
            status = "text_only"
        session.finish(event_id, text, status, owner=record)
        logger.info(
            "remote-chat completed device=%s event_id=%s audio_status=%s elapsed_ms=%d",
            device_id, event_id, status, int((time.monotonic() - started) * 1000),
        )

    async def handle_router_request(self, device_id: str, request: dict) -> None:
        """Handle backend=router RemoteChatRequest (conversation turns)."""
        module_id = request.get("module_id") or ""
        content_id = request.get("content_id") or ""
        cmd = request.get("command")
        module_key = f"{module_id}/{content_id}"

        event_id = request.get("event_id") or ""

        if cmd == "notify":
            self._observe_selection(device_id, request)

        if self.is_remote_module(module_id, content_id):
            logger.debug("RCR:%s for remote module %s", cmd, module_key)
            # Notify is history-only and can fire while WAKEUP is still looping.
            # Marking conversation active on that notify stopped wake-gate STT
            # inject, so the robot re-prompted English and never reached prompt.
            await self._remote_module_response(
                device_id, request, module_id, content_id,
            )
            return

        # Onboard / unknown module: let its local response/actions win. Language
        # replacement belongs in Cloud TTS, where it cannot discard transitions.
        if cmd == "notify":
            # A delayed WAKEUP notify can follow the actual chat prompt. It
            # does not change module ownership or clear the output language.
            return
        if cmd not in {"start", "prompt", "continue", "reprompt"}:
            return
        selection = self._selection_for(device_id, event_id)
        fingerprint = (cmd, module_id, content_id, request.get("speech") or "")
        if event_id and selection.native_request == fingerprint:
            return
        selection.native_request = fingerprint
        if self.output_language(device_id) is not None:
            logger.info(
                "remote-chat left device=%s next_module=%s command=%s event_id=%s",
                device_id, module_key, cmd, event_id,
            )
        self._clear_session(device_id)
        self._native_turns.pop(device_id, None)
        if module_id in _SESSION_BOUNDARIES or not module_id:
            localization_context.clear(device_id)
            note_onboard_module(device_id, wake_gate=module_id in _ENTRANCE_MODULES)
        else:
            note_native_activity(device_id)
            localization_context.select(device_id, module_id, content_id)
            logger.info("native activity active device=%s module=%s; router response=FALLBACK",
                        device_id, module_key)
        if module_id in _ENTRANCE_MODULES:
            logger.info(
                "entrance deferred to local response device=%s module=%s "
                "command=%s event_id=%s output_type=FALLBACK",
                device_id, module_key, cmd, event_id,
            )
            self._defer_entrance(device_id, request)
            return
        if not module_id:
            logger.warning(
                "remote-chat missing module device=%s command=%s event_id=%s "
                "content_id=%r; wake trigger disabled",
                device_id, cmd, event_id, content_id,
            )
        logger.debug(
            "FALLBACK for non-remote module %s cmd=%s device=%s",
            module_key,
            cmd,
            device_id,
        )
        line = fallback_line(fallback_language(request.get("speech") or ""))
        token = object()
        self._native_turns[device_id] = token
        revision = device_state.speech_revision
        self._send_remote_chat(
            device_id,
            _build_response(
                request,
                text=line,
                markup=line,
                output_type="FALLBACK",
            ),
        )
        # Normally local content wins and its CloudTTSRequest owns the audio.
        # If the robot explicitly selects our canned fallback, it still needs
        # REMOTECHAT audio; never call conversational GPT inside a native module.
        if await self._remote_audio_enabled():
            await self._voice_remote_output(
                device_id, request, line, fallback_language(request.get("speech") or ""), line,
                is_current=lambda: (self._native_turns.get(device_id) is token
                                    and device_state.speech_revision == revision
                                    and selection.owner != "native"),
                deadline=time.monotonic() + settings.REMOTE_REPLY_TIMEOUT_S,
                selection=selection,
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
        logger.info(
            "activity-log device=%s subtopic=%s query=%r request_id=%r",
            device_id, subtopic, request.get("query"), request.get("request_id"),
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
            # Reference OpenMoxie omits license replies without Google keys;
            # it provides no remote_lines schema. Do not invent either reply.
            key = (device_id, str(query))
            now = time.monotonic()
            if now - self._unsupported_queries.get(key, float("-inf")) >= 60:
                self._unsupported_queries[key] = now
                self._unsupported_queries.move_to_end(key)
                while len(self._unsupported_queries) > 128:
                    self._unsupported_queries.popitem(last=False)
                logger.warning(
                    "protocol compatibility gap query=%r device=%s request_id=%r; "
                    "no verified response contract configured",
                    query, device_id, request.get("request_id"),
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
