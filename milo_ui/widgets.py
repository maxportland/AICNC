"""
Machine-bound widgets: they read a MachineModel and send it commands.

DRO             position readout; tap an axis for zero / set / home / go-to
JogPad          press-and-hold jogging with increments and speed
SpindleCard     RPM ring, direction, speed, coolant
OverridesCard   feed / rapid / spindle / max velocity
CycleControls   Start / Pause / Stop for the dock
Stage           the toolpath preview with view controls and run progress
"""

import os
import time
from typing import Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T, AXIS_COLORS
from milo_ui.machine import MachineModel, WCS_NAMES


def fmt_pos(value, metric=True):
    return f"{value:.3f}" if metric else f"{value:.4f}"


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


# =======================================================================================
# DRO
# =======================================================================================

class AxisRow(QtWidgets.QAbstractButton):
    """One axis of the DRO, painted for crisp, fixed-width digits"""

    def __init__(self, axis, big=56, parent=None):
        super().__init__(parent)
        self.axis = axis
        self.big = big
        self.primary = 0.0
        self.secondary = 0.0
        self.dtg = 0.0
        self.homed = False
        self.metric = True
        self.show_dtg = False
        self.primary_is_work = True
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMinimumHeight(int(big * 1.72))
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def set_values(self, primary, secondary, dtg, homed, metric, show_dtg):
        self.primary, self.secondary, self.dtg = primary, secondary, dtg
        self.homed, self.metric, self.show_dtg = homed, metric, show_dtg
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        pressed = self.isDown()
        p.setPen(QtGui.QPen(QtGui.QColor(C.line), 1))
        p.setBrush(QtGui.QColor(C.card_top if pressed else C.card_hi))
        p.drawRoundedRect(rect, 16, 16)

        color = QtGui.QColor(AXIS_COLORS.get(self.axis, C.text))
        # axis letter badge
        badge = QtCore.QRectF(rect.left() + 16, rect.center().y() - 26, 52, 52)
        tint = QtGui.QColor(color)
        tint.setAlphaF(0.14)
        p.setPen(Qt.NoPen)
        p.setBrush(tint)
        p.drawRoundedRect(badge, 14, 14)
        p.setPen(color)
        p.setFont(theme.font(28, theme.BOLD))
        p.drawText(badge, Qt.AlignCenter, self.axis)
        # homed dot
        dot_color = QtGui.QColor(C.green if self.homed else C.amber)
        p.setPen(QtGui.QPen(QtGui.QColor(C.card_hi), 3))
        p.setBrush(dot_color)
        p.drawEllipse(QtCore.QPointF(badge.right() - 2, badge.top() + 2), 6, 6)

        # big number
        text = fmt_pos(self.primary, self.metric)
        p.setPen(QtGui.QColor(C.text))
        p.setFont(theme.font(self.big, theme.MEDIUM, mono=True))
        number_rect = QtCore.QRectF(badge.right() + 12, rect.top() + 4, rect.width() - badge.right() - 30,
                                    rect.height() * 0.64)
        p.drawText(number_rect, Qt.AlignRight | Qt.AlignVCenter, text)

        # secondary line
        p.setFont(theme.font(14, theme.MEDIUM, mono=True))
        other = "MACH" if self.primary_is_work else "WORK"
        line = f"{other} {fmt_pos(self.secondary, self.metric)}"
        if self.show_dtg:
            line = f"DTG {fmt_pos(self.dtg, self.metric)}    " + line
        p.setPen(QtGui.QColor(C.text_3))
        sub_rect = QtCore.QRectF(number_rect.left(), number_rect.bottom() - 6, number_rect.width(),
                                 rect.height() * 0.3)
        p.drawText(sub_rect, Qt.AlignRight | Qt.AlignTop, line)


