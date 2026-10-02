"""
Tools: the tool in the spindle front and center, tool changes and measuring, the drawbar,
and the tool table.
"""

import os

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit, probing
from milo_ui.theme import C, T
from milo_ui.shell import Page

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read_tool_table(path):
    """[(number, diameter, z, comment)] from a LinuxCNC tool table file"""
    tools = []
    try:
        with open(path) as f:
            for line in f:
                data, _, comment = line.partition(";")
                words = data.split()
                if not words or not words[0].upper().startswith("T"):
                    continue
                values = {w[0].upper(): w[1:] for w in words}
                try:
                    number = int(values["T"])
                except (KeyError, ValueError):
                    continue
                tools.append((number, float(values.get("D", 0) or 0), float(values.get("Z", 0) or 0),
                              comment.strip()))
    except OSError:
        pass
    return tools


class SimToolTable(QtWidgets.QTableWidget):
    """Read-only tool table for previews (the real screen uses qtvcp's ToolOffsetView)"""

    def __init__(self, path=None, parent=None):
        super().__init__(parent)
        self.path = path or os.path.join(CONFIG_DIR, "tool.tbl")
        self.setColumnCount(4)
        self.setHorizontalHeaderLabels(["Tool", "Diameter", "Length (Z)", "Description"])
        self.verticalHeader().hide()
        self.setShowGrid(False)
        self.setAlternatingRowColors(True)
        self.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectRows)
        self.horizontalHeader().setStretchLastSection(True)
        self.verticalHeader().setDefaultSectionSize(60)
        self.reload()

    def reload(self):
        tools = read_tool_table(self.path)
        self.setRowCount(len(tools))
        for row, (number, dia, z, comment) in enumerate(sorted(tools)):
            for col, text in enumerate((f"T{number}", f"{dia:g}", f"{z:.3f}", comment)):
                self.setItem(row, col, QtWidgets.QTableWidgetItem(text))
        for col, width in enumerate((110, 160, 180)):
            self.setColumnWidth(col, width)


class CurrentTool(kit.Card):
    def __init__(self, shell, parent=None):
        super().__init__(title="In the spindle", parent=parent, spacing=16)
        self.shell = shell
        m = self.machine = shell.machine
        hero = QtWidgets.QHBoxLayout()
        hero.setSpacing(20)
        self.number = kit.label("", size=72, weight=theme.SEMIBOLD, mono=True, color=C.text)
        hero.addWidget(self.number)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(4)
        self.name = kit.label("", "value", size=T.title, weight=theme.SEMIBOLD, wrap=True)
        self.facts = kit.label("", "muted")
        self.catalog = kit.label("", "muted", wrap=True)
        words.addStretch(1)
        words.addWidget(self.name)
        words.addWidget(self.facts)
        words.addWidget(self.catalog)
        words.addStretch(1)
        hero.addLayout(words, 1)
        self.add(hero)
        self.add(kit.hline())

        grid = QtWidgets.QGridLayout()
        grid.setSpacing(12)
        self.change = kit.Button("Change tool…", icon="arrows-left-right", variant="primary", size="lg",
                                 on_click=lambda: self._ask_tool("Change to tool", m.change_tool,
                                                                 "Runs T# M6 G43: you'll be asked to swap the tool."))
        self.set = kit.Button("Set tool in spindle…", icon="pencil-simple", size="lg",
                              on_click=lambda: self._ask_tool("Tool now in the spindle", m.set_tool,
                                                              "Runs M61 Q# G43: no tool change, just tells "
                                                              "LinuxCNC which tool is loaded."))
        self.measure = kit.Button("Measure length", icon="ruler", size="lg",
                                  on_click=lambda: probing.measure_tool(self, m, shell.prefs, shell.toaster.show))
        self.g43 = kit.Button("Apply length (G43)", icon="arrow-line-down", size="lg",
                              on_click=lambda: m.mdi(f"G43 H{m.tool}") if m.tool else None)
        grid.addWidget(self.change, 0, 0)
        grid.addWidget(self.set, 0, 1)
        grid.addWidget(self.measure, 1, 0)
        grid.addWidget(self.g43, 1, 1)
        self.add(grid)

        self.add(kit.hline())
        drawbar = QtWidgets.QHBoxLayout()
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(2)
        words.addWidget(kit.label("Power drawbar", "value", size=T.body, weight=theme.MEDIUM))
        self.drawbar_state = kit.label("", "muted")
        words.addWidget(self.drawbar_state)
        drawbar.addLayout(words, 1)
        self.drawbar = kit.Button("", icon="eject", size="lg", on_click=m.toggle_drawbar)
        self.drawbar.setMinimumWidth(220)
        drawbar.addWidget(self.drawbar)
        self.add(drawbar)
        m.changed.connect(lambda topic: topic in ("tool", "state", "drawbar", "homing", "spindle") and self.refresh())
        self.refresh()

    def _ask_tool(self, title, action, hint):
        kit.NumPad(self, title, lambda v: action(int(v)), initial=None, hint=hint).show_centered()

    def refresh(self):
        m = self.machine
        self.number.setText(f"T{m.tool}" if m.tool else "T–")
        self.name.setText(m.tool_comment or ("No tool loaded" if not m.tool else f"Tool {m.tool}"))
        facts = []
        if m.tool_diameter:
            facts.append(f"⌀ {m.tool_diameter:g} {m.units}")
        if m.tool_length:
            facts.append(f"length {m.tool_length:.3f}")
        self.facts.setText("  ·  ".join(facts))
        import fusion_tools as ft
        linked = ft.load_links(m.tool_links_path).get(m.tool) if m.tool else None
        if linked is not None:
            self.catalog.setText(f"<span style='color:{C.accent_hi}'>●</span> {linked.vendor} {linked.product_id}"
                                 f" · {linked.flutes} flutes · cuts {linked.flute_length:.3g} mm deep"
                                 f" · {len(linked.presets)} material preset{'s' if len(linked.presets) != 1 else ''}")
        self.catalog.setVisible(linked is not None)
        ok = m.ready
        for button in (self.change, self.set, self.g43):
            button.setEnabled(ok)
        self.measure.setEnabled(ok and m.tool != 0)
        self.drawbar.setText("Clamp tool" if m.drawbar else "Release tool")
        self.drawbar.set_variant("warn" if m.drawbar else None)
        self.drawbar_state.setText("Released: the tool is free" if m.drawbar else "Clamped")
        # Only with the spindle commanded off and actually stopped
        stopped = not m.spindle_dir and m.spindle_actual < 10
        self.drawbar.setEnabled(not m.is_running and stopped and not m.estop)


