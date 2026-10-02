"""
A game controller as a jog pendant (the 8BitDo Ultimate 2 through the xpad driver, or any
Xbox-style pad).

GamepadReader reads the controller's evdev node on a thread. PendantLogic turns the controls
into jog commands; it knows nothing about Qt or hardware, so the safety rules are tested on
their own. Pendant ties them to the machine on a 50 ms tick.

Safety:
- Nothing moves unless the dead-man control is held past its threshold; letting go stops
  every axis at once.
- Stick jogs are sent as small increments, one tick of travel each. LinuxCNC adds each to the
  axis target, so the machine follows smoothly while ticks keep coming and stops by itself
  within about a tick if they don't (the screen freezes, the controller drops out).
- If the controller disappears, jogging stops; if it sends nothing at all for the hold timeout,
  stick jogging pauses until any control moves.
- Only when the machine is ready (on, homed, idle). Not an E-stop.
"""

import copy
import os
import re
import select
import struct
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from PyQt5 import QtCore

TICK = 0.05  # s between jog updates
CATCH_UP = 1.25  # jog increments run this much faster than the stick speed, so the target never runs away
MAX_DT = 0.1  # a late tick still moves at most this much time's worth

# Controls: id -> label. Buttons are on/off; triggers and sticks are analog.
BUTTONS = {"A": "A", "B": "B", "X": "X", "Y": "Y", "LB": "LB (left bumper)", "RB": "RB (right bumper)",
           "LS": "Left stick click", "RS": "Right stick click", "SELECT": "Select", "START": "Start",
           "HOME": "Home", "DPAD_UP": "D-pad up", "DPAD_DOWN": "D-pad down", "DPAD_LEFT": "D-pad left",
           "DPAD_RIGHT": "D-pad right", "LT": "LT (left trigger)", "RT": "RT (right trigger)"}
STICKS = {"LX": "Left stick left/right", "LY": "Left stick up/down", "RX": "Right stick left/right",
          "RY": "Right stick up/down"}
TRIGGERS = ("LT", "RT")
AXES = ("X", "Y", "Z")

# Actions that can be put on a button: id -> (label, needs the dead-man)
ACTIONS = {
    "step_x_minus": ("Step X −", True), "step_x_plus": ("Step X +", True),
    "step_y_minus": ("Step Y −", True), "step_y_plus": ("Step Y +", True),
    "step_z_minus": ("Step Z − (down)", True), "step_z_plus": ("Step Z + (up)", True),
    "step_smaller": ("Smaller step size", False), "step_larger": ("Larger step size", False),
    "stop": ("Stop all jogging", False), "talk": ("Talk to Milo (push to talk)", False),
    "confirm": ("Confirm Milo's action", True),
    "radial": ("Open the quick menu", False),
    "radial2": ("Open the zero & go-to menu", False),
}

# The quick (radial) menu: id -> (label, icon, needs the dead-man held to run). Starting things
# needs the dead-man; stopping things and opening pages don't.
RADIAL_ACTIONS = {
    "home_all": ("Home all", "house-line", True),
    "spindle": ("Spindle start / stop", "arrow-clockwise", True),  # shown as the one that applies (radial_entry)
    "mist": ("Mist on / off", "drop", True),
    "probe": ("Probe", "target", False),
    "z_top": ("Raise Z to top", "arrow-line-up", True),
    "talk": ("Talk to Milo", "microphone", False),
    "set_spindle": ("Set spindle speed", "gauge", False),
    "set_feed": ("Set feed speed", "lightning", False),
    "set_jog": ("Jog speed", "arrows-out-cardinal", False),
    "power": ("Power on / off", "power", True),
}
# The second menu (right stick click): zeroing and going to fixed places. Not configurable.
# Moving and zeroing need the dead-man; opening the Zero submenu doesn't.
MOVE_ACTIONS = {
    "zero": ("Zero", "crosshair", False),
    "go_work_zero": ("Go to Work Zero", "target", True),
    "go_abs_home": ("Go to ABS Home", "house-line", True),
    "go_g54": ("Go to G54", "map-pin", True),
}
MOVE_ITEMS = list(MOVE_ACTIONS)
SUBMENUS = {"zero"}  # items that open another ring instead of running


def zero_items(axes) -> List[str]:
    """The Zero submenu: each axis, then all of them"""
    return [f"zero_{a.lower()}" for a in axes] + ["zero_all"]


