"""
The machine, as the UI sees it.

MachineModel holds a snapshot of the machine state (plain attributes) and emits
`changed(topic)` when a group of fields changes, plus `position_changed` for the
high-rate DRO updates. Commands are methods on the model. Pages never touch
qtvcp's STATUS / ACTION directly, which keeps them testable and lets the whole
screen run against SimMachine for previews.

Topics: state, homing, position, spindle, overrides, tool, program, offsets, jog, coolant, drawbar
"""

import math
import os
import re
import time
from typing import Dict, List, Optional, Tuple

from PyQt5 import QtCore

try:
    import linuxcnc
except ImportError:  # previews on a machine without LinuxCNC
    linuxcnc = None

import machine_safety

AXES = ("X", "Y", "Z")
WCS_NAMES = ["G54", "G55", "G56", "G57", "G58", "G59", "G59.1", "G59.2", "G59.3"]

# Machine state labels shown in the top bar (and their tone for color)
STATE_TONES = {
    "E-STOP": "red", "OFF": "muted", "NOT HOMED": "amber", "HOMING": "accent",
    "READY": "green", "RUNNING": "green", "PAUSED": "amber", "MDI": "accent", "BUSY": "accent",
}

_INCREMENT_RE = re.compile(r'^\s*([0-9]*\.?[0-9]+)\s*(mm|cm|um|in|inch|mil)?\s*$', re.I)


