"""Machine power by voice: safety checks, execution and the E-stop refusal"""

import types

import linuxcnc
import pytest

from conftest import FakeStat
from machine_safety import check_power_off, check_power_on, program_running


def test_power_on_checks():
    assert check_power_on(FakeStat(task_state=linuxcnc.STATE_OFF)) is None
    assert check_power_on(FakeStat(task_state=linuxcnc.STATE_ESTOP_RESET)) is None
    assert "E-stop is active" in check_power_on(FakeStat(task_state=linuxcnc.STATE_ESTOP))
    assert "already on" in check_power_on(FakeStat(task_state=linuxcnc.STATE_ON))


def test_power_off_checks():
    assert check_power_off(FakeStat(task_state=linuxcnc.STATE_ON)) is None
    assert "already off" in check_power_off(FakeStat(task_state=linuxcnc.STATE_OFF))
    assert "already off" in check_power_off(FakeStat(task_state=linuxcnc.STATE_ESTOP))


def test_program_running():
    assert not program_running(FakeStat())
    assert program_running(FakeStat(interp_state=linuxcnc.INTERP_READING))


class FakeAction:
    def __init__(self):
        self.calls = []

    def SET_MACHINE_STATE(self, state):
        self.calls.append(state)


def _handler(stat):
    from milo_engine import MiloEngine as HandlerClass
    h = HandlerClass.__new__(HandlerClass)
    h.action = FakeAction()
    h.status = types.SimpleNamespace(stat=stat)
    h.logs = []
    h.log = h.logs.append
    return h


@pytest.mark.parametrize("kind,state,expected", [
    ("power_on", linuxcnc.STATE_OFF, [True]),
    ("power_off", linuxcnc.STATE_ON, [False]),
    ("power_on", linuxcnc.STATE_ESTOP, []),  # E-stop pressed after the card appeared: re-check refuses
    ("power_off", linuxcnc.STATE_OFF, []),
])
def test_confirmed_power_change_rechecks_state(kind, state, expected):
    h = _handler(FakeStat(task_state=state))
    h._execute_confirmed_action({"kind": kind, "summary": "x"})
    assert h.action.calls == expected
    if not expected:
        assert h.logs and h.logs[-1].startswith("[MILO] Not changing power")


class FakeGate:
    def __init__(self):
        self.proposed = []
        self.pending = False

    def propose(self, action):
        self.proposed.append(action)


def _route(h, intent):
    from milo_engine import MiloEngine as HandlerClass
    h.w = types.SimpleNamespace()
    h.router_history = []
    h.routing_request = ("text", False)
    h.confirmation = FakeGate()
    HandlerClass._on_intent(h, {"intent": intent, "mdi": "", "summary": "", "answer": "", "raw": "{}"})
    return h


def test_power_off_warns_when_program_running():
    h = _route(_handler(FakeStat(task_state=linuxcnc.STATE_ON, interp_state=linuxcnc.INTERP_READING)), "power_off")
    assert h.confirmation.proposed == [{"kind": "power_off",
                                        "summary": "Turn the machine off (this stops the running program)"}]


def test_power_on_refused_in_estop_without_proposing():
    h = _route(_handler(FakeStat(task_state=linuxcnc.STATE_ESTOP)), "power_on")
    assert h.confirmation.proposed == []
    assert "E-stop is active" in h.logs[-1]


def test_estop_reset_is_always_refused():
    from milo_engine import ESTOP_RESET_ANSWER
    h = _route(_handler(FakeStat(task_state=linuxcnc.STATE_ESTOP)), "estop_reset")
    assert h.confirmation.proposed == []
    assert h.action.calls == []
    assert h.logs[-1] == f"[MILO] {ESTOP_RESET_ANSWER}"