# Items that switch something on or off: they show the one that would happen
TOGGLE_ITEMS = ("spindle", "mist", "power")
# Earlier versions had a slot for each half of a toggle
OLD_RADIAL_ITEMS = {"spindle_on": "spindle", "spindle_off": "spindle", "mist_on": "mist", "mist_off": "mist"}


def radial_entry(item: str, machine) -> tuple:
    """(label, icon, needs the dead-man) of a quick-menu item as it applies right now. Toggles show
    the one that would happen: switching on needs the dead-man, switching off never does."""
    if item == "spindle":
        return ("Stop spindle", "stop-circle", False) if machine.spindle_dir else (
            "Start spindle", "arrow-clockwise", True)
    if item == "mist":
        return ("Mist off", "drop-half", False) if machine.mist else ("Mist on", "drop", True)
    if item == "power":
        return ("Power off", "power", False) if machine.on else ("Power on", "power", True)
    if item == "zero_all":
        return ("Zero all", "crosshair", True)
    if item.startswith("zero_"):
        return (f"Zero {item[5:].upper()}", "crosshair-simple", True)
    if item in MOVE_ACTIONS:
        return MOVE_ACTIONS[item]
    return RADIAL_ACTIONS[item]


def _renamed(items) -> List[str]:
    """Quick-menu ids with the old split toggles merged, in order, without repeats"""
    result = []
    for item in items:
        item = OLD_RADIAL_ITEMS.get(item, item)
        if item not in result:
            result.append(item)
    return result


FIRST_RADIAL_ITEMS = ["home_all", "spindle_on", "spindle_off", "mist_on", "mist_off", "probe", "z_top", "talk"]
ADJUST_TIMEOUT = 30.0  # s without input before the slider closes itself (nothing changed)
MENU_SELECT, MENU_CANCEL = "A", "B"  # in the menu; the menu button itself also selects
MENU_TIMEOUT = 20.0  # s without input before an open menu closes itself
# One button can do both: with the dead-man held it confirms, otherwise it talks
SHAREABLE = {"talk", "confirm"}
CURVES = {"Linear": 1.0, "Gentle": 1.6, "Fine": 2.5}  # stick response exponent

DEFAULTS = {
    "enabled": False,
    "deadman": "RT",
    "deadman_threshold": 0.5,  # how far the dead-man trigger must be pressed (0..1)
    "fine": "LT",  # squeezing it slows everything down, "" for none
    "fine_min": 0.1,  # speed factor with the fine control fully pressed
    "max_xy": 1000.0,  # units/min at full stick
    "max_z": 400.0,
    "deadzone": 0.15,
    "curve": "Gentle",
    "hold_timeout": 1.5,  # s without any input before stick jogging pauses
    "steps": [0.01, 0.1, 1.0, 5.0],
    "step_index": 1,
    "sticks": {"X": "LX", "Y": "LY", "Z": "RY"},
    "invert": {"X": False, "Y": False, "Z": False},
    "buttons": {"step_x_minus": "DPAD_LEFT", "step_x_plus": "DPAD_RIGHT", "step_y_minus": "DPAD_DOWN",
                "step_y_plus": "DPAD_UP", "step_z_minus": "A", "step_z_plus": "Y",
                "step_smaller": "LB", "step_larger": "RB", "stop": "B", "talk": "X", "confirm": "X",
                "radial": "LS", "radial2": "RS"},
    "radial_items": ["home_all", "spindle", "mist", "probe", "set_spindle", "set_feed", "set_jog", "power"],
    "radial_rpm": 0.0,  # "Start spindle" speed; 0 = the machine's default speed
    "radial_known": [],  # quick-menu items the user has had the chance to see (see merged_config)
}


def merged_config(saved: Optional[dict]) -> dict:
    """Saved settings over the defaults (new settings get their default)"""
    config = copy.deepcopy(DEFAULTS)
    for key, value in (saved or {}).items():
        if isinstance(config.get(key), dict) and isinstance(value, dict):
            config[key].update(value)
        elif key in config:
            config[key] = value
    # Quick-menu items added in a newer version appear once (if they're on by default); ones the
    # user turned off stay off
    known = (saved or {}).get("radial_known") or (FIRST_RADIAL_ITEMS if saved and "radial_items" in saved else [])
    # A merged toggle takes the place of either half the user had on
    known = _renamed(known)
    config["radial_items"] = _renamed(config["radial_items"]) + [
        item for item in DEFAULTS["radial_items"] if item not in known and item not in config["radial_items"]]
    config["radial_known"] = list(RADIAL_ACTIONS)
    return config


