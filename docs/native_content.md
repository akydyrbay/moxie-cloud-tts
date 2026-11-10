# Native content

Native content runs on Moxie's firmware. It owns its animations, questions,
choices and transitions. This agent recognizes answers and localizes speech;
it does not replace a running native activity with generated conversation.
Available activities and content IDs depend on the installed firmware/assets.

## What this integration supports

- Native `CloudTTSRequest` handling: English/Latin speech is localized to
  `DEFAULT_TTS_LANGUAGE`, synthesized and returned as `ROBOT_TTS_REQUEST` audio.
- Original event IDs, chunk numbers and command marks are preserved, including
  command-only lines and failure silence when synthesis cannot finish.
- Native answers use real recognition and the English bridge without requiring
  the free-chat wake word. Translation preserves yes/no, choices and ordering.
- Router requests receive FALLBACK without conversational generation while
  native content owns the turn. Free chat resumes on real remote-module reentry.

This is generic protocol support, not a verified launch/completion/stop contract
for every native activity. There is no native activity listing or launch HTTP
endpoint. Request an activity conversationally, answer its confirmation/choices,
and let the firmware select the content. Do not fabricate a remote turn to
force a launch.

## Content recorded in previous investigations

| Module/content | Evidence and limitations |
| --- | --- |
| `JOKE/123` | Recorded in October 1 and October 27 source logs, with native joke prompts and answers. This is the observed singular module ID; do not substitute `JOKES`. |
| `STORY/the_robot_olympics`, `STORY/dr_wilde_makes_a_speech` | Earlier investigations recorded selection and narration requests. October 14 included audible background music but failed narration synthesis; entry does not establish a successfully completed story. |
| `WAKEUP`, `EXIT`, `SESSIONDISMOUNT` | Recorded entrance, activity-confirmation and session-exit interactions. They are lifecycle modules, not standalone exercises. |
| `BO` | Recorded in native notifications; no standalone activity contract is established. |

The remaining lifecycle modules handled specially are `ENROLLCONVO`, `WELCOME`,
`SLEEP` and `TELEHEALTH`. Enrollment is configured to be skipped with
`max_enroll=0`. WAKEUP still uses the entrance trigger; there is no verified
server-side “mark WAKEUP passed” implementation.

## Firmware catalog reference

The source OpenMoxie checkout's `RECOMMENDABLE_MODULES` lists these 23 native
module IDs. This is the known catalog reference, **not runtime discovery or
proof that every item works on the connected robot**. Abbreviated IDs are kept
verbatim where the catalog does not expand their names.

| Category | Module IDs |
| --- | --- |
| Regulation | `AFFIRM`, `AB`, `BODYSCAN`, `BREATHINGSHAPES`, `GUIDEDVIS`, `AUDMED` |
| Movement | `ANIMALEXERCISE`, `DANCE` |
| Creativity | `COMPOSING`, `DRAW`, `STORYTELLING` |
| Playful games | `FACES`, `MENTORSAYS`, `SCAVENGERHUNT` |
| Puzzle games | `PASSWORDGAME` |
| Fun tidbits | `RDL`, `FF`, `JOKE`, `NONSENSE`, `WHIMSY` |
| Listening | `JUKEBOX`, `STORY` |
| Reading | `READ` |

OpenMoxie's example schedules also reference `DM` daily missions,
`EVENTSANDHOLIDAYS`, `TNT` and `SYSTEMSCHECK`. They are not in this agent's
provided schedule and have no verified end-to-end support here. Content IDs
and their availability must be checked on the target robot.

`OPENMOXIE_CHAT/default` and `/short` are the only remote module/content pairs
advertised by this agent. Other stock OpenMoxie remote modules such as
`OPENCONVO`, `SIMPLELINE`, `WAKEUP_LAUNCHER` and `MOXIE_GO` are not implemented
by this service. The agent's [scripted activities](capabilities.md#scripted-questions)
are also separate from firmware-native content.

## Handoff and failure rules

A story or joke request can first enter `EXIT` for confirmation. Hearing and
answering that native question is necessary; unrelated generated speech would
hide the choices while firmware continues underneath. A speakable native
notification/request cancels competing remote audio for the same event.
Animation-only notifications do not.

Native localization changes speech only. Keep choices, names, numbers,
negations, narrative order and command marks. Never shorten a story to the
free-chat word limit. The engine cannot retract already-published audio, and
`Waiting`/`MarkingUp` timeout recovery to `IDLE` is not successful completion.

For a new supported exercise, verify launch, localized prompts, short answers,
choices, narration, completion, return to chat and stop/reconnect recovery on
hardware. Mocked publication tests do not validate those physical outcomes.

Implementation: [native speech handler](../app/services/speech/synthesis/handler.py),
[localization context](../app/services/speech/localization_context.py), and
[remote routing](../app/services/conversation/remote_chat.py). Upstream background:
[content modules](../openmoxie/doc/ContentModules.md) and
[robot overview](../openmoxie/doc/MoxieOverview.md).
