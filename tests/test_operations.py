"""Tests for parameter-driven operations: CAM IR from form values, the Operations page, the engine"""

import pytest

import operations as ops
from cam_ir_processor import CAMIRProcessor

TOOLS = [
    {"tool": 1, "type": "endmill", "diameter": 6.35, "flutes": 2, "description": "1/4 endmill", "usable": True},
    {"tool": 2, "type": "drill", "diameter": 5.0, "flutes": 2, "description": "5mm drill", "usable": True},
    {"tool": 3, "type": "endmill", "diameter": 6.0, "flutes": 3, "description": "6mm thread mill", "usable": True},
    {"tool": 4, "type": "endmill", "diameter": 3.0, "flutes": 1, "description": "60 deg engraving v-bit",
     "usable": True},
]


def _variants():
    """Every operation with its defaults, and once more for each other choice it offers"""
    for op in ops.OPERATIONS:
        yield op.key, {}
        for p in op.all_params():
            if p.kind == ops.CHOICE and p.key != "coolant":
                for choice in p.choices[1:]:
                    yield op.key, {p.key: choice}


@pytest.mark.parametrize("key,extra", list(_variants()))
def test_every_operation_makes_a_clean_program(key, extra, tmp_path):
    """Defaults go through the real CAM processor: valid, planned, posted, and the simulation finds nothing"""
    op = ops.BY_KEY[key]
    values = dict(ops.default_values(op, "mm", 1000), **extra)
    tool = ops.default_tool(TOOLS, op.tool_hint)
    ir = ops.build_program(key, values, tool, "mm", 3000)
    processor = CAMIRProcessor()
    path, _, error = processor.process_ir_to_gcode(ir, output_dir=str(tmp_path), machine_units="mm", max_rpm=3000,
                                                   tools={t["tool"]: t["diameter"] for t in TOOLS})
    assert error is None and path
    assert processor.last_review["problems"] == []


@pytest.mark.parametrize("key,hint_tool", [("face", 1), ("hole", 2), ("grid", 2), ("circle", 2), ("pocket", 1),
                                           ("thread", 3), ("text", 4)])
def test_default_tools_fit_the_operation(key, hint_tool):
    assert ops.default_tool(TOOLS, ops.BY_KEY[key].tool_hint)["tool"] == hint_tool


def test_the_tool_in_the_spindle_is_preferred_when_it_fits():
    other_endmill = dict(TOOLS[0], tool=9, description="1/8 endmill")
    tools = TOOLS + [other_endmill]
    assert ops.default_tool(tools, "endmill", in_spindle=9)["tool"] == 9
    assert ops.default_tool(tools, "drill", in_spindle=9)["tool"] == 2  # an end mill doesn't drill
    assert ops.default_tool([], "endmill") is None


def _build(key, tool=1, **values):
    op = ops.BY_KEY[key]
    v = dict(ops.default_values(op, "mm", 1000), **values)
    return ops.build_program(key, v, next(t for t in TOOLS if t["tool"] == tool), "mm", 3000)


def test_grid_snakes_through_the_rows():
    ir = _build("grid", tool=2, columns=3, rows=2, x=10, y=5, x_spacing=10, y_spacing=20)
    assert ir["ops"][0]["points"] == [[10, 5], [20, 5], [30, 5], [30, 25], [20, 25], [10, 25]]


def test_slot_cuts_back_and_forth_in_equal_layers():
    ir = _build("slot", depth=2.5, stepdown=1.0, start_x=0, start_y=0, end_x=40, end_y=0)
    layers = ir["ops"]
    assert [o["depth"] for o in layers] == pytest.approx([2.5 / 3, 5 / 3, 2.5])
    assert layers[0]["path"] == [[0, 0], [40, 0]] and layers[1]["path"] == [[40, 0], [0, 0]]


