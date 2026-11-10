"""Translation at the robot's audio boundary: content, correlation and failures."""

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.speech.synthesis import handler as cloud_tts_request
from tests.test_cloud_tts_request import _decode_response, _frame_request


@pytest.fixture(autouse=True)
def configure_translation(monkeypatch):
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", True)


async def request(published, text, event_id="live-event", device="robot", chunk=2):
    return await cloud_tts_request.handle_payload(device, _frame_request(
        event_id=event_id, chunk_num=chunk,
        markup=f'<mark name="cmd:keep-this"/><usel>{text}</usel>',
    ))


@pytest.mark.parametrize("reply", ["", " ", "Good morning!", "Hello, Сәлем!", "<sleep>Сәлем!", "```Сәлем!```", None])
async def test_invalid_translation_completes_original_waiter_without_english(
    fake_openai, captured_publish, reply,
):
    fake_openai.chat.completions._reply = reply
    summary = await request(captured_publish, "Good morning.", "", chunk=7)
    assert summary["reason"] == "translation_failed"
    assert fake_openai.audio.speech.calls == []
    assert len(captured_publish) == 1
    audio = _decode_response(captured_publish[0][1])
    assert (audio.event_id, audio.chunk_num) == ("", 7)
    assert audio.audio.buffer == b"\0" * 9600
    assert [m.value for m in audio.marks if m.type == "ssml"] == ["cmd:keep-this"]


@pytest.mark.parametrize("failure", ["exception", "truncated", "refusal", "no-choice"])
async def test_provider_failure_is_not_cached_and_next_request_can_recover(
    fake_openai, captured_publish, monkeypatch, failure,
):
    original = fake_openai.chat.completions.create

    def fail(**kwargs):
        if failure == "exception":
            raise RuntimeError("provider unavailable")
        message = SimpleNamespace(content="Қайырлы таң!", refusal="refused" if failure == "refusal" else None)
        choice = SimpleNamespace(message=message, finish_reason="length" if failure == "truncated" else "stop")
        return SimpleNamespace(choices=[] if failure == "no-choice" else [choice])

    monkeypatch.setattr(fake_openai.chat.completions, "create", fail)
    summary = await request(captured_publish, "Good morning.", "failed")
    assert summary["reason"] == "translation_failed"
    monkeypatch.setattr(fake_openai.chat.completions, "create", original)
    fake_openai.chat.completions._reply = "Қайырлы таң!"
    summary = await request(captured_publish, "Good morning.", "recovered")
    assert summary["status"] == "published"
    assert len(captured_publish) == 2
    assert len(fake_openai.audio.speech.calls) == 1
