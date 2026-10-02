"""
Vision: the camera on the head. Live view or the stitched table map with what was found,
table scans, calibration, and vision-guided probing (the camera finds the part, the probe
measures it and sets G54).
"""

import math
import os
import time
from typing import List, Optional

import numpy as np
from PyQt5 import QtCore, QtGui, QtWidgets
from PyQt5.QtCore import Qt

from milo_ui import theme, kit
from milo_ui.theme import C, T
from milo_ui.shell import Page
from milo_ui.pages.probe import ParamRow
from milo_ui.vision_runner import StepRunner
from milo_vision import geometry, scan as scanning, state as vstate
from milo_vision.geometry import CameraModel
from milo_vision.heightmap import HeightMap
from milo_vision.probe_plan import ProbeSettings, probe_program
from milo_vision import calibration, detect

PROBE_DIR = os.path.expanduser("~/linuxcnc/nc_files/milo")


def to_qimage(image: np.ndarray) -> QtGui.QImage:
    image = np.ascontiguousarray(image)
    h, w = image.shape[:2]
    if image.ndim == 2:
        return QtGui.QImage(image.data, w, h, w, QtGui.QImage.Format_Grayscale8).copy()
    return QtGui.QImage(image.data, w, h, 3 * w, QtGui.QImage.Format_BGR888).copy()


class VisionCanvas(QtWidgets.QWidget):
    """Shows a frame or the table map, scaled to fit, with overlays"""

    part_tapped = QtCore.pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image: Optional[QtGui.QImage] = None
        self.placeholder = ""
        self.overlay = None   # callable(painter, to_screen) drawing in image pixel coordinates
        self.hit_test = None  # callable(image_xy) -> part index or None
        self.setMinimumSize(400, 300)

    def set_image(self, image: Optional[QtGui.QImage], overlay=None, hit_test=None, placeholder=""):
        self.image, self.overlay, self.hit_test, self.placeholder = image, overlay, hit_test, placeholder
        self.update()

    def _fit(self):
        w, h = self.image.width(), self.image.height()
        scale = min(self.width() / w, self.height() / h)
        ox, oy = (self.width() - w * scale) / 2, (self.height() - h * scale) / 2
        return scale, ox, oy

    def paintEvent(self, event):
        p = QtGui.QPainter(self)
        p.setRenderHint(QtGui.QPainter.Antialiasing)
        p.setRenderHint(QtGui.QPainter.SmoothPixmapTransform)
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(self.rect()), 14, 14)
        p.setClipPath(path)
        p.fillRect(self.rect(), QtGui.QColor("#07090D"))
        if self.image is None:
            p.setPen(QtGui.QColor(C.text_3))
            p.setFont(theme.font(T.body))
            p.drawText(self.rect().adjusted(40, 0, -40, 0), Qt.AlignCenter | Qt.TextWordWrap, self.placeholder)
            return
        scale, ox, oy = self._fit()
        p.drawImage(QtCore.QRectF(ox, oy, self.image.width() * scale, self.image.height() * scale), self.image)
        if self.overlay:
            self.overlay(p, lambda x, y: QtCore.QPointF(ox + x * scale, oy + y * scale), scale)

    def mouseReleaseEvent(self, event):
        if self.image is None or self.hit_test is None:
            return
        scale, ox, oy = self._fit()
        index = self.hit_test(((event.x() - ox) / scale, (event.y() - oy) / scale))
        if index is not None:
            self.part_tapped.emit(index)


