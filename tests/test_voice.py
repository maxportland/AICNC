"""Tests for speech detection and Whisper hallucination filtering"""

import numpy as np
import pytest

from voice_recording_manager import is_likely_hallucination
from workers import has_speech

RATE = 44100


@pytest.mark.parametrize("text", [
    "最後まで視聴してくださって 本当にありがとうございます。",  # the one from the screen
    "Thank you for watching!", "Thanks for watching.", "Thank you.", "you", "Please subscribe",
    "Subtitles by the Amara.org community", "", "...",
])
def test_hallucinations_are_dropped(text):
    assert is_likely_hallucination(text)


@pytest.mark.parametrize("text", [
    "Move X ten millimeters", "Yes.", "No.", "Okay", "Stop the spindle.", "Run current program.",
    "Thank you, now home the machine", "Cut a 3 inch circle, 5 mm deep",
])
def test_real_requests_pass(text):
    assert not is_likely_hallucination(text)


def _tone(seconds, amplitude):
    t = np.arange(int(RATE * seconds)) / RATE
    return (amplitude * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def test_silence_has_no_speech():
    assert not has_speech(np.zeros(RATE * 3, dtype=np.float32), RATE)


def test_quiet_hum_has_no_speech():
    assert not has_speech(_tone(3, 0.01), RATE)


def test_brief_click_is_not_speech():
    audio = np.zeros(RATE * 2, dtype=np.float32)
    audio[RATE:RATE + int(RATE * 0.1)] = _tone(0.1, 0.5)
    assert not has_speech(audio, RATE)


def test_sustained_voice_level_is_speech():
    audio = np.concatenate([np.zeros(RATE, dtype=np.float32), _tone(0.5, 0.2)])
    assert has_speech(audio.reshape(-1, 1), RATE)


class _FakeStream:
    """Stands in for sounddevice.InputStream: feeds one block of audio, never touches the mic"""
    level = 0.0

    def __init__(self, samplerate, channels, dtype, callback, blocksize):
        self.callback, self.blocksize = callback, blocksize

    def start(self):
        block = np.full((self.blocksize, 1), self.level, dtype=np.float32)
        for _ in range(10):
            self.callback(block, self.blocksize, None, None)

    def stop(self):
        pass

    def close(self):
        pass


def _run_worker(qapp, monkeypatch, level, action):
    import workers
    from PyQt5.QtCore import QEventLoop, QTimer
    _FakeStream.level = level
    monkeypatch.setattr(workers.sd, "InputStream", _FakeStream)
    calls = []

    class NoWhisper:
        def __init__(self, **kwargs):
            calls.append("client")
            raise AssertionError("Whisper must not be called")

    monkeypatch.setattr(workers, "OpenAIClient", NoWhisper)
    worker = workers.VoiceRecordingWorker("sk-test")
    events = []
    for name in ("finished", "error", "no_speech", "cancelled"):
        getattr(worker, name).connect(lambda *args, name=name: events.append(name))
    worker.start()
    worker.start_recording()
    QTimer.singleShot(150, lambda: getattr(worker, action)())
    loop = QEventLoop()
    worker.finished.connect(loop.quit); worker.error.connect(loop.quit)
    worker.no_speech.connect(loop.quit); worker.cancelled.connect(loop.quit)
    QTimer.singleShot(3000, loop.quit)
    loop.exec_()
    worker.wait(2000)
    return events, calls


def test_cancel_discards_recording(qapp, monkeypatch):
    events, calls = _run_worker(qapp, monkeypatch, level=0.3, action="cancel")
    assert events == ["cancelled"]
    assert calls == []


def test_silence_is_not_transcribed(qapp, monkeypatch):
    events, calls = _run_worker(qapp, monkeypatch, level=0.0, action="stop_recording")
    assert events == ["no_speech"]
    assert calls == []


def test_cancel_listening_drops_open_question():
    from milo_engine import MiloEngine as HandlerClass
    h = HandlerClass.__new__(HandlerClass)
    logs = []
    h.log = logs.append
    h.voice_manager = None
    h._auto_listens = 1
    h._listening_for_reply = True
    h._awaiting_clarification = True
    h.router_history = [
        {"role": "user", "content": "move it"},
        {"role": "assistant", "content": '{"intent": "unclear", "answer": "Which axis?"}'},
    ]
    h.cancel_listening()
    assert h.router_history == []
    assert not h._awaiting_clarification and not h._listening_for_reply and h._auto_listens == 0
    assert logs == ["[MILO] Okay, never mind."]


def test_silence_is_trimmed_around_speech():
    import numpy as np
    from workers import trim_silence, TRIM_PAD
    rate = 16000
    quiet = np.zeros(rate * 2, dtype=np.float32)
    speech = (0.3 * np.sin(np.arange(rate) * 2 * np.pi * 220 / rate)).astype(np.float32)
    trimmed = trim_silence(np.concatenate([quiet, speech, quiet]), rate)
    assert abs(len(trimmed) / rate - (1.0 + 2 * TRIM_PAD)) < 0.1  # one second of speech plus the padding
    assert len(trim_silence(quiet, rate)) == len(quiet)  # nothing loud: left alone (has_speech rejects it)
