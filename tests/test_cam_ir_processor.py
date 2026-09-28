"""Tests for the CAM IR processor's machine checks and output handling"""

import os
import time

import pytest

from cam_ir_processor import CAM_IR_AVAILABLE, CAMIRProcessor

pytestmark = pytest.mark.skipif(not CAM_IR_AVAILABLE, reason="cam_ir library not importable")


def _ir(rpm=2000, tool=8, diameter=6.0, **op_overrides):
    op = {"op": "profile_2d", "tool": tool, "circle": {"center": [50, 50], "diameter": 40}, "side": "outside",
          "top_z": 0, "bottom_z": -2, "stepdown": 1, "feed_xy": 300, "feed_z": 100, "rpm": rpm}
    op.update(op_overrides)
    return {"version": "1.0", "units": "mm", "stock": {"min": [0, 0, -10], "max": [100, 100, 0]},
            "clearance_z": 10, "safe_z": 5,
            "tools": [{"tool": tool, "type": "endmill", "diameter": diameter, "flutes": 4}],
            "ops": [op], "post": {"dialect": "linuxcnc", "program_number": 1, "spindle": "CW"}}


def _run(tmp_path, ir, **options):
    logs = []
    path, _, error = CAMIRProcessor(log_callback=logs.append).process_ir_to_gcode(
        ir, output_dir=str(tmp_path), **options)
    return path, error, logs


def test_generates_gcode(tmp_path):
    path, error, _ = _run(tmp_path, _ir(), tools={8: 6.35}, max_rpm=3000)
    assert error is None
    assert os.path.dirname(path) == str(tmp_path)
    assert "S2000 M3" in open(path).read()


def test_tool_table_diameter_replaces_ai_guess(tmp_path):
    path, error, logs = _run(tmp_path, _ir(diameter=6.0), tools={8: 6.35})
    assert error is None
    assert any("using tool table diameter 6.35 instead of 6" in line for line in logs)


def test_unknown_tool_rejected(tmp_path):
    _, error, _ = _run(tmp_path, _ir(tool=3), tools={8: 6.35, 5: None})
    assert "Tool 3 is not a usable tool" in error and "T5, T8" in error


def test_tool_with_unknown_diameter_is_accepted(tmp_path):
    _, error, _ = _run(tmp_path, _ir(), tools={8: None})
    assert error is None


def test_rpm_above_spindle_max_rejected(tmp_path):
    _, error, _ = _run(tmp_path, _ir(rpm=12000), max_rpm=3000)
    assert "rpm 12000 is above the spindle maximum of 3000" in error


def test_inch_ir_uses_converted_table_diameter(tmp_path):
    ir = _ir(diameter=0.2)
    ir["units"] = "inch"
    ir["stock"] = {"min": [0, 0, -1], "max": [4, 4, 0]}
    ir["ops"][0]["circle"] = {"center": [2, 2], "diameter": 1}
    ir["ops"][0].update(bottom_z=-0.1, stepdown=0.05, feed_xy=10, feed_z=5)
    _, error, logs = _run(tmp_path, ir, tools={8: 6.35}, machine_units="mm")
    assert error is None
    assert any("diameter 0.25 instead of 0.2" in line for line in logs)


def test_prune_keeps_newest(tmp_path):
    for i in range(5):
        for ext in (".ngc", ".json"):
            p = tmp_path / f"ai_toolpath_2026010{i}{ext}"
            p.write_text("x")
            os.utime(p, (time.time() + i, time.time() + i))
    CAMIRProcessor().prune_outputs(str(tmp_path), keep=2)
    assert sorted(os.listdir(tmp_path)) == [
        "ai_toolpath_20260103.json", "ai_toolpath_20260103.ngc",
        "ai_toolpath_20260104.json", "ai_toolpath_20260104.ngc",
    ]


@pytest.mark.parametrize("text", [
    '{"a": 1}',
    'Here you go:\n```json\n{"a": 1}\n```\nDone.',
    'prose {"a": 1} more prose',
])
def test_extract_json(text):
    assert CAMIRProcessor().extract_json_from_response(text) == {"a": 1}


def test_extract_json_rejects_arithmetic():
    assert CAMIRProcessor().extract_json_from_response('{"points": [[50 + 30, 50]]}') is None
