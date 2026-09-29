# Remote-chat microphone STT — implementation and verification

Updated: 2026-09-29. **Recognition is observed on hardware; the English input bridge is implemented and awaits robot acceptance.** See [localization repair](LOCALIZATION_FIXES.md).

## Historical recognition failure and protocol reference

The Sep 28 `log/debug.log` run reached a real `OPENMOXIE_CHAT/default` prompt at
16:34:44 (`f25e74d9-a0b0-40bf-bd6d-61746c0ffb12`). At 16:35:12 and 16:35:20,
chat listens `39e4e130-055f-4a10-9b0e-2052946e79ae` and
`5025db38-80ad-4167-9482-a782fb0f41b4` failed with code 66 and
“Robot transcription is not implemented”. The handler discarded all PCM.

The copied reference at
`/Users/bake.72/Desktop/hri-lab/openmoxie/site/hive/mqtt/zmq_stt_handler.py`
appends `audio_content` on **every** VAD frame, then wraps the resulting
incremental stream as 16 kHz mono PCM16 WAV on END_OF_SPEECH. It responds with
FINAL and error code 66 on provider failure. This is the basis for our format
and failure contract. The old log did not record raw audio or frame sizes;
it cannot independently prove the encoding on this specific firmware.

## Implemented behavior

`stt_request.py` now accumulates START/SPEECH/END per `(device_id, uuid)` and,
once chat is active, sends a WAV to `openai_stt.transcribe` off the asyncio loop.
Auto mode has no language hint or imperative prompt. The September 28 evening
capture echoed the former prompt as fake speech, so that prompt was removed.
`STT_INPUT_LANGUAGE` can explicitly select kk/ru/en for controlled recognition
tests. Auto-mode language metadata remains unknown. Original recognized text is
validated, retained server-side and translated to English for FINAL when the
default-enabled input bridge is active. The bridge never answers the question.
Recognition uses the
[OpenAI file-transcription documentation](https://developers.openai.com/api/docs/guides/speech-to-text).
The existing configured model remains `gpt-4o-transcribe`.

Each live utterance receives one FINAL with its original UUID and robot-clock
start/end timestamps. Empty/silent/invalid audio, an empty transcript, provider
errors, and timeouts receive the existing FINAL error 66 with a specific reason.
Errors never place an assistant apology in the human transcript field.

Limits and lifecycle:

- `OPENAI_STT_TIMEOUT_S=6`: total recognition plus input-translation wait after
  END; configurable up to 8 seconds. Both stages disable SDK retries. The input
  translator has a 2-second limit further bounded by the remaining total budget.
- 30 seconds of PCM per utterance; streams lacking END expire after 30 seconds.
- At most two pending streams per device and 16 globally.
- Duplicate END ignored while processing and after completion. A bounded
  1,024-entry, 120-second UUID cache also rejects late frames after reset/exit.
- Exit, broker disconnect/reconnect and service shutdown discard pending audio
  and cancel awaiting recognition. An already-running synchronous HTTP call may
  finish later, but it cannot publish a stale response.
- Frame metadata, transcripts and elapsed time are logged. OpenAI SDK debug
  request dumps are disabled so the entire microphone WAV is not logged.

The existing wake trigger remains limited to the entrance phase. Other onboard
modules still receive a completion error; translating their English-only answer
matching is separate work. Real `continue` and nonempty first `reprompt` speech
reach the LLM. The first-reprompt history check was corrected with this change.
Reply language is still the configured default (`kk`, or explicitly `ru`);
automatic reply-language switching remains M3.

## Verification

Automated checks cover:

- Original Kazakh/Russian preserved for GPT; faithful English FINAL by default,
  original UUID, one FINAL and valid times. Bridge-off A/B mode returns original text.
- Exact concatenated bytes and **16 kHz / mono / PCM16** WAV parameters, including
  START/END audio, missing START, and audio carried only on END.
- MQTT network-thread dispatch into ordered async audio accumulation.
- Wake shim preservation and no STT provider calls in onboard modules.
- Empty/silent/malformed input, provider failure, no recognized speech and next-listen recovery.
- Duplicate END before/after completion; total timeout with a late worker result.
- Exit/reset/disconnect during an API call; independent devices sharing a UUID.
- Buffer/count bounds and abandoned-session expiry without additional frames.
- Sep 28 phase/UUID replay through FINAL → simulated firmware brain turn →
  requested cloud TTS, including a first `reprompt` without notify history.

`tests/fixtures/stt_2026_09_28.json` preserves the original wake UUID, actual chat
prompt ID and two failed chat UUIDs. These were checked against `log/debug.log`.
Audio, transcripts and subsequent firmware turns in the regression are
explicitly synthetic; they were not present in the log.

Historical pre-bridge verification: **204 passed**. Current full suite: **322 passed**.
The new input bridge, prompt-echo, timeout and correlation suites use mocked providers.

Before the bridge repair, a live API check generated two synthetic utterances, resampled them to 16 kHz,
split them into microphone-like frames and called the actual STT handler.
MQTT publishing was captured in memory; no robot commands were sent.

| Language | Source | Recognized text | Handler elapsed |
| --- | --- | --- | --- |
| kk | Менің сүйікті жемісім — алма. | Менің сүйікті жемісім алма. | 1,952 ms |
| ru | Мой любимый фрукт — яблоко. | Мой любимый фрукт — яблоко. | 1,558 ms |

Both returned error code 0 with matching UUIDs. An initial no-context Kazakh
sample was rendered with Kyrgyz-like wording; the language context hint was
added before the successful run. Those runs used separately generated samples,
so this is **not** a controlled accuracy comparison or proof the ambiguity is
eliminated. An intermediate attempt timed out during synthetic TTS generation,
before STT ran. Provider latency and real microphone accuracy need measurement.
Local smoke script, PCM samples and final results are in ignored `local/work/`.

## Next operator run

1. Restart with `mise start`, keep puppet off, wake Moxie and say “Moxie, let's go”.
2. Confirm a real `OPENMOXIE_CHAT` prompt; wait for the opener to finish.
3. Speak a short Kazakh sentence, then a Russian sentence on a later listen.
4. Check `STT validated/bridge` records the original transcript and faithful
   English wire text. FINAL and the following real router turn must share the
   listen UUID; `original_recovered=True` confirms GPT receives original speech.
5. Check the reply answers your sentence, plays audibly, and another UUID/listen
   follows. Repeat five turns in each language; test a fixed Russian output
   session with `DEFAULT_TTS_LANGUAGE=ru` and a restart.
6. Check silence/unclear speech and a fresh reconnect recover. Preserve the log
   if `zmqSTT: UUID has gone too long`, forced-idle, repeated error 66, missing
   remote turns, or incorrect language recognition occurs.

Do not mark M2 hardware acceptance or the full conversation goal complete until
those checks pass. See the [plan](SPEAK_LIVE_PLAN.md) and
[README test instructions](../README.md#verify-microphone-transcription).
