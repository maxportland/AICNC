"""
Program: find a program, read it, and set how it runs. The toolpath stage sits alongside.
"""

import os
import time

from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.shell import Page

PROGRAM_EXTENSIONS = (".ngc", ".nc", ".tap", ".gcode", ".py")
AI_DIR = os.path.expanduser("~/linuxcnc/nc_files/ai")


def human_size(size):
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.0f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GB"


def human_time(stamp):
    delta = time.time() - stamp
    if delta < 3600:
        return f"{max(1, int(delta // 60))} min ago"
    if delta < 86400:
        return f"{int(delta // 3600)} h ago"
    return time.strftime("%b %d", time.localtime(stamp))


class FileRow(QtWidgets.QWidget):
    def __init__(self, icon, name, detail, loaded=False, parent=None):
        super().__init__(parent)
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(14, 8, 14, 8)
        row.setSpacing(14)
        badge = QtWidgets.QLabel()
        badge.setFixedSize(44, 44)
        badge.setAlignment(Qt.AlignCenter)
        color = C.accent_hi if loaded else (C.amber if icon == "folder" else C.text_2)
        badge.setPixmap(theme.pixmap(icon, color, 24))
        badge.setStyleSheet(f"background: {C.accent_soft if loaded else C.card_hi}; border-radius: 12px;")
        row.addWidget(badge)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(1)
        words.addWidget(kit.label(name, "value", size=T.body, weight=theme.MEDIUM))
        words.addWidget(kit.label(detail, "muted"))
        row.addLayout(words, 1)
        if loaded:
            row.addWidget(kit.label("LOADED", "eyebrow", color=C.accent_hi))
        for child in self.findChildren(QtWidgets.QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)


