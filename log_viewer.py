"""
Log Viewer widget: live, filterable, searchable view of the machine's log files.

Sources and parsing live in log_sources.py; styling in milo_ui/theme.py (#log_viewer, #log_view).
"""

import datetime
import os
from typing import List, Optional, Tuple

from PyQt5 import QtCore, QtGui, QtWidgets

from log_sources import (DEBUG, ERROR, INFO, LEVELS, SOURCES, TIMESTAMP_RE, WARNING, TailReader,
                         classify_line, parse_lines)

try:
    import qtawesome as qta
except ImportError:
    qta = None

POLL_MS = 1000
MAX_LINES = 20000  # older lines are dropped from memory and the view
MAX_HIGHLIGHTS = 2000  # search matches highlighted at once
EXPORT_DIR = os.path.expanduser("~/linuxcnc/log_exports")

LEVEL_COLORS = {ERROR: "#ff6b6b", WARNING: "#e3b341", INFO: "#d0d4dc", DEBUG: "#6b737c"}
TIMESTAMP_COLOR = "#6b737c"
LEVEL_LABELS = {ERROR: "Errors", WARNING: "Warnings", INFO: "Info", DEBUG: "Debug"}


class LogHighlighter(QtGui.QSyntaxHighlighter):
    """Colors each line by level (continuation lines follow the line above) and dims timestamps"""

    def __init__(self, document):
        super().__init__(document)
        self.formats = {}
        for level, color in LEVEL_COLORS.items():
            fmt = QtGui.QTextCharFormat()
            fmt.setForeground(QtGui.QColor(color))
            if level == ERROR:
                fmt.setFontWeight(QtGui.QFont.Bold)
            self.formats[level] = fmt
        self.timestamp_format = QtGui.QTextCharFormat()
        self.timestamp_format.setForeground(QtGui.QColor(TIMESTAMP_COLOR))

    def highlightBlock(self, text):
        previous = self.previousBlockState()
        level = classify_line(text, LEVELS[previous] if 0 <= previous < len(LEVELS) else INFO)
        self.setCurrentBlockState(LEVELS.index(level))
        self.setFormat(0, len(text), self.formats[level])
        stamp = TIMESTAMP_RE.match(text)
        if stamp:
            self.setFormat(0, stamp.end(), self.timestamp_format)


