"""Moxie face customization (OpenMoxie `child_pii.face_options`).

The robot draws face assets named by these labels, at most one per layer.
OpenMoxie (site/hive/content/data.py, MOXIE_CUSTOMIZATIONS) is the source of
the catalog; its face page warns that only Eyes and Face_Colors are well
tested, and that other layers may make Moxie unstable or crash Unity.

Face options live inside `child_pii`, which must never be re-pushed mid-session
(docs/SPEAK_PUPPET_ANALYSIS.md Bug A). A change is therefore only stored here
and in `device_state`; the robot receives it with the full bootstrap config
the next time it connects to the broker.
"""

import json
import logging
import uuid
from pathlib import Path

from src.config import settings

logger = logging.getLogger(__name__)

# Layer -> asset labels, in OpenMoxie's catalog order.
FACE_LAYERS: dict[str, tuple[str, ...]] = {
    "Eyes": (
        "MX_010_Eyes_Brown", "MX_010_Eyes_Gold", "MX_010_Eyes_Grey",
        "MX_010_Eyes_Hazel", "MX_010_Eyes_LightBlue", "MX_010_Eyes_Purple",
        "MX_010_Eyes_Turquoise",
    ),
    "Face_Colors": (
        "MX_020_Face_Colors_Green", "MX_020_Face_Colors_Pink",
        "MX_020_Face_Colors_Purple", "MX_020_Face_Colors_Teal",
        "MX_020_Face_Colors_Yellow",
    ),
    "Eye_Designs": (
        "MX_030_Eye_Designs_BlueCircuits", "MX_030_Eye_Designs_BlueClouds",
        "MX_030_Eye_Designs_Circuits", "MX_030_Eye_Designs_Clouds",
        "MX_030_Eye_Designs_Gears", "MX_030_Eye_Designs_GoldStars",
        "MX_030_Eye_Designs_PurpleGears", "MX_030_Eye_Designs_RedHearts",
        "MX_030_Eye_Designs_Stars",
    ),
    "Face_Designs": (
        "MX_040_Face_Designs_Candies", "MX_040_Face_Designs_Flowers01",
        "MX_040_Face_Designs_Hearts", "MX_040_Face_Designs_Leaves01",
        "MX_040_Face_Designs_Stars",
    ),
    "Eyelid_Designs": (
        "MX_050_Eyelid_Designs_GreenEyeShadow", "MX_050_Eyelid_Designs_PurpleEyeShadow",
        "MX_050_Eyelid_Designs_RainbowStars", "MX_050_Eyelid_Designs_RedEyeShadow",
        "MX_050_Eyelid_Designs_SmokeyLashes",
    ),
    "Mouth": (
        "MX_060_Mouth_BlackSmall", "MX_060_Mouth_DarkRedMedium",
        "MX_060_Mouth_PinkPointy", "MX_060_Mouth_PurpleFull",
        "MX_060_Mouth_RedMedium",
    ),
    "Head_Hair": (
        "MX_080_Head_Hair_BlackBob", "MX_080_Head_Hair_BlackCenter",
        "MX_080_Head_Hair_PinkShag", "MX_080_Head_Hair_RedShag",
    ),
    "Facial_Hair": (
        "MX_090_Facial_Hair_BlackAngled", "MX_090_Facial_Hair_BlackDali",
        "MX_090_Facial_Hair_BrownHandlebar", "MX_090_Facial_Hair_OrangeBatWing",
        "MX_090_Facial_Hair_YellowUpturn",
    ),
    "Brows": (
        "MX_100_Brows_BrownCut", "MX_100_Brows_GreyShort", "MX_100_Brows_Purple",
        "MX_100_Brows_WhiteBushy", "MX_100_Brows_YellowThin",
    ),
    "Glasses": (
        "MX_120_Glasses_BlueHeart", "MX_120_Glasses_GoldHalfRound",
        "MX_120_Glasses_RedCat", "MX_120_Glasses_RoundWhiteDot",
        "MX_120_Glasses_SmallRound",
    ),
    "Nose": (
        "MX_130_Nose_Cat", "MX_130_Nose_Clown", "MX_130_Nose_Dog",
        "MX_130_Nose_Human01", "MX_130_Nose_Pig",
    ),
}

# OpenMoxie: "Aside from Eyes and Face_Colors, other custom assets have had
# little testing, use more system resources, and may cause Moxie to be unstable."
TESTED_LAYERS: frozenset[str] = frozenset({"Eyes", "Face_Colors"})

_LAYER_OF: dict[str, str] = {
    label: layer for layer, labels in FACE_LAYERS.items() for label in labels
}
_ORDER: dict[str, int] = {label: index for index, label in enumerate(_LAYER_OF)}


def catalog() -> list[dict]:
    """Layers with their assets, as served by `GET /tts/face`."""
    return [
        {"layer": layer, "tested": layer in TESTED_LAYERS, "labels": list(labels)}
        for layer, labels in FACE_LAYERS.items()
    ]


def normalize(options: list[str]) -> list[str]:
    """Validate labels and return them in catalog order, one per layer.

    Raises ValueError for an unknown label or two labels on the same layer;
    the robot only has one slot per layer.
    """
    unknown = [label for label in options if label not in _LAYER_OF]
    if unknown:
        raise ValueError(f"unknown face asset(s): {', '.join(unknown)}")
    chosen: dict[str, str] = {}
    for label in options:
        layer = _LAYER_OF[label]
        if chosen.get(layer, label) != label:
            raise ValueError(f"only one {layer} asset at a time: {chosen[layer]}, {label}")
        chosen[layer] = label
    return sorted(chosen.values(), key=_ORDER.__getitem__)


def untested(options: list[str]) -> list[str]:
    return [label for label in options if _LAYER_OF[label] not in TESTED_LAYERS]


def new_child_id() -> str:
    """OpenMoxie's recovery: Moxie caches face textures keyed by child_pii.id."""
    return str(uuid.uuid4())


def load() -> dict | None:
    """Return the saved {"face_options", "child_id"} record, if any."""
    path = Path(settings.MOXIE_FACE_FILE)
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        options = normalize(record["face_options"])
        child_id = record.get("child_id")
        if child_id is not None and not isinstance(child_id, str):
            raise ValueError("child_id must be a string")
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError) as exc:
        # A bad file must not stop the service or reach the robot.
        logger.error("ignoring unreadable face file %s: %s", path, exc)
        return None
    return {"face_options": options, "child_id": child_id}


def save(options: list[str], child_id: str | None) -> None:
    path = Path(settings.MOXIE_FACE_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"face_options": options, "child_id": child_id}, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)
