"""
Vision-guided probing: turn a part found by the camera (to +-0.5 mm) into a touch-probe program
that measures it to the probe's accuracy and sets G54 on it.

The program is ordinary G-code, loaded for review and started by the operator like any other.
How it stays safe when the vision estimate is off:

- It refuses to run unless the probe (probe_tool) is the tool in the spindle.
- It starts fully raised (G53 Z0) and moves over the part's centre in machine X/Y.
- It finds the top with G38.2 first; every later height is relative to that measured top.
- Descents beside the part are G38.3 moves: touching anything on the way down (the part, a
  clamp) stops the move and the program aborts with a message instead of probing sideways.
- Side probes are G38.2 moves with a limited search distance: if the edge isn't there, LinuxCNC
  stops with a probe error.
- Tool tip heights use #5403 (the active tool's Z offset), so the probe must be measured.

Coordinates: the part is in machine X/Y; top_z is a tip-Z estimate (optional).
"""

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple

ORIGINS = ("corner", "center")


@dataclass
class ProbeSettings:
    probe_tool: int = 99            # tool number of the touch probe
    tip_diameter: float = 2.0       # probe ball diameter (mm)
    side_clearance: float = 6.0     # start this far outside each estimated edge (mm)
    search_beyond: float = 6.0      # and search this far past the estimated edge
    depth_below_top: float = 3.0    # probe the sides this far below the top
    travel_above_top: float = 8.0   # travel this far above the top between probes
    fast_feed: float = 200.0        # mm/min
    slow_feed: float = 25.0         # mm/min
    top_search: float = 30.0        # how far below the estimate to look for the top (mm)


def _fmt(v):
    return f"{v:.4f}"


