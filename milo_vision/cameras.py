"""
Camera backends. Each gives BGR frames (numpy arrays) through grab().

PiCamera      Raspberry Pi camera on the CSI port (picamera2: sudo apt install python3-picamera2)
OpenCVCamera  any USB/UVC camera OpenCV can open (/dev/videoN)
SimCamera     renders the view of a pretend table from the camera model and the machine's
              position: for previews, development without hardware, and tests with known answers

open_camera("auto") tries the Pi camera, then USB, and returns None if neither is there.
"""

import math
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

from milo_vision.geometry import CameraModel


class Camera:
    name = "camera"

    def grab(self) -> Optional[np.ndarray]:
        raise NotImplementedError

    def close(self):
        pass


class PiCamera(Camera):
    name = "Raspberry Pi camera"

    def __init__(self, size=(1456, 1088)):
        from picamera2 import Picamera2  # noqa: import here: optional dependency
        self.cam = Picamera2()
        config = self.cam.create_still_configuration(main={"size": size, "format": "RGB888"})
        self.cam.configure(config)
        self.cam.start()

    def grab(self):
        frame = self.cam.capture_array("main")  # "RGB888" is BGR byte order in picamera2
        return frame

    def close(self):
        self.cam.stop()
        self.cam.close()


class OpenCVCamera(Camera):
    name = "USB camera"

    def __init__(self, index=0, size=(1920, 1080)):
        import cv2
        self.cap = cv2.VideoCapture(index)
        if not self.cap.isOpened():
            raise RuntimeError(f"No camera at /dev/video{index}")
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, size[0])
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, size[1])
        # a Pi 5's CSI nodes (/dev/video0..) open fine but never deliver a frame
        if not self.cap.read()[0]:
            self.cap.release()
            raise RuntimeError(f"/dev/video{index} gives no frames")

    def grab(self):
        for _ in range(2):  # drop a buffered frame so the image matches the current position
            self.cap.grab()
        ok, frame = self.cap.read()
        return frame if ok else None

    def close(self):
        self.cap.release()


def open_camera(kind: str = "auto") -> Optional[Camera]:
    if kind in ("auto", "pi"):
        try:
            return PiCamera()
        except Exception:
            if kind == "pi":
                raise
    if kind in ("auto", "usb"):
        import glob
        for index in range(0, 4):
            try:
                return OpenCVCamera(index)
            except Exception:
                continue
        if kind == "usb":
            raise RuntimeError("No USB camera found")
    return None


# --- simulation --------------------------------------------------------------------------------

@dataclass
class SimObject:
    polygon: List[Tuple[float, float]]   # machine XY
    top_z: float                          # tip Z of its top
    colour: Tuple[int, int, int]          # BGR
    label: str = ""


@dataclass
class SimTag:
    id: int
    center: Tuple[float, float]
    size: float          # printed black square, mm
    z: float             # tip Z of the surface it's stuck on


@dataclass
class SimScene:
    table_z: float = -240.0
    objects: List[SimObject] = field(default_factory=list)
    tags: List[SimTag] = field(default_factory=list)
    slot_ys: Tuple[float, ...] = (20.0, 87.5, 155.0)  # T-slot centre lines along X
    table_colour: Tuple[int, int, int] = (38, 40, 44)  # BGR; dark cast iron
    hole_pitch: float = 0.0       # a fixture plate's grid of holes (0: none), mm
    hole_diameter: float = 10.0

    @staticmethod
    def fixture_plate(table_z=-240.0):
        """This machine's table: bright aluminium with a grid of dark holes, no T-slots"""
        scene = SimScene.demo(table_z)
        scene.slot_ys, scene.table_colour, scene.hole_pitch = (), (150, 152, 155), 25.0
        return scene

    @staticmethod
    def demo(table_z=-240.0):
        """A vise near the left with a block of aluminium in it, and a tag on the fixed jaw"""
        stock_top = table_z + 45.0
        return SimScene(table_z, objects=[
            SimObject([(150, 50), (270, 50), (270, 60), (150, 60)], table_z + 50, (95, 98, 104), "vise fixed jaw"),
            SimObject([(150, 110), (270, 110), (270, 120), (150, 120)], table_z + 50, (95, 98, 104), "vise moving jaw"),
            SimObject([(172.3, 61.2), (248.5, 61.2), (248.5, 108.8), (172.3, 108.8)], stock_top, (205, 210, 215),
                      "aluminium block"),
        ], tags=[SimTag(7, (160.0, 40.0), 12.0, table_z + 1.0)])


