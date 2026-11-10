"""Live output boundaries, full scripted turns, and cancellation races.

These tests assert wire contracts, never physical playback.
"""

import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.config import settings
from app.router.moxie import router
from app.services.actions import live_actions as actions_module
from app.services.speech.recognition import handler as stt_request
from app.services.actions.behaviors import Behavior, output_markup
from app.services.devices.state import device_state
from app.services.devices.registry import device_registry
from app.services.actions.live_actions import Question, live_actions
from app.services.speech.marks import command_marks
from app.services.conversation.remote_chat import RemoteChatBrain
from app.services.speech.recognition import transcripts
from tests.conftest import TEST_DEVICE_ID as DEVICE
from tests.test_remote_chat_audio import _router, _split
from tests.test_stt_request import _request
from app.protos.embodied.perception.audio.zmqSTT_pb2 import zmqSTTRequest


@pytest.fixture
def client(known_device, monkeypatch):
    device_registry.mark_bootstrapped(DEVICE)
    device_state._config["settings"]["props"]["cloud_tts"] = "1"
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


@pytest.fixture
async def brain(known_device, monkeypatch, captured_publish):
    monkeypatch.setattr(settings, "REMOTE_SELECTION_GRACE_S", 0)
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "ru")
    await device_state.set_cloud_tts(True)
    brain = RemoteChatBrain()
    await brain.ensure_device_bootstrapped(DEVICE)
    captured_publish.clear()
    return brain


def questions(language="ru"):
    return [
        Question(text="Сколько будет пять плюс пять?" if language == "ru" else "Беске бесті қосқанда қанша болады?", answers=["10"]),
        Question(text="Сколько будет два плюс три?" if language == "ru" else "Екіге үшті қосқанда қанша болады?", answers=["5"]),
    ]


async def turn(brain, event, speech="", command="continue", module="OPENMOXIE_CHAT", device=DEVICE):
    await brain.handle_remote_chat_event(device, _router(command, module, event, speech))


def test_markup_escapes_text_and_only_runs_allowlisted_command():
    text = '<mark name="cmd:evil"/> 5 < 10 & 20'
    markup = output_markup(text, Behavior.SPIN)
    marks = command_marks(markup, 1000)
    assert len(marks) == 1
    assert "bht_spin_360" in marks[0].value
    assert "&lt;mark" in markup
    assert "&amp;" in markup


def test_api_validation_and_mode_guards(client, captured_publish):
    for body in ({"text": " "}, {"text": "!"}, {"text": "а" * 1001},
                 {"text": "Привет", "behavior": "cmd:evil"},
                 {"text": "Привет", "language": "en"},
                 {"text": "Привет", "ttl_s": 0},
                 {"text": "Привет", "markup": "injected"}):
        assert client.post("/moxie/speak", json=body).status_code == 422
    device_state._config["moxie_mode"] = "TELEHEALTH"
    assert client.post("/moxie/speak", json={"text": "Привет"}).status_code == 409
    device_state._config.pop("moxie_mode")
    device_state._config["settings"]["props"]["cloud_tts"] = "0"
    assert client.post("/moxie/behavior", json={"behavior": Behavior.SPIN}).status_code == 409
    assert captured_publish == []


def test_api_activity_conflict_status_cancel_and_capacity(client):
    body = {"questions": [q.model_dump() for q in questions()], "language": "ru"}
    response = client.post("/moxie/activities", json=body)
    assert response.status_code == 202
    operation = response.json()["id"]
    assert client.post("/moxie/speak", json={"text": "Привет"}).status_code == 409
    assert client.post("/moxie/activities", json=body).status_code == 409
    assert client.get(f"/moxie/operations/{operation}").json()["question_count"] == 2
    assert client.delete(f"/moxie/operations/{operation}").json()["status"] == "cancelled"
    for _ in range(actions_module.MAX_DEVICE_QUEUE):
        assert client.post("/moxie/speak", json={"text": "Привет"}).status_code == 202
    assert client.post("/moxie/speak", json={"text": "Привет"}).status_code == 429
    assert client.get("/moxie/operations/missing").status_code == 404
    assert client.delete("/moxie/operations/missing").status_code == 404


