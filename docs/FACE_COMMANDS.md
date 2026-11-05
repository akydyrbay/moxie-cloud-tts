# Moxie face commands

Change Moxie's face (eye colour, face tint, glasses, mustache, animal nose and
more) through this service's HTTP API. The asset names come from OpenMoxie
(`site/hive/content/data.py`), which uses the same mechanism: the robot config
field `child_pii.face_options`.

## How a change reaches Moxie

1. You send a face command below. The service checks it and saves it to
   `local/work/moxie_face.json`. The saved face survives a server restart.
2. By default the service immediately publishes a **face-only config update**
   to each bootstrapped robot: `{"child_pii":{"face_options":[...]}}`.
   It does not send nickname, child ID, pairing, mode or speech settings, or
   issue wake/sleep commands. Moxie can be awake and in a conversation.
3. The saved face also goes into the normal bootstrap config when Moxie next
   connects. Devices whose bootstrap is pending (including observed
   disconnects) receive no live push. The face selection is global, like the
   existing config toggles; it applies to all eligible discovered robots.

The old restriction came from a **full identity** push that crashed the native
brain in `SetMentorName`; see [SPEAK_PUPPET_ANALYSIS.md](SPEAK_PUPPET_ANALYSIS.md),
Bug A. Toggle identity stripping remains in place. The dedicated face publisher
allows only `face_options` through. This narrower payload needs hardware
verification: a successful MQTT publish does not prove Unity accepted it or
that the firmware preserves its current activity. See the implementation
[plan and hardware checklist](FACE_LIVE_PLAN.md).

With `OPENMOXIE_ENABLED=true`, this service saves locally but does not publish
live config. The response says `OpenMoxie managed`; use OpenMoxie's face editor
to apply there. The local saved face is not synchronized into OpenMoxie's DB.

The server runs on `http://127.0.0.1:8000`. Every command is also available
in Swagger at `http://127.0.0.1:8000/docs`.

## Commands

### See the saved face and every asset you can choose

```bash
curl -s localhost:8000/tts/face
```

### Set a face

`POST /tts/face` replaces the **whole** face. Any layer you leave out goes back
to its default. Send at most one asset per layer.

```bash
curl -s -X POST localhost:8000/tts/face \
  -H 'Content-Type: application/json' \
  -d '{"face_options": ["MX_010_Eyes_Purple", "MX_120_Glasses_RedCat"]}'
```

The response lists the saved assets in `face_options`. Any asset from a layer
that OpenMoxie has not tested is listed in `untested`. For example:

```json
{
  "saved": true,
  "face_options": ["MX_010_Eyes_Purple"],
  "child_id_reset": false,
  "untested": [],
  "applies": "live update requested",
  "live": {
    "status": "published",
    "devices": ["d_your_robot"],
    "failed_devices": [],
    "deferred_devices": [],
    "reason": null
  },
  "note": "Saved and face-only update accepted for publication; robot display is not acknowledged. Failed/deferred devices receive the saved face on their next bootstrap."
}
```

`live.status` is `published`, `partial`, `failed` or `deferred`. `devices` lists
local MQTT publish acceptances, not robot acknowledgements. `failed_devices`
lists publish errors; `deferred_devices` lists known targets awaiting bootstrap.
With no eligible target, `reason` is `no_bootstrapped_devices`. Other deferred
reasons are `save_only`, `child_id_reset` and `openmoxie_owns_config`.
`GET /tts/face` returns the saved selection/catalog, not the robot's current
rendered face or a delivery receipt.

HTTP 200 means persistence succeeded, even if live publication failed. Check
`live.status`; you can repeat the same request to retry publication. A retry
is intentionally sent again even if the saved selection is unchanged.

### Save for the next robot connect only

```bash
curl -s -X POST localhost:8000/tts/face \
  -H 'Content-Type: application/json' \
  -d '{"face_options": ["MX_010_Eyes_Purple"], "apply_live": false}'
```

Then reboot Moxie to apply. Do not restart the server with Moxie connected
solely to apply a deferred face: that re-bootstraps a live session with full
identity. `apply_live=false` is also the fallback if firmware rejects live
face-only updates.

### Back to the default face

```bash
curl -s -X DELETE localhost:8000/tts/face
```

This saves an empty face list and requests a live reset. It also clears the
saved customization for the next connect. To defer the reset, use
`DELETE /tts/face?apply_live=false`.

### Emergency recovery

If Moxie's face looks corrupt or Moxie is unstable after a face change, reset
the face and give Moxie a new child ID. Moxie caches face textures by
`child_pii.id`, so a new ID throws that cache away. This is OpenMoxie's
"Reset Child ID" checkbox.

```bash
curl -s -X POST localhost:8000/tts/face \
  -H 'Content-Type: application/json' \
  -d '{"face_options": [], "reset_child_id": true}'
```

Then reboot Moxie. `reset_child_id=true` always defers this whole request,
even when `apply_live=true`: a new identity is never sent live. The ID is saved
and reused. Later ordinary face commands still send only face options live;
the pending ID takes effect on the next bootstrap.

### Errors

| Response | Meaning |
| --- | --- |
| `422 unknown face asset(s): ...` | Name not in the list below (check spelling and case). Nothing was saved. |
| `422 only one <Layer> asset at a time: ...` | Two assets from the same layer. Nothing was saved. |
| `500 could not save the face` | The face file could not be written. Nothing was changed. |

## Ready-made faces

Copy one of these `-d` bodies into the "Set a face" command above.

