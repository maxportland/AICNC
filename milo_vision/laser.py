"""
Line-laser triangulation: heights from where the laser line appears in the image.

The laser sits beside the camera and projects a line across the camera's view at an angle.
On the table the line falls at one image row; on anything taller it shifts sideways in the
image (along v), by an amount proportional to the height. Two reference sweeps (the bare table,
and a gauge block of known height) calibrate that proportion per image column, which absorbs
lens and mounting imperfections.

The camera on this machine is monochrome and the table is bright aluminium, so the line is found
in the difference between a frame with the laser on and one with it off (capture_pair): the table,
the parts and the lighting cancel, and only the line is left. A colour camera on a dark table can
use a single frame.

    on, off = capture_pair(camera, set_laser)
    rows = extract_line(on, background=off) sub-pixel row of the line in each column (NaN: none)
    cal  = calibrate(rows_table, table_z, rows_block, block_z)
    z    = heights(rows, cal)               tip Z per column
    pts  = profile_points(z, rows, model, spindle)   machine (x, y, z) points for the height map
"""

from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

import numpy as np

from milo_vision.geometry import CameraModel


def is_mono(image: np.ndarray) -> bool:
    """A greyscale frame: one channel, or three equal ones (how picamera2 delivers a mono sensor)"""
    if image.ndim == 2 or image.shape[2] == 1:
        return True
    sample = image[::8, ::8].astype(np.int16)
    return int(np.abs(sample[..., 0] - sample[..., 2]).max()) <= 2 and \
        int(np.abs(sample[..., 1] - sample[..., 2]).max()) <= 2


def line_signal(image: np.ndarray, colour: str = "auto") -> np.ndarray:
    """How much each pixel looks like the laser: its brightness on a mono camera, how much more of
    the laser's colour it has than the other two on a colour camera"""
    img = image.astype(np.float32)
    if img.ndim == 3 and img.shape[2] == 1:
        img = img[..., 0]
    if img.ndim == 2 or colour == "gray" or (colour == "auto" and is_mono(image)):
        return img if img.ndim == 2 else img.mean(axis=2)
    b, g, r = img[..., 0], img[..., 1], img[..., 2]
    return {"red": r - (g + b) / 2, "green": g - (r + b) / 2, "blue": b - (r + g) / 2}.get(
        "red" if colour == "auto" else colour, r)


def extract_line(image: np.ndarray, colour: str = "auto", min_strength: float = 40.0,
                 background: Optional[np.ndarray] = None, max_width: int = 40) -> np.ndarray:
    """
    Row of the laser line in each column, to a fraction of a pixel. background: the same view with
    the laser off; the line is then found in the difference, so a bright table doesn't matter.
    The row is the centroid of the brighter half of the peak, over its whole width, so a
    saturated (flat-topped) line still gives its middle.
    """
    signal = line_signal(image, colour)
    if background is not None:
        signal = signal - line_signal(background, colour)
    signal = np.clip(signal, 0, None)
    peak = signal.max(axis=0)
    rows = np.full(signal.shape[1], np.nan)
    idx = signal.argmax(axis=0)
    h = signal.shape[0]
    for col in np.nonzero(peak >= min_strength)[0]:
        column, top = signal[:, col], peak[col]
        half = top * 0.5
        # walk out from the peak while above half height (bounded: a real line is thin)
        r0 = r1 = int(idx[col])
        while r0 > 0 and idx[col] - r0 < max_width and column[r0 - 1] > half:
            r0 -= 1
        while r1 < h - 1 and r1 - idx[col] < max_width and column[r1 + 1] > half:
            r1 += 1
        window = column[r0:r1 + 1]
        weight = window - half
        weight[weight < 0] = 0
        if weight.sum() > 0:
            rows[col] = r0 + float((weight * np.arange(len(window))).sum() / weight.sum())
    return rows


