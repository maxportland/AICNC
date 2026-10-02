"""Tests for the game controller jog pendant: the safety rules, input decoding and the machine link"""

import pytest

from milo_ui.pendant import (DEFAULTS, InputState, PendantLogic, apply_event, conflicts, find_gamepad,
                             merged_config, EV_ABS, EV_KEY)


def _logic(**changes):
    config = merged_config({"enabled": True})
    config.update(changes)
    return PendantLogic(config)


def _state(rt=0.0, lt=0.0, buttons=(), last_event=100.0, **sticks):
    state = InputState(connected=True, buttons=set(buttons), last_event=last_event)
    state.triggers.update(RT=rt, LT=lt)
    state.sticks.update(sticks)
    return state


# --- the dead-man and machine readiness ---

def test_nothing_moves_without_the_deadman():
    logic = _logic()
    out = logic.update(_state(rt=0.2, LX=1.0), 100.0, True)
    assert out.status == "released" and out.velocities == {} and out.steps == []


def test_deadman_held_jogs_proportionally():
    logic = _logic(curve="Linear", deadzone=0.0)
    out = logic.update(_state(rt=0.8, LX=0.5, LY=-1.0, RY=1.0), 100.0, True)
    assert out.status == "jogging"
    assert out.velocities == {"X": pytest.approx(500.0), "Y": pytest.approx(-1000.0), "Z": pytest.approx(400.0)}


def test_not_ready_or_disabled_or_disconnected_never_moves():
    assert _logic().update(_state(rt=1.0, LX=1.0), 100.0, False).status == "not_ready"
    assert _logic(enabled=False).update(_state(rt=1.0, LX=1.0), 100.0, True).status == "off"
    state = _state(rt=1.0, LX=1.0)
    state.connected = False
    assert _logic().update(state, 100.0, True).status == "disconnected"


def test_a_silent_controller_pauses_stick_jogging():
    logic = _logic()
    held = _state(rt=1.0, LX=1.0, last_event=100.0)
    assert logic.update(held, 101.0, True).velocities  # 1 s of no events: still going
    out = logic.update(held, 101.6, True)  # past the 1.5 s hold timeout
    assert out.status == "paused" and out.velocities == {}


# --- speed shaping ---

def test_deadzone_curve_caps_and_invert():
    logic = _logic()
    assert logic.stick_speed(0.1, "X", 1.0) == 0.0  # inside the 15 % dead zone
    assert logic.stick_speed(1.0, "X", 1.0) == pytest.approx(1000.0)
    assert logic.stick_speed(1.0, "Z", 1.0) == pytest.approx(400.0)
    half = logic.stick_speed(0.575, "X", 1.0)  # halfway past the dead zone, gentle curve
    assert 250 < half < 400
    logic.config["invert"]["X"] = True
    assert logic.stick_speed(1.0, "X", 1.0) == pytest.approx(-1000.0)


def test_fine_trigger_slows_everything():
    logic = _logic(curve="Linear", deadzone=0.0)
    out = logic.update(_state(rt=1.0, lt=1.0, LX=1.0), 100.0, True)
    assert out.velocities["X"] == pytest.approx(100.0)  # fine_min 10 %


# --- buttons ---

def test_steps_fire_once_per_press_and_need_the_deadman():
    logic = _logic()
    assert logic.update(_state(rt=1.0, buttons={"DPAD_RIGHT"}), 100.0, True).steps == [("X", 1)]
    assert logic.update(_state(rt=1.0, buttons={"DPAD_RIGHT"}), 100.05, True).steps == []  # still held
    logic.update(_state(rt=1.0), 100.1, True)
    assert logic.update(_state(rt=0.0, buttons={"Y"}), 100.15, True).steps == []  # no dead-man


def test_stop_talk_and_step_size_work_without_the_deadman():
    logic = _logic()
    out = logic.update(_state(buttons={"B", "X", "RB"}), 100.0, True)
    assert sorted(out.actions) == ["step_larger", "stop", "talk"]


def test_stop_cancels_motion_in_the_same_update():
    logic = _logic()
    out = logic.update(_state(rt=1.0, buttons={"B"}, LX=1.0), 100.0, True)
    assert out.actions == ["stop"] and out.velocities == {}


