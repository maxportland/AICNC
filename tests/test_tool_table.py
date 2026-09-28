"""Tests for tool table parsing"""

from tool_table import format_tools_for_prompt, get_tool_table, read_tool_table, usable_tools

TABLE = """T0   P0   ;Probe
T8   P0   D+6.350000 Z+330.159000 ;1/4 4-Flute
T2   P0   D+0.250000 Z+312.021000 ;Long
T59  P0   Z-155.816000 ;New Tool
T59  P0   D+6.35 ;1/4 90D Chamfer
T5   P0   D+3.175 Z-165.966000 ;1/8 2 flute endmill
T7   P0   D6 ;6mm ball nose
T9   P0   D3.3 ;3.3 drill
"""


def _read(tmp_path, units="mm"):
    path = tmp_path / "tool.tbl"
    path.write_text(TABLE)
    return read_tool_table(str(path), units)


def test_parses_word_format(tmp_path):
    tools, _ = _read(tmp_path)
    t8 = next(t for t in tools if t["tool"] == 8)
    assert t8["diameter"] == 6.35
    assert t8["flutes"] == 4
    assert t8["description"] == "1/4 4-Flute"
    assert next(t for t in tools if t["tool"] == 5)["flutes"] == 2


def test_tool_types(tmp_path):
    tools, _ = _read(tmp_path)
    types = {t["tool"]: t["type"] for t in tools}
    assert types[7] == "ball_endmill"
    assert types[9] == "drill"
    assert types[8] == "endmill"


def test_implausible_diameter_is_dropped_and_reported(tmp_path):
    tools, warnings = _read(tmp_path)
    assert next(t for t in tools if t["tool"] == 2)["diameter"] is None
    assert "T2 Long: diameter 0.25 looks wrong for a mm table (entered in inches?)" in warnings


def test_duplicates_and_t0_are_unusable(tmp_path):
    tools, warnings = _read(tmp_path)
    usable = usable_tools(tools)
    assert 59 not in usable
    assert 0 not in usable
    assert usable[8] == 6.35
    assert usable[2] is None
    assert "T59 is listed 2 times; not used until it's unique" in warnings


def test_inch_table(tmp_path):
    tools, warnings = _read(tmp_path, "inch")
    assert next(t for t in tools if t["tool"] == 2)["diameter"] == 0.25
    assert next(t for t in tools if t["tool"] == 8)["diameter"] is None


def test_prompt_lists_only_usable_tools(tmp_path):
    tools, _ = _read(tmp_path)
    text = format_tools_for_prompt(tools)
    assert "Tool 8: 1/4 4-Flute (endmill, diameter 6.35 mm (from the tool table; use exactly this), 4 flutes)" in text
    assert "Tool 2: Long (endmill, diameter not recorded" in text
    assert "Tool 59" not in text and "Tool 0" not in text


def test_get_tool_table_missing_file(tmp_path):
    text, tools = get_tool_table(str(tmp_path))
    assert "No tool table file found" in text
    assert tools is None
