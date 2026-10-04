"""
Scanning the table: move the camera over it in a serpentine, then stitch what each frame saw
into one map in machine coordinates.

TableMap collects, per frame: stock outlines (mapped onto the table plane), tag centres, and
the frame itself warped into an orthographic mosaic. finish() merges outlines seen in several
frames into parts, estimates each part's top height from parallax between two views, and
reports each part as a rotated rectangle.

Parallax: a surface above the table appears to move against the table as the camera moves.
Mapping the same point from two spindle positions onto a trial plane only agrees when the
trial plane is at the point's real height, which gives the height to about +-1 mm (the laser
does better).
"""

import math
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from milo_vision.geometry import CameraModel
from milo_vision import detect

TAG_QUIET_ZONE = 1.6  # tag outline scaled by this to also mask its white border


@dataclass
class Part:
    center: Tuple[float, float]         # machine XY
    size: Tuple[float, float]           # along its own axes, long side first (mm)
    angle: float                        # degrees of the long side from machine +X
    corners: List[Tuple[float, float]]  # machine XY
    top_z: Optional[float] = None       # tip Z of the top, when parallax could tell
    top_z_error: Optional[float] = None
    frames: int = 0                     # how many frames saw it

    def to_dict(self):
        return asdict(self)


@dataclass
class ScanResult:
    parts: List[Part]
    tags: Dict[int, Tuple[float, float]]
    table_z: float
    frames: int
    finished_at: float = field(default_factory=time.time)
    mosaic: Optional[np.ndarray] = None
    mosaic_origin: Tuple[float, float] = (0.0, 0.0)  # machine XY of the mosaic's pixel (0, bottom)
    mosaic_px_per_mm: float = 2.0


def plan_scan(limits: Dict[str, Tuple[float, float]], model: CameraModel, scan_z: float,
              overlap: float = 0.3, margin: float = 0.0) -> List[Tuple[float, float, float]]:
    """
    Spindle positions that sweep the camera over the whole reachable table, serpentine order,
    with the given frame overlap. Positions are clamped to the soft limits.
    """
    fov_w, fov_h = model.field_of_view(scan_z)
    step_x, step_y = fov_w * (1 - overlap), fov_h * (1 - overlap)
    (x0, x1), (y0, y1) = limits["X"], limits["Y"]
    # the camera sees the table at spindle + offset: cover the camera's reachable area
    cam_x0, cam_x1 = x0 + model.offset_x + margin, x1 + model.offset_x - margin
    cam_y0, cam_y1 = y0 + model.offset_y + margin, y1 + model.offset_y - margin
    cols = max(1, math.ceil((cam_x1 - cam_x0 - fov_w) / step_x) + 1)
    rows = max(1, math.ceil((cam_y1 - cam_y0 - fov_h) / step_y) + 1)
    xs = np.linspace(cam_x0 + fov_w / 2, max(cam_x0 + fov_w / 2, cam_x1 - fov_w / 2), cols)
    ys = np.linspace(cam_y0 + fov_h / 2, max(cam_y0 + fov_h / 2, cam_y1 - fov_h / 2), rows)
    views = []
    for r, cy in enumerate(ys):
        for cx in (xs if r % 2 == 0 else xs[::-1]):
            sx, sy = model.centre_over(cx, cy)
            views.append((float(np.clip(sx, x0, x1)), float(np.clip(sy, y0, y1)), scan_z))
    return views


def refine_views(part_center: Tuple[float, float], model: CameraModel, scan_z: float,
                 limits: Dict[str, Tuple[float, float]], baseline: float = 60.0) -> List[Tuple[float, float, float]]:
    """Two spindle positions that see a part whole from either side, for its height by parallax.
    A wider baseline measures height better; it's limited by keeping the part in view."""
    sx, sy = model.centre_over(*part_center)
    views = []
    for dx in (-baseline / 2, baseline / 2):
        x = float(np.clip(sx + dx, *limits["X"]))
        y = float(np.clip(sy, *limits["Y"]))
        views.append((x, y, scan_z))
    return views


