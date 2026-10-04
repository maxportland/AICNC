"""Tests for the camera pipeline: geometry, calibration, scanning, heights, probing, safety"""

import os
import shutil
import subprocess

import numpy as np
import pytest

from conftest import FakeStat
from milo_vision.geometry import CameraModel
from milo_vision import calibration, scan, laser, state
from milo_vision.cameras import SimCamera, SimScene
from milo_vision.heightmap import HeightMap
from milo_vision.probe_plan import probe_program, ProbeSettings

TRUTH = CameraModel(rotation=1.5, offset_x=58.0, offset_y=-12.0, z_offset=120.0, table_z=-240.0, calibrated=True)
LIMITS = {"X": (0.0, 500.0), "Y": (0.0, 175.0)}


@pytest.mark.parametrize("model", [
    TRUTH,
    CameraModel(rotation=-90.0, flip=-1.0, offset_x=-40.0, offset_y=25.0),
    CameraModel(dist=[-0.12, 0.05, 0.001, -0.0005, 0.0]),
])
def test_pixel_machine_round_trip(model):
    spindle = (120.0, 60.0, -30.0)
    points = np.array([[150.0, 40.0], [210.0, 95.0], [175.0, 60.0]])
    for plane in (model.table_z, model.table_z + 40.0):
        pixels = model.machine_to_pixel(points, spindle, plane)
        back = model.pixel_to_machine(pixels, spindle, plane)
        assert np.allclose(back, points, atol=1e-3)


def test_camera_centre_looks_straight_down_at_its_offset():
    spindle = (100.0, 50.0, 0.0)
    xy = TRUTH.pixel_to_machine([(TRUTH.cx, TRUTH.cy)], spindle)[0]
    assert xy == pytest.approx((158.0, 38.0))
    assert TRUTH.centre_over(158.0, 38.0) == pytest.approx((100.0, 50.0))


def test_placement_calibration_recovers_the_mounting():
    """Views of a tag through the simulated camera, solved from a wrong starting guess"""
    from milo_vision import detect
    tag_xy = (180.0, 90.0)
    scene = SimScene(TRUTH.table_z, tags=[__import__("milo_vision.cameras", fromlist=["SimTag"]).SimTag(
        3, tag_xy, 20.0, TRUTH.table_z)])
    pos = [0.0, 0.0, 0.0]
    cam = SimCamera(TRUTH, lambda: pos, scene, noise=2.0)
    guess = CameraModel(offset_x=50.0, offset_y=0.0, rotation=0.0, z_offset=100.0)
    observations = []
    for view in calibration.placement_views(tag_xy, guess, heights=[-60.0, 0.0]):
        pos[:] = view
        tags = detect.find_tags(cam.grab())
        if tags:
            observations.append(calibration.Observation(tuple(view), tags[0].center, tag_xy, TRUTH.table_z))
    assert len(observations) >= 12
    fitted, rms = calibration.solve_placement(guess, observations)
    assert rms < 0.1
    assert fitted.offset_x == pytest.approx(58.0, abs=0.2)
    assert fitted.offset_y == pytest.approx(-12.0, abs=0.2)
    assert fitted.rotation == pytest.approx(1.5, abs=0.1)
    assert fitted.z_offset == pytest.approx(120.0, abs=1.0)
    assert fitted.calibrated


def _scan(model_for_detection=TRUTH, scene=None):
    pos = [0.0, 0.0, 0.0]
    cam = SimCamera(TRUTH, lambda: pos, scene)
    table = scan.TableMap(model_for_detection, LIMITS)
    for view in scan.plan_scan(LIMITS, model_for_detection, -60.0):
        pos[:] = view
        table.add_frame(cam.grab(), view)
    for part in table.finish().parts:
        for view in scan.refine_views(part.center, model_for_detection, -60.0, LIMITS):
            pos[:] = view
            table.add_frame(cam.grab(), view)
    return table.finish()


def test_scan_finds_the_block_its_size_and_height():
    result = _scan()
    assert len(result.parts) == 1
    part = result.parts[0]
    # the demo block: X 172.3..248.5, Y 61.2..108.8, top 45 mm above the table
    assert part.center == pytest.approx((210.4, 85.0), abs=0.5)
    assert part.size == pytest.approx((76.2, 47.6), abs=0.5)
    assert abs(part.angle) < 1.0
    assert part.top_z == pytest.approx(-195.0, abs=1.0)
    assert result.tags[7] == pytest.approx((160.0, 40.0), abs=0.5)
    assert result.mosaic is not None and result.mosaic.size


