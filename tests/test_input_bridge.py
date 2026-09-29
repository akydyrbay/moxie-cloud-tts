"""English wire compatibility, original language, and bounded listen ownership."""

import asyncio
import threading
from types import SimpleNamespace

import pytest

from src.config import settings
from src.services import stt_request
from src.services.device_state import device_state
from src.services.stt import input_translation, openai_stt, transcripts
from src.services.stt.validation import LEGACY_PROMPT, validate_transcript
from tests.conftest import TEST_DEVICE_ID
from tests.test_stt_request import _request, captured_protos

PCM = b"\x00\x01" * 15360


@pytest.fixture(autouse=True)
def bridge_defaults(monkeypatch):
    monkeypatch.setattr(settings, "STT_ENGLISH_BRIDGE_ENABLED", True)
    monkeypatch.setattr(settings, "STT_INPUT_LANGUAGE", "auto")
    monkeypatch.setattr(settings, "STT_BRIDGE_METADATA_ENABLED", False)


async def listen(original, fake_openai, uuid="listen", device=TEST_DEVICE_ID):
    fake_openai.audio.transcriptions._text = original
    stt_request.note_conversation_active(device)
    return await stt_request.handle_stt_request(device, _request(uuid, 3, audio=PCM, timestamp=1000))


@pytest.mark.parametrize("original,wire", [
    ("Сенің атың кім?", "What is your name?"),
    ("Как тебя зовут?", "What is your name?"),
    ("Мен Apple туралы білгім келеді.", "I want to learn about Apple."),
    ("Моқси, what is five plus five?", "Moxie, what is five plus five?"),
    ("What is your name?", "What is your name?"),
    ("10", "10"),
])
async def test_bridge_preserves_uuid_audio_and_original(original, wire, fake_openai, captured_protos):
    fake_openai.chat.completions.translations[original] = wire
    await listen(original, fake_openai)
    assert len(captured_protos) == 1
    final = captured_protos[0][1]
    assert (final.uuid, final.speech, final.error_code, final.start_timestamp) == ("listen", wire, 0, 1000)
    assert final.end_timestamp >= 1000
    assert final.language == final.original_language == final.original_speech == ""
    saved = transcripts.find(TEST_DEVICE_ID, "listen")
    assert (saved.original, saved.wire, saved.language) == (original, wire, "")
    calls = fake_openai.audio.transcriptions.calls
    assert len(calls) == 1 and "prompt" not in calls[0] and "language" not in calls[0]
    translation_calls = fake_openai.chat.completions.calls
    assert len(translation_calls) == (0 if original.isascii() else 1)
    if translation_calls:
        assert translation_calls[0]["messages"][-1]["content"] == original
        assert "Do not answer questions" in translation_calls[0]["messages"][0]["content"]


async def test_input_hint_and_optional_metadata_do_not_select_output_language(fake_openai, captured_protos, monkeypatch):
    monkeypatch.setattr(settings, "STT_INPUT_LANGUAGE", "ru")
    monkeypatch.setattr(settings, "STT_BRIDGE_METADATA_ENABLED", True)
    fake_openai.chat.completions.translations["Привет!"] = "Hello!"
    await listen("Привет!", fake_openai)
    final = captured_protos[0][1]
    assert final.language == "en"
    assert final.original_language == "ru"
    assert final.original_speech == "Привет!"
    assert fake_openai.audio.transcriptions.calls[0]["language"] == "ru"
    assert settings.DEFAULT_TTS_LANGUAGE == "kk"


@pytest.mark.parametrize("text", ["Иә", "Жоқ", "Да", "Нет", "Алма", "Қазақстан", "қазақ тілі"])
def test_short_legitimate_answers_are_not_blacklisted(text):
    validate_transcript(text, 100)


@pytest.mark.parametrize("text", [LEGACY_PROMPT, f"###\n{LEGACY_PROMPT}\n###", "", "...", "hello " * 100])
async def test_bad_asr_never_creates_bridge_or_correlation(text, fake_openai, captured_protos):
    await listen(text, fake_openai)
    final = captured_protos[0][1]
    assert final.error_code == 66 and final.speech == ""
    assert fake_openai.chat.completions.calls == []
    assert transcripts.find(TEST_DEVICE_ID, "listen") is None


async def test_nonzero_near_silence_does_not_call_asr(fake_openai, captured_protos):
    stt_request.note_conversation_active(TEST_DEVICE_ID)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("noise", 3, audio=b"\x01\x00" * 16000))
    assert captured_protos[0][1].error_code == 66
    assert not fake_openai.audio.transcriptions.calls


