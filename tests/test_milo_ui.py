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
    # Delete the screen now, in Qt's order, instead of leaving it to Python's garbage collector
    # at exit (which destroys Qt objects in no particular order and can crash)
    shell.pendant.shutdown()
    shell.deleteLater()
    machine.deleteLater()  # its timer would keep signalling the deleted screen
    from PyQt5.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


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
    assert not shell.stage.isVisibleTo(shell)
    shell.navigate("program")
    assert shell.stage.isVisibleTo(shell)
    shell.navigate("tools")
    assert not shell.stage.isVisibleTo(shell)


def test_toolpath_page_gives_the_stage_the_whole_area(shell):
    from PyQt5.QtWidgets import QApplication
    assert list(shell.pages)[:3] == ["home", "toolpath", "jog"]
    shell.navigate("toolpath")
    QApplication.processEvents()
    assert shell.stage.isVisibleTo(shell) and not shell.stack.isVisibleTo(shell)
    assert shell.stage.width() > 1500
    shell.navigate("program")
    QApplication.processEvents()
    assert shell.stack.isVisibleTo(shell) and shell.stage.width() == 600


def test_tool_table_says_why_it_is_locked(shell):
    m = shell.machine
    page = shell.pages["tools"]
    shell.navigate("tools")
    m.set_estop(True)
    assert page.lock.isVisibleTo(shell) and "E-stop" in page.lock_reason.text()
    m.set_estop(False)
    assert "turn the machine on" in page.lock_reason.text()
    m.set_power(True)
    m.unhome_all()
    assert "home the machine" in page.lock_reason.text()
    assert not page.hint.isVisibleTo(shell)
    for axis in m.axes:
        m.home_axis(axis)
    assert not page.lock.isVisibleTo(shell)
    assert page.hint.isVisibleTo(shell)
    m.home_required = False
    m.unhome_all()
    assert not page.lock.isVisibleTo(shell)


def test_toasts_leave_milos_peek_alone(shell):
    shell.navigate("jog")
    shell.bridge.on_message("[MILO] Spindle is at 2400 rpm.")
    for i in range(6):
        shell.toaster.show(f"note {i}")
    from PyQt5.QtWidgets import QApplication, QFrame
    from PyQt5.QtCore import QCoreApplication, QEvent
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    QApplication.processEvents()
    assert shell.peek.isVisible()
    assert len([t for t in shell.findChildren(QFrame, "toast") if t is not shell.peek]) == 4
    shell.navigate("home")
    assert not shell.peek.isVisible()


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


def test_tool_library_adds_a_tool_to_the_machine(shell, tmp_path, monkeypatch):
    import json
    import fusion_tools as ft
    monkeypatch.setattr(ft, "LIBRARY_DIR", str(tmp_path / "libs"))
    os.makedirs(ft.LIBRARY_DIR)
    tool = {"guid": "g1", "vendor": "Acme", "product-id": "A-1", "description": "Acme 6mm upcut",
            "type": "flat end mill", "unit": "millimeters",
            "geometry": {"DC": 6, "NOF": 2, "LCF": 20, "OAL": 60, "SFDM": 6},
            "start-values": {"presets": [{"name": "Oak", "n": 18000, "v_f": 3600, "f_z": 0.1}]}}
    with open(os.path.join(ft.LIBRARY_DIR, "acme.json"), "w") as f:
        json.dump({"data": [tool], "version": 1}, f)
    shell.navigate("tools")
    library = shell.pages["tools"].open_library()
    assert library.list.count() == 1
    from milo_ui.pages.tool_library import TOOL_ROLE
    library._write(12, library.list.item(0).data(TOOL_ROLE))
    m = shell.machine
    assert "T12 P12 D6.0000" in open(m.tool_table_path).read()
    assert ft.load_links(m.tool_links_path)[12].product_id == "A-1"
    m.tool = 12
    page = shell.pages["tools"]
    page.current.refresh()
    assert "Acme A-1" in page.current.catalog.text()
    # the real tool table is untouched: previews edit a copy
    assert m.tool_table_path.startswith(os.path.join(os.sep, "tmp")) or "milo-sim-" in m.tool_table_path


def test_compact_conversation_hides_the_suggestion_chips(shell):
    home = shell.pages["home"]
    shell.navigate("home")
    assert home.suggestions.isVisibleTo(shell)
    shell.conversation.set_style("compact")
    assert not home.suggestions.isVisibleTo(shell)
    shell.conversation.set_style("bubbles")
    assert home.suggestions.isVisibleTo(shell)


def test_settings_tabs_switch_and_are_remembered(shell):
    page = shell.pages["settings"]
    shell.navigate("settings")
    assert page.tab_names == ["Milo", "Screen", "Voice", "Pendant", "Machine"]
    page.tabs.buttons[2].click()
    assert page.tab_stack.currentIndex() == 2 and page.speak.isVisibleTo(shell)
    assert not page.key_field.isVisibleTo(shell)
    assert shell.prefs.get("settings_tab") == "Voice"


