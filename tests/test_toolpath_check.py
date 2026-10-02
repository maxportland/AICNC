"""Tests for the cut simulation that checks generated programs"""

import os

import numpy as np
import pytest

from cam_ir_processor import CAM_IR_AVAILABLE, CAMIRProcessor

pytestmark = pytest.mark.skipif(not CAM_IR_AVAILABLE, reason="cam_ir library not importable")


def _ir(ops):
    return {"version": "1.0", "units": "mm", "stock": {"min": [0, 0, -10], "max": [100, 100, 0]},
            "clearance_z": 10, "safe_z": 5,
            "tools": [{"tool": 1, "type": "endmill", "diameter": 3.0, "flutes": 2},
                      {"tool": 2, "type": "endmill", "diameter": 6.0, "flutes": 2}],
            "ops": ops, "post": {"dialect": "linuxcnc", "program_number": 1, "spindle": "CW"}}


def _pocket(center, diameter, bottom, tool=2):
    return {"op": "pocket_2d", "tool": tool, "circle": {"center": center, "diameter": diameter},
            "top_z": 0, "bottom_z": bottom, "stepdown": 1, "stepover": 2.5, "strategy": "offset",
            "feed_xy": 300, "feed_z": 100, "rpm": 3000}


def _engrave(path, depth, top=0):
    return {"op": "engrave", "tool": 1, "path": path, "top_z": top, "depth": depth,
            "feed_xy": 300, "feed_z": 100, "rpm": 3000}


def _load(data):
    from cam_ir.ir_types import CAMIR
    return CAMIR.model_validate(data)


def _review(tmp_path, ops):
    processor = CAMIRProcessor()
    path, _, error = processor.process_ir_to_gcode(_ir(ops), output_dir=str(tmp_path))
    assert error is None, error
    return processor.last_review


def test_a_clean_program_has_no_problems_and_a_picture(tmp_path):
    review = _review(tmp_path, [_pocket([50, 50], 30, -2), _engrave([[10, 10], [30, 10]], 1)])
    assert review["problems"] == []
    assert os.path.exists(review["image"])
    assert review["operations"][0].startswith("1. pocket_2d T2 circle ⌀30 at [50, 50]")


def test_features_inside_a_deeper_pocket_are_air_cuts(tmp_path):
    """The smiley face: the face pocket swallows the eyes and the mouth"""
    review = _review(tmp_path, [
        _pocket([50, 50], 76, -2),
        _pocket([35, 65], 10, -2, tool=1),
        _engrave([[30, 35], [50, 30], [70, 35]], 1),
    ])
    problems = review["problems"]
    assert len(problems) == 2
    assert problems[0].startswith("Operation 2 (pocket_2d T1 circle ⌀10 at [35, 65]) removes no material")
    assert "operation 1 already cut that area" in problems[0]
    assert problems[1].startswith("Operation 3 (engrave T1")


def test_features_cut_deeper_than_the_pocket_are_fine(tmp_path):
    review = _review(tmp_path, [_pocket([50, 50], 76, -2), _pocket([35, 65], 10, -4, tool=1)])
    assert review["problems"] == []


def test_an_operation_above_the_stock_never_reaches_it(tmp_path):
    review = _review(tmp_path, [_pocket([50, 50], 30, -2), _engrave([[10, 10], [30, 10]], 1, top=5)])
    # The engrave starts at Z5 and goes 1 deep: all above the stock's top at Z0
    assert review["problems"] == [p for p in review["problems"] if p.startswith("Operation 2")]
    assert "never reaches the material" in review["problems"][0]


def test_stock_box_not_at_the_work_zero():
    """The smiley's stock was 0..10 in Z while every operation started at Z0"""
    import toolpath_check as tc
    ir = _load(dict(_ir([_pocket([50, 50], 30, -2)]), stock={"min": [0, 0, 0], "max": [100, 100, 10]}))
    assert tc.surface_z(ir) == 0


def test_rapids_through_material_are_reported():
    import toolpath_check as tc
    from cam_ir.planner.moves import Move
    ir = _load(_ir([_pocket([50, 50], 20, -2)]))
    moves = [[Move(type="rapid", x=20, y=50, z=5), Move(type="linear", z=-2, feed=100),
              Move(type="rapid", x=80, y=50)]]
    sim = tc.simulate(ir, moves)
    problems = tc.find_problems(sim)
    assert problems and "rapid (G0) moves through material" in problems[0]


def test_arcs_are_followed():
    import toolpath_check as tc
    from cam_ir.planner.moves import Move
    points = list(tc._points((10, 0, 0), Move(type="arc_ccw", x=-10, y=0, i=-10, j=0), 0.5))
    assert all(abs(np.hypot(x, y) - 10) < 1e-6 for x, y, _ in points)
    assert max(y for _, y, _ in points) > 9.9  # went over the top (counter-clockwise), not under
    full = list(tc._points((10, 0, 0), Move(type="arc_cw", x=10, y=0, i=-10, j=0), 0.5))
    assert len(full) > 100  # a full circle, not a zero-length arc


# --- the engine's review loop ---

def _engine(tmp_path, monkeypatch):
    import milo_engine
    from test_engine import Recorder
    monkeypatch.setattr(milo_engine, "VISUAL_REVIEW", False)
    engine = milo_engine.MiloEngine(listener=Recorder(), settings_path=str(tmp_path / "cfg.json"),
                                    config_dir=str(tmp_path))
    engine.message_history = [{"role": "system", "content": "x"}, {"role": "user", "content": "smiley"}]
    sent, loaded = [], []
    engine.send_openai = lambda: sent.append(engine.message_history[-1]["content"])
    engine.load_gcode = loaded.append
    return engine, sent, loaded


def test_problems_go_back_to_the_ai_once(tmp_path, monkeypatch, qapp):
    engine, sent, loaded = _engine(tmp_path, monkeypatch)
    review = {"problems": ["Operation 2 removes no material"], "image": None, "operations": []}
    engine._on_cam_finished("/x/a.ngc", None, review)
    assert loaded == [] and len(sent) == 1
    assert "Operation 2 removes no material" in sent[0] and "corrected CAM IR" in sent[0]
    # The fixed program still has a problem: it's loaded, with a heads-up on the card
    engine._on_cam_finished("/x/b.ngc", None, review)
    assert loaded == ["/x/b.ngc"] and len(sent) == 1
    ready = [args[0] for name, args in engine.listener.events if name == "on_program_ready"]
    assert ready[-1]["problems"] == ["Operation 2 removes no material"]


def test_a_clean_program_loads_straight_away(tmp_path, monkeypatch, qapp):
    engine, sent, loaded = _engine(tmp_path, monkeypatch)
    engine._on_cam_finished("/x/a.ngc", None, {"problems": [], "image": None, "operations": []})
    assert loaded == ["/x/a.ngc"] and sent == []
