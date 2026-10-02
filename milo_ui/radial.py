"""
The quick menu: a ring of slices over the screen, chosen with a pendant stick (tilt, then A)
or by tapping. Opened from the pendant's quick-menu button.
"""

import math

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme
from milo_ui.theme import C, T

OUTER = 300  # px, ring radius
INNER = 120


class RadialMenu(QtWidgets.QWidget):
    """Covers its parent; draws the ring centred on it"""

    picked = QtCore.pyqtSignal(int)  # a tap on a slice
    dismissed = QtCore.pyqtSignal()  # a tap outside the ring

    def __init__(self, parent):
        super().__init__(parent)
        self.items = []  # [(label, icon)]
        self.hover = -1
        self.hint = ""
        self.default_hint = ""
        self.title = "Quick menu"
        self.setAttribute(Qt.WA_StyledBackground, False)
        self.hide()

    def open(self, items, default_hint, title="Quick menu"):
        self.items = list(items)
        self.title = title
        self.hover = -1
        self.default_hint = default_hint
        self.hint = default_hint
        self.setGeometry(self.parentWidget().rect())
        self.show()
        self.raise_()
        self.update()

    def set_items(self, items):
        """Relabel the slices in place (same count, so the highlighted one stays)"""
        self.items = list(items)
        self.update()

    def set_hover(self, index):
        self.hover = index
        self.hint = self.default_hint
        self.update()

    def set_hint(self, text):
        self.hint = text
        self.update()

    # --- geometry ---

    def _centre(self):
        return QtCore.QPointF(self.width() / 2, self.height() / 2)

    def slice_at_point(self, point):
        """Slice under a screen point, -1 if not on the ring"""
        c = self._centre()
        dx, dy = point.x() - c.x(), c.y() - point.y()
        distance = math.hypot(dx, dy)
        if not self.items or distance < INNER or distance > OUTER:
            return -1
        from milo_ui.pendant import slice_at
        return slice_at(dx, dy, len(self.items))

    def mousePressEvent(self, event):
        index = self.slice_at_point(event.pos())
        if index >= 0:
            self.picked.emit(index)
        elif math.hypot(event.pos().x() - self._centre().x(), event.pos().y() - self._centre().y()) > OUTER:
            self.dismissed.emit()

    # --- drawing ---

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 160))
        c = self._centre()
        # A solid disc behind the ring, so the page doesn't show through the gaps
        p.setPen(QtGui.QPen(QtGui.QColor(C.line), 1))
        p.setBrush(QtGui.QColor(C.bg))
        p.drawEllipse(c, OUTER + 14, OUTER + 14)
        n = max(1, len(self.items))
        span = 360.0 / n
        gap = 2.0  # degrees between slices
        for i, (label, icon) in enumerate(self.items):
            # Qt angles: 0 = 3 o'clock, counter-clockwise; slice i is centred on i*span clockwise from 12
            mid = 90.0 - i * span
            start = mid - span / 2 + gap / 2
            path = QtGui.QPainterPath()
            outer = QtCore.QRectF(c.x() - OUTER, c.y() - OUTER, 2 * OUTER, 2 * OUTER)
            inner = QtCore.QRectF(c.x() - INNER, c.y() - INNER, 2 * INNER, 2 * INNER)
            path.arcMoveTo(outer, start)
            path.arcTo(outer, start, span - gap)
            path.arcTo(inner, start + span - gap, -(span - gap))
            path.closeSubpath()
            on = i == self.hover
            p.setPen(QtGui.QPen(QtGui.QColor(C.accent if on else C.line_hi), 3 if on else 1.5))
            p.setBrush(QtGui.QColor(C.accent_soft if on else C.card))
            p.drawPath(path)
            # Icon and label at the middle of the slice
            r = (OUTER + INNER) / 2
            a = math.radians(mid)
            spot = QtCore.QPointF(c.x() + r * math.cos(a), c.y() - r * math.sin(a))
            pixmap = theme.pixmap(icon, C.accent_hi if on else C.text_2, 40)
            p.drawPixmap(QtCore.QPointF(spot.x() - 20, spot.y() - 34), pixmap)
            p.setPen(QtGui.QColor(C.text if on else C.text_2))
            p.setFont(theme.font(T.label, theme.SEMIBOLD if on else theme.MEDIUM))
            p.drawText(QtCore.QRectF(spot.x() - 85, spot.y() + 10, 170, 44), Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                       label)
        # Centre: what's highlighted and what to press
        p.setPen(QtGui.QPen(QtGui.QColor(C.line_hi), 1.5))
        p.setBrush(QtGui.QColor(C.bg))
        p.drawEllipse(c, INNER - 10, INNER - 10)
        title = self.items[self.hover][0] if 0 <= self.hover < len(self.items) else self.title
        p.setPen(QtGui.QColor(C.text))
        p.setFont(theme.font(T.body_lg, theme.SEMIBOLD))
        p.drawText(QtCore.QRectF(c.x() - 100, c.y() - 50, 200, 50), Qt.AlignHCenter | Qt.AlignBottom | Qt.TextWordWrap,
                   title)
        p.setPen(QtGui.QColor(C.amber if self.hint != self.default_hint else C.text_3))
        p.setFont(theme.font(T.caption))
        p.drawText(QtCore.QRectF(c.x() - 95, c.y() + 6, 190, 60), Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                   self.hint)


