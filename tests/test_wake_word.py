"""Wake phrase detection with synthesized speech (needs the Vosk model and espeak-ng)"""

import os
import shutil
import subprocess

import numpy as np
import pytest

vosk = pytest.importorskip("vosk")
sf = pytest.importorskip("soundfile")
from wake_word_detector import MODEL_DIR_NAME, WakePhraseMatcher  # noqa: E402

MODEL_PATH = os.path.expanduser(f"~/{MODEL_DIR_NAME}")
pytestmark = pytest.mark.skipif(
    not os.path.isdir(MODEL_PATH) or shutil.which("espeak-ng") is None,
    reason="needs the Vosk model and espeak-ng",
)


@pytest.fixture(scope="module")
def model():
    return vosk.Model(MODEL_PATH)


def _speech(tmp_path, text, voice="en-us", speed=150):
    wav = tmp_path / "tts.wav"
    subprocess.run(["espeak-ng", "-v", voice, "-s", str(speed), "-w", str(wav), text], check=True)
    x, sr = sf.read(str(wav), dtype="float32")
    t = np.arange(0, len(x) / sr, 1 / 16000)
    y = np.interp(t, np.arange(len(x)) / sr, x)
    pad = np.zeros(8000, dtype=np.float32)
    return (np.concatenate([pad, y, pad, pad]) * 32767).astype(np.int16).tobytes()


def _hits(model, audio):
    matcher = WakePhraseMatcher(model)
    return sum(matcher.process(audio[i:i + 3200]) for i in range(0, len(audio), 3200))


@pytest.mark.parametrize("text,voice,speed", [
    ("hey milo", "en-us", 150), ("hi milo", "en-us", 150), ("Hey Milo, move X ten millimeters", "en-us", 150),
    ("hey milo", "en-gb", 150), ("hey milo", "en-us", 200), ("hey milo", "en-us+f3", 150),
])
def test_wake_phrase_detected_once(tmp_path, model, text, voice, speed):
    assert _hits(model, _speech(tmp_path, text, voice, speed)) == 1


@pytest.mark.parametrize("text", [
    "hey mike", "hey Miles", "hey mind the tool", "hey buddy", "hi Miles", "hey, what time is it",
    "move X ten millimeters", "Milo", "the spindle is running at twelve thousand",
])
def test_sound_alikes_ignored(tmp_path, model, text):
    assert _hits(model, _speech(tmp_path, text)) == 0
