"""Tests for probe_jobs: routines, the plan (checked against qtvcp's real routines), results, offsets"""

import re

import pytest

import probe_jobs as pj

SETUP = pj.ProbeSetup()


def _jobs():
    jobs = []
    for corner in pj.CORNERS:
        jobs += [pj.ProbeJob(goal="corner", corner=corner), pj.ProbeJob(goal="corner", corner=corner, inside=True)]
    for edge in pj.EDGES:
        jobs += [pj.ProbeJob(goal="edge", edge=edge), pj.ProbeJob(goal="angle", edge=edge)]
    jobs += [pj.ProbeJob(goal="hole", diameter=30), pj.ProbeJob(goal="hole", shape="rectangle", width=40, length=30),
             pj.ProbeJob(goal="boss", diameter=30), pj.ProbeJob(goal="boss", shape="rectangle", width=40, length=24),
             pj.ProbeJob(goal="surface")]
    return jobs


def _name(job):
    return f"{job.goal}-{job.corner if job.goal == 'corner' else job.edge}-{job.shape}-{'in' if job.inside else 'out'}"


@pytest.mark.parametrize("job,routine", [
    (pj.ProbeJob(goal="corner", corner="front_left"), "probe_outside_xpyp"),
    (pj.ProbeJob(goal="corner", corner="back_right"), "probe_outside_xmym"),
    (pj.ProbeJob(goal="corner", corner="back_right", inside=True), "probe_inside_xpyp"),
    (pj.ProbeJob(goal="corner", corner="front_left", inside=True), "probe_inside_xmym"),
    (pj.ProbeJob(goal="edge", edge="left"), "probe_xp"),
    (pj.ProbeJob(goal="edge", edge="back"), "probe_ym"),
    (pj.ProbeJob(goal="angle", edge="front"), "probe_angle_yp"),
    (pj.ProbeJob(goal="hole"), "probe_round_pocket"),
    (pj.ProbeJob(goal="hole", shape="rectangle"), "probe_rectangular_pocket"),
    (pj.ProbeJob(goal="boss"), "probe_rectangular_boss"),  # never qtvcp's round boss routine (see routine())
    (pj.ProbeJob(goal="surface"), "probe_down"),
])
def test_routine_for_each_job(job, routine):
    assert pj.routine(job) == routine


def test_parameters_never_let_qtvcp_set_offsets():
    params = pj.parameters(pj.ProbeJob(goal="boss", diameter=30), SETUP)
    assert params["allow_auto_zero"] == "0" and params["allow_auto_skew"] == "0"
    assert params["x_hint_bp"] == params["y_hint_bp"] == "30"  # a round boss probed as a square one
    assert all(isinstance(v, str) for v in params.values())


# --- the plan, against qtvcp's real routines ----------------------------------------------------

