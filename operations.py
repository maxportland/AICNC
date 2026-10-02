"""
Parameter-driven operations: facing, holes, pockets, slots, contours, thread milling and text,
each described by a few numbers instead of a conversation with the AI.

build_program() turns an operation's parameter values into CAM IR, which the same CAM
processor that runs Milo's programs turns into G-code (planning, machine checks, simulation).
Nothing here touches Qt or the machine.
"""

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ai_config import DEFAULT_COOLANT

# Parameter kinds: number formatting and unit scaling (defaults below are in mm)
LENGTH, FEED, RPM, ANGLE, COUNT, PERCENT, CHOICE, TEXT, TOOL = (
    "length", "feed", "rpm", "angle", "count", "percent", "choice", "text", "tool")
SCALED = (LENGTH, FEED)
ARC_STEP_DEG = 3.0  # slot arcs are followed with chords this many degrees apart


class OperationError(ValueError):
    """Parameters that don't make a program, in words for the operator"""


@dataclass
class Param:
    key: str
    label: str
    kind: str = LENGTH
    default: Any = 0.0  # mm for lengths and feeds
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    choices: Tuple[str, ...] = ()
    show_if: Optional[Tuple[str, str]] = None  # only for this (param key, choice)
    hint: str = ""

    def unit(self, units: str) -> str:
        return {LENGTH: units, FEED: f"{units}/min", RPM: "rpm", ANGLE: "°", PERCENT: "%"}.get(self.kind, "")


@dataclass
class Operation:
    key: str
    title: str
    icon: str
    summary: str
    params: List[Param]
    build: Callable[[dict, dict], Tuple[List[dict], List[Tuple[float, float]], float]] = None
    tool_hint: str = "endmill"  # what kind of tool to pick by default
    plunge: bool = True  # has a separate plunge feed
    description: str = ""  # a sentence or two for the parameters card

    def visible(self, values: dict) -> List[Param]:
        """The parameters that apply with these values (a circle pocket has no width)"""
        return [p for p in self.all_params() if p.show_if is None or values.get(p.show_if[0]) == p.show_if[1]]

    def all_params(self) -> List[Param]:
        common = [Param("tool", "Tool", TOOL, None)]
        common += self.params
        common += [Param("top_z", "Top of stock (Z)", LENGTH, 0.0, hint="Work Z of the surface the cut starts from"),
                   Param("safe_z", "Safe height (Z)", LENGTH, 5.0, hint="Rapid moves happen at this work Z"),
                   Param("rpm", "Spindle speed", RPM, 1000, 1),
                   Param("feed", "Feed" if self.plunge else "Feed rate", FEED, 300.0, 0.1)]
        if self.plunge:
            common.append(Param("plunge", "Plunge feed", FEED, 100.0, 0.1))
        common.append(Param("coolant", "Coolant", CHOICE, "Off", choices=("Off", DEFAULT_COOLANT.capitalize())))
        return common


# --- geometry helpers -------------------------------------------------------------------------

def _rectangle(cx, cy, width, length):
    """Counter-clockwise corners of a rectangle centred on (cx, cy)"""
    w, h = width / 2.0, length / 2.0
    return [[cx - w, cy - h], [cx + w, cy - h], [cx + w, cy + h], [cx - w, cy + h]]


def _arc_points(cx, cy, radius, start_deg, end_deg):
    """Points along an arc, counter-clockwise from start to end (end < start wraps once)"""
    sweep = end_deg - start_deg
    if sweep <= 0:
        sweep += 360.0
    n = max(2, int(math.ceil(sweep / ARC_STEP_DEG)))
    return [[cx + radius * math.cos(math.radians(start_deg + sweep * k / n)),
             cy + radius * math.sin(math.radians(start_deg + sweep * k / n))] for k in range(n + 1)]


def _depths(total, step):
    """Cumulative depths of each pass: equal steps no bigger than `step`, ending at `total`"""
    passes = max(1, int(math.ceil(total / step - 1e-9)))
    return [total * k / passes for k in range(1, passes + 1)]


