"""
Finding things in one camera frame (pixel coordinates; scan.py maps them to the machine).

Tags   AprilTag 36h11 markers: printed fiducials on the vise, fixtures or the table. Their
       centres are found to a fraction of a pixel, so they are the most reliable reference.
Stock  what's on the table. With a photo of the empty table (background.py) it's whatever changed
       from that photo, so it works on any table, bright or holed; without one, it's material
       brighter than a dark cast-iron table (thresholding). Each outline is a polygon. Both need
       good, even lighting (see VISION_HARDWARE.md).
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np

TAG_FAMILY = "DICT_APRILTAG_36h11"


@dataclass
class Tag:
    id: int
    center: Tuple[float, float]
    corners: np.ndarray  # 4x2 pixels, clockwise from top-left of the printed tag


@dataclass
class Blob:
    polygon: np.ndarray       # Nx2 pixels, the outline
    area: float               # pixels^2
    touches_border: bool      # cut off by the image edge (only part of it is visible)


def _gray(image):
    import cv2
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image


def find_tags(image) -> List[Tag]:
    import cv2
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, TAG_FAMILY))
    if hasattr(cv2.aruco, "ArucoDetector"):
        corners, ids, _ = cv2.aruco.ArucoDetector(dictionary).detectMarkers(_gray(image))
    else:
        params = cv2.aruco.DetectorParameters_create()
        params.cornerRefinementMethod = cv2.aruco.CORNER_REFINE_SUBPIX
        corners, ids, _ = cv2.aruco.detectMarkers(_gray(image), dictionary, parameters=params)
    tags = []
    if ids is None:
        return tags
    for quad, tag_id in zip(corners, ids.ravel()):
        pts = quad.reshape(4, 2).astype(float)
        tags.append(Tag(int(tag_id), (float(pts[:, 0].mean()), float(pts[:, 1].mean())), pts))
    return tags


def find_stock(image, min_area_fraction=0.002, exclude: Optional[List[np.ndarray]] = None,
               background: Optional[np.ndarray] = None, tolerance: int = 2, min_change: float = 30.0,
               valid: Optional[np.ndarray] = None) -> List[Blob]:
    """
    What's on the table. exclude: pixel polygons to ignore (e.g. tags).
    background: the empty table from the same view. A pixel has changed when it's brighter or
    darker than every background pixel within `tolerance` pixels by min_change, so the edges of
    the table's holes don't count when the views line up a pixel or two apart. valid: where the
    background is known (elsewhere nothing is reported). Without a background: bright regions on
    a dark table. Returns outlines, largest first.
    """
    import cv2
    gray = cv2.GaussianBlur(_gray(image), (7, 7), 0)
    if background is not None:
        bg = cv2.GaussianBlur(_gray(background), (7, 7), 0).astype(np.int16)
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * tolerance + 1, 2 * tolerance + 1))
        hi, lo = cv2.dilate(bg, k), cv2.erode(bg, k)
        g = gray.astype(np.int16)
        mask = (((g > hi + min_change) | (g < lo - min_change)) * 255).astype(np.uint8)
        if valid is not None:
            mask[valid == 0] = 0
        # a part over plain table may only differ at its edges and over the holes: fill outlines
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(mask, contours, -1, 255, cv2.FILLED)
    else:
        _, mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        # An almost empty frame makes Otsu split the table's own texture: require real contrast
        if gray[mask > 0].size and gray[mask == 0].size:
            if float(gray[mask > 0].mean()) - float(gray[mask == 0].mean()) < 40:
                return []
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (9, 9))
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
    for poly in exclude or []:
        cv2.fillPoly(mask, [np.asarray(poly, np.int32).reshape(-1, 1, 2)], 0)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = mask.shape
    min_area = min_area_fraction * w * h
    blobs = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        approx = cv2.approxPolyDP(contour, 0.004 * cv2.arcLength(contour, True), True).reshape(-1, 2).astype(float)
        x, y, bw, bh = cv2.boundingRect(contour)
        touches = x <= 2 or y <= 2 or x + bw >= w - 2 or y + bh >= h - 2
        blobs.append(Blob(approx, float(area), touches))
    return sorted(blobs, key=lambda b: -b.area)
