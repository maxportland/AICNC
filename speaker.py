"""
Milo's voice: reads replies aloud with OpenAI text to speech.

speakable() decides which conversation lines are worth saying and how to say them. The
Speaker plays them one at a time on a background thread, streaming the audio so speech
starts before the whole reply is synthesized. The engine asks it whether Milo is talking,
so the microphone never records Milo's own voice.
"""

import queue
import re
import threading
import time

import numpy as np
from PyQt5 import QtCore

from ai_config import TTS_MODEL, TTS_DEFAULT_STYLE, tts_instructions

SAMPLE_RATE = 24000  # OpenAI's "pcm" format: 24 kHz, 16-bit signed little-endian, mono
CHUNK_BYTES = 4800  # 100 ms of audio
TAIL_SECONDS = 0.3  # Quiet after speaking before the microphone may listen (room echo)
MAX_SPOKEN_ERROR = 160  # Longer errors are summarized; the details are on screen

CONFIRM_SUFFIX = " - say 'yes' or press Confirm, 'no' or Cancel to abort."
REVIEW_LINE = "The program is loaded. Please check the toolpath before you run it."
LONG_ERROR_LINE = "Something went wrong. The details are on the screen."


def clean(text):
    """Conversation text as something to say: no markdown, no code listings"""
    text = re.sub(r"```.*?(```|$)", " The code is on the screen. ", text, flags=re.S)
    text = re.sub(r"^\s*(#+|[-*•]|\d+\.)\s+", "", text, flags=re.M)
    text = re.sub(r"(\*\*|__|`)", "", text)
    text = re.sub(r"https?://\S+", "the link on the screen", text)
    return " ".join(text.split())


def speakable(line):
    """What Milo says out loud for a "[TAG] text" conversation line, or None to stay quiet"""
    match = re.match(r"\[(\w+)\]\s*(.*)", line or "", re.S)
    if not match:
        return None
    tag, text = match.group(1), match.group(2).strip()
    if tag == "MILO":
        return clean(text) or None
    if tag == "CONFIRM" and text.endswith(CONFIRM_SUFFIX):
        # "Move X to 10  [G0 X10] - say 'yes'..." -> "Move X to 10. Say yes to go ahead."
        summary = re.sub(r"\s*\[[^\]]*\]\s*$", "", text[:-len(CONFIRM_SUFFIX)]).strip()
        return f"{summary.rstrip('.')}. Say yes to go ahead." if summary else None
    if tag == "REVIEW":
        return REVIEW_LINE
    if tag == "ERROR":
        first = clean(text.split("\n", 1)[0])
        if not first:
            return None
        return first if len(first) <= MAX_SPOKEN_ERROR else LONG_ERROR_LINE
    return None


_clients = {}


def openai_speech(text, voice, api_key, speed=1.0, instructions=None):
    """Stream 24 kHz 16-bit mono PCM for `text` from OpenAI. speed: 0.25..4, 1 is normal;
    instructions: how to speak (the default style if None)"""
    if api_key not in _clients:
        from openai import OpenAI
        _clients.clear()
        _clients[api_key] = OpenAI(api_key=api_key)  # kept, so later replies reuse its connection
    with _clients[api_key].audio.speech.with_streaming_response.create(
            model=TTS_MODEL, voice=voice, input=text, speed=speed,
            instructions=instructions or tts_instructions(TTS_DEFAULT_STYLE),
            response_format="pcm") as response:
        yield from response.iter_bytes(CHUNK_BYTES)


def sound_output():
    import sounddevice as sd
    return sd.RawOutputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16")


def scale(pcm, volume):
    """16-bit PCM at `volume` (0..1)"""
    if volume >= 0.999:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) * max(0.0, volume)
    return np.clip(samples, -32768, 32767).astype(np.int16).tobytes()


class Speaker(QtCore.QObject):
    """Says things one at a time on a background thread. Use it from the main thread."""

    speaking_changed = QtCore.pyqtSignal(bool)
    failed = QtCore.pyqtSignal(str)
    _done = QtCore.pyqtSignal(int)  # generation of a finished utterance (from the worker thread)

    def __init__(self, synthesize=openai_speech, open_output=sound_output, parent=None):
        super().__init__(parent)
        self._synthesize = synthesize
        self._open_output = open_output
        self._queue = queue.Queue()
        self._generation = 0  # bumped by stop(); the worker drops anything older
        self._pending = []  # texts said and not finished yet (main thread only)
        self._waiters = []
        self._last_text, self._last_end = "", 0.0
        self._done.connect(self._on_done)
        self._thread = threading.Thread(target=self._run, name="milo-speaker", daemon=True)
        self._thread.start()

    @property
    def busy(self):
        return bool(self._pending)

    def say(self, text, voice, volume, api_key, speed=1.0, instructions=None):
        if not text:
            return
        self._pending.append(text)
        if len(self._pending) == 1:
            self.speaking_changed.emit(True)
        self._queue.put((self._generation, text, voice, volume, api_key, speed, instructions))

    def stop(self):
        """Stop talking now and forget everything queued, including when_idle callbacks"""
        self._generation += 1
        self._waiters.clear()
        if self._pending:
            self._last_text, self._last_end = self._pending[0], time.monotonic()
            self._pending.clear()
            self.speaking_changed.emit(False)

    def when_idle(self, callback):
        """Call back once Milo has finished talking (now, if it isn't). stop() cancels it."""
        if self.busy:
            self._waiters.append(callback)
        else:
            callback()

    def may_have_said(self, word, within=2.0):
        """Whether `word` is in what Milo is saying or said in the last few seconds"""
        word = word.lower()
        recent = [self._last_text] if time.monotonic() - self._last_end < within else []
        return any(word in text.lower() for text in self._pending + recent)

    def shutdown(self):
        self.stop()
        self._queue.put(None)
        self._thread.join(2.0)

    # --- main thread ---

    def _on_done(self, generation):
        if generation != self._generation or not self._pending:
            return
        self._last_text, self._last_end = self._pending.pop(0), time.monotonic()
        if self._pending:
            return
        self.speaking_changed.emit(False)
        waiters, self._waiters = self._waiters, []
        for callback in waiters:
            callback()

    # --- worker thread ---

    def _run(self):
        while True:
            item = self._queue.get()
            if item is None:
                return
            generation = item[0]
            if generation == self._generation:
                try:
                    self._speak(*item)
                except Exception as e:
                    self.failed.emit(str(e))
            self._done.emit(generation)

    def _speak(self, generation, text, voice, volume, api_key, speed, instructions):
        audio = self._synthesize(text, voice, api_key, speed, instructions)
        stream, leftover, interrupted = None, b"", False
        try:
            for chunk in audio:
                if generation != self._generation:
                    interrupted = True
                    break
                chunk = leftover + chunk
                cut = len(chunk) - len(chunk) % 2  # whole 16-bit samples only
                chunk, leftover = chunk[:cut], chunk[cut:]
                if not chunk:
                    continue
                if stream is None:
                    stream = self._open_output()
                    stream.start()
                stream.write(scale(chunk, volume))
        finally:
            close = getattr(audio, "close", None)
            if close is not None:
                close()
            if stream is not None:
                # stop() lets the buffered audio finish; abort() cuts it off
                stream.abort() if interrupted else stream.stop()
                stream.close()
        if not interrupted:
            time.sleep(TAIL_SECONDS)
