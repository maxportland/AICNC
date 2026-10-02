"""Tests for rewriting a program's speeds and feeds"""

import pytest

from feeds_adjust import Unsupported, analyze, apply

PROGRAM = """\
(hand-written test part)
G21 G90 G94
T1 M6 (quarter inch endmill)
S2000 M3
G0 X0 Y0 Z5
G1 Z-1 F100 (plunge)
G1 X20 F300
Y20
G2 X0 Y20 I-10 J0
G0 Z5
T2 M6
S1000 M3
G81 X10 Y10 Z-5 R2 F80
X20
G80
T3 M6 ; tap
S500 M3
G84 X5 Y5 Z-8 R2 F625
G80
M30
"""


def test_analyze_finds_each_tools_usual_values():
    uses, units = analyze(PROGRAM)
    assert units == "mm"
    assert uses[1].summary() == {"rpm": 2000, "feed": 300, "plunge": 100}
    assert uses[2].summary() == {"rpm": 1000, "feed": None, "plunge": 80}  # drilling cycle = plunge
    assert uses[3].locked


def test_apply_scales_speeds_and_feeds_per_tool():
    new, changes = apply(PROGRAM, {1: {"rpm": 3000, "feed": 600, "plunge": 150},
                                   2: {"rpm": 1500, "plunge": 120}, 3: {"rpm": 900}})
    lines = new.splitlines()
    assert "S3000 M3" in lines[3]
    assert lines[5] == "G1 Z-1 F150 (plunge)"          # comment kept
    assert lines[6] == "G1 X20 F600"
    assert lines[7] == "Y20"                            # modal feed still 600
    assert lines[8] == "G2 X0 Y20 I-10 J0"
    assert lines[11] == "S1500 M3"
    assert lines[12] == "G81 X10 Y10 Z-5 R2 F120"
    assert lines[13] == "X20"
    assert "S500 M3" in new and "G84 X5 Y5 Z-8 R2 F625" in new  # the tap is left alone
    assert changes[1] == {"rpm": (2000, 3000), "feed": (300, 600), "plunge": (100, 150)}
    assert 3 not in changes


def test_modal_feed_set_on_a_plunge_is_split_from_the_cutting_feed():
    """One F serves both a plunge and the cut after it: the new program needs both explicitly"""
    program = "T1 M6\nS1000 M3\nG1 Z-1 F200\nX10\nX20\nG0 Z5\nG1 Z-1\nX0\n"
    new, _ = apply(program, {1: {"feed": 400, "plunge": 100}})
    assert new.splitlines()[2:] == ["G1 Z-1 F100", "X10 F400", "X20", "G0 Z5", "G1 Z-1 F100", "X0 F400"]


def test_limits_are_respected():
    new, changes = apply(PROGRAM, {1: {"rpm": 24000, "feed": 9000}}, max_rpm=3000, max_feed=2500)
    assert "S3000 M3" in new and "G1 X20 F2500" in new
    assert changes[1]["rpm"] == (2000, 3000)


def test_feed_only_lines_move_to_where_the_feed_is_used():
    program = "T1 M6\nS1000 M3\nF250\nG1 X10\nG0 X0 F999\nG1 X5\n"
    new, _ = apply(program, {1: {"feed": 500}})
    # F999 on the rapid still sets the feed for the next cut, so that cut keeps its (scaled) feed
    assert new.splitlines()[2:] == ["", "G1 X10 F500", "G0 X0", "G1 X5 F1998"]


@pytest.mark.parametrize("line", ["#1 = 300", "G1 X[#1 * 2]", "o100 sub", "G93 G1 X10 F2", "G95 G1 X1 F0.1"])
def test_programs_it_cannot_follow_are_refused(line):
    with pytest.raises(Unsupported):
        analyze(f"T1 M6\n{line}\n")


def test_a_generated_program_round_trips(tmp_path):
    from cam_ir_processor import CAMIRProcessor, CAM_IR_AVAILABLE
    if not CAM_IR_AVAILABLE:
        pytest.skip("cam_ir not importable")
    ir = {"version": "1.0", "units": "mm", "stock": {"min": [0, 0, -10], "max": [100, 100, 0]},
          "clearance_z": 10, "safe_z": 5, "tools": [{"tool": 2, "type": "endmill", "diameter": 6.35, "flutes": 1}],
          "ops": [{"op": "pocket_2d", "tool": 2, "circle": {"center": [50, 50], "diameter": 30}, "top_z": 0,
                   "bottom_z": -2, "stepdown": 1, "stepover": 2.5, "strategy": "offset",
                   "feed_xy": 232.8, "feed_z": 116.4, "rpm": 3000}],
          "post": {"dialect": "linuxcnc", "program_number": 1, "spindle": "CW"}}
    path, _, error = CAMIRProcessor().process_ir_to_gcode(ir, output_dir=str(tmp_path))
    assert error is None
    text = open(path).read()
    uses, _ = analyze(text)
    assert uses[2].summary() == {"rpm": 3000, "feed": 232.8, "plunge": 116.4}
    new, _ = apply(text, {2: {"feed": 465.6, "plunge": 232.8}})
    after, _ = analyze(new)
    assert after[2].summary() == {"rpm": 3000, "feed": 465.6, "plunge": 232.8}
    # Only F words changed
    strip = lambda t: [__import__("re").sub(r"\s*F[\d.]+", "", l) for l in t.splitlines()]
    assert strip(new) == strip(text)


# --- routing and the engine ---

def test_router_asks_for_the_material():
    from intent_router import IntentRouter
    result = IntentRouter._normalize({"intent": "adjust_feeds"}, "{}")
    assert result["intent"] == "unclear" and "material" in result["answer"]
    result = IntentRouter._normalize({"intent": "adjust_feeds", "material": "6061 aluminum"}, "{}")
    assert result["intent"] == "adjust_feeds" and result["material"] == "6061 aluminum"


def _engine(tmp_path, stat):
    from milo_engine import MiloEngine
    from test_engine import Recorder
    engine = MiloEngine(listener=Recorder(), stat_getter=lambda: stat, settings_path=str(tmp_path / "cfg.json"),
                        config_dir=str(tmp_path))
    engine.load_gcode = lambda path: engine.loaded.append(path)
    engine.loaded = []
    return engine


def test_no_program_loaded(tmp_path, qapp):
    from conftest import FakeStat
    engine = _engine(tmp_path, FakeStat(file=""))
    engine._adjust_feeds("walnut")
    lines = [args[0] for name, args in engine.listener.events if name == "on_message"]
    assert lines[-1] == "[MILO] There's no program loaded to adjust. Open one first."


def test_adjusted_program_is_reported_and_loaded(tmp_path, qapp):
    from conftest import FakeStat
    engine = _engine(tmp_path, FakeStat())
    engine._on_feeds_adjusted("6061 aluminum", {
        "path": "/x/part_6061-aluminum.ngc", "original": "/x/part.ngc", "units": "mm",
        "changes": {1: {"rpm": (2000, 3000), "feed": (300, 450)}}, "why": {1: "0.05 mm chip load."},
        "notes": "Use mist.", "skipped": [3]})
    assert engine.loaded == ["/x/part_6061-aluminum.ngc"]
    message = [args[0] for name, args in engine.listener.events if name == "on_message"][-1]
    assert "T1: 3,000 rpm (was 2,000), feed 450 mm/min (was 300). 0.05 mm chip load." in message
    assert "the original is unchanged" in message and "T3" in message
    ready = [args[0] for name, args in engine.listener.events if name == "on_program_ready"][-1]
    assert ready["name"] == "part_6061-aluminum.ngc"
