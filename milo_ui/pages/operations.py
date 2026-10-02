"""
Operations: pick a machining operation (facing, holes, pocket, slot, contour, thread, text), set its
parameters, and generate and load the program. The toolpath stage sits alongside to review it.
"""

import os

from PyQt5 import QtCore, QtWidgets
from PyQt5.QtCore import Qt

import operations as ops
from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.shell import Page

PREFS_KEY = "operations"  # last values per operation, so a tweak-and-regenerate keeps them


class OperationRow(QtWidgets.QPushButton):
    """One operation in the list: icon, name and a line on what it does"""

    def __init__(self, op: ops.Operation, parent=None):
        super().__init__(parent)
        self.op = op
        self.setCheckable(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(68)
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid transparent; border-radius: 14px; "
            f"text-align: left; }} QPushButton:checked {{ background: {C.accent_soft}; border-color: {C.accent}; }}"
            f"QPushButton:pressed {{ background: {C.card_hi}; }}")
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(14)
        badge = QtWidgets.QLabel()
        badge.setFixedSize(44, 44)
        badge.setAlignment(Qt.AlignCenter)
        badge.setPixmap(theme.pixmap(op.icon, C.accent_hi, 24))
        badge.setStyleSheet(f"background: {C.card_hi}; border-radius: 12px;")
        row.addWidget(badge)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(1)
        words.addWidget(kit.label(op.title, "value", size=T.body, weight=theme.MEDIUM))
        words.addWidget(kit.label(op.summary, "muted"))
        row.addLayout(words, 1)
        for child in self.findChildren(QtWidgets.QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)