class ToolsPage(Page):
    key = "tools"
    title = "Tools"
    icon = "wrench"
    wants_stage = False

    def __init__(self, shell, tool_table=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)
        self.current = CurrentTool(shell)
        left = kit.vbox(self.current, "stretch", spacing=20)
        holder = QtWidgets.QWidget()
        holder.setLayout(left)
        holder.setFixedWidth(620)
        row.addWidget(holder)

        self.table = tool_table if tool_table is not None else SimToolTable(shell.machine.tool_table_path)
        actions = QtWidgets.QHBoxLayout()
        actions.setSpacing(8)
        if hasattr(self.table, "add_tool"):
            actions.addWidget(kit.Button("Add", icon="plus", size="sm", on_click=self.table.add_tool))
            actions.addWidget(kit.Button("Delete checked", icon="trash", size="sm", on_click=self.table.delete_tools))
        actions.addWidget(kit.Button("Reload", icon="arrows-clockwise", size="sm", on_click=self._reload))
        actions.addWidget(kit.Button("Tool library", icon="books", variant="primary", size="sm",
                                     on_click=self.open_library))
        card = kit.Card(title="Tool table", trailing=actions)
        # qtvcp's table locks itself unless the machine is on, idle and homed: say so
        self.lock = QtWidgets.QWidget()
        lock_row = QtWidgets.QHBoxLayout(self.lock)
        lock_row.setContentsMargins(0, 0, 0, 0)
        lock_row.setSpacing(10)
        padlock = QtWidgets.QLabel()
        padlock.setPixmap(theme.pixmap("lock", color=C.amber, size=26))
        lock_row.addWidget(padlock)
        self.lock_reason = kit.label("", "value", color=C.amber, weight=theme.MEDIUM)
        lock_row.addWidget(self.lock_reason, 1)
        card.add(self.lock)
        card.add(self.table, 1)
        self.hint = kit.label("Tap a cell to edit it. Milo reads this table when it plans programs.", "muted")
        card.add(self.hint)
        row.addWidget(card, 1)
        shell.machine.changed.connect(lambda topic: topic in ("state", "homing") and self._show_lock())
        self._show_lock()

    def _show_lock(self):
        reason = self.shell.machine.tool_table_lock
        self.lock_reason.setText(reason)
        self.lock.setVisible(bool(reason))
        self.hint.setVisible(not reason)

    def open_library(self):
        from milo_ui.pages.tool_library import ToolLibrary
        library = ToolLibrary(self, self.shell)
        library.changed.connect(self._reload)
        library.changed.connect(self.current.refresh)
        library.show_centered()
        return library

    def _reload(self):
        self.shell.machine.reload_tool_table()
        if hasattr(self.table, "reload"):
            self.table.reload()