| Look | Body | Risk |
| --- | --- | --- |
| Purple eyes | `{"face_options": ["MX_010_Eyes_Purple"]}` | Tested |
| Teal face | `{"face_options": ["MX_020_Face_Colors_Teal"]}` | Tested |
| Gold eyes + pink face | `{"face_options": ["MX_010_Eyes_Gold", "MX_020_Face_Colors_Pink"]}` | Tested |
| Round glasses | `{"face_options": ["MX_120_Glasses_SmallRound"]}` | Untested |
| Cat-eye glasses + purple eyes | `{"face_options": ["MX_010_Eyes_Purple", "MX_120_Glasses_RedCat"]}` | Untested (glasses) |
| Cat | `{"face_options": ["MX_130_Nose_Cat"]}` | Untested |
| Dog | `{"face_options": ["MX_130_Nose_Dog"]}` | Untested |
| Pig | `{"face_options": ["MX_130_Nose_Pig"]}` | Untested |
| Handlebar mustache | `{"face_options": ["MX_090_Facial_Hair_BrownHandlebar"]}` | Untested |
| Clown | `{"face_options": ["MX_130_Nose_Clown", "MX_080_Head_Hair_RedShag"]}` | Untested |

There are **no full animal masks** in the catalog (no zebra or similar). The
closest things are the Cat, Dog and Pig noses.

## All assets

OpenMoxie's warning: aside from Eyes and Face_Colors, assets "have had little
testing, use more system resources, and may cause Moxie to be unstable". Some
assets were seen crashing Unity on Moxie. Try untested layers one at a time,
and use the default-face or recovery command if something goes wrong.

| Layer | Tested | Assets |
| --- | --- | --- |
| Eyes | Yes | `MX_010_Eyes_Brown`, `MX_010_Eyes_Gold`, `MX_010_Eyes_Grey`, `MX_010_Eyes_Hazel`, `MX_010_Eyes_LightBlue`, `MX_010_Eyes_Purple`, `MX_010_Eyes_Turquoise` |
| Face_Colors | Yes | `MX_020_Face_Colors_Green`, `MX_020_Face_Colors_Pink`, `MX_020_Face_Colors_Purple`, `MX_020_Face_Colors_Teal`, `MX_020_Face_Colors_Yellow` |
| Eye_Designs | No | `MX_030_Eye_Designs_BlueCircuits`, `MX_030_Eye_Designs_BlueClouds`, `MX_030_Eye_Designs_Circuits`, `MX_030_Eye_Designs_Clouds`, `MX_030_Eye_Designs_Gears`, `MX_030_Eye_Designs_GoldStars`, `MX_030_Eye_Designs_PurpleGears`, `MX_030_Eye_Designs_RedHearts`, `MX_030_Eye_Designs_Stars` |
| Face_Designs | No | `MX_040_Face_Designs_Candies`, `MX_040_Face_Designs_Flowers01`, `MX_040_Face_Designs_Hearts`, `MX_040_Face_Designs_Leaves01`, `MX_040_Face_Designs_Stars` |
| Eyelid_Designs | No | `MX_050_Eyelid_Designs_GreenEyeShadow`, `MX_050_Eyelid_Designs_PurpleEyeShadow`, `MX_050_Eyelid_Designs_RainbowStars`, `MX_050_Eyelid_Designs_RedEyeShadow`, `MX_050_Eyelid_Designs_SmokeyLashes` |
| Mouth | No | `MX_060_Mouth_BlackSmall`, `MX_060_Mouth_DarkRedMedium`, `MX_060_Mouth_PinkPointy`, `MX_060_Mouth_PurpleFull`, `MX_060_Mouth_RedMedium` |
| Head_Hair | No | `MX_080_Head_Hair_BlackBob`, `MX_080_Head_Hair_BlackCenter`, `MX_080_Head_Hair_PinkShag`, `MX_080_Head_Hair_RedShag` |
| Facial_Hair | No | `MX_090_Facial_Hair_BlackAngled`, `MX_090_Facial_Hair_BlackDali`, `MX_090_Facial_Hair_BrownHandlebar`, `MX_090_Facial_Hair_OrangeBatWing`, `MX_090_Facial_Hair_YellowUpturn` |
| Brows | No | `MX_100_Brows_BrownCut`, `MX_100_Brows_GreyShort`, `MX_100_Brows_Purple`, `MX_100_Brows_WhiteBushy`, `MX_100_Brows_YellowThin` |
| Glasses | No | `MX_120_Glasses_BlueHeart`, `MX_120_Glasses_GoldHalfRound`, `MX_120_Glasses_RedCat`, `MX_120_Glasses_RoundWhiteDot`, `MX_120_Glasses_SmallRound` |
| Nose | No | `MX_130_Nose_Cat`, `MX_130_Nose_Clown`, `MX_130_Nose_Dog`, `MX_130_Nose_Human01`, `MX_130_Nose_Pig` |

## Related robot settings

These already exist in `HARDCODED_DEVICE_CONFIG` (`src/config.py`). They are not
face commands.

- `file_sync_wait: "0"` is already set. It skips the long file-sync wait at boot.
- `default_loglevel` comes from `ROBOT_LOG_LEVEL`, which defaults to `info` so the
  robot's speech pipeline shows up in this server's logs. OpenMoxie uses
  `warning`, which it says lowers load and raises the frame rate. To use it, set
  `ROBOT_LOG_LEVEL=warning` in `.env` and reboot Moxie. You will lose most
  `robot-log` lines that are useful for debugging.
