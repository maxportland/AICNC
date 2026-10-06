"""
Calibrate: measure the machine and set what LinuxCNC compensates for.

Start from what you want to calibrate. Backlash measures the slack in X, Y or Z, either guided
(Milo moves the axis, you read a dial indicator) or automatically with the camera (an AprilTag
for X and Y, the line laser on the table for Z), and writes BACKLASH into the INI. The probe tip
and the camera are calibrated on their own pages; their tiles here lead there.

backlash.py holds the logic (the stops, the fit, the INI), backlash_runner.py moves the machine.
"""

import os
import time
from typing import Dict, Optional

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

import backlash as bl
from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.shell import Page
from milo_ui.pages.probe import CheckRow, GoalTile, ParamRow
from milo_ui.backlash_runner import BacklashRunner, SETTLE_S

GOALS = {
    "backlash": ("Backlash", "arrows-left-right", "Slack in X, Y and Z"),
    "probe_tip": ("Probe tip", "target", "Tip size on a ring gauge"),
    "camera": ("Camera", "camera", "Lens and where it's mounted"),
}
METHOD_NAMES = {"indicator": "dial indicator", "camera": "camera"}
FRAMES = 6            # camera frames averaged per stop
LASER_PAIRS = 3       # laser on/off pairs averaged per stop
SIM_LASH = {0: 0.06, 1: 0.04, 2: 0.10}  # mm: the simulated machine's slack

INDICATOR_SETUP = {
    "X": "Fix a dial indicator on its magnetic base on the column or the head, with the plunger along X against "
         "the end of the table (or a block clamped to it).",
    "Y": "Fix a dial indicator on its magnetic base on the column or the head, with the plunger along Y against "
         "the front of the table (or a block clamped to it).",
    "Z": "Stand the dial indicator's magnetic base on the table with the plunger pointing straight up against "
         "the spindle nose (or a flat on the head).",
}
INDICATOR_COMMON = ("Use a plunger indicator reading 0.01 mm or finer with at least {travel:.0f} mm of travel, and "
                    "press it in about half way. Milo moves {axis} back and forth around where it is now; at each "
                    "stop, read the indicator and type in what it shows. Don't re-zero it in between. Which way "
                    "the plunger points doesn't matter: Milo works it out.")
CAMERA_SETUP = {
    "XY": "Stick a printed AprilTag (36h11, like the camera calibration tag) flat on the table, and jog so it's "
          "near the middle of the camera's view. Lower the head as far as you safely can: closer is finer. "
          "Milo then moves {axis} back and forth and watches the tag. Nothing needs touching.",
    "Z": "Jog over a flat, clear bit of table where the laser line shows across the view, as low as it stays in "
         "view. Milo moves Z up and down and reads each height from the line, switching the laser on and off.",
}


