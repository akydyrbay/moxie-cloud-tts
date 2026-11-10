"""WAV container correctness — the root cause of "only the first line plays".

A RIFF header that declares more audio than it carries (OpenAI's streaming
`0xFFFFFFFF` placeholder) makes Moxie believe it is speaking for ~24.8 hours,
so it never voices another telehealth line for the rest of the session. Nothing
published to the robot may claim a length it does not have.
"""

import struct

from app.services.speech.audio import repair_wav
from tests.conftest import FAKE_PCM, make_streaming_wav


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


def test_repair_wav_fixes_streaming_placeholder() -> None:
    broken = make_streaming_wav(FAKE_PCM)
    assert declared_sizes(broken) == (0xFFFFFFFF, 0xFFFFFFFF)

    fixed = repair_wav(broken)
    assert len(fixed) == len(broken)  # only the size fields change
    assert fixed[44:] == broken[44:]  # samples untouched
    assert_sizes_truthful(fixed)
