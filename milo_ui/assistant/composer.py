"""
Milo's presence in the dock, on every page:

Composer      the orb (tap to talk) and the "Ask Milo" input
ConfirmSheet  the floating card that asks the operator to confirm a machine action
Peek          a short-lived bubble with Milo's latest reply when the conversation isn't on screen
"""

import html
from typing import Callable, Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.assistant.orb import Orb, Mark

PLACEHOLDER = "Ask Milo to move, make a program, or answer a question…"
PLACEHOLDER_NO_KEY = "Add an OpenAI API key in Settings to talk to Milo"


class Composer(QtWidgets.QWidget):
    """Orb + input + send. Emits submitted(text) and voice_clicked()."""

    submitted = QtCore.pyqtSignal(str)
    voice_clicked = QtCore.pyqtSignal()
    cancel_listening = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)

        self.orb = Orb(diameter=64)
        self.orb.setToolTip("Talk to Milo (or say “Hey Milo”)")
        self.orb.clicked.connect(self.voice_clicked.emit)
        row.addWidget(self.orb, 0, Qt.AlignVCenter)

        self.frame = QtWidgets.QFrame()
        self.frame.setObjectName("composer")
        self.frame.setMinimumHeight(72)
        inner = QtWidgets.QHBoxLayout(self.frame)
        inner.setContentsMargins(26, 8, 10, 8)
        inner.setSpacing(12)
        self.input = QtWidgets.QLineEdit()
        self.input.setObjectName("composer_input")
        self.input.setPlaceholderText(PLACEHOLDER)
        self.input.returnPressed.connect(self._submit)
        self.input.textChanged.connect(self._update_send)
        self.input.installEventFilter(self)
        pal = self.input.palette()
        pal.setColor(QtGui.QPalette.PlaceholderText, QtGui.QColor(C.text_3))
        self.input.setPalette(pal)
        inner.addWidget(self.input, 1)

        self.status = kit.label("", "composer_status")
        self.status.hide()
        inner.addWidget(self.status)
        self.level = LevelBars()
        self.level.hide()
        inner.addWidget(self.level)
        self.stop_listening = kit.Button("Cancel", variant="ghost", size="sm")
        self.stop_listening.hide()
        self.stop_listening.clicked.connect(self.cancel_listening.emit)
        inner.addWidget(self.stop_listening)

        self.send = QtWidgets.QPushButton()
        self.send.setObjectName("send_button")
        self.send.setFocusPolicy(Qt.NoFocus)
        self.send.setCursor(Qt.PointingHandCursor)
        self.send.setFixedSize(52, 52)
        self.send.setIcon(theme.icon("arrow-up", color="#0B0A1A", color_disabled=C.text_4))
        self.send.setIconSize(QtCore.QSize(24, 24))
        self.send.clicked.connect(self._submit)
        inner.addWidget(self.send)
        row.addWidget(self.frame, 1)
        self._busy = False
        self._update_send()

    def eventFilter(self, obj, event):
        if obj is self.input and event.type() in (QtCore.QEvent.FocusIn, QtCore.QEvent.FocusOut):
            theme.set_prop(self.frame, "focused", event.type() == QtCore.QEvent.FocusIn)
        return False

    def _submit(self):
        text = self.input.text().strip()
        if text and self.send.isEnabled():
            self.submitted.emit(text)

    def _update_send(self):
        self.send.setEnabled(bool(self.input.text().strip()) and not self._busy)

    # --- state from the engine ----------------------------------------------------------

    _available = True
    _voice = "idle"
    _busy_text = ""

    def set_ai_available(self, available: bool):
        self._available = available
        self.input.setPlaceholderText(PLACEHOLDER if available else PLACEHOLDER_NO_KEY)
        self._refresh()

    def set_voice_state(self, state: str):
        self._voice = state
        self._refresh()

    def set_busy(self, busy: bool, text: str):
        self._busy, self._busy_text = busy, text
        self._refresh()
        self._update_send()

    def set_level(self, level: float):
        self.orb.set_level(level)
        self.level.set_level(level)

    def _refresh(self):
        voice = self._voice
        if not self._available:
            orb = "disabled"
        elif voice in ("listening", "transcribing"):
            orb = voice
        elif self._busy:
            orb = "thinking"
        else:
            orb = "idle"
        self.orb.set_state(orb)
        status = {"listening": "Listening", "transcribing": "Transcribing\u2026"}.get(voice)
        if status is None and self._busy:
            status = self._busy_text
        self.status.setText(status or "")
        self.status.setVisible(bool(status))
        self.level.setVisible(voice == "listening")
        self.stop_listening.setVisible(voice in ("listening", "transcribing"))

    def set_text(self, text):
        self.input.setText(text)
        self.input.setCursorPosition(len(text))

    def clear(self):
        self.input.clear()