# --- configuration ---

def test_defaults_have_no_conflicts_and_saved_settings_merge():
    assert conflicts(merged_config({})) == []
    config = merged_config({"max_xy": 500, "buttons": {"stop": "A"}, "unknown": 1})
    assert config["max_xy"] == 500 and config["buttons"]["stop"] == "A"
    assert config["buttons"]["talk"] == DEFAULTS["buttons"]["talk"] and "unknown" not in config
    problems = conflicts(config)
    assert any("A (" in p or p.startswith("A does") for p in problems)
    assert conflicts(merged_config({"buttons": {"talk": "RT"}}))  # the dead-man can't do anything else


# --- reading the device ---

def test_finds_the_pad_but_not_its_keyboard():
    text = ('I: Bus=0003 Vendor=2dc8 Product=310b\nN: Name="8BitDo Ultimate 2 Keyboard"\nH: Handlers=sysrq kbd event5\n\n'
            'I: Bus=0003 Vendor=2dc8 Product=310b\nN: Name="Generic X-Box pad"\nH: Handlers=event3 js1\n\n'
            'N: Name="Weida touch"\nH: Handlers=mouse0 event0 js0\n')
    assert find_gamepad(text) == ("/dev/input/event3", "Generic X-Box pad")


def test_events_decode_to_the_xbox_layout():
    state = InputState(connected=True)
    apply_event(state, EV_KEY, 304, 1)
    apply_event(state, EV_ABS, 1, -32768)  # left stick pushed up
    apply_event(state, EV_ABS, 5, 255)  # right trigger
    apply_event(state, EV_ABS, 16, -1)  # d-pad left
    assert state.buttons == {"A", "DPAD_LEFT"}
    assert state.sticks["LY"] == pytest.approx(1.0) and state.triggers["RT"] == 1.0
    apply_event(state, EV_ABS, 16, 0)
    apply_event(state, EV_KEY, 304, 0)
    assert state.buttons == set()


# --- the machine link ---

class FakeReader:
    from PyQt5 import QtCore

    def __init__(self):
        from PyQt5 import QtCore

        class Signals(QtCore.QObject):
            changed = QtCore.pyqtSignal(object)
        self._signals = Signals()
        self.changed = self._signals.changed
        self.started = self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeMachine:
    axes = ("X", "Y", "Z")
    ready = True
    is_running = False
    on = True
    spindle_dir = 0
    mist = False

    def __init__(self):
        self.calls = []

    def pendant_jog(self, axis, velocity, dt):
        self.calls.append(("jog", axis, round(velocity), dt))

    def pendant_step(self, axis, direction, distance):
        self.calls.append(("step", axis, direction, distance))

    def pendant_stop(self):
        self.calls.append(("stop",))


class FakePrefs(dict):
    def set(self, key, value):
        self[key] = value


def test_pendant_drives_the_machine_and_stops_on_release(qapp):
    from milo_ui.pendant import Pendant
    machine, prefs = FakeMachine(), FakePrefs()
    pendant = Pendant(machine, prefs, reader_factory=FakeReader)
    assert pendant.reader is None  # off by default: no reader
    pendant.set("enabled", True)
    assert pendant.reader.started and prefs["pendant"]["enabled"]
    import time
    pendant.state = _state(rt=1.0, LX=1.0, last_event=time.monotonic())
    pendant.tick()
    assert machine.calls and machine.calls[-1][:3] == ("jog", "X", 1000)
    assert machine.calls[-1][3] <= 0.1  # never more than MAX_DT of travel per tick
    pendant.state = _state(rt=0.0, LX=1.0, last_event=time.monotonic())
    pendant.tick()
    assert machine.calls[-1] == ("stop",) and pendant.status == "released"
    pendant.state = _state(rt=1.0, buttons={"RB"}, last_event=time.monotonic())
    pendant.tick()
    assert pendant.step_size == 1.0  # RB: larger step
    pendant.set("enabled", False)
    assert pendant.status == "off"
    pendant.shutdown()


def test_disconnect_stops_motion(qapp):
    import time
    from milo_ui.pendant import Pendant
    machine = FakeMachine()
    pendant = Pendant(machine, FakePrefs(pendant={"enabled": True}), reader_factory=FakeReader)
    pendant.state = _state(rt=1.0, LY=1.0, last_event=time.monotonic())
    pendant.tick()
    pendant._on_input(InputState(connected=False))
    assert machine.calls[-1] == ("stop",)
    pendant.shutdown()