def _positive(v, name, allow_zero=False):
    if v < 0 or (v == 0 and not allow_zero):
        raise OperationError(f"{name} must be {'zero or more' if allow_zero else 'greater than zero'}.")


# --- the operations: each returns (IR ops, XY extent points, deepest Z) ----------------------

def _feeds(v, tool):
    return {"tool": tool["tool"], "rpm": int(round(v["rpm"])), "feed_xy": v["feed"], "feed_z": v["plunge"]}


def _build_face(v, tool):
    _positive(v["width"], "Width")
    _positive(v["length"], "Length")
    _positive(v["depth"], "Depth")
    _positive(v["stepdown"], "Step down")
    margin = tool["diameter"] / 2.0 + 1.0  # plunge beside the stock, not into it
    lo = [v["x"] - margin, v["y"] - margin]
    hi = [v["x"] + v["width"] + margin, v["y"] + v["length"] + margin]
    op = dict(op="face", area={"min": lo, "max": hi}, top_z=v["top_z"], depth=v["depth"],
              stepdown=min(v["stepdown"], v["depth"]), stepover=tool["diameter"] * v["stepover"] / 100.0,
              **_feeds(v, tool))
    return [op], [(v["x"], v["y"]), (v["x"] + v["width"], v["y"] + v["length"])], v["top_z"] - v["depth"]


def _drill(v, tool, points=None, bolt_circle=None):
    _positive(v["depth"], "Depth")
    _positive(v["peck"], "Peck", allow_zero=True)
    op = dict(op="drill", tool=tool["tool"], top_z=v["top_z"], bottom_z=v["top_z"] - v["depth"],
              feed=v["feed"], rpm=int(round(v["rpm"])))
    if v["peck"] > 0:
        op["peck"] = v["peck"]
    if bolt_circle:
        op["bolt_circle"] = bolt_circle
    else:
        op["points"] = points
    return op


def _build_hole(v, tool):
    point = [v["x"], v["y"]]
    return [_drill(v, tool, points=[point])], [tuple(point)], v["top_z"] - v["depth"]


def _build_grid(v, tool):
    columns, rows = int(v["columns"]), int(v["rows"])
    if columns < 1 or rows < 1:
        raise OperationError("A grid needs at least one column and one row.")
    if (columns > 1 and v["x_spacing"] == 0) or (rows > 1 and v["y_spacing"] == 0):
        raise OperationError("Spacing can't be zero with more than one hole in that direction.")
    points = [[v["x"] + c * v["x_spacing"], v["y"] + r * v["y_spacing"]] for r in range(rows) for c in range(columns)]
    # Snake through the rows so the tool never crosses back over the whole grid
    ordered = []
    for r in range(rows):
        row = points[r * columns:(r + 1) * columns]
        ordered += row if r % 2 == 0 else row[::-1]
    return [_drill(v, tool, points=ordered)], [tuple(p) for p in ordered], v["top_z"] - v["depth"]


def _build_circle(v, tool):
    _positive(v["circle_diameter"], "Circle diameter")
    count = int(v["count"])
    if count < 1:
        raise OperationError("The circle needs at least one hole.")
    bolt = {"center": [v["x"], v["y"]], "diameter": v["circle_diameter"], "count": count,
            "start_angle_deg": v["start_angle"]}
    r = v["circle_diameter"] / 2.0
    extent = [(v["x"] - r, v["y"] - r), (v["x"] + r, v["y"] + r)]
    return [_drill(v, tool, bolt_circle=bolt)], extent, v["top_z"] - v["depth"]


def _shape(v, tool, kind):
    """boundary/circle fields for a rectangle or circle centred on (x, y)"""
    if v["shape"] == "Circle":
        _positive(v["diameter"], "Diameter")
        r = v["diameter"] / 2.0
        return {"circle": {"center": [v["x"], v["y"]], "diameter": v["diameter"]}}, \
            [(v["x"] - r, v["y"] - r), (v["x"] + r, v["y"] + r)]
    _positive(v["width"], "Width")
    _positive(v["length"], "Length")
    corners = _rectangle(v["x"], v["y"], v["width"], v["length"])
    return {"boundary": corners}, [tuple(p) for p in corners]


