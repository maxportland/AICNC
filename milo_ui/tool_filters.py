"""
Filters and sorting for the Tool library: flutes, diameter, flute length, overall length, shank,
tool material and whether the vendor gave cutting data. No Qt here.

Active filters are a dict {key: value}:
  flutes          1, 2, 3, 4 or "5+"
  diameter        (low, high) mm, either may be None: low <= d < high
  overall_length  (low, high) mm
  flute_length    low mm: at least this much reach
  shank           mm (one shank size)
  material        "carbide", "hss", ...
  cutting_data    True: only tools with vendor presets
Lengths are always mm inside (Fusion libraries store mm); labels follow the machine's units.
"""

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

IN = 25.4
TOLERANCE = 1e-3  # mm: a 6.000 mm tool is in "6-10", not "3-6"
SHANK_TOLERANCE = 0.03  # mm: vendors round 1/8" to 3.17 or 3.18; same collet


@dataclass
class Option:
    label: str
    value: object


@dataclass
class Filter:
    key: str
    title: str
    kind: str  # choice | range | min | values | flag
    icon: str


FILTERS = [
    Filter("flutes", "Flutes", "choice", "asterisk"),
    Filter("diameter", "Diameter", "range", "circle"),
    Filter("flute_length", "Flute length", "min", "arrow-line-down"),
    Filter("overall_length", "Overall length", "range", "ruler"),
    Filter("shank", "Shank", "values", "circle-half"),
    Filter("material", "Material", "values", "cube"),
    Filter("cutting_data", "Cutting data", "flag", "gauge"),
]
BY_KEY = {f.key: f for f in FILTERS}

# Bucket edges (in the display units) for the range and minimum filters
RANGE_EDGES = {
    "diameter": {"mm": [3, 6, 10, 13, 20], "in": [0.125, 0.25, 0.375, 0.5, 0.75]},
    "overall_length": {"mm": [50, 65, 80, 100], "in": [2, 2.5, 3, 4]},
}
MINIMUMS = {"flute_length": {"mm": [3, 5, 10, 15, 20, 25, 30, 40], "in": [0.125, 0.25, 0.375, 0.5, 0.75, 1, 1.5]}}

SORTS = [
    ("As listed", None),
    ("Smallest diameter", lambda t: (t.diameter, t.flute_length)),
    ("Largest diameter", lambda t: (-t.diameter, t.flute_length)),
    ("Longest reach", lambda t: (-t.flute_length, t.diameter)),
    ("Shortest overall", lambda t: (t.overall_length, t.diameter)),
    ("Most flutes", lambda t: (-t.flutes, t.diameter)),
]


def _attr(tool, key) -> float:
    return {"diameter": tool.diameter, "overall_length": tool.overall_length, "flute_length": tool.flute_length,
            "shank": tool.shank_diameter}[key]


def matches(tool, key: str, value) -> bool:
    """Whether one tool passes one filter"""
    if value is None:
        return True
    if key == "flutes":
        return tool.flutes >= 5 if value == "5+" else tool.flutes == int(value)
    if key in ("diameter", "overall_length"):
        low, high = value
        v = _attr(tool, key)
        return (low is None or v >= low - TOLERANCE) and (high is None or v < high - TOLERANCE)
    if key == "flute_length":
        return tool.flute_length >= float(value) - TOLERANCE
    if key == "shank":
        return abs(tool.shank_diameter - float(value)) <= SHANK_TOLERANCE
    if key == "material":
        return (tool.material or "").strip().lower() == str(value).lower()
    if key == "cutting_data":
        return bool(tool.presets) == bool(value)
    return True


def apply(tools: Sequence, active: Dict[str, object], skip: Optional[str] = None) -> list:
    """The tools passing every active filter (but `skip`, for counting that filter's options)"""
    checks = [(k, v) for k, v in active.items() if k != skip and v is not None]
    return [t for t in tools if all(matches(t, k, v) for k, v in checks)]


def sort(tools: list, how: Optional[Callable]) -> list:
    return sorted(tools, key=how) if how else tools


def fmt(mm: float, units: str) -> str:
    """A length in the display units, short: '6 mm', '1/4"', '0.236"'"""
    if units == "in":
        return inch_fraction(mm) or f'{mm / IN:.4g}"'
    return f"{mm:.3g} mm"


def _to_mm(value: float, units: str) -> float:
    return value * IN if units == "in" else value


