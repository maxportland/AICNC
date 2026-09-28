"""
Jog: hands-on control. Hold-to-jog pad with steps and speed, the position readout,
the spindle, and the everyday moves (home, zero, go-to, macros, MDI).
"""

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import kit
from milo_ui.theme import C
from milo_ui.shell import Page
from milo_ui.widgets import DRO, JogPad, JogSettings, SpindleCard


class MdiCard(kit.Card):
    """Type (or dictate) one G-code line; recent lines are one tap away"""

    def __init__(self, machine, parent=None):
        super().__init__(title="MDI", parent=parent)
        self.machine = machine
        self.history = []
        row = QtWidgets.QHBoxLayout()
        row.setSpacing(10)
        self.field = QtWidgets.QLineEdit()
        self.field.setPlaceholderText("G0 X10 Y10")
        self.field.setStyleSheet(f"font-family: 'JetBrains Mono';")
        self.field.returnPressed.connect(self._run)
        row.addWidget(self.field, 1)
        run = kit.Button("Run", icon="play-fill", variant="primary", on_click=self._run)
        row.addWidget(run)
        self.add(row)
        self.recent = QtWidgets.QWidget()
        self.recent_layout = kit.FlowLayout(self.recent, spacing=8)
        self.add(self.recent)

    def _run(self):
        command = self.field.text().strip().upper()
        if not command:
            return
        if self.machine.mdi(command):
            self.field.clear()
            if command in self.history:
                self.history.remove(command)
            self.history.insert(0, command)
            del self.history[6:]
            kit.clear_layout(self.recent_layout)
            for line in self.history:
                chip = kit.Chip(line)
                chip.setStyleSheet("font-family: 'JetBrains Mono';")
                chip.clicked.connect(lambda _=False, text=line: self.field.setText(text))
                self.recent_layout.addWidget(chip)


class JogPage(Page):
    key = "jog"
    title = "Jog"
    icon = "arrows-out-cardinal"
    wants_stage = False

    def __init__(self, shell, parent=None):
        super().__init__(parent)
        m = self.machine = shell.machine
        self.shell = shell
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)

        jog = kit.Card(title="Jog")
        pad = JogPad(m, size=132)
        jog.add(pad)
        jog.body.setAlignment(pad, Qt.AlignHCenter)
        jog.add(kit.hline())
        jog.add(JogSettings(m))
        col1 = kit.vbox(jog, "stretch", spacing=20)
        row.addLayout(col1, 10)

        col2 = kit.vbox(DRO(m, big=52), SpindleCard(m, compact=True), "stretch", spacing=20)
        row.addLayout(col2, 10)

        moves = kit.Card(title="Moves")
        grid = QtWidgets.QGridLayout()
        grid.setSpacing(10)
        self.home_button = kit.Button("Home all", icon="house-line", on_click=self._home)
        grid.addWidget(self.home_button, 0, 0)
        self.zero_button = kit.Button("Zero X Y Z", icon="crosshair", on_click=self._zero_all)
        grid.addWidget(self.zero_button, 0, 1)
        grid.addWidget(kit.Button("Go to work zero", icon="map-pin-line", on_click=m.go_to_work_zero), 1, 0)
        grid.addWidget(kit.Button("Raise Z", icon="arrow-line-up",
                                  on_click=lambda: m.mdi("G90 G53 G0 Z0")), 1, 1)
        moves.add(grid)
        self.move_buttons = moves
        col3 = QtWidgets.QVBoxLayout()
        col3.setSpacing(20)
        col3.addWidget(moves)
        if m.mdi_commands:
            macros = kit.Card(title="Macros")
            flow_holder = QtWidgets.QWidget()
            flow = kit.FlowLayout(flow_holder, spacing=10)
            for index, (label, _code) in enumerate(m.mdi_commands):
                chip = kit.Chip(label, icon="lightning")
                chip.clicked.connect(lambda _=False, i=index, c=chip: self._macro(i, c))
                flow.addWidget(chip)
            macros.add(flow_holder)
            col3.addWidget(macros)
        col3.addWidget(MdiCard(m))
        col3.addStretch(1)
        row.addLayout(col3, 10)

        m.changed.connect(lambda topic: topic in ("state", "homing") and self.refresh())
        self.refresh()

    def _home(self):
        if self.machine.all_homed:
            kit.ActionSheet(self, "Unhome all axes?", [
                ("house-line", "Home again", self.machine.home_all, "primary"),
                ("x", "Unhome all", self.machine.unhome_all, "danger"),
            ], subtitle="The machine is already homed.").show_at(self.home_button, "below")
        else:
            self.machine.home_all()

    def _macro(self, index, anchor):
        """INI macros can move the machine, so they're confirmed first"""
        label, code = self.machine.mdi_commands[index]
        kit.ActionSheet(self, f"Run \u201c{label}\u201d?", [
            ("play-fill", "Run", lambda: self.machine.run_macro(index), "warn"),
            ("x", "Cancel", lambda: None),
        ], subtitle=code.replace(";", "  \u00b7  "), width=560).show_at(anchor, "below")

    def _zero_all(self):
        m = self.machine
        if not m.on or m.is_running:
            return m.message.emit("warning", "Turn the machine on (and stop any program) to change offsets")

        def go():
            for axis in ("X", "Y", "Z"):
                if axis in m.axes:
                    m.set_axis_origin(axis, 0.0)
        kit.ActionSheet(self, f"Zero X, Y and Z in {m.wcs}?", [
            ("crosshair", "Zero all three", go, "warn"),
            ("x", "Cancel", lambda: None),
        ], subtitle="The current position becomes the work origin.").show_at(self.zero_button, "below")

    def refresh(self):
        m = self.machine
        self.home_button.setText("Homed" if m.all_homed else "Home all")
        self.home_button.set_variant(None if m.all_homed else "primary")
        self.home_button.setEnabled(m.on and not m.is_running)
        self.zero_button.setEnabled(m.on and not m.is_running)
