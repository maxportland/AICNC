"""
OffsetMap: the machine's table from above (its X/Y travel, the camera photo when there is a scan),
with every work system's origin as a pin, the saved fixtures, and the tool. Tap a pin to pick it.
"""

import math
from typing import Dict, List, Optional, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme
from milo_ui.theme import C, T


class OffsetMap(QtWidgets.QWidget):
    picked = QtCore.pyqtSignal(str)  # a work system's name

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 260)
        self.limits: Dict[str, Tuple[float, float]] = {"X": (0.0, 500.0), "Y": (0.0, 175.0)}
        self.pins: List[dict] = []      # {name, label, xy, active, rotation}
        self.fixtures: List[Tuple[str, Tuple[float, float]]] = []
        self.tool: Optional[Tuple[float, float]] = None
        self.selected = ""
        self.scan = None
        self._image = None

    def set_scene(self, limits, pins, fixtures, tool, selected):
        self.limits, self.pins, self.fixtures, self.tool, self.selected = limits, pins, fixtures, tool, selected
        self.update()

    def set_scan(self, scan):
        self.scan = scan
        self._image = None
        if scan is not None and getattr(scan, "mosaic", None) is not None:
            image = scan.mosaic
            h, w = image.shape[:2]
            self._image = QtGui.QImage(image.data, w, h, image.strides[0], QtGui.QImage.Format_BGR888).copy()
        self.update()

    # --- mapping ---

    def _area(self) -> QtCore.QRectF:
        (x0, x1), (y0, y1) = self.limits["X"], self.limits["Y"]
        span_x, span_y = max(1.0, x1 - x0), max(1.0, y1 - y0)
        margin = 26
        w, h = self.width() - 2 * margin, self.height() - 2 * margin - 18
        scale = min(w / span_x, h / span_y)
        rect = QtCore.QRectF(0, 0, span_x * scale, span_y * scale)
        rect.moveCenter(QtCore.QPointF(self.width() / 2, (self.height() - 18) / 2))
        return rect

    def _map(self, x, y) -> QtCore.QPointF:
        (x0, x1), (y0, y1) = self.limits["X"], self.limits["Y"]
        area = self._area()
        return QtCore.QPointF(area.left() + (x - x0) / max(1.0, x1 - x0) * area.width(),
                              area.bottom() - (y - y0) / max(1.0, y1 - y0) * area.height())

    def _clamped(self, x, y) -> Tuple[QtCore.QPointF, bool]:
        """A point on the map, pulled inside the travel if it's outside (and whether it was)"""
        (x0, x1), (y0, y1) = self.limits["X"], self.limits["Y"]
        cx, cy = min(max(x, x0), x1), min(max(y, y0), y1)
        return self._map(cx, cy), (cx, cy) != (x, y)

    def mousePressEvent(self, event):
        best, best_d = None, 40.0
        for pin in self.pins:
            point, _ = self._clamped(*pin["xy"])
            d = QtCore.QLineF(event.pos(), point).length()
            if d < best_d:
                best, best_d = pin["name"], d
        if best:
            self.picked.emit(best)

    # --- drawing ---

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(C.bg))
        area = self._area()
        p.setPen(QtGui.QPen(QtGui.QColor(C.line_hi), 1.5))
        p.setBrush(QtGui.QColor(C.card))
        p.drawRoundedRect(area, 6, 6)
        if self._image is not None:
            ox, oy = self.scan.mosaic_origin
            ppm = self.scan.mosaic_px_per_mm
            w, h = self._image.width() / ppm, self._image.height() / ppm
            p.save()
            p.setClipRect(area)
            p.setOpacity(0.7)
            p.drawImage(QtCore.QRectF(self._map(ox, oy + h), self._map(ox + w, oy)), self._image)
            p.restore()
        # 50 mm grid
        (x0, x1), (y0, y1) = self.limits["X"], self.limits["Y"]
        p.setPen(QtGui.QPen(QtGui.QColor(C.line), 1))
        step = 50.0 if x1 - x0 > 60 else 1.0
        x = math.ceil(x0 / step) * step
        while x <= x1:
            p.drawLine(self._map(x, y0), self._map(x, y1))
            x += step
        y = math.ceil(y0 / step) * step
        while y <= y1:
            p.drawLine(self._map(x0, y), self._map(x1, y))
            y += step
        # Fixtures: hollow diamonds
        p.setFont(theme.font(T.caption))
        for name, (fx, fy) in self.fixtures:
            point, _ = self._clamped(fx, fy)
            p.setPen(QtGui.QPen(QtGui.QColor(C.text_3), 1.5))
            p.setBrush(Qt.NoBrush)
            diamond = QtGui.QPolygonF([QtCore.QPointF(point.x(), point.y() - 7), QtCore.QPointF(point.x() + 7, point.y()),
                                       QtCore.QPointF(point.x(), point.y() + 7), QtCore.QPointF(point.x() - 7, point.y())])
            p.drawPolygon(diamond)
            p.drawText(QtCore.QRectF(point.x() - 80, point.y() + 9, 160, 16), Qt.AlignHCenter, name)
        # Work origins: pins with their axes
        for pin in sorted(self.pins, key=lambda pin: pin["name"] == self.selected):
            point, outside = self._clamped(*pin["xy"])
            on, active = pin["name"] == self.selected, pin["active"]
            color = QtGui.QColor(C.accent_hi if on else C.green if active else C.text_2)
            angle = math.radians(pin.get("rotation", 0.0))
            for ax, ay, label in ((math.cos(angle), math.sin(angle), "X"), (-math.sin(angle), math.cos(angle), "Y")):
                end = QtCore.QPointF(point.x() + ax * 26, point.y() - ay * 26)
                p.setPen(QtGui.QPen(color, 2 if on else 1.2))
                p.drawLine(point, end)
            p.setPen(QtGui.QPen(QtGui.QColor("#FFFFFF") if on else color, 2))
            p.setBrush(color if (on or active) else QtGui.QColor(C.card_hi))
            p.drawEllipse(point, 8 if on else 6, 8 if on else 6)
            p.setPen(color)
            p.setFont(theme.font(T.caption, theme.SEMIBOLD if on else theme.MEDIUM))
            text = pin["label"] + ("  (off the table)" if outside else "")
            p.drawText(QtCore.QRectF(point.x() + 10, point.y() - 22, 260, 18), Qt.AlignLeft, text)
        # The tool
        if self.tool is not None:
            point, _ = self._clamped(*self.tool)
            p.setPen(QtGui.QPen(QtGui.QColor(C.amber), 2))
            p.drawLine(QtCore.QPointF(point.x() - 9, point.y()), QtCore.QPointF(point.x() + 9, point.y()))
            p.drawLine(QtCore.QPointF(point.x(), point.y() - 9), QtCore.QPointF(point.x(), point.y() + 9))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(point, 5, 5)
        p.setPen(QtGui.QColor(C.text_3))
        p.setFont(theme.font(T.caption))
        legend = "● origin   ◇ fixture   + tool   ·   back of the machine is up"
        p.drawText(QtCore.QRectF(8, self.height() - 20, self.width() - 16, 18), Qt.AlignLeft, legend)
