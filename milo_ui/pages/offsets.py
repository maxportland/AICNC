"""
Offsets: the work coordinate systems (G54-G59.3) as things you use, not a table of numbers.

Each system has a name you give it ("Left vise"), shows whether it's active, in use or unused, where
its origin is, and where the tool is in it. Pick one (from the list or its pin on the map) to zero
it at the tool, set an axis, type an origin, make it active, go to it, probe it, copy it, save it
as a fixture, or clear it. The two offsets that shift every system, G92 and the tool length, are
shown with a warning when they're likely to surprise. Every change can be undone, and any earlier
state restored from the recent changes. qtvcp's full offset table is under Advanced.
"""

import json
import os
import time
from typing import List, Optional, Tuple

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt

import machine_safety
from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.shell import Page
from milo_ui.machine import WCS_NAMES
from milo_ui.offset_widgets import OffsetMap

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_FILE = os.path.join(CONFIG_DIR, "fixtures.json")
MAIN_SYSTEMS = WCS_NAMES[:6]  # G54-G59; G59.1-G59.3 behind "More"
NAMES_KEY = "wcs_names"       # prefs: {"G55": "Right vise"}


def load_fixtures(path=None):
    # The path is looked up when called (not bound as a default), so tests can point it elsewhere
    try:
        with open(path or FIXTURE_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_fixtures(data, path=None):
    with open(path or FIXTURE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def is_unused(offset: Optional[Tuple[List[float], float]]) -> bool:
    """A system nobody has set: its origin at machine zero and no rotation"""
    return offset is not None and all(abs(v) < 1e-9 for v in offset[0]) and abs(offset[1]) < 1e-9


def ago(stamp: float) -> str:
    minutes = int(max(0.0, time.time() - stamp) // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    if minutes < 24 * 60:
        return f"{minutes // 60} h ago"
    return time.strftime("%b %d", time.localtime(stamp))


class SimOffsetTable(QtWidgets.QTableWidget):
    """Offsets for previews (the real screen shows qtvcp's OriginOffsetView under Advanced)"""

    def __init__(self, machine, parent=None):
        super().__init__(parent)
        self.machine = machine
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["System", "X", "Y", "Z"])
        self.verticalHeader().hide()
        self.verticalHeader().setDefaultSectionSize(56)
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.Stretch)
        machine.changed.connect(lambda t: t == "offsets" and self.reload())
        self.reload()

    def reload(self):
        self.setRowCount(len(WCS_NAMES))
        for r, name in enumerate(WCS_NAMES):
            values = self.machine.wcs_offsets.get(name, [0.0, 0.0, 0.0])
            for c, text in enumerate([name] + [f"{v:.3f}" for v in values]):
                item = QtWidgets.QTableWidgetItem(text)
                if name == self.machine.wcs:
                    item.setForeground(Qt.white)
                    item.setBackground(theme.alpha(C.accent, 0.18))
                self.setItem(r, c, item)


class SystemRow(QtWidgets.QPushButton):
    """One work system in the list: code, its name, status and origin"""

    def __init__(self, name, parent=None):
        super().__init__(parent)
        self.name = name
        self.setCheckable(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(74)
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid transparent; border-radius: 14px; "
            f"text-align: left; }} QPushButton:checked {{ background: {C.accent_soft}; border-color: {C.accent}; }}"
            f"QPushButton:pressed {{ background: {C.card_hi}; }}")
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(14, 6, 14, 6)
        row.setSpacing(14)
        self.code = kit.label(name, "value", size=T.body_lg, weight=theme.SEMIBOLD, mono=True)
        self.code.setFixedWidth(84)
        row.addWidget(self.code)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(1)
        self.title = kit.label("", "value", size=T.body, weight=theme.MEDIUM)
        self.origin = kit.label("", "muted", mono=True, size=13)
        words.addWidget(self.title)
        words.addWidget(self.origin)
        row.addLayout(words, 1)
        self.status = kit.label("", "eyebrow")
        row.addWidget(self.status)
        for child in self.findChildren(QtWidgets.QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)

    def show_state(self, nickname, offset, active, units):
        unused = is_unused(offset)
        self.title.setText(nickname or ("Not used" if unused and not active else "No name"))
        self.title.setStyleSheet(f"color: {C.text if nickname else C.text_3};")
        self.origin.setVisible(offset is None or not unused)
        if offset is None:
            self.origin.setText("origin unknown")
        elif not unused:
            # X and Y, short: the panel shows all of it
            places = 2 if units == "mm" else 3
            self.origin.setText("  ".join(f"{a} {v:.{places}f}" for a, v in zip("XY", offset[0][:2]))
                                + (f"  ↻{offset[1]:.1f}°" if abs(offset[1]) > 1e-9 else ""))
        self.code.setStyleSheet(f"color: {C.text if active or not unused else C.text_3};")
        if active:
            self.status.setText("ACTIVE")
            self.status.setStyleSheet(f"color: {C.green};")
        else:
            self.status.setText("" if unused else "IN USE")
            self.status.setStyleSheet(f"color: {C.text_3};")


class AxisRow(QtWidgets.QPushButton):
    """An axis of the selected system: its origin and where the tool reads; tap for the choices"""

    def __init__(self, axis, on_tap, parent=None):
        super().__init__(parent)
        self.axis = axis
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(66)
        self.setStyleSheet(f"QPushButton {{ background: transparent; border: none; border-bottom: 1px solid {C.line};"
                           f"border-radius: 0; }} QPushButton:pressed {{ background: {C.card_hi}; }}")
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(6, 0, 6, 0)
        row.setSpacing(16)
        letter = kit.label(axis, "value", size=T.title, weight=theme.BOLD)
        letter.setFixedWidth(36)
        row.addWidget(letter)
        origin = QtWidgets.QVBoxLayout()
        origin.setSpacing(0)
        origin.addWidget(kit.label("ORIGIN (MACHINE)", "eyebrow"))
        self.origin = kit.label("", "value", mono=True, size=T.body_lg)
        origin.addWidget(self.origin)
        row.addLayout(origin, 1)
        tool = QtWidgets.QVBoxLayout()
        tool.setSpacing(0)
        tool.addWidget(kit.label("TOOL READS", "eyebrow"))
        self.tool = kit.label("", "value", mono=True, size=T.body_lg)
        tool.addWidget(self.tool)
        row.addLayout(tool, 1)
        chevron = QtWidgets.QLabel()
        chevron.setPixmap(theme.pixmap("caret-right", C.text_3, 20))
        row.addWidget(chevron)
        for child in self.findChildren(QtWidgets.QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.clicked.connect(lambda: on_tap(axis))


class NoticeRow(QtWidgets.QWidget):
    """A global offset (G92, tool length): a mark, what it is, and a fix"""

    def __init__(self, parent=None):
        super().__init__(parent)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 2, 0, 2)
        row.setSpacing(12)
        self.icon = QtWidgets.QLabel()
        self.icon.setFixedSize(24, 24)
        row.addWidget(self.icon)
        self.text = kit.label("", "body", wrap=True)
        row.addWidget(self.text, 1)
        self.button = kit.Button("", size="sm", variant="outline")
        self.button.hide()
        row.addWidget(self.button)
        self._action = None
        self.button.clicked.connect(lambda: self._action and self._action())

    def set(self, ok, text, fix=None):
        self.icon.setPixmap(theme.pixmap("check-circle" if ok else "warning", C.green if ok else C.amber, 22))
        self.text.setText(text)
        self.button.setVisible(fix is not None)
        if fix:
            self.button.setText(fix[0])
            self._action = fix[1]


class OffsetsPage(Page):
    key = "offsets"
    title = "Offsets"
    icon = "crosshair"
    wants_stage = False

    def __init__(self, shell, offset_table=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        m = self.machine = shell.machine
        self.units = "mm" if m.machine_metric else "inch"
        self.selected = m.wcs if m.wcs in WCS_NAMES else "G54"
        self.show_more = self.selected not in MAIN_SYSTEMS
        self.offsets = {}

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)
        row.addWidget(self._build_list())
        self.stack = QtWidgets.QStackedWidget()
        self.detail = self._build_detail()
        self.stack.addWidget(self.detail)
        self.advanced = self._build_advanced(offset_table if offset_table is not None else SimOffsetTable(m))
        self.stack.addWidget(self.advanced)
        row.addWidget(self.stack, 1)
        row.addLayout(self._build_side())

        m.changed.connect(lambda t: t in ("offsets", "state", "offset_history", "tool") and self.refresh())
        m.position_changed.connect(self._refresh_position)
        self.refresh()
        self.refresh_fixtures()

    @property
    def u(self):
        return "mm" if self.units == "mm" else "in"

    @property
    def places(self):
        return 3 if self.units == "mm" else 4

    # --- building --------------------------------------------------------------------------------

    def _build_list(self):
        card = kit.Card(title="Work systems")
        card.setFixedWidth(400)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        self.rows = {}
        for name in WCS_NAMES:
            system_row = SystemRow(name)
            system_row.clicked.connect(lambda _=False, n=name: self.select(n))
            self.group.addButton(system_row)
            self.rows[name] = system_row
            card.add(system_row)
        self.more_button = kit.Button("More systems (G59.1–G59.3)", variant="ghost", size="sm",
                                      on_click=self._toggle_more)
        card.add(self.more_button)
        card.body.addStretch(1)
        card.add(kit.label("Programs, the DRO, zeroing and Milo use the active system.", "muted", wrap=True))
        return card

    def _build_detail(self):
        card = kit.Card(title="G54")
        self.detail_card = card
        header = QtWidgets.QHBoxLayout()
        header.setSpacing(12)
        self.nickname = kit.label("", "value", size=T.title, weight=theme.SEMIBOLD)
        header.addWidget(self.nickname, 1)
        header.addWidget(kit.Button("Rename", icon="pencil-simple", variant="ghost", size="sm", on_click=self._rename))
        card.add(header)
        status = QtWidgets.QHBoxLayout()
        status.setSpacing(12)
        self.status_text = kit.label("", "body", wrap=True)
        status.addWidget(self.status_text, 1)
        self.activate_button = kit.Button("Make active", icon="check", variant="primary", on_click=self.make_active)
        status.addWidget(self.activate_button)
        card.add(status)

        self.axis_rows = {axis: AxisRow(axis, self._axis_menu) for axis in "XYZ"}
        for axis_row in self.axis_rows.values():
            card.add(axis_row)
        rotation = QtWidgets.QHBoxLayout()
        self.rotation_text = kit.label("", "body", wrap=True)
        rotation.addWidget(self.rotation_text, 1)
        self.clear_rotation_button = kit.Button("Clear rotation", variant="outline", size="sm",
                                                on_click=self._clear_rotation)
        rotation.addWidget(self.clear_rotation_button)
        self.rotation_row = QtWidgets.QWidget()
        self.rotation_row.setLayout(rotation)
        card.add(self.rotation_row)

        card.add(kit.eyebrow("Set it"))
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(10)
        self.action_buttons = {
            "zero_xy": kit.Button("Zero X Y at the tool", icon="crosshair", variant="outline",
                                  on_click=lambda: self.zero_here("XY")),
            "zero_z": kit.Button("Zero Z at the tool", icon="arrow-line-down", variant="outline",
                                 on_click=lambda: self.zero_here("Z")),
            "probe": kit.Button("Probe it…", icon="target", variant="outline", on_click=self._probe),
            "goto": kit.Button("Go to the origin", icon="navigation-arrow", variant="outline", on_click=self.go_to),
            "copy": kit.Button("Copy to…", icon="copy", variant="outline", on_click=self._copy_menu),
            "fixture": kit.Button("Save as fixture…", icon="push-pin", variant="outline", on_click=self._save_fixture),
            "clear": kit.Button("Clear", icon="eraser", variant="outline", on_click=self._clear),
            "advanced": kit.Button("Full table…", icon="table", variant="ghost",
                                   on_click=lambda: self.stack.setCurrentWidget(self.advanced)),
        }
        for i, button in enumerate(self.action_buttons.values()):
            grid.addWidget(button, i // 2, i % 2)
        card.add(grid)

        card.add(kit.eyebrow("These shift every system"))
        self.g92_row = NoticeRow()
        self.tool_row = NoticeRow()
        card.add(self.g92_row)
        card.add(self.tool_row)
        card.body.addStretch(1)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(card)
        QtWidgets.QScroller.grabGesture(scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        return scroll

    def _build_advanced(self, table):
        card = kit.Card(title="Full offset table", trailing=kit.Button(
            "Back", icon="arrow-left", variant="ghost", size="sm", on_click=lambda: self.stack.setCurrentWidget(self.detail)))
        card.add(kit.label("Every system and axis, G92, rotation and the tool offset, as LinuxCNC keeps them. Changes "
                           "made here aren't in Recent changes.", "muted", wrap=True))
        self.table = table
        card.add(table, 1)
        return card

    def _build_side(self):
        side = QtWidgets.QVBoxLayout()
        side.setSpacing(20)
        map_card = kit.Card(title="On the table")
        map_card.setFixedWidth(600)
        self.map = OffsetMap()
        self.map.picked.connect(self.select)
        map_card.add(self.map, 1)
        self.tool_text = kit.label("", "muted", mono=True)
        map_card.add(self.tool_text)
        side.addWidget(map_card, 3)

        lists = kit.Card()
        lists.setFixedWidth(600)
        self.list_toggle = kit.Segmented(["Fixtures", "Recent changes"])
        self.list_toggle.selected.connect(lambda i: self.lists.setCurrentIndex(i))
        lists.add(self.list_toggle)
        self.lists = QtWidgets.QStackedWidget()
        fixtures = QtWidgets.QWidget()
        fixture_column = QtWidgets.QVBoxLayout(fixtures)
        fixture_column.setContentsMargins(0, 0, 0, 0)
        fixture_column.setSpacing(8)
        self.fixture_hint = kit.label("", "muted", wrap=True)
        fixture_column.addWidget(self.fixture_hint)
        self.fixture_list = QtWidgets.QVBoxLayout()
        self.fixture_list.setSpacing(8)
        fixture_column.addLayout(self.fixture_list)
        fixture_column.addStretch(1)
        self.lists.addWidget(self._scrolled(fixtures))
        history = QtWidgets.QWidget()
        self.history_list = QtWidgets.QVBoxLayout(history)
        self.history_list.setContentsMargins(0, 0, 0, 0)
        self.history_list.setSpacing(8)
        self.lists.addWidget(self._scrolled(history))
        lists.add(self.lists, 1)
        side.addWidget(lists, 2)
        return side

    def _scrolled(self, widget):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        QtWidgets.QScroller.grabGesture(scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        return scroll

    # --- state -----------------------------------------------------------------------------------------

    def nickname_of(self, name) -> str:
        return (self.shell.prefs.get(NAMES_KEY) or {}).get(name, "")

    def label_of(self, name) -> str:
        nickname = self.nickname_of(name)
        return f"{name} · {nickname}" if nickname else name

    def tool_reads(self, name) -> Optional[List[float]]:
        """Where the tool is in a system (what the DRO would show with it active)"""
        offset = self.offsets.get(name)
        if offset is None:
            return None
        m = self.machine
        position = m.machine_position()
        tool = [0.0, 0.0, m.tool_offset_z if m.tool_length_applied else 0.0]
        return [p - o - g - t for p, o, g, t in zip(position, offset[0], m.g92, tool)]

    def select(self, name):
        if name not in WCS_NAMES:
            return
        self.selected = name
        if name not in MAIN_SYSTEMS:
            self.show_more = True
        self.stack.setCurrentWidget(self.detail)
        self.refresh()

    def _toggle_more(self):
        self.show_more = not self.show_more
        if not self.show_more and self.selected not in MAIN_SYSTEMS:
            self.selected = self.machine.wcs if self.machine.wcs in MAIN_SYSTEMS else "G54"
        self.refresh()

    def describe_for_milo(self) -> str:
        """The work systems that are set up or named, for Milo's machine context"""
        m = self.machine
        lines = []
        for name, offset in m.all_offsets().items():
            nickname = self.nickname_of(name)
            if offset is None or (is_unused(offset) and not nickname and name != m.wcs):
                continue
            words = [name] + ([f'"{nickname}"'] if nickname else []) + (["(active)"] if name == m.wcs else [])
            where = "origin at machine " + " ".join(f"{a}{v:.3f}" for a, v in zip("XYZ", offset[0]))
            if abs(offset[1]) > 1e-9:
                where += f", rotated {offset[1]:.3f} deg"
            lines.append(f"  {' '.join(words)}: {where}")
        if not lines:
            return ""
        return ("- Work systems the operator uses (to go over one's origin: G53 G0 Z0, then G53 G0 X<x> Y<y>):\n"
                + "\n".join(lines))

    # --- refreshing -------------------------------------------------------------------------------------

    def refresh(self):
        m, name = self.machine, self.selected
        self.offsets = m.all_offsets()
        for system, system_row in self.rows.items():
            system_row.setVisible(system in MAIN_SYSTEMS or self.show_more or system == m.wcs)
            system_row.show_state(self.nickname_of(system), self.offsets.get(system), system == m.wcs, self.units)
        self.rows[name].setChecked(True)
        self.more_button.setText("Fewer systems" if self.show_more else "More systems (G59.1–G59.3)")

        offset = self.offsets.get(name)
        active = name == m.wcs
        self.detail_card.title_label.setText(name)
        nickname = self.nickname_of(name)
        self.nickname.setText(nickname or "No name yet")
        self.nickname.setStyleSheet(f"color: {C.text if nickname else C.text_3};")
        if offset is None:
            self.status_text.setText("Its origin can't be read right now.")
        elif active:
            self.status_text.setText("Active: programs, the DRO, zeroing and Milo use this one.")
        elif is_unused(offset):
            self.status_text.setText("Not used yet: its origin is still at machine zero.")
        else:
            self.status_text.setText("Set up, not active.")
        busy = m.is_running or not m.on or m.interp != "idle"
        self.activate_button.setVisible(not active)
        self.activate_button.setEnabled(not busy)
        rotation = offset[1] if offset else 0.0
        self.rotation_row.setVisible(abs(rotation) > 1e-9)
        self.rotation_text.setText(f"Rotated {rotation:.3f}° around its origin (from probing a part's angle). "
                                   "Milo won't move X or Y while a rotation is active.")
        for button in self.action_buttons.values():
            button.setEnabled(not busy and offset is not None)
        self.action_buttons["advanced"].setEnabled(True)
        self.action_buttons["goto"].setEnabled(not busy and offset is not None and m.all_homed)
        self._refresh_globals()
        self._refresh_position()
        self.refresh_history()

    def _refresh_position(self):
        m, name = self.machine, self.selected
        offset = self.offsets.get(name)
        reads = self.tool_reads(name)
        for i, axis in enumerate("XYZ"):
            axis_row = self.axis_rows[axis]
            axis_row.origin.setText("—" if offset is None else f"{offset[0][i]:.{self.places}f}")
            axis_row.tool.setText("—" if reads is None else f"{reads[i]:.{self.places}f}")
        position = m.machine_position()
        self.tool_text.setText(f"Tool at X {position[0]:.{self.places}f}  Y {position[1]:.{self.places}f}  "
                               f"Z {position[2]:.{self.places}f} (machine)")
        pins = []
        for system, value in self.offsets.items():
            if value is None or (is_unused(value) and system != m.wcs and system != name):
                continue
            pins.append({"name": system, "label": self.label_of(system), "xy": (value[0][0] + m.g92[0],
                                                                                   value[0][1] + m.g92[1]),
                         "active": system == m.wcs, "rotation": value[1]})
        fixtures = [(n, (float(d.get("x", 0)), float(d.get("y", 0)))) for n, d in sorted(load_fixtures().items())]
        limits = {axis: m.limits.get(axis, (0.0, 100.0)) for axis in "XY"}
        self.map.set_scene(limits, pins, fixtures, (position[0], position[1]), name)

    def _refresh_globals(self):
        m, u = self.machine, self.u
        if any(abs(v) > 1e-9 for v in m.g92):
            shift = "  ".join(f"{a} {v:+.{self.places}f}" for a, v in zip("XYZ", m.g92) if abs(v) > 1e-9)
            self.g92_row.set(False, f"A G92 shift is active ({shift} {u}): every work system is moved by it. "
                                    f"Usually it's left over from an old program.", ("Clear G92", self._clear_g92))
        else:
            self.g92_row.set(True, "No G92 shift.")
        tool = m.tool
        if not tool:
            self.tool_row.set(True, "No tool in the spindle.")
        elif m.tool_length_applied:
            self.tool_row.set(True, f"T{tool}'s length is applied ({m.tool_offset_z:+.{self.places}f} {u}), so Z reads "
                                    f"at the tool's tip.")
        else:
            self.tool_row.set(False, f"T{tool}'s length isn't applied (no G43): Z work positions are off by the "
                                     f"tool's length.", ("Apply it (G43)", self._apply_tool_length))

    # --- actions ---------------------------------------------------------------------------------------

    def make_active(self):
        m = self.machine
        if m.is_running:
            return self.shell.toaster.show("Not while a program is running.", "warning")
        m.set_wcs(self.selected)
        self.shell.toaster.show(f"{self.label_of(self.selected)} is active.", "success")

    def zero_here(self, axes):
        m, name = self.machine, self.selected
        if m.set_origin_here(name, {a: 0.0 for a in axes}, label=f"Zero {' '.join(axes)} at the tool"):
            self.shell.toaster.show(f"{name} {' '.join(f'{a}0' for a in axes)} is where the tool is.", "success")
            self.refresh()

    def set_value(self, axis, value):
        m, name = self.machine, self.selected
        if m.set_origin_here(name, {axis: value}, label=f"Set {axis} to {value:g} at the tool"):
            self.shell.toaster.show(f"The tool now reads {axis} {value:g} in {name}.", "success")
            self.refresh()

    def set_origin(self, axis, value):
        m, name = self.machine, self.selected
        if m.apply_offsets(name, {axis: value}, label=f"Type {axis} origin {value:g}"):
            self.shell.toaster.show(f"{name}'s {axis} origin is at machine {value:g}.", "success")
            self.refresh()

    def _axis_menu(self, axis):
        m, name = self.machine, self.selected
        offset, reads = self.offsets.get(name), self.tool_reads(name)
        if offset is None or m.is_running or not m.on:
            return self.shell.toaster.show("Turn the machine on (and stop any program) to change offsets.", "warning")
        i = "XYZ".index(axis)

        def typed_value():
            kit.NumPad(self, f"Tool position in {name}, {axis}", lambda v: self.set_value(axis, v),
                       initial=round(reads[i], self.places), units=self.u,
                       hint=f"The tool's current {axis} becomes this value in {name}. Tip: type 45/2 to halve."
                       ).show_centered()

        def typed_origin():
            kit.NumPad(self, f"{name} {axis} origin (machine)", lambda v: self.set_origin(axis, v),
                       initial=round(offset[0][i], self.places), units=self.u,
                       hint=f"Where {name}'s {axis} zero is, in machine coordinates.").show_centered()
        kit.ActionSheet(self, f"{name} · {axis}", [
            ("crosshair", f"Zero {axis} at the tool", lambda: self.zero_here(axis), "primary"),
            ("pencil-simple", f"Set the tool's {axis} to a value…", typed_value),
            ("hash", f"Type the origin's machine {axis}…", typed_origin),
        ], subtitle=f"Origin {offset[0][i]:.{self.places}f} (machine) · tool reads {reads[i]:.{self.places}f}"
           ).show_centered()

    def _clear_rotation(self):
        if self.machine.apply_offsets(self.selected, {}, rotation=0.0, label="Clear rotation"):
            self.shell.toaster.show(f"{self.selected}'s rotation is cleared.", "success")

    def go_to(self):
        """Rapid over the selected system's origin: Z up first, then X/Y in machine coordinates"""
        m, name = self.machine, self.selected
        offset = self.offsets.get(name)
        if offset is None or not m.ready:
            return self.shell.toaster.show(m.state_detail or "The machine isn't ready.", "warning")
        x, y = offset[0][0] + m.g92[0], offset[0][1] + m.g92[1]
        (x0, x1), (y0, y1) = m.limits.get("X", (x, x)), m.limits.get("Y", (y, y))
        if not (x0 - 1e-6 <= x <= x1 + 1e-6 and y0 - 1e-6 <= y <= y1 + 1e-6):
            return self.shell.toaster.show(f"{name}'s origin is outside the machine's travel.", "warning")

        def go():
            if m.mdi_lines(["G90 G53 G0 Z0", f"G90 G53 G0 X{x:.4f} Y{y:.4f}"]):
                self.shell.toaster.show(f"Going over {self.label_of(name)}'s origin.", "info")
        kit.ActionSheet(self, f"Go to {name}'s origin?", [("navigation-arrow", "Go", go, "warn"),
                                                          ("x", "Cancel", lambda: None)],
                        subtitle=f"Z goes to the top first, then X {x:.3f} Y {y:.3f} (machine).").show_centered()

    def _probe(self):
        page = self.shell.pages.get("probe")
        if page is None:
            return
        page.job.wcs = self.selected if self.selected in ("G54", "G55", "G56", "G57", "G58", "G59") else "G54"
        page._save_job()
        self.shell.navigate("probe")
        self.shell.toaster.show(f"Probing will set {page.job.wcs}.", "info")

    def _copy_menu(self):
        name, offset = self.selected, self.offsets.get(self.selected)
        if offset is None:
            return
        targets = [n for n in WCS_NAMES if n != name and (n in MAIN_SYSTEMS or self.show_more)]
        actions = [("copy", f"{self.label_of(n)}" + ("" if is_unused(self.offsets.get(n)) else "  (replaces it)"),
                    lambda n=n: self.copy_to(n)) for n in targets]
        kit.ActionSheet(self, f"Copy {name} to…", actions, subtitle="The origin and rotation; undo works.",
                        width=520).show_centered()

    def copy_to(self, target):
        offset = self.offsets.get(self.selected)
        if offset is None:
            return False
        values = dict(zip("XYZ", offset[0]))
        ok = self.machine.apply_offsets(target, values, rotation=offset[1], label=f"Copy {self.selected}")
        if ok:
            self.shell.toaster.show(f"{self.selected} copied to {target}.", "success")
            self.refresh()
        return ok

    def _clear(self):
        name = self.selected

        def go():
            if self.machine.apply_offsets(name, {"X": 0.0, "Y": 0.0, "Z": 0.0}, rotation=0.0, label=f"Clear {name}"):
                self.shell.toaster.show(f"{name} cleared. Undo puts it back.", "success")
                self.refresh()
        kit.ActionSheet(self, f"Clear {name}?", [("eraser", "Clear it", go, "danger"), ("x", "Keep it", lambda: None)],
                        subtitle="Its origin goes back to machine zero. You can undo it.").show_centered()

    def _rename(self):
        name = self.selected

        def save(text):
            names = dict(self.shell.prefs.get(NAMES_KEY) or {})
            names[name] = text.strip()[:40]
            if not names[name]:
                names.pop(name)
            self.shell.prefs.set(NAMES_KEY, names)
            self.refresh()
        self.shell.ask_text(f"Name {name}", save, initial=self.nickname_of(name), placeholder="e.g. Left vise",
                            action="Save", hint="What's set up there, so it's easy to pick. Milo sees it too.")

    def _clear_g92(self):
        def go():
            if self.machine.clear_g92():
                self.shell.toaster.show("G92 shift cleared.", "success")
        kit.ActionSheet(self, "Clear the G92 shift?", [("eraser", "Clear G92", go, "warn"),
                                                       ("x", "Keep it", lambda: None)],
                        subtitle="Every work system moves back by it. This can't be undone here.").show_centered()

    def _apply_tool_length(self):
        if self.machine.apply_tool_length():
            self.shell.toaster.show(f"T{self.machine.tool}'s length is applied.", "success")

    # --- fixtures ----------------------------------------------------------------------------------------

    def on_show(self):
        self.refresh()
        self.refresh_fixtures()

    def refresh_fixtures(self):
        kit.clear_layout(self.fixture_list)
        fixtures = load_fixtures()
        self.fixture_hint.setText(f"Named origins for a vise or jig. Use one to load it into {self.selected}."
                                  if fixtures else "No fixtures yet. Pick a system and Save as fixture.")
        for name, data in sorted(fixtures.items()):
            self.fixture_list.addWidget(self._fixture_row(name, data))

    def _fixture_row(self, name, data):
        frame = QtWidgets.QFrame()
        frame.setObjectName("card")
        frame.setStyleSheet(f"#card {{ background: {C.card_hi}; border-radius: 14px; }}")
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(14, 8, 8, 8)
        row.setSpacing(10)
        icon = QtWidgets.QLabel()
        icon.setPixmap(theme.pixmap("push-pin-fill", C.accent_hi, 20))
        row.addWidget(icon)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(1)
        words.addWidget(kit.label(name, "value", size=T.body, weight=theme.SEMIBOLD))
        coords = "  ".join(f"{a} {float(data.get(a.lower(), 0.0)):.{self.places}f}" for a in "XYZ")
        if abs(float(data.get("rotation", 0.0))) > 1e-9:
            coords += f"  ↻{float(data['rotation']):.2f}°"
        words.addWidget(kit.label(coords, "muted", mono=True, size=13))
        row.addLayout(words, 1)
        row.addWidget(kit.Button("Use", icon="check", variant="primary", size="sm",
                                 on_click=lambda: self._use_fixture(name)))
        row.addWidget(kit.RoundButton("trash", 40, variant="ghost", icon_size=18, tip="Delete",
                                      on_click=lambda: self._delete_fixture(name)))
        return frame

    def _save_fixture(self):
        name = self.selected
        offset = self.offsets.get(name)
        if offset is None:
            return self.shell.toaster.show(f"Can't read {name} right now, so nothing was saved.", "error")

        def save(fixture):
            fixtures = load_fixtures()
            values = self.machine.current_offset(name) or offset
            fixtures[fixture] = {"x": values[0][0], "y": values[0][1], "z": values[0][2], "rotation": values[1],
                                 "from": name}
            save_fixtures(fixtures)
            self.refresh_fixtures()
            self.refresh()
            self.shell.toaster.show(f"Saved {name} as “{fixture}”.", "success")
        self.shell.ask_text(f"Save {name} as a fixture", save, initial=self.nickname_of(name),
                            placeholder="Fixture name, e.g. Left vise",
                            hint="An existing fixture with the same name is replaced.")

    def _use_fixture(self, fixture):
        data = load_fixtures().get(fixture)
        if not data:
            return
        name = self.selected
        x, y, z = (float(data.get(a, 0.0)) for a in ("x", "y", "z"))
        rotation = float(data.get("rotation", 0.0))

        def go():
            # Fixtures are stored in machine units, like apply_offsets takes them
            if self.machine.apply_offsets(name, {"X": x, "Y": y, "Z": z}, rotation=rotation,
                                          label=f"Load fixture {fixture}"):
                self.shell.toaster.show(f"Loaded “{fixture}” into {name}. Undo puts the old one back.", "success")
                self.refresh()
        kit.ActionSheet(self, f"Load “{fixture}” into {name}?", [("check", f"Load into {name}", go, "primary"),
                                                                ("x", "Cancel", lambda: None)],
                        subtitle=f"{name} becomes X {x:.3f}  Y {y:.3f}  Z {z:.3f}"
                                 + (f", rotated {rotation:.2f}°" if rotation else "")).show_centered()

    def _delete_fixture(self, fixture):
        def go():
            fixtures = load_fixtures()
            fixtures.pop(fixture, None)
            save_fixtures(fixtures)
            self.refresh_fixtures()
            self.refresh()
        kit.ActionSheet(self, f"Delete “{fixture}”?", [("trash", "Delete", go, "danger"),
                                                       ("x", "Keep it", lambda: None)]).show_centered()

    # --- recent changes --------------------------------------------------------------------------------

    def refresh_history(self):
        kit.clear_layout(self.history_list)
        history = self.machine.offset_history
        if not history:
            self.history_list.addWidget(kit.label("No changes yet. Zeroing, probing, fixtures and edits made here "
                                                  "are listed, and can be put back.", "muted", wrap=True))
        for index in range(len(history) - 1, -1, -1):
            self.history_list.addWidget(self._history_row(index, history[index], newest=index == len(history) - 1))
        self.history_list.addStretch(1)

    def _history_row(self, index, entry, newest):
        frame = QtWidgets.QFrame()
        row = QtWidgets.QHBoxLayout(frame)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(10)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(1)
        words.addWidget(kit.label(entry["label"], "body"))
        before = "  ".join(f"{a} {v:.{self.places}f}" for a, v in zip("XYZ", entry["offset"]))
        words.addWidget(kit.label(f"{entry['wcs']} · {ago(entry['time'])} · was {before}", "muted", size=13))
        row.addLayout(words, 1)
        if newest:
            row.addWidget(kit.Button("Undo", icon="arrow-counter-clockwise", variant="outline", size="sm",
                                     on_click=self.undo))
        else:
            row.addWidget(kit.Button("Restore", variant="ghost", size="sm", on_click=lambda: self.restore(index)))
        return frame

    def undo(self):
        entry = self.machine.undo_offsets()
        if entry:
            self.shell.toaster.show(f"Undid “{entry['label']}”: {entry['wcs']} is back as it was.", "success")
            self.refresh()

    def restore(self, index):
        """Put a system back the way it was before an older change (itself a change you can undo)"""
        entry = self.machine.offset_history[index]

        def go():
            values = dict(zip("XYZ", entry["offset"]))
            if self.machine.apply_offsets(entry["wcs"], values, rotation=entry.get("rotation"),
                                          label=f"Restore {entry['wcs']} from before “{entry['label']}”"):
                self.shell.toaster.show(f"{entry['wcs']} is back to before “{entry['label']}”.", "success")
                self.refresh()
        kit.ActionSheet(self, f"Restore {entry['wcs']}?", [("arrow-counter-clockwise", "Restore", go, "warn"),
                                                           ("x", "Cancel", lambda: None)],
                        subtitle=f"Back to how it was before “{entry['label']}” ({ago(entry['time'])}). "
                                 "Undo works on this too.").show_centered()
