"""
Probing jobs for the Probe page: what to find (a corner, an edge, a hole...), how the probe is set up,
which of qtvcp's probe routines measures it, what the probe will do (for the preview and the soft
limit check), where to put the probe first, and what the result means for a work offset.
No Qt and no machine here.

The routines are qtvcp's (qtvcp/widgets/probe_routines.py), run by milo_probe_subprog.py. Each
starts from where the probe is: it steps out by the XY clearance (and along an edge by the edge
length), goes down by the Z clearance, and searches up to the max travel. Their names give the
probing directions: probe_outside_xpyp probes in +X and +Y, so it finds a part's front-left corner.
Results come back in work coordinates, probe radius already allowed for.
"""

import math
import re
from dataclasses import asdict, dataclass, fields
from typing import Dict, List, Optional, Tuple

# goal -> (title, icon, one line on what it finds)
GOALS = {
    "corner": ("Corner", "frame-corners", "Set X and Y on a corner"),
    "edge": ("Edge", "align-left", "Set X or Y on one edge"),
    "hole": ("Hole centre", "circle-dashed", "Centre of a hole or pocket"),
    "boss": ("Boss centre", "circle", "Round or square boss"),
    "surface": ("Top surface", "arrow-line-down", "Set Z on the top"),
    "angle": ("Part angle", "compass", "How square the part sits"),
    "tool": ("Tool length", "ruler", "Tool setter or touch plate"),
}
PROBE_GOALS = ("corner", "edge", "hole", "boss", "surface", "angle")  # the ones run by the probe routines

CORNERS = {"back_left": "back-left", "back_right": "back-right", "front_left": "front-left",
           "front_right": "front-right"}
EDGES = {"left": "left", "right": "right", "front": "front", "back": "back"}
SHAPES = ("round", "rectangle")
WCS = ("G54", "G55", "G56", "G57", "G58", "G59")


def corner_signs(corner: str) -> Tuple[int, int]:
    """(+1 for a left corner, -1 right), (+1 front, -1 back): the directions into the part"""
    return (1 if corner.endswith("left") else -1), (1 if corner.startswith("front") else -1)


# Probing directions -> routine. Outside a part the probe moves toward it (into the corner's
# faces); inside a pocket it moves toward the walls, so the same directions mean the opposite corner.
CORNER_ROUTINES = {(1, 1): "xpyp", (1, -1): "xpym", (-1, 1): "xmyp", (-1, -1): "xmym"}
EDGE_DIRECTIONS = {"left": (1, 0), "right": (-1, 0), "front": (0, 1), "back": (0, -1)}
EDGE_ROUTINES = {"left": "probe_xp", "right": "probe_xm", "front": "probe_yp", "back": "probe_ym"}
ANGLE_ROUTINES = {"left": "probe_angle_xp", "right": "probe_angle_xm", "front": "probe_angle_yp",
                  "back": "probe_angle_ym"}


