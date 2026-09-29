"""
Milo's eyes: a downward camera on the spindle head (and optionally a line laser beside it).

Everything here works in machine coordinates. The camera rides on the head, so the machine
position tells us exactly where it is; after calibration every pixel maps to a machine X/Y on
the plane it is looking at.

    geometry.py     camera model: pixel <-> machine coordinates
    calibration.py  intrinsics (ChArUco) and camera-to-spindle placement (solver)
    detect.py       AprilTags and stock outlines in an image
    scan.py         raster scan planning, stitching frames into a table map
    laser.py        line-laser triangulation: heights from the laser line
    heightmap.py    obstacle height map and collision checks for straight moves
    probe_plan.py   vision-guided probing: a G38 program that sets G54 on the found part
    cameras.py      camera backends: Raspberry Pi camera, USB/OpenCV, simulated
    state.py        what the last scan found (shared with Milo)

No Qt here except cameras.SimCamera's optional rendering helpers; the maths is testable alone.
See VISION_HARDWARE.md for the hardware this is written for.
"""