def test_x_with_the_deadman_confirms_and_alone_talks():
    logic = _logic()
    assert logic.update(_state(rt=1.0, buttons={"X"}), 100.0, True).actions == ["confirm"]
    logic.update(_state(), 100.05, True)
    assert logic.update(_state(buttons={"X"}), 100.1, True).actions == ["talk"]
    logic.update(_state(), 100.15, True)
    # Confirming works even when the machine isn't ready to jog ("turn the machine on")
    assert logic.update(_state(rt=1.0, buttons={"X"}), 100.2, False).actions == ["confirm"]
    assert conflicts(merged_config({})) == []  # talk and confirm may share a button
    assert conflicts(merged_config({"buttons": {"confirm": "B"}}))  # but not with stop


from test_milo_ui import shell  # noqa: E402,F401  (the screen fixture)


def test_pendant_confirm_reaches_the_engine(shell):
    import time
    asked = []
    shell.engine.confirm = lambda action_id=None: asked.append("confirm")
    shell.pendant.config["enabled"] = True
    shell.pendant.state = _state(rt=1.0, buttons={"X"}, last_event=time.monotonic())
    shell.pendant.tick()
    assert asked == ["confirm"]


# --- the quick (radial) menu ---

def test_slice_directions():
    from milo_ui.pendant import slice_at
    assert slice_at(0, 1, 6) == 0      # up: first slice
    assert slice_at(1, 0.3, 6) == 1    # right-ish: clockwise
    assert slice_at(0, -1, 6) == 3     # down
    assert slice_at(-1, 0.3, 6) == 5   # left-ish


def test_menu_mode_never_jogs_and_reports_select_cancel():
    logic = _logic()
    out = logic.update(_state(rt=1.0, LX=1.0, buttons={"A"}), 100.0, True, menu_open=True)
    assert out.status == "menu" and out.velocities == {} and out.steps == []
    assert out.actions == ["menu_select"] and out.deadman and out.menu_stick == (1.0, 0.0)
    assert logic.update(_state(buttons={"A", "B"}), 100.05, True, menu_open=True).actions == ["menu_cancel"]


def test_left_stick_click_opens_the_menu(qapp):
    import time
    from milo_ui.pendant import Pendant
    machine = FakeMachine()
    from milo_ui.pendant import RADIAL_ACTIONS
    six = ["mist", "probe", "spindle", "talk", "set_jog", "power"]
    machine.spindle_dir = 1  # running: the spindle slot is Stop spindle
    pendant = Pendant(machine, FakePrefs(pendant={"enabled": True, "radial_items": six,
                                                  "radial_known": list(RADIAL_ACTIONS)}), reader_factory=FakeReader)
    opened, run, hints = [], [], []
    pendant.menu_changed.connect(opened.append)
    pendant.quick_action.connect(run.append)
    pendant.menu_hint.connect(hints.append)
    now = time.monotonic
    pendant.state = _state(rt=1.0, LX=1.0, last_event=now())
    pendant.tick()  # jogging
    pendant.state = _state(buttons={"LS"}, last_event=now())
    pendant.tick()
    assert opened == [True] and pendant.menu_open and machine.calls[-1] == ("stop",)
    pendant.state = _state(buttons={"LS"}, LY=1.0, last_event=now())  # up: Mist on (still holding LS)
    pendant.tick()
    assert pendant.menu_index == 0 and run == []
    pendant.state = _state(buttons={"A"}, LY=1.0, last_event=now())  # A without the dead-man
    pendant.tick()
    assert run == [] and hints == ["Hold RT to run Mist on"] and pendant.menu_open
    pendant.state = _state(LX=1.0, LY=-0.6, last_event=now())  # down-right: Stop spindle (no dead-man needed)
    pendant.tick()
    pendant.state = _state(buttons={"A"}, LX=1.0, LY=-0.6, last_event=now())
    pendant.tick()
    assert run == ["spindle"] and opened == [True, False] and not pendant.menu_open
    pendant.state = _state(buttons={"LS"}, last_event=now())
    pendant.tick()
    pendant.state = _state(buttons={"B"}, last_event=now())
    pendant.tick()
    assert not pendant.menu_open and run == ["spindle"]  # B closed it without running anything
    pendant.shutdown()


