"""
Probe: find a corner, an edge, the centre of a hole or boss, the top, or how square a part sits,
and set a work offset from it.

Start from what you want to find, answer a question on a picture, put the probe where it says
(with a camera scan Milo can take it there), check the preview, and probe. The result is shown in
plain words before anything changes, and every work offset change can be undone. Tool length (tool
setter, touch plate), the probe's own setup with tip calibration, and qtvcp's complete set of
routines (Advanced) are here too.

The routines are qtvcp's, run through milo_probe_subprog.py (protected descents); probe_jobs.py
holds the logic, probe_widgets.py the drawings.
"""

import time
from dataclasses import asdict, fields
from typing import List, Optional, Tuple

from PyQt5 import QtWidgets
from PyQt5.QtCore import Qt

import machine_safety
import probe_jobs as pj
from milo_ui import theme, kit, probing
from milo_ui.theme import C, T
from milo_ui.shell import Page
from milo_ui.probe_widgets import (PartPicker, ProbePreview, camera_start, classify_corners, nearest_feature,
                                   part_at, squareness_warning)

# qtvcp's probe widget (Advanced) is laid out for a smaller font than Milo's: its buttons are too
# small for Milo's 20 px side padding, and its labels have fixed widths
PROBE_WIDGET_QSS = "QPushButton { padding: 0px 10px; }"
CAMERA_AGREEMENT = 3.0   # mm: a result further than this from where the camera saw the feature is flagged
REPEAT_TOLERANCE = 0.02  # mm: two touches further apart than this are flagged

# ProbeSetup field -> (label, unit kind) for the setup screen
SETUP_FIELDS = [
    ("tool", "Probe tool number", ""),
    ("tip_diameter", "Tip diameter (effective)", "len"),
    ("travel_feed", "Travel speed", "vel"),
    ("search_feed", "Fast touch speed", "vel"),
    ("probe_feed", "Measuring touch speed", "vel"),
    ("max_travel", "Search distance (sideways)", "len"),
    ("max_z_travel", "Search distance (down, top surface)", "len"),
    ("step_off", "Back off between touches", "len"),
    ("xy_clearance", "Step out past an edge", "len"),
    ("z_clearance", "Go down beside the part", "len"),
    ("extra_depth", "…and this much more", "len"),
    ("edge_length", "Distance along an edge", "len"),
]

GOAL_SUMMARIES = {
    "corner": "Touches the two faces at a corner and sets X and Y there.",
    "edge": "Touches one face and sets X or Y on it.",
    "hole": "Touches the inside of a hole or pocket on four sides and finds its centre and size.",
    "boss": "Touches the outside of a boss on four sides and finds its centre and size.",
    "surface": "Touches the top and sets Z there.",
    "angle": "Touches one edge twice and tells you how far it's turned from square.",
}


def fit_texts(root: QtWidgets.QWidget):
    """
    Widen the labels and buttons under `root` whose text doesn't fit their fixed size (Designer
    layouts made for another font), so nothing is cut off. Widgets that were the same fixed width
    grow to the same width, so columns stay lined up; containers with a width cap grow to fit.
    """
    widgets = []
    for w in root.findChildren((QtWidgets.QLabel, QtWidgets.QAbstractButton)):
        if isinstance(w, QtWidgets.QLabel) and (w.wordWrap() or not w.text().strip() or w.pixmap() is not None):
            continue
        if isinstance(w, QtWidgets.QAbstractButton) and not w.text().strip():
            continue
        w.ensurePolished()
        widgets.append(w)
    # Same kind and same width cap (e.g. every 100 px parameter label): one width for all of them
    needed = {}
    for w in widgets:
        key = (type(w), w.maximumWidth())
        needed[key] = max(needed.get(key, 0), w.sizeHint().width())
    for w in widgets:
        hint = w.sizeHint()
        capped = w.maximumWidth() < QtWidgets.QWIDGETSIZE_MAX
        width = needed[(type(w), w.maximumWidth())] if capped else hint.width()
        if w.maximumWidth() < width:
            w.setMaximumWidth(width)
        if w.minimumWidth() < width:
            w.setMinimumWidth(width)
        if w.maximumHeight() < hint.height():
            w.setMaximumHeight(hint.height())
    # Containers with a width cap (the CLEAR buttons' 90 px column): deepest first, so each
    # sees its children's new sizes
    containers = [c for c in root.findChildren(QtWidgets.QWidget)
                  if c.layout() is not None and c.maximumWidth() < QtWidgets.QWIDGETSIZE_MAX]
    containers.sort(key=lambda c: -_depth(c, root))
    for c in containers:
        c.layout().invalidate()
        width = c.layout().minimumSize().width()
        if c.maximumWidth() < width:
            c.setMaximumWidth(width)


def _depth(widget, root):
    depth = 0
    while widget is not None and widget is not root:
        widget, depth = widget.parentWidget(), depth + 1
    return depth


