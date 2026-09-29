"""
Line-laser triangulation: heights from where the laser line appears in the image.

The laser sits beside the camera and projects a line across the camera's view at an angle.
On the table the line falls at one image row; on anything taller it shifts sideways in the
image (along v), by an amount proportional to the height. Two reference sweeps (the bare table,
and a gauge block of known height) calibrate that proportion per image column, which absorbs
lens and mounting imperfections.

    rows = extract_line(frame)              sub-pixel row of the line in each column (NaN: none)
    cal  = calibrate(rows_table, table_z, rows_block, block_z)
    z    = heights(rows, cal)               tip Z per column
    pts  = profile_points(z, rows, model, spindle)   machine (x, y, z) points for the height map
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

from milo_vision.geometry import CameraModel


def extract_line(image: np.ndarray, colour: str = "red", min_strength: float = 40.0) -> np.ndarray:
    """Row of the laser line in each column, to a fraction of a pixel (centroid of the peak)"""
    img = image.astype(np.float32)
    if img.ndim == 3:
        b, g, r = img[..., 0], img[..., 1], img[..., 2]
        signal = {"red": r - (g + b) / 2, "green": g - (r + b) / 2, "blue": b - (r + g) / 2}.get(colour, r)
    else:
        signal = img
    signal = np.clip(signal, 0, None)
    peak = signal.max(axis=0)
    rows = np.full(signal.shape[1], np.nan)
    idx = signal.argmax(axis=0)
    h = signal.shape[0]
    for col in np.nonzero(peak >= min_strength)[0]:
        r0, r1 = max(0, idx[col] - 4), min(h, idx[col] + 5)
        window = signal[r0:r1, col]
        weight = window - window.max() * 0.5
        weight[weight < 0] = 0
        if weight.sum() > 0:
            rows[col] = r0 + float((weight * np.arange(r1 - r0)).sum() / weight.sum())
    return rows


@dataclass
class LaserCalibration:
    row_at_table: np.ndarray    # per column
    mm_per_row: np.ndarray      # per column: height change per pixel of shift (sign included)
    table_z: float

    def to_dict(self):
        return {"row_at_table": self.row_at_table.tolist(), "mm_per_row": self.mm_per_row.tolist(),
                "table_z": self.table_z}

    @staticmethod
    def from_dict(d):
        return LaserCalibration(np.asarray(d["row_at_table"], float), np.asarray(d["mm_per_row"], float),
                                float(d["table_z"]))


def calibrate(rows_table: np.ndarray, table_z: float, rows_block: np.ndarray, block_z: float) -> LaserCalibration:
    """From the line on the bare table and on a gauge block (both at the same camera height)"""
    shift = rows_block - rows_table
    with np.errstate(divide="ignore", invalid="ignore"):
        per_row = (block_z - table_z) / shift
    good = np.isfinite(per_row)
    if good.sum() < 10:
        raise ValueError("The laser line wasn't visible on both the table and the block")
    # columns the block didn't cover: use the median of the ones it did (nearly constant anyway)
    per_row[~good] = np.median(per_row[good])
    table_rows = rows_table.copy()
    missing = ~np.isfinite(table_rows)
    if missing.any():
        cols = np.arange(len(table_rows))
        table_rows[missing] = np.interp(cols[missing], cols[~missing], table_rows[~missing])
    return LaserCalibration(table_rows, per_row, table_z)


def heights(rows: np.ndarray, cal: LaserCalibration) -> np.ndarray:
    return cal.table_z + (rows - cal.row_at_table) * cal.mm_per_row


def profile_points(z: np.ndarray, rows: np.ndarray, model: CameraModel,
                   spindle: Tuple[float, float, float], step: int = 4) -> List[Tuple[float, float, float]]:
    """Machine (x, y, tip z) of the laser line, every `step` columns"""
    points = []
    for col in range(0, len(z), step):
        if np.isfinite(z[col]) and np.isfinite(rows[col]):
            x, y = model.pixel_to_machine([(col, rows[col])], spindle, float(z[col]))[0]
            points.append((float(x), float(y), float(z[col])))
    return points
