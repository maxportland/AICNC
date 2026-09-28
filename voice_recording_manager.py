"""
Voice recording management module.

This module manages voice recording and transcription for the AI Assistant.
Transcripts are handed to a callback; deciding what they mean is done by the
intent router.
"""

import re

from PyQt5.QtCore import QTimer
from typing import Optional, Callable

from workers import VoiceRecordingWorker


# What Whisper is known to produce from silence or background noise: whole transcripts
# that are just a filler phrase, and sign-off / credit lines that begin a transcript
NOISE_EXACT = ("thank you", "thanks", "you", "bye", "the end")
NOISE_PREFIXES = (
    "thank you for watching", "thanks for watching", "thank you so much for watching",
    "please subscribe", "like and subscribe", "subtitles by", "subtitled by", "captions by",
)


def is_likely_hallucination(transcript: str) -> bool:
    """True for transcripts that are almost certainly noise, not something the user said"""
    text = transcript.strip()
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True
    # Mostly non-Latin text: Whisper's typical output for noise (e.g. Japanese sign-offs)
    if sum(1 for c in letters if not c.isascii()) / len(letters) > 0.3:
        return True
    normalized = " ".join(re.sub(r"[^a-z' ]", " ", text.lower()).split())
    return normalized in NOISE_EXACT or normalized.startswith(NOISE_PREFIXES)


class VoiceListener:
    """What the voice manager reports to the UI. Subclass or pass any object with these methods."""

    def on_voice_state(self, state: str):
        """'idle', 'listening' or 'transcribing'"""

    def on_audio_level(self, level: float):
        """Microphone level while listening, 0.0 to 1.0"""

    def on_transcript(self, text: str):
        """A transcript is about to be handled (the UI can show it in the input)"""


