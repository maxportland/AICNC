"""Tests for the redesigned Probe page: checks, running, results, undo, Milo, the pendant, safety, camera"""

import os
import sys
import textwrap
import time

import pytest

import machine_safety
import probe_jobs as pj
from conftest import FakeStat
from test_milo_ui import shell  # noqa: F401  (fixture)


def pump(qapp, seconds=0.0):
    end = time.time() + seconds
    while True:
        qapp.processEvents()
        if time.time() >= end:
            break
        time.sleep(0.01)


def respond_with(results):
    """A SimProbeRunner answer: the same results every time (strings like the subprogram's)"""
    return lambda routine, params: {k: f"{v:.3f}" for k, v in results.items()}


@pytest.fixture
def page(shell, qapp):
    from milo_ui.probe_runner import SimProbeRunner
    p = shell.pages["probe"]
    m = shell.machine
    p.runner = SimProbeRunner(m, delay_ms=1)
    p.runner.finished.connect(p._on_finished)
    p.runner.failed.connect(p._on_failed)
    m.set_estop(False)
    m.set_power(True)
    m.homed = {a: True for a in m.axes}
    m.pos_abs = [200.0, 90.0, -60.0]
    m._update_rel()
    m.offset_history = []
    yield p
    machine_safety.set_probe_tool(None)


def ready(page, qapp):
    """Probe in the spindle and tapped"""
    m = page.machine
    m.tool = page.setup.tool
    m._emit("tool")
    m.probe_tripped = True
    m._emit("state")
    m.probe_tripped = False
    m._emit("state")
    pump(qapp)


def run(page, qapp, results, **job):
    page.runner.respond = respond_with(results)
    page.select_goal(job.pop("goal", "corner"))
    page._set_job(**job)
    assert page.start(), [t for ok, t, _ in page.problems() if not ok]
    pump(qapp, 0.1)
    assert not page.running


def test_probe_sits_in_the_rail_with_every_goal(page):
    assert list(page.goal_tiles) == list(pj.GOALS)
    page.select_goal("hole")
    assert page.stack.currentWidget() is page.job_view and page.size_rows["diameter"].isVisibleTo(page)
    page.select_goal("tool")
    assert page.stack.currentWidget() is page.tool_view and not page.preview_card.isVisibleTo(page)
    page.select_goal("setup")
    assert page.stack.currentWidget() is page.setup_view


def test_start_waits_for_the_probe_and_a_tap(page, qapp):
    m = page.machine
    page.select_goal("corner")
    assert not page.start_button.isEnabled()
    texts = [t for ok, t, _ in page.problems() if not ok]
    assert "Put the probe (T99) in the spindle" in texts and any("Tap the probe tip" in t for t in texts)
    # "It's in" tells LinuxCNC (M61) which tool is in the spindle
    fix = next(f for ok, t, f in page.problems() if f)
    sent = []
    m.mdi = lambda line: sent.append(line) or True
    fix[1]()
    assert sent == ["M61 Q99 G43"]
    del m.mdi
    ready(page, qapp)
    assert page.start_button.isEnabled()
    # Putting the probe in again needs a new tap; a probe that reads as touching blocks it
    m.tool = 3
    m._emit("tool")
    m.tool = 99
    m._emit("tool")
    assert not page.probe_checked
    ready(page, qapp)
    m.probe_tripped = True
    page.refresh()
    assert any("reads as touching" in t for ok, t, _ in page.problems() if not ok)


def test_inch_mode_and_soft_limits_block_probing(page, qapp):
    m = page.machine
    ready(page, qapp)
    m.metric = False
    assert any("Switch to G21" in t for ok, t, _ in page.problems() if not ok)
    m.metric = True
    m.pos_abs = [2.0, 90.0, -60.0]  # stepping out 5 mm would go past X0
    assert any("soft limits" in t for ok, t, _ in page.problems() if not ok)
    assert not page.start()


def test_corner_result_set_and_undo(page, qapp, shell):
    m = page.machine
    ready(page, qapp)
    before = list(m.wcs_offsets["G54"])
    run(page, qapp, {"xp": 12.5, "yp": -3.25}, goal="corner", corner="front_left", repeat=False)
    assert page.stack.currentWidget() is page.result_view
    assert page.result_headline.text().startswith("Corner found at X 12.500  Y -3.250")
    assert "moves its origin X +12.500, Y -3.250 mm" in page.result_details.text()
    assert page.set_button.text() == "Set G54 X0 Y0"
    assert page.apply_result()
    assert m.wcs_offsets["G54"][:2] == pytest.approx([before[0] + 12.5, before[1] - 3.25])
    assert shell.prefs.get("offset_history")[-1]["label"] == "Probe the front-left outside corner"
    page.undo()
    assert m.wcs_offsets["G54"] == pytest.approx(before) and m.offset_history == []


