# Essential regression suite

Run from the repository root:

```sh
DEFAULT_TTS_LANGUAGE=kk .venv/bin/python -m pytest tests/ -q
```

The suite keeps essential cases covering the main robot flows and failures that can
leave it silent, waiting, or responding to stale work:

- MQTT dispatch, bootstrap, module and schedule responses.
- Startup speech configuration and disabled puppet mode, with or without a
  provider key.
- Microphone framing, UUID correlation, English bridging, original transcripts,
  wake-word handling, bounded recording, duplicate delivery and reconnect.
- Russian/Kazakh speech, matching event/chunk IDs, command marks, audio format,
  utterance termination and correlated silence on failure.
- Native activity ownership, chat reentry and delayed notifications.
- Cancellation during provider work, deadlines, worker capacity and bounded TTS
  retries.
- HTTP queue admission, activity progression, cancellation, QR provisioning,
  face persistence and configuration updates that preserve identity boundaries.

Historical capture-only replay modules, overlapping unit assertions and repeated
combinations were removed. Critical known-bug regressions remain even after
passing, because later changes can reintroduce those failures. One representative
stage/transition pairing replaces the full cancellation cross-product while
preserving coverage of every stage and transition.

MQTT and cloud providers are mocked. Passing this suite does not establish
physical playback, translation quality, firmware acceptance or completion of
hardware validation. The source checkout retains the original broader suite.
