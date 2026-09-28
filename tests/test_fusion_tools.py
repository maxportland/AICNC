"""Tests for reading Fusion 360 tool libraries and using them on this machine"""

import glob
import json
import os
import zipfile

import pytest

import fusion_tools as ft

DESKTOP_LIBRARIES = sorted(glob.glob(os.path.expanduser("~/Desktop/*Fusion*.json")))


def _raw(unit="inches", dc=0.25, flutes=2, n=18000, v_f=160.0, f_z=None, kind="flat end mill", **extra):
    preset = {"name": "Softwood", "n": n, "v_f": v_f, "v_f_plunge": v_f / 3, "stepdown": 0.125,
              "stepover": 0.05, "use-stepdown": True, "use-stepover": True, "tool-coolant": "flood"}
    if f_z is not None:
        preset["f_z"] = f_z
    raw = {"guid": "g1", "vendor": "Acme Tools", "product-id": "A-1", "description": "Acme 1/4 upcut",
           "type": kind, "unit": unit, "BMC": "carbide", "product-link": "https://example.com",
           "geometry": {"DC": dc, "NOF": flutes, "LCF": 0.75, "OAL": 2.5, "SFDM": 0.25, "RE": 0},
           "start-values": {"presets": [preset]}}
    raw.update(extra)
    return raw


def test_inch_tool_is_converted_to_mm():
    tool = ft.parse_tool(_raw(), "lib.json")
    assert tool.diameter == pytest.approx(6.35)
    assert tool.flute_length == pytest.approx(19.05)
    assert tool.presets[0].feed == pytest.approx(160 * 25.4)
    # no f_z in the file: chip load comes from feed / (rpm * flutes)
    assert tool.presets[0].chip_load == pytest.approx(160 * 25.4 / (18000 * 2))
    assert tool.diameter_label.startswith('0.25"')


def test_holders_probes_and_broken_entries_are_skipped():
    assert ft.parse_tool({"type": "holder"}) is None
    assert ft.parse_tool(_raw(kind="probe")) is None
    assert ft.parse_tool(_raw(dc=0)) is None


def test_router_preset_is_rescaled_by_chip_load():
    tool = ft.parse_tool(_raw(unit="millimeters", dc=6, f_z=0.1, n=18000, v_f=3600))
    scaled = ft.scale_preset(tool.presets[0], tool.flutes, 100, 3000)
    assert scaled.rpm == 3000 and scaled.limited
    assert scaled.feed == pytest.approx(0.1 * 2 * 3000)   # same chip load, not the router's 3600
    assert scaled.plunge_feed == pytest.approx(1200 * 3000 / 18000)
    slow = ft.scale_preset(ft.Preset("x", 1500, 300, 100, 0.1, None, None, None), 2, 100, 3000)
    assert slow.rpm == 1500 and not slow.limited and slow.feed == pytest.approx(300)


def test_json_and_zipped_tools_files(tmp_path):
    data = {"data": [_raw(), {"type": "holder"}], "version": 1}
    plain = tmp_path / "lib.json"
    plain.write_text(json.dumps(data))
    zipped = tmp_path / "lib.tools"
    with zipfile.ZipFile(zipped, "w") as z:
        z.writestr("tools.json", json.dumps(data))
    for path in (plain, zipped):
        tools = ft.load_library(str(path))
        assert len(tools) == 1 and tools[0].source == path.name
    bad = tmp_path / "bad.json"
    bad.write_text('{"something": 1}')
    with pytest.raises(ValueError):
        ft.load_library(str(bad))


@pytest.mark.skipif(not DESKTOP_LIBRARIES, reason="example libraries not on the Desktop")
def test_example_libraries_load():
    tools = [t for path in DESKTOP_LIBRARIES for t in ft.load_library(path)]
    assert len(tools) > 500
    amana = next(t for t in tools if t.product_id == "46211-K")
    assert amana.diameter == pytest.approx(5.0, abs=0.01) and amana.flutes == 2
    assert all(t.diameter > 0 for t in tools)


def test_write_tool_entry_adds_and_updates(tmp_path):
    table = tmp_path / "tool.tbl"
    table.write_text("T8 P8 D+6.350000 Z+330.159000 ;1/4 4-Flute\n")
    assert ft.write_tool_entry(str(table), 12, 5.0, "Amana 46211-K 5mm 2FL") is True
    assert ft.write_tool_entry(str(table), 8, 6.35, "Amana 46094; 1/4in") is False
    lines = table.read_text().splitlines()
    assert lines[0] == "T8 P8 D6.3500 Z+330.159000 ;Amana 46094, 1/4in"  # length kept, ; removed
    assert lines[1] == "T12 P12 D5.0000 Z0.000 ;Amana 46211-K 5mm 2FL"
    ft.write_tool_entry(str(table), 13, 25.4, "inch table", machine_units="inch")
    assert "D1.0000" in table.read_text().splitlines()[2]


def test_links_round_trip(tmp_path):
    path = str(tmp_path / "links.json")
    tool = ft.parse_tool(_raw())
    ft.link_tool(12, tool, path)
    loaded = ft.load_links(path)
    assert loaded[12].product_id == "A-1" and loaded[12].presets[0].name == "Softwood"
    ft.unlink_tool(12, path)
    assert ft.load_links(path) == {}


def test_prompt_has_geometry_rescaled_feeds_and_depth_limit():
    tool = ft.parse_tool(_raw(unit="millimeters", dc=6, f_z=0.1, n=18000, v_f=3600))
    text = ft.describe_for_prompt(12, tool, 0, 3000)
    assert "2 flutes" in text and "flute length 0.75 mm" in text
    assert "rpm 3000, feed 600 mm/min" in text
    assert "never cut deeper than 0.75 mm" in text


def test_engine_adds_linked_tools_to_the_cam_prompt(tmp_path, qapp):
    from conftest import FakeStat
    from milo_engine import MiloEngine
    (tmp_path / "tool.tbl").write_text("T12 P12 D6.35 Z10 ;Acme 1/4 upcut\nT8 P8 D6.35 Z10 ;plain tool\n")
    ft.link_tool(12, ft.parse_tool(_raw()), str(tmp_path / "tool_links.json"))
    engine = MiloEngine(stat_getter=lambda: FakeStat(), config_dir=str(tmp_path),
                        settings_path=str(tmp_path / "cfg.json"))
    text = engine._load_tool_table()
    assert "CATALOG DATA FOR LINKED TOOLS" in text and "T12 geometry" in text and "T8 geometry" not in text
    assert engine.machine_flute_lengths == {12: pytest.approx(19.05)}
