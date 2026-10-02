"""Tests for turning Milo's log lines into conversation items"""

import pytest

from milo_ui.assistant.conversation import Conversation, classify_log_line


@pytest.mark.parametrize("line,kind,text,code", [
    ("[USER] Move X ten", "user", "Move X ten", None),
    ("[MILO] Tool 8 is loaded.", "assistant", "Tool 8 is loaded.", None),
    ("[CONFIRM] Rapid X +10 mm (incremental)  [G91 G0 X10.0000] - say 'yes' or press Confirm, 'no' or Cancel to abort.",
     "confirm", "Rapid X +10 mm (incremental)", "G91 G0 X10.0000"),
    ("[CONFIRM] Run part.ngc from the start - say 'yes' or press Confirm, 'no' or Cancel to abort.",
     "confirm", "Run part.ngc from the start", None),
    ("[CONFIRM] Confirmed: Home all axes", "status", "✓ Confirmed: Home all axes", None),
    ("[CONFIRM] Timed out: Home all axes (not executed)", "status", "Timed out: Home all axes (not executed)", None),
    ("[MDI] Executed: M5", "success", "Done", "M5"),
    ("[ERROR] Machine is in E-stop.", "error", "Machine is in E-stop.", None),
    ("[TOOLS] T59 is listed 2 times", "tool_warning", "T59 is listed 2 times", None),
    ("[WARN] Failed to save raw response", "warning", "Failed to save raw response", None),
    ("[CAM] Generating G-code...", "status", "Generating G-code...", None),
    ("[SESSION] Saved to x.json", "status", "Saved to x.json", None),
    ("[REVIEW] G-code has been loaded.", "detail", "[REVIEW] G-code has been loaded.", None),
    ("[VAD] Initial speech detected", "detail", "[VAD] Initial speech detected", None),
    ("no tag at all", "detail", "no tag at all", None),
])
def test_classify_log_line(line, kind, text, code):
    assert classify_log_line(line) == (kind, text, code)


def test_details_hidden_until_enabled(qapp):
    view = Conversation()
    view.add_log_line("[INFO] something technical")
    view.add_log_line("[USER] hello")
    rows = {kind: row for row, kind in view._rows}
    assert rows["detail"].isHidden()
    view.set_show_details(True)
    assert not rows["detail"].isHidden()


def test_welcome_shows_only_when_empty(qapp):
    view = Conversation()
    view.add_log_line("[INFO] startup detail")
    view.add_log_line("[CAM] background status")
    assert not view.welcome.isHidden()
    view.add_log_line("[USER] hello")
    assert view.welcome.isHidden()
    view.clear()
    assert not view.welcome.isHidden()


def test_old_messages_are_trimmed(qapp, monkeypatch):
    from milo_ui.assistant import conversation
    monkeypatch.setattr(conversation, "MAX_MESSAGES", 5)
    view = Conversation()
    for i in range(12):
        view.add_log_line(f"[MILO] message {i}")
    assert len(view._rows) == 5


def test_example_tile_emits_text(qapp):
    view = Conversation()
    seen = []
    view.example_clicked.connect(seen.append)
    from PyQt5.QtWidgets import QPushButton
    tiles = [b for b in view.welcome.findChildren(QPushButton) if b.objectName() == "example_tile"]
    tiles[0].click()
    assert seen == ["Move X ten millimeters"]


def test_tool_warnings_group_into_one_collapsed_notice(qapp):
    view = Conversation()
    view.add_log_line("[TOOLS] T2 Long: diameter 0.25 looks wrong")
    view.add_log_line("[TOOLS] T59 is listed 2 times")
    assert len([row for row, kind in view._rows if kind == "tool_notice"]) == 1
    notice = view.tool_notice()
    assert notice.items == ["T2 Long: diameter 0.25 looks wrong", "T59 is listed 2 times"]
    assert "2 issues" in notice.title.text()
    assert notice.body.isHidden()
    notice.toggle.click()
    assert not notice.body.isHidden()
    assert not view.welcome.isHidden()


def test_program_card_buttons(qapp):
    view = Conversation()
    ran, viewed = [], []
    view.run_program.connect(lambda: ran.append(1))
    view.view_program.connect(lambda: viewed.append(1))
    view.add_program({"name": "part.ngc", "ops": [{"name": "Pocket", "tool": 8}], "tools": [], "stock": "100 × 50 × 10 mm"})
    from PyQt5.QtWidgets import QPushButton
    buttons = {b.text(): b for b in view.findChildren(QPushButton)}
    buttons["Run…"].click()
    buttons["View toolpath"].click()
    assert ran == [1] and viewed == [1]
    assert view.welcome.isHidden()


def _fill(view):
    view.add_log_line("[USER] Move X ten")
    view.add_log_line("[MILO] Tool 8 is **loaded**, use `T8`.")
    view.add_log_line("[CONFIRM] Rapid X +10 mm  [G91 G0 X10.0000] - say 'yes' or press Confirm, 'no' or Cancel to abort.")
    view.add_log_line("[MDI] Executed: G91 G0 X10.0000")
    view.add_log_line("[TOOLS] T59 is listed 2 times")
    view.add_log_line("[TOOLS] T2 diameter looks wrong")
    view.add_program({"name": "part.ngc", "ops": [{"name": "Pocket", "tool": 8}], "tools": [], "stock": "100 × 50 × 10 mm"})


def _texts(view):
    from PyQt5.QtWidgets import QLabel
    return " ".join(label.text() for label in view.column.findChildren(QLabel) if label.isVisibleTo(view))


def test_switching_style_redraws_the_conversation(qapp):
    view = Conversation()
    _fill(view)
    kinds = [kind for _, kind in view._rows]
    view.set_style("compact")
    qapp.processEvents()
    assert [kind for _, kind in view._rows] == kinds  # same items, redrawn
    text = _texts(view)
    assert "&gt;" in text and "●" in text and "└" in text
    assert "<b>loaded</b>" in text and "T8</span>" in text  # light markdown
    assert view.tool_notice().compact and len(view.tool_notice().items) == 2
    view.set_style("bubbles")
    qapp.processEvents()
    assert [kind for _, kind in view._rows] == kinds and not view.tool_notice().compact


def test_compact_program_buttons_and_working_row(qapp):
    view = Conversation()
    view.set_style("compact")
    ran, viewed = [], []
    view.run_program.connect(lambda: ran.append(1))
    view.view_program.connect(lambda: viewed.append(1))
    _fill(view)
    from PyQt5.QtWidgets import QPushButton
    buttons = {b.text(): b for b in view.findChildren(QPushButton)}
    buttons["Run…"].click()
    buttons["View toolpath"].click()
    assert ran == [1] and viewed == [1]
    view.set_working(True, "Designing toolpaths…")
    assert view._working_compact.text.text() == "Designing toolpaths…"
    assert view._working_bubbles.isHidden()


def test_clear_forgets_history(qapp):
    view = Conversation()
    _fill(view)
    view.clear()
    view.set_style("compact")
    assert view._rows == [] and not view.welcome.isHidden()
