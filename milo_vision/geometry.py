"""
The camera model: a pinhole camera fixed to the spindle head, looking straight down.

Frames:
  machine   LinuxCNC machine coordinates of the spindle (G53), in mm
  tip Z     heights are "tool tip machine Z": what G53 Z reads with the tip touching a surface
            (machine Z minus the active tool length offset). The table map, stock tops and
            obstacle heights all use this, so they compare directly with a tool's tip.
  image     pixels, u to the right, v down

For a camera at spindle position (mx, my, mz) looking at a plane at tip-Z height h:

    distance d = (mz + z_offset) - h            camera optical centre above the plane
    dx, dy     = (u - cx) * d / fx, (v - cy) * d / fy      offset in the image's own axes (mm)
    X, Y       = (mx, my) + (offset_x, offset_y) + R(rotation) * (dx, -dy * flip)

offset_x/y is where the camera's optical axis is relative to the spindle axis, rotation is
the angle of the image's +u axis from machine +X, and z_offset is how far the camera centre
sits above the spindle's controlled point. Lens distortion is removed first (cv2 model).
"""

import json
import math
import os
from dataclasses import dataclass, asdict, field
from typing import List, Optional, Sequence, Tuple

import numpy as np

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VISION_DIR = os.path.join(CONFIG_DIR, "vision")
CAMERA_FILE = os.path.join(VISION_DIR, "camera.json")


@dataclass
class CameraModel:
    width: int = 1456             # image size the calibration was done at
    height: int = 1088
    fx: float = 1740.0            # focal length in pixels (IMX296 + 6 mm lens)
    fy: float = 1740.0
    cx: float = 728.0             # principal point
    cy: float = 544.0
    dist: List[float] = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0, 0.0])  # k1 k2 p1 p2 k3
    offset_x: float = 60.0        # optical axis relative to spindle axis, machine mm
    offset_y: float = 0.0
    rotation: float = 0.0         # degrees, image +u relative to machine +X
    flip: float = 1.0             # 1: image up is machine +Y at rotation 0; -1 if mirrored
    z_offset: float = 120.0       # camera centre above the spindle's controlled point (mm)
    table_z: float = -240.0       # tip Z of the machine table surface
    calibrated: bool = False      # False: placeholder values, measurements are not trusted

    # --- persistence ------------------------------------------------------------------------

    def save(self, path: str = CAMERA_FILE):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)

    @staticmethod
    def load(path: str = CAMERA_FILE) -> "CameraModel":
        try:
            with open(path) as f:
                data = json.load(f)
            known = {k: v for k, v in data.items() if k in CameraModel.__dataclass_fields__}
            return CameraModel(**known)
        except (OSError, ValueError, TypeError):
            return CameraModel()

    # --- geometry -------------------------------------------------------------------------------

    def camera_matrix(self) -> np.ndarray:
        return np.array([[self.fx, 0, self.cx], [0, self.fy, self.cy], [0, 0, 1]], dtype=float)

    def distance(self, machine_z: float, plane_z: float) -> float:
        """Camera centre height above a plane (mm); must be positive"""
        return machine_z + self.z_offset - plane_z

    def undistort_points(self, pixels: Sequence[Tuple[float, float]]) -> np.ndarray:
        pts = np.asarray(pixels, dtype=float).reshape(-1, 2)
        if not any(self.dist):
            return pts
        import cv2
        out = cv2.undistortPoints(pts.reshape(-1, 1, 2), self.camera_matrix(), np.asarray(self.dist, float),
                                  P=self.camera_matrix())
        return out.reshape(-1, 2)

    def _rot(self):
        a = math.radians(self.rotation)
        return np.array([[math.cos(a), -math.sin(a)], [math.sin(a), math.cos(a)]])

    def pixel_to_machine(self, pixels, spindle: Tuple[float, float, float], plane_z: Optional[float] = None,
                         undistort=True) -> np.ndarray:
        """Pixels -> machine XY on the plane at tip-Z plane_z (default: the table)"""
        plane = self.table_z if plane_z is None else plane_z
        d = self.distance(spindle[2], plane)
        if d <= 0:
            raise ValueError("the camera is below the plane it is looking at")
        pts = self.undistort_points(pixels) if undistort else np.asarray(pixels, float).reshape(-1, 2)
        local = np.stack([(pts[:, 0] - self.cx) * d / self.fx, -(pts[:, 1] - self.cy) * d / self.fy * self.flip], axis=1)
        world = local @ self._rot().T
        return world + np.array([spindle[0] + self.offset_x, spindle[1] + self.offset_y])

    def machine_to_pixel(self, points, spindle: Tuple[float, float, float], plane_z: Optional[float] = None,
                         distort=True) -> np.ndarray:
        """Machine XY on a plane -> pixels (inverse of pixel_to_machine)"""
        plane = self.table_z if plane_z is None else plane_z
        d = self.distance(spindle[2], plane)
        pts = np.asarray(points, float).reshape(-1, 2) - np.array([spindle[0] + self.offset_x, spindle[1] + self.offset_y])
        local = pts @ self._rot()
        u = local[:, 0] * self.fx / d + self.cx
        v = -local[:, 1] / self.flip * self.fy / d + self.cy
        pix = np.stack([u, v], axis=1)
        if distort and any(self.dist):
            import cv2
            norm = np.stack([(u - self.cx) / self.fx, (v - self.cy) / self.fy, np.ones_like(u)], axis=1)
            projected, _ = cv2.projectPoints(norm.reshape(-1, 1, 3), np.zeros(3), np.zeros(3),
                                             self.camera_matrix(), np.asarray(self.dist, float))
            pix = projected.reshape(-1, 2)
        return pix

    def mm_per_pixel(self, machine_z: float, plane_z: Optional[float] = None) -> float:
        plane = self.table_z if plane_z is None else plane_z
        return self.distance(machine_z, plane) / self.fx

    def field_of_view(self, machine_z: float, plane_z: Optional[float] = None) -> Tuple[float, float]:
        """Width and height (mm) the camera sees on a plane"""
        s = self.mm_per_pixel(machine_z, plane_z)
        return self.width * s, self.height * s

    def centre_over(self, x: float, y: float) -> Tuple[float, float]:
        """Spindle XY that puts the camera's optical axis over machine point (x, y)"""
        return x - self.offset_x, y - self.offset_y
