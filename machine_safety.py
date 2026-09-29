"""
Safety checks for machine actions proposed by the AI Assistant.

The LLM only proposes an MDI line. Before it is shown for confirmation, and
again right before it runs, it must pass:

- a strict allowlist of G/M codes and words (no O-words, parameters, expressions, G20/G21, ...)
- machine state checks (e-stop clear, machine on, interpreter idle, homed for motion)
- a soft-limit check of the target position for G0/G1 moves

LinuxCNC enforces soft limits itself as well; the check here rejects the
command before the user is asked to confirm it, with a readable reason.
"""

import os
import re
from typing import List, Optional, Tuple

try:
    import linuxcnc
except ImportError:
    linuxcnc = None


AXIS_LETTERS = "XYZABCUVW"
ROTARY_AXES = "ABC"
ALLOWED_G = {0, 1, 28, 30, 53, 90, 91}
MOTION_G = {0, 1, 28, 30}
ALLOWED_M = {3, 4, 5, 6, 7, 8, 9}
ALLOWED_LETTERS = set("GMSFT") | set(AXIS_LETTERS)

_COMMAND_RE = re.compile(r'(?:[A-Z][-+]?(?:\d+\.?\d*|\.\d+))+')
_WORD_RE = re.compile(r'([A-Z])([-+]?(?:\d+\.?\d*|\.\d+))')


_ini_cache = {}


def _ini(stat):
    """linuxcnc.ini for the running config, or None"""
    if stat is None or linuxcnc is None:
        return None
    path = getattr(stat, "ini_filename", "")
    if not path:
        # A stat object that was never polled has no INI path yet
        stat.poll()
        path = getattr(stat, "ini_filename", "")
    if not path:
        return None
    if path not in _ini_cache:
        try:
            _ini_cache[path] = linuxcnc.ini(path)
        except Exception:
            _ini_cache[path] = None
    return _ini_cache[path]


def spindle_max_rpm(stat) -> Optional[float]:
    """Maximum spindle speed from the INI [SPINDLE_0] section, or None if not configured"""
    ini = _ini(stat)
    if ini is None:
        return None
    for key in ("MAX_FORWARD_VELOCITY", "MAX_OUTPUT"):
        value = ini.find("SPINDLE_0", key)
        try:
            if value is not None and float(value) > 0:
                return float(value)
        except ValueError:
            pass
    return None


def machine_units(stat) -> str:
    """'mm' or 'inch' for the machine's linear units"""
    if stat is None:
        return "mm"
    if not stat.linear_units:
        stat.poll()  # never polled yet
    # stat.linear_units is machine units per mm
    return "mm" if abs(stat.linear_units - 1.0) < 1e-6 else "inch"


def parse_mdi(command: str) -> List[Tuple[str, float]]:
    """
    Split an MDI line into (letter, value) words.

    Raises:
        ValueError if the line contains anything other than plain words
    """
    compact = "".join(command.upper().split())
    if not compact or not _COMMAND_RE.fullmatch(compact):
        raise ValueError(f"'{command}' is not a plain G-code line")
    return [(letter, float(value)) for letter, value in _WORD_RE.findall(compact)]


def _all_homed(stat) -> bool:
    return all(stat.homed[j] for j in range(stat.joints))


def check_machine_ready(stat, needs_homed: bool) -> Optional[str]:
    """Return a reason the machine can't take a command now, or None if it can"""
    if stat is None or linuxcnc is None:
        return "Machine status is not available."
    stat.poll()
    if stat.task_state == linuxcnc.STATE_ESTOP:
        return "Machine is in E-stop."
    if stat.task_state != linuxcnc.STATE_ON:
        return "Machine is not powered on."
    if stat.interp_state != linuxcnc.INTERP_IDLE:
        return "Machine is busy (a program or command is running)."
    if any(stat.joint[j].get("homing") for j in range(stat.joints)):
        return "Machine is homing."
    if needs_homed and not _all_homed(stat):
        return "Machine is not homed."
    return None


def check_power_on(stat) -> Optional[str]:
    """Return a reason the machine can't be turned on now, or None if it can"""
    if stat is None or linuxcnc is None:
        return "Machine status is not available."
    stat.poll()
    if stat.task_state == linuxcnc.STATE_ESTOP:
        return "The E-stop is active. Release it at the machine first."
    if stat.task_state == linuxcnc.STATE_ON:
        return "The machine is already on."
    return None