def test_arc_slot_goes_counter_clockwise_from_start_to_end():
    path = _build("slot", shape="Arc", x=0, y=0, radius=10, start_angle=0, end_angle=90)["ops"][0]["path"]
    assert path[0] == pytest.approx([10, 0]) and path[-1] == pytest.approx([0, 10], abs=1e-9)
    assert all(abs((p[0] ** 2 + p[1] ** 2) ** 0.5 - 10) < 1e-9 for p in path)


def test_facing_starts_beside_the_stock():
    area = _build("face", x=0, y=0, width=100, length=50)["ops"][0]["area"]
    margin = 6.35 / 2 + 1.0
    assert area == {"min": [-margin, -margin], "max": [100 + margin, 50 + margin]}


def test_thread_and_coolant_fields():
    ir = _build("thread", tool=3, major_diameter=12, pitch=1.75, length=8, hand="Left", thread_type="External",
                coolant="Mist")
    op = ir["ops"][0]
    assert (op["op"], op["major_diameter"], op["pitch"], op["bottom_z"]) == ("thread_mill", 12, 1.75, -8)
    assert (op["hand"], op["thread_type"], op["coolant"]) == ("left", "external", "mist")


def test_stock_covers_the_cut():
    ir = _build("circle", tool=2, x=50, y=40, circle_diameter=30, depth=6)
    assert ir["stock"]["min"][0] < 35 and ir["stock"]["max"][0] > 65
    assert ir["stock"]["max"][2] == 0 and ir["stock"]["min"][2] < -6


@pytest.mark.parametrize("key,values,message", [
    ("pocket", {"width": 5}, "wider than the tool"),
    ("hole", {"depth": 0}, "Hole depth"[:0] + "Depth must be greater than zero"),
    ("grid", {"columns": 2, "x_spacing": 0}, "Spacing can't be zero"),
    ("slot", {"end_x": 0, "end_y": 0}, "no length"),
    ("text", {"text": "  "}, "Type the text"),
    ("face", {"rpm": 5000}, "above the machine's maximum"),
    ("face", {"safe_z": -1}, "safe height must be above"),
    ("face", {"feed": 0}, "Feed must be greater than zero"),
])
def test_bad_parameters_are_explained(key, values, message):
    with pytest.raises(ops.OperationError, match=message):
        _build(key, tool=2 if key in ("hole", "grid") else 1, **values)


def test_a_tool_is_required():
    with pytest.raises(ops.OperationError, match="Choose a tool"):
        ops.build_program("face", ops.default_values(ops.BY_KEY["face"]), None)


def test_inch_machines_get_inch_defaults():
    values = ops.default_values(ops.BY_KEY["pocket"], "inch")
    assert values["width"] == pytest.approx(1.57) and values["feed"] == pytest.approx(11.8)
    assert values["stepover"] == 40 and values["rpm"] == 1000  # percent and rpm aren't lengths


def test_choices_show_only_their_parameters():
    pocket = ops.BY_KEY["pocket"]
    keys = {p.key for p in pocket.visible(dict(ops.default_values(pocket), shape="Circle"))}
    assert "diameter" in keys and "width" not in keys


def test_long_tool_descriptions_are_shortened():
    tool = dict(TOOLS[0], description="Amana 51417-K 0.1875in 1FL flat end mill with a long name")
    text = ops.describe_tool(tool, "mm")
    assert text.startswith("T1 · ⌀6.35 mm · Amana") and text.endswith("…") and len(text) < 60


# --- the page -----------------------------------------------------------------------------------

from test_milo_ui import shell  # noqa: E402,F401  (fixture)


def test_operations_sits_between_program_and_tools(shell):
    keys = list(shell.pages)
    assert keys.index("operations") == keys.index("program") + 1 == keys.index("tools") - 1
    assert shell.rail.buttons["operations"].text() == "Operations"


