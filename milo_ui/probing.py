"""
Tool length measurement (tool setter) and touch-plate Z touch-off.

These are the same routines the previous screen ran: the tool setter goes through
CustomAction.probe_with_toolsetter (subprograms.py) using the sensor location from the
INI [TOOL_SENSOR] section, and the touch plate uses qtvcp's TOUCHPLATE_TOUCHOFF.
Their parameters are kept in the screen preferences.
"""

from milo_ui import kit

# (key, label, default, units kind)  -- defaults are this machine's previous settings
PARAMETERS = [
    ("search_vel", "Search speed", 200.0, "vel"),
    ("probe_vel", "Probe speed", 100.0, "vel"),
    ("max_probe", "Max probe distance", 50.0, "len"),
    ("retract", "Retract distance", 10.0, "len"),
    ("z_safe", "Z safe travel", -60.0, "len"),
    ("sensor_height", "Tool setter height", 66.43, "len"),
    ("work_height", "Work height", -100.0, "len"),
    ("touch_height", "Touch plate height", 40.0, "len"),
]


def get(prefs, key):
    default = next(p[2] for p in PARAMETERS if p[0] == key)
    try:
        return float(prefs.get(f"probe.{key}", default))
    except (TypeError, ValueError):
        return default


def _confirm(host, title, text, go):
    kit.ActionSheet(host, title, [("play-fill", "Start", go, "warn"), ("x", "Cancel", lambda: None)],
                    subtitle=text, width=520).show_centered()


def measure_tool(host, machine, prefs, notify):
    """Measure the loaded tool's length on the tool setter (asks first)"""
    if machine.tool == 0:
        return notify("Load a tool before measuring it.", "warning")
    if not machine.all_homed:
        return notify("Home the machine before measuring a tool.", "warning")
    if not machine.ready:
        return notify(machine.state_detail or "The machine isn't ready.", "warning")
    _confirm(host, "Measure tool length",
             f"T{machine.tool} will move to the tool setter and probe down. Make sure the path is clear "
             "and the setter is in place.", lambda: _run_toolsetter(machine, prefs, notify))


def _run_toolsetter(machine, prefs, notify):
    try:
        from qtvcp.core import Info
        from custom_action import CustomAction
    except ImportError:
        return notify("Tool setter is only available on the machine.", "info")
    info = Info()
    try:
        x = float(info.get_error_safe_setting("TOOL_SENSOR", "X", None))
        y = float(info.get_error_safe_setting("TOOL_SENSOR", "Y", None))
        z = float(info.get_error_safe_setting("TOOL_SENSOR", "Z", None))
    except (TypeError, ValueError):
        return notify("Set X, Y and Z in the INI [TOOL_SENSOR] section first.", "error")
    z_offset = get(prefs, "sensor_height") - get(prefs, "work_height")
    action = getattr(machine, "_custom_action", None) or CustomAction()
    machine._custom_action = action
    started = action.probe_with_toolsetter(
        get(prefs, "search_vel"), get(prefs, "probe_vel"), get(prefs, "max_probe"), z_offset,
        get(prefs, "retract"), get(prefs, "z_safe"), x, y, z, machine.tool,
        lambda code, status: _toolsetter_done(action, machine, notify))
    if started == 0:
        notify("The tool setter routine is already running.", "warning")
    else:
        notify(f"Measuring T{machine.tool}…", "info")


def _toolsetter_done(action, machine, notify):
    """Only report success if the routine said so; it exits normally even when it failed"""
    machine.reload_tool_table()
    if getattr(action, "completed", False) and not getattr(action, "error_text", ""):
        notify("Tool measured.", "success")
    else:
        detail = getattr(action, "error_text", "") or "the routine didn't report completion"
        notify(f"Tool measurement failed: {detail}. Check the tool length offset before running.", "error")


def touch_plate(host, machine, prefs, notify):
    """Probe down to the touch plate and set work Z (asks first)"""
    if not machine.ready:
        return notify(machine.state_detail or "The machine isn't ready.", "warning")
    _confirm(host, "Touch off Z with the plate",
             f"Z will probe down up to {get(prefs, 'max_probe'):g} {machine.units}. Put the touch plate under "
             "the tool and connect the clip.", lambda: _run_touchplate(machine, prefs, notify))


def _run_touchplate(machine, prefs, notify):
    try:
        from qtvcp.core import Action
    except ImportError:
        return notify("Touch plate is only available on the machine.", "info")
    result = Action().TOUCHPLATE_TOUCHOFF(
        str(get(prefs, "search_vel")), str(get(prefs, "probe_vel")), str(get(prefs, "max_probe")),
        get(prefs, "touch_height"), str(get(prefs, "retract")), str(get(prefs, "z_safe")),
        lambda data: notify("Z touched off on the plate.", "success"))
    if result == 0:
        notify("The touch-off routine is already running.", "warning")