class DRO(kit.Card):
    """Digital readout: work (or machine) position per axis, tap an axis for actions"""

    def __init__(self, machine: MachineModel, big=56, parent=None):
        self.machine = machine
        self.mode = kit.Segmented(["Work", "Machine"], min_width=86)
        self.mode.set_index(0)
        super().__init__(title="Position", trailing=self.mode, parent=parent, spacing=10)
        self.rows = {}
        for axis in machine.axes:
            row = AxisRow(axis, big=big)
            row.clicked.connect(lambda _=False, a=axis: self._axis_menu(a))
            self.rows[axis] = row
            self.add(row)
        self.footer = kit.label("", "muted")
        self.add(self.footer)
        self.mode.selected.connect(lambda _: self.refresh())
        machine.position_changed.connect(self.refresh)
        machine.changed.connect(lambda topic: topic in ("homing", "offsets", "state") and self.refresh())
        self.refresh()

    def refresh(self):
        m = self.machine
        work = self.mode.index() != 1
        for i, axis in enumerate(m.axes):
            row = self.rows[axis]
            row.primary_is_work = work
            primary = m.pos_rel[i] if work else m.pos_abs[i]
            secondary = m.pos_abs[i] if work else m.pos_rel[i]
            row.set_values(primary, secondary, m.pos_dtg[i], m.homed.get(axis, False), m.metric, m.is_running)
        self.title_label.setText(f"POSITION · {m.wcs}" if work else "POSITION · MACHINE")
        if m.work_offset_known:
            self.footer.setStyleSheet("")
            self.footer.setText(f"Tap an axis to zero it, set it or home it · {m.units}")
        else:
            self.footer.setStyleSheet(f"color: {C.amber};")
            self.footer.setText(f"{m.wcs} offset unknown: work position shows machine coordinates")

    def _axis_menu(self, axis):
        m = self.machine
        i = m.axis_index(axis)
        current = m.pos_rel[i]
        if m.is_running or not m.on:
            m.message.emit("warning", "Turn the machine on (and stop any program) to change offsets")
            return

        def set_value():
            kit.NumPad(self, f"Set {axis} in {m.wcs}", lambda v: m.set_axis_origin(axis, v),
                       initial=round(current, 4), units=m.units,
                       hint=f"The current {axis} position becomes this value in {m.wcs}. "
                            f"Tip: type 45/2 to halve.").show_centered()

        actions = [
            ("crosshair", f"Zero {axis} here", lambda: m.set_axis_origin(axis, 0.0), "primary"),
            ("pencil-simple", f"Set {axis} to a value…", set_value),
            ("house-line", f"Home {axis}", lambda: m.home_axis(axis)),
        ]
        if m.work_offset_known:
            # Halving needs the real work position (Zero and Set are worked out by LinuxCNC).
            # Read the position when tapped: the pendant may have moved the axis since
            actions.insert(2, ("arrows-in", f"Halve {axis} (find center)",
                               lambda: m.set_axis_origin(axis, m.pos_rel[i] / 2.0)))
        if m.ready:
            actions.append(("arrow-right", f"Move to {axis}0", lambda: self._go_zero(axis)))
        kit.ActionSheet(self, f"{axis} axis", actions,
                        subtitle=f"Work {fmt_pos(current, m.metric)} {m.units} in {m.wcs}").show_at(
            self.rows[axis], "right")

    def _go_zero(self, axis):
        if axis == "Z":
            self.machine.mdi("G90 G0 Z0")
        else:
            self.machine.mdi_lines(["G90 G53 G0 Z0", f"G90 G0 {axis}0"])


# =======================================================================================
# Jogging
# =======================================================================================

