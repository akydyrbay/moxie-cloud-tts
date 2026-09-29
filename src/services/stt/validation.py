"""Conservative ASR checks; no language guess or short-answer blacklist."""

from array import array
import math
import re
import sys

LEGACY_PROMPT = (
    "The speaker uses Kazakh (қазақ тілі, Қазақстан) or Russian "
    "(русский язык). Preserve the spoken language and use Cyrillic."
)


class InvalidTranscript(ValueError):
    """A local, safe-to-log transcript validation reason."""


def _normalized(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.casefold()))


def validate_transcript(text: str, audio_ms: int) -> None:
    if not text or not any(c.isalnum() for c in text):
        raise InvalidTranscript("No speech recognized")
    if _normalized(LEGACY_PROMPT) in _normalized(text):
        raise InvalidTranscript("Transcription echoed provider instructions")
    if len(text) > 4000:
        raise InvalidTranscript("Transcript exceeds bounded input length")
    # Deliberately generous: reject only grossly impossible long output from
    # tiny audio. Short words are never rejected solely for their duration.
    if len(text.split()) > max(20, audio_ms / 1000 * 12):
        raise InvalidTranscript("Transcript length is implausible for audio duration")


def pcm_energy(pcm: bytes) -> tuple[float, int]:
    samples = array("h", pcm)
    if sys.byteorder != "little":
        samples.byteswap()
    rms = math.sqrt(sum(s * s for s in samples) / max(1, len(samples)))
    # Diagnostic only: no calibrated VAD threshold is claimed here.
    active_ms = sum(abs(s) >= 64 for s in samples) * 1000 // 16000
    return rms, active_ms