class VisionPage(Page):
    key = "vision"
    title = "Vision"
    icon = "camera"
    wants_stage = False

    def __init__(self, shell, vision_dir: str = geometry.VISION_DIR, parent=None):
        super().__init__(parent)
        self.shell = shell
        self.machine = shell.machine
        self.vision_dir = vision_dir
        self.camera_path = os.path.join(vision_dir, "camera.json")
        self.model = CameraModel.load(self.camera_path)
        self.camera = None
        self.camera_error = ""
        self.result = vstate.load(vision_dir)
        self.selected = 0
        self.runner: Optional[StepRunner] = None
        self.live_frame = None

        row = QtWidgets.QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(20)

        self.mode = kit.Segmented(["Table map", "Live"], min_width=120)
        self.mode.set_index(0)
        self.mode.selected.connect(lambda _: self.refresh_view())
        view_card = kit.Card(title="Camera", trailing=self.mode)
        self.canvas = VisionCanvas()
        self.canvas.part_tapped.connect(self._select)
        view_card.add(self.canvas, 1)
        self.caption = kit.label("", "muted")
        view_card.add(self.caption)
        row.addWidget(view_card, 1)

        side = QtWidgets.QVBoxLayout()
        side.setSpacing(16)
        # status
        self.status_card = kit.Card(title="Setup")
        self.status_text = kit.label("", "body", wrap=True)
        self.status_card.add(self.status_text)
        status_buttons = QtWidgets.QHBoxLayout()
        status_buttons.addWidget(kit.Button("Calibrate…", icon="crosshair", on_click=self._calibrate))
        status_buttons.addWidget(kit.Button("Hardware guide", icon="info", variant="ghost",
                                            on_click=self._guide))
        self.status_card.add(status_buttons)
        side.addWidget(self.status_card)
        # scan
        scan_card = kit.Card(title="Find parts")
        self.scan_button = kit.Button("Scan the table", icon="frame-corners", variant="primary", size="lg",
                                      on_click=self._ask_scan)
        scan_card.add(self.scan_button)
        self.scan_progress = QtWidgets.QProgressBar()
        self.scan_progress.setRange(0, 100)
        self.scan_progress.hide()
        scan_card.add(self.scan_progress)
        self.scan_stop = kit.Button("Stop scan", icon="stop-fill", variant="danger", on_click=lambda: self.runner and self.runner.cancel())
        self.scan_stop.hide()
        scan_card.add(self.scan_stop)
        scan_card.add(ParamRow("Camera height (machine Z)", lambda: float(shell.prefs.get("vision.scan_z", 0.0)),
                               lambda v: shell.prefs.set("vision.scan_z", min(0.0, v)), "mm"))
        side.addWidget(scan_card)
        # parts + probing
        self.parts_card = kit.Card(title="Found")
        self.parts_list = QtWidgets.QVBoxLayout()
        self.parts_list.setSpacing(8)
        self.parts_card.add(self.parts_list)
        self.origin = kit.Segmented(["Corner (X−Y−)", "Center"])
        self.origin.set_index(0)
        self.parts_card.add(self.origin)
        self.probe_button = kit.Button("Create probe program", icon="target", variant="primary", size="lg",
                                       on_click=self._make_probe_program)
        self.parts_card.add(self.probe_button)
        self.parts_card.add(kit.label("Probe tool and tip: Probe page → Probe setup.", "muted"))
        side.addWidget(self.parts_card)
        side.addStretch(1)
        holder = QtWidgets.QWidget()
        holder.setLayout(side)
        holder.setFixedWidth(560)
        row.addWidget(holder)

        self._live_timer = QtCore.QTimer(self)
        self._live_timer.timeout.connect(self._grab_live)
        self.machine.changed.connect(lambda t: t in ("state", "homing") and self.refresh_controls())
        self.refresh()

    # --- camera -----------------------------------------------------------------------------------

    def _ensure_camera(self):
        if self.camera is not None:
            return self.camera
        try:
            if type(self.machine).__name__ == "SimMachine":
                from milo_vision.cameras import SimCamera
                truth = CameraModel(rotation=1.5, offset_x=58.0, offset_y=-12.0, calibrated=True)
                self.camera = SimCamera(truth, lambda: self.machine.pos_abs)
            else:
                from milo_vision.cameras import open_camera
                self.camera = open_camera("auto")
                if self.camera is None:
                    self.camera_error = "No camera connected."
        except Exception as e:
            self.camera, self.camera_error = None, f"Camera error: {e}"
        return self.camera

    def on_show(self):
        self._ensure_camera()
        self.refresh()
        if self.mode.index() == 1:
            self._live_timer.start(300)

    def hideEvent(self, event):
        self._live_timer.stop()
        super().hideEvent(event)

    def _grab_live(self):
        if self.mode.index() != 1 or self.runner is not None or not self.isVisible():
            return
        cam = self._ensure_camera()
        if cam is not None:
            self.live_frame = cam.grab()
            self.refresh_view()

    # --- display ------------------------------------------------------------------------------------

    def refresh(self):
        self.refresh_controls()
        self.refresh_parts()
        self.refresh_view()

    def refresh_controls(self):
        m = self.machine
        cam = self.camera
        lines = [f"<b>{cam.name if cam else 'No camera'}</b>" + ("" if cam else f" · {self.camera_error or 'not opened yet'}")]
        if self.model.calibrated:
            lines.append(f"Calibrated · {self.model.mm_per_pixel(float(self.shell.prefs.get('vision.scan_z', 0.0))):.2f} mm per pixel "
                         f"at scan height")
        else:
            lines.append(f"<span style='color:{C.amber}'>Not calibrated</span>: positions are rough until you calibrate")
        self.status_text.setText("<br>".join(lines))
        ready = m.on and not m.estop and m.all_homed and not m.is_running and self.runner is None
        self.scan_button.setEnabled(ready and cam is not None)
        self.probe_button.setEnabled(bool(self.result and self.result.parts) and self.runner is None)

    def refresh_parts(self):
        kit.clear_layout(self.parts_list)
        r = self.result
        if r is None or not r.parts:
            self.parts_list.addWidget(kit.label("Nothing yet. Scan the table to find parts.", "muted", wrap=True))
            return
        age = (time.time() - r.finished_at) / 60
        self.parts_list.addWidget(kit.label(f"Scanned {age:.0f} min ago · {r.frames} photos", "muted"))
        for i, part in enumerate(r.parts[:5]):
            top = f" · top {part.top_z:.1f}" if part.top_z is not None else " · height unknown"
            button = kit.Button(f"{i + 1}.  {part.size[0]:.1f} × {part.size[1]:.1f} mm{top}", checkable=True)
            button.setChecked(i == self.selected)
            button.setStyleSheet("text-align: left; padding-left: 16px;")
            button.clicked.connect(lambda _=False, index=i: self._select(index))
            self.parts_list.addWidget(button)

    def _select(self, index):
        self.selected = index
        self.refresh_parts()
        self.refresh_view()

    def refresh_view(self):
        if self.mode.index() == 1:
            self._live_timer.start(300) if self.isVisible() else None
            frame = self.live_frame
            if frame is None:
                self.canvas.set_image(None, placeholder=self.camera_error or "Waiting for the camera…")
                return
            tags = detect.find_tags(frame)
            masks = [np.asarray(t.center) + (t.corners - np.asarray(t.center)) * scanning.TAG_QUIET_ZONE for t in tags]
            blobs = detect.find_stock(frame, exclude=masks)

            def overlay(p, at, scale):
                p.setPen(QtGui.QPen(QtGui.QColor(C.accent_2), 2))
                for blob in blobs:
                    p.drawPolygon(QtGui.QPolygonF([at(*pt) for pt in blob.polygon]))
                p.setPen(QtGui.QPen(QtGui.QColor(C.amber), 2))
                for tag in tags:
                    p.drawPolygon(QtGui.QPolygonF([at(*pt) for pt in tag.corners]))
                c = at(self.model.cx, self.model.cy)
                p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 120), 1))
                p.drawLine(QtCore.QPointF(c.x() - 14, c.y()), QtCore.QPointF(c.x() + 14, c.y()))
                p.drawLine(QtCore.QPointF(c.x(), c.y() - 14), QtCore.QPointF(c.x(), c.y() + 14))
            self.canvas.set_image(to_qimage(frame), overlay)
            self.caption.setText(f"{len(blobs)} bright region(s), {len(tags)} marker(s) in view")
            return
        self._live_timer.stop()
        r = self.result
        if r is None or r.mosaic is None:
            self.canvas.set_image(None, placeholder="Scan the table and the map of what's on it appears here.")
            self.caption.setText("")
            return
        origin, ppm, height = r.mosaic_origin, r.mosaic_px_per_mm, r.mosaic.shape[0]

        def to_px(x, y):
            return (x - origin[0]) * ppm, height - (y - origin[1]) * ppm

        def overlay(p, at, scale):
            for i, part in enumerate(r.parts):
                chosen = i == self.selected
                colour = QtGui.QColor(C.accent_hi if chosen else C.accent_2)
                p.setPen(QtGui.QPen(colour, 3 if chosen else 2))
                fill = QtGui.QColor(colour)
                fill.setAlpha(50 if chosen else 20)
                p.setBrush(fill)
                p.drawPolygon(QtGui.QPolygonF([at(*to_px(*c)) for c in part.corners]))
                label = at(*to_px(*part.center))
                p.setPen(QtGui.QColor("#FFFFFF"))
                p.setFont(theme.font(T.body, theme.SEMIBOLD))
                p.drawText(QtCore.QRectF(label.x() - 60, label.y() - 14, 120, 28), Qt.AlignCenter, str(i + 1))
            p.setBrush(Qt.NoBrush)
            p.setPen(QtGui.QPen(QtGui.QColor(C.amber), 2))
            for tag_id, (x, y) in r.tags.items():
                c = at(*to_px(x, y))
                p.drawEllipse(c, 6, 6)
            # where the spindle is now
            sx, sy = self.machine.pos_abs[0], self.machine.pos_abs[1]
            c = at(*to_px(sx, sy))
            p.setPen(QtGui.QPen(QtGui.QColor(C.red), 2))
            p.drawLine(QtCore.QPointF(c.x() - 10, c.y()), QtCore.QPointF(c.x() + 10, c.y()))
            p.drawLine(QtCore.QPointF(c.x(), c.y() - 10), QtCore.QPointF(c.x(), c.y() + 10))

        def hit(xy):
            x = origin[0] + xy[0] / ppm
            y = origin[1] + (height - xy[1]) / ppm
            from shapely.geometry import Point, Polygon
            for i, part in enumerate(r.parts):
                if Polygon(part.corners).buffer(3).contains(Point(x, y)):
                    return i
            return None

        self.canvas.set_image(to_qimage(r.mosaic), overlay, hit)
        self.caption.setText("Machine coordinates · tap a part to select it · red cross: spindle · "
                             "yellow: markers")

    # --- scanning -----------------------------------------------------------------------------------

    def _scan_z(self):
        return min(0.0, float(self.shell.prefs.get("vision.scan_z", 0.0)))

    def _ask_scan(self):
        m = self.machine
        views = scanning.plan_scan(m.limits, self.model, self._scan_z())
        kit.ActionSheet(self, "Scan the table?", [
            ("play-fill", "Start scan", lambda: self._scan(views), "warn"),
            ("x", "Cancel", lambda: None),
        ], subtitle=f"The head goes up to machine Z {self._scan_z():g} first, then visits {len(views)} spots over "
                    f"the table (plus two per part found). The spindle must be stopped.", width=600).show_centered()

    def _scan(self, views):
        self.table_map = scanning.TableMap(self.model, self.machine.limits)
        self._refined = False
        self._run(views, self.table_map.add_frame, self._scan_done)

    def _run(self, views, on_frame, on_done):
        self.runner = StepRunner(self.machine, self.camera, views, self)
        self.runner.frame.connect(on_frame)
        self.runner.progress.connect(lambda i, n: self.scan_progress.setValue(int(100 * i / max(1, n))))
        self.runner.finished.connect(on_done)
        self.scan_progress.setValue(0)
        self.scan_progress.show()
        self.scan_stop.show()
        self.refresh_controls()
        self.runner.start()

    def _scan_done(self, ok, message):
        self.runner = None
        if not ok:
            self._end_scan()
            return self.shell.toaster.show(f"Scan stopped: {message}", "warning")
        first = self.table_map.finish()
        if not self._refined and first.parts:
            # a closer look at each part from both sides gives its height by parallax
            self._refined = True
            views = []
            for part in first.parts[:4]:
                views += scanning.refine_views(part.center, self.model, self._scan_z(), self.machine.limits)
            return self._run(views, self.table_map.add_frame, self._scan_done)
        self.result = first
        vstate.save(first, self.vision_dir)
        self._save_heightmap(first)
        self._end_scan()
        self.selected = 0
        found = len(first.parts)
        self.shell.toaster.show(f"Scan done: {found} part{'s' if found != 1 else ''} found. "
                                "Milo knows about them now." if found else "Scan done: nothing found on the table.",
                                "success" if found else "info")
        self.refresh()

    def _end_scan(self):
        self.scan_progress.hide()
        self.scan_stop.hide()
        self.refresh_controls()

    def _save_heightmap(self, result):
        (x0, x1), (y0, y1) = self.machine.limits["X"], self.machine.limits["Y"]
        pad = 150.0
        hm = HeightMap((x0 - pad, x1 + pad), (y0 - pad, y1 + pad), 2.0)
        for i, part in enumerate(result.parts):
            if part.top_z is not None:
                hm.add_box(part.corners, part.top_z, f"part {i + 1}")
        hm.save(os.path.join(self.vision_dir, "heightmap.npz"))

    # --- probing ----------------------------------------------------------------------------------------

    def _make_probe_program(self):
        r = self.result
        if not r or not r.parts:
            return
        part = r.parts[min(self.selected, len(r.parts) - 1)]
        prefs = self.shell.prefs
        from probe_jobs import ProbeSetup
        probe = ProbeSetup.from_prefs(prefs)  # the same probe the Probe page uses
        settings = ProbeSettings(probe_tool=int(probe.tool), tip_diameter=float(probe.tip_diameter))
        origin = "corner" if self.origin.index() == 0 else "center"
        try:
            text = probe_program(part.center, part.size, part.angle, part.top_z, settings, origin,
                                 label=f"part {self.selected + 1}")
        except ValueError as e:
            return self.shell.toaster.show(str(e), "warning")
        os.makedirs(PROBE_DIR, exist_ok=True)
        path = os.path.join(PROBE_DIR, f"probe_part_{time.strftime('%Y%m%d_%H%M%S')}.ngc")
        with open(path, "w") as f:
            f.write(text)
        self.machine.open_program(path)
        self.shell.navigate("program")
        self.shell.toaster.show(f"Probe program loaded. Put the probe (T{settings.probe_tool}) in the spindle, "
                                "check the path, then press Start.", "success", seconds=10)

    # --- help & calibration ---------------------------------------------------------------------------

    def _guide(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "VISION_HARDWARE.md")
        pop = kit.Popover(self, title="Vision hardware", width=1100)
        view = QtWidgets.QTextBrowser()
        view.setMinimumHeight(700)
        try:
            with open(path) as f:
                view.setMarkdown(f.read())
        except OSError:
            view.setPlainText("VISION_HARDWARE.md is missing from the config folder.")
        QtWidgets.QScroller.grabGesture(view.viewport(), QtWidgets.QScroller.LeftMouseButtonGesture)
        pop.add(view)
        pop.show_centered()

    def _calibrate(self):
        CalibrationWizard(self).show_centered()