class JogButton(QtWidgets.QAbstractButton):
    """Press and hold to jog. Stops on release, on leaving the button, and when hidden."""

    def __init__(self, machine, axis, direction, glyph, size=128, parent=None):
        super().__init__(parent)
        self.machine, self.axis, self.direction, self.glyph = machine, axis, direction, glyph
        self.setFixedSize(size, size)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self._jogging = False
        self.pressed.connect(self._start)
        self.released.connect(self._stop)

    def _start(self):
        self._jogging = True
        self.machine.jog_start(self.axis, self.direction)
        self.update()

    def _stop(self):
        if self._jogging:
            self._jogging = False
            self.machine.jog_stop(self.axis)
            self.update()

    def hideEvent(self, event):
        self._stop()
        super().hideEvent(event)

    def leaveEvent(self, event):
        if self.isDown():
            self.setDown(False)
        self._stop()
        super().leaveEvent(event)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(self.rect()).adjusted(1, 1, -1, -1)
        color = QtGui.QColor(AXIS_COLORS.get(self.axis, C.text))
        active = self._jogging or self.isDown()
        enabled = self.isEnabled()
        if active:
            fill = QtGui.QColor(color)
            fill.setAlphaF(0.28)
            border = color
        else:
            fill = QtGui.QColor(C.card_hi)
            border = QtGui.QColor(C.line_hi)
        p.setPen(QtGui.QPen(border, 1.5))
        p.setBrush(fill)
        p.drawRoundedRect(rect, 26, 26)
        # arrow
        p.setPen(Qt.NoPen)
        p.setBrush(color if enabled else QtGui.QColor(C.text_4))
        c = rect.center()
        s = rect.width() * 0.12
        path = QtGui.QPainterPath()
        if self.glyph == "up":
            path.moveTo(c.x(), c.y() - s * 1.6); path.lineTo(c.x() + s * 1.3, c.y() - s * 0.1)
            path.lineTo(c.x() - s * 1.3, c.y() - s * 0.1)
        elif self.glyph == "down":
            path.moveTo(c.x(), c.y() + s * 0.5); path.lineTo(c.x() + s * 1.3, c.y() - s * 1.0)
            path.lineTo(c.x() - s * 1.3, c.y() - s * 1.0)
            path.translate(0, s * 0.6)
        elif self.glyph == "left":
            path.moveTo(c.x() - s * 1.6, c.y() - s * 0.4); path.lineTo(c.x() - s * 0.1, c.y() - s * 1.7)
            path.lineTo(c.x() - s * 0.1, c.y() + s * 0.9)
            path.translate(s * 0.5, s * 0.4)
        else:
            path.moveTo(c.x() + s * 1.6, c.y() - s * 0.4); path.lineTo(c.x() + s * 0.1, c.y() - s * 1.7)
            path.lineTo(c.x() + s * 0.1, c.y() + s * 0.9)
            path.translate(-s * 0.5, s * 0.4)
        p.drawPath(path)
        # label
        p.setPen(QtGui.QColor(C.text if enabled else C.text_4))
        p.setFont(theme.font(20, theme.BOLD))
        text = f"{self.axis}{'+' if self.direction > 0 else '−'}"
        label_rect = QtCore.QRectF(rect.left(), rect.top() + rect.height() * 0.58, rect.width(),
                                   rect.height() * 0.3)
        if self.glyph == "down":
            label_rect.moveTop(rect.top() + rect.height() * 0.14)
        p.drawText(label_rect, Qt.AlignCenter, text)


class JogPad(QtWidgets.QWidget):
    """XY cross + Z column of hold-to-jog buttons"""

    def __init__(self, machine: MachineModel, size=132, parent=None):
        super().__init__(parent)
        self.machine = machine
        grid = QtWidgets.QGridLayout(self)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(14)
        self.buttons = [
            (JogButton(machine, "Y", 1, "up", size), 0, 1),
            (JogButton(machine, "X", -1, "left", size), 1, 0),
            (JogButton(machine, "X", 1, "right", size), 1, 2),
            (JogButton(machine, "Y", -1, "down", size), 2, 1),
            (JogButton(machine, "Z", 1, "up", size), 0, 4),
            (JogButton(machine, "Z", -1, "down", size), 2, 4),
        ]
        for button, r, c in self.buttons:
            grid.addWidget(button, r, c)
        self.center = QtWidgets.QLabel()
        self.center.setFixedSize(size, size)
        self.center.setAlignment(Qt.AlignCenter)
        grid.addWidget(self.center, 1, 1)
        self.z_label = QtWidgets.QLabel()
        self.z_label.setAlignment(Qt.AlignCenter)
        grid.addWidget(self.z_label, 1, 4)
        grid.setColumnMinimumWidth(3, 22)
        machine.changed.connect(lambda topic: topic in ("state", "jog", "homing") and self.refresh())
        self.refresh()

    def refresh(self):
        m = self.machine
        can_jog = m.on and not m.estop and not m.is_running
        for button, _, _ in self.buttons:
            button.setEnabled(can_jog)
        inc = "Continuous" if m.jog_increment == 0 else f"{m.jog_increment:g} {m.machine_units}"
        self.center.setText(f"<div style='text-align:center'><span style='font-size:13px;color:{C.text_3};"
                            f"font-weight:700;letter-spacing:1px'>STEP</span><br>"
                            f"<span style='font-size:19px;font-weight:600;color:{C.text}'>{inc}</span></div>")
        self.z_label.setText(f"<span style='font-size:13px;color:{C.text_3};font-weight:700'>Z</span>")


