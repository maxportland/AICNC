"""
Milo, the AI operator assistant, without any UI.

Every request, typed or spoken, goes to the intent router. Machine actions are validated
(machine_safety), put up for confirmation (ActionConfirmation) and re-validated before they
run (action_controller). Program requests go to the CAM model, whose CAM IR is turned into
G-code (cam_ir_processor) and loaded for review, never started.

The engine reports to a listener (see EngineListener) and never touches widgets, so the
screen can present Milo however it likes and the logic can be tested on its own.
"""

import glob
import json
import os
from datetime import datetime
from typing import Optional

try:
    from openai import OpenAI as OpenAIClient
except ImportError:
    OpenAIClient = None

from workers import OpenAIWorker, IntentRouterWorker, CamWorker
from ai_config import MAX_CAM_RETRIES, CAM_HISTORY_EXCHANGES, KEEP_RAW_RESPONSES, DEFAULT_COOLANT
from intent_router import IntentRouter
from machine_safety import describe_machine, spindle_max_rpm, machine_units
from action_controller import MACHINE_INTENTS, propose_action, execute_action
from action_confirmation import ActionConfirmation, classify_reply, is_cancel_reply, strip_wake_phrase
from tool_table import get_tool_table
from fusion_tools import load_links, describe_for_prompt
from cam_ir_processor import CAMIRProcessor
from log_sources import append_ai_log
from voice_recording_manager import VoiceRecordingManager

try:
    from wake_word_detector import WakeWordDetector
    WAKE_WORD_AVAILABLE = True
except ImportError:
    WAKE_WORD_AVAILABLE = False
    WakeWordDetector = None

ESTOP_RESET_ANSWER = ("I can't release the E-stop. For safety that has to be done by someone at the machine. "
                      "Once it's released, I can turn the machine on for you.")

CONFIG_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.expanduser("~/.linuxcnc/gpt_config.json")
SESSION_DIR = os.path.expanduser("~/linuxcnc/gpt_sessions/")

OP_NAMES = {
    "drill": "Drill", "profile_2d": "Profile", "pocket_2d": "Pocket", "face": "Face", "engrave": "Engrave",
    "text": "Text", "bore": "Bore", "tap": "Tap", "thread": "Thread mill",
}


class EngineListener:
    """What Milo tells the screen. Every method is optional."""

    def on_message(self, line: str):
        """A transcript line: "[TAG] text" (USER, MILO, CONFIRM, MDI, ERROR, CAM, ...)"""

    def on_busy(self, busy: bool, text: str):
        """Milo is working (thinking, generating) or done"""

    def on_proposal(self, action: Optional[dict]):
        """An action awaits confirmation, or None once it was confirmed/cancelled/expired"""

    def on_voice_state(self, state: str):
        """'idle', 'listening' or 'transcribing'"""

    def on_audio_level(self, level: float):
        """Microphone level while listening"""

    def on_transcript(self, text: str):
        """What was heard, before it is handled"""

    def on_clear_input(self):
        """The typed request was handled; clear the input"""

    def on_program_ready(self, info: dict):
        """A generated program was loaded: {path, name, ops, tools, stock}"""

    def on_wake(self):
        """The wake word was heard"""

    def on_show_run(self):
        """A program was started"""


def summarize_ir(ir: dict, path: str = "") -> dict:
    """The facts about a CAM IR program worth showing on its card"""
    ops = []
    for op in ir.get("ops", []) or []:
        name = OP_NAMES.get(op.get("op"), str(op.get("op", "?")).replace("_", " ").title())
        ops.append({"name": name, "tool": op.get("tool")})
    tools = []
    for tool in ir.get("tools", []) or []:
        tools.append({"number": tool.get("tool"), "diameter": tool.get("diameter"),
                      "description": tool.get("description", "")})
    stock = ir.get("stock") or {}
    size = ""
    try:
        lo, hi = stock["min"], stock["max"]
        dims = [abs(float(hi[i]) - float(lo[i])) for i in range(3)]
        size = " × ".join(f"{d:g}" for d in dims) + f" {ir.get('units', 'mm')}"
    except (KeyError, TypeError, ValueError, IndexError):
        pass
    return {"path": path, "name": os.path.basename(path) if path else "", "ops": ops, "tools": tools, "stock": size}


class _StatSource:
    """Gives the engine `self.status.stat` from a callable (the old code read qtvcp's Status.stat)"""

    def __init__(self, getter):
        self._getter = getter

    @property
    def stat(self):
        return self._getter()