def _build_pocket(v, tool):
    _positive(v["depth"], "Depth")
    _positive(v["stepdown"], "Step down")
    shape, extent = _shape(v, tool, "pocket")
    smallest = v["diameter"] if v["shape"] == "Circle" else min(v["width"], v["length"])
    if smallest <= tool["diameter"]:
        raise OperationError(f"The pocket ({smallest:g}) must be wider than the tool ({tool['diameter']:g}).")
    op = dict(op="pocket_2d", top_z=v["top_z"], bottom_z=v["top_z"] - v["depth"],
              stepdown=min(v["stepdown"], v["depth"]), stepover=tool["diameter"] * v["stepover"] / 100.0,
              strategy="offset", **shape, **_feeds(v, tool))
    return [op], extent, v["top_z"] - v["depth"]


def _build_slot(v, tool):
    _positive(v["depth"], "Depth")
    _positive(v["stepdown"], "Step down")
    if v["shape"] == "Arc":
        _positive(v["radius"], "Radius")
        if v["end_angle"] == v["start_angle"]:
            raise OperationError("Start and end angles are the same: the arc has no length.")
        path = _arc_points(v["x"], v["y"], v["radius"], v["start_angle"], v["end_angle"])
    else:
        path = [[v["start_x"], v["start_y"]], [v["end_x"], v["end_y"]]]
        if math.hypot(v["end_x"] - v["start_x"], v["end_y"] - v["start_y"]) < 1e-9:
            raise OperationError("Start and end are the same point: the slot has no length.")
    ops = []
    for k, depth in enumerate(_depths(v["depth"], v["stepdown"])):
        # Back and forth: each pass starts where the last one ended
        ops.append(dict(op="engrave", path=path if k % 2 == 0 else path[::-1], top_z=v["top_z"], depth=depth,
                        **_feeds(v, tool)))
    return ops, [tuple(p) for p in path], v["top_z"] - v["depth"]


def _build_contour(v, tool):
    _positive(v["depth"], "Depth")
    _positive(v["stepdown"], "Step down")
    shape, extent = _shape(v, tool, "profile")
    side = {"Outside": "outside", "Inside": "inside", "On the line": "on"}[v["side"]]
    if side == "outside":
        r = tool["diameter"] / 2.0
        (x0, y0), (x1, y1) = (min(p[0] for p in extent), min(p[1] for p in extent)), \
                             (max(p[0] for p in extent), max(p[1] for p in extent))
        extent = [(x0 - r, y0 - r), (x1 + r, y1 + r)]
    op = dict(op="profile_2d", side=side, top_z=v["top_z"], bottom_z=v["top_z"] - v["depth"],
              stepdown=min(v["stepdown"], v["depth"]), **shape, **_feeds(v, tool))
    return [op], extent, v["top_z"] - v["depth"]


def _build_thread(v, tool):
    _positive(v["major_diameter"], "Thread diameter")
    _positive(v["pitch"], "Pitch")
    _positive(v["length"], "Thread length")
    internal = v["thread_type"] == "Internal"
    op = dict(op="thread_mill", tool=tool["tool"], points=[[v["x"], v["y"]]], major_diameter=v["major_diameter"],
              pitch=v["pitch"], top_z=v["top_z"], bottom_z=v["top_z"] - v["length"],
              thread_type="internal" if internal else "external", hand=v["hand"].lower(),
              radial_passes=max(1, int(v["passes"])), feed_xy=v["feed"], rpm=int(round(v["rpm"])))
    r = v["major_diameter"] / 2.0 + (0 if internal else 2 * tool["diameter"] + 1.0)
    return [op], [(v["x"] - r, v["y"] - r), (v["x"] + r, v["y"] + r)], v["top_z"] - v["length"]


