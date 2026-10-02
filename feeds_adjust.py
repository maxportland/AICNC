"""
Re-feeding a G-code program: new spindle speeds and feeds per tool, everything else untouched.

The AI only recommends numbers (rpm, cutting feed, plunge feed per tool). This module does
the rewriting, deterministically: it follows the program's modal state (active tool, motion
mode, feed), scales each tool's S words and feed moves by the ratio between the recommendation
and what the program used most, and writes F explicitly wherever the new effective feed
changes. Rapids, coordinates and comments stay as they are.

Programs it can't follow safely are refused (Unsupported): parameters and expressions,
subroutines, inverse-time or feed-per-rev feeds. Tools that tap or thread are left alone,
since their feed is tied to the thread pitch.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

_COMMENT_RE = re.compile(r"\([^)]*\)")
_WORD_RE = re.compile(r"([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))")
_F_RE = re.compile(r"F\s*[-+]?(?:\d+\.?\d*|\.\d+)", re.I)
_S_RE = re.compile(r"S\s*([-+]?(?:\d+\.?\d*|\.\d+))", re.I)

CUT_MOTIONS = {1.0, 2.0, 3.0}
CANNED = {73.0, 81.0, 82.0, 83.0, 85.0, 86.0, 89.0}  # drilling/boring cycles: F is the plunge feed
LOCKED = {33.0, 33.1, 74.0, 76.0, 84.0}  # threading/tapping: F follows the pitch
PROBE = 38.0


class Unsupported(Exception):
    """The program uses something this module won't rewrite"""


@dataclass
class ToolUse:
    tool: int
    rpm: Counter = field(default_factory=Counter)  # S value -> times set
    cut: Counter = field(default_factory=Counter)  # effective feed -> moves
    plunge: Counter = field(default_factory=Counter)
    locked: bool = False  # taps or threads: leave alone

    @staticmethod
    def usual(counter) -> Optional[float]:
        return counter.most_common(1)[0][0] if counter else None

    def summary(self) -> dict:
        return {"rpm": self.usual(self.rpm), "feed": self.usual(self.cut), "plunge": self.usual(self.plunge)}


def _split(line: str) -> Tuple[str, str]:
    """(code without comments, the ;-comment tail)"""
    code, semi, tail = line.partition(";")
    return _COMMENT_RE.sub(" ", code), (semi + tail)


def _words(code: str) -> List[Tuple[str, float]]:
    return [(letter, float(value)) for letter, value in _WORD_RE.findall(code.upper())]


def _check_supported(code: str, number: int):
    upper = code.upper()
    if "#" in upper or "[" in upper:
        raise Unsupported(f"line {number} uses parameters or expressions")
    if re.match(r"\s*O", upper):
        raise Unsupported(f"line {number} uses O-word subroutines or control flow")
    for letter, value in _words(upper):
        if letter == "G" and value in (93.0, 95.0, 96.0):
            raise Unsupported(f"line {number} uses G{value:g} (inverse-time, feed per revolution or constant "
                              "surface speed)")
        if letter == "M" and value in (98.0, 99.0):
            raise Unsupported(f"line {number} calls a subprogram (M{value:g})")


class _State:
    """The modal state that matters: tool, motion mode, feed, units"""

    def __init__(self, tool):
        self.tool = tool
        self.pending = None
        self.motion = None
        self.feed = None
        self.units = None

    def step(self, words):
        """Update from one line; returns (tool for this line's S/feeds, motion class or None, has axes)"""
        has_axes = any(letter in "XYZABCUVW" for letter, _ in words)
        gs = [value for letter, value in words if letter == "G"]
        ms = [value for letter, value in words if letter == "M"]
        for letter, value in words:
            if letter == "T":
                self.pending = int(value)
            if letter == "Q" and 61.0 in ms:
                self.tool = int(value)
        if 6.0 in ms and self.pending is not None:
            self.tool = self.pending
        for g in gs:
            if g == 20.0:
                self.units = "inch"
            elif g == 21.0:
                self.units = "mm"
            elif g in (0.0, 1.0, 2.0, 3.0, 80.0) or g in CANNED or g in LOCKED or int(g) == PROBE:
                self.motion = g
        for letter, value in words:
            if letter == "F":
                self.feed = value
        kind = None
        if has_axes and self.motion is not None:
            if self.motion in CANNED:
                kind = "plunge"
            elif self.motion in LOCKED:
                kind = "locked"
            elif int(self.motion) == PROBE:
                kind = "probe"
            elif self.motion in CUT_MOTIONS:
                xy = any(letter in "XYABCUVW" for letter, _ in words) or self.motion != 1.0
                kind = "cut" if xy else "plunge"
        return kind, has_axes


