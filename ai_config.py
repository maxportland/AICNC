"""
Shared settings for the AI Assistant: which models to use and how much
generated output to keep.
"""

# OpenAI models
ROUTER_MODEL = "gpt-4o-mini"  # Fast first-pass intent routing and MDI generation
CAM_MODEL = "gpt-4o"  # CAM IR JSON generation
TRANSCRIPTION_MODEL = "whisper-1"  # Speech to text
TRANSCRIPTION_LANGUAGE = "en"  # Pinning the language stops Whisper inventing other-language text from noise
# Vocabulary hint for Whisper, so shop terms are spelled right
TRANSCRIPTION_PROMPT = ("Hey Milo. Move the X axis ten millimeters. Jog Z up. Home the machine. Spindle on. "
                        "Coolant off. Drill four holes on a bolt circle. Pocket, profile, facing, endmill, "
                        "G-code, run the program. Yes. No. Cancel.")

# Voice/typed machine actions
DEFAULT_CIRCLE_FEED = 500  # mm/min when the user doesn't give a feed for a circle move
# What "coolant on" means when the user doesn't say mist or flood. Only mist is wired on this
# machine (Mesa7I96S.hal: coolant-mist -> 7i84 output-06; coolant-flood has no output).
DEFAULT_COOLANT = "mist"

# CAM generation
MAX_CAM_RETRIES = 1  # Times a rejected CAM IR is sent back to the model with the errors
CAM_HISTORY_EXCHANGES = 3  # Past request/response pairs kept in the CAM conversation

# Generated files
AI_OUTPUT_DIR = "~/linuxcnc/nc_files/ai"  # G-code + IR JSON from the assistant
KEEP_GENERATED_PROGRAMS = 50  # Older generated programs are deleted
KEEP_RAW_RESPONSES = 50  # Older raw model responses in json_rep/ are deleted
