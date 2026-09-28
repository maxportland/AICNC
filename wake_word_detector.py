"""
Wake Word Detection for QtVCP

Detects "Hey Milo" (or "Hi Milo") fully offline with Vosk. No account or API
key is needed; the model lives on disk.

The recognizer is restricted to a small grammar (the wake word, a list of
everyday decoy words, and "[unk]" for everything else). That keeps CPU use
low (~6% of one Pi 5 core) while giving sound-alikes somewhere else to land.

Setup: pip install vosk, then download and extract
https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
to ~/vosk-model-small-en-us-0.15 (or the config directory).
"""

from PyQt5.QtCore import QThread, pyqtSignal
import sounddevice as sd
import json
import os
import queue
import time

try:
    from vosk import Model, KaldiRecognizer, SetLogLevel
    SetLogLevel(-1)  # Silence Kaldi's console logging
    VOSK_AVAILABLE = True
except ImportError:
    VOSK_AVAILABLE = False

MODEL_DIR_NAME = "vosk-model-small-en-us-0.15"

# Everyday words the recognizer can pick instead of "milo". Without them a
# closed grammar forces anything similar ("hey Miles", "hey mind the tool")
# onto the wake word. "milo" must stay an ordinary word here, not a
# "hey milo" phrase, or the grammar favors it over word pairs.
DECOY_WORDS = """
a about all and are at be but by can come could do for from get go good have he her here him his how i if in is it
just know like look make man me mind more my no not now of oh ok okay on one or out please right see she so some take
tell that the them then there think this time to tool up us want was way we well what when where which who why
will with would yeah yes you your hello hey hi mike miles mile mild milk mellow mello mila lo low mo mow blow slow
follow hollow willow pillow billow buddy guys wait stop start move turn x y z spindle coolant home jog feed speed zero
probe cut drill
""".split()


class WakePhraseMatcher:
    """Feeds audio to a grammar-restricted Vosk recognizer and reports wake phrase hits"""

    STABLE_BLOCKS = 3  # Partial result must hold the wake phrase this many blocks in a row
    COOLDOWN_SECONDS = 2.0

    def __init__(self, model, name="milo", greetings=("hey", "hi"), sample_rate=16000):
        self.name = name
        self.greetings = set(greetings)
        grammar = sorted(set(DECOY_WORDS) | self.greetings | {name}) + ["[unk]"]
        self.rec = KaldiRecognizer(model, sample_rate, json.dumps(grammar))
        self._stable = 0
        self._last_hit = 0.0

    def process(self, data: bytes) -> bool:
        """Feed a block of 16-bit mono PCM; return True when the wake phrase was just heard"""
        if self.rec.AcceptWaveform(data):
            heard = self._matches(json.loads(self.rec.Result()).get("text", ""))
            self._stable = 0
        else:
            # Partial results let us trigger while the user is still talking, so a
            # command spoken right after the wake phrase isn't lost. Sound-alikes
            # show up briefly as "milo" before the recognizer corrects itself,
            # so require the match to hold for a few blocks.
            if self._matches(json.loads(self.rec.PartialResult()).get("partial", "")):
                self._stable += 1
            else:
                self._stable = 0
            heard = self._stable >= self.STABLE_BLOCKS
        if not heard:
            return False
        # Start fresh so the same utterance can't trigger again
        self.rec.Reset()
        self._stable = 0
        now = time.monotonic()
        if now - self._last_hit < self.COOLDOWN_SECONDS:
            return False
        self._last_hit = now
        return True

    def _matches(self, text: str) -> bool:
        words = text.split()
        return any(a in self.greetings and b == self.name for a, b in zip(words, words[1:]))


class WakeWordDetector(QThread):
    """Lightweight wake word detection thread"""
    wake_word_detected = pyqtSignal()  # Emitted when wake word is detected
    status_message = pyqtSignal(str)  # Emit status/error messages
    initialized = pyqtSignal(bool)  # Emit True if successfully initialized, False otherwise

    SAMPLE_RATE = 16000
    BLOCK_SIZE = 1600  # 100 ms of audio per block

    def __init__(self, wake_phrase="Hey Milo", config_dir=None):
        super().__init__()
        self.wake_phrase = wake_phrase
        self.config_dir = config_dir
        self.running = False
        self.matcher = None
        self._audio = queue.Queue()

    def _find_model(self):
        """Look for the Vosk model in the config directory, then the home directory"""
        candidates = []
        if self.config_dir:
            candidates.append(os.path.join(self.config_dir, MODEL_DIR_NAME))
        candidates.append(os.path.expanduser(f"~/{MODEL_DIR_NAME}"))
        for path in candidates:
            if os.path.isdir(path):
                return path, candidates
        return None, candidates

    def _init_vosk(self):
        """Load the model and build the recognizer"""
        if not VOSK_AVAILABLE:
            self.status_message.emit("[ERROR] Vosk is not installed. Install: pip install vosk")
            return False
        model_path, candidates = self._find_model()
        if model_path is None:
            self.status_message.emit(f"[WARN] Vosk model not found. Checked: {', '.join(candidates)}")
            self.status_message.emit(f"[INFO] Download https://alphacephei.com/vosk/models/{MODEL_DIR_NAME}.zip "
                                     f"and extract it to ~/{MODEL_DIR_NAME}")
            return False
        try:
            name = self.wake_phrase.split()[-1].lower()
            self.matcher = WakePhraseMatcher(Model(model_path), name=name, sample_rate=self.SAMPLE_RATE)
            self.status_message.emit(f"[WAKE] Vosk initialized with wake phrase '{self.wake_phrase}' ({model_path})")
            return True
        except Exception as e:
            self.status_message.emit(f"[ERROR] Failed to initialize Vosk: {e}")
            return False

    def run(self):
        """Run wake word detection in background thread"""
        self.running = True
        if not self._init_vosk():
            self.initialized.emit(False)
            return
        self.initialized.emit(True)

        def callback(indata, frames, time_info, status):
            if status:
                self.status_message.emit(f"[WARN] Audio input status: {status}")
            self._audio.put(bytes(indata))

        try:
            with sd.RawInputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=self.BLOCK_SIZE,
                callback=callback
            ):
                self.status_message.emit(f"[WAKE] Listening for '{self.wake_phrase}'...")
                while self.running:
                    try:
                        data = self._audio.get(timeout=0.2)
                    except queue.Empty:
                        continue
                    if self.matcher.process(data):
                        self.status_message.emit(f"[WAKE] '{self.wake_phrase}' detected!")
                        self.wake_word_detected.emit()
        except Exception as e:
            self.status_message.emit(f"[ERROR] Wake word detection error: {e}")

    def stop(self):
        """Stop wake word detection"""
        self.running = False
        self.wait()
