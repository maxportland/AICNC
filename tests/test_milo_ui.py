"""Tests for the Milo screen: machine model, suggestions, number entry, and the whole shell"""

import os

import pytest

from milo_ui.machine import SimMachine, parse_increment


@pytest.mark.parametrize("text,metric,expected", [
    ("Continuous", True, 0.0), ("10mm", True, 10.0), (".5mm", True, 0.5), (".005mm", True, 0.005),
    ("0.1 in", True, 2.54), ("1", True, 1.0), ("1 mm", False, 1 / 25.4), ("bogus", True, None),
])
def test_parse_increment(text, metric, expected):
    value = parse_increment(text, metric)
    assert value == pytest.approx(expected) if expected is not None else value is None


def test_state_labels_follow_the_machine(qapp):
    m = SimMachine()
    assert m.state_label == "E-STOP"
    m.set_estop(False)
    assert m.state_label == "OFF"
    m.set_power(True)
    assert m.state_label == "NOT HOMED" and "XYZ" in m.state_detail
    m.homed = {a: True for a in m.axes}
    assert m.state_label == "READY" and m.ready
    m.open_program(__file__)
    m.run()
    assert m.state_label == "RUNNING" and not m.ready
    m.pause_resume()
    assert m.state_label == "PAUSED"
    m.set_estop(True)
    assert m.state_label == "E-STOP" and not m.on and not m.is_running


def test_power_needs_estop_released(qapp):
    m = SimMachine()
    messages = []
    m.message.connect(lambda level, text: messages.append(text))
    m.set_power(True)
    assert not m.on and messages


def test_set_axis_origin_moves_the_work_zero(qapp):
    m = SimMachine()
    m.set_axis_origin("X", 0.0)
    assert m.pos_rel[0] == pytest.approx(0.0)
    m.set_axis_origin("Y", 25.0)
    assert m.pos_rel[1] == pytest.approx(25.0)


def test_model_actions_map_to_the_model(qapp):
    m = SimMachine()
    api = m.action_api()
    assert api.CALL_MDI("G0 X0") == -1  # machine off: refused
    m.set_estop(False)
    api.SET_MACHINE_STATE(True)
    assert m.on
    assert api.CALL_MDI("G0 X0") == 0


def test_suggestions_follow_state(qapp):
    from milo_ui.pages.home import suggestions
    m = SimMachine()
    assert suggestions(m)[0][1].startswith("Why")
    m.set_estop(False)
    assert suggestions(m)[0][1] == "Turn the machine on"
    m.set_power(True)
    assert suggestions(m)[0][1] == "Home all axes"
    m.homed = {a: True for a in m.axes}
    m.open_program("/tmp/part.ngc")
    assert suggestions(m)[0][1] == "Run part.ngc"
    assert len(suggestions(m)) <= 3


@pytest.mark.parametrize("text,value", [("12.5", 12.5), ("45/2", 22.5), ("-3", -3.0), ("10+2.5", 12.5),
                                        ("", None), ("2**99", None), ("abs(1)", None), ("1/0", None)])
def test_numpad_arithmetic(qapp, text, value):
    from PyQt5 import QtWidgets
    from milo_ui import kit
    host = QtWidgets.QWidget()
    pad = kit.NumPad(host, "Value", lambda v: None)
    pad.display.setText(text)
    assert pad.value() == (pytest.approx(value) if value is not None else None)


@pytest.fixture
def shell(qapp, tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    from milo_ui import preview
    _app, shell, machine = preview.build("home")
    shell.show()
    yield shell
    shell.close()


def test_every_page_fits_the_screen(shell):
    from PyQt5.QtWidgets import QApplication
    for key in shell.pages:
        shell.navigate(key)
        QApplication.processEvents()
        hint = shell.minimumSizeHint()
        assert hint.width() <= 1920 and hint.height() <= 1080, (key, hint)
        # the page itself fits the space left by the rail, bars, margins and (maybe) the stage
        page = shell.pages[key].minimumSizeHint()
        width = 1920 - 104 - 40 - (620 if shell.pages[key].wants_stage else 0)
        assert page.width() <= width and page.height() <= 1080 - 84 - 112 - 40, (key, page)


def test_stage_shows_only_where_wanted(shell):
    shell.navigate("home")
    assert shell.stage.isVisibleTo(shell)
    shell.navigate("tools")
    assert not shell.stage.isVisibleTo(shell)


def test_suggestion_chip_asks_milo(shell):
    shell.machine.set_estop(False)
    from PyQt5.QtWidgets import QApplication
    QApplication.processEvents()
    from milo_ui import kit
    chips = [c for c in shell.pages["home"].suggestions.findChildren(kit.Chip) if c.isVisibleTo(shell)]
    chips[0].click()
    assert shell.engine.asked == ["Turn the machine on"]


def test_confirmation_sheet_shows_and_hides(shell):
    shell.engine.confirmation = type("Gate", (), {"remaining_seconds": lambda self: 12.0})()
    shell.bridge.on_proposal({"kind": "mdi", "command": "G0 X0", "summary": "Move to X0"})
    assert shell.confirm_sheet.isVisible()
    assert shell.confirm_sheet.summary.text() == "Move to X0"
    shell.bridge.on_proposal(None)
    assert not shell.confirm_sheet.isVisible()


def test_milo_replies_peek_on_other_pages(shell):
    shell.navigate("jog")
    shell.bridge.on_message("[MILO] Spindle is at 2400 rpm.")
    assert shell.peek.isVisible()
    shell.navigate("home")
    assert not shell.peek.isVisible()


def test_keyboard_pushes_the_screen_up(shell):
    shell.keyboard.show_for(shell.composer.input)
    assert shell.layout().contentsMargins().bottom() == shell.keyboard.height()
    shell.keyboard.hide_keyboard()
    assert shell.layout().contentsMargins().bottom() == 0


def test_cycle_start_needs_a_ready_machine(shell):
    m = shell.machine
    assert not shell.cycle.start.isEnabled()
    m.set_estop(False)
    m.set_power(True)
    m.homed = {a: True for a in m.axes}
    m.open_program(__file__)
    shell.cycle.refresh()
    assert shell.cycle.start.isEnabled()
    shell.start_program()
    assert m.is_running
    shell.cycle.refresh()
    assert not shell.cycle.start.isEnabled() and shell.cycle.pause.isEnabled()


def test_estop_double_tap_does_not_reset(shell):
    m = shell.machine
    m.set_estop(False)
    shell.topbar.refresh()
    shell.topbar.estop.click()
    assert m.estop
    shell.topbar.estop.click()  # immediately again
    assert m.estop


def test_macros_ask_first(shell, monkeypatch):
    m = shell.machine
    ran = []
    monkeypatch.setattr(m, "run_macro", lambda i: ran.append(i))
    page = shell.pages["jog"]
    page._macro(0, page.zero_button)
    assert ran == []  # only after tapping Run on the sheet