class ParamRow(QtWidgets.QPushButton):
    """A setting row: label left, value right; tap to edit on the number pad"""

    def __init__(self, title, get_value, set_value, units, parent=None, hint=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(60)
        self.setStyleSheet(f"QPushButton {{ background: transparent; border: none; border-bottom: 1px solid {C.line};"
                           f"border-radius: 0; padding: 0 4px; }} QPushButton:pressed {{ background: {C.card_hi}; }}")
        self.title, self.get_value, self.set_value, self.units, self.hint = title, get_value, set_value, units, hint
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
        self.value.setText(f"{self.get_value():g} {self.units}".strip())

    def _edit(self):
        kit.NumPad(self, self.title, lambda v: (self.set_value(v), self.refresh()),
                   initial=self.get_value(), units=self.units, hint=self.hint).show_centered()


class GoalTile(QtWidgets.QPushButton):
    """One thing to find (or to calibrate): icon, name and a line on what it does"""

    def __init__(self, key, parent=None, entry=None):
        super().__init__(parent)
        title, icon, summary = entry or pj.GOALS[key]
        self.setCheckable(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setMinimumHeight(70)
        self.setStyleSheet(
            f"QPushButton {{ background: transparent; border: 1px solid transparent; border-radius: 14px; "
            f"text-align: left; }} QPushButton:checked {{ background: {C.accent_soft}; border-color: {C.accent}; }}"
            f"QPushButton:pressed {{ background: {C.card_hi}; }}")
        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(12, 6, 12, 6)
        row.setSpacing(14)
        badge = QtWidgets.QLabel()
        badge.setFixedSize(46, 46)
        badge.setAlignment(Qt.AlignCenter)
        badge.setPixmap(theme.pixmap(icon, C.accent_hi, 26))
        badge.setStyleSheet(f"background: {C.card_hi}; border-radius: 12px;")
        row.addWidget(badge)
        words = QtWidgets.QVBoxLayout()
        words.setSpacing(1)
        words.addWidget(kit.label(title, "value", size=T.body, weight=theme.MEDIUM))
        words.addWidget(kit.label(summary, "muted"))
        row.addLayout(words, 1)
        for child in self.findChildren(QtWidgets.QWidget):
            child.setAttribute(Qt.WA_TransparentForMouseEvents)


class CheckRow(QtWidgets.QWidget):
    """One thing that has to be right before probing: a mark, the words, and maybe a fix button"""

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
        self.ok = False
        self._action = None
        self.button.clicked.connect(lambda: self._action and self._action())

    def set(self, ok: bool, text: str, fix=None):
        self.ok = ok
        self.icon.setPixmap(theme.pixmap("check-circle" if ok else "warning", C.green if ok else C.amber, 22))
        self.text.setText(text)
        self.text.setStyleSheet(f"color: {C.text_2 if ok else C.text};")
        self.button.setVisible(fix is not None)
        if fix:
            self.button.setText(fix[0])
            self._action = fix[1]


class ProbePage(Page):
    key = "probe"
    title = "Probe"
    icon = "target"
    wants_stage = False

    def __init__(self, shell, probe_widget=None, runner=None, scan=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        m = self.machine = shell.machine
        self.units = "mm" if m.machine_metric else "inch"
        self.setup = pj.ProbeSetup.from_prefs(shell.prefs, self.units)
        saved = shell.prefs.get("probe_job") or {}
        self.job = pj.ProbeJob(**{f.name: saved[f.name] for f in fields(pj.ProbeJob) if f.name in saved})
        self.goal = self.job.goal
        self.probe_checked = False
        self._last_tool = m.tool
        self._last_tripped = m.probe_tripped
        self.running = False
        self.auto_apply = False   # set the result without asking (a confirmed request from Milo or the pendant)
        self.first: Optional[pj.Found] = None
        self.found: Optional[pj.Found] = None
        self.calibrating = False
        self.applied_label = ""
        self.scan = scan if scan is not None else self._load_scan()
        self.part_index = -1
        self.runner = runner if runner is not None else self._make_runner()
        self.runner.finished.connect(self._on_finished)
        self.runner.failed.connect(self._on_failed)
        self._apply_probe_tool()

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)
        row.addWidget(self._build_goals())
        self.stack = QtWidgets.QStackedWidget()
        self.job_view = self._build_job_view()
        self.result_view = self._build_result_view()
        self.tool_view = self._build_tool_view()
        self.setup_view = self._build_setup_view()
        self.advanced_view = self._build_advanced_view(probe_widget)
        for view in (self.job_view, self.result_view, self.tool_view, self.setup_view, self.advanced_view):
            self.stack.addWidget(view)
        row.addWidget(self.stack, 1)
        self.preview_card = self._build_preview()
        row.addWidget(self.preview_card)

        m.changed.connect(self._on_machine_changed)
        m.position_changed.connect(self._on_position)
        self.select_goal(self.goal if self.goal in pj.GOALS else "corner")

    # --- setup ------------------------------------------------------------------------------

    def _make_runner(self):
        from milo_ui.probe_runner import ProbeRunner, SimProbeRunner
        if type(self.machine).__name__ != "QtvcpMachine":
            return SimProbeRunner(self.machine)
        try:
            from qtvcp.core import Status
            status = Status()
            return ProbeRunner(block_errors=status.block_error_polling, unblock_errors=status.unblock_error_polling)
        except Exception:
            return ProbeRunner()

    def _load_scan(self):
        try:
            from milo_vision import state as vstate
            scan = vstate.load()
        except Exception:
            return None
        return scan if scan is not None and scan.parts else None

    def _apply_probe_tool(self):
        tool = int(self.setup.tool) if self.setup.tool else None
        self.machine.probe_tool = tool
        machine_safety.set_probe_tool(tool)

    def _save_job(self):
        self.shell.prefs.set("probe_job", asdict(self.job))

    @property
    def u(self) -> str:
        return "mm" if self.units == "mm" else "in"

    def _machine_pos(self) -> List[float]:
        """The probe's machine position in machine units (the DRO may show program units)"""
        m = self.machine
        k = 1.0 if m.metric == m.machine_metric else (25.4 if m.metric else 1 / 25.4)
        return [v / k for v in (list(m.pos_abs) + [0.0, 0.0, 0.0])[:3]]

    # --- building the page ----------------------------------------------------------------------

    def _build_goals(self):
        card = kit.Card(title="What do you want to find?")
        card.setFixedWidth(330)
        self.goal_group = QtWidgets.QButtonGroup(self)
        self.goal_group.setExclusive(True)
        self.goal_tiles = {}
        for key in pj.GOALS:
            tile = GoalTile(key)
            tile.clicked.connect(lambda _=False, k=key: self.select_goal(k))
            self.goal_group.addButton(tile)
            self.goal_tiles[key] = tile
            card.add(tile)
        card.body.addStretch(1)
        self.undo_button = kit.Button("Undo", icon="arrow-counter-clockwise", variant="outline", on_click=self.undo)
        card.add(self.undo_button)
        self.setup_button = kit.Button("Probe setup", icon="sliders", variant="ghost",
                                       on_click=lambda: self.select_goal("setup"))
        self.advanced_button = kit.Button("Advanced: all routines", icon="gear", variant="ghost",
                                          on_click=lambda: self.select_goal("advanced"))
        card.add(self.setup_button)
        card.add(self.advanced_button)
        return card

    def _build_job_view(self):
        card = kit.Card(title="Corner")
        self.job_card = card
        self.job_summary = kit.label("", "muted", wrap=True)
        card.add(self.job_summary)

        # The question
        self.side_toggle = kit.Segmented(["Outside corner", "Inside corner"])
        self.side_toggle.selected.connect(lambda i: self._set_job(inside=i == 1))
        card.add(self.side_toggle)
        self.picker = PartPicker()
        self.picker.picked.connect(self._picked)
        card.add(self.picker)
        self.shape_toggle = kit.Segmented(["Round", "Rectangle"])
        self.shape_toggle.selected.connect(lambda i: self._set_job(shape=pj.SHAPES[i]))
        card.add(self.shape_toggle)
        self.size_rows = {
            "diameter": ParamRow("Diameter (roughly)", lambda: self.job.diameter,
                                 lambda v: self._set_job(diameter=abs(v)), self.u),
            "width": ParamRow("Width along X (roughly)", lambda: self.job.width,
                              lambda v: self._set_job(width=abs(v)), self.u),
            "length": ParamRow("Length along Y (roughly)", lambda: self.job.length,
                               lambda v: self._set_job(length=abs(v)), self.u),
        }
        for size_row in self.size_rows.values():
            card.add(size_row)
        self.surface_note = kit.label("Nothing to choose: the probe goes straight down until it touches.",
                                      "body", wrap=True)
        card.add(self.surface_note)

        # Where to put the probe
        card.add(kit.eyebrow("Put the probe here"))
        self.hint = kit.label("", "body", wrap=True)
        card.add(self.hint)
        camera = QtWidgets.QHBoxLayout()
        camera.setSpacing(8)
        self.camera_text = kit.label("", "muted", wrap=True)
        camera.addWidget(self.camera_text, 1)
        self.next_part_button = kit.Button("Next part", variant="ghost", size="sm", on_click=self._next_part)
        camera.addWidget(self.next_part_button)
        self.move_button = kit.Button("Take me there", icon="crosshair", variant="outline", size="sm",
                                      on_click=self.move_to_start)
        camera.addWidget(self.move_button)
        card.add(camera)

        # What has to be right
        card.add(kit.eyebrow("Before probing"))
        self.checks = [CheckRow() for _ in range(4)]
        for check in self.checks:
            card.add(check)
        self.repeat_row = kit.ToggleRow("Probe twice and compare", "Flags a loose part or a dirty tip")
        self.repeat_row.toggled.connect(lambda on: self._set_job(repeat=on))
        card.add(self.repeat_row)
        card.body.addStretch(1)

        # Start stays in view below the scrolling questions
        bar = kit.Card(padding=16)
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(12)
        self.status = kit.label("", "muted", wrap=True)
        bottom.addWidget(self.status, 1)
        self.stop_button = kit.Button("Stop", icon="stop", variant="danger", size="lg", on_click=self.stop)
        self.stop_button.hide()
        bottom.addWidget(self.stop_button)
        self.start_button = kit.Button("Start probing", icon="play-fill", variant="primary", size="lg",
                                       on_click=self.start)
        self.start_button.setMinimumWidth(240)
        bottom.addWidget(self.start_button)
        bar.add(bottom)
        view = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(view)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        column.addWidget(self._scrolled(card), 1)
        column.addWidget(bar)
        return view

    def _build_result_view(self):
        card = kit.Card(title="Result")
        self.result_headline = kit.label("", "value", wrap=True, size=T.body_lg)
        card.add(self.result_headline)
        self.result_details = kit.label("", "body", wrap=True)
        card.add(self.result_details)
        self.result_warnings = kit.label("", "body", wrap=True, color=C.amber)
        card.add(self.result_warnings)
        target = QtWidgets.QHBoxLayout()
        target.addWidget(kit.label("Set it in", "body"))
        self.wcs_toggle = kit.Segmented(list(pj.WCS))
        self.wcs_toggle.selected.connect(lambda i: (self._set_job(wcs=pj.WCS[i]), self._show_result()))
        target.addWidget(self.wcs_toggle)
        target.addStretch(1)
        card.add(target)
        self.applied = kit.label("", "body", wrap=True, color=C.green)
        card.add(self.applied)
        card.body.addStretch(1)
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(12)
        self.done_button = kit.Button("Done", variant="ghost", size="lg", on_click=lambda: self.select_goal(self.goal))
        self.again_button = kit.Button("Probe again", icon="arrow-counter-clockwise", variant="outline", size="lg",
                                       on_click=self.start)
        self.set_button = kit.Button("Set", icon="check", variant="primary", size="lg", on_click=self.apply_result)
        self.set_button.setMinimumWidth(260)
        buttons.addWidget(self.done_button)
        buttons.addStretch(1)
        buttons.addWidget(self.again_button)
        buttons.addWidget(self.set_button)
        card.add(buttons)
        return card

    def _build_tool_view(self):
        m, shell = self.machine, self.shell
        card = kit.Card(title="Tool length")
        card.add(kit.label("Measure the loaded tool on the tool setter, or touch it off on the touch plate to set Z.",
                           "muted", wrap=True))
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(12)
        buttons.addWidget(kit.Button("Measure tool", icon="ruler", variant="primary", size="lg",
                                     on_click=lambda: probing.measure_tool(self, m, shell.prefs, shell.toaster.show)))
        buttons.addWidget(kit.Button("Touch off Z", icon="arrow-line-down", size="lg",
                                     on_click=lambda: probing.touch_plate(self, m, shell.prefs, shell.toaster.show)))
        buttons.addStretch(1)
        card.add(buttons)
        card.add(kit.eyebrow("Tool setter and touch plate"))
        for key, title, _default, kind in probing.PARAMETERS:
            units = f"{m.units}/min" if kind == "vel" else m.units
            card.add(ParamRow(title, lambda k=key: probing.get(shell.prefs, k),
                              lambda v, k=key: shell.prefs.set(f"probe.{k}", v), units))
        card.body.addStretch(1)
        return self._scrolled(card)

    def _build_setup_view(self):
        card = kit.Card(title="Probe setup")
        card.add(kit.label("Set once for your probe. Lengths in the machine's units; speeds per minute. The camera's "
                           "probing on the Vision page uses the same probe tool and tip.", "muted", wrap=True))
        self.setup_rows = []
        for name, title, kind in SETUP_FIELDS:
            unit = {"len": self.u, "vel": f"{self.u}/min"}.get(kind, "")
            setup_row = ParamRow(title, lambda n=name: float(getattr(self.setup, n)),
                                 lambda v, n=name: self._set_setup(n, v), unit)
            self.setup_rows.append(setup_row)
            card.add(setup_row)
        card.add(kit.eyebrow("Calibrate the tip on a ring gauge"))
        card.add(kit.label("A ball acts a little smaller than it is (it deflects before it triggers). Put the "
                           "probe in the middle of a ring gauge with the tip a couple of mm above it, then "
                           "Calibrate: it probes the ring twice and works out the tip's effective size.",
                           "muted", wrap=True))
        default_ring = 25.0 if self.units == "mm" else 1.0
        self.ring_row = ParamRow("Ring gauge diameter (exact)",
                                 lambda: float(self.shell.prefs.get("probe_ring_gauge", default_ring)),
                                 lambda v: self.shell.prefs.set("probe_ring_gauge", abs(v)), self.u)
        card.add(self.ring_row)
        calibrate = QtWidgets.QHBoxLayout()
        calibrate.setSpacing(12)
        self.calibrate_status = kit.label("", "body", wrap=True)
        calibrate.addWidget(self.calibrate_status, 1)
        self.save_tip_button = kit.Button("Save tip size", icon="check", variant="primary", on_click=self._save_tip)
        self.save_tip_button.hide()
        calibrate.addWidget(self.save_tip_button)
        calibrate.addWidget(kit.Button("Calibrate", icon="target", variant="outline", on_click=self.calibrate))
        card.add(calibrate)
        card.body.addStretch(1)
        return self._scrolled(card)

    def _build_advanced_view(self, probe_widget):
        card = kit.Card(title="Advanced: every probe routine")
        card.add(kit.label("qtvcp's own probe screen, with the routines guided probing leaves out (ridges, valleys, "
                           "the other angle and calibration variants). It keeps its own parameters, separate from "
                           "Probe setup, and its descents beside the part are not probe-protected.", "muted",
                           wrap=True))
        if probe_widget is not None:
            probe_widget.setStyleSheet(PROBE_WIDGET_QSS)
            fit_texts(probe_widget)
            scroll = QtWidgets.QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setWidget(probe_widget)
            QtWidgets.QScroller.grabGesture(scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
            card.add(scroll, 1)
        else:
            card.add(kit.label("It appears here when the screen runs on the machine ([PROBE] USE_PROBE in the INI).",
                               "body", wrap=True))
            card.body.addStretch(1)
        return card

    def _build_preview(self):
        card = kit.Card(title="Preview")
        card.setFixedWidth(560)
        self.preview = ProbePreview()
        self.preview.tapped.connect(self._camera_tapped)
        card.add(self.preview, 1)
        self.live = kit.label("", "muted", mono=True, wrap=True)
        card.add(self.live)
        self.view_toggle = kit.Segmented(["Drawing", "Camera"])
        self.view_toggle.selected.connect(lambda i: self._set_camera(i == 1))
        card.add(self.view_toggle)
        return card

    def _scrolled(self, widget):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        QtWidgets.QScroller.grabGesture(scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        return scroll

    # --- choosing ---------------------------------------------------------------------------------

    def select_goal(self, goal):
        """A goal tile, or 'setup' / 'advanced'"""
        if self.running:
            return
        if goal in self.goal_tiles:
            self.goal_tiles[goal].setChecked(True)
        else:
            checked = self.goal_group.checkedButton()
            if checked is not None:
                self.goal_group.setExclusive(False)
                checked.setChecked(False)
                self.goal_group.setExclusive(True)
        self.goal = goal
        if goal in pj.PROBE_GOALS:
            self.job.goal = goal
            self._save_job()
            self.stack.setCurrentWidget(self.job_view)
        elif goal == "tool":
            self.stack.setCurrentWidget(self.tool_view)
        elif goal == "setup":
            self.stack.setCurrentWidget(self.setup_view)
        elif goal == "advanced":
            self.stack.setCurrentWidget(self.advanced_view)
        self.preview_card.setVisible(goal in pj.PROBE_GOALS)
        self.refresh()

    def _set_job(self, **changes):
        for key, value in changes.items():
            setattr(self.job, key, value)
        self._save_job()
        self.refresh()

    def _picked(self, name):
        if self.job.goal == "corner":
            self._set_job(corner=name)
        else:
            self._set_job(edge=name)

    def _set_setup(self, name, value):
        kind = type(getattr(self.setup, name))
        setattr(self.setup, name, kind(value if name == "extra_depth" else abs(value)))
        self.setup.save(self.shell.prefs)
        if name == "tool":
            self._apply_probe_tool()
        self.refresh()

    # --- the camera ---------------------------------------------------------------------------------

    def _set_camera(self, on):
        if on and self.scan is not None:
            if self.part_index < 0:
                x, y, _ = self._machine_pos()
                self.part_index = part_at(self.scan, x, y)
        else:
            self.part_index = -1
        self.refresh()

    def _next_part(self):
        if self.scan is not None and self.scan.parts:
            self.part_index = (self.part_index + 1) % len(self.scan.parts)
            self.refresh()

    def _camera_part(self):
        if self.scan is None or not (0 <= self.part_index < len(self.scan.parts)):
            return None
        return self.scan.parts[self.part_index]

    def _camera_tapped(self, x, y):
        if self.scan is None:
            return
        index = part_at(self.scan, x, y)
        if index >= 0:
            self.part_index = index
        part = self._camera_part()
        feature = nearest_feature(part, x, y, self.job.goal) if part is not None else None
        if feature and self.job.goal == "corner":
            self._set_job(corner=feature, inside=False)
        elif feature:
            self._set_job(edge=feature)
        else:
            self.refresh()

    def camera_target(self) -> Optional[Tuple[float, float]]:
        part = self._camera_part()
        return camera_start(self.job, self.setup, part) if part is not None else None

    def move_to_start(self):
        """Lift to the top, then go over the start point the camera worked out (the operator lowers Z)"""
        target, m = self.camera_target(), self.machine
        if target is None:
            return False
        if not m.ready:
            self.shell.toaster.show(m.state_detail or "The machine isn't ready.", "warning")
            return False
        ok = m.mdi_lines(["G90 G53 G0 Z0", f"G90 G53 G0 X{target[0]:.3f} Y{target[1]:.3f}"])
        if ok:
            self.shell.toaster.show(f"Going over the start point. Then lower the probe until the tip is about "
                                    f"{self.setup.start_height:g} {self.u} above the top.", "info")
        return ok

    # --- the checks ---------------------------------------------------------------------------------

    def problems(self) -> List[Tuple[bool, str, Optional[tuple]]]:
        """(ok, text, fix) for each thing that has to be right before probing"""
        m, s = self.machine, self.setup
        rows = []
        ready = m.on and not m.estop and m.all_homed and m.interp == "idle" and not m.is_running
        rows.append((ready, "Machine on, homed and idle" if ready else (m.state_detail or "The machine isn't ready"),
                     None))
        if m.probe_in_spindle:
            rows.append((True, f"Probe T{s.tool} is in the spindle", None))
        else:
            rows.append((False, f"Put the probe (T{s.tool}) in the spindle", ("It's in", lambda: m.set_tool(s.tool))))
        if m.probe_tripped:
            rows.append((False, "The probe reads as touching. Make sure it isn't resting on anything (and check its "
                                "battery or cable).", None))
        elif not self.probe_checked:
            rows.append((False, "Tap the probe tip with a finger, so Milo knows the probe works", None))
        else:
            rows.append((True, "Probe checked: it responds to a touch", None))
        problem = None
        if m.metric != m.machine_metric:
            problem = (f"Switch to {'G21 (mm)' if m.machine_metric else 'G20 (inch)'}: probing distances are in the "
                       f"machine's units")
        else:
            current = m.current_offset(m.wcs)
            if current and abs(current[1]) > 1e-6 and self.job.goal != "angle":
                problem = f"{m.wcs} has a {current[1]:.3f}° rotation: results along X and Y would be off. Clear it first."
        if problem is None:
            problem = self._size_problem() or pj.limit_problem(self._plan(), self._machine_pos(), m.limits)
        rows.append((problem is None, problem or "Within the soft limits from here", None))
        return rows

    def _size_problem(self) -> Optional[str]:
        job, tip, c = self.job, self.setup.tip_diameter, self.setup.xy_clearance
        if job.goal not in ("hole", "boss"):
            return None
        smallest = job.diameter if job.shape == "round" else min(job.width, job.length)
        kind = "hole" if job.goal == "hole" else "boss"
        if smallest <= tip * 2:
            return f"The {kind} has to be more than twice the tip's size."
        if job.goal == "hole" and job.shape == "rectangle" and smallest / 2 <= c:
            return f"“Step out past an edge” ({c:g} {self.u}) is too big for this pocket; make it smaller in setup."
        return None

    def _plan(self) -> pj.Plan:
        return pj.plan(self.job, self.setup, self.units)

    def can_start(self) -> bool:
        return all(ok for ok, _, _ in self.problems()) and not self.running

    # --- refreshing ---------------------------------------------------------------------------------

    def refresh(self):
        history = self.machine.offset_history
        self.undo_button.setVisible(bool(history))
        if history:
            entry = history[-1]
            ago = max(0, int((time.time() - entry["time"]) // 60))
            self.undo_button.setText(f"Undo: {entry['label']} ({entry['wcs']}" + (f", {ago} min ago)" if ago else ")"))
        if self.goal not in pj.PROBE_GOALS:
            return
        job, goal = self.job, self.job.goal
        self.job_card.title_label.setText(pj.GOALS[goal][0].upper())
        self.job_summary.setText(GOAL_SUMMARIES[goal])
        self.side_toggle.setVisible(goal == "corner")
        self.side_toggle.set_index(1 if job.inside else 0)
        self.picker.setVisible(goal in ("corner", "edge", "angle"))
        self.picker.set_state("corner" if goal == "corner" else "edge",
                              job.corner if goal == "corner" else job.edge, job.inside)
        sized = goal in ("hole", "boss")
        self.shape_toggle.setVisible(sized)
        self.shape_toggle.set_index(pj.SHAPES.index(job.shape))
        self.size_rows["diameter"].setVisible(sized and job.shape == "round")
        self.size_rows["width"].setVisible(sized and job.shape == "rectangle")
        self.size_rows["length"].setVisible(sized and job.shape == "rectangle")
        for size_row in self.size_rows.values():
            size_row.refresh()
        self.surface_note.setVisible(goal == "surface")
        self.hint.setText(self._plan().start_hint)
        self.repeat_row.setChecked(job.repeat)

        part = self._camera_part()
        has_scan = self.scan is not None and bool(self.scan.parts)
        self.view_toggle.setVisible(has_scan)
        self.view_toggle.set_index(1 if part is not None else 0)
        self.next_part_button.setVisible(part is not None and len(self.scan.parts) > 1)
        self.move_button.setVisible(self.camera_target() is not None)
        if part is None:
            self.camera_text.setText(
                "Scan the table on the Vision page and Milo can show your part here and take the probe to the start."
                if not has_scan else "Switch the preview to Camera to use the part the camera found.")
        else:
            words = f"Camera: part {self.part_index + 1} of {len(self.scan.parts)}. Tap a corner or edge on the photo."
            warn = squareness_warning(part) if goal in ("corner", "edge") else None
            if goal == "corner" and job.inside:
                warn = "The camera can't see inside a pocket: place the probe by eye."
            self.camera_text.setText(words + (f" {warn}" if warn else ""))
        self._on_position()
        rows = self.problems()
        for check, (ok, text, fix) in zip(self.checks, rows):
            check.set(ok, text, fix)
        self.start_button.setEnabled(self.can_start())
        self.start_button.setVisible(not self.running)
        self.stop_button.setVisible(self.running)

    def _on_position(self):
        x, y, z = self._machine_pos()
        text = f"Probe at X {x:.3f}  Y {y:.3f}  Z {z:.3f} (machine)"
        if self.job.goal in pj.PROBE_GOALS:
            self.preview.set_plan(self._plan(), self.setup.tip_diameter, self.units)
            scan = self.scan if self._camera_part() is not None else None
            if self.preview.scan is not scan or self.preview.part_index != self.part_index:
                self.preview.set_scan(scan, self.part_index)
            target = self.camera_target()
            self.preview.set_live((x, y), target)
            if target is not None:
                off = ((target[0] - x) ** 2 + (target[1] - y) ** 2) ** 0.5
                text += " · on the start point" if off < 1.0 else f" · {off:.1f} {self.u} from the start point"
        self.live.setText(text)

    def _on_machine_changed(self, topic):
        m = self.machine
        if topic == "tool":
            if m.tool != self._last_tool and m.probe_in_spindle:
                self.probe_checked = False  # the probe was just put in: check it again
            self._last_tool = m.tool
        if topic == "state" and m.probe_tripped != self._last_tripped:
            if m.probe_tripped and not self.running:
                self.probe_checked = True
            self._last_tripped = m.probe_tripped
        if topic in ("state", "tool", "homing", "offsets", "offset_history") and not self.running:
            self.refresh()

    def on_show(self):
        if self.scan is None:
            self.scan = self._load_scan()
        self.refresh()

    # --- running ----------------------------------------------------------------------------------------

    def start(self, auto_apply=False):
        """Probe the current job (auto_apply: set the result unless something looks off)"""
        if self.running:
            return False
        if self.stack.currentWidget() is self.result_view:
            self.stack.setCurrentWidget(self.job_view)
        failing = [text for ok, text, _ in self.problems() if not ok]
        if failing:
            self._show_status(failing[0], C.amber)
            self.refresh()
            return False
        self.auto_apply = auto_apply
        self.first = self.found = None
        self.applied_label = ""
        return self._run()

    def _run(self):
        if not self.runner.run(pj.routine(self.job), pj.parameters(self.job, self.setup)):
            self._show_status("The probe is busy with another routine.", C.amber)
            return False
        self.running = True
        self._show_status("Probing… hands clear. Stop or E-stop to abort." if self.first is None
                          else "Probing a second time to compare…", C.text_2)
        self.refresh()
        return True

    def stop(self):
        self.runner.stop()
        self.machine.abort()

    def _on_failed(self, message):
        self.running = False
        self.first = None
        if self.calibrating:
            self.calibrating = False
            self.job = self._saved_job
            self.calibrate_status.setText(f"Calibration stopped: {message}")
        self._show_status(f"Probing stopped: {message}", C.red)
        self.refresh()

    def _on_finished(self, data):
        try:
            found = pj.interpret(self.job, data)
        except ValueError as e:
            return self._on_failed(str(e))
        if self.job.repeat and self.first is None:
            self.first = found
            self.running = False
            return self._run()
        self.running = False
        self.found = found
        if self.calibrating:
            return self._finish_calibration()
        self._show_status("", C.text_3)
        self.stack.setCurrentWidget(self.result_view)
        self.wcs_toggle.set_index(pj.WCS.index(self.job.wcs) if self.job.wcs in pj.WCS else 0)
        warnings = self._show_result()
        if self.auto_apply and not warnings and self.job.axes:
            self.apply_result()
        self.refresh()

    # --- the result -----------------------------------------------------------------------------------

    def new_offsets(self) -> Optional[dict]:
        """The target system's new origin from the result (machine units), or None"""
        active = self.machine.current_offset(self.machine.wcs)
        if active is None or self.found is None:
            return None
        return pj.new_origin(self.found, active[0])

    def _target_rotation(self) -> float:
        """The rotation that squares the target system to the probed edge"""
        active = self.machine.current_offset(self.machine.wcs)
        return (active[1] if active else 0.0) + (self.found.angle or 0.0)

    def _show_result(self) -> List[str]:
        """Fill in the result view; returns the warnings"""
        job, found = self.job, self.found
        if found is None:
            return []
        places = 3 if self.units == "mm" else 4
        self.result_headline.setText(pj.describe_found(job, found, self.units))
        details, warnings = [], []
        if self.first is not None:
            difference = pj.spread(self.first, found)
            tolerance = REPEAT_TOLERANCE if self.units == "mm" else REPEAT_TOLERANCE / 25.4
            if difference > tolerance:
                warnings.append(f"The two touches differ by {difference:.{places}f} {self.u}. Something moved, or "
                                f"the tip or part is dirty: check before trusting it.")
            else:
                details.append(f"Both touches agree within {difference:.{places}f} {self.u}.")
        for warning in (pj.size_warning(job, found, self.units), self._camera_disagreement()):
            if warning:
                warnings.append(warning)
        if job.goal == "angle":
            rotation = self._target_rotation()
            details.append(f"To square {job.wcs} to it, rotate it to {rotation:.3f}° (that turns the work coordinates, "
                           f"not the part). Or tap the part square and probe again.")
            self.set_button.setText(f"Rotate {job.wcs} to {rotation:.3f}°")
            can_set = True
        else:
            new = self.new_offsets()
            current = self.machine.current_offset(job.wcs)
            can_set = new is not None and current is not None
            if not can_set:
                details.append("The work offsets can't be read right now, so it can't be set.")
            else:
                moves = ", ".join(f"{a} {value - current[0]['XYZ'.index(a)]:+.{places}f}" for a, value in new.items())
                details.append(f"Setting {job.wcs} " + " ".join(f"{a}0" for a in new) +
                               f" here moves its origin {moves} {self.u}.")
            self.set_button.setText(f"Set {job.wcs} " + " ".join(f"{a}0" for a in job.axes))
        self.result_details.setText(" ".join(details))
        self.result_warnings.setText("\n".join(f"⚠ {w}" for w in warnings))
        self.result_warnings.setVisible(bool(warnings))
        self.applied.setText(self.applied_label)
        self.applied.setVisible(bool(self.applied_label))
        self.set_button.setEnabled(can_set and not self.applied_label)
        return warnings

    def _camera_disagreement(self) -> Optional[str]:
        """The probed feature against where the camera saw it (machine XY), when it's far off"""
        part = self._camera_part()
        active = self.machine.current_offset(self.machine.wcs)
        found = self.found
        if part is None or active is None or found is None or "X" not in found.values or "Y" not in found.values:
            return None
        if self.job.goal == "corner":
            seen = None if self.job.inside else classify_corners(part).get(self.job.corner)
        elif self.job.goal in ("hole", "boss"):
            seen = part.center
        else:
            seen = None
        if seen is None:
            return None
        x, y = found.values["X"] + active[0][0], found.values["Y"] + active[0][1]
        off = ((x - seen[0]) ** 2 + (y - seen[1]) ** 2) ** 0.5
        limit = CAMERA_AGREEMENT if self.units == "mm" else CAMERA_AGREEMENT / 25.4
        if off > limit:
            return (f"That's {off:.1f} {self.u} from where the camera saw it. Fine if the part moved since the scan; "
                    f"otherwise check it's the feature you meant.")
        return None

    def apply_result(self):
        job, m = self.job, self.machine
        if self.found is None or self.applied_label:
            return False
        label = f"Probe {job.describe()}"
        if job.goal == "angle":
            rotation = self._target_rotation()
            ok = m.apply_offsets(job.wcs, {}, rotation=rotation, label=label)
            done = f"{job.wcs} rotated to {rotation:.3f}°."
        else:
            new = self.new_offsets()
            if new is None:
                return False
            ok = m.apply_offsets(job.wcs, new, label=label)
            done = f"{job.wcs} " + " ".join(f"{a}0" for a in new) + f" set on {job.describe()}."
        if ok:
            self.applied_label = done + " Undo puts the old one back."
            self.shell.toaster.show(done, "success")
            self._show_result()
            self.refresh()
        return ok

    def undo(self):
        entry = self.machine.undo_offsets()
        if entry:
            self.shell.toaster.show(f"Undid “{entry['label']}”: {entry['wcs']} is back as it was.", "success")
            self.applied_label = ""
            if self.stack.currentWidget() is self.result_view:
                self._show_result()
        self.refresh()

    # --- calibration ------------------------------------------------------------------------------------

    def calibrate(self):
        """Probe a ring gauge twice and work out the tip's effective diameter"""
        ring = float(self.shell.prefs.get("probe_ring_gauge", 25.0 if self.units == "mm" else 1.0))
        self._saved_job = self.job
        self.job = pj.ProbeJob(goal="hole", shape="round", diameter=ring, repeat=True)
        failing = [text for ok, text, _ in self.problems() if not ok]
        if failing:
            self.job = self._saved_job
            self.calibrate_status.setText(failing[0])
            return False
        self.calibrating, self.first, self.found = True, None, None
        self._tip_used = self.setup.tip_diameter
        self.calibrate_status.setText("Probing the ring gauge twice…")
        if not self._run():
            self.calibrating = False
            self.job = self._saved_job
            return False
        return True

    def _finish_calibration(self):
        ring = self.job.diameter
        self.calibrating = False
        self.job = self._saved_job
        measured = self.found.size[0]
        tip = pj.effective_tip(ring, measured, self._tip_used)
        self._new_tip = round(tip, 4)
        places = 3 if self.units == "mm" else 4
        spread = f" (the two runs agree within {pj.spread(self.first, self.found):.{places}f})" if self.first else ""
        self.calibrate_status.setText(f"The ring measured ⌀{measured:.{places}f} {self.u}{spread}, so the tip acts "
                                      f"like ⌀{tip:.{places}f} {self.u} (set to {self._tip_used:g} now).")
        self.save_tip_button.show()
        self._show_status("", C.text_3)
        self.refresh()

    def _save_tip(self):
        self._set_setup("tip_diameter", self._new_tip)
        for setup_row in self.setup_rows:
            setup_row.refresh()
        self.save_tip_button.hide()
        self.calibrate_status.setText(f"Tip saved as ⌀{self._new_tip:g} {self.u}.")

    # --- Milo and the pendant --------------------------------------------------------------------------

    def prepare(self, request: dict):
        """
        A probe request from Milo (voice) or the pendant: set up the job, show it, and say what
        would happen. Returns (action for the confirmation gate, None) or (None, why not yet).
        """
        goal = str(request.get("goal") or "").lower()
        if goal not in pj.PROBE_GOALS:
            return None, "What should I find: a corner, an edge, a hole or boss centre, the top, or a part's angle?"
        changes = {}
        corner = str(request.get("corner") or "").lower().replace("-", "_").replace(" ", "_")
        if corner in pj.CORNERS:
            changes["corner"] = corner
        if request.get("inside") is not None:
            changes["inside"] = bool(request["inside"])
        edge = str(request.get("edge") or "").lower()
        if edge in pj.EDGES:
            changes["edge"] = edge
        shape = str(request.get("shape") or "").lower()
        if shape in pj.SHAPES:
            changes["shape"] = shape
        for key in ("diameter", "width", "length"):
            try:
                if request.get(key):
                    changes[key] = abs(float(request[key]))
            except (TypeError, ValueError):
                pass
        wcs = str(request.get("wcs") or "").upper()
        if wcs in pj.WCS:
            changes["wcs"] = wcs
        self.shell.navigate("probe")
        self.select_goal(goal)
        self._set_job(**changes)
        failing = [text for ok, text, _ in self.problems() if not ok]
        if failing:
            reason = failing[0][0].lower() + failing[0][1:]
            return None, f"I've set up probing {self.job.describe()} on the Probe page. First: {reason}."
        return {"kind": "probe", "summary": pj.proposal_summary(self.job), "job": asdict(self.job)}, None

    def execute(self, action) -> str:
        """Run a confirmed probe action; the result is set unless something looks off (then it waits)"""
        job = action.get("job") or {}
        for f in fields(pj.ProbeJob):
            if f.name in job:
                setattr(self.job, f.name, job[f.name])
        self._save_job()
        self.shell.navigate("probe")
        self.select_goal(self.job.goal)
        if self.start(auto_apply=True):
            return f"[MILO] Probing {self.job.describe()}."
        return f"[MILO] Not probing: {self.status.text()}"

    def _show_status(self, text, color):
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)