@dataclass
class ProbeSetup:
    """The probe itself: set once on the Probe setup screen (lengths mm, feeds mm/min)"""
    tool: int = 99                  # the probe's tool number
    tip_diameter: float = 2.0       # effective ball diameter (calibrate it on a ring gauge)
    travel_feed: float = 600.0      # moving between touches
    search_feed: float = 200.0      # first, fast touch
    probe_feed: float = 25.0        # second, slow touch that is measured
    max_travel: float = 15.0        # how far each XY touch searches
    max_z_travel: float = 15.0      # how far the top-surface touch searches
    step_off: float = 2.0           # back off this far between the fast and slow touches
    xy_clearance: float = 5.0       # step this far out past an edge before going down
    z_clearance: float = 5.0        # go down this far beside the part
    extra_depth: float = 0.0        # ...and this much more
    edge_length: float = 10.0       # how far along an edge from the corner to touch it

    @classmethod
    def from_prefs(cls, prefs, units: str = "mm") -> "ProbeSetup":
        """Saved setup (prefs "probe_setup"), defaults scaled for an inch machine"""
        setup = cls()
        if units == "inch":
            for f in fields(cls):
                if isinstance(getattr(setup, f.name), float):
                    setattr(setup, f.name, round(getattr(setup, f.name) / 25.4, 4))
        saved = prefs.get("probe_setup") or {}
        if "tool" not in saved and prefs.get("vision.probe_tool") is not None:
            saved = dict(saved, tool=prefs.get("vision.probe_tool"))  # set on the Vision page before
        if "tip_diameter" not in saved and prefs.get("vision.probe_tip") is not None:
            saved = dict(saved, tip_diameter=prefs.get("vision.probe_tip"))
        for f in fields(cls):
            if f.name in saved:
                try:
                    setattr(setup, f.name, type(getattr(setup, f.name))(saved[f.name]))
                except (TypeError, ValueError):
                    pass
        return setup

    def save(self, prefs):
        prefs.set("probe_setup", asdict(self))

    @property
    def lowest(self) -> float:
        """How far below the start a side touch happens"""
        return self.z_clearance + self.extra_depth

    @property
    def start_height(self) -> float:
        """How far above the top to start, so side touches land ~3 mm below it"""
        return max(1.0 if self.z_clearance > 2 else 0.5, round(self.lowest - 3.0, 1))

    @property
    def inset(self) -> float:
        """How far in from an edge (or out from a wall) to start: well inside the clearance"""
        return round(min(self.xy_clearance * 0.5, max(0.5, self.max_travel - self.xy_clearance - 1)), 1)


@dataclass
class ProbeJob:
    """One thing to find, with the operator's answers"""
    goal: str = "corner"
    corner: str = "front_left"
    inside: bool = False            # a pocket's corner instead of the part's
    edge: str = "left"
    shape: str = "round"            # hole / boss
    diameter: float = 20.0          # round hole / boss, roughly
    width: float = 40.0             # rectangular hole / boss, X
    length: float = 20.0            # rectangular hole / boss, Y
    repeat: bool = False            # touch twice and compare
    wcs: str = "G54"                # where the result goes

    def describe(self) -> str:
        """'the front-left outside corner', 'the centre of a 20 mm hole' (no units: see describe_job)"""
        if self.goal == "corner":
            return f"the {CORNERS[self.corner]} {'inside' if self.inside else 'outside'} corner"
        if self.goal == "edge":
            return f"the {self.edge} edge"
        if self.goal == "angle":
            return f"the angle of the {self.edge} edge"
        if self.goal == "surface":
            return "the top surface"
        kind = "hole" if self.goal == "hole" else "boss"
        return f"the centre of the {kind}"

    @property
    def axes(self) -> str:
        """The work offset axes this job sets"""
        if self.goal in ("corner", "hole", "boss"):
            return "XY"
        if self.goal == "edge":
            return "X" if self.edge in ("left", "right") else "Y"
        if self.goal == "surface":
            return "Z"
        return ""  # angle: a rotation, not an origin


# --- the routine ------------------------------------------------------------------------------

def directions(job: ProbeJob) -> Tuple[int, int]:
    """The directions the probe moves to touch (0 for an axis it doesn't touch)"""
    if job.goal == "corner":
        sx, sy = corner_signs(job.corner)
        return (-sx, -sy) if job.inside else (sx, sy)
    if job.goal in ("edge", "angle"):
        return EDGE_DIRECTIONS[job.edge]
    return 0, 0


