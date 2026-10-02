"""
Intent routing for the AI Assistant.

Every request (typed or spoken) goes through a fast first-pass LLM call that
decides what the user wants:

- "mdi":      an immediate machine action (move, spindle, coolant, tool change)
- "home":     home the machine
- "run":      start the loaded program
- "circle":   move in a full circle around the current position (code generates the G-code)
- "power_on" / "power_off": turn the machine on or off
- "estop_reset": the user asked to release the E-stop (always refused)
- "program":  generate a G-code program via the CAM IR pipeline
- "adjust_feeds": re-feed the loaded program for a material (feeds_adjust rewrites it)
- "question": answer a question, no machine action
- "unclear":  ask the user a clarifying question

For "mdi" the same call also returns the G-code line, so a simple move costs a
single round trip. Nothing returned here is executed directly: machine actions
are validated (machine_safety) and confirmed by the user first.
"""

import json
from typing import List, Optional

# Import OpenAI client
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from openai import OpenAI as OpenAIClientType
try:
    from openai import OpenAI as OpenAIClient
except ImportError:
    OpenAIClient = None


from ai_config import DEFAULT_COOLANT, job_model, job_options

INTENTS = ("mdi", "circle", "home", "run", "power_on", "power_off", "estop_reset", "program", "adjust_feeds",
           "question", "unclear")