class JogSettings(QtWidgets.QWidget):
    """Increment choice and jog speed"""

    def __init__(self, machine: MachineModel, parent=None):
        super().__init__(parent)
        self.machine = machine
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(kit.eyebrow("Step"))
        names = [text if value else "Cont" for text, value in machine.increments]
        self.steps = kit.Segmented(names)
        self.steps.selected.connect(self._pick)
        layout.addWidget(self.steps)
        self.speed = kit.OverrideControl("Jog speed", machine.jog_rate_min, machine.jog_rate_max,
                                         machine.jog_rate, step=10, nudge=100, suffix=f" {machine.machine_units}/min",
                                         reset_value=None, color=C.accent_2)
        self.speed.changed.connect(machine.set_jog_rate)
        layout.addWidget(self.speed)
        machine.changed.connect(lambda topic: topic == "jog" and self.refresh())
        self.refresh()

    def _pick(self, index):
        text, value = self.machine.increments[index]
        self.machine.set_jog_increment(value, text)

    def refresh(self):
        m = self.machine
        for index, (_, value) in enumerate(m.increments):
            if abs(value - m.jog_increment) < 1e-9:
                self.steps.set_index(index)
        self.speed.set_value(m.jog_rate)


# =======================================================================================
# Spindle & overrides
# =======================================================================================

class SpindleCard(kit.Card):
    """RPM ring with direction and speed controls, plus coolant"""

    def __init__(self, machine: MachineModel, compact=False, parent=None):
        self.machine = machine
        self.state_label = kit.label("", "muted")
        super().__init__(title="Spindle", trailing=self.state_label, parent=parent)
        self.gauge = kit.RingGauge(machine.spindle_max, "RPM", diameter=170 if compact else 210)
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(18)
        top.addWidget(self.gauge, 0, Qt.AlignVCenter)
        controls = QtWidgets.QVBoxLayout()
        controls.setSpacing(10)
        self.rpm_button = kit.Button("", icon="gauge", variant="outline")
        self.rpm_button.clicked.connect(self._ask_rpm)
        controls.addWidget(self.rpm_button)
        steps = QtWidgets.QHBoxLayout()
        steps.setSpacing(10)
        self.slower = kit.Button("", icon="minus", variant="outline", on_click=lambda: self._nudge(-1))
        self.faster = kit.Button("", icon="plus", variant="outline", on_click=lambda: self._nudge(1))
        steps.addWidget(self.slower)
        steps.addWidget(self.faster)
        controls.addLayout(steps)
        dirs = QtWidgets.QHBoxLayout()
        dirs.setSpacing(10)
        self.fwd = kit.Button("FWD", icon="arrow-clockwise", on_click=lambda: self._start(1))
        self.rev = kit.Button("REV", icon="arrow-counter-clockwise", on_click=lambda: self._start(-1))
        dirs.addWidget(self.fwd)
        dirs.addWidget(self.rev)
        controls.addLayout(dirs)
        self.stop = kit.Button("Stop spindle", icon="stop-fill", variant="danger", on_click=machine.spindle_stop)
        controls.addWidget(self.stop)
        top.addLayout(controls, 1)
        self.add(top)
        coolant = QtWidgets.QHBoxLayout()
        coolant.setSpacing(10)
        self.mist = kit.Button("Mist", icon="drop", checkable=True, on_click=machine.toggle_mist)
        self.flood = kit.Button("Flood", icon="drop", checkable=True, on_click=machine.toggle_flood)
        coolant.addWidget(self.mist)
        coolant.addWidget(self.flood)
        self.add(coolant)
        self.target = machine.spindle_default
        machine.changed.connect(lambda topic: topic in ("spindle", "state", "coolant", "overrides") and self.refresh())
        self.refresh()

    def _ask_rpm(self):
        m = self.machine
        kit.NumPad(self, "Spindle speed", self._set_rpm, initial=self.target, units="rpm",
                   hint=f"{m.spindle_min:g} to {m.spindle_max:g} rpm",
                   presets=[(f"{v:g}", v) for v in (500, 1000, 1500, 2000, 3000) if v <= m.spindle_max]).show_centered()

    def _set_rpm(self, rpm):
        m = self.machine
        self.target = max(m.spindle_min, min(m.spindle_max, rpm))
        if m.spindle_dir:
            m.spindle_start(m.spindle_dir, self.target)
        self.refresh()

    def _nudge(self, sign):
        m = self.machine
        self._set_rpm(self.target + sign * m.spindle_step)

    def _start(self, direction):
        self.machine.spindle_start(direction, self.target)

    def refresh(self):
        m = self.machine
        if m.spindle_dir and m.spindle_requested:
            self.target = m.spindle_requested
        self.gauge.set_values(m.spindle_actual, m.spindle_requested if m.spindle_dir else 0, bool(m.spindle_dir))
        self.rpm_button.setText(f"{self.target:,.0f} rpm")
        manual = m.on and not m.estop and not m.is_running
        for button in (self.fwd, self.rev, self.rpm_button, self.slower, self.faster):
            button.setEnabled(manual)
        self.stop.setEnabled(m.on and not m.estop and m.spindle_dir != 0 and not m.is_running)
        self.fwd.setChecked(False)
        self.fwd.set_variant("go" if m.spindle_dir > 0 else None)
        self.rev.set_variant("go" if m.spindle_dir < 0 else None)
        self.mist.setChecked(m.mist)
        self.flood.setChecked(m.flood)
        state = {1: "Forward", -1: "Reverse"}.get(m.spindle_dir, "Stopped")
        if m.spindle_dir and m.spindle_override != 100:
            state += f" · {m.spindle_override:.0f}%"
        self.state_label.setText(state)


