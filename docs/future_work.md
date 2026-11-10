# Future work

Backlog from the project discussion. Model choices and experimental approaches
remain subject to benchmarking and testing on Moxie.

## Completed: live face updates

- Face changes can be requested while Moxie is awake, without rebooting.
- The operator confirmed that live changes work on their robot.
- This does not establish that every asset or combination is stable.
- See [face commands](capabilities.md#face-customization).

## 1. Face command validation

- Reject unexpected request fields to catch misspelled options.
- Block assets or combinations confirmed to cause failures, with clear errors.
- Consider an optional strict mode allowing only Eyes and Face_Colors.
- Preserve existing checks for unknown labels and conflicting assets per layer.
- Do not treat `tested: false` as proof that an asset is unsafe.

## 2. Recorded audio playback

- Add an API to upload/select and queue recorded Kazakh or Russian audio.
- Validate format and size/duration limits; convert to 24 kHz mono PCM16.
- Reuse the live playback path and its cancellation and turn-ownership checks.
- Optionally accept a transcript for approximate mouth-animation timings.
- Test repeated playback and interruptions on Moxie.

## 3. Consecutive behaviors

- Add a sequence API for actions such as wave → raise arms → spin.
- Support ordered steps, pauses, cancellation, and progress reporting.
- Investigate movement-completion feedback; otherwise use hardware-tested
  timings and clearly distinguish scheduled steps from confirmed completion.
- Explore combining recordings with timed gestures.
- Existing behavior requests run on separate eligible conversation turns;
  they do not provide a continuous choreography sequence.

## 4. Faster conversation responses

- Measure recognition, English translation, robot routing, reply generation,
  and synthesis separately, plus end-of-speech to first audible response.
- Benchmark typical and slow responses in both kk and ru.
- Prepare candidate replies from the original transcript while English
  translation and robot routing run in parallel.
- Use candidates only for the matching free-chat turn; discard stale results
  after interruptions, activity changes, or reconnects.
- Preserve native activities and reserve provider capacity for recognition.
- Explore sentence-by-sentence generation/synthesis only after verifying
  audio-chunk playback on Moxie. More workers alone do not shorten dependent stages.

## 5. Common answers and audio caching

- Create a reviewed library of common question variants and kk/ru answers.
- Start with explicit phrase matching; use normal generation when uncertain.
- Pre-generate audio and mouth timings for common answers and fixed service lines.
- Avoid fixed answers for context-dependent, personalized, or changing information.
- Invalidate recordings when answer text, voice, or audio settings change.
- Cache audio/timings, not complete robot responses; use fresh turn identifiers.
- Offline spoken-question matching still requires local speech recognition.

## 6. Model migration and offline operation

- Replace retiring chat/translation, recognition, and synthesis models before
  their shutdown dates; verify the current schedules before migration.
- Benchmark local models on the available two-GPU computer, recording exact
  GPU/VRAM specifications and kk/ru quality as well as latency.
- Replace providers gradually while preserving the existing robot protocol.
- Evaluate local recognition, chat, translation/localization, and synthesis;
  replacing only the chat model does not make the system offline.
- Keep a maintained cloud backend as an optional fallback during evaluation.
- Test startup, conversation, native activities, and recovery with internet
  disconnected but the local network available.

## 7. Realtime speech-to-speech investigation

- Compare speech-to-speech with the conventional local pipeline after benchmarking.
- Account for Moxie's English input requirement for onboard routing: free-chat
  generation already uses the original kk/ru transcript.
- Verify microphone availability during playback, streaming audio delivery,
  playback interruption, and handling of speaker echo.
- Preserve text/translation where native activities require it, and coordinate
  model responses with the robot's actual conversation turns.
- Do not assume a Realtime model alone enables simultaneous listening/speaking
  or removes all recognition and translation work from this integration.
