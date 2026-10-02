"""Tests for Milo's spoken replies (no network, no sound card)"""

import threading
import time

import numpy as np
import pytest

import speaker as speaker_module
from speaker import Speaker, speakable, scale


@pytest.fixture(autouse=True)
def no_tail(monkeypatch):
    monkeypatch.setattr(speaker_module, "TAIL_SECONDS", 0.0)


def wait_for(qapp, condition, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not condition() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    qapp.processEvents()
    return condition()


class FakeOutput:
    def __init__(self, log):
        self.log = log

    def start(self):
        self.log.append("start")

    def write(self, data):
        self.log.append(bytes(data))

    def stop(self):
        self.log.append("stop")

    def abort(self):
        self.log.append("abort")

    def close(self):
        self.log.append("close")


def make_speaker(chunks_for, log):
    return Speaker(synthesize=lambda text, voice, key, speed, how: iter(chunks_for(text)),
                   open_output=lambda: FakeOutput(log))


# --- what gets said ---

def test_milo_replies_are_spoken_without_markdown():
    assert speakable("[MILO] Spindle is at **2400** rpm.") == "Spindle is at 2400 rpm."
    assert speakable("[MILO] Try this:\n```\nG0 X0\n```") == "Try this: The code is on the screen."
    assert speakable("[MILO] - one\n- two") == "one two"


def test_confirmations_drop_the_command_and_the_button_hint():
    line = "[CONFIRM] Move X to 10 mm  [G0 X10] - say 'yes' or press Confirm, 'no' or Cancel to abort."
    assert speakable(line) == "Move X to 10 mm. Say yes to go ahead."
    assert speakable("[CONFIRM] Confirmed: Move X to 10 mm") is None


def test_status_lines_stay_quiet():
    for line in ("[USER] hello", "[MDI] Executed: G0 X0", "[INFO] G-code loaded: /x.ngc", "[WAKE] ready",
                 "[CONFIG] Settings saved.", "[WARN] something", "plain text"):
        assert speakable(line) is None, line
    assert speakable("[REVIEW] G-code has been loaded.") == speaker_module.REVIEW_LINE


def test_long_errors_are_summarized():
    assert speakable("[ERROR] OpenAI API key is missing.") == "OpenAI API key is missing."
    assert speakable("[ERROR] " + "x" * 300) == speaker_module.LONG_ERROR_LINE


def test_volume_scales_samples():
    pcm = np.array([1000, -1000, 32767], dtype=np.int16).tobytes()
    assert np.frombuffer(scale(pcm, 0.5), dtype=np.int16).tolist() == [500, -500, 16383]
    assert scale(pcm, 1.0) == pcm


# --- the speaker ---

def test_speaker_plays_in_order_and_reports_when_idle(qapp):
    log, changes, idle = [], [], []
    s = make_speaker(lambda text: [text.encode() + b"-a", b"b"], log)  # odd-length chunks get rejoined
    s.speaking_changed.connect(changes.append)
    s.say("one", "marin", 1.0, "sk")
    s.say("two", "marin", 1.0, "sk")
    s.when_idle(lambda: idle.append(True))
    assert s.busy and changes == [True] and not idle
    assert wait_for(qapp, lambda: not s.busy)
    assert idle == [True] and changes == [True, False]
    audio = b"".join(x for x in log if isinstance(x, bytes))
    assert audio == b"one-ab" + b"two-ab"
    assert log.count("stop") == 2 and "abort" not in log
    s.shutdown()


def test_stop_cuts_off_speech_and_drops_the_queue(qapp):
    log, idle = [], []
    gate = threading.Event()

    def slow(text):
        yield b"aa"
        gate.wait(2)
        yield b"bb"

    s = make_speaker(slow, log)
    s.say("first", "marin", 1.0, "sk")
    s.say("second", "marin", 1.0, "sk")
    s.when_idle(lambda: idle.append(True))
    assert wait_for(qapp, lambda: b"aa" in log)
    s.stop()
    assert not s.busy
    gate.set()
    assert wait_for(qapp, lambda: "abort" in log)
    time.sleep(0.05)
    qapp.processEvents()
    assert b"bb" not in log and log.count("start") == 1  # "second" never played
    assert idle == []  # stop() cancels when_idle callbacks
    s.shutdown()


def test_synthesis_errors_are_reported(qapp):
    errors = []

    def broken(text, voice, key, speed, how):
        raise RuntimeError("no network")

    s = Speaker(synthesize=broken, open_output=lambda: FakeOutput([]))
    s.failed.connect(errors.append)
    s.say("hello", "marin", 1.0, "sk")
    assert wait_for(qapp, lambda: not s.busy)
    assert errors == ["no network"]
    s.shutdown()


def test_may_have_said(qapp):
    s = make_speaker(lambda text: [b"xx"], [])
    s.say("Say Hey Milo to talk to me.", "marin", 1.0, "sk")
    assert s.may_have_said("milo")
    assert wait_for(qapp, lambda: not s.busy)
    assert s.may_have_said("milo")  # it just finished: the wake word detector may still be catching up
    assert not s.may_have_said("milo", within=0.0)
    s.shutdown()


# --- the engine ---

class FakeSpeaker:
    def __init__(self):
        self.said, self.stops, self.waiters, self.busy = [], 0, [], False

    def say(self, text, voice, volume, api_key, speed=1.0, instructions=None):
        self.said.append((text, voice, volume) if speed == 1.0 else (text, voice, volume, speed))
        self.instructions = instructions
        self.busy = True

    def stop(self):
        self.stops += 1
        self.busy = False
        self.waiters.clear()

    def when_idle(self, callback):
        self.waiters.append(callback)

    def may_have_said(self, word, within=2.0):
        return False

    def finish(self):
        self.busy = False
        waiters, self.waiters = self.waiters, []
        for callback in waiters:
            callback()


def _engine(tmp_path, **settings):
    from test_engine import Recorder
    from milo_engine import MiloEngine
    engine = MiloEngine(listener=Recorder(), settings_path=str(tmp_path / "cfg.json"), config_dir=str(tmp_path))
    engine.settings.update(api_key="sk-test", **settings)
    engine.speaker = FakeSpeaker()
    return engine


def test_engine_speaks_only_when_enabled(tmp_path, qapp):
    engine = _engine(tmp_path)
    engine.log("[MILO] Hello.")
    assert engine.speaker.said == []
    engine.settings.update(speak_replies=True, tts_voice="cedar", speech_volume=50)
    engine.log("[MILO] Hello.")
    engine.log("[USER] hi")
    assert engine.speaker.said == [("Hello.", "cedar", 0.5)]
    engine.settings.update(speech_speed=1.3)
    engine.log("[MILO] Faster.")
    assert engine.speaker.said[-1] == ("Faster.", "cedar", 0.5, 1.3)
    assert "calmly" in engine.speaker.instructions  # the default style
    engine.settings.update(speech_style="deadpan")
    engine.log("[MILO] Dry.")
    assert "deadpan" in engine.speaker.instructions and "G fifty-four" in engine.speaker.instructions


def test_every_style_has_instructions():
    from ai_config import TTS_STYLES, tts_instructions
    assert len(TTS_STYLES) == 10 and len({key for key, _, _ in TTS_STYLES}) == 10
    for key, name, how in TTS_STYLES:
        assert tts_instructions(key).endswith(how)
    assert tts_instructions("nonsense") == tts_instructions("calm")


def test_test_button_speaks_with_replies_off(tmp_path, qapp):
    engine = _engine(tmp_path)
    engine.preview_voice()
    assert len(engine.speaker.said) == 1


def test_a_new_request_or_the_mic_stops_speech(tmp_path, qapp):
    engine = _engine(tmp_path, speak_replies=True)
    engine.intent_router = None
    engine.handle_request("   ", from_voice=False)
    assert engine.speaker.stops == 0
    engine._busy_working = lambda: True  # don't route anything
    engine.handle_request("what rpm?", from_voice=False)
    assert engine.speaker.stops == 1
    engine.voice_manager = None
    engine.toggle_voice()
    assert engine.speaker.stops == 2


def test_listening_for_yes_waits_until_milo_has_asked(tmp_path, qapp):
    engine = _engine(tmp_path, speak_replies=True)
    listens = []
    engine.voice_manager = type("VM", (), {"is_recording_voice": False, "is_busy": False})()
    engine.toggle_voice = lambda auto=False: listens.append(auto)
    engine.confirmation = type("Gate", (), {"pending": True})()
    engine.log("[CONFIRM] Home all axes - say 'yes' or press Confirm, 'no' or Cancel to abort.")
    engine._listen_for_reply()
    assert listens == []  # still talking
    engine.speaker.finish()
    assert listens == [True]


def test_no_listening_if_the_question_was_answered_meanwhile(tmp_path, qapp):
    engine = _engine(tmp_path, speak_replies=True)
    listens = []
    engine.voice_manager = type("VM", (), {"is_recording_voice": False, "is_busy": False})()
    engine.toggle_voice = lambda auto=False: listens.append(auto)
    gate = type("Gate", (), {"pending": True})()
    engine.confirmation = gate
    engine.log("[CONFIRM] Home all axes - say 'yes' or press Confirm, 'no' or Cancel to abort.")
    engine._listen_for_reply()
    gate.pending = False  # e.g. the Confirm button was tapped, without stopping the speech
    engine.speaker.finish()
    assert listens == []