class Recorder:
    """Runs a qtvcp ProbeRoutines method, tracking XY through its MDI lines and recording each
    G38.2 touch (start, direction, travel). Each touch reports the probe meeting the nominal part
    (the plan's contacts), so routines that move to a measured centre move where they really would.
    Nothing reaches LinuxCNC: the module's ACTION is replaced while it runs."""

    def __init__(self, module):
        self.module = module
        self.cls, self.status = module.ProbeRoutines, module.STATUS

    def run(self, routine, params, contacts=(), tip=0.0):
        pos = {"X": 0.0, "Y": 0.0, "Z": 0.0}
        saved = {}
        mode = {"relative": False}
        touches = []
        status = self.status

        def probed():
            index = len(touches) // 2 - 1  # each touch is a fast and a slow G38.2
            if 0 <= index < len(contacts) and contacts[index] is not None:
                (cx, cy), axis, travel = contacts[index], touches[-1][1], touches[-1][2]
                sign = 1 if travel > 0 else -1
                # The ball's centre stops a radius short of the wall
                return (cx - sign * tip / 2 if axis == "X" else pos["X"],
                        cy - sign * tip / 2 if axis == "Y" else pos["Y"], pos["Z"])
            return pos["X"], pos["Y"], pos["Z"]

        def mdi(code, timeout=5):
            for line in code.split("\n"):
                line = line.strip().upper()
                if not line:
                    continue
                if line.startswith("#<"):
                    saved[line[2]] = pos[line[2].upper()]
                    continue
                if "G91" in line:
                    mode["relative"] = True
                if "G90" in line:
                    mode["relative"] = False
                words = dict((m.group(1), m.group(2)) for m in re.finditer(r"([XYZ])(#<\w>|-?[\d.]+)", line))
                if "G38" in line:
                    (axis, value), = words.items()
                    v = float(value)
                    if axis in "XY":
                        touches.append(((round(pos["X"], 6), round(pos["Y"], 6)), axis, v))
                    else:
                        touches.append(((round(pos["X"], 6), round(pos["Y"], 6)), "Z", v))
                    continue
                for axis, value in words.items():
                    if value.startswith("#<"):
                        pos[axis] = saved[value[2].upper()]
                    elif mode["relative"]:
                        pos[axis] += float(value)
                    else:
                        pos[axis] = float(value)
            return 1

        class Run(self.cls):
            pass
        r = Run()
        r.CALL_MDI_WAIT = mdi
        for key, value in params.items():
            try:
                setattr(r, "data_" + key, float(value))
            except ValueError:
                pass
        r.allow_auto_zero = r.allow_auto_skew = False
        for key in ("xm", "xc", "xp", "ym", "yc", "yp", "lx", "ly", "z", "d", "a", "delta"):
            setattr(r, "status_" + key, None)
        r.history_log = ""
        class Action:
            def CALL_MDI(self, code):
                mdi(code)

            def __getattr__(self, name):
                return lambda *args, **kwargs: None
        real_action = self.module.ACTION
        self.module.ACTION = Action()
        real = status.get_probed_position_with_offsets, status.is_metric_mode
        status.get_probed_position_with_offsets = probed
        status.is_metric_mode = lambda: True
        try:
            assert getattr(r, routine)() == 1
        finally:
            self.module.ACTION = real_action
            status.get_probed_position_with_offsets, status.is_metric_mode = real
        return touches


@pytest.fixture(scope="module")
def recorder():
    try:
        from qtvcp.widgets import probe_routines
    except Exception as e:  # qtvcp needs a LinuxCNC config to import
        pytest.skip(f"qtvcp probe routines unavailable: {e}")
    return Recorder(probe_routines)


@pytest.mark.parametrize("job", _jobs(), ids=_name)
def test_plan_matches_the_real_routine(job, recorder):
    """Every touch in the preview starts where qtvcp's routine really goes down, and searches the same way"""
    plan = pj.plan(job, SETUP)
    touches = recorder.run(pj.routine(job), pj.parameters(job, SETUP), [t.contact for t in plan.touches],
                           SETUP.tip_diameter)
    assert len(touches) == len(plan.touches) * 2  # each touch is a fast and a slow G38.2
    fast = touches[::2]
    for real, planned in zip(fast, plan.touches):
        (x, y), axis, travel = real
        assert (x, y) == pytest.approx(planned.start, abs=1e-6)
        if axis == "Z":
            assert planned.direction == (0, 0) and travel == pytest.approx(-planned.travel)
        else:
            direction = planned.direction[0 if axis == "X" else 1]
            assert travel == pytest.approx(direction * planned.travel)


@pytest.mark.parametrize("job", _jobs(), ids=_name)
def test_expected_touches_are_within_reach(job):
    """The nominal part (where the hint says to start) is reached by every search"""
    for touch in pj.plan(job, SETUP).touches:
        if touch.contact is None:
            continue
        dx, dy = touch.contact[0] - touch.start[0], touch.contact[1] - touch.start[1]
        distance = abs(dx) + abs(dy)
        assert 0 < distance <= touch.travel, (touch, distance)
        assert (dx * touch.direction[0] + dy * touch.direction[1]) > 0  # ahead of the probe, not behind


def test_hints_say_where_to_start():
    hint = pj.plan(pj.ProbeJob(goal="corner", corner="back_right"), SETUP).start_hint
    assert "back-right corner" in hint and "2.5 mm in from both edges" in hint and "2 mm above the top" in hint
    assert "Inside the pocket" in pj.plan(pj.ProbeJob(goal="corner", inside=True), SETUP).start_hint
    assert "start near the edge's left end" in pj.plan(pj.ProbeJob(goal="angle", edge="front"), SETUP).start_hint


