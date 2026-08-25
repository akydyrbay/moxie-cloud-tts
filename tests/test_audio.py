"""WAV container correctness — the root cause of "only the first line plays".

A RIFF header that declares more audio than it carries (OpenAI's streaming
`0xFFFFFFFF` placeholder) makes Moxie believe it is speaking for ~24.8 hours,
so it never voices another telehealth line for the rest of the session. Nothing
published to the robot may claim a length it does not have.
"""

import io
import struct
import wave

from src.services.audio import is_wav, pcm_to_wav, repair_wav
from tests.conftest import FAKE_PCM, make_streaming_wav, make_wav


def declared_sizes(data: bytes) -> tuple[int, int]:
    """Return the (RIFF chunk size, data chunk size) fields of a WAV."""
    riff = struct.unpack("<I", data[4:8])[0]
    at = data.find(b"data")
    data_size = struct.unpack("<I", data[at + 4:at + 8])[0]
    return riff, data_size


def assert_sizes_truthful(wav: bytes) -> None:
    at = wav.find(b"data")
    riff, data_size = declared_sizes(wav)
    assert riff == len(wav) - 8, f"RIFF size {riff} != {len(wav) - 8}"
    assert data_size == len(wav) - (at + 8), (
        f"data size {data_size} != {len(wav) - (at + 8)}"
    )


def test_pcm_to_wav_declares_real_sizes() -> None:
    wav = pcm_to_wav(FAKE_PCM, 24000, 1)
    assert is_wav(wav)
    assert_sizes_truthful(wav)
    assert wav.endswith(FAKE_PCM)


def test_pcm_to_wav_is_readable_and_keeps_duration() -> None:
    wav = pcm_to_wav(FAKE_PCM, 24000, 1)
    with wave.open(io.BytesIO(wav), "rb") as reader:
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        assert reader.getframerate() == 24000
        assert reader.getnframes() == len(FAKE_PCM) // 2


def test_repair_wav_fixes_streaming_placeholder() -> None:
    broken = make_streaming_wav(FAKE_PCM)
    assert declared_sizes(broken) == (0xFFFFFFFF, 0xFFFFFFFF)

    fixed = repair_wav(broken)
    assert len(fixed) == len(broken)  # only the size fields change
    assert fixed[44:] == broken[44:]  # samples untouched
    assert_sizes_truthful(fixed)


def test_repair_wav_leaves_valid_wav_alone() -> None:
    good = make_wav()
    assert repair_wav(good) == good


def test_repair_wav_passes_through_raw_pcm() -> None:
    assert repair_wav(FAKE_PCM) == FAKE_PCM
    assert repair_wav(b"") == b""
    assert not is_wav(FAKE_PCM)


def test_repair_wav_clamps_truncated_data_chunk() -> None:
    """A short read must not leave the robot waiting for samples that never come."""
    full = pcm_to_wav(FAKE_PCM, 24000, 1)
    truncated = full[: len(full) - 500]
    fixed = repair_wav(truncated)
    assert_sizes_truthful(fixed)
