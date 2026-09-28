"""Tests for the intent router's result normalization (no API calls)"""

from intent_router import IntentRouter


def test_mdi_is_uppercased_and_collapsed():
    result = IntentRouter._normalize({"intent": "mdi", "mdi": "g91  g0 x10", "summary": "Jog X"}, "{}")
    assert result["mdi"] == "G91 G0 X10"


def test_unknown_intent_becomes_unclear():
    result = IntentRouter._normalize({"intent": "dance"}, "{}")
    assert result["intent"] == "unclear"
    assert result["answer"]


def test_mdi_without_command_becomes_unclear():
    assert IntentRouter._normalize({"intent": "mdi", "mdi": ""}, "{}")["intent"] == "unclear"


def test_run_intent_is_kept():
    assert IntentRouter._normalize({"intent": "run"}, "{}")["intent"] == "run"


def test_power_and_estop_intents_are_kept():
    for intent in ("power_on", "power_off", "estop_reset"):
        assert IntentRouter._normalize({"intent": intent}, "{}")["intent"] == intent
