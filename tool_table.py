"""
Tool table utilities.

Reads LinuxCNC's tool table (word format: "T8 P0 D+6.35 Z+33.2 ;1/4 4-Flute")
and formats it for AI prompts. Diameters that can't be right for the machine's
units are flagged instead of passed on, so the model and the CAM processor
never trust a bad value.
"""

import os
import re
from typing import Dict, List, Optional, Tuple

# A diameter outside this range (in machine units) is almost certainly entered
# in the wrong units, e.g. 0.25 in a mm table for a 1/4" endmill.
PLAUSIBLE_DIAMETER = {"mm": (0.5, 100.0), "inch": (0.02, 4.0)}

_WORD_RE = re.compile(r'([A-Za-z])\s*([-+]?(?:\d+\.?\d*|\.\d+))')
_FLUTES_RE = re.compile(r'(\d+)\s*-?\s*(?:flute|fl\b)', re.IGNORECASE)


def _tool_type(description: str) -> str:
    text = description.lower()
    if "ball" in text:
        return "ball_endmill"
    if "drill" in text:
        return "drill"
    return "endmill"


def read_tool_table(path: str, machine_units: str = "mm") -> Tuple[List[dict], List[str]]:
    """
    Parse a LinuxCNC tool table.

    Returns:
        (tools, warnings). Each tool is a dict with tool, description, type,
        diameter (machine units, or None if missing/implausible), flutes (or None),
        and usable (False for T0 and duplicated tool numbers).
    """
    tools: List[dict] = []
    warnings: List[str] = []
    lo, hi = PLAUSIBLE_DIAMETER.get(machine_units, PLAUSIBLE_DIAMETER["mm"])

    with open(path, "r") as f:
        for line in f:
            data, _, comment = line.partition(";")
            words = {letter.upper(): float(value) for letter, value in _WORD_RE.findall(data)}
            if "T" not in words:
                continue
            number = int(words["T"])
            description = comment.strip() or f"Tool {number}"
            diameter = words.get("D")
            if diameter is not None and not lo <= abs(diameter) <= hi:
                warnings.append(f"T{number} {description}: diameter {diameter:g} looks wrong for a "
                                f"{machine_units} table (entered in {'inches' if machine_units == 'mm' else 'mm'}?)")
                diameter = None
            flutes = _FLUTES_RE.search(description)
            tools.append({
                "tool": number,
                "description": description,
                "type": _tool_type(description),
                "diameter": abs(diameter) if diameter is not None else None,
                "flutes": int(flutes.group(1)) if flutes else None,
                "usable": True,
            })

    counts: Dict[int, int] = {}
    for t in tools:
        counts[t["tool"]] = counts.get(t["tool"], 0) + 1
    for number, count in sorted(counts.items()):
        if count > 1:
            warnings.append(f"T{number} is listed {count} times; not used until it's unique")
    for t in tools:
        if counts[t["tool"]] > 1:
            t["usable"] = False
        if t["tool"] == 0:
            # In LinuxCNC T0 means "no tool in the spindle"
            t["usable"] = False
    return tools, warnings


def usable_tools(tools: List[dict]) -> Dict[int, Optional[float]]:
    """Usable tool number -> trusted diameter in machine units (None if not recorded or implausible)"""
    return {t["tool"]: t["diameter"] for t in tools if t["usable"]}


def format_tools_for_prompt(tools: List[dict], machine_units: str = "mm") -> str:
    """Tool list for the CAM system prompt"""
    usable = [t for t in tools if t["usable"]]
    if not usable:
        return ("\nAVAILABLE TOOLS: No usable tools in the machine's tool table. Include an \"error\" field "
                "explaining that tools must be added before programs can be generated.\n")
    text = "\nAVAILABLE TOOLS (use ONLY these tools from the machine's tool table):\n"
    for t in sorted(usable, key=lambda t: t["tool"]):
        details = [t["type"]]
        if t["diameter"] is not None:
            details.append(f"diameter {t['diameter']:g} {machine_units} (from the tool table; use exactly this)")
        else:
            details.append("diameter not recorded in the tool table; infer it from the description")
        if t["flutes"] is not None:
            details.append(f"{t['flutes']} flutes")
        text += f"- Tool {t['tool']}: {t['description']} ({', '.join(details)})\n"
    text += ("\nIMPORTANT: You MUST only use tool numbers from the list above. "
             "Do not create or suggest tools that are not in the machine's tool table.\n")
    return text


def get_tool_table(config_path: str, machine_units: str = "mm",
                   log_callback=None) -> Tuple[str, Optional[Dict[int, Optional[float]]]]:
    """
    Read tool.tbl from the config directory.

    Returns:
        (prompt text, usable tools -> trusted diameter, or None if the table couldn't be
        read). Problems in the table are reported through log_callback.
    """
    log = log_callback or (lambda msg: None)
    path = os.path.join(config_path, "tool.tbl")
    if not os.path.exists(path):
        return ("\nAVAILABLE TOOLS: No tool table file found. Include an \"error\" field explaining "
                "that the tool table is missing.\n"), None
    try:
        tools, warnings = read_tool_table(path, machine_units)
    except Exception as e:
        log(f"[WARN] Failed to read tool table {path}: {e}")
        return "\nAVAILABLE TOOLS: The tool table could not be read.\n", None
    for warning in warnings:
        log(f"[TOOLS] {warning}")
    return format_tools_for_prompt(tools, machine_units), usable_tools(tools)
