"""Tests for the power drawbar: the config, the subroutines in LinuxCNC's interpreter, the safety
checks, the machine model and the Tools page"""

import os
import re
import shutil
import subprocess

import pytest

import machine_safety
from conftest import FakeStat
from test_milo_ui import shell  # noqa: F401  (fixture)

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INI = os.path.join(CONFIG_DIR, "Mesa7I96S.ini")


def ini_text():
    with open(INI) as f:
        return f.read()


def section(name):
    text = ini_text()
    match = re.search(rf"^\[{re.escape(name)}\]\n(.*?)(?=^\[|\Z)", text, re.S | re.M)
    values = {}
    for line in (match.group(1) if match else "").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


# --- the configuration ------------------------------------------------------------------------------

def test_a_is_a_coordinate_so_gcode_can_turn_it():
    assert section("KINS")["KINEMATICS"] == "trivkins coordinates=XYZA"
    assert section("TRAJ")["COORDINATES"] == "XYZA"
    joint = section("JOINT_3")
    assert joint["TYPE"] == "ANGULAR" and float(joint["MAX_OUTPUT"]) == 0  # no 30 deg/s cap on the PID
    assert float(joint["STEPGEN_MAXVEL"]) >= float(joint["MAX_VELOCITY"]) * 1.2
    assert float(joint["STEP_SCALE"]) == pytest.approx(1600 / 360, abs=1e-4)  # CL86Y at 1600 pulses/turn
    assert section("DISPLAY")["GEOMETRY"] == "xyz"  # the preview mustn't turn with the drawbar
    assert section("RS274NGC")["REMAP"] == "M6 modalgroup=6 ngc=milo_m6"
    drawbar = section("DRAWBAR")
    for key in ("AXIS", "LOWER_IS_ON", "TIGHTEN_DIRECTION", "RELEASE_TURNS", "CLAMP_TURNS", "TURN_SPEED",
                "ENGAGE_TURNS", "ENGAGE_SPEED", "LOWER_TIME", "RAISE_TIME", "SPINDLE_STOP_TIME"):
        assert key in drawbar, key


def test_the_lift_valve_is_driven_by_gcode():
    with open(os.path.join(CONFIG_DIR, "Mesa7I96S.hal")) as f:
        hal = f.read()
    assert re.search(r"^net drawbar-lift motion\.digital-out-00 => hm2_7i96s\.0\.7i84\.1\.0\.output-05$", hal, re.M)
    with open(os.path.join(CONFIG_DIR, "custom_postgui.hal")) as f:
        postgui = f.read()
    # One writer per pin: the screen no longer drives the valve
    assert not re.search(r"^net .*output-05", postgui, re.M)


def test_the_closed_loop_driver_alarm_faults_the_machine():
    with open(os.path.join(CONFIG_DIR, "Mesa7I96S.hal")) as f:
        hal = f.read()
    # ALM is joint 3's amp fault, and ENA follows joint 3's enable (on = driver disabled)
    assert re.search(r"^net a-fault +<= hm2_7i96s\.0\.7i84\.1\.0\.input-05(-not)?$", hal, re.M)
    assert re.search(r"^net a-fault +=> joint\.3\.amp-fault-in$", hal, re.M)
    assert re.search(r"^net a-enable +=> hm2_7i96s\.0\.7i84\.1\.0\.output-07$", hal, re.M)
    # Nothing else uses those pins
    assert len(re.findall(r"^net .*7i84\.1\.0\.input-05\b", hal, re.M)) == 1
    assert len(re.findall(r"^net .*7i84\.1\.0\.output-07\b", hal, re.M)) == 1
    drawbar = section("DRAWBAR")
    # The clamp goes only a little past where the release started, under the driver's error limit
    overshoot = float(drawbar["CLAMP_TURNS"]) - float(drawbar["RELEASE_TURNS"])
    assert 0 < overshoot <= 0.5


# --- the subroutines, in LinuxCNC's own interpreter ---------------------------------------------------

def run_gcode(tmp_path, program, ini=INI):
    if shutil.which("rs274") is None:
        pytest.skip("rs274 (LinuxCNC's standalone interpreter) isn't installed")
    ngc = tmp_path / "test.ngc"
    ngc.write_text(program + "\nM2\n")
    tbl = tmp_path / "tool.tbl"
    tbl.write_text("T1 P1 D6 Z10 ;tool\nT2 P2 D3 Z20 ;tool\n")
    out = subprocess.run(["rs274", "-i", str(ini), "-t", str(tbl), "-g", str(ngc)], capture_output=True, text=True,
                         timeout=30, cwd=CONFIG_DIR)
    text = out.stdout + out.stderr
    assert "PROGRAM_END" in text, text[-2000:]
    calls = []
    for line in text.splitlines():
        m = re.search(r"N\.\.\.\.\.\s+(\w+)\((.*)\)", line)
        if not m:
            continue
        name, args = m.group(1), m.group(2)
        if name in ("STRAIGHT_FEED", "STRAIGHT_TRAVERSE"):
            calls.append((name, round(float(args.split(",")[3]), 3)))  # A
        elif name in ("SET_AUX_OUTPUT_BIT", "CLEAR_AUX_OUTPUT_BIT", "DWELL", "CHANGE_TOOL", "START_SPINDLE_CLOCKWISE",
                      "STOP_SPINDLE_TURNING", "MESSAGE"):
            calls.append((name, args.strip().strip('"').strip()))
    return calls