def test_quick_actions_on_the_machine(shell):
    m = shell.machine
    m.set_estop(False)
    shell.run_quick_action("spindle")
    assert not m.spindle_dir  # machine off: refused
    m.set_power(True)
    shell.run_quick_action("spindle")
    assert m.spindle_dir == 1
    shell.run_quick_action("mist")
    assert m.mist
    shell.run_quick_action("mist")
    shell.run_quick_action("spindle")
    assert not m.mist and not m.spindle_dir
    shell.run_quick_action("probe")
    assert shell.current == "probe"


def test_quick_menu_overlay_opens_and_taps(shell):
    p = shell.pendant
    p.config["enabled"] = True
    p.open_menu()
    assert shell.quick_menu.isVisible() and len(shell.quick_menu.items) == 7
    ran = []
    shell.run_quick_action = ran.append
    centre = shell.quick_menu.rect().center()
    from PyQt5.QtCore import QPoint
    assert shell.quick_menu.slice_at_point(QPoint(centre.x(), centre.y() - 200)) == 0  # top slice
    p.close_menu()
    assert not shell.quick_menu.isVisible()


# --- the slider (set spindle speed / feed) ---

def _adjusting(value=1000.0):
    from milo_ui.pendant import Pendant
    pendant = Pendant(FakeMachine(), FakePrefs(pendant={"enabled": True}), reader_factory=FakeReader)
    values, done = [], []
    pendant.adjust_changed.connect(values.append)
    pendant.adjust_done.connect(lambda apply, v: done.append((apply, v)))
    pendant.open_adjuster({"value": value, "min": 100, "max": 3000, "step": 100})
    return pendant, values, done


def test_dpad_steps_and_limits(qapp):
    import time
    pendant, values, done = _adjusting(2900)
    pendant.state = _state(buttons={"DPAD_RIGHT"}, last_event=time.monotonic())
    pendant.tick()
    pendant.state = _state(last_event=time.monotonic())
    pendant.tick()
    pendant.state = _state(buttons={"DPAD_UP"}, last_event=time.monotonic())
    pendant.tick()
    assert values == [2900, 3000]  # the second step stopped at the maximum
    pendant.state = _state(buttons={"A"}, last_event=time.monotonic())
    pendant.tick()
    assert done == [(True, 3000)] and pendant.adjust is None
    pendant.shutdown()


def test_stick_repeats_faster_when_held(qapp, monkeypatch):
    from milo_ui import pendant as pendant_module
    pendant, values, done = _adjusting(1000)
    clock = [100.0]
    monkeypatch.setattr(pendant_module.time, "monotonic", lambda: clock[0])
    for _ in range(40):  # 2 s of holding the stick right, ticks every 50 ms
        pendant.state = _state(LX=0.7, last_event=clock[0])
        pendant.tick()
        clock[0] += 0.05
    first_second = [v for v in values if v <= 1400]
    assert values[1] == 1100 and len(values) > 8  # stepped at once, then kept going faster
    assert len(first_second) <= 5
    pendant.state = _state(buttons={"B"}, last_event=clock[0])
    pendant.tick()
    assert done == [(False, values[-1])]  # B: cancelled
    pendant.shutdown()


def test_new_quick_menu_items_appear_once_for_existing_settings():
    from milo_ui.pendant import merged_config
    old = {"radial_items": ["home_all", "probe"]}  # saved before the slider items existed
    config = merged_config(old)
    assert config["radial_items"] == ["probe", "set_spindle", "set_feed", "set_jog", "power"]
    config["radial_items"].remove("set_feed")  # the user turns one off...
    again = merged_config(config)
    assert "set_feed" not in again["radial_items"]  # ...and it stays off


