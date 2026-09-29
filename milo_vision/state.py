"""
What the camera last found, saved in vision/ so the screen and Milo share it.

    vision/state.json    parts, tags, when, and the mosaic's placement
    vision/mosaic.png    the stitched top-down image of the table
    vision/heightmap.npz obstacle heights (heightmap.py)

describe() turns it into the text Milo gets with the machine state, so "where's my part?" and
"face the stock" are answered from what the camera actually saw (with its age and accuracy).
"""

import json
import os
import time
from typing import Optional

from milo_vision.geometry import VISION_DIR
from milo_vision.scan import Part, ScanResult

STATE_FILE = os.path.join(VISION_DIR, "state.json")
MOSAIC_FILE = os.path.join(VISION_DIR, "mosaic.png")
STALE_AFTER = 4 * 3600  # seconds: older scans are reported as possibly out of date


def save(result: ScanResult, directory: str = VISION_DIR):
    import cv2
    os.makedirs(directory, exist_ok=True)
    if result.mosaic is not None:
        cv2.imwrite(os.path.join(directory, "mosaic.png"), result.mosaic)
    data = {
        "finished_at": result.finished_at, "frames": result.frames, "table_z": result.table_z,
        "parts": [p.to_dict() for p in result.parts],
        "tags": {str(k): list(v) for k, v in result.tags.items()},
        "mosaic_origin": list(result.mosaic_origin), "mosaic_px_per_mm": result.mosaic_px_per_mm,
    }
    with open(os.path.join(directory, "state.json"), "w") as f:
        json.dump(data, f, indent=1)


def load(directory: str = VISION_DIR) -> Optional[ScanResult]:
    try:
        with open(os.path.join(directory, "state.json")) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    mosaic = None
    path = os.path.join(directory, "mosaic.png")
    if os.path.exists(path):
        import cv2
        mosaic = cv2.imread(path)
    parts = [Part(**{**p, "center": tuple(p["center"]), "size": tuple(p["size"]),
                     "corners": [tuple(c) for c in p["corners"]]}) for p in data.get("parts", [])]
    return ScanResult(parts=parts, tags={int(k): tuple(v) for k, v in data.get("tags", {}).items()},
                      table_z=data.get("table_z", 0.0), frames=data.get("frames", 0),
                      finished_at=data.get("finished_at", 0.0), mosaic=mosaic,
                      mosaic_origin=tuple(data.get("mosaic_origin", (0, 0))),
                      mosaic_px_per_mm=data.get("mosaic_px_per_mm", 2.0))


def describe(result: Optional[ScanResult], calibrated: bool = True, now: Optional[float] = None) -> str:
    """Scan results for Milo's machine context (machine coordinates, mm)"""
    if result is None:
        return "Camera: no table scan yet."
    age_min = ((now or time.time()) - result.finished_at) / 60
    age = f"{age_min:.0f} min ago" if age_min < 120 else f"{age_min / 60:.1f} h ago"
    lines = [f"Camera table scan ({age}{'; may be out of date' if age_min * 60 > STALE_AFTER else ''}; "
             f"machine coordinates, camera accuracy about +-0.5 mm, probe for precision"
             + ("" if calibrated else "; CAMERA NOT CALIBRATED, positions are rough") + "):"]
    if not result.parts:
        lines.append("  no parts found on the table")
    for i, p in enumerate(result.parts, 1):
        xs = [c[0] for c in p.corners]
        ys = [c[1] for c in p.corners]
        top = f", top at tip Z {p.top_z:.1f}" if p.top_z is not None else ", height unknown"
        lines.append(f"  part {i}: {p.size[0]:.1f} x {p.size[1]:.1f} mm, centre X {p.center[0]:.1f} "
                     f"Y {p.center[1]:.1f}, spans X {min(xs):.1f}..{max(xs):.1f} Y {min(ys):.1f}..{max(ys):.1f}, "
                     f"turned {p.angle:.1f} deg{top}")
    for tag_id, (x, y) in sorted(result.tags.items()):
        lines.append(f"  marker {tag_id} at X {x:.1f} Y {y:.1f}")
    return "\n".join(lines)