def test_shutdown_is_on_every_settings_tab(shell):
    page = shell.pages["settings"]
    shell.navigate("settings")
    for index in range(len(page.tab_names)):
        page.show_tab(index)
        assert page.shutdown.isVisibleTo(shell)


def test_pendant_tab_turns_the_pendant_on_and_off(shell, monkeypatch):
    from milo_ui import pendant as pendant_module
    started = []

    class Reader(pendant_module.QtCore.QObject):
        changed = pendant_module.QtCore.pyqtSignal(object)
        name = "test pad"

        def start(self):
            started.append(True)

        def stop(self):
            started.append(False)
    shell.pendant._reader_factory = Reader
    shell.show_settings_tab("Pendant")
    card = shell.pages["settings"].pendant_card
    assert card.isVisibleTo(shell) and not shell.topbar.pendant.isVisibleTo(shell)
    card.enable_toggle.toggle.click()
    assert started == [True] and shell.pendant.config["enabled"]
    assert shell.topbar.pendant.isVisibleTo(shell)
    card.enable_toggle.toggle.click()
    assert started == [True, False] and shell.pendant.status == "off"


def test_settings_model_picker(shell):
    import ai_config
    page = shell.pages["settings"]
    shell.navigate("settings")
    page.on_show()
    models = [page.model.itemData(i) for i in range(page.model.count())]
    assert models[0] == "" and "gpt-5.5" in models  # Recommended first, then the fetched list
    page.model.activated.emit(models.index("gpt-4o"))
    assert shell.engine.settings["ai_model"] == "gpt-4o" and not page.quality.isEnabled()
    page.model.activated.emit(0)
    page.quality.buttons[2].click()
    assert shell.engine.settings["ai_quality"] == "best" and page.quality.isEnabled()
    stt = [page.stt_model.itemData(i) for i in range(page.stt_model.count())]
    assert stt[0] == "" and "whisper-1" in stt
    page.stt_model.activated.emit(stt.index("whisper-1"))
    assert shell.engine.settings["transcription_model"] == "whisper-1"
    ai_config.choose("", "balanced")


def test_pendant_chip_shows_the_step_size(shell):
    import time
    from test_pendant import _state
    pendant = shell.pendant
    pendant.config["enabled"] = True
    pendant.state = _state(last_event=time.monotonic())
    pendant.tick()
    assert shell.topbar.pendant.text().endswith("step 0.1 mm")
    pendant.state = _state(buttons={"RB"}, last_event=time.monotonic())
    pendant.tick()
    assert shell.topbar.pendant.text().endswith("step 1 mm")


def test_new_conversation_clears_the_transcript(shell):
    conversation = shell.conversation
    conversation.add_log_line("[USER] face the stock")
    conversation.add_log_line("[MILO] Generating a program...")
    assert conversation._rows
    shell.engine.new_conversation()
    assert conversation._rows == [] and conversation._history == []


def test_a_busy_top_bar_never_widens_the_window(shell):
    """Running, spindle on, pendant on and a long tool name made the window wider than the screen,
    cutting off E-STOP and Stop. The long pills shorten instead; the short ones stay whole."""
    m, bar = shell.machine, shell.topbar
    m.tool, m.tool_comment, m.tool_diameter = 1, "Amana 51417-K 0.1875in 1FL flat end mill with a long name", 4.7625
    bar.pendant.setText("Pendant · hold RT · step 0.01 mm")
    bar.pendant.show()
    m.set_estop(False)
    m.set_power(True)
    m.spindle_start(1, 2500)
    m.open_program(__file__)
    m.total_lines = 99999
    m.run()
    m.feed_rate, m.feed_override = 12345, 120
    m._timer.stop()
    bar.refresh()
    for key in shell.pages:
        shell.navigate(key)
        assert shell.minimumSizeHint().width() <= 1920, key
    shell.resize(1920, 1080)
    from PyQt5.QtWidgets import QApplication
    QApplication.processEvents()
    assert bar.pendant.text() == "Pendant · hold RT · step 0.01 mm"  # the full text, whatever is shown
    for pill in (bar.wcs, bar.spindle, bar.feed):
        assert pill.minimumSizeHint().width() == pill.sizeHint().width()  # never shortened
    m.abort()


def test_info_pill_shortens_only_when_narrow(qapp):
    from milo_ui.shell import InfoPill
    pill = InfoPill("wrench", shrinkable=True)
    pill.setText("T1 · Amana 51417-K 0.1875in 1FL flat end mill")
    pill.show()  # hidden widgets get no resize events
    pill.resize(pill.sizeHint())
    assert QPushButtonText(pill) == pill.text()
    pill.resize(pill.minimumSizeHint().width() + 40, pill.height())
    assert QPushButtonText(pill).endswith("…") and pill.text().endswith("end mill")


def QPushButtonText(pill):
    """What the pill actually shows"""
    from PyQt5.QtWidgets import QPushButton
    return QPushButton.text(pill)
