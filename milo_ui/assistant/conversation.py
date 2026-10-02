"""
The conversation with Milo.

Log lines from the engine ("[TAG] message") become conversation items: the operator's
requests as bubbles, Milo's replies as plain text beside its mark, and outcomes as
cards (confirmations, results, errors). Generated programs get a rich card with Run and
View buttons. Technical lines are kept as small "detail" rows, hidden unless details are on.
"""

import html
import os
import re
from datetime import datetime
from typing import Optional, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.assistant.orb import Mark, ThinkingDots, Orb
from milo_ui.assistant import terminal

MAX_MESSAGES = 300  # oldest items are removed beyond this
COLUMN_MAX_WIDTH = 820
STYLES = ("bubbles", "compact")  # bubbles and cards, or a terminal-like transcript (see terminal.py)
COMPACT_MAX_WIDTH = 1240

_TAG_RE = re.compile(r'^\[([A-Z][A-Z0-9_-]*)\]\s*(.*)$', re.S)
_CONFIRM_RE = re.compile(r'^(.*?)(?:\s+\[([^\]]+)\])?\s+-\s+say .*$', re.S)

CARD_ICONS = {
    "confirm": ("hand-palm-fill", C.amber),
    "success": ("check-circle-fill", C.green),
    "error": ("warning-octagon-fill", C.red),
    "warning": ("warning-fill", C.amber),
}

QUIET_TAGS = {"SESSION", "CONTEXT", "CONFIG"}


def classify_log_line(message: str) -> Tuple[str, str, Optional[str]]:
    """
    Map a log line to (kind, text, code).

    kind is one of user, assistant, confirm, success, error, warning, tool_warning, status, detail.
    code is a G-code line to show in monospace, if any.
    """
    match = _TAG_RE.match(message.strip())
    if not match:
        return "detail", message, None
    tag, text = match.group(1), match.group(2).strip()

    if tag == "USER":
        return "user", text, None
    if tag == "MILO":
        return "assistant", text, None
    if tag == "CONFIRM":
        if text.startswith("Confirmed:"):
            # A result card (Executed / Homing / Running) follows, so keep this quiet
            return "status", "✓ " + text, None
        if text.endswith("(not executed)"):
            return "status", text, None
        proposal = _CONFIRM_RE.match(text)
        if proposal:
            return "confirm", proposal.group(1).strip(), proposal.group(2)
        return "confirm", text, None
    if tag == "MDI" and text.startswith("Executed:"):
        return "success", "Done", text[len("Executed:"):].strip()
    if tag == "ERROR":
        return "error", text, None
    if tag == "TOOLS":
        return "tool_warning", text, None
    if tag == "WARN":
        return "warning", text, None
    if tag == "CAM" or tag in QUIET_TAGS:
        return "status", text, None
    return "detail", f"[{tag}] {text}", None


def greeting() -> str:
    hour = datetime.now().hour
    if hour < 5:
        return "Working late"
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


class ToolNotice(QtWidgets.QFrame):
    """One collapsible notice listing tool table problems"""

    def __init__(self, parent=None, compact=False):
        super().__init__(parent)
        self.compact = compact
        self.items = []
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(4)
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(10)
        if compact:
            layout.setContentsMargins(0, 0, 0, 0)
            header.setSpacing(0)
            mark = terminal.text_label("●", C.amber)
            mark.setFixedWidth(terminal.GUTTER)
            header.addWidget(mark)
            self.title = terminal.text_label("", C.text)
            self.body = terminal.text_label("", C.text_3)
            self.body.setContentsMargins(terminal.GUTTER, 0, 0, 0)
        else:
            self.setObjectName("msg_card")
            self.setProperty("kind", "warning")
            layout.setContentsMargins(16, 8, 10, 8)
            icon = QtWidgets.QLabel()
            icon.setPixmap(theme.pixmap("wrench", C.amber, 20))
            header.addWidget(icon)
            self.title = kit.label("", "msg_card_text")
            self.body = kit.label("", "msg_card_text", wrap=True)
            self.body.setTextFormat(Qt.RichText)
        header.addWidget(self.title, 1)
        self.toggle = kit.Button("Show", variant="ghost", size="sm")
        self.toggle.clicked.connect(lambda: self.set_expanded(self.body.isHidden()))
        header.addWidget(self.toggle)
        layout.addLayout(header)
        self.body.setVisible(False)
        layout.addWidget(self.body)

    def add_item(self, text: str):
        self.items.append(text)
        count = len(self.items)
        self.title.setText(f"Tool table needs attention · {count} issue{'s' if count != 1 else ''}")
        bullet = terminal.HOOK + " " if self.compact else "• "
        self.body.setText("<br>".join(f"{bullet}{html.escape(item)}" for item in self.items)
                          + f"<br><span style='color:{C.text_3};'>Fix these on the Tools page.</span>")

    def set_expanded(self, expanded: bool):
        self.body.setVisible(expanded)
        self.toggle.setText("Hide" if expanded else "Show")