class LevelBars(QtWidgets.QWidget):
    """A tiny live microphone meter"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(54, 32)
        self.levels = [0.0] * 7

    def set_level(self, level):
        self.levels = self.levels[1:] + [max(0.05, min(1.0, level * 1.6))]
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(C.accent_2))
        w = 4
        for i, level in enumerate(self.levels):
            h = max(4.0, level * self.height())
            p.drawRoundedRect(QtCore.QRectF(i * (w + 3.5), (self.height() - h) / 2, w, h), 2, 2)


class ConfirmSheet(QtWidgets.QFrame):
    """
    The confirmation card for a machine action Milo proposed.

    Floats above the dock on whatever page is showing, with a countdown ring for the
    gate's timeout. Buttons call confirm/cancel; the operator can also say yes/no.
    """

    confirmed = QtCore.pyqtSignal(object)  # the id of the action the operator was looking at
    cancelled = QtCore.pyqtSignal()

    def __init__(self, host: QtWidgets.QWidget, remaining: Callable[[], float], total_seconds=30):
        super().__init__(host)
        self.host = host
        self.remaining = remaining
        self.total = float(total_seconds)
        self.setObjectName("confirm_sheet")
        self.setFixedWidth(980)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(24, 20, 20, 20)
        layout.setSpacing(20)

        self.ring = kit.ProgressArc(64, 5, C.amber)
        layout.addWidget(self.ring, 0, Qt.AlignVCenter)

        words = QtWidgets.QVBoxLayout()
        words.setSpacing(6)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(Mark(22))
        top.addWidget(kit.label("MILO WANTS TO", "confirm_eyebrow"))
        top.addStretch(1)
        words.addLayout(top)
        self.summary = kit.label("", "confirm_summary", wrap=True)
        words.addWidget(self.summary)
        self.code = kit.label("", "code_chip", wrap=True)
        words.addWidget(self.code, 0, Qt.AlignLeft)
        self.hint = kit.label("Say “yes” or “no”, or tap a button", "confirm_hint")
        words.addWidget(self.hint)
        layout.addLayout(words, 1)

        self.cancel_button = kit.Button("Cancel", icon="x", variant="outline", size="lg")
        self.cancel_button.setMinimumWidth(170)
        self.cancel_button.clicked.connect(self.cancelled.emit)
        self.confirm_button = kit.Button("Confirm", icon="check", variant="warn", size="lg")
        self.confirm_button.setMinimumWidth(220)
        self.confirm_button.clicked.connect(lambda: self.confirmed.emit(self._action_id))
        self._action_id = None
        layout.addWidget(self.cancel_button, 0, Qt.AlignVCenter)
        layout.addWidget(self.confirm_button, 0, Qt.AlignVCenter)

        effect = QtWidgets.QGraphicsDropShadowEffect(self)
        effect.setBlurRadius(70)
        effect.setOffset(0, 16)
        effect.setColor(theme.alpha("#000000", 0.7))
        self.setGraphicsEffect(effect)

        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._anchor_bottom = 120
        self.hide()
        host.installEventFilter(self)

    def eventFilter(self, obj, event):
        try:
            if obj is self.host and event.type() == QtCore.QEvent.Resize and self.isVisible():
                self._place()
        except (RuntimeError, AttributeError):  # during teardown
            pass
        return False

    def show_action(self, action: dict, bottom_offset: int, center_x: Optional[int] = None):
        self._anchor_bottom = bottom_offset
        self._center_x = center_x
        self._action_id = action.get("id")
        self.summary.setText(action.get("summary", ""))
        command = action.get("command") or ""
        self.code.setText(command)
        self.code.setVisible(bool(command))
        verb = {"power_on": "Turn on", "power_off": "Turn off", "run": "Run", "home": "Home",
                "probe": "Probe"}.get(action.get("kind"))
        self.confirm_button.setText(verb or "Confirm")
        self.confirm_button.set_variant("go" if action.get("kind") == "run" else "warn")
        self._tick()
        self._timer.start(100)
        self._place()
        self.show()
        self.raise_()

    def dismiss(self):
        self._timer.stop()
        self.hide()

    def _place(self):
        self.adjustSize()
        cx = self._center_x if getattr(self, "_center_x", None) is not None else self.host.width() // 2
        x = max(16, min(self.host.width() - self.width() - 16, cx - self.width() // 2))
        self.move(x, self.host.height() - self._anchor_bottom - self.height() - 16)

    def _tick(self):
        left = self.remaining()
        self.ring.set_fraction(left / self.total if self.total else 0, f"{int(left + 0.99)}")


class Peek(QtWidgets.QFrame):
    """Milo's latest reply, shown above the dock when the conversation isn't visible"""

    open_clicked = QtCore.pyqtSignal()

    def __init__(self, host: QtWidgets.QWidget):
        super().__init__(host)
        self.host = host
        self.setObjectName("toast")
        self.setFixedWidth(720)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(18, 14, 12, 14)
        layout.setSpacing(14)
        layout.addWidget(Mark(34), 0, Qt.AlignTop)
        self.text = kit.label("", "msg_milo_text", wrap=True)
        self.text.setTextFormat(Qt.RichText)
        layout.addWidget(self.text, 1)
        self.open_button = kit.Button("Open", icon="chat-teardrop-dots", variant="ghost", size="sm")
        self.open_button.clicked.connect(lambda: (self.hide(), self.open_clicked.emit()))
        layout.addWidget(self.open_button, 0, Qt.AlignTop)
        close = kit.RoundButton("x", 44, variant="ghost", icon_size=18, on_click=self.hide)
        layout.addWidget(close, 0, Qt.AlignTop)
        self._hide_timer = QtCore.QTimer(self, singleShot=True)
        self._hide_timer.timeout.connect(self.hide)
        effect = QtWidgets.QGraphicsDropShadowEffect(self)
        effect.setBlurRadius(50)
        effect.setOffset(0, 12)
        effect.setColor(theme.alpha("#000000", 0.6))
        self.setGraphicsEffect(effect)
        self.hide()

    def show_text(self, text, level, bottom_offset, left_offset):
        color = {"error": C.red, "warning": C.amber, "success": C.green}.get(level)
        body = html.escape(text).replace("\n", "<br>")
        if color:
            body = f"<span style='color:{color}'>{body}</span>"
        self.text.setText(body)
        self.adjustSize()
        self.move(left_offset, self.host.height() - bottom_offset - self.height() - 16)
        self.show()
        self.raise_()
        self._hide_timer.start(9000 if level == "error" else 7000)
