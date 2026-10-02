"""
Settings → Pendant: turn the game controller pendant on, watch its controls live, and set
everything about it (safety, speeds, which stick moves which axis, what each button does).
"""

import math
import time

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.pages.probe import ParamRow
from milo_ui.pendant import (ACTIONS, AXES, BUTTONS, CURVES, RADIAL_ACTIONS, STICKS, TOGGLE_ITEMS, TRIGGERS,
                             InputState, conflicts, status_text)

DEADMAN_CHOICES = ("RT", "LT", "RB", "LB")
FINE_CHOICES = ("", "LT", "RT", "LB", "RB")


class PadView(QtWidgets.QWidget):
    """The controller's sticks, triggers and buttons as they are right now"""

    def __init__(self, pendant, parent=None):
        super().__init__(parent)
        self.pendant = pendant
        self.state = InputState()
        self.setMinimumHeight(230)
        self._rate_events, self._rate_time, self.rate = 0, time.monotonic(), 0.0
        pendant.input_changed.connect(self.set_state)

    def set_state(self, state):
        now = time.monotonic()
        if now - self._rate_time >= 1.0:
            self.rate = (state.events - self._rate_events) / (now - self._rate_time)
            self._rate_events, self._rate_time = state.events, now
        self.state = state
        self.update()

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        s, config = self.state, self.pendant.config
        if not s.connected:
            p.setPen(QtGui.QColor(C.text_3))
            p.setFont(theme.font(T.body))
            text = ("Controller not connected. Wake it with its Home button." if config["enabled"]
                    else "Turn the pendant on to see the controller here.")
            p.drawText(self.rect(), Qt.AlignCenter, text)
            return
        h = self.height()
        radius = min(80, (h - 60) / 2)
        cy = 20 + radius
        dead = float(config["deadzone"])
        for i, (x_name, y_name, label) in enumerate((("LX", "LY", "Left stick"), ("RX", "RY", "Right stick"))):
            cx = 30 + radius + i * (2 * radius + 60)
            p.setPen(QtGui.QPen(QtGui.QColor(C.line_hi), 2))
            p.setBrush(QtGui.QColor(C.bg))
            p.drawEllipse(QtCore.QPointF(cx, cy), radius, radius)
            p.setPen(QtGui.QPen(QtGui.QColor(C.text_4), 1, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(QtCore.QPointF(cx, cy), radius * dead, radius * dead)
            dot = QtCore.QPointF(cx + s.sticks[x_name] * radius, cy - s.sticks[y_name] * radius)
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(C.accent_hi))
            p.drawEllipse(dot, 9, 9)
            p.setPen(QtGui.QColor(C.text_3))
            p.setFont(theme.font(T.caption))
            p.drawText(QtCore.QRectF(cx - radius, cy + radius + 6, 2 * radius, 20), Qt.AlignCenter, label)
        # Triggers, with the dead-man threshold marked
        x0 = 30 + 4 * radius + 100
        for i, name in enumerate(TRIGGERS):
            x = x0 + i * 46
            top, height = 20, 2 * radius
            p.setPen(QtGui.QPen(QtGui.QColor(C.line_hi), 2))
            p.setBrush(QtGui.QColor(C.bg))
            p.drawRoundedRect(QtCore.QRectF(x, top, 26, height), 6, 6)
            fill = s.triggers[name] * height
            color = C.green if name == config["deadman"] and s.pressed(name, config["deadman_threshold"]) else C.accent
            p.setPen(Qt.NoPen)
            p.setBrush(QtGui.QColor(color))
            p.drawRoundedRect(QtCore.QRectF(x + 3, top + height - fill, 20, fill), 4, 4)
            if name == config["deadman"]:
                y = top + height * (1 - float(config["deadman_threshold"]))
                p.setPen(QtGui.QPen(QtGui.QColor(C.amber), 2))
                p.drawLine(QtCore.QPointF(x - 4, y), QtCore.QPointF(x + 30, y))
            p.setPen(QtGui.QColor(C.text_3))
            p.drawText(QtCore.QRectF(x - 10, top + height + 6, 46, 20), Qt.AlignCenter, name)
        # Buttons
        x1 = x0 + 120
        names = [b for b in BUTTONS if b not in TRIGGERS]
        per_row = 5
        for i, name in enumerate(names):
            col, row = i % per_row, i // per_row
            rect = QtCore.QRectF(x1 + col * 92, 20 + row * 46, 84, 36)
            on = name in s.buttons
            p.setPen(QtGui.QPen(QtGui.QColor(C.accent if on else C.line_hi), 2))
            p.setBrush(QtGui.QColor(C.accent_soft if on else C.bg))
            p.drawRoundedRect(rect, 8, 8)
            p.setPen(QtGui.QColor(C.text if on else C.text_3))
            label = name.replace("DPAD_", "D-").title() if name.startswith("DPAD") else name.title()
            p.drawText(rect, Qt.AlignCenter, label if len(name) > 2 else name)
        p.setPen(QtGui.QColor(C.text_3))
        p.drawText(QtCore.QRectF(x1, h - 22, 600, 20), Qt.AlignLeft,
                   f"{self.pendant.reader.name if self.pendant.reader else ''} · {self.rate:.0f} events/s")


def build_pendant_tab(shell):
    """The Pendant tab's card"""
    pendant, machine = shell.pendant, shell.machine
    units = machine.units
    card = kit.Card()
    refreshers = []

    def refresh_all():
        for refresh in refreshers:
            refresh()

    def combo_row(title, choices, key, subkey=None):
        """A dropdown for one setting. choices: [(value, label)]"""
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(kit.label(title, "body"), 1)
        combo = QtWidgets.QComboBox()
        combo.setMinimumWidth(320)
        for value, label in choices:
            combo.addItem(label, value)

        def current():
            value = pendant.config[key]
            return value.get(subkey, "") if subkey else value

        def refresh():
            combo.blockSignals(True)
            combo.setCurrentIndex(max(0, combo.findData(current())))
            combo.blockSignals(False)

        def chosen(index):
            value = combo.itemData(index)
            pendant.set(key, {subkey: value} if subkey else value)
        combo.activated.connect(chosen)
        refreshers.append(refresh)
        refresh()
        row.addWidget(combo)
        return row, combo

    def number_row(title, key, unit, scale=1.0, lo=0.0, hi=1e9, index=None):
        def get():
            value = pendant.config[key][index] if index is not None else pendant.config[key]
            return round(float(value) * scale, 4)

        def put(value):
            value = max(lo, min(hi, float(value))) / scale
            if index is not None:
                steps = list(pendant.config[key])
                steps[index] = value
                pendant.set(key, steps)
            else:
                pendant.set(key, value)
        row = ParamRow(title, get, put, unit)
        refreshers.append(row.refresh)
        return row

    # --- on/off and live view ---
    enable = kit.ToggleRow("Use a game controller as a jog pendant",
                           f"Hold {pendant.config['deadman']} and move the sticks to jog. Not an emergency stop.")
    enable.setChecked(pendant.config["enabled"])
    enable.toggled.connect(lambda on: pendant.set("enabled", on))
    card.add(enable)
    status = kit.label("", "muted")
    card.add(status)
    view = PadView(pendant)
    card.add(view)

    def show_status(*_):
        status.setText(status_text(pendant.status, pendant.config)
                       + f"  ·  step {pendant.step_size:g} {units}")
        view.update()
    pendant.status_changed.connect(show_status)
    refreshers.append(show_status)

    # --- safety ---
    card.add(kit.eyebrow("Safety"))
    row, _ = combo_row("Dead-man control (hold to allow motion)",
                       [(c, BUTTONS[c]) for c in DEADMAN_CHOICES], "deadman")
    card.add(row)
    card.add(number_row("Dead-man pressed at least", "deadman_threshold", "%", 100, 10, 100))
    card.add(number_row("Pause stick jogging after no input for", "hold_timeout", "s", 1, 0.3, 10))

    # --- speed ---
    card.add(kit.eyebrow("Speed"))
    card.add(number_row("X / Y speed at full stick", "max_xy", f"{units}/min", 1, 1, machine.max_velocity))
    card.add(number_row("Z speed at full stick", "max_z", f"{units}/min", 1, 1, machine.max_velocity))
    card.add(number_row("Stick dead zone", "deadzone", "%", 100, 0, 50))
    curve_row = QtWidgets.QHBoxLayout()
    curve_row.addWidget(kit.label("Stick response", "body"), 1)
    curve_names = list(CURVES)
    curve = kit.Segmented(curve_names)
    curve.selected.connect(lambda i: pendant.set("curve", curve_names[i]))
    refreshers.append(lambda: curve.set_index(curve_names.index(pendant.config["curve"])
                                              if pendant.config["curve"] in curve_names else 1))
    curve_row.addWidget(curve)
    card.add(curve_row)
    row, _ = combo_row("Fine-mode control (squeeze to slow down)",
                       [(c, BUTTONS[c] if c else "None") for c in FINE_CHOICES], "fine")
    card.add(row)
    card.add(number_row("Slowest fine-mode speed", "fine_min", "%", 100, 1, 100))

    # --- sticks ---
    card.add(kit.eyebrow("Sticks"))
    for axis in AXES:
        row, _ = combo_row(f"{axis} axis", [("", "Not on a stick")] + list(STICKS.items()), "sticks", axis)
        flip = kit.Toggle(pendant.config["invert"].get(axis, False))
        flip.toggled.connect(lambda on, a=axis: pendant.set("invert", {a: on}))
        def refresh_flip(f=flip, a=axis):
            on = bool(pendant.config["invert"].get(a, False))
            f.blockSignals(True)
            f.setChecked(on)
            f._set_pos(1.0 if on else 0.0)
            f.blockSignals(False)
        refreshers.append(refresh_flip)
        row.addSpacing(12)
        row.addWidget(kit.label("Reverse", "muted"))
        row.addWidget(flip)
        card.add(row)

    # --- steps ---
    card.add(kit.eyebrow("Step sizes (smaller / larger step buttons cycle through these)"))
    for i in range(len(pendant.config["steps"])):
        card.add(number_row(f"Step {i + 1}", "steps", units, 1, 0.0001, 100, index=i))

    # --- quick menu ---
    card.add(kit.eyebrow("Quick menu (opened with its button; tilt a stick, then A to run, B to close)"))
    for item, (label, _, needs_deadman) in RADIAL_ACTIONS.items():
        if item in TOGGLE_ITEMS:
            hint = f"Shows the one that applies. Switching on needs {pendant.config['deadman']} held; off doesn't"
        elif needs_deadman:
            hint = f"Needs {pendant.config['deadman']} held to run"
        else:
            hint = "Runs without the dead-man"
        row = kit.ToggleRow(label, hint)

        def toggle(on, item=item):
            items = [i for i in RADIAL_ACTIONS if (i == item and on) or (i != item and i in pendant.config["radial_items"])]
            pendant.set("radial_items", items)
        row.toggled.connect(toggle)
        refreshers.append(lambda r=row, i=item: r.setChecked(i in pendant.config["radial_items"]))
        card.add(row)
    card.add(number_row("“Start spindle” speed (0 = the machine's default)", "radial_rpm", "rpm", 1, 0,
                        machine.spindle_max))

    # --- buttons ---
    card.add(kit.eyebrow("Buttons"))
    button_choices = [("", "Nothing")] + [(b, label) for b, label in BUTTONS.items()]
    for action, (label, needs_deadman) in ACTIONS.items():
        note = {"confirm": "  (with the dead-man held)", "talk": "  (without the dead-man)"}.get(
            action, "" if needs_deadman else "  (works without the dead-man)")
        row, _ = combo_row(label + note,
                           button_choices, "buttons", action)
        card.add(row)
    problems = kit.label("", "body", color=C.amber, wrap=True)
    card.add(problems)
    refreshers.append(lambda: problems.setText("\n".join(f"⚠ {p}" for p in conflicts(pendant.config))))

    reset = kit.Button("Reset to defaults", icon="arrow-counter-clockwise", variant="outline")
    reset.clicked.connect(lambda: kit.ActionSheet(card, "Reset the pendant settings?", [
        ("arrow-counter-clockwise", "Reset", pendant.reset, "warn"), ("x", "Cancel", lambda: None),
    ], subtitle="Speeds, sticks and buttons go back to the defaults.").show_centered())
    card.add(reset, 0)

    pendant.config_changed.connect(refresh_all)
    refresh_all()
    card.enable_toggle = enable
    card.pad_view = view
    return card
