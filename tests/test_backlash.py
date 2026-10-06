"""Tests for backlash calibration: the plan, the fit, the INI, the runner and the Calibrate page"""

import os
import shutil
import time

import numpy as np
import pytest

import backlash as bl
from test_milo_ui import shell  # noqa: F401  (fixture)

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_INI = os.path.join(CONFIG_DIR, "Mesa7I96S.ini")


def pump(qapp, until, timeout=30.0):
    end = time.time() + timeout
    while not until() and time.time() < end:
        qapp.processEvents()
        time.sleep(0.005)
    return until()


def readings_for(lash, settings, gauge, axis=0, origin=100.0):
    """Readings an axis with `lash` mm of slack would give, through gauge(actual position)"""
    sim = bl.LashSim({axis: lash})
    out = []
    for stop in bl.plan(axis, origin, settings):
        for position in (stop.start, stop.target):
            commanded = [0.0, 0.0, 0.0]
            commanded[axis] = position
            actual = sim(commanded)[axis]
        out.append(bl.Reading(stop, gauge(actual)))
    return out


# --- plan and fit ------------------------------------------------------------------------------------

def test_plan_approaches_each_stop_from_the_side_it_says():
    s = bl.Settings(approach=1.0, step=0.5, repeats=2)
    stops = bl.plan(1, 50.0, s)
    assert [(x.kind, x.target, x.direction) for x in stops[:3]] == [
        ("plus", 50.0, 1), ("step", 50.5, 1), ("minus", 50.0, -1)]
    assert len(stops) == 6
    for stop in stops:
        assert (stop.target - stop.start) * stop.direction == pytest.approx(1.0)
    assert bl.travel(stops) == (49.0, 51.0)


def test_limit_problem_names_the_way_to_move():
    stops = bl.plan(2, -0.5, bl.Settings())
    problem = bl.limit_problem(stops, {"Z": (-253.0, 0.0)})
    assert "upper soft limit" in problem and "move Z down" in problem
    assert bl.limit_problem(bl.plan(2, -10.0, bl.Settings()), {"Z": (-253.0, 0.0)}) is None


@pytest.mark.parametrize("sign", [1.0, -1.0])
def test_indicator_either_way_round_gives_the_backlash(sign):
    s = bl.Settings()
    readings = readings_for(0.05, s, lambda actual: sign * (actual - 100.0) + 0.3)
    result = bl.analyse(readings, s, "indicator")
    assert result.backlash == pytest.approx(0.05, abs=1e-4)
    assert result.spread == pytest.approx(0.0, abs=1e-4)
    assert result.scale == pytest.approx(sign)
    assert result.warnings == []
    assert result.suggested == pytest.approx(0.05, abs=1e-4)


def test_what_remains_adds_to_the_compensation_running():
    s = bl.Settings()
    readings = readings_for(0.02, s, lambda actual: actual)
    result = bl.analyse(readings, s, "indicator", active=0.05)
    assert result.suggested == pytest.approx(0.07, abs=1e-4)
    over = bl.analyse(readings_for(-0.01, s, lambda actual: actual), s, "indicator", active=0.05)
    assert over.suggested == pytest.approx(0.04, abs=1e-4)


def test_camera_readings_are_projected_onto_the_way_the_tag_moves():
    s = bl.Settings()
    angle = np.radians(33.0)

    def tag(actual):  # 9 px per mm, along a tilted direction in the image
        d = actual - 100.0
        return (700.0 + 9.0 * d * np.cos(angle), 500.0 - 9.0 * d * np.sin(angle))
    result = bl.analyse(readings_for(0.04, s, tag), s, "camera")
    assert result.backlash == pytest.approx(0.04, abs=1e-4)
    assert abs(result.scale) == pytest.approx(9.0, rel=1e-3)


def test_noisy_readings_are_flagged():
    s = bl.Settings(repeats=4)
    rng = np.random.default_rng(3)
    result = bl.analyse(readings_for(0.05, s, lambda a: a + rng.normal(0, 0.02)), s, "indicator")
    assert any("disagree" in w or "scatter" in w for w in result.warnings)


def test_inch_readings_and_short_approaches_are_flagged():
    s = bl.Settings(approach=0.1)
    result = bl.analyse(readings_for(0.09, s, lambda a: a / 25.4), s, "indicator")
    assert any("inches" in w for w in result.warnings)
    assert any("approach distance" in w for w in result.warnings)


def test_lash_sim_follows_only_after_the_slack():
    sim = bl.LashSim({0: 0.1})
    assert sim([10.0])[0] == pytest.approx(9.95)
    assert sim([10.04])[0] == pytest.approx(9.99)   # still moving +: it follows
    assert sim([9.99])[0] == pytest.approx(9.99)    # reversed within the slack: the table stays
    assert sim([9.9])[0] == pytest.approx(9.95)


# --- the INI -------------------------------------------------------------------------------------------

@pytest.fixture
def ini(tmp_path):
    path = tmp_path / "Mesa7I96S.ini"
    shutil.copy(REAL_INI, path)
    return str(path)


def test_joints_come_from_the_kinematics(ini):
    text = open(ini).read()
    assert [bl.joint_for(text, a) for a in "XYZ"] == [0, 1, 2]
    assert bl.read_backlash(ini) == {"X": 0.0, "Y": 0.0, "Z": 0.0}