@pytest.mark.parametrize("language,first_answer,second_answer,praise", [
    ("ru", "десять", "пять", "Молодец!"), ("kk", "он", "бес", "Жарайсың!"),
])
async def test_complete_activity_original_answers_and_behavior(
    brain, fake_openai, captured_publish, language, first_answer, second_answer, praise,
):
    op = live_actions.start(DEVICE, questions(language), language, behavior=Behavior.ARMS_OUT)
    assert not live_actions.awaiting_answer(DEVICE)
    await turn(brain, "ask", command="prompt")
    assert live_actions.awaiting_answer(DEVICE)
    transcripts.remember(DEVICE, "answer1", first_answer, "ten", language)
    await turn(brain, "answer1", "ten")
    status = live_actions.get(op["id"])
    assert status["question_index"] == 1
    await turn(brain, "answer1", "ten")
    assert live_actions.get(op["id"])["question_index"] == 1
    await turn(brain, "answer2", second_answer)
    assert live_actions.get(op["id"])["status"] == "completed"
    assert not live_actions.awaiting_answer(DEVICE)
    chats, audio = _split(captured_publish)
    assert len(audio) == len(chats) == 3
    assert chats[1]["output"]["text"].startswith(praise)
    assert questions(language)[1].text in chats[1]["output"]["text"]
    assert "bht_arms_out" in chats[1]["output"]["markup"]
    assert any("bht_arms_out" in m.value for m in audio[1].marks)
    assert fake_openai.chat.completions.calls == []
    await turn(brain, "after-completion", command="reprompt")
    assert _split(captured_publish)[0][-1]["output"]["text"] == ""
    await turn(brain, "chat-again", "Мокси, привет!")
    assert len(fake_openai.chat.completions.calls) == 1


async def test_wrong_answer_silence_and_stop_are_bounded(brain, fake_openai, captured_publish):
    op = live_actions.start(DEVICE, questions(), "ru", max_attempts=2)
    await turn(brain, "ask")
    await turn(brain, "wrong", "10 или 11")
    assert live_actions.get(op["id"])["question_index"] == 0
    await turn(brain, "silence", command="reprompt")
    status = live_actions.get(op["id"])
    assert status["question_index"] == 1 and status["attempts"] == 0
    assert "Ответ: 10." in _split(captured_publish)[0][-1]["output"]["text"]
    await turn(brain, "stop", "стоп")
    assert live_actions.get(op["id"])["status"] == "cancelled"


@pytest.mark.parametrize("interruption", ["cancel", "native", "reset", "mode", "expire"])
async def test_inflight_script_cannot_publish_after_invalidation(
    brain, fake_openai, captured_publish, monkeypatch, interruption,
):
    from app.services.speech import output as speak_service
    op = live_actions.start(DEVICE, questions(), "ru")
    started, release = asyncio.Event(), asyncio.Event()
    original = speak_service._synthesize

    async def blocked(*args, **kwargs):
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(speak_service, "_synthesize", blocked)
    pending = asyncio.create_task(turn(brain, "blocked"))
    await asyncio.wait_for(started.wait(), 1)
    if interruption == "cancel":
        live_actions.cancel(op["id"])
    elif interruption == "native":
        brain.observe_native_audio(DEVICE, "blocked")
    elif interruption == "reset":
        brain.reset_device(DEVICE)
    elif interruption == "mode":
        await device_state.set_puppet_mode(True)
    else:
        live_actions._operations[op["id"]].expires = 0
    release.set()
    await pending
    audio = _split(captured_publish)[1]
    if interruption in {"cancel", "expire"}:
        assert len(audio) == 1
        assert not any(m.type == "ssml" or m.type == "word" for m in audio[0].marks)
        assert not any(audio[0].audio.buffer)  # Correlated silence releases the waiter.
    else:
        assert audio == []
    assert live_actions.get(op["id"])["question_index"] == 0
    assert not live_actions.awaiting_answer(DEVICE)


async def test_failed_question_does_not_advance_activity(brain, fake_openai, captured_publish, monkeypatch):
    from app.services.speech import output as speak_service

    async def fail(*args, **kwargs):
        raise RuntimeError("fake synthesis failure")

    monkeypatch.setattr(speak_service, "_synthesize", fail)
    op = live_actions.start(DEVICE, questions(), "ru")
    await turn(brain, "failed")
    status = live_actions.get(op["id"])
    assert status["status"] == "failed" and status["question_index"] == 0
    assert not live_actions.awaiting_answer(DEVICE)


