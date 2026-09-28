"""
Offsets: which work coordinate system is active, the offset table, and saved fixtures
(named G54 origins you can recall, stored in fixtures.json as before).
"""

import json
import os

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.shell import Page
from milo_ui.machine import WCS_NAMES

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE_FILE = os.path.join(CONFIG_DIR, "fixtures.json")


def load_fixtures(path=FIXTURE_FILE):
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_fixtures(data, path=FIXTURE_FILE):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


class SimOffsetTable(QtWidgets.QTableWidget):
    """Offsets for previews (the real screen uses qtvcp's OriginOffsetView)"""

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
        rows = WCS_NAMES[:6]
        self.setRowCount(len(rows))
        for r, name in enumerate(rows):
            values = self.machine.wcs_offsets.get(name, [0.0, 0.0, 0.0])
            for c, text in enumerate([name] + [f"{v:.3f}" for v in values]):
                item = QtWidgets.QTableWidgetItem(text)
                if name == self.machine.wcs:
                    item.setForeground(Qt.white)
                    item.setBackground(theme.alpha(C.accent, 0.18))
                self.setItem(r, c, item)


class FixtureRow(QtWidgets.QFrame):
    def __init__(self, name, data, on_apply, on_delete, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setStyleSheet(f"#card {{ background: {C.card_hi}; border-radius: 16px; }}")
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(16, 10, 10, 10)
        row.setSpacing(12)
        icon = QtWidgets.QLabel()
        icon.setPixmap(theme.pixmap("push-pin-fill", C.accent_hi, 22))
        row.addWidget(icon)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(2)
        words.addWidget(kit.label(name, "value", size=T.body, weight=theme.SEMIBOLD))
        coords = "   ".join(f"{a} {float(data.get(a.lower(), 0.0)):.3f}" for a in ("X", "Y", "Z"))
        words.addWidget(kit.label(coords, "muted", mono=True, size=14))
        row.addLayout(words, 1)
        row.addWidget(kit.Button("Use", icon="check", variant="primary", size="sm", on_click=on_apply))
        row.addWidget(kit.RoundButton("trash", 44, variant="ghost", icon_size=20, tip="Delete", on_click=on_delete))


class OffsetsPage(Page):
    key = "offsets"
    title = "Offsets"
    icon = "crosshair"
    wants_stage = False

    def __init__(self, shell, offset_table=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        m = self.machine = shell.machine
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)

        left = QtWidgets.QVBoxLayout()
        left.setSpacing(20)
        wcs = kit.Card(title="Active work system")
        self.wcs_buttons = kit.Segmented(WCS_NAMES[:6])
        self.wcs_buttons.selected.connect(lambda i: m.set_wcs(WCS_NAMES[i]))
        wcs.add(self.wcs_buttons)
        wcs.add(kit.label("Positions, zeroing and programs use the active system. Milo sees it too.", "muted",
                          wrap=True))
        left.addWidget(wcs)

        self.table = offset_table if offset_table is not None else SimOffsetTable(m)
        table_card = kit.Card(title="Offset table")
        table_card.add(self.table, 1)
        table_card.add(kit.label("Tap a value to edit it.", "muted"))
        left.addWidget(table_card, 1)
        row.addLayout(left, 3)

        self.fixtures_card = kit.Card(title="Saved fixtures", trailing=kit.Button(
            "Save G54…", icon="plus", size="sm", on_click=self._save))
        self.fixtures_card.add(kit.label("Recall a vise or jig position into G54 with one tap.", "muted", wrap=True))
        self.fixture_list = QtWidgets.QVBoxLayout()
        self.fixture_list.setSpacing(10)
        self.fixtures_card.add(self.fixture_list)
        self.fixtures_card.body.addStretch(1)
        row.addWidget(self.fixtures_card, 2)

        m.changed.connect(lambda t: t in ("offsets", "state") and self.refresh())
        self.refresh()
        self.refresh_fixtures()

    def refresh(self):
        m = self.machine
        if m.wcs in WCS_NAMES[:6]:
            self.wcs_buttons.set_index(WCS_NAMES.index(m.wcs))
        self.wcs_buttons.setEnabled(m.on and not m.is_running)

    def on_show(self):
        self.refresh_fixtures()

    def refresh_fixtures(self):
        kit.clear_layout(self.fixture_list)
        fixtures = load_fixtures()
        if not fixtures:
            self.fixture_list.addWidget(kit.label("No fixtures saved yet.", "muted"))
        for name, data in sorted(fixtures.items()):
            self.fixture_list.addWidget(FixtureRow(
                name, data, lambda n=name: self._apply(n), lambda n=name: self._delete(n)))

    def _current_g54(self):
        stat = self.machine.stat()
        if stat is not None and hasattr(stat, "g5x_offset") and getattr(stat, "g5x_index", 1) == 1:
            return list(stat.g5x_offset[:3])
        return list(self.machine.wcs_offsets.get("G54", [0.0, 0.0, 0.0]))

    def _save(self):
        if self.machine.wcs != "G54":
            return self.shell.toaster.show("Switch to G54 to save it as a fixture.", "warning")

        def save(name):
            fixtures = load_fixtures()
            x, y, z = self._current_g54()
            fixtures[name] = {"x": x, "y": y, "z": z}
            save_fixtures(fixtures)
            self.refresh_fixtures()
            self.shell.toaster.show(f"Saved fixture “{name}”.", "success")
        self.shell.ask_text("Save G54 as a fixture", save, placeholder="Fixture name, e.g. Left vise",
                            hint="An existing fixture with the same name is replaced.")

    def _apply(self, name):
        data = load_fixtures().get(name)
        if not data:
            return
        x, y, z = (float(data.get(a, 0.0)) for a in ("x", "y", "z"))

        def go():
            # Fixtures are stored in machine units; G10 reads the current program units
            m = self.machine
            units, restore = ("G21", "G20") if m.machine_metric else ("G20", "G21")
            lines = [units, f"G10 L2 P1 X{x:.6f} Y{y:.6f} Z{z:.6f}"]
            if m.metric != m.machine_metric:
                lines.append(restore)
            if m.mdi_lines(lines):
                self.shell.toaster.show(f"Loaded “{name}” into G54.", "success")
        kit.ActionSheet(self, f"Use “{name}”?", [("check", "Load into G54", go, "primary"),
                                                         ("x", "Cancel", lambda: None)],
                        subtitle=f"G54 becomes X {x:.3f}  Y {y:.3f}  Z {z:.3f}").show_centered()

    def _delete(self, name):
        def go():
            fixtures = load_fixtures()
            fixtures.pop(name, None)
            save_fixtures(fixtures)
            self.refresh_fixtures()
        kit.ActionSheet(self, f"Delete “{name}”?", [("trash", "Delete", go, "danger"),
                                                            ("x", "Keep it", lambda: None)]).show_centered()