def test_scan_plan_covers_the_table_in_serpentine():
    views = scan.plan_scan(LIMITS, TRUTH, 0.0)
    assert len(views) >= 2
    fov = TRUTH.field_of_view(0.0)
    covered_x = [v[0] + TRUTH.offset_x for v in views]
    assert min(covered_x) - fov[0] / 2 <= LIMITS["X"][0] + TRUTH.offset_x + 1
    assert all(LIMITS["X"][0] <= v[0] <= LIMITS["X"][1] and LIMITS["Y"][0] <= v[1] <= LIMITS["Y"][1] for v in views)


def test_parallax_height_of_a_point():
    point, top = (200.0, 80.0), -190.0
    a, b = (120.0, 90.0, -40.0), (170.0, 90.0, -40.0)
    pa = TRUTH.machine_to_pixel([point], a, top)[0]
    pb = TRUTH.machine_to_pixel([point], b, top)[0]
    assert scan.parallax_height(TRUTH, pa, a, pb, b) == pytest.approx(top, abs=1e-3)
    assert scan.parallax_height(TRUTH, pa, a, pa, a) is None  # same view: can't tell


def test_heightmap_blocks_moves_through_things():
    hm = HeightMap((0, 300), (0, 200), 2.0)
    hm.add_box([(100, 50), (160, 50), (160, 90), (100, 90)], -200.0, "vise")
    assert hm.check_move((50, 70, -150), (200, 70, -150)) is None            # well above
    hit = hm.check_move((50, 70, -210), (200, 70, -210))                      # straight through
    assert hit and hit["obstacle_z"] == pytest.approx(-200.0) and 95 <= hit["x"] <= 105
    assert hm.check_move((50, 70, -210), (95, 70, -210), tool_radius=3) is None   # stops short
    assert hm.check_move((50, 70, -210), (95, 70, -210), tool_radius=6) is not None  # but a fat tool reaches
    assert hm.check_move((130, 70, -205), (130, 70, -150), ignore_below=-200.0) is None  # lifting out


def test_heightmap_save_load(tmp_path):
    hm = HeightMap((0, 100), (0, 100), 2.0)
    hm.add_box([(10, 10), (30, 10), (30, 30), (10, 30)], -180.0, "block")
    path = str(tmp_path / "hm.npz")
    hm.save(path)
    loaded = HeightMap.load(path)
    assert loaded.max_in_circle(20, 20, 1) == pytest.approx(-180.0)
    assert loaded.boxes[0]["label"] == "block"
    assert HeightMap.load(str(tmp_path / "missing.npz")) is None


def _laser_image(rows_per_col, width=200, height=120):
    img = np.zeros((height, width, 3), np.uint8)
    for col, row in enumerate(rows_per_col):
        for dr in (-1, 0, 1):
            r = int(round(row)) + dr
            if 0 <= r < height:
                img[r, col, 2] = 255 if dr == 0 else 120
    return img


def test_laser_line_heights():
    width = 200
    table = laser.extract_line(_laser_image([60.0] * width))
    block = laser.extract_line(_laser_image([40.0] * width))        # 20 rows higher for a 10 mm block
    cal = laser.calibrate(table, -240.0, block, -230.0)
    profile = [60.0] * 50 + [50.0] * 100 + [60.0] * 50              # a 5 mm step in the middle
    z = laser.heights(laser.extract_line(_laser_image(profile)), cal)
    assert z[10] == pytest.approx(-240.0, abs=0.05)
    assert z[100] == pytest.approx(-235.0, abs=0.05)


def test_probe_program_structure():
    text = probe_program((210.4, 85.0), (76.2, 47.6), 0.3, -195.0, ProbeSettings(probe_tool=99), "corner")
    assert "o100 if [#5400 NE 99]" in text and "(abort, Load the touch probe T99 first)" in text
    assert text.index("#<top> = #5063") < text.index("(--- X- edge ---)") < text.index("(--- Y- edge ---)")
    assert "G53 G0 X165.3000 Y85.0000" in text          # 6 mm clearance + 1 mm tip radius outside X-
    assert "G10 L20 P1 X[#5420 - #<xmin>] Y[#5421 - #<ymin>] Z[#5422 - #<top>]" in text
    assert "X+ edge" not in text
    center = probe_program((210.4, 85.0), (76.2, 47.6), 0.3, None, origin="center")
    assert "X+ edge" in center and "Y+ edge" in center and "G38.2 Z-250" in center