def check_power_off(stat) -> Optional[str]:
    """Return a reason there's nothing to turn off, or None if the machine is on"""
    if stat is None or linuxcnc is None:
        return "Machine status is not available."
    stat.poll()
    if stat.task_state != linuxcnc.STATE_ON:
        return "The machine is already off."
    return None


def program_running(stat) -> bool:
    """True if a program or command is running or paused"""
    stat.poll()
    return stat.interp_state != linuxcnc.INTERP_IDLE


def check_program_ready(stat) -> Optional[str]:
    """Return a reason the loaded program can't be started now, or None if it can"""
    reason = check_machine_ready(stat, needs_homed=True)
    if reason:
        return reason
    if not stat.file:
        return "No program is loaded."
    if not os.path.isfile(stat.file):
        return f"The loaded program file no longer exists: {stat.file}"
    return None


def validate_mdi(command: str, stat) -> Optional[str]:
    """
    Validate an MDI line against the allowlist, machine state and soft limits.

    Returns:
        A reason the command is rejected, or None if it may be offered for confirmation
    """
    try:
        words = parse_mdi(command)
    except ValueError as e:
        return str(e)

    seen_axes = set()
    g_codes = set()
    m_codes = set()
    for letter, value in words:
        if letter not in ALLOWED_LETTERS:
            return f"'{letter}' words are not allowed from the assistant."
        if letter in "GM":
            if not value.is_integer():
                return f"{letter}{value:g} is not allowed from the assistant."
            code = int(value)
            allowed = ALLOWED_G if letter == "G" else ALLOWED_M
            if code not in allowed:
                return f"{letter}{code} is not allowed from the assistant."
            (g_codes if letter == "G" else m_codes).add(code)
        elif letter in AXIS_LETTERS:
            if letter in seen_axes:
                return f"Axis {letter} appears more than once."
            seen_axes.add(letter)
        elif letter == "S" and value < 0:
            return "Spindle speed can't be negative."
        elif letter == "F" and value <= 0:
            return "Feed rate must be positive."
        elif letter == "T" and (value < 0 or not value.is_integer()):
            return f"T{value:g} is not a valid tool number."

    motion = g_codes & MOTION_G
    if len(motion) > 1:
        return "More than one motion command in one line."
    if {90, 91} <= g_codes:
        return "G90 and G91 in the same line."
    if 53 in g_codes and not motion & {0, 1}:
        return "G53 must be used with G0 or G1."
    if 53 in g_codes and 91 in g_codes:
        return "G53 can't be combined with G91."
    if seen_axes and not motion:
        # Axis words alone would use whatever motion mode is active (possibly G2/G3)
        return "Axis moves must include G0 or G1."

    if stat is None or linuxcnc is None:
        return "Machine status is not available."
    stat.poll()
    for axis in seen_axes:
        if not stat.axis_mask & (1 << AXIS_LETTERS.index(axis)):
            return f"This machine has no {axis} axis."

    max_rpm = spindle_max_rpm(stat)
    for letter, value in words:
        if letter == "S" and max_rpm is not None and value > max_rpm:
            return f"S{value:g} is above the spindle maximum of {max_rpm:g} rpm."

    reason = check_machine_ready(stat, needs_homed=bool(motion) or 6 in m_codes)
    if reason:
        return reason

    if motion & {0, 1}:
        return _check_soft_limits(words, g_codes, stat)
    return None


def _parameter_file(stat) -> Optional[str]:
    """Path of the interpreter's parameter file (linuxcnc.var) from the INI"""
    ini = _ini(stat)
    if ini is None:
        return None
    name = ini.find("RS274NGC", "PARAMETER_FILE")
    if not name:
        return None
    return name if os.path.isabs(name) else os.path.join(os.path.dirname(stat.ini_filename), name)


def read_parameters(path: str) -> dict:
    """Parse a LinuxCNC parameter file: '<number> <value>' per line"""
    params = {}
    with open(path) as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                try:
                    params[int(parts[0])] = float(parts[1])
                except ValueError:
                    pass
    return params


