"""
Milo's orb: the one visual that says "the assistant", everywhere it appears.

States:
  idle          slow breathing glow
  listening     rings that swell with the microphone level
  transcribing  a bright arc sweeping around the edge
  thinking      the gradient itself rotates
  disabled      flat and dim (no API key)

The orb only animates while it is visible and not idle-static, to stay cheap on the Pi.
"""

import math
import time

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme
from milo_ui.theme import C


class Orb(QtWidgets.QAbstractButton):
    """A painted, animated orb. Clickable (the composer uses it as the microphone button)."""

    def __init__(self, diameter=72, show_icon=True, parent=None):
        super().__init__(parent)
        self.diameter = diameter
        self.show_icon = show_icon
        self.state = "idle"
        self.level = 0.0
        self._smooth = 0.0
        self._t0 = time.time()
        self.setFixedSize(diameter + 24, diameter + 24)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(40)

    def set_state(self, state):
        if state != self.state:
            self.state = state
            if state != "listening":
                self.level = 0.0
            self.update()

    def set_level(self, level):
        self.level = max(0.0, min(1.0, float(level)))

    def _tick(self):
        if not self.isVisible():
            return
        self._smooth += (self.level - self._smooth) * 0.35
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        t = time.time() - self._t0
        c = QtCore.QPointF(self.width() / 2, self.height() / 2)
        r = self.diameter / 2
        disabled = self.state == "disabled" or not self.isEnabled()

        # glow / rings
        if not disabled:
            if self.state == "listening":
                for i in range(3):
                    phase = (t * 0.9 + i / 3.0) % 1.0
                    ring_r = r + 2 + phase * (10 + 16 * self._smooth)
                    color = QtGui.QColor(C.accent_2)
                    color.setAlphaF(max(0.0, 0.45 * (1 - phase)) * (0.5 + self._smooth))
                    p.setPen(QtGui.QPen(color, 2.5))
                    p.setBrush(Qt.NoBrush)
                    p.drawEllipse(c, ring_r, ring_r)
            breathe = 0.5 + 0.5 * math.sin(t * (1.2 if self.state == "idle" else 3.0))
            glow = QtGui.QRadialGradient(c, r + 12)
            base = QtGui.QColor(C.accent)
            base.setAlphaF(0.20 + 0.18 * breathe + (0.3 * self._smooth if self.state == "listening" else 0))
            glow.setColorAt(0.65, base)
            glow.setColorAt(1.0, QtGui.QColor(0, 0, 0, 0))
            p.setPen(Qt.NoPen)
            p.setBrush(glow)
            p.drawEllipse(c, r + 12, r + 12)

        # body
        if disabled:
            p.setPen(QtGui.QPen(QtGui.QColor(C.line_hi), 1.5))
            p.setBrush(QtGui.QColor(C.card_hi))
            p.drawEllipse(c, r, r)
        else:
            spin = {"thinking": 140, "transcribing": 220}.get(self.state, 18)
            angle = (t * spin) % 360
            body = QtGui.QConicalGradient(c, angle)
            body.setColorAt(0.0, QtGui.QColor(C.accent))
            body.setColorAt(0.35, QtGui.QColor("#6D5CF6"))
            body.setColorAt(0.6, QtGui.QColor(C.accent_2))
            body.setColorAt(0.85, QtGui.QColor("#B39DFF"))
            body.setColorAt(1.0, QtGui.QColor(C.accent))
            p.setPen(Qt.NoPen)
            p.setBrush(body)
            scale = 1.0 + (0.06 * self._smooth if self.state == "listening" else 0.0)
            p.drawEllipse(c, r * scale, r * scale)
            # glassy highlight
            hi = QtGui.QRadialGradient(QtCore.QPointF(c.x() - r * 0.3, c.y() - r * 0.4), r * 1.1)
            hi.setColorAt(0.0, QtGui.QColor(255, 255, 255, 110))
            hi.setColorAt(0.5, QtGui.QColor(255, 255, 255, 12))
            hi.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
            p.setBrush(hi)
            p.drawEllipse(c, r * scale, r * scale)
            # inner shade for depth
            shade = QtGui.QRadialGradient(c, r)
            shade.setColorAt(0.7, QtGui.QColor(0, 0, 0, 0))
            shade.setColorAt(1.0, QtGui.QColor(10, 6, 40, 90))
            p.setBrush(shade)
            p.drawEllipse(c, r * scale, r * scale)
            if self.state == "transcribing":
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 220), 3, Qt.SolidLine, Qt.RoundCap))
                p.setBrush(Qt.NoBrush)
                arc = QtCore.QRectF(c.x() - r + 5, c.y() - r + 5, 2 * r - 10, 2 * r - 10)
                p.drawArc(arc, int(-t * 360 * 16) % (360 * 16), 90 * 16)

        if self.isDown():
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(0, 0, 0, 60))
            p.drawEllipse(c, r, r)

        if self.show_icon:
            name = {"listening": "stop-fill", "transcribing": "dots-three",
                    "thinking": "sparkle-fill"}.get(self.state, "microphone-fill")
            color = C.text_3 if disabled else "#FFFFFF"
            size = int(r * 0.8)
            pm = theme.pixmap(name, color, size)
            p.drawPixmap(int(c.x() - size / 2), int(c.y() - size / 2), pm)


class Mark(QtWidgets.QWidget):
    """Milo's small static avatar next to its messages"""

    def __init__(self, diameter=34, parent=None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self.width() / 2 - 1
        c = QtCore.QPointF(self.width() / 2, self.height() / 2)
        g = QtGui.QConicalGradient(c, 30)
        g.setColorAt(0.0, QtGui.QColor(C.accent))
        g.setColorAt(0.5, QtGui.QColor(C.accent_2))
        g.setColorAt(1.0, QtGui.QColor(C.accent))
        p.setPen(Qt.NoPen)
        p.setBrush(g)
        p.drawEllipse(c, r, r)
        hi = QtGui.QRadialGradient(QtCore.QPointF(c.x() - r * 0.3, c.y() - r * 0.4), r)
        hi.setColorAt(0.0, QtGui.QColor(255, 255, 255, 120))
        hi.setColorAt(1.0, QtGui.QColor(255, 255, 255, 0))
        p.setBrush(hi)
        p.drawEllipse(c, r, r)


class ThinkingDots(QtWidgets.QWidget):
    """Three dots bouncing in sequence"""

    def __init__(self, parent=None, color=None):
        super().__init__(parent)
        self.setFixedSize(46, 20)
        self.color = QtGui.QColor(color or C.accent_hi)
        self._t0 = time.time()
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.update)
        self._timer.start(50)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        t = time.time() - self._t0
        p.setPen(Qt.NoPen)
        for i in range(3):
            phase = (t * 1.6 - i * 0.18) % 1.0
            lift = max(0.0, math.sin(phase * math.pi * 2)) * 5
            color = QtGui.QColor(self.color)
            color.setAlphaF(0.45 + 0.55 * (lift / 5))
            p.setBrush(color)
            p.drawEllipse(QtCore.QPointF(8 + i * 15, 12 - lift), 4.5, 4.5)
