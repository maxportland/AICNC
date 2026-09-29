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
                 noise: float = 3.0, seed: int = 1):
        self.model = model
        self.position = position
        self.scene = scene or SimScene.demo(model.table_z)
        self.noise = noise
        self.rng = np.random.default_rng(seed)

    def grab(self):
        import cv2
        m = self.model
        spindle = tuple(self.position())[:3]
        img = np.full((m.height, m.width, 3), (38, 40, 44), np.uint8)
        # T-slots: darker bands along X
        z = self.scene.table_z
        for y in self.scene.slot_ys:
            band = [(-1000, y - 7), (2000, y - 7), (2000, y + 7), (-1000, y + 7)]
            pts = m.machine_to_pixel(band, spindle, z, distort=False)
            cv2.fillPoly(img, [pts.astype(np.int32)], (22, 23, 26))
        for obj in sorted(self.scene.objects, key=lambda o: o.top_z):
            pts = m.machine_to_pixel(obj.polygon, spindle, obj.top_z, distort=False)
            cv2.fillPoly(img, [np.round(pts).astype(np.int32)], obj.colour, lineType=cv2.LINE_AA)
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
            img = np.clip(img.astype(np.int16) + self.rng.normal(0, self.noise, img.shape).astype(np.int16),
                          0, 255).astype(np.uint8)
        return img