class ValueRow(QtWidgets.QPushButton):
    """A parameter: label left, value right; tap to change it"""

    def __init__(self, title, on_tap, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(56)
        self.setStyleSheet(f"QPushButton {{ background: transparent; border: none; border-bottom: 1px solid {C.line};"
                           f"border-radius: 0; padding: 0 4px; }} QPushButton:pressed {{ background: {C.card_hi}; }}")
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(4, 0, 4, 0)
        name = kit.label(title, "body")
        self.value = kit.label("", "value", mono=True, size=T.body, weight=theme.MEDIUM)
        self.value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        chevron = QtWidgets.QLabel()
        chevron.setPixmap(theme.pixmap("caret-right", C.text_3, 18))
        for w in (name, self.value, chevron):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        row.addWidget(name, 1)
        row.addWidget(self.value, 1)
        row.addWidget(chevron)
        self.clicked.connect(on_tap)

    def set_text(self, text):
        self.value.setText(text)


class OperationsPage(Page):
    key = "operations"
    title = "Operations"
    icon = "stack"
    wants_stage = True

    def __init__(self, shell, parent=None):
        super().__init__(parent)
        self.shell = shell
        self.machine = shell.machine
        self.tools = []
        self.current = None
        self.values = {}
        self.generating = False
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)

        catalogue = kit.Card(title="Operations")
        catalogue.setFixedWidth(330)
        self.group = QtWidgets.QButtonGroup(self)
        self.group.setExclusive(True)
        self.rows = {}
        for op in ops.OPERATIONS:
            op_row = OperationRow(op)
            op_row.clicked.connect(lambda _=False, k=op.key: self.select(k))
            self.group.addButton(op_row)
            self.rows[op.key] = op_row
            catalogue.add(op_row)
        catalogue.body.addStretch(1)
        row.addWidget(catalogue)

        self.reset_button = kit.Button("Defaults", icon="arrow-counter-clockwise", variant="ghost", size="sm",
                                       on_click=self._reset)
        self.form_card = kit.Card(title="Parameters", trailing=self.reset_button)
        self.summary = kit.label("", "muted", wrap=True)
        self.form_card.add(self.summary)
        self.form = QtWidgets.QVBoxLayout()
        self.form.setSpacing(4)
        form_holder = QtWidgets.QWidget()
        form_holder.setLayout(self.form)
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(form_holder)
        QtWidgets.QScroller.grabGesture(scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        self.form_card.add(scroll, 1)
        self.status = kit.label("", "muted", wrap=True)
        self.form_card.add(self.status)
        self.generate_button = kit.Button("Generate and load", icon="sparkle", variant="primary", size="lg",
                                          on_click=self.generate)
        self.form_card.add(self.generate_button)
        row.addWidget(self.form_card, 1)

        self.select(ops.OPERATIONS[0].key)

    # --- units and tools ---------------------------------------------------------------------

    @property
    def ir_units(self):
        return "mm" if self.machine.machine_metric else "inch"

    @property
    def units(self):
        return self.machine.machine_units

    def _load_tools(self):
        from tool_table import read_tool_table
        try:
            tools, _ = read_tool_table(self.machine.tool_table_path, self.ir_units)
        except Exception:
            tools = []
        self.tools = [t for t in tools if t["usable"] and t["diameter"]]

    def _tool(self, number):
        return next((t for t in self.tools if t["tool"] == number), None)

    def on_show(self):
        self._load_tools()
        if self.current is not None:
            tool = self.values.get("tool")
            if tool is None or self._tool(tool) is None:
                picked = ops.default_tool(self.tools, self.current.tool_hint, self.machine.tool)
                self.values["tool"] = picked["tool"] if picked else None
            self._build_form()

    # --- selecting and editing ---------------------------------------------------------------

    def select(self, key):
        op = ops.BY_KEY[key]
        self.current = op
        self.rows[key].setChecked(True)
        if not self.tools:
            self._load_tools()
        values = ops.default_values(op, self.ir_units, self.machine.spindle_default)
        picked = ops.default_tool(self.tools, op.tool_hint, self.machine.tool)
        values["tool"] = picked["tool"] if picked else None
        saved = (self.shell.prefs.get(PREFS_KEY) or {}).get(key) or {}
        values.update({k: v for k, v in saved.items() if k in values})
        if values.get("tool") is not None and self._tool(values["tool"]) is None:
            values["tool"] = picked["tool"] if picked else None
        self.values = values
        self.form_card.title_label.setText(op.title.upper())
        self.summary.setText(op.description or op.summary)
        self.status.setText("")
        self._build_form()

    def _reset(self):
        stored = dict(self.shell.prefs.get(PREFS_KEY) or {})
        stored.pop(self.current.key, None)
        self.shell.prefs.set(PREFS_KEY, stored)
        self.select(self.current.key)

    def _remember(self):
        stored = dict(self.shell.prefs.get(PREFS_KEY) or {})
        stored[self.current.key] = dict(self.values)
        self.shell.prefs.set(PREFS_KEY, stored)

    def _build_form(self):
        kit.clear_layout(self.form)
        self.value_rows = {}
        for param in self.current.visible(self.values):
            if param.kind == ops.CHOICE:
                line = QtWidgets.QHBoxLayout()
                line.setContentsMargins(4, 6, 4, 6)
                line.addWidget(kit.label(param.label, "body"), 1)
                segmented = kit.Segmented(list(param.choices))
                segmented.set_index(list(param.choices).index(self.values[param.key]))
                segmented.selected.connect(lambda i, p=param: self._set(p, p.choices[i], rebuild=True))
                line.addWidget(segmented)
                self.form.addLayout(line)
                continue
            value_row = ValueRow(param.label, lambda _=False, p=param: self._edit(p))
            self.value_rows[param.key] = value_row
            self.form.addWidget(value_row)
            self._show_value(param)
        self.form.addStretch(1)

    def _show_value(self, param):
        value = self.values.get(param.key)
        if param.kind == ops.TOOL:
            tool = self._tool(value) if value is not None else None
            text = ops.describe_tool(tool, self.units) if tool else "Choose a tool"
        elif param.kind == ops.TEXT:
            text = f"“{value}”"
        elif param.kind == ops.COUNT:
            text = f"{int(value)}"
        else:
            unit = param.unit(self.units)
            text = f"{value:g} {unit}".strip()
        self.value_rows[param.key].set_text(text)

    def _set(self, param, value, rebuild=False):
        self.values[param.key] = value
        self.status.setText("")
        self._remember()
        if rebuild:
            self._build_form()
        elif param.key in self.value_rows:
            self._show_value(param)

    def _edit(self, param):
        if param.kind == ops.TOOL:
            return self._choose_tool(param)
        if param.kind == ops.TEXT:
            return self.shell.ask_text(param.label, lambda text: self._set(param, text), initial=self.values[param.key],
                                       action="Set")

        def done(value):
            if param.kind == ops.COUNT:
                value = int(round(value))
            if param.minimum is not None and value < param.minimum:
                return self.shell.toaster.show(f"{param.label} must be at least {param.minimum:g}.", "warning")
            if param.maximum is not None and value > param.maximum:
                return self.shell.toaster.show(f"{param.label} can be at most {param.maximum:g}.", "warning")
            self._set(param, value)
        kit.NumPad(self, param.label, done, initial=self.values[param.key], units=param.unit(self.units),
                   hint=param.hint or None).show_centered()

    def _choose_tool(self, param):
        self._load_tools()
        if not self.tools:
            return self.shell.toaster.show("No tools with a diameter in the tool table. Add one on the Tools page.",
                                           "warning")
        actions = [("wrench", ops.describe_tool(t, self.units, limit=48), lambda t=t: self._set(param, t["tool"]))
                   for t in self.tools]
        kit.ActionSheet(self, "Tool", actions, subtitle="From the machine's tool table", width=560).show_centered()

    # --- generating ----------------------------------------------------------------------------

    def generate(self):
        m, engine = self.machine, self.shell.engine
        if engine is None:
            return self.shell.toaster.show("Milo isn't running, so programs can't be generated.", "warning")
        if m.is_running:
            return self.shell.toaster.show("Stop the running program first.", "warning")
        if self.generating:
            return
        try:
            ir = ops.build_program(self.current.key, self.values, self._tool(self.values.get("tool")),
                                   self.ir_units, m.spindle_max)
        except ops.OperationError as e:
            return self._show_status(str(e), C.red)
        title = self.current.title
        # Busy before starting: on_done may come back right away
        self.generating = True
        self.generate_button.setEnabled(False)
        self._show_status("Generating…", C.text_3)
        if not engine.generate_operation(ir, title, on_done=lambda path, error: self._done(title, path, error)):
            self.generating = False
            self.generate_button.setEnabled(True)
            self._show_status("Milo is busy with another request. Try again in a moment.", C.amber)

    def _done(self, title, path, error):
        self.generating = False
        self.generate_button.setEnabled(True)
        if error:
            self._show_status(error.split("\n")[0], C.red)
            return
        self._show_status(f"{title} loaded as {os.path.basename(path)}. Review the toolpath before running.", C.green)
        self.shell.toaster.show(f"{title} program loaded.", "success")

    def _show_status(self, text, color):
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)
