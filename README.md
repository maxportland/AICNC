# Mesa7I96S LinuxCNC Configuration

A comprehensive LinuxCNC configuration for a CNC milling machine based on the Mesa Electronics 7I96S motion controller, featuring **Milo**, an AI-first, touch-first operator screen built from the ground up, with an AI assistant at its center and deterministic CAM.

## Table of Contents

- [Overview](#overview)
- [Machine Specifications](#machine-specifications)
- [Hardware Configuration](#hardware-configuration)
- [Features](#features)
- [The Milo Screen](#the-milo-screen)
- [CNC AI Assistant](#cnc-ai-assistant)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage](#usage)
- [Troubleshooting](#troubleshooting)
- [Reference Documents](#reference-documents)

## Overview

This configuration provides a production-ready LinuxCNC setup optimized for a 3-axis CNC milling machine with a stepper-driven power drawbar. The system includes:

- **Mesa Electronics 7I96S** motion controller with Ethernet connectivity
- **Milo screen**: a custom qtvcp screen designed for a 1920×1080 touchscreen, with the AI assistant at its center
- **AI-powered CNC Assistant** for natural language G-code generation
- **CAM IR** integration for deterministic toolpath generation
- **Voice control** with wake word detection ("Hey Milo")
- **Custom panels** for enhanced workflow management

## Machine Specifications

### Work Envelope
- **X-Axis**: 0 to 500mm (19.7")
- **Y-Axis**: 0 to 175mm (6.9")
- **Z-Axis**: -253mm to 0mm (-10" to 0")

### Motion Parameters
- **Max Linear Velocity**: 83.33 mm/s (X/Y), 15.83 mm/s (Z)
- **Max Acceleration**: 200 mm/s² (all axes)
- **Spindle Speed Range**: 100-3000 RPM
- **Default Spindle Speed**: 1000 RPM
- **Spindle Increment**: 200 RPM

### Control System
- **Motion Controller**: Mesa Electronics 7I96S (Ethernet)
- **I/O Expansion**: Mesa 7I84 (via smart serial)
- **Servo Period**: 1ms (1000Hz)
- **Task Cycle Time**: 10ms
- **Control Type**: Closed-loop stepper control with PID

## Hardware Configuration

### Motion Controller Setup
- **Board IP**: 10.10.10.10 (`[HOSTMOT2] BOARD_IP` in the INI)
- **Firmware config**: `[HOSTMOT2] CONFIG` in the INI (the HAL file reads both)
- **Step Generators**: 4 (X, Y, Z, drawbar)
- **PWM Generators**: 1 (loaded but unused; the spindle runs from the 7I83 analog output)
- **Encoders**: 1 (spindle feedback)
- **Smart Serial Ports**: 1 (7I84 I/O expansion)

### Axis Configuration
- **X-Axis (Joint 0)**: Closed-loop stepper, 644 steps/mm
- **Y-Axis (Joint 1)**: Closed-loop stepper, -646 steps/mm (reversed)
- **Z-Axis (Joint 2)**: Closed-loop stepper, 644 steps/mm
- **Drawbar (Joint 3, the A axis)**: Closed-loop stepper (CL86Y driver) in degrees (A360 = one
  turn; `STEP_SCALE` 1600/360 for the driver at 1600 pulses/turn). A coordinate (`trivkins coordinates=XYZA`) so G-code can turn it, but
  not a machine axis to the operator: the screen hides it and Milo won't move it.

### Power drawbar
Four pneumatic cylinders (valve on 7I84 output 5, driven by `M64 P0` / `M65 P0` through
`motion.digital-out-00`) lower the A motor onto the drawbar's square, and the motor undoes or does up the
drawbar. Subroutines in `subroutines/` run it, reading their settings from **`[DRAWBAR]` in
`Mesa7I96S.ini`** (polarity of the valve, which way tightens, release/clamp turns, speeds, engage turn,
cylinder and spindle-stop times):
- `o<drawbar_release> call` / `o<drawbar_clamp> call`: stop the spindle if needed, lower, ease the square on,
  turn, raise. Clamping turns a little past tight: the closed-loop driver (CL86Y) pushes at its peak current,
  which sets the torque. If the motor falls further behind than the driver's position error limit (a jam,
  or `CLAMP_TURNS` too far past tight) the driver alarms: its ALM (7I84 input 5) is joint 3's amp fault, so
  the machine turns off rather than carrying on with an unclamped tool. Its ENA is 7I84 output 7; turning
  the machine off and on resets the alarm.
- `o<drawbar_lower> call`, `o<drawbar_raise> call`, `o<drawbar_turn> call [turns] [turns per second]`
  (positive tightens): single steps, for setting it up.
- **M6** is remapped (`milo_m6.ngc`): Z up, release, the usual manual tool change prompt, clamp.
- The **Tools** page has Release / Clamp tool and, under *Test and set up*, Lower / Raise motor and
  Tighten / Loosen 1 turn for commissioning. The spindle (and programs) won't start while the motor is down
  on the drawbar or the tool is released.

Commissioning: set the driver to 1600 pulses/turn to match `STEP_SCALE`; turn the machine on (if it
faults at once, use `input-05-not` for ALM; if the motor is limp, set `output-07-invert` false); in the
CL86Y tuning software set the peak current for the clamp torque and the position error limit above
`CLAMP_TURNS`' overshoot (e.g. 2000 counts for 0.25 turn); with no tool, **Lower motor** and
check it seats (flip `LOWER_IS_ON` if it rises instead); **Tighten 1 turn** and check it tightens (flip
`TIGHTEN_DIRECTION` if not); then set `RELEASE_TURNS` / `CLAMP_TURNS` and the times. Restart LinuxCNC
after editing the INI.

### Spindle Control
- **Type**: ±10 V analog speed command from the 7I83 (`analogout0`, direction via `mux2`), enabled by `spindle.0.on`
- **Feedback**: 7I96S encoder 0

### I/O Configuration
- **Home Switches**: X, Y, Z (via 7I84 inputs)
- **Limit Switches**: None wired. Soft limits only, which LinuxCNC enforces after homing
- **Tool Sensor**: Configured at X=5.446, Y=78.012, Z=-130.00
- **Probe**: Basic probe support enabled

### Pendant Support
- **Model**: XHC-WHB04B-6 Wireless Pendant
- **Features**: Jog wheel, axis selection, feed override, program control
- **Configuration**: Half-step mode for smoother jog wheel operation

## The Milo Screen

The screen (`DISPLAY = qtvcp -f milo`) is built in Python rather than Designer: `qtvcp/screens/milo/`
holds an empty `.ui` and the handler, and the interface itself lives in `milo_ui/`. It is designed for a
touchscreen: every control is at least 52 px (most are 64-72 px), there is a built-in on-screen keyboard and a
number pad, nothing relies on hover, and scrolling is by drag. Fonts (Inter, JetBrains Mono; OFL) are bundled in
`milo_ui/fonts/`.

### Layout

- **Navigation rail** (left): Milo, Jog, Program, Tools, Offsets, Probe, and at the bottom Activity and Settings.
- **Status bar** (top): the machine state in color (E-STOP / OFF / NOT HOMED / READY / RUNNING / PAUSED) with
  a one-line explanation, the *next step* button when there is one ("Turn on", "Home all"), the tool, work
  offset, spindle and feed, then **Power** and **E-STOP** on the right.
- **Dock** (bottom, on every page): Milo's orb (tap to talk) and the "Ask Milo" input, and the cycle
  controls **Start / Pause-Resume / Stop**. These two groups never move.
- **Toolpath stage** (right, on Milo and Program): the 3D preview, view buttons, run progress with time left,
  and program facts (run time, line, tools, size).

### Pages

- **Milo (home)**: the conversation with Milo in the center, the position readout and overrides beside it,
  and suggestion chips that follow the machine state ("Turn the machine on", "Home all axes", "Run part.ngc").
  Milo answers in cards: confirmations with the exact G-code, results, errors, and a **program card** for
  generated programs (operations, stock, tools) with **View toolpath** and **Run…** buttons.
- **Jog**: hold-to-jog pad (stops on release), step sizes from the INI, jog speed, position, spindle
  (direction, speed on a number pad, coolant), home / zero / go-to-zero, the INI macros, and MDI with recent lines.
- **Program**: a touch file browser (Programs, Made by Milo, USB), the G-code with the current line, run from
  a selected line, optional stop, and the Facing / Hole-circle generators.
- **Tools**: the tool in the spindle, change tool (M6), set tool (M61), measure length on the tool setter,
  apply G43, the power drawbar, the tool table, and the Fusion 360 **Tool library** (below).
- **Offsets**: the work systems (G54-G59, G59.1-3 under More) as things you use: each with a name you give it
  ("Left vise"), Active / In use / Not used, and its origin. Pick one from the list or its pin on the **map**
  (the table from above, the camera photo when there's a scan, origins, fixtures and the tool) to see where
  its origin is and what the tool reads in it, then zero it at the tool (any system, active or not), set an
  axis to a value or type its origin, make it active, go over its origin, probe it, copy it to another
  system, save it as a fixture or clear it. **G92** and the **tool length** (which shift every system) are
  flagged when they're likely to surprise, with one-tap fixes. **Fixtures** (`fixtures.json`, now with
  rotation) load into any system, and **Recent changes** lists every offset change with Undo, or Restore for
  an earlier state. Milo knows the systems' names. qtvcp's full offset table is under **Full table**.
- **Probe**: start from what you want to find: a **corner** (outside or inside), an **edge**, the centre of a
  **hole** or **boss**, the **top surface**, a part's **angle**, or **tool length** (tool setter, touch plate).
  Tap the corner or edge on a picture, and the page says where to put the probe and draws exactly what it
  will do (where it goes down, which way it searches and how far, where it should touch). It won't start
  until the probe is in the spindle, has been tapped once to show it works, and the moves fit the soft
  limits. The result is shown in plain words ("Corner found at X 12.345 Y 8.210; setting G54 X0 Y0 here
  moves its origin X +0.42") with **Set** and **Probe again**; every work offset change (probing, zeroing,
  fixtures) can be **undone**. *Probe twice and compare* flags a loose part; a hole or boss far from the size
  you gave, or a result far from where the camera saw the part, is flagged too. With a camera scan the
  preview shows the real part: tap its corner, and **Take me there** moves the probe over the start point.
  Milo can do it by voice ("find the centre of this hole") and the pendant from its right-stick menu
  (Probe…); both go through the usual confirmation. **Probe setup** holds the probe's tool number, tip,
  speeds and distances, and calibrates the tip on a ring gauge. The spindle won't start with the probe in.
  The routines are qtvcp's, run by `milo_probe_subprog.py` with probe-protected (G38.3) descents beside the
  part; qtvcp's own probe screen is still there under **Advanced**.
- **Vision**: the camera on the head (see [VISION_HARDWARE.md](VISION_HARDWARE.md) for the shopping list
  and setup). Scan the table to find parts, see them on a stitched map, and create a probe program
  that measures the chosen part and sets G54. Milo knows what the scan found, and its moves are
  checked against the measured heights.
- **Activity**: every log (machine, screen, Milo, LinuxCNC) live, filterable and searchable.
- **Settings**: OpenAI API key, wake word, listening timeouts, technical details, on-screen keyboard,
  pendant configuration, machine facts, shut down.

Tap an axis in the position readout to zero it, set it to a value (the number pad accepts `45/2`), halve it
to find a center, home it, or move to it.

### Fusion 360 tool libraries

**Tools → Tool library** reads vendor tool libraries exported from Fusion 360 (`.json`, or `.tools`,
which is the same JSON zipped). **Import…** finds them on the Desktop, in Downloads and on USB sticks and
keeps a copy in `~/linuxcnc/tool_libraries/`. Search by diameter, product number or description and filter
by type.

Each tool shows its geometry and the vendor's cutting presets per material. Vendor presets usually assume
router speeds (18,000 rpm and up); **"This machine"** shows them at this spindle's limit with the vendor's
chip load per tooth kept, so the feed is recomputed rather than copied (copying would feed several times
too fast).

**Add to machine…** writes the tool into `tool.tbl` under the number you choose (an existing entry keeps its
measured length) and links it to the catalog tool in `tool_links.json`. Measure its length on the tool setter
before cutting. For linked tools, Milo:

- plans programs with the rescaled cutting data for the material you name,
- knows the flute count, flute length, corner radius and point angle,
- rejects operations that cut deeper than the tool's flute length,
- answers questions about the tool in the spindle with its real description.

`tool.tbl` stays the record of what's in the rack; the libraries are catalogs.

### How Milo is everywhere

Milo's orb in the dock shows its state on every page: breathing when idle, rings that follow your voice while
listening, a sweeping arc while transcribing, a spinning gradient while thinking. When Milo proposes a machine
action, a confirmation card floats above the dock on whatever page you are on, with a 30-second countdown ring
and Confirm / Cancel (or say "yes" / "no"). Replies that arrive while you're on another page appear briefly
above the dock, with a button to open the conversation.

### Previewing the screen without LinuxCNC

```bash
venv/bin/python -m milo_ui.preview                          # window with a simulated machine
venv/bin/python -m milo_ui.preview --shot home.png --page home --scenario program
```

Scenarios: `off`, `estop`, `ready`, `chat`, `confirm`, `program`, `running`, `listening`. Screenshots render
offscreen, so this works over SSH, and nothing talks to LinuxCNC.

## CNC AI Assistant

The CNC AI Assistant is a powerful feature that allows you to generate G-code using natural language descriptions. It uses OpenAI models (set in `ai_config.py`, currently `gpt-5.6-sol`) to understand your machining requirements and generate appropriate toolpaths.

### Key Features

#### Natural Language Processing
Describe your machining operations in plain English:
- "Drill 4 holes at the corners of a 100mm square"
- "Create a pocket 50mm wide, 30mm deep in aluminum"
- "Profile cut around this rectangle with a 0.5mm finish pass"

#### CAM IR Integration
The assistant generates **CAM IR (Intermediate Representation)** JSON, which is then processed by the deterministic CAM engine to produce G-code. This ensures:
- **Deterministic output**: Same input always produces identical G-code
- **Validation**: Schema validation catches errors before G-code generation
- **Consistency**: Reliable, repeatable results

#### Supported Operations
- **Drill**: Standard drilling operations
- **Profile 2D**: Outside/inside profiling with lead-in/lead-out
- **Pocket 2D**: Adaptive clearing with constant engagement angle
- **Face**: Face milling operations
- **Engrave**: Engraving operations
- **Text**: Text engraving with various fonts
- **Bore**: Boring operations (straight, spiral, radial stepover)
- **Tap**: Tapping operations
- **Thread**: Threading operations

#### Intent Routing and Machine Actions
Every request, typed or spoken, first goes to a model with low reasoning effort (`ROUTER_MODEL` in `ai_config.py`) that decides what you want:
- **Immediate action** ("move X ten millimeters", "spindle on at 12000", "go to X 10 Y 20"): the G-code line is
  checked against an allowlist, machine state (E-stop, power, idle, homed) and soft limits, then shown for
  confirmation. Say or type "yes", or press **Confirm**, to run it; "no" or **Cancel** aborts. Unconfirmed
  actions expire after 30 seconds.
- **Home the machine**: confirmed the same way, then homes all axes (the machine must be on).
- **Machine on / off** ("turn the machine on", "machine off"): confirmed, then switched like the Machine
  On/Off button. Turning on is refused while the E-stop is active; turning off warns if a program is running.
- **E-stop**: Milo never releases the E-stop by voice; it has to be done at the machine.
- **Run the loaded program** ("run current program", "cycle start"): requires a loaded file, a homed and idle
  machine; the confirmation names the file, and it is re-checked (same file still loaded) before starting.
- **Program** ("face the stock 100 by 50, 1 mm deep"): generates CAM IR and G-code, which is loaded for review, never started.
  Programs must use tools from `tool.tbl` (its diameters override the AI's) and stay within the spindle
  maximum from `[SPINDLE_0]`; a rejected program is sent back to the AI once with the errors to fix.
  Every program is then checked: its cut is simulated (operations that remove no material, rapid moves
  through material) and a vision model compares a top view of the result with the request. Problems go
  back to the AI for one fix round; the program card shows the simulated result and anything still wrong.
  Output goes to `~/linuxcnc/nc_files/ai/`, keeping the newest 50.
- **Question** ("what tool is loaded?"): answered in the conversation.
- **Unclear** ("move Y"): Milo asks a clarifying question instead of guessing.

#### Voice Control
- **Wake Word Detection**: Say "Hey Milo" (or "Hi Milo") on any page; detection runs offline with Vosk
- **Tap to talk**: tap the orb in the dock (or the big orb on the empty conversation); tap again to stop
- **Live feedback**: the orb and a small level meter follow your voice; "Cancel" discards the recording
- **Auto-stop**: after 2 s of silence, or the recording timeout (default 20 s)
- **Spoken replies** (Settings → Voice, off by default): Milo reads answers and questions aloud with OpenAI text to speech, and waits until it has finished before listening for a reply
- **Transcription**: OpenAI `gpt-transcribe` (`TRANSCRIPTION_MODEL` in `ai_config.py`)

### Usage

1. **Type**: tap "Ask Milo…" in the dock (the keyboard slides up) and press Send.
2. **Talk**: tap the orb, or say "Hey Milo, …" from anywhere. For machine actions Milo listens for your
   "yes" or "no" right away.
3. **Tap a suggestion**: the chips under the conversation change with the machine state.
4. **Programs**: generated programs are loaded for review, never started. Use **Run…** on the program card
   (it asks for confirmation like any other action) or **Start** in the dock.

Conversation options (the ••• button): new conversation, save / load a conversation (the program context Milo
builds on, in `~/linuxcnc/gpt_sessions/`), show technical details.

### Activity (logs)

A live view of the machine's logs:

- **Sources**: Machine (power, E-stop, limits, tool changes), Screen (qtvcp's log), Milo
  (everything Milo logged, including hidden details; `ai_assistant.log`), and LinuxCNC's console output
  and debug output for the current run.
- **Levels**: lines are colored as errors, warnings, info or debug, with counts; tap the chips to
  filter. Tracebacks stay with their error.
- **Search** highlights every match with previous/next; **Matches only** hides the rest.
- **Follow** keeps the newest line in view (scrolling up pauses it), **Wrap** wraps long lines,
  **Copy** copies the selection or everything shown, **Save** writes what's shown to
  `~/linuxcnc/log_exports/`, and **Clear** hides existing lines so only new ones appear.

### CAM IR Schema

The AI generates JSON following the CAM IR schema:

```json
{
  "stock": {
    "x": 200,
    "y": 200,
    "z": 25
  },
  "clearance_z": 10,
  "safe_z": 5,
  "tools": [
    {
      "number": 1,
      "name": "6mm End Mill",
      "diameter": 6,
      "type": "end_mill"
    }
  ],
  "ops": [
    {
      "type": "pocket_2d",
      "geometry": [[0, 0], [100, 0], [100, 100], [0, 100]],
      "tool": 1,
      "depth": -10,
      "stepdown": 2,
      "material": "aluminum"
    }
  ]
}
```

### Configuration

#### OpenAI API Key
1. Open **Settings** (bottom of the rail)
2. Enter your OpenAI API key and tap **Save**
3. The key is stored in `~/.linuxcnc/gpt_config.json`, readable by this user only

#### Listening
**Settings → Listening**: how long Milo listens at most (default 20 s) and how much silence ends a request
(default 2 s). The wake word can be switched off there too.

#### Wake Word Setup
See `WAKE_WORD_SETUP.md` for detailed instructions on setting up "Hey Milo" wake word detection.

## Installation

### Prerequisites

- LinuxCNC 2.9 (Master branch)
- Python 3.8+
- Mesa Electronics 7I96S motion controller
- Mesa 7I84 I/O expansion board (optional)
- Network connection to motion controller

### Python Dependencies

The configuration uses a Python virtual environment located at `venv/`. The packages are
pinned in `requirements.txt`: openai, numpy, qtawesome, pyqtspinner, sounddevice, soundfile,
vosk (wake word; the model itself is covered in WAKE_WORD_SETUP.md), pytest, and the `cam_ir`
submodule in `libs/cam_ir`. PyQt5 and the LinuxCNC modules come from the system.

### Installation Steps

1. **Clone this configuration** (with the CAM IR submodule):
   ```bash
   cd ~/linuxcnc/configs
   git clone --recurse-submodules git@github.com:maxportland/AICNC.git
   ```

2. **Set up Python virtual environment** (if not already done):
   ```bash
   cd AICNC
   python3 -m venv --system-site-packages venv
   ```
   `--system-site-packages` is required: the AI Assistant runs inside qtvcp and needs the
   system `linuxcnc`, `hal`, `qtvcp` and PyQt5 modules.

3. **Fetch CAM IR** (git submodule) **and install the Python dependencies**:
   ```bash
   git submodule update --init
   venv/bin/pip install -r requirements.txt
   ```
   Don't `pip install PyQt5`; a second copy would shadow the system PyQt5 that qtvcp uses.
   To pull a newer CAM IR later, run `./update_cam_ir.sh`.

4. **Configure motion controller IP**:
   - Edit `Mesa7I96S.ini`:
   ```ini
   [HOSTMOT2]
   BOARD_IP="YOUR_IP_ADDRESS"
   ```
   - Default is `10.10.10.10`

5. **Set up OpenAI API key** (for AI Assistant):
   - Launch LinuxCNC
   - Open **Settings**, enter your API key and tap Save

6. **Configure wake word** (optional):
   - See `WAKE_WORD_SETUP.md` for instructions

## Configuration

### Machine-Specific Settings

#### Axis Limits
Edit `Mesa7I96S.ini` to adjust axis limits:
```ini
[AXIS_X]
MIN_LIMIT = -0.0
MAX_LIMIT = 500.0

[AXIS_Y]
MIN_LIMIT = -0.0
MAX_LIMIT = 175.0

[AXIS_Z]
MIN_LIMIT = -253.0
MAX_LIMIT = 0.0
```

#### Spindle Parameters
```ini
[SPINDLE_0]
MAX_SPINDLE_0_SPEED = 3000
MIN_SPINDLE_0_SPEED = 100
DEFAULT_SPINDLE_0_SPEED = 1000
```

#### Tool Sensor Position
```ini
[TOOL_SENSOR]
X = 5.446
Y = 78.012
Z = -130.00
APPROACH_FEED = 600
```
Measuring a tool goes to the top of Z, across to the setter, down to `Z` as a probe move at
`APPROACH_FEED` (a tool too long for that start height stops on the setter instead of hitting it), searches
down and probes slowly, writes the length (a longer tool gets a larger offset) and turns G43 back on. The
speeds, search distance, setter height and work height are screen settings on the Probe page.

### HAL Configuration

#### PID Tuning and Step Generator Settings
`Mesa7I96S.hal` reads these from each joint's section of the INI, so edit them there:
```ini
[JOINT_0]
P = 1000.0
I = 0
D = 0
STEP_SCALE = 644.0
STEPGEN_MAXVEL = 62.50
STEPGEN_MAXACCEL = 250.00
```

### UI Customization

- **Colors, type, radii**: `milo_ui/theme.py` holds every design token and the stylesheet.
- **Components**: `milo_ui/kit.py` (buttons, cards, sliders, toggles, gauges, number pad, keyboard, toasts)
  and `milo_ui/widgets.py` (position readout, jog pad, spindle, overrides, cycle controls, toolpath stage).
- **Pages**: `milo_ui/pages/`, one file per page; `milo_ui/shell.py` is the frame around them.
- **Machine access**: pages only use `milo_ui/machine.py` (`QtvcpMachine` on the machine, `SimMachine` in previews).
- Screen preferences (on-screen keyboard, probe setup, the work offset undo history) are kept in
  `~/.linuxcnc/milo_ui.json`.

## Usage

### Starting LinuxCNC

```bash
cd ~/linuxcnc/configs/AICNC
linuxcnc Mesa7I96S.ini
```

### Basic Operations

#### Homing
1. Release the E-stop and tap **Turn on** in the status bar (or say "Hey Milo, turn the machine on")
2. Tap **Home all** in the status bar (or ask Milo)

#### Loading G-code
1. Open **Program** and tap a file (or **Open** on the toolpath stage)
2. The toolpath appears on the stage

#### Running a Program
1. Check the toolpath and overrides
2. Tap **Start** in the dock (or **Run…** on a program card, or ask Milo to run it)
3. **Pause / Resume** and **Stop** stay in the dock; progress and time left are on the stage

#### Using Milo
1. Tap the orb or say "Hey Milo"
2. Ask for a move, a program, or a question
3. Confirm machine actions on the card that appears (or say "yes")

### MDI Commands

The INI's `[MDI_COMMAND_LIST]` entries appear as **Macros** on the Jog page:
- `Go to G54`: Move to G54 origin
- `Go to X/Y G54`: Move to X/Y G54 origin
- `ABS Home`: Move to machine home (G53)
- `Spindle Test`: Ramp spindle from 250 to 2000 RPM
- `Motion Test`: Test motion across full travel
- `Center Machine`: Move to machine center
- `Spindle 1000`: Set spindle to 1000 RPM
- `Spindle 2500`: Set spindle to 2500 RPM

### Pendant Usage

The XHC-WHB04B-6 wireless pendant provides:
- **Jog Wheel**: Continuous or step jogging
- **Axis Selection**: X, Y, Z buttons
- **Feed Override**: Adjust feed rate on the fly
- **Program Control**: Start, pause, stop
- **Emergency Stop**: Large red button

## Troubleshooting

### Motion Controller Issues

**Problem**: Cannot connect to motion controller
- **Solution**: Check network connection and IP address (default: 10.10.10.10)
- Verify `[HOSTMOT2] BOARD_IP` in `Mesa7I96S.ini` (the HAL file reads it from there)
- Check Ethernet cable connection

**Problem**: Axes not moving
- **Solution**: 
  - Check enable signals in HAL
  - Verify step/direction connections
  - Check PID tuning parameters
  - Check the joint isn't at a soft limit (there are no limit switches)

### AI Assistant Issues

**Problem**: "OpenAI API key required"
- **Solution**: 
  - Open **Settings**, enter your OpenAI API key and tap Save

**Problem**: "CAM IR validation failed"
- **Solution**: 
  - Check the error message for specific field issues
  - Ensure all required fields are present (stock, clearance_z, safe_z, tools, ops)
  - Verify tool numbers match between tools array and operations

**Problem**: Voice recording not working
- **Solution**:
  - Check microphone permissions
  - Verify `sounddevice` and `soundfile` are installed
  - Test microphone with system audio settings
  - Check that the orb's rings and the level meter react while listening

**Problem**: Wake word not detected
- **Solution**: See `WAKE_WORD_SETUP.md` for setup instructions
- Verify Vosk is installed and the model is in `~/vosk-model-small-en-us-0.15`
- Check **Activity → Milo** for `[WAKE]` messages

### UI Issues

**Problem**: The screen doesn't start
- **Solution**:
  - Run `venv/bin/python -m milo_ui.preview` to check the UI without LinuxCNC
  - Check the LinuxCNC console output for errors from `milo_handler.py`
  - The toolpath preview needs OpenGL; the rest of the screen works without it

**Problem**: HAL pins not found (`milo.led-probe`)
- **Solution**: the screen's HAL component is named `milo`; `qtvcp_postgui.hal` and `custom_postgui.hal` use it

### Performance Issues

**Problem**: UI is slow or unresponsive
- **Solution**:
  - Increase `[DISPLAY] CYCLE_TIME` in `Mesa7I96S.ini` (default 100 ms) so the screen polls less often
  - Check system resources (CPU, memory)
  - Reduce log verbosity

**Problem**: Audio stuttering during recording
- **Solution**:
  - Increase audio buffer size
  - Close other applications using audio
  - Check system audio settings

## Reference Documents

The `reference_documents/` directory contains:
- **Mesa 7I96S Manual**: Motion controller documentation
- **Mesa 7I84 Manual**: I/O expansion board documentation
- **Motor Driver Manuals**: CL57T, CL86T servo drive documentation
- **Motor Datasheets**: Stepper motor specifications
- **Wiring Diagrams**: Connection schematics
- **LinuxCNC Documentation**: General LinuxCNC reference

## File Structure

```
AICNC/
├── Mesa7I96S.ini           # Main configuration file (DISPLAY = qtvcp -f milo)
├── Mesa7I96S.hal           # HAL configuration
├── qtvcp/
│   ├── screens/milo/       # Empty .ui + milo_handler.py (builds the screen, wires qtvcp widgets)
│   └── panels/pendant_config/   # Pendant configuration panel (opened from Settings)
├── milo_ui/                # The screen
│   ├── theme.py            # Design tokens and stylesheet
│   ├── kit.py              # Touch components (buttons, sliders, number pad, keyboard, toasts...)
│   ├── machine.py          # MachineModel: QtvcpMachine (real) and SimMachine (previews/tests)
│   ├── widgets.py          # Position readout, jog pad, spindle, overrides, cycle controls, stage
│   ├── shell.py            # Rail, status bar, dock, overlays, Milo bridge
│   ├── probing.py          # Tool setter and touch plate routines
│   ├── assistant/          # Orb, conversation, composer, confirmation sheet
│   ├── pages/              # One file per page (tool_library.py: Fusion 360 libraries)
│   ├── fonts/              # Inter and JetBrains Mono (OFL)
│   └── preview.py          # Run the screen against a simulated machine
├── milo_engine.py          # Milo's logic, no UI: routing, confirmation, CAM, voice, wake word
├── ai_config.py            # Model names, CAM retry/history limits, generated-file retention
├── intent_router.py        # First-pass intent routing (move/home/program/question)
├── machine_safety.py       # Allowlist, machine state and soft-limit checks for AI actions
├── action_confirmation.py  # Confirm/cancel gate for AI machine actions
├── action_controller.py    # Proposes AI machine actions and runs confirmed ones (no Qt)
├── cam_ir_processor.py     # CAM IR -> G-code, checked against tool table and spindle limit
├── tool_table.py           # tool.tbl parsing for the CAM prompt and tool checks
├── voice_recording_manager.py, workers.py, wake_word_detector.py   # Voice input
├── log_viewer.py, log_sources.py   # Activity page
├── custom_action.py, subprograms.py   # Tool setter routine
├── facing_utility.py       # Facing generator with step-down passes
├── fusion_tools.py         # Fusion 360 tool libraries: reading, feed rescaling, tool.tbl links
├── milo_vision/            # Camera: calibration, detection, table scans, heights, probe programs
├── VISION_HARDWARE.md      # Camera/laser/probe shopping list and mounting design
├── requirements.txt        # Pinned Python packages for the venv
├── libs/cam_ir/            # CAM IR library (git submodule)
├── tests/                  # venv/bin/python -m pytest tests
├── reference_documents/    # Hardware documentation
└── venv/                   # Python virtual environment
```

## Contributing

When modifying this configuration:

1. **Backup first**: Always backup your working configuration
2. **Test thoroughly**: Test changes on a safe, non-production setup
3. **Document changes**: Update this README with new features
4. **Version control**: Use git or similar to track changes
5. **HAL changes**: Document any HAL modifications in comments

## License

This configuration is provided as-is for use with LinuxCNC. Refer to individual component licenses:
- LinuxCNC: GPL v2
- qtvcp: GPL v2
- Inter, JetBrains Mono fonts: SIL Open Font License (`milo_ui/fonts/`)
- CAM IR: Check `libs/cam_ir/LICENSE`
- Other components: Check respective licenses

## Support

For issues specific to this configuration:
1. Check the troubleshooting section above
2. Review the logs on the **Activity** page (or `ruckus_log.log`)
3. Check HAL status with `halcmd show`
4. Review reference documents for hardware-specific issues

For general LinuxCNC support:
- LinuxCNC Forum: https://forum.linuxcnc.org/
- LinuxCNC Wiki: https://linuxcnc.org/docs/
- IRC: #linuxcnc on Libera.Chat

## Version History

- **v1.5**: Current version with AI Assistant, CAM IR integration, voice control
- **v1.1**: Base configuration with Mesa 7I96S support

---

**Last Updated**: 2026-09-29  
**LinuxCNC Version**: Master (2.9)  
**Configuration Version**: 1.5
