"""
Backlash: measuring the slack in an axis and setting LinuxCNC's compensation for it.

An axis with backlash ends up in a slightly different place depending on which way it arrived:
moving +, the table lags behind the screw by half the slack; moving -, it's ahead by half. So
Milo brings the axis to the same commanded spot from both sides and something fixed watches
where it really is (a dial indicator the operator reads, an AprilTag the camera sees, or the
line laser's height on the table). Each repeat is three stops:

    A  the start point, arriving moving +
    C  one test step further on, still arriving moving +   (how far the gauge reads per mm)
    B  the start point again, arriving moving -            (B - A is the backlash)

The step makes the measurement self-scaling: whatever the gauge reads in (indicator mm either
way round, camera pixels, laser rows), the readings are fitted as

    reading = a + k * commanded + k * backlash * (1 if it arrived moving - else 0)

so the backlash comes out in machine mm without a calibrated camera or knowing which way the
indicator's plunger points. Camera readings are 2D (a tag's centre in pixels): they're projected
onto the direction the tag moves in the image first.

The compensation itself is LinuxCNC's: BACKLASH in [JOINT_n] of the INI (read when LinuxCNC
starts). The motion planner then adds the extra travel on every reversal, which the step
generator has to have the acceleration for: STEPGEN_MAXACCEL should be twice MAX_ACCELERATION.
"""

import math
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple, Union

AXES = "XYZ"
METHODS = ("indicator", "camera")
MAX_SENSIBLE = 0.3       # mm: more than this is worth fixing mechanically, not just compensating
SPREAD_LIMIT = 0.01      # mm: repeats further apart than this (or a quarter of the backlash) are flagged
COMMENT = "# Backlash measured by Milo (Calibrate page)"


@dataclass
class Settings:
    approach: float = 1.0    # mm: each approach starts this far back (more than the slack)
    step: float = 0.5        # mm: the test step that scales the readings
    repeats: int = 3
    feed: float = 300.0      # mm/min: the final approach

    @staticmethod
    def from_prefs(prefs) -> "Settings":
        saved = prefs.get("calibrate.backlash_settings") or {}
        known = {k: v for k, v in saved.items() if k in Settings.__dataclass_fields__}
        try:
            return Settings(**known)
        except TypeError:
            return Settings()

    def save(self, prefs):
        prefs.set("calibrate.backlash_settings", asdict(self))


@dataclass
class Stop:
    axis: int          # 0 X, 1 Y, 2 Z
    target: float      # machine position the axis stops at
    direction: int     # +1: arrives moving +, -1: arrives moving -
    start: float       # where the final approach starts
    repeat: int
    kind: str          # "plus" (A), "step" (C) or "minus" (B)

    def describe(self, units="mm") -> str:
        name = AXES[self.axis]
        side = f"moving + (toward +{name})" if self.direction > 0 else f"moving − (toward −{name})"
        return f"{name} {self.target:.3f} {units}, arriving {side}"


def plan(axis: int, origin: float, settings: Settings) -> List[Stop]:
    """The stops for one axis measured around machine position `origin`"""
    stops = []
    a, s = abs(settings.approach), abs(settings.step)
    for r in range(max(1, int(settings.repeats))):
        stops.append(Stop(axis, origin, +1, origin - a, r, "plus"))
        stops.append(Stop(axis, origin + s, +1, origin + s - a, r, "step"))
        stops.append(Stop(axis, origin, -1, origin + a, r, "minus"))
    return stops


def travel(stops: Sequence[Stop]) -> Tuple[float, float]:
    """Lowest and highest position the measurement goes to"""
    points = [p for stop in stops for p in (stop.start, stop.target)]
    return min(points), max(points)


def limit_problem(stops: Sequence[Stop], limits: Dict[str, Tuple[float, float]]) -> Optional[str]:
    if not stops:
        return None
    name = AXES[stops[0].axis]
    lo, hi = travel(stops)
    low, high = limits.get(name, (-math.inf, math.inf))
    if lo < low - 1e-6:
        return f"It would go {low - lo:.2f} mm past {name}'s lower soft limit: move {name} up by that much first."
    if hi > high + 1e-6:
        return f"It would go {hi - high:.2f} mm past {name}'s upper soft limit: move {name} down by that much first."
    return None


