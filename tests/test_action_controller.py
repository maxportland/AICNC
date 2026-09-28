"""Tests for proposing and executing machine actions, without the chat widget"""

import linuxcnc
import pytest

from action_controller import MACHINE_INTENTS, execute_action, propose_action
from conftest import FakeStat


class FakeAction:
    """Records what would have been sent to the machine"""

    def __init__(self, mdi_result=None):
        self.calls = []
        self.mdi_result = mdi_result

    def CALL_MDI(self, line):
        self.calls.append(("mdi", line))
        return self.mdi_result

    def SET_MACHINE_HOMING(self, joint):
        self.calls.append(("home", joint))

    def SET_MACHINE_STATE(self, state):
        self.calls.append(("power", state))

    def RUN(self):
        self.calls.append(("run",))


def _result(intent, **fields):
    return {"intent": intent, "mdi": "", "summary": "", "answer": "", "raw": "{}", **fields}


def test_mdi_proposal_notes_machine_target(stat):
    action, refusal = propose_action(_result("mdi", mdi="G0 X10", summary="Move X to 10"), stat)
    assert refusal is None
    assert action["kind"] == "mdi" and action["command"] == "G0 X10"
    assert action["summary"].startswith("Move X to 10 (G54 work coordinates")


def test_mdi_outside_limits_is_refused_without_an_action(stat):
    action, refusal = propose_action(_result("mdi", mdi="G0 X9999"), stat)
    assert action is None
    assert refusal.startswith("I won't run 'G0 X9999'")


def test_home_refused_in_estop():
    action, refusal = propose_action(_result("home"), FakeStat(task_state=linuxcnc.STATE_ESTOP))
    assert action is None and "E-stop" in refusal


def test_run_proposal_names_the_loaded_file(tmp_path):
    program = tmp_path / "part.ngc"
    program.write_text("M2\n")
    action, refusal = propose_action(_result("run"), FakeStat(file=str(program)))
    assert refusal is None
    assert action == {"kind": "run", "file": str(program), "summary": "Run part.ngc from the start"}


def test_non_machine_intent_is_rejected(stat):
    assert "answer" not in MACHINE_INTENTS
    with pytest.raises(ValueError):
        propose_action(_result("answer"), stat)


def test_confirmed_mdi_runs_and_restores_g90(stat):
    api = FakeAction()
    message, ran = execute_action({"kind": "mdi", "command": "G91 G0 X5", "summary": "x"}, stat, api)
    assert ran and message == "[MDI] Executed: G91 G0 X5"
    assert api.calls == [("mdi", "G91 G0 X5"), ("mdi", "G90")]


def test_confirmed_mdi_is_rechecked_against_current_state(stat):
    action, _ = propose_action(_result("mdi", mdi="G0 X10"), stat)
    # The machine went into E-stop while the confirmation card was open
    api = FakeAction()
    message, ran = execute_action(action, FakeStat(task_state=linuxcnc.STATE_ESTOP), api)
    assert not ran and api.calls == []
    assert message.startswith("[MILO] Not running 'G0 X10'")


def test_refused_mdi_stops_before_restoring_modes(stat):
    api = FakeAction(mdi_result=-1)
    message, ran = execute_action({"kind": "mdi", "command": "G91 G0 X5", "summary": "x"}, stat, api)
    assert not ran and message == "[ERROR] LinuxCNC refused 'G91 G0 X5'."
    assert api.calls == [("mdi", "G91 G0 X5")]


def test_run_refused_when_a_different_program_was_loaded(tmp_path):
    first, second = tmp_path / "a.ngc", tmp_path / "b.ngc"
    first.write_text("M2\n")
    second.write_text("M2\n")
    api = FakeAction()
    message, ran = execute_action({"kind": "run", "file": str(first), "summary": "x"},
                                  FakeStat(file=str(second)), api)
    assert not ran and api.calls == []
    assert "A different program is loaded now (b.ngc)" in message


def test_confirmed_home_runs(stat):
    api = FakeAction()
    message, ran = execute_action({"kind": "home", "summary": "x"}, stat, api)
    assert ran and api.calls == [("home", -1)]
