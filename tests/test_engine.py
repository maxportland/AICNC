"""Tests for MiloEngine's UI-facing behavior"""

import types

import linuxcnc

from conftest import FakeStat
from milo_engine import MiloEngine, summarize_ir


class Recorder:
    def __init__(self):
        self.events = []

    def __getattr__(self, name):
        if name.startswith("on_"):
            return lambda *args: self.events.append((name, args))
        raise AttributeError(name)


def test_summarize_ir():
    ir = {"units": "mm", "stock": {"min": [0, 0, 0], "max": [100, 50, 10]},
          "tools": [{"tool": 8, "diameter": 6.35, "description": "1/4 endmill"}],
          "ops": [{"op": "face", "tool": 8}, {"op": "pocket_2d", "tool": 8}, {"op": "weird_op"}]}
    info = summarize_ir(ir, "/x/part.ngc")
    assert info["name"] == "part.ngc"
    assert [op["name"] for op in info["ops"]] == ["Face", "Pocket", "Weird Op"]
    assert info["stock"] == "100 × 50 × 10 mm"
    assert info["tools"][0]["number"] == 8


def test_settings_round_trip(tmp_path, qapp):
    path = tmp_path / "cfg.json"
    engine = MiloEngine(listener=Recorder(), settings_path=str(path), config_dir=str(tmp_path))
    engine.save_settings(api_key="sk-test", recording_timeout=15)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    again = MiloEngine(settings_path=str(path), config_dir=str(tmp_path))
    again.load_settings()
    assert again.api_key() == "sk-test" and again.settings["recording_timeout"] == 15


def _engine(stat, tmp_path):
    listener = Recorder()
    calls = []
    api = types.SimpleNamespace(RUN=lambda line=0: calls.append("run"))
    engine = MiloEngine(listener=listener, stat_getter=lambda: stat, action_api=api,
                        settings_path=str(tmp_path / "cfg.json"), config_dir=str(tmp_path))
    from action_confirmation import ActionConfirmation
    engine.confirmation = ActionConfirmation(
        execute_callback=engine._execute_confirmed_action, log_callback=engine.log,
        resolved_callback=engine._on_confirmation_resolved,
        proposed_callback=lambda a: listener.on_proposal(a))
    return engine, listener, calls


def test_run_button_goes_through_confirmation(tmp_path, qapp):
    program = tmp_path / "part.ngc"
    program.write_text("G0 X0\nM30\n")
    stat = FakeStat(file=str(program))
    engine, listener, calls = _engine(stat, tmp_path)
    engine.propose_run()
    proposals = [args[0] for name, args in listener.events if name == "on_proposal"]
    assert proposals and proposals[0]["kind"] == "run"
    assert calls == []  # nothing runs until confirmed
    engine.confirm()
    assert calls == ["run"]
    assert ("on_show_run", ()) in listener.events
    assert ("on_proposal", (None,)) in listener.events


def test_run_button_refused_when_not_ready(tmp_path, qapp):
    stat = FakeStat(file="/x/part.ngc", task_state=linuxcnc.STATE_ESTOP)
    engine, listener, calls = _engine(stat, tmp_path)
    engine.propose_run()
    assert not engine.confirmation.pending
    assert any(name == "on_message" and args[0].startswith("[MILO] I can't start") for name, args in listener.events)


def test_missing_key_is_reported_not_sent(tmp_path, qapp):
    engine, listener, _ = _engine(FakeStat(), tmp_path)
    engine.submit("move x ten")
    lines = [args[0] for name, args in listener.events if name == "on_message"]
    assert any("API key is missing" in line for line in lines)
    assert not any(name == "on_busy" and args[0] for name, args in listener.events)