def work_offsets(stat) -> Optional[Tuple[List[float], float, str]]:
    """
    Active work offset (G5x + G92) per axis in machine units, the XY rotation in degrees,
    and where they came from; None if they can't be determined.

    LinuxCNC's status only reports offsets once a work-offset command has run: before that
    it says g5x_index 0 with all-zero offsets, even though the interpreter applies the
    offsets it loaded from the parameter file. In that case read them from the file.
    Any touch-off or G5x/G10 this session updates the status, so the file is only used
    while it still matches what the interpreter loaded.
    """
    if 1 <= stat.g5x_index <= 9:
        offsets = [stat.g5x_offset[i] + stat.g92_offset[i] for i in range(9)]
        return offsets, stat.rotation_xy, "status"

    number = WCS_NUMBERS.get(next((c for c in stat.gcodes if c in WCS_NUMBERS), None))
    path = _parameter_file(stat)
    if number is None or path is None or not os.path.isfile(path):
        return None
    try:
        params = read_parameters(path)
    except OSError:
        return None
    base = 5201 + 20 * number  # G54 X is 5221, G55 X is 5241, ...
    g5x = [params.get(base + i, 0.0) for i in range(9)]
    rotation = params.get(base + 9, 0.0)
    g92 = [params.get(5211 + i, 0.0) for i in range(9)] if params.get(5210, 0.0) == 1.0 else [0.0] * 9
    return [a + b for a, b in zip(g5x, g92)], rotation, "parameter file"


def _move_targets(words, g_codes, stat) -> Tuple[dict, Optional[str]]:
    """Machine-coordinate target of each axis in a G0/G1 line, or an error"""
    program_inch = stat.program_units == 1
    incremental = 91 in g_codes or (910 in stat.gcodes and 90 not in g_codes)
    machine_coords = 53 in g_codes
    offsets = None
    if not machine_coords and not incremental:
        found = work_offsets(stat)
        if found is None:
            return {}, ("The active work offset isn't known yet, so a work-coordinate move can't be checked. "
                        "Use a machine-coordinate move instead.")
        offsets, rotation, _ = found
        if rotation and any(letter in "XY" for letter, _ in words):
            return {}, "Coordinate system rotation is active; the assistant won't move X/Y with it."

    targets = {}
    for letter, value in words:
        if letter not in AXIS_LETTERS:
            continue
        i = AXIS_LETTERS.index(letter)
        if letter in ROTARY_AXES:
            scale = 1.0
        else:
            # stat.linear_units is machine units per mm
            scale = (25.4 if program_inch else 1.0) * stat.linear_units
        v = value * scale
        if machine_coords:
            targets[letter] = v
        elif incremental:
            targets[letter] = stat.position[i] + v
        else:
            targets[letter] = v + offsets[i] + stat.tool_offset[i]
    return targets, None


def _check_soft_limits(words, g_codes, stat) -> Optional[str]:
    """Check the target of a G0/G1 move against the axis soft limits (machine coordinates)"""
    targets, error = _move_targets(words, g_codes, stat)
    if error:
        return error
    for letter, target in targets.items():
        limits = stat.axis[AXIS_LETTERS.index(letter)]
        lo, hi = limits["min_position_limit"], limits["max_position_limit"]
        if target < lo - 1e-6 or target > hi + 1e-6:
            return (f"{letter} would end at {target:.3f} (machine), outside the soft limits "
                    f"[{lo:.3f}, {hi:.3f}].")
    return None


