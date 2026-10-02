"""Tests for machine_safety: MDI allowlist, machine state, soft limits, spindle limit"""

import linuxcnc
import pytest

import machine_safety
from machine_safety import (check_machine_ready, check_program_ready, describe_machine, parse_mdi,
                            spindle_max_rpm, validate_mdi)


@pytest.mark.parametrize("command", [
    "G91 G0 X10.0000", "G90 G0 X480", "G53 G0 Z0", "M3 S2000", "M3", "M5", "T5 M6", "G28", "M7", "M8", "M9",
    "G1 X10 F300",
])
def test_allowed_commands(stat, command):
    assert validate_mdi(command, stat) is None


@pytest.mark.parametrize("command,reason", [
    ("X10", "must include G0 or G1"),
    ("G2 X1", "G2 is not allowed"),
    ("G21", "G21 is not allowed"),
    ("#1=5", "not a plain G-code line"),
    ("o100 call", "not a plain G-code line"),
    ("G1 X10 F0", "Feed rate must be positive"),
    ("M3 S-1", "can't be negative"),
    ("G90 G91 G0 X1", "G90 and G91"),
    ("G91 G53 G0 X1", "G53 can't be combined with G91"),
    ("G0 B5", "no B axis"),
    ("G0 X1 X2", "more than once"),
])
def test_rejected_commands(stat, command, reason):
    assert reason in validate_mdi(command, stat)


@pytest.mark.parametrize("command,ok", [
    ("G91 G0 X10", True),     # 100 + 10 = 110
    ("G91 G0 X-150", False),  # 100 - 150 = -50 < 0
    ("G90 G0 X480", True),    # 480 + 10 offset = 490
    ("G90 G0 X495", False),   # 505 > 500
    ("G53 G0 Z0", True),
    ("G53 G0 Z5", False),     # above Z max 0
])
def test_soft_limits(stat, command, ok):
    assert (validate_mdi(command, stat) is None) == ok


def test_soft_limits_in_inch_mode(stat):
    stat.program_units = 1
    assert validate_mdi("G91 G0 X15", stat) is None  # 100 + 381 = 481
    assert "soft limits" in validate_mdi("G91 G0 X16", stat)  # 100 + 406.4 = 506.4


def test_motion_requires_homing(stat):
    stat.homed = (1, 0, 1)
    assert validate_mdi("G0 X1", stat) == "Machine is not homed."
    assert validate_mdi("M3 S1000", stat) is None


@pytest.mark.parametrize("field,value,reason", [
    ("task_state", linuxcnc.STATE_ESTOP, "E-stop"),
    ("task_state", linuxcnc.STATE_OFF, "not powered on"),
    ("interp_state", linuxcnc.INTERP_READING, "busy"),
])
def test_machine_not_ready(stat, field, value, reason):
    setattr(stat, field, value)
    assert reason in check_machine_ready(stat, needs_homed=False)


def test_spindle_max_from_ini(stat, ini_with_spindle_max):
    machine_safety._ini_cache.clear()
    stat.ini_filename = ini_with_spindle_max
    assert spindle_max_rpm(stat) == 3000
    assert "above the spindle maximum of 3000" in validate_mdi("M3 S12000", stat)
    assert validate_mdi("M3 S3000", stat) is None
    assert "maximum speed 3000 rpm" in describe_machine(stat)


def test_no_ini_means_no_spindle_limit(stat):
    assert spindle_max_rpm(stat) is None
    assert validate_mdi("M3 S12000", stat) is None


def test_program_ready(stat, tmp_path):
    program = tmp_path / "part.ngc"
    program.write_text("G0 X0\nM2\n")
    assert check_program_ready(stat) == "No program is loaded."
    stat.file = str(tmp_path / "missing.ngc")
    assert "no longer exists" in check_program_ready(stat)
    stat.file = str(program)
    assert check_program_ready(stat) is None
    stat.interp_state = linuxcnc.INTERP_PAUSED
    assert "busy" in check_program_ready(stat)


def test_describe_machine(stat):
    stat.file = "/home/cnc/linuxcnc/nc_files/ai/part.ngc"
    text = describe_machine(stat)
    assert "Work position: X90.0000 Y40.0000 Z90.0000" in text
    assert "Machine position: X100.0000 Y50.0000 Z-10.0000" in text
    assert "Loaded program: part.ngc; program state: idle" in text
    assert "work offset: G54" in text


def test_parse_mdi():
    assert parse_mdi("g91 g0 x-10.5") == [("G", 91.0), ("G", 0.0), ("X", -10.5)]


def test_work_offset_read_from_gcodes(stat):
    from machine_safety import active_work_offset
    assert active_work_offset(stat) == "G54"
    stat.gcodes = (0, 900, 210, 591)
    assert active_work_offset(stat) == "G59.1"


