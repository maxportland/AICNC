"""
Worker thread classes for non-blocking operations in the AI Assistant.

This module contains QThread subclasses that handle long-running operations
without blocking the UI thread.
"""

from PyQt5.QtCore import QThread, pyqtSignal, QMutex, QMutexLocker
import sounddevice as sd
import soundfile as sf
import tempfile
import time
import numpy as np

from ai_config import TRANSCRIPTION_LANGUAGE, TRANSCRIPTION_PROMPT, job_model, job_options, transcription_model

# Speech detection: RMS of a 100 ms block above this counts as speech (matches the
# recording manager's VAD: level = rms * 3 >= 0.1), and a recording needs this many
# speech blocks to be worth transcribing.
SPEECH_RMS = 0.1 / 3.0
MIN_SPEECH_BLOCKS = 3
# Speech is recorded at 16 kHz (all speech-to-text models work at 16 kHz anyway) and sent as
# 16-bit audio with the silence before and after trimmed: about a quarter of the upload.
RECORD_RATE = 16000
TRIM_RMS = SPEECH_RMS / 3  # quieter than this at the ends counts as silence
TRIM_PAD = 0.3  # s of audio kept around the speech, so word edges aren't clipped


def trim_silence(recording, sample_rate, block_seconds=0.05):
    """The recording without the silence before the first and after the last sound"""
    samples = np.asarray(recording, dtype=np.float32).reshape(-1)
    block = max(1, int(sample_rate * block_seconds))
    loud = [i for i in range(0, len(samples), block)
            if np.sqrt(np.mean(samples[i:i + block] ** 2)) >= TRIM_RMS]
    if not loud:
        return samples
    pad = int(TRIM_PAD * sample_rate)
    return samples[max(0, loud[0] - pad):min(len(samples), loud[-1] + block + pad)]


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
                model=job_model("cam"),
                messages=self.message_history,
                **job_options("cam", temperature=0.2),
                response_format={"type": "json_object"},
            )
            result = response.choices[0].message.content
            self.finished.emit(result)
        except Exception as e:
            self.error.emit(f"OpenAI API failed: {str(e)}")


REVIEW_PROMPT = (
    "You check CNC programs before they run. The image is a simulated top view of the finished part "
    "(+Y up): light tan is the untouched stock surface, blue is where material was cut, darker blue is deeper. "
    "Judge only whether the carved result clearly matches what the user asked for: missing or invisible "
    "features, wrong or unrecognizable shapes, features in the wrong place or merged together. Don't comment on "
    "feeds, speeds, tools, efficiency or small differences in proportion; a reasonable interpretation is a match. "
    "Reply with JSON only: {\"matches\": true|false, \"problems\": [\"...\"]}. Each problem must say what is "
    "wrong in the picture and how the program should change (which operation, what shape, depth or position)."
)


class ReviewWorker(QThread):
    """Asks a vision model whether the simulated result looks like what was requested"""
    finished = pyqtSignal(object)  # {"matches": bool, "problems": [str]}
    error = pyqtSignal(str)

    def __init__(self, api_key, image_path, requests, operations):
        super().__init__()
        self.api_key = api_key
        self.image_path = image_path
        self.requests = requests
        self.operations = operations

    def run(self):
        try:
            import base64
            import json
            with open(self.image_path, "rb") as f:
                image = base64.b64encode(f.read()).decode("ascii")
            asked = self.requests[-1] if self.requests else ""
            text = f"The user asked: {asked}\n"
            if len(self.requests) > 1:
                text += "Earlier requests in this conversation (the program may build on them): " \
                        + " | ".join(self.requests[:-1]) + "\n"
            text += "The program's operations, in order:\n" + "\n".join(self.operations)
            client = OpenAIClient(api_key=self.api_key)
            response = client.chat.completions.create(
                model=job_model("review"),
                messages=[{"role": "system", "content": REVIEW_PROMPT},
                          {"role": "user", "content": [
                              {"type": "text", "text": text},
                              {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image}"}},
                          ]}],
                **job_options("review", temperature=0),
                response_format={"type": "json_object"},
            )
            verdict = json.loads(response.choices[0].message.content)
            problems = [str(p) for p in verdict.get("problems") or [] if str(p).strip()]
            self.finished.emit({"matches": bool(verdict.get("matches", not problems)), "problems": problems})
        except Exception as e:
            self.error.emit(f"Visual check failed: {e}")


