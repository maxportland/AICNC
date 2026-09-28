"""
Worker thread classes for non-blocking operations in the AI Assistant.

This module contains QThread subclasses that handle long-running operations
without blocking the UI thread.
"""

from PyQt5.QtCore import QThread, pyqtSignal, QMutex, QMutexLocker
import sounddevice as sd
import soundfile as sf
import tempfile
import numpy as np

from ai_config import CAM_MODEL, TRANSCRIPTION_MODEL, TRANSCRIPTION_LANGUAGE, TRANSCRIPTION_PROMPT

# Speech detection: RMS of a 100 ms block above this counts as speech (matches the
# recording manager's VAD: level = rms * 3 >= 0.1), and a recording needs this many
# speech blocks to be worth transcribing.
SPEECH_RMS = 0.1 / 3.0
MIN_SPEECH_BLOCKS = 3


def has_speech(recording, sample_rate, block_seconds=0.1):
    """True if the recording has enough speech-level audio to transcribe"""
    samples = np.asarray(recording, dtype=np.float32).reshape(-1)
    block = max(1, int(sample_rate * block_seconds))
    loud = 0
    for start in range(0, len(samples), block):
        chunk = samples[start:start + block]
        if chunk.size and np.sqrt(np.mean(chunk ** 2)) >= SPEECH_RMS:
            loud += 1
            if loud >= MIN_SPEECH_BLOCKS:
                return True
    return False

# Import OpenAI client
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from openai import OpenAI as OpenAIClientType
try:
    from openai import OpenAI as OpenAIClient
except ImportError:
    OpenAIClient = None


class OpenAIWorker(QThread):
    """Worker thread for making OpenAI API calls without blocking the UI"""
    finished = pyqtSignal(str)  # Emits the response text
    error = pyqtSignal(str)  # Emits error message
    
    def __init__(self, api_key, message_history):
        super().__init__()
        self.api_key = api_key
        self.message_history = message_history
    
    def run(self):
        """Run the OpenAI API call in this thread"""
        try:
            if OpenAIClient is None:
                self.error.emit("OpenAI Python library (>=1.0.0) is not installed.")
                return
            
            if not self.api_key:
                self.error.emit("OpenAI API key is missing.")
                return
            
            # Create a fresh client for each call
            client = OpenAIClient(api_key=self.api_key)
            
            # Make the API call (this will block this thread, not the UI thread)
            # JSON mode guarantees a parseable CAM IR object (no prose, no arithmetic in numbers)
            response = client.chat.completions.create(
                model=CAM_MODEL,
                messages=self.message_history,
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            result = response.choices[0].message.content
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"OpenAI API failed: {str(e)}")


class CamWorker(QThread):
    """Worker thread that turns CAM IR into G-code without freezing the UI"""
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    finished = pyqtSignal(object, object)  # (G-code path or None, error message or None)

    def __init__(self, ir_data, **options):
        """
        Args:
            ir_data: CAM IR dict
            options: Passed to CAMIRProcessor.process_ir_to_gcode (tools, machine_units, max_rpm, ...)
        """
        super().__init__()
        self.ir_data = ir_data
        self.options = options

    def run(self):
        # Imported here so the CAM library loads only when it's needed
        from cam_ir_processor import CAMIRProcessor
        processor = CAMIRProcessor(log_callback=self.log.emit, progress_callback=self.progress.emit)
        try:
            output_path, _, error = processor.process_ir_to_gcode(self.ir_data, **self.options)
        except Exception as e:
            output_path, error = None, f"Failed to process CAM IR: {e}"
        self.finished.emit(output_path, error)


class IntentRouterWorker(QThread):
    """Worker thread for the intent routing call so the UI stays responsive"""
    finished = pyqtSignal(object)  # Emits the router result dict
    error = pyqtSignal(str)  # Emits error message

    def __init__(self, router, text, machine_context, history):
        super().__init__()
        self.router = router
        self.text = text
        self.machine_context = machine_context
        self.history = history

    def run(self):
        """Run the routing call in this thread"""
        try:
            result = self.router.route(self.text, self.machine_context, self.history)
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"Intent routing failed: {str(e)}")