def routine(job: ProbeJob) -> str:
    """The qtvcp probe routine for a job"""
    if job.goal == "corner":
        return f"probe_{'inside' if job.inside else 'outside'}_{CORNER_ROUTINES[directions(job)]}"
    if job.goal == "edge":
        return EDGE_ROUTINES[job.edge]
    if job.goal == "angle":
        return ANGLE_ROUTINES[job.edge]
    if job.goal == "hole":
        return "probe_round_pocket" if job.shape == "round" else "probe_rectangular_pocket"
    if job.goal == "boss":
        # Round bosses too: qtvcp's round boss routine goes down only the step-off distance past the
        # far side, so a boss a little bigger or off centre gets hit. The rectangular one clears it.
        return "probe_rectangular_boss"
    if job.goal == "surface":
        return "probe_down"
    raise ValueError(f"No probe routine for {job.goal}")


def parameters(job: ProbeJob, setup: ProbeSetup) -> Dict[str, str]:
    """What qtvcp's probe subprogram reads (all strings, like its own widget sends). Auto-zero and
    auto-skew stay off: the page sets the work offset itself, after showing the result."""
    width, length = (job.diameter, job.diameter) if job.shape == "round" else (job.width, job.length)
    values = {
        "probe_diam": setup.tip_diameter, "latch_return_dist": setup.step_off, "max_travel": setup.max_travel,
        "max_z_travel": setup.max_z_travel, "search_vel": setup.search_feed, "probe_vel": setup.probe_feed,
        "rapid_vel": setup.travel_feed, "side_edge_length": setup.edge_length, "xy_clearance": setup.xy_clearance,
        "z_clearance": setup.z_clearance, "extra_depth": setup.extra_depth,
        "diameter_hint": job.diameter, "x_hint_bp": width, "y_hint_bp": length,
        "x_hint_rv": 0.0, "y_hint_rv": 0.0, "cal_diameter": 0.0, "cal_x_width": 0.0, "cal_y_width": 0.0,
        "calibration_offset": 0.0,
    }
    data = {k: f"{v:g}" for k, v in values.items()}
    data.update(allow_auto_zero="0", allow_auto_skew="0", cal_avg_error="0", cal_x_error="0", cal_y_error="0")
    return data


# --- what it will do ----------------------------------------------------------------------------

@dataclass
class Touch:
    start: Tuple[float, float]      # XY relative to where the probe starts
    direction: Tuple[float, float]  # unit vector; (0, 0) = straight down
    travel: float                   # how far it searches
    contact: Optional[Tuple[float, float]] = None  # where it should touch the nominal part


@dataclass
class Plan:
    touches: List[Touch]
    path: List[Tuple[float, float]]  # XY waypoints from the start, in order
    lowest: float                    # deepest Z below the start (positive)
    part: dict                       # the nominal part, for the drawing: {"kind": ..., ...}
    start_hint: str                  # where to put the probe, in words