def analyze(text: str, start_tool: int = 0) -> Tuple[Dict[int, ToolUse], Optional[str]]:
    """Speeds and feeds each tool uses: ({tool: ToolUse}, program units or None)"""
    state = _State(start_tool)
    uses: Dict[int, ToolUse] = {}
    for number, line in enumerate(text.splitlines(), 1):
        code, _ = _split(line)
        if not code.strip():
            continue
        _check_supported(code, number)
        words = _words(code)
        kind, _ = state.step(words)
        use = uses.setdefault(state.tool, ToolUse(state.tool))
        for letter, value in words:
            if letter == "S":
                use.rpm[value] += 1
        if kind == "locked":
            use.locked = True
        elif kind in ("cut", "plunge") and state.feed:
            (use.cut if kind == "cut" else use.plunge)[state.feed] += 1
    return {t: u for t, u in uses.items() if u.rpm or u.cut or u.plunge or u.locked}, state.units


def _fmt(value: float) -> str:
    text = f"{value:.1f}".rstrip("0").rstrip(".")
    return text or "0"


def apply(text: str, targets: Dict[int, dict], start_tool: int = 0, max_rpm: Optional[float] = None,
          max_feed: Optional[float] = None) -> Tuple[str, Dict[int, dict]]:
    """
    Rewrite speeds and feeds. targets: {tool: {"rpm", "feed", "plunge"}} (any may be missing).

    Returns (new program, {tool: {"rpm": (old, new), "feed": (old, new), "plunge": (old, new)}})
    describing the usual values before and after.
    """
    uses, _ = analyze(text, start_tool)
    ratios = {}
    changes = {}
    for tool, use in uses.items():
        target = targets.get(tool)
        if not target or use.locked:
            continue
        usual = use.summary()
        ratio, change = {}, {}
        for key, counter_key in (("rpm", "rpm"), ("feed", "cut"), ("plunge", "plunge")):
            new, old = target.get(key), usual[key]
            if new and old:
                if key == "rpm" and max_rpm:
                    new = min(new, max_rpm)
                if key != "rpm" and max_feed:
                    new = min(new, max_feed)
                ratio[counter_key] = new / old
                change[key] = (old, new)
        ratios[tool], changes[tool] = ratio, change

    state = _State(start_tool)
    emitted_feed = None  # the feed in effect in the new program
    out = []
    for line in text.splitlines():
        code, _ = _split(line)
        if not code.strip():
            out.append(line)
            continue
        words = _words(code)
        kind, has_axes = state.step(words)
        ratio = ratios.get(state.tool, {})
        new_line = line
        if "rpm" in ratio and "S" in code.upper():
            def scale_s(match):
                value = float(match.group(1)) * ratio["rpm"]
                if max_rpm:
                    value = min(value, max_rpm)
                return f"S{value:.0f}"
            new_line = _replace_in_code(new_line, _S_RE, scale_s)
        if kind in ("cut", "plunge") and state.feed:
            factor = ratio.get("cut" if kind == "cut" else "plunge", 1.0)
            feed = round(state.feed * factor, 1)
            if max_feed and factor != 1.0:
                feed = min(feed, max_feed)
            if feed != emitted_feed:
                new_line = _set_feed(new_line, feed)
                emitted_feed = feed
            else:
                new_line = _replace_in_code(new_line, _F_RE, lambda m: "")
        elif kind in ("locked", "probe") and state.feed:
            # Taps, threads and probing keep their own feed
            if state.feed != emitted_feed:
                new_line = _set_feed(new_line, state.feed)
                emitted_feed = state.feed
        elif "F" in code.upper():
            # A feed set without a feed move (G0 line, "F300" alone): written where it's used instead
            new_line = _replace_in_code(new_line, _F_RE, lambda m: "")
        out.append(new_line.rstrip() if new_line != line else line)
    result = "\n".join(out)
    if text.endswith("\n"):
        result += "\n"
    return result, changes


def _replace_in_code(line: str, pattern, replacement) -> str:
    """Apply a regex replacement outside (comments) and ; comments"""
    code, semi, tail = line.partition(";")
    parts = re.split(r"(\([^)]*\))", code)
    for i in range(0, len(parts), 2):
        parts[i] = pattern.sub(replacement, parts[i])
    return "".join(parts) + semi + tail


def _set_feed(line: str, feed: float) -> str:
    code, _ = _split(line)
    if _F_RE.search(code):
        done = []

        def first_only(match):
            if done:
                return ""
            done.append(1)
            return f"F{_fmt(feed)}"
        return _replace_in_code(line, _F_RE, first_only)
    # Append F after the code, before any comment
    code, semi, tail = line.partition(";")
    match = re.search(r"\([^)]*\)\s*$", code)
    if match and code[:match.start()].strip():
        head, comment = code[:match.start()].rstrip(), " " + code[match.start():].strip()
    else:
        head, comment = code.rstrip(), ""
    return f"{head} F{_fmt(feed)}{comment}" + (" " + semi + tail if semi else "")
