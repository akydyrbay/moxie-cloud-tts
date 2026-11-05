# Live face updates

## Findings

- `POST /tts/face` and `DELETE /tts/face` currently validate and save a global
  face selection. `DeviceState` includes it in the next bootstrap config only.
- The previous restriction protects against the full-config crash documented
  in `SPEAK_PUPPET_ANALYSIS.md`, Bug A: nickname/pairing identity triggers
  `SetMentorName`. Toggle updates must retain their identity stripping.
- OpenMoxie's `face_edit` calls `handle_config_updated`, which sends config to
  online robots. Its documented face field is `child_pii.face_options`; there
  is no separate face command in the checked-in protocol definitions.
- The firmware source is unavailable here. A face-only config update is a
  hardware-testable inference, not proof that every firmware version accepts
  partial child records or refreshes Unity without restarting.

## Implementation plan

1. Preserve catalog validation, atomic file replacement, startup restore and
   full-face replacement semantics (an empty list resets the face).
2. Default POST and DELETE to a live update after persistence. Serialize save,
   in-memory update and publication to preserve the order of face requests.
3. Use a dedicated face publisher with exactly
   `{"child_pii":{"face_options":[...]}}`. Never send nickname, child ID,
   pairing status, mode, settings, wake/sleep commands or synthetic chat turns.
   Do not change speech revision or conversation state.
4. Target bootstrapped robots using the existing global config target list.
   Defer unknown/disconnected/unbootstrapped robots to normal bootstrap.
   Respect `OPENMOXIE_ENABLED` config ownership. Preserve the saved selection
   on MQTT failure and distinguish local publish acceptance from robot display.
5. Add `apply_live=false` for save-only operation. `reset_child_id=true` always
   defers the entire request to bootstrap; subsequent live face updates never
   send that saved identity. Keep child-ID recovery available without using it
   for ordinary live changes.
6. Cover live set/reset, exact wire payload, persistence/reconnect, save-only,
   ID recovery, mode/conversation preservation, disconnected devices, external
   config ownership, and save/publish failures. Update API docs and run the
   full mocked suite plus `git diff --check`.

## Hardware acceptance (operator)

With the updated server running and Moxie bootstrapped and awake:

1. While chatting, POST purple eyes. Check that the API reports a published
   update, eyes change without reboot, and the next spoken exchange works.
2. POST gold eyes and teal face. Then POST only purple eyes and confirm that
   the omitted face tint returns to default. Repeat a request to check retry.
3. DELETE the face and confirm that all customization clears while awake.
4. Repeat during a native activity and verify its dialogue still progresses.
5. Save with `apply_live=false`: the visible face should stay unchanged until
   Moxie reboots. Verify the saved selection also survives a server restart.
6. Check logs for `SetMentorName`, SIGABRT, Unity errors, unexpected sleep/mode
   transitions or a reconnect after a live update. Record the request, response
   and log timestamps if anything fails. Publication alone is not acceptance.

Start with Eyes and Face_Colors. Other catalog layers have the existing
OpenMoxie stability caveat. If partial updates are rejected or still trigger
the identity crash, use save-only mode pending firmware/log investigation;
do not fall back to sending full identity live.

Reference: https://github.com/jbeghtol/openmoxie/blob/main/doc/MoxieOverview.md
