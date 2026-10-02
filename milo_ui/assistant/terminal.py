"""
The compact conversation style: a terminal-like transcript in monospace with colored markers,
in the manner of Claude Code in a terminal.

    > what tool is loaded?
    ● Tool 8 is in the spindle: a 1/4" 4-flute endmill.
    ● Confirm  Rapid X +10 mm
      └ G91 G0 X10.0000
      └ say "yes" or tap Confirm

Each builder returns one row widget; Conversation decides which style to use.
"""

import html
import os
import re

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C

SIZE = 16  # px, monospace
GUTTER = 26  # width of the marker column
BULLETS = {"assistant": C.accent_hi, "confirm": C.amber, "success": C.green, "error": C.red,
           "warning": C.amber, "program": C.accent_2}
SPINNER = "·✢✳✶✻✽✻✶✳✢"
HOOK = "└"  # marks a result line (Claude Code uses ⎿, which the bundled mono font lacks)


def markup(text):
    """Plain text with light markdown (**bold**, `code`) as rich text"""
    out = html.escape(text)
    out = re.sub(r"`([^`\n]+)`", rf"<span style='color:{C.accent_2};'>\1</span>", out)
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", out)
    return out.replace("\n", "<br>")


def text_label(rich, color=C.text, size=SIZE):
    label = QtWidgets.QLabel()
    label.setTextFormat(Qt.RichText)
    label.setWordWrap(True)
    label.setText(rich)
    # Inline, so it wins over the app stylesheet's label fonts
    label.setStyleSheet(f"color: {color}; background: transparent; font-family: '{theme.MONO_FONT}'; "
                        f"font-size: {size}px;")
    label.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return label


def line(marker, rich, marker_color=C.text_3, color=C.text, indent=0):
    """One transcript line: a marker in the gutter, then wrapped text"""
    row = QtWidgets.QWidget()
    layout = QtWidgets.QHBoxLayout(row)
    layout.setContentsMargins(indent, 0, 0, 0)
    layout.setSpacing(0)
    mark = text_label(f"<span style='color:{marker_color};'>{marker}</span>", marker_color)
    mark.setFixedWidth(GUTTER)
    mark.setAlignment(Qt.AlignLeft | Qt.AlignTop)
    layout.addWidget(mark, 0, Qt.AlignTop)
    layout.addWidget(text_label(rich, color), 1)
    return row


def sub_line(rich, color=C.text_3):
    """An indented result line hanging off the item above"""
    return line(HOOK, rich, C.text_4, color, indent=GUTTER - 4)


def stack(*rows):
    widget = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)
    for row in rows:
        if isinstance(row, QtWidgets.QLayout):
            layout.addLayout(row)
        else:
            layout.addWidget(row)
    return widget


def user_row(text):
    frame = QtWidgets.QFrame()
    frame.setStyleSheet(f"QFrame {{ background: {C.card}; border-radius: 6px; }}")
    layout = QtWidgets.QHBoxLayout(frame)
    layout.setContentsMargins(0, 6, 12, 6)
    layout.addWidget(line("&gt;", markup(text), C.text_3, C.text_2, indent=8))
    return frame


def milo_row(text):
    return line("●", markup(text), BULLETS["assistant"])


def note_row(kind, text):
    if kind == "detail":
        return line("", f"<span style='font-size:13px;'>{html.escape(text)}</span>", C.text_4, C.text_4)
    return sub_line(html.escape(text))


def event_row(kind, text, code):
    """Confirmation, success, error and warning items"""
    color = BULLETS.get(kind, C.blue)
    if kind == "confirm":
        head = f"<span style='color:{C.amber};'><b>Confirm</b></span>&nbsp;&nbsp;{markup(text)}"
    elif kind == "success":
        head = f"<span style='color:{C.green};'><b>{markup(text)}</b></span>"
    elif kind == "error":
        head = f"<span style='color:{C.red};'>{markup(text)}</span>"
    else:
        head = markup(text)
    rows = [line("●", head, color)]
    if code:
        rows.append(sub_line(f"<span style='color:{C.accent_2};'>{html.escape(code)}</span>"))
    if kind == "confirm":
        rows.append(sub_line("say “yes” or tap Confirm · “no” or Cancel"))
    return stack(*rows)


class ProgramBlock(QtWidgets.QWidget):
    """A generated program as transcript lines, with the same buttons as the card"""

    view_clicked = QtCore.pyqtSignal()
    run_clicked = QtCore.pyqtSignal()

    def __init__(self, info, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        name = info.get("name") or "Generated program"
        layout.addWidget(line("●", f"<span style='color:{C.accent_2};'><b>Program ready</b></span>&nbsp;&nbsp;"
                                   f"{html.escape(name)}", BULLETS["program"]))
        ops = ", ".join(f"{op['name']}" + (f" T{op['tool']}" if op.get("tool") is not None else "")
                        for op in info.get("ops", [])[:8])
        if ops:
            layout.addWidget(sub_line(html.escape(ops), C.text_2))
        facts = []
        if info.get("stock"):
            facts.append(f"stock {info['stock']}")
        for tool in info.get("tools", [])[:3]:
            dia = f" ⌀{tool['diameter']:g}" if isinstance(tool.get("diameter"), (int, float)) else ""
            facts.append(f"T{tool.get('number')}{dia} {tool.get('description') or ''}".strip())
        if facts:
            layout.addWidget(sub_line(html.escape(" · ".join(facts))))
        for problem in info.get("problems") or []:
            layout.addWidget(sub_line(f"<span style='color:{C.amber};'>⚠ {html.escape(problem)}</span>"))
        preview = info.get("preview")
        if preview and os.path.exists(preview):
            pixmap = QtGui.QPixmap(preview)
            if not pixmap.isNull():
                picture = QtWidgets.QLabel()
                picture.setPixmap(pixmap.scaledToWidth(min(260, pixmap.width()), Qt.SmoothTransformation))
                layout.addLayout(self._indented(picture))
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(8)
        view = kit.Button("View toolpath", icon="cube", variant="outline", size="sm")
        view.clicked.connect(self.view_clicked.emit)
        run = kit.Button("Run…", icon="play-fill", variant="go", size="sm")
        run.clicked.connect(self.run_clicked.emit)
        buttons.addWidget(view)
        buttons.addWidget(run)
        buttons.addStretch(1)
        layout.addLayout(self._indented(buttons))

    @staticmethod
    def _indented(item):
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(GUTTER * 2 - 4, 6, 0, 2)
        if isinstance(item, QtWidgets.QLayout):
            row.addLayout(item, 1)
        else:
            row.addWidget(item)
            row.addStretch(1)
        return row


class Working(QtWidgets.QWidget):
    """✻ Thinking… with a turning spinner glyph"""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.glyph = text_label("", C.amber)
        self.glyph.setFixedWidth(GUTTER)
        layout.addWidget(self.glyph)
        self.text = text_label("Thinking…", C.amber)
        layout.addWidget(self.text, 1)
        self._frame = 0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._tick()

    def set_text(self, text):
        self.text.setText(html.escape(text))

    def _tick(self):
        self._frame = (self._frame + 1) % len(SPINNER)
        self.glyph.setText(SPINNER[self._frame])

    def showEvent(self, event):
        self._timer.start(120)
        super().showEvent(event)

    def hideEvent(self, event):
        self._timer.stop()
        super().hideEvent(event)
