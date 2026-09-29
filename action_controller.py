"""
Machine actions for the AI Assistant: turn the intent router's decision into a proposed
action, and run an action once the user has confirmed it.

No Qt here. The widget passes in linuxcnc.stat and the qtvcp Action API, so the safety path
(validate on proposal, re-validate on execution) can be tested without a GUI.
"""

import os

from ai_config import DEFAULT_CIRCLE_FEED
from machine_safety import (validate_mdi, check_obstacles, check_machine_ready, check_program_ready, check_power_on,
                            check_power_off, program_running, parse_mdi, machine_units, work_move_note,
                            circle_commands, validate_circle)

_heightmap_cache = {}


def _heightmap():
    """The camera's height map (vision/heightmap.npz), if a scan made one; reloaded when it changes"""
    try:
        from milo_vision.heightmap import HeightMap, HEIGHTMAP_FILE
    except ImportError:
        return None
    try:
        mtime = os.path.getmtime(HEIGHTMAP_FILE)
    except OSError:
        return None
    if _heightmap_cache.get("mtime") != mtime:
        _heightmap_cache.update(mtime=mtime, map=HeightMap.load(HEIGHTMAP_FILE))
    return _heightmap_cache["map"]


def check_path(command, stat, heightmap=None):
    """validate_mdi, plus the camera's obstacle map when there is one"""
    reason = validate_mdi(command, stat)
    if reason:
        return reason
    return check_obstacles(command, stat, heightmap if heightmap is not None else _heightmap())


# Router intents that become a confirmable machine action
MACHINE_INTENTS = {"mdi", "home", "circle", "power_on", "power_off", "run"}


def propose_action(result, stat):
    """
    Build the action to put in front of the user for a machine intent.

    Returns (action, refusal): the action dict for the confirmation gate, or None and a
    message saying why Milo won't propose it.
    """
    intent = result["intent"]

    if intent == "mdi":
        command = result["mdi"]
        reason = check_path(command, stat)
        if reason:
            return None, f"I won't run '{command}': {reason}"
        summary = result["summary"] or command
        # Say where a work-coordinate move really ends up, so an unexpected offset is visible
        note = work_move_note(command, stat)
        if note:
            summary += f" ({note})"
        return {"kind": "mdi", "command": command, "summary": summary}, None

    if intent == "home":
        reason = check_machine_ready(stat, needs_homed=False)
        if reason:
            return None, f"I can't home the machine: {reason}"
        return {"kind": "home", "summary": result["summary"] or "Home all axes"}, None

    if intent == "circle":
        circle = result["circle"]
        feed = circle["feed"] or DEFAULT_CIRCLE_FEED
        reason = validate_circle(circle["diameter"], feed, stat)
        if reason:
            return None, f"I won't make that circle: {reason}"
        units = machine_units(stat) if stat.program_units != 1 else "inch"
        direction = "clockwise" if circle["clockwise"] else "counter-clockwise"
        return {
            "kind": "circle",
            "diameter": circle["diameter"],
            "clockwise": circle["clockwise"],
            "feed": feed,
            "command": " → ".join(circle_commands(circle["diameter"], circle["clockwise"], feed, stat)),
            "summary": (f"Move in a {circle['diameter']:g} {units} {direction} circle around the current "
                        f"position at F{feed:g}, at the current Z height"),
        }, None

    if intent == "power_on":
        reason = check_power_on(stat)
        if reason:
            return None, reason
        return {"kind": "power_on", "summary": "Turn the machine on"}, None

    if intent == "power_off":
        reason = check_power_off(stat)
        if reason:
            return None, reason
        summary = "Turn the machine off"
        if program_running(stat):
            summary += " (this stops the running program)"
        return {"kind": "power_off", "summary": summary}, None

    if intent == "run":
        reason = check_program_ready(stat)
        if reason:
            return None, f"I can't start the program: {reason}"
        # Name the file ourselves so the confirmation shows exactly what will run
        return {"kind": "run", "file": stat.file, "summary": f"Run {os.path.basename(stat.file)} from the start"}, None

    raise ValueError(f"Not a machine intent: {intent}")


def execute_action(action, stat, api):
    """
    Run a confirmed action, re-checking it against the current machine state first.

    api is the qtvcp Action object. Returns (log_line, ran): the line for the chat log, and
    whether anything was sent to the machine.
    """
    kind = action["kind"]

    if kind == "home":
        reason = check_machine_ready(stat, needs_homed=False)
        if reason:
            return f"[MILO] Not homing: {reason}", False
        api.SET_MACHINE_HOMING(-1)
        return "[MILO] Homing all axes.", True

    if kind == "circle":
        reason = validate_circle(action["diameter"], action["feed"], stat)
        if reason:
            return f"[MILO] Not moving: {reason}", False
        # Restore the distance / arc-distance modes the machine was in
        restore = []
        if 910 not in stat.gcodes:
            restore.append("G90")
        if 901 in stat.gcodes:
            restore.append("G90.1")
        for line in circle_commands(action["diameter"], action["clockwise"], action["feed"], stat):
            if api.CALL_MDI(line) == -1:
                return f"[ERROR] LinuxCNC refused '{line}'.", False
        if restore:
            api.CALL_MDI(" ".join(restore))
        return f"[MDI] Executed: circle, {action['diameter']:g} diameter at F{action['feed']:g}", True

    if kind in ("power_on", "power_off"):
        turn_on = kind == "power_on"
        reason = check_power_on(stat) if turn_on else check_power_off(stat)
        if reason:
            return f"[MILO] Not changing power: {reason}", False
        api.SET_MACHINE_STATE(turn_on)
        return f"[MILO] Machine {'on' if turn_on else 'off'}.", True

    if kind == "run":
        reason = check_program_ready(stat)
        if not reason and stat.file != action["file"]:
            reason = f"A different program is loaded now ({os.path.basename(stat.file)})."
        if reason:
            return f"[MILO] Not starting the program: {reason}", False
        api.RUN()
        return f"[MILO] Running {os.path.basename(stat.file)}.", True

    command = action["command"]
    reason = check_path(command, stat)
    if reason:
        return f"[MILO] Not running '{command}': {reason}", False
    # G91 is modal; put the machine back in G90 afterwards if that's where it was
    g_codes = {int(v) for letter, v in parse_mdi(command) if letter == "G"}
    restore_g90 = 91 in g_codes and 910 not in stat.gcodes
    if api.CALL_MDI(command) == -1:
        return f"[ERROR] LinuxCNC refused '{command}'.", False
    if restore_g90:
        api.CALL_MDI("G90")
    return f"[MDI] Executed: {command}", True