async def test_short_answer_wake_word_exception_is_activity_scoped(brain, fake_openai, captured_publish, monkeypatch):
    monkeypatch.setattr(settings, "STT_WAKE_WORD_REQUIRED", True)
    fake_openai.audio.transcriptions._text = "десять"
    fake_openai.chat.completions.translations["десять"] = "ten"
    op = live_actions.start(DEVICE, questions(), "ru")
    await turn(brain, "ask")

    async def listen(uuid):
        await stt_request.handle_stt_request(DEVICE, _request(uuid, zmqSTTRequest.START_OF_SPEECH))
        return await stt_request.handle_stt_request(
            DEVICE, _request(uuid, zmqSTTRequest.END_OF_SPEECH, audio=b"\x00\x01" * 1600),
        )

    result = await listen("short-answer")
    assert result["speech"] == "ten"
    live_actions.cancel(op["id"])
    result = await listen("ordinary-chat")
    assert result["speech"] == "" and result["error_code"] == 0


async def test_http_speech_reaches_live_turn_with_exact_text_and_marks(brain, client, fake_openai, captured_publish):
    text = 'Молодец! <mark name="cmd:evil"/>'
    response = client.post("/moxie/speak", json={"text": text, "behavior": Behavior.ARMS_OUT})
    assert response.status_code == 202
    op = response.json()["id"]
    assert captured_publish == []
    await turn(brain, "http-to-live")
    chats, audio = _split(captured_publish)
    assert chats[0]["output"]["text"] == text
    assert audio[0].event_id == "http-to-live"
    assert fake_openai.audio.speech.calls[0]["input"] == text
    assert len([m for m in audio[0].marks if m.type == "ssml"]) == 1
    assert client.get(f"/moxie/operations/{op}").json()["status"] == "audio_sent"
    assert not any(topic.endswith("/commands/telehealth") for topic, _ in captured_publish)


async def test_late_answer_cannot_be_graded_against_a_different_question(brain, fake_openai, captured_publish):
    op = live_actions.start(DEVICE, questions(), "ru")
    await turn(brain, "ask")
    key = live_actions.answer_key(DEVICE)
    transcripts.remember(DEVICE, "late", "пять", "five", "ru", activity_key=key)
    await turn(brain, "answer1", "десять")
    assert live_actions.get(op["id"])["question_index"] == 1
    await turn(brain, "late", "five")
    assert live_actions.get(op["id"])["status"] == "awaiting_answer"
    assert live_actions.get(op["id"])["question_index"] == 1
    assert _split(captured_publish)[0][-1]["output"]["text"] == ""
    assert fake_openai.chat.completions.calls == []


async def test_cancel_during_recognition_does_not_admit_unaddressed_answer(
    brain, fake_openai, captured_publish, monkeypatch,
):
    monkeypatch.setattr(settings, "STT_WAKE_WORD_REQUIRED", True)
    fake_openai.audio.transcriptions._text = "десять"
    fake_openai.chat.completions.translations["десять"] = "ten"
    op = live_actions.start(DEVICE, questions(), "ru")
    await turn(brain, "ask")
    started, release = asyncio.Event(), asyncio.Event()
    original = stt_request._recognize_and_bridge

    async def recognize(*args, **kwargs):
        assert kwargs["require_wake_word"] is False
        assert questions()[0].text in kwargs["context"]
        assert "10" not in kwargs["context"]  # Do not feed expected answers to translation.
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(stt_request, "_recognize_and_bridge", recognize)
    pending = asyncio.create_task(stt_request.handle_stt_request(
        DEVICE, _request("cancelled-listen", zmqSTTRequest.END_OF_SPEECH, audio=b"\x00\x01" * 1600),
    ))
    await asyncio.wait_for(started.wait(), 1)
    live_actions.cancel(op["id"])
    release.set()
    result = await pending
    assert result["error_code"] == 66 and result["speech"] == ""
    assert transcripts.find(DEVICE, "cancelled-listen") is None