def test_probe_program_refuses_turned_parts():
    with pytest.raises(ValueError):
        probe_program((200, 80), (60, 40), 12.0, -190.0)
    probe_program((200, 80), (60, 40), 91.0, -190.0)  # a quarter turn is still square to the table


@pytest.mark.skipif(shutil.which("rs274") is None, reason="LinuxCNC's rs274 interpreter isn't installed")
@pytest.mark.parametrize("origin", ["corner", "center"])
def test_probe_program_runs_in_the_interpreter(tmp_path, origin):
    ngc = tmp_path / "probe.ngc"
    ngc.write_text(probe_program((210.4, 85.0), (76.2, 47.6), 0.3, -195.0, ProbeSettings(probe_tool=0), origin))
    tbl = tmp_path / "tool.tbl"
    tbl.write_text("T1 P1 D6 Z10 ;tool\n")
    out = subprocess.run(["rs274", "-t", str(tbl), "-g", str(ngc)], capture_output=True, text=True, timeout=30)
    text = out.stdout + out.stderr
    assert "PROGRAM_END" in text, text[-2000:]
    assert "STRAIGHT_PROBE" in text and "SET_G5X_OFFSET(1" in text
    assert "abort" not in text.lower()


def test_state_round_trip_and_description(tmp_path):
    result = _scan()
    state.save(result, str(tmp_path))
    loaded = state.load(str(tmp_path))
    assert loaded.parts[0].center == pytest.approx(result.parts[0].center)
    text = state.describe(loaded, calibrated=True)
    assert "part 1: 76." in text and "top at tip Z -19" in text and "marker 7" in text
    assert "NOT CALIBRATED" in state.describe(loaded, calibrated=False)
    assert state.describe(None) == "Camera: no table scan yet."


def test_obstacles_refuse_ai_moves_through_them():
    from machine_safety import check_obstacles
    hm = HeightMap((0, 500), (0, 200), 2.0)
    hm.add_box([(150, 50), (270, 50), (270, 60), (150, 60)], -190.0, "vise jaw")
    # FakeStat: at machine X100 Y50 Z-10, no tool offset
    s = FakeStat()
    assert check_obstacles("G53 G0 X300", s, hm) is None                      # Z-10 is far above the jaw
    low = FakeStat(position=[100.0, 55.0, -200.0] + [0.0] * 6)
    reason = check_obstacles("G53 G0 X300", low, hm)
    assert reason and "Raise Z first" in reason
    assert check_obstacles("G53 G0 Z-20", low, hm) is None                    # straight up is fine
    assert check_obstacles("M5", low, hm) is None
    assert check_obstacles("G53 G0 X300", low, None) is None                  # no map, no check


def test_action_controller_uses_the_map(monkeypatch):
    import action_controller
    hm = HeightMap((0, 500), (0, 200), 2.0)
    hm.add_box([(150, 50), (270, 50), (270, 60), (150, 60)], -190.0, "vise jaw")
    monkeypatch.setattr(action_controller, "_heightmap", lambda: hm)
    low = FakeStat(position=[100.0, 55.0, -200.0] + [0.0] * 6)
    action, refusal = action_controller.propose_action(
        {"intent": "mdi", "mdi": "G53 G0 X300", "summary": "Move to X300"}, low)
    assert action is None and "camera measured" in refusal


def test_vision_page_scans_the_simulated_table(qapp, tmp_path, monkeypatch):
    from PyQt5 import QtCore
    from PyQt5.QtWidgets import QApplication
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    import milo_ui.pages.vision as vision_page
    monkeypatch.setattr(vision_page, "PROBE_DIR", str(tmp_path / "probe"))
    from milo_ui import preview
    _app, shell, m = preview.build("vision")
    m.estop, m.on = False, True
    m.homed = {a: True for a in m.axes}
    page = shell.pages["vision"]
    page.model = TRUTH  # as if calibrated
    page.on_show()
    from milo_vision.background import PHOTO_OVERLAP
    views = scan.plan_scan(m.limits, page.model, page._scan_z(), overlap=PHOTO_OVERLAP)

    def wait(done):
        timer = QtCore.QElapsedTimer()
        timer.start()
        while not done() and timer.elapsed() < 60000:
            QApplication.processEvents()
            QtCore.QThread.msleep(10)

    # The sim table is a bright fixture plate, like the real one: photograph it empty first (the
    # block taken off), through the page's own routine
    assert page.empty_table() is None
    objects = page.camera.scene.objects
    page.camera.scene.objects = [o for o in objects if o.label != "aluminium block"]
    page._photograph_empty(views)
    wait(lambda: page.runner is None)
    page.camera.scene.objects = objects
    assert page.empty_table() is not None and len(page.empty_table().frames) == len(views)
    assert np.allclose(page._scan_views(), views, atol=1e-3)   # scans revisit the photographed spots
    page._scan(page._scan_views())
    wait(lambda: page.runner is None and page.result is not None)
    assert page.result is not None and len(page.result.parts) == 1
    assert page.result.parts[0].top_z == pytest.approx(-195.0, abs=1.5)
    assert os.path.exists(os.path.join(page.vision_dir, "heightmap.npz"))
    page._make_probe_program()
    assert m.file.startswith(str(tmp_path)) and "probe_part_" in m.file
    shell.close()