# --- the fit -------------------------------------------------------------------------------------

Value = Union[float, Tuple[float, float]]


@dataclass
class Reading:
    stop: Stop
    value: Value


@dataclass
class Result:
    axis: str
    method: str
    backlash: float          # mm, measured (with the compensation that was running)
    spread: float            # mm, range of the per-repeat values
    repeats: List[float]     # mm, each repeat's value
    scale: float             # gauge units per mm (indicator ~±1; camera px/mm; laser rows/mm)
    noise: float             # mm, how far the + readings sit from a straight line
    active: float            # BACKLASH LinuxCNC was running with while measuring
    warnings: List[str] = field(default_factory=list)
    when: float = 0.0

    @property
    def suggested(self) -> float:
        """BACKLASH to set: what was running plus what's still there"""
        return round(max(0.0, self.active + self.backlash), 4)

    def to_dict(self):
        return asdict(self)

    @staticmethod
    def from_dict(d) -> Optional["Result"]:
        try:
            return Result(**{k: v for k, v in d.items() if k in Result.__dataclass_fields__})
        except TypeError:
            return None


def _lstsq(rows, values):
    import numpy as np
    solution, *_ = np.linalg.lstsq(np.asarray(rows, float), np.asarray(values, float), rcond=None)
    return solution


def _scalars(readings: Sequence[Reading]) -> List[float]:
    """Readings as numbers: 2D camera readings projected onto the way the tag moves"""
    import numpy as np
    if not readings or not isinstance(readings[0].value, (tuple, list)):
        return [float(r.value) for r in readings]
    plus = [r for r in readings if r.stop.direction > 0]
    rows = [[1.0, r.stop.target] for r in plus]
    fit = _lstsq(rows, [list(r.value) for r in plus])   # 2 x 2: intercept and px per mm, per image axis
    direction = np.asarray(fit[1], float)
    length = float(np.hypot(*direction))
    if length < 1e-9:
        raise ValueError("The target didn't move in the camera's view")
    unit = direction / length
    return [float(np.dot(np.asarray(r.value, float), unit)) for r in readings]


def analyse(readings: Sequence[Reading], settings: Settings, method: str, active: float = 0.0,
            now: Optional[float] = None) -> Result:
    """Backlash from a measurement's readings (see the module docstring)"""
    import numpy as np
    if len(readings) < 3:
        raise ValueError("Not enough readings")
    axis = AXES[readings[0].stop.axis]
    values = _scalars(readings)
    rows = [[1.0, r.stop.target, 1.0 if r.stop.direction < 0 else 0.0] for r in readings]
    a, k, c = _lstsq(rows, values)
    if abs(k) < 1e-9:
        raise ValueError("The readings didn't change with the test step: is the gauge touching?")
    backlash = float(c / k)
    per_repeat = []
    for r in sorted({x.stop.repeat for x in readings}):
        minus = [(v - a - k * x.stop.target) / k for x, v in zip(readings, values)
                 if x.stop.repeat == r and x.stop.direction < 0]
        if minus:
            per_repeat.append(float(np.mean(minus)))
    spread = (max(per_repeat) - min(per_repeat)) if len(per_repeat) > 1 else 0.0
    plus = [(v - a - k * x.stop.target) / k for x, v in zip(readings, values) if x.stop.direction > 0]
    noise = float(np.sqrt(np.mean(np.square(plus)))) if plus else 0.0
    result = Result(axis, method, round(backlash, 4), round(spread, 4), [round(v, 4) for v in per_repeat],
                    float(k), round(noise, 4), float(active), when=now if now is not None else time.time())
    result.warnings = warnings(result, settings)
    return result