def _tag_image(tag_id: int, pixels: int = 160) -> np.ndarray:
    import cv2
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_APRILTAG_36h11)
    inner = cv2.aruco.drawMarker(dictionary, tag_id, pixels) if hasattr(cv2.aruco, "drawMarker") \
        else cv2.aruco.generateImageMarker(dictionary, tag_id, pixels)
    border = pixels // 4
    return cv2.copyMakeBorder(inner, border, border, border, border, cv2.BORDER_CONSTANT, value=255)


class SimCamera(Camera):
    name = "Simulated camera"

    def __init__(self, model: CameraModel, position: Callable[[], Sequence[float]], scene: Optional[SimScene] = None,
                 noise: float = 3.0, seed: int = 1, mono: bool = False,
                 laser: Optional[Callable[[], bool]] = None, laser_baseline: float = 90.0,
                 laser_aim_z: float = 0.0):
        """mono: frames like this machine's monochrome camera (three equal channels).
        laser: says whether the line laser is on. It sits laser_baseline mm toward machine -Y
        from the camera, tilted to hit the table under the camera when the spindle is at
        laser_aim_z."""
        self.model = model
        self.position = position
        self.scene = scene or SimScene.demo(model.table_z)
        self.noise = noise
        self.rng = np.random.default_rng(seed)
        self.mono = mono
        self.laser = laser
        self.laser_baseline = laser_baseline
        self.laser_tan = laser_baseline / model.distance(laser_aim_z, self.scene.table_z)

    def laser_y(self, spindle, surface_z):
        """Machine Y where the laser line falls on a surface at tip Z surface_z"""
        camera_y = spindle[1] + self.model.offset_y
        return camera_y - self.laser_baseline + self.model.distance(spindle[2], surface_z) * self.laser_tan

    def _laser_mask(self, spindle, surface_z, polygon=None):
        import cv2
        m = self.model
        y = self.laser_y(spindle, surface_z)
        line = m.machine_to_pixel([(-1000.0, y), (2000.0, y)], spindle, surface_z, distort=False)
        mask = np.zeros((m.height, m.width), np.uint8)
        fixed = np.round(line * 16).astype(int)  # sub-pixel: cv2 shift=4
        cv2.line(mask, tuple(fixed[0]), tuple(fixed[1]), 255, 3, cv2.LINE_AA, shift=4)
        if polygon is not None:
            keep = np.zeros_like(mask)
            pts = m.machine_to_pixel(polygon, spindle, surface_z, distort=False)
            cv2.fillPoly(keep, [np.round(pts).astype(np.int32)], 255)
            mask = np.minimum(mask, keep)
        return mask

    def _add_laser(self, img, mask):
        boost = (mask.astype(np.float32) / 255.0 * 220.0)[..., None]
        if self.mono:
            colour = np.array([1.0, 1.0, 1.0], np.float32)
        else:
            colour = np.array([0.15, 0.15, 1.0], np.float32)  # BGR: red
        img[:] = np.clip(img.astype(np.float32) + boost * colour, 0, 255).astype(np.uint8)

    def grab(self):
        import cv2
        m = self.model
        spindle = tuple(self.position())[:3]
        img = np.full((m.height, m.width, 3), self.scene.table_colour, np.uint8)
        # T-slots: darker bands along X
        z = self.scene.table_z
        for y in self.scene.slot_ys:
            band = [(-1000, y - 7), (2000, y - 7), (2000, y + 7), (-1000, y + 7)]
            pts = m.machine_to_pixel(band, spindle, z, distort=False)
            cv2.fillPoly(img, [pts.astype(np.int32)], (22, 23, 26))
        if self.scene.hole_pitch:
            # the fixture plate's holes, in the part of the table this view sees
            px = np.array([[0, 0], [m.width - 1, 0], [m.width - 1, m.height - 1], [0, m.height - 1]], float)
            seen = m.pixel_to_machine(px, spindle, z, undistort=False)
            (x0, y0), (x1, y1) = seen.min(axis=0), seen.max(axis=0)
            pitch, radius_px = self.scene.hole_pitch, self.scene.hole_diameter / 2 / m.mm_per_pixel(spindle[2], z)
            for hx in np.arange(np.floor(x0 / pitch) * pitch, x1 + pitch, pitch):
                for hy in np.arange(np.floor(y0 / pitch) * pitch, y1 + pitch, pitch):
                    c = m.machine_to_pixel([(hx, hy)], spindle, z, distort=False)[0]
                    cv2.circle(img, (int(round(c[0])), int(round(c[1]))), int(round(radius_px)), (25, 26, 28), -1,
                               cv2.LINE_AA)
        laser_on = bool(self.laser and self.laser())
        if laser_on:
            self._add_laser(img, self._laser_mask(spindle, z))
        for obj in sorted(self.scene.objects, key=lambda o: o.top_z):
            pts = m.machine_to_pixel(obj.polygon, spindle, obj.top_z, distort=False)
            cv2.fillPoly(img, [np.round(pts).astype(np.int32)], obj.colour, lineType=cv2.LINE_AA)
            if laser_on:
                self._add_laser(img, self._laser_mask(spindle, obj.top_z, obj.polygon))
        for tag in self.scene.tags:
            image = _tag_image(tag.id)
            full = tag.size * image.shape[0] / (image.shape[0] * 2 / 3)  # printed square + white border
            half = full / 2
            cx, cy = tag.center
            quad = [(cx - half, cy + half), (cx + half, cy + half), (cx + half, cy - half), (cx - half, cy - half)]
            dst = m.machine_to_pixel(quad, spindle, tag.z, distort=False).astype(np.float32)
            src = np.float32([[0, 0], [image.shape[1], 0], [image.shape[1], image.shape[0]], [0, image.shape[0]]])
            warped = cv2.warpPerspective(cv2.cvtColor(image, cv2.COLOR_GRAY2BGR),
                                         cv2.getPerspectiveTransform(src, dst), (m.width, m.height),
                                         flags=cv2.INTER_LINEAR, borderValue=(0, 0, 0))
            mask = cv2.warpPerspective(np.full(image.shape, 255, np.uint8), cv2.getPerspectiveTransform(src, dst),
                                       (m.width, m.height))
            img[mask > 128] = warped[mask > 128]
        if self.noise:
            shape = img.shape[:2] if self.mono else img.shape
            noise = self.rng.normal(0, self.noise, shape).astype(np.int16)
            img = np.clip(img.astype(np.int16) + (noise[..., None] if self.mono else noise), 0, 255).astype(np.uint8)
        if self.mono:
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            img = cv2.merge([gray, gray, gray])  # as picamera2 delivers the mono sensor
        return img


def sim_empty_table(camera: SimCamera, views, model: CameraModel, without=("aluminium block",)):
    """An EmptyTable photographed instantly from the simulated camera's scene with the parts
    (`without`, by label) taken away: for previews and tests, where the sim machine can't
    clear its own table"""
    from dataclasses import replace
    from milo_vision.background import EmptyTable
    scene = replace(camera.scene, objects=[o for o in camera.scene.objects if o.label not in without])
    frames = []
    for view in views:
        shot = SimCamera(camera.model, lambda v=view: v, scene, noise=camera.noise, mono=camera.mono)
        frames.append((tuple(view), shot.grab()))
    return EmptyTable(model, frames)