def test_set_spindle_and_feed_from_the_quick_menu(shell):
    m = shell.machine
    shell.run_quick_action("set_feed")
    assert shell.adjust_panel.isVisible() and shell.pendant.adjust["value"] == 100
    shell.pendant.set_adjust_value(142)  # touch drag snaps to 5 % steps
    assert shell.pendant.adjust["value"] == 140
    shell.adjust_panel.apply_button.click()
    assert m.feed_override == 140 and not shell.adjust_panel.isVisible()
    shell.run_quick_action("set_spindle")  # spindle off: sets the speed "Start spindle" uses
    shell.pendant.set_adjust_value(1800)
    shell.pendant.close_adjuster(True)
    assert shell.pendant.config["radial_rpm"] == 1800
    m.set_estop(False)
    m.set_power(True)
    shell.run_quick_action("spindle")
    assert m.spindle_dir == 1
    shell.run_quick_action("set_spindle")
    shell.pendant.set_adjust_value(2200)
    shell.adjust_panel.cancel_button.click()
    shell.run_quick_action("set_spindle")
    shell.pendant.set_adjust_value(2200)
    shell.pendant.close_adjuster(True)
    assert m.spindle_requested == 2200 or getattr(m, "spindle_rpm", 2200) == 2200


def test_jog_speed_from_the_quick_menu(shell):
    shell.run_quick_action("set_jog")
    assert shell.pendant.adjust["value"] == 1000
    shell.pendant.set_adjust_value(1530)  # snaps to 50s
    shell.pendant.close_adjuster(True)
    assert shell.pendant.config["max_xy"] == 1550 and shell.pendant.config["max_z"] == 400


def test_a_newer_item_is_added_for_settings_saved_with_the_previous_menu():
    from milo_ui.pendant import merged_config
    previous = ["home_all", "spindle_on", "spindle_off", "mist_on", "mist_off", "probe", "z_top", "talk",
                "set_spindle", "set_feed"]
    config = merged_config({"radial_items": ["home_all", "set_feed"], "radial_known": previous})
    assert config["radial_items"] == ["set_feed", "set_jog", "power"]


def test_power_item_shows_what_it_would_do():
    from milo_ui.pendant import radial_entry
    machine = FakeMachine()
    machine.on = False
    assert radial_entry("power", machine) == ("Power on", "power", True)  # starting needs the dead-man
    machine.on = True
    assert radial_entry("power", machine) == ("Power off", "power", False)
    assert radial_entry("home_all", machine) == ("Home all", "house-line", True)


def test_power_from_the_quick_menu(shell):
    m = shell.machine
    shell.run_quick_action("power")
    assert not m.on  # E-stop: refused
    m.set_estop(False)
    shell.pendant.config["radial_items"] = ["probe", "power"]
    shell.pendant.open_menu()
    assert shell.quick_menu.items[1] == ("Power on", "power")
    shell.run_quick_action("power")
    assert m.on and shell.quick_menu.items[1] == ("Power off", "power")  # relabelled while open
    shell.run_quick_action("power")
    assert not m.on
    shell.pendant.close_menu()


def test_power_off_runs_without_the_deadman(qapp):
    import time
    from milo_ui.pendant import Pendant, RADIAL_ACTIONS
    machine = FakeMachine()
    machine.on = True
    pendant = Pendant(machine, FakePrefs(pendant={"enabled": True, "radial_items": ["power"],
                                                  "radial_known": list(RADIAL_ACTIONS)}), reader_factory=FakeReader)
    run, hints = [], []
    pendant.quick_action.connect(run.append)
    pendant.menu_hint.connect(hints.append)
    now = time.monotonic
    pendant.open_menu()
    pendant.state = _state(LY=1.0, last_event=now())
    pendant.tick()
    pendant.state = _state(buttons={"A"}, LY=1.0, last_event=now())
    pendant.tick()
    assert run == ["power"]
    machine.on = False  # turning on needs the dead-man
    pendant.open_menu()
    pendant.state = _state(LY=1.0, last_event=now())
    pendant.tick()
    pendant.state = _state(buttons={"A"}, LY=1.0, last_event=now())
    pendant.tick()
    assert run == ["power"] and hints == ["Hold RT to run Power on"]
    pendant.shutdown()


def test_spindle_and_mist_show_what_they_would_do():
    from milo_ui.pendant import radial_entry
    machine = FakeMachine()
    assert radial_entry("spindle", machine) == ("Start spindle", "arrow-clockwise", True)
    assert radial_entry("mist", machine) == ("Mist on", "drop", True)
    machine.spindle_dir, machine.mist = -1, True
    assert radial_entry("spindle", machine) == ("Stop spindle", "stop-circle", False)
    assert radial_entry("mist", machine) == ("Mist off", "drop-half", False)


