"""
The component kit: touch-sized building blocks shared by every page.

Simple controls (buttons, chips, cards) are styled by the stylesheet in theme.py through
objectName and dynamic properties. Controls that stylesheets can't express well (sliders,
toggles, gauges, rings) are painted here.
"""

import math
from typing import Callable, List, Optional, Sequence, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme
from milo_ui.theme import C, T


# --- small helpers ----------------------------------------------------------------------

CSS_WEIGHTS = {theme.LIGHT: 300, theme.REGULAR: 400, theme.MEDIUM: 500, theme.SEMIBOLD: 600, theme.BOLD: 700}


def label(text="", name="body", size=None, weight=None, color=None, mono=False, wrap=False, align=None):
    w = QtWidgets.QLabel(text)
    w.setObjectName(name)
    css = []
    if size or weight or mono:
        # Inline, so it wins over the named style's font rules in the app stylesheet
        f = theme.font(size or T.body, weight or theme.REGULAR, mono=mono)
        css.append(f"font-family: '{f.family()}'; font-size: {f.pixelSize()}px; "
                   f"font-weight: {CSS_WEIGHTS.get(weight or theme.REGULAR, 400)};")
    if color:
        css.append(f"color: {color};")
    if css:
        w.setStyleSheet(" ".join(css))
    if wrap:
        w.setWordWrap(True)
    if align is not None:
        w.setAlignment(align)
    return w


def eyebrow(text):
    return label(text.upper(), "eyebrow")


def hline():
    line = QtWidgets.QFrame()
    line.setObjectName("divider")
    return line


def vline():
    line = QtWidgets.QFrame()
    line.setObjectName("vdivider")
    return line


def hbox(*items, spacing=12, margins=(0, 0, 0, 0)):
    layout = QtWidgets.QHBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    for item in items:
        _add(layout, item)
    return layout


def vbox(*items, spacing=12, margins=(0, 0, 0, 0)):
    layout = QtWidgets.QVBoxLayout()
    layout.setSpacing(spacing)
    layout.setContentsMargins(*margins)
    for item in items:
        _add(layout, item)
    return layout


def _add(layout, item):
    if item is None:
        return
    if item == "stretch":
        layout.addStretch(1)
    elif isinstance(item, int):
        layout.addSpacing(item)
    elif isinstance(item, QtWidgets.QLayout):
        layout.addLayout(item)
    else:
        layout.addWidget(item)


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout():
            clear_layout(item.layout())


# --- buttons ----------------------------------------------------------------------------

ICON_COLORS = {
    None: C.text, "primary": "#0B0A1A", "go": "#04160F", "warn": "#1C1403", "danger": "#FFFFFF",
    "ghost": C.text_2, "outline": C.text,
}


class Button(QtWidgets.QPushButton):
    """Styled push button. variant: primary/go/warn/danger/ghost/outline; size: sm/lg"""

    def __init__(self, text="", icon=None, variant=None, size=None, parent=None, checkable=False,
                 icon_size=None, on_click=None):
        super().__init__(text, parent)
        self._icon_name = icon
        self._variant = variant
        if variant:
            self.setProperty("variant", variant)
        if size:
            self.setProperty("size", size)
        self.setCheckable(checkable)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        px = icon_size or (26 if size == "lg" else 20 if size == "sm" else 22)
        self.setIconSize(QtCore.QSize(px, px))
        self._refresh_icon()
        if on_click:
            self.clicked.connect(lambda _=False: on_click())

    def set_variant(self, variant):
        self._variant = variant
        theme.set_prop(self, "variant", variant or "")
        self._refresh_icon()

    def set_icon_name(self, name):
        self._icon_name = name
        self._refresh_icon()

    def _refresh_icon(self):
        if self._icon_name:
            color = ICON_COLORS.get(self._variant, C.text)
            self.setIcon(theme.icon(self._icon_name, color=color, color_disabled=C.text_4))
        else:
            self.setIcon(QtGui.QIcon())


class RoundButton(Button):
    """Circular icon button"""

    def __init__(self, icon, diameter=64, variant=None, icon_size=None, parent=None, tip="", on_click=None):
        super().__init__("", icon=icon, variant=variant, parent=parent,
                         icon_size=icon_size or int(diameter * 0.4), on_click=on_click)
        self.setProperty("shape", "round")
        self.setFixedSize(diameter, diameter)
        self.setStyleSheet(f"border-radius: {diameter // 2}px; min-height: {diameter}px;")
        if tip:
            self.setToolTip(tip)