def parse_increment(text: str, machine_metric: bool = True) -> Optional[float]:
    """'.5mm' -> 0.5 (in machine units); 'Continuous' -> 0; None if unreadable"""
    if text.strip().lower().startswith("cont"):
        return 0.0
    match = _INCREMENT_RE.match(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = (match.group(2) or ("mm" if machine_metric else "in")).lower()
    mm = {"mm": value, "cm": value * 10, "um": value / 1000, "in": value * 25.4,
          "inch": value * 25.4, "mil": value * 0.0254}[unit]
    return mm if machine_metric else mm / 25.4


def format_increment(value: float, units: str) -> str:
    if value == 0:
        return "Cont"
    return f"{value:g}"


class MachineModel(QtCore.QObject):
    """Machine snapshot + commands. Subclasses fill in the state and implement the commands."""

    changed = QtCore.pyqtSignal(str)
    position_changed = QtCore.pyqtSignal()
    message = QtCore.pyqtSignal(str, str)  # level (error/warning/info), text

    def __init__(self, parent=None):
        super().__init__(parent)
        self.axes: Tuple[str, ...] = AXES
        self.estop = True
        self.on = False
        self.mode = "manual"  # manual | mdi | auto
        self.interp = "idle"  # idle | running | paused | reading | waiting
        self.paused = False
        self.homed: Dict[str, bool] = {a: False for a in self.axes}
        self.homing = False
        self.home_required = True  # False with [TRAJ] NO_FORCE_HOMING
        self.metric = True  # display units (G21)
        self.machine_metric = True
        self.limits: Dict[str, Tuple[float, float]] = {a: (0.0, 0.0) for a in self.axes}

        self.pos_abs: List[float] = [0.0] * len(self.axes)
        self.pos_rel: List[float] = [0.0] * len(self.axes)
        self.pos_dtg: List[float] = [0.0] * len(self.axes)
        self.feed_rate = 0.0  # current velocity, units/min

        self.spindle_requested = 0.0
        self.spindle_actual = 0.0
        self.spindle_dir = 0  # -1, 0, 1
        self.spindle_min = 100.0
        self.spindle_max = 3000.0
        self.spindle_default = 1000.0
        self.spindle_step = 200.0

        self.feed_override = 100.0
        self.rapid_override = 100.0
        self.spindle_override = 100.0
        self.max_feed_override = 200.0
        self.min_spindle_override = 50.0
        self.max_spindle_override = 100.0
        self.max_velocity = 5000.0  # units/min, machine maximum
        self.velocity_limit = 5000.0  # units/min, current max-velocity setting

        self.tool = 0
        self.tool_comment = ""
        self.tool_diameter = 0.0
        self.tool_length = 0.0

        self.wcs = "G54"
        self.wcs_offsets: Dict[str, List[float]] = {}
        # False when the work position can't be worked out (the DRO then says so)
        self.work_offset_known = True
        # Work offset changes made from the screen, newest last: {time, label, wcs, offset, rotation}
        self.offset_history: List[dict] = []
        self.probe_tool: Optional[int] = None  # the touch probe's tool number (the spindle won't start with it)
        self.g92 = [0.0, 0.0, 0.0]          # G92 shift (machine units), applies to every work system
        self.tool_offset_z = 0.0            # the tool length offset in effect
        self.tool_length_applied = False    # G43 is on
        self.laser_on = False               # the camera's line laser (screen side; G-code can also turn it on)

        self.file = ""
        self.line = 0
        self.total_lines = 0
        self.run_started: Optional[float] = None
        self.run_elapsed = 0.0
        self.gcode_properties: Dict[str, str] = {}

        self.jog_rate = 1000.0  # units/min
        self.jog_rate_max = 3000.0
        self.jog_rate_min = 30.0
        self.increments: List[Tuple[str, float]] = [("Cont", 0.0), ("1", 1.0), ("0.1", 0.1), ("0.01", 0.01)]
        self.jog_increment = 0.0

        self.flood = False
        self.mist = False
        self.drawbar = False           # released (by the drawbar sequence): the tool is free
        self.drawbar_lowered = False   # the motor is down on the drawbar
        self.drawbar_axis = ""         # the A axis that turns it ("" if there's no power drawbar)
        self.drawbar_turn_speed = 1.5  # turns per second for the test turns
        self.probe_tripped = False

        self.mdi_commands: List[Tuple[str, str]] = []  # (label, code) from the INI
        self.program_prefix = os.path.expanduser("~/linuxcnc/nc_files")
        config_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.tool_table_path = os.path.join(config_dir, "tool.tbl")
        self.tool_links_path = os.path.join(config_dir, "tool_links.json")
        self.ini_path = os.path.join(config_dir, "Mesa7I96S.ini")  # the machine's INI (Calibrate writes BACKLASH)

    # --- derived -------------------------------------------------------------------------

    @property
    def units(self) -> str:
        return "mm" if self.metric else "in"

    @property
    def machine_units(self) -> str:
        """Units of jog increments and jog speed (always machine units, whatever G20/G21 says)"""
        return "mm" if self.machine_metric else "in"

    @property
    def all_homed(self) -> bool:
        return all(self.homed.get(a, False) for a in self.axes)

    @property
    def is_running(self) -> bool:
        return self.mode == "auto" and self.interp != "idle"

    @property
    def moving(self) -> bool:
        return self.interp != "idle" or bool(getattr(self, "_targets", None))

    @property
    def ready(self) -> bool:
        """On, idle and homed: safe to accept motion commands"""
        return not self.estop and self.on and self.interp == "idle" and self.all_homed

    @property
    def state_label(self) -> str:
        if self.estop:
            return "E-STOP"
        if not self.on:
            return "OFF"
        if self.homing:
            return "HOMING"
        if self.mode == "auto" and self.interp != "idle":
            return "PAUSED" if self.paused else "RUNNING"
        if self.interp != "idle":
            return "BUSY"
        if not self.all_homed:
            return "NOT HOMED"
        return "READY"

    @property
    def state_detail(self) -> str:
        label = self.state_label
        if label == "E-STOP":
            return "Release E-stop to continue"
        if label == "OFF":
            return "Machine power is off"
        if label == "NOT HOMED":
            missing = [a for a in self.axes if not self.homed.get(a)]
            return "Home " + "".join(missing) + " before moving"
        if label in ("RUNNING", "PAUSED"):
            if self.total_lines:
                return f"Line {self.line} of {self.total_lines}"
            return os.path.basename(self.file)
        return {"manual": "Manual", "mdi": "MDI", "auto": "Auto"}.get(self.mode, "")

    @property
    def tool_table_lock(self) -> str:
        """Why the tool table can't be edited right now, or "" if it can. qtvcp's table applies
        each edit with an MDI G43, so it only unlocks when the machine is on, idle and homed."""
        if self.estop:
            return "Locked: release E-stop to edit tools"
        if not self.on:
            return "Locked: turn the machine on to edit tools"
        if self.is_running:
            return "Locked while a program runs"
        if self.interp != "idle":
            return "Locked while the machine is busy"
        if self.home_required and not self.all_homed:
            return "Locked: home the machine to edit tools"
        return ""

    @property
    def progress(self) -> float:
        """0..1 through the loaded program"""
        if not self.total_lines:
            return 0.0
        return max(0.0, min(1.0, self.line / float(self.total_lines)))

    def axis_index(self, axis: str) -> int:
        return self.axes.index(axis)

    # --- commands (overridden) ---------------------------------------------------------

    def set_estop(self, tripped: bool): raise NotImplementedError
    def set_power(self, on: bool): raise NotImplementedError
    def home_all(self): raise NotImplementedError
    def home_axis(self, axis: str): raise NotImplementedError
    def unhome_all(self): raise NotImplementedError
    def jog_start(self, axis: str, direction: int): raise NotImplementedError
    def jog_stop(self, axis: str): raise NotImplementedError
    # Game controller pendant: a jog of one tick's travel (velocity in units/min, signed), a
    # single step, and stop. LinuxCNC adds each increment to the axis target.
    def pendant_jog(self, axis: str, velocity: float, dt: float): raise NotImplementedError
    def pendant_step(self, axis: str, direction: int, distance: float): raise NotImplementedError
    def pendant_stop(self): raise NotImplementedError
    def set_jog_rate(self, rate: float): raise NotImplementedError
    def set_jog_increment(self, value: float, text: str): raise NotImplementedError
    def set_axis_origin(self, axis: str, value: float, remember: bool = True): raise NotImplementedError
    def mdi(self, command: str) -> bool: raise NotImplementedError
    def run(self, line: int = 0): raise NotImplementedError
    def pause_resume(self): raise NotImplementedError
    def step(self): raise NotImplementedError
    def abort(self): raise NotImplementedError
    def spindle_start(self, direction: int, rpm: float): raise NotImplementedError
    def spindle_stop(self): raise NotImplementedError
    def set_feed_override(self, pct: float): raise NotImplementedError
    def set_rapid_override(self, pct: float): raise NotImplementedError
    def set_spindle_override(self, pct: float): raise NotImplementedError
    def set_velocity_limit(self, units_per_min: float): raise NotImplementedError
    def toggle_flood(self): raise NotImplementedError
    def toggle_mist(self): raise NotImplementedError
    def open_program(self, path: str): raise NotImplementedError
    def set_wcs(self, name: str): raise NotImplementedError
    def reload_tool_table(self): raise NotImplementedError
    def current_offset(self, wcs: str) -> Optional[Tuple[List[float], float]]: raise NotImplementedError

    # --- the probe in the spindle ---------------------------------------------------------

    @property
    def probe_in_spindle(self) -> bool:
        return bool(self.probe_tool) and self.tool == self.probe_tool

    def spindle_locked(self, command: str = "M3") -> bool:
        """True (with a warning) if this would start the spindle with the touch probe in it, the
        drawbar motor down on the drawbar, or the tool released"""
        from probe_jobs import starts_spindle
        if not starts_spindle(command):
            return False
        if self.probe_in_spindle:
            reason = f"The touch probe (T{self.probe_tool}) is in the spindle: it won't start. Load a cutting tool first."
        elif self.drawbar_lowered:
            reason = "The drawbar motor is down on the drawbar: the spindle won't start. Raise it first."
        elif self.drawbar:
            reason = "The tool is released (drawbar undone): the spindle won't start. Clamp the tool first."
        else:
            return False
        self.message.emit("warning", reason)
        return True

    # --- the power drawbar (subroutines/drawbar_*.ngc, settings in the INI's [DRAWBAR]) ---------

    def _drawbar_ready(self) -> bool:
        if not self.drawbar_axis:
            self.message.emit("warning", "There's no power drawbar set up ([DRAWBAR] in the INI).")
            return False
        if self.is_running:
            self.message.emit("warning", "Not while a program is running.")
            return False
        if self.spindle_dir or self.spindle_actual > 10:
            self.message.emit("warning", "Stop the spindle first.")
            return False
        return True

    def drawbar_release(self) -> bool:
        """Lower the motor, undo the drawbar, lift the motor: the tool is free"""
        if not self._drawbar_ready() or not self.mdi("o<drawbar_release> call"):
            return False
        self._set("drawbar", drawbar=True)
        return True

    def drawbar_clamp(self) -> bool:
        """Lower the motor, do the drawbar up, lift the motor: the tool is held"""
        if not self._drawbar_ready() or not self.mdi("o<drawbar_clamp> call"):
            return False
        self._set("drawbar", drawbar=False)
        return True

    def drawbar_lower(self) -> bool:
        return self._drawbar_ready() and self.mdi("o<drawbar_lower> call")

    def drawbar_raise(self) -> bool:
        return bool(self.drawbar_axis) and self.mdi("o<drawbar_raise> call")

    def drawbar_turn(self, turns: float) -> bool:
        """Turn the drawbar motor (positive tightens), for setting it up"""
        return self._drawbar_ready() and self.mdi(f"o<drawbar_turn> call [{turns:g}] [{self.drawbar_turn_speed:g}]")

    # --- work offset history (undo) ---------------------------------------------------------

    MAX_OFFSET_HISTORY = 20

    def remember_offsets(self, label: str, wcs: Optional[str] = None) -> bool:
        """Note a work system's offset before changing it, so it can be undone"""
        wcs = wcs or self.wcs
        current = self.current_offset(wcs)
        if current is None:
            return False
        offset, rotation = current
        self.offset_history.append({"time": time.time(), "label": label, "wcs": wcs,
                                    "offset": [float(v) for v in offset[:3]], "rotation": float(rotation)})
        del self.offset_history[:-self.MAX_OFFSET_HISTORY]
        self.changed.emit("offset_history")
        return True

    def apply_offsets(self, wcs: str, values: Dict[str, float], rotation: Optional[float] = None,
                      label: Optional[str] = None) -> bool:
        """Set a work system's X/Y/Z origin (machine units) and/or rotation, remembering the old ones"""
        if label and not self.remember_offsets(label, wcs):
            self.message.emit("warning", f"Couldn't read {wcs} to keep an undo copy, so it wasn't changed.")
            return False
        words = " ".join(f"{axis}{value:.6f}" for axis, value in values.items())
        if rotation is not None:
            words += f" R{rotation:.6f}"
        if not self._g10(f"G10 L2 P{WCS_NAMES.index(wcs) + 1} {words}"):
            return False
        current = self.current_offset(wcs)
        if current is not None:
            offset = list(current[0])
            for axis, value in values.items():
                offset["XYZ".index(axis)] = value
            self._written(wcs, offset, current[1] if rotation is None else rotation)
        return True

    def set_origin_here(self, wcs: str, values: Dict[str, float], label: Optional[str] = None) -> bool:
        """Move a work system's origin so the tool's position reads `values` there (G10 L20: 'zero here'),
        for any system, active or not; remembers the old origin"""
        if label and not self.remember_offsets(label, wcs):
            self.message.emit("warning", f"Couldn't read {wcs} to keep an undo copy, so it wasn't changed.")
            return False
        current = self.current_offset(wcs)
        words = " ".join(f"{axis}{value:.6f}" for axis, value in values.items())
        if not self._g10(f"G10 L20 P{WCS_NAMES.index(wcs) + 1} {words}"):
            return False
        if current is not None:
            # Where the interpreter puts it: machine position - tool length (Z) - G92 - the value
            offset = list(current[0])
            position = self.machine_position()
            for axis, value in values.items():
                i = "XYZ".index(axis)
                offset[i] = position[i] - (self.tool_offset_z if axis == "Z" else 0.0) - self.g92[i] - value
            self._written(wcs, offset, current[1])
        return True

    def _g10(self, line: str) -> bool:
        # G10 reads the program's units; offsets are in machine units
        units, restore = ("G21", "G20") if self.machine_metric else ("G20", "G21")
        lines = [units, line]
        if self.metric != self.machine_metric:
            lines.append(restore)
        return self.mdi_lines(lines)

    def _written(self, wcs: str, offset: List[float], rotation: float):
        """A work offset this screen just wrote (subclasses use it until LinuxCNC reports it)"""

    def machine_position(self) -> List[float]:
        """The tool's machine position in machine units (pos_abs may be in program units)"""
        k = 1.0 if self.metric == self.machine_metric else (25.4 if self.metric else 1 / 25.4)
        return [v / k for v in (list(self.pos_abs) + [0.0, 0.0, 0.0])[:3]]

    def all_offsets(self) -> Dict[str, Optional[Tuple[List[float], float]]]:
        """Every work system's origin (machine units) and rotation, None where unknown"""
        return {name: self.current_offset(name) for name in WCS_NAMES}

    def clear_g92(self) -> bool:
        return self.mdi("G92.1")

    def apply_tool_length(self) -> bool:
        return self.mdi("G43")

    def undo_offsets(self) -> Optional[dict]:
        """Put back the work offset from before the last change; returns that history entry"""
        if not self.offset_history:
            return None
        entry = self.offset_history[-1]
        values = dict(zip("XYZ", entry["offset"]))
        if not self.apply_offsets(entry["wcs"], values, entry.get("rotation")):
            return None
        self.offset_history.pop()
        self.changed.emit("offset_history")
        return entry

    # Composite commands shared by both implementations

    def reload_program(self):
        if self.file:
            self.open_program(self.file)

    def set_tool(self, number: int):
        """Tell LinuxCNC which tool is in the spindle, without a tool change (M61), and apply its length"""
        return self.mdi(f"M61 Q{int(number)} G43")

    def change_tool(self, number: int):
        """Full tool change (prompts through the manual tool change dialog), then apply its length"""
        return self.mdi(f"T{int(number)} M6 G43")

    def mdi_lines(self, lines):
        """Several MDI lines as one command: checked once, then queued in order (LinuxCNC
        queues MDI commands, so they run one after the other)"""
        lines = [line.strip() for line in lines if line.strip()]
        if any(self.spindle_locked(line) for line in lines):
            return False
        if not lines or not self.mdi(lines[0]):
            return False
        for line in lines[1:]:
            self._queue_mdi(line)
        return True

    def _queue_mdi(self, line):
        raise NotImplementedError

    def move_to(self, x=None, y=None, z=None):
        """Rapid the spindle to a machine position (G53); Z first when going up, last when down"""
        words = []
        if z is not None and z >= self.pos_abs[2]:
            if not self.mdi(f"G90 G53 G0 Z{z:.4f}"):
                return False
            z_done = True
        else:
            z_done = False
        if x is not None:
            words.append(f"X{x:.4f}")
        if y is not None:
            words.append(f"Y{y:.4f}")
        lines = [f"G90 G53 G0 {' '.join(words)}"] if words else []
        if z is not None and not z_done:
            lines.append(f"G90 G53 G0 Z{z:.4f}")
        if not lines:
            return True
        if z_done:
            for line in lines:
                self._queue_mdi(line)
            return True
        return self.mdi_lines(lines)

    def set_laser(self, on: bool) -> bool:
        """The line laser beside the camera (milo.laser, 7i96S OUT0): camera routines switch it.
        Returns False if there's no way to switch it."""
        self.laser_on = bool(on)
        return True

    def at_position(self, x=None, y=None, z=None, tolerance=0.02) -> bool:
        """Stopped at a machine position (for step-by-step routines like a camera scan)"""
        target = (x, y, z)
        for i, value in enumerate(target):
            if value is not None and abs(self.pos_abs[i] - value) > tolerance:
                return False
        return not self.moving

    def go_to_work_zero(self):
        """Lift to machine Z0 first, then rapid to the work X0 Y0"""
        return self.mdi_lines(["G90 G53 G0 Z0", "G90 G0 X0 Y0"])

    def stat(self):
        """A linuxcnc.stat-like object for the AI safety checks"""
        return None

    def action_api(self):
        """What Milo's confirmed actions call: CALL_MDI, SET_MACHINE_HOMING, SET_MACHINE_STATE, RUN"""
        return _ModelActions(self)

    def run_macro(self, index: int):
        """Run one of the INI's [MDI_COMMAND_LIST] entries"""
        if 0 <= index < len(self.mdi_commands):
            if self.spindle_locked(self.mdi_commands[index][1]):
                return False
            return self.mdi_lines(self.mdi_commands[index][1].split(";"))
        return False

    # --- helpers -------------------------------------------------------------------------

    def _emit(self, *topics):
        for topic in topics:
            self.changed.emit(topic)

    def _set(self, topic, **fields):
        """Set attributes; emit `topic` if any of them changed"""
        changed = False
        for name, value in fields.items():
            if getattr(self, name) != value:
                setattr(self, name, value)
                changed = True
        if changed:
            self.changed.emit(topic)
        return changed


class _ModelActions:
    """The qtvcp Action calls that action_controller uses, mapped onto a MachineModel"""

    def __init__(self, model):
        self.model = model

    def CALL_MDI(self, line):
        return 0 if self.model.mdi(line) else -1

    def SET_MACHINE_HOMING(self, joint):
        self.model.home_all()

    def SET_MACHINE_STATE(self, on):
        self.model.set_power(on)

    def RUN(self, line=0):
        self.model.run(line)


# =======================================================================================
# Real machine: qtvcp STATUS / ACTION
# =======================================================================================

class QtvcpMachine(MachineModel):
    """MachineModel backed by qtvcp's Status (hal_glib) and Action singletons"""

    def __init__(self, halcomp=None, parent=None):
        super().__init__(parent)
        from qtvcp.core import Status, Action, Info, Tool
        self.STATUS, self.ACTION, self.INFO, self.TOOL = Status(), Action(), Info(), Tool()
        self.halcomp = halcomp
        self._probe_pin = None
        self._laser_pin = None
        self._offset_sync_tried = False
        self._read_ini()
        self._connect()
        self._poll()

    # --- setup -------------------------------------------------------------------------

    def _read_ini(self):
        info = self.INFO
        coords = (info.get_error_safe_setting("TRAJ", "COORDINATES", "XYZ") or "XYZ").replace(" ", "")
        # The power drawbar's motor is an axis to LinuxCNC (so G-code can turn it) but not to the operator
        drawbar = (info.get_error_safe_setting("DRAWBAR", "AXIS", "") or "").strip().upper()
        self.drawbar_axis = drawbar if drawbar and drawbar in coords.upper() else ""
        self.drawbar_lower_is_on = str(info.get_error_safe_setting("DRAWBAR", "LOWER_IS_ON", "1")).strip() != "0"
        try:
            self.drawbar_turn_speed = float(info.get_error_safe_setting("DRAWBAR", "TURN_SPEED", "1.5"))
        except (TypeError, ValueError):
            pass
        self.axes = tuple(a for a in coords.upper() if a in "XYZABCUVW" and a != self.drawbar_axis) or AXES
        self.homed = {a: False for a in self.axes}
        self.pos_abs = [0.0] * len(self.axes)
        self.pos_rel = [0.0] * len(self.axes)
        self.pos_dtg = [0.0] * len(self.axes)
        self.machine_metric = bool(info.MACHINE_IS_METRIC)
        self.metric = self.machine_metric
        self.limits = {}
        for axis in self.axes:
            lo = float(info.get_error_safe_setting(f"AXIS_{axis}", "MIN_LIMIT", "0") or 0)
            hi = float(info.get_error_safe_setting(f"AXIS_{axis}", "MAX_LIMIT", "0") or 0)
            self.limits[axis] = (lo, hi)
        self.spindle_min = float(info.MIN_SPINDLE_SPEED or 100)
        self.spindle_max = float(info.MAX_SPINDLE_SPEED or 3000)
        self.spindle_default = float(info.DEFAULT_SPINDLE_SPEED or 1000)
        self.spindle_step = float(info.get_error_safe_setting("DISPLAY", "SPINDLE_INCREMENT", "200") or 200)
        # Info already scales the override limits to percent
        self.max_feed_override = float(info.MAX_FEED_OVERRIDE or 200)
        self.min_spindle_override = float(info.MIN_SPINDLE_OVERRIDE or 50)
        self.max_spindle_override = float(info.MAX_SPINDLE_OVERRIDE or 100)
        self.max_velocity = float(info.MAX_TRAJ_VELOCITY or 5000)  # units/min
        self.velocity_limit = self.max_velocity
        self.jog_rate = float(info.DEFAULT_LINEAR_JOG_VEL or 1000)
        self.jog_rate_max = float(info.MAX_LINEAR_JOG_VEL or self.max_velocity)
        self.jog_rate_min = float(info.MIN_LINEAR_JOG_VEL or 30)
        self.increments = []
        for text in info.JOG_INCREMENTS or ["Continuous"]:
            value = parse_increment(text, self.machine_metric)
            if value is not None:
                self.increments.append((format_increment(value, self.units), value))
        if not any(v == 0 for _, v in self.increments):
            self.increments.insert(0, ("Cont", 0.0))
        self.mdi_commands = []
        labels = info.MDI_COMMAND_LABEL_LIST or []
        for index, code in enumerate(info.MDI_COMMAND_LIST or []):
            label = labels[index] if index < len(labels) and labels[index] else code
            self.mdi_commands.append((str(label).replace("\\n", " ").strip(), code.strip()))
        self.program_prefix = info.PROGRAM_PREFIX or self.program_prefix
        self.home_required = not info.NO_HOME_REQUIRED
        table = info.get_error_safe_setting("EMCIO", "TOOL_TABLE", "tool.tbl") or "tool.tbl"
        config_dir = os.path.dirname(os.path.abspath(info.INIPATH))
        self.tool_table_path = table if os.path.isabs(table) else os.path.join(config_dir, table)
        self.tool_links_path = os.path.join(config_dir, "tool_links.json")
        self.ini_path = os.path.abspath(info.INIPATH)

    def _connect(self):
        S = self.STATUS
        S.connect("periodic", lambda w: self._poll())
        S.connect("current-position", self._on_position)
        S.connect("actual-spindle-speed-changed", self._on_spindle_actual)
        S.connect("file-loaded", self._on_file_loaded)
        S.connect("line-changed", self._on_line)
        S.connect("graphics-gcode-properties", lambda w, props: self._on_properties(props))
        S.connect("error", self._on_error)
        S.connect("jograte-changed", lambda w, rate: self._set("jog", jog_rate=float(rate)))
        S.connect("jogincrement-changed", lambda w, incr, text: self._set("jog", jog_increment=float(incr)))
        S.connect("tool-info-changed", lambda w, tool: self._on_tool_info(tool))
        S.connect("interp-run", lambda w: self._on_run_started())

    def make_pins(self, qhal):
        """HAL pins owned by the screen (called by the screen handler once HAL is available)"""
        try:
            self._probe_pin = qhal.newpin("led-probe", qhal.HAL_BIT, qhal.HAL_IN)
            self._probe_pin.value_changed.connect(lambda v: self._set("state", probe_tripped=bool(v)))
            # the line laser: or'd with M64 P1 in Mesa7I96S.hal / custom_postgui.hal
            self._laser_pin = qhal.newpin("laser", qhal.HAL_BIT, qhal.HAL_OUT)
        except Exception as e:
            self.message.emit("warning", f"Could not create screen HAL pins: {e}")

    # --- state updates -----------------------------------------------------------------

    def _poll(self):
        s = self.STATUS.stat
        L = linuxcnc
        mode = {L.MODE_MANUAL: "manual", L.MODE_AUTO: "auto", L.MODE_MDI: "mdi"}.get(s.task_mode, "manual")
        interp = {L.INTERP_IDLE: "idle", L.INTERP_READING: "reading", L.INTERP_PAUSED: "paused",
                  L.INTERP_WAITING: "waiting"}.get(s.interp_state, "idle")
        if interp in ("reading", "waiting") and mode == "auto":
            interp = "running"
        joints = s.joints
        homed = {}
        for i, axis in enumerate(self.axes):
            homed[axis] = bool(s.homed[i]) if i < joints else False
        homing = any(s.joint[j]["homing"] for j in range(joints))
        self._set("state", estop=s.task_state == L.STATE_ESTOP, on=s.task_state == L.STATE_ON,
                  mode=mode, interp=interp, paused=bool(s.paused))
        self._set("homing", homed=homed, homing=homing)
        wcs = machine_safety.active_work_offset(s)
        self._set("offsets", metric=s.program_units != 1 if s.program_units else self.machine_metric,
                  wcs=wcs if wcs in WCS_NAMES else "G54")
        self._sync_offsets_if_needed(s)
        parts = machine_safety.offset_parts(s)
        g92 = [round(v, 6) for v in (parts[1][:3] if parts else [0.0, 0.0, 0.0])]
        self._set("offsets", g92=g92, tool_offset_z=round(float(s.tool_offset[2]), 6),
                  tool_length_applied=any(code in s.gcodes for code in (430, 431, 432)))
        spindle = s.spindle[0]
        direction = int(spindle["direction"]) if spindle["enabled"] else 0
        self._set("spindle", spindle_requested=abs(float(spindle["speed"])), spindle_dir=direction)
        self._set("overrides", feed_override=round(s.feedrate * 100), rapid_override=round(s.rapidrate * 100),
                  spindle_override=round(spindle["override"] * 100), velocity_limit=s.max_velocity * 60)
        self._set("coolant", flood=bool(s.flood), mist=bool(s.mist))
        if self.drawbar_axis:
            out = bool(s.dout[0]) if len(s.dout) else False  # M64/M65 P0: the lift valve
            self._set("drawbar", drawbar_lowered=out == self.drawbar_lower_is_on)
        self._set("tool", tool=int(s.tool_in_spindle))
        feed = s.current_vel * 60
        if abs(feed - self.feed_rate) > 0.5:
            self.feed_rate = feed
        if self.run_started is not None and not self.paused:
            self.run_elapsed = time.time() - self.run_started
        if self.run_started is not None and interp == "idle":
            self.run_started = None
            self.changed.emit("program")

    def _sync_offsets_if_needed(self, s):
        """
        Make LinuxCNC's status report the work offset the interpreter applies.

        After startup the status says "no offset" (machine_safety.offsets_synced) while the
        interpreter applies the G54 it loaded from linuxcnc.var, so qtvcp's DRO and preview show
        machine coordinates as work coordinates. Selecting the active system again (an MDI G54,
        no motion) makes the interpreter report it. Once per power-on, as soon as the machine is
        on, idle, still, and homed when homing is required (LinuxCNC refuses MDI before that).
        """
        if not self.on:
            self._offset_sync_tried = False
            return
        if (self._offset_sync_tried or machine_safety.offsets_synced(s) or self.estop
                or self.interp != "idle" or self.mode == "auto" or self.homing or not s.inpos
                or (self.home_required and not self.all_homed)):
            return
        wcs = machine_safety.active_work_offset(s)
        if wcs not in WCS_NAMES:
            return
        self._offset_sync_tried = True
        # Not from inside the status poll: SET_USER_SYSTEM changes mode and waits for the MDI
        QtCore.QTimer.singleShot(0, lambda: self._sync_offsets(wcs))

    def _sync_offsets(self, wcs):
        if not self.on or self.interp != "idle":
            self._offset_sync_tried = False  # try again on a later poll
            return
        try:
            self.ACTION.SET_USER_SYSTEM(wcs)
        except Exception as e:
            self.message.emit("warning", f"Could not load the {wcs} work offset into the display: {e}")

    def _convert(self, values):
        """Machine units -> display units"""
        if self.metric == self.machine_metric:
            return list(values)
        factor = 25.4 if self.metric else 1 / 25.4
        return [v * factor for v in values]

    def _on_position(self, w, absolute, relative, dtg, joint):
        n = len(self.axes)
        s = self.STATUS.stat
        known = True
        if not machine_safety.offsets_synced(s):
            # qtvcp's relative position uses the status offsets, which read zero until synced
            work = machine_safety.work_position(s, list(absolute))
            if work is None:
                known = False
            else:
                relative = work
        self.pos_abs = self._convert(absolute[:n])
        self.pos_rel = self._convert(relative[:n])
        if known != self.work_offset_known:
            self._set("offsets", work_offset_known=known)
        self.pos_dtg = self._convert(dtg[:n])
        self.position_changed.emit()

    def _on_spindle_actual(self, w, speed):
        self._set("spindle", spindle_actual=abs(float(speed)))

    def _on_file_loaded(self, w, filename):
        total = 0
        try:
            with open(filename, "r", errors="ignore") as f:
                total = sum(1 for _ in f)
        except OSError:
            pass
        self.file, self.total_lines, self.line = filename, total, 0
        self.gcode_properties = {}
        self.run_elapsed = 0.0
        self.changed.emit("program")

    def _on_line(self, w, line):
        if line != self.line:
            self.line = int(line)
            self.changed.emit("program")

    def _on_run_started(self):
        if self.mode == "auto" and self.run_started is None:
            self.run_started = time.time()
            self.run_elapsed = 0.0
            self.changed.emit("program")

    def _on_properties(self, props):
        self.gcode_properties = dict(props or {})
        self.changed.emit("program")

    def _on_tool_info(self, tool):
        comment, diameter, length = "", 0.0, 0.0
        try:
            if tool.id not in (-1, 0):
                info = self.TOOL.GET_TOOL_INFO(tool.id)
                comment = str(info[self.TOOL.COMMENTS]).strip()
                diameter = float(info[self.TOOL.DIAMETER])
                length = float(info[self.TOOL.Z])
        except Exception:
            pass
        self._set("tool", tool_comment=comment, tool_diameter=diameter, tool_length=length)

    def _on_error(self, w, kind, text):
        L = linuxcnc
        if kind in (L.NML_ERROR, L.OPERATOR_ERROR):
            level = "error"
        elif kind in (L.NML_DISPLAY, L.OPERATOR_DISPLAY):
            level = "info"
        else:
            level = "info"
        self.message.emit(level, str(text).strip())

    # --- commands ------------------------------------------------------------------------

    def set_estop(self, tripped):
        self.ACTION.SET_ESTOP_STATE(bool(tripped))

    def set_power(self, on):
        self.ACTION.SET_MACHINE_STATE(bool(on))

    def home_all(self):
        self.ACTION.SET_MACHINE_HOMING(-1)

    def home_axis(self, axis):
        self.ACTION.SET_MACHINE_HOMING(self.INFO.get_jnum_from_axisnum(self.axis_index(axis)))

    def unhome_all(self):
        self.ACTION.SET_MACHINE_UNHOMED(-1)

    def jog_start(self, axis, direction):
        if not (self.on and not self.estop):
            self.message.emit("warning", "Turn the machine on to jog")
            return
        if self.mode != "manual":
            self.ACTION.SET_MANUAL_MODE()
        increment = self.jog_increment
        rate = self.jog_rate / 60.0
        self.ACTION.JOG(self.axis_index(axis), direction, rate, increment)

    def jog_stop(self, axis):
        if self.jog_increment == 0:  # incremental jogs finish on their own
            self.ACTION.JOG(self.axis_index(axis), 0, 0, 0)

    def pendant_jog(self, axis, velocity, dt):
        distance = abs(velocity) * dt / 60.0
        if distance <= 0 or not self.ready:
            return
        if self.mode != "manual":
            self.ACTION.SET_MANUAL_MODE()
        # A little faster than the stick asks, so the axis keeps up with its target
        from milo_ui.pendant import CATCH_UP
        self.ACTION.JOG(self.axis_index(axis), 1 if velocity > 0 else -1, abs(velocity) * CATCH_UP / 60.0, distance)

    def pendant_step(self, axis, direction, distance):
        if not self.ready or distance <= 0:
            return
        if self.mode != "manual":
            self.ACTION.SET_MANUAL_MODE()
        self.ACTION.JOG(self.axis_index(axis), direction, self.jog_rate / 60.0, distance)

    def pendant_stop(self):
        for axis in self.axes:
            self.ACTION.JOG(self.axis_index(axis), 0, 0, 0)

    def set_jog_rate(self, rate):
        self.ACTION.SET_JOG_RATE(max(self.jog_rate_min, min(self.jog_rate_max, float(rate))))

    def set_jog_increment(self, value, text):
        self.ACTION.SET_JOG_INCR(float(value), text)

    def set_axis_origin(self, axis, value, remember=True):
        if remember:
            self.remember_offsets(f"{'Zero' if float(value) == 0 else 'Set'} {axis}"
                                  + ("" if float(value) == 0 else f" to {float(value):g}"))
        self.ACTION.SET_AXIS_ORIGIN(axis, float(value))

    def current_offset(self, wcs):
        stat = self.STATUS.stat
        if not (machine_safety.offsets_synced(stat) and machine_safety.active_work_offset(stat) == wcs):
            # LinuxCNC rewrites linuxcnc.var only now and then: until it does, what was just written wins
            written = getattr(self, "_written_offsets", {}).get(wcs)
            path = machine_safety._parameter_file(stat)
            try:
                stale = written is not None and (path is None or os.path.getmtime(path) <= written[2])
            except OSError:
                stale = written is not None
            if stale:
                return list(written[0]), written[1]
        offset = machine_safety.stored_work_offset(stat, wcs)
        if offset is None:
            return None
        return list(offset[:3]), machine_safety.stored_work_rotation(stat, wcs) or 0.0

    def _written(self, wcs, offset, rotation):
        if not hasattr(self, "_written_offsets"):
            self._written_offsets = {}
        self._written_offsets[wcs] = (list(offset), float(rotation), time.time())
        self._emit("offsets")

    def mdi(self, command):
        if self.spindle_locked(command):
            return False
        if self.estop or not self.on:
            self.message.emit("warning", "Turn the machine on first")
            return False
        if self.interp != "idle":
            self.message.emit("warning", "The machine is busy")
            return False
        return self.ACTION.CALL_MDI(command) != -1

    def _queue_mdi(self, line):
        self.ACTION.CALL_MDI(line)

    def run(self, line=0):
        line = int(line)
        if line > 1:
            # qtvcp's run-from-line dialog lets the operator preset spindle/feed/offsets first
            info = f"<b>Running from line: {line}</b>"
            self.ACTION.CALL_DIALOG({"NAME": "RUNFROMLINE", "TITLE": "Run from line", "ID": "_RUNFROMLINE",
                                     "MESSAGE": info, "LINE": line})
        else:
            self.ACTION.RUN(0)

    def pause_resume(self):
        self.ACTION.PAUSE()

    def step(self):
        self.ACTION.STEP()

    def abort(self):
        self.ACTION.ABORT()

    def set_laser(self, on):
        if self._laser_pin is None:
            return False
        self._laser_pin.set(bool(on))
        self.laser_on = bool(on)
        return True

    def spindle_start(self, direction, rpm):
        if self.spindle_locked():
            return
        if self.mode != "manual" and self.interp == "idle":
            self.ACTION.SET_MANUAL_MODE()
        rpm = max(self.spindle_min, min(self.spindle_max, float(rpm)))
        self.ACTION.SET_SPINDLE_ROTATION(1 if direction >= 0 else -1, rpm, 0)

    def spindle_stop(self):
        self.ACTION.SET_SPINDLE_STOP(0)

    def set_feed_override(self, pct):
        self.ACTION.SET_FEED_RATE(max(0, min(self.max_feed_override, pct)))

    def set_rapid_override(self, pct):
        self.ACTION.SET_RAPID_RATE(max(0, min(100, pct)))

    def set_spindle_override(self, pct):
        self.ACTION.SET_SPINDLE_RATE(max(self.min_spindle_override, min(self.max_spindle_override, pct)))

    def set_velocity_limit(self, units_per_min):
        self.ACTION.SET_MAX_VELOCITY_RATE(max(1.0, min(self.max_velocity, units_per_min)))

    def toggle_flood(self):
        self.ACTION.TOGGLE_FLOOD()

    def toggle_mist(self):
        self.ACTION.TOGGLE_MIST()

    def open_program(self, path):
        self.ACTION.OPEN_PROGRAM(path)

    def set_wcs(self, name):
        self.ACTION.SET_USER_SYSTEM(name)

    def reload_tool_table(self):
        self.ACTION.RELOAD_TOOLTABLE()

    def run_macro(self, index):
        # The same checks as a typed MDI line
        if 0 <= index < len(self.mdi_commands) and self.spindle_locked(self.mdi_commands[index][1]):
            return False
        if self.estop or not self.on:
            self.message.emit("warning", "Turn the machine on first")
            return False
        if self.interp != "idle":
            self.message.emit("warning", "The machine is busy")
            return False
        self.ACTION.CALL_INI_MDI(index)
        return True

    def stat(self):
        return self.STATUS.stat

    def action_api(self):
        return self.ACTION


# =======================================================================================
# Simulated machine: previews, screenshots and tests
# =======================================================================================

class SimStat:
    """Enough of linuxcnc.stat for the AI safety checks, built from a SimMachine"""

    def __init__(self, machine: "SimMachine"):
        L = linuxcnc
        m = machine
        self.task_state = (L.STATE_ESTOP if m.estop else L.STATE_ON if m.on else L.STATE_OFF) if L else 0
        self.interp_state = (L.INTERP_IDLE if m.interp == "idle" else L.INTERP_READING) if L else 1
        self.task_mode = {"manual": 1, "auto": 2, "mdi": 3}[m.mode]
        self.joints = len(m.axes)
        self.homed = tuple(int(m.homed[a]) for a in m.axes)
        self.joint = [{"homing": 0}] * len(m.axes)
        self.axis_mask = (1 << len(m.axes)) - 1
        self.program_units = 2 if m.metric else 1
        self.linear_units = 1.0 if m.machine_metric else 1 / 25.4
        self.gcodes = (0, 900, 210, 540)
        self.rotation_xy = 0.0
        self.position = list(m.pos_abs) + [0.0] * (9 - len(m.axes))
        offset = m.wcs_offsets.get(m.wcs, [0.0] * 3)
        self.g5x_offset = list(offset) + [0.0] * (9 - len(offset))
        self.g92_offset = [0.0] * 9
        self.tool_offset = [0.0] * 9
        self.axis = [{"min_position_limit": m.limits[a][0], "max_position_limit": m.limits[a][1]} for a in m.axes]
        self.g5x_index = WCS_NAMES.index(m.wcs) + 1
        self.spindle = [{"enabled": int(m.spindle_dir != 0), "speed": m.spindle_requested * m.spindle_dir}]
        self.tool_in_spindle = m.tool
        self.file = m.file
        self.ini_filename = ""

    def poll(self):
        pass


class SimMachine(MachineModel):
    """A believable pretend machine: jogs move, homing homes, programs run"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.limits = {"X": (0.0, 500.0), "Y": (0.0, 175.0), "Z": (-253.0, 0.0)}
        self.increments = [("Cont", 0.0), ("10", 10.0), ("5", 5.0), ("1", 1.0), ("0.5", 0.5),
                           ("0.1", 0.1), ("0.05", 0.05), ("0.01", 0.01)]
        self.max_velocity = 1980.0
        self.velocity_limit = 1980.0
        self.jog_rate = 1500.0
        self.jog_rate_max = 1800.0
        self.wcs_offsets = {name: [0.0, 0.0, 0.0] for name in WCS_NAMES}
        self.wcs_offsets["G54"] = [94.675, 84.35, -112.8375]
        self.wcs_rotation: Dict[str, float] = {}
        self.drawbar_axis = "A"  # a power drawbar, like the real machine
        self.pos_abs = [212.5, 96.35, -64.2]
        self._update_rel()
        self.mdi_commands = [("Go to G54", "G0 Z0;X0 Y0"), ("Center machine", "G53 G0 Z-10;G53 G0 X250 Y87.5"),
                             ("Spindle test", "M3 S1000")]
        # Previews edit copies, never the real tool table
        import shutil
        import tempfile
        sim_dir = tempfile.mkdtemp(prefix="milo-sim-")
        if os.path.exists(self.tool_table_path):
            shutil.copy(self.tool_table_path, os.path.join(sim_dir, "tool.tbl"))
        self.tool_table_path = os.path.join(sim_dir, "tool.tbl")
        self.tool_links_path = os.path.join(sim_dir, "tool_links.json")
        if os.path.exists(self.ini_path):
            shutil.copy(self.ini_path, os.path.join(sim_dir, "Mesa7I96S.ini"))
        self.ini_path = os.path.join(sim_dir, "Mesa7I96S.ini")
        self._jogging: Dict[str, int] = {}
        self._targets: List[List[Optional[float]]] = []  # queued simulated G53 rapids
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)
        self._last = time.time()

    def _update_rel(self):
        off = self.wcs_offsets.get(self.wcs, [0.0, 0.0, 0.0])
        tool = [0.0, 0.0, self.tool_offset_z if self.tool_length_applied else 0.0]
        self.pos_rel = [p - o - g - t for p, o, g, t in zip(self.pos_abs, off, self.g92, tool)]

    def _tick(self):
        now = time.time()
        dt, self._last = now - self._last, now
        moved = False
        for axis, direction in list(self._jogging.items()):
            i = self.axis_index(axis)
            lo, hi = self.limits[axis]
            self.pos_abs[i] = max(lo, min(hi, self.pos_abs[i] + direction * self.jog_rate / 60.0 * dt))
            moved = True
        if self._targets:
            target = self._targets[0]
            speed = 6000.0 / 60.0 * dt
            done = True
            for i, value in enumerate(target):
                if value is None:
                    continue
                delta = value - self.pos_abs[i]
                if abs(delta) > speed:
                    self.pos_abs[i] += math.copysign(speed, delta)
                    done = False
                else:
                    self.pos_abs[i] = value
            moved = True
            if done:
                self._targets.pop(0)
        if self.spindle_dir:
            target = self.spindle_requested * self.spindle_override / 100.0
            self.spindle_actual += (target - self.spindle_actual) * min(1.0, dt * 3)
            self.changed.emit("spindle")
        elif self.spindle_actual > 1:
            self.spindle_actual *= max(0.0, 1 - dt * 3)
            self.changed.emit("spindle")
        if self.is_running and not self.paused:
            self.line = min(self.total_lines, self.line + max(1, int(dt * 20)))
            self.run_elapsed += dt
            self.pos_abs[0] = 150 + 60 * ((self.line % 40) / 40.0)
            self.pos_abs[1] = 90 + 30 * ((self.line % 17) / 17.0)
            moved = True
            if self.line >= self.total_lines:
                self.interp, self.mode, self.run_started = "idle", "manual", None
                self.message.emit("success", f"{os.path.basename(self.file)} finished")
                self._emit("state")
            self.changed.emit("program")
        if moved:
            self._update_rel()
            self.position_changed.emit()

    # --- commands ------------------------------------------------------------------------

    def set_estop(self, tripped):
        self.estop = bool(tripped)
        if tripped:
            self.on = False
            self._stop_everything()
        self._emit("state")

    def set_power(self, on):
        if self.estop and on:
            self.message.emit("warning", "Release the E-stop first")
            return
        self.on = bool(on)
        if not on:
            self._stop_everything()
        self._emit("state")

    def _stop_everything(self):
        self._jogging.clear()
        self._targets.clear()
        self.spindle_dir = 0
        if self.is_running:
            self.interp, self.mode = "idle", "manual"
        self._emit("spindle", "program")

    def home_all(self):
        if not self.on:
            return self.message.emit("warning", "Turn the machine on first")
        self.homing = True
        self._emit("state", "homing")

        def done():
            self.homing = False
            self.homed = {a: True for a in self.axes}
            self.pos_abs = [10.0, 10.0, -10.0]
            self._update_rel()
            self.position_changed.emit()
            self._emit("state", "homing")
        QtCore.QTimer.singleShot(1500, done)

    def home_axis(self, axis):
        self.homed[axis] = True
        self._emit("state", "homing")

    def unhome_all(self):
        self.homed = {a: False for a in self.axes}
        self._emit("state", "homing")

    def jog_start(self, axis, direction):
        if not self.on:
            return self.message.emit("warning", "Turn the machine on to jog")
        if self.jog_increment:
            i = self.axis_index(axis)
            lo, hi = self.limits[axis]
            self.pos_abs[i] = max(lo, min(hi, self.pos_abs[i] + direction * self.jog_increment))
            self._update_rel()
            self.position_changed.emit()
        else:
            self._jogging[axis] = direction

    def jog_stop(self, axis):
        self._jogging.pop(axis, None)

    def pendant_jog(self, axis, velocity, dt):
        self._move_axis(axis, velocity * dt / 60.0)

    def pendant_step(self, axis, direction, distance):
        self._move_axis(axis, direction * distance)

    def pendant_stop(self):
        pass

    def _move_axis(self, axis, delta):
        if not self.ready:
            return
        i = self.axis_index(axis)
        lo, hi = self.limits[axis]
        self.pos_abs[i] = max(lo, min(hi, self.pos_abs[i] + delta))
        self._update_rel()
        self.position_changed.emit()

    def set_jog_rate(self, rate):
        self.jog_rate = max(self.jog_rate_min, min(self.jog_rate_max, float(rate)))
        self._emit("jog")

    def set_jog_increment(self, value, text):
        self.jog_increment = float(value)
        self._emit("jog")

    def set_axis_origin(self, axis, value, remember=True):
        if remember:
            self.remember_offsets(f"{'Zero' if float(value) == 0 else 'Set'} {axis}")
        i = self.axis_index(axis)
        self.wcs_offsets[self.wcs][i] = self.pos_abs[i] - float(value)
        self._update_rel()
        self.position_changed.emit()
        self._emit("offsets")

    def current_offset(self, wcs):
        offset = self.wcs_offsets.get(wcs)
        return (list(offset[:3]), self.wcs_rotation.get(wcs, 0.0)) if offset is not None else None

    def mdi(self, command):
        if self.spindle_locked(command):
            return False
        if not self.on:
            self.message.emit("warning", "Turn the machine on first")
            return False
        self._simulate(command)
        self.message.emit("info", f"MDI: {command}")
        return True

    def _queue_mdi(self, line):
        self._simulate(line)
        self.message.emit("info", f"MDI: {line}")

    def _simulate(self, line):
        """Pretend-move for G53 rapids and feeds (camera scans, backlash measurements and moves in previews and tests), and G10 L2
        work offset changes"""
        words = line.upper().split()
        if words and words[0].startswith("O<DRAWBAR_"):
            # The drawbar subroutines: lower/raise move the motor; the sequences end with it up
            self._set("drawbar", drawbar_lowered=words[0] == "O<DRAWBAR_LOWER>")
            return
        if words == ["G92.1"]:
            self.g92 = [0.0, 0.0, 0.0]
            self._update_rel()
            self._emit("offsets")
            return
        if "G43" in words:
            self.tool_offset_z, self.tool_length_applied = self.tool_length, True
            self._emit("offsets")
        if words[:2] in (["G10", "L2"], ["G10", "L20"]) and len(words) > 2 and words[2].startswith("P"):
            name = WCS_NAMES[int(float(words[2][1:])) - 1]
            for w in words[3:]:
                if w[0] in "XYZ":
                    i = "XYZ".index(w[0])
                    if words[1] == "L2":
                        self.wcs_offsets[name][i] = float(w[1:])
                    else:  # the tool's position reads this value
                        tool = self.tool_offset_z if w[0] == "Z" else 0.0
                        self.wcs_offsets[name][i] = self.pos_abs[i] - tool - self.g92[i] - float(w[1:])
                elif w[0] == "R":
                    self.wcs_rotation[name] = float(w[1:])
            self._update_rel()
            self.position_changed.emit()
            self._emit("offsets")
            return
        if "G53" in words and ("G0" in words or "G1" in words):
            target = [None, None, None]
            for w in words:
                if w[0] in "XYZ" and len(w) > 1:
                    target["XYZ".index(w[0])] = float(w[1:])
            self._targets.append(target)

    def run(self, line=0):
        if not self.file:
            return
        self.mode, self.interp, self.paused = "auto", "running", False
        self.line = max(0, int(line))
        self.run_elapsed = 0.0
        self.run_started = time.time()
        self._emit("state", "program")

    def pause_resume(self):
        if self.is_running:
            self.paused = not self.paused
            self._emit("state")

    def step(self):
        pass

    def abort(self):
        self._jogging.clear()
        self._targets.clear()
        if self.is_running:
            self.interp, self.mode, self.paused, self.run_started = "idle", "manual", False, None
            self._emit("state", "program")

    def spindle_start(self, direction, rpm):
        if self.spindle_locked():
            return
        if not self.on:
            return self.message.emit("warning", "Turn the machine on first")
        self.spindle_dir = 1 if direction >= 0 else -1
        self.spindle_requested = max(self.spindle_min, min(self.spindle_max, float(rpm)))
        self._emit("spindle")

    def spindle_stop(self):
        self.spindle_dir = 0
        self._emit("spindle")

    def set_feed_override(self, pct):
        self.feed_override = max(0.0, min(self.max_feed_override, float(pct)))
        self._emit("overrides")

    def set_rapid_override(self, pct):
        self.rapid_override = max(0.0, min(100.0, float(pct)))
        self._emit("overrides")

    def set_spindle_override(self, pct):
        self.spindle_override = max(self.min_spindle_override, min(self.max_spindle_override, float(pct)))
        self._emit("overrides")

    def set_velocity_limit(self, units_per_min):
        self.velocity_limit = max(1.0, min(self.max_velocity, float(units_per_min)))
        self._emit("overrides")

    def toggle_flood(self):
        self.flood = not self.flood
        self._emit("coolant")

    def toggle_mist(self):
        self.mist = not self.mist
        self._emit("coolant")


    def open_program(self, path):
        self.file = path
        try:
            with open(path, "r", errors="ignore") as f:
                self.total_lines = sum(1 for _ in f)
        except OSError:
            self.total_lines = 480
        self.line = 0
        self.run_elapsed = 0.0
        self.gcode_properties = {"run": "12:40", "x": "12.00 to 88.00 = 76.00 mm", "y": "12.00 to 88.00 = 76.00 mm",
                                 "z": "-5.00 to 5.00 = 10.00 mm", "toollist": "T8"}
        self._emit("program")

    def set_wcs(self, name):
        self.wcs = name
        self._update_rel()
        self.position_changed.emit()
        self._emit("offsets")

    def reload_tool_table(self):
        pass

    def stat(self):
        return SimStat(self) if linuxcnc else None