def _ago(when: float) -> str:
    minutes = max(0, int((time.time() - when) // 60))
    if minutes < 1:
        return "just now"
    if minutes < 120:
        return f"{minutes} min ago"
    return time.strftime("%b %d %H:%M", time.localtime(when))


class CalibratePage(Page):
    key = "calibrate"
    title = "Calibrate"
    icon = "gauge"
    wants_stage = False

    def __init__(self, shell, camera=None, parent=None):
        super().__init__(parent)
        self.shell = shell
        m = self.machine = shell.machine
        prefs = shell.prefs
        self.settings = bl.Settings.from_prefs(prefs)
        self.method = prefs.get("calibrate.method", "indicator")
        self.method = self.method if self.method in bl.METHODS else "indicator"
        self.axis = prefs.get("calibrate.axis", "X")
        self.axis = self.axis if self.axis in bl.AXES else "X"
        self.goal = "backlash"
        # What LinuxCNC is running with: the INI as it was when the screen started
        self.active = bl.read_backlash(m.ini_path)
        self.results: Dict[str, bl.Result] = {}
        for axis, data in (prefs.get("calibrate.backlash_results") or {}).items():
            result = bl.Result.from_dict(data)
            if result is not None and axis in bl.AXES:
                self.results[axis] = result
        self.runner: Optional[BacklashRunner] = None
        self.settle = SETTLE_S  # seconds still at each stop before the reading
        self.camera = camera
        self.camera_error = ""
        self.live_frame = None
        self.live_tags = []
        self.live_line = None
        self.tag_id = None
        self._laser_reference = None  # the laser line's row in each column at the first stop
        self.lash_sim = None
        if type(m).__name__ == "SimMachine":
            # the pretend machine has slack, so a pretend measurement finds something
            self.lash_sim = bl.LashSim(SIM_LASH)
            m.position_changed.connect(lambda: self.lash_sim(m.pos_abs))

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)
        row.addWidget(self._build_goals())
        self.stack = QtWidgets.QStackedWidget()
        self.backlash_view = self._build_backlash_view()
        self.probe_tip_view = self._build_link_view(
            "Probe tip", "The tip of a touch probe triggers a little before its ball really touches, so it acts "
            "smaller than it is. Probe setup measures how much on a ring gauge. Do it again after changing the "
            "backlash compensation: the ring measurement includes the slack.",
            "Open Probe setup", "target", self._open_probe_setup)
        self.camera_view = self._build_link_view(
            "Camera", "The lens (from photos of a printed board) and where the camera sits relative to the "
            "spindle (from a touched AprilTag). Do it after mounting or bumping the camera.",
            "Calibrate the camera", "camera", self._open_camera_calibration)
        for view in (self.backlash_view, self.probe_tip_view, self.camera_view):
            self.stack.addWidget(view)
        row.addWidget(self.stack, 1)
        self.side = self._build_side()
        row.addWidget(self.side)

        self._live_timer = QtCore.QTimer(self)
        self._live_timer.timeout.connect(self._grab_live)
        m.changed.connect(self._on_machine_changed)
        m.position_changed.connect(self._refresh_checks)
        self.select_goal("backlash")

    # --- building ------------------------------------------------------------------------------------

    def _build_goals(self):
        card = kit.Card(title="What do you want to calibrate?")
        card.setFixedWidth(330)
        self.goal_group = QtWidgets.QButtonGroup(self)
        self.goal_group.setExclusive(True)
        self.goal_tiles = {}
        for key, entry in GOALS.items():
            tile = GoalTile(key, entry=entry)
            tile.clicked.connect(lambda _=False, k=key: self.select_goal(k))
            self.goal_group.addButton(tile)
            self.goal_tiles[key] = tile
            card.add(tile)
        card.body.addStretch(1)
        return card

    def _build_backlash_view(self):
        card = kit.Card(title="Backlash")
        card.add(kit.label("The slack between a screw and its nut: when an axis changes direction, the screw turns "
                           "a little before the table moves. Milo measures it from both sides of the same spot and "
                           "sets LinuxCNC's compensation for it (BACKLASH in the INI).", "muted", wrap=True))
        card.add(kit.eyebrow("How to measure"))
        self.method_toggle = kit.Segmented(["Dial indicator (guided)", "Camera (automatic)"])
        self.method_toggle.selected.connect(lambda i: self._set_method(bl.METHODS[i]))
        card.add(self.method_toggle)
        card.add(kit.eyebrow("Axis"))
        self.axis_toggle = kit.Segmented(list(bl.AXES), min_width=90)
        self.axis_toggle.selected.connect(lambda i: self._set_axis(bl.AXES[i]))
        axis_row = QtWidgets.QHBoxLayout()
        axis_row.addWidget(self.axis_toggle)
        axis_row.addStretch(1)
        card.add(axis_row)
        card.add(kit.eyebrow("Set it up"))
        self.setup_text = kit.label("", "body", wrap=True)
        card.add(self.setup_text)
        card.add(kit.eyebrow("Before measuring"))
        self.checks = [CheckRow() for _ in range(4)]
        for check in self.checks:
            card.add(check)
        card.add(kit.eyebrow("Measurement"))
        self.setting_rows = [
            ParamRow("Approach from (more than the slack)", lambda: self.settings.approach,
                     lambda v: self._set_setting(approach=max(0.1, abs(v))), "mm"),
            ParamRow("Test step", lambda: self.settings.step,
                     lambda v: self._set_setting(step=max(0.05, abs(v))), "mm"),
            ParamRow("Repeats", lambda: float(self.settings.repeats),
                     lambda v: self._set_setting(repeats=max(2, min(10, int(round(abs(v)))))), ""),
            ParamRow("Approach speed", lambda: self.settings.feed,
                     lambda v: self._set_setting(feed=max(20.0, min(2000.0, abs(v)))), "mm/min"),
        ]
        for setting_row in self.setting_rows:
            card.add(setting_row)
        card.body.addStretch(1)

        # The indicator reading the run is waiting for
        self.reading_card = kit.Card(title="Read the indicator", tone="accent", padding=16)
        self.reading_text = kit.label("", "value", wrap=True, size=T.body_lg)
        self.reading_card.add(self.reading_text)
        self.readings_so_far = kit.label("", "muted", mono=True, wrap=True)
        self.reading_card.add(self.readings_so_far)
        self.reading_button = kit.Button("Enter reading", icon="pencil-simple", variant="primary", size="lg",
                                         on_click=self._ask_reading)
        self.reading_card.add(self.reading_button)
        self.reading_card.hide()

        bar = kit.Card(padding=16)
        bottom = QtWidgets.QHBoxLayout()
        bottom.setSpacing(12)
        self.status = kit.label("", "muted", wrap=True)
        bottom.addWidget(self.status, 1)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setFixedWidth(160)
        self.progress.hide()
        bottom.addWidget(self.progress)
        self.stop_button = kit.Button("Stop", icon="stop", variant="danger", size="lg", on_click=self.stop)
        self.stop_button.hide()
        bottom.addWidget(self.stop_button)
        self.start_button = kit.Button("Measure X", icon="play-fill", variant="primary", size="lg", on_click=self.start)
        self.start_button.setMinimumWidth(240)
        bottom.addWidget(self.start_button)
        bar.add(bottom)

        view = QtWidgets.QWidget()
        column = QtWidgets.QVBoxLayout(view)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(12)
        column.addWidget(self._scrolled(card), 1)
        column.addWidget(self.reading_card)
        column.addWidget(bar)
        return view

    def _build_link_view(self, title, text, button, icon, action):
        card = kit.Card(title=title)
        card.add(kit.label(text, "body", wrap=True))
        row = QtWidgets.QHBoxLayout()
        row.addWidget(kit.Button(button, icon=icon, variant="primary", size="lg", on_click=action))
        row.addStretch(1)
        card.add(row)
        card.body.addStretch(1)
        return card

    def _build_side(self):
        from milo_ui.pages.vision import VisionCanvas
        holder = QtWidgets.QWidget()
        holder.setFixedWidth(560)
        side = QtWidgets.QVBoxLayout(holder)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(16)
        self.camera_card = kit.Card(title="Camera")
        self.canvas = VisionCanvas()
        self.canvas.setMinimumSize(400, 280)
        self.canvas.setFixedHeight(300)
        self.camera_card.add(self.canvas)
        self.camera_caption = kit.label("", "muted", wrap=True)
        self.camera_card.add(self.camera_caption)
        side.addWidget(self.camera_card)

        card = kit.Card(title="Results")
        self.axis_rows = {}
        for axis in bl.AXES:
            axis_row = QtWidgets.QHBoxLayout()
            axis_row.setSpacing(12)
            name = kit.label(axis, "value", size=T.body_lg, weight=theme.SEMIBOLD)
            name.setFixedWidth(28)
            axis_row.addWidget(name, 0, Qt.AlignTop)
            text = kit.label("", "body", wrap=True)
            axis_row.addWidget(text, 1)
            card.add(axis_row)
            self.axis_rows[axis] = text
        card.add(kit.hline())
        self.result_headline = kit.label("", "value", wrap=True, size=T.body)
        card.add(self.result_headline)
        self.result_details = kit.label("", "body", wrap=True)
        card.add(self.result_details)
        self.result_warnings = kit.label("", "body", wrap=True, color=C.amber)
        card.add(self.result_warnings)
        buttons = QtWidgets.QHBoxLayout()
        buttons.setSpacing(12)
        self.zero_button = kit.Button("Turn off", variant="ghost", on_click=self._ask_zero)
        buttons.addWidget(self.zero_button)
        buttons.addStretch(1)
        self.save_button = kit.Button("Save to LinuxCNC", icon="floppy-disk", variant="primary", size="lg",
                                      on_click=self._ask_save)
        buttons.addWidget(self.save_button)
        card.add(buttons)
        card.body.addStretch(1)
        side.addWidget(self._scrolled(card), 1)
        return holder

    def _scrolled(self, widget):
        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(widget)
        QtWidgets.QScroller.grabGesture(scroll.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        return scroll

    # --- choosing ------------------------------------------------------------------------------------

    def select_goal(self, goal):
        if self.running or goal not in GOALS:
            return
        self.goal_tiles[goal].setChecked(True)
        self.goal = goal
        self.stack.setCurrentWidget({"backlash": self.backlash_view, "probe_tip": self.probe_tip_view,
                                     "camera": self.camera_view}[goal])
        self.side.setVisible(goal == "backlash")
        self.refresh()

    def _set_method(self, method):
        if self.running:
            return self.method_toggle.set_index(bl.METHODS.index(self.method))
        self.method = method
        self.shell.prefs.set("calibrate.method", method)
        self.live_frame, self.live_tags, self.live_line = None, [], None
        if method == "camera":
            self._ensure_camera(retry=True)
        self.refresh()

    def _set_axis(self, axis):
        if self.running:
            return self.axis_toggle.set_index(bl.AXES.index(self.axis))
        self.axis = axis
        self.shell.prefs.set("calibrate.axis", axis)
        self.live_frame, self.live_tags, self.live_line = None, [], None
        self.refresh()

    def _set_setting(self, **changes):
        for key, value in changes.items():
            setattr(self.settings, key, value)
        self.settings.save(self.shell.prefs)
        self.refresh()

    def _open_probe_setup(self):
        self.shell.navigate("probe")
        probe = self.shell.pages.get("probe")
        if probe is not None:
            probe.select_goal("setup")

    def _open_camera_calibration(self):
        vision = self.shell.pages.get("vision")
        if vision is None:
            return
        self.shell.navigate("vision")
        vision._calibrate()

    # --- the camera ----------------------------------------------------------------------------------

    def _ensure_camera(self, retry=False):
        """The camera, opened on first use (after a failure, only tried again when retry)"""
        if self.camera is not None:
            return self.camera
        if self.camera_error and not retry:
            return None
        try:
            if self.lash_sim is not None:
                from milo_vision.cameras import SimCamera, SimScene
                from milo_vision.geometry import CameraModel
                truth = CameraModel(rotation=1.5, offset_x=58.0, offset_y=-12.0, calibrated=True)
                m = self.machine
                self.camera = SimCamera(truth, lambda: self.lash_sim(m.pos_abs), SimScene.fixture_plate(truth.table_z),
                                        mono=True, laser=lambda: m.laser_on)
            else:
                # the Vision page's camera: there's only one, and it can only be opened once
                vision = self.shell.pages.get("vision")
                self.camera = vision._ensure_camera() if vision is not None else None
                if self.camera is None:
                    self.camera_error = (vision.camera_error if vision is not None else "") or "No camera connected."
        except Exception as e:
            self.camera, self.camera_error = None, f"Camera error: {e}"
        return self.camera

    def _uses_camera(self):
        return self.goal == "backlash" and self.method == "camera"

    def _grab_live(self):
        if not self._uses_camera() or self.running or not self.isVisible():
            return
        cam = self._ensure_camera()
        if cam is None:
            return self._show_live()
        from milo_vision import detect, laser
        if self.axis == "Z":
            on, off = laser.capture_pair(cam, self.machine.set_laser, settle_frames=1)
            if on is None or off is None:
                return
            self.live_frame, self.live_tags = on, []
            self.live_line = laser.extract_line(on, background=off)
        else:
            frame = cam.grab()
            if frame is None:
                return
            self.live_frame, self.live_line = frame, None
            self.live_tags = detect.find_tags(frame)
        self._show_live()
        self._refresh_checks()

    def _pick_tag(self, tags, shape):
        """The tag being watched: the one nearest the middle of the view, then the same one"""
        if not tags:
            return None
        if self.tag_id is not None:
            same = [t for t in tags if t.id == self.tag_id]
            return same[0] if same else None
        h, w = shape[:2]
        return min(tags, key=lambda t: (t.center[0] - w / 2) ** 2 + (t.center[1] - h / 2) ** 2)

    def _measure_tag(self, stop):
        from milo_vision import detect
        cam = self._ensure_camera()
        if cam is None:
            raise RuntimeError(self.camera_error or "No camera")
        for _ in range(2):  # frames already in flight were taken while moving
            cam.grab()
        centres, frame, tags = [], None, []
        for _ in range(FRAMES):
            frame = cam.grab()
            if frame is None:
                continue
            tags = detect.find_tags(frame)
            tag = self._pick_tag(tags, frame.shape)
            if tag is not None:
                self.tag_id = tag.id
                centres.append(tag.center)
        self.live_frame, self.live_tags, self.live_line = frame, tags, None
        self._show_live()
        if len(centres) < FRAMES // 2:
            raise RuntimeError("The tag went out of view: put it nearer the middle and measure again")
        return tuple(float(v) for v in np.mean(np.asarray(centres), axis=0))

    def _measure_laser(self, stop):
        """How far the laser line has shifted since the first stop: the median over the columns of
        each column's own shift, so a column crossing a part doesn't mix with one on the table"""
        from milo_vision import laser
        cam = self._ensure_camera()
        if cam is None:
            raise RuntimeError(self.camera_error or "No camera")
        lines = []
        for _ in range(LASER_PAIRS):
            on, off = laser.capture_pair(cam, self.machine.set_laser)
            if on is None or off is None:
                continue
            line = laser.extract_line(on, background=off)
            self.live_frame, self.live_line, self.live_tags = on, line, []
            if np.isfinite(line).sum() >= 20:
                lines.append(line)
        self._show_live()
        if not lines:
            raise RuntimeError("The laser line wasn't found: jog over flat, clear table and measure again")
        with np.errstate(all="ignore"):
            rows = np.nanmean(np.asarray(lines), axis=0)
        if self._laser_reference is None:
            self._laser_reference = rows
        shift = rows - self._laser_reference
        shift = shift[np.isfinite(shift)]
        if shift.size < 20:
            raise RuntimeError("The laser line moved out of view: measure over a bigger flat area")
        return float(np.median(shift))

    def _show_live(self):
        if not self._uses_camera():
            return
        from milo_ui.pages.vision import to_qimage
        frame = self.live_frame
        if frame is None:
            text = self.camera_error if self.camera is None and self.camera_error else "Waiting for the camera…"
            self.canvas.set_image(None, placeholder=text)
            self.camera_caption.setText("")
            return
        tags, line, watched = self.live_tags, self.live_line, self.tag_id

        def overlay(p, at, scale):
            for tag in tags:
                chosen = watched is None or tag.id == watched
                p.setPen(QtGui.QPen(QtGui.QColor(C.green if chosen else C.amber), 3 if chosen else 2))
                p.drawPolygon(QtGui.QPolygonF([at(*pt) for pt in tag.corners]))
            if line is not None:
                p.setPen(QtGui.QPen(QtGui.QColor(C.green), 2))
                cols = np.nonzero(np.isfinite(line))[0]
                for a, b in zip(cols[:-1:4], cols[4::4]):
                    if b - a <= 8:
                        p.drawLine(at(a, line[a]), at(b, line[b]))
        self.canvas.set_image(to_qimage(frame), overlay)
        if self.axis == "Z":
            found = 0 if line is None else int(np.isfinite(line).sum())
            self.camera_caption.setText(f"Laser line in {100 * found / max(1, frame.shape[1]):.0f}% of the view"
                                        if found else "No laser line in view")
        else:
            self.camera_caption.setText(f"{len(tags)} tag(s) in view" + (f" · watching tag {watched}"
                                                                          if watched is not None else ""))

    # --- checks --------------------------------------------------------------------------------------

    def _stops(self):
        i = bl.AXES.index(self.axis)
        return bl.plan(i, round(self.machine.pos_abs[i], 4), self.settings)

    def problems(self):
        """(ok, text) for each thing that has to be right before measuring"""
        m = self.machine
        rows = []
        ready = m.on and not m.estop and m.all_homed and m.interp == "idle" and not m.is_running
        rows.append((ready, "Machine on, homed and idle" if ready else (m.state_detail or "The machine isn't ready")))
        rows.append((not m.spindle_dir, "Spindle stopped" if not m.spindle_dir else "Stop the spindle first"))
        if self.method == "camera":
            if self._ensure_camera() is None:
                rows.append((False, self.camera_error or "No camera connected"))
            elif self.axis == "Z":
                line = self.live_line
                seen = line is not None and int(np.isfinite(line).sum()) >= 20
                rows.append((seen, "The laser line is in view" if seen else
                             "Jog over flat, clear table until the laser line shows in the camera view"))
            else:
                seen = bool(self.live_tags)
                rows.append((seen, "A tag is in view" if seen else
                             "Jog so an AprilTag is in the camera view (near the middle)"))
        if not m.machine_metric:
            rows.append((False, "The measurement is in mm: this machine's INI is in inches"))
        else:
            problem = bl.limit_problem(self._stops(), m.limits)
            rows.append((problem is None, problem or "Room to move both ways within the soft limits"))
        return rows

    def can_start(self):
        return not self.running and all(ok for ok, _ in self.problems())

    @property
    def running(self) -> bool:
        return self.runner is not None and self.runner.running

    # --- refreshing ----------------------------------------------------------------------------------

    def refresh(self):
        if self.goal != "backlash":
            self._live_timer.stop()
            return
        self.method_toggle.set_index(bl.METHODS.index(self.method))
        self.axis_toggle.set_index(bl.AXES.index(self.axis))
        stops = self._stops()
        lo, hi = bl.travel(stops)
        if self.method == "indicator":
            self.setup_text.setText(INDICATOR_SETUP[self.axis] + " " +
                                    INDICATOR_COMMON.format(travel=max(2.0, (hi - lo) + 1.0), axis=self.axis))
        else:
            self.setup_text.setText(CAMERA_SETUP["Z" if self.axis == "Z" else "XY"].format(axis=self.axis))
        for setting_row in self.setting_rows:
            setting_row.refresh()
        self.camera_card.setVisible(self._uses_camera())
        if self._uses_camera() and self.isVisible() and not self.running:
            self._live_timer.start(400)
        elif not self.running:
            self._live_timer.stop()
            if not self._uses_camera():
                self.machine.set_laser(False)
        self._show_live()
        self._refresh_checks()
        self._refresh_results()

    def _refresh_checks(self):
        if self.goal != "backlash" or not self.isVisible():
            return
        rows = self.problems()
        for i, check in enumerate(self.checks):
            check.setVisible(i < len(rows))
            if i < len(rows):
                check.set(*rows[i])
        self.start_button.setText(f"Measure {self.axis}")
        self.start_button.setEnabled(self.can_start())
        self.start_button.setVisible(not self.running)
        self.stop_button.setVisible(self.running)
        self.progress.setVisible(self.running)

    def _saved(self) -> Dict[str, float]:
        """BACKLASH in the INI file now (LinuxCNC uses it from its next start)"""
        return bl.read_backlash(self.machine.ini_path)

    def _refresh_results(self):
        saved = self._saved()
        for axis, text in self.axis_rows.items():
            running = self.active.get(axis, 0.0)
            words = [f"Compensating {running:.3f} mm" if running else "No compensation"]
            if abs(saved.get(axis, running) - running) > 1e-6:
                words.append(f"<span style='color:{C.amber}'>saved {saved[axis]:.3f}: restart LinuxCNC to use "
                             f"it</span>")
            r = self.results.get(axis)
            if r is not None:
                words.append(f"measured {r.backlash:+.3f} mm ±{r.spread / 2:.3f} with the "
                             f"{METHOD_NAMES.get(r.method, r.method)}, {_ago(r.when)}")
            text.setText(" · ".join(words))
        r = self.results.get(self.axis)
        axis = self.axis
        if r is None:
            self.result_headline.setText(f"{axis} hasn't been measured yet.")
            self.result_details.setText("")
            self.result_warnings.hide()
        else:
            if r.active:
                self.result_headline.setText(f"{axis} still has {r.backlash:.3f} mm of slack with {r.active:.3f} mm "
                                             f"compensation running, so it needs {r.suggested:.3f} mm.")
            else:
                self.result_headline.setText(f"{axis} has {r.backlash:.3f} mm of backlash.")
            details = [f"The {len(r.repeats)} repeats: " + ", ".join(f"{v:.3f}" for v in r.repeats) + " mm."]
            if r.method == "camera":
                unit = "laser rows" if axis == "Z" else "pixels"
                details.append(f"The camera saw {abs(r.scale):.1f} {unit} per mm.")
            self.result_details.setText(" ".join(details))
            self.result_warnings.setText("\n".join(f"⚠ {w}" for w in r.warnings))
            self.result_warnings.setVisible(bool(r.warnings))
        pending = r is not None and abs(r.suggested - saved.get(axis, 0.0)) > 0.0005
        self.save_button.setText(f"Set {axis} to {r.suggested:.3f} mm" if pending else "Save to LinuxCNC")
        self.save_button.setEnabled(pending and not self.running)
        self.zero_button.setText(f"Turn off {axis}")
        self.zero_button.setVisible(saved.get(axis, 0.0) > 0 and not self.running)

    def _on_machine_changed(self, topic):
        if topic in ("state", "homing", "spindle") and self.goal == "backlash":
            self._refresh_checks()

    def on_show(self):
        if self._uses_camera():
            self._ensure_camera(retry=True)
        self.refresh()

    def hideEvent(self, event):
        self._live_timer.stop()
        if not self.running:
            self.machine.set_laser(False)
        super().hideEvent(event)

    # --- running -------------------------------------------------------------------------------------

    def start(self):
        if self.running:
            return False
        failing = [text for ok, text in self.problems() if not ok]
        if failing:
            self._show_status(failing[0], C.amber)
            self._refresh_checks()
            return False
        self._live_timer.stop()
        self.tag_id = None
        self._laser_reference = None
        measure = None
        if self.method == "camera":
            measure = self._measure_laser if self.axis == "Z" else self._measure_tag
        self._run_method, self._run_axis = self.method, self.axis
        self._run_active = self.active.get(self.axis, 0.0)
        self.runner = BacklashRunner(self.machine, self._stops(), self.settings.feed, measure,
                                     settle=self.settle, parent=self)
        self.runner.progress.connect(self._on_progress)
        self.runner.awaiting.connect(self._on_awaiting)
        self.runner.finished.connect(self._on_finished)
        self.progress.setValue(0)
        if not self.runner.start():
            return False
        self._show_status("Measuring… hands clear of the table." if measure else
                          "Moving to the first stop…", C.text_2)
        self._refresh_checks()
        self._refresh_results()
        return True

    def stop(self):
        if self.runner is not None:
            self.runner.cancel("Stopped")

    def _on_progress(self, done, total):
        self.progress.setValue(int(100 * done / max(1, total)))
        readings = self.runner.readings if self.runner else []
        if self._run_method == "indicator":
            self.readings_so_far.setText("  ".join(f"{r.value:+.3f}" for r in readings))
            self.reading_card.hide()
            self._show_status(f"Moving to stop {done + 1} of {total}…" if done < total else "", C.text_2)

    def _on_awaiting(self, index, stop):
        total = len(self.runner.stops)
        self.reading_card.title_label.setText(f"READING {index + 1} OF {total}")
        self.reading_text.setText(f"{stop.describe()}. Read the indicator and enter what it shows (mm).")
        self.reading_card.show()
        self._show_status("Waiting for the indicator reading.", C.text_2)

    def _ask_reading(self):
        if self.runner is None or self.runner.state != "waiting":
            return
        kit.NumPad(self, f"Indicator reading {self.runner.index + 1}", self._submit, units="mm",
                   hint="As the dial shows it, with its sign (− below zero).").show_centered()

    def _submit(self, value):
        if self.runner is not None and self.runner.submit(float(value)):
            self.reading_card.hide()

    def _on_finished(self, ok, message):
        runner = self.runner
        self.reading_card.hide()
        self.machine.set_laser(False)
        if not ok:
            self._show_status(f"Measurement stopped: {message}", C.red if message != "Stopped" else C.amber)
            self.refresh()
            return
        try:
            result = bl.analyse(runner.readings, self.settings, self._run_method, active=self._run_active)
        except ValueError as e:
            self._show_status(f"Couldn't work it out: {e}", C.red)
            self.refresh()
            return
        self.results[self._run_axis] = result
        self.shell.prefs.set("calibrate.backlash_results", {a: r.to_dict() for a, r in self.results.items()})
        self._show_status(f"{self._run_axis} measured.", C.green)
        self.refresh()

    # --- saving --------------------------------------------------------------------------------------

    def _ask_save(self):
        r = self.results.get(self.axis)
        if r is None:
            return
        self._confirm_write({self.axis: r.suggested}, METHOD_NAMES.get(r.method, r.method))

    def _ask_zero(self):
        self._confirm_write({self.axis: 0.0}, "turned off")

    def _confirm_write(self, values, method):
        path = self.machine.ini_path
        lines = [f"{axis}: BACKLASH = {value:.4f} mm" for axis, value in values.items()]
        warn = ""
        r = self.results.get(self.axis)
        if r is not None and r.warnings and any(v > 0 for v in values.values()):
            warn = " The measurement has warnings: read them first."
        kit.ActionSheet(self, "Save to LinuxCNC?", [
            ("floppy-disk", "Save", lambda: self._write(values, method), "primary"),
            ("x", "Cancel", lambda: None),
        ], subtitle=f"{'; '.join(lines)} in {os.path.basename(path)} (a copy of it goes to backups/ first). The "
                    f"step generator's acceleration is raised to twice the axis's if it needs the room. LinuxCNC "
                    f"reads it when it next starts.{warn}", width=640).show_centered()

    def _write(self, values, method):
        path = self.machine.ini_path
        try:
            changes = bl.apply(path, values, backup_dir=os.path.join(os.path.dirname(path), "backups"),
                               method=method)
        except (OSError, ValueError) as e:
            self.shell.toaster.show(f"Couldn't save: {e}", "error")
            return False
        self.refresh()
        if not changes:
            self.shell.toaster.show("Nothing to change: the INI already has it.", "info")
            return True
        self.shell.toaster.show("Saved: " + "; ".join(changes) + ". Restart LinuxCNC to use it, then calibrate "
                                "the probe tip again (its ring measurement included the old slack).", "success",
                                seconds=14)
        return True

    def _show_status(self, text, color):
        self.status.setStyleSheet(f"color: {color};")
        self.status.setText(text)