class CalibrationWizard(kit.Popover):
    """Lens photos -> intrinsics; a touched tag -> camera placement relative to the spindle"""

    def __init__(self, page: VisionPage):
        super().__init__(page, title="Calibrate the camera", width=900)
        self.page = page
        self.photos: List[np.ndarray] = []
        self.touch = None  # (x, y, spindle z, tip z)
        self.add(kit.eyebrow("1 · Lens (once per camera and focus setting)"))
        self.add(kit.label("Save the board image, print it at 100% and stick it to something flat. Hold it under "
                           "the camera at 15 different angles and spots, tapping Take photo for each.",
                           "muted", wrap=True))
        lens = QtWidgets.QHBoxLayout()
        lens.addWidget(kit.Button("Save board image", icon="download-simple", on_click=self._save_board))
        self.photo_button = kit.Button("Take photo (0)", icon="camera", on_click=self._photo)
        lens.addWidget(self.photo_button)
        lens.addWidget(kit.Button("Solve lens", icon="check", variant="primary", on_click=self._solve_lens))
        self.add(lens)
        self.add(kit.hline())
        self.add(kit.eyebrow("2 · Where the camera is (after mounting or bumping it)"))
        self.add(kit.label("Put a printed AprilTag (family 36h11) flat on the table. Jog a pointed tool so its tip "
                           "just touches the centre of the tag, then tap Use this point. Milo then raises the head "
                           "and photographs the tag from 18 spots.", "muted", wrap=True))
        place = QtWidgets.QHBoxLayout()
        place.addWidget(kit.Button("Use this point", icon="crosshair", on_click=self._touch))
        self.view_button = kit.Button("Photograph the tag", icon="play-fill", variant="warn", on_click=self._views)
        self.view_button.setEnabled(False)
        place.addWidget(self.view_button)
        self.add(place)
        self.result = kit.label("", "body", wrap=True)
        self.add(self.result)
        self.save_button = kit.Button("Save calibration", icon="floppy-disk", variant="primary", size="lg",
                                      on_click=self._save)
        self.save_button.setEnabled(False)
        self.add(self.save_button)
        self.solved: Optional[CameraModel] = None

    def _save_board(self):
        path = os.path.expanduser("~/linuxcnc/vision_calibration_board.png")
        calibration.write_charuco_png(path)
        self.page.shell.toaster.show(f"Saved {path.replace(os.path.expanduser('~'), '~')}: print at 100%, "
                                     "check a square measures 25 mm.", "success", seconds=10)

    def _photo(self):
        cam = self.page._ensure_camera()
        frame = cam.grab() if cam else None
        if frame is None:
            return self.page.shell.toaster.show("No picture from the camera.", "error")
        self.photos.append(frame)
        self.photo_button.setText(f"Take photo ({len(self.photos)})")

    def _solve_lens(self):
        try:
            model, error = calibration.calibrate_intrinsics(self.photos, self.page.model)
        except ValueError as e:
            return self.result.setText(str(e))
        self.page.model = model
        model.save(self.page.camera_path)
        self.result.setText(f"Lens solved: reprojection error {error:.2f} px "
                            f"({'good' if error < 0.5 else 'usable' if error < 1.0 else 'poor: retake the photos'}).")

    def _touch(self):
        m = self.page.machine
        tip_z = m.pos_abs[2] - (m.tool_length or 0.0)
        self.touch = (m.pos_abs[0], m.pos_abs[1], m.pos_abs[2], tip_z)
        self.view_button.setEnabled(True)
        self.result.setText(f"Tag centre at machine X {self.touch[0]:.3f} Y {self.touch[1]:.3f}; "
                            f"table at tip Z {tip_z:.3f}.")

    def _views(self):
        x, y, spindle_z, tip_z = self.touch
        model = self.page.model
        heights = [min(0.0, spindle_z + 120.0), min(0.0, spindle_z + 180.0)]
        heights = sorted(set(round(h, 3) for h in heights))
        views = calibration.placement_views((x, y), model, heights)
        self.observations = []

        def on_frame(image, spindle):
            tags = detect.find_tags(image)
            if tags:
                self.observations.append(calibration.Observation(spindle, tags[0].center, (x, y), tip_z))

        def done(ok, message):
            self.page.runner = None
            self.page._end_scan()
            if not ok:
                return self.result.setText(f"Stopped: {message}")
            try:
                fitted, rms = calibration.solve_placement(model, self.observations)
            except ValueError as e:
                return self.result.setText(str(e))
            from dataclasses import replace
            self.solved = replace(fitted, table_z=tip_z)
            quality = "good" if rms < 0.2 else "usable" if rms < 0.5 else "poor: check the tag is flat and the touch was centred"
            self.result.setText(f"Camera is {fitted.offset_x:.1f}, {fitted.offset_y:.1f} mm from the spindle, "
                                f"turned {fitted.rotation:.2f}°, {fitted.z_offset:.1f} mm above it. "
                                f"Fit error {rms:.3f} mm ({quality}), from {len(self.observations)} photos.")
            self.save_button.setEnabled(True)
        self.page._ensure_camera()
        self.page._run(views, on_frame, done)

    def _save(self):
        if self.solved is None:
            return
        self.page.model = self.solved
        self.solved.save(self.page.camera_path)
        self.page.refresh()
        self.page.shell.toaster.show("Camera calibration saved.", "success")
        self.close()
