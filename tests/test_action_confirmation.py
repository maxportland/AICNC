"""Tests for the confirmation gate and reply parsing"""

import pytest

from action_confirmation import ActionConfirmation, classify_reply, is_cancel_reply, strip_wake_phrase


@pytest.mark.parametrize("text,expected", [
    ("Yes.", "yes"), ("yeah go ahead", "yes"), ("Hey Milo, yes", "yes"), ("do it", "yes"), ("Confirm.", "yes"),
    ("okay", "yes"), ("no", "no"), ("No, cancel that", "no"), ("stop", "no"), ("don't", "no"),
    ("Thank you.", None), ("it", None), ("move X 10", None), ("", None),
])
def test_classify_reply(text, expected):
    assert classify_reply(text) == expected


def test_strip_wake_phrase():
    assert strip_wake_phrase("Hey Milo, move X ten") == "move X ten"
    assert strip_wake_phrase("hi milo run the program") == "run the program"
    assert strip_wake_phrase("Milo is my name") == "Milo is my name"


class _NoWidgets:
    pass


def _gate(qapp, executed, logs):
    return ActionConfirmation(_NoWidgets(), execute_callback=executed.append, log_callback=logs.append)


def test_confirm_runs_action_once(qapp):
    executed, logs = [], []
    gate = _gate(qapp, executed, logs)
    action = {"kind": "mdi", "command": "M5", "summary": "Stop the spindle"}
    gate.propose(action)
    assert gate.pending
    gate.confirm()
    gate.confirm()
    assert executed == [action]
    assert not gate.pending


def test_cancel_does_not_run(qapp):
    executed, logs = [], []
    gate = _gate(qapp, executed, logs)
    gate.propose({"kind": "home", "summary": "Home all axes"})
    gate.cancel()
    gate.confirm()
    assert executed == []
    assert any("not executed" in line for line in logs)


def test_timeout_cancels(qapp):
    from PyQt5.QtCore import QEventLoop, QTimer
    executed, logs = [], []
    gate = ActionConfirmation(_NoWidgets(), executed.append, logs.append, timeout_seconds=0)
    gate.propose({"kind": "mdi", "command": "M5", "summary": "Stop the spindle"})
    loop = QEventLoop()
    QTimer.singleShot(50, loop.quit)
    loop.exec_()
    assert not gate.pending
    assert executed == []


@pytest.mark.parametrize("text,expected", [
    ("Cancel", True), ("never mind", True), ("Nevermind.", True), ("no thanks", True), ("forget it", True),
    ("Hey Milo, cancel that", True), ("No.", True),
    ("No, I meant move Y ten", False), ("move Y ten", False), ("", False), ("okay", False),
])
def test_is_cancel_reply(text, expected):
    assert is_cancel_reply(text) == expected


def test_resolved_callback_runs_on_confirm_cancel_and_timeout(qapp):
    from PyQt5.QtCore import QEventLoop, QTimer
    resolved = []
    gate = ActionConfirmation(_NoWidgets(), execute_callback=lambda a: None, log_callback=lambda m: None,
                              timeout_seconds=0, resolved_callback=lambda: resolved.append(1))
    action = {"kind": "mdi", "command": "M5", "summary": "Stop the spindle"}
    gate.propose(action); gate.confirm()
    gate.propose(action); gate.cancel()
    gate.propose(action)
    loop = QEventLoop(); QTimer.singleShot(50, loop.quit); loop.exec_()
    assert len(resolved) == 3
    gate.cancel()  # nothing pending: no extra callback
    assert len(resolved) == 3


@pytest.mark.parametrize("text,expected", [
    ("No, move Y ten", None), ("stop the spindle", None), ("no, don't do that", "no"),
    ("run it", None), ("run", None),
])
def test_refusals_with_a_new_request_are_not_just_no(text, expected):
    assert classify_reply(text) == expected


def test_confirm_with_a_stale_id_runs_nothing(qapp):
    executed, logs = [], []
    gate = _gate(qapp, executed, logs)
    gate.propose({"kind": "run", "summary": "Run part.ngc"})
    shown = gate.action["id"]
    gate.propose({"kind": "mdi", "command": "G91 G0 X10", "summary": "Move X 10"})
    assert any("Superseded" in line for line in logs)
    gate.confirm(shown)
    assert executed == [] and gate.pending
    gate.confirm(gate.action["id"])
    assert executed and executed[0]["summary"] == "Move X 10"