def a_moves(calls):
    """The A positions the motor is fed to, in order"""
    return [a for name, a in calls if name == "STRAIGHT_FEED"]


def outputs(calls):
    return [name for name, _ in calls if name.endswith("AUX_OUTPUT_BIT")]


def test_release_and_clamp_sequences(tmp_path):
    calls = run_gcode(tmp_path, "G21 G90 F500\nM3 S1000\no<drawbar_release> call\n(debug, released)\n"
                                "o<drawbar_clamp> call\n(debug, clamped)")
    released = calls[:[c[1] for c in calls].index("released")]
    names = [n for n, _ in released]
    # Spindle stopped (and given time to stop) before the motor goes down
    assert names.index("STOP_SPINDLE_TURNING") < names.index("SET_AUX_OUTPUT_BIT")
    assert ("DWELL", "3.0000") in released
    # Lower, ease on (0.3 turn), undo 3 turns, raise; loosening is negative with TIGHTEN_DIRECTION = 1
    assert outputs(released) == ["SET_AUX_OUTPUT_BIT", "CLEAR_AUX_OUTPUT_BIT"]
    assert a_moves(released) == [-108.0, -1188.0]
    # The spindle is NOT switched back on afterwards (it was on before: M73 would have restored it)
    assert "START_SPINDLE_CLOCKWISE" not in [n for n, _ in calls[len(released):]]
    clamped = calls[len(released):]
    assert a_moves(clamped) == [-1080.0, 90.0]  # ease on 0.3 turn, then 3.25 turns tighter
    assert outputs(clamped) == ["SET_AUX_OUTPUT_BIT", "CLEAR_AUX_OUTPUT_BIT"]


def test_turning_leaves_g90_and_the_feed_as_they_were(tmp_path):
    calls = run_gcode(tmp_path, "G21 G90 F500\no<drawbar_turn> call [1] [1]\nG1 X5")
    assert ("STRAIGHT_FEED", 360.0) in calls
    # G1 X5 is absolute again: X goes to 5, A stays put
    text = subprocess.run(["rs274", "-i", INI, "-g", str(tmp_path / "test.ngc")], capture_output=True, text=True,
                          cwd=CONFIG_DIR).stdout
    assert "STRAIGHT_FEED(5.0000, 0.0000, 0.0000, 360.0000" in text and "SET_FEED_RATE(500.0000)" in text


def test_m6_runs_the_drawbar(tmp_path):
    calls = run_gcode(tmp_path, "G21\nT1 M6\n(debug, again)\nT1 M6\n(debug, unload)\nT0 M6")
    split = [c[1] for c in calls].index("again")
    change, rest = calls[:split], calls[split:]
    names = [n for n, _ in change]
    # Release, LinuxCNC's own tool change (the prompt), then clamp
    assert names.count("SET_AUX_OUTPUT_BIT") == 2
    assert names.index("CHANGE_TOOL") > names.index("CLEAR_AUX_OUTPUT_BIT")
    assert a_moves(change)[:2] == [-108.0, -1188.0] and a_moves(change)[-1] == 90.0
    unload = rest[[c[1] for c in rest].index("unload"):]
    again = rest[:[c[1] for c in rest].index("unload")]
    assert not a_moves(again) and "CHANGE_TOOL" not in [n for n, _ in again]  # same tool: nothing
    assert ("CHANGE_TOOL", "0") in unload and len(a_moves(unload)) == 2  # released, not clamped again


def test_direction_and_valve_polarity_come_from_the_ini(tmp_path):
    text = ini_text().replace("TIGHTEN_DIRECTION = 1", "TIGHTEN_DIRECTION = -1").replace("LOWER_IS_ON = 1",
                                                                                         "LOWER_IS_ON = 0")
    ini = tmp_path / "flipped.ini"
    ini.write_text(text)
    calls = run_gcode(tmp_path, "G21 G90\no<drawbar_release> call", ini=ini)
    assert a_moves(calls) == [108.0, 1188.0]  # loosening is now positive
    assert outputs(calls) == ["CLEAR_AUX_OUTPUT_BIT", "SET_AUX_OUTPUT_BIT"]  # output off lowers


# --- Milo's checks --------------------------------------------------------------------------------------