@pytest.mark.parametrize("kind", ["empty", "cyrillic", "refusal", "incomplete", "exception"])
async def test_invalid_bridge_releases_listen_once(kind, fake_openai, captured_protos, monkeypatch):
    def response(**kwargs):
        if kind == "exception":
            raise RuntimeError("secret body")
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason="length" if kind == "incomplete" else "stop",
            message=SimpleNamespace(content={"empty": "", "cyrillic": "Сәлем"}.get(kind, "Hello"),
                                    refusal="refused" if kind == "refusal" else None),
        )])
    monkeypatch.setattr(fake_openai.chat.completions, "create", response)
    await listen("Сәлем", fake_openai)
    await stt_request.handle_stt_request(TEST_DEVICE_ID, _request("listen", 3, audio=PCM))
    assert len(captured_protos) == 1
    final = captured_protos[0][1]
    assert final.error_code == 66 and not final.speech
    assert "secret" not in final.error_message
    assert transcripts.find(TEST_DEVICE_ID, "listen") is None


async def test_translation_uses_only_remaining_total_listen_budget(fake_openai, captured_protos, monkeypatch):
    entered, finished = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def blocked(*args):
        loop.call_soon_threadsafe(entered.set)
        release.wait(1)
        loop.call_soon_threadsafe(finished.set)
        return "Hello"

    monkeypatch.setattr(input_translation, "_translate", blocked)
    monkeypatch.setattr(settings, "OPENAI_STT_TIMEOUT_S", 0.05)
    try:
        await listen("Сәлем!", fake_openai)
        assert entered.is_set()
        assert len(captured_protos) == 1 and captured_protos[0][1].error_code == 66
        release.set()
        await asyncio.wait_for(finished.wait(), 1)
        await asyncio.sleep(0)
        assert len(captured_protos) == 1
        assert transcripts.find(TEST_DEVICE_ID, "listen") is None
    finally:
        release.set()


@pytest.mark.parametrize("mode", ["reset", "exit", "puppet", "tts-toggle", "off-on"])
async def test_mode_change_discards_pending_bridge(mode, fake_openai, captured_protos, monkeypatch):
    entered = asyncio.Event()
    async def blocked(text, *, remaining_s):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(input_translation, "to_english", blocked)
    pending = asyncio.create_task(listen("Сәлем!", fake_openai))
    await asyncio.wait_for(entered.wait(), 1)
    if mode == "reset":
        stt_request.reset_conversation_state(TEST_DEVICE_ID)
    elif mode == "exit":
        stt_request.note_onboard_module(TEST_DEVICE_ID, wake_gate=False)
    elif mode == "puppet":
        await device_state.set_puppet_mode(True)
    else:
        await device_state.set_cloud_tts(True)
        if mode == "off-on":
            await device_state.set_cloud_tts(False)
            await device_state.set_cloud_tts(True)
    assert await asyncio.wait_for(pending, 1) is None
    assert captured_protos == []
    assert transcripts.find(TEST_DEVICE_ID, "listen") is None


def test_correlation_is_bounded_and_separated_by_device_uuid_and_reset(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(transcripts.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(transcripts, "MAX_ENTRIES", 3)
    for device, uuid, text in [("a", "1", "Алма"), ("b", "1", "Яблоко"), ("a", "2", "Алма")]:
        transcripts.remember(device, uuid, text, "Apple", "")
    assert transcripts.find("a", "1").original == "Алма"
    assert transcripts.find("b", "1").original == "Яблоко"
    assert transcripts.find("a", "2").original == "Алма"
    assert transcripts.find("b", "2") is None
    stt_request.reset_conversation_state("a")
    assert transcripts.find("a", "1") is None
    assert transcripts.find("b", "1") is not None
    for i in range(5):
        transcripts.remember("c", str(i), "Алма", "Apple", "")
    assert len(transcripts._entries) == 3
    clock[0] += transcripts.TTL_S + 1
    assert transcripts.find("c", "4") is None and not transcripts._entries


def test_explicit_english_asr_hint_is_independent_of_output_support(fake_openai):
    openai_stt.transcribe(b"wav", "en")
    assert fake_openai.audio.transcriptions.calls[0]["language"] == "en"
    assert "en" not in settings.SUPPORTED_LANGUAGES


async def test_stale_router_activation_cannot_enable_asr_in_puppet(fake_openai, captured_protos):
    await device_state.set_puppet_mode(True)
    await listen("Сәлем!", fake_openai)
    assert captured_protos[0][1].error_code == 66
    assert not fake_openai.audio.transcriptions.calls