def warnings(result: Result, settings: Settings) -> List[str]:
    out = []
    b, k = result.backlash, abs(result.scale)
    if result.method == "indicator" and abs(k - 1.0) > 0.15:
        if abs(k - 1 / 25.4) < 0.01:
            out.append("The readings look like inches: enter the indicator's readings in mm.")
        else:
            out.append(f"The indicator moved {k * settings.step:.3f} mm for a {settings.step:g} mm step. Check it's "
                       f"square to the axis, its base is solid, and it reads in mm.")
    if result.method == "camera" and result.axis != "Z" and k < 2.0:
        out.append(f"The camera only sees {k:.1f} pixels per mm here: bring it closer for a finer measurement.")
    if result.method == "camera" and result.axis == "Z" and k < 0.5:
        out.append(f"The laser line only moves {k:.2f} rows per mm of Z here: lower the head for a finer "
                   f"measurement.")
    if b > 0.8 * abs(settings.approach):
        out.append(f"The backlash is close to the approach distance ({settings.approach:g} mm), so some approaches "
                   f"may not have taken up the slack. Make the approach distance bigger and measure again.")
    if b < 0 and -b > max(SPREAD_LIMIT, result.spread) and result.active <= 0:
        out.append("It measured negative, which backlash can't be: something slipped (the indicator's base, the "
                   "tag or the part). Check and measure again.")
    elif b < 0 and result.active > 0 and result.active + b < 0:
        out.append("It's now overcompensated by more than the whole setting: check the measurement.")
    if result.spread > max(SPREAD_LIMIT, 0.25 * abs(b)):
        out.append(f"The repeats disagree by {result.spread:.3f} mm: something isn't rigid, or the gibs let the "
                   f"table rock. Tighten things up and measure again before trusting it.")
        if result.axis == "Z":
            out.append("On Z the head's weight usually keeps the nut loaded one way, so its backlash can come and "
                       "go. If it won't repeat, leave Z's compensation at 0.")
    if result.noise > max(SPREAD_LIMIT, 0.25 * abs(b)):
        out.append(f"The readings scatter by {result.noise:.3f} mm even from the same side: check the gauge is firm.")
    if result.active + b > MAX_SENSIBLE:
        out.append(f"That's a lot of slack ({result.active + b:.3f} mm). Compensation fixes positioning, but cutting "
                   f"forces can still pull the table across the gap (climb milling especially): check the gibs, the "
                   f"nut and the screw's thrust bearings.")
    return out


# --- a pretend axis with slack (the sim machine, previews and tests) ------------------------------

class LashSim:
    """Where the table really is for a commanded position: it only follows once the screw has
    turned through the slack (play centred on the commanded position: +b/2 behind moving +)"""

    def __init__(self, lash: Dict[int, float]):
        self.lash = dict(lash)
        self.actual: Dict[int, float] = {}

    def __call__(self, commanded: Sequence[float]) -> List[float]:
        out = list(commanded)
        for i, b in self.lash.items():
            if i >= len(out):
                continue
            c = out[i]
            actual = self.actual.get(i, c - b / 2)
            if c > actual + b / 2:
                actual = c - b / 2
            elif c < actual - b / 2:
                actual = c + b / 2
            self.actual[i] = actual
            out[i] = actual
        return out


# --- the INI ---------------------------------------------------------------------------------------

def _sections(lines: List[str]) -> Dict[str, Tuple[int, int]]:
    """Section name -> (index of its header line, index just past its last line)"""
    found, name, start = {}, None, 0
    for i, line in enumerate(lines):
        m = re.match(r"\s*\[([^\]]+)\]", line)
        if m:
            if name is not None:
                found.setdefault(name, (start, i))
            name, start = m.group(1).strip(), i
    if name is not None:
        found.setdefault(name, (start, len(lines)))
    return found


def _find_key(lines, span, key) -> Optional[int]:
    for i in range(span[0] + 1, span[1]):
        m = re.match(r"\s*([A-Za-z0-9_]+)\s*=", lines[i])
        if m and m.group(1).upper() == key.upper():
            return i
    return None


def read_value(text: str, section: str, key: str) -> Optional[str]:
    lines = text.splitlines()
    span = _sections(lines).get(section)
    if span is None:
        return None
    i = _find_key(lines, span, key)
    return lines[i].split("=", 1)[1].split("#")[0].strip() if i is not None else None