def conflicts(config: dict) -> List[str]:
    """Problems with a configuration, in plain words"""
    problems = []
    used: Dict[str, List[str]] = {}
    for action, control in config["buttons"].items():
        if control:
            used.setdefault(control, []).append(ACTIONS[action][0])
    for control, actions in used.items():
        shared = {a for a, c in config["buttons"].items() if c == control}
        if len(actions) > 1 and shared != SHAREABLE:
            problems.append(f"{BUTTONS.get(control, control)} does several things: {', '.join(actions)}")
    deadman = config["deadman"]
    if deadman in used:
        problems.append(f"The dead-man control ({BUTTONS.get(deadman, deadman)}) also does: {', '.join(used[deadman])}")
    if config["fine"] and config["fine"] == deadman:
        problems.append("The dead-man and fine-mode controls are the same")
    sticks = [s for s in config["sticks"].values() if s]
    if len(set(sticks)) < len(sticks):
        problems.append("Two machine axes are on the same stick direction")
    return problems


@dataclass
class InputState:
    """The controller right now. Sticks -1..1 (up and right positive), triggers 0..1."""
    connected: bool = False
    buttons: Set[str] = field(default_factory=set)
    sticks: Dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in STICKS})
    triggers: Dict[str, float] = field(default_factory=lambda: {k: 0.0 for k in TRIGGERS})
    last_event: float = 0.0  # time.monotonic() of the last input from the controller
    events: int = 0  # running count, for an events-per-second readout

    def pressed(self, control: str, threshold: float = 0.5) -> bool:
        """Buttons, and triggers pressed past threshold"""
        if control in TRIGGERS:
            return self.triggers.get(control, 0.0) >= threshold
        return control in self.buttons

    def copy(self) -> "InputState":
        return InputState(self.connected, set(self.buttons), dict(self.sticks), dict(self.triggers),
                          self.last_event, self.events)


@dataclass
class Output:
    status: str  # off, disconnected, not_ready, released, paused, armed, jogging
    velocities: Dict[str, float] = field(default_factory=dict)  # axis -> units/min (signed)
    steps: List[Tuple[str, int]] = field(default_factory=list)  # (axis, +1/-1) at the current step size
    actions: List[str] = field(default_factory=list)  # stop, talk, step_smaller, step_larger, radial, menu_*
    deadman: bool = False
    menu_stick: Tuple[float, float] = (0.0, 0.0)  # the more deflected stick, while the menu is open
    new_buttons: Set[str] = field(default_factory=set)  # pressed since the last update (menu mode)