def test_result_into_another_system(page, qapp):
    m = page.machine
    ready(page, qapp)
    run(page, qapp, {"z": -4.0}, goal="surface")
    page.wcs_toggle.selected.emit(1)  # G55
    assert page.apply_result()
    # Work Z -4 in G54 (offset -112.8375) is machine -116.8375: that's G55's Z0 now
    assert m.wcs_offsets["G55"][2] == pytest.approx(-116.8375) and m.wcs_offsets["G54"][2] == pytest.approx(-112.8375)


def test_two_touches_that_disagree_are_flagged_and_not_auto_set(page, qapp):
    m = page.machine
    ready(page, qapp)
    answers = iter([{"xc": 10.0, "yc": 5.0, "d": 20.0}, {"xc": 10.05, "yc": 5.0, "d": 20.0}])
    page.runner.respond = lambda routine, params: {k: f"{v:.3f}" for k, v in next(answers).items()}
    page.select_goal("hole")
    page._set_job(shape="round", diameter=20, repeat=True)
    before = list(m.wcs_offsets["G54"])
    assert page.start(auto_apply=True)
    pump(qapp, 0.2)
    assert "differ by 0.050" in page.result_warnings.text()
    assert m.wcs_offsets["G54"] == before  # a confirmed request still waits when something looks off


def test_a_hole_of_the_wrong_size_is_flagged(page, qapp):
    ready(page, qapp)
    run(page, qapp, {"xc": 0, "yc": 0, "d": 26.0}, goal="hole", shape="round", diameter=20, repeat=False)
    assert "you said about 20" in page.result_warnings.text()


def test_a_failed_routine_says_why(page, qapp):
    ready(page, qapp)
    page.runner.respond = lambda routine, params: "the probe touched something on the way down"
    page.select_goal("edge")
    assert page.start()
    pump(qapp, 0.1)
    assert not page.running and "touched something on the way down" in page.status.text()
    assert page.stack.currentWidget() is page.job_view


def test_angle_rotates_the_system(page, qapp):
    m = page.machine
    ready(page, qapp)
    run(page, qapp, {"a": 0.25, "yc": 1.0, "yp": 1.04}, goal="angle", edge="front", repeat=False)
    assert "turned +0.250°" in page.result_headline.text() and page.set_button.text() == "Rotate G54 to 0.250°"
    assert page.apply_result()
    assert m.wcs_rotation["G54"] == pytest.approx(0.25)
    page.undo()
    assert m.wcs_rotation["G54"] == pytest.approx(0.0)


def test_calibrating_the_tip(page, qapp, shell):
    ready(page, qapp)
    shell.prefs.set("probe_ring_gauge", 25.0)
    page.runner.respond = respond_with({"xc": 0.0, "yc": 0.0, "d": 24.6})
    page.select_goal("setup")
    assert page.calibrate()
    pump(qapp, 0.2)
    assert "acts like ⌀2.400 mm" in page.calibrate_status.text()
    page._save_tip()
    assert page.setup.tip_diameter == pytest.approx(2.4) and shell.prefs.get("probe_setup")["tip_diameter"] == 2.4
    assert page.job.goal != "hole" or page.job.diameter != 25.0  # the operator's job is back


# --- Milo and the pendant -----------------------------------------------------------------------------

def test_milo_request_is_set_up_and_confirmed(page, qapp):
    m = page.machine
    action, reason = page.prepare({"goal": "corner", "corner": "back right", "inside": True, "wcs": "G55"})
    assert action is None and "First:" in reason  # probe not in yet
    assert page.job.corner == "back_right" and page.job.inside and page.job.wcs == "G55"
    ready(page, qapp)
    action, reason = page.prepare({"goal": "corner", "corner": "back_right", "inside": True, "wcs": "G55"})
    assert action["summary"] == "Probe the back-right inside corner and set G55 X0 Y0 there"
    page.runner.respond = respond_with({"xp": 4.0, "yp": 6.0})
    before = list(m.wcs_offsets["G55"])
    assert page.execute(action).startswith("[MILO] Probing")
    pump(qapp, 0.1)
    assert m.wcs_offsets["G55"] != before  # confirmed "and set": applied without asking again
    assert page.applied_label.startswith("G55 X0 Y0 set")