class CamWorker(QThread):
    """Worker thread that turns CAM IR into G-code without freezing the UI"""
    log = pyqtSignal(str)
    progress = pyqtSignal(int)
    # (G-code path or None, error message or None, the simulation's review dict or None)
    finished = pyqtSignal(object, object, object)

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
        self.finished.emit(output_path, error, processor.last_review if output_path else None)


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
    transcribed_info = pyqtSignal(str)  # How the transcription went (model, time, audio length)
    cancelled = pyqtSignal()  # The user cancelled; audio was discarded
    
    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key
        self.is_recording = False
        self.should_stop = False
        self.recording_data = []
        self.sample_rate = RECORD_RATE
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
                
                trimmed = trim_silence(recording, self.sample_rate)
                sf.write(filepath, trimmed, self.sample_rate, subtype="PCM_16")
                
                # Check prerequisites
                if not self.api_key:
                    self.error.emit("OpenAI API key required for transcription.")
                    return
                
                if OpenAIClient is None:
                    self.error.emit("OpenAI Python library (>=1.0.0) is not installed.")
                    return
                
                # Transcribe audio (blocks this thread, not UI thread)
                client = OpenAIClient(api_key=self.api_key)
                model = transcription_model()
                started = time.monotonic()
                with open(filepath, "rb") as audio_file:
                    response = client.audio.transcriptions.create(
                        model=model,
                        file=audio_file,
                        language=TRANSCRIPTION_LANGUAGE,
                        prompt=TRANSCRIPTION_PROMPT,
                    )
                transcript = response.text
                import os
                self.transcribed_info.emit(
                    f"[VOICE] Transcribed in {time.monotonic() - started:.1f} s by {model} "
                    f"({len(trimmed) / self.sample_rate:.1f} s of audio, {os.path.getsize(filepath) // 1024} KB)")
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



FEEDS_PROMPT = (
    "You recommend CNC milling speeds and feeds. For each tool listed, give a spindle speed, a cutting feed\n"
    "and a plunge feed for the given material, as starting points for this machine.\n"
    "- Prefer vendor cutting data from the tool information when present.\n"
    "- Otherwise use a typical surface speed and chip load for the material, cutter diameter and flute count.\n"
    "- rpm must not exceed the spindle maximum. When the maximum limits rpm, keep the chip load per tooth\n"
    "  (feed = rpm x flutes x chip load) rather than the catalog feed.\n"
    "- Plunge feed is usually a third to a half of the cutting feed; for drills it's the drilling feed.\n"
    "- A small hobby mill is less rigid than an industrial machine: be conservative.\n"
    "- Units: feeds in the program's units per minute.\n"
    "- If a tool's diameter is missing or suspect, infer it from the description and say so in why.\n"
    "Reply with JSON only: {\"tools\": {\"<tool number>\": {\"rpm\": number, \"feed\": number, \"plunge\": number,\n"
    "\"why\": \"one short sentence: chip load and surface speed used\"}}, \"notes\": \"anything the machinist\n"
    "should watch for, one or two sentences\"}"
)


