"""
Runs camera routines on the machine one stop at a time: move, wait until stopped, let
vibration settle, grab a frame, hand it on. Used for the table scan and the calibration views.

It never runs by itself: the page asks the operator first. Abort (Stop, E-stop, machine off, or
a move that doesn't arrive) stops the machine and ends the routine.
"""

import time
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np
from PyQt5 import QtCore

SETTLE_S = 0.25
MOVE_TIMEOUT_S = 60.0


class StepRunner(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int)       # stops done, total
    frame = QtCore.pyqtSignal(object, object)    # image, spindle (x, y, z)
    finished = QtCore.pyqtSignal(bool, str)      # completed, message

    def __init__(self, machine, camera, views: Sequence[Tuple[float, float, float]], parent=None):
        super().__init__(parent)
        self.machine, self.camera = machine, camera
        self.views = list(views)
        self.index = -1
        self.state = "idle"
        self._since = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)

    def start(self):
        m = self.machine
        if not (m.on and not m.estop and m.all_homed and m.interp == "idle" and not m.is_running):
            self.finished.emit(False, "The machine must be on, homed and idle")
            return
        if m.spindle_dir:
            self.finished.emit(False, "Stop the spindle before using the camera")
            return
        self.index = -1
        self._next()
        self._timer.start(50)

    def cancel(self, message="Stopped"):
        if self.state in ("moving", "settling"):
            self.machine.abort()
        self._end(False, message)

    def _end(self, ok, message):
        self._timer.stop()
        self.state = "idle"
        self.finished.emit(ok, message)

    def _next(self):
        self.index += 1
        if self.index >= len(self.views):
            return self._end(True, "Done")
        x, y, z = self.views[self.index]
        if not self.machine.move_to(x, y, z):
            return self._end(False, "The machine refused the move")
        self.state, self._since = "moving", time.time()

    def _tick(self):
        m = self.machine
        if m.estop or not m.on:
            return self._end(False, "The machine stopped")
        x, y, z = self.views[self.index]
        if self.state == "moving":
            if m.at_position(x, y, z):
                self.state, self._since = "settling", time.time()
            elif time.time() - self._since > MOVE_TIMEOUT_S:
                self.cancel("A move didn't arrive in time")
        elif self.state == "settling" and time.time() - self._since >= SETTLE_S:
            image = self.camera.grab()
            if image is None:
                return self.cancel("The camera didn't return a picture")
            spindle = tuple(m.pos_abs[:3])
            self.frame.emit(image, spindle)
            self.progress.emit(self.index + 1, len(self.views))
            self._next()
