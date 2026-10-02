"""Tests for the Offsets page: systems, zeroing any system, editing, copy, fixtures, history, G92, tool length"""

import os

import pytest

from milo_ui.pages import offsets
from test_milo_ui import shell  # noqa: F401  (fixture)


@pytest.fixture
def page(shell, monkeypatch, tmp_path):
    m = shell.machine
    m.set_estop(False)
    m.set_power(True)
    m.homed = {a: True for a in m.axes}
    m.pos_abs = [200.0, 90.0, -60.0]
    m._update_rel()
    m.offset_history = []
    monkeypatch.setattr(offsets, "FIXTURE_FILE", str(tmp_path / "fixtures.json"))
    # Confirmation sheets: take the first (doing) choice straight away
    monkeypatch.setattr(offsets.kit, "ActionSheet", ImmediateSheet)
    p = shell.pages["offsets"]
    p.refresh()
    p.refresh_fixtures()
    return p


class ImmediateSheet:
    def __init__(self, host, title, actions, subtitle=None, width=440):
        self.actions = actions

    def show_centered(self):
        self.actions[0][2]()

    show_at = show_centered


def test_systems_show_what_they_are(page):
    rows = page.rows
    assert rows["G54"].status.text() == "ACTIVE" and rows["G54"].origin.text() == "X 94.67  Y 84.35"
    assert rows["G55"].title.text() == "Not used" and rows["G55"].status.text() == ""
    assert not rows["G59.1"].isVisibleTo(page)
    page._toggle_more()
    assert rows["G59.1"].isVisibleTo(page)
    page.map.picked.emit("G55")
    assert page.selected == "G55" and "Not used yet" in page.status_text.text()
    assert page.activate_button.isVisibleTo(page)


def test_zero_another_system_at_the_tool_and_undo(page):
    m = page.machine
    page.select("G55")
    page.zero_here("XY")
    assert m.wcs_offsets["G55"][:2] == pytest.approx([200.0, 90.0]) and m.wcs == "G54"  # not made active
    assert page.rows["G55"].status.text() == "IN USE"
    assert page.axis_rows["X"].tool.text() == "0.000"
    page.undo()
    assert m.wcs_offsets["G55"] == pytest.approx([0.0, 0.0, 0.0])


def test_set_the_tool_to_a_value_and_type_an_origin(page):
    m = page.machine
    page.select("G56")
    page.set_value("X", 25.0)  # the tool reads X 25 in G56
    assert m.wcs_offsets["G56"][0] == pytest.approx(175.0)
    page.set_origin("Y", 40.5)  # G56's Y zero is at machine Y 40.5
    assert m.wcs_offsets["G56"][1] == pytest.approx(40.5)
    assert [e["label"] for e in m.offset_history] == ["Set X to 25 at the tool", "Type Y origin 40.5"]


def test_tool_length_counts_when_zeroing_z(page):
    m = page.machine
    m.tool, m.tool_length = 3, 45.0
    m.mdi("M61 Q3 G43")
    assert m.tool_length_applied and m.tool_offset_z == 45.0
    page.select("G54")
    page.zero_here("Z")
    # The tool's tip, 45 below the spindle reference, is Z0
    assert m.wcs_offsets["G54"][2] == pytest.approx(-60.0 - 45.0)
    assert page.axis_rows["Z"].tool.text() == "0.000"


def test_copy_clear_and_restore(page):
    m = page.machine
    g54 = list(m.wcs_offsets["G54"])
    page.select("G54")
    assert page.copy_to("G57")
    assert m.wcs_offsets["G57"] == pytest.approx(g54)
    page.select("G57")
    page._clear()
    assert m.wcs_offsets["G57"] == pytest.approx([0.0, 0.0, 0.0])
    # Restore the older entry: G57 as it was before the copy (unused)... and the newest is undoable
    page.restore(1)  # history: [copy, clear]; index 1 = before the clear
    assert m.wcs_offsets["G57"] == pytest.approx(g54)
    assert m.offset_history[-1]["label"].startswith("Restore G57")


def test_make_active_and_go_to(page):
    m = page.machine
    page.select("G54")
    page.copy_to("G55")
    page.select("G55")
    page.make_active()
    assert m.wcs == "G55" and page.rows["G55"].status.text() == "ACTIVE"
    sent = []
    m.mdi_lines = lambda lines: sent.append(lines) or True
    page.go_to()
    assert sent == [["G90 G53 G0 Z0", "G90 G53 G0 X94.6750 Y84.3500"]]
    del m.mdi_lines
    m.open_program(__file__)
    m.run()
    page.refresh()
    assert not page.action_buttons["zero_xy"].isEnabled()
    m.abort()