def test_soft_limits():
    plan = pj.plan(pj.ProbeJob(goal="corner", corner="front_left"), SETUP)
    limits = {"X": (0, 500), "Y": (0, 175), "Z": (-253, 0)}
    assert pj.limit_problem(plan, (100, 100, -50), limits) is None
    assert "outside the soft limits" in pj.limit_problem(plan, (3, 100, -50), limits)  # steps out 5 mm to X-2
    assert "Z" in pj.limit_problem(plan, (100, 100, -250), limits)


# --- results and offsets ---------------------------------------------------------------------------

def test_interpret_corner_edge_and_centres():
    data = {"xp": "12.5", "yp": "8.25", "xm": "None", "ym": "None", "xc": "40.000", "yc": "20.000", "lx": "30.010",
            "ly": "29.990", "d": "None", "z": "-3.200", "a": "0.214"}
    assert pj.interpret(pj.ProbeJob(goal="corner", corner="front_left"), data).values == {"X": 12.5, "Y": 8.25}
    assert pj.interpret(pj.ProbeJob(goal="edge", edge="left"), data).values == {"X": 12.5}
    boss = pj.interpret(pj.ProbeJob(goal="boss", diameter=30), data)
    assert boss.values == {"X": 40.0, "Y": 20.0} and boss.size == (pytest.approx(30.0),)  # round: mean of widths
    assert pj.interpret(pj.ProbeJob(goal="surface"), data).values == {"Z": -3.2}
    assert pj.interpret(pj.ProbeJob(goal="angle"), data).angle == 0.214
    with pytest.raises(ValueError):
        pj.interpret(pj.ProbeJob(goal="corner", corner="back_right"), data)  # xm/ym missing


def test_new_origin_puts_the_feature_at_zero():
    """Work = machine - g5x: a corner at work X 12.5 under a G54 of 100 is at machine 112.5"""
    found = pj.Found({"X": 12.5, "Y": -4.0})
    assert pj.new_origin(found, [100.0, 50.0, -20.0]) == {"X": 112.5, "Y": 46.0}


def test_checks_on_the_result():
    a, b = pj.Found({"X": 1.0, "Y": 2.0}, (20.0,)), pj.Found({"X": 1.004, "Y": 2.0}, (20.03,))
    assert pj.spread(a, b) == pytest.approx(0.03)
    hole = pj.ProbeJob(goal="hole", diameter=20)
    assert pj.size_warning(hole, pj.Found({}, (20.4,))) is None
    assert "you said about 20" in pj.size_warning(hole, pj.Found({}, (24.0,)))


def test_effective_tip_from_a_ring_gauge():
    # A 25 mm ring measured 24.6 with a 2 mm tip set: the tip acts like 2.4 mm
    assert pj.effective_tip(25.0, 24.6, 2.0) == pytest.approx(2.4)


def test_summaries():
    assert pj.proposal_summary(pj.ProbeJob(goal="corner", corner="back_left")) == \
        "Probe the back-left outside corner and set G54 X0 Y0 there"
    assert pj.proposal_summary(pj.ProbeJob(goal="surface", wcs="G55", repeat=True)) == \
        "Probe the top surface and set G55 Z0 there (twice, to compare)"
    found = pj.Found({"X": 1.0, "Y": 2.0}, (19.987,))
    assert pj.describe_found(pj.ProbeJob(goal="hole"), found) == \
        "Hole centre found at X 1.000  Y 2.000, ⌀19.987 mm (current work coordinates)."


def test_spindle_safety_helpers():
    assert pj.spins_before_tool_change("G21\nM3 S1000\nT1 M6\n")
    assert not pj.spins_before_tool_change("(M3 in a comment)\nT1 M6\nM3 S1000\n")
    assert pj.starts_spindle("m3 s500") and pj.starts_spindle("M04") and not pj.starts_spindle("M30")


def test_setup_from_prefs_reuses_the_vision_settings():
    class Prefs(dict):
        def set(self, k, v):
            self[k] = v
    prefs = Prefs({"vision.probe_tool": 42, "vision.probe_tip": 3.0})
    setup = pj.ProbeSetup.from_prefs(prefs)
    assert (setup.tool, setup.tip_diameter) == (42, 3.0)
    setup.max_travel = 20
    setup.save(prefs)
    assert pj.ProbeSetup.from_prefs(prefs).max_travel == 20
    inch = pj.ProbeSetup.from_prefs(Prefs(), "inch")
    assert inch.max_travel == pytest.approx(15 / 25.4, abs=1e-4) and inch.tool == 99