def check_obstacles(command: str, stat, heightmap, tool_radius: float = 3.2, clearance: float = 0.5) -> Optional[str]:
    """
    Check a G0/G1 line against the camera's height map (milo_vision.heightmap.HeightMap, machine
    mm): the tool tip must stay above everything within tool_radius along the straight path.
    Obstacles no higher than what the tool is already in at the start (the stock being cut)
    don't count. Returns a reason to refuse, or None.
    """
    if heightmap is None:
        return None
    try:
        words = parse_mdi(command)
    except ValueError:
        return None
    g_codes = {int(v) for letter, v in words if letter == "G"}
    if not g_codes & {0, 1} or g_codes & {28, 30}:
        return None
    targets, error = _move_targets(words, g_codes, stat)
    if error or not targets:
        return None
    pos, tool_z = stat.position, stat.tool_offset[2]
    start = (pos[0], pos[1], pos[2] - tool_z)
    end = (targets.get("X", pos[0]), targets.get("Y", pos[1]), targets.get("Z", pos[2]) - tool_z)
    engaged = heightmap.max_in_circle(start[0], start[1], tool_radius)
    ignore = engaged if engaged is not None and start[2] < engaged + clearance else None
    hit = heightmap.check_move(start, end, tool_radius, clearance, ignore_below=ignore)
    if hit is None:
        return None
    return (f"the tool would pass through something the camera measured there: at X{hit['x']:.1f} "
            f"Y{hit['y']:.1f} the tip would be at Z{hit['tip_z']:.1f}, but there's material up to "
            f"Z{hit['obstacle_z']:.1f} (machine, tip). Raise Z first, or rescan the table if the setup changed")


def max_feed(stat) -> Optional[float]:
    """Maximum linear feed from [TRAJ] MAX_LINEAR_VELOCITY, in machine units per minute"""
    ini = _ini(stat)
    value = ini.find("TRAJ", "MAX_LINEAR_VELOCITY") if ini is not None else None
    try:
        return float(value) * 60.0 if value else None
    except ValueError:
        return None


def circle_commands(diameter: float, clockwise: bool, feed: float, stat) -> List[str]:
    """
    MDI lines for a full circle in XY around the current position, at the current Z:
    feed out to the edge (+X), one full arc back to that point, feed back to the center.
    Incremental distances and arc centers (G91 / G91.1) make it independent of work offsets.
    """
    r = diameter / 2.0
    arc = "G2" if clockwise else "G3"
    return [
        f"G91 G1 X{r:.4f} F{feed:g}",
        f"G91 G91.1 {arc} X0 Y0 I{-r:.4f} J0 F{feed:g}",
        f"G91 G1 X{-r:.4f} F{feed:g}",
    ]


def validate_circle(diameter: float, feed: float, stat) -> Optional[str]:
    """Check a circle move around the current position: machine state, size, feed and soft limits"""
    if stat is None or linuxcnc is None:
        return "Machine status is not available."
    if not diameter or diameter <= 0:
        return "The circle needs a diameter greater than zero."
    if not feed or feed <= 0:
        return "The feed rate must be positive."
    reason = check_machine_ready(stat, needs_homed=True)
    if reason:
        return reason
    for axis in "XY":
        if not stat.axis_mask & (1 << AXIS_LETTERS.index(axis)):
            return f"This machine has no {axis} axis."

    scale = (25.4 if stat.program_units == 1 else 1.0) * stat.linear_units  # program -> machine units
    fastest = max_feed(stat)
    if fastest is not None and feed * scale > fastest + 1e-6:
        return f"F{feed:g} is above the machine's maximum feed of {fastest / scale:g}."
    r = diameter / 2.0 * scale
    for axis in "XY":
        i = AXIS_LETTERS.index(axis)
        center = stat.position[i]
        lo, hi = stat.axis[i]["min_position_limit"], stat.axis[i]["max_position_limit"]
        if center - r < lo - 1e-6 or center + r > hi + 1e-6:
            return (f"A {diameter:g} diameter circle here would reach {axis} {center - r:.3f} to {center + r:.3f} "
                    f"(machine), outside the soft limits [{lo:.3f}, {hi:.3f}].")
    return None


def work_move_note(command: str, stat) -> str:
    """For an absolute work-coordinate move, where it ends in machine coordinates ('' otherwise)"""
    try:
        words = parse_mdi(command)
    except ValueError:
        return ""
    g_codes = {int(v) for letter, v in words if letter == "G"}
    if not g_codes & {0, 1} or 53 in g_codes or 91 in g_codes or (910 in stat.gcodes and 90 not in g_codes):
        return ""
    targets, error = _move_targets(words, g_codes, stat)
    if error or not targets:
        return ""
    wcs = active_work_offset(stat)
    return f"{wcs} work coordinates \u2192 machine " + " ".join(f"{a}{t:.3f}" for a, t in targets.items())


# Modal group 12 codes as they appear in stat.gcodes (G-number * 10)
WORK_OFFSETS = {540: "G54", 550: "G55", 560: "G56", 570: "G57", 580: "G58", 590: "G59",
                591: "G59.1", 592: "G59.2", 593: "G59.3"}