def _float(text: Optional[str], default=0.0) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def joint_for(text: str, axis: str) -> int:
    """The joint that drives an axis (trivkins: the axis's place in coordinates=)"""
    kins = read_value(text, "KINS", "KINEMATICS") or ""
    m = re.search(r"coordinates=([A-Za-z]+)", kins)
    letters = m.group(1).upper() if m else "XYZ"
    return letters.index(axis) if axis in letters else AXES.index(axis)


def read_backlash(path: str) -> Dict[str, float]:
    """BACKLASH per axis (0 where it isn't set), from the INI file"""
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        return {}
    return {a: _float(read_value(text, f"JOINT_{joint_for(text, a)}", "BACKLASH")) for a in AXES}


def set_value(text: str, section: str, key: str, value: str, after: Sequence[str] = (),
              comment: Optional[str] = None) -> str:
    """The INI text with key = value in the section (replaced in place, or added after the first
    of `after` that's there, else at the section's end). comment: a line kept just above it."""
    trailing = text.endswith("\n")
    lines = text.splitlines()
    sections = _sections(lines)
    if section not in sections:
        raise ValueError(f"There's no [{section}] in the INI")
    span = sections[section]
    i = _find_key(lines, span, key)
    new = f"{key} = {value}"
    if i is not None:
        lines[i] = new
        if comment is not None:
            if i > 0 and lines[i - 1].startswith(COMMENT):
                lines[i - 1] = comment
            else:
                lines.insert(i, comment)
    else:
        at = None
        for name in after:
            j = _find_key(lines, span, name)
            if j is not None:
                at = j + 1
                break
        if at is None:
            at = span[1]
            while at - 1 > span[0] and not lines[at - 1].strip():
                at -= 1
        lines[at:at] = ([comment] if comment is not None else []) + [new]
    return "\n".join(lines) + ("\n" if trailing else "")


def apply(path: str, values: Dict[str, float], backup_dir: Optional[str] = None, method: str = "",
          now: Optional[float] = None) -> List[str]:
    """
    Write BACKLASH for each axis in `values` (mm) into the INI, raising STEPGEN_MAXACCEL to twice
    MAX_ACCELERATION where compensation needs the headroom. A copy of the INI goes to backup_dir
    first. Returns what changed, in words. LinuxCNC reads it at its next start.
    """
    with open(path) as f:
        text = f.read()
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(now if now is not None else time.time()))
    comment = f"{COMMENT}, {stamp}" + (f", {method}" if method else "")
    changes, new = [], text
    for axis, value in values.items():
        section = f"JOINT_{joint_for(text, axis)}"
        value = max(0.0, float(value))
        old = _float(read_value(new, section, "BACKLASH"))
        new = set_value(new, section, "BACKLASH", f"{value:.4f}",
                        after=("STEPGEN_MAXACCEL", "MAX_ACCELERATION"), comment=comment)
        changes.append(f"{axis}: BACKLASH {old:g} → {value:.4f} mm")
        accel = _float(read_value(new, section, "MAX_ACCELERATION"))
        stepgen = read_value(new, section, "STEPGEN_MAXACCEL")
        if value > 0 and stepgen is not None and accel > 0 and _float(stepgen) < 2 * accel:
            new = set_value(new, section, "STEPGEN_MAXACCEL", f"{2 * accel:.2f}")
            changes.append(f"{axis}: STEPGEN_MAXACCEL {_float(stepgen):g} → {2 * accel:.2f} (room for the "
                           f"compensation moves)")
    if new == text:
        return []
    if backup_dir:
        os.makedirs(backup_dir, exist_ok=True)
        name = f"{os.path.basename(path)}.{time.strftime('%Y%m%d_%H%M%S')}.before-backlash"
        shutil.copy2(path, os.path.join(backup_dir, name))
    tmp = path + ".milo-tmp"
    with open(tmp, "w") as f:
        f.write(new)
    os.replace(tmp, path)
    return changes
