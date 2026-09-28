"""
Log sources for the Log Viewer panel: where each log lives, how to read it
incrementally, and how to tell errors from warnings from chatter.

No Qt here, so it can be tested on its own.
"""

import glob
import os
import re
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
AI_LOG_FILE = os.path.join(CONFIG_DIR, "ai_assistant.log")
AI_LOG_MAX_BYTES = 2 * 1024 * 1024  # then it's moved to ai_assistant.log.1 and a new file starts

ERROR, WARNING, INFO, DEBUG = "error", "warning", "info", "debug"
LEVELS = (ERROR, WARNING, INFO, DEBUG)

_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
# "2026-09-27 17:05:02,939 - QTvcp.QTVCP.QT_ACTION - INFO - Homing Joint: -1"
_PYLOG_RE = re.compile(r' - (CRITICAL|ERROR|WARNING|INFO|DEBUG) - ')
# "[QTvcp.QTVCP.QT_ISTAT][WARNING]  INI Parsing Error..." (after ANSI codes are stripped)
_BRACKET_RE = re.compile(r'^\[[^\]]*\]\[(CRITICAL|ERROR|WARNING|INFO|DEBUG)\]')
# AI Assistant log: "2026-09-27 17:40:01 [MILO] ..."
_AI_TAG_RE = re.compile(r'^\S+ \S+ \[([A-Z][A-Z0-9_-]*)\]')
_EXCEPTION_RE = re.compile(r'^[A-Za-z_][\w.]*(Error|Exception)\b')
_ERROR_WORDS_RE = re.compile(r'\b(error|exception|failed|failure|fault|tripped|abort(ed)?|critical|refused)\b', re.I)
_WARNING_WORDS_RE = re.compile(r'\b(warn(ing)?|limit|timeout|timed out|not found|missing)\b', re.I)
# Leading timestamp to dim: "2026-09-27 17:05:02,939" / "2026-09-27 17:40:01" / "Sun27 17:26:"
TIMESTAMP_RE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:[,.]\d+)?|[A-Z][a-z]{2}\d{1,2} \d\d:\d\d:?)')

_PY_LEVELS = {"CRITICAL": ERROR, "ERROR": ERROR, "WARNING": WARNING, "INFO": INFO, "DEBUG": DEBUG}
_AI_TAG_LEVELS = {
    "ERROR": ERROR, "WARN": WARNING, "TOOLS": WARNING,
    "USER": INFO, "MILO": INFO, "CONFIRM": INFO, "MDI": INFO, "REVIEW": INFO, "CAM": INFO,
    "SESSION": INFO, "CONTEXT": INFO,
}


def append_ai_log(message: str, path: Optional[str] = None):
    """Write one AI Assistant log line ("[TAG] message") with a timestamp; never raises"""
    import datetime
    path = path or AI_LOG_FILE  # looked up at call time so tests can redirect it
    try:
        if os.path.exists(path) and os.path.getsize(path) > AI_LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(path, "a", encoding="utf-8") as f:
            for i, line in enumerate(message.splitlines() or [""]):
                f.write(f"{stamp} {line}\n" if i == 0 else f"    {line}\n")
    except OSError:
        pass


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def is_continuation(line: str) -> bool:
    """Lines that belong to the record above (traceback frames, wrapped output)"""
    return line[:1] in (" ", "\t") and bool(line.strip())


def classify_line(line: str, previous: str = INFO) -> str:
    """Level of one log line; continuation lines take the level of the line above"""
    if is_continuation(line):
        return previous
    for pattern, levels in ((_BRACKET_RE, _PY_LEVELS), (_PYLOG_RE, _PY_LEVELS)):
        match = pattern.search(line)
        if match:
            return levels[match.group(1)]
    match = _AI_TAG_RE.match(line)
    if match:
        return _AI_TAG_LEVELS.get(match.group(1), DEBUG)
    if line.startswith("Traceback") or _EXCEPTION_RE.match(line):
        return ERROR
    if _ERROR_WORDS_RE.search(line):
        return ERROR
    if _WARNING_WORDS_RE.search(line):
        return WARNING
    return INFO


