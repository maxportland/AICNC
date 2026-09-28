"""
Facing utility with multiple step-down passes.

Extends LinuxCNC's built-in facing utility (qtvcp.lib.gcode_utility.facing) rather than
editing it, since that file belongs to the LinuxCNC package. The original faces one pass
at Z0; this adds STEP DOWN and PASSES: the full facing pattern is cut at Z0, then again
one step down, and so on, returning to the start corner above the work between passes.
With PASSES = 1 the program is the same as the original's.
"""

from typing import List

from PyQt5 import QtCore, QtGui, QtWidgets

from qtvcp.lib.gcode_utility.facing import Facing as BaseFacing

MAX_PASSES = 100
LABEL_WIDTH = 104  # the original's 80 px cut off "FEED RATE", "STEPOVER" and "COMMENT"

HELP_EXTRA = """

STEP DOWN / PASSES
The whole facing pattern is repeated PASSES times, each pass STEP DOWN deeper.
The first pass is at Z0, so 3 passes with a 2 mm step down cut at Z0, Z-2 and Z-4
(4 mm total below Z0). Between passes the tool retracts, returns to the start
corner, and feeds down to the next depth. Leave PASSES at 1 for a single pass."""


def pass_depths(step_down: float, passes: int) -> List[float]:
    """Z level of each pass: 0, -step, -2*step, ..."""
    return [-i * step_down if i else 0.0 for i in range(passes)]


def format_z(z: float) -> str:
    return f"{z:.4f}".rstrip("0").rstrip(".") if z else "0.0"