class PendantLogic:
    """Controls in, jog commands out. No hardware, no Qt."""

    def __init__(self, config: dict):
        self.config = config
        self._previous: Set[str] = set()

    def stick_speed(self, value: float, axis: str, fine: float) -> float:
        """Signed units/min for a stick value, after the dead zone, curve and caps"""
        c = self.config
        dead = min(max(float(c["deadzone"]), 0.0), 0.9)
        magnitude = abs(value)
        if magnitude <= dead:
            return 0.0
        scaled = min(1.0, (magnitude - dead) / (1.0 - dead)) ** CURVES.get(c["curve"], 1.6)
        top = float(c["max_z"] if axis == "Z" else c["max_xy"])
        speed = scaled * top * fine
        if c["invert"].get(axis):
            value = -value
        return speed if value > 0 else -speed

    def update(self, state: InputState, now: float, machine_ready: bool, menu_open: bool = False) -> Output:
        c = self.config
        if not c["enabled"]:
            self._previous = set()
            return Output("off")
        if not state.connected:
            self._previous = set()
            return Output("disconnected")
        # Edges: controls newly pressed since the last update
        pressed = {control for control in BUTTONS if state.pressed(control)}
        new = pressed - self._previous
        self._previous = pressed
        output = Output("armed")
        deadman = state.pressed(c["deadman"], float(c["deadman_threshold"]))
        output.deadman = deadman
        if menu_open:
            # The sticks choose a slice; nothing jogs
            output.status = "menu"
            output.new_buttons = new
            menu_buttons = {c["buttons"].get("radial"), c["buttons"].get("radial2")} - {None, ""}
            if MENU_SELECT in new or menu_buttons & new:
                output.actions.append("menu_select")
            if MENU_CANCEL in new:
                output.actions.append("menu_cancel")
            sticks = [(state.sticks["LX"], state.sticks["LY"]), (state.sticks["RX"], state.sticks["RY"])]
            output.menu_stick = max(sticks, key=lambda xy: xy[0] ** 2 + xy[1] ** 2)
            return output
        for action, control in c["buttons"].items():
            if not control or control not in new:
                continue
            if action == "confirm":
                # A deliberate two-handed yes; works whether or not the machine is ready to jog
                # (Milo re-checks every action before running it)
                if deadman:
                    output.actions.append("confirm")
            elif action == "talk":
                if not (deadman and c["buttons"].get("confirm") == control):
                    output.actions.append("talk")
            elif action in ("stop", "step_smaller", "step_larger", "radial", "radial2"):
                output.actions.append(action)
            elif deadman and machine_ready:
                _, axis, sign = action.split("_")
                output.steps.append((axis.upper(), 1 if sign == "plus" else -1))
        if not machine_ready:
            output.status = "not_ready"
            output.steps = []
            return output
        if not deadman:
            output.status = "released"
            output.steps = []
            return output
        if "stop" in output.actions:
            return output
        fine = 1.0
        if c["fine"]:
            amount = state.triggers[c["fine"]] if c["fine"] in TRIGGERS else (1.0 if c["fine"] in state.buttons else 0.0)
            fine = 1.0 - amount * (1.0 - float(c["fine_min"]))
        for axis in AXES:
            stick = c["sticks"].get(axis)
            if stick:
                speed = self.stick_speed(state.sticks.get(stick, 0.0), axis, fine)
                if speed:
                    output.velocities[axis] = speed
        if output.velocities and now - state.last_event > float(c["hold_timeout"]):
            output.velocities = {}
            output.status = "paused"
        elif output.velocities or output.steps:
            output.status = "jogging"
        return output


# --- reading the controller ------------------------------------------------------------

EVENT = struct.Struct("llHHi")
EV_KEY, EV_ABS = 1, 3
KEY_CODES = {304: "A", 305: "B", 307: "X", 308: "Y", 310: "LB", 311: "RB", 314: "SELECT", 315: "START",
             316: "HOME", 317: "LS", 318: "RS",
             # Some pads report the d-pad as buttons
             544: "DPAD_UP", 545: "DPAD_DOWN", 546: "DPAD_LEFT", 547: "DPAD_RIGHT"}


def find_gamepad(devices_text: str) -> Optional[Tuple[str, str]]:
    """(event node, name) of the first Xbox-style pad in /proc/bus/input/devices"""
    for block in devices_text.split("\n\n"):
        name = re.search(r'N: Name="([^"]*)"', block)
        handlers = re.search(r"H: Handlers=.*", block)
        if not name or not handlers or "js" not in handlers.group(0):
            continue
        if re.search(r"x-?box|gamepad|8bitdo", name.group(1), re.I) and "Keyboard" not in name.group(1):
            event = re.search(r"\bevent\d+", handlers.group(0))
            if event:
                return "/dev/input/" + event.group(0), name.group(1)
    return None


def apply_event(state: InputState, etype: int, code: int, value: int):
    """Update state from one evdev event (Xbox 360 layout: sticks ±32768, triggers 0..255)"""
    if etype == EV_KEY and code in KEY_CODES:
        name = KEY_CODES[code]
        if value:
            state.buttons.add(name)
        else:
            state.buttons.discard(name)
    elif etype == EV_ABS:
        if code in (0, 1, 3, 4):
            stick = {0: "LX", 1: "LY", 3: "RX", 4: "RY"}[code]
            normalized = max(-1.0, min(1.0, value / 32767.0))
            # Up is negative on the device; make it positive
            state.sticks[stick] = -normalized if stick in ("LY", "RY") else normalized
        elif code in (2, 5):
            state.triggers["LT" if code == 2 else "RT"] = max(0.0, min(1.0, value / 255.0))
        elif code in (16, 17):
            minus, plus = ("DPAD_LEFT", "DPAD_RIGHT") if code == 16 else ("DPAD_UP", "DPAD_DOWN")
            state.buttons.discard(minus)
            state.buttons.discard(plus)
            if value < 0:
                state.buttons.add(minus)
            elif value > 0:
                state.buttons.add(plus)


