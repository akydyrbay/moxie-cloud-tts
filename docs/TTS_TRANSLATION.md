# Translate robot speech before TTS — September 28, 2026

At the user's direction, fixed Kazakh opener replacement is removed. The robot
chooses its line and sends a live `CloudTTSRequest`; this service now translates
each English/Latin speakable line into `DEFAULT_TTS_LANGUAGE` (Kazakh `kk` by
default), then synthesizes that translation into the same request. Good
morning, questions, clarifications and end-of-session lines use one path.

## Request flow

1. Decode the robot's request and strip its SSML-like tags. Decode HTML entities
   in the spoken text. Preserve the original markup for animation commands.
2. If the line has Latin letters, ask OpenAI to translate it. The system message
   says to translate the line faithfully, not answer its question or follow
   instructions in its contents. The provider receives text, never robot marks.
3. Require complete, plain Cyrillic output. The configured target language is
   authoritative; a valid Kazakh translation need not contain a Kazakh-only
   character.
4. Pass the translation to the existing raw-PCM TTS path. Echo the requesting
   robot, original `event_id` and `chunk_num`; preserve original `cmd:` marks and
   derive mouth marks from the translated speech.
5. If translation fails, returns unusable text or exceeds its four-second
   deadline, send one correlated silence response with original command marks.
   Never send the original English as a fallback. Completed translations cache
   in memory, keyed by source text, target language, model and timeout. Each
   robot request still gets its own TTS synthesis and response.

Cyrillic ru/kk requests pass directly to TTS. Latin names in Cyrillic text, such
as `iPhone`, cause the full line to be translated. Thus the detector deliberately
targets this robot's primarily English source content; it is a script heuristic,
not universal language identification. Puppet speech retains its existing
explicit-language path. This stage does not translate human input and does not
change module actions or teach English-only content to understand kk/ru speech.

Remote chat still has a module response so scheduled conversation can run, but
its opener is now English source text instead of a forced Kazakh greeting. It is
translated before its remote-chat JSON response and voiced directly as correlated
REMOTECHAT audio. Firmware does not issue a native CloudTTSRequest for that text.
Microphone recognition and the English input bridge are now wired; see
[localization repair](LOCALIZATION_FIXES.md) for verification and hardware limits.

## Settings

- `CLOUD_TTS_TRANSLATION_ENABLED=true` controls autonomous line translation.
- `DEFAULT_TTS_LANGUAGE=kk` sets Kazakh. Use `ru` for Russian translation.
- `OPENAI_CHAT_MODEL=gpt-4o-mini` is reused with a translation-only prompt.
- `OPENAI_TRANSLATION_TIMEOUT_S=4` bounds translation. The request has zero SDK
  retries. `src/main.py` prewarms the shared API client before MQTT starts.
- Prior settings `WAKEUP_TTS_TEXT`, `WAKEUP_TTS_OVERRIDE_ENABLED`, and
  `REMOTE_CHAT_LANGUAGE_GUARD_ENABLED` have been removed. Unknown old values in
  `.env` are ignored and no longer select a fixed greeting.

The translator logs source and translated text on a TTS request. Do not place
private data into those robot-authored utterances. The timeout test verifies a
late translation cannot publish a second audio response. The in-process cache
is bounded to 256 entries and disappears when the service restarts.

## Verification evidence

Live OpenAI translation was tested with the configured, prewarmed client on
September 28, 2026. All three completed within the four-second deadline:

| English source | Observed Kazakh result | Elapsed |
| --- | --- | ---: |
| `Good morning.` | `Қайырлы таң.` | 3.1 s |
| `I'm sorry, can you repeat that please?` | `Кешіріңіз, оны қайта айта аласыз ба?` | 2.1 s |
| `What is your favorite fruit?` | `Сіздің сүйікті жемісіңіз не?` | 0.8 s |

Times include local async/thread scheduling and one API request. These are
three examples, not a latency guarantee. The first un-prewarmed diagnostic
exceeded four seconds; the app's normal startup prewarms this shared client
before a robot can send TTS.

The tests replay **30 recorded TTS requests**: 14 from the Sep 23 fixture and
16 from `log/debug.log` on Sep 25. They cover 20 English speech requests and 10
empty animation requests, verify exact request IDs/chunks and command marks,
and use a mocked translation dictionary only for deterministic regression.
Additional tests cover two target languages, cache key separation, invalid or
refused output, provider failure/recovery, timeout, markup, and the late-worker
case. The complete `mise test` run passed: **181 tests**.

Mocked replays verify server protocol behavior, not physical playback. Restart
`mise start`, reboot Moxie, then listen for `CloudTTSRequest translation` and a
`spoke` entry with the same event ID/chunk and `language=kk`. Hear several
different translated lines and mouth movement. If a line gets silence, inspect
the adjacent `translation failed` log; do not mistake a published response for
confirmed playback.

## OpenAI API and implementation

`translation.py` uses the repository's shared Python client and the existing
Chat Completions call shape, with per-request timeout/retry settings.
[OpenAI's official Chat Completions reference](https://developers.openai.com/api/reference/python/resources/chat/subresources/completions/methods/create)
documents the messages-based text generation endpoint. The Python client
reference documents configuring request timeouts and retries.