class Chip(QtWidgets.QPushButton):
    def __init__(self, text, icon=None, tone=None, parent=None, on_click=None):
        super().__init__(text, parent)
        self.setObjectName("chip")
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        if tone:
            self.setProperty("tone", tone)
        if icon:
            self.setIcon(theme.icon(icon, color=C.accent_hi if tone == "accent" else C.text_2))
            self.setIconSize(QtCore.QSize(18, 18))
        if on_click:
            self.clicked.connect(lambda _=False: on_click())


class Card(QtWidgets.QFrame):
    """Rounded surface with an optional eyebrow title and trailing header widget"""

    def __init__(self, title=None, trailing=None, tone=None, padding=20, spacing=14, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        if tone:
            self.setProperty("tone", tone)
        self.body = QtWidgets.QVBoxLayout(self)
        self.body.setContentsMargins(padding, padding - 2, padding, padding)
        self.body.setSpacing(spacing)
        if title or trailing:
            header = QtWidgets.QHBoxLayout()
            header.setSpacing(8)
            if title:
                self.title_label = eyebrow(title)
                header.addWidget(self.title_label)
            header.addStretch(1)
            if trailing is not None:
                if isinstance(trailing, QtWidgets.QLayout):
                    header.addLayout(trailing)
                else:
                    header.addWidget(trailing)
            self.body.addLayout(header)

    def add(self, item, stretch=0):
        if isinstance(item, QtWidgets.QLayout):
            self.body.addLayout(item, stretch)
        else:
            self.body.addWidget(item, stretch)
        return item


class Segmented(QtWidgets.QFrame):
    """Segmented control: one of several options. Emits selected(index)"""

    selected = QtCore.pyqtSignal(int)

    def __init__(self, options: Sequence[str], parent=None, min_width=0):
        super().__init__(parent)
        self.setObjectName("segmented")
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons = []
        for index, text in enumerate(options):
            button = QtWidgets.QPushButton(text)
            button.setObjectName("segment")
            button.setCheckable(True)
            button.setFocusPolicy(Qt.NoFocus)
            button.setCursor(Qt.PointingHandCursor)
            if min_width:
                button.setMinimumWidth(min_width)
            self.group.addButton(button, index)
            layout.addWidget(button)
            self.buttons.append(button)
        self.group.buttonClicked[int].connect(self.selected.emit)

    def set_index(self, index):
        if 0 <= index < len(self.buttons):
            self.buttons[index].setChecked(True)

    def index(self):
        return self.group.checkedId()


# --- painted controls ----------------------------------------------------------------------

class Toggle(QtWidgets.QAbstractButton):
    """iOS-style switch, 64x36 (the whole row around it is usually the touch target)"""

    def __init__(self, checked=False, parent=None, color=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setChecked(checked)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.NoFocus)
        self.setFixedSize(64, 36)
        self._color = QtGui.QColor(color or C.accent)
        self._pos = 1.0 if checked else 0.0
        self._anim = QtCore.QVariantAnimation(self, duration=140, easingCurve=QtCore.QEasingCurve.OutCubic)
        self._anim.valueChanged.connect(self._set_pos)
        self.toggled.connect(self._animate)

    def _set_pos(self, value):
        self._pos = value
        self.update()

    def _animate(self, on):
        self._anim.stop()
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(1.0 if on else 0.0)
        self._anim.start()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        rect = QtCore.QRectF(0, 0, self.width(), self.height())
        track = theme.mix(C.line_hi, self._color.name(), self._pos)
        if not self.isEnabled():
            track = QtGui.QColor(C.card_hi)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        d = rect.height() - 8
        x = 4 + (rect.width() - d - 8) * self._pos
        p.setBrush(QtGui.QColor("#FFFFFF") if self.isEnabled() else QtGui.QColor(C.text_4))
        p.drawEllipse(QtCore.QRectF(x, 4, d, d))

    def sizeHint(self):
        return QtCore.QSize(64, 36)


class ToggleRow(QtWidgets.QFrame):
    """A full-width touch row: title + optional detail on the left, a toggle on the right"""

    toggled = QtCore.pyqtSignal(bool)

    def __init__(self, title, detail="", checked=False, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(64)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 6, 0, 6)
        text = QtWidgets.QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(label(title, "value", size=T.body, weight=theme.MEDIUM))
        if detail:
            text.addWidget(label(detail, "muted", wrap=True))
        layout.addLayout(text, 1)
        self.toggle = Toggle(checked)
        self.toggle.toggled.connect(self.toggled.emit)
        layout.addWidget(self.toggle, 0, Qt.AlignVCenter)

    def mouseReleaseEvent(self, event):
        if self.rect().contains(event.pos()):
            self.toggle.toggle()

    def isChecked(self):
        return self.toggle.isChecked()

    def setChecked(self, on):
        self.toggle.blockSignals(True)
        self.toggle.setChecked(on)
        self.toggle._set_pos(1.0 if on else 0.0)
        self.toggle.blockSignals(False)


class TouchSlider(QtWidgets.QWidget):
    """
    Horizontal slider built for fingers: a tall hit area, a fat thumb, drag anywhere.

    Values are floats; `step` snaps them. Emits valueChanged continuously while dragging
    and valueCommitted when the finger lifts.
    """

    valueChanged = QtCore.pyqtSignal(float)
    valueCommitted = QtCore.pyqtSignal(float)

    def __init__(self, minimum=0.0, maximum=100.0, value=0.0, step=1.0, color=None, marker=None, parent=None):
        super().__init__(parent)
        self.minimum, self.maximum, self.step = float(minimum), float(maximum), float(step)
        self._value = float(value)
        self.marker = marker  # a value to draw a tick at (e.g. 100%)
        self.color = QtGui.QColor(color or C.accent)
        self._dragging = False
        self.setMinimumHeight(56)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def value(self):
        return self._value

    def setRange(self, minimum, maximum):
        self.minimum, self.maximum = float(minimum), float(maximum)
        self.update()

    def setValue(self, value, emit=False):
        value = self._snap(value)
        if value != self._value:
            self._value = value
            self.update()
            if emit:
                self.valueChanged.emit(value)

    def _snap(self, value):
        value = max(self.minimum, min(self.maximum, float(value)))
        if self.step > 0:
            value = round(value / self.step) * self.step
        return round(value, 6)

    def _track(self):
        pad = 24
        return QtCore.QRectF(pad, self.height() / 2 - 5, self.width() - 2 * pad, 10)

    def _value_at(self, x):
        track = self._track()
        t = (x - track.left()) / max(1.0, track.width())
        return self.minimum + max(0.0, min(1.0, t)) * (self.maximum - self.minimum)

    def mousePressEvent(self, event):
        self._dragging = True
        self.setValue(self._value_at(event.x()), emit=True)

    def mouseMoveEvent(self, event):
        if self._dragging:
            self.setValue(self._value_at(event.x()), emit=True)

    def mouseReleaseEvent(self, event):
        if self._dragging:
            self._dragging = False
            self.valueCommitted.emit(self._value)

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        track = self._track()
        span = self.maximum - self.minimum or 1.0
        t = (self._value - self.minimum) / span
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(C.card_top))
        p.drawRoundedRect(track, 5, 5)
        fill = QtCore.QRectF(track.left(), track.top(), track.width() * t, track.height())
        grad = QtGui.QLinearGradient(fill.topLeft(), fill.topRight())
        grad.setColorAt(0, theme.mix(self.color.name(), C.bg, 0.35))
        grad.setColorAt(1, self.color)
        p.setBrush(grad if self.isEnabled() else QtGui.QColor(C.line_hi))
        p.drawRoundedRect(fill, 5, 5)
        if self.marker is not None and self.minimum < self.marker < self.maximum:
            mx = track.left() + track.width() * (self.marker - self.minimum) / span
            p.setBrush(QtGui.QColor(C.text_3))
            p.drawRoundedRect(QtCore.QRectF(mx - 1.5, track.top() - 9, 3, track.height() + 18), 1.5, 1.5)
        cx = track.left() + track.width() * t
        r = 17 if not self._dragging else 20
        p.setBrush(theme.alpha(self.color.name(), 0.25))
        p.drawEllipse(QtCore.QPointF(cx, track.center().y()), r + 6, r + 6)
        p.setBrush(QtGui.QColor("#FFFFFF") if self.isEnabled() else QtGui.QColor(C.text_3))
        p.drawEllipse(QtCore.QPointF(cx, track.center().y()), r, r)


