"""
Shared settings for the AI Assistant: which models to use and how much
generated output to keep.
"""

import re

# OpenAI models. Reasoning models (GPT-5 family, o-series) think before answering: *_EFFORT sets
# how hard ("none", "low", "medium", "high"). Older models use a fixed temperature instead.
ROUTER_MODEL = "gpt-5.6-sol"  # Fast first-pass intent routing and MDI generation (speed matters: keep effort low)
ROUTER_EFFORT = "low"
CAM_MODEL = "gpt-5.6-sol"  # CAM IR JSON generation
CAM_EFFORT = "medium"
TRANSCRIPTION_MODEL = "gpt-transcribe"  # Speech to text
TRANSCRIPTION_LANGUAGE = "en"  # Pinning the language stops Whisper inventing other-language text from noise
# Vocabulary hint for Whisper, so shop terms are spelled right
TRANSCRIPTION_PROMPT = ("Hey Milo. Move the X axis ten millimeters. Jog Z up. Home the machine. Spindle on. "
                        "Coolant off. Drill four holes on a bolt circle. Pocket, profile, facing, endmill, "
                        "G-code, run the program. Yes. No. Cancel.")

# Milo's spoken replies (text to speech)
TTS_MODEL = "gpt-4o-mini-tts"
TTS_VOICES = ("marin", "cedar", "coral", "sage", "ash", "ballad", "verse", "alloy", "echo", "shimmer")
TTS_DEFAULT_VOICE = "marin"
TTS_SPEEDS = (("Normal", 1.0), ("Brisk", 1.15), ("Fast", 1.3), ("Fastest", 1.5))  # Settings choices
# How Milo sounds: every style shares the setting and pronunciation; Settings picks the style
TTS_BASE_INSTRUCTIONS = ("You are Milo, the assistant on a CNC mill, talking to a machinist in a shop. Read machining "
                         "terms naturally: G54 as 'G fifty-four', mm as 'millimeters', rpm as 'R P M'.")
TTS_STYLES = (  # (key, name in Settings, how to speak)
    ("calm", "Calm", "Speak clearly and calmly at a brisk, natural pace, like a helpful colleague."),
    ("upbeat", "Upbeat", "Sound upbeat and positive, with lively energy and a smile in your voice, but stay clear."),
    ("terse", "Terse", "Be clipped and matter-of-fact: flat delivery, no warmth or filler, short pauses, straight "
                       "to the point."),
    ("friendly", "Friendly", "Sound warm and friendly, relaxed and approachable, like a coworker you get along with."),
    ("professional", "Professional", "Sound polished and professional, neutral and precise, like a technical "
                                     "support engineer."),
    ("mentor", "Mentor", "Sound like a patient, experienced machinist teaching an apprentice: reassuring, "
                         "unhurried, with gentle emphasis on the important parts."),
    ("mission_control", "Mission control", "Sound like mission control on a radio loop: crisp, steady, "
                                           "checklist-like, stressing numbers and directions."),
    ("deadpan", "Deadpan", "Deliver everything in a dry, deadpan monotone with understated wit, never excited."),
    ("laid_back", "Laid-back", "Sound laid-back and easygoing, unhurried and casual, like nothing could rattle you."),
    ("gruff", "Old-school machinist", "Sound like a gruff old-school machinist: low, a little rough, no-nonsense, "
                                      "but good-natured."),
)
TTS_DEFAULT_STYLE = "calm"


def tts_instructions(style):
    """The voice instructions for a style key (unknown keys get the default style)"""
    styles = {key: how for key, _, how in TTS_STYLES}
    return TTS_BASE_INSTRUCTIONS + " " + styles.get(style, styles[TTS_DEFAULT_STYLE])

# Voice/typed machine actions
DEFAULT_CIRCLE_FEED = 500  # mm/min when the user doesn't give a feed for a circle move
# What "coolant on" means when the user doesn't say mist or flood. Only mist is wired on this
# machine (Mesa7I96S.hal: coolant-mist -> 7i84 output-06; coolant-flood has no output).
DEFAULT_COOLANT = "mist"