class OverridesCard(kit.Card):
    def __init__(self, machine: MachineModel, parent=None, title="Overrides"):
        self.machine = machine
        super().__init__(title=title, parent=parent, spacing=8)
        self.feed = kit.OverrideControl("Feed", 0, machine.max_feed_override, machine.feed_override, nudge=10)
        self.feed.changed.connect(machine.set_feed_override)
        self.rapid = kit.OverrideControl("Rapid", 0, 100, machine.rapid_override, nudge=10, color=C.accent_2)
        self.rapid.changed.connect(machine.set_rapid_override)
        self.spindle = kit.OverrideControl("Spindle", machine.min_spindle_override, machine.max_spindle_override,
                                           machine.spindle_override, nudge=5, color=C.green)
        self.spindle.changed.connect(machine.set_spindle_override)
        for control in (self.feed, self.rapid, self.spindle):
            self.add(control)
        machine.changed.connect(lambda topic: topic == "overrides" and self.refresh())
        self.refresh()

    def refresh(self):
        m = self.machine
        self.feed.set_value(m.feed_override)
        self.rapid.set_value(m.rapid_override)
        self.spindle.set_value(m.spindle_override)


# =======================================================================================
# Cycle controls (dock)
# =======================================================================================

class CycleControls(QtWidgets.QWidget):
    """Start / Pause-Resume / Stop, always in the same place"""

    start_requested = QtCore.pyqtSignal()

    def __init__(self, machine: MachineModel, parent=None):
        super().__init__(parent)
        self.machine = machine
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)
        self.start = kit.Button("Start", icon="play-fill", variant="go", size="lg")
        self.start.setMinimumWidth(176)
        self.start.clicked.connect(self.start_requested.emit)
        self.pause = kit.Button("Pause", icon="pause-fill", variant="outline", size="lg")
        self.pause.setMinimumWidth(152)
        self.pause.clicked.connect(machine.pause_resume)
        self.stop = kit.Button("Stop", icon="stop-fill", variant="danger", size="lg")
        self.stop.setMinimumWidth(140)
        self.stop.clicked.connect(machine.abort)
        for button in (self.start, self.pause, self.stop):
            row.addWidget(button)
        machine.changed.connect(lambda topic: topic in ("state", "program", "homing") and self.refresh())
        self.refresh()

    def refresh(self):
        m = self.machine
        running = m.is_running
        self.start.setEnabled(bool(m.file) and m.ready and not running)
        self.pause.setEnabled(running)
        if running and m.paused:
            self.pause.setText("Resume")
            self.pause.set_icon_name("play-fill")
            self.pause.set_variant("warn")
        else:
            self.pause.setText("Pause")
            self.pause.set_icon_name("pause-fill")
            self.pause.set_variant("outline")
        self.stop.setEnabled(m.on and not m.estop)