def parallax_height(model: CameraModel, pixel_a, spindle_a, pixel_b, spindle_b,
                    z_range=(-300.0, 50.0)) -> Optional[float]:
    """Tip-Z height of a point seen at pixel_a from spindle_a and pixel_b from spindle_b"""
    def gap(z):
        a = model.pixel_to_machine([pixel_a], spindle_a, z)[0]
        b = model.pixel_to_machine([pixel_b], spindle_b, z)[0]
        return a - b
    # the gap is linear in z; solve from two samples
    lo, hi = model.table_z, model.table_z + 50.0
    g0, g1 = gap(lo), gap(hi)
    slope = (g1 - g0) / (hi - lo)
    denom = float(slope @ slope)
    if denom < 1e-12:
        return None  # the views are too close together to tell
    z = lo - float(g0 @ slope) / denom
    return z if z_range[0] <= z <= z_range[1] else None


class TableMap:
    def __init__(self, model: CameraModel, limits: Dict[str, Tuple[float, float]], px_per_mm: float = 2.0,
                 empty_table=None):
        """empty_table: a background.EmptyTable; parts are then what changed from it"""
        self.model = model
        self.empty_table = empty_table
        self.found = None        # parts' outlines (shapely) after a finish(): later frames only add to them
        self.limits = limits
        self.px_per_mm = px_per_mm
        self.polygons = []       # (machine polygon ndarray, frame index)
        self.observations = []   # (frame index, pixel polygon, spindle, touches_border)
        self.tag_sightings: Dict[int, List[Tuple[float, float]]] = {}
        self.frames = 0
        # mosaic covers everything the camera can see
        (x0, x1), (y0, y1) = limits["X"], limits["Y"]
        fov_w, fov_h = model.field_of_view(0.0)
        self.origin = (x0 + model.offset_x - fov_w, y0 + model.offset_y - fov_h)
        width_mm = (x1 - x0) + 2 * fov_w
        height_mm = (y1 - y0) + 2 * fov_h
        self.mosaic = np.zeros((int(height_mm * px_per_mm), int(width_mm * px_per_mm), 3), np.uint8)

    def _to_mosaic(self, xy):
        xy = np.asarray(xy, float).reshape(-1, 2)
        return np.stack([(xy[:, 0] - self.origin[0]) * self.px_per_mm,
                         self.mosaic.shape[0] - (xy[:, 1] - self.origin[1]) * self.px_per_mm], axis=1)

    def add_frame(self, image: np.ndarray, spindle: Tuple[float, float, float]):
        import cv2
        index = self.frames
        self.frames += 1
        m = self.model
        tags = detect.find_tags(image)
        masks = []
        for tag in tags:
            c = np.asarray(tag.center)
            masks.append(c + (tag.corners - c) * TAG_QUIET_ZONE)
            xy = m.pixel_to_machine([tag.center], spindle)[0]
            self.tag_sightings.setdefault(tag.id, []).append((float(xy[0]), float(xy[1])))
        empty = self.empty_table.view(spindle, image.shape) if self.empty_table is not None else None
        if empty is not None:
            blobs = detect.find_stock(image, exclude=masks, background=empty.background,
                                      tolerance=empty.tolerance, valid=empty.valid)
        elif self.empty_table is not None:
            blobs = []  # a spot the empty-table photos never covered: brightness would find the table
        else:
            blobs = detect.find_stock(image, exclude=masks)
        for blob in blobs:
            poly = m.pixel_to_machine(blob.polygon, spindle)
            if self.found is not None and not self._near_found(poly):
                continue  # a closer look: it may only add to a part the scan already found
            self.polygons.append((poly, index))
            self.observations.append((index, blob.polygon, spindle, blob.touches_border))
        # mosaic: warp the frame onto the table plane
        h, w = image.shape[:2]
        corners_px = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], float)
        target = self._to_mosaic(m.pixel_to_machine(corners_px, spindle)).astype(np.float32)
        H = cv2.getPerspectiveTransform(corners_px.astype(np.float32), target)
        size = (self.mosaic.shape[1], self.mosaic.shape[0])
        frame = image if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        warped = cv2.warpPerspective(frame, H, size)
        mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), H, size)
        self.mosaic[mask > 0] = warped[mask > 0]

    def _near_found(self, poly) -> bool:
        from shapely.geometry import Polygon
        try:
            shape = Polygon(poly).buffer(0)
        except ValueError:
            return False
        return any(shape.intersects(piece) for piece in self.found)

    def finish(self) -> ScanResult:
        from shapely.geometry import Polygon
        from shapely.ops import unary_union
        shapes = []
        for poly, index in self.polygons:
            try:
                shape = Polygon(poly).buffer(0)
            except ValueError:
                continue
            if shape.area > 1.0:
                shapes.append((shape.buffer(1.0), index))
        merged = unary_union([s for s, _ in shapes]) if shapes else None
        pieces = [] if merged is None else (list(merged.geoms) if hasattr(merged, "geoms") else [merged])
        parts, found = [], []
        for piece in pieces:
            piece = piece.buffer(-1.0)
            if piece.is_empty or piece.area < 25.0:
                continue
            top, error = self._part_height(piece)
            # Seen from above, a raised part projected onto the table plane looks too big:
            # project its outlines again at its own top height before measuring it
            plane = top if top is not None else self.model.table_z
            outlines = []
            seen = set()
            # self.polygons and self.observations are appended together, one entry per outline
            for (poly_table, index), (_, poly_px, spindle, _t) in zip(self.polygons, self.observations):
                try:
                    if Polygon(poly_table).buffer(0).intersects(piece):
                        seen.add(index)
                        shape = Polygon(self.model.pixel_to_machine(poly_px, spindle, plane)).buffer(0)
                        outlines.append(shape.buffer(1.0))
                except ValueError:
                    continue
            shape = unary_union(outlines).buffer(-1.0) if outlines else piece
            if hasattr(shape, "geoms"):
                shape = max(shape.geoms, key=lambda g: g.area)
            rect = shape.minimum_rotated_rectangle
            corners = list(rect.exterior.coords)[:4]
            edges = [np.subtract(corners[(i + 1) % 4], corners[i]) for i in range(2)]
            lengths = [float(np.hypot(*e)) for e in edges]
            long_edge = edges[int(np.argmax(lengths))]
            angle = math.degrees(math.atan2(long_edge[1], long_edge[0]))
            angle = ((angle + 90) % 180) - 90
            found.append(piece.buffer(2.0))
            parts.append(Part(center=(float(rect.centroid.x), float(rect.centroid.y)),
                              size=(max(lengths), min(lengths)), angle=angle,
                              corners=[(float(x), float(y)) for x, y in corners],
                              top_z=top, top_z_error=error, frames=len(seen)))
        if self.found is None:
            self.found = found
        tags = {tag_id: tuple(np.mean(points, axis=0).tolist()) for tag_id, points in self.tag_sightings.items()}
        mosaic, origin = self._cropped_mosaic()
        return ScanResult(parts=sorted(parts, key=lambda p: -p.size[0] * p.size[1]), tags=tags,
                          table_z=self.model.table_z, frames=self.frames, mosaic=mosaic,
                          mosaic_origin=origin, mosaic_px_per_mm=self.px_per_mm)

    def _cropped_mosaic(self):
        """The mosaic cut down to what was actually scanned; origin is the machine XY of its
        bottom-left pixel"""
        filled = np.argwhere(self.mosaic.any(axis=2))
        if not filled.size:
            return self.mosaic, self.origin
        (r0, c0), (r1, c1) = filled.min(axis=0), filled.max(axis=0) + 1
        crop = self.mosaic[r0:r1, c0:c1].copy()
        origin = (self.origin[0] + c0 / self.px_per_mm,
                  self.origin[1] + (self.mosaic.shape[0] - r1) / self.px_per_mm)
        return crop, origin

    def _part_height(self, piece) -> Tuple[Optional[float], Optional[float]]:
        """Top height from parallax: pairs of frames that each saw the whole part"""
        from shapely.geometry import Point
        whole = []
        for index, poly_px, spindle, touches in self.observations:
            if touches:
                continue
            centroid_px = poly_px.mean(axis=0)
            if piece.buffer(10).contains(Point(*self.model.pixel_to_machine([centroid_px], spindle)[0])):
                whole.append((centroid_px, spindle))
        heights = []
        for i in range(len(whole)):
            for j in range(i + 1, len(whole)):
                (pa, sa), (pb, sb) = whole[i], whole[j]
                if math.hypot(sa[0] - sb[0], sa[1] - sb[1]) < 10:
                    continue
                z = parallax_height(self.model, pa, sa, pb, sb)
                if z is not None:
                    heights.append(z)
        if not heights:
            return None, None
        return float(np.median(heights)), float(np.std(heights)) if len(heights) > 1 else None