def test_names(page, shell):
    page.select("G55")
    shell.ask_text = lambda title, save, **kwargs: save("Right vise")
    page._rename()
    assert shell.prefs.get(offsets.NAMES_KEY) == {"G55": "Right vise"}
    assert page.rows["G55"].title.text() == "Right vise" and page.nickname.text() == "Right vise"


def test_fixtures_for_any_system(page, shell):
    m = page.machine
    m.wcs_rotation["G54"] = 1.5
    page.select("G54")
    shell.ask_text = lambda title, save, **kwargs: save("Left vise")
    page._save_fixture()
    saved = offsets.load_fixtures()["Left vise"]
    assert saved["rotation"] == 1.5 and saved["from"] == "G54" and saved["x"] == pytest.approx(94.675)
    # Old fixtures (no rotation) still load, into whichever system is picked
    fixtures = offsets.load_fixtures()
    fixtures["Old"] = {"x": 10.0, "y": 20.0, "z": -30.0}
    offsets.save_fixtures(fixtures)
    page.select("G58")
    page._use_fixture("Old")
    assert m.wcs_offsets["G58"] == pytest.approx([10.0, 20.0, -30.0]) and m.wcs_rotation["G58"] == 0.0
    page._use_fixture("Left vise")
    assert m.wcs_rotation["G58"] == 1.5


def test_g92_and_tool_length_warnings(page):
    m = page.machine
    m.g92 = [5.0, 0.0, 0.0]
    m.tool, m.tool_length_applied = 8, False
    page.refresh()
    assert "G92 shift is active (X +5.000 mm)" in page.g92_row.text.text() and page.g92_row.button.isVisibleTo(page)
    assert "isn't applied" in page.tool_row.text.text()
    page._clear_g92()
    page.tool_row._action()
    page.refresh()
    assert m.g92 == [0.0, 0.0, 0.0] and "No G92 shift" in page.g92_row.text.text()
    assert "is applied" in page.tool_row.text.text()


def test_map_shows_used_systems_fixtures_and_the_tool(page):
    page.select("G54")
    page.copy_to("G55")
    offsets.save_fixtures({"Jig": {"x": 300.0, "y": 50.0, "z": 0.0}})
    page.refresh()
    names = [pin["name"] for pin in page.map.pins]
    assert "G54" in names and "G55" in names and "G56" not in names
    assert page.map.fixtures == [("Jig", (300.0, 50.0))] and page.map.tool == (200.0, 90.0)


# --- the real machine: offsets written to systems LinuxCNC hasn't saved yet ---------------------------------

def test_written_offsets_win_until_linuxcnc_saves_them(qapp, tmp_path):
    from milo_ui.machine import MachineModel, QtvcpMachine
    from test_machine_safety import VAR_FILE
    from conftest import FakeStat
    import machine_safety
    var = tmp_path / "linuxcnc.var"
    var.write_text(VAR_FILE)
    ini = tmp_path / "machine.ini"
    ini.write_text("[RS274NGC]\nPARAMETER_FILE = linuxcnc.var\n")
    machine_safety._ini_cache.clear()
    m = QtvcpMachine.__new__(QtvcpMachine)
    MachineModel.__init__(m)
    m.STATUS = type("S", (), {"stat": FakeStat(g5x_index=1, g5x_offset=[230.0, 8.4858, -147.9] + [0.0] * 6,
                                               ini_filename=str(ini))})()
    assert m.current_offset("G55")[0] == [0.0, 0.0, 0.0]
    m._written("G55", [1.0, 2.0, 3.0], 0.0)
    assert m.current_offset("G55") == ([1.0, 2.0, 3.0], 0.0)
    future = os.path.getmtime(var) + 10
    os.utime(var, (future, future))  # LinuxCNC rewrote the file: it's the truth again
    assert m.current_offset("G55")[0] == [0.0, 0.0, 0.0]


def test_milo_knows_the_named_systems(page, shell, tmp_path, qapp):
    page.select("G54")
    page.copy_to("G55")
    shell.prefs.set(offsets.NAMES_KEY, {"G55": "Right vise"})
    text = page.describe_for_milo()
    assert 'G54 (active): origin at machine X94.675 Y84.350 Z-112.838' in text
    assert 'G55 "Right vise": origin at machine X94.675' in text and "G56" not in text
    from milo_engine import MiloEngine
    from conftest import FakeStat
    engine = MiloEngine(stat_getter=lambda: FakeStat(), settings_path=str(tmp_path / "s.json"), config_dir=str(tmp_path))
    engine.context_providers.append(page.describe_for_milo)
    assert '"Right vise"' in engine._machine_context()