class ProgramCard(QtWidgets.QFrame):
    """A generated program: what it does, and buttons to look at it or run it"""

    view_clicked = QtCore.pyqtSignal()
    run_clicked = QtCore.pyqtSignal()

    def __init__(self, info: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("msg_card")
        self.setProperty("kind", "program")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)
        icon = QtWidgets.QLabel()
        icon.setPixmap(theme.pixmap("code", C.accent_hi, 26))
        head.addWidget(icon, 0, Qt.AlignTop)
        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(kit.label("Program ready", "eyebrow"))
        text.addWidget(kit.label(info.get("name") or "Generated program", "msg_card_title"))
        head.addLayout(text, 1)
        layout.addLayout(head)

        chips = kit.FlowLayout(spacing=8)
        holder = QtWidgets.QWidget()
        holder.setLayout(chips)
        for op in info.get("ops", [])[:8]:
            tool = f" · T{op['tool']}" if op.get("tool") is not None else ""
            chip = QtWidgets.QLabel(f"{op['name']}{tool}")
            chip.setStyleSheet(f"background: {C.bg}; border: 1px solid {C.line_hi}; border-radius: 14px;"
                               f"padding: 6px 12px; font-size: {T.label}px; color: {C.text};")
            chips.addWidget(chip)
        layout.addWidget(holder)

        facts = []
        if info.get("stock"):
            facts.append(("Stock", info["stock"]))
        for tool in info.get("tools", [])[:3]:
            desc = tool.get("description") or ""
            dia = f"⌀{tool['diameter']:g}" if isinstance(tool.get("diameter"), (int, float)) else ""
            facts.append((f"T{tool.get('number')}", " ".join(x for x in (dia, desc) if x)))
        if facts:
            grid = QtWidgets.QGridLayout()
            grid.setHorizontalSpacing(16)
            grid.setVerticalSpacing(4)
            for row, (key, value) in enumerate(facts):
                grid.addWidget(kit.label(key, "muted"), row, 0)
                value_label = kit.label(value, "msg_card_text")
                grid.addWidget(value_label, row, 1)
            grid.setColumnStretch(1, 1)
            layout.addLayout(grid)

        preview = info.get("preview")
        if preview and os.path.exists(preview):
            # The simulated finished part, from above
            picture = QtWidgets.QLabel()
            pixmap = QtGui.QPixmap(preview)
            if not pixmap.isNull():
                picture.setPixmap(pixmap.scaledToWidth(min(420, pixmap.width()), Qt.SmoothTransformation))
                picture.setStyleSheet(f"border: 1px solid {C.line_hi}; border-radius: 10px;")
                layout.addWidget(picture, 0, Qt.AlignLeft)
        for problem in info.get("problems") or []:
            layout.addWidget(kit.label(f"⚠ {problem}", "body", color=C.amber, wrap=True))

        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(10)
        view = kit.Button("View toolpath", icon="cube", variant="outline")
        view.clicked.connect(self.view_clicked.emit)
        run = kit.Button("Run…", icon="play-fill", variant="go")
        run.clicked.connect(self.run_clicked.emit)
        buttons.addWidget(view)
        buttons.addWidget(run)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        hint = kit.label("Loaded for review. Check the preview before running.", "muted")
        layout.addWidget(hint)