class FileBrowser(kit.Card):
    """Touch file browser: big rows, quick locations, breadcrumb, tap to load"""

    def __init__(self, machine, parent=None):
        self.up_button = kit.RoundButton("arrow-up", 48, variant="ghost", icon_size=20, tip="Up one folder")
        super().__init__(title="Programs", trailing=self.up_button, parent=parent)
        self.machine = machine
        self.root = machine.program_prefix if os.path.isdir(machine.program_prefix) else os.path.expanduser("~")
        self.path = self.root
        self.up_button.clicked.connect(self._up)

        places = QtWidgets.QHBoxLayout()
        places.setSpacing(8)
        self.places = [("Programs", "folder-open", self.root), ("Made by Milo", "sparkle", AI_DIR),
                       ("USB", "hard-drives", "/media")]
        for text, icon, path in self.places:
            chip = kit.Chip(text, icon=icon)
            chip.clicked.connect(lambda _=False, p=path: self.open_dir(p))
            places.addWidget(chip)
        places.addStretch(1)
        self.add(places)
        self.crumb = kit.label("", "muted")
        self.add(self.crumb)

        self.list = QtWidgets.QListWidget()
        self.list.setObjectName("file_list")
        self.list.setStyleSheet(f"QListWidget {{ background: transparent; border: none; }}"
                                f"QListWidget::item {{ border-radius: 14px; margin: 2px 0; }}"
                                f"QListWidget::item:selected {{ background: {C.card_hi}; }}")
        self.list.setVerticalScrollMode(QtWidgets.QAbstractItemView.ScrollPerPixel)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        QtWidgets.QScroller.grabGesture(self.list.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        self.list.itemClicked.connect(self._clicked)
        self.add(self.list, 1)
        machine.changed.connect(lambda topic: topic == "program" and self._mark_loaded())
        self.open_dir(self.root)

    def open_dir(self, path):
        if not os.path.isdir(path):
            os.makedirs(path, exist_ok=True) if path == AI_DIR else None
            if not os.path.isdir(path):
                self.crumb.setText(f"{path} isn't available")
                self.list.clear()
                return
        self.path = path
        self.refresh()

    def _up(self):
        parent = os.path.dirname(self.path.rstrip("/"))
        if parent and parent != self.path:
            self.open_dir(parent)

    def refresh(self):
        self.list.clear()
        home = os.path.expanduser("~")
        self.crumb.setText(self.path.replace(home, "~", 1))
        try:
            entries = sorted(os.scandir(self.path), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError as e:
            self.crumb.setText(f"Can't open {self.path}: {e.strerror}")
            return
        dirs = [e for e in entries if e.is_dir() and not e.name.startswith((".", "__"))]
        files = [e for e in entries if e.is_file() and e.name.lower().endswith(PROGRAM_EXTENSIONS)]
        files.sort(key=lambda e: e.stat().st_mtime, reverse=True)
        for entry in dirs:
            self._add(entry.path, "folder", entry.name, "Folder")
        for entry in files:
            info = entry.stat()
            loaded = os.path.abspath(entry.path) == os.path.abspath(self.machine.file or "")
            self._add(entry.path, "file-text", entry.name,
                      f"{human_size(info.st_size)} · {human_time(info.st_mtime)}", loaded)
        if not dirs and not files:
            item = QtWidgets.QListWidgetItem("No programs here")
            item.setFlags(Qt.NoItemFlags)
            self.list.addItem(item)
        self.up_button.setEnabled(self.path.rstrip("/") != "/")

    def _add(self, path, icon, name, detail, loaded=False):
        item = QtWidgets.QListWidgetItem()
        item.setData(Qt.UserRole, path)
        item.setSizeHint(QtCore.QSize(10, 72))
        self.list.addItem(item)
        self.list.setItemWidget(item, FileRow(icon, name, detail, loaded))

    def _clicked(self, item):
        path = item.data(Qt.UserRole)
        if not path:
            return
        if os.path.isdir(path):
            return self.open_dir(path)
        if self.machine.is_running:
            return
        self.machine.open_program(path)

    def _mark_loaded(self):
        QtCore.QTimer.singleShot(50, self.refresh)


class SimCodeView(QtWidgets.QPlainTextEdit):
    """G-code view for previews (the real screen uses qtvcp's GcodeDisplay)"""

    line_selected = QtCore.pyqtSignal(int)

    def __init__(self, machine, parent=None):
        super().__init__(parent)
        self.machine = machine
        self.setReadOnly(True)
        self.zoomTo(0)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        machine.changed.connect(lambda t: t == "program" and self._load())
        self._file = None
        self.cursorPositionChanged.connect(lambda: self.line_selected.emit(self.textCursor().blockNumber() + 1))

    def zoomTo(self, level):
        # The app stylesheet sets text edits' font size, so size this one the same way
        self.setStyleSheet(f"font-family: '{theme.MONO_FONT}'; font-size: {19 + 2 * level}px;")

    def _load(self):
        if self.machine.file != self._file:
            self._file = self.machine.file
            try:
                with open(self._file, errors="ignore") as f:
                    self.setPlainText(f.read())
            except (OSError, TypeError):
                self.setPlainText("")


class ProgramPage(Page):
    key = "program"
    title = "Program"
    icon = "file-text"
    wants_stage = True

    def __init__(self, shell, code_view=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        m = self.machine = shell.machine
        self._selected_line = 0
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)
        self.files = FileBrowser(m)
        self._wizards = {}
        if type(m).__name__ == "QtvcpMachine":
            wizards = QtWidgets.QHBoxLayout()
            wizards.setSpacing(8)
            wizards.addWidget(kit.label("Create", "muted"))
            wizards.addWidget(kit.Chip("Facing…", icon="square", on_click=lambda: self._wizard("facing")))
            wizards.addWidget(kit.Chip("Hole circle…", icon="dots-nine", on_click=lambda: self._wizard("holes")))
            wizards.addStretch(1)
            self.files.add(wizards)
        row.addWidget(self.files, 1)

        zoom = QtWidgets.QHBoxLayout()
        zoom.setSpacing(8)
        zoom.addWidget(kit.RoundButton("magnifying-glass-minus", 48, variant="ghost", icon_size=20,
                                       tip="Smaller text", on_click=lambda: self._zoom(-1)))
        zoom.addWidget(kit.RoundButton("magnifying-glass-plus", 48, variant="ghost", icon_size=20,
                                       tip="Larger text", on_click=lambda: self._zoom(1)))
        code = kit.Card(title="G-code", trailing=zoom)
        self.code_view = code_view if code_view is not None else SimCodeView(m)
        self._zoom_level = 0
        self._zoom(int(shell.prefs.get("gcode_zoom", 0)), save=False)
        self.code_view.setMinimumHeight(200)
        if isinstance(self.code_view, SimCodeView):
            self.code_view.line_selected.connect(self._line_selected)
        code.add(self.code_view, 1)
        self.run_from = kit.ToggleRow("Run from selected line", "Tap a line above, then press Start")
        self.run_from.toggled.connect(lambda on: self._update_hint())
        code.add(self.run_from)
        self.optional_stop = kit.ToggleRow("Stop at M1", "Pause at optional stops")
        self.optional_stop.toggled.connect(self._optional_stop)
        code.add(self.optional_stop)
        row.addWidget(code, 1)

    def _wizard(self, kind):
        """qtvcp's G-code generators (facing with step-downs, hole circle) in a popover"""
        widget = self._wizards.get(kind)
        if widget is None:
            try:
                if kind == "facing":
                    from facing_utility import Facing
                    widget = Facing()
                else:
                    from qtvcp.lib.gcode_utility.hole_circle import Hole_Circle
                    widget = Hole_Circle()
                if hasattr(widget, "init"):
                    widget.init()
            except Exception as e:
                return self.shell.toaster.show(f"That generator isn't available: {e}", "error")
            self._wizards[kind] = widget
        title = "Facing" if kind == "facing" else "Hole circle"
        pop = kit.Popover(self, title=title, width=1000)
        pop.add(kit.label("Or just ask Milo, e.g. \u201cface the stock 100 by 50, 1 mm deep\u201d.", "muted"))
        pop.add(widget)
        pop.closed.connect(lambda: widget.setParent(None))
        widget.show()
        pop.show_centered()

    def _zoom(self, steps, save=True):
        """Text size for the G-code view, remembered between runs"""
        level = max(-4, min(10, self._zoom_level + steps))
        self._zoom_level = level
        if hasattr(self.code_view, "zoomTo"):  # QScintilla's zoom, or SimCodeView's
            self.code_view.zoomTo(level)
        if save:
            self.shell.prefs.set("gcode_zoom", level)

    def _optional_stop(self, on):
        try:
            from qtvcp.core import Action
            Action().SET_OPTIONAL_STOP_ON() if on else Action().SET_OPTIONAL_STOP_OFF()
        except Exception:
            pass

    def _line_selected(self, line):
        self._selected_line = int(line)
        self._update_hint()

    def set_selected_line(self, line):
        self._line_selected(line)

    def _update_hint(self):
        if self.run_from.isChecked() and self._selected_line > 1:
            self.shell.cycle.start.setText(f"Start at {self._selected_line}")
        else:
            self.shell.cycle.start.setText("Start")

    def start_line(self):
        return self._selected_line if self.run_from.isChecked() and self._selected_line > 1 else 0

    def on_show(self):
        self.files.refresh()