# CAM generation
MAX_CAM_RETRIES = 1  # Times a rejected CAM IR is sent back to the model with the errors
CAM_HISTORY_EXCHANGES = 3  # Past request/response pairs kept in the CAM conversation

# Checking a generated program: its cut is simulated (operations that remove nothing, rapids
# through material) and a vision model compares a top view of the result with the request.
# Problems go back to the CAM model this many times before the program is loaded anyway.
MAX_REVIEW_ROUNDS = 1
VISUAL_REVIEW = True
REVIEW_MODEL = "gpt-5.6-sol"  # Needs image input
REVIEW_EFFORT = "low"

# Generated files
AI_OUTPUT_DIR = "~/linuxcnc/nc_files/ai"  # G-code + IR JSON from the assistant
KEEP_GENERATED_PROGRAMS = 50  # Older generated programs are deleted
KEEP_RAW_RESPONSES = 50  # Older raw model responses in json_rep/ are deleted


def model_options(model, effort, temperature):
    """Chat completion arguments for how hard/how randomly `model` answers: reasoning models take
    reasoning_effort and reject a custom temperature; older models are the other way round"""
    if model.startswith(("gpt-5", "o1", "o3", "o4")):
        return {"reasoning_effort": effort}
    return {"temperature": temperature}


# --- the user's choice (Settings → Milo); the *_MODEL / *_EFFORT values above are the defaults ---

QUALITY_LEVELS = (("fast", "Fastest"), ("balanced", "Balanced"), ("best", "Best quality"))
QUALITY_EFFORTS = {  # reasoning effort per job
    "fast": {"router": "low", "cam": "low", "review": "low"},
    "balanced": {"router": ROUTER_EFFORT, "cam": CAM_EFFORT, "review": REVIEW_EFFORT},
    "best": {"router": "medium", "cam": "high", "review": "medium"},
}
DEFAULT_MODELS = {"router": ROUTER_MODEL, "cam": CAM_MODEL, "review": REVIEW_MODEL}
_choice = {"model": "", "quality": "balanced", "transcription": ""}


def choose(model=None, quality=None):
    """Use one model for every job ("" = the defaults above), at a speed vs. quality level"""
    if model is not None:
        _choice["model"] = (model or "").strip()
    if quality is not None:
        _choice["quality"] = quality if quality in QUALITY_EFFORTS else "balanced"


def job_model(job):
    """The model for a job: "router", "cam" or "review" """
    return _choice["model"] or DEFAULT_MODELS[job]


def job_options(job, temperature):
    """reasoning_effort or temperature for the job's model at the chosen level"""
    return model_options(job_model(job), QUALITY_EFFORTS[_choice["quality"]][job], temperature)


def is_reasoning(model):
    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def chat_models(models):
    """The models worth offering for Milo, newest first. models: [(id, created)].
    Leaves out dated snapshots and models for code, audio, images, search, transcription and
    speech, the slow "pro" models, and ones without image input (the visual check needs it)."""
    import re
    keep = []
    for model_id, created in models:
        if not re.match(r"^(gpt-5|gpt-4\.1|gpt-4o|o3$|o4-mini$)", model_id):
            continue
        if re.search(r"\d{4}-\d\d-\d\d|codex|pro|audio|realtime|search|transcribe|tts|image|chat-latest|live",
                     model_id):
            continue
        keep.append((created, model_id))
    return [model_id for _, model_id in sorted(keep, reverse=True)]


def choose_transcription(model):
    """Speech-to-text model ("" = TRANSCRIPTION_MODEL)"""
    _choice["transcription"] = (model or "").strip()


def transcription_model():
    return _choice["transcription"] or TRANSCRIPTION_MODEL


def transcription_models(models):
    """Speech-to-text models worth offering, newest first. models: [(id, created)].
    Leaves out dated snapshots, speaker-labelling (diarize) and live-streaming models."""
    keep = [(created, model_id) for model_id, created in models
            if ("transcribe" in model_id or model_id == "whisper-1")
            and not re.search(r"\d{4}-\d\d-\d\d|diarize|live|realtime", model_id)]
    return [model_id for _, model_id in sorted(keep, reverse=True)]
