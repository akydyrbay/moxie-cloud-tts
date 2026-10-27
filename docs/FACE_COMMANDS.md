# Moxie face commands

Change Moxie's face (eye colour, face tint, glasses, mustache, animal nose and
more) through this service's HTTP API. The asset names come from OpenMoxie
(`site/hive/content/data.py`), which uses the same mechanism: the robot config
field `child_pii.face_options`.

## How a change reaches Moxie

1. You send a face command below. The service checks it and saves it to
   `local/work/moxie_face.json`. The saved face survives a server restart.
2. **Nothing is sent to the robot yet.** Face options live in `child_pii`, and
   re-sending `child_pii` to a running Moxie crashes its brain (SIGABRT in
   `SetMentorName`, see [SPEAK_PUPPET_ANALYSIS.md](SPEAK_PUPPET_ANALYSIS.md)
   Bug A). The service only sends it at bootstrap.
3. **Reboot Moxie** (or let it drop and rejoin Wi-Fi). When it reconnects to the
   broker, the log shows `bootstrapping device ...` and Moxie receives the new
   face with its config.

Rebooting Moxie is the safe way to apply a face. Do not restart this server
while Moxie stays connected just to apply a face: it would re-bootstrap a live
session.

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
that OpenMoxie has not tested is listed in `untested`.

### Back to the default face

```bash
curl -s -X DELETE localhost:8000/tts/face
```

This saves an empty face list. On the next connect, Moxie drops any face
customization it had.

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

Then reboot Moxie. The new ID is saved and reused, so you only need this once.

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