def _build_text(v, tool):
    text = str(v["text"]).strip()
    if not text:
        raise OperationError("Type the text to engrave.")
    _positive(v["height"], "Letter height")
    _positive(v["depth"], "Depth")
    op = dict(op="text", text=text, position=[v["x"], v["y"]], height=v["height"], angle=v["angle"],
              top_z=v["top_z"], depth=v["depth"], **_feeds(v, tool))
    # Rough extent for the stock box (the font decides the real one)
    width = len(text) * v["height"] * 0.8
    a = math.radians(v["angle"])
    corners = [(v["x"] + dx * math.cos(a) - dy * math.sin(a), v["y"] + dx * math.sin(a) + dy * math.cos(a))
               for dx, dy in ((0, 0), (width, 0), (width, v["height"]), (0, v["height"]))]
    return [op], corners, v["top_z"] - v["depth"]


_XY = [Param("x", "X", LENGTH, 0.0), Param("y", "Y", LENGTH, 0.0)]
_HOLE = [Param("depth", "Hole depth", LENGTH, 5.0, 0.001),
         Param("peck", "Peck depth (0 = none)", LENGTH, 0.0, 0.0)]
_SHAPE = [Param("shape", "Shape", CHOICE, "Rectangle", choices=("Rectangle", "Circle")),
          Param("x", "Centre X", LENGTH, 0.0), Param("y", "Centre Y", LENGTH, 0.0),
          Param("width", "Width (X)", LENGTH, 40.0, 0.001, show_if=("shape", "Rectangle")),
          Param("length", "Length (Y)", LENGTH, 20.0, 0.001, show_if=("shape", "Rectangle")),
          Param("diameter", "Diameter", LENGTH, 30.0, 0.001, show_if=("shape", "Circle"))]

