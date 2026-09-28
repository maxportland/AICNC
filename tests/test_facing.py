"""Facing utility with step-down passes"""

import os
import re

import pytest

import tempfile

os.environ.setdefault("INI_FILE_NAME", os.path.join(os.path.dirname(os.path.dirname(__file__)), "Mesa7I96S.ini"))
# qtvcp's logger (re)creates its log file in CONFIG_DIR: keep it away from the real config's logs
os.environ["CONFIG_DIR"] = tempfile.mkdtemp(prefix="qtvcp-test-")
facing_utility = pytest.importorskip("facing_utility", reason="needs qtvcp")
from facing_utility import MAX_PASSES, format_z, pass_depths  # noqa: E402


def test_pass_depths():
    assert pass_depths(2.0, 3) == [0.0, -2.0, -4.0]
    assert pass_depths(0.5, 1) == [0.0]
    assert [format_z(z) for z in pass_depths(0.25, 3)] == ["0.0", "-0.25", "-0.5"]


def _setup(widget, passes="1", step="2", raster="rbtn_raster_0"):
    widget.init()
    widget.rbtn_mm.click()
    for name, value in (("size_x", "100"), ("size_y", "60"), ("spindle", "2000"), ("feedrate", "600"),
                        ("tool", "10"), ("stepover", "5")):
        getattr(widget, f"lineEdit_{name}").setText(value)
    getattr(widget, raster).click()
    if hasattr(widget, "lineEdit_passes"):
        widget.lineEdit_stepdown.setText(step)
        widget.lineEdit_passes.setText(passes)
    widget.validate()


def _program(widget, tmp_path, name):
    path = tmp_path / name
    widget.calculate_toolpath(str(path))
    return path.read_text()


@pytest.mark.parametrize("raster", ["rbtn_raster_0", "rbtn_raster_45", "rbtn_raster_90"])
def test_one_pass_is_identical_to_the_original(qapp, tmp_path, raster):
    from qtvcp.lib.gcode_utility.facing import Facing as Original
    original, extended = Original(), facing_utility.Facing()
    _setup(original, raster=raster)
    _setup(extended, passes="1", raster=raster)
    assert extended.valid
    assert _program(extended, tmp_path, "new.ngc") == _program(original, tmp_path, "old.ngc")


@pytest.mark.parametrize("raster", ["rbtn_raster_0", "rbtn_raster_45", "rbtn_raster_90"])
def test_three_passes(qapp, tmp_path, raster):
    widget = facing_utility.Facing()
    _setup(widget, passes="3", step="2", raster=raster)
    assert widget.valid
    program = _program(widget, tmp_path, "multi.ngc")
    plunges = re.findall(r"G1 Z(\S+) F300.0", program)
    assert plunges == ["0.0", "-2", "-4"]
    assert "(3 passes, 2.0 step down: Z 0.0, -2, -4)" in program
    # every pass after the first starts back at the corner, from above the work
    blocks = program.split("(Pass ")[1:]
    assert len(blocks) == 3
    for block in blocks[1:]:
        moves = [line.split(" ", 1)[1] for line in block.splitlines() if line.startswith("N")][:3]
        assert moves == ["G0 Z10.0", "G0 X0.0 Y0.0", moves[2]] and moves[2].startswith("G1 Z-")
    # the raster itself is identical in each pass
    def raster_lines(block):
        lines = [l.split(" ", 1)[1] for l in block.splitlines() if l.startswith("N")]
        return lines[lines.index(next(l for l in lines if l.startswith("G1 Z"))) + 1:]
    assert raster_lines(blocks[0].split("G0 Z20.0")[0]) == raster_lines(blocks[1])
    # line numbers keep counting up; program ends normally
    numbers = [int(n) for n in re.findall(r"^N(\d+) ", program, re.M)]
    assert numbers == sorted(numbers) and len(set(numbers)) == len(numbers)
    assert program.rstrip().endswith("M2\n%")


@pytest.mark.parametrize("passes,step,valid", [
    ("1", "0", True),       # one pass doesn't need a step down
    ("3", "2", True),
    ("3", "0", False),      # several passes need one
    ("0", "2", False),
    ("", "2", False),
    (str(MAX_PASSES + 1), "1", False),
])
def test_validation(qapp, passes, step, valid):
    widget = facing_utility.Facing()
    _setup(widget, passes=passes, step=step)
    assert widget.valid == valid


def test_summary_spells_out_the_depths(qapp):
    widget = facing_utility.Facing()
    _setup(widget, passes="3", step="2")
    assert widget.lbl_passes_summary.text() == "3 passes at Z 0.0, -2, -4  ·  4 mm below Z0 in total"
    widget.lineEdit_passes.setText("1")
    assert widget.lbl_passes_summary.text() == "1 pass at Z0."


def test_init_can_run_twice_without_duplicate_connections(qapp):
    widget = facing_utility.Facing()
    widget.init()
    widget.init()  # qtvcp's forced-update calls init() again
    calls = []
    original = widget.validate
    widget.validate = lambda: calls.append(1) or original()
    widget.lineEdit_passes.setText("2")
    assert len(calls) <= 1
