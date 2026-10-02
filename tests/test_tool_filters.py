"""Tests for the Tool library's filters: flutes, diameter, reach, length, shank, material, cutting data, sort"""

import pytest

import fusion_tools as ft
from milo_ui import tool_filters as tf
from test_milo_ui import shell  # noqa: F401  (fixture)


def tool(name, diameter, flutes=2, flute_length=12.0, overall_length=50.0, shank=None, material="carbide",
         presets=True, kind="flat end mill"):
    preset = ft.Preset(name="Aluminum", rpm=18000, feed=1000, plunge_feed=300, chip_load=0.03, stepdown=None,
                       stepover=None, ramp_angle=None) if presets else None
    return ft.FusionTool(guid=name, vendor="Amana", product_id=name, description=f"{name} {diameter} mm",
                         type=kind, material=material, diameter=diameter, flutes=flutes, flute_length=flute_length,
                         overall_length=overall_length, shank_diameter=shank or diameter,
                         presets=[preset] if preset else [], source="lib.json")


TOOLS = [
    tool("A", 3.0, flutes=1, flute_length=8, overall_length=38),
    tool("B", 3.175, flutes=2, flute_length=12.7, overall_length=50.8, shank=3.18),
    tool("C", 6.0, flutes=3, flute_length=20, overall_length=65, presets=False),
    tool("D", 6.35, flutes=2, flute_length=25.4, overall_length=76.2, shank=6.35, material="hss"),
    tool("E", 12.7, flutes=4, flute_length=38, overall_length=100, shank=12.7),
    tool("F", 20.0, flutes=6, flute_length=30, overall_length=110, shank=20.0),
]


def names(tools):
    return [t.product_id for t in tools]


@pytest.mark.parametrize("key,value,expected", [
    ("flutes", 2, ["B", "D"]),
    ("flutes", "5+", ["F"]),
    ("diameter", (None, 3.0), []),
    ("diameter", (3.0, 6.0), ["A", "B"]),     # 3.000 is in 3-6, 6.000 is not
    ("diameter", (6.0, 10.0), ["C", "D"]),
    ("diameter", (20.0, None), ["F"]),
    ("flute_length", 20.0, ["C", "D", "E", "F"]),
    ("overall_length", (65.0, 80.0), ["C", "D"]),
    ("shank", 3.175, ["B"]),                  # 3.18 in the file is the same 1/8" shank
    ("material", "hss", ["D"]),
    ("cutting_data", True, ["A", "B", "D", "E", "F"]),
    ("cutting_data", False, ["C"]),
])
def test_each_filter(key, value, expected):
    assert names(tf.apply(TOOLS, {key: value})) == expected


def test_filters_combine_and_skip_one_for_counting():
    active = {"flutes": 2, "diameter": (6.0, 10.0)}
    assert names(tf.apply(TOOLS, active)) == ["D"]
    assert names(tf.apply(TOOLS, active, skip="diameter")) == ["B", "D"]  # what the Diameter menu counts from


def test_options_and_labels():
    assert [o.label for o in tf.options("diameter", TOOLS)][:2] == ["Under 3 mm", "3 mm to 6 mm"]
    inch = tf.options("diameter", TOOLS, "in")
    assert inch[1].label == '1/8" to 1/4"' and inch[1].value == pytest.approx((3.175, 6.35))
    assert tf.options("overall_length", TOOLS, "in")[0].label == 'Under 2"'
    assert tf.describe("diameter", (3.0, 6.0), "in") == 'Diameter 0.1181"–0.2362"'
    assert tf.shank_label(6.0, "in") == '0.2362" (6 mm)'
    assert tf.options("flute_length", TOOLS, "in")[0].value == pytest.approx(3.175)
    assert [o.label for o in tf.options("shank", TOOLS)] == ['3 mm', '1/8" (3.175 mm)', '6 mm', '1/4" (6.350 mm)',
                                                            '1/2" (12.700 mm)', '20 mm']
    assert [o.label for o in tf.options("material", TOOLS)] == ["Carbide", "HSS"]
    assert tf.describe("diameter", (3.0, 6.0)) == "Diameter 3 mm–6 mm"
    assert tf.describe("flute_length", 20.0) == "Reach ≥ 20 mm"
    assert tf.describe("shank", 6.35) == 'Shank 1/4"' and tf.describe("flutes", None) == "Flutes"