OPERATIONS: List[Operation] = [
    Operation("face", "Facing", "selection-all", "Flatten the top of the stock", description=(
        "Zig-zag passes over a rectangle, starting beside the stock so the tool never plunges into it. "
        "The area is the stock's top; passes overhang it by the tool radius."), params=[
        Param("x", "Start X (corner)", LENGTH, 0.0), Param("y", "Start Y (corner)", LENGTH, 0.0),
        Param("width", "Width (X)", LENGTH, 100.0, 0.001), Param("length", "Length (Y)", LENGTH, 50.0, 0.001),
        Param("depth", "Total depth", LENGTH, 0.5, 0.001), Param("stepdown", "Step down", LENGTH, 0.5, 0.001),
        Param("stepover", "Stepover", PERCENT, 60, 5, 100, hint="Of the tool diameter"),
    ], build=_build_face),
    Operation("hole", "Single Hole", "record", "Drill one hole", _XY + _HOLE, _build_hole,
              tool_hint="drill", plunge=False, description="Drill one hole, pecking if a peck depth is set."),
    Operation("grid", "Hole Grid", "dots-nine", "Rows and columns of holes", description=(
        "Holes in rows and columns from the first hole's position. Negative spacing goes the other way."), params=[
        Param("x", "First hole X", LENGTH, 0.0), Param("y", "First hole Y", LENGTH, 0.0),
        Param("columns", "Columns (along X)", COUNT, 3, 1, 100), Param("rows", "Rows (along Y)", COUNT, 2, 1, 100),
        Param("x_spacing", "X spacing", LENGTH, 20.0), Param("y_spacing", "Y spacing", LENGTH, 20.0),
    ] + _HOLE, build=_build_grid, tool_hint="drill", plunge=False),
    Operation("circle", "Hole Circle", "circle-dashed", "Holes around a centre", description=(
        "Holes evenly spaced on a circle (a bolt circle) around a centre point."), params=[
        Param("x", "Centre X", LENGTH, 0.0), Param("y", "Centre Y", LENGTH, 0.0),
        Param("circle_diameter", "Circle diameter", LENGTH, 50.0, 0.001),
        Param("count", "Number of holes", COUNT, 6, 1, 360),
        Param("start_angle", "First hole angle", ANGLE, 0.0, hint="Counter-clockwise from +X"),
    ] + _HOLE, build=_build_circle, tool_hint="drill", plunge=False),
    Operation("pocket", "Pocket", "selection", "Clear a rectangle or circle", description=(
        "Clears all the material inside a rectangle or circle, in layers no deeper than the step down."),
        params=_SHAPE + [
        Param("depth", "Depth", LENGTH, 5.0, 0.001), Param("stepdown", "Step down", LENGTH, 1.0, 0.001),
        Param("stepover", "Stepover", PERCENT, 40, 5, 100, hint="Of the tool diameter"),
    ], build=_build_pocket),
    Operation("slot", "Slotting / Grooving", "line-segment", "A straight or curved track", description=(
        "A track as wide as the tool, along a line or an arc, cut back and forth in layers."), params=[
        Param("shape", "Path", CHOICE, "Straight", choices=("Straight", "Arc")),
        Param("start_x", "Start X", LENGTH, 0.0, show_if=("shape", "Straight")),
        Param("start_y", "Start Y", LENGTH, 0.0, show_if=("shape", "Straight")),
        Param("end_x", "End X", LENGTH, 50.0, show_if=("shape", "Straight")),
        Param("end_y", "End Y", LENGTH, 0.0, show_if=("shape", "Straight")),
        Param("x", "Arc centre X", LENGTH, 0.0, show_if=("shape", "Arc")),
        Param("y", "Arc centre Y", LENGTH, 0.0, show_if=("shape", "Arc")),
        Param("radius", "Arc radius", LENGTH, 25.0, 0.001, show_if=("shape", "Arc")),
        Param("start_angle", "Start angle", ANGLE, 0.0, show_if=("shape", "Arc"), hint="Counter-clockwise from +X"),
        Param("end_angle", "End angle", ANGLE, 90.0, show_if=("shape", "Arc"), hint="Cut counter-clockwise to here"),
        Param("depth", "Depth", LENGTH, 3.0, 0.001), Param("stepdown", "Step down", LENGTH, 1.0, 0.001),
    ], build=_build_slot),
    Operation("contour", "Contour / Profile", "polygon", "Cut around a shape", description=(
        "Follows the outline of a rectangle or circle: outside it to cut a part out, inside it to cut a "
        "hole, or on the line."), params=_SHAPE + [
        Param("side", "Cut", CHOICE, "Outside", choices=("Outside", "Inside", "On the line")),
        Param("depth", "Depth", LENGTH, 5.0, 0.001), Param("stepdown", "Step down", LENGTH, 1.0, 0.001),
    ], build=_build_contour),
    Operation("thread", "Thread Milling", "nut", "Internal or external thread", description=(
        "A thread mill follows a helix, one pitch per turn. Internal threads need the hole drilled to the "
        "minor diameter first; the tool climbs from the bottom for right-hand threads."), params=_XY + [
        Param("thread_type", "Thread", CHOICE, "Internal", choices=("Internal", "External")),
        Param("major_diameter", "Thread diameter (major)", LENGTH, 10.0, 0.001, hint="e.g. 10 for M10"),
        Param("pitch", "Pitch", LENGTH, 1.5, 0.001, hint="Distance per turn, e.g. 1.5 for M10"),
        Param("length", "Thread length", LENGTH, 10.0, 0.001),
        Param("hand", "Hand", CHOICE, "Right", choices=("Right", "Left")),
        Param("passes", "Radial passes", COUNT, 1, 1, 10),
    ], build=_build_thread, tool_hint="thread", plunge=False),
    Operation("text", "Text / Engraving", "text-aa", "Engrave text", description=(
        "Engraves text in a single-line font, starting from its lower-left corner."), params=[
        Param("text", "Text", TEXT, "MILO"),
        Param("x", "Start X (lower left)", LENGTH, 0.0), Param("y", "Start Y (lower left)", LENGTH, 0.0),
        Param("height", "Letter height", LENGTH, 10.0, 0.001), Param("angle", "Angle", ANGLE, 0.0),
        Param("depth", "Depth", LENGTH, 0.3, 0.001),
    ], build=_build_text, tool_hint="engrave"),
]
BY_KEY: Dict[str, Operation] = {op.key: op for op in OPERATIONS}


# --- defaults and tools -------------------------------------------------------------------------

def _nice(value: float) -> float:
    """An inch default rounded to something an operator would type"""
    return float(f"{value:.3g}")