def capture_pair(camera, set_laser: Callable[[bool], object], settle_frames: int = 2):
    """A frame with the laser on and one with it off, from the same spot (the laser is left off).
    Frames already in flight when the laser switches are thrown away."""
    def fresh():
        frame = None
        for _ in range(settle_frames + 1):
            frame = camera.grab()
        return frame
    try:
        set_laser(False)
        off = fresh()
        set_laser(True)
        on = fresh()
    finally:
        set_laser(False)
    return on, off


@dataclass
class LaserCalibration:
    row_at_table: np.ndarray    # per column
    mm_per_row: np.ndarray      # per column: height change per pixel of shift (sign included); linear model
    table_z: float
    # Exact model (when the camera's height above the table was given): a surface h above the table
    # shifts the line by shift = h * scale / (distance - h) pixels, wherever the laser sits; so
    # h = distance * shift / (scale + shift). scale is per column.
    distance: Optional[float] = None
    scale: Optional[np.ndarray] = None

    def to_dict(self):
        d = {"row_at_table": self.row_at_table.tolist(), "mm_per_row": self.mm_per_row.tolist(),
             "table_z": self.table_z}
        if self.distance is not None:
            d.update(distance=self.distance, scale=self.scale.tolist())
        return d

    @staticmethod
    def from_dict(d):
        return LaserCalibration(np.asarray(d["row_at_table"], float), np.asarray(d["mm_per_row"], float),
                                float(d["table_z"]), d.get("distance"),
                                np.asarray(d["scale"], float) if "scale" in d else None)


def _fill(values: np.ndarray, how: str) -> np.ndarray:
    """Columns without a value: interpolated from their neighbours, or the median"""
    values = values.copy()
    missing = ~np.isfinite(values)
    if missing.any():
        if how == "median":
            values[missing] = np.median(values[~missing])
        else:
            cols = np.arange(len(values))
            values[missing] = np.interp(cols[missing], cols[~missing], values[~missing])
    return values


def calibrate(rows_table: np.ndarray, table_z: float, rows_block: np.ndarray, block_z: float,
              distance: Optional[float] = None) -> LaserCalibration:
    """
    From the line on the bare table and on a gauge block (both at the same camera height).
    distance: the camera's height above the table during the sweeps (CameraModel.distance). With it,
    heights follow the exact perspective model; without it, a straight line through the two
    references, which reads tall surfaces wrong by a few percent of their height.
    """
    shift = rows_block - rows_table
    h1 = block_z - table_z
    with np.errstate(divide="ignore", invalid="ignore"):
        per_row = h1 / shift
        scale = shift * (distance - h1) / h1 if distance else None
    good = np.isfinite(per_row) & (np.abs(shift) > 0.5)
    if good.sum() < 10:
        raise ValueError("The laser line wasn't visible on both the table and the block")
    per_row[~good] = np.nan
    # columns the block didn't cover: the median of the ones it did (nearly constant anyway)
    per_row = _fill(per_row, "median")
    if scale is not None:
        scale[~good] = np.nan
        scale = _fill(scale, "median")
    return LaserCalibration(_fill(rows_table, "interpolate"), per_row, table_z, distance, scale)


def heights(rows: np.ndarray, cal: LaserCalibration) -> np.ndarray:
    shift = rows - cal.row_at_table
    if cal.distance is None or cal.scale is None:
        return cal.table_z + shift * cal.mm_per_row
    with np.errstate(divide="ignore", invalid="ignore"):
        return cal.table_z + cal.distance * shift / (cal.scale + shift)


def profile_points(z: np.ndarray, rows: np.ndarray, model: CameraModel,
                   spindle: Tuple[float, float, float], step: int = 4) -> List[Tuple[float, float, float]]:
    """Machine (x, y, tip z) of the laser line, every `step` columns"""
    points = []
    for col in range(0, len(z), step):
        if np.isfinite(z[col]) and np.isfinite(rows[col]):
            x, y = model.pixel_to_machine([(col, rows[col])], spindle, float(z[col]))[0]
            points.append((float(x), float(y), float(z[col])))
    return points