def options(key: str, tools: Sequence, units: str = "mm") -> List[Option]:
    """The choices for a filter (the value-list ones come from the tools themselves)"""
    if key == "flutes":
        return [Option(f"{n} flute{'s' if n != 1 else ''}", n) for n in (1, 2, 3, 4)] + [Option("5 or more", "5+")]
    if key in RANGE_EDGES:
        edges = [_to_mm(e, units) for e in RANGE_EDGES[key][units]]
        bounds = [None] + edges + [None]
        result = []
        for low, high in zip(bounds, bounds[1:]):
            if low is None:
                label = f"Under {fmt(high, units)}"
            elif high is None:
                label = f"{fmt(low, units)} or more"
            else:
                label = f"{fmt(low, units)} to {fmt(high, units)}"
            result.append(Option(label, (low, high)))
        return result
    if key in MINIMUMS:
        return [Option(f"At least {fmt(_to_mm(v, units), units)}", _to_mm(v, units)) for v in MINIMUMS[key][units]]
    if key == "shank":
        return [Option(shank_label(s, units), s) for s in shank_sizes(tools)]
    if key == "material":
        names = sorted({(t.material or "").strip().lower() for t in tools if (t.material or "").strip()})
        return [Option(n.upper() if len(n) <= 3 else n.capitalize(), n) for n in names]
    if key == "cutting_data":
        return [Option("Has cutting data", True), Option("No cutting data", False)]
    return []


def shank_sizes(tools: Sequence) -> List[float]:
    """The distinct shank sizes, near-equal ones (3.17 / 3.175 / 3.18) as one"""
    sizes: List[List[float]] = []
    for d in sorted(t.shank_diameter for t in tools if t.shank_diameter > 0):
        if sizes and d - sizes[-1][-1] <= SHANK_TOLERANCE:
            sizes[-1].append(d)
        else:
            sizes.append([d])
    return [_snap_inch(sum(group) / len(group)) for group in sizes]


def _snap_inch(mm: float) -> float:
    """A size that is an inch fraction (vendors write 1/8" as 3.17 or 3.18) as the exact fraction"""
    for denominator in (2, 4, 8, 16, 32, 64):
        numerator = round(mm / IN * denominator)
        if numerator and abs(numerator * IN / denominator - mm) <= SHANK_TOLERANCE:
            return round(numerator * IN / denominator, 4)
    return round(mm, 3)


def inch_fraction(mm: float) -> Optional[str]:
    """'1/8"' for 3.175 mm: a common inch size within a few hundredths"""
    for denominator in (2, 4, 8, 16, 32, 64):
        numerator = round(mm / IN * denominator)
        if numerator and abs(numerator * IN / denominator - mm) <= SHANK_TOLERANCE:
            whole, rest = divmod(numerator, denominator)
            if rest == 0:
                return f'{whole}"'
            fraction = f"{rest}/{denominator}"
            return f'{whole}-{fraction}"' if whole else f'{fraction}"'
    return None


def shank_label(mm: float, units: str) -> str:
    inch = inch_fraction(mm)
    if units == "in":
        return f"{inch} ({mm / IN:.4g} in)" if inch else f"{fmt(mm, units)} ({mm:.3g} mm)"
    return f"{inch} ({mm:.3f} mm)" if inch else f"{mm:.3g} mm"


def describe(key: str, value, units: str = "mm") -> str:
    """The chip's text: 'Flutes', or 'Diameter 6 mm to 10 mm' when one is set"""
    title = BY_KEY[key].title
    if value is None:
        return title
    if key == "flutes":
        return "5+ flutes" if value == "5+" else f"{value} flute{'s' if value != 1 else ''}"
    if key in ("diameter", "overall_length"):
        low, high = value
        if low is None:
            return f"{title} < {fmt(high, units)}"
        if high is None:
            return f"{title} ≥ {fmt(low, units)}"
        return f"{title} {fmt(low, units)}–{fmt(high, units)}"
    if key == "flute_length":
        return f"Reach ≥ {fmt(float(value), units)}"
    if key == "shank":
        inch = inch_fraction(float(value))
        return f"Shank {inch}" if inch else f"Shank {fmt(float(value), units)}"
    if key == "material":
        name = str(value)
        return name.upper() if len(name) <= 3 else name.capitalize()
    if key == "cutting_data":
        return "Has cutting data" if value else "No cutting data"
    return title


def custom_range(low: Optional[float], high: Optional[float], units: str) -> Tuple[Optional[float], Optional[float]]:
    """A typed range (display units, 0 = open) as mm, low <= high"""
    low_mm = _to_mm(low, units) if low else None
    high_mm = _to_mm(high, units) if high else None
    if low_mm is not None and high_mm is not None and low_mm > high_mm:
        low_mm, high_mm = high_mm, low_mm
    return low_mm, high_mm


def to_json(active: Dict[str, object]) -> dict:
    return {k: list(v) if isinstance(v, tuple) else v for k, v in active.items() if v is not None}


def from_json(data) -> Dict[str, object]:
    active = {}
    for key, value in (data or {}).items():
        if key not in BY_KEY:
            continue
        if BY_KEY[key].kind == "range":
            if isinstance(value, (list, tuple)) and len(value) == 2:
                active[key] = (value[0], value[1])
        else:
            active[key] = value
    return active