def test_page_lists_every_operation_and_generates(shell, monkeypatch):
    page = shell.pages["operations"]
    page.tools = [t for t in TOOLS]
    monkeypatch.setattr(page, "_load_tools", lambda: None)
    assert list(page.rows) == [op.key for op in ops.OPERATIONS]
    page.select("circle")
    assert page.values["tool"] == 2  # a drill for holes
    assert "count" in page.value_rows and "plunge" not in page.value_rows
    sent = []
    monkeypatch.setattr(shell.engine, "generate_operation",
                        lambda ir, title, on_done=None: (sent.append((ir, title)), on_done("/x/p.ngc", None), True)[2])
    page.generate()
    ir, title = sent[0]
    assert title == "Hole Circle" and ir["ops"][0]["bolt_circle"]["count"] == 6
    assert "loaded" in page.status.text() and page.generate_button.isEnabled()


def test_page_explains_bad_parameters_without_generating(shell, monkeypatch):
    page = shell.pages["operations"]
    page.tools = [t for t in TOOLS]
    monkeypatch.setattr(page, "_load_tools", lambda: None)
    page.select("pocket")
    page.values["width"] = 2.0
    sent = []
    monkeypatch.setattr(shell.engine, "generate_operation", lambda *a, **k: sent.append(a) or True)
    page.generate()
    assert sent == [] and "wider than the tool" in page.status.text()


def test_choice_rebuilds_the_form_and_values_are_remembered(shell, monkeypatch):
    page = shell.pages["operations"]
    page.tools = [t for t in TOOLS]
    monkeypatch.setattr(page, "_load_tools", lambda: None)
    page.select("slot")
    shape = next(p for p in page.current.all_params() if p.key == "shape")
    page._set(shape, "Arc", rebuild=True)
    assert "radius" in page.value_rows and "end_x" not in page.value_rows
    page.select("face")
    page.select("slot")
    assert page.values["shape"] == "Arc"  # kept between visits
    page._reset()
    assert page.values["shape"] == "Straight"


def test_page_refuses_while_a_program_runs(shell):
    page = shell.pages["operations"]
    m = shell.machine
    m.set_estop(False)
    m.set_power(True)
    m.open_program(__file__)
    m.run()
    page.generate()
    assert page.status.text() == ""  # nothing started
    m.abort()


# --- the engine ---------------------------------------------------------------------------------

def _engine(tmp_path):
    from milo_engine import MiloEngine
    from test_engine import Recorder
    from conftest import FakeStat
    listener = Recorder()
    loaded = []
    engine = MiloEngine(listener=listener, stat_getter=lambda: FakeStat(), settings_path=str(tmp_path / "cfg.json"),
                        config_dir=str(tmp_path))
    engine.load_program = loaded.append
    return engine, listener, loaded


def test_engine_loads_a_finished_operation(tmp_path, qapp):
    engine, listener, loaded = _engine(tmp_path)
    ir = _build("hole", tool=2)
    results = []
    engine._on_operation_finished(ir, "Single Hole", "/x/op.ngc", None, {"problems": [], "image": None},
                                  lambda path, error: results.append((path, error)))
    assert loaded == ["/x/op.ngc"] and results == [("/x/op.ngc", None)]
    cards = [args[0] for name, args in listener.events if name == "on_program_ready"]
    assert cards and cards[0]["ops"][0]["name"] == "Drill"


def test_engine_reports_a_rejected_operation(tmp_path, qapp):
    engine, listener, loaded = _engine(tmp_path)
    results = []
    engine._on_operation_finished({}, "Pocket", None, "CAM IR doesn't fit the machine: T1 is not usable\nTraceback...",
                                  None, lambda path, error: results.append((path, error)))
    assert loaded == [] and results[0][0] is None
    messages = [args[0] for name, args in listener.events if name == "on_message"]
    assert any(m.startswith("[ERROR] Pocket: CAM IR doesn't fit") and "Traceback" not in m for m in messages)


def test_engine_wont_start_while_busy(tmp_path, qapp, monkeypatch):
    engine, _, _ = _engine(tmp_path)
    monkeypatch.setattr(engine, "_busy_working", lambda: True)
    assert engine.generate_operation(_build("hole", tool=2), "Single Hole") is False
    assert engine.cam_worker is None