# --- the line laser on this machine: mono camera, bright fixture plate -----------------------------

def _sim(scene, mono=True, at=(0.0, 0.0, 0.0)):
    pos, state = list(at), {"laser": False}
    cam = SimCamera(TRUTH, lambda: pos, scene, mono=mono, laser=lambda: state["laser"])
    return cam, pos, lambda on: state.__setitem__("laser", bool(on))


def test_capture_pair_throws_away_frames_in_flight_and_leaves_the_laser_off():
    log, state = [], {"laser": False}

    class Cam:
        def grab(self):
            log.append(state["laser"])
            return np.full((4, 4), 200 if state["laser"] else 10, np.uint8)

    on, off = laser.capture_pair(Cam(), lambda v: state.__setitem__("laser", v), settle_frames=2)
    assert log == [False] * 3 + [True] * 3 and on[0, 0] == 200 and off[0, 0] == 10
    assert state["laser"] is False


def test_laser_line_on_a_mono_camera_over_the_bright_plate():
    scene = SimScene.fixture_plate(TRUTH.table_z)
    scene.objects = []
    cam, pos, set_laser = _sim(scene)
    spindle = (300.0, 90.0, 0.0)
    pos[:] = spindle
    on, off = laser.capture_pair(cam, set_laser)
    assert laser.is_mono(on)
    # the colour method sees nothing on a mono camera; the laser-off difference finds the whole line
    assert np.isfinite(laser.extract_line(on, colour="red")).sum() == 0
    rows = laser.extract_line(on, background=off)
    assert np.isfinite(rows).mean() > 0.95
    y = cam.laser_y(spindle, TRUTH.table_z)
    expected = TRUTH.machine_to_pixel([(TRUTH.offset_x + spindle[0], y)], spindle, TRUTH.table_z)[0][1]
    assert np.nanmedian(rows) == pytest.approx(expected, abs=0.5)


def test_saturated_wide_line_gives_its_middle():
    img = np.zeros((100, 30), np.uint8)
    img[40:52, :] = 255            # flat-topped, 12 rows wide
    img[39, :] = img[52, :] = 120
    assert np.nanmean(laser.extract_line(img)) == pytest.approx(45.5, abs=0.1)


def test_laser_heights_with_the_exact_model():
    """Calibrate on the bare plate and a 20 mm gauge, then measure the 45 mm block: the straight-line
    model reads it a few mm out, the perspective model to a few tenths (the sim's line is anti-aliased)"""
    spindle = (152.0, 97.0, 0.0)   # camera over the block, the line across it
    def rows_for(objects):
        scene = SimScene.fixture_plate(TRUTH.table_z)
        scene.objects = objects
        cam, pos, set_laser = _sim(scene)
        pos[:] = spindle
        on, off = laser.capture_pair(cam, set_laser)
        return laser.extract_line(on, background=off)
    from milo_vision.cameras import SimObject
    plate = [(-500, -500), (1500, -500), (1500, 700), (-500, 700)]
    table = rows_for([])
    gauge = rows_for([SimObject(plate, TRUTH.table_z + 20.0, (180, 180, 180), "gauge")])
    distance = TRUTH.distance(spindle[2], TRUTH.table_z)
    exact = laser.calibrate(table, TRUTH.table_z, gauge, TRUTH.table_z + 20.0, distance=distance)
    linear = laser.calibrate(table, TRUTH.table_z, gauge, TRUTH.table_z + 20.0)
    demo = rows_for(SimScene.demo(TRUTH.table_z).objects)
    block_cols = slice(700, 760)   # columns over the middle of the block
    top = TRUTH.table_z + 45.0
    assert np.nanmedian(laser.heights(demo, exact)[block_cols]) == pytest.approx(top, abs=0.3)
    assert abs(np.nanmedian(laser.heights(demo, linear)[block_cols]) - top) > 2.0
    assert np.nanmedian(laser.heights(table, exact)) == pytest.approx(TRUTH.table_z, abs=0.05)
    again = laser.LaserCalibration.from_dict(exact.to_dict())
    assert np.nanmedian(laser.heights(demo, again)[block_cols]) == pytest.approx(top, abs=0.3)