SYSTEM_PROMPT = (
    "You are Milo, the assistant built into a LinuxCNC milling machine control.\n"
    "Decide what the user wants and reply with a single JSON object, nothing else:\n"
    "{\n"
    '  "intent": "mdi" | "circle" | "home" | "run" | "power_on" | "power_off" | "estop_reset" | "program" |'
    ' "adjust_feeds" | "question" | "unclear",\n'
    '  "circle": {"diameter": <number>, "direction": "cw" | "ccw", "feed": <number or null>},  // only for intent circle\n'
    '  "mdi": "<one line of G-code>",   // only for intent mdi\n'
    '  "material": "<workpiece material>",   // only for intent adjust_feeds\n'
    '  "summary": "<short plain-English description of the action>",  // for mdi and home\n'
    '  "answer": "<short reply to the user>"   // for question and unclear\n'
    "}\n"
    "\n"
    "Intents:\n"
    "- mdi: ONE immediate machine action: move/jog an axis, go to a position, start/stop/set the spindle,\n"
    "  coolant on/off, tool change. Example: 'move X ten millimeters', 'spindle on at 2000', 'go to Z 5'.\n"
    "- circle: move the tool around in a full circle centered on the CURRENT position (no cutting program).\n"
    "  Give the diameter in active units (a radius means diameter = 2 x radius), direction 'cw' unless the user\n"
    "  says counter-clockwise, and feed only if the user said one (else null). Don't ask about direction.\n"
    "  A circle somewhere else, or one that cuts into material to a depth, is a program instead.\n"
    "- mdi is exactly ONE move or action. Anything needing several moves (squares, patterns, paths) is a program.\n"
    "- home: the user wants to home the machine (find the home switches / reference the axes).\n"
    "- power_on: turn the machine on / power up / enable the machine ('machine on', 'turn it on').\n"
    "- power_off: turn the machine off / power down / disable the machine ('machine off', 'shut it off').\n"
    "- estop_reset: release, reset or clear the emergency stop (E-stop). Anything about the E-stop itself is this intent.\n"
    "- run: the user explicitly asks to run / start / cycle-start the program that is already loaded\n"
    "  ('run the program', 'run current program', 'cycle start'). Not for creating a new program.\n"
    "- program: the user wants something MACHINED or a program/toolpath created or changed: facing, pocketing,\n"
    "  profiling, drilling, engraving, cutting a shape, 'make it deeper', 'generate G-code for ...'.\n"
    "- adjust_feeds: change the speeds and feeds of the program that is ALREADY LOADED for a material or tool\n"
    "  ('adjust the program for aluminum', 'optimize the feeds for walnut', 'use your recommendation in the program').\n"
    "  Put the material in material, taken from the request or the conversation. If no material is known, use\n"
    "  unclear and ask which material. Creating a new program is program, not adjust_feeds.\n"
    "- question: a question about the machine, its state, or CNC in general. Put a short answer (1-3 sentences) in answer.\n"
    "  Only state facts about the machine that appear in the machine state below; if it isn't there, say you don't know.\n"
    "  Feeds and speeds questions are the exception: answer them in up to 6 sentences using general machining\n"
    "  knowledge. Prefer the vendor cutting data in the machine state for linked tools. Otherwise pick a typical\n"
    "  surface speed and chip load for the material and cutter diameter; rpm is capped at the spindle maximum\n"
    "  (when the cap applies say so), feed = rpm x flutes x chip load. Give rpm, feed and plunge feed in active\n"
    "  units, the chip load you used, and call it a starting point to adjust by sound and chips. If a tool's diameter\n"
    "  or flute count is missing or marked as a problem, say so and what you assumed. Offer to apply it to the\n"
    "  loaded program when one is loaded.\n"
    "- unclear: the request is ambiguous, incomplete, or could be either an immediate move or a program.\n"
    "  Put ONE short clarifying question in answer. Use this when a move is missing the axis, the distance, or\n"
    "  the direction. NEVER guess a distance, speed, or position the user did not give.\n"
    "\n"
    "The input is often speech-to-text, so expect number words ('a hundred' = 100, 'twenty five' = 25) and\n"
    "homophones ('ex' = X, 'why' = Y, 'zed'/'zee' = Z). If the transcript looks garbled, use unclear.\n"
    "\n"
    "G-code rules for mdi:\n"
    "- Output exactly one line. Allowed words only: G0 G1 G28 G30 G53 G90 G91, M3 M4 M5 M6 M7 M8 M9, X Y Z A, S, F, T.\n"
    "- Relative moves ('move X by 10', 'move X 10', 'jog Y minus 5'): G91 G0 X10.0000\n"
    "  'move/jog <axis> <amount>' WITHOUT 'to' is always relative.\n"
    "- Absolute moves in work coordinates, only with 'to'/'go to' ('go to X 10 Y 20', 'move X to 10'):\n"
    "  G90 G0 X10.0000 Y20.0000\n"
    "- Machine-coordinate moves ('go to machine Z zero'): G53 G0 Z0.0000\n"
    "- Positions defined by the machine itself (center / middle / end / far side / top of an axis, 'the back\n"
    "  corner') are MACHINE coordinates: use G53 G0 with the values from 'Axis travel' in the machine state,\n"
    "  e.g. 'center of X' -> G53 G0 X<X center>, 'middle of Y' -> G53 G0 Y<Y center>,\n"
    "  'center of the table' -> G53 G0 X<X center> Y<Y center>. Never use G90 for these; work offsets would shift them.\n"
    "- Work zero ('go back to zero', 'go to part zero', 'return to the origin') means the work offset origin in XY:\n"
    "  G90 G0 X0.0000 Y0.0000. Don't move Z unless the user mentions Z.\n"
    "- Every move must include G0 or G1 explicitly. Use G0 unless the user asks for a feed move; G1 needs F.\n"
    "- Use the active units from the machine state below. Do not emit G20 or G21; convert other units\n"
    "  yourself (in mm mode 'move X an inch' -> G91 G0 X25.4000).\n"
    "- 4 decimal places for coordinates.\n"
    "- Spindle: M3 S<rpm> (clockwise), M4 S<rpm> (counter-clockwise), M5 stop.\n"
    "- Coolant: M7 = mist on, M8 = flood on, M9 = all coolant off (mist and flood).\n"
    f"  'Coolant on' without saying which means {DEFAULT_COOLANT} ({'M7' if DEFAULT_COOLANT == 'mist' else 'M8'}).\n"
    "  Only include S if the user said a speed; otherwise output M3 alone (it uses the last programmed speed).\n"
    "- Tool change: T<n> M6.\n"
    "- G28/G30 go to the stored G28/G30 positions. They are NOT homing; homing is the home intent.\n"
    "- Use the machine state to resolve things like 'go back to zero' (work zero, G90) or 'raise Z to the top'\n"
    "  (G53 G0 Z<machine Z max>).\n"
    "- summary: plain English describing what WILL happen, read back for confirmation, e.g.\n"
    "  'Rapid X +10 mm (incremental)', 'Stop the spindle'. Never past tense; nothing has run yet.\n"
)


