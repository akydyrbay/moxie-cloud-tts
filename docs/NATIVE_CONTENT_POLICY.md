# Native content and free conversation — October 13, 2026

## Why the policy changed

The October 12 log shows GPT speech replacing native audio successfully, while
the underlying robot continued into `EXIT` and `STORY/the_robot_olympics`.
At 18:41:31 GPT asked a math question while native STORY asked the child to
choose a story. At 18:42:08 GPT said it had no screen while story visuals were
visible to the operator. Replacing audio cannot replace the native activity's
state, choices or animations.

The suppression setting, GPT replacement of native PCM, background-content
muting, and GPT generation inside native modules have been removed. No robot
bundles or puppet paths were changed. The operator's configured language is
preserved (`DEFAULT_TTS_LANGUAGE`, currently Russian in the repository).

## Ownership and audio

1. Real `OPENMOXIE_CHAT` turns generate short GPT replies with normal
   `GLOBAL_RESPONSE` priority, leaving native global commands available.
   Questions requesting stories/jokes reach the robot through the existing
   English input bridge; the robot owns native launch/confirmation decisions.
2. Remote text and audio belong to the live router event. Correlated
   `notify_source=REMOTE_RESPONSE` (or legacy `response_chunks`) records remote
   selection, but is NOT required before audio: October 12 prompt `f439b0c9`
   sent audio at 18:37:44 and notified at 18:37:45. Other turns notify first.
   Interim `[animation:curious]`, `[animation:thinking]`, `[silent:reprompt]`
   notifications and command-only audio requests do not select native speech.
3. A native speech notification or speakable CloudTTSRequest selects native ownership for
   that event. Pending GPT text/audio is discarded; a still-pending router
   request receives FALLBACK. Native PCM is never replaced with the GPT answer.
   Guards also discard a late TTS failure's silence after native takeover.
4. Native turns receive FALLBACK without conversational GPT generation. Each
   native CloudTTSRequest goes through localization and TTS, preserving its
   event, chunk, command marks and native actions. All story chunks are voiced.
   The canned fallback also has correlated REMOTECHAT audio available if the
   robot needs it, without starting a GPT conversation. It uses the same brief
   selection grace and native-takeover guards as remote chat; an explicit
   selection notification is not required before audio.
5. `REMOTE_SELECTION_GRACE_S=0.15` admits immediately arriving native selections
   before synthesis, within the existing 12-second reply budget. After that
   brief window, a live remote turn proceeds without waiting for a notify.
   Native selection is checked again throughout synthesis and immediately before
   publication. The firmware still performs final response arbitration; the
   server cannot retract audio already played if a later activity interrupts it.
6. Returning to a real OPENMOXIE_CHAT turn restores free chat. Delayed native
   notifications for older turns do not take over a newer chat turn. Native
   duplicate router deliveries cannot replay a selected fallback.
7. Reconnect/reset, actual wake/session boundaries, newer turns and speech-mode
   revisions still invalidate obsolete work. Selection state is bounded and
   expires; no HTTP speak path or synthetic router/STT turn is introduced.

## Listening and native answers

STT has distinct `chat` and `native` phases, both using real recognition and the
English input bridge. Chat/native transitions preserve in-flight microphone
audio and original/wire transcript correlation. WAKEUP keeps its existing
entrance shim. Sleep, dismount and puppet boundaries do not enable native STT.

Native answers do not require the wake word: the child can say “Иә”, “Да”, or
“Екінші ертегі”. The free-chat wake-word setting is unchanged. The input bridge
gets recent activity prompts and source/localized lines to preserve yes/no,
first/second choices, and map mentioned localized titles to their English names.
It must never infer a choice or answer a question on the child's behalf.
The context is captured when the utterance ends, before any recognition queue
wait, so a later module's prompt cannot reinterpret an already recorded answer.

The October 12 18:38:12 capacity error was separate from module arbitration:
two finished recordings were still being processed when a third UUID arrived.
There are now six bounded pending listens per device, at most two still
recording, and 16 globally. Recognition/bridging runs one at a time per device;
queue wait is included in the existing six-second total STT deadline. The
recording limit, expiry, UUID deduplication and global provider bounds remain.
Provider failures, overload beyond these bounds and invalid recordings can
still return error 66; the changes do not promise failure-free external APIs.

## Localization contract

Native localization receives the observed module/content, current prompt and
up to four recent source/localized lines. This bounded context is separate from
GPT conversation history, separated from trusted system instructions, included
in the translation cache key, and invalidated on module/reset/mode changes.

The system instructions ask for simple, natural child-friendly kk/ru, consistent
names and titles, and idiomatic phrasing. They preserve every question, answer
choice and its order, negation, number, story fact, setup/punchline and action
instruction. Translators must not answer the question, invent another activity,
replace a joke with an unrelated one, or summarize a story chunk. The free-chat
limit of three short sentences remains; native story chunks are not truncated
to fit that limit. Existing translation/TTS deadlines and failure responses remain.

Prompt design reference: [OpenAI prompt engineering](https://developers.openai.com/api/docs/guides/prompt-engineering).
Protocol reference: the local OpenMoxie `doc/RemoteModuleAPI.md`, especially
local/remote arbitration and notification of the selected response.

Instructions and context improve localization but do not prove translation
quality for every English pun or story. Review actual kk/ru output on the robot;
some language-dependent jokes may eventually need curated localized content.

## Verification and robot acceptance

Run `DEFAULT_TTS_LANGUAGE=kk mise test`. This test override matches the existing
Kazakh capture fixtures and does not change the operator's runtime language.
The native handoff tests explicitly exercise both kk and ru.

Local verification: **344 tests passed** with
`DEFAULT_TTS_LANGUAGE=kk .venv/bin/python -m pytest tests/ -q`;
`git diff --check` passed. Providers and MQTT were mocked; no new robot run
or live provider translation-quality assessment was performed.

`tests/test_native_content_handoff.py` replays October 12 event IDs and request
ordering with fake MQTT/providers. It covers native notify/TTS before remote
selection, selection during GPT generation or synthesis, late failures, native
choices without wake words, retained story chunks and localization context,
return to chat, remote notify after audio, interim animation notifications, selected fallback
audio, duplicate turns, and the three overlapping microphone UUIDs that caused
the observed capacity error. Older chat tests now emulate firmware selection
notifications; dedicated capture tests also cover notifications arriving after audio.

On the robot after restarting the service:

1. Wake and ask ordinary questions. Expect GLOBAL_RESPONSE and REMOTECHAT
   audio on the same event. REMOTE_RESPONSE notify may precede or follow audio.
   Thinking/curious animations must not cancel the spoken reply.
2. Ask for a joke/story in kk or ru. Expect the native launch/confirmation,
   `native owns turn ... GPT audio cancelled` when a GPT turn was pending, and
   a localized ROBOT audio response. There must be no competing GPT audio for
   the native-owned event.
3. Answer confirmation/choices without “Moxie”. Expect `STT FINAL ... phase=native`
   with faithful English, FALLBACK routing and another localized native line.
4. Let the activity continue. Speech and displayed activity should agree, and
   story chunks should retain their event/chunk IDs and command marks.
5. Finish/exit the activity through native controls and verify actual
   OPENMOXIE_CHAT reentry, a free-chat answer, and continued listening.
6. Check for translation failures, provider deadlines and
   capacity errors. These are separate failure categories, not proof that a
   successfully transcribed utterance was spoken.

Automated tests establish server-side routing/correlation, not physical playback,
firmware return-to-chat behavior or naturalness of every live translation.