@pytest.fixture
def drawbar_stat(tmp_path):
    ini = tmp_path / "machine.ini"
    ini.write_text("[DRAWBAR]\nAXIS = A\nLOWER_IS_ON = 1\n")
    machine_safety._ini_cache.clear()
    stat = FakeStat(axis_mask=0b1111, ini_filename=str(ini), dout=[0, 0, 0, 0], tool_in_spindle=3)
    stat.axis.append({"min_position_limit": -1e9, "max_position_limit": 1e9})
    yield stat
    machine_safety._ini_cache.clear()


def test_milo_never_turns_the_drawbar(drawbar_stat):
    assert "power drawbar's motor" in machine_safety.validate_mdi("G0 A360", drawbar_stat)
    assert machine_safety.validate_mdi("G91 G0 X10", drawbar_stat) is None
    text = machine_safety.describe_machine(drawbar_stat)
    assert " A" not in text.split("Machine position:")[1].split("\n")[0]


def test_no_spindle_or_program_with_the_motor_down(drawbar_stat, tmp_path):
    assert machine_safety.validate_mdi("M3 S1000", drawbar_stat) is None
    drawbar_stat.dout = [1, 0, 0, 0]
    assert "drawbar motor is down" in machine_safety.validate_mdi("M3 S1000", drawbar_stat)
    program = tmp_path / "part.ngc"
    program.write_text("M30\n")
    drawbar_stat.file = str(program)
    assert "drawbar motor is down" in machine_safety.check_program_ready(drawbar_stat)


# --- the machine model and the Tools page ------------------------------------------------------------------

@pytest.fixture
def machine(shell):
    m = shell.machine
    m.set_estop(False)
    m.set_power(True)
    m.homed = {a: True for a in m.axes}
    return m


def test_model_runs_the_subroutines_and_locks_the_spindle(machine, monkeypatch):
    m = machine
    sent = []
    real = m.mdi
    monkeypatch.setattr(m, "mdi", lambda line: sent.append(line) or real(line))
    assert m.drawbar_release() and m.drawbar
    m.spindle_start(1, 1000)
    assert not m.spindle_dir  # released: the tool would fly out
    assert m.drawbar_clamp() and not m.drawbar
    assert m.drawbar_lower() and m.drawbar_lowered
    assert not m.mdi("M3 S500") and not m.spindle_dir  # the motor is on the drawbar
    assert m.drawbar_raise() and not m.drawbar_lowered
    assert m.drawbar_turn(-1)
    assert sent[:2] == ["o<drawbar_release> call", "o<drawbar_clamp> call"]
    assert "o<drawbar_turn> call [-1] [1.5]" in sent
    m.spindle_start(1, 1000)
    assert m.spindle_dir == 1
    assert not m.drawbar_release()  # not with the spindle running


def test_the_dro_and_jog_never_show_the_drawbar_axis(qapp):
    from milo_ui.machine import MachineModel, QtvcpMachine

    class Info:
        MACHINE_IS_METRIC = True
        MIN_SPINDLE_SPEED = MAX_SPINDLE_SPEED = DEFAULT_SPINDLE_SPEED = None
        MAX_FEED_OVERRIDE = MIN_SPINDLE_OVERRIDE = MAX_SPINDLE_OVERRIDE = MAX_TRAJ_VELOCITY = None
        DEFAULT_LINEAR_JOG_VEL = MAX_LINEAR_JOG_VEL = MIN_LINEAR_JOG_VEL = None
        JOG_INCREMENTS = MDI_COMMAND_LIST = MDI_COMMAND_LABEL_LIST = None
        PROGRAM_PREFIX = None
        NO_HOME_REQUIRED = False
        INIPATH = INI

        def get_error_safe_setting(self, section, key, default=None):
            return {("TRAJ", "COORDINATES"): "XYZA", ("DRAWBAR", "AXIS"): "A",
                    ("DRAWBAR", "TURN_SPEED"): "2"}.get((section, key), default)
    m = QtvcpMachine.__new__(QtvcpMachine)
    MachineModel.__init__(m)
    m.INFO = Info()
    m._read_ini()
    assert m.axes == ("X", "Y", "Z") and m.drawbar_axis == "A" and m.drawbar_turn_speed == 2.0


def test_tools_page_drawbar_controls(shell, machine, monkeypatch):
    from milo_ui.pages import tools
    card = shell.findChild(tools.CurrentTool)
    card.refresh()
    assert card.release.isEnabled() and card.clamp.isEnabled() and "Clamped" in card.drawbar_state.text()
    assert not card.test_box.isVisibleTo(card)
    card.test_button.click()
    assert card.test_box.isVisibleTo(card)
    # Release asks first, then runs the sequence
    monkeypatch.setattr(tools.kit, "ActionSheet", lambda host, title, actions, **kw: type(
        "S", (), {"show_centered": lambda self: actions[0][2]()})())
    card.release.click()
    assert machine.drawbar and "Released" in card.drawbar_state.text()
    machine.spindle_start(1, 1000)
    assert not machine.spindle_dir
    card.clamp.click()
    assert not machine.drawbar
    machine.spindle_start(1, 1000)
    card.refresh()
    assert not card.release.isEnabled()  # spindle running
