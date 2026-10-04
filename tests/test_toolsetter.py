"""Tests for the tool setter routine (subprograms.py) and how the screen reads its result
(custom_action.py), against a simulated machine: MDI moves Z, and the setter trips where the tip meets it"""

import importlib
import io
import re
import sys
import types

import pytest

SENSOR_TOP = -200.0   # machine Z of the tip when the setter trips
# What milo_ui/probing.py sends: search, probe, max, retract, z_safe, z_offset (66.43 - -100), x, y, start z, tool
COMMAND = "toolsetter$200$100$50$10$-60$166.43$5.446$78.012$-130$2\n"


class Machine:
    def __init__(self, tool_length, z=-40.0, on_and_idle=True):
        self.length, self.z, self.on_and_idle = tool_length, z, on_and_idle
        self.tripped, self.probed, self.log, self.g91 = False, [0.0, 0.0, 0.0], [], False

    def mdi(self, line):
        self.log.append(line)
        for word in line.split():
            self.g91 = True if word == "G91" else False if word == "G90" else self.g91
        m = re.search(r"Z(-?[\d.]+)", line)
        if not m or line.startswith("G10"):
            return
        value = float(m.group(1))
        target = self.z + value if self.g91 and "G53" not in line else value
        trip_at = SENSOR_TOP + self.length   # the spindle nose's Z when the tip meets the setter
        if "G38" in line:
            self.tripped = target <= trip_at <= self.z
            if self.tripped:
                self.z, self.probed = trip_at, [0.0, 0.0, trip_at]
            elif "G38.2" in line:
                raise RuntimeError("G38.2 move finished without making contact")
            else:
                self.z = target
        else:
            self.z = target
            self.tripped = self.tripped and self.z <= trip_at


@pytest.fixture
def modules(monkeypatch):
    """subprograms and custom_action, imported against stand-ins for qtvcp"""
    core = types.ModuleType("qtvcp.core")
    core.Status = core.Action = core.Info = lambda: types.SimpleNamespace(
        get_error_safe_setting=lambda section, key, default=None: default)
    logger = types.ModuleType("qtvcp.logger")
    logger.getLogger = lambda name: types.SimpleNamespace(**{k: (lambda *a, **kw: None) for k in
                                                             ("debug", "info", "warning", "error", "critical")})
    qt_action = types.ModuleType("qtvcp.qt_action")
    qt_action._Lcnc_Action = object
    qtvcp = types.ModuleType("qtvcp")
    qtvcp.logger = logger
    for name, module in (("qtvcp", qtvcp), ("qtvcp.core", core), ("qtvcp.logger", logger),
                         ("qtvcp.qt_action", qt_action)):
        monkeypatch.setitem(sys.modules, name, module)
    for name in ("subprograms", "custom_action"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    yield importlib.import_module("subprograms"), importlib.import_module("custom_action")
    for name in ("subprograms", "custom_action"):
        sys.modules.pop(name, None)


def measure(modules, machine, monkeypatch):
    """Run the routine as the screen does; return what the screen concludes (completed, error text)"""
    subprograms, custom_action = modules

    class Stat:
        def poll(self):
            pass

        @property
        def actual_position(self):
            return (0.0, 0.0, machine.z)

        @property
        def probe_tripped(self):
            return machine.tripped

    def wait_complete(timeout):
        return 1

    def mdi(line):
        try:
            machine.mdi(line)
        except RuntimeError as e:
            errors.append((0, str(e)))

    errors = []
    monkeypatch.setattr(subprograms, "ACTION", types.SimpleNamespace(
        CALL_MDI=mdi, ABORT=lambda: machine.log.append("ABORT"), cmd=types.SimpleNamespace(wait_complete=wait_complete)))
    monkeypatch.setattr(subprograms, "STATUS", types.SimpleNamespace(
        stat=Stat(), is_on_and_idle=lambda: machine.on_and_idle, is_metric_mode=lambda: True,
        unblock_error_polling=lambda: None, block_error_polling=lambda: None,
        ERROR=types.SimpleNamespace(poll=lambda: errors.pop() if errors else None),
        get_probed_position=lambda: list(machine.probed)))
    monkeypatch.setattr(subprograms, "INFO", types.SimpleNamespace(MACHINE_IS_METRIC=True))
    monkeypatch.setattr(subprograms.time, "sleep", lambda t: None)

    routine = subprograms.SubPrograms.__new__(subprograms.SubPrograms)
    routine.timeout, routine.send_dict, routine.approach_vel = 30, {}, 600.0
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(COMMAND))
    monkeypatch.setattr(sys, "stdout", out)
    routine.process()
    monkeypatch.undo()   # sys.stdout back before anything prints

    screen = custom_action.CustomAction.__new__(custom_action.CustomAction)
    screen.SET_DISPLAY_MESSAGE = screen.SET_ERROR_MESSAGE = lambda *a: None
    screen.completed, screen.error_text, screen._toolsetter_return = False, "", (lambda *a: None)
    screen.parse_line(out.getvalue().encode())
    return screen.completed, screen.error_text


def tool_length_written(machine):
    (line,) = [line for line in machine.log if line.startswith("G10 L1")]
    return float(line.split("Z")[1])


def test_a_longer_tool_gets_a_larger_offset_by_its_extra_length(modules, monkeypatch):
    lengths = {}
    for length in (40.0, 55.0):
        machine = Machine(length)
        assert measure(modules, machine, monkeypatch) == (True, "")
        lengths[length] = tool_length_written(machine)
    assert lengths[55.0] - lengths[40.0] == pytest.approx(15.0)
    # the setter height less the work height comes off the machine Z where it touched
    assert lengths[40.0] == pytest.approx(SENSOR_TOP + 40.0 - 166.43)


def test_it_goes_up_before_across_and_ends_with_g43(modules, monkeypatch):
    machine = Machine(40.0, z=-10.0)
    measure(modules, machine, monkeypatch)
    log = machine.log
    up, across = log.index("G0 G53 Z0"), log.index("G0 G53 X5.446 Y78.012")
    assert up < across
    assert not any("Z" in line and "G0" in line for line in log[:up])   # nothing went down first
    assert log[across + 1].startswith("G91 G38.3 Z-130")                # down to the start as a probe move
    assert log[-3:] == ["G10 L1 P2 Z-326.4300", "M72", "G43"]
    assert machine.z == 0.0


def test_a_tool_too_long_for_the_start_height_stops_on_the_setter(modules, monkeypatch):
    machine = Machine(110.0)  # touches at nose Z -90, above the -130 start
    assert measure(modules, machine, monkeypatch) == (True, "")
    approach = next(i for i, line in enumerate(machine.log) if "G38.3" in line)
    assert machine.log[approach + 2] == "G91 G1 Z10.0 F200.0"   # backed off before searching
    assert tool_length_written(machine) == pytest.approx(SENSOR_TOP + 110.0 - 166.43)


def test_no_contact_is_a_failure_and_still_restores_g43(modules, monkeypatch):
    machine = Machine(-100.0)  # never reaches the setter
    completed, error = measure(modules, machine, monkeypatch)
    assert not completed and "1st Probe down failed" in error
    assert not any(line.startswith("G10") for line in machine.log)
    assert machine.log[-2:] == ["M72", "G43"]


def test_a_machine_not_on_and_idle_is_reported_as_a_failure(modules, monkeypatch):
    machine = Machine(40.0, on_and_idle=False)
    completed, error = measure(modules, machine, monkeypatch)
    assert not completed and "isn't on and idle" in error
    assert machine.log == []
