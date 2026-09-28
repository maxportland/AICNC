"""Circle moves around the current position: generated G-code, checks and execution"""

import types

import linuxcnc
import pytest

import machine_safety
from conftest import FakeStat
from intent_router import IntentRouter
from machine_safety import circle_commands, validate_circle


@pytest.fixture
def centered(tmp_path):
    ini = tmp_path / "machine.ini"
    ini.write_text("[TRAJ]\nMAX_LINEAR_VELOCITY = 83.33\n")
    machine_safety._ini_cache.clear()
    return FakeStat(position=[250.0, 87.5, -10.0] + [0.0] * 6, ini_filename=str(ini))


def test_commands_clockwise():
    assert circle_commands(100, True, 500, FakeStat()) == [
        "G91 G1 X50.0000 F500",
        "G91 G91.1 G2 X0 Y0 I-50.0000 J0 F500",
        "G91 G1 X-50.0000 F500",
    ]


def test_commands_counter_clockwise():
    assert circle_commands(20, False, 300, FakeStat())[1] == "G91 G91.1 G3 X0 Y0 I-10.0000 J0 F300"


def test_circle_that_fits(centered):
    assert validate_circle(100, 500, centered) is None


def test_the_reported_position_is_too_close_to_the_limits(centered):
    """At machine X480 Y10, a 100 mm circle would leave the travel in X and Y"""
    centered.position = [480.0, 10.0, -10.0] + [0.0] * 6
    reason = validate_circle(100, 500, centered)
    assert "X 430.000 to 530.000" in reason and "[0.000, 500.000]" in reason


def test_feed_above_machine_maximum(centered):
    assert "maximum feed of 4999.8" in validate_circle(50, 8000, centered)


@pytest.mark.parametrize("change,reason", [
    ({"homed": (1, 0, 1)}, "not homed"),
    ({"task_state": linuxcnc.STATE_ESTOP}, "E-stop"),
])
def test_machine_must_be_ready(centered, change, reason):
    centered.__dict__.update(change)
    assert reason in validate_circle(50, 500, centered)


def test_bad_sizes(centered):
    assert "greater than zero" in validate_circle(0, 500, centered)
    assert "feed rate must be positive" in validate_circle(10, 0, centered)


def test_router_normalizes_circle():
    result = IntentRouter._normalize(
        {"intent": "circle", "circle": {"diameter": "100", "direction": "counter-clockwise", "feed": None}}, "{}")
    assert result["intent"] == "circle"
    assert result["circle"] == {"diameter": 100.0, "clockwise": False, "feed": None}
    assert IntentRouter._normalize({"intent": "circle", "circle": {}}, "{}")["intent"] == "unclear"


def test_execution_sends_lines_then_restores_modes(centered):
    from milo_engine import MiloEngine as HandlerClass
    calls = []
    h = HandlerClass.__new__(HandlerClass)
    h.action = types.SimpleNamespace(CALL_MDI=lambda line: calls.append(line))
    h.status = types.SimpleNamespace(stat=centered)
    h.log = lambda message: None
    h._execute_confirmed_action({"kind": "circle", "diameter": 100.0, "clockwise": True, "feed": 500,
                                 "summary": "circle"})
    assert calls == circle_commands(100, True, 500, centered) + ["G90"]