def plan(job: ProbeJob, setup: ProbeSetup, units: str = "mm") -> Plan:
    """What the routine will do, relative to its start, and the nominal part it expects there.
    The geometry mirrors the moves in qtvcp's routines (checked against them in the tests)."""
    c, e, travel, d = setup.xy_clearance, setup.edge_length, setup.max_travel, setup.inset
    h = setup.start_height
    u = "mm" if units == "mm" else "in"
    height = f"with the tip about {h:g} {u} above the top"
    if job.goal == "corner":
        dx, dy = directions(job)
        if not job.inside:
            p1, p2 = (-dx * c, dy * e), (dx * e, -dy * c)
            corner = (-dx * d, -dy * d)  # the start is d in from both faces
            contacts = [(corner[0], p1[1]), (p2[0], corner[1])]
            part = {"kind": "block", "corner": corner, "into": (dx, dy)}
            hint = (f"Over the part, about {d:g} {u} in from both edges at its {CORNERS[job.corner]} corner, "
                    f"{height}.")
        else:
            p1, p2 = (-dx * c, -dy * e), (-dx * e, -dy * c)
            corner = (dx * d, dy * d)  # walls d away in the probing directions
            contacts = [(corner[0], p1[1]), (p2[0], corner[1])]
            part = {"kind": "pocket_corner", "corner": corner, "walls": (dx, dy)}
            hint = (f"Inside the pocket, about {d:g} {u} from both walls of its {CORNERS[job.corner]} corner, "
                    f"{height}.")
        touches = [Touch(p1, (dx, 0), travel, contacts[0]), Touch(p2, (0, dy), travel, contacts[1])]
        # Afterwards the routine moves to the corner it found
        return Plan(touches, [(0, 0), p1, p2, corner], setup.lowest, part, hint)
    if job.goal in ("edge", "angle"):
        dx, dy = EDGE_DIRECTIONS[job.edge]

        def on_face(point):
            """Where a touch from `point` meets the face, which is d behind the start"""
            return (-dx * d, point[1]) if dx else (point[0], -dy * d)
        p1 = (-dx * c, -dy * c)
        touches = [Touch(p1, (dx, dy), travel, on_face(p1))]
        path = [(0, 0), p1]
        extra = ""
        if job.goal == "angle":
            along = (dy, -dx)  # the second touch is this way along the edge
            p2 = (p1[0] + along[0] * e, p1[1] + along[1] * e)
            touches.append(Touch(p2, (dx, dy), travel, on_face(p2)))
            path.append(p2)
            end = {"front": "left", "back": "right", "left": "back", "right": "front"}[job.edge]
            extra = f" It touches the edge twice, {e:g} {u} apart, so start near the edge's {end} end."
        part = {"kind": "block_edge", "into": (dx, dy), "face": -d}
        hint = f"Over the part, about {d:g} {u} in from its {job.edge} edge, {height}.{extra}"
        return Plan(touches, path + [(0, 0)], setup.lowest, part, hint)
    if job.goal == "hole":
        if job.shape == "round":
            r = job.diameter / 2
            first = r - c
            p1 = (-first, 0.0) if first > 0 else (0.0, 0.0)
            step = 2 * r - setup.step_off - c
            p2 = (p1[0] + step, 0.0) if step > 0 else p1
            p3 = (0.0, -first) if first > 0 else (0.0, 0.0)
            p4 = (0.0, p3[1] + step) if step > 0 else p3
            touches = [Touch(p1, (-1, 0), travel, (-r, 0)), Touch(p2, (1, 0), travel, (r, 0)),
                       Touch(p3, (0, -1), travel, (0, -r)), Touch(p4, (0, 1), travel, (0, r))]
            part = {"kind": "hole", "radius": r}
        else:
            hw, hl = job.width / 2, job.length / 2
            p1, p2 = (-(hw - c), 0.0), (hw - c, 0.0)
            p3, p4 = (0.0, -(hl - c)), (0.0, hl - c)
            touches = [Touch(p1, (-1, 0), travel, (-hw, 0)), Touch(p2, (1, 0), travel, (hw, 0)),
                       Touch(p3, (0, -1), travel, (0, -hl)), Touch(p4, (0, 1), travel, (0, hl))]
            part = {"kind": "pocket", "half": (hw, hl)}
        hint = f"Over the middle of the hole (by eye is fine), {height}. The probe goes down inside it."
        return Plan(touches, [(0, 0)] + [t.start for t in touches] + [(0, 0)], setup.lowest, part, hint)
    if job.goal == "boss":
        hw, hl = (job.diameter / 2, job.diameter / 2) if job.shape == "round" else (job.width / 2, job.length / 2)
        p1, p2 = (-(hw + c), 0.0), (hw + c, 0.0)
        p3, p4 = (0.0, -(hl + c)), (0.0, hl + c)
        touches = [Touch(p1, (1, 0), travel, (-hw, 0)), Touch(p2, (-1, 0), travel, (hw, 0)),
                   Touch(p3, (0, 1), travel, (0, -hl)), Touch(p4, (0, -1), travel, (0, hl))]
        part = {"kind": "boss", "radius": hw} if job.shape == "round" else {"kind": "boss_rect", "half": (hw, hl)}
        hint = (f"Over the middle of the boss (by eye is fine), {height}. The probe goes down "
                f"{c:g} {u} outside each side.")
        return Plan(touches, [(0, 0)] + [t.start for t in touches] + [(0, 0)], setup.lowest, part, hint)
    if job.goal == "surface":
        touches = [Touch((0.0, 0.0), (0, 0), setup.max_z_travel)]
        hint = f"Over the surface, with the tip less than {setup.max_z_travel:g} {u} above it."
        return Plan(touches, [(0, 0)], setup.max_z_travel, {"kind": "surface"}, hint)
    raise ValueError(f"No plan for {job.goal}")


