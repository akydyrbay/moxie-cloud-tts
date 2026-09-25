# Kazakh wake-up diagnosis — 2026-09-25

Analyzed `log/debug.log`, whose captured session is **2026-09-23**, not Sep 25.
The server was connected and handling cloud synthesis. The immediate failure
was English text reaching TTS unchanged; there was no successful chat handoff
or Kazakh speech request in this capture.

## Captured execution

| Local log time / line | Evidence | Meaning |
| --- | --- | --- |
| 14:23:52 / 144 | `bootstrapping ... (cloud_tts=1 moxie_mode=None)` | Autonomous cloud TTS enabled. |
| 14:24:44 / 210–212 | Request `296549b2-4b95-49b7-870b-09f30abeb6d3`, `speakable='Good morning.'`; API `input='Good morning.'`, `response_format='pcm'` | The period bypassed the old exclamation-only override. |
| 14:24:47 / 236 | Same event, `language=ru audio_bytes=129600` | Audio was synthesized, but labeling English as `ru` did not translate it. |
| 14:25:01 / 240 | Request `382cd197-227c-4484-95c0-5d823a60bc18`, “When you're ready to start the day…” | Another live wake-up speech slot bypassed the allowlist. |
| 14:26:01 / 275–278 | Wake-gate STT trigger, WAKEUP `start`, then Kazakh `GLOBAL_COMMAND` | Brain requested the greeting using the wake volley. |
| 14:26:02 / 280–281 | `OPENMOXIE_CHAT` activity marked `SCHEDULED`, then WAKEUP `notify` with Kazakh | Intended content was recorded; neither message proves chat entry or playback. |

There is **no `OPENMOXIE_CHAT` router `prompt`**, no chat completion request,
no Cyrillic `CloudTTSRequest`, and no wake-up override in that capture. Later
requests repeat the English day-start instruction around pickup/putdown lines.
Sleep commands at 14:25:56 and 14:26:23 and a native crash before reconnect are
also logged; this trace does not establish either as the cause of the language
failure. The proven speech slot is the existing wake-up Cloud TTS request.

## Blocking code and fix

- `src/services/cloud_tts_request.py`: `_WAKEUP_LOCAL_LINES` only matched
  `good morning!` and `excellent!`. Matching now normalizes boundary punctuation
  and recognizes the complete day-start phrases captured in this log. It sends
  `WAKEUP_TTS_TEXT` for the original live request, without manufacturing audio
  events. The override remains disabled in puppet mode.
- `src/config.py`: `DEFAULT_TTS_LANGUAGE` was `ru`; the system prompt followed
  the input language. Default is now `kk`, with a Kazakh system prompt. Only
  `kk` and `ru` are accepted as output language configuration.
- `src/services/remote_chat.py`: `continue` selected the reply language from
  incoming speech and an empty turn supplied `hello`/`hm`. Actual module entry
  now sets the session output language before responding. `start` and `prompt`
  use the opener immediately; replies and failure text use that same language.
  English assistant notifications cannot seed model history. A script check
  substitutes a known localized fallback for invalid-language model output.
- `src/services/llm/openai_chat.py`: instructions specify the **output** language
  without asserting that the child spoke it. Kazakh remains the output even
  when an incoming transcript is English or Russian.
- `src/services/language.py`: Russian Cyrillic is still detected as `ru` after
  changing the ambiguous-input default to `kk`. This function labels text;
  it does not translate it. The conservative output guard can reject valid
  Kazakh lacking distinctive letters or containing Latin names; a short known
  Kazakh fallback is intentional for this milestone, not full language detection.

After an actual chat router turn, `REMOTE_CHAT_LANGUAGE_GUARD_ENABLED=true`
also checks incoming TTS text against the session language. A mismatched line
is replaced in its own waiter. Notify/scheduling alone cannot activate this
guard. Reboot bootstrap and a new onboard router turn clear chat language state.

No changes to the TTS engine, raw PCM format, MQTT framing, generated protobufs,
puppet sequencing, identity bootstrap, schedule, or wake-gate STT transcript.
Outside actual chat, arbitrary onboard alerts and warmup content retain their
original text. This implements the one-line Path A milestone, not a complete
localization of firmware or an STT conversation loop.

## Verification

`mise test`: **166 passed**. Coverage includes punctuation variants, default
Kazakh entry, English/Russian input and history, model failure/wrong-language
output, Russian opt-in, module-state boundaries, puppet bypass, empty event IDs,
chunk correlation, PCM format, visemes, command marks, and silence on failure.

Also ran the replay directly against the original `log/debug.log`:

```bash
MOXIE_REPLAY_LOG=log/debug.log .venv/bin/python -m pytest tests/test_wakeup_log_replay.py -q --log-file=/tmp/moxie-kz-log-replay.log --log-file-level=INFO
```

Result: **1 passed**. All **14** captured TTS requests received exactly one
response. Four empty requests received 200 ms silence. All five recognized
wake-up requests used Kazakh synthesis input. The five remaining speech
requests (warmup and unrelated onboard alerts) retained their text.

The replay log includes:

```text
CloudTTSRequest wakeup override ... event_id='296549b2-4b95-49b7-870b-09f30abeb6d3' chunk=0 requested_text='Good morning.' synthesis_text='Сәлем! Қалайсың?'
spoke event_id=296549b2-4b95-49b7-870b-09f30abeb6d3 chunk=0 language=kk audio_bytes=48000 ...
```

Those bytes are **mock PCM**, not a live synthesis or playback claim. The
capture only logged the first 80 markup characters, so replay uses exact
captured speech/IDs and representative complete command marks. The original
log is left intact; the committed fixture keeps regression checks reproducible.

Restart `mise start`, reboot Moxie with puppet off, then wake it. The remaining
hardware check is hearing **“Сәлем! Қалайсың?”** with mouth movement after a
matching override/`spoke ... language=kk` pair in the new real log. `notify`
alone is still insufficient evidence.