class FeedsWorker(QThread):
    """Recommends speeds and feeds for the loaded program's tools and writes an adjusted copy"""
    finished = pyqtSignal(object)  # {"path", "changes", "why", "notes", "units"} or {"error": str}

    def __init__(self, api_key, path, material, tools_context, start_tool, max_rpm, max_feed, units, output_dir):
        super().__init__()
        self.api_key = api_key
        self.path = path
        self.material = material
        self.tools_context = tools_context
        self.start_tool = start_tool
        self.max_rpm = max_rpm
        self.max_feed = max_feed
        self.units = units
        self.output_dir = output_dir

    def run(self):
        try:
            self.finished.emit(self._adjust())
        except Exception as e:
            self.finished.emit({"error": f"Adjusting the feeds failed: {e}"})

    def _adjust(self):
        import json
        import os
        import re
        import feeds_adjust
        with open(self.path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        try:
            uses, program_units = feeds_adjust.analyze(text, self.start_tool)
        except feeds_adjust.Unsupported as e:
            return {"error": f"I can't safely adjust this program: {e}."}
        units = program_units or self.units
        adjustable = {t: u for t, u in uses.items() if not u.locked and (u.cut or u.plunge or u.rpm)}
        if not adjustable:
            return {"error": "This program has no feed moves or spindle speeds I can adjust."}
        request = [f"Material: {self.material}", f"Program units: {units}",
                   f"Spindle maximum: {self.max_rpm:g} rpm" if self.max_rpm else "Spindle maximum: unknown",
                   "Tools in the program and what it uses now (most common values):"]
        for tool, use in sorted(adjustable.items()):
            now = use.summary()
            request.append(f"- T{tool}: rpm {now['rpm'] or 'not set'}, cutting feed {now['feed'] or 'none'}, "
                           f"plunge feed {now['plunge'] or 'none'}")
        request.append("Tool information:\n" + self.tools_context)
        client = OpenAIClient(api_key=self.api_key)
        response = client.chat.completions.create(
            model=job_model("cam"),
            messages=[{"role": "system", "content": FEEDS_PROMPT}, {"role": "user", "content": "\n".join(request)}],
            **job_options("cam", temperature=0.2),
            response_format={"type": "json_object"},
        )
        reply = json.loads(response.choices[0].message.content)
        targets, why = {}, {}
        for key, values in (reply.get("tools") or {}).items():
            try:
                tool = int(str(key).lstrip("Tt"))
            except ValueError:
                continue
            if tool in adjustable and isinstance(values, dict):
                targets[tool] = {k: float(values[k]) for k in ("rpm", "feed", "plunge")
                                 if isinstance(values.get(k), (int, float)) and values[k] > 0}
                why[tool] = str(values.get("why") or "")
        new_text, changes = feeds_adjust.apply(text, targets, self.start_tool, self.max_rpm, self.max_feed)
        if not any(changes.values()):
            return {"error": "I couldn't work out new speeds and feeds for this program's tools."}
        stem = os.path.splitext(os.path.basename(self.path))[0]
        slug = re.sub(r"[^a-z0-9]+", "-", self.material.lower()).strip("-")[:30] or "adjusted"
        output_dir = os.path.expanduser(self.output_dir)
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(output_dir, f"{stem}_{slug}.ngc")
        header = (f"(Speeds and feeds adjusted by Milo for {self.material}; "
                  f"original: {os.path.basename(self.path)})\n")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(header + new_text)
        skipped = [t for t, u in uses.items() if u.locked]
        return {"path": out_path, "changes": changes, "why": why, "notes": str(reply.get("notes") or ""),
                "units": units, "skipped": skipped, "original": self.path}


class ModelListWorker(QThread):
    """The models this API key can use, for the Settings picker"""
    finished = pyqtSignal(object)  # {"chat": [...], "transcription": [...]} newest first, or None

    def __init__(self, api_key):
        super().__init__()
        self.api_key = api_key

    def run(self):
        try:
            from ai_config import chat_models, transcription_models
            models = [(m.id, getattr(m, "created", 0) or 0) for m in OpenAIClient(api_key=self.api_key).models.list().data]
            self.finished.emit({"chat": chat_models(models), "transcription": transcription_models(models)})
        except Exception:
            self.finished.emit(None)