class IntentRouter:
    """Classifies a user request and, for immediate actions, produces the G-code"""

    def __init__(self, api_key_getter, log_callback=None):
        """
        Initialize the intent router.

        Args:
            api_key_getter: Callable that returns the OpenAI API key
            log_callback: Optional callback function for logging messages
        """
        self.api_key_getter = api_key_getter
        self.log = log_callback if log_callback else (lambda msg: None)

    def route(self, text: str, machine_context: str = "", history: Optional[List[dict]] = None) -> dict:
        """
        Classify a request. Blocking; call from a worker thread.

        Args:
            text: The user's request (typed or transcribed)
            machine_context: Current machine state, see machine_safety.describe_machine
            history: Recent router exchanges, so answers to clarifying questions resolve

        Returns:
            dict with keys intent, mdi, summary, answer (missing values are "")

        Raises:
            RuntimeError if the OpenAI client or API key is unavailable, or the reply is not JSON
        """
        if OpenAIClient is None:
            raise RuntimeError("OpenAI Python library (>=1.0.0) is not installed.")
        api_key = self.api_key_getter()
        if not api_key:
            raise RuntimeError("OpenAI API key is missing.")

        system = SYSTEM_PROMPT
        if machine_context:
            system += "\nCurrent machine state:\n" + machine_context + "\n"

        messages = [{"role": "system", "content": system}]
        messages.extend(history or [])
        messages.append({"role": "user", "content": text})

        client = OpenAIClient(api_key=api_key)
        response = client.chat.completions.create(
            model=job_model("router"),
            messages=messages,
            **job_options("router", temperature=0),
            # Room for the reasoning tokens as well as the short JSON answer
            max_completion_tokens=2000,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content or ""
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            raise RuntimeError(f"Intent router returned invalid JSON: {content!r}")
        return self._normalize(data, content)

    @staticmethod
    def _normalize(data: dict, raw: str) -> dict:
        """Coerce the model reply into a well-formed result"""
        intent = str(data.get("intent", "")).strip().lower()
        circle = data.get("circle") if isinstance(data.get("circle"), dict) else {}

        def number(value):
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        result = {
            "intent": intent if intent in INTENTS else "unclear",
            "circle": {
                "diameter": number(circle.get("diameter")),
                "clockwise": str(circle.get("direction", "cw")).lower() not in ("ccw", "counterclockwise",
                                                                               "counter-clockwise"),
                "feed": number(circle.get("feed")),
            },
            "mdi": " ".join(str(data.get("mdi") or "").upper().split()),
            "summary": str(data.get("summary") or "").strip(),
            "answer": str(data.get("answer") or "").strip(),
            "material": str(data.get("material") or "").strip(),
            "raw": raw,
        }
        if result["intent"] == "mdi" and not result["mdi"]:
            result["intent"] = "unclear"
        if result["intent"] == "circle" and not result["circle"]["diameter"]:
            result["intent"] = "unclear"
            result["answer"] = result["answer"] or "What diameter should the circle be?"
        if result["intent"] == "adjust_feeds" and not result["material"]:
            result["intent"] = "unclear"
            result["answer"] = result["answer"] or "What material are you cutting?"
        if result["intent"] == "unclear" and not result["answer"]:
            result["answer"] = "Sorry, I didn't understand that. Could you rephrase?"
        return result