class GamepadReader(QtCore.QThread):
    """Reads the controller; reconnects when it comes back. Emits a copy of the state on change."""

    changed = QtCore.pyqtSignal(object)  # InputState

    def __init__(self, devices_path="/proc/bus/input/devices", parent=None):
        super().__init__(parent)
        self.devices_path = devices_path
        self._running = True
        self.name = ""

    def stop(self):
        self._running = False
        self.wait(2000)

    def run(self):
        while self._running:
            try:
                with open(self.devices_path) as f:
                    found = find_gamepad(f.read())
            except OSError:
                found = None
            if found is None:
                self.changed.emit(InputState(connected=False))
                self._sleep(1.0)
                continue
            path, self.name = found
            self._read(path)
            self.changed.emit(InputState(connected=False))

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(0.1)

    def _read(self, path):
        state = InputState(connected=True, last_event=time.monotonic())
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            self._sleep(1.0)
            return
        self.changed.emit(state.copy())
        try:
            while self._running:
                ready, _, _ = select.select([fd], [], [], 0.2)
                if not ready:
                    continue
                data = os.read(fd, EVENT.size * 64)
                if not data:
                    return
                for offset in range(0, len(data) - EVENT.size + 1, EVENT.size):
                    _, _, etype, code, value = EVENT.unpack_from(data, offset)
                    if etype in (EV_KEY, EV_ABS):
                        apply_event(state, etype, code, value)
                        state.events += 1
                state.last_event = time.monotonic()
                self.changed.emit(state.copy())
        except OSError:
            return  # unplugged or the controller went to sleep
        finally:
            os.close(fd)


# --- tying it to the machine -------------------------------------------------------------

