"""
Tool Library: browse Fusion 360 tool libraries (vendor catalogs), see each tool's geometry and
its cutting presets rescaled to this spindle, and add a tool to the machine's tool table.
"""

import os
from typing import Dict, List, Optional, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

import fusion_tools as ft
from milo_ui import theme, kit
from milo_ui.theme import C, T

TOOL_ROLE = Qt.UserRole + 1

FILTERS = [
    ("All", None),
    ("Flat", ("flat end mill", "bull nose end mill", "slot mill", "face mill")),
    ("Ball", ("ball end mill", "lollipop mill")),
    ("Chamfer / V", ("chamfer mill", "counter sink", "tapered mill", "engrave")),
    ("Drill", ("drill", "spot drill", "center drill")),
    ("Other", "other"),
]
KNOWN = {t for _, types in FILTERS[1:-1] for t in types}

TYPE_ICONS = {"ball end mill": "circle-half", "chamfer mill": "triangle", "counter sink": "triangle",
              "tapered mill": "triangle", "drill": "arrow-down", "spot drill": "arrow-down",
              "face mill": "square-half", "form mill": "circle-dashed"}

_cache: Dict[str, Tuple[float, List[ft.FusionTool]]] = {}


def load_cached(path) -> List[ft.FusionTool]:
    """Libraries are a few MB of JSON; parse each file once per change"""
    mtime = os.path.getmtime(path)
    cached = _cache.get(path)
    if cached is None or cached[0] != mtime:
        _cache[path] = (mtime, ft.load_library(path))
    return _cache[path][1]


def fmt_len(mm, unit):
    if unit == "inches":
        return f"{mm / ft.IN:.4g}\""
    return f"{mm:.3g} mm"


class ToolDelegate(QtWidgets.QStyledItemDelegate):
    """Two-line tool rows, painted (hundreds of rows stay fast on the Pi)"""

    def sizeHint(self, option, index):
        return QtCore.QSize(10, 78)

    def paint(self, p, option, index):
        tool: ft.FusionTool = index.data(TOOL_ROLE)
        if tool is None:
            return super().paint(p, option, index)
        p.save()
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        r = QtCore.QRectF(option.rect).adjusted(2, 3, -8, -3)
        selected = bool(option.state & QtWidgets.QStyle.State_Selected)
        p.setPen(Qt.NoPen)
        p.setBrush(QtGui.QColor(C.accent_soft if selected else C.card_hi))
        p.drawRoundedRect(r, 14, 14)
        badge = QtCore.QRectF(r.left() + 14, r.center().y() - 22, 44, 44)
        p.setBrush(QtGui.QColor(C.bg))
        p.drawRoundedRect(badge, 12, 12)
        pm = theme.pixmap(TYPE_ICONS.get(tool.type, "circle"), C.accent_hi if selected else C.text_2, 22)
        p.drawPixmap(int(badge.center().x() - 11), int(badge.center().y() - 11), pm)
        text_left = badge.right() + 14
        width = r.right() - text_left - 14
        p.setPen(QtGui.QColor(C.text))
        p.setFont(theme.font(T.body, theme.MEDIUM))
        title = p.fontMetrics().elidedText(tool.description or tool.product_id, Qt.ElideRight, int(width))
        p.drawText(QtCore.QRectF(text_left, r.top() + 10, width, 26), Qt.AlignLeft | Qt.AlignVCenter, title)
        p.setPen(QtGui.QColor(C.text_3))
        p.setFont(theme.font(T.label))
        facts = [f"⌀ {tool.diameter_label}", f"{tool.flutes} fl", f"cut {fmt_len(tool.flute_length, tool.unit)}",
                 tool.type, f"{tool.vendor} {tool.product_id}".strip()]
        line = p.fontMetrics().elidedText("  ·  ".join(facts), Qt.ElideRight, int(width))
        p.drawText(QtCore.QRectF(text_left, r.top() + 40, width, 24), Qt.AlignLeft | Qt.AlignVCenter, line)
        p.restore()