def test_quick_menu_relabels_spindle_and_mist_while_open(shell):
    m = shell.machine
    m.set_estop(False)
    m.set_power(True)
    shell.pendant.config["radial_items"] = ["spindle", "mist"]
    shell.pendant.open_menu()
    assert shell.quick_menu.items == [("Start spindle", "arrow-clockwise"), ("Mist on", "drop")]
    shell.run_quick_action("spindle")
    shell.run_quick_action("mist")
    assert shell.quick_menu.items == [("Stop spindle", "stop-circle"), ("Mist off", "drop-half")]
    shell.pendant.close_menu()


@pytest.mark.parametrize("saved,expected", [
    # Both halves on: one slot where the first was
    (["home_all", "spindle_on", "spindle_off", "mist_on", "mist_off", "probe"],
     ["spindle", "mist", "probe"]),
    # Either half on keeps the toggle
    (["mist_off", "home_all", "spindle_on"], ["mist", "spindle"]),
    # Both halves off stays off
    (["home_all", "probe"], ["probe"]),
])
def test_split_spindle_and_mist_items_are_merged(saved, expected):
    from milo_ui.pendant import RADIAL_ACTIONS, merged_config
    previous = ["home_all", "spindle_on", "spindle_off", "mist_on", "mist_off", "probe", "z_top", "talk",
                "set_spindle", "set_feed", "set_jog", "power"]
    config = merged_config({"radial_items": saved, "radial_known": previous})
    assert config["radial_items"] == expected
    assert all(item in RADIAL_ACTIONS for item in config["radial_known"])


# --- the zero & go-to menu (right stick click) ---

def _moves_pendant(machine=None):
    from milo_ui.pendant import Pendant, RADIAL_ACTIONS
    machine = machine or FakeMachine()
    pendant = Pendant(machine, FakePrefs(pendant={"enabled": True, "radial_known": list(RADIAL_ACTIONS)}),
                      reader_factory=FakeReader)
    run, hints, opened = [], [], []
    pendant.quick_action.connect(run.append)
    pendant.menu_hint.connect(hints.append)
    pendant.menu_changed.connect(opened.append)
    return pendant, run, hints, opened


def _press(pendant, **state):
    import time
    pendant.state = _state(last_event=time.monotonic(), **state)
    pendant.tick()


def test_right_stick_click_opens_the_zero_and_go_to_menu(qapp):
    pendant, run, hints, _ = _moves_pendant()
    _press(pendant, buttons={"RS"})
    assert pendant.menu_open and pendant.menu_items == ["home_all", "zero", "go_work_zero", "go_abs_home", "go_g54",
                                                    "probe_menu"]
    assert pendant.menu_title == "Zero & go to"
    _press(pendant, LX=0.6, LY=-0.8)  # lower right: Go to Work Zero
    _press(pendant, buttons={"A"}, LX=0.6, LY=-0.8)
    assert run == [] and hints == ["Hold RT to run Go to Work Zero"]  # moving needs the dead-man
    _press(pendant, LX=0.6, LY=-0.8, rt=1.0)  # let go of A, hold the dead-man, press A again
    _press(pendant, buttons={"A"}, LX=0.6, LY=-0.8, rt=1.0)
    assert run == ["go_work_zero"] and not pendant.menu_open
    pendant.shutdown()