class Welcome(QtWidgets.QWidget):
    """The empty conversation: orb, greeting, what Milo can do"""

    example_clicked = QtCore.pyqtSignal(str)
    orb_clicked = QtCore.pyqtSignal()

    EXAMPLES = [
        ("arrows-out-cardinal", "Move the machine", "Move X ten millimeters"),
        ("circle-dashed", "Make a program", "Cut a 3 inch circle, 5 mm deep"),
        ("dots-nine", "Drill a pattern", "Drill 4 holes on a 60 mm bolt circle"),
        ("question", "Ask anything", "What tool is loaded, and how long is it?"),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("welcome")
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 24, 0, 16)
        layout.setSpacing(0)
        layout.addStretch(2)
        self.orb = Orb(diameter=112, show_icon=False)
        self.orb.clicked.connect(self.orb_clicked.emit)
        layout.addWidget(self.orb, 0, Qt.AlignHCenter)
        layout.addSpacing(18)
        self.title = kit.label(f"{greeting()}.", "welcome_title", align=Qt.AlignHCenter)
        layout.addWidget(self.title)
        layout.addSpacing(8)
        self.subtitle = kit.label("", "welcome_sub", wrap=True, align=Qt.AlignHCenter)
        layout.addWidget(self.subtitle)
        layout.addSpacing(34)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(14)
        for index, (icon, title, text) in enumerate(self.EXAMPLES):
            tile = QtWidgets.QPushButton()
            tile.setObjectName("example_tile")
            tile.setFocusPolicy(Qt.NoFocus)
            tile.setCursor(Qt.PointingHandCursor)
            tile.setMinimumHeight(104)
            tile.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
            inner = QtWidgets.QHBoxLayout(tile)
            inner.setContentsMargins(18, 14, 18, 14)
            inner.setSpacing(14)
            badge = QtWidgets.QLabel()
            badge.setFixedSize(44, 44)
            badge.setAlignment(Qt.AlignCenter)
            badge.setPixmap(theme.pixmap(icon, C.accent_hi, 24))
            badge.setStyleSheet(f"background: {C.accent_soft}; border-radius: 12px;")
            badge.setAttribute(Qt.WA_TransparentForMouseEvents)
            inner.addWidget(badge, 0, Qt.AlignTop)
            words = QtWidgets.QVBoxLayout()
            words.setSpacing(3)
            t1 = kit.label(title, "example_title")
            t2 = kit.label(f"“{text}”", "example_text", wrap=True)
            for w in (t1, t2):
                w.setAttribute(Qt.WA_TransparentForMouseEvents)
                words.addWidget(w)
            inner.addLayout(words, 1)
            tile.clicked.connect(lambda _=False, t=text: self.example_clicked.emit(t))
            grid.addWidget(tile, index // 2, index % 2)
        layout.addLayout(grid)
        layout.addStretch(3)

    def set_context(self, text: str):
        self.title.setText(f"{greeting()}.")
        self.subtitle.setText(text)


class Conversation(QtWidgets.QScrollArea):
    """Scrolling conversation column, centered, with a live 'Milo is working' row"""

    example_clicked = QtCore.pyqtSignal(str)
    orb_clicked = QtCore.pyqtSignal()
    view_program = QtCore.pyqtSignal()
    run_program = QtCore.pyqtSignal()
    style_changed = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("conversation")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        QtWidgets.QScroller.grabGesture(self.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        self._scroller_props()

        body = QtWidgets.QWidget()
        body.setObjectName("conversation_body")
        outer = QtWidgets.QHBoxLayout(body)
        outer.setContentsMargins(8, 8, 8, 8)
        self.column = QtWidgets.QWidget()
        self.column.setMaximumWidth(COLUMN_MAX_WIDTH)
        outer.addWidget(self.column)
        self._layout = QtWidgets.QVBoxLayout(self.column)
        self._layout.setContentsMargins(4, 12, 4, 20)
        self._layout.setSpacing(16)
        self._layout.addStretch(1)
        self.setWidget(body)

        self.welcome = Welcome()
        self.welcome.example_clicked.connect(self.example_clicked.emit)
        self.welcome.orb_clicked.connect(self.orb_clicked.emit)
        self._layout.insertWidget(0, self.welcome)

        self.working = self._build_working()
        self._layout.addWidget(self.working)
        self.working.hide()

        self._rows = []  # (widget, kind)
        self._history = []  # what was shown, to redraw it in another style: ("message", kind, text, code) / ("program", info)
        self.style = "bubbles"
        self._show_details = False
        self._stick_to_bottom = True
        self.verticalScrollBar().rangeChanged.connect(self._on_range_changed)
        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)

    def _scroller_props(self):
        scroller = QtWidgets.QScroller.scroller(self.viewport())
        props = scroller.scrollerProperties()
        props.setScrollMetric(QtWidgets.QScrollerProperties.OvershootDragResistanceFactor, 0.3)
        props.setScrollMetric(QtWidgets.QScrollerProperties.OvershootScrollDistanceFactor, 0.1)
        props.setScrollMetric(QtWidgets.QScrollerProperties.DragStartDistance, 0.004)
        scroller.setScrollerProperties(props)

    # --- public API ---------------------------------------------------------------------

    def add_log_line(self, message: str):
        kind, text, code = classify_log_line(message)
        self.add_message(kind, text, code)

    def add_message(self, kind: str, text: str, code: Optional[str] = None):
        self._remember(("message", kind, text, code))
        self._render_message(kind, text, code)

    def add_program(self, info: dict):
        self._remember(("program", info))
        self._render_program(info)

    def set_style(self, style: str):
        """Bubbles or compact; what's already in the conversation is redrawn"""
        style = style if style in STYLES else "bubbles"
        if style == self.style:
            return
        self.style = style
        compact = style == "compact"
        self.column.setMaximumWidth(COMPACT_MAX_WIDTH if compact else COLUMN_MAX_WIDTH)
        self._layout.setSpacing(10 if compact else 16)
        self._working_bubbles.setVisible(not compact)
        self._working_compact.setVisible(compact)
        for row, _ in self._rows:
            row.deleteLater()
        self._rows = []
        for item in self._history:
            if item[0] == "program":
                self._render_program(item[1])
            else:
                self._render_message(*item[1:])
        self.welcome.setVisible(self.is_empty())
        self._stick_to_bottom = True
        QtCore.QTimer.singleShot(0, self._scroll_to_end)
        self.style_changed.emit(style)

    def _remember(self, item):
        self._history.append(item)
        del self._history[:-MAX_MESSAGES]

    def _render_message(self, kind, text, code):
        if kind == "tool_warning":
            return self._add_tool_warning(text)
        if self.style == "compact":
            if kind == "user":
                widget = terminal.user_row(text)
            elif kind == "assistant":
                widget = terminal.milo_row(text)
            elif kind in ("detail", "status"):
                widget = terminal.note_row(kind, text)
            else:
                widget = terminal.event_row(kind, text, code)
        elif kind == "user":
            widget = self._user_row(text)
        elif kind == "assistant":
            widget = self._milo_row(text)
        elif kind in ("detail", "status"):
            widget = self._note_row(kind, text)
        else:
            widget = self._card_row(kind, text, code)
        self._append(widget, kind)

    def _render_program(self, info):
        if self.style == "compact":
            card = terminal.ProgramBlock(info)
            row = card
        else:
            card = ProgramCard(info)
            row = self._indent(card)
        card.view_clicked.connect(self.view_program.emit)
        card.run_clicked.connect(self.run_program.emit)
        self._append(row, "program")

    def set_working(self, busy: bool, text: str = ""):
        self.working.setVisible(busy)
        self.working_text.setText(text or "Thinking…")
        self._working_compact.set_text(text or "Thinking…")
        if busy:
            self._stick_to_bottom = True
            QtCore.QTimer.singleShot(0, self._scroll_to_end)

    def set_context(self, text: str):
        self.welcome.set_context(text)

    def clear(self):
        for row, _ in self._rows:
            row.deleteLater()
        self._rows = []
        self._history = []
        self.welcome.setVisible(True)

    def set_show_details(self, show: bool):
        self._show_details = show
        for row, kind in self._rows:
            if kind == "detail":
                row.setVisible(show)

    def tool_notice(self) -> Optional[ToolNotice]:
        for row, kind in reversed(self._rows):
            if kind == "tool_notice":
                return row if isinstance(row, ToolNotice) else row.findChild(ToolNotice)
        return None

    def is_empty(self) -> bool:
        return not any(k not in ("detail", "tool_notice", "status") for _, k in self._rows)

    # --- rows -------------------------------------------------------------------------------

    def _append(self, widget, kind):
        self._layout.insertWidget(self._layout.indexOf(self.working), widget)
        self._rows.append((widget, kind))
        widget.setVisible(kind != "detail" or self._show_details)
        self.welcome.setVisible(self.is_empty())
        self._trim()

    def _user_row(self, text):
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(80, 0, 0, 0)
        layout.addStretch(1)
        bubble = QtWidgets.QFrame()
        bubble.setObjectName("msg_user")
        inner = QtWidgets.QHBoxLayout(bubble)
        inner.setContentsMargins(20, 14, 20, 14)
        label = kit.label(text, "msg_user_text", wrap=True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        inner.addWidget(label)
        self._fit(label, text, 560)
        layout.addWidget(bubble)
        return row

    def _milo_row(self, text):
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 60, 0)
        layout.setSpacing(14)
        layout.addWidget(Mark(34), 0, Qt.AlignTop)
        label = kit.label("", "msg_milo_text", wrap=True)
        label.setTextFormat(Qt.RichText)
        label.setText(html.escape(text).replace("\n", "<br>"))
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        label.setContentsMargins(0, 4, 0, 0)
        layout.addWidget(label, 1)
        return row

    def _note_row(self, kind, text):
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(48, 0, 0, 0)
        label = kit.label(text, "msg_note", wrap=True)
        label.setProperty("kind", kind)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(label)
        return row

    def _card_row(self, kind, text, code):
        card = QtWidgets.QFrame()
        card.setObjectName("msg_card")
        card.setProperty("kind", kind)
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(14)
        icon_name, color = CARD_ICONS.get(kind, ("info-fill", C.blue))
        icon = QtWidgets.QLabel()
        icon.setPixmap(theme.pixmap(icon_name, color, 24))
        layout.addWidget(icon, 0, Qt.AlignTop)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(8)
        if kind == "confirm":
            words.addWidget(kit.label("ASKED TO CONFIRM", "eyebrow"))
        body = kit.label(text, "msg_card_title" if kind in ("confirm", "success") else "msg_card_text", wrap=True)
        body.setTextInteractionFlags(Qt.TextSelectableByMouse)
        words.addWidget(body)
        if code:
            chip = kit.label(code, "code_chip", wrap=True)
            chip.setTextInteractionFlags(Qt.TextSelectableByMouse)
            words.addWidget(chip, 0, Qt.AlignLeft)
        layout.addLayout(words, 1)
        return self._indent(card)

    def _indent(self, widget):
        row = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(row)
        layout.setContentsMargins(48, 0, 40, 0)
        layout.addWidget(widget)
        return row

    def _add_tool_warning(self, text):
        notice = self.tool_notice() if self._rows and self._rows[-1][1] == "tool_notice" else None
        if notice is None:
            notice = ToolNotice(compact=self.style == "compact")
            self._append(notice if notice.compact else self._indent(notice), "tool_notice")
        notice.add_item(text)

    def _build_working(self):
        """The "Milo is working" row, in both styles (one is shown)"""
        holder = QtWidgets.QWidget()
        outer = QtWidgets.QVBoxLayout(holder)
        outer.setContentsMargins(0, 0, 0, 0)
        self._working_bubbles = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(self._working_bubbles)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(Mark(34), 0, Qt.AlignVCenter)
        layout.addWidget(ThinkingDots(), 0, Qt.AlignVCenter)
        self.working_text = kit.label("Thinking…", "composer_status")
        layout.addWidget(self.working_text, 1)
        outer.addWidget(self._working_bubbles)
        self._working_compact = terminal.Working()
        self._working_compact.hide()
        outer.addWidget(self._working_compact)
        return holder

    @staticmethod
    def _fit(label, text, max_width):
        """Keep short bubbles on one line; wrap long ones at max_width"""
        label.ensurePolished()
        natural = max(label.fontMetrics().horizontalAdvance(line) for line in text.split("\n")) + 6
        label.setMinimumWidth(min(natural, max_width))
        label.setMaximumWidth(max_width)

    # --- behavior -----------------------------------------------------------------------------

    def _trim(self):
        while len(self._rows) > MAX_MESSAGES:
            row, _ = self._rows.pop(0)
            row.deleteLater()

    def _scroll_to_end(self):
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_scrolled(self, value):
        bar = self.verticalScrollBar()
        self._stick_to_bottom = value >= bar.maximum() - 32

    def _on_range_changed(self, _minimum, maximum):
        if self._stick_to_bottom:
            self.verticalScrollBar().setValue(maximum)
