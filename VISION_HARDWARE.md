# Milo Vision: hardware guide

Giving Milo eyes: a camera on the spindle head finds parts and obstacles on the table to about
half a millimetre, and the touch probe then measures the part to a hundredth and sets G54.
Vision gets the probe *close*; the probe does the *precise* part.

```
   side view (looking along X: column at the back,      top view of the head
   operator at the front)
                                                                 column
   column ┌────── head casting ──────────────┐            ─────────────────────
          │                                  │                   ● spindle
          │  spindle       bracket ──────────┤                   │ ~60 mm
          │    │      ┌─────────┐     │      │          ═══════════◉═══════════  line on the table,
          │    │      │ camera  │◄────┘      │                   │ camera        along X, under the
          │    │      │ + ring  │   laser ▭  │                   │ ~90 mm        camera's centre
          │    │      │  light  │     ╱ θ ≈ 14°                  ▭ laser
          │   tool    └────┬────┘    ╱                       (operator)
          │    ▼           │ view   ╱
   ───────┴────────────────▼◄──────  line on the table, under the camera's centre
```

- The **camera** looks straight down, ~60 mm in front of the spindle axis (the exact offset is measured
  by calibration, so it doesn't need to be precise). Its lens sits **well above the spindle nose**
  (~100–150 mm), so it can never reach anything the tool can't.
- A **ring light** around the lens gives even, shadow-free light; that matters more than the camera.
- A **line laser** in front of the camera (spindle, camera, laser in a line toward the operator),
  tilted back toward the camera's view, draws a line left to right across the image. Anything taller
  than the table shifts the line up or down the image; that shift is its height.
- Everything is fixed rigidly to the **head casting** (it moves with Z), so the machine position
  always tells the software where the camera is.

## Shopping list

Prices are approximate (USD, 2026); check current prices and availability.

### Core: camera, light, markers (≈ $120–170)

| # | Item | Recommended | Why | ≈ Price |
|---|------|-------------|-----|---------|
| 1 | Camera | **Raspberry Pi Global Shutter Camera** (Sony IMX296, 1456×1088, C/CS mount) | Clean, low-noise pixels; interchangeable lenses; plugs into the Pi 5 | $50 |
| 2 | Lens | **Raspberry Pi 6 mm wide-angle CS-mount lens** | At the scan height it sees ~300×225 mm at ~0.2 mm/pixel, so the table is covered in about 4 photos | $25 |
| 3 | Cable | **CSI-to-HDMI extension kit for Pi 5** (22-pin), plus a **high-flex HDMI cable** routed through the drag chain | The flat camera ribbon is short and hates flexing | $20–30 |
| 4 | Light | **LED ring light**, ~60–80 mm inner diameter, 12 or 24 V, with diffuser | Even light without hard shadows; shiny aluminium becomes readable | $15–25 |
| 5 | Optional | **Linear polarizer film** (one piece over the light, one over the lens, crossed) | Kills glare off machined aluminium | $10 |
| 6 | Markers | **AprilTag 36h11** markers printed on vinyl sticker paper or laminated, 15–25 mm squares; a few stuck on the vise and table | Sub-pixel reference points; make fixtures instantly recognisable | $5 |
| 7 | Calibration board | Print `vision_calibration_board.png` (Vision → Calibrate → Save board image) and glue it to a flat plate (acrylic, aluminium, glass) | Lens calibration needs a flat, accurate target | $10 |

**Alternative to 1–3 (easier wiring):** a **USB global-shutter camera with an M12 lens mount**
(e.g. an Arducam UVC global-shutter module, ~$60–90) and a **high-flex USB cable** through the
drag chain. Milo supports both; USB is simpler to route, the Pi camera gives a slightly better image.

### Heights: line laser (≈ $30–40)

| # | Item | Recommended | Why | ≈ Price |
|---|------|-------------|-----|---------|
| 8 | Line laser | **[Quarton VLM-650-28 LPT](https://www.amazon.com/Quarton-VLM-650-28-LPT-Generator-ECONOMICAL/dp/B00ARBPI5Y)**, 650 nm red line, Class 1 (<0.39 mW), >60° fan, Ø9 × 26 mm, 2.6–6 V | Eye-safe; optimised for short range (0.3–1.8 m), close to the camera's ~150–300 mm; automatic power control keeps the line's brightness steady, which the height measurement relies on | $29 |
| 9 | Switching | Laser: nothing to buy, the **7i96S's OUT0** (a floating solid-state switch) and its +5VP pin (see *Wire*). Light: a free 7i84 output if it's 24 V, else a MOSFET or relay module | Lets LinuxCNC turn them on only during scans | $0–10 |

**Choosing the laser.** The ideal spec is a 650 nm line laser, Class 2 (≤1 mW) or lower, focused for
short range, with stable output. When checked on Amazon (September 2026), no listing combined that
spec with a 12 mm body, 4.5+ stars and a decent number of reviews: well-reviewed modules are mostly
5 mW (Class 3R), and those matching the spec have few ratings. Prices and ratings change; check
before buying.

| Option | Spec | Rating when checked | ≈ Price | Notes |
|--------|------|---------------------|---------|-------|
| **[Quarton VLM-650-28 LPT](https://www.amazon.com/Quarton-VLM-650-28-LPT-Generator-ECONOMICAL/dp/B00ARBPI5Y)** (recommended) | Class 1, Ø9 × 26 mm, fixed focus for 0.3–1.8 m, APC, plastic optics | 4.2★, 136 ratings | $29 | Industrial maker (Taiwan). Needs a **9 mm** hole in the bracket. Dimmer than Class 2: the height sweep turns the ring light off while the laser is on |
| [BEITESI S070, "line" 2-pack](https://www.amazon.com/dp/B0CN9HZ9B4) | Class 2 (<1 mW), Ø12 mm copper, adjustable focus, 3–5 V | 4.0★, 14 ratings | $18 | Cheapest exact spec match; generic, no stated power control; cut off the USB plug to wire it. Its 4.5★ ["traverse"](https://www.amazon.com/dp/B0CN9G7DTW) sibling is most likely the **cross** pattern: don't buy that one |
| [Quarton VLM-650-27 LPT](https://www.amazon.com/Quarton-VLM-650-27-LPT-Generator-INDUSTRIAL/dp/B00ARBOLPM) | Class 2 (<1 mW centre), Ø12.5 × 30 mm brass, quartz line lens, APC, 2.6–5 V | 4.0★, 1 rating | $29 | Exact spec from the better maker, but barely any reviews yet |
| [Quarton VLM-650-27 LPT-30](https://www.amazon.com/dp/B00ZXDIO70) | Class 1, Ø12.5 × 30 mm brass, >90° fan | 4.1★, 65 ratings | $89 | Well made but focused for 5–10 m: the line would be wide and soft at our range. Not recommended here |

Avoid the many 5 mW (Class 3R) modules: they're brighter than needed and not eye-safe for a light
that points at the table you're standing over.

### Protection and mounting (≈ $20–40, or print/machine it)

| # | Item | Recommended | Why | ≈ Price |
|---|------|-------------|-----|---------|
| 10 | Bracket | Aluminium plate bracket, or 3D printed in PETG/ASA, bolted to the **head casting** (not the belt cover) | Any flex or slip means recalibrating | $0–20 |
| 11 | Housing | Small box around the camera with a **replaceable window** (glass microscope slide or 2 mm acrylic) | Chips and coolant stay off the lens | $5–10 |
| 12 | Air puff | Small **air nozzle** on the window, from shop air through a regulator and a **24 V solenoid valve** (switchable from a 7i84 output) | Blows chips off before each scan | $15–25 |

### Precision: the touch probe

Your HAL already has a workpiece probe input: `hm2_7i96s.0.7i84.1.0.input-03-not` → `probe-or`
→ `motion.probe-input` (the tool setter shares it). If what's on it now is a touch plate or a basic
probe, a real 3D touch probe is the upgrade that makes vision-guided probing worthwhile:

| # | Item | Options | Notes | ≈ Price |
|---|------|---------|-------|---------|
| 13 | 3D touch probe | Tormach PassivProbe · Wildhorse Innovations 3D probe · budget "V6"-style 3D probes | Pick one with a **straight shank** that fits an R8 collet or your ER holder; **normally-closed** wiring so a broken wire reads as "triggered" (fails safe) | $60–300 |
| 14 | Probe cable | Coiled cable with a quick-disconnect connector near the head | So the probe can live in the tool rack | $10–20 |

Measure the probe's length with the tool setter like any tool, and set its **tip diameter** on the
Vision page (Probe tip diameter). Use a dedicated tool number for it (the Vision page defaults to
**T99**; add it to the tool table).

### Later, optional: whole-table awareness

| Item | Options | Use | ≈ Price |
|------|---------|-----|---------|
| Overhead depth camera on a fixed bracket | Luxonis OAK-D Lite / OAK-D Pro (runs its own neural network), Intel RealSense D405 (better up close) | Live, coarse 3D view of the whole work area: "a clamp or a hand is in the way". Millimetres to centimetres, not for measuring | $150–300 |

**Not recommended:** spinning lidars and small time-of-flight sensors. At this machine's scale they
are far too coarse (±1–3 cm) and many can't measure closer than 10–15 cm. The line laser above *is*
the kind of lidar that works here.

## Setting it up

### 1. Mount

1. Bolt the bracket to the head casting so the camera looks straight down (a small tilt is
   calibrated out; aim for "looks square by eye"). Keep the lens **100–150 mm above the spindle nose**.
2. Mount the laser in front of the camera: see *Mounting the laser* below.
3. Fit the ring light around the lens and the housing window below it; aim the air nozzle across the window.
4. Route the camera cable, light and laser wiring through the drag chain with strain relief at both ends.

#### Mounting the laser

What `milo_vision/laser.py` needs:

1. **The line runs left to right in the Live image** (along the image's long side, 1456 px). The code
   finds the line once per image column, so a line running top to bottom isn't seen.
2. **The laser sits off the image's top or bottom edge**, perpendicular to the line. A taller surface
   moves the line up or down the image; that movement is the height.

On this PM-25 the camera is in front of the spindle, so the laser goes **in front of the camera**:
spindle, camera, laser in a line toward the operator, and the line runs along **machine X** (the long
axis, ~300 mm per pass; a sweep steps through Y). Nothing is in its way there, and the beam tilts back
toward the column, away from the operator. **Check the camera first:** jogging X should slide the Live
image along its long side. If the long side runs front to back, turn the camera 90° (then redo the
placement calibration) or put the laser to the left or right of the camera instead.

**Distance and tilt.** The angle is right for one camera height only, and the laser calibration and
sweeps run at a fixed height near the top of Z. With the default camera model (lens ~120 mm above the
tip, table at tip Z −240) the lens is **D ≈ 360 mm** above the table at Z0. Aim the beam at the table
under the camera's centre: **tan θ = b / D**, θ from vertical, b the camera-to-laser distance.

| b | θ (D = 360 mm) | Height per pixel | |
|---|---|---|---|
| 60 mm | 9.5° | ~1.2 mm | compact, coarser (still usable) |
| **90 mm** | **14°** | **~0.75 mm** (~0.1 mm with sub-pixel line finding) | **recommended** |
| 150 mm | 23° | ~0.45 mm | finer, but a long arm and more shadowing behind tall parts |

Every option reads heights well beyond anything on the table, so range isn't the limit. (A ~30° tilt
only 30–60 mm from the camera would cross the camera's view ~70 mm below the lens and miss the field
of view at the table.)

**The bracket:**
- **Adjustable tilt** (a pivot with about ±10° and a lock screw): with the head at the scan height,
  turn the laser on and tilt until the line lies across the middle of the Live image.
- **A 9 mm bore with a set screw**, so the barrel turns to level the line in the image.
- The laser exit **no lower than the camera lens**, so it never comes closer to the work than the camera.
- On the **same rigid bracket or the head casting** as the camera: if one moves against the other, recalibrate.
- It's ~150 mm in front of the spindle, where heads and hands go: tuck it under a lip or a shroud so
  it doesn't get knocked or covered in chips (a knock means re-aiming and recalibrating).
- The VLM-650-28's >60° fan makes a ~400 mm line at 360 mm, wider than the ~300 mm view, and 360 mm is
  inside its 0.3–1.8 m focus range.

### 2. Wire

- **Camera**: Pi 5 camera port (via the CSI-HDMI kit), or USB.
- **Laser**: switched by the **7i96S's OUT0** from its own 5 V (below), already set up in the HAL.
- **Light, air valve**: from free 7i84 outputs (through a MOSFET or relay module if they aren't
  24 V; a 7i84 output gives up to 300 mA). In use in `Mesa7I96S.hal`: output-00 (spindle power), 05
  (drawbar lift), 06 (mist), 07 (drawbar driver ENA). Output-02 is set to sink, not source. Free
  sourcing outputs: **01, 03, 04, 08–15**. The HAL pins for these aren't created yet; see *What's
  next* below. Until then, leave the light on.
- **Probe**: normally-closed probe on input 03 as today.

#### The laser circuit

The 7i96S's outputs OUT0–OUT3 are floating DC solid-state switches (0–36 V, 2 A, 75 mΩ), used
"just like a switch or relay contact", and its +5VP pins are PTC-protected 5 V for field wiring
([7I96S manual](https://www.mesanet.com/pdf/parallel/7i96sman.pdf)). So the laser needs no regulator:

```
 TB1 pin 6  (+5VP) ─────────── TB3 pin 14 (OUT0+)
                               TB3 pin 13 (OUT0−) ─────── laser +
 TB1 pin 7  (GND)  ───────────────────────────────────── laser −
```

- **OUT0+ to the 5 V side.** The outputs are polarised: reversed, they're stuck on.
- **Check the laser's leads before powering it.** Usually red + and black −, but confirm on the
  module's label or datasheet: reversed polarity can kill the laser diode.
- Any +5VP / GND pair works (TB1 pins 6/12/18/24 are +5VP, 7/19 GND). The laser's tens of mA are
  nothing to the 7i96S's 5 V supply, and the PTC covers a short in the cable.
- A 2-core cable through the drag chain to the head. Leave slack at the head for tilting and
  turning the barrel when aiming.
- Not always-on 5 V: the line is found in the difference between a frame with the laser off and
  one with it on (the camera is monochrome and the table bright), so the screen must be able to
  switch it.

In the HAL (`Mesa7I96S.hal`, `custom_postgui.hal`) OUT0 (`hm2_7i96s.0.ssr.00.out-00`) is on when
either G-code asks (**M64 P1** on, **M65 P1** off) or the screen does (its `milo.laser` pin: the
Vision page's **Laser** button, and the laser sweeps). It's off whenever LinuxCNC isn't running.

**Aim it** with **Vision → Live → Laser**: the screen flashes the laser off and on, finds the line,
draws it in green and says where it is ("row 555, image centre 544"). With the head at the scan
height, tilt the laser until the line crosses the middle of the image, level. If it says *laser line
not found*, the laser isn't being switched (still on fixed 5 V?) or isn't in view.

### 3. Software

```bash
sudo apt install python3-picamera2     # only for the Pi camera; USB cameras work already
```

OpenCV and the rest are already in the config's venv. Open **Vision** in the rail: the Setup
card should name your camera. **Live** shows what it sees.

### 4. Calibrate (Vision → Calibrate…)

1. **Lens** (once per camera, lens and focus setting): save and print the board at 100% (check a
   square measures 25 mm), glue it flat, and take ~15 photos with it at different positions and
   tilts under the camera. *Solve lens* should report under 0.5 px.
2. **Placement** (after mounting, and after anything bumps the camera): stick a printed AprilTag
   flat on the table, jog a pointed tool so its tip just touches the tag's centre, tap *Use this
   point*, then *Photograph the tag*. The head rises and photographs the tag from 18 spots at two
   heights; the fit error should be under 0.2 mm. Save.
3. **Laser** (when the laser is fitted): a sweep over the bare table and over a gauge block of known
   height (planned, see below). The height calculation is ready for it in `milo_vision/laser.py`
   (the exact perspective model: a surface h above the table shifts the line by
   `scale·h / (D − h)` pixels, D the camera's height above the table, so one gauge block calibrates
   it for every height).
4. **Empty table** (Vision → *Photograph the empty table*): clear off everything that isn't always
   there, leave the vise and fixtures that stay, and the head photographs the table from a grid of
   spots at the scan height. This table is bright aluminium with dark holes, so parts can't be found
   by being brighter than the table: scans find what's *changed* since this photo instead. The
   Setup card says when it was taken. Take it again after changing the camera height, the lighting
   or the fixtures.

### 5. Use

1. **Vision → Scan the table.** The head goes up first, then photographs the table from the same
   spots as the empty-table photo, and finds what changed. The grid is dense enough that each part
   is seen whole from two spots, which gives its height by parallax; a part seen whole only once
   keeps an unknown height (the probe program then measures the top itself). Without an
   empty-table photo it finds bright parts on a dark table, then takes two closer looks at each
   part for its height.
2. The **table map** shows what was found. Tap a part to select it.
3. Choose **Corner** or **Center** for the origin, then **Create probe program**. It's loaded for
   review on the Program page. Load the probe, check the path in the preview, press **Start**.
   The program sets G54 on the measured part.
4. Milo now knows the scan too: ask "where's my part?" or "face the stock, 1 mm" and it uses the
   scanned size. When Milo proposes a straight move, the move is checked against the heights the
   scan measured, and refused if the tool would pass through something.

## What to expect

| Measurement | Typical accuracy |
|-------------|------------------|
| Part position and size from the camera | ±0.3–0.5 mm (well calibrated, good light) |
| Part top height from parallax | ±1 mm |
| Heights from the line laser | ±0.1 mm |
| Touch probe | ±0.01 mm or better |

With the empty-table photo it finds anything that looks different from the table there: stock
brighter or darker than the plate, or covering its holes. Stock the same shade as the plate is found
by its edges and the holes it covers. Shadows from one-sided light, very shiny surfaces without the
polarizer, coolant puddles and chip piles all confuse it; markers on fixtures make everything more
reliable. In **Live**, spots the empty table wasn't photographed from are compared with the photos
stitched together, where tall things like a vise land in the wrong place: expect stray outlines
at a vise's edges there (scans only use the exact photos).

## Safety built in

- Camera routines only start after you confirm them, with the spindle stopped, and always rise to
  the scan height before moving across the table. Stop, E-stop or power-off ends them.
- The probe program checks the probe is in the spindle, measures the top first, descends beside the
  part with moves that stop and abort if they touch anything, and searches only a limited distance
  for each edge. It sets G54 only after all edges were found.
- The height map only ever refuses moves; it never lets through a move that LinuxCNC's own limits
  wouldn't. If the setup changes after a scan, rescan (a stale map can refuse moves that are fine).

## What's next (software)

Done now:
- the camera model, calibration solvers (lens, placement), and the calibration wizard
- AprilTag and part detection, table scanning and stitching, height by parallax
- the height map, and collision checks on Milo's moves
- vision-guided probe programs (G38.2/G38.3, sets G54), tested in LinuxCNC's interpreter
- the **Vision** page, a simulated camera for development, and Milo knowing what the scan found

Also done:
- the laser on 7i96S OUT0 (M64 P1, or the screen's `milo.laser`), the **Laser** aiming view, and line
  finding for the mono camera (laser off/on difference)
- the exact perspective height model for the laser
- **Photograph the empty table**, and scans that find what changed from it

Next steps:
- HAL outputs for the light and air puff, switched automatically during scans
- the laser calibration and sweep UI (the line finding and height model are ready in
  `milo_vision/laser.py`; the sweep should also shorten the exposure so the line isn't saturated)
- "Hey Milo, find the part and set G54": scan, pick the part, create the probe program and
  propose running it, all confirmed like any other action
- marking vises and clamps as keep-out zones (by marker or by tapping them on the map)
- rotated parts (probe two points per edge and set G54 rotation)
- tuning part detection on real photos of this table