class Pendant(QtCore.QObject):
    """Runs the logic on a timer and sends the jogs. Config lives in the screen prefs ("pendant")."""

    status_changed = QtCore.pyqtSignal(str)
    input_changed = QtCore.pyqtSignal(object)  # InputState, for the live view
    config_changed = QtCore.pyqtSignal()
    talk = QtCore.pyqtSignal()
    confirm = QtCore.pyqtSignal()
    menu_changed = QtCore.pyqtSignal(bool)  # the quick menu opened / closed
    menu_hover = QtCore.pyqtSignal(int)  # highlighted slice, -1 for none
    menu_hint = QtCore.pyqtSignal(str)  # e.g. "Hold RT to run Home all"
    quick_action = QtCore.pyqtSignal(str)  # a RADIAL_ACTIONS id to run
    adjust_changed = QtCore.pyqtSignal(float)  # the slider's value
    adjust_done = QtCore.pyqtSignal(bool, float)  # (apply?, value) when the slider closes

    def __init__(self, machine, prefs, reader_factory=GamepadReader, parent=None):
        super().__init__(parent)
        self.machine = machine
        self.prefs = prefs
        self.config = merged_config(prefs.get("pendant"))
        self.logic = PendantLogic(self.config)
        self.state = InputState()
        self.status = "off"
        self._reader_factory = reader_factory
        self.reader = None
        self._moving = False
        self.menu_open = False
        self.menu_index = -1
        self._menu: List[str] = []
        self.menu_title = "Quick menu"
        self._menu_parents: List[Tuple[List[str], str]] = []  # rings to go back to with B
        self.adjust = None  # the open slider: {"value", "min", "max", "step", ...} (see open_adjuster)
        self._adjust_dir = 0
        self._adjust_since = self._adjust_next = 0.0
        self._menu_activity = 0.0
        self._last_tick = time.monotonic()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._apply_enabled()

    # --- config ---

    def set(self, key, value):
        """Change one setting (dicts are merged) and save"""
        if isinstance(self.config.get(key), dict) and isinstance(value, dict):
            self.config[key].update(value)
        else:
            self.config[key] = value
        self.prefs.set("pendant", self.config)
        if key == "enabled":
            self._apply_enabled()
        self.config_changed.emit()

    def reset(self):
        enabled = self.config["enabled"]
        self.config.clear()
        self.config.update(merged_config({"enabled": enabled}))
        self.prefs.set("pendant", self.config)
        self.config_changed.emit()

    @property
    def step_size(self) -> float:
        steps = self.config["steps"]
        return float(steps[max(0, min(len(steps) - 1, int(self.config["step_index"])))])

    # --- running ---

    def _apply_enabled(self):
        if self.config["enabled"]:
            if self.reader is None:
                self.reader = self._reader_factory()
                self.reader.changed.connect(self._on_input)
                self.reader.start()
            self._last_tick = time.monotonic()
            self._timer.start(int(TICK * 1000))
            self.tick()  # show the status straight away
        else:
            self._timer.stop()
            self._stop_motion()
            if self.reader is not None:
                self.reader.stop()
                self.reader = None
            self.state = InputState()
            self._set_status("off")

    def shutdown(self):
        self._timer.stop()
        self._stop_motion()
        if self.reader is not None:
            self.reader.stop()
            self.reader = None

    def _on_input(self, state):
        self.state = state
        self.input_changed.emit(state)
        if not state.connected:
            self._stop_motion()

    def tick(self):
        now = time.monotonic()
        dt = min(MAX_DT, max(0.0, now - self._last_tick))
        self._last_tick = now
        m = self.machine
        ready = bool(m.ready) and not m.is_running
        output = self.logic.update(self.state, now, ready, self.menu_open or self.adjust is not None)
        if self.adjust is not None:
            self._adjust_tick(output, now)
            self._set_status("menu")
            return
        if self.menu_open:
            self._menu_tick(output, now)
            self._set_status("menu" if self.menu_open else output.status)
            return
        for action in output.actions:
            if action == "stop":
                self._stop_motion()
            elif action == "talk":
                self.talk.emit()
            elif action == "confirm":
                self.confirm.emit()
            elif action == "radial":
                self.open_menu()
            elif action == "radial2":
                self.open_menu(MOVE_ITEMS, "Zero & go to")
            elif action in ("step_smaller", "step_larger"):
                index = int(self.config["step_index"]) + (1 if action == "step_larger" else -1)
                self.set("step_index", max(0, min(len(self.config["steps"]) - 1, index)))
        if output.velocities:
            for axis, velocity in output.velocities.items():
                if axis in m.axes:
                    m.pendant_jog(axis, velocity, dt)
            self._moving = True
        elif self._moving:
            self._stop_motion()
        for axis, direction in output.steps:
            if axis in m.axes:
                m.pendant_step(axis, direction, self.step_size)
        self._set_status(output.status)

    # --- the quick menu ---

    @property
    def quick_items(self) -> List[str]:
        """The quick menu's items, as set up in the pendant settings"""
        return [item for item in self.config["radial_items"] if item in RADIAL_ACTIONS]

    @property
    def menu_items(self) -> List[str]:
        """The open ring's items (the quick menu's when none is open)"""
        return list(self._menu) if self.menu_open else self.quick_items

    @property
    def in_submenu(self) -> bool:
        return bool(self._menu_parents)

    def open_menu(self, items=None, title="Quick menu"):
        """Show a ring: the quick menu by default, or `items` (ids radial_entry knows)"""
        items = self.quick_items if items is None else list(items)
        if not items:
            return
        self._stop_motion()
        if not self.menu_open:
            self._menu_parents = []
        self._menu, self.menu_title = items, title
        self.menu_open = True
        self.menu_index = -1
        self._menu_activity = time.monotonic()
        self.menu_changed.emit(True)
        self.menu_hover.emit(-1)

    def close_menu(self):
        self._menu_parents = []
        if self.menu_open:
            self.menu_open = False
            self.menu_changed.emit(False)

    def choose(self, item):
        """Run a ring's item, or open its submenu (Zero -> each axis)"""
        if item in SUBMENUS:
            self._menu_parents.append((self._menu, self.menu_title))
            return self.open_menu(zero_items(self.machine.axes), "Zero which axis?")
        self.close_menu()
        self.quick_action.emit(item)

    def back(self):
        """B: up to the parent ring, or close the top one"""
        if not self._menu_parents:
            return self.close_menu()
        items, title = self._menu_parents.pop()
        parents = self._menu_parents
        self.open_menu(items, title)
        self._menu_parents = parents

    def _menu_tick(self, output, now):
        x, y = output.menu_stick
        if x * x + y * y >= 0.25:  # tilted at least halfway
            index = slice_at(x, y, len(self.menu_items))
            if index != self.menu_index:
                self.menu_index = index
                self.menu_hover.emit(index)
            self._menu_activity = now
        if output.actions:
            self._menu_activity = now
        if "menu_cancel" in output.actions:
            return self.back()
        if "menu_select" in output.actions and 0 <= self.menu_index < len(self.menu_items):
            item = self.menu_items[self.menu_index]
            label, _, needs_deadman = radial_entry(item, self.machine)
            if needs_deadman and not output.deadman:
                self.menu_hint.emit(f"Hold {self.config['deadman']} to run {label}")
                return
            return self.choose(item)
        if now - self._menu_activity > MENU_TIMEOUT:
            self.close_menu()

    # --- the slider (set spindle speed / feed) ---

    def open_adjuster(self, spec: dict):
        """Show a value to adjust with the sticks or D-pad. spec: value, min, max, step (and anything
        the caller wants back). A applies, B cancels."""
        self._stop_motion()
        self.adjust = dict(spec)
        self.adjust["value"] = self._snap(float(spec["value"]))
        self._adjust_dir = 0
        self._menu_activity = time.monotonic()
        self.adjust_changed.emit(self.adjust["value"])

    def close_adjuster(self, apply: bool):
        if self.adjust is not None:
            value = self.adjust["value"]
            self.adjust = None
            self.adjust_done.emit(apply, value)

    def set_adjust_value(self, value):
        """From the touch slider"""
        if self.adjust is not None:
            self.adjust["value"] = self._snap(value)
            self.adjust_changed.emit(self.adjust["value"])

    def _snap(self, value):
        a = self.adjust
        step = float(a["step"])
        value = round(value / step) * step  # round numbers: 1,800 rpm, not 1,700 / 1,900 counted from the minimum
        return max(float(a["min"]), min(float(a["max"]), round(value, 6)))

    def _adjust_tick(self, output, now):
        a = self.adjust
        if "menu_cancel" in output.actions:
            return self.close_adjuster(False)
        if "menu_select" in output.actions:
            return self.close_adjuster(True)
        new = output.new_buttons
        steps = (("DPAD_RIGHT" in new) + ("DPAD_UP" in new)) - (("DPAD_LEFT" in new) + ("DPAD_DOWN" in new))
        x, y = output.menu_stick
        tilt = x if abs(x) >= abs(y) else y  # right or up is more
        if abs(tilt) >= 0.4:
            direction = 1 if tilt > 0 else -1
            if direction != self._adjust_dir:
                self._adjust_dir, self._adjust_since, self._adjust_next = direction, now, now
            if now >= self._adjust_next:
                held = now - self._adjust_since
                # One step, then repeating faster the longer it's held; fully over for a while: 5 at a time
                self._adjust_next = now + (0.3 if held < 1.0 else 0.12 if held < 2.5 else 0.06)
                steps += direction * (5 if abs(tilt) > 0.95 and held > 1.0 else 1)
        else:
            self._adjust_dir = 0
        if steps or output.actions or abs(tilt) >= 0.4:
            self._menu_activity = now
        if steps:
            value = self._snap(a["value"] + steps * float(a["step"]))
            if value != a["value"]:
                a["value"] = value
                self.adjust_changed.emit(value)
        elif now - self._menu_activity > ADJUST_TIMEOUT:
            self.close_adjuster(False)

    def _stop_motion(self):
        if self._moving:
            self.machine.pendant_stop()
        self._moving = False

    def _set_status(self, status):
        if status != self.status:
            self.status = status
            self.status_changed.emit(status)


STATUS_TEXT = {
    "off": "Pendant off",
    "disconnected": "Pendant not connected",
    "not_ready": "Pendant · machine not ready",
    "released": "Pendant · hold {deadman}",
    "paused": "Pendant · paused (no input)",
    "armed": "Pendant ready",
    "jogging": "Pendant · jogging",
    "menu": "Pendant · quick menu",
}


def slice_at(x: float, y: float, count: int) -> int:
    """The menu slice a stick points at: 0 at the top, then clockwise"""
    import math
    angle = math.atan2(x, y) % (2 * math.pi)  # 0 = up, clockwise
    return int(round(angle / (2 * math.pi / count))) % count


def status_text(status: str, config: dict) -> str:
    deadman = config.get("deadman", "RT")
    return STATUS_TEXT.get(status, status).format(deadman=deadman)