def test_engine_routes_probe_requests(tmp_path, qapp):
    from milo_engine import MiloEngine
    from test_engine import Recorder

    class Handler:
        def __init__(self):
            self.executed = []

        def prepare(self, request):
            return {"kind": "probe", "summary": "Probe the top surface and set G54 Z0 there", "job": {}}, None

        def execute(self, action):
            self.executed.append(action)
            return "[MILO] Probing the top surface."
    listener = Recorder()
    engine = MiloEngine(listener=listener, stat_getter=lambda: FakeStat(), settings_path=str(tmp_path / "s.json"),
                        config_dir=str(tmp_path))
    from action_confirmation import ActionConfirmation
    engine.confirmation = ActionConfirmation(execute_callback=engine._execute_confirmed_action, log_callback=engine.log)
    engine._propose_probe({"goal": "surface"})
    assert "Probing is only available" in [a[0] for n, a in listener.events if n == "on_message"][-1]
    engine.probe_handler = Handler()
    engine._propose_probe({"goal": "surface"})
    assert engine.confirmation.pending
    engine.confirm()
    assert engine.probe_handler.executed and "[MILO] Probing the top surface." in \
        [a[0] for n, a in listener.events if n == "on_message"]


def test_router_reads_probe_requests():
    from intent_router import IntentRouter
    result = IntentRouter._normalize({"intent": "probe", "probe": {"goal": "Hole", "diameter": "20", "wcs": "G55",
                                                                   "inside": None}}, "{}")
    assert result["intent"] == "probe" and result["probe"] == {"goal": "hole", "wcs": "G55", "diameter": 20.0}
    vague = IntentRouter._normalize({"intent": "probe", "probe": {}}, "{}")
    assert vague["intent"] == "unclear" and "corner, an edge" in vague["answer"]


def test_pendant_probe_ring(page, shell, qapp):
    from milo_ui.pendant import MOVE_ITEMS, PROBE_ACTIONS, radial_entry
    p = shell.pendant
    assert "probe_menu" in MOVE_ITEMS
    p.open_menu(MOVE_ITEMS, "Zero & go to")
    p.choose("probe_menu")
    assert p.menu_items == list(PROBE_ACTIONS) and p.menu_title == "Probe what?" and p.in_submenu
    assert radial_entry("probe_hole", shell.machine) == ("Hole centre", "circle-dashed", False)
    p.close_menu()
    ready(page, qapp)
    proposed = []
    shell.engine.confirmation = type("Gate", (), {"propose": lambda self, a: proposed.append(a)})()
    shell.run_quick_action("probe_surface")
    assert shell.current == "probe" and proposed[0]["summary"].startswith("Probe the top surface")
    shell.engine.confirmation = None


# --- safety ----------------------------------------------------------------------------------------------

def test_the_spindle_wont_start_with_the_probe_in(page, qapp, shell):
    m = page.machine
    ready(page, qapp)
    m.spindle_start(1, 1000)
    assert not m.spindle_dir
    assert not m.mdi("M3 S1000") and not m.mdi_lines(["G0 X1", "M4 S500"])
    assert m.mdi("G0 X1")  # moving is fine
    stat = FakeStat(tool_in_spindle=99)
    assert "touch probe (T99)" in machine_safety.validate_mdi("M3 S1000", stat)
    assert machine_safety.validate_mdi("M5", stat) is None


def test_a_program_that_spins_first_wont_start_with_the_probe_in(page, qapp, shell, tmp_path):
    m = page.machine
    ready(page, qapp)
    spins = tmp_path / "spins.ngc"
    spins.write_text("G21\nM3 S1000\nG0 X0\nM30\n")
    changes = tmp_path / "changes.ngc"
    changes.write_text("G21\nT1 M6\nM3 S1000\nM30\n")
    for path, runs in ((spins, False), (changes, True)):
        m.open_program(str(path))
        shell.start_program()
        assert m.is_running == runs, path
        m.abort()


# --- camera ---------------------------------------------------------------------------------------------

def _scan():
    from milo_vision.scan import Part, ScanResult
    part = Part(center=(150.0, 80.0), size=(60.0, 40.0), angle=0.0,
                corners=[(120.0, 60.0), (180.0, 60.0), (180.0, 100.0), (120.0, 100.0)])
    return ScanResult(parts=[part], tags={}, table_z=0.0, frames=3)


