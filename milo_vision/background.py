"""
The empty table, photographed once, so parts are found by what changed instead of by being
brighter than the table (this table is bright aluminium with dark holes, so brightness alone
finds the table itself).

EmptyTable keeps each photo with the spindle position it was taken from, plus a mosaic of the
table plane stitched from them. For a view it returns the background to compare against:

    the photo from the same spot (within 0.5 mm): lines up exactly. Scans revisit the spots the
    empty table was photographed from, a denser grid than a plain scan, so a part is seen whole
    from two of them, which gives its height by parallax; or
    the mosaic re-projected into the view, for spots that weren't photographed (the Live view):
    lines up as well as the camera calibration on the table itself, but anything tall (a vise)
    lands in the wrong place, so scans don't rely on it.

Saved in vision/empty/: index.json, frame_NN.png and mosaic.png.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import List, Optional, Sequence, Tuple

import numpy as np

from milo_vision.geometry import CameraModel

SAME_SPOT_MM = 0.5
EXACT_TOLERANCE_PX = 2       # the photo from the same spot: repeatability and vibration
PROJECTED_TOLERANCE_PX = 6   # the mosaic re-projected: calibration error
PHOTO_OVERLAP = 0.7          # the empty-table grid: a part up to ~120 mm is whole in two photos (its height)


@dataclass
class View:
    background: np.ndarray        # greyscale, same size as the frame
    valid: np.ndarray             # 255 where the background is known
    tolerance: int                # pixels of misalignment to allow


def _gray(image):
    import cv2
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


class EmptyTable:
    def __init__(self, model: CameraModel, frames: Sequence[Tuple[Tuple[float, float, float], np.ndarray]],
                 px_per_mm: float = 4.0, taken_at: Optional[float] = None):
        self.model = model
        self.frames = [(tuple(float(v) for v in spindle), _gray(image)) for spindle, image in frames]
        self.px_per_mm = px_per_mm
        self.taken_at = taken_at or time.time()
        self.mosaic, self.origin = self._stitch()

    @property
    def positions(self) -> List[Tuple[float, float, float]]:
        """Where the photos were taken, in order: scans revisit these, so each frame has its exact
        empty-table photo"""
        return [spindle for spindle, _ in self.frames]

    def covers(self, scan_z: float) -> bool:
        """Photographed at this camera height (scans can then revisit the same spots)"""
        return bool(self.frames) and all(abs(z - scan_z) <= SAME_SPOT_MM for _, _, z in self.positions)

    # --- the table-plane mosaic -------------------------------------------------------------------

    def _stitch(self):
        """Table plane, orthographic: pixel (0, bottom) is machine XY `origin`; 0 = not seen"""
        import cv2
        if not self.frames:
            return np.zeros((1, 1), np.uint8), (0.0, 0.0)
        m = self.model
        corners = []
        for spindle, image in self.frames:
            h, w = image.shape[:2]
            px = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], float)
            corners.append(m.pixel_to_machine(px, spindle))
        allxy = np.vstack(corners)
        (x0, y0), (x1, y1) = allxy.min(axis=0), allxy.max(axis=0)
        origin = (float(x0), float(y0))
        size = (int((x1 - x0) * self.px_per_mm) + 2, int((y1 - y0) * self.px_per_mm) + 2)
        mosaic = np.zeros((size[1], size[0]), np.uint8)
        for (spindle, image), quad in zip(self.frames, corners):
            h, w = image.shape[:2]
            src = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
            H = cv2.getPerspectiveTransform(src, self._to_mosaic(quad, origin, size[1]).astype(np.float32))
            warped = cv2.warpPerspective(np.maximum(image, 1), H, size)  # 0 is kept for "not seen"
            mosaic[warped > 0] = warped[warped > 0]
        return mosaic, origin

    def _to_mosaic(self, xy, origin=None, height=None):
        origin = self.origin if origin is None else origin
        height = self.mosaic.shape[0] if height is None else height
        xy = np.asarray(xy, float).reshape(-1, 2)
        return np.stack([(xy[:, 0] - origin[0]) * self.px_per_mm, height - (xy[:, 1] - origin[1]) * self.px_per_mm],
                        axis=1)

    # --- backgrounds for a view -------------------------------------------------------------------

    def view(self, spindle: Sequence[float], shape: Tuple[int, ...]) -> Optional[View]:
        """The empty table as the camera at `spindle` would see it, or None if it was never seen"""
        import cv2
        h, w = shape[:2]
        best = None
        for frame_spindle, image in self.frames:
            gap = max(abs(a - b) for a, b in zip(frame_spindle, spindle[:3]))
            if gap <= SAME_SPOT_MM and image.shape[:2] == (h, w) and (best is None or gap < best[0]):
                best = (gap, image)
        if best is not None:
            return View(best[1], np.full((h, w), 255, np.uint8), EXACT_TOLERANCE_PX)
        if not self.mosaic.any():
            return None
        px = np.float32([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]])
        quad = self._to_mosaic(self.model.pixel_to_machine(px, tuple(spindle[:3]))).astype(np.float32)
        H = cv2.getPerspectiveTransform(quad, px)
        background = cv2.warpPerspective(self.mosaic, H, (w, h), flags=cv2.INTER_LINEAR)
        valid = cv2.erode(((background > 0) * 255).astype(np.uint8), np.ones((5, 5), np.uint8))
        if not valid.any():
            return None
        return View(background, valid, PROJECTED_TOLERANCE_PX)

    # --- persistence ------------------------------------------------------------------------------

    def save(self, directory: str):
        import cv2
        os.makedirs(directory, exist_ok=True)
        for name in os.listdir(directory):
            if name.startswith("frame_") and name.endswith(".png"):
                os.remove(os.path.join(directory, name))
        index = {"taken_at": self.taken_at, "px_per_mm": self.px_per_mm, "origin": list(self.origin), "frames": []}
        for i, (spindle, image) in enumerate(self.frames):
            name = f"frame_{i:02d}.png"
            cv2.imwrite(os.path.join(directory, name), image)
            index["frames"].append({"file": name, "spindle": list(spindle)})
        cv2.imwrite(os.path.join(directory, "mosaic.png"), self.mosaic)
        with open(os.path.join(directory, "index.json"), "w") as f:
            json.dump(index, f, indent=2)

    @staticmethod
    def load(directory: str, model: CameraModel) -> Optional["EmptyTable"]:
        """None if there's no photo of the empty table. The mosaic is rebuilt with `model`, so a
        newer calibration also improves the re-projected backgrounds."""
        import cv2
        try:
            with open(os.path.join(directory, "index.json")) as f:
                index = json.load(f)
            frames: List = []
            for entry in index["frames"]:
                image = cv2.imread(os.path.join(directory, entry["file"]), cv2.IMREAD_GRAYSCALE)
                if image is not None:
                    frames.append((tuple(entry["spindle"]), image))
        except (OSError, ValueError, KeyError, TypeError):
            return None
        if not frames:
            return None
        return EmptyTable(model, frames, float(index.get("px_per_mm", 4.0)), index.get("taken_at"))