def test_inch_fractions():
    assert tf.inch_fraction(3.175) == '1/8"' and tf.inch_fraction(19.05) == '3/4"'
    assert tf.inch_fraction(25.4) == '1"' and tf.inch_fraction(31.75) == '1-1/4"'
    assert tf.inch_fraction(6.0) is None


def test_custom_ranges_and_saving():
    assert tf.custom_range(10, 4, "mm") == (4, 10)  # either order
    assert tf.custom_range(0, 0.5, "in") == (None, pytest.approx(12.7))
    active = {"diameter": (3.0, None), "flutes": "5+"}
    assert tf.from_json(tf.to_json(active)) == active
    assert tf.from_json({"bogus": 1, "diameter": "x"}) == {}


def test_sorting():
    assert names(tf.sort(list(TOOLS), dict(tf.SORTS)["Longest reach"]))[:2] == ["E", "F"]
    assert names(tf.sort(list(TOOLS), dict(tf.SORTS)["Largest diameter"]))[0] == "F"


def test_library_filters_counts_and_memory(shell, monkeypatch):
    from milo_ui.pages import tool_library
    monkeypatch.setattr(tool_library.ft, "installed_libraries", lambda: [])
    library = tool_library.ToolLibrary(shell, shell)
    library.tools = list(TOOLS)
    library._filter()
    assert library.list.count() == 6 and not library.clear_button.isVisibleTo(library)
    library.set_filter("flutes", 2)
    library.set_filter("flute_length", 20.0)
    assert library.list.count() == 1 and library.filter_chips["flutes"].text() == "2 flutes"
    # The Diameter menu counts what each choice would leave, with the other filters
    sheets = []
    monkeypatch.setattr(tool_library.kit, "ActionSheet",
                        lambda host, title, actions, **kw: sheets.append(actions) or type(
                            "S", (), {"show_at": lambda *a: None})())
    library._filter_menu("diameter")
    labels = [a[1] for a in sheets[-1]]
    assert labels[0] == "Any (1)" and "6 mm to 10 mm (1)" in labels and "Under 3 mm (0)" in labels
    library._filter_menu("material")
    assert [a[1] for a in sheets[-1]] == ["Any (1)", "HSS (1)"]  # materials none of these tools have are left out
    assert shell.prefs.get(tool_library.FILTER_PREFS)["filters"] == {"flutes": 2, "flute_length": 20.0}
    again = tool_library.ToolLibrary(shell, shell)  # remembered next time
    assert again.active == {"flutes": 2, "flute_length": 20.0}
    again.clear_filters()
    assert again.active == {} and shell.prefs.get(tool_library.FILTER_PREFS)["filters"] == {}


def test_the_modal_switches_units(shell, monkeypatch):
    """Only the modal's display: the machine stays as it is"""
    from milo_ui.pages import tool_library
    monkeypatch.setattr(tool_library.ft, "installed_libraries", lambda: [])
    library = tool_library.ToolLibrary(shell, shell)
    library.tools = list(TOOLS)
    library.set_filter("diameter", (6.0, 10.0))
    assert library.units == "mm" and library.filter_chips["diameter"].text() == "Diameter 6 mm–10 mm"
    library.list.setCurrentRow(1)  # D, 6.35 mm
    assert tool_library.fmt_diameter(6.35, "mm") == '6.35 mm (1/4")'
    library.units_toggle.selected.emit(1)
    assert library.units == "in" and library.delegate.units == "in" and library.detail.units == "in"
    assert library.filter_chips["diameter"].text() == 'Diameter 0.2362"–0.3937"'
    assert library.list.count() == 2  # the same tools: filters are kept in mm
    texts = [w.text() for w in library.detail.findChildren(type(library.count))]
    assert '1/4" (6.35 mm)' in texts and '1"' in texts  # diameter, flute length 25.4
    assert shell.machine.metric and shell.machine.units == "mm"
    again = tool_library.ToolLibrary(shell, shell)  # remembered
    assert again.units == "in" and again.units_toggle.index() == 1
    again.set_units("mm")
    assert again.units_toggle.index() == 0


def test_lengths_read_naturally():
    from milo_ui.pages.tool_library import fmt_len
    assert fmt_len(28.575, "in") == '1-1/8"' and fmt_len(6.0, "in") == '0.2362"' and fmt_len(38.1, "mm") == "38.1 mm"