def default_values(op: Operation, units: str = "mm", spindle_default: Optional[float] = None) -> dict:
    """Every parameter's default, in the machine's units"""
    values = {}
    for p in op.all_params():
        value = p.default
        if p.kind in SCALED and units == "inch" and isinstance(value, (int, float)):
            value = _nice(value / 25.4)
        values[p.key] = value
    if spindle_default:
        values["rpm"] = spindle_default
    return values


TOOL_WORDS = {"drill": ("drill",), "thread": ("thread",), "engrave": ("engrav", "v-bit", "vbit", "chamfer", "°", "deg")}


def _matches(tool: dict, hint: str) -> bool:
    text = f"{tool.get('type', '')} {tool.get('description', '')}".lower()
    words = TOOL_WORDS.get(hint)
    if words:
        return any(w in text for w in words)
    # An end mill: not one of the special kinds
    return tool.get("type") == "endmill" and not any(w in text for ws in TOOL_WORDS.values() for w in ws)


def default_tool(tools: Sequence[dict], hint: str, in_spindle: Optional[int] = None) -> Optional[dict]:
    """The tool an operation most likely wants: a drill for holes, a thread mill for threads,
    an engraving bit for text, otherwise an end mill. The tool in the spindle wins when it's
    that kind (no tool change)."""
    usable = [t for t in tools if t.get("usable", True) and t.get("diameter")]
    if not usable:
        return None
    usable.sort(key=lambda t: t["tool"] != in_spindle)  # the spindle's tool first, otherwise table order
    for t in usable:
        if _matches(t, hint):
            return t
    for t in usable:
        if _matches(t, "endmill"):
            return t
    return usable[0]


def describe_tool(tool: dict, units: str, limit: int = 34) -> str:
    """'T8 · ⌀6.35 mm · 1/4" 4-flute endmill', the description shortened to `limit` characters"""
    description = tool.get("description", "")
    if len(description) > limit:
        description = description[:limit - 1].rstrip() + "…"
    return f"T{tool['tool']} · ⌀{tool['diameter']:g} {units} · {description}".strip(" ·")


# --- the program --------------------------------------------------------------------------------

def build_program(key: str, values: dict, tool: Optional[dict], units: str = "mm",
                  max_rpm: Optional[float] = None) -> dict:
    """CAM IR for one operation. Raises OperationError if the parameters don't make sense."""
    op = BY_KEY[key]
    if tool is None:
        raise OperationError("Choose a tool. Only tools in the tool table with a diameter can be used.")
    if not tool.get("diameter"):
        raise OperationError(f"T{tool['tool']} has no diameter in the tool table.")
    v = dict(default_values(op, units))
    v.update(values)
    for name in ("rpm", "feed") + (("plunge",) if op.plunge else ()):
        _positive(float(v[name]), {"rpm": "Spindle speed", "feed": "Feed", "plunge": "Plunge feed"}[name])
    if max_rpm and v["rpm"] > max_rpm:
        raise OperationError(f"Spindle speed {v['rpm']:g} is above the machine's maximum of {max_rpm:g} rpm.")
    if v["safe_z"] <= v["top_z"]:
        raise OperationError("The safe height must be above the top of the stock.")

    ops, extent, deepest = op.build(v, tool)
    coolant = "none" if v.get("coolant", "Off") == "Off" else DEFAULT_COOLANT
    for o in ops:
        o["coolant"] = coolant
    margin = tool["diameter"] / 2.0 + (1.0 if units == "mm" else 0.04)
    xs, ys = [p[0] for p in extent], [p[1] for p in extent]
    stock = {"min": [min(xs) - margin, min(ys) - margin, deepest - margin],
             "max": [max(xs) + margin, max(ys) + margin, v["top_z"]]}
    ir_tool = {"tool": tool["tool"], "type": tool.get("type") if tool.get("type") in ("endmill", "drill", "ball_endmill")
               else "endmill", "diameter": tool["diameter"], "flutes": tool.get("flutes") or 2,
               "description": tool.get("description", "")}
    return {"version": "1.0", "units": units, "stock": stock, "clearance_z": v["safe_z"], "safe_z": v["safe_z"],
            "tools": [ir_tool], "ops": ops,
            "post": {"dialect": "linuxcnc", "program_number": 1, "spindle": "CW"}}
