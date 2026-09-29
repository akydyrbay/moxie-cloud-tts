"""Replay Sep 23's captured speech and router sequence without a robot/provider.

Set MOXIE_REPLAY_LOG=log/debug.log to extract events directly from that capture.
The default fixture preserves its speech IDs/text/order without mutable logs.
Full inbound markup was truncated to 80 characters in the log; command marks
below are representative reconstruction, not a claim of a raw packet replay.
"""

import ast
import json
import logging
import os
from pathlib import Path
import re

from src.config import settings
from src.services import cloud_tts_request
from src.services.device_state import device_state
from src.services.devices import device_registry
from src.services.remote_chat import RemoteChatBrain
from tests.test_cloud_tts_request import _decode_response, _frame_request

DEVICE = "d_d05e3c66-dd29-4fe2-9758-7223e31511dc"


def captured_events():
    path = os.getenv("MOXIE_REPLAY_LOG")
    if not path:
        fixture = Path(__file__).parent / "fixtures" / "wakeup_2026_09_23.json"
        return json.loads(fixture.read_text())
    events = []
    for line in Path(path).read_text().splitlines():
        if "bootstrapping device" in line:
            assert "cloud_tts=1 moxie_mode=None" in line
            events.append({"kind": "bootstrap"})
        elif "CloudTTSRequest fields" in line:
            match = re.search(r"event_id=(.*?) chunk_num=(\d+).* speakable=(.*)$", line)
            assert match, line
            events.append({
                "kind": "tts", "event_id": ast.literal_eval(match[1]),
                "chunk_num": int(match[2]), "text": ast.literal_eval(match[3]),
            })
        elif "Rx remote-chat" in line and "backend='router'" in line:
            match = re.search(
                r"command=(.*?) module=([^/]*)/(\S*) event_id=(.*?) speech=(.*?) keys=",
                line,
            )
            assert match, line
            events.append({
                "kind": "router", "command": ast.literal_eval(match[1]),
                "module_id": match[2], "content_id": match[3],
                "event_id": ast.literal_eval(match[4]), "speech": ast.literal_eval(match[5]),
            })
        elif "activity-log" in line and "'action': 'SCHEDULED'" in line:
            events.append({"kind": "scheduled"})
    assert events, "No captured events found"
    return events