def test_camera_places_the_start_and_checks_the_result(page, qapp):
    from milo_ui.probe_widgets import camera_start, classify_corners, nearest_feature
    part = _scan().parts[0]
    assert classify_corners(part)["front_left"] == (120.0, 60.0)
    corner = pj.ProbeJob(goal="corner", corner="front_left")
    assert camera_start(corner, page.setup, part) == pytest.approx((122.5, 62.5))  # inset into the part
    assert camera_start(pj.ProbeJob(goal="corner", inside=True), page.setup, part) is None
    assert camera_start(pj.ProbeJob(goal="edge", edge="back"), page.setup, part) == pytest.approx((150.0, 97.5))
    assert nearest_feature(part, 178, 99, "corner") == "back_right"
    assert nearest_feature(part, 119, 82, "edge") == "left"

    m = page.machine
    ready(page, qapp)
    page.scan = _scan()
    page.select_goal("corner")
    page._set_camera(True)
    page._camera_tapped(181, 99)
    assert page.job.corner == "back_right" and page.move_button.isVisibleTo(page)
    sent = []
    m.mdi_lines = lambda lines: sent.append(lines) or True
    assert page.move_to_start()
    assert sent == [["G90 G53 G0 Z0", "G90 G53 G0 X177.500 Y97.500"]]
    del m.mdi_lines
    # The probe finds the corner 10 mm from where the camera saw it: flagged
    g54 = m.wcs_offsets["G54"]
    run(page, qapp, {"xm": 190.0 - g54[0], "ym": 100.0 - g54[1]}, goal="corner", corner="back_right", repeat=False)
    assert "from where the camera saw it" in page.result_warnings.text()


# --- the subprogram protocol ---------------------------------------------------------------------------

@pytest.mark.parametrize("output,kind,value", [
    ('COMPLETE${"xp": "1.000"}', "complete", {"xp": "1.000"}),
    ("ERROR INFO Probe routine: failed: the probe touched something", "error", "the probe touched something"),
    ("ERROR Probe routine returned with error", "error", "Probe routine returned with error"),
    ("HISTORY Outside XPYP", None, None),
])
def test_parse_subprogram_lines(output, kind, value):
    from milo_ui.probe_runner import parse_line
    assert parse_line(output) == (kind, value)


def test_runner_talks_to_a_subprogram(qapp, tmp_path):
    from milo_ui.probe_runner import ProbeRunner
    script = tmp_path / "fake_subprog.py"
    script.write_text(textwrap.dedent('''
        import json, sys
        routine, params = sys.stdin.readline().rstrip().split("$", 1)
        params = json.loads(params)
        if routine == "probe_down":
            print("HISTORY Straight Down")
            print("COMPLETE$" + json.dumps({"z": params["max_z_travel"]}), flush=True)
        elif routine == "probe_xp":
            print("ERROR INFO Probe routine: failed: no contact", flush=True)
    '''))
    blocked = []
    runner = ProbeRunner(program=str(script), python=sys.executable, block_errors=lambda: blocked.append("block"),
                         unblock_errors=lambda: blocked.append("unblock"))
    results, failures = [], []
    runner.finished.connect(results.append)
    runner.failed.connect(failures.append)
    for routine in ("probe_down", "probe_xp", "probe_ym"):
        assert runner.run(routine, {"max_z_travel": "15"})
        end = time.time() + 10
        while runner.busy and time.time() < end:
            pump(qapp, 0.02)
    assert results == [{"z": "15"}]
    assert failures[0] == "no contact" and "ended without a result" in failures[1]
    assert blocked == ["block", "unblock"] * 3


# --- undo everywhere ----------------------------------------------------------------------------------------

def test_zero_all_from_the_pendant_is_one_undo(page, shell):
    m = page.machine
    before = list(m.wcs_offsets["G54"])
    shell.run_quick_action("zero_all")
    assert len(m.offset_history) == 1 and m.offset_history[0]["label"] == "Zero all"
    m.undo_offsets()
    assert m.wcs_offsets["G54"] == pytest.approx(before)


def test_fixture_loads_can_be_undone(page, shell, tmp_path, monkeypatch):
    from milo_ui.pages import offsets
    m = page.machine
    monkeypatch.setattr(offsets, "FIXTURE_FILE", str(tmp_path / "fixtures.json"))
    offsets.save_fixtures({"vise": {"x": 10.0, "y": 20.0, "z": -30.0}}, str(tmp_path / "fixtures.json"))
    off_page = shell.pages["offsets"]
    before = list(m.wcs_offsets["G54"])
    assert m.apply_offsets("G54", {"X": 10.0, "Y": 20.0, "Z": -30.0}, label="Load fixture vise")
    assert m.wcs_offsets["G54"] == pytest.approx([10.0, 20.0, -30.0])
    off_page.refresh()
    newest = off_page.history_list.itemAt(0).widget()
    assert "Load fixture vise" in [w.text() for w in newest.findChildren(type(off_page.status_text))]
    off_page.undo()
    assert m.wcs_offsets["G54"] == pytest.approx(before)
