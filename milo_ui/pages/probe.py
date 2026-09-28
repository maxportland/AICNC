"""
Probe: the probing routines (qtvcp's BasicProbe), the tool setter and the touch plate,
with their parameters.
"""

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit, probing
from milo_ui.theme import C, T
from milo_ui.shell import Page


class ParamRow(QtWidgets.QPushButton):
    """A setting row: label left, value right; tap to edit on the number pad"""

    def __init__(self, title, get_value, set_value, units, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(60)
        self.setStyleSheet(f"QPushButton {{ background: transparent; border: none; border-bottom: 1px solid {C.line};"
                           f"border-radius: 0; padding: 0 4px; }} QPushButton:pressed {{ background: {C.card_hi}; }}")
        self.title, self.get_value, self.set_value, self.units = title, get_value, set_value, units
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(4, 0, 4, 0)
        name = kit.label(title, "body")
        self.value = kit.label("", "value", mono=True, size=T.body, weight=theme.MEDIUM)
        for w in (name, self.value):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(name, 1)
        row.addWidget(self.value)
        chevron = QtWidgets.QLabel()
        chevron.setPixmap(theme.pixmap("caret-right", C.text_3, 18))
        chevron.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(chevron)
        self.clicked.connect(self._edit)
        self.refresh()

    def refresh(self):
        self.value.setText(f"{self.get_value():g} {self.units}")

    def _edit(self):
        kit.NumPad(self, self.title, lambda v: (self.set_value(v), self.refresh()),
                   initial=self.get_value(), units=self.units).show_centered()


class ProbePage(Page):
    key = "probe"
    title = "Probe"
    icon = "target"
    wants_stage = False

    def __init__(self, shell, probe_widget=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        m = self.machine = shell.machine
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)

        self.probe_state = kit.label("", "muted")
        routines = kit.Card(title="Probing routines", trailing=self.probe_state)
        if probe_widget is not None:
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(probe_widget)
            QtWidgets.QScroller.grabGesture(scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
            routines.add(scroll, 1)
        else:
            empty = kit.label("Probing routines appear here when the screen runs on the machine "
                              "([PROBE] USE_PROBE in the INI).", "muted", wrap=True)
            routines.add(empty)
            routines.body.addStretch(1)
        row.addWidget(routines, 3)

        right = kit.Card(title="Tool setter & touch plate")
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addWidget(kit.Button("Measure tool", icon="ruler", variant="primary", size="lg",
                                     on_click=lambda: probing.measure_tool(self, m, shell.prefs, shell.toaster.show)))
        buttons.addWidget(kit.Button("Touch off Z", icon="arrow-line-down", size="lg",
                                     on_click=lambda: probing.touch_plate(self, m, shell.prefs, shell.toaster.show)))
        right.add(buttons)
        right.add(kit.eyebrow("Parameters"))
        for key, title, _default, kind in probing.PARAMETERS:
            units = f"{m.units}/min" if kind == "vel" else m.units
            right.add(ParamRow(title, lambda k=key: probing.get(shell.prefs, k),
                               lambda v, k=key: shell.prefs.set(f"probe.{k}", v), units))
        right.body.addStretch(1)
        right.setFixedWidth(620)
        row.addWidget(right)

        m.changed.connect(lambda t: t == "state" and self.refresh())
        self.refresh()

    def refresh(self):
        tripped = self.machine.probe_tripped
        self.probe_state.setText("● Probe triggered" if tripped else "Probe clear")
        self.probe_state.setStyleSheet(f"color: {C.amber if tripped else C.text_3};")