class VoiceRecordingWorker(QThread):
    """Worker thread for recording and transcribing voice without blocking the UI"""
    finished = pyqtSignal(str)  # Emits the transcribed text
    error = pyqtSignal(str)  # Emits error message
    recording_started = pyqtSignal()  # Emitted when recording starts
    recording_finished = pyqtSignal()  # Emitted when recording finishes (before transcription)
    audio_level = pyqtSignal(float)  # Emits audio level (0.0 to 1.0) for VU meter
    no_speech = pyqtSignal()  # Recording had no speech; nothing was sent for transcription
    cancelled = pyqtSignal()  # The user cancelled; audio was discarded
    
    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key
        self.is_recording = False
        self.should_stop = False
        self.recording_data = []
        self.sample_rate = 44100
        self.stream = None
        self.is_cancelled = False
        self._lock = QMutex()  # For thread-safe access
    
    def start_recording(self):
        """Request to start recording (called from main thread)"""
        with QMutexLocker(self._lock):
            if self.is_recording:
                return
            self.is_recording = True
            self.should_stop = False
            self.recording_data = []
    
    def stop_recording(self):
        """Request to stop recording (called from main thread)"""
        with QMutexLocker(self._lock):
            self.should_stop = True

    def cancel(self):
        """Stop recording and discard it: nothing is transcribed (called from main thread)"""
        with QMutexLocker(self._lock):
            self.is_cancelled = True
            self.should_stop = True

    def _cancelled(self):
        with QMutexLocker(self._lock):
            return self.is_cancelled
    
    def run(self):
        """Thread run method - handles recording in this thread"""
        # Wait for start_recording to be called
        while True:
            with QMutexLocker(self._lock):
                if self.is_recording or self.should_stop:
                    break
            self.msleep(10)
        
        with QMutexLocker(self._lock):
            if not self.is_recording or self.should_stop:
                return
        
        # Emit signal that recording has started
        self.recording_started.emit()
        
        # Define callback for audio stream
        def callback(indata, frames, time, status):
            if status:
                print(f"[WARN] Audio input status: {status}")
            
            should_record = False
            with QMutexLocker(self._lock):
                if self.is_recording and not self.should_stop:
                    should_record = True
                    self.recording_data.append(indata.copy())
            
            if should_record:
                # Calculate audio level (RMS) for VU meter
                # indata is shape (frames, channels), so for mono it's (frames, 1)
                audio_chunk = indata[:, 0] if indata.ndim > 1 else indata
                rms = np.sqrt(np.mean(audio_chunk**2))
                # Normalize to 0-1 range (assuming max RMS is around 0.3-0.5 for normal speech)
                # Use a more sensitive scale for better visual feedback
                normalized_level = min(1.0, rms * 3.0)  # Scale factor for better sensitivity
                self.audio_level.emit(normalized_level)
        
        # Start recording stream in this thread
        try:
            self.stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=1,
                dtype=np.float32,
                callback=callback,
                blocksize=int(self.sample_rate * 0.1)  # 100ms blocks
            )
            self.stream.start()
            
            # Keep running until stop is requested
            while True:
                with QMutexLocker(self._lock):
                    if not self.is_recording or self.should_stop:
                        break
                self.msleep(100)  # Small sleep to prevent CPU spinning
            
            # Stop the stream
            if self.stream:
                self.stream.stop()
                self.stream.close()
                self.stream = None
            
            if self._cancelled():
                with QMutexLocker(self._lock):
                    self.is_recording = False
                self.cancelled.emit()
                return

            # Signal that recording is finished
            self.recording_finished.emit()
            
            # Process the recorded audio
            with QMutexLocker(self._lock):
                recording_data_copy = list(self.recording_data)
                self.is_recording = False
            
            if not recording_data_copy:
                self.error.emit("No audio data recorded.")
                return
            
            # Concatenate all recorded chunks
            try:
                recording = np.concatenate(recording_data_copy, axis=0)

                # Whisper invents text for silence and noise ("Thanks for watching!"),
                # so don't send recordings without speech at all
                if not has_speech(recording, self.sample_rate):
                    self.no_speech.emit()
                    return

                # Save to temporary file
                with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmpfile:
                    filepath = tmpfile.name
                
                sf.write(filepath, recording, self.sample_rate)
                
                # Check prerequisites
                if not self.api_key:
                    self.error.emit("OpenAI API key required for transcription.")
                    return
                
                if OpenAIClient is None:
                    self.error.emit("OpenAI Python library (>=1.0.0) is not installed.")
                    return
                
                # Transcribe audio (blocks this thread, not UI thread)
                client = OpenAIClient(api_key=self.api_key)
                with open(filepath, "rb") as audio_file:
                    response = client.audio.transcriptions.create(
                        model=TRANSCRIPTION_MODEL,
                        file=audio_file,
                        language=TRANSCRIPTION_LANGUAGE,
                        prompt=TRANSCRIPTION_PROMPT,
                    )
                transcript = response.text
                # Cancelled while Whisper was working: drop the result
                if self._cancelled():
                    self.cancelled.emit()
                else:
                    self.finished.emit(transcript)
                
                # Clean up temp file
                try:
                    import os
                    os.unlink(filepath)
                except Exception:
                    pass
                    
            except Exception as e:
                self.error.emit(f"Voice transcription failed: {str(e)}")
                
        except Exception as e:
            with QMutexLocker(self._lock):
                self.is_recording = False
            self.error.emit(f"Failed to start recording: {str(e)}")