def test_apply_writes_backlash_and_makes_room_for_it(ini, tmp_path):
    before = open(ini).read()
    changes = bl.apply(ini, {"X": 0.052}, backup_dir=str(tmp_path / "backups"), method="dial indicator")
    after = open(ini).read()
    assert bl.read_backlash(ini)["X"] == pytest.approx(0.052)
    assert bl.read_value(after, "JOINT_0", "STEPGEN_MAXACCEL") == "400.00"
    assert bl.read_value(after, "JOINT_1", "STEPGEN_MAXACCEL") == "250.00"  # other joints untouched
    assert any("STEPGEN_MAXACCEL" in c for c in changes)
    lines = after.splitlines()
    i = lines.index("BACKLASH = 0.0520")
    assert lines[i - 1].startswith(bl.COMMENT) and lines[i - 2].startswith("STEPGEN_MAXACCEL")
    assert after.endswith(before[-1])  # the file's last line ending is kept as it was
    import difflib
    diff = [d for d in difflib.ndiff(before.splitlines(), after.splitlines()) if d[:2] in ("- ", "+ ")]
    assert diff == ["- STEPGEN_MAXACCEL = 250.00", "+ STEPGEN_MAXACCEL = 400.00",
                    f"+ {lines[i - 1]}", "+ BACKLASH = 0.0520"]
    backups = os.listdir(tmp_path / "backups")
    assert len(backups) == 1 and open(tmp_path / "backups" / backups[0]).read() == before


def test_apply_again_replaces_in_place(ini):
    bl.apply(ini, {"Z": 0.03})
    bl.apply(ini, {"Z": 0.0}, method="turned off")
    text = open(ini).read()
    assert text.count("\nBACKLASH =") == 1 and text.count(bl.COMMENT) == 1
    assert bl.read_backlash(ini)["Z"] == 0.0


# --- the runner and the page ------------------------------------------------------------------------------

@pytest.fixture
def page(shell, qapp):
    p = shell.pages["calibrate"]
    m = shell.machine
    m.set_estop(False)
    m.set_power(True)
    m.homed = {a: True for a in m.axes}
    p.settle = 0.01
    shell.navigate("calibrate")
    return p


def at(machine, x, y, z):
    machine.pos_abs = [x, y, z]
    machine._update_rel()
    machine.position_changed.emit()


def test_calibrate_sits_between_probe_and_vision(shell):
    keys = list(shell.pages)
    assert keys.index("calibrate") == keys.index("probe") + 1 == keys.index("vision") - 1
    page = shell.pages["calibrate"]
    assert page.goal_tiles and list(page.goal_tiles)[0] == "backlash"


def test_guided_indicator_measurement(page, qapp):
    m = page.machine
    at(m, 200.0, 90.0, -60.0)
    page._set_method("indicator")
    page._set_axis("X")
    assert page.can_start()
    assert page.start()
    origin = 200.0
    for _ in range(9):
        assert pump(qapp, lambda: page.runner.state == "waiting")
        assert page.reading_card.isVisibleTo(page)
        actual = page.lash_sim(m.pos_abs)[0]
        page._submit(round(-(actual - origin) + 1.2, 3))   # plunger pointing the other way, 0.001 dial
    assert pump(qapp, lambda: not page.running)
    result = page.results["X"]
    assert result.backlash == pytest.approx(0.06, abs=0.002)
    assert page.save_button.isEnabled()
    assert page._write({"X": result.suggested}, "dial indicator")
    assert bl.read_backlash(m.ini_path)["X"] == pytest.approx(result.suggested)
    assert m.ini_path != REAL_INI


@pytest.mark.parametrize("axis", ["X", "Y"])
def test_camera_measures_x_and_y_from_a_tag(page, qapp, axis):
    m = page.machine
    cam = page._ensure_camera()
    x, y = cam.model.centre_over(160.0, 40.0)   # the sim scene's tag
    at(m, x, y, -150.0)
    page._set_method("camera")
    page._set_axis(axis)
    page._grab_live()
    assert page.live_tags, "the tag should be in view"
    assert page.can_start()
    assert page.start()
    assert pump(qapp, lambda: not page.running, timeout=60)
    result = page.results[axis]
    assert result.backlash == pytest.approx(page.lash_sim.lash["XYZ".index(axis)], abs=0.012)


def test_camera_measures_z_from_the_laser(page, qapp):
    m = page.machine
    at(m, 350.0, 150.0, -64.0)   # over clear table, away from the vise
    page._set_method("camera")
    page._set_axis("Z")
    page._grab_live()
    assert page.can_start(), page.problems()
    assert page.start()
    assert pump(qapp, lambda: not page.running, timeout=60)
    assert not m.laser_on
    assert page.results["Z"].backlash == pytest.approx(page.lash_sim.lash[2], abs=0.02)


def test_estop_ends_a_measurement(page, qapp):
    m = page.machine
    at(m, 200.0, 90.0, -60.0)
    page._set_method("indicator")
    page._set_axis("Y")
    assert page.start()
    assert pump(qapp, lambda: page.runner.state == "waiting")
    m.set_estop(True)
    assert pump(qapp, lambda: not page.running)
    assert "Y" not in page.results
    assert "stopped" in page.status.text().lower()


def test_wont_start_without_room_or_with_the_spindle_on(page):
    m = page.machine
    page._set_method("indicator")
    page._set_axis("Z")
    at(m, 200.0, 90.0, -0.2)
    assert not page.can_start()
    at(m, 200.0, 90.0, -50.0)
    assert page.can_start()
    m.spindle_start(1, 1000)
    assert not page.can_start()
    m.spindle_stop()
