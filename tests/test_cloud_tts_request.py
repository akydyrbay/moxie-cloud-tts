"""Inbound CloudTTSRequest -> synthesis -> CloudTTSResponse.

Verifies the robot-triggered path at the message boundary: a framed
CloudTTSRequest is decoded, markup-stripped, synthesized, and published back to
the *requesting* device with its event_id/chunk_num echoed.
"""

import pytest

from src.protos.embodied.unity.CloudTTS_pb2 import (
    CloudTTSRequest,
    CloudTTSResponse,
)
from src.services import cloud_tts_request
from src.services.language import detect_language
from src.services.synthesis import openai_tts
from tests.conftest import TEST_DEVICE_ID

OTHER_DEVICE = "some-other-robot"


def _frame_request(**kwargs) -> bytes:  # noqa: ANN003
    req = CloudTTSRequest(**kwargs)
    return (
        CloudTTSRequest.DESCRIPTOR.full_name.encode()
        + b":"
        + req.SerializeToString()
    )


def _decode_response(payload: bytes) -> CloudTTSResponse:
    prefix = b"embodied.unity.CloudTTSResponse:"
    assert payload.startswith(prefix)
    resp = CloudTTSResponse()
    resp.ParseFromString(payload[len(prefix):])
    return resp


def test_parse_ignores_non_cloud_tts_request() -> None:
    assert cloud_tts_request.parse_cloud_tts_request(b"something.else:xxx") is None


def test_parse_decodes_framed_request() -> None:
    payload = _frame_request(markup="Привет", event_id="e1", chunk_num=2)
    req = cloud_tts_request.parse_cloud_tts_request(payload)
    assert req is not None
    assert req.markup == "Привет"
    assert req.event_id == "e1"
    assert req.chunk_num == 2


@pytest.mark.parametrize(
    ("markup", "expected"),
    [
        ("<mark name='x'/>Привет!", "Привет!"),
        ("Привет <break time='1s'/> как дела?", "Привет как дела?"),
        ("<mood:happy>Сәлем!</mood>", "Сәлем!"),
        ("   ", ""),
        ("<mark/>", ""),
    ],
)
def test_strip_markup_removes_tags(markup, expected) -> None:
    assert cloud_tts_request.strip_markup(markup) == expected


async def test_handle_payload_replies_to_requesting_device(
    fake_openai, captured_publish,
) -> None:
    """The response must go to the robot that asked, not a configured default."""
    payload = _frame_request(
        markup="Сәлем! Қалайсың?", event_id="evt-9", chunk_num=3, user_id="u1",
    )
    summary = await cloud_tts_request.handle_payload(OTHER_DEVICE, payload)

    assert summary is not None
    assert summary["status"] == "published"
    assert summary["event_id"] == "evt-9"
    assert summary["chunk_num"] == 3
    assert summary["language"] == "kk"
    assert summary["device_id"] == OTHER_DEVICE

    assert len(captured_publish) == 1
    topic, out = captured_publish[0]
    assert topic == f"/devices/{OTHER_DEVICE}/commands/zmq"
    resp = _decode_response(out)
    assert resp.event_id == "evt-9"
    assert resp.chunk_num == 3
    assert len(resp.audio.buffer) > 0
    # WAV is the default container.
    assert resp.audio.buffer.startswith(b"RIFF")


async def test_markup_tags_are_not_spoken(fake_openai, captured_publish) -> None:
    seen: list[str] = []
    original = openai_tts.openai_tts_engine.synthesize

    def _spy(text: str, language: str):  # noqa: ANN202
        seen.append(text)
        return original(text, language)

    openai_tts.openai_tts_engine.synthesize = _spy
    try:
        payload = _frame_request(
            markup="<mark name='viseme'/>Привет!", event_id="e", chunk_num=0,
        )
        await cloud_tts_request.handle_payload(TEST_DEVICE_ID, payload)
    finally:
        openai_tts.openai_tts_engine.synthesize = original

    assert seen == ["Привет!"]


async def test_empty_markup_still_answers_so_robot_does_not_stall(
    fake_openai, captured_publish,
) -> None:
    """Silence beats silence-on-the-wire: no response leaves the robot in MarkingUp."""
    payload = _frame_request(markup="   ", event_id="e-empty", chunk_num=0)
    summary = await cloud_tts_request.handle_payload(TEST_DEVICE_ID, payload)

    assert summary["status"] == "published_silence"
    assert len(captured_publish) == 1
    resp = _decode_response(captured_publish[0][1])
    assert resp.event_id == "e-empty"
    assert len(resp.audio.buffer) > 0


async def test_synthesis_failure_publishes_silence(
    fake_openai, captured_publish,
) -> None:
    original = openai_tts.openai_tts_engine.synthesize

    def _boom(text: str, language: str):  # noqa: ANN202
        msg = "openai exploded"
        raise RuntimeError(msg)

    openai_tts.openai_tts_engine.synthesize = _boom
    try:
        payload = _frame_request(markup="Привет", event_id="e-fail", chunk_num=1)
        summary = await cloud_tts_request.handle_payload(TEST_DEVICE_ID, payload)
    finally:
        openai_tts.openai_tts_engine.synthesize = original

    assert summary["status"] == "failed"
    assert len(captured_publish) == 1
    resp = _decode_response(captured_publish[0][1])
    assert resp.event_id == "e-fail"
    assert resp.chunk_num == 1
    assert len(resp.audio.buffer) > 0


async def test_handle_payload_returns_none_for_other_types(
    fake_openai, captured_publish,
) -> None:
    assert await cloud_tts_request.handle_payload(
        TEST_DEVICE_ID, b"embodied.power.X:zzz",
    ) is None
    assert captured_publish == []


def test_language_detection() -> None:
    assert detect_language("Привет, как дела") == "ru"
    assert detect_language("Сәлем, қалайсың") == "kk"
    # No Cyrillic is not evidence of either language -> configured default.
    assert detect_language("hello there") == "ru"
