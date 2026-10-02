"""
Drawings for the Probe page:

PartPicker    a block (or a pocket) seen from above; tap the corner or edge to probe
ProbePreview  what the probe will do, from above: the part it expects, where it starts, each touch
              (where it goes down, which way it searches and how far, where it should meet the part).
              With a camera scan, the real part is drawn on the table photo and can be tapped.
"""

import math
from typing import List, Optional, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme
from milo_ui.theme import C, T

import probe_jobs as pj

BACK_IS_UP = "Back of the machine is up"


def _pen(color, width=2.0, style=Qt.SolidLine):
    pen = QtGui.QPen(QtGui.QColor(color), width, style)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    return pen


def _arrow(p: QtGui.QPainter, a: QtCore.QPointF, b: QtCore.QPointF, color, width=2.5, head=10.0):
    p.setPen(_pen(color, width))
    p.drawLine(a, b)
    angle = math.atan2(b.y() - a.y(), b.x() - a.x())
    for side in (-0.45, 0.45):
        tip = QtCore.QPointF(b.x() - head * math.cos(angle + side), b.y() - head * math.sin(angle + side))
        p.drawLine(b, tip)


class PartPicker(QtWidgets.QWidget):
    """Tap a corner or an edge. Emits picked('front_left' / 'left' ...)."""

    picked = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "corner"  # corner | edge
        self.inside = False
        self.selected = "front_left"
        self.setMinimumSize(320, 210)
        self.setCursor(Qt.PointingHandCursor)

    def set_state(self, mode, selected, inside=False):
        self.mode, self.selected, self.inside = mode, selected, inside
        self.update()

    def _block(self) -> QtCore.QRectF:
        w, h = self.width(), self.height() - 28
        size = min(w * 0.62, h * 0.72)
        return QtCore.QRectF((w - size * 1.25) / 2, (h - size * 0.8) / 2 + 4, size * 1.25, size * 0.8)

    def _spots(self):
        """name -> (point, kind) of the tappable places"""
        r = self._block()
        if self.mode == "corner":
            return {"back_left": r.topLeft(), "back_right": r.topRight(),
                    "front_left": r.bottomLeft(), "front_right": r.bottomRight()}
        return {"back": QtCore.QPointF(r.center().x(), r.top()), "front": QtCore.QPointF(r.center().x(), r.bottom()),
                "left": QtCore.QPointF(r.left(), r.center().y()), "right": QtCore.QPointF(r.right(), r.center().y())}

    def mousePressEvent(self, event):
        spots = self._spots()
        name = min(spots, key=lambda k: QtCore.QLineF(event.pos(), spots[k]).length())
        if QtCore.QLineF(event.pos(), spots[name]).length() < 90:
            self.selected = name
            self.update()
            self.picked.emit(name)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = self._block()
        material, edge = QtGui.QColor(C.card_top), QtGui.QColor(C.line_hi)
        if self.mode == "corner" and self.inside:
            # A plate with a pocket: the tappable corners are the pocket's
            outer = r.adjusted(-34, -26, 34, 26)
            p.setPen(_pen(edge, 1.5))
            p.setBrush(material)
            p.drawRoundedRect(outer, 10, 10)
            p.setBrush(QtGui.QColor(C.bg))
            p.drawRect(r)
        else:
            p.setPen(_pen(edge, 1.5))
            p.setBrush(material)
            p.drawRect(r)
        for name, point in self._spots().items():
            on = name == self.selected
            color = QtGui.QColor(C.accent_hi if on else C.text_3)
            if self.mode == "corner":
                p.setPen(_pen(color, 3 if on else 1.5))
                p.setBrush(QtGui.QColor(C.accent_soft) if on else Qt.NoBrush)
                p.drawEllipse(point, 18 if on else 13, 18 if on else 13)
            else:
                p.setPen(_pen(color, 7 if on else 3))
                horizontal = name in ("front", "back")
                half = (r.width() if horizontal else r.height()) * 0.38
                a = QtCore.QPointF(point.x() - half, point.y()) if horizontal else QtCore.QPointF(point.x(), point.y() - half)
                b = QtCore.QPointF(point.x() + half, point.y()) if horizontal else QtCore.QPointF(point.x(), point.y() + half)
                p.drawLine(a, b)
        p.setPen(QtGui.QColor(C.text_3))
        p.setFont(theme.font(T.caption, theme.SEMIBOLD))
        p.drawText(QtCore.QRectF(0, r.top() - 26, self.width(), 18), Qt.AlignHCenter, "BACK")
        p.drawText(QtCore.QRectF(0, self.height() - 22, self.width(), 18), Qt.AlignHCenter, "FRONT  ·  you")