# =======================================================================================
# Toolpath stage
# =======================================================================================

class PreviewPlaceholder(QtWidgets.QWidget):
    """Stands in for the OpenGL toolpath view when LinuxCNC isn't running (previews)"""

    def __init__(self, machine, parent=None):
        super().__init__(parent)
        self.machine = machine
        machine.position_changed.connect(self.update)
        machine.changed.connect(lambda t: t == "program" and self.update())

    def paintEvent(self, event):
        import math
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        w, h = self.width(), self.height()
        grad = QtGui.QLinearGradient(0, 0, 0, h)
        grad.setColorAt(0, QtGui.QColor("#121823"))
        grad.setColorAt(1, QtGui.QColor("#0B0F16"))
        p.fillRect(self.rect(), grad)
        cx, cy = w / 2, h / 2 + 10
        scale = min(w, h) / 260.0

        def iso(x, y, z=0.0):
            return QtCore.QPointF(cx + (x - y) * 0.87 * scale, cy + (x + y) * 0.5 * scale - z * scale)

        p.setPen(QtGui.QPen(QtGui.QColor(C.line), 1))
        for i in range(-100, 101, 20):
            p.drawLine(iso(i, -100), iso(i, 100))
            p.drawLine(iso(-100, i), iso(100, i))
        # stock outline
        p.setPen(QtGui.QPen(QtGui.QColor(C.text_4), 1.2))
        for z in (0, 12):
            pts = [iso(-60, -60, z), iso(60, -60, z), iso(60, 60, z), iso(-60, 60, z), iso(-60, -60, z)]
            p.drawPolyline(QtGui.QPolygonF(pts))
        if self.machine.file:
            path = QtGui.QPainterPath()
            first = True
            for depth in range(4):
                z = 12 - depth * 3
                for k in range(0, 361, 6):
                    a = math.radians(k)
                    r = 38 - depth * 0.5
                    pt = iso(r * math.cos(a), r * math.sin(a), z)
                    if first:
                        path.moveTo(pt)
                        first = False
                    else:
                        path.lineTo(pt)
            p.setPen(QtGui.QPen(QtGui.QColor(C.accent_hi), 2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(path)
            p.setPen(QtGui.QPen(QtGui.QColor(C.accent_2), 1.2, Qt.DashLine))
            p.drawLine(iso(0, 0, 40), iso(38, 0, 12))
        # axes
        for (dx, dy, dz), color in (((30, 0, 0), C.x), ((0, 30, 0), C.y), ((0, 0, 30), C.z)):
            p.setPen(QtGui.QPen(QtGui.QColor(color), 2.5))
            p.drawLine(iso(-90, -90, 0), iso(-90 + dx, -90 + dy, dz))
        # tool
        tip = iso(38, 0, 12)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(C.amber))
        p.drawEllipse(tip, 5, 5)
        p.setPen(QtGui.QPen(QtGui.QColor(C.amber), 6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(tip, QtCore.QPointF(tip.x(), tip.y() - 60 * scale / 2))
        if not self.machine.file:
            p.setPen(QtGui.QColor(C.text_3))
            p.setFont(theme.font(T.body))
            p.drawText(QtCore.QRectF(0, h - 70, w, 30), Qt.AlignCenter, "No program loaded")


class Stage(QtWidgets.QFrame):
    """
    The toolpath preview. Holds the one GCodeGraphics instance for the whole screen
    (it is an OpenGL widget and can't move between pages), with view controls, the
    loaded file, run progress and program facts.
    """

    open_files = QtCore.pyqtSignal()

    def __init__(self, machine: MachineModel, graphics: Optional[QtWidgets.QWidget] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("stage")
        self.machine = machine
        self.graphics = graphics
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(12)

        head = QtWidgets.QHBoxLayout()
        head.setSpacing(12)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(2)
        words.addWidget(kit.eyebrow("Toolpath"))
        self.file_label = kit.label("No program loaded", "value")
        self.file_label.setMinimumWidth(10)
        words.addWidget(self.file_label)
        head.addLayout(words, 1)
        self.open_button = kit.Button("Open", icon="folder-open", variant="outline", size="sm")
        self.open_button.clicked.connect(self.open_files.emit)
        head.addWidget(self.open_button, 0, Qt.AlignVCenter)
        self.reload_button = kit.RoundButton("arrows-clockwise", 44, variant="ghost", icon_size=20,
                                             tip="Reload program", on_click=machine.reload_program)
        head.addWidget(self.reload_button, 0, Qt.AlignVCenter)
        layout.addLayout(head)

        self.view_frame = QtWidgets.QFrame()
        self.view_frame.setStyleSheet(f"background: #0B0F16; border-radius: 14px;")
        view_layout = QtWidgets.QVBoxLayout(self.view_frame)
        view_layout.setContentsMargins(0, 0, 0, 0)
        self.view = graphics if graphics is not None else PreviewPlaceholder(machine)
        self.view.setMinimumHeight(300)
        view_layout.addWidget(self.view)
        layout.addWidget(self.view_frame, 1)

        views = QtWidgets.QHBoxLayout()
        views.setSpacing(8)
        self.view_buttons = kit.Segmented(["3D", "Top", "Front", "Side"])
        self.view_buttons.set_index(0)
        self.view_buttons.selected.connect(lambda i: self._view(["p", "z", "y", "x"][i]))
        views.addWidget(self.view_buttons, 1)
        for icon, view, tip in (("magnifying-glass-minus", "zoom-out", "Zoom out"),
                                ("magnifying-glass-plus", "zoom-in", "Zoom in"),
                                ("eraser", "clear", "Clear the live tool path")):
            views.addWidget(kit.RoundButton(icon, 56, variant="outline", icon_size=22, tip=tip,
                                            on_click=lambda v=view: self._view(v)))
        layout.addLayout(views)

        progress = QtWidgets.QHBoxLayout()
        progress.setSpacing(12)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        progress.addWidget(self.progress, 1)
        self.progress_text = kit.label("", "muted")
        progress.addWidget(self.progress_text)
        layout.addLayout(progress)

        self.facts = QtWidgets.QGridLayout()
        self.facts.setHorizontalSpacing(18)
        self.facts.setVerticalSpacing(6)
        layout.addLayout(self.facts)
        self._fact_labels = {}
        for index, key in enumerate(("Time", "Line", "Tools", "Size")):
            self.facts.addWidget(kit.label(key.upper(), "eyebrow"), 0, index)
            value = kit.label("—", "value", size=T.body, weight=theme.MEDIUM)
            self.facts.addWidget(value, 1, index)
            self._fact_labels[key] = value

        machine.changed.connect(lambda topic: topic in ("program", "state") and self.refresh())
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self.refresh()

    def _view(self, view):
        g = self.graphics
        if g is not None and hasattr(g, "set_view_signal"):
            g.set_view_signal(view, None)

    def refresh(self):
        m = self.machine
        name = os.path.basename(m.file) if m.file else "No program loaded"
        metrics = self.file_label.fontMetrics()
        self.file_label.setText(metrics.elidedText(name, Qt.ElideMiddle, max(120, self.file_label.width())))
        self.file_label.setToolTip(m.file)
        self.progress.setValue(int(m.progress * 1000))
        props = m.gcode_properties or {}
        if m.is_running or m.run_elapsed:
            left = ""
            if m.progress > 0.02 and m.run_elapsed > 5:
                remaining = m.run_elapsed * (1 - m.progress) / m.progress
                left = f" · ~{fmt_duration(remaining)} left"
            self.progress_text.setText(f"{m.progress * 100:.0f}% · {fmt_duration(m.run_elapsed)}{left}")
        else:
            self.progress_text.setText("Ready" if m.file else "")
        self._fact_labels["Time"].setText(str(props.get("run", "—")).split(".")[0] or "—")
        self._fact_labels["Line"].setText(f"{m.line} / {m.total_lines}" if m.total_lines else "—")
        tools = props.get("toollist") or "—"
        self._fact_labels["Tools"].setText(str(tools)[:24])
        size = []
        for axis in ("x", "y"):
            text = str(props.get(axis, ""))
            if "=" in text:
                size.append(text.split("=")[-1].strip().split(" ")[0])
        self._fact_labels["Size"].setText(" × ".join(size) + (f" {m.units}" if size else "") if size else "—")
        self.reload_button.setEnabled(bool(m.file) and not m.is_running)
        self.open_button.setEnabled(not m.is_running)
