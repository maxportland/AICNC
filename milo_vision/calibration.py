"""
Calibration, in two parts.

1. Lens (intrinsics): photograph a printed ChArUco board from ~15 angles; OpenCV solves focal
   length, principal point and distortion. Done once per camera + lens + focus setting.

2. Placement (where the camera is relative to the spindle): stick an AprilTag on the table,
   touch its centre with a pointed tool to learn its machine X/Y (and the table's tip Z), then
   let the machine view the tag from a grid of spindle positions at two heights. The solver
   finds offset_x, offset_y, rotation and z_offset that map every observed tag centre onto the
   touched point. Its RMS error (mm) says how good the calibration is.
"""

import math
from dataclasses import dataclass, replace
from typing import List, Sequence, Tuple

import numpy as np

from milo_vision.geometry import CameraModel

ARUCO_DICT = "DICT_5X5_100"


# --- 1. lens ------------------------------------------------------------------------------------

def charuco_board(squares=(9, 6), square_mm=25.0, marker_mm=18.0):
    import cv2
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, ARUCO_DICT))
    if hasattr(cv2.aruco, "CharucoBoard_create"):  # OpenCV < 4.7
        return cv2.aruco.CharucoBoard_create(squares[0], squares[1], square_mm, marker_mm, dictionary), dictionary
    return cv2.aruco.CharucoBoard(squares, square_mm, marker_mm, dictionary), dictionary


def write_charuco_png(path, squares=(9, 6), square_mm=25.0, marker_mm=18.0, dpi=300):
    """A printable ChArUco board (print at 100% scale; check one square measures square_mm)"""
    import cv2
    board, _ = charuco_board(squares, square_mm, marker_mm)
    px = int(round(squares[0] * square_mm / 25.4 * dpi)), int(round(squares[1] * square_mm / 25.4 * dpi))
    image = board.draw(px) if hasattr(board, "draw") else board.generateImage(px)
    cv2.imwrite(path, image)
    return path


def calibrate_intrinsics(images: Sequence[np.ndarray], model: CameraModel, squares=(9, 6),
                         square_mm=25.0, marker_mm=18.0) -> Tuple[CameraModel, float]:
    """Solve the lens from ChArUco photos. Returns (updated model, reprojection error in px)."""
    import cv2
    board, dictionary = charuco_board(squares, square_mm, marker_mm)
    all_corners, all_ids, size = [], [], None
    for image in images:
        gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
        size = gray.shape[::-1]
        corners, ids, _ = cv2.aruco.detectMarkers(gray, dictionary)
        if ids is None or len(ids) < 4:
            continue
        count, ch_corners, ch_ids = cv2.aruco.interpolateCornersCharuco(corners, ids, gray, board)
        if count and count >= 6:
            all_corners.append(ch_corners)
            all_ids.append(ch_ids)
    if len(all_corners) < 5:
        raise ValueError(f"Only {len(all_corners)} usable board photos; take at least 5 (15 is better)")
    error, matrix, dist, _, _ = cv2.aruco.calibrateCameraCharuco(all_corners, all_ids, board, size, None, None)
    updated = replace(model, width=size[0], height=size[1], fx=float(matrix[0, 0]), fy=float(matrix[1, 1]),
                      cx=float(matrix[0, 2]), cy=float(matrix[1, 2]), dist=[float(v) for v in dist.ravel()[:5]])
    return updated, float(error)


# --- 2. placement -------------------------------------------------------------------------------

@dataclass
class Observation:
    """The tag (whose centre is at target_xy on the plane at plane_z) seen at pixel from spindle"""
    spindle: Tuple[float, float, float]
    pixel: Tuple[float, float]
    target_xy: Tuple[float, float]
    plane_z: float


def _residuals(model: CameraModel, observations: List[Observation]) -> np.ndarray:
    out = []
    for obs in observations:
        xy = model.pixel_to_machine([obs.pixel], obs.spindle, obs.plane_z)[0]
        out.extend(xy - np.asarray(obs.target_xy))
    return np.asarray(out)


def _with(model, params):
    return replace(model, offset_x=params[0], offset_y=params[1], rotation=params[2], z_offset=params[3])


def solve_placement(model: CameraModel, observations: List[Observation], iterations=50) -> Tuple[CameraModel, float]:
    """
    Least-squares fit of offset_x, offset_y, rotation and z_offset (Gauss-Newton; the mirror
    flip is tried both ways). Returns (calibrated model, RMS error in mm).
    Needs at least 3 observations; views at two different heights pin down z_offset.
    """
    if len(observations) < 3:
        raise ValueError("Need at least 3 views of the calibration tag")
    best = None
    for flip in (1.0, -1.0):
        start = replace(model, flip=flip)
        params = np.array([start.offset_x, start.offset_y, start.rotation, start.z_offset], float)
        for guess_rot in (params[2], params[2] + 90, params[2] + 180, params[2] - 90):
            p = params.copy()
            p[2] = guess_rot
            p = _gauss_newton(start, observations, p, iterations)
            if p is None:
                continue
            fitted = _with(start, p)
            rms = float(np.sqrt(np.mean(_residuals(fitted, observations) ** 2)))
            if best is None or rms < best[1]:
                best = (fitted, rms)
    if best is None:
        raise ValueError("Calibration did not converge; check the tag positions and the touched point")
    fitted, rms = best
    fitted = replace(fitted, rotation=((fitted.rotation + 180) % 360) - 180, calibrated=True)
    return fitted, rms


def _gauss_newton(model, observations, params, iterations):
    steps = np.array([1e-3, 1e-3, 1e-4, 1e-3])
    for _ in range(iterations):
        try:
            r = _residuals(_with(model, params), observations)
            jac = np.zeros((r.size, 4))
            for j in range(4):
                shifted = params.copy()
                shifted[j] += steps[j]
                jac[:, j] = (_residuals(_with(model, shifted), observations) - r) / steps[j]
            delta, *_ = np.linalg.lstsq(jac, -r, rcond=None)
        except (ValueError, np.linalg.LinAlgError):
            return None
        params = params + delta
        if np.all(np.abs(delta) < 1e-7):
            break
    return params if np.all(np.isfinite(params)) else None


def placement_views(tag_xy: Tuple[float, float], model: CameraModel, heights: Sequence[float],
                    spread_mm: float = 25.0) -> List[Tuple[float, float, float]]:
    """Spindle positions for the placement wizard: a 3x3 grid around the tag at each height,
    so the tag lands near the centre, edges and corners of the image"""
    cx, cy = model.centre_over(*tag_xy)
    views = []
    for z in heights:
        for dy in (-spread_mm, 0.0, spread_mm):
            for dx in (-spread_mm, 0.0, spread_mm):
                views.append((cx + dx, cy + dy, z))
    return views