async def test_september_23_capture_translates_all_english_speech_slots(
    fake_openai, translated_lines, captured_publish, monkeypatch, caplog,
):
    brain = RemoteChatBrain()
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", True)
    monkeypatch.setattr(settings, "CLOUD_TTS_AUDIO_RAW_PCM", True)
    monkeypatch.setattr(settings, "CLOUD_TTS_EMIT_MARKS", True)
    pcm = b"\x00\x01" * 24000
    fake_openai.audio.speech._pcm = pcm
    await device_state.set_cloud_tts(True)
    caplog.set_level(logging.INFO)
    translated_ids = set()
    source_lines = set()
    total, silent = 0, 0
    for event in captured_events():
        captured_publish.clear()
        if event["kind"] == "bootstrap":
            device_registry.forget_bootstrap(DEVICE)
            await brain.ensure_device_bootstrapped(DEVICE)
            config = json.loads(captured_publish[0][1])
            assert config["settings"]["props"]["cloud_tts"] == "1"
            assert "moxie_mode" not in config
            continue
        if event["kind"] == "router":
            await brain.handle_router_request(DEVICE, event)
            assert all(t.endswith("/commands/remote_chat") for t, _ in captured_publish)
            if event["command"] == "notify":
                assert captured_publish == []
            continue
        if event["kind"] == "scheduled":
            await brain.handle_activity_log_event(DEVICE, json.dumps({
                "context": {"module_id": "OPENMOXIE_CHAT"},
                "mentor_behavior": {"module_id": "OPENMOXIE_CHAT", "action": "SCHEDULED"},
            }))
            assert brain.output_language(DEVICE) is None
            assert captured_publish == []
            continue

        total += 1
        text, event_id = event["text"], event["event_id"]
        chunk = event.get("chunk_num", 0)
        # The source contains only markup[:80]. Use explicit complete test marks
        # to verify that replacement never loses firmware command callbacks.
        markup = (
            '<mark name="cmd:replay,start"/>'
            f'<usel variant="2" genre="none">{text}</usel>'
            '<mark name="cmd:replay,end"/>'
        )
        calls_before = len(fake_openai.audio.speech.calls)
        summary = await cloud_tts_request.handle_payload(DEVICE, _frame_request(
            markup=markup, event_id=event_id, chunk_num=chunk,
        ))
        assert len(captured_publish) == 1
        topic, frame = captured_publish[0]
        assert topic == f"/devices/{DEVICE}/commands/zmq"
        audio = _decode_response(frame)
        assert (audio.event_id, audio.chunk_num) == (event_id, chunk)
        assert (audio.audio.sample_rate, audio.audio.channels) == (24000, 1)
        assert [m.value for m in audio.marks if m.type == "ssml"] == [
            "cmd:replay,start", "cmd:replay,end",
        ]
        if not text:
            silent += 1
            assert len(fake_openai.audio.speech.calls) == calls_before
            assert audio.audio.buffer == b"\0" * 9600
            continue
        assert len(fake_openai.audio.speech.calls) == calls_before + 1
        expected = translated_lines[text]
        call = fake_openai.audio.speech.calls[-1]
        assert call["input"] == expected
        assert call["response_format"] == "pcm"
        assert audio.audio.buffer == pcm
        translated_ids.add(event_id)
        source_lines.add(text)
        assert summary["language"] == "kk"
        assert f"spoke event_id={event_id} chunk={chunk} language=kk" in caplog.text
        assert f"requested_text={text!r} synthesis_text={expected!r}" in caplog.text
        assert any(m.type == "viseme" and m.value != "sil" for m in audio.marks)

    assert (total, silent) == (14, 4)
    assert len(translated_ids) == 10
    assert {c["messages"][-1]["content"] for c in fake_openai.chat.completions.calls} == source_lines
    assert len(fake_openai.chat.completions.calls) == len(source_lines)
    assert "synthesis failed" not in caplog.text
    assert "translation failed" not in caplog.text


async def test_september_25_all_tts_requests_translate_without_chat_entry(
    fake_openai, translated_lines, captured_publish, monkeypatch, caplog,
):
    """The latest capture's non-wake English must also become meaningful Kazakh."""
    fixture = Path(__file__).parent / "fixtures" / "tts_2026_09_25.json"
    events = json.loads(fixture.read_text())["events"]
    monkeypatch.setattr(settings, "DEFAULT_TTS_LANGUAGE", "kk")
    monkeypatch.setattr(settings, "CLOUD_TTS_TRANSLATION_ENABLED", True)
    caplog.set_level(logging.INFO)
    translated, silent = 0, 0
    for event in events:
        captured_publish.clear()
        text = event["text"]
        summary = await cloud_tts_request.handle_payload(DEVICE, _frame_request(
            markup=f'<mark name="cmd:original"/><usel>{text}</usel>',
            event_id=event["event_id"], chunk_num=event["chunk_num"],
        ))
        assert len(captured_publish) == 1
        audio = _decode_response(captured_publish[0][1])
        assert (audio.event_id, audio.chunk_num) == (event["event_id"], event["chunk_num"])
        assert [m.value for m in audio.marks if m.type == "ssml"] == ["cmd:original"]
        if text:
            translated += 1
            assert summary["language"] == "kk"
            assert fake_openai.audio.speech.calls[-1]["input"] == translated_lines[text]
            assert f"requested_text={text!r} synthesis_text={translated_lines[text]!r}" in caplog.text
        else:
            silent += 1
            assert summary["status"] == "published_silence"
    assert (translated, silent) == (10, 6)
    assert "translation failed" not in caplog.text
    assert "synthesis failed" not in caplog.text