class ToolDetail(QtWidgets.QFrame):
    add_clicked = QtCore.pyqtSignal(object)

    def __init__(self, machine, parent=None):
        super().__init__(parent)
        self.machine = machine
        self.setObjectName("card")
        self.setStyleSheet(f"#card {{ background: {C.card_hi}; border-radius: 18px; }}")
        self.layout_ = QtWidgets.QVBoxLayout(self)
        self.layout_.setContentsMargins(22, 20, 22, 20)
        self.layout_.setSpacing(12)
        self.tool = None
        self.show_tool(None)

    def show_tool(self, tool: Optional[ft.FusionTool]):
        self.tool = tool
        kit.clear_layout(self.layout_)
        if tool is None:
            self.layout_.addStretch(1)
            self.layout_.addWidget(kit.label("Pick a tool to see its geometry and cutting data.", "muted",
                                             wrap=True, align=Qt.AlignCenter))
            self.layout_.addStretch(1)
            return
        m = self.machine
        self.layout_.addWidget(kit.eyebrow(f"{tool.vendor}  {tool.product_id}".strip() or "Tool"))
        self.layout_.addWidget(kit.label(tool.description, "value", size=T.body_lg, weight=theme.SEMIBOLD, wrap=True))
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(18)
        grid.setVerticalSpacing(6)
        facts = [("Diameter", tool.diameter_label), ("Flutes", str(tool.flutes)),
                 ("Flute length", fmt_len(tool.flute_length, tool.unit)),
                 ("Overall length", fmt_len(tool.overall_length, tool.unit)),
                 ("Shank", fmt_len(tool.shank_diameter, tool.unit)), ("Type", tool.type)]
        if tool.corner_radius:
            facts.append(("Corner radius", fmt_len(tool.corner_radius, tool.unit)))
        if tool.point_angle:
            facts.append(("Point angle", f"{tool.point_angle:g}°"))
        if tool.taper_angle:
            facts.append(("Taper angle", f"{tool.taper_angle:g}°"))
        for i, (key, value) in enumerate(facts):
            grid.addWidget(kit.label(key, "muted"), (i // 2), (i % 2) * 2)
            grid.addWidget(kit.label(value, "body"), (i // 2), (i % 2) * 2 + 1)
        self.layout_.addLayout(grid)

        self.layout_.addWidget(kit.hline())
        if tool.presets:
            limited = any(ft.scale_preset(p, tool.flutes, m.spindle_min, m.spindle_max).limited for p in tool.presets)
            self.layout_.addWidget(kit.eyebrow("Cutting data on this machine"))
            if limited:
                self.layout_.addWidget(kit.label(
                    f"The vendor's speeds are outside this spindle's {m.spindle_min:g}–{m.spindle_max:g} rpm, so "
                    "feeds are recomputed from the vendor's chip load per tooth.", "muted", wrap=True))
            table = QtWidgets.QTableWidget(0, 4)
            table.setHorizontalHeaderLabels(["Material", "Vendor", "This machine", "Down / over"])
            table.verticalHeader().hide()
            table.setShowGrid(False)
            table.setEditTriggers(QtWidgets.QAbstractItemView.NoEditTriggers)
            table.setSelectionMode(QtWidgets.QAbstractItemView.NoSelection)
            table.verticalHeader().setDefaultSectionSize(48)
            table.horizontalHeader().setStretchLastSection(True)
            k, u = (1.0, "mm") if m.metric else (1 / ft.IN, "in")
            for preset in tool.presets:
                s = ft.scale_preset(preset, tool.flutes, m.spindle_min, m.spindle_max)
                row = table.rowCount()
                table.insertRow(row)
                steps = " / ".join(f"{v * k:.3g}" for v in (s.stepdown, s.stepover) if v) or "—"
                for col, text in enumerate((s.name, f"{s.vendor_rpm:,.0f} rpm · {s.vendor_feed * k:,.0f} {u}/min",
                                            f"{s.rpm:,.0f} rpm · {s.feed * k:,.0f} {u}/min", steps)):
                    item = QtWidgets.QTableWidgetItem(text)
                    if col == 2:
                        item.setForeground(QtGui.QColor(C.accent_hi))
                    table.setItem(row, col, item)
            if not any(p.stepdown or p.stepover for p in tool.presets):
                table.setColumnHidden(3, True)
            table.setStyleSheet(f"font-size: {T.label}px;")
            table.resizeColumnsToContents()
            table.setMinimumHeight(min(6, len(tool.presets)) * 48 + 48)
            QtWidgets.QScroller.grabGesture(table.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
            self.layout_.addWidget(table, 1)
        else:
            self.layout_.addWidget(kit.label("This tool has no cutting presets in the library.", "muted"))
            self.layout_.addStretch(1)
        add = kit.Button("Add to machine…", icon="plus", variant="primary", size="lg")
        add.clicked.connect(lambda: self.add_clicked.emit(tool))
        self.layout_.addWidget(add)


class ToolLibrary(kit.Popover):
    """Full-screen-ish browser over the installed Fusion libraries"""

    changed = QtCore.pyqtSignal()  # a tool was added to the machine

    def __init__(self, host, shell):
        super().__init__(host, title="Tool library", width=1640)
        self.shell = shell
        self.machine = shell.machine
        self.source = None
        self.kind = None
        self.tools: List[ft.FusionTool] = []

        self.add(kit.label("Vendor catalogs from Fusion 360 (.json or .tools). Add a tool to the machine and Milo "
                           "plans with its real geometry and cutting data.", "muted", wrap=True))
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(10)
        self.sources = QtWidgets.QHBoxLayout()
        self.sources.setSpacing(8)
        top.addLayout(self.sources)
        top.addStretch(1)
        top.addWidget(kit.Button("Import…", icon="download-simple", on_click=self._import))
        self.add(top)

        filters = QtWidgets.QHBoxLayout()
        filters.setSpacing(12)
        self.search = QtWidgets.QLineEdit()
        self.search.setPlaceholderText("Search: diameter, product number, description…")
        self.search.textChanged.connect(lambda _: self._filter())
        filters.addWidget(self.search, 1)
        self.types = kit.Segmented([name for name, _ in FILTERS])
        self.types.set_index(0)
        self.types.selected.connect(lambda i: (setattr(self, "kind", FILTERS[i][1]), self._filter()))
        filters.addWidget(self.types)
        self.add(filters)

        body = QtWidgets.QHBoxLayout()
        body.setSpacing(18)
        self.list = QtWidgets.QListWidget()
        self.list.setItemDelegate(ToolDelegate(self.list))
        self.list.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.list.setUniformItemSizes(True)
        QtWidgets.QScroller.grabGesture(self.list.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        self.list.currentItemChanged.connect(lambda item, _: self.detail.show_tool(item.data(TOOL_ROLE) if item else None))
        body.addWidget(self.list, 4)
        self.detail = ToolDetail(self.machine)
        self.detail.add_clicked.connect(self._add)
        body.addWidget(self.detail, 5)
        holder = QtWidgets.QWidget()
        holder.setLayout(body)
        holder.setFixedHeight(620)
        self.add(holder)
        self.count = kit.label("", "muted")
        self.add(self.count)
        self._load()

    # --- data ----------------------------------------------------------------------------------

    def _load(self):
        self.tools = []
        errors = []
        paths = ft.installed_libraries()
        for path in paths:
            try:
                self.tools.extend(load_cached(path))
            except (ValueError, OSError) as e:
                errors.append(str(e))
        kit.clear_layout(self.sources)
        if paths:
            names = [("All libraries", None)] + [(os.path.splitext(os.path.basename(p))[0], os.path.basename(p))
                                                 for p in paths]
            for text, source in names:
                chip = kit.Chip(text[:28], tone="accent" if source == self.source else None)
                chip.clicked.connect(lambda _=False, s=source: self._pick_source(s))
                self.sources.addWidget(chip)
        for error in errors:
            self.shell.toaster.show(error, "error")
        self._filter()

    def _pick_source(self, source):
        self.source = source
        self._load()

    def _filter(self):
        words = self.search.text().lower().replace("\"", " in").split()
        self.list.clear()
        shown = 0
        for tool in self.tools:
            if self.source and tool.source != self.source:
                continue
            if self.kind == "other" and tool.type in KNOWN:
                continue
            if isinstance(self.kind, tuple) and tool.type not in self.kind:
                continue
            haystack = " ".join((tool.description, tool.product_id, tool.vendor, tool.type,
                                 f"{tool.diameter:g}mm {tool.diameter:g} mm", f"{tool.diameter / ft.IN:.4g} in",
                                 f"{tool.diameter / ft.IN:.4g}in")).lower()
            if any(w not in haystack for w in words):
                continue
            item = QtWidgets.QListWidgetItem()
            item.setData(TOOL_ROLE, tool)
            self.list.addItem(item)
            shown += 1
            if shown >= 400:  # a list longer than this isn't browsable anyway
                break
        total = len(self.tools)
        if not total:
            self.count.setText("No libraries yet. Tap Import… to add Fusion 360 tool files from the Desktop, "
                               "Downloads or a USB stick.")
        else:
            self.count.setText(f"Showing {shown} of {total} tools" + (" (refine the search to see more)" if shown >= 400 else ""))
        if self.list.count():
            self.list.setCurrentRow(0)
        else:
            self.detail.show_tool(None)

    # --- actions ----------------------------------------------------------------------------------

    def _import(self):
        installed = {os.path.basename(p) for p in ft.installed_libraries()}
        candidates = [p for p in ft.find_library_files() if os.path.basename(p) not in installed
                      and not p.startswith(ft.LIBRARY_DIR)]
        sheet = kit.Popover(self.shell, title="Import tool library", width=820)
        sheet.add(kit.label("Fusion 360 tool libraries found on the Desktop, in Downloads and on USB sticks. "
                            f"Imported copies are kept in {ft.LIBRARY_DIR.replace(os.path.expanduser('~'), '~')}.",
                            "muted", wrap=True))
        if not candidates:
            sheet.add(kit.label("No new .json or .tools files found.", "body"))
        for path in candidates[:10]:
            row = QtWidgets.QHBoxLayout()
            words = kit.vbox(kit.label(os.path.basename(path), "value", size=T.body, weight=theme.MEDIUM),
                             kit.label(os.path.dirname(path).replace(os.path.expanduser("~"), "~"), "muted"), spacing=2)
            row.addLayout(words, 1)
            button = kit.Button("Import", icon="download-simple", variant="primary", size="sm")
            button.clicked.connect(lambda _=False, p=path, s=sheet: self._do_import(p, s))
            row.addWidget(button)
            sheet.add(row)
        sheet.show_centered()

    def _do_import(self, path, sheet):
        try:
            installed = ft.install_library(path)
            count = len(load_cached(installed))
        except (ValueError, OSError) as e:
            self.shell.toaster.show(str(e), "error")
            return
        sheet.close()
        self.shell.toaster.show(f"Imported {count} tools from {os.path.basename(path)}.", "success")
        self.source = os.path.basename(installed)
        self._load()

    def _add(self, tool: ft.FusionTool):
        m = self.machine
        if m.is_running:
            return self.shell.toaster.show("Stop the program before changing the tool table.", "warning")
        used = {}
        from milo_ui.pages.tools import read_tool_table
        for number, _dia, _z, comment in read_tool_table(m.tool_table_path):
            used[number] = comment
        suggestion = next(n for n in range(1, 1000) if n not in used)

        def chosen(value):
            number = int(value)
            if number <= 0:
                return self.shell.toaster.show("Tool numbers start at 1 (T0 means no tool).", "warning")
            if number in used:
                kit.ActionSheet(self.shell, f"T{number} is already “{used[number]}”", [
                    ("arrows-clockwise", f"Replace T{number}", lambda: self._write(number, tool), "warn"),
                    ("x", "Pick another number", lambda: self._add(tool)),
                ], subtitle="Replacing keeps its measured length. Measure again if it's a different tool.",
                    width=560).show_centered()
            else:
                self._write(number, tool)
        kit.NumPad(self.shell, "Tool number", chosen, initial=suggestion,
                   hint=f"The first free number is T{suggestion}.").show_centered()

    def _write(self, number, tool: ft.FusionTool):
        m = self.machine
        try:
            ft.write_tool_entry(m.tool_table_path, number, tool.diameter, tool.short_name(),
                                "mm" if m.machine_metric else "inch")
            ft.link_tool(number, tool, m.tool_links_path)
        except OSError as e:
            return self.shell.toaster.show(f"Couldn't write the tool table: {e}", "error")
        m.reload_tool_table()
        self.changed.emit()
        self.shell.toaster.show(f"T{number} added: {tool.short_name()}. Measure its length on the tool setter "
                                "before cutting with it.", "success", seconds=10)