def parse_lines(lines: List[str], previous: str = INFO) -> List[Tuple[str, str]]:
    """[(level, text)] for a run of lines, continuing from the level of the line before them"""
    entries = []
    for raw in lines:
        # NUL bytes appear when a file is truncated while a writer keeps its old offset
        text = strip_ansi(raw.rstrip("\r\n")).replace("\x00", "")
        previous = classify_line(text, previous)
        entries.append((previous, text))
    return entries


class TailReader:
    """Reads a growing file incrementally, starting from the last `initial_bytes`.

    Handles the file being replaced or truncated (LinuxCNC rewrites some logs each run)
    by starting over from the beginning of the new file.
    """

    def __init__(self, path: Optional[str], initial_bytes: int = 512 * 1024):
        self.path = path
        self.initial_bytes = initial_bytes
        self._offset = None
        self._inode = None
        self._partial = ""

    def read_new(self) -> Tuple[List[str], bool]:
        """(new complete lines, reset) where reset means the file was replaced and lines start over"""
        if not self.path:
            return [], False
        try:
            st = os.stat(self.path)
        except OSError:
            return [], False
        reset = False
        if self._offset is None:
            self._offset = max(0, st.st_size - self.initial_bytes)
            self._inode = st.st_ino
            skip_partial_first = self._offset > 0
        elif st.st_ino != self._inode or st.st_size < self._offset:
            self._offset, self._inode, self._partial = 0, st.st_ino, ""
            skip_partial_first, reset = False, True
        else:
            skip_partial_first = False
        if st.st_size == self._offset:
            return [], reset
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(self._offset)
            data = f.read()
            self._offset = f.tell()
        text = self._partial + data
        lines = text.split("\n")
        self._partial = lines.pop()  # incomplete last line waits for the rest
        if skip_partial_first and lines:
            lines = lines[1:]  # started mid-file: the first line is a fragment
        return lines, reset

    def reset(self):
        self._offset = None
        self._partial = ""


@dataclass
class LogSource:
    key: str
    title: str
    description: str
    locate: Callable[[], Optional[str]]

    def path(self) -> Optional[str]:
        try:
            return self.locate()
        except Exception:
            return None


def _config_relative(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    path = os.path.expanduser(path)
    return path if os.path.isabs(path) else os.path.join(CONFIG_DIR, path)


def _qtvcp_info_path(attribute: str, default: str) -> Optional[str]:
    try:
        from qtvcp.core import Info
        return _config_relative(getattr(Info(), attribute, None) or default)
    except Exception:
        return _config_relative(default)


def _linuxcnc_output(fd: int, pattern: str) -> Optional[str]:
    """This run's LinuxCNC console file: where this process's stdout/stderr go, else the newest"""
    try:
        target = os.readlink(f"/proc/self/fd/{fd}")
        if os.path.basename(target).startswith(pattern) and os.path.isfile(target):
            return target
    except OSError:
        pass
    candidates = glob.glob(f"/tmp/{pattern}*")
    return max(candidates, key=os.path.getmtime) if candidates else None


SOURCES = [
    LogSource("machine", "Machine", "Machine events: power, E-stop, limits, tool changes",
              lambda: _qtvcp_info_path("MACHINE_LOG_HISTORY_PATH", "machine_log.dat")),
    LogSource("screen", "Screen", "Screen (qtvcp) log",
              lambda: _qtvcp_info_path("QTVCP_LOG_HISTORY_PATH", "ruckus_log.log")),
    LogSource("ai", "Milo", "Everything Milo logged, including technical details",
              lambda: globals()["AI_LOG_FILE"]),
    LogSource("linuxcnc", "LinuxCNC", "LinuxCNC console output for this run",
              lambda: _linuxcnc_output(1, "linuxcnc.print.")),
    LogSource("debug", "Debug", "LinuxCNC and QtVCP debug output for this run",
              lambda: _linuxcnc_output(2, "linuxcnc.debug.")),
]