class VoiceRecordingManager:
    """Records a spoken request, detects the end of speech, and transcribes it"""

    def __init__(self, listener=None, log_callback=None):
        """
        Args:
            listener: A VoiceListener-like object for UI updates (optional)
            log_callback: Optional callback function for logging
        """
        self.listener = listener or VoiceListener()
        self.log = log_callback if log_callback else (lambda msg: None)

        # State
        self.voice_worker = None
        self.is_recording_voice = False
        self.recording_timeout_timer = None
        self.recording_timeout_seconds = 20
        self.silence_timeout_timer = None
        self.silence_timeout_seconds = 2.0
        self.initial_speech_detected = False
        self.speech_threshold = 0.1
        self.transcript_callback = None

    def start_recording(self, api_key: str, openai_client_available: bool,
                        transcript_callback: Callable[[str], None] = None) -> bool:
        """
        Start voice recording.

        Args:
            api_key: OpenAI API key for transcription
            openai_client_available: Whether OpenAI client is available
            transcript_callback: Called with the transcribed text

        Returns:
            True if recording started, False otherwise
        """
        if not api_key:
            self.log("[ERROR] OpenAI API key required for transcription. Add it in Settings.")
            return False

        if not openai_client_available:
            self.log("[ERROR] OpenAI Python library (>=1.0.0) is not installed.")
            return False

        self.transcript_callback = transcript_callback

        # Stop any existing voice worker
        if self.voice_worker is not None:
            old = self.voice_worker
            if old.isRunning():
                # Never block the GUI waiting on a network call: discard it and keep it alive
                # until it finishes on its own
                old.cancel()
                self._discarded = old
                self._retired = [w for w in getattr(self, "_retired", []) if w.isRunning()] + [old]
                old.finished.connect(lambda *_: old.deleteLater())
            else:
                old.deleteLater()

        self._stop_all_timers()
        self.initial_speech_detected = False

        self.voice_worker = VoiceRecordingWorker(api_key)
        self.voice_worker.recording_started.connect(self._on_recording_started)
        self.voice_worker.recording_finished.connect(self._on_recording_finished)
        worker = self.voice_worker
        self.voice_worker.finished.connect(lambda text, w=worker: self._on_voice_transcription_complete(text, w))
        self.voice_worker.error.connect(self._on_voice_error)
        self.voice_worker.audio_level.connect(self._on_audio_level)
        self.voice_worker.no_speech.connect(self._on_no_speech)
        self.voice_worker.cancelled.connect(self._on_cancelled)
        self.voice_worker.start()

        self.voice_worker.start_recording()
        self.is_recording_voice = True
        self._start_recording_timeout()
        return True

    def stop_recording(self):
        """Stop listening and transcribe what was heard"""
        self._stop_all_timers()
        self.initial_speech_detected = False
        if self.voice_worker and self.is_recording_voice:
            self.voice_worker.stop_recording()
            self.is_recording_voice = False

    def cancel_recording(self):
        """Stop listening and throw the audio away, including a transcription in progress"""
        self._stop_all_timers()
        self.initial_speech_detected = False
        if self.voice_worker is not None and self.voice_worker.isRunning():
            self.voice_worker.cancel()
            self._discarded = self.voice_worker  # a transcript it still delivers is thrown away
        self.is_recording_voice = False
        self.listener.on_voice_state("idle")

    @property
    def is_busy(self) -> bool:
        """Recording or transcribing"""
        return self.voice_worker is not None and self.voice_worker.isRunning()

    def set_timeouts(self, recording_timeout: int, silence_timeout: float):
        self.recording_timeout_seconds = recording_timeout
        self.silence_timeout_seconds = silence_timeout

    # --- timers ---------------------------------------------------------------------------

    def _stop_all_timers(self):
        for name in ("recording_timeout_timer", "silence_timeout_timer"):
            timer = getattr(self, name)
            if timer is not None:
                timer.stop()
                timer.deleteLater()
                setattr(self, name, None)

    def _start_recording_timeout(self):
        self.recording_timeout_timer = QTimer()
        self.recording_timeout_timer.setSingleShot(True)
        self.recording_timeout_timer.timeout.connect(self._on_recording_timeout)
        self.recording_timeout_timer.start(int(self.recording_timeout_seconds * 1000))

    def _on_recording_timeout(self):
        if self.is_recording_voice:
            self.log(f"[INFO] Recording timeout ({self.recording_timeout_seconds}s) reached. Stopping recording...")
            self.stop_recording()

    def _start_silence_timeout(self):
        """Start the end-of-speech timer (only after speech has been heard)"""
        if self.silence_timeout_timer is not None:
            self.silence_timeout_timer.stop()
            self.silence_timeout_timer.deleteLater()
        self.silence_timeout_timer = QTimer()
        self.silence_timeout_timer.setSingleShot(True)
        self.silence_timeout_timer.timeout.connect(self._on_silence_timeout)
        self.silence_timeout_timer.start(int(self.silence_timeout_seconds * 1000))

    def _on_silence_timeout(self):
        if self.is_recording_voice and self.initial_speech_detected:
            self.log(f"[VAD] Silence timeout ({self.silence_timeout_seconds}s) reached. Stopping recording...")
            self.stop_recording()

    # --- worker events --------------------------------------------------------------------

    def _on_recording_started(self):
        self.listener.on_voice_state("listening")
        self.log("[INFO] Recording started.")

    def _on_recording_finished(self):
        self.listener.on_voice_state("transcribing")
        self.log("[INFO] Recording finished. Transcribing...")

    def _on_no_speech(self):
        self.listener.on_voice_state("idle")
        self.log("[VOICE] No speech heard; nothing sent for transcription.")

    def _on_cancelled(self):
        self.listener.on_voice_state("idle")
        self.log("[VOICE] Listening cancelled; the recording was discarded.")

    def _on_voice_transcription_complete(self, transcript: str, worker=None):
        if worker is not None and (worker is getattr(self, "_discarded", None) or worker is not self.voice_worker):
            return  # cancelled or replaced: never let a late transcript confirm anything
        self.listener.on_voice_state("idle")
        if is_likely_hallucination(transcript):
            self.log(f"[VOICE] Ignored \u201c{transcript.strip()}\u201d (likely background noise, not speech).")
            return
        self.log(f"[VOICE] Transcribed: {transcript}")
        if transcript and transcript.strip():
            self.listener.on_transcript(transcript)
            if self.transcript_callback:
                self.transcript_callback(transcript)

    def _on_audio_level(self, level: float):
        """Report the level and detect the end of speech"""
        self.listener.on_audio_level(level)
        if not self.is_recording_voice:
            return
        if level >= self.speech_threshold:
            if not self.initial_speech_detected:
                self.initial_speech_detected = True
                self.log("[VAD] Initial speech detected")
            if self.silence_timeout_timer is not None:
                self.silence_timeout_timer.stop()
        elif self.initial_speech_detected:
            if self.silence_timeout_timer is None or not self.silence_timeout_timer.isActive():
                self._start_silence_timeout()

    def _on_voice_error(self, error_message: str):
        self._stop_all_timers()
        self.initial_speech_detected = False
        self.is_recording_voice = False
        self.listener.on_voice_state("idle")
        self.log(f"[ERROR] {error_message}")