def footprint(p: Plan) -> Tuple[float, float, float, float]:
    """(min x, min y, max x, max y) the probe can reach, relative to the start"""
    xs, ys = [0.0], [0.0]
    for x, y in p.path:
        xs.append(x)
        ys.append(y)
    for t in p.touches:
        xs += [t.start[0], t.start[0] + t.direction[0] * t.travel]
        ys += [t.start[1], t.start[1] + t.direction[1] * t.travel]
    return min(xs), min(ys), max(xs), max(ys)


def limit_problem(p: Plan, start: Tuple[float, float, float], limits: Dict[str, Tuple[float, float]],
                  tip_radius: float = 0.0) -> Optional[str]:
    """Why the probe would leave the soft limits from this machine position, or None"""
    x0, y0, x1, y1 = footprint(p)
    reach = {"X": (start[0] + x0, start[0] + x1), "Y": (start[1] + y0, start[1] + y1),
             "Z": (start[2] - p.lowest, start[2])}
    for axis, (lo, hi) in reach.items():
        if axis not in limits:
            continue
        low, high = limits[axis]
        if lo < low - 1e-6 or hi > high + 1e-6:
            return (f"From here the probe would reach {axis} {lo:.1f} to {hi:.1f} (machine), outside the "
                    f"soft limits {low:g} to {high:g}. Move it further from the end of travel.")
    return None


# --- the result ---------------------------------------------------------------------------------

@dataclass
class Found:
    """What a routine measured, in work coordinates of the active system (machine units)"""
    values: Dict[str, float]        # axis -> work coordinate of the feature
    size: Optional[Tuple[float, ...]] = None  # diameter, or (width, length)
    angle: Optional[float] = None   # degrees, for the angle job


def _number(data, key) -> Optional[float]:
    value = data.get(key)
    try:
        return None if value in (None, "None", "") else float(value)
    except (TypeError, ValueError):
        return None


def interpret(job: ProbeJob, data: Dict[str, str]) -> Found:
    """Read the routine's results. Raises ValueError if what the job needs is missing."""
    def need(key):
        value = _number(data, key)
        if value is None:
            raise ValueError(f"The probe routine didn't report {key}")
        return value
    if job.goal in ("corner", "edge"):
        dx, dy = directions(job)
        values = {}
        if dx:
            values["X"] = need("xp" if dx > 0 else "xm")
        if dy:
            values["Y"] = need("yp" if dy > 0 else "ym")
        return Found(values)
    if job.goal in ("hole", "boss"):
        values = {"X": need("xc"), "Y": need("yc")}
        if job.shape == "round":
            d = _number(data, "d")
            if d is None:
                d = (need("lx") + need("ly")) / 2
            return Found(values, (d,))
        return Found(values, (need("lx"), need("ly")))
    if job.goal == "surface":
        return Found({"Z": need("z")})
    if job.goal == "angle":
        return Found({}, angle=need("a"))
    raise ValueError(f"Nothing to interpret for {job.goal}")


def spread(a: Found, b: Found) -> float:
    """The biggest difference between two runs (positions and sizes)"""
    diffs = [abs(a.values[k] - b.values[k]) for k in a.values if k in b.values]
    if a.size and b.size:
        diffs += [abs(x - y) for x, y in zip(a.size, b.size)]
    if a.angle is not None and b.angle is not None:
        diffs.append(abs(a.angle - b.angle))
    return max(diffs) if diffs else 0.0