class OverrideControl(QtWidgets.QWidget):
    """Title + big value + [-] slider [+]; tapping the value resets it"""

    changed = QtCore.pyqtSignal(float)

    def __init__(self, title, minimum, maximum, value=100.0, step=1.0, nudge=5.0, suffix="%",
                 reset_value=100.0, color=None, fmt="{:.0f}", parent=None):
        super().__init__(parent)
        self.suffix, self.fmt, self.nudge_step, self.reset_value = suffix, fmt, nudge, reset_value
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        top = QtWidgets.QHBoxLayout()
        self.title = label(title, "muted")
        top.addWidget(self.title)
        top.addStretch(1)
        self.value_button = QtWidgets.QPushButton()
        self.value_button.setFocusPolicy(Qt.NoFocus)
        self.value_button.setCursor(Qt.PointingHandCursor)
        self.value_button.setStyleSheet(
            f"background: transparent; border: none; min-height: 32px; padding: 0; "
            f"font-family: '{theme.MONO_FONT}'; font-size: 22px; font-weight: 600; color: {C.text};")
        self.value_button.clicked.connect(self.reset)
        top.addWidget(self.value_button)
        layout.addLayout(top)
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(6)
        self.minus = RoundButton("minus", 52, variant="ghost", on_click=lambda: self.nudge(-1))
        self.slider = TouchSlider(minimum, maximum, value, step, color=color,
                                  marker=reset_value if reset_value is not None and minimum < reset_value < maximum else None)
        self.plus = RoundButton("plus", 52, variant="ghost", on_click=lambda: self.nudge(1))
        row.addWidget(self.minus)
        row.addWidget(self.slider, 1)
        row.addWidget(self.plus)
        layout.addLayout(row)
        self.slider.valueChanged.connect(self._show)
        self.slider.valueCommitted.connect(self.changed.emit)
        self._show(value)

    def _show(self, value):
        self.value_button.setText(self.fmt.format(value) + self.suffix)

    def set_value(self, value):
        """Update from the machine (doesn't emit), unless the user is dragging"""
        if not self.slider._dragging:
            self.slider.setValue(value)
            self._show(self.slider.value())

    def set_range(self, minimum, maximum):
        self.slider.setRange(minimum, maximum)

    def nudge(self, sign):
        self.slider.setValue(self.slider.value() + sign * self.nudge_step)
        self._show(self.slider.value())
        self.changed.emit(self.slider.value())

    def reset(self):
        if self.reset_value is None:
            return
        self.slider.setValue(self.reset_value)
        self._show(self.slider.value())
        self.changed.emit(self.slider.value())


