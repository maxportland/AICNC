"""Threads in AI programs: the CAM prompt teaches thread_mill, and the plunge-style 'thread' op is refused"""

import pytest

from cam_ir_processor import CAMIRProcessor
from milo_engine import CAM_PROMPT_HEAD, CAM_PROMPT_SCHEMA

TOOLS = [{"tool": 2, "type": "drill", "diameter": 8.5, "flutes": 2, "description": "8.5mm drill"},
         {"tool": 3, "type": "endmill", "diameter": 6.0, "flutes": 3, "description": "6mm thread mill"}]


def _ir(*ops):
    return {"version": "1.0", "units": "mm", "stock": {"min": [0, 0, -15], "max": [40, 40, 0]},
            "clearance_z": 10, "safe_z": 5, "tools": TOOLS, "ops": list(ops),
            "post": {"dialect": "linuxcnc", "program_number": 1, "spindle": "CW"}}


def _process(ir, tmp_path):
    processor = CAMIRProcessor()
    path, _, error = processor.process_ir_to_gcode(ir, output_dir=str(tmp_path), machine_units="mm", max_rpm=3000,
                                                   tools={2: 8.5, 3: 6.0})
    return processor, path, error


def test_prompt_teaches_thread_milling_not_the_plunge():
    prompt = CAM_PROMPT_HEAD + CAM_PROMPT_SCHEMA
    assert '- thread_mill: { op: "thread_mill"' in prompt
    assert "major_diameter" in prompt and "1 / TPI" in prompt
    assert "drill (or bore/pocket) op before the thread_mill" in prompt
    assert '- thread: { op: "thread"' not in prompt  # no longer offered
    assert 'Never use an op named "thread"' in prompt


def test_a_thread_op_is_sent_back_with_the_fix(tmp_path):
    """Milo returns rejections to the AI, so the message must say what to use instead"""
    thread = {"op": "thread", "tool": 3, "points": [[20, 20]], "top_z": 0, "bottom_z": -10, "pitch": 1.5,
              "rpm": 1000}
    _, path, error = _process(_ir(thread), tmp_path)
    assert path is None
    assert "straight plunge, not thread milling" in error and "thread_mill" in error


def test_drilled_and_thread_milled_hole(tmp_path):
    """What the prompt asks for: an M10x1.5 hole drilled to 8.5, then thread milled"""
    drill = {"op": "drill", "tool": 2, "points": [[20, 20]], "top_z": 0, "bottom_z": -12, "feed": 100, "rpm": 1000}
    thread = {"op": "thread_mill", "tool": 3, "points": [[20, 20]], "major_diameter": 10, "pitch": 1.5,
              "top_z": 0, "bottom_z": -10, "radial_passes": 2, "feed_xy": 150, "rpm": 2000}
    processor, path, error = _process(_ir(drill, thread), tmp_path)
    assert error is None and path
    assert processor.last_review["problems"] == []
    gcode = open(path).read()
    assert "G3" in gcode and " I" in gcode  # helical arcs, climbing for a right-hand internal thread