class ProbePreview(QtWidgets.QWidget):
    """
    The plan, from above, in machine-unit millimetres. Schematic: the nominal part around a start
    point. Camera: the table photo, the parts found on it, the probe where it really is, and the plan
    drawn from there. Emits tapped(machine_x, machine_y) for taps on the photo.
    """

    tapped = QtCore.pyqtSignal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(420, 380)
        self.plan: Optional[pj.Plan] = None
        self.tip = 2.0
        self.units = "mm"
        self.scan = None          # milo_vision ScanResult, for the camera view
        self.part_index = -1      # the selected camera part
        self.probe_xy = None      # the probe's machine XY (camera view)
        self.target_xy = None     # where the probe should start (camera view)
        self.problem = ""
        self._image = None
        self._view = None         # (scale px/mm, origin machine XY at the widget centre)

    def set_plan(self, plan, tip, units):
        self.plan, self.tip, self.units = plan, tip, units
        self.update()

    def set_scan(self, scan, part_index=-1):
        self.scan, self.part_index = scan, part_index
        self._image = None
        if scan is not None and getattr(scan, "mosaic", None) is not None:
            image = scan.mosaic
            h, w = image.shape[:2]
            self._image = QtGui.QImage(image.data, w, h, image.strides[0], QtGui.QImage.Format_BGR888).copy()
        self.update()

    def set_live(self, probe_xy, target_xy=None):
        self.probe_xy, self.target_xy = probe_xy, target_xy
        self.update()

    @property
    def camera(self) -> bool:
        return self.scan is not None and 0 <= self.part_index < len(self.scan.parts)

    # --- mapping ---

    def _fit(self, xs, ys, margin=48):
        w, h = max(1, self.width() - 2 * margin), max(1, self.height() - 2 * margin - 24)
        span_x, span_y = max(1e-3, max(xs) - min(xs)), max(1e-3, max(ys) - min(ys))
        scale = min(w / span_x, h / span_y)
        centre = ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2)
        self._view = (scale, centre)

    def _map(self, x, y) -> QtCore.QPointF:
        scale, (cx, cy) = self._view
        return QtCore.QPointF(self.width() / 2 + (x - cx) * scale, (self.height() - 24) / 2 - (y - cy) * scale + 4)

    def _unmap(self, point) -> Tuple[float, float]:
        scale, (cx, cy) = self._view
        return cx + (point.x() - self.width() / 2) / scale, cy - (point.y() - 4 - (self.height() - 24) / 2) / scale

    def mousePressEvent(self, event):
        if self.camera and self._view is not None:
            self.tapped.emit(*self._unmap(event.pos()))

    # --- drawing ---

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.fillRect(self.rect(), QtGui.QColor(C.bg))
        if self.plan is None:
            return
        if self.camera:
            self._paint_camera(p)
        else:
            self._paint_schematic(p)
        p.setPen(QtGui.QColor(C.text_3))
        p.setFont(theme.font(T.caption))
        legend = "● probe   ○ goes down   → searches   ✕ should touch"
        p.drawText(QtCore.QRectF(8, self.height() - 22, self.width() - 16, 18), Qt.AlignLeft, legend)
        p.drawText(QtCore.QRectF(8, 6, self.width() - 16, 18), Qt.AlignRight, "BACK ↑")

    def _plan_points(self, origin=(0.0, 0.0)):
        ox, oy = origin
        pts = [(ox + x, oy + y) for x, y in self.plan.path]
        for t in self.plan.touches:
            pts.append((ox + t.start[0], oy + t.start[1]))
            pts.append((ox + t.start[0] + t.direction[0] * t.travel, oy + t.start[1] + t.direction[1] * t.travel))
        return pts

    def _paint_plan(self, p, origin=(0.0, 0.0), contacts=True):
        ox, oy = origin
        path = [self._map(ox + x, oy + y) for x, y in self.plan.path]
        p.setPen(_pen(C.text_3, 1.5, Qt.DashLine))
        for a, b in zip(path, path[1:]):
            p.drawLine(a, b)
        tip_px = min(10.0, max(4.0, self.tip / 2 * self._view[0]))
        for t in self.plan.touches:
            start = self._map(ox + t.start[0], oy + t.start[1])
            if t.direction == (0, 0):
                p.setPen(_pen(C.accent_hi, 2.5))
                p.setBrush(Qt.NoBrush)
                p.drawEllipse(start, tip_px + 6, tip_px + 6)
                p.drawText(QtCore.QRectF(start.x() + 14, start.y() - 10, 220, 20), Qt.AlignLeft,
                           f"down up to {t.travel:g} {'mm' if self.units == 'mm' else 'in'}")
                continue
            end = self._map(ox + t.start[0] + t.direction[0] * t.travel, oy + t.start[1] + t.direction[1] * t.travel)
            p.setPen(_pen(C.text_3, 1.5))
            p.setBrush(QtGui.QColor(C.card_hi))
            p.drawEllipse(start, tip_px, tip_px)  # goes down here
            _arrow(p, start, end, C.accent_hi)
            if contacts and t.contact is not None:
                c = self._map(ox + t.contact[0], oy + t.contact[1])
                p.setPen(_pen(C.amber, 2.5))
                p.drawLine(QtCore.QPointF(c.x() - 6, c.y() - 6), QtCore.QPointF(c.x() + 6, c.y() + 6))
                p.drawLine(QtCore.QPointF(c.x() - 6, c.y() + 6), QtCore.QPointF(c.x() + 6, c.y() - 6))

    def _paint_probe(self, p, at: QtCore.QPointF):
        p.setPen(_pen("#FFFFFF", 2))
        p.setBrush(QtGui.QColor(C.accent))
        r = min(14.0, max(7.0, self.tip / 2 * self._view[0]))
        p.drawEllipse(at, r, r)

    def _paint_schematic(self, p):
        part = self.plan.part
        pts = self._plan_points()
        size = 40.0 if self.units == "mm" else 1.6
        kind = part["kind"]
        if kind in ("hole", "boss"):
            r = part["radius"]
            pts += [(-r - 2, -r - 2), (r + 2, r + 2)]
        elif kind in ("pocket", "boss_rect"):
            hw, hl = part["half"]
            pts += [(-hw - 2, -hl - 2), (hw + 2, hl + 2)]
        self._fit([x for x, _ in pts], [y for _, y in pts])
        p.setPen(_pen(C.line_hi, 1.5))
        material = QtGui.QColor(C.card_top)
        if kind == "block":
            (cx, cy), (dx, dy) = part["corner"], part["into"]
            far = (cx + dx * size * 3, cy + dy * size * 3)
            p.setBrush(material)
            p.drawRect(QtCore.QRectF(self._map(min(cx, far[0]), max(cy, far[1])), self._map(max(cx, far[0]), min(cy, far[1]))))
        elif kind == "pocket_corner":
            (cx, cy), (dx, dy) = part["corner"], part["walls"]
            p.setBrush(material)
            # Material beyond both walls: an L around the pocket corner
            for rect in ((cx, cy - dy * size * 3, cx + dx * size * 3, cy + dy * size * 3),
                         (cx - dx * size * 3, cy, cx + dx * size * 3, cy + dy * size * 3)):
                a, b = self._map(min(rect[0], rect[2]), max(rect[1], rect[3])), self._map(max(rect[0], rect[2]), min(rect[1], rect[3]))
                p.drawRect(QtCore.QRectF(a, b))
        elif kind == "block_edge":
            (dx, dy), face = part["into"], part["face"]
            if dx:
                x0, x1 = face * dx, face * dx + dx * size * 3
                rect = (min(x0, x1), -size * 3, max(x0, x1), size * 3)
            else:
                y0, y1 = face * dy, face * dy + dy * size * 3
                rect = (-size * 3, min(y0, y1), size * 3, max(y0, y1))
            p.setBrush(material)
            p.drawRect(QtCore.QRectF(self._map(rect[0], rect[3]), self._map(rect[2], rect[1])))
        elif kind in ("hole", "pocket"):
            p.setBrush(material)
            p.drawRect(self.rect())
            p.setBrush(QtGui.QColor(C.bg))
            if kind == "hole":
                r = part["radius"] * self._view[0]
                p.drawEllipse(self._map(0, 0), r, r)
            else:
                hw, hl = part["half"]
                p.drawRect(QtCore.QRectF(self._map(-hw, hl), self._map(hw, -hl)))
        elif kind in ("boss", "boss_rect"):
            p.setBrush(material)
            if kind == "boss":
                r = part["radius"] * self._view[0]
                p.drawEllipse(self._map(0, 0), r, r)
            else:
                hw, hl = part["half"]
                p.drawRect(QtCore.QRectF(self._map(-hw, hl), self._map(hw, -hl)))
        elif kind == "surface":
            p.setBrush(material)
            p.drawRect(QtCore.QRectF(self._map(-size, size * 0.6), self._map(size, -size * 0.6)))
        self._paint_plan(p)
        self._paint_probe(p, self._map(0, 0))

    def _paint_camera(self, p):
        start = self.probe_xy or self.scan.parts[self.part_index].center
        origin = self.target_xy or start
        xs, ys = [], []
        for x, y in self._plan_points(origin) + ([self.probe_xy] if self.probe_xy else []):
            xs.append(x)
            ys.append(y)
        # Zoomed to the probing, with some of the part around it for context
        context = max(25.0, (max(xs) - min(xs)) * 0.5, (max(ys) - min(ys)) * 0.5)
        xs += [min(xs) - context, max(xs) + context]
        ys += [min(ys) - context, max(ys) + context]
        self._fit(xs, ys, margin=20)
        if self._image is not None:
            ox, oy = self.scan.mosaic_origin
            ppm = self.scan.mosaic_px_per_mm
            w, h = self._image.width() / ppm, self._image.height() / ppm
            p.setOpacity(0.85)
            p.drawImage(QtCore.QRectF(self._map(ox, oy + h), self._map(ox + w, oy)), self._image)
            p.setOpacity(1.0)
        for i, other in enumerate(self.scan.parts):
            on = i == self.part_index
            p.setPen(_pen(C.accent_hi if on else C.text_3, 3 if on else 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawPolygon(QtGui.QPolygonF([self._map(*c) for c in other.corners]))
        self._paint_plan(p, origin, contacts=False)
        if self.target_xy:
            t = self._map(*self.target_xy)
            p.setPen(_pen(C.green, 2, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(t, 12, 12)
        if self.probe_xy:
            self._paint_probe(p, self._map(*self.probe_xy))


# --- camera geometry -------------------------------------------------------------------------------

def classify_corners(part) -> dict:
    """'front_left' etc. -> machine XY of a camera part's corners (left = smaller X, front = smaller Y)"""
    cx, cy = part.center
    named = {}
    for x, y in part.corners:
        named[f"{'front' if y < cy else 'back'}_{'left' if x < cx else 'right'}"] = (x, y)
    return named


def classify_edges(part) -> dict:
    """'left' etc. -> (end a, end b) of a camera part's edges"""
    corners = classify_corners(part)
    pick = lambda *names: tuple(corners.get(n, part.center) for n in names)  # noqa: E731
    return {"front": pick("front_left", "front_right"), "back": pick("back_left", "back_right"),
            "left": pick("front_left", "back_left"), "right": pick("front_right", "back_right")}


def _unit(a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def camera_start(job: pj.ProbeJob, setup: pj.ProbeSetup, part) -> Optional[Tuple[float, float]]:
    """Machine XY to start the probe for a job on a part the camera found (None if the camera
    can't place it, e.g. an inside corner)"""
    d = setup.inset
    if job.goal in ("hole", "boss", "surface"):
        return part.center
    if job.goal == "corner":
        if job.inside:
            return None
        corners = classify_corners(part)
        if job.corner not in corners:
            return None
        cx, cy = corners[job.corner]
        neighbours = [c for name, c in corners.items() if name != job.corner and
                      (name.split("_")[0] == job.corner.split("_")[0] or name.split("_")[1] == job.corner.split("_")[1])]
        x, y = cx, cy
        for n in neighbours:
            ux, uy = _unit((cx, cy), n)
            x, y = x + ux * d, y + uy * d
        return x, y
    if job.goal in ("edge", "angle"):
        a, b = classify_edges(part)[job.edge]
        inward = _unit(((a[0] + b[0]) / 2, (a[1] + b[1]) / 2), part.center)
        if job.goal == "edge":
            mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        else:
            # Near the end the second touch steps away from
            end = {"front": "left", "back": "right", "left": "back", "right": "front"}[job.edge]
            corner = classify_corners(part).get(
                {"front": "front_left", "back": "back_right", "left": "back_left", "right": "front_right"}[job.edge])
            other = b if corner == a else a
            along = _unit(corner, other)
            mx, my = corner[0] + along[0] * d * 2, corner[1] + along[1] * d * 2
            del end
        return mx + inward[0] * d, my + inward[1] * d
    return None


def nearest_feature(part, x, y, goal: str) -> Optional[str]:
    """The corner or edge of a camera part nearest a tap"""
    if goal == "corner":
        corners = classify_corners(part)
        return min(corners, key=lambda k: math.hypot(corners[k][0] - x, corners[k][1] - y))
    if goal in ("edge", "angle"):
        edges = classify_edges(part)
        mids = {k: ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for k, (a, b) in edges.items()}
        return min(mids, key=lambda k: math.hypot(mids[k][0] - x, mids[k][1] - y))
    return None


def part_at(scan, x, y) -> int:
    """Index of the camera part containing (or nearest) a machine XY"""
    best, best_d = -1, float("inf")
    for i, part in enumerate(scan.parts):
        d = math.hypot(part.center[0] - x, part.center[1] - y) - max(part.size) / 2
        if d < best_d:
            best, best_d = i, d
    return best


def squareness_warning(part) -> Optional[str]:
    """Corner and edge probing go along X and Y: a turned part needs its angle fixed first"""
    turn = abs((part.angle + 45) % 90 - 45)
    if turn > 3:
        return (f"The camera sees this part turned {turn:.1f}° from square. Corner and edge probing go along X "
                f"and Y, so square it up first (Part angle shows by how much).")
    return None
