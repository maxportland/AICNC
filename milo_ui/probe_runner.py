"""
Runs one probe routine at a time: milo_probe_subprog.py (qtvcp's routines with protected descents)
in a subprocess, the same protocol qtvcp's BasicProbe widget uses. SimProbeRunner stands in for it
in previews and tests.
"""

import json
import os
import sys
from typing import Callable, Optional

from PyQt5 import QtCore

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUBPROGRAM = os.path.join(CONFIG_DIR, "milo_probe_subprog.py")
TIMEOUT_MS = 5 * 60 * 1000  # nothing a probe routine does takes this long


def parse_line(line: str):
    """('complete', results) / ('error', message) / (None, None) for one line of subprogram output"""
    line = line.strip()
    if line.startswith("COMPLETE$"):
        try:
            return "complete", json.loads(line.split("$", 1)[1])
        except ValueError:
            return "error", "The probe routine's results couldn't be read."
    if line.startswith("ERROR INFO"):
        message = line.split("Probe routine:", 1)[1].strip() if "Probe routine:" in line else line[10:].strip()
        if message.startswith("failed: "):
            message = message[8:]
        return "error", message
    if line.startswith("ERROR"):
        return "error", line[5:].strip() or "The probe routine stopped with an error."
    return None, None


class ProbeRunner(QtCore.QObject):
    finished = QtCore.pyqtSignal(dict)  # the routine's results (strings, work coordinates)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, program: str = SUBPROGRAM, python: str = sys.executable,
                 block_errors: Optional[Callable[[], None]] = None,
                 unblock_errors: Optional[Callable[[], None]] = None, parent=None):
        """block_errors/unblock_errors: stop the screen reading LinuxCNC's error channel while the
        subprogram runs (it reads the errors itself), as qtvcp's widget does"""
        super().__init__(parent)
        self.program, self.python = program, python
        self.block_errors = block_errors or (lambda: None)
        self.unblock_errors = unblock_errors or (lambda: None)
        self.proc = None
        self._buffer = ""
        self._done = False
        self._timer = QtCore.QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(lambda: self._end("The probe routine took too long and was stopped."))

    @property
    def busy(self) -> bool:
        return self.proc is not None

    def run(self, routine: str, params: dict) -> bool:
        if self.busy:
            return False
        self._buffer, self._done = "", False
        self.proc = QtCore.QProcess(self)
        self.proc.setProcessChannelMode(QtCore.QProcess.MergedChannels)
        self.proc.readyReadStandardOutput.connect(self._read)
        self.proc.finished.connect(self._finished)
        self.block_errors()
        self.proc.start(self.python, [self.program])
        self.proc.write(f"{routine}${json.dumps(params)}\n".encode())
        self._timer.start(TIMEOUT_MS)
        return True

    def stop(self):
        if self.proc is not None:
            self._end("Probing was stopped.")

    def _read(self):
        if self.proc is None:
            return
        self._buffer += bytes(self.proc.readAllStandardOutput()).decode("utf-8", "replace")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            kind, value = parse_line(line)
            if kind == "complete":
                self._end(None, value)
            elif kind == "error":
                self._end(value)

    def _finished(self, *_):
        self._read()
        self._end("The probe routine ended without a result. Is the machine on, homed and idle?")

    def _end(self, error, results=None):
        if self._done:
            return
        self._done = True
        self._timer.stop()
        proc, self.proc = self.proc, None
        if proc is not None and proc.state() != QtCore.QProcess.NotRunning:
            proc.kill()
            proc.waitForFinished(1000)
        self.unblock_errors()
        if error:
            self.failed.emit(error)
        else:
            self.finished.emit(results or {})


class SimProbeRunner(QtCore.QObject):
    """Pretend probing for previews and tests: answers after a moment with results near the
    simulated machine's position (or whatever `respond(routine, params)` returns)"""

    finished = QtCore.pyqtSignal(dict)
    failed = QtCore.pyqtSignal(str)

    def __init__(self, machine=None, respond: Optional[Callable[[str, dict], dict]] = None, delay_ms: int = 400,
                 parent=None):
        super().__init__(parent)
        self.machine, self.respond, self.delay_ms = machine, respond, delay_ms
        self.calls = []
        self._pending = False

    @property
    def busy(self) -> bool:
        return self._pending

    def run(self, routine: str, params: dict) -> bool:
        if self._pending:
            return False
        self._pending = True
        self.calls.append((routine, dict(params)))
        QtCore.QTimer.singleShot(self.delay_ms, lambda: self._answer(routine, params))
        return True

    def stop(self):
        if self._pending:
            self._pending = False
            self.failed.emit("Probing was stopped.")

    def _answer(self, routine, params):
        if not self._pending:
            return
        self._pending = False
        try:
            results = self.respond(routine, params) if self.respond else self._default(params)
        except Exception as e:
            return self.failed.emit(str(e))
        if isinstance(results, str):
            return self.failed.emit(results)
        self.finished.emit(results)

    def _default(self, params):
        x, y, z = (list(self.machine.pos_rel) + [0, 0, 0])[:3] if self.machine is not None else (0.0, 0.0, 0.0)
        width, length = float(params.get("x_hint_bp", 0)), float(params.get("y_hint_bp", 0))
        values = {"xp": x - 2.5, "xm": x + 2.5, "yp": y - 2.5, "ym": y + 2.5, "xc": x + 0.12, "yc": y - 0.08,
                  "lx": width + 0.01, "ly": length - 0.01, "d": float(params.get("diameter_hint", 0)) + 0.01,
                  "z": z - 3.0, "a": 0.214}
        return {k: f"{v:.3f}" for k, v in values.items()}