class RingGauge(QtWidgets.QWidget):
    """Arc gauge (spindle RPM): value arc, setpoint tick, big number in the middle"""

    def __init__(self, maximum=3000.0, caption="RPM", color=None, parent=None, diameter=200):
        super().__init__(parent)
        self.maximum = float(maximum)
        self.value = 0.0
        self.setpoint = 0.0
        self.caption = caption
        self.color = QtGui.QColor(color or C.accent_2)
        self.active = False
        self.setMinimumSize(diameter, diameter)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Preferred)

    def set_values(self, value, setpoint=None, active=None):
        self.value = max(0.0, float(value))
        if setpoint is not None:
            self.setpoint = float(setpoint)
        if active is not None:
            self.active = active
        self.update()

    def heightForWidth(self, w):
        return w

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        side = min(self.width(), self.height())
        stroke = max(10.0, side * 0.07)
        rect = QtCore.QRectF((self.width() - side) / 2 + stroke, (self.height() - side) / 2 + stroke,
                             side - 2 * stroke, side - 2 * stroke)
        start, span = 225 * 16, -270 * 16
        pen = QtGui.QPen(QtGui.QColor(C.card_top), stroke, Qt.SolidLine, Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(rect, start, span)
        t = min(1.0, self.value / self.maximum) if self.maximum else 0
        if t > 0.002:
            grad = QtGui.QConicalGradient(rect.center(), 225)
            grad.setColorAt(0.0, QtGui.QColor(C.accent))
            grad.setColorAt(0.75, self.color)
            grad.setColorAt(1.0, QtGui.QColor(C.accent))
            pen = QtGui.QPen(QtGui.QBrush(grad), stroke, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            p.drawArc(rect, start, int(span * t))
        if self.setpoint > 0 and self.maximum:
            ts = min(1.0, self.setpoint / self.maximum)
            angle = math.radians(225 - 270 * ts)
            r1, r2 = rect.width() / 2 - stroke * 0.9, rect.width() / 2 + stroke * 0.9
            c = rect.center()
            p.setPen(QtGui.QPen(QtGui.QColor(C.text), 3, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QtCore.QPointF(c.x() + r1 * math.cos(angle), c.y() - r1 * math.sin(angle)),
                       QtCore.QPointF(c.x() + r2 * math.cos(angle), c.y() - r2 * math.sin(angle)))
        p.setPen(QtGui.QColor(C.text if self.active else C.text_2))
        p.setFont(theme.font(int(side * 0.2), theme.MEDIUM, mono=True))
        number_rect = QtCore.QRectF(rect.left(), rect.center().y() - side * 0.16, rect.width(), side * 0.24)
        p.drawText(number_rect, Qt.AlignCenter, f"{self.value:,.0f}")
        p.setPen(QtGui.QColor(C.text_3))
        p.setFont(theme.font(max(11, int(side * 0.065)), theme.SEMIBOLD))
        cap_rect = QtCore.QRectF(rect.left(), number_rect.bottom() + 2, rect.width(), side * 0.1)
        p.drawText(cap_rect, Qt.AlignCenter, self.caption)


class ProgressArc(QtWidgets.QWidget):
    """Small circular progress/countdown ring"""

    def __init__(self, diameter=56, stroke=5, color=None, parent=None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self.stroke = stroke
        self.fraction = 0.0
        self.text = ""
        self.color = QtGui.QColor(color or C.amber)

    def set_fraction(self, fraction, text=""):
        self.fraction = max(0.0, min(1.0, fraction))
        self.text = text
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        s = self.stroke
        rect = QtCore.QRectF(s, s, self.width() - 2 * s, self.height() - 2 * s)
        p.setPen(QtGui.QPen(QtGui.QColor(C.line_hi), s))
        p.drawEllipse(rect)
        p.setPen(QtGui.QPen(self.color, s, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(rect, 90 * 16, int(-360 * 16 * self.fraction))
        if self.text:
            p.setPen(QtGui.QColor(C.text))
            p.setFont(theme.font(int(self.height() * 0.3), theme.SEMIBOLD, mono=True))
            p.drawText(self.rect(), Qt.AlignCenter, self.text)


class Dot(QtWidgets.QWidget):
    """A status dot, optionally pulsing"""

    def __init__(self, color=C.green, diameter=10, parent=None):
        super().__init__(parent)
        self.color = QtGui.QColor(color)
        self.setFixedSize(diameter + 8, diameter + 8)
        self.diameter = diameter
        self._pulse = 0.0
        self._anim = None

    def set_color(self, color, pulse=False):
        self.color = QtGui.QColor(color)
        if pulse and self._anim is None:
            self._anim = QtCore.QVariantAnimation(self, startValue=0.0, endValue=1.0, duration=1400,
                                                  loopCount=-1)
            self._anim.valueChanged.connect(self._set_pulse)
            self._anim.start()
        elif not pulse and self._anim is not None:
            self._anim.stop()
            self._anim = None
            self._pulse = 0.0
        self.update()

    def _set_pulse(self, value):
        self._pulse = value
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        c = QtCore.QPointF(self.width() / 2, self.height() / 2)
        r = self.diameter / 2
        if self._anim is not None:
            halo = QtGui.QColor(self.color)
            halo.setAlphaF(0.5 * (1 - self._pulse))
            p.setPen(Qt.NoPen)
            p.setBrush(halo)
            p.drawEllipse(c, r + 4 * self._pulse, r + 4 * self._pulse)
        p.setPen(Qt.NoPen)
        p.setBrush(self.color)
        p.drawEllipse(c, r, r)


class FlowLayout(QtWidgets.QLayout):
    """Wraps widgets onto new lines (for chips)"""

    def __init__(self, parent=None, spacing=10):
        super().__init__(parent)
        self._items = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._layout(QtCore.QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._layout(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        size = QtCore.QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _layout(self, rect, test):
        x, y, line_height = rect.x(), rect.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            if x + hint.width() > rect.right() + 1 and line_height > 0:
                x = rect.x()
                y += line_height + self._spacing
                line_height = 0
            if not test:
                item.setGeometry(QtCore.QRect(QtCore.QPoint(x, y), hint))
            x += hint.width() + self._spacing
            line_height = max(line_height, hint.height())
        return y + line_height - rect.y()


# --- overlays ---------------------------------------------------------------------------

def screen_root(widget):
    """The Milo screen widget containing `widget` (overlays go there, under the keyboard)"""
    w = widget
    while w is not None:
        if w.objectName() == "milo_root":
            return w
        w = w.parentWidget()
    return widget.window()


class Scrim(QtWidgets.QWidget):
    """Dims the window behind a popover; tapping it dismisses"""

    tapped = QtCore.pyqtSignal()

    def __init__(self, parent, opacity=0.55):
        super().__init__(parent)
        self.opacity = opacity
        self.setGeometry(parent.rect())
        parent.installEventFilter(self)

    def eventFilter(self, obj, event):
        try:
            if event.type() == QtCore.QEvent.Resize:
                self.setGeometry(obj.rect())
        except (RuntimeError, AttributeError):  # during teardown
            pass
        return False

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), theme.alpha("#000000", self.opacity))

    def mousePressEvent(self, event):
        self.tapped.emit()


class Popover(QtWidgets.QFrame):
    """
    A floating panel over the main window with a dimmed backdrop.

    Shown centered, or anchored next to a widget. close() hides and deletes it.
    """

    closed = QtCore.pyqtSignal()

    def __init__(self, host: QtWidgets.QWidget, title=None, width=520, object_name="card", dismissable=True):
        self.host = screen_root(host)
        self.scrim = Scrim(self.host)
        super().__init__(self.host)
        self.setObjectName(object_name)
        self.setFixedWidth(width)
        self.layout_ = QtWidgets.QVBoxLayout(self)
        self.layout_.setContentsMargins(24, 22, 24, 24)
        self.layout_.setSpacing(16)
        if title:
            header = QtWidgets.QHBoxLayout()
            header.addWidget(label(title, "value", size=T.title, weight=theme.SEMIBOLD))
            header.addStretch(1)
            header.addWidget(RoundButton("x", 52, variant="ghost", on_click=self.close))
            self.layout_.addLayout(header)
        if dismissable:
            self.scrim.tapped.connect(self.close)
        effect = QtWidgets.QGraphicsDropShadowEffect(self)
        effect.setBlurRadius(60)
        effect.setOffset(0, 18)
        effect.setColor(theme.alpha("#000000", 0.6))
        self.setGraphicsEffect(effect)

    def add(self, item):
        _add(self.layout_, item)
        return item

    def show_centered(self):
        self.adjustSize()
        g = self.host.rect()
        self.move(g.center().x() - self.width() // 2, g.center().y() - self.height() // 2)
        self._show()

    def show_at(self, anchor: QtWidgets.QWidget, side="right"):
        self.adjustSize()
        top_left = anchor.mapTo(self.host, QtCore.QPoint(0, 0))
        if side == "right":
            x, y = top_left.x() + anchor.width() + 16, top_left.y()
        elif side == "above":
            x, y = top_left.x(), top_left.y() - self.height() - 12
        else:  # below
            x, y = top_left.x(), top_left.y() + anchor.height() + 12
        x = max(16, min(self.host.width() - self.width() - 16, x))
        y = max(16, min(self.host.height() - self.height() - 16, y))
        self.move(x, y)
        self._show()

    def _show(self):
        self.scrim.show()
        self.scrim.raise_()
        self.show()
        self.raise_()

    def close(self):
        self.scrim.hide()
        self.scrim.deleteLater()
        self.hide()
        self.closed.emit()
        self.deleteLater()
        return True


class ActionSheet(Popover):
    """A popover listing big touch actions: [(icon, text, callback, variant), ...]"""

    def __init__(self, host, title, actions, subtitle=None, width=440):
        super().__init__(host, title=title, width=width)
        if subtitle:
            self.add(label(subtitle, "muted", wrap=True))
        for entry in actions:
            icon, text, callback = entry[:3]
            variant = entry[3] if len(entry) > 3 else None
            button = Button(text, icon=icon, variant=variant or "outline", size="lg")
            button.setStyleSheet("text-align: left; padding-left: 20px;")
            button.clicked.connect(lambda _=False, cb=callback: (self.close(), cb()))
            self.add(button)


class NumPad(Popover):
    """
    Touch number entry. Calls on_value(float) with the entered value.

    Accepts simple arithmetic (e.g. "12.5/2") so operators can halve or offset a value.
    """

    def __init__(self, host, title, on_value: Callable[[float], None], initial: Optional[float] = None,
                 units="", hint=None, presets: Sequence[Tuple[str, float]] = ()):
        super().__init__(host, title=title, width=440, object_name="numpad")
        self.on_value = on_value
        self.units = units
        self.fresh = True
        if hint:
            self.add(label(hint, "muted", wrap=True))
        self.display = QtWidgets.QLabel("" if initial is None else f"{initial:g}")
        self.display.setObjectName("numpad_display")
        self.display.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.add(self.display)
        if presets:
            row = QtWidgets.QHBoxLayout()
            row.setSpacing(8)
            for text, value in presets:
                chip = Chip(text)
                chip.clicked.connect(lambda _=False, v=value: self._set(f"{v:g}"))
                row.addWidget(chip)
            row.addStretch(1)
            self.add(row)
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(10)
        keys = [("7", "8", "9", "⌫"), ("4", "5", "6", "−"), ("1", "2", "3", "+"), ("±", "0", ".", "÷")]
        for r, row in enumerate(keys):
            for c, key in enumerate(row):
                button = QtWidgets.QPushButton(key)
                button.setObjectName("key")
                if c == 3:
                    button.setProperty("kind", "mod")
                button.setFocusPolicy(Qt.NoFocus)
                button.setMinimumHeight(72)
                button.clicked.connect(lambda _=False, k=key: self._press(k))
                grid.addWidget(button, r, c)
        self.add(grid)
        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(10)
        clear = Button("Clear", variant="outline", size="lg", on_click=lambda: self._set(""))
        ok = Button("Set", icon="check", variant="primary", size="lg", on_click=self._commit)
        actions.addWidget(clear, 1)
        actions.addWidget(ok, 2)
        self.add(actions)

    def _set(self, text):
        self.display.setText(text)
        self.fresh = False

    def _press(self, key):
        text = "" if self.fresh and key not in ("⌫", "±", "−", "+", "÷") else self.display.text()
        self.fresh = False
        if key == "⌫":
            text = text[:-1]
        elif key == "±":
            text = text[1:] if text.startswith("-") else "-" + text
        elif key == "−":
            text += "-"
        elif key == "+":
            text += "+"
        elif key == "÷":
            text += "/"
        else:
            text += key
        self.display.setText(text)

    def value(self) -> Optional[float]:
        text = self.display.text().strip()
        if not text:
            return None
        # Only digits and + - / reach eval (the keypad has nothing else), so no powers or names
        if not all(ch in "0123456789.+-/ " for ch in text):
            return None
        try:
            return float(eval(text, {"__builtins__": {}}, {}))
        except Exception:
            return None

    def _commit(self):
        value = self.value()
        if value is None:
            self.display.setStyleSheet(f"border-color: {C.red};")
            return
        self.close()
        self.on_value(value)


class TouchKeyboard(QtWidgets.QFrame):
    """
    Full on-screen keyboard that slides up from the bottom of the window.

    It types into whichever QLineEdit / QPlainTextEdit has focus, by sending real key
    events, so it works with any widget.
    """

    enter_pressed = QtCore.pyqtSignal()
    visibility_changed = QtCore.pyqtSignal(bool)

    ROWS = [
        list("1234567890"),
        list("qwertyuiop"),
        list("asdfghjkl"),
        ["⇧"] + list("zxcvbnm") + ["⌫"],
        ["?123", ",", "space", ".", "hide", "enter"],
    ]
    SYMBOLS = [
        list("1234567890"),
        list("-/:;()$&@\""),
        list("[]{}#%^*+="),
        ["⇧"] + list("_\\|~<>'") + ["⌫"],
        ["ABC", ",", "space", ".", "hide", "enter"],
    ]

    def __init__(self, host: QtWidgets.QWidget):
        super().__init__(host)
        self.host = host
        self.setObjectName("keyboard")
        self.shift = False
        self.symbols = False
        self.target: Optional[QtWidgets.QWidget] = None
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(120, 14, 120, 18)
        self._layout.setSpacing(10)
        self._build()
        self.hide()
        host.installEventFilter(self)

    def eventFilter(self, obj, event):
        try:
            if obj is self.host and event.type() == QtCore.QEvent.Resize and self.isVisible():
                self._place()
        except (RuntimeError, AttributeError):  # during teardown
            pass
        return False

    def _build(self):
        clear_layout(self._layout)
        rows = self.SYMBOLS if self.symbols else self.ROWS
        for row in rows:
            line = QtWidgets.QHBoxLayout()
            line.setSpacing(10)
            if row is rows[2] and not self.symbols:
                line.addSpacing(40)
            for key in row:
                button = QtWidgets.QPushButton()
                button.setObjectName("key")
                button.setFocusPolicy(Qt.NoFocus)
                button.setAutoRepeat(key == "⌫")
                stretch = 1
                if key == "space":
                    stretch = 6
                    button.setText("")
                elif key in ("⇧", "⌫", "?123", "ABC", "hide"):
                    button.setProperty("kind", "mod")
                    stretch = 2 if key != "hide" else 1
                    if key == "⌫":
                        button.setIcon(theme.icon("backspace", color=C.text_2))
                        button.setIconSize(QtCore.QSize(26, 26))
                    elif key == "hide":
                        button.setIcon(theme.icon("keyboard", color=C.text_2))
                        button.setIconSize(QtCore.QSize(26, 26))
                    elif key == "⇧":
                        button.setIcon(theme.icon("arrow-fat-up-fill" if self.shift else "arrow-fat-up",
                                                  color=C.text if self.shift else C.text_2))
                        button.setIconSize(QtCore.QSize(24, 24))
                    else:
                        button.setText(key)
                elif key == "enter":
                    button.setProperty("kind", "enter")
                    button.setText("Send" if self._target_is_composer() else "Done")
                    stretch = 2
                else:
                    button.setText(key.upper() if self.shift else key)
                button.clicked.connect(lambda _=False, k=key: self._press(k))
                line.addWidget(button, stretch)
            if row is rows[2] and not self.symbols:
                line.addSpacing(40)
            self._layout.addLayout(line)

    def _target_is_composer(self):
        return self.target is not None and self.target.objectName() == "composer_input"

    def show_for(self, target: QtWidgets.QWidget):
        changed = target is not self.target
        self.target = target
        if changed:
            self._build()
        was_visible = self.isVisible()
        self._place()
        self.show()
        self.raise_()
        if not was_visible:
            self.visibility_changed.emit(True)

    def _place(self):
        height = 5 * 64 + 4 * 10 + 32
        self.setGeometry(0, self.host.height() - height, self.host.width(), height)

    def hide_keyboard(self):
        was_visible = self.isVisible()
        self.hide()
        if self.target is not None:
            self.target.clearFocus()
        if was_visible:
            self.visibility_changed.emit(False)

    def _press(self, key):
        target = QtWidgets.QApplication.focusWidget() or self.target
        if key == "⇧":
            self.shift = not self.shift
            return self._build()
        if key in ("?123", "ABC"):
            self.symbols = not self.symbols
            return self._build()
        if key == "hide":
            return self.hide_keyboard()
        if target is None:
            return
        if key == "enter":
            if self._target_is_composer():
                self.enter_pressed.emit()
            else:
                self._send(target, Qt.Key_Return, "\r")
                self.hide_keyboard()
            return
        if key == "⌫":
            return self._send(target, Qt.Key_Backspace, "")
        text = " " if key == "space" else (key.upper() if self.shift else key)
        self._send(target, 0, text)
        if self.shift:
            self.shift = False
            self._build()

    @staticmethod
    def _send(target, key, text):
        for kind in (QtCore.QEvent.KeyPress, QtCore.QEvent.KeyRelease):
            QtWidgets.QApplication.sendEvent(target, QtGui.QKeyEvent(kind, key, Qt.NoModifier, text))


class Toaster(QtCore.QObject):
    """Stacks short notifications at the top-right of the window"""

    ICONS = {"error": ("warning-octagon-fill", C.red), "warning": ("warning-fill", C.amber),
             "success": ("check-circle-fill", C.green), "info": ("info-fill", C.blue)}

    def __init__(self, host: QtWidgets.QWidget, top=96, right=24, width=460):
        super().__init__(host)
        self.host, self.top, self.right, self.width = host, top, right, width
        self.toasts: List[QtWidgets.QFrame] = []

    def show(self, text, level="info", seconds=None):
        toast = QtWidgets.QFrame(self.host)
        toast.setObjectName("toast")
        toast.setProperty("level", level)
        toast.setFixedWidth(self.width)
        row = QtWidgets.QHBoxLayout(toast)
        row.setContentsMargins(18, 14, 10, 14)
        row.setSpacing(14)
        name, color = self.ICONS.get(level, self.ICONS["info"])
        icon = QtWidgets.QLabel()
        icon.setPixmap(theme.pixmap(name, color, 26))
        row.addWidget(icon, 0, Qt.AlignTop)
        text_label = label(text, "toast_text", wrap=True)
        row.addWidget(text_label, 1)
        close = RoundButton("x", 44, variant="ghost", icon_size=18)
        row.addWidget(close, 0, Qt.AlignTop)
        close.clicked.connect(lambda: self.dismiss(toast))
        toast.adjustSize()
        toast.show()
        self.toasts.insert(0, toast)
        # Keep a short stack. Only close our own toasts: Milo's peek shares the "toast" style name
        for old in self.toasts[4:]:
            old.deleteLater()
        del self.toasts[4:]
        self._relayout()
        timeout = seconds if seconds is not None else (10 if level == "error" else 5)
        # The timer belongs to the toast, so it goes away with it (and with the screen)
        timer = QtCore.QTimer(toast)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self.dismiss(toast))
        timer.start(int(timeout * 1000))

    def dismiss(self, toast):
        if toast in self.toasts:
            self.toasts.remove(toast)
            toast.deleteLater()
            self._relayout()

    def _relayout(self):
        y = self.top
        for toast in self.toasts:
            toast.adjustSize()
            toast.move(self.host.width() - self.width - self.right, y)
            toast.raise_()
            y += toast.height() + 10