def size_warning(job: ProbeJob, found: Found, units: str = "mm") -> Optional[str]:
    """When a measured hole or boss is far from the size given (the wrong feature, a chip, a bad hint)"""
    if job.goal not in ("hole", "boss") or not found.size:
        return None
    expected = (job.diameter,) if job.shape == "round" else (job.width, job.length)
    tolerance = 1.0 if units == "mm" else 0.04
    for got, want in zip(found.size, expected):
        if abs(got - want) > max(tolerance, 0.1 * want):
            u = "mm" if units == "mm" else "in"
            return (f"It measured {got:.3f} {u}, but you said about {want:g} {u}. Check it's the feature you "
                    f"meant before using the result.")
    return None


def new_origin(found: Found, active_offset: List[float]) -> Dict[str, float]:
    """The work offset that puts the found feature at 0, for any work system: the feature's
    machine position minus G92 (which cancels out: work = machine - g5x - g92 - tool)"""
    index = {"X": 0, "Y": 1, "Z": 2}
    return {axis: active_offset[index[axis]] + value for axis, value in found.values.items()}


def describe_found(job: ProbeJob, found: Found, units: str = "mm") -> str:
    """The headline: 'Corner found at X 12.345 Y 8.210' (work coordinates)"""
    u = "mm" if units == "mm" else "in"
    places = 3 if units == "mm" else 4
    where = "  ".join(f"{k} {v:.{places}f}" for k, v in found.values.items())
    if job.goal == "angle":
        return f"The {job.edge} edge is turned {found.angle:+.3f}° from the machine's {'X' if job.edge in ('front', 'back') else 'Y'} axis."
    title = {"corner": "Corner", "edge": "Edge", "hole": "Hole centre", "boss": "Boss centre",
             "surface": "Top surface"}[job.goal]
    text = f"{title} found at {where}"
    if found.size:
        if len(found.size) == 1:
            text += f", ⌀{found.size[0]:.{places}f} {u}"
        else:
            text += f", {found.size[0]:.{places}f} × {found.size[1]:.{places}f} {u}"
    return text + " (current work coordinates)."


def proposal_summary(job: ProbeJob) -> str:
    """For the confirmation sheet: what will happen, including setting the offset"""
    axes = job.axes
    tail = f" and set {job.wcs} " + " ".join(f"{a}0" for a in axes) + " there" if axes else ""
    if job.goal == "angle":
        tail = " and show how far it's turned"
    return f"Probe {job.describe()}{tail}" + (" (twice, to compare)" if job.repeat else "")


# --- calibration and safety helpers ---------------------------------------------------------------

def effective_tip(true_diameter: float, measured_diameter: float, tip_used: float) -> float:
    """The tip diameter that makes a ring gauge measure its true size. A hole measures
    true - (effective tip) + (tip used), so a measured hole that's too small means a bigger tip."""
    return true_diameter - measured_diameter + tip_used


_TOOL_CHANGE_RE = re.compile(r"\bM0*6\b")
_SPINDLE_RE = re.compile(r"\bM0*[34]\b")


def spins_before_tool_change(gcode: str) -> bool:
    """True if a program starts the spindle before its first tool change (with the probe in the
    spindle that would spin the probe)"""
    for line in gcode.splitlines():
        code = re.sub(r"\(.*?\)|;.*", "", line).upper()
        if _TOOL_CHANGE_RE.search(code):
            return False
        if _SPINDLE_RE.search(code):
            return True
    return False


def starts_spindle(command: str) -> bool:
    """An MDI line that starts the spindle (M3 / M4)"""
    return bool(_SPINDLE_RE.search(re.sub(r"\(.*?\)|;.*", "", command).upper()))