def test_zero_opens_an_axis_ring_and_b_goes_back(qapp):
    pendant, run, hints, opened = _moves_pendant()
    _press(pendant, buttons={"RS"})
    _press(pendant, LX=0.95, LY=0.31)  # upper right: Zero
    _press(pendant, buttons={"A"}, LX=0.95, LY=0.31)  # opening the submenu needs no dead-man
    assert pendant.menu_items == ["zero_x", "zero_y", "zero_z", "zero_all"] and pendant.in_submenu
    assert run == []
    _press(pendant, buttons={"B"})
    assert pendant.menu_open and "zero" in pendant.menu_items and not pendant.in_submenu  # back, not closed
    _press(pendant, LX=0.95, LY=0.31)
    _press(pendant, buttons={"A"}, LX=0.95, LY=0.31)
    _press(pendant, LX=-1.0)  # left: All
    _press(pendant, buttons={"A"}, LX=-1.0)
    assert run == [] and hints[-1] == "Hold RT to run Zero all"
    _press(pendant, LX=-1.0, rt=1.0)
    _press(pendant, buttons={"A"}, LX=-1.0, rt=1.0)
    assert run == ["zero_all"] and not pendant.menu_open
    _press(pendant, buttons={"RS"})
    _press(pendant, buttons={"B"})
    assert not pendant.menu_open  # B on the top ring closes it
    pendant.shutdown()


def test_the_quick_menu_is_unchanged(qapp):
    pendant, _, _, _ = _moves_pendant()
    _press(pendant, buttons={"LS"})
    assert pendant.menu_items == pendant.quick_items and pendant.menu_title == "Quick menu"
    pendant.close_menu()
    assert pendant.menu_items == pendant.quick_items  # closed: the configured quick menu
    pendant.shutdown()


def test_existing_settings_get_the_right_stick_button():
    from milo_ui.pendant import conflicts, merged_config
    config = merged_config({"buttons": {"radial": "LS", "talk": "X"}})
    assert config["buttons"]["radial2"] == "RS" and conflicts(config) == []


def test_zero_and_go_to_on_the_machine(shell):
    m, p = shell.machine, shell.pendant
    m.set_estop(False)
    m.set_power(True)
    m.homed = {a: True for a in m.axes}
    m.pos_abs = [120.0, 60.0, -20.0]
    shell.run_quick_action("zero_x")
    assert m.pos_rel[0] == pytest.approx(0.0) and m.pos_rel[1] != pytest.approx(0.0)
    shell.run_quick_action("zero_all")
    assert m.pos_rel == pytest.approx([0.0, 0.0, 0.0])
    sent = []
    m.mdi_lines = lambda lines: sent.append(list(lines)) or True
    shell.run_quick_action("go_abs_home")
    shell.run_quick_action("go_g54")
    m.wcs = "G55"
    shell.run_quick_action("go_g54")
    assert sent == [["G90 G53 G0 Z0", "G90 G53 G0 X0 Y0"],
                    ["G90 G53 G0 Z0", "G54 G90 G0 X0 Y0", "G90 G0 Z0"],
                    ["G90 G53 G0 Z0", "G54 G90 G0 X0 Y0", "G90 G0 Z0", "G55"]]  # back to the active system
    m.homed = {a: False for a in m.axes}
    shell.run_quick_action("go_work_zero")
    assert len(sent) == 3  # not homed: refused


def test_tapping_zero_on_the_screen_opens_the_axis_ring(shell):
    p = shell.pendant
    p.open_menu(["zero", "go_work_zero", "go_abs_home", "go_g54"], "Zero & go to")
    shell.quick_menu.picked.emit(0)
    assert shell.quick_menu.isVisible() and shell.quick_menu.title == "Zero which axis?"
    assert [label for label, _ in shell.quick_menu.items] == ["Zero X", "Zero Y", "Zero Z", "Zero all"]
    assert "B goes back" in shell.quick_menu.default_hint
    p.close_menu()
    assert not shell.quick_menu.isVisible()


def test_home_all_lives_in_the_zero_and_go_to_menu(qapp):
    from milo_ui.pendant import MOVE_ITEMS, RADIAL_ACTIONS, radial_entry
    assert MOVE_ITEMS[0] == "home_all" and "home_all" not in RADIAL_ACTIONS  # not in the quick menu or its settings
    assert radial_entry("home_all", FakeMachine()) == ("Home all", "house-line", True)  # needs the dead-man
    pendant, run, hints, _ = _moves_pendant()
    _press(pendant, buttons={"RS"})
    _press(pendant, LY=1.0)  # up: Home all
    _press(pendant, buttons={"A"}, LY=1.0)
    assert run == [] and hints == ["Hold RT to run Home all"]
    _press(pendant, LY=1.0, rt=1.0)
    _press(pendant, buttons={"A"}, LY=1.0, rt=1.0)
    assert run == ["home_all"]
    pendant.shutdown()