class LogViewer(QtWidgets.QWidget):
    """Live log view with source tabs, level filters, search, follow, copy/save/clear"""

    def __init__(self, sources=SOURCES, parent=None, export_dir=EXPORT_DIR):
        super().__init__(parent)
        self.setObjectName("log_viewer")
        self.sources = {s.key: s for s in sources}
        self.export_dir = export_dir
        self.source = None
        self.reader: Optional[TailReader] = None
        self.entries: List[Tuple[str, str]] = []
        self.cleared_at = 0
        self.levels = set(LEVELS)
        self.matches: List[QtGui.QTextCursor] = []
        self.current_match = -1
        self.last_update: Optional[datetime.datetime] = None
        self._updating = False  # our own view changes, not the user scrolling

        self._build()
        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(POLL_MS)
        self.timer.timeout.connect(self.poll)
        self.select_source(sources[0].key)

    # --- construction -------------------------------------------------------

    def _icon(self, name, color="#c4cad1"):
        return qta.icon(name, color=color) if qta is not None else QtGui.QIcon()

    def _tool_button(self, text, icon=None, tip="", checkable=False, name="log_tool_button"):
        button = QtWidgets.QPushButton(text)
        button.setObjectName(name)
        button.setCursor(QtCore.Qt.PointingHandCursor)
        button.setCheckable(checkable)
        button.setToolTip(tip)
        if icon:
            button.setIcon(self._icon(icon))
            button.setIconSize(QtCore.QSize(13, 13))
        return button

    def _build(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 8)
        layout.setSpacing(8)

        # Row 1: sources on the left, search on the right
        top = QtWidgets.QHBoxLayout()
        top.setSpacing(6)
        self.source_group = QtWidgets.QButtonGroup(self)
        self.source_group.setExclusive(True)
        self.source_buttons = {}
        for key, source in self.sources.items():
            button = self._tool_button(source.title, tip=source.description, checkable=True, name="log_source_button")
            button.clicked.connect(lambda _, k=key: self.select_source(k))
            self.source_group.addButton(button)
            self.source_buttons[key] = button
            top.addWidget(button)
        top.addStretch(1)

        self.search = QtWidgets.QLineEdit()
        self.search.setObjectName("log_search")
        self.search.setPlaceholderText("Search…")
        self.search.setClearButtonEnabled(True)
        self.search.setMinimumWidth(220)
        if qta is not None:
            self.search.addAction(self._icon("fa5s.search", "#6b737c"), QtWidgets.QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self._on_search_changed)
        self.search.returnPressed.connect(self.next_match)
        top.addWidget(self.search)
        self.prev_button = self._tool_button("", "fa5s.chevron-up", "Previous match")
        self.next_button = self._tool_button("", "fa5s.chevron-down", "Next match")
        self.prev_button.clicked.connect(self.previous_match)
        self.next_button.clicked.connect(self.next_match)
        self.match_label = QtWidgets.QLabel("")
        self.match_label.setObjectName("log_match_label")
        self.match_label.setMinimumWidth(64)
        for widget in (self.prev_button, self.next_button, self.match_label):
            top.addWidget(widget)
        layout.addLayout(top)

        # Row 2: level chips and "matches only" on the left, view actions on the right
        bar = QtWidgets.QHBoxLayout()
        bar.setSpacing(6)
        self.level_buttons = {}
        for level in LEVELS:
            chip = self._tool_button(LEVEL_LABELS[level], checkable=True, name="log_level_chip")
            chip.setProperty("level", level)
            chip.setChecked(True)
            chip.toggled.connect(lambda on, lv=level: self._set_level(lv, on))
            self.level_buttons[level] = chip
            bar.addWidget(chip)
        self.matches_only = self._tool_button("Matches only", "fa5s.filter", "Show only lines that match the search",
                                              checkable=True)
        self.matches_only.toggled.connect(lambda _: self.rebuild())
        bar.addWidget(self.matches_only)
        bar.addStretch(1)
        self.follow_button = self._tool_button("Follow", "fa5s.angle-double-down", "Keep scrolled to the newest line",
                                               checkable=True)
        self.follow_button.setChecked(True)
        self.follow_button.toggled.connect(self._on_follow_toggled)
        self.wrap_button = self._tool_button("Wrap", "fa5s.level-down-alt", "Wrap long lines", checkable=True)
        self.wrap_button.toggled.connect(self._on_wrap_toggled)
        self.copy_button = self._tool_button("Copy", "fa5s.copy", "Copy the selection, or everything shown")
        self.copy_button.clicked.connect(self.copy)
        self.save_button = self._tool_button("Save", "fa5s.download", f"Save what's shown to {EXPORT_DIR}")
        self.save_button.clicked.connect(self.save)
        self.clear_button = self._tool_button("Clear", "fa5s.eraser", "Hide existing lines and show only new ones")
        self.clear_button.clicked.connect(self.clear)
        for widget in (self.follow_button, self.wrap_button, self.copy_button, self.save_button, self.clear_button):
            bar.addWidget(widget)
        layout.addLayout(bar)

        # The log itself
        self.view = QtWidgets.QPlainTextEdit()
        self.view.setObjectName("log_view")
        self.view.setReadOnly(True)
        self.view.setUndoRedoEnabled(False)
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.view.setMaximumBlockCount(MAX_LINES)
        font = QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)
        font.setPointSize(9)
        self.view.setFont(font)
        self.view.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.highlighter = LogHighlighter(self.view.document())
        # Scroll bars are styled when created, so re-polish after naming them
        for scrollbar in (self.view.verticalScrollBar(), self.view.horizontalScrollBar()):
            scrollbar.setObjectName("log_scrollbar")
            scrollbar.style().unpolish(scrollbar)
            scrollbar.style().polish(scrollbar)
        layout.addWidget(self.view, 1)

        # Footer: file, size, freshness, counts, messages
        footer = QtWidgets.QHBoxLayout()
        self.path_label = QtWidgets.QLabel("")
        self.path_label.setObjectName("log_footer")
        self.path_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self.status_label = QtWidgets.QLabel("")
        self.status_label.setObjectName("log_footer")
        self.message_label = QtWidgets.QLabel("")
        self.message_label.setObjectName("log_message")
        self.cleared_link = QtWidgets.QPushButton("Show all")
        self.cleared_link.setObjectName("log_link")
        self.cleared_link.setCursor(QtCore.Qt.PointingHandCursor)
        self.cleared_link.clicked.connect(self.show_all)
        self.cleared_link.setVisible(False)
        footer.addWidget(self.path_label, 1)
        footer.addWidget(self.message_label)
        footer.addWidget(self.cleared_link)
        footer.addWidget(self.status_label)
        layout.addLayout(footer)

        self.message_timer = QtCore.QTimer(self)
        self.message_timer.setSingleShot(True)
        self.message_timer.timeout.connect(lambda: self.message_label.setText(""))

    # --- sources and loading ------------------------------------------------

    def select_source(self, key: str):
        self.source = self.sources[key]
        self.source_buttons[key].setChecked(True)
        self.reader = TailReader(self.source.path())
        self.entries = []
        self.cleared_at = 0
        self.cleared_link.setVisible(False)
        self.follow_button.setChecked(True)
        self._read(initial=True)

    def poll(self):
        """Timer: pick up new lines (and a new file if the source moved, e.g. a LinuxCNC restart)"""
        path = self.source.path()
        if path != self.reader.path:
            self.reader = TailReader(path)
            self.entries, self.cleared_at = [], 0
            self._read(initial=True)
        else:
            self._read(initial=False)

    def _read(self, initial: bool):
        lines, reset = self.reader.read_new()
        if reset:
            self.entries, self.cleared_at = [], 0
        previous = self.entries[-1][0] if self.entries else INFO
        new = parse_lines(lines, previous)
        if new or initial or reset:
            self.last_update = datetime.datetime.now()
        self.entries.extend(new)
        overflow = len(self.entries) - MAX_LINES
        if overflow > 0:
            self.entries = self.entries[overflow:]
            self.cleared_at = max(0, self.cleared_at - overflow)
        if initial or reset:
            self.rebuild()
        elif new:
            self._append(new)
        self._update_footer()

    # --- filtering and display ----------------------------------------------

    def _visible(self, level: str, text: str) -> bool:
        if level not in self.levels:
            return False
        needle = self.search.text()
        if self.matches_only.isChecked() and needle and needle.lower() not in text.lower():
            return False
        return True

    def visible_lines(self) -> List[str]:
        return [text for level, text in self.entries[self.cleared_at:] if self._visible(level, text)]

    def rebuild(self):
        """Redraw the view from the parsed entries (filters or source changed)"""
        follow = self.follow_button.isChecked()
        self._updating = True
        try:
            self.view.setPlainText("\n".join(self.visible_lines()))
            self._refresh_counts()
            self._find_matches()
            if follow:
                self._scroll_to_end()
        finally:
            self._updating = False

    def _append(self, new: List[Tuple[str, str]]):
        shown = [text for level, text in new if self._visible(level, text)]
        if shown:
            scrollbar = self.view.verticalScrollBar()
            keep, follow = scrollbar.value(), self.follow_button.isChecked()
            self._updating = True
            try:
                self.view.appendPlainText("\n".join(shown))
                if follow:
                    self._scroll_to_end()
                else:
                    scrollbar.setValue(keep)
            finally:
                self._updating = False
        self._refresh_counts()
        if self.search.text():
            self._find_matches(keep_current=True)

    def _refresh_counts(self):
        counts = {level: 0 for level in LEVELS}
        for level, _ in self.entries[self.cleared_at:]:
            counts[level] += 1
        for level, chip in self.level_buttons.items():
            chip.setText(f"{LEVEL_LABELS[level]}  {counts[level]}")
        self.counts = counts

    def _set_level(self, level: str, on: bool):
        (self.levels.add if on else self.levels.discard)(level)
        self.rebuild()

    def _update_footer(self):
        path = self.reader.path if self.reader else None
        if not path or not os.path.exists(path):
            self.path_label.setText(f"{self.source.title}: log file not found" + (f" ({path})" if path else ""))
            self.status_label.setText("")
            return
        size = os.path.getsize(path)
        human = f"{size / 1024:.1f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"
        self.path_label.setText(f"{path}  ·  {human}")
        shown = self.view.blockCount() if self.view.toPlainText() else 0
        total = len(self.entries) - self.cleared_at
        updated = self.last_update.strftime("%H:%M:%S") if self.last_update else "-"
        self.status_label.setText(f"Showing {shown:,} of {total:,} lines  ·  updated {updated}")

    # --- search ---------------------------------------------------------------

    def _on_search_changed(self, _text):
        if self.matches_only.isChecked():
            self.rebuild()
        else:
            self._find_matches()
            if self.matches:
                self._go_to_match(0)

    def _find_matches(self, keep_current=False):
        needle = self.search.text()
        previous = self.current_match
        self.matches = []
        if needle:
            document = self.view.document()
            cursor = QtGui.QTextCursor(document)
            while len(self.matches) < MAX_HIGHLIGHTS:
                cursor = document.find(needle, cursor)
                if cursor.isNull():
                    break
                self.matches.append(QtGui.QTextCursor(cursor))
        if keep_current and 0 <= previous < len(self.matches):
            self.current_match = previous
        else:
            self.current_match = -1 if not self.matches else min(max(previous, 0), len(self.matches) - 1)
        self._paint_matches()

    def _paint_matches(self):
        selections = []
        for i, cursor in enumerate(self.matches):
            selection = QtWidgets.QTextEdit.ExtraSelection()
            selection.cursor = cursor
            current = i == self.current_match
            selection.format.setBackground(QtGui.QColor("#2A97D6" if current else "#3a3320"))
            if current:
                selection.format.setForeground(QtGui.QColor("#ffffff"))
            selections.append(selection)
        self.view.setExtraSelections(selections)
        if not self.search.text():
            self.match_label.setText("")
        elif not self.matches:
            self.match_label.setText("No matches")
        else:
            more = "+" if len(self.matches) >= MAX_HIGHLIGHTS else ""
            self.match_label.setText(f"{self.current_match + 1} of {len(self.matches)}{more}")

    def _go_to_match(self, index: int):
        if not self.matches:
            return
        self.current_match = index % len(self.matches)
        self.follow_button.setChecked(False)
        self.view.setTextCursor(self.matches[self.current_match])
        self.view.centerCursor()
        self._paint_matches()

    def next_match(self):
        self._go_to_match(self.current_match + 1)

    def previous_match(self):
        self._go_to_match(self.current_match - 1)

    # --- actions --------------------------------------------------------------

    def copy(self):
        cursor = self.view.textCursor()
        text = cursor.selectedText().replace(" ", "\n") if cursor.hasSelection() else self.view.toPlainText()
        QtWidgets.QApplication.clipboard().setText(text)
        self._flash("Copied selection" if cursor.hasSelection() else f"Copied {self.view.blockCount():,} lines")

    def save(self) -> Optional[str]:
        """Save what's shown (with filters applied) to the export folder; returns the file path"""
        try:
            os.makedirs(self.export_dir, exist_ok=True)
            stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
            path = os.path.join(self.export_dir, f"{self.source.key}-{stamp}.log")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# {self.source.title} log from {self.reader.path}\n")
                f.write(f"# Saved {datetime.datetime.now():%Y-%m-%d %H:%M:%S}; levels: "
                        f"{', '.join(LEVEL_LABELS[lv] for lv in LEVELS if lv in self.levels)}"
                        + (f"; search: {self.search.text()!r}" if self.search.text() else "") + "\n")
                f.write(self.view.toPlainText() + "\n")
            self._flash(f"Saved to {path}")
            return path
        except OSError as e:
            self._flash(f"Couldn't save: {e}", error=True)
            return None

    def clear(self):
        """Hide what's there now; new lines keep appearing"""
        self.cleared_at = len(self.entries)
        self.cleared_link.setVisible(True)
        self.rebuild()
        self._update_footer()
        self._flash(f"Cleared at {datetime.datetime.now():%H:%M:%S}")

    def show_all(self):
        self.cleared_at = 0
        self.cleared_link.setVisible(False)
        self.rebuild()
        self._update_footer()

    def _flash(self, message: str, error: bool = False):
        self.message_label.setProperty("error", error)
        self.message_label.style().unpolish(self.message_label)
        self.message_label.style().polish(self.message_label)
        self.message_label.setText(message)
        self.message_timer.start(6000)

    # --- follow / wrap / visibility -------------------------------------------

    def _scroll_to_end(self):
        scrollbar = self.view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _on_follow_toggled(self, on):
        if on:
            self._scroll_to_end()

    def _on_scrolled(self, value):
        # Scrolling up to read pauses following; scrolling back to the bottom resumes it
        if self._updating:
            return
        at_bottom = value >= self.view.verticalScrollBar().maximum() - 2
        if at_bottom != self.follow_button.isChecked():
            self.follow_button.blockSignals(True)
            self.follow_button.setChecked(at_bottom)
            self.follow_button.blockSignals(False)

    def _on_wrap_toggled(self, on):
        self.view.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth if on else QtWidgets.QPlainTextEdit.NoWrap)

    def showEvent(self, event):
        super().showEvent(event)
        self.poll()
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()
