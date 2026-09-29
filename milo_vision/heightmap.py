"""
What's on the table, as heights: a grid over the machine's X/Y holding the tip Z of the
highest thing in each cell (NaN where nothing was measured or known).

Filled from laser sweeps (laser.py), from scanned parts with a known top, and from keep-out
boxes (a vise's jaws, a clamp) the operator or Milo adds. check_move() tells whether a straight
move keeps the tool tip above everything along the way. It is conservative: cells within the
tool radius plus a clearance count, and it never makes a move look safer than LinuxCNC's own
limits do.
"""

import json
import math
import os
import time
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from milo_vision.geometry import VISION_DIR

HEIGHTMAP_FILE = os.path.join(VISION_DIR, "heightmap.npz")


class HeightMap:
    def __init__(self, x_range: Tuple[float, float], y_range: Tuple[float, float], resolution: float = 2.0):
        self.x0, self.y0 = float(x_range[0]), float(y_range[0])
        self.res = float(resolution)
        self.nx = max(1, int(math.ceil((x_range[1] - x_range[0]) / self.res)) + 1)
        self.ny = max(1, int(math.ceil((y_range[1] - y_range[0]) / self.res)) + 1)
        self.top = np.full((self.ny, self.nx), np.nan)
        self.updated = time.time()
        self.boxes: List[dict] = []   # keep-outs as given, for display and editing

    # --- filling ----------------------------------------------------------------------------------

    def _cell(self, x, y):
        return int(round((y - self.y0) / self.res)), int(round((x - self.x0) / self.res))

    def raise_to(self, x: float, y: float, z: float):
        j, i = self._cell(x, y)
        if 0 <= j < self.ny and 0 <= i < self.nx:
            current = self.top[j, i]
            self.top[j, i] = z if np.isnan(current) else max(current, z)

    def add_points(self, points: Sequence[Tuple[float, float, float]]):
        for x, y, z in points:
            self.raise_to(x, y, z)
        self.updated = time.time()

    def add_box(self, polygon: Sequence[Tuple[float, float]], top_z: float, label: str = ""):
        """A keep-out region: solid up to top_z inside the polygon (machine XY)"""
        poly = np.asarray(polygon, float)
        xs, ys = poly[:, 0], poly[:, 1]
        jj, ii = np.mgrid[0:self.ny, 0:self.nx]
        cx, cy = self.x0 + ii * self.res, self.y0 + jj * self.res
        inside = _points_in_polygon(cx, cy, xs, ys)
        # also cells the outline passes through, so thin boxes aren't missed
        near = np.zeros_like(inside)
        for k in range(len(poly)):
            (ax, ay), (bx, by) = poly[k], poly[(k + 1) % len(poly)]
            near |= _distance_to_segment(cx, cy, ax, ay, bx, by) <= self.res * 0.75
        mask = inside | near
        self.top[mask] = np.where(np.isnan(self.top[mask]), top_z, np.maximum(self.top[mask], top_z))
        self.boxes.append({"polygon": poly.tolist(), "top_z": float(top_z), "label": label})
        self.updated = time.time()

    # --- queries ----------------------------------------------------------------------------------

    def max_in_circle(self, x: float, y: float, radius: float) -> Optional[float]:
        r_cells = int(math.ceil(radius / self.res))
        j0, i0 = self._cell(x, y)
        best = None
        for j in range(j0 - r_cells, j0 + r_cells + 1):
            if not 0 <= j < self.ny:
                continue
            for i in range(i0 - r_cells, i0 + r_cells + 1):
                if not 0 <= i < self.nx:
                    continue
                cx, cy = self.x0 + i * self.res, self.y0 + j * self.res
                if math.hypot(cx - x, cy - y) > radius + self.res * 0.71:
                    continue
                v = self.top[j, i]
                if not np.isnan(v) and (best is None or v > best):
                    best = float(v)
        return best

    def check_move(self, start: Sequence[float], end: Sequence[float], tool_radius: float = 3.0,
                   clearance: float = 2.0, ignore_below: Optional[float] = None) -> Optional[dict]:
        """
        A straight move of the tool tip from start to end (machine X, Y, tip Z).
        Returns None if it stays clearance above everything within tool_radius, otherwise
        {x, y, tip_z, obstacle_z} of the first point where it doesn't.
        ignore_below: heights at or below this (e.g. the stock being cut) don't count.
        """
        (x0, y0, z0), (x1, y1, z1) = start[:3], end[:3]
        length = math.hypot(x1 - x0, y1 - y0)
        steps = max(1, int(math.ceil(length / (self.res / 2))))
        for k in range(steps + 1):
            t = k / steps
            x, y, z = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, z0 + (z1 - z0) * t
            top = self.max_in_circle(x, y, tool_radius)
            if top is None or (ignore_below is not None and top <= ignore_below):
                continue
            if z < top + clearance:
                return {"x": x, "y": y, "tip_z": z, "obstacle_z": top}
        return None

    # --- persistence --------------------------------------------------------------------------------

    def save(self, path: str = HEIGHTMAP_FILE):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        np.savez_compressed(path, top=self.top, meta=np.array(json.dumps({
            "x0": self.x0, "y0": self.y0, "res": self.res, "updated": self.updated, "boxes": self.boxes})))

    @staticmethod
    def load(path: str = HEIGHTMAP_FILE) -> Optional["HeightMap"]:
        try:
            data = np.load(path, allow_pickle=False)
            meta = json.loads(str(data["meta"]))
        except (OSError, ValueError, KeyError):
            return None
        top = data["top"]
        hm = HeightMap((meta["x0"], meta["x0"]), (meta["y0"], meta["y0"]), meta["res"])
        hm.top, (hm.ny, hm.nx) = top, top.shape
        hm.updated, hm.boxes = meta.get("updated", 0.0), meta.get("boxes", [])
        return hm


def _points_in_polygon(px, py, xs, ys):
    """Even-odd rule, vectorised over grids px, py"""
    inside = np.zeros(px.shape, bool)
    n = len(xs)
    for k in range(n):
        xa, ya, xb, yb = xs[k], ys[k], xs[(k + 1) % n], ys[(k + 1) % n]
        crosses = (ya > py) != (yb > py)
        with np.errstate(divide="ignore", invalid="ignore"):
            x_at = xa + (py - ya) * (xb - xa) / (yb - ya)
        inside ^= crosses & (px < x_at)
    return inside


def _distance_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = np.clip(((px - ax) * dx + (py - ay) * dy) / length2, 0, 1) if length2 else 0.0
    return np.hypot(px - (ax + t * dx), py - (ay + t * dy))