class Facing(BaseFacing):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._stepdown_connected = False
        self.step_down = 0.0
        self.passes = 1
        self._add_stepdown_row()
        self._add_summary()
        self._widen_labels()
        self.mb.setText(self.mb.text() + HELP_EXTRA)

    # --- UI -----------------------------------------------------------------

    def _add_stepdown_row(self):
        """STEP DOWN [..] MM   PASSES [..]   (ok)  -- inserted under the STEPOVER row, same style"""
        stepover_row = self.lineEdit_stepover.parentWidget()
        column = stepover_row.parentWidget().layout()

        row = QtWidgets.QWidget()
        row.setObjectName("widget_stepdown")
        row.setMaximumHeight(40)
        layout = QtWidgets.QHBoxLayout(row)
        margins = stepover_row.layout().contentsMargins()
        layout.setContentsMargins(margins)
        layout.setSpacing(stepover_row.layout().spacing())

        self.lbl_stepdown = QtWidgets.QLabel("STEP DOWN")
        self.lbl_stepdown.setObjectName("lbl_stepdown")
        self.lbl_stepdown.setToolTip("Depth of each pass below the one before")
        self.lineEdit_stepdown = QtWidgets.QLineEdit()
        self.lineEdit_stepdown.setObjectName("lineEdit_stepdown")
        self.lineEdit_stepdown.setFixedWidth(70)
        self.lineEdit_stepdown.setAlignment(self.lineEdit_stepover.alignment())
        self.lbl_stepdown_unit = QtWidgets.QLabel("MM")
        self.lbl_stepdown_unit.setObjectName("lbl_stepdown_unit")
        self.lbl_passes = QtWidgets.QLabel("PASSES")
        self.lbl_passes.setObjectName("lbl_passes")
        self.lbl_passes.setToolTip("How many times to run the facing pattern (1 = a single pass at Z0)")
        self.lineEdit_passes = QtWidgets.QLineEdit()
        self.lineEdit_passes.setObjectName("lineEdit_passes")
        self.lineEdit_passes.setFixedWidth(50)
        self.lineEdit_passes.setAlignment(self.lineEdit_stepover.alignment())
        self.lineEdit_passes.setValidator(QtGui.QIntValidator(1, MAX_PASSES))
        self.lbl_stepdown_ok = QtWidgets.QLabel()
        self.lbl_stepdown_ok.setObjectName("lbl_stepdown_ok")
        self.lbl_stepdown_ok.setFixedSize(24, 24)
        self.lbl_stepdown_ok.setScaledContents(True)

        for widget in (self.lbl_stepdown, self.lineEdit_stepdown, self.lbl_stepdown_unit):
            layout.addWidget(widget)
        layout.addSpacing(18)
        layout.addWidget(self.lbl_passes)
        layout.addWidget(self.lineEdit_passes)
        layout.addStretch(1)
        layout.addWidget(self.lbl_stepdown_ok)
        column.insertWidget(column.indexOf(stepover_row) + 1, row)

    def _add_summary(self):
        """A line under the preview spelling out every pass depth"""
        self.lbl_passes_summary = QtWidgets.QLabel()
        self.lbl_passes_summary.setObjectName("lbl_passes_summary")
        self.lbl_passes_summary.setWordWrap(True)
        preview = self.lbl_image.parentWidget().layout()
        preview.insertWidget(preview.indexOf(self.lbl_image) + 1, self.lbl_passes_summary)

    def _widen_labels(self):
        for name in ("lbl_size", "lbl_spindle_rpm", "lbl_feedrate", "lbl_tool", "lbl_stepover", "lbl_units",
                     "lbl_raster", "lbl_comment", "lbl_blank"):
            label = getattr(self, name, None)
            if label is not None:
                label.setMinimumWidth(LABEL_WIDTH)
                label.setMaximumWidth(LABEL_WIDTH)
        self.lbl_stepdown.setFixedWidth(LABEL_WIDTH)

    # --- behavior ------------------------------------------------------------

    def init(self):
        super().init()
        self.lineEdit_stepdown.setText("2" if self.unit_code == "G21" else "0.08")
        self.lineEdit_passes.setText("1")
        if not self._stepdown_connected:
            # init() runs again on qtvcp's forced-update; connect only once
            self.lineEdit_stepdown.textChanged.connect(self.validate)
            self.lineEdit_passes.textChanged.connect(self.validate)
            self._stepdown_connected = True
        self.validate()

    def units_changed(self):
        super().units_changed()
        if hasattr(self, "lbl_stepdown_unit"):  # not yet during the base class's first call
            self.lbl_stepdown_unit.setText(self.lbl_stepover_unit.text())
            self.lineEdit_stepdown.setValidator(self.lineEdit_stepover.validator())

    def validate(self):
        super().validate()
        if not hasattr(self, "lineEdit_passes"):
            return  # the base class validates once before our widgets exist
        ok = True
        try:
            self.passes = int(self.lineEdit_passes.text())
            if not 1 <= self.passes <= MAX_PASSES:
                ok = False
        except ValueError:
            self.passes, ok = 1, False
        try:
            self.step_down = float(self.lineEdit_stepdown.text() or 0)
        except ValueError:
            self.step_down, ok = 0.0, False
        if self.passes > 1 and self.step_down <= 0:
            ok = False  # several passes need a real step down
        self.lbl_stepdown_ok.setPixmap(self.checked if ok else self.unchecked)
        if not ok:
            self.valid = False
        self._update_summary(ok)

    def _update_summary(self, ok: bool):
        unit = self.lbl_stepover_unit.text().lower()
        if not ok:
            self.lbl_passes_summary.setText("Set STEP DOWN greater than 0 for more than one pass "
                                            f"(PASSES 1 to {MAX_PASSES}).")
            return
        depths = pass_depths(self.step_down, self.passes)
        if self.passes == 1:
            self.lbl_passes_summary.setText("1 pass at Z0.")
            return
        listed = ", ".join(format_z(z) for z in depths)
        self.lbl_passes_summary.setText(f"{self.passes} passes at Z {listed}  ·  "
                                        f"{format_z(-depths[-1])} {unit} below Z0 in total")

    def calculate_toolpath(self, fname):
        """Same program as the original, with the facing pattern repeated at each pass depth"""
        depths = pass_depths(self.step_down, self.passes)
        comment = self.lineEdit_comment.text()
        self.line_num = 5
        self.file = open(fname, 'w')
        self.file.write("%\n")
        self.file.write("({})\n".format(comment))
        self.file.write("({})\n".format(self.units_text))
        self.file.write("(Area: X {} by Y {})\n".format(self.size_x, self.size_y))
        self.file.write("({} Tool Diameter with {} Stepover)\n".format(self.tool_dia, self.stepover))
        if self.passes > 1:
            self.file.write("({} passes, {} step down: Z {})\n".format(
                self.passes, self.step_down, ", ".join(format_z(z) for z in depths)))
        self.file.write("\n")
        self.next_line("{} G40 G49 G64 P0.03".format(self.unit_code))
        self.next_line("G17")
        self.next_line("G0 Z{}".format(self.safe_z))
        self.next_line("G0 X0.0 Y0.0")
        self.next_line("S{} M3".format(self.rpm))
        for number, z in enumerate(depths, start=1):
            if self.passes > 1:
                self.file.write("{}(Pass {} of {} at Z{})\n".format("\n" if number > 1 else "", number,
                                                                     self.passes, format_z(z)))
            self.next_line("G0 Z{}".format(self.safe_z / 2))
            if number > 1:
                # Back to the start corner above the work before going down to the next depth
                self.next_line("G0 X0.0 Y0.0")
            self.next_line("G1 Z{} F{}".format(format_z(z), self.feedrate / 2))
            if self.rbtn_raster_0.isChecked():
                self.raster_0()
            elif self.rbtn_raster_45.isChecked():
                self.raster_45()
            else:
                self.raster_90()
        self.next_line("G0 Z{}".format(self.safe_z))
        self.next_line("M2")
        self.file.write("%\n")
        self.file.close()
