"""Tests for CAM conversation trimming"""

from ai_config import CAM_HISTORY_EXCHANGES
from milo_engine import MiloEngine as HandlerClass


def _handler(messages):
    h = HandlerClass.__new__(HandlerClass)
    h.message_history = messages
    return h


def test_trim_keeps_system_and_recent_exchanges():
    system = {"role": "system", "content": "sys"}
    history = [system]
    for i in range(10):
        history += [{"role": "user", "content": f"u{i}"}, {"role": "assistant", "content": f"a{i}"}]
    history.append({"role": "user", "content": "latest"})
    h = _handler(history)
    h._trim_history()
    assert h.message_history[0] is system
    assert h.message_history[1]["role"] == "user"
    assert h.message_history[-1]["content"] == "latest"
    assert len(h.message_history) <= 2 * CAM_HISTORY_EXCHANGES + 2


def test_trim_leaves_short_history_alone():
    history = [{"role": "system", "content": "sys"}, {"role": "user", "content": "u"}]
    h = _handler(list(history))
    h._trim_history()
    assert h.message_history == history