def probe_program(center: Tuple[float, float], size: Tuple[float, float], angle: float,
                  top_z: Optional[float], settings: ProbeSettings = ProbeSettings(),
                  origin: str = "corner", travel_above_top: Optional[float] = None,
                  label: str = "part") -> str:
    """
    G-code that measures an axis-aligned rectangular part and sets G54 on it.

    center, size, angle: the camera's rectangle (machine XY, mm, degrees of the long side).
    origin: "corner" (X0 Y0 at the X-/Y- corner) or "center"; Z0 is always the top.
    travel_above_top: override (e.g. from the height map, to clear taller clamps nearby).
    """
    if origin not in ORIGINS:
        raise ValueError(f"origin must be one of {ORIGINS}")
    a = ((angle + 45) % 90) - 45  # deviation from the nearest machine axis
    if abs(a) > 3.0:
        raise ValueError(f"The part is turned {angle:.1f} degrees; square it to the table within 3 degrees "
                         "(rotated setups aren't supported yet)")
    # extent along machine axes
    turned = abs(((angle % 180) + 180) % 180 - 90) < 45  # long side closer to Y
    sx, sy = (size[1], size[0]) if turned else (size[0], size[1])
    xmin, xmax = center[0] - sx / 2, center[0] + sx / 2
    ymin, ymax = center[1] - sy / 2, center[1] + sy / 2
    s = settings
    r = s.tip_diameter / 2
    travel = max(s.travel_above_top, travel_above_top or 0.0)
    reach = s.side_clearance + s.search_beyond  # G38.2 search distance from the start point
    if min(sx, sy) < 2 * s.side_clearance / 3:
        raise ValueError("The part is too small to probe safely with these clearances")

    top_known = top_z is not None
    lines = [
        "%",
        f"(Milo vision-guided probe: {label} about {sx:.1f} x {sy:.1f} mm)",
        f"(camera estimate: machine X {xmin:.2f}..{xmax:.2f}  Y {ymin:.2f}..{ymax:.2f}"
        + (f"  top ~{top_z:.2f} tip Z" if top_known else "") + ")",
        f"(Sets G54: X0 Y0 at the {'X-/Y- corner' if origin == 'corner' else 'center'}, Z0 on the top.)",
        "(Review the path in the preview before starting.)",
        "G21 G90 G94 G40 G17 G54",
        "M5 M9",
        f"o100 if [#5400 NE {s.probe_tool}]",
        f"  (abort, Load the touch probe T{s.probe_tool} first)",
        "o100 endif",
        f"#<r> = {_fmt(r)}",
        f"#<fast> = {_fmt(s.fast_feed)}",
        f"#<slow> = {_fmt(s.slow_feed)}",
        f"#<travel> = {_fmt(travel)}",
        "",
        "(--- top: from fully raised, over the centre ---)",
        "G53 G0 Z0",
        f"G53 G0 X{_fmt(center[0])} Y{_fmt(center[1])}",
    ]
    if top_known:
        # tip Z now is G53 Z0 minus the tool length; rapid to `travel` above the estimate
        lines += [
            f"#<tip_now> = [0 - #5403]",
            f"#<drop> = [#<tip_now> - {_fmt(top_z + travel)}]",
            "o110 if [#<drop> GT 0]",
            "  G91 G38.3 Z[0 - #<drop>] F1000",
            "  o111 if [#5070 EQ 1]",
            "    (abort, Something is above the part: the probe touched it on the way down)",
            "  o111 endif",
            "  G90",
            "o110 endif",
            f"G91 G38.2 Z-{_fmt(travel + s.top_search)} F#<fast>",
        ]
    else:
        lines += [
            "(no height estimate: search slowly all the way down)",
            "G91 G38.2 Z-250 F#<fast>",
        ]
    lines += [
        "G91 G0 Z1",
        "G38.2 Z-2 F#<slow>",
        "G90",
        "#<top> = #5063",
        "G0 Z[#<top> + #<travel>]",
        "",
    ]

    counter = [120]

    def side(name, axis, approach_xy, direction):
        counter[0] += 10
        o = counter[0]
        sign = "" if direction > 0 else "-"
        back = "-" if direction > 0 else ""
        ax, ay = approach_xy
        result = "#5061" if axis == "X" else "#5062"
        edge = f"[{result} + #<r>]" if direction > 0 else f"[{result} - #<r>]"
        return [
            f"(--- {name} ---)",
            f"G53 G0 X{_fmt(ax)} Y{_fmt(ay)}",
            f"G91 G38.3 Z-[#<travel> + {_fmt(s.depth_below_top)}] F1000",
            f"o{o} if [#5070 EQ 1]",
            f"  (abort, Hit something beside the {name.split()[0]} edge on the way down; check the setup)",
            f"o{o} endif",
            f"G38.2 {axis}{sign}{_fmt(reach)} F#<fast>",
            f"G0 {axis}{back}1",
            f"G38.2 {axis}{sign}2 F#<slow>",
            "G90",
            f"#<{name.split()[0].lower().replace('-', 'min').replace('+', 'max')}> = {edge}",
            f"G91 G0 {axis}{back}{_fmt(s.side_clearance)}",
            "G90 G0 Z[#<top> + #<travel>]",
            "",
        ]

    ymid, xmid = (ymin + ymax) / 2, (xmin + xmax) / 2
    lines += side("X- edge", "X", (xmin - s.side_clearance - r, ymid), +1)
    if origin == "center":
        lines += side("X+ edge", "X", (xmax + s.side_clearance + r, ymid), -1)
    lines += side("Y- edge", "Y", (xmid, ymin - s.side_clearance - r), +1)
    if origin == "center":
        lines += side("Y+ edge", "Y", (xmid, ymax + s.side_clearance + r), -1)

    if origin == "corner":
        x0, y0 = "#<xmin>", "#<ymin>"
    else:
        x0, y0 = "[[#<xmin> + #<xmax>] / 2]", "[[#<ymin> + #<ymax>] / 2]"
    lines += [
        "(--- set G54: the current position gets the value that puts the origin where measured ---)",
        f"G10 L20 P1 X[#5420 - {x0}] Y[#5421 - {y0}] Z[#5422 - #<top>]",
        "G0 Z#<travel>",
    ]
    if origin == "center":
        lines += ["#<size_x> = [#<xmax> - #<xmin>]", "#<size_y> = [#<ymax> - #<ymin>]",
                  "(DEBUG, Measured size X #<size_x> Y #<size_y>)"]
    lines += ["(MSG, G54 set from the probe)", "M2", "%", ""]
    return "\n".join(lines)