class AdjustPanel(QtWidgets.QWidget):
    """A value on a stepped slider (spindle speed, feed), set from the pendant or by touch"""

    dragged = QtCore.pyqtSignal(float)  # touch: a new value
    apply_clicked = QtCore.pyqtSignal()
    cancel_clicked = QtCore.pyqtSignal()

    WIDTH, HEIGHT = 900, 380
    TRACK_MARGIN = 70

    def __init__(self, parent):
        super().__init__(parent)
        from milo_ui import kit
        self.spec = {}
        self.value = 0.0
        self.apply_button = kit.Button("Apply", icon="check", variant="primary", size="lg", parent=self)
        self.cancel_button = kit.Button("Cancel", icon="x", size="lg", parent=self)
        self.apply_button.clicked.connect(self.apply_clicked.emit)
        self.cancel_button.clicked.connect(self.cancel_clicked.emit)
        self.hide()

    def open(self, spec, hint):
        self.spec = dict(spec)
        self.hint = hint
        self.setGeometry(self.parentWidget().rect())
        panel = self._panel()
        for i, button in enumerate((self.cancel_button, self.apply_button)):
            button.setFixedSize(180, 60)
            button.move(int(panel.right() - 32 - 380 + i * 200), int(panel.bottom() - 84))
        self.show()
        self.raise_()

    def set_value(self, value):
        self.value = value
        self.update()

    def _panel(self):
        return QtCore.QRectF((self.width() - self.WIDTH) / 2, (self.height() - self.HEIGHT) / 2, self.WIDTH, self.HEIGHT)

    def _track(self):
        panel = self._panel()
        return QtCore.QRectF(panel.left() + self.TRACK_MARGIN, panel.top() + 200,
                             panel.width() - 2 * self.TRACK_MARGIN, 0)

    def _fraction(self, value):
        lo, hi = float(self.spec.get("min", 0)), float(self.spec.get("max", 1))
        return 0.0 if hi <= lo else (value - lo) / (hi - lo)

    def _text(self, value):
        unit = self.spec.get("unit", "")
        return f"{value:,.0f} {unit}".strip()

    def mousePressEvent(self, event):
        self._drag(event.pos())

    def mouseMoveEvent(self, event):
        self._drag(event.pos())

    def _drag(self, pos):
        track = self._track()
        if abs(pos.y() - track.top()) > 50 or not track.left() - 30 <= pos.x() <= track.right() + 30:
            return
        fraction = max(0.0, min(1.0, (pos.x() - track.left()) / track.width()))
        lo, hi = float(self.spec["min"]), float(self.spec["max"])
        self.dragged.emit(lo + fraction * (hi - lo))

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(0, 0, 0, 160))
        panel = self._panel()
        p.setPen(QtGui.QPen(QtGui.QColor(C.line_hi), 1.5))
        p.setBrush(QtGui.QColor(C.card))
        p.drawRoundedRect(panel, 24, 24)
        p.setPen(QtGui.QColor(C.text_3))
        p.setFont(theme.font(T.label, theme.SEMIBOLD))
        p.drawText(QtCore.QRectF(panel.left() + 32, panel.top() + 24, 600, 28), Qt.AlignLeft,
                   self.spec.get("title", "").upper())
        p.setPen(QtGui.QColor(C.text))
        p.setFont(theme.font(T.display, theme.SEMIBOLD, mono=True))
        p.drawText(QtCore.QRectF(panel.left(), panel.top() + 56, panel.width(), 90), Qt.AlignCenter,
                   self._text(self.value))
        current = self.spec.get("current_text")
        if current:
            p.setPen(QtGui.QColor(C.text_3))
            p.setFont(theme.font(T.label))
            p.drawText(QtCore.QRectF(panel.left(), panel.top() + 146, panel.width(), 24), Qt.AlignCenter, current)
        # Track with a tick at every step
        track = self._track()
        y = track.top()
        p.setPen(QtGui.QPen(QtGui.QColor(C.line_hi), 8, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QtCore.QPointF(track.left(), y), QtCore.QPointF(track.right(), y))
        x = track.left() + self._fraction(self.value) * track.width()
        p.setPen(QtGui.QPen(QtGui.QColor(C.accent), 8, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QtCore.QPointF(track.left(), y), QtCore.QPointF(x, y))
        lo, hi, step = float(self.spec["min"]), float(self.spec["max"]), float(self.spec["step"])
        # Ticks at the round-number steps the value snaps to
        first = math.ceil(lo / step - 1e-9) if step > 0 else 0
        last = math.floor(hi / step + 1e-9) if step > 0 else -1
        every = max(1, (last - first) // 40)  # don't draw more than ~40 ticks
        p.setPen(QtGui.QPen(QtGui.QColor(C.text_4), 2))
        for k in range(first, last + 1, every):
            tx = track.left() + self._fraction(k * step) * track.width()
            major = k % (every * 5) == 0
            p.drawLine(QtCore.QPointF(tx, y + 14), QtCore.QPointF(tx, y + (26 if major else 20)))
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor("#FFFFFF"))
        p.drawEllipse(QtCore.QPointF(x, y), 16, 16)
        p.setPen(QtGui.QColor(C.text_3))
        p.setFont(theme.font(T.caption))
        p.drawText(QtCore.QRectF(track.left() - 40, y + 30, 80, 20), Qt.AlignCenter, self._text(lo))
        p.drawText(QtCore.QRectF(track.right() - 40, y + 30, 80, 20), Qt.AlignCenter, self._text(hi))
        p.drawText(QtCore.QRectF(panel.left() + 32, panel.bottom() - 74, panel.width() - 470, 44),
                   Qt.AlignLeft | Qt.AlignVCenter | Qt.TextWordWrap, self.hint)