def test_describe_machine_gives_axis_travel_and_center(stat):
    text = describe_machine(stat)
    assert "Axis travel (machine coordinates): X 0.000 to 500.000 (center 250.000); " \
           "Y 0.000 to 175.000 (center 87.500); Z -253.000 to 0.000 (center -126.500)" in text


VAR_FILE = """5210\t0.000000
5211\t0.000000
5212\t0.000000
5213\t0.000000
5220\t1.000000
5221\t230.000000
5222\t8.485800
5223\t-147.907333
5230\t0.000000
"""


@pytest.fixture
def unsynced_stat(tmp_path):
    """Status before any work-offset command: g5x_index 0 and zero offsets, like LinuxCNC 2.9 at startup"""
    (tmp_path / "linuxcnc.var").write_text(VAR_FILE)
    ini = tmp_path / "machine.ini"
    ini.write_text("[RS274NGC]\nPARAMETER_FILE = linuxcnc.var\n")
    machine_safety._ini_cache.clear()
    from conftest import FakeStat
    return FakeStat(g5x_index=0, g5x_offset=[0.0] * 9, position=[10.0, 10.0, -10.0] + [0.0] * 6,
                    ini_filename=str(ini))


def test_offsets_read_from_parameter_file_when_status_unsynced(unsynced_stat):
    from machine_safety import work_offsets
    offsets, rotation, source = work_offsets(unsynced_stat)
    assert offsets[:3] == pytest.approx([230.0, 8.4858, -147.907333])
    assert source == "parameter file"


def test_the_reported_move(unsynced_stat):
    """'Go to X250' in G54 with a 230 offset ends at machine X480, and the confirmation says so"""
    from machine_safety import work_move_note
    assert validate_mdi("G90 G0 X250.0000", unsynced_stat) is None
    assert work_move_note("G90 G0 X250.0000", unsynced_stat) == "G54 work coordinates \u2192 machine X480.000"
    assert "would end at 530.000" in validate_mdi("G90 G0 X300", unsynced_stat)
    assert work_move_note("G53 G0 X250", unsynced_stat) == ""
    assert work_move_note("G91 G0 X10", unsynced_stat) == ""


def test_unknown_offsets_refuse_work_moves_only(stat):
    stat.g5x_index = 0  # unsynced and no parameter file to fall back on
    assert "work offset isn't known" in validate_mdi("G90 G0 X100", stat)
    assert validate_mdi("G53 G0 X100", stat) is None
    assert validate_mdi("G91 G0 X10", stat) is None
    assert "Work position: unknown" in describe_machine(stat)


def test_describe_machine_uses_file_offsets(unsynced_stat):
    text = describe_machine(unsynced_stat)
    assert "G54 origin is at machine X230.000 Y8.486 Z-147.907" in text
    assert "Work position: X-220.0000" in text


def test_offsets_synced_follows_g5x_index(stat, unsynced_stat):
    from machine_safety import offsets_synced
    assert offsets_synced(stat)
    assert not offsets_synced(unsynced_stat)


def test_work_position_uses_file_offsets_until_synced(unsynced_stat):
    """The DRO's work position at startup: machine X480 is G54 X250, not X480"""
    from machine_safety import work_position
    work = work_position(unsynced_stat, [480.0, 8.4858, -147.907333] + [0.0] * 6)
    assert work[:3] == pytest.approx([250.0, 0.0, 0.0])


def test_work_position_matches_linuxcnc_math(stat):
    """Minus G5x and tool offset, rotated by the XY rotation, minus G92 (as hal_glib does)"""
    from machine_safety import work_position
    stat.g5x_offset = [10.0, 20.0, -100.0] + [0.0] * 6
    stat.tool_offset = [0.0, 0.0, 25.0] + [0.0] * 6
    stat.g92_offset = [1.0, 2.0, 3.0] + [0.0] * 6
    stat.rotation_xy = 90.0
    work = work_position(stat, [20.0, 20.0, -50.0] + [0.0] * 6)
    # (10, 0) rotated by -90 degrees is (0, -10)
    assert work[:3] == pytest.approx([-1.0, -12.0, 22.0])


def test_work_position_unknown_without_offsets(stat):
    from machine_safety import work_position
    stat.g5x_index = 0  # unsynced and no parameter file
    assert work_position(stat, [0.0] * 9) is None


def test_stored_work_offset(stat, unsynced_stat):
    from machine_safety import stored_work_offset
    # Before sync the status says zero; the file has the real G54
    assert stored_work_offset(unsynced_stat, "G54")[:3] == pytest.approx([230.0, 8.4858, -147.907333])
    assert stored_work_offset(unsynced_stat, "G55")[:3] == [0.0, 0.0, 0.0]
    # Once synced, the active system comes from the status
    assert stored_work_offset(stat, "G54")[:3] == [10.0, 10.0, -100.0]
    assert stored_work_offset(stat, "G55") is None  # inactive and no parameter file
    assert stored_work_offset(stat, "G99") is None