class MiloEngine:
    listener = EngineListener()  # replaced in __init__; a class default keeps partial objects usable in tests

    def __init__(self, listener=None, stat_getter=None, action_api=None, load_program=None,
                 config_dir=CONFIG_DIR, settings_path=SETTINGS_PATH):
        """
        Args:
            listener: EngineListener receiving UI updates
            stat_getter: returns a linuxcnc.stat-like object (polled by the caller)
            action_api: object with CALL_MDI / SET_MACHINE_HOMING / SET_MACHINE_STATE / RUN
            load_program: callable(path) that opens a program in LinuxCNC
        """
        self.listener = listener or EngineListener()
        self.status = _StatSource(stat_getter or (lambda: None))
        self.action = action_api
        self.load_program = load_program
        self.config_dir = config_dir
        self.settings_path = settings_path
        self.session_dir = SESSION_DIR
        self.json_rep_dir = os.path.join(config_dir, "json_rep")
        os.makedirs(self.session_dir, exist_ok=True)
        os.makedirs(self.json_rep_dir, exist_ok=True)

        self.settings = {"api_key": "", "show_details": False, "recording_timeout": 20,
                         "silence_timeout": 2.0, "wake_word": True}
        self.openai_worker = None
        self.cam_worker = None
        self.cam_attempts = 0
        self.machine_tools = None
        self._logged_once = set()
        self.wake_word_detector = None
        self.wake_word_ready = False

        self.intent_router = None
        self.router_worker = None
        self.router_history = []
        self.routing_request = None
        self.confirmation = None
        self._auto_listens = 0
        self._listening_for_reply = False
        self._awaiting_clarification = False
        self.cam_ir_processor = None
        self.voice_manager = None
        self.last_ir = None
        self.message_history = []
        self._shut_down = False

    # --- lifecycle -------------------------------------------------------------------------

    def start(self, wake_word=True):
        """Create the router, gate, CAM processor and voice manager; start wake word detection"""
        self.load_settings()
        self.intent_router = IntentRouter(api_key_getter=self.api_key, log_callback=self.log)
        self.confirmation = ActionConfirmation(
            execute_callback=self._execute_confirmed_action,
            log_callback=self.log,
            resolved_callback=self._on_confirmation_resolved,
            proposed_callback=lambda action: self.listener.on_proposal(action),
        )
        self.cam_ir_processor = CAMIRProcessor(log_callback=self.log, progress_callback=self._update_progress)
        self.voice_manager = VoiceRecordingManager(listener=self.listener, log_callback=self.log)
        self.voice_manager.set_timeouts(self.settings["recording_timeout"], self.settings["silence_timeout"])
        self.reset_message_history(announce=False)
        if wake_word and self.settings.get("wake_word", True):
            self._init_wake_word_detector()

    def shutdown(self):
        """Stop background threads so the application can exit cleanly (safe to call twice)"""
        if getattr(self, "_shut_down", False):
            return
        self._shut_down = True
        if self.confirmation:
            self.confirmation.cancel("Shutting down")
        if self.wake_word_detector is not None and self.wake_word_detector.isRunning():
            self.wake_word_detector.stop()
        if self.voice_manager:
            self.voice_manager.stop_recording()
        workers = [self.router_worker, self.openai_worker, self.cam_worker,
                   getattr(self.voice_manager, "voice_worker", None)]
        for worker in workers:
            # Network calls can't be interrupted; give them a few seconds to finish
            if worker is not None and worker.isRunning():
                worker.wait(5000)

    # --- settings --------------------------------------------------------------------------

    def api_key(self) -> str:
        return (self.settings.get("api_key") or "").strip()

    @property
    def show_details(self) -> bool:
        return bool(self.settings.get("show_details"))

    def load_settings(self):
        try:
            with open(self.settings_path, "r") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            return
        for key in self.settings:
            if key in stored:
                self.settings[key] = stored[key]

    def save_settings(self, **changes):
        """Update and persist settings (the file holds the API key: readable by this user only)"""
        self.settings.update(changes)
        if self.voice_manager:
            self.voice_manager.set_timeouts(float(self.settings["recording_timeout"]),
                                            float(self.settings["silence_timeout"]))
        directory = os.path.dirname(self.settings_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        try:
            fd = os.open(self.settings_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(self.settings, f)
            os.chmod(self.settings_path, 0o600)
        except OSError as e:
            self.log(f"[ERROR] Could not save settings: {e}")
            return
        self.log("[CONFIG] Settings saved.")
        if "wake_word" in changes:
            if changes["wake_word"]:
                if self.wake_word_detector is None:
                    self._init_wake_word_detector()
                else:
                    self.start_wake_word_detection()
            else:
                self.stop_wake_word_detection()

    # --- logging ---------------------------------------------------------------------------

    def log(self, message):
        """Add a "[TAG] message" line to the conversation and to ai_assistant.log"""
        append_ai_log(message)
        try:
            self.listener.on_message(message)
        except Exception as e:
            print(f"[Milo] {message} (display failed: {e})")

    def _log_once(self, message):
        if message not in self._logged_once:
            self._logged_once.add(message)
            self.log(message)

    # --- requests ----------------------------------------------------------------------------

    def submit(self, text, from_voice=False):
        """A typed request (the send button)"""
        text = (text or "").strip()
        if not text:
            return
        self.handle_request(text, from_voice=from_voice)

    def handle_request(self, text, from_voice):
        """
        Entry point for every request, typed or spoken.

        A pending confirmation consumes yes/no replies; anything else cancels it
        and is routed as a new request.
        """
        text = strip_wake_phrase(text)
        if not text:
            return
        if self.confirmation and self.confirmation.pending:
            reply = classify_reply(text)
            if reply is not None:
                self.log(f"[USER] {text}")
            if reply == "yes":
                self.clear_input()
                self.confirmation.confirm()
                return
            if reply == "no":
                self.clear_input()
                self.confirmation.cancel()
                return
            self.confirmation.cancel("Superseded by a new request")
        elif self._awaiting_clarification and is_cancel_reply(text):
            self.log(f"[USER] {text}")
            self.clear_input()
            self._drop_clarification()
            self.log("[MILO] Okay, never mind.")
            return
        self._awaiting_clarification = False

        if self._busy_working():
            self.log("[MILO] I'm still working on the previous request. Ask again when it's done.")
            return
        if OpenAIClient is None:
            return self.log("[ERROR] OpenAI Python library (>=1.0.0) is not installed.")
        if not self.api_key():
            return self.log("[ERROR] OpenAI API key is missing. Add it in Settings.")

        self.log(f"[USER] {text}")
        self.clear_input()
        self._show_busy("Thinking…")
        self.routing_request = (text, from_voice)
        self.router_worker = IntentRouterWorker(
            self.intent_router, text, self._machine_context(), list(self.router_history)
        )
        self.router_worker.finished.connect(self._on_intent)
        self.router_worker.error.connect(self._on_intent_error)
        self.router_worker.start()

    def _machine_context(self):
        """Machine state for the router, plus the catalog data of the tool in the spindle"""
        stat = self._stat()
        context = describe_machine(stat)
        number = getattr(stat, "tool_in_spindle", 0) if stat is not None else 0
        if number:
            tool = load_links(os.path.join(self.config_dir, "tool_links.json")).get(int(number))
            if tool is not None:
                context += "\n" + describe_for_prompt(int(number), tool, 0, spindle_max_rpm(stat) or 0,
                                                      machine_units(stat)).rstrip()
                context += f"\n  T{number} description: {tool.description}"
        return context

    def _on_intent(self, result):
        """Act on the router's decision (runs in main thread)"""
        text, from_voice = self.routing_request
        self.router_history.extend([
            {"role": "user", "content": text},
            {"role": "assistant", "content": result["raw"]},
        ])
        self.router_history = self.router_history[-8:]
        intent = result["intent"]

        if intent == "program":
            self.log("[MILO] Generating a program...")
            self._start_program_generation(text)
            return

        self._hide_busy()
        self.clear_input()

        if intent in MACHINE_INTENTS:
            action, refusal = propose_action(result, self._stat())
            if refusal:
                self.log(f"[MILO] {refusal}")
                return
            self.confirmation.propose(action)
            if from_voice:
                self._listen_for_reply()
        elif intent == "estop_reset":
            self.log(f"[MILO] {ESTOP_RESET_ANSWER}")
        else:
            self.log(f"[MILO] {result['answer']}")
            self._awaiting_clarification = intent == "unclear"
            if intent == "unclear" and from_voice:
                self._listen_for_reply(clarifying=True)

    def _on_intent_error(self, error_message):
        """A failed routing call; nothing is executed"""
        self.log(f"[ERROR] {error_message}")
        self._hide_busy()

    def _busy_working(self):
        workers = (self.router_worker, self.openai_worker, self.cam_worker)
        return any(w is not None and w.isRunning() for w in workers)

    def propose_run(self):
        """The Run button on a program card: the same gate as "run the loaded program" """
        if self.confirmation is None:
            return
        if self._busy_working():
            self.log("[MILO] I'm still working on a request; try Run again when I'm done.")
            return
        action, refusal = propose_action({"intent": "run"}, self._stat())
        if refusal:
            self.log(f"[MILO] {refusal}")
            return
        self.confirmation.propose(action)

    def confirm(self, action_id=None):
        if self.confirmation:
            self.confirmation.confirm(action_id)

    def cancel(self):
        if self.confirmation:
            self.confirmation.cancel()

    def _on_confirmation_resolved(self):
        self._stop_listening_for_reply()
        self.listener.on_proposal(None)

    def _execute_confirmed_action(self, action):
        """Run a confirmed machine action, re-checking it against the current machine state"""
        if not self.action:
            return self.log("[ERROR] Action API not available. Cannot run machine commands.")
        try:
            message, ran = execute_action(action, self._stat(), self.action)
        except Exception as e:
            return self.log(f"[ERROR] Failed to run {action['summary']}: {e}")
        self.log(message)
        if ran and action["kind"] == "run":
            self.listener.on_show_run()

    # --- voice -------------------------------------------------------------------------------

    def toggle_voice(self, auto=False):
        """Start listening, or stop and transcribe. auto=True when Milo re-listens for a reply."""
        if not auto:
            self._auto_listens = 0  # the user started this one
            self._listening_for_reply = False
        if not self.voice_manager:
            self.log("[ERROR] Voice input is not available.")
            return
        if self.voice_manager.is_busy and not self.voice_manager.is_recording_voice:
            return  # still transcribing the last request
        if self.voice_manager.is_recording_voice:
            self.voice_manager.stop_recording()
        else:
            self.voice_manager.start_recording(
                api_key=self.api_key(),
                openai_client_available=(OpenAIClient is not None),
                transcript_callback=lambda text: self.handle_request(text, from_voice=True),
            )

    # The previous name, used by the wake word and older callers
    record_voice_prompt = toggle_voice

    def _listen_for_reply(self, clarifying=False):
        """After a voice request, listen for yes/no or a clarification.

        Re-listening for a clarification happens once in a row, so background noise that
        gets transcribed as an unclear request can't keep the microphone open in a loop.
        """
        if clarifying and self._auto_listens >= 1:
            return
        if self.voice_manager and not self.voice_manager.is_recording_voice:
            self._auto_listens += 1
            self.toggle_voice(auto=True)
            self._listening_for_reply = True

    def cancel_listening(self):
        """Stop listening, discard the audio, and drop an open question"""
        if self.voice_manager:
            self.voice_manager.cancel_recording()
        self._listening_for_reply = False
        self._auto_listens = 0
        if self._awaiting_clarification:
            self._drop_clarification()
            self.log("[MILO] Okay, never mind.")

    def _stop_listening_for_reply(self):
        """A confirmation was answered (buttons, voice, timeout): stop an automatic re-listen"""
        if self._listening_for_reply and self.voice_manager and self.voice_manager.is_busy:
            self.voice_manager.cancel_recording()
        self._listening_for_reply = False

    def _drop_clarification(self):
        """Forget Milo's last clarifying question so the next request starts fresh"""
        self._awaiting_clarification = False
        if len(self.router_history) >= 2 and '"unclear"' in self.router_history[-1].get("content", ""):
            self.router_history = self.router_history[:-2]

    def _init_wake_word_detector(self):
        if not WAKE_WORD_AVAILABLE or WakeWordDetector is None:
            self.log("[INFO] Wake word detection not available. Install: pip install vosk sounddevice")
            return
        try:
            self.wake_word_detector = WakeWordDetector(wake_phrase="Hey Milo", config_dir=self.config_dir)
            self.wake_word_detector.wake_word_detected.connect(self._on_wake_word_detected)
            self.wake_word_detector.status_message.connect(self.log)
            self.wake_word_detector.initialized.connect(self._on_wake_word_initialized)
            self.wake_word_detector.start()
        except Exception as e:
            self.log(f"[WARN] Failed to start wake word detection: {e}")
            self.wake_word_detector = None

    def _on_wake_word_initialized(self, success):
        self.wake_word_ready = bool(success)
        if success:
            self.log("[WAKE] Wake word detection ready. Say 'Hey Milo' to talk.")
        else:
            self.log("[WAKE] Wake word detection failed to initialize. See WAKE_WORD_SETUP.md.")

    def _on_wake_word_detected(self):
        if self.voice_manager and self.voice_manager.is_busy:
            return  # already listening (e.g. for a confirmation); toggling would cut it off
        self.listener.on_wake()
        self.toggle_voice()

    def start_wake_word_detection(self):
        if self.wake_word_detector and not self.wake_word_detector.isRunning():
            self.wake_word_detector.start()
            self.log("[WAKE] Wake word detection started.")

    def stop_wake_word_detection(self):
        if self.wake_word_detector and self.wake_word_detector.isRunning():
            self.wake_word_detector.stop()
            self.log("[WAKE] Wake word detection stopped.")

    # --- programs (CAM) ----------------------------------------------------------------------

    def _load_tool_table(self):
        """Read tool.tbl; returns the prompt text and stores the usable tools for the CAM processor.
        Tools linked to a Fusion 360 catalog entry add their geometry and cutting data."""
        units = machine_units(self._stat())
        text, self.machine_tools = get_tool_table(self.config_dir, units, self._log_once)
        self.machine_flute_lengths = {}
        links = load_links(os.path.join(self.config_dir, "tool_links.json"))
        linked = {n: t for n, t in links.items() if self.machine_tools and n in self.machine_tools}
        if linked:
            max_rpm = spindle_max_rpm(self._stat()) or 0
            text += "\nCATALOG DATA FOR LINKED TOOLS (from the vendors' Fusion 360 libraries):\n"
            k = 1.0 if units == "mm" else 1 / 25.4
            for number, tool in sorted(linked.items()):
                table_diameter = self.machine_tools.get(number)
                if table_diameter is not None and abs(table_diameter - tool.diameter * k) > 0.05 * max(1.0, table_diameter):
                    self._log_once(f"[TOOLS] T{number}: tool table diameter {table_diameter:g} doesn't match its "
                                   f"catalog tool ({tool.diameter * k:.4g}); the table is used")
                text += describe_for_prompt(number, tool, 0, max_rpm, units)
                if tool.flute_length > 0:
                    self.machine_flute_lengths[number] = tool.flute_length * k
        return text

    def _cam_system_prompt(self):
        """System prompt for CAM IR generation, built from the current tool table and machine limits"""
        tool_table_context = self._load_tool_table()
        stat = self._stat()
        max_rpm = spindle_max_rpm(stat)
        machine_limits = (f"MACHINE: units {machine_units(stat)}; only {DEFAULT_COOLANT} coolant is connected, "
                          f"so coolant must be \"{DEFAULT_COOLANT}\" or \"none\"")
        if max_rpm is not None:
            machine_limits += f"; spindle maximum {max_rpm:g} rpm. Every rpm value MUST be <= {max_rpm:g}"
        return CAM_PROMPT_HEAD + f"{tool_table_context}\n{machine_limits}\n\n" + CAM_PROMPT_SCHEMA

    def reset_message_history(self, announce=True):
        if self.confirmation is not None and self.confirmation.pending:
            self.confirmation.cancel("New conversation")
        self.router_history = []
        self.message_history = [{"role": "system", "content": self._cam_system_prompt()}]
        if announce:
            self.log("[CONTEXT] Conversation reset.")

    def list_sessions(self):
        if not os.path.isdir(self.session_dir):
            return []
        return sorted((f for f in os.listdir(self.session_dir) if f.endswith(".json")),
                      key=lambda f: os.path.getmtime(os.path.join(self.session_dir, f)), reverse=True)

    def save_session(self, name):
        name = "".join(c for c in name.strip() if c.isalnum() or c in " -_.").strip()
        if not name:
            return None
        if not name.endswith(".json"):
            name += ".json"
        path = os.path.join(self.session_dir, name)
        try:
            with open(path, "w") as f:
                json.dump(self.message_history, f, indent=2)
        except OSError as e:
            self.log(f"[ERROR] Failed to save session: {e}")
            return None
        self.log(f"[SESSION] Saved to {path}")
        return path

    def load_session(self, filename):
        path = os.path.join(self.session_dir, os.path.basename(filename))
        try:
            with open(path, "r") as f:
                self.message_history = json.load(f)
        except (OSError, ValueError) as e:
            self.log(f"[ERROR] Failed to load session: {e}")
            return
        requests = sum(1 for m in self.message_history if m.get("role") == "user")
        self.log(f"[SESSION] Loaded {os.path.basename(path)} ({requests} request(s) of program context).")

    def _start_program_generation(self, prompt):
        """Send the request to the CAM IR model to generate a G-code program"""
        self.message_history[0] = {"role": "system", "content": self._cam_system_prompt()}
        self.message_history.append({"role": "user", "content": prompt})
        self._trim_history()
        self.cam_attempts = 0
        self.send_openai()

    def _trim_history(self):
        """Keep the system prompt and the last few exchanges; older programs just cost tokens"""
        system, rest = self.message_history[0], self.message_history[1:]
        rest = rest[-(2 * CAM_HISTORY_EXCHANGES + 1):]
        while rest and rest[0]["role"] != "user":
            rest = rest[1:]
        self.message_history = [system] + rest

    def send_openai(self):
        """Start the CAM IR request in a worker thread"""
        if self.openai_worker is not None and self.openai_worker.isRunning():
            self.log("[INFO] Still waiting for the previous program request.")
            return
        api_key = self.api_key()
        if OpenAIClient is None or not api_key:
            self.log("[ERROR] OpenAI library or API key missing.")
            self._drop_unanswered_request()
            self._hide_busy()
            return
        self._show_busy("Designing toolpaths…")
        self.openai_worker = OpenAIWorker(api_key, list(self.message_history))
        self.openai_worker.finished.connect(self._on_openai_response)
        self.openai_worker.error.connect(self._on_openai_error)
        self.openai_worker.start()

    def _on_openai_response(self, result):
        """Handle the CAM IR reply (runs in main thread)"""
        self.message_history.append({"role": "assistant", "content": result})
        self._save_raw_response(result)

        ir_data = self.cam_ir_processor.extract_json_from_response(result)
        if not ir_data:
            return self._on_cam_rejected("The response was not valid CAM IR JSON.")
        if "error" in ir_data and not ir_data.get("ops"):
            self.log(f"[MILO] {ir_data['error']}")
            return self._hide_busy()

        self.last_ir = ir_data
        self.log(f"[CAM] Received a program with {len(ir_data.get('ops', []))} operation(s). Generating G-code...")
        self._show_busy("Generating G-code…")
        self.cam_worker = CamWorker(
            ir_data,
            tools=self.machine_tools,
            machine_units=machine_units(self._stat()),
            max_rpm=spindle_max_rpm(self._stat()),
            flute_lengths=getattr(self, "machine_flute_lengths", None),
        )
        self.cam_worker.log.connect(self.log)
        self.cam_worker.progress.connect(self._update_progress)
        self.cam_worker.finished.connect(self._on_cam_finished)
        self.cam_worker.start()

    def _on_cam_finished(self, output_path, error):
        if error:
            return self._on_cam_rejected(error)
        self.load_gcode(output_path)
        self.log("[REVIEW] G-code has been loaded. Please review the toolpath in the main preview before running.")
        self.listener.on_program_ready(summarize_ir(self.last_ir or {}, output_path))
        self._hide_busy()

    def _on_cam_rejected(self, error):
        """Report a rejected program, and send the errors back to the model for a corrected version"""
        error = error.split("\nTraceback", 1)[0].strip()
        self.log(f"[ERROR] {error}")
        if self.cam_attempts < MAX_CAM_RETRIES:
            self.cam_attempts += 1
            self.log(f"[CAM] Asking the AI to fix it (retry {self.cam_attempts} of {MAX_CAM_RETRIES})...")
            self.message_history.append({
                "role": "user",
                "content": f"The CAM IR you produced was rejected:\n{error}\n"
                           "Fix these problems and output the complete corrected CAM IR JSON.",
            })
            self.send_openai()
            return
        self._hide_busy()

    def _on_openai_error(self, error_message):
        self.log(f"[ERROR] {error_message}")
        self._drop_unanswered_request()
        self._hide_busy()

    def _drop_unanswered_request(self):
        """A request the CAM model never answered shouldn't stay in its history (two user turns in a row)"""
        if len(self.message_history) > 1 and self.message_history[-1].get("role") == "user":
            self.message_history.pop()

    def _save_raw_response(self, result):
        """Keep the model's raw reply in json_rep/ for debugging, pruning old ones"""
        try:
            os.makedirs(self.json_rep_dir, exist_ok=True)
            unique_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
            with open(os.path.join(self.json_rep_dir, f"{unique_id}.json"), "w", encoding="utf-8") as f:
                f.write(result)
            saved = sorted(glob.glob(os.path.join(self.json_rep_dir, "*.json")))
            for old in saved[:-KEEP_RAW_RESPONSES]:
                os.remove(old)
        except Exception as e:
            self.log(f"[WARN] Failed to save raw response: {e}")

    def load_gcode(self, filepath):
        """Open a generated program in LinuxCNC"""
        if self.load_program is None:
            self.log(f"[INFO] G-code ready: {filepath}")
            return
        try:
            self.load_program(filepath)
            self.log(f"[INFO] G-code loaded: {filepath}")
        except Exception as e:
            self.log(f"[INFO] G-code ready: {filepath} (auto-open failed: {e})")

    # --- helpers -------------------------------------------------------------------------------

    def _stat(self):
        """The linuxcnc.stat object (or a stand-in), or None"""
        return getattr(self.status, "stat", None) if getattr(self, "status", None) else None

    def clear_input(self):
        self.listener.on_clear_input()

    def _show_busy(self, text):
        self.listener.on_busy(True, text)

    def _hide_busy(self):
        self.listener.on_busy(False, "")

    def _update_progress(self, percent: int):
        self.listener.on_busy(True, f"Generating G-code… {percent}%")


CAM_PROMPT_HEAD = (
    "You are a CNC CAM assistant that generates CAM IR (Intermediate Representation) JSON files.\n"
    "The JSON you generate will be processed by the cam_ir library to produce LinuxCNC-compatible G-code.\n"
    "\n"
    "STRICT REQUIREMENTS:\n"
    "- You MUST output ONLY valid JSON matching the CAM IR schema (version 1.0).\n"
    "- Do NOT wrap the JSON in markdown code blocks (no ```json or ```).\n"
    "- Output the raw JSON object directly.\n"
    "- Use millimeters (mm) as the default unit system.\n"
    "- Provide boundaries as arrays of [x, y] coordinate pairs.\n"
    "- For ANY circular profile or pocket, use circle: { center: [x, y], diameter: number } INSTEAD of boundary.\n"
    "  Never approximate a circle with boundary points; the library generates an exact circle.\n"
    "- For holes evenly spaced on a circle (bolt circles), use bolt_circle: { center: [x, y], diameter: number, count: number,\n"
    "  start_angle_deg?: number } INSTEAD of points in drill/bore/tap/thread.\n"
    "- Every number must be a literal number. Never write arithmetic such as 50 + 30.\n"
    "- stock is the real material block. All geometry you machine (boundaries, circles, islands, drill/bore/tap/thread\n"
    "  points, engrave paths) MUST lie within stock min/max X and Y, or the program is rejected. Size and place\n"
    "  the stock to contain the part (e.g. a 76.2 mm circle centered at [50, 50] needs X and Y from at most 11.9 to at least 88.1).\n"
    "- If you cannot represent something in the schema, include an \"error\" field explaining the limitation.\n"
    "\n"
)

CAM_PROMPT_SCHEMA = (
    "CAM IR Schema Structure (ALL FIELDS BELOW ARE REQUIRED):\n"
    "- version: \"1.0\" (required)\n"
    "- units: \"mm\" or \"inch\" (required)\n"
    "- stock: { min: [x, y, z], max: [x, y, z] } (REQUIRED - defines stock bounding box)\n"
    "- clearance_z: number (REQUIRED - clearance plane height in Z)\n"
    "- safe_z: number (REQUIRED - safe retract height in Z, must be <= clearance_z)\n"
    "- tools: array of tool objects (REQUIRED - must have at least 1 tool)\n"
    "  Each tool: { tool: number, type: \"endmill\"|\"drill\"|\"ball_endmill\", diameter: number, flutes: number, description: string }\n"
    "  NOTE: The tool number MUST match one of the available tools listed above.\n"
    "- ops: array of operation objects (REQUIRED - must have at least 1 operation)\n"
    "- post: { dialect: \"linuxcnc\", program_number: number, spindle: \"CW\" or \"CCW\" } (required)\n"
    "- wcs or wcs_list: optional - work coordinate system(s)\n"
    "  Each WCS: { name?: string (e.g., \"G54\", \"G55\"), origin: [x, y, z], rotation_deg?: number (rotation around Z-axis in degrees, default 0.0) }\n"
    "\n"
    "CRITICAL: You MUST include ALL required fields (stock, clearance_z, safe_z, tools, ops) in EVERY response.\n"
    "If any required field is missing, the JSON will be rejected.\n"
    "\n"
    "Supported Operations (all operations support optional wcs?: number field to specify WCS index):\n"
    "- drill: { op: \"drill\", tool: number, points: [[x, y], ...] OR bolt_circle: { center: [x, y], diameter: number, count: number, start_angle_deg?: number }, top_z: number, bottom_z: number, peck?: number, feed: number, rpm: number, wcs?: number, coolant?: \"none\"|\"mist\"|\"flood\", use_calculated_feeds?: boolean }\n"
    "- profile_2d: { op: \"profile_2d\", tool: number, boundary: [[x, y], ...] OR circle: { center: [x, y], diameter: number }, top_z: number, bottom_z: number, stepdown: number, feed_xy: number, feed_z: number, rpm: number, side: \"inside\"|\"outside\"|\"on\", leads?: {...}, tabs?: [...], wcs?: number, compensation?: \"none\"|\"left\"|\"right\", coolant?: \"none\"|\"mist\"|\"flood\" }\n"
    "- pocket_2d: { op: \"pocket_2d\", tool: number, boundary: [[x, y], ...] OR circle: { center: [x, y], diameter: number }, top_z: number, bottom_z: number, stepdown: number, stepover: number, strategy: \"offset\"|\"adaptive\", feed_xy: number, feed_z: number, rpm: number, islands?: [[[x, y], ...], ...], entry?: {...}, wcs?: number, compensation?: \"none\"|\"left\"|\"right\", coolant?: \"none\"|\"mist\"|\"flood\" }\n"
    "- face: { op: \"face\", tool: number, area: [[x, y], ...] or { min: [x, y], max: [x, y] }, top_z: number, depth?: number, bottom_z?: number, stepdown?: number (default: full depth in one pass), stepover: number, feed_xy: number, feed_z: number, rpm: number, wcs?: number, coolant?: \"none\"|\"mist\"|\"flood\", use_calculated_feeds?: boolean }\n"
    "- engrave: { op: \"engrave\", tool: number, path: [[x, y], ...], top_z: number, depth: number, feed_xy: number, feed_z: number, rpm: number, wcs?: number, coolant?: \"none\"|\"mist\"|\"flood\", use_calculated_feeds?: boolean }\n"
    "- text: { op: \"text\", tool: number, text: string, position: [x, y], top_z: number, depth: number, height: number, feed_xy: number, feed_z: number, rpm: number, wcs?: number, coolant?: \"none\"|\"mist\"|\"flood\", use_calculated_feeds?: boolean }\n"
    "- bore: { op: \"bore\", tool: number, points: [[x, y], ...] OR bolt_circle: { center: [x, y], diameter: number, count: number, start_angle_deg?: number }, top_z: number, bottom_z: number, feed: number, rpm: number, strategy?: \"straight\"|\"spiral\"|\"radial_stepover\" (default \"straight\"), retract_z?: number, dwell?: number, passes?: number (default 1), stepover?: number (radial stepover per pass for radial_stepover strategy, or spiral radius increment), spiral_pitch?: number (Z distance per rotation for spiral strategy, default: stepdown/2), wcs?: number, coolant?: \"none\"|\"mist\"|\"flood\", use_calculated_feeds?: boolean }\n"
    "- tap: { op: \"tap\", tool: number, points: [[x, y], ...] OR bolt_circle: { center: [x, y], diameter: number, count: number, start_angle_deg?: number }, top_z: number, bottom_z: number, pitch: number, rpm: number, direction?: \"CW\"|\"CCW\", wcs?: number }\n"
    "- thread: { op: \"thread\", tool: number, points: [[x, y], ...] OR bolt_circle: { center: [x, y], diameter: number, count: number, start_angle_deg?: number }, top_z: number, bottom_z: number, pitch: number, rpm: number, thread_type?: \"internal\"|\"external\", passes?: number, wcs?: number }\n"
    "\n"
    "Example minimal valid JSON structure:\n"
    "{\n"
    "  \"version\": \"1.0\",\n"
    "  \"units\": \"mm\",\n"
    "  \"stock\": { \"min\": [0, 0, 0], \"max\": [100, 100, 10] },\n"
    "  \"clearance_z\": 25.0,\n"
    "  \"safe_z\": 20.0,\n"
    "  \"tools\": [\n"
    "    { \"tool\": 1, \"type\": \"endmill\", \"diameter\": 6.0, \"flutes\": 4, \"description\": \"6mm endmill\" }\n"
    "  ],\n"
    "  \"ops\": [\n"
    "    { \"op\": \"drill\", \"tool\": 1, \"points\": [[50, 50]], \"top_z\": 0, \"bottom_z\": -5, \"feed\": 100, \"rpm\": 3000 }\n"
    "  ],\n"
    "  \"post\": { \"dialect\": \"linuxcnc\", \"program_number\": 1, \"spindle\": \"CW\" }\n"
    "}\n"
    "\n"
    "Maintain context between prompts and update the JSON incrementally when asked."
)
