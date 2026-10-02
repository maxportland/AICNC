"""
The screen's frame: navigation rail, machine status bar, the page area with the toolpath
stage, and the dock (Milo's composer + cycle controls). Overlays (confirmation sheet,
Milo's peek, toasts, on-screen keyboard) live here too, and so does the bridge between
Milo's engine and the UI.
"""

import json
import os
import time
from typing import Dict, Optional

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.machine import MachineModel, STATE_TONES
from milo_ui.widgets import Stage, CycleControls
from milo_ui.assistant.orb import Orb
from milo_ui.assistant.composer import Composer, ConfirmSheet, Peek
from milo_ui.assistant.conversation import classify_log_line

UI_PREFS_PATH = os.path.expanduser("~/.linuxcnc/milo_ui.json")

TONE_COLORS = {"red": C.red, "amber": C.amber, "green": C.green, "accent": C.accent_hi, "muted": C.text_3}


class Prefs:
    """Small JSON store for screen preferences (not machine settings)"""

    def __init__(self, path=UI_PREFS_PATH):
        self.path = path
        self.data = {}
        try:
            with open(path) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            pass

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w") as f:
                json.dump(self.data, f, indent=2)
        except OSError:
            pass


# =======================================================================================
# Rail
# =======================================================================================

class RailButton(QtWidgets.QToolButton):
    def __init__(self, icon, text, parent=None):
        super().__init__(parent)
        self.setObjectName("rail_button")
        self.setText(text)
        self.setIcon(theme.icon(icon, color=C.text_3, color_on=C.text, color_active=C.text))
        self.setIconSize(QtCore.QSize(28, 28))
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setCheckable(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(88, 80)


class Rail(QtWidgets.QFrame):
    selected = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("rail")
        self.setFixedWidth(104)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(8, 16, 8, 16)
        self._layout.setSpacing(6)
        self.logo = Orb(diameter=40, show_icon=False)
        self.logo.setToolTip("Milo")
        self.logo.clicked.connect(lambda: self.selected.emit("home"))
        self._layout.addWidget(self.logo, 0, Qt.AlignHCenter)
        self._layout.addSpacing(14)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        self.buttons: Dict[str, RailButton] = {}
        self._bottom_index = None

    def add(self, key, icon, text, bottom=False):
        button = RailButton(icon, text)
        button.clicked.connect(lambda: self.selected.emit(key))
        self.group.addButton(button)
        self.buttons[key] = button
        if bottom and self._bottom_index is None:
            self._layout.addStretch(1)
            self._bottom_index = True
        self._layout.addWidget(button, 0, Qt.AlignHCenter)

    def set_current(self, key):
        if key in self.buttons:
            self.buttons[key].setChecked(True)

    def set_attention(self, key, on):
        if key in self.buttons:
            theme.set_prop(self.buttons[key], "attention", bool(on))


# =======================================================================================
# Top bar
# =======================================================================================

class StatePill(QtWidgets.QFrame):
    """Colored machine state with a one-line detail"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("pill")
        self.setMinimumHeight(56)
        # Never squeezed: when the top bar is tight the info pills shorten instead
        self.setSizePolicy(QtWidgets.QSizePolicy.Minimum, QtWidgets.QSizePolicy.Preferred)
        self.setStyleSheet("#pill { border-radius: 28px; padding: 0; }")
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(16, 6, 22, 6)
        row.setSpacing(10)
        self.dot = kit.Dot(C.green, 12)
        row.addWidget(self.dot)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(0)
        self.text = kit.label("", "state_pill_text")
        self.sub = kit.label("", "state_pill_sub")
        words.addWidget(self.text)
        words.addWidget(self.sub)
        row.addLayout(words)

    def set_state(self, label, detail, tone):
        color = TONE_COLORS.get(tone, C.text)
        self.text.setText(label)
        self.text.setStyleSheet(f"color: {color};")
        self.sub.setText(detail)
        self.dot.set_color(color, pulse=label in ("RUNNING", "HOMING", "E-STOP"))
        border = {"red": "#6A2334", "amber": "#6B5217", "green": "#1D4C3B", "accent": "#3E3480"}.get(tone, C.line)
        bg = {"red": "#22121A", "amber": "#1C170C", "green": "#0F1C18", "accent": "#16142C"}.get(tone, C.card_hi)
        self.setStyleSheet(f"#pill {{ border-radius: 28px; padding: 0; border-color: {border}; background: {bg}; }}")


class InfoPill(QtWidgets.QPushButton):
    """
    A tappable fact in the status bar (tool, work offset, spindle...).

    A shrinkable pill (the long ones: tool, pendant) shortens its text with "…" when the bar runs
    out of room instead of insisting on its full width: several pills at once (running, spindle
    on, pendant, a long tool name) would otherwise make the window wider than the screen. The
    short ones (work offset, spindle, feed) always show in full. text() is the full text.
    """

    MIN_CHARS = "WWW…"  # the narrowest a pill gets: its icon and a few characters

    def __init__(self, icon, shrinkable=False, parent=None):
        super().__init__(parent)
        self.shrinkable = shrinkable
        self.setObjectName("pill")
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setIcon(theme.icon(icon, color=C.text_3))
        self.setIconSize(QtCore.QSize(20, 20))
        self.setMinimumHeight(48)
        # Preferred, not QPushButton's Minimum: the layout may make it narrower than its text
        policy = QtWidgets.QSizePolicy.Preferred if shrinkable else QtWidgets.QSizePolicy.Minimum
        self.setSizePolicy(policy, QtWidgets.QSizePolicy.Fixed)
        self._full = ""

    def text(self):
        return self._full

    def setText(self, text):
        self._full = text or ""
        self._fit()
        self.updateGeometry()

    def _chrome(self):
        """Width of everything but the text: icon, padding, border"""
        return super().sizeHint().width() - self.fontMetrics().horizontalAdvance(super().text())

    def sizeHint(self):
        hint = super().sizeHint()
        return QtCore.QSize(self._chrome() + self.fontMetrics().horizontalAdvance(self._full), hint.height())

    def minimumSizeHint(self):
        hint = self.sizeHint()
        if not self.shrinkable:
            return hint
        narrowest = self._chrome() + self.fontMetrics().horizontalAdvance(self.MIN_CHARS)
        return QtCore.QSize(min(hint.width(), narrowest), hint.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self):
        metrics = self.fontMetrics()
        room = self.width() - self._chrome()
        shown = self._full if metrics.horizontalAdvance(self._full) <= room else \
            metrics.elidedText(self._full, Qt.ElideRight, max(0, room))
        if shown != super().text():
            super().setText(shown)


class TopBar(QtWidgets.QFrame):
    navigate = QtCore.pyqtSignal(str)

    def __init__(self, machine: MachineModel, parent=None):
        super().__init__(parent)
        self.setObjectName("topbar")
        self.setFixedHeight(84)
        self.machine = machine
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(20, 12, 20, 12)
        row.setSpacing(12)

        self.state = StatePill()
        row.addWidget(self.state)
        self.next_step = kit.Button("", variant="primary")
        self.next_step.clicked.connect(self._do_next_step)
        row.addWidget(self.next_step)
        row.addSpacing(8)

        self.tool = InfoPill("wrench", shrinkable=True)
        self.tool.clicked.connect(lambda: self.navigate.emit("tools"))
        self.wcs = InfoPill("crosshair")
        self.wcs.clicked.connect(lambda: self.navigate.emit("offsets"))
        self.spindle = InfoPill("gauge")
        self.spindle.clicked.connect(lambda: self.navigate.emit("jog"))
        self.feed = InfoPill("lightning")
        self.feed.clicked.connect(lambda: self.navigate.emit("jog"))
        self.pendant = InfoPill("game-controller", shrinkable=True)  # the game controller pendant's state, when it's on
        self.pendant.hide()
        for pill in (self.tool, self.wcs, self.spindle, self.feed, self.pendant):
            row.addWidget(pill)
        row.addStretch(1)

        self.clock = kit.label("", "clock")
        row.addWidget(self.clock)
        row.addSpacing(8)
        self.power = kit.Button("Power", icon="power", size="lg")
        self.power.setMinimumWidth(150)
        self.power.clicked.connect(self._power_clicked)
        row.addWidget(self.power)
        self.estop = kit.Button("E-STOP", icon="hand-palm-fill", variant="danger", size="lg")
        self.estop.setMinimumWidth(190)
        self.estop.clicked.connect(self._estop_clicked)
        self._estop_tripped_at = 0.0
        self._power_changed_at = 0.0
        row.addWidget(self.estop)

        machine.changed.connect(self._on_changed)
        self._clock_timer = QtCore.QTimer(self)
        self._clock_timer.timeout.connect(self._tick)
        self._clock_timer.start(1000)
        self._tick()
        self.refresh()

    def _on_changed(self, topic):
        if topic in ("state", "homing", "tool", "offsets", "spindle", "overrides", "program"):
            self.refresh()

    def _tick(self):
        self.clock.setText(time.strftime("%H:%M"))
        if self.machine.is_running:
            self._refresh_feed()

    def _refresh_feed(self):
        m = self.machine
        if m.is_running:
            self.feed.setText(f"{m.feed_rate:,.0f} {m.units}/min · {m.feed_override:.0f}%")
        else:
            self.feed.setText(f"Feed {m.feed_override:.0f}%")
        self.feed.setVisible(m.is_running or m.feed_override != 100)

    def refresh(self):
        m = self.machine
        if m.estop and not self._estop_tripped_at:
            self._estop_tripped_at = time.time()
        elif not m.estop:
            self._estop_tripped_at = 0.0
        label = m.state_label
        self.state.set_state(label, m.state_detail, STATE_TONES.get(label, "muted"))

        step = None
        if not m.estop and not m.on:
            step = ("Turn on", "power")
        elif m.on and not m.all_homed and not m.homing:
            step = ("Home all", "house-line")
        self.next_step.setVisible(step is not None)
        if step:
            self.next_step.setText(step[0])
            self.next_step.set_icon_name(step[1])

        if m.tool:
            name = m.tool_comment or ""
            text = f"T{m.tool}" + (f" · {name}" if name else "")
            if m.tool_diameter:
                text += f" · ⌀{m.tool_diameter:g}"
        else:
            text = "No tool"
        self.tool.setText(self.tool.fontMetrics().elidedText(text, Qt.ElideRight, 300))
        self.wcs.setText(f"{m.wcs} · {m.units}")
        if m.spindle_dir:
            arrow = "↻" if m.spindle_dir > 0 else "↺"
            self.spindle.setText(f"{arrow} {m.spindle_actual:,.0f} rpm")
        self.spindle.setVisible(bool(m.spindle_dir))
        self._refresh_feed()

        # Power & E-stop
        self.power.setEnabled(not m.estop)
        self.power.setText("On" if m.on else "Off")
        self.power.set_variant("go" if m.on else "outline")
        if m.estop:
            self.estop.setText("Reset E-stop")
            self.estop.set_variant("warn")
            self.estop.set_icon_name("arrow-counter-clockwise")
        else:
            self.estop.setText("E-STOP")
            self.estop.set_variant("danger")
            self.estop.set_icon_name("hand-palm-fill")

    def _estop_clicked(self):
        """E-STOP always trips. Reset is the same button once it reads "Reset E-stop", but not in
        the first 1.5 s, so a panicked double tap can't undo the stop it just made."""
        m = self.machine
        if not m.estop:
            self._estop_tripped_at = time.time()
            m.set_estop(True)
        elif time.time() - self._estop_tripped_at > 1.5:
            m.set_estop(False)

    def _power_clicked(self):
        if time.time() - self._power_changed_at < 1.0:
            return  # ignore a double tap
        self._power_changed_at = time.time()
        self.machine.set_power(not self.machine.on)

    def _do_next_step(self):
        m = self.machine
        if not m.on:
            m.set_power(True)
        elif not m.all_homed:
            m.home_all()


# =======================================================================================
# Engine bridge
# =======================================================================================

class EngineBridge(QtCore.QObject):
    """
    Receives MiloEngine's callbacks (EngineListener) and updates the UI.

    Engine callbacks can arrive from worker-thread signal handlers, which Qt already
    delivers on the main thread, so the UI can be touched directly here.
    """

    def __init__(self, shell: "MiloShell"):
        super().__init__(shell)
        self.shell = shell

    def on_message(self, line):
        self.shell.on_milo_message(line)

    def on_busy(self, busy, text):
        self.shell.composer.set_busy(busy, text)
        if self.shell.conversation is not None:
            self.shell.conversation.set_working(busy, text)

    def on_proposal(self, action):
        self.shell.show_proposal(action)

    def on_voice_state(self, state):
        self.shell.composer.set_voice_state(state)
        orb = self.shell.home_orb()
        if orb is not None:
            orb.set_state(state if state != "idle" else "idle")

    def on_audio_level(self, level):
        self.shell.composer.set_level(level)
        orb = self.shell.home_orb()
        if orb is not None:
            orb.set_level(level)

    def on_transcript(self, text):
        pass

    def on_clear_input(self):
        self.shell.composer.clear()

    def on_program_ready(self, info):
        if self.shell.conversation is not None:
            self.shell.conversation.add_program(info)
        if self.shell.current != "home":
            self.shell.peek.show_text(f"Program ready: {info.get('name')}. It's loaded for review.", "success",
                                      self.shell.dock.height(), self.shell.rail.width() + 24)

    def on_wake(self):
        self.shell.window().raise_()

    def on_show_run(self):
        self.shell.navigate("toolpath")

    def on_conversation_reset(self):
        if self.shell.conversation is not None:
            self.shell.conversation.clear()


# =======================================================================================
# Shell
# =======================================================================================

class Page(QtWidgets.QWidget):
    """Base for pages: title, stage visibility, and hooks"""

    key = ""
    title = ""
    icon = ""
    wants_stage = False  # the toolpath stage shows beside the page
    full_stage = False  # ...or fills the content area instead of the page

    def on_show(self):
        pass


class MiloShell(QtWidgets.QWidget):
    """The whole screen"""

    def __init__(self, machine: MachineModel, graphics: Optional[QtWidgets.QWidget] = None,
                 prefs: Optional[Prefs] = None, parent=None):
        super().__init__(parent)
        self.setObjectName("milo_root")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.machine = machine
        self.prefs = prefs or Prefs()
        # Work offset undo history survives a restart (the offsets themselves are in linuxcnc.var)
        machine.offset_history = list(self.prefs.get("offset_history") or [])
        machine.changed.connect(lambda topic: topic == "offset_history"
                                and self.prefs.set("offset_history", machine.offset_history))
        self.engine = None
        self.conversation = None
        self.pages: Dict[str, Page] = {}
        self.current = None

        root = QtWidgets.QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.rail = Rail()
        self.rail.selected.connect(self.navigate)
        root.addWidget(self.rail)

        right = QtWidgets.QVBoxLayout()
        right.setContentsMargins(0, 0, 0, 0)
        right.setSpacing(0)
        root.addLayout(right, 1)

        self.topbar = TopBar(machine)
        self.topbar.navigate.connect(self.navigate)
        right.addWidget(self.topbar)

        from milo_ui.pendant import Pendant, status_text
        self.pendant = Pendant(machine, self.prefs)
        self.pendant.talk.connect(self._voice)
        self.pendant.confirm.connect(lambda: self.engine is not None and self.engine.confirm())

        def show_pendant_status(*_):
            chip, status = self.topbar.pendant, self.pendant.status
            chip.setText(f"{status_text(status, self.pendant.config)} · step {self.pendant.step_size:g} {machine.units}")
            chip.setVisible(status != "off")
        self.pendant.status_changed.connect(show_pendant_status)
        self.pendant.config_changed.connect(show_pendant_status)  # e.g. the step size buttons
        self.topbar.pendant.clicked.connect(lambda: self.show_settings_tab("Pendant"))
        self._build_quick_menu()

        body = QtWidgets.QHBoxLayout()
        body.setContentsMargins(20, 20, 20, 20)
        body.setSpacing(20)
        self.stack = QtWidgets.QStackedWidget()
        # Pages may be squeezed (clipped at the bottom) while the keyboard is up, so the dock
        # and the composer always stay on screen above it
        self.stack.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Ignored)
        body.addWidget(self.stack, 1)
        self.stage = Stage(machine, graphics)
        self._place_stage(full=False)
        self.stage.open_files.connect(lambda: self.navigate("program"))
        body.addWidget(self.stage)
        right.addLayout(body, 1)

        self.dock = QtWidgets.QFrame()
        self.dock.setObjectName("dock")
        self.dock.setFixedHeight(112)
        dock = QtWidgets.QHBoxLayout(self.dock)
        dock.setContentsMargins(12, 12, 20, 12)
        dock.setSpacing(20)
        self.composer = Composer()
        self.composer.submitted.connect(self.ask)
        self.composer.voice_clicked.connect(self._voice)
        self.composer.cancel_listening.connect(lambda: self.engine and self.engine.cancel_listening())
        dock.addWidget(self.composer, 1)
        dock.addWidget(kit.vline())
        self.cycle = CycleControls(machine)
        self.cycle.start_requested.connect(self.start_program)
        self.cycle.stop.clicked.connect(lambda: self.engine and self.engine.stop_speaking())
        dock.addWidget(self.cycle)
        right.addWidget(self.dock)

        self.keyboard = kit.TouchKeyboard(self)
        self.keyboard.enter_pressed.connect(self._keyboard_send)
        self.keyboard.visibility_changed.connect(self._keyboard_visible)

        self.confirm_sheet = ConfirmSheet(self, remaining=self._confirm_remaining)
        self.confirm_sheet.confirmed.connect(lambda action_id: self.engine and self.engine.confirm(action_id))
        self.confirm_sheet.cancelled.connect(lambda: self.engine and self.engine.cancel())
        self.peek = Peek(self)
        self.peek.open_clicked.connect(lambda: self.navigate("home"))
        self.toaster = kit.Toaster(self, top=100)
        machine.message.connect(self._machine_message)

        QtWidgets.QApplication.instance().installEventFilter(self)

    # --- pages ----------------------------------------------------------------------------

    def add_page(self, page: Page, bottom=False):
        self.pages[page.key] = page
        # Each page sits in a scroll area: when the keyboard takes space it clips (and can be
        # dragged) instead of squeezing its widgets on top of each other
        holder = QtWidgets.QScrollArea()
        holder.setObjectName("page_holder")
        holder.setWidgetResizable(True)
        holder.setFrameShape(QtWidgets.QFrame.NoFrame)
        holder.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        holder.setWidget(page)
        holder.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Ignored)
        page.holder = holder
        self.stack.addWidget(holder)
        self.rail.add(page.key, page.icon, page.title, bottom=bottom)

    def navigate(self, key):
        if key not in self.pages:
            return
        self.current = key
        page = self.pages[key]
        # A stacked widget is as wide as its widest page; only the showing page should count
        for other in self.pages.values():
            policy = QtWidgets.QSizePolicy.Preferred if other is page else QtWidgets.QSizePolicy.Ignored
            other.holder.setSizePolicy(policy, QtWidgets.QSizePolicy.Ignored)
        self.stack.setCurrentWidget(page.holder)
        self._place_stage(full=page.full_stage)
        self.stack.setVisible(not page.full_stage)
        self.stage.setVisible(page.wants_stage)
        self.rail.set_current(key)
        if key == "home":
            self.peek.hide()
        page.on_show()

    # --- the pendant's quick menu -----------------------------------------------------------

    def _build_quick_menu(self):
        from milo_ui.radial import RadialMenu
        from milo_ui.pendant import radial_entry
        self.quick_menu = RadialMenu(self)
        p = self.pendant

        def items():
            return [radial_entry(item, self.machine)[:2] for item in p.menu_items]

        def opened(is_open):
            if is_open:
                closes = "B goes back" if p.in_submenu else "B closes"
                self.quick_menu.open(items(), f"Tilt a stick, then {self._menu_select_name()} · {closes}",
                                     title=p.menu_title)
            else:
                self.quick_menu.hide()
        p.menu_changed.connect(opened)
        # Toggles (spindle, mist, power) follow the machine while the menu is open
        self.machine.changed.connect(lambda topic: topic in ("state", "spindle", "coolant")
                                     and self.quick_menu.isVisible() and self.quick_menu.set_items(items()))
        p.menu_hover.connect(self.quick_menu.set_hover)
        p.menu_hint.connect(self.quick_menu.set_hint)
        p.quick_action.connect(self.run_quick_action)
        # Tapping a slice runs it (the person at the screen chose it deliberately)
        self.quick_menu.picked.connect(lambda i: p.choose(p.menu_items[i]))
        self.quick_menu.dismissed.connect(p.close_menu)

        from milo_ui.radial import AdjustPanel
        self.adjust_panel = AdjustPanel(self)
        p.adjust_changed.connect(self.adjust_panel.set_value)
        p.adjust_done.connect(self._adjust_done)
        self.adjust_panel.dragged.connect(p.set_adjust_value)
        self.adjust_panel.apply_clicked.connect(lambda: p.close_adjuster(True))
        self.adjust_panel.cancel_clicked.connect(lambda: p.close_adjuster(False))

    def _open_adjuster(self, spec):
        hint = "Tilt a stick or use the D-pad to change it (hold to go faster) · A applies · B cancels"
        self.adjust_panel.open(spec, hint)
        self.pendant.open_adjuster(spec)

    def _adjust_done(self, apply, value):
        spec = self.adjust_panel.spec
        self.adjust_panel.hide()
        if not apply:
            return
        m, say = self.machine, self.toaster.show
        if spec.get("kind") == "spindle":
            if m.spindle_dir:
                m.spindle_start(m.spindle_dir, value)
                say(f"Spindle speed {value:,.0f} rpm.", "info")
            else:
                self.pendant.set("radial_rpm", value)
                say(f"Spindle will start at {value:,.0f} rpm (Start spindle in the quick menu).", "info")
        elif spec.get("kind") == "jog":
            self.pendant.set("max_xy", value)
            say(f"Jog speed {value:,.0f} {m.units}/min at full stick.", "info")
        elif spec.get("kind") == "feed":
            m.set_feed_override(value)
            say(f"Feed override {value:.0f}%.", "info")
    def _menu_select_name(self):
        button = self.pendant.config["buttons"].get("radial")
        return "A" if button in (None, "", "A") else f"A or {button}"

    def run_quick_action(self, item):
        """Run a quick-menu item, with a toast saying what happened"""
        m, say = self.machine, self.toaster.show
        if item == "probe":
            return self.navigate("probe")
        if item == "set_spindle":
            running = bool(m.spindle_dir)
            current = m.spindle_requested if running and m.spindle_requested else (
                float(self.pendant.config.get("radial_rpm") or 0) or m.spindle_default)
            return self._open_adjuster({
                "kind": "spindle", "title": "Spindle speed", "unit": "rpm", "value": current,
                "min": m.spindle_min, "max": m.spindle_max, "step": m.spindle_step or 100,
                "current_text": f"Running, set to {current:,.0f} rpm" if running else
                "Spindle is off: this sets the speed it starts at"})
        if item == "set_jog":
            return self._open_adjuster({
                "kind": "jog", "title": "Jog speed (X / Y at full stick)", "unit": f"{m.units}/min",
                "value": float(self.pendant.config["max_xy"]), "min": 50, "max": m.max_velocity, "step": 50,
                "current_text": f"Now {float(self.pendant.config['max_xy']):,.0f} {m.units}/min · "
                                f"Z stays at {float(self.pendant.config['max_z']):,.0f}"})
        if item == "set_feed":
            return self._open_adjuster({
                "kind": "feed", "title": "Feed override", "unit": "%", "value": m.feed_override,
                "min": 0, "max": m.max_feed_override, "step": 5,
                "current_text": f"Now {m.feed_override:.0f}% of the programmed feed"})
        if item == "talk":
            return self._voice()
        if item.startswith("probe_"):
            return self._probe_from_pendant(item[6:])
        if item == "power":
            if m.on:
                m.set_power(False)
                return say("Machine off.", "info")
            if m.estop:
                return say("Release the E-stop first.", "warning")
            m.set_power(True)
            return say("Machine on.", "info")
        if m.estop or not m.on:
            return say("Turn the machine on first.", "warning")
        if item == "home_all":
            if m.is_running:
                return say("Not while a program is running.", "warning")
            m.home_all()
            say("Homing all axes.", "info")
        elif item == "spindle":
            if m.spindle_dir:
                m.spindle_stop()
                return say("Spindle stopped.", "info")
            if m.is_running:
                return say("Not while a program is running.", "warning")
            rpm = float(self.pendant.config.get("radial_rpm") or 0) or m.spindle_default
            m.spindle_start(1, rpm)
            say(f"Spindle on at {rpm:,.0f} rpm.", "info")
        elif item == "mist":
            turning_on = not m.mist  # read first: the real machine reports the change on a later poll
            m.toggle_mist()
            say("Mist on." if turning_on else "Mist off.", "info")
        elif item.startswith("zero_"):
            if m.is_running:
                return say("Not while a program is running.", "warning")
            axes = list(m.axes) if item == "zero_all" else [item[5:].upper()]
            m.remember_offsets("Zero " + ("all" if len(axes) > 1 else axes[0]))  # one undo for all of them
            for axis in axes:
                m.set_axis_origin(axis, 0.0, remember=False)
            say(f"{' '.join(axes)} zeroed in {m.wcs}.", "success")
        elif item in ("go_work_zero", "go_abs_home", "go_g54"):
            if not m.ready:
                return say(m.state_detail or "The machine isn't ready.", "warning")
            if item == "go_work_zero":
                m.go_to_work_zero()
                say(f"Going to work zero (X0 Y0 in {m.wcs}).", "info")
            elif item == "go_abs_home":
                m.mdi_lines(["G90 G53 G0 Z0", "G90 G53 G0 X0 Y0"])
                say("Going to machine home (ABS X0 Y0 Z0).", "info")
            else:
                # Z up, over to G54's X0 Y0, down to its Z0; then back to the active system
                lines = ["G90 G53 G0 Z0", "G54 G90 G0 X0 Y0", "G90 G0 Z0"]
                if m.wcs != "G54":
                    lines.append(m.wcs)
                m.mdi_lines(lines)
                say("Going to G54 X0 Y0 Z0.", "info")
        elif item == "z_top":
            if not m.ready:
                return say(m.state_detail or "The machine isn't ready.", "warning")
            m.mdi(f"G53 G0 Z{m.limits['Z'][1]:.4f}")
            say("Raising Z to the top.", "info")

    def _probe_from_pendant(self, goal):
        """Set up probing on the Probe page, then ask for confirmation (dead-man + confirm runs it)"""
        page = self.pages.get("probe")
        if page is None:
            return
        action, reason = page.prepare({"goal": goal})
        if action is None:
            return self.toaster.show(reason, "warning")
        gate = getattr(self.engine, "confirmation", None)
        if gate is None:
            return self.toaster.show("Set up on the Probe page: tap Start probing.", "info")
        gate.propose(action)

    def show_settings_tab(self, name):
        self.navigate("settings")
        page = self.pages.get("settings")
        if page is not None and name in getattr(page, "tab_names", []):
            page.show_tab(page.tab_names.index(name))

    def _place_stage(self, full):
        """Beside the page at a fixed width, or filling the content area"""
        if full:
            self.stage.setMinimumWidth(600)
            self.stage.setMaximumWidth(QtWidgets.QWIDGETSIZE_MAX)
            self.stage.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Ignored)
        else:
            self.stage.setFixedWidth(600)
            self.stage.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Ignored)

    def home_orb(self):
        home = self.pages.get("home")
        if home is not None and self.conversation is not None and self.conversation.welcome.isVisible():
            return self.conversation.welcome.orb
        return None

    # --- Milo -------------------------------------------------------------------------------

    def attach_engine(self, engine):
        """Connect a MiloEngine (already constructed with self.bridge as its listener)"""
        self.engine = engine
        if "probe" in self.pages and hasattr(engine, "probe_handler"):
            engine.probe_handler = self.pages["probe"]
        if "offsets" in self.pages and hasattr(engine, "context_providers"):
            engine.context_providers.append(self.pages["offsets"].describe_for_milo)
        self.composer.set_ai_available(bool(engine.api_key()))
        if self.conversation is not None:
            self.conversation.set_show_details(engine.show_details)

    def make_bridge(self):
        self.bridge = EngineBridge(self)
        return self.bridge

    def ask(self, text):
        if self.engine is None:
            return
        self.engine.submit(text)

    def _voice(self):
        if self.engine is None:
            return
        if not self.engine.api_key():
            self.toaster.show("Add your OpenAI API key in Settings so Milo can listen.", "warning")
            self.navigate("settings")
            return
        self.engine.toggle_voice()

    def on_milo_message(self, line):
        if self.conversation is not None:
            self.conversation.add_log_line(line)
        kind, text, _ = classify_log_line(line)
        if self.current != "home" and kind in ("assistant", "error", "success", "warning"):
            level = {"assistant": "info"}.get(kind, kind)
            self.peek.show_text(text, level, self.dock.height(), self.rail.width() + 24)

    def show_proposal(self, action):
        if action is None:
            self.confirm_sheet.dismiss()
            return
        center = self.rail.width() + (self.width() - self.rail.width()) // 2
        self.peek.hide()
        self.confirm_sheet.show_action(action, self.dock.height(), center)

    def _confirm_remaining(self):
        gate = getattr(self.engine, "confirmation", None)
        return gate.remaining_seconds() if gate is not None else 0.0

    def start_program(self):
        """Cycle Start: runs the loaded program directly (the operator pressed Start)"""
        m = self.machine
        if not m.file:
            return self.toaster.show("Load a program first.", "warning")
        if not m.ready:
            return self.toaster.show(m.state_detail or "The machine isn't ready.", "warning")
        if m.probe_in_spindle:
            from probe_jobs import spins_before_tool_change
            try:
                with open(m.file, errors="ignore") as f:
                    spins = spins_before_tool_change(f.read())
            except OSError:
                spins = True
            if spins:
                return self.toaster.show(f"The touch probe (T{m.probe_tool}) is in the spindle and this program starts "
                                         "the spindle before changing tools. Load the cutting tool first.", "warning")
        m.run(self.pages["program"].start_line() if "program" in self.pages else 0)

    # --- machine messages & keyboard --------------------------------------------------------

    def _machine_message(self, level, text):
        if level == "info" and text.startswith("MDI:"):
            return
        self.toaster.show(text, level)

    def eventFilter(self, obj, event):
        # Only a tap on a text field brings the keyboard up, not programmatic focus
        if event.type() == QtCore.QEvent.MouseButtonRelease and self.prefs.get("touch_keyboard", True) \
                and isinstance(obj, QtWidgets.QWidget):
            target = obj
            if not isinstance(target, QtWidgets.QLineEdit):
                target = obj.parentWidget()  # a text edit receives clicks on its viewport
            editable = isinstance(target, (QtWidgets.QLineEdit, QtWidgets.QPlainTextEdit, QtWidgets.QTextEdit))
            if editable and not target.isReadOnly() and target.window() is self.window() \
                    and not self.keyboard.isAncestorOf(target):
                QtCore.QTimer.singleShot(0, lambda t=target: self._show_keyboard(t))
        return False

    def _show_keyboard(self, target):
        if QtWidgets.QApplication.focusWidget() is target:
            self.keyboard.show_for(target)

    def ask_text(self, title, on_text, placeholder="", initial="", action="Save", hint=None):
        """A small popover with one text field; the keyboard comes up with it"""
        dialog = kit.Popover(self, title=title, width=640)
        if hint:
            dialog.add(kit.label(hint, "muted", wrap=True))
        field = QtWidgets.QLineEdit(initial)
        field.setPlaceholderText(placeholder)
        dialog.add(field)
        ok = kit.Button(action, icon="check", variant="primary", size="lg")

        def done():
            text = field.text().strip()
            if text:
                dialog.close()
                self.keyboard.hide_keyboard()
                on_text(text)
        ok.clicked.connect(done)
        field.returnPressed.connect(done)
        dialog.closed.connect(self.keyboard.hide_keyboard)
        dialog.add(ok)
        dialog.show_at(self.topbar, "below")
        dialog.move(self.width() // 2 - dialog.width() // 2 + self.rail.width() // 2, 120)
        field.setFocus()
        if self.prefs.get("touch_keyboard", True):
            self.keyboard.show_for(field)
        return dialog

    def _keyboard_visible(self, visible):
        # Push the whole screen up so the keyboard never covers the composer or the field
        self.layout().setContentsMargins(0, 0, 0, self.keyboard.height() if visible else 0)

    def _keyboard_send(self):
        self.composer._submit()
        self.keyboard.hide_keyboard()
