"""
Checks a generated program by simulating what it cuts.

simulate() sweeps each operation's tool along its planned moves over a height map of the
stock (a flat end mill cutting a flat-bottomed circle, which is close enough to judge the
result). From that:

- find_problems() reports operations that remove no material (an earlier operation already
  cut that area as deep or deeper, or the tool never reaches the stock) and rapid moves
  that would plough through material.
- render() draws the finished part from above, so a vision model can judge whether it
  looks like what was asked for.
"""

import math
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

MAX_CELLS = 400  # Height map cells along the longer stock side
EPS = 0.01  # Height changes smaller than this (IR units) don't count as cutting
AIR_CUT_FRACTION = 0.02  # An operation that changes less of its footprint than this cuts air


@dataclass
class OpResult:
    index: int  # 0-based
    label: str
    footprint: int = 0  # cells the tool passed over below the stock surface
    changed: int = 0  # cells where it removed material
    covered_by: dict = field(default_factory=dict)  # earlier op index -> cells it had already cut
    rapid_hits: List[tuple] = field(default_factory=list)  # (x, y) where a rapid cut material


@dataclass
class Simulation:
    heights: np.ndarray  # [row, col], row 0 at stock min Y
    x0: float
    y0: float
    cell: float
    top: float  # stock surface Z
    ops: List[OpResult]
    units: str = "mm"

    @property
    def deepest(self):
        return float(self.top - self.heights.min())


def describe_op(op) -> str:
    """A short handle for an operation the model will recognize in its own IR"""
    parts = [op.op, f"T{op.tool}"]
    circle = getattr(op, "circle", None)
    if circle is not None:
        parts.append(f"circle ⌀{circle.diameter:g} at [{circle.center[0]:g}, {circle.center[1]:g}]")
    elif getattr(op, "text", None):
        parts.append(f"text '{op.text}'")
    else:
        points = getattr(op, "path", None) or getattr(op, "boundary", None) or getattr(op, "points", None)
        if points:
            parts.append(f"starting at [{points[0][0]:g}, {points[0][1]:g}]")
    return " ".join(parts)


def surface_z(ir) -> float:
    """Where the program assumes the material starts. The stock's top when an operation starts
    there; otherwise the most common top_z (the stock box's Z range is often just a size, not
    placed at the work zero)."""
    stock_top = float(ir.stock.max[2])
    tops = [float(op.top_z) for op in ir.ops if getattr(op, "top_z", None) is not None]
    if not tops or any(abs(t - stock_top) < EPS for t in tops):
        return stock_top
    return max(set(tops), key=tops.count)


def _points(start, move, step):
    """Points along a move from `start`, at most `step` apart (arcs are followed)"""
    x0, y0, z0 = start
    x1 = move.x if move.x is not None else x0
    y1 = move.y if move.y is not None else y0
    z1 = move.z if move.z is not None else z0
    if move.type in ("arc_cw", "arc_ccw") and (move.i is not None or move.j is not None):
        cx, cy = x0 + (move.i or 0.0), y0 + (move.j or 0.0)
        radius = math.hypot(x0 - cx, y0 - cy)
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        sweep = a1 - a0
        if move.type == "arc_cw":
            sweep = sweep - 2 * math.pi if sweep >= -1e-9 else sweep
        else:
            sweep = sweep + 2 * math.pi if sweep <= 1e-9 else sweep
        if math.hypot(x1 - x0, y1 - y0) > 1e-6 and abs(abs(sweep) - 2 * math.pi) < 1e-6:
            sweep = 0.0
        n = max(1, int(math.ceil(abs(sweep) * radius / step)))
        for k in range(1, n + 1):
            t = k / n
            a = a0 + sweep * t
            yield cx + radius * math.cos(a), cy + radius * math.sin(a), z0 + (z1 - z0) * t
        return
    n = max(1, int(math.ceil(math.hypot(x1 - x0, y1 - y0) / step)))
    for k in range(1, n + 1):
        t = k / n
        yield x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, z0 + (z1 - z0) * t


def _disk(radius_cells):
    r = max(0, int(math.ceil(radius_cells)))
    yy, xx = np.mgrid[-r:r + 1, -r:r + 1]
    return r, (xx * xx + yy * yy) <= max(radius_cells, 0.5) ** 2