# --- parts on the bright plate: what changed from the empty table ------------------------------------

def _plate_scan(empty_table):
    """As the page does it: the empty table's spots, then closer looks only where the height is unknown"""
    pos = [0.0, 0.0, 0.0]
    cam = SimCamera(TRUTH, lambda: pos, SimScene.fixture_plate(TRUTH.table_z), mono=True)
    table = scan.TableMap(TRUTH, LIMITS, empty_table=empty_table)
    views = empty_table.positions if empty_table is not None else scan.plan_scan(LIMITS, TRUTH, -60.0)
    for view in views:
        pos[:] = view
        table.add_frame(cam.grab(), view)
    for part in table.finish().parts:
        if empty_table is not None:
            break
        for view in scan.refine_views(part.center, TRUTH, -60.0, LIMITS):
            pos[:] = view
            table.add_frame(cam.grab(), view)
    return table.finish(), cam


def test_scan_on_the_bright_plate_finds_the_block_against_the_empty_table(tmp_path):
    from milo_vision.cameras import sim_empty_table
    from milo_vision.background import EmptyTable
    cam = SimCamera(TRUTH, lambda: (0, 0, 0), SimScene.fixture_plate(TRUTH.table_z), mono=True)
    from milo_vision.background import PHOTO_OVERLAP
    empty = sim_empty_table(cam, scan.plan_scan(LIMITS, TRUTH, -60.0, overlap=PHOTO_OVERLAP), TRUTH)
    empty.save(str(tmp_path / "empty"))
    empty = EmptyTable.load(str(tmp_path / "empty"), TRUTH)
    result, _ = _plate_scan(empty)
    assert len(result.parts) == 1        # the block; the vise was there when the table was photographed
    part = result.parts[0]
    assert part.center == pytest.approx((210.4, 85.0), abs=0.6)
    assert part.size == pytest.approx((76.2, 47.6), abs=0.8)
    assert part.top_z == pytest.approx(-195.0, abs=1.5)


def test_brightness_alone_finds_the_plate_not_the_block():
    result, _ = _plate_scan(None)
    sizes = [p.size for p in result.parts]
    assert not any(abs(w - 76.2) < 1 and abs(h - 47.6) < 1 for w, h in sizes)


def test_empty_table_views_line_up(tmp_path):
    from milo_vision.cameras import sim_empty_table
    cam = SimCamera(TRUTH, lambda: (0, 0, 0), SimScene.fixture_plate(TRUTH.table_z), mono=True, noise=0)
    views = scan.plan_scan(LIMITS, TRUTH, -60.0)
    empty = sim_empty_table(cam, views, TRUTH, without=("aluminium block", "vise fixed jaw", "vise moving jaw"))
    exact = empty.view(views[0], (TRUTH.height, TRUTH.width))
    assert exact.tolerance == 2 and exact.valid.all()
    # a spot between the photos: the mosaic re-projected matches what the camera sees there
    spot = ((views[0][0] + views[1][0]) / 2, views[0][1] + 7.0, -60.0)
    projected = empty.view(spot, (TRUTH.height, TRUTH.width))
    assert projected.tolerance == 6 and projected.valid.mean() > 0.8
    actual = SimCamera(TRUTH, lambda: spot, empty_scene_of(cam), mono=True, noise=0).grab()[..., 0]
    known = projected.valid > 0
    assert np.median(np.abs(actual[known].astype(int) - projected.background[known].astype(int))) <= 3
    # and nothing there counts as a part
    from milo_vision import detect
    assert detect.find_stock(actual, background=projected.background, tolerance=projected.tolerance,
                             valid=projected.valid) == []


def empty_scene_of(cam):
    from dataclasses import replace
    return replace(cam.scene, objects=[])