WCS_NUMBERS = {code: n for n, code in enumerate(WORK_OFFSETS, start=1)}  # G54 = 1 ... G59.3 = 9


def active_work_offset(stat) -> str:
    """Active work offset (G54..G59.3), read from the active G-codes.

    Not from stat.g5x_index, whose numbering differs between LinuxCNC versions
    (2.9 reports 0 for G54).
    """
    for code in stat.gcodes:
        if code in WORK_OFFSETS:
            return WORK_OFFSETS[code]
    return "unknown"


def describe_machine(stat) -> str:
    """Summarize machine state for the intent router prompt"""
    if stat is None or linuxcnc is None:
        return "Machine status unavailable."
    try:
        stat.poll()
        program_inch = stat.program_units == 1
        units = "inch (G20)" if program_inch else "mm (G21)"
        # machine units -> program units
        to_prog = 1.0 / ((25.4 if program_inch else 1.0) * stat.linear_units)
        axes = [a for i, a in enumerate(AXIS_LETTERS) if stat.axis_mask & (1 << i)]

        found = work_offsets(stat)
        offsets = found[0] if found else None
        work, machine, limits, travel, origin = [], [], [], [], []
        for a in axes:
            i = AXIS_LETTERS.index(a)
            s = 1.0 if a in ROTARY_AXES else to_prog
            pos = stat.position[i]
            if offsets is not None:
                wpos = pos - offsets[i] - stat.tool_offset[i]
                work.append(f"{a}{wpos * s:.4f}")
                origin.append(f"{a}{offsets[i] * s:.3f}")
            machine.append(f"{a}{pos * s:.4f}")
            ax = stat.axis[i]
            # "+ 0.0" turns the INI's "-0.0" into 0.0
            lo, hi = ax['min_position_limit'] * s + 0.0, ax['max_position_limit'] * s + 0.0
            limits.append(f"{a}[{lo:.3f}, {hi:.3f}]")
            if a not in ROTARY_AXES and hi - lo < 1e6:
                # Pre-computed so the model never does travel arithmetic itself
                travel.append(f"{a} {lo:.3f} to {hi:.3f} (center {(lo + hi) / 2:.3f})")

        if stat.interp_state == linuxcnc.INTERP_IDLE:
            program_state = "idle"
        elif stat.interp_state == linuxcnc.INTERP_PAUSED:
            program_state = "paused"
        else:
            program_state = "running"
        program = os.path.basename(stat.file) if stat.file else "none"

        if stat.task_state == linuxcnc.STATE_ESTOP:
            power = "E-stop"
        elif stat.task_state == linuxcnc.STATE_ON:
            power = "on"
        else:
            power = "off"
        spindle = stat.spindle[0]
        spindle_text = f"running at {spindle['speed']:.0f} rpm" if spindle["enabled"] else "stopped"
        max_rpm = spindle_max_rpm(stat)
        if max_rpm is not None:
            spindle_text += f"; maximum speed {max_rpm:g} rpm (never command more)"
        distance = "G91 incremental" if 910 in stat.gcodes else "G90 absolute"
        wcs = active_work_offset(stat)
        lines = [
            f"- Power: {power}; homed: {'yes' if _all_homed(stat) else 'no'}; "
            f"interpreter: {'idle' if stat.interp_state == linuxcnc.INTERP_IDLE else 'busy'}",
            f"- Active units: {units}; distance mode: {distance}; work offset: {wcs}",
            f"- Work position: {' '.join(work)}" if work else
            "- Work position: unknown (work offset not available; use G53 machine coordinates)",
            f"- {wcs} origin is at machine {' '.join(origin)}" if origin else "- Work offset values unknown",
            f"- Machine position: {' '.join(machine)}",
            f"- Soft limits (machine coordinates): {' '.join(limits)}",
            f"- Axis travel (machine coordinates): {'; '.join(travel)}",
            f"- Spindle: {spindle_text}; tool in spindle: T{stat.tool_in_spindle}",
            f"- Loaded program: {program}; program state: {program_state}",
        ]
        return "\n".join(lines)
    except Exception as e:
        return f"Machine status unavailable ({e})."
