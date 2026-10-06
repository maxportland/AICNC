"""
Runs a backlash measurement one stop at a time: back off to the approach start, come in slowly to
the stop from one side, wait until stopped, let it settle, then take the reading. Automatic
readings (the camera) come from `measure`; without it the runner waits for the operator to read
the indicator and submit() the number.

It never starts by itself: the page asks first. Stop, E-stop, machine off, or a move that doesn't
arrive ends it (and stops the machine).
"""

import time
from typing import Callable, List, Optional, Sequence

from PyQt5 import QtCore

import backlash as bl

SETTLE_S = 0.5
MOVE_TIMEOUT_S = 60.0
BACK_OFF_FEED = 1000.0  # mm/min: getting to where the final approach starts


class BacklashRunner(QtCore.QObject):
    progress = QtCore.pyqtSignal(int, int)      # readings taken, total
    awaiting = QtCore.pyqtSignal(int, object)   # index, Stop: read the indicator now
    finished = QtCore.pyqtSignal(bool, str)     # completed, message

    def __init__(self, machine, stops: Sequence[bl.Stop], feed: float,
                 measure: Optional[Callable[[bl.Stop], object]] = None, settle: float = SETTLE_S, parent=None):
        super().__init__(parent)
        self.machine, self.stops, self.feed, self.measure, self.settle = machine, list(stops), feed, measure, settle
        self.readings: List[bl.Reading] = []
        self.index = -1
        self.state = "idle"
        self._since = 0.0
        self._timer = QtCore.QTimer(self)
        self._timer.timeout.connect(self._tick)

    @property
    def running(self) -> bool:
        return self.state != "idle"

    def start(self):
        m = self.machine
        if not (m.on and not m.estop and m.all_homed and m.interp == "idle" and not m.is_running):
            self.finished.emit(False, "The machine must be on, homed and idle")
            return False
        if m.spindle_dir:
            self.finished.emit(False, "Stop the spindle first")
            return False
        self.readings, self.index = [], -1
        self._next()
        if self.running:
            self._timer.start(50)
        return self.running

    def cancel(self, message="Stopped"):
        if self.state in ("moving", "settling"):
            self.machine.abort()
        self._end(False, message)

    def submit(self, value):
        """The operator's indicator reading for the stop it's waiting at"""
        if self.state != "waiting":
            return False
        self._record(value)
        return True

    def _end(self, ok, message):
        self._timer.stop()
        was = self.state
        self.state = "idle"
        if was != "idle" or ok:
            self.finished.emit(ok, message)

    def _record(self, value):
        self.readings.append(bl.Reading(self.stops[self.index], value))
        self.progress.emit(len(self.readings), len(self.stops))
        self._next()

    def _next(self):
        self.index += 1
        if self.index >= len(self.stops):
            self.state = "measuring"  # so _end reports it
            return self._end(True, "Done")
        stop = self.stops[self.index]
        letter = bl.AXES[stop.axis]
        lines = [f"G90 G53 G1 {letter}{stop.start:.4f} F{max(self.feed, BACK_OFF_FEED):.0f}",
                 f"G90 G53 G1 {letter}{stop.target:.4f} F{self.feed:.0f}"]
        if not self.machine.mdi_lines(lines):
            self.state = "moving"
            return self._end(False, "The machine refused the move")
        self.state, self._since = "moving", time.time()

    def _target(self):
        stop = self.stops[self.index]
        where = [None, None, None]
        where[stop.axis] = stop.target
        return where

    def _tick(self):
        m = self.machine
        if m.estop or not m.on:
            return self._end(False, "The machine stopped")
        if self.state == "moving":
            if m.at_position(*self._target(), tolerance=0.005):
                self.state, self._since = "settling", time.time()
            elif time.time() - self._since > MOVE_TIMEOUT_S:
                self.cancel("A move didn't arrive in time")
        elif self.state == "settling" and time.time() - self._since >= self.settle:
            if self.measure is None:
                self.state = "waiting"
                self.awaiting.emit(self.index, self.stops[self.index])
                return
            self.state = "measuring"
            try:
                value = self.measure(self.stops[self.index])
            except Exception as e:  # a camera error ends the run, not the screen
                return self._end(False, str(e))
            if value is None:
                return self._end(False, "No reading")
            self._record(value)