def simulate(ir, operation_moves) -> Simulation:
    """Cut the stock with every operation in order"""
    (sx0, sy0), (sx1, sy1) = ir.stock.min[:2], ir.stock.max[:2]
    cell = max(sx1 - sx0, sy1 - sy0) / MAX_CELLS
    cols = max(1, int(math.ceil((sx1 - sx0) / cell)))
    rows = max(1, int(math.ceil((sy1 - sy0) / cell)))
    top = surface_z(ir)
    heights = np.full((rows, cols), top, dtype=np.float32)
    owner = np.full((rows, cols), -1, dtype=np.int16)  # which op cut each cell last
    tools = {t.tool: t for t in ir.tools}
    results = []
    position = (sx0, sy0, top + 1000.0)  # far above until the first move says otherwise

    for index, (op, moves) in enumerate(zip(ir.ops, operation_moves)):
        result = OpResult(index, describe_op(op))
        results.append(result)
        tool = tools.get(op.tool)
        diameter = float(getattr(tool, "diameter", 0) or 0) or cell
        r, disk = _disk(diameter / 2 / cell)
        footprint = np.zeros((rows, cols), dtype=bool)
        changed = np.zeros((rows, cols), dtype=bool)
        covered = {}

        for move in moves:
            if move.type == "dwell" or not move.has_position():
                continue
            rapid = move.type == "rapid"
            for x, y, z in _points(position, move, cell):
                if z >= top - EPS:
                    continue
                col, row = int(round((x - sx0) / cell)), int(round((y - sy0) / cell))
                r0, r1, c0, c1 = row - r, row + r + 1, col - r, col + r + 1
                if r1 <= 0 or c1 <= 0 or r0 >= rows or c0 >= cols:
                    continue
                mask = disk[max(0, -r0):disk.shape[0] - max(0, r1 - rows), max(0, -c0):disk.shape[1] - max(0, c1 - cols)]
                area = (slice(max(0, r0), min(rows, r1)), slice(max(0, c0), min(cols, c1)))
                cutting = mask & (heights[area] > z + EPS)
                if rapid:
                    if cutting.any() and len(result.rapid_hits) < 5:
                        result.rapid_hits.append((round(x, 3), round(y, 3)))
                    continue
                footprint[area] |= mask
                # Cells another operation already cut at least this deep
                already = mask & ~cutting & (owner[area] >= 0) & (owner[area] != index)
                if already.any():
                    for other, n in zip(*np.unique(owner[area][already], return_counts=True)):
                        covered[int(other)] = covered.get(int(other), 0) + int(n)
                if cutting.any():
                    heights[area][cutting] = z
                    owner[area][cutting] = index
                    changed[area] |= cutting
            position = (move.x if move.x is not None else position[0],
                        move.y if move.y is not None else position[1],
                        move.z if move.z is not None else position[2])

        result.footprint = int(footprint.sum())
        result.changed = int(changed.sum())
        result.covered_by = covered
    units = getattr(ir, "units", "mm") or "mm"
    return Simulation(heights, sx0, sy0, cell, top, results, str(getattr(units, "value", units)))


def find_problems(sim: Simulation) -> List[str]:
    """Plain-language problems the CAM model can fix, one per operation"""
    problems = []
    for result in sim.ops:
        name = f"Operation {result.index + 1} ({result.label})"
        if result.rapid_hits:
            where = ", ".join(f"[{x:g}, {y:g}]" for x, y in result.rapid_hits[:3])
            problems.append(f"{name} makes rapid (G0) moves through material near {where}. "
                            "Keep safe_z and clearance_z above the stock surface.")
        if result.footprint == 0:
            problems.append(f"{name} never reaches the material: the tool stays above the stock surface "
                            f"(Z {sim.top:g}). Check its top_z and depth.")
        elif result.changed < AIR_CUT_FRACTION * result.footprint:
            earlier = sorted(result.covered_by, key=result.covered_by.get, reverse=True)
            if earlier:
                who = " and ".join(f"operation {i + 1}" for i in earlier[:2])
                problems.append(f"{name} removes no material, so it won't be visible: {who} already cut that "
                                "area as deep or deeper. Cut it deeper than the earlier operation, or change the "
                                "earlier operation so it doesn't clear this area (e.g. profile an outline "
                                "instead of pocketing the whole region).")
            else:
                problems.append(f"{name} removes no material.")
    return problems


def render(sim: Simulation, path: str, max_px: int = 768):
    """Top view of the finished part, +Y up: untouched stock is light, deeper cuts darker"""
    from PIL import Image, ImageDraw
    depth = np.clip(sim.top - sim.heights, 0, None)[::-1]  # row 0 at the top of the image = max Y
    deepest = float(depth.max()) or 1.0
    t = depth / deepest
    stock = np.array([222, 205, 170], dtype=np.float32)  # untouched: light wood/aluminum tone
    shallow = np.array([90, 140, 200], dtype=np.float32)
    deep = np.array([20, 30, 80], dtype=np.float32)
    rgb = np.where((depth > EPS)[..., None],
                   shallow[None, None] + (deep - shallow)[None, None] * t[..., None],
                   stock[None, None])
    image = Image.fromarray(rgb.astype(np.uint8), "RGB")
    scale = max_px / max(image.size)
    image = image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.NEAREST)
    margin = 28
    canvas = Image.new("RGB", (image.width + 2 * margin, image.height + 2 * margin), (255, 255, 255))
    canvas.paste(image, (margin, margin))
    draw = ImageDraw.Draw(canvas)
    w, h = sim.heights.shape[1] * sim.cell, sim.heights.shape[0] * sim.cell
    draw.text((margin, 6), f"Top view, +Y up. Stock X {sim.x0:g}..{sim.x0 + w:g}, "
                           f"Y {sim.y0:g}..{sim.y0 + h:g} {sim.units}", fill=(0, 0, 0))
    draw.text((margin, canvas.height - 20), f"Light = uncut surface. Blue = cut, darker = deeper "
                                            f"(deepest {deepest:.3g} {sim.units}).", fill=(0, 0, 0))
    canvas.save(path)
    return path
