r"""WAV/PCM container helpers for audio published to the robot.

OpenAI's `/v1/audio/speech` *streams* its reply, so with `response_format="wav"`
it cannot know the length up front and writes the RIFF "size unknown"
placeholder `0xFFFFFFFF` into BOTH the `RIFF` chunk size and the `data` chunk
size. Verified on the wire (`log/debug.log.2026-08-19`, and reproduced against
the live API)::

    RIFF \xff\xff\xff\xff WAVE fmt ... data \xff\xff\xff\xff <samples>

A player that trusts that size schedules ~24.8 hours of 24 kHz mono 16-bit PCM.
That is a real defect and is why we never ask OpenAI for `wav`: we fetch `pcm`
and, when a container is wanted, build it here with truthful sizes.

It is **not** the cause of "only the first puppet line plays". After this
repair, both a truthful WAV and raw PCM still play line 1 and stay silent for
line 2 in the same TELEHEALTH session. See `PUPPET_SPEAK_SILENCE_ANALYSIS.md`.
"""

import io
import logging
import wave

logger = logging.getLogger(__name__)

# RIFF's "I do not know the length yet" placeholder, as emitted by streaming
# encoders. A player that trusts it schedules ~24.8 hours of 24 kHz audio.
STREAMING_SIZE_PLACEHOLDER = 0xFFFFFFFF

_RIFF_MAGIC = b"RIFF"
_WAVE_MAGIC = b"WAVE"
_RIFF_HEADER_BYTES = 12


def is_wav(data: bytes) -> bool:
    """True if `data` looks like a RIFF/WAVE container."""
    return (
        len(data) >= _RIFF_HEADER_BYTES
        and data[:4] == _RIFF_MAGIC
        and data[8:12] == _WAVE_MAGIC
    )


def pcm_to_wav(
    pcm: bytes,
    sample_rate: int,
    channels: int,
    sample_width: int = 2,
) -> bytes:
    """Wrap raw little-endian PCM in a WAV container with truthful chunk sizes.

    `wave` seeks back and patches the real `RIFF`/`data` sizes on close, which is
    exactly the guarantee the robot's player needs.
    """
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(channels)
        writer.setsampwidth(sample_width)
        writer.setframerate(sample_rate)
        writer.writeframes(pcm)
    return buf.getvalue()


def duration_ms(
    data: bytes,
    sample_rate: int,
    channels: int,
    sample_width: int = 2,
) -> int:
    """Playback length of a WAV or raw-PCM buffer, in milliseconds.

    Read from the container when there is one, so the answer stays right if the
    engine ever returns a rate other than the configured one. Returns 0 for a
    buffer that cannot be measured, which callers must treat as "no timings".
    """
    if is_wav(data):
        try:
            with wave.open(io.BytesIO(data), "rb") as reader:
                rate = reader.getframerate() or sample_rate
                return int(reader.getnframes() * 1000 / rate) if rate else 0
        except (wave.Error, EOFError):
            return 0

    frame_bytes = sample_width * channels
    if frame_bytes <= 0 or sample_rate <= 0:
        return 0
    return int(len(data) * 1000 / (frame_bytes * sample_rate))


def repair_wav(data: bytes) -> bytes:
    """Rewrite bogus RIFF chunk sizes so the declared length matches the bytes.

    Non-WAV payloads (raw PCM, anything unrecognised) are returned untouched, so
    this is safe to apply to every buffer on the way out. Chunks whose declared
    size is the streaming placeholder, or simply longer than the bytes actually
    present, are clamped to what is really there.
    """
    if not is_wav(data):
        return data

    buf = bytearray(data)
    repaired: list[str] = []

    pos = _RIFF_HEADER_BYTES
    while pos + 8 <= len(buf):
        chunk_id = bytes(buf[pos:pos + 4])
        declared = int.from_bytes(buf[pos + 4:pos + 8], "little")
        body = pos + 8
        available = len(buf) - body

        if declared > available:
            buf[pos + 4:pos + 8] = available.to_bytes(4, "little")
            repaired.append(
                f"{chunk_id.decode('ascii', 'replace')}={declared}->{available}",
            )
            declared = available

        if chunk_id == b"data":
            # Everything the player needs is in place; trailing chunks (LIST,
            # id3, ...) do not affect playback length.
            break
        # Chunk bodies are word-aligned.
        pos = body + declared + (declared & 1)

    riff_size = len(buf) - 8
    if int.from_bytes(buf[4:8], "little") != riff_size:
        repaired.append(f"RIFF->{riff_size}")
        buf[4:8] = riff_size.to_bytes(4, "little")

    if repaired:
        logger.warning(
            "repaired WAV chunk sizes before publishing (%s); a streaming "
            "0x%X size makes Moxie treat the clip as ~24 hours long and go "
            "mute for the rest of the session",
            ", ".join(repaired),
            STREAMING_SIZE_PLACEHOLDER,
        )
    return bytes(buf)


__all__ = [
    "STREAMING_SIZE_PLACEHOLDER",
    "duration_ms",
    "is_wav",
    "pcm_to_wav",
    "repair_wav",
]
