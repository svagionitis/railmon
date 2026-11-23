"""Geospatial projection engine and Braille sub-pixel raster canvas.

Provides mathematical transformations between WGS84 geographic coordinates
and terminal screen space with font aspect ratio compensation, alongside
a 2x4 sub-pixel Braille rasterizer for rendering railway tracks.
"""

import json
import locale
import math
import os
import sys
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Union


# Base Unicode code point for Braille Patterns block (U+2800)
BRAILLE_BASE = 0x2800

# Sub-pixel to Braille bitmask lookup table:
# Column 0: sub_x = 0; Column 1: sub_x = 1
# Rows 0..3: sub_y in 0..3
DOT_MASKS: Tuple[Tuple[int, ...], Tuple[int, ...]] = (
    (0x01, 0x02, 0x04, 0x40),  # col 0: dots 1, 2, 3, 7
    (0x08, 0x10, 0x20, 0x80),  # col 1: dots 4, 5, 6, 8
)


def _generate_ascii_braille_table() -> Tuple[str, ...]:
    """Precompute 256-element lookup table mapping 2x4 subpixel bitmasks to ASCII characters."""
    table: List[str] = []
    for mask in range(256):
        if mask == 0:
            table.append(" ")
            continue
        c0 = [bool(mask & DOT_MASKS[0][r]) for r in range(4)]
        c1 = [bool(mask & DOT_MASKS[1][r]) for r in range(4)]
        total = sum(c0) + sum(c1)
        if total == 1:
            table.append(".")
        elif any(c0) and not any(c1):
            table.append("|")
        elif any(c1) and not any(c0):
            table.append("|")
        else:
            horiz = sum(1 for r in range(4) if c0[r] and c1[r])
            if horiz > 0 and total <= 3:
                table.append("-")
            elif (c0[0] or c0[1]) and (c1[2] or c1[3]) and not ((c0[2] or c0[3]) and (c1[0] or c1[1])) and total <= 3:
                table.append("\\")
            elif (c0[2] or c0[3]) and (c1[0] or c1[1]) and not ((c0[0] or c0[1]) and (c1[2] or c1[3])) and total <= 3:
                table.append("/")
            elif total >= 3:
                table.append("+")
            else:
                table.append("-")
    return tuple(table)


ASCII_BRAILLE_TABLE: Tuple[str, ...] = _generate_ascii_braille_table()


def is_utf8_supported(stream: Optional[Any] = None) -> bool:
    """Determine whether the output stream and runtime environment support UTF-8 encoding.

    Inspects stream.encoding (defaulting to sys.stdout), locale.getpreferredencoding(),
    and validates actual encoding capabilities by attempting to encode representative
    Unicode Braille and directional glyphs.

    Args:
        stream: Optional stream object to probe. Defaults to sys.stdout.

    Returns:
        bool: True if the environment safely encodes UTF-8 characters;
              False if restricted to 7-bit ASCII, legacy code pages, or encoding fails.
    """
    if os.environ.get("RAILMON_ASCII", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if os.environ.get("TERM", "").strip().lower() in {"ascii"}:
        return False

    target_stream = stream if stream is not None else sys.stdout
    encoding = getattr(target_stream, "encoding", None)
    if not encoding:
        try:
            encoding = locale.getpreferredencoding(False)
        except Exception:
            encoding = "ascii"
    if not encoding:
        return False

    norm_enc = encoding.lower().replace("-", "").replace("_", "")
    if "utf8" not in norm_enc:
        return False

    test_chars = "\u2801\u25c9\u25b2\u00b7\u2500"
    try:
        test_chars.encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError, ValueError):
        return False


@dataclass(frozen=True)
class GeoPoint:
    """Geographic coordinate in decimal degrees (WGS84).

    Attributes:
        lat: Latitude in decimal degrees (e.g. 37.9838 for Athens).
        lng: Longitude in decimal degrees (e.g. 23.7275 for Athens).
    """
    lat: float
    lng: float


# Mean spherical Earth radius in kilometers (WGS84)
EARTH_RADIUS_KM = 6371.0
EARTH_RADIUS_M = 6371000.0

# Standard meridional kilometers per degree of latitude
KM_PER_DEGREE_LAT: float = 111.32

# Standard cartographic scale bar distance intervals in kilometers.
# Spaced with Voronoï cell ratios < 1.25x so every +/- zoom step (1.25x)
# transitions to a distinct cartographic interval across 0.2x..25.0x.
SCALE_BAR_INTERVALS_KM: Tuple[int, ...] = (
    1,
    2,
    3,
    4,
    5,
    6,
    7,
    8,
    10,
    12,
    15,
    18,
    20,
    25,
    30,
    35,
    40,
    50,
    60,
    75,
    90,
    100,
    120,
    150,
    180,
    200,
    250,
    300,
    350,
    400,
    500,
    600,
    750,
    900,
    1000,
    1200,
    1500,
)


@dataclass(frozen=True)
class ScaleBarSpec:
    """Computed conformal geographic scale bar parameters for the current viewport.

    Attributes:
        distance_km: Snapped cartographic interval in kilometers (from SCALE_BAR_INTERVALS_KM).
        width_cells: Total terminal character cell width of the scale bar (clamped to min_cells..max_cells).
        km_per_cell: Conformal ground distance in kilometers per terminal column cell at viewport center.
    """

    distance_km: int
    width_cells: int
    km_per_cell: float


def haversine_distance_km(a: GeoPoint, b: GeoPoint) -> float:
    """Calculate the great-circle distance between two WGS84 GeoPoints in kilometers.

    Uses the Haversine formula with mean spherical Earth radius R = 6371.0 km.

    Args:
        a: Origin geographic coordinate (lat, lng).
        b: Destination geographic coordinate (lat, lng).

    Returns:
        Great-circle distance in kilometers (non-negative float).
    """
    phi1 = math.radians(a.lat)
    phi2 = math.radians(b.lat)
    delta_phi = math.radians(b.lat - a.lat)
    delta_lambda = math.radians(b.lng - a.lng)

    sin_dphi = math.sin(delta_phi / 2.0)
    sin_dlng = math.sin(delta_lambda / 2.0)
    h = sin_dphi * sin_dphi + math.cos(phi1) * math.cos(phi2) * sin_dlng * sin_dlng
    h = min(1.0, max(0.0, h))
    return 2.0 * EARTH_RADIUS_KM * math.atan2(math.sqrt(h), math.sqrt(1.0 - h))


def project_point_to_segment(
    point: GeoPoint, p0: GeoPoint, p1: GeoPoint
) -> Tuple[GeoPoint, float]:
    """Orthogonally project a geographic point onto a line segment [p0, p1].

    Uses a local Equirectangular tangent-plane approximation scaled at the
    segment's mean latitude to compute the clamped projection parameter
    t in [0.0, 1.0], the interpolated GeoPoint on [p0, p1], and the
    perpendicular distance in meters.

    Args:
        point: Query geographic coordinate (lat, lng).
        p0: Segment start coordinate (lat, lng).
        p1: Segment end coordinate (lat, lng).

    Returns:
        Tuple of (snapped_geopoint, perpendicular_distance_m).
    """
    mean_lat_rad = math.radians((p0.lat + p1.lat) * 0.5)
    m_per_deg_lat = (math.pi / 180.0) * EARTH_RADIUS_M
    m_per_deg_lng = m_per_deg_lat * math.cos(mean_lat_rad)

    ux = (p1.lng - p0.lng) * m_per_deg_lng
    uy = (p1.lat - p0.lat) * m_per_deg_lat

    vx = (point.lng - p0.lng) * m_per_deg_lng
    vy = (point.lat - p0.lat) * m_per_deg_lat

    seg_len_sq = ux * ux + uy * uy
    if seg_len_sq <= 1e-12:
        return p0, math.hypot(vx, vy)

    t = (vx * ux + vy * uy) / seg_len_sq
    t = max(0.0, min(1.0, t))

    snapped_lat = p0.lat + t * (p1.lat - p0.lat)
    snapped_lng = p0.lng + t * (p1.lng - p0.lng)

    dx = vx - t * ux
    dy = vy - t * uy
    perp_dist_m = math.hypot(dx, dy)

    return GeoPoint(lat=snapped_lat, lng=snapped_lng), perp_dist_m


@dataclass(frozen=True)
class BoundingBox:
    """Geographic rectangular bounding box.

    Attributes:
        min_lat: Southernmost latitude in degrees.
        max_lat: Northernmost latitude in degrees.
        min_lng: Westernmost longitude in degrees.
        max_lng: Easternmost longitude in degrees.
    """
    min_lat: float
    max_lat: float
    min_lng: float
    max_lng: float

    @property
    def center(self) -> GeoPoint:
        """Calculate the geographic center point of the bounding box."""
        return GeoPoint(
            lat=(self.min_lat + self.max_lat) / 2.0,
            lng=(self.min_lng + self.max_lng) / 2.0,
        )

    @property
    def span_lat(self) -> float:
        """Total latitude span in degrees."""
        return self.max_lat - self.min_lat

    @property
    def span_lng(self) -> float:
        """Total longitude span in degrees."""
        return self.max_lng - self.min_lng

    def contains(self, point: GeoPoint) -> bool:
        """Check if a geographic point lies inside the bounding box."""
        return (
            self.min_lat <= point.lat <= self.max_lat
            and self.min_lng <= point.lng <= self.max_lng
        )


# Curated Greek Railway Network presets
GREECE_PRESET = BoundingBox(
    min_lat=37.50,
    max_lat=41.35,
    min_lng=21.45,
    max_lng=26.65,
)

PROASTIAKOS_PRESET = BoundingBox(
    min_lat=37.85,
    max_lat=38.45,
    min_lng=22.75,
    max_lng=24.05,
)

PATHE_PRESET = BoundingBox(
    min_lat=37.90,
    max_lat=40.75,
    min_lng=21.80,
    max_lng=23.95,
)

NORTHERN_GREECE_PRESET = BoundingBox(
    min_lat=40.30,
    max_lat=41.35,
    min_lng=21.40,
    max_lng=26.65,
)


@dataclass
class Viewport:
    """Terminal viewport configuration for geospatial rendering.

    Attributes:
        center: Focus coordinate of the viewport.
        zoom: Magnification multiplier (1.0 = base scale).
        width_cells: Terminal width in character columns.
        height_cells: Terminal height in character rows.
        aspect_ratio: Terminal font aspect ratio (height / width, typically ~2.0).
    """
    center: GeoPoint
    zoom: float = 1.0
    width_cells: int = 80
    height_cells: int = 24
    aspect_ratio: float = 2.0


class GeoProjector:
    """Projects WGS84 geographic coordinates onto a 2D sub-pixel raster plane.

    Uses an Equirectangular projection centered on the viewport focus, scaled
    with standard parallel cosine correction and terminal font cell aspect ratio
    compensation.

    Attributes:
        viewport: Current Viewport state.
        standard_parallel: Reference latitude in degrees for cosine correction.
    """

    def __init__(self, viewport: Viewport, standard_parallel: float = 39.5) -> None:
        """Initialize the projector with a viewport and standard parallel.

        Args:
            viewport: Viewport geometry and magnification settings.
            standard_parallel: Reference latitude for cosine scaling (defaults to 39.5 deg N).
        """
        self.viewport = viewport
        self.standard_parallel = standard_parallel
        self._cos_phi0 = math.cos(math.radians(standard_parallel))

    @property
    def sub_width(self) -> int:
        """Sub-pixel canvas width (2 sub-pixels per character cell)."""
        return self.viewport.width_cells * 2

    @property
    def sub_height(self) -> int:
        """Sub-pixel canvas height (4 sub-pixels per character cell)."""
        return self.viewport.height_cells * 4

    @property
    def subpixel_aspect_ratio(self) -> float:
        """Physical height-to-width ratio of a single 2x4 Braille sub-pixel dot.

        Because each terminal character cell is subdivided into 2 horizontal
        sub-pixels and 4 vertical sub-pixels, the 2:4 sub-pixel grid already
        divides the character cell aspect ratio (H_cell / W_cell, typically ~2.0)
        by 2.0, yielding a square 1.0 sub-pixel aspect ratio for a 2:1 font.
        """
        return max(0.25, self.viewport.aspect_ratio / 2.0)

    def _get_base_scale_x(self) -> float:
        """Compute base horizontal scale (sub-pixels / deg lng) fitting GREECE_PRESET at zoom=1.0."""
        scale_x_w = self.sub_width / GREECE_PRESET.span_lng
        scale_x_h = (self.sub_height * self.subpixel_aspect_ratio) / GREECE_PRESET.span_lat
        return min(scale_x_w, scale_x_h)

    def get_scale_x(self) -> float:
        """Calculate the horizontal scale in sub-pixels per degree of longitude.

        Base scale fits the Greek national bounding box across both canvas width
        and canvas height at zoom=1.0.

        Returns:
            Scale factor in sub-pixels per degree longitude.
        """
        return self._get_base_scale_x() * self.viewport.zoom

    def get_scale_y(self) -> float:
        """Calculate the vertical scale in sub-pixels per degree of latitude.

        Returns:
            Scale factor in sub-pixels per degree latitude.
        """
        return self.get_scale_x() / self.subpixel_aspect_ratio

    def geo_to_subpixel(self, point: GeoPoint) -> Tuple[int, int]:
        """Convert a WGS84 coordinate into sub-pixel canvas coordinates (px, py).

        Args:
            point: Geographic point (lat, lng).

        Returns:
            Tuple of integer sub-pixel coordinates (px, py).
        """
        center_x = self.sub_width / 2.0
        center_y = self.sub_height / 2.0
        scale_x = self.get_scale_x()
        scale_y = self.get_scale_y()

        delta_lng = point.lng - self.viewport.center.lng
        delta_lat = point.lat - self.viewport.center.lat

        px = center_x + scale_x * delta_lng * self._cos_phi0
        py = center_y - scale_y * delta_lat

        return int(round(px)), int(round(py))

    def geo_to_cell(self, point: GeoPoint) -> Tuple[int, int]:
        """Convert a WGS84 coordinate into character cell coordinates (col, row).

        Args:
            point: Geographic point (lat, lng).

        Returns:
            Tuple of integer character cell coordinates (col, row).
        """
        px, py = self.geo_to_subpixel(point)
        return px // 2, py // 4

    def resolve_track_cell(
        self,
        point: GeoPoint,
        canvas: Optional["BrailleCanvas"] = None,
    ) -> Tuple[int, int]:
        """Convert a WGS84 coordinate to character cell (col, row) with sub-pixel track tie-breaking.

        When a point lies on a track polyline, floating-point rounding in geo_to_subpixel()
        can occasionally place (px, py) 1 sub-pixel across a character cell boundary from the
        integer Bresenham line rasterized on BrailleCanvas. If canvas is provided and the
        primary cell (px // 2, py // 4) has no active Braille dots, this method inspects the
        immediate 1-sub-pixel neighborhood and snaps to the adjacent cell containing the
        rasterized track dot.

        Args:
            point: Geographic point (lat, lng).
            canvas: Optional rasterized BrailleCanvas containing track bitmasks.

        Returns:
            Tuple of integer character cell coordinates (col, row).
        """
        px, py = self.geo_to_subpixel(point)
        col = px // 2
        row = py // 4

        if canvas is None:
            return col, row

        w_cells = canvas.width_cells
        h_cells = canvas.height_cells

        if 0 <= col < w_cells and 0 <= row < h_cells:
            if canvas.buffer[row * w_cells + col] != 0:
                return col, row

        neighbors = (
            (0, -1),
            (0, 1),
            (-1, 0),
            (1, 0),
            (-1, -1),
            (1, -1),
            (-1, 1),
            (1, 1),
        )
        for dx, dy in neighbors:
            adj_px = px + dx
            adj_py = py + dy
            if 0 <= adj_px < canvas.sub_width and 0 <= adj_py < canvas.sub_height:
                adj_col = adj_px // 2
                adj_row = adj_py // 4
                if canvas.buffer[adj_row * w_cells + adj_col] != 0:
                    return adj_col, adj_row

        return col, row

    def subpixel_to_geo(self, px: int, py: int) -> GeoPoint:
        """Convert sub-pixel coordinates (px, py) back to WGS84 coordinates.

        Args:
            px: Sub-pixel X coordinate.
            py: Sub-pixel Y coordinate.

        Returns:
            Corresponding GeoPoint (lat, lng).
        """
        center_x = self.sub_width / 2.0
        center_y = self.sub_height / 2.0
        scale_x = self.get_scale_x()
        scale_y = self.get_scale_y()

        delta_lng = (px - center_x) / (scale_x * self._cos_phi0)
        delta_lat = (center_y - py) / scale_y

        lng = self.viewport.center.lng + delta_lng
        lat = self.viewport.center.lat + delta_lat

        return GeoPoint(lat=lat, lng=lng)

    def is_visible(self, point: GeoPoint) -> bool:
        """Check if a geographic point falls within the current sub-pixel viewport.

        Args:
            point: Geographic coordinate to test.

        Returns:
            True if point projects within canvas sub-pixel boundaries; False otherwise.
        """
        px, py = self.geo_to_subpixel(point)
        return 0 <= px < self.sub_width and 0 <= py < self.sub_height

    def fit_bounding_box(self, bbox: BoundingBox) -> None:
        """Adjust viewport center and zoom to fit a bounding box within the canvas.

        Args:
            bbox: Geographic bounding box to frame in the viewport.
        """
        self.viewport.center = bbox.center
        scale_x_needed = self.sub_width / (bbox.span_lng * self._cos_phi0)
        scale_y_needed = self.sub_height / bbox.span_lat
        effective_scale_x = min(scale_x_needed, scale_y_needed * self.subpixel_aspect_ratio)

        base_scale = self._get_base_scale_x()
        self.viewport.zoom = max(0.2, effective_scale_x / base_scale)

    def get_km_per_cell(self) -> float:
        """Compute kilometers per terminal column cell at current viewport center latitude.

        Uses the conformal equirectangular cell resolution formula:
            km_per_cell = (2.0 / (S_x * cos(phi_0))) * 111.32
        where S_x is the horizontal scale in sub-pixels per degree longitude and
        phi_0 is the current viewport center latitude in degrees.

        Returns:
            Kilometers per horizontal character cell (positive float).
        """
        scale_x = max(1e-6, self.get_scale_x())
        cos_phi0 = max(0.05, abs(math.cos(math.radians(self.viewport.center.lat))))
        return (2.0 / (scale_x * cos_phi0)) * KM_PER_DEGREE_LAT

    def compute_scale_bar(
        self,
        min_cells: int = 12,
        max_cells: int = 18,
    ) -> ScaleBarSpec:
        """Snap a target bar width of min_cells..max_cells to the nearest standard cartographic interval.

        Evaluates candidate intervals in SCALE_BAR_INTERVALS_KM (1..1500 km) against
        the current conformal km_per_cell resolution. Prefers intervals whose ideal cell
        width falls within [min_cells, max_cells] and is closest to the midpoint target;
        otherwise selects the interval closest to the target cell width and clamps the
        rendered cell width to [min_cells, max_cells].

        Args:
            min_cells: Minimum character cell width for the scale bar (default: 12).
            max_cells: Maximum character cell width for the scale bar (default: 18).

        Returns:
            ScaleBarSpec containing (distance_km, width_cells, km_per_cell).
        """
        low = max(6, int(min_cells))
        high = max(low, int(max_cells))
        target_cells = (low + high) / 2.0
        km_per_cell = self.get_km_per_cell()

        in_band: List[Tuple[float, int, int]] = []
        all_candidates: List[Tuple[float, int, int]] = []

        for interval_km in SCALE_BAR_INTERVALS_KM:
            exact_cells = interval_km / km_per_cell
            rounded_cells = int(round(exact_cells))
            dist_to_target = abs(exact_cells - target_cells)
            candidate = (dist_to_target, interval_km, rounded_cells)
            all_candidates.append(candidate)
            if low <= exact_cells <= high or low <= rounded_cells <= high:
                in_band.append(candidate)

        if in_band:
            _, best_km, raw_cells = min(in_band, key=lambda item: (item[0], item[1]))
        else:
            _, best_km, raw_cells = min(all_candidates, key=lambda item: (item[0], item[1]))

        width_cells = max(low, min(high, raw_cells))
        return ScaleBarSpec(
            distance_km=best_km,
            width_cells=width_cells,
            km_per_cell=km_per_cell,
        )


class BrailleCanvas:
    """A 2D terminal drawing canvas backed by Unicode Braille patterns (2x4 dots/cell).

    Provides sub-pixel graphics rendering by representing each terminal cell
    as an 8-dot matrix. Supports line drawing via Bresenham's algorithm and
    renders rows directly to Unicode strings.

    Attributes:
        width_cells: Canvas width in character cells.
        height_cells: Canvas height in character cells.
        sub_width: Canvas width in sub-pixels (width_cells * 2).
        sub_height: Canvas height in sub-pixels (height_cells * 4).
    """

    def __init__(self, width_cells: int, height_cells: int, ascii_mode: bool = False) -> None:
        """Initialize the Braille canvas with dimensions in character cells.

        Args:
            width_cells: Terminal columns allocated for the canvas.
            height_cells: Terminal rows allocated for the canvas.
            ascii_mode: Whether to render using standard 7-bit ASCII characters
                        instead of Unicode Braille patterns.
        """
        if width_cells <= 0 or height_cells <= 0:
            raise ValueError("Canvas dimensions must be positive integers.")

        self.width_cells = width_cells
        self.height_cells = height_cells
        self.sub_width = width_cells * 2
        self.sub_height = height_cells * 4
        self.ascii_mode = ascii_mode
        self.buffer = bytearray(width_cells * height_cells)

    def clear(self) -> None:
        """Clear all active dots on the canvas."""
        for i in range(len(self.buffer)):
            self.buffer[i] = 0

    def _get_buffer_index(self, px: int, py: int) -> Tuple[int, int, int]:
        """Compute buffer index and sub-pixel offsets.

        Args:
            px: Sub-pixel X coordinate.
            py: Sub-pixel Y coordinate.

        Returns:
            Tuple of (buffer_index, sub_x, sub_y).
        """
        cell_x = px // 2
        cell_y = py // 4
        idx = cell_y * self.width_cells + cell_x
        sub_x = px % 2
        sub_y = py % 4
        return idx, sub_x, sub_y

    def set_pixel(self, px: int, py: int) -> None:
        """Turn on the sub-pixel at (px, py).

        Silently ignores out-of-bounds coordinates to prevent rendering clipping errors.

        Args:
            px: Sub-pixel X coordinate (0 <= px < sub_width).
            py: Sub-pixel Y coordinate (0 <= py < sub_height).
        """
        if not (0 <= px < self.sub_width and 0 <= py < self.sub_height):
            return

        idx, sub_x, sub_y = self._get_buffer_index(px, py)
        self.buffer[idx] |= DOT_MASKS[sub_x][sub_y]

    def clear_pixel(self, px: int, py: int) -> None:
        """Turn off the sub-pixel at (px, py).

        Args:
            px: Sub-pixel X coordinate.
            py: Sub-pixel Y coordinate.
        """
        if not (0 <= px < self.sub_width and 0 <= py < self.sub_height):
            return

        idx, sub_x, sub_y = self._get_buffer_index(px, py)
        self.buffer[idx] &= ~DOT_MASKS[sub_x][sub_y]

    def get_pixel(self, px: int, py: int) -> bool:
        """Check whether the sub-pixel at (px, py) is active.

        Args:
            px: Sub-pixel X coordinate.
            py: Sub-pixel Y coordinate.

        Returns:
            True if the dot is set; False otherwise.
        """
        if not (0 <= px < self.sub_width and 0 <= py < self.sub_height):
            return False

        idx, sub_x, sub_y = self._get_buffer_index(px, py)
        mask = DOT_MASKS[sub_x][sub_y]
        return bool(self.buffer[idx] & mask)

    def draw_line(self, x0: int, y0: int, x1: int, y1: int) -> None:
        """Rasterize a line between two sub-pixel points using Bresenham's algorithm.

        Points outside canvas boundaries are clipped safely without errors.

        Args:
            x0: Start sub-pixel X coordinate.
            y0: Start sub-pixel Y coordinate.
            x1: End sub-pixel X coordinate.
            y1: End sub-pixel Y coordinate.
        """
        dx = abs(x1 - x0)
        dy = abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx - dy

        curr_x, curr_y = x0, y0

        while True:
            self.set_pixel(curr_x, curr_y)

            if curr_x == x1 and curr_y == y1:
                break

            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                curr_x += sx
            if e2 < dx:
                err += dx
                curr_y += sy

    def draw_polyline(self, points: List[Tuple[int, int]]) -> None:
        """Draw connected line segments across a sequence of sub-pixel coordinates.

        Args:
            points: List of (px, py) coordinates to connect sequentially.
        """
        if len(points) < 2:
            if len(points) == 1:
                self.set_pixel(points[0][0], points[0][1])
            return

        for i in range(len(points) - 1):
            p1 = points[i]
            p2 = points[i + 1]
            self.draw_line(p1[0], p1[1], p2[0], p2[1])

    def render_rows(self, ascii_mode: Optional[bool] = None) -> List[str]:
        """Convert the canvas buffer into a list of terminal row strings.

        Args:
            ascii_mode: Optional boolean override to force ASCII or Braille rendering.
                        If None, uses self.ascii_mode.

        Returns:
            List of character strings representing character cells row-by-row.
        """
        use_ascii = self.ascii_mode if ascii_mode is None else ascii_mode
        rows: List[str] = []
        for r in range(self.height_cells):
            row_chars: List[str] = []
            row_offset = r * self.width_cells
            for c in range(self.width_cells):
                cell_bits = self.buffer[row_offset + c]
                if use_ascii:
                    row_chars.append(ASCII_BRAILLE_TABLE[cell_bits])
                else:
                    row_chars.append(chr(BRAILLE_BASE + cell_bits))
            rows.append("".join(row_chars))
        return rows

    def render_to_string(self, ascii_mode: Optional[bool] = None) -> str:
        """Render the complete canvas as a multi-line string joined by newlines.

        Args:
            ascii_mode: Optional boolean override to force ASCII or Braille rendering.

        Returns:
            Multi-line string representing the rendered grid.
        """
        return "\n".join(self.render_rows(ascii_mode=ascii_mode))


# ==============================================================================
# F-7.2: Infrastructure GIS Layers: Tunnels, Bridges & Signals
# ==============================================================================


class InfrastructureType(Enum):
    """Classification of civil and operational railway infrastructure features."""

    TUNNEL = "tunnel"
    BRIDGE = "bridge"
    SIGNAL = "signal"


class SignalAspect(Enum):
    """Interlocking signal type or operational aspect."""

    JUNCTION = "junction"
    INTERLOCKING = "interlocking"
    PORTAL = "portal"
    BLOCK = "block"


@dataclass(frozen=True)
class PortalNode:
    """Geographic tunnel portal entry/exit boundary point."""

    name: str
    point: GeoPoint
    altitude_m: Optional[float] = None


@dataclass(frozen=True)
class InfrastructureFeature:
    """Railway infrastructure GIS feature (tunnel, bridge, or interlocking signal)."""

    id: str
    name: str
    feature_type: InfrastructureType
    corridor_id: str
    point: GeoPoint
    points: Tuple[GeoPoint, ...] = ()
    length_m: Optional[float] = None
    bbox: Optional[BoundingBox] = None
    portals: Tuple[PortalNode, ...] = ()
    signal_aspect: Optional[SignalAspect] = None
    description: str = ""

    @classmethod
    def create_tunnel(
        cls,
        id: str,
        name: str,
        corridor_id: str,
        points: Sequence[GeoPoint],
        portals: Sequence[PortalNode] = (),
        length_m: Optional[float] = None,
        description: str = "",
    ) -> "InfrastructureFeature":
        """Factory method to construct a validated tunnel feature with precalculated BoundingBox."""
        pts = tuple(points)
        if pts:
            min_lat = min(p.lat for p in pts)
            max_lat = max(p.lat for p in pts)
            min_lng = min(p.lng for p in pts)
            max_lng = max(p.lng for p in pts)
        else:
            min_lat = max_lat = min_lng = max_lng = 0.0
        pad = 0.001
        bbox = BoundingBox(
            min_lat=min_lat - pad,
            max_lat=max_lat + pad,
            min_lng=min_lng - pad,
            max_lng=max_lng + pad,
        )
        calc_len = length_m
        if calc_len is None and len(pts) >= 2:
            calc_len = sum(
                haversine_distance_km(pts[i], pts[i + 1]) * 1000.0
                for i in range(len(pts) - 1)
            )
        center_pt = portals[0].point if portals else (pts[len(pts) // 2] if pts else GeoPoint(0.0, 0.0))
        return cls(
            id=id,
            name=name,
            feature_type=InfrastructureType.TUNNEL,
            corridor_id=corridor_id,
            point=center_pt,
            points=pts,
            length_m=calc_len,
            bbox=bbox,
            portals=tuple(portals),
            description=description,
        )

    @classmethod
    def create_bridge(
        cls,
        id: str,
        name: str,
        corridor_id: str,
        points: Sequence[GeoPoint],
        length_m: Optional[float] = None,
        description: str = "",
    ) -> "InfrastructureFeature":
        """Factory method to construct a bridge or viaduct feature."""
        pts = tuple(points)
        if pts:
            min_lat = min(p.lat for p in pts)
            max_lat = max(p.lat for p in pts)
            min_lng = min(p.lng for p in pts)
            max_lng = max(p.lng for p in pts)
            mid_pt = pts[len(pts) // 2]
        else:
            min_lat = max_lat = min_lng = max_lng = 0.0
            mid_pt = GeoPoint(0.0, 0.0)
        pad = 0.001
        bbox = BoundingBox(
            min_lat=min_lat - pad,
            max_lat=max_lat + pad,
            min_lng=min_lng - pad,
            max_lng=max_lng + pad,
        )
        calc_len = length_m
        if calc_len is None and len(pts) >= 2:
            calc_len = sum(
                haversine_distance_km(pts[i], pts[i + 1]) * 1000.0
                for i in range(len(pts) - 1)
            )
        return cls(
            id=id,
            name=name,
            feature_type=InfrastructureType.BRIDGE,
            corridor_id=corridor_id,
            point=mid_pt,
            points=pts,
            length_m=calc_len,
            bbox=bbox,
            description=description,
        )

    @classmethod
    def create_signal(
        cls,
        id: str,
        name: str,
        corridor_id: str,
        point: GeoPoint,
        signal_aspect: SignalAspect = SignalAspect.JUNCTION,
        description: str = "",
    ) -> "InfrastructureFeature":
        """Factory method to construct an interlocking or junction signal feature."""
        bbox = BoundingBox(
            min_lat=point.lat - 0.0005,
            max_lat=point.lat + 0.0005,
            min_lng=point.lng - 0.0005,
            max_lng=point.lng + 0.0005,
        )
        return cls(
            id=id,
            name=name,
            feature_type=InfrastructureType.SIGNAL,
            corridor_id=corridor_id,
            point=point,
            points=(point,),
            bbox=bbox,
            signal_aspect=signal_aspect,
            description=description,
        )


def is_point_in_tunnel(
    point: GeoPoint, tunnel: InfrastructureFeature, max_dist_m: float = 45.0
) -> bool:
    """Determine whether a geographic point lies within a tunnel polyline corridor.

    Args:
        point: Query coordinate (lat, lng).
        tunnel: InfrastructureFeature of type TUNNEL.
        max_dist_m: Maximum perpendicular distance in meters (default: 45m).

    Returns:
        True if the point lies inside the tunnel envelope; False otherwise.
    """
    if tunnel.feature_type != InfrastructureType.TUNNEL:
        return False
    if tunnel.bbox and not tunnel.bbox.contains(point):
        return False

    pts = tunnel.points
    if len(pts) < 2:
        if len(pts) == 1:
            return haversine_distance_km(point, pts[0]) * 1000.0 <= max_dist_m
        return False

    for i in range(len(pts) - 1):
        _, perp_dist_m = project_point_to_segment(point, pts[i], pts[i + 1])
        if perp_dist_m <= max_dist_m:
            return True
    return False


def detect_train_tunnel(
    point: GeoPoint,
    tunnels: Sequence[InfrastructureFeature],
    max_dist_m: float = 45.0,
) -> Optional[Tuple[InfrastructureFeature, float, float]]:
    """Identify which tunnel (if any) contains the given point and compute progress.

    Args:
        point: Query coordinate (lat, lng).
        tunnels: Candidate tunnel infrastructure features.
        max_dist_m: Maximum perpendicular distance threshold in meters.

    Returns:
        Tuple of (tunnel_feature, distance_along_m, progress_ratio_0_to_1) if inside
        a tunnel; None otherwise.
    """
    for tunnel in tunnels:
        if tunnel.feature_type != InfrastructureType.TUNNEL:
            continue
        if tunnel.bbox and not tunnel.bbox.contains(point):
            continue

        pts = tunnel.points
        if len(pts) < 2:
            continue

        best_perp = float("inf")
        best_seg_idx = -1
        best_t = 0.0

        for i in range(len(pts) - 1):
            p0, p1 = pts[i], pts[i + 1]
            mean_lat_rad = math.radians((p0.lat + p1.lat) * 0.5)
            m_per_deg_lat = (math.pi / 180.0) * EARTH_RADIUS_M
            m_per_deg_lng = m_per_deg_lat * math.cos(mean_lat_rad)
            ux = (p1.lng - p0.lng) * m_per_deg_lng
            uy = (p1.lat - p0.lat) * m_per_deg_lat
            vx = (point.lng - p0.lng) * m_per_deg_lng
            vy = (point.lat - p0.lat) * m_per_deg_lat
            seg_len_sq = ux * ux + uy * uy
            if seg_len_sq <= 1e-12:
                t = 0.0
                perp = math.hypot(vx, vy)
            else:
                t = max(0.0, min(1.0, (vx * ux + vy * uy) / seg_len_sq))
                dx = vx - t * ux
                dy = vy - t * uy
                perp = math.hypot(dx, dy)

            if perp < best_perp:
                best_perp = perp
                best_seg_idx = i
                best_t = t

        if best_perp <= max_dist_m and best_seg_idx >= 0:
            dist_along = 0.0
            for i in range(best_seg_idx):
                dist_along += haversine_distance_km(pts[i], pts[i + 1]) * 1000.0
            dist_along += haversine_distance_km(pts[best_seg_idx], pts[best_seg_idx + 1]) * 1000.0 * best_t
            total_len = tunnel.length_m or max(1.0, dist_along)
            dist_along = max(0.0, min(total_len, dist_along))
            ratio = max(0.0, min(1.0, dist_along / total_len))
            return tunnel, dist_along, ratio

    return None


def _extract_geometry_points(geom: Dict[str, Any]) -> List[GeoPoint]:
    """Extract ordered GeoPoint coordinates from GeoJSON geometry dict."""
    g_type = geom.get("type", "")
    coords = geom.get("coordinates", [])
    pts: List[GeoPoint] = []

    if g_type == "Point" and isinstance(coords, (list, tuple)) and len(coords) >= 2:
        try:
            return [GeoPoint(lat=float(coords[1]), lng=float(coords[0]))]
        except (ValueError, TypeError):
            return []

    if g_type == "LineString" and isinstance(coords, list):
        for c in coords:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                try:
                    pts.append(GeoPoint(lat=float(c[1]), lng=float(c[0])))
                except (ValueError, TypeError):
                    pass
        return pts

    if g_type == "MultiLineString" and isinstance(coords, list):
        for line in coords:
            if isinstance(line, list):
                for c in line:
                    if isinstance(c, (list, tuple)) and len(c) >= 2:
                        try:
                            pts.append(GeoPoint(lat=float(c[1]), lng=float(c[0])))
                        except (ValueError, TypeError):
                            pass
        return pts

    return pts


def _parse_portal_nodes(raw_portals: Any) -> List[PortalNode]:
    """Parse tunnel portal dictionary list into PortalNodes."""
    if not isinstance(raw_portals, list):
        return []
    portals: List[PortalNode] = []
    for p in raw_portals:
        if not isinstance(p, dict):
            continue
        p_name = str(p.get("name") or "Portal")
        p_coords = p.get("coordinates") or p.get("point") or []
        if isinstance(p_coords, (list, tuple)) and len(p_coords) >= 2:
            try:
                portals.append(
                    PortalNode(
                        name=p_name,
                        point=GeoPoint(lat=float(p_coords[1]), lng=float(p_coords[0])),
                        altitude_m=float(p["altitude_m"]) if p.get("altitude_m") is not None else None,
                    )
                )
            except (ValueError, TypeError):
                pass
    return portals


def _parse_signal_aspect(aspect_val: Any) -> SignalAspect:
    """Normalize raw signal aspect string into SignalAspect enum."""
    aspect_str = str(aspect_val or "junction").strip().lower()
    if aspect_str == "interlocking":
        return SignalAspect.INTERLOCKING
    if aspect_str == "portal":
        return SignalAspect.PORTAL
    if aspect_str == "block":
        return SignalAspect.BLOCK
    return SignalAspect.JUNCTION


def _parse_single_feature(item: Dict[str, Any]) -> Optional[InfrastructureFeature]:
    """Parse a single GeoJSON feature dictionary into an InfrastructureFeature."""
    if not isinstance(item, dict):
        return None
    geom = item.get("geometry") or {}
    props = item.get("properties") or {}
    fid = str(item.get("id") or props.get("id") or "")
    name = str(props.get("name") or fid)
    raw_type = str(props.get("type") or "").strip().lower()
    corridor = str(props.get("corridor") or props.get("corridor_id") or "")
    desc = str(props.get("description") or "")

    length_m: Optional[float] = None
    if props.get("length_m") is not None:
        try:
            length_m = float(props["length_m"])
        except (ValueError, TypeError):
            length_m = None

    pts = _extract_geometry_points(geom)
    if not pts:
        return None

    if raw_type == "tunnel":
        portals = _parse_portal_nodes(props.get("portals"))
        return InfrastructureFeature.create_tunnel(
            id=fid,
            name=name,
            corridor_id=corridor,
            points=pts,
            portals=portals,
            length_m=length_m,
            description=desc,
        )

    if raw_type == "bridge":
        return InfrastructureFeature.create_bridge(
            id=fid,
            name=name,
            corridor_id=corridor,
            points=pts,
            length_m=length_m,
            description=desc,
        )

    if raw_type == "signal":
        aspect = _parse_signal_aspect(props.get("aspect") or props.get("signal_aspect"))
        return InfrastructureFeature.create_signal(
            id=fid,
            name=name,
            corridor_id=corridor,
            point=pts[0],
            signal_aspect=aspect,
            description=desc,
        )

    return None


def parse_infrastructure_geojson(
    payload: Union[Dict[str, Any], str]
) -> List[InfrastructureFeature]:
    """Parse GeoJSON FeatureCollection or JSON string into InfrastructureFeatures.

    Handles LineString, MultiLineString, and Point geometries defensively.

    Args:
        payload: GeoJSON dictionary or JSON string.

    Returns:
        List of parsed InfrastructureFeature objects.
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except Exception:
            return []
    elif isinstance(payload, dict):
        data = payload
    else:
        return []

    items = data.get("features", [])
    if not isinstance(items, list):
        return []

    features: List[InfrastructureFeature] = []
    for item in items:
        feat = _parse_single_feature(item)
        if feat is not None:
            features.append(feat)

    return features


# -----------------------------------------------------------------------------
# F-5.3 Corridor Speed Limit Exceedance & Slow-Order (TSR) Domain Models & Math
# -----------------------------------------------------------------------------
# Regulatory Reference: Hellenic Railways Network Statement (Δήλωση Δικτύου ΟΣΕ)
# URL: https://www.hellenicrailways.gr/etaireia/dilosi-diktyou
# Line speeds and segment restrictions are derived from:
#   • Παράρτημα ΙΙΙ-Α: «Φόρτιση και Ταχύτητα» (Maximum Permissible Speeds by Track Section)
#   • Παράρτημα Ι-Α & Ι-Β: «Χαρακτηριστικά των Γραμμών του Δικτύου»
# -----------------------------------------------------------------------------


class SpeedLimitZoneType(Enum):
    """Categorization of railway speed restriction zones."""

    CORRIDOR_BASE = auto()   # Baseline corridor design speed limit (e.g. PATHE 160 km/h)
    SEGMENT_LIMIT = auto()   # Civil engineering segment limit (e.g. SKA-Oinoi 110 km/h)
    SLOW_ORDER_TSR = auto()  # Operational Temporary Speed Restriction (e.g. 40 km/h maintenance)


class OverspeedSeverity(Enum):
    """Hierarchical severity classification for train velocity relative to speed limits."""

    NORMAL = auto()          # Speed strictly under governing limit or compliant
    ADVISORY = auto()        # Minor speed threshold margin (0.0 <= delta < 10.0 km/h)
    OVERSPEED = auto()       # General line speed exceedance (delta >= 10.0 km/h)
    TSR_VIOLATION = auto()   # Critical Slow-Order exceedance (delta >= 5.0 km/h in TSR zone)


@dataclass(frozen=True)
class SlowOrder:
    """Operational Temporary Speed Restriction (TSR / Βραδυπορία) record."""

    id: str
    corridor_id: str
    name: str
    max_speed_kmh: float
    points: Tuple[GeoPoint, ...]
    bbox: BoundingBox
    start_chainage_km: Optional[float] = None
    end_chainage_km: Optional[float] = None
    reason: str = "Track Maintenance"
    active: bool = True
    start_time_iso: str = ""
    end_time_iso: str = ""
    buffer_m: float = 200.0

    @classmethod
    def create(
        cls,
        id: str,
        corridor_id: str,
        name: str,
        max_speed_kmh: float,
        points: Sequence[GeoPoint],
        start_chainage_km: Optional[float] = None,
        end_chainage_km: Optional[float] = None,
        reason: str = "Track Maintenance",
        active: bool = True,
        start_time_iso: str = "",
        end_time_iso: str = "",
        buffer_m: float = 200.0,
    ) -> "SlowOrder":
        """Factory method to construct a validated SlowOrder with calculated BoundingBox."""
        pts = tuple(points)
        if pts:
            min_lat = min(p.lat for p in pts)
            max_lat = max(p.lat for p in pts)
            min_lng = min(p.lng for p in pts)
            max_lng = max(p.lng for p in pts)
        else:
            min_lat = max_lat = min_lng = max_lng = 0.0
        pad = 0.003
        bbox = BoundingBox(
            min_lat=min_lat - pad,
            max_lat=max_lat + pad,
            min_lng=min_lng - pad,
            max_lng=max_lng + pad,
        )
        return cls(
            id=id,
            corridor_id=corridor_id,
            name=name,
            max_speed_kmh=float(max_speed_kmh),
            points=pts,
            bbox=bbox,
            start_chainage_km=start_chainage_km,
            end_chainage_km=end_chainage_km,
            reason=reason,
            active=active,
            start_time_iso=start_time_iso,
            end_time_iso=end_time_iso,
            buffer_m=buffer_m,
        )


@dataclass(frozen=True)
class CorridorSegmentLimit:
    """Civil engineering speed restriction on a localized segment of a rail corridor."""

    id: str
    corridor_id: str
    name: str
    max_speed_kmh: float
    points: Tuple[GeoPoint, ...]
    bbox: BoundingBox
    start_chainage_km: Optional[float] = None
    end_chainage_km: Optional[float] = None
    buffer_m: float = 120.0

    @classmethod
    def create(
        cls,
        id: str,
        corridor_id: str,
        name: str,
        max_speed_kmh: float,
        points: Sequence[GeoPoint],
        start_chainage_km: Optional[float] = None,
        end_chainage_km: Optional[float] = None,
        buffer_m: float = 120.0,
    ) -> "CorridorSegmentLimit":
        """Factory method constructing a CorridorSegmentLimit with BoundingBox."""
        pts = tuple(points)
        if pts:
            min_lat = min(p.lat for p in pts)
            max_lat = max(p.lat for p in pts)
            min_lng = min(p.lng for p in pts)
            max_lng = max(p.lng for p in pts)
        else:
            min_lat = max_lat = min_lng = max_lng = 0.0
        pad = 0.003
        bbox = BoundingBox(
            min_lat=min_lat - pad,
            max_lat=max_lat + pad,
            min_lng=min_lng - pad,
            max_lng=max_lng + pad,
        )
        return cls(
            id=id,
            corridor_id=corridor_id,
            name=name,
            max_speed_kmh=float(max_speed_kmh),
            points=pts,
            bbox=bbox,
            start_chainage_km=start_chainage_km,
            end_chainage_km=end_chainage_km,
            buffer_m=buffer_m,
        )


@dataclass(frozen=True)
class CorridorSpeedRule:
    """Master speed configuration for a railway corridor."""

    corridor_id: str
    name: str
    default_speed_kmh: float
    segments: Tuple[CorridorSegmentLimit, ...] = ()
    description: str = ""


@dataclass(frozen=True)
class SpeedEnforcementResult:
    """Evaluation result comparing instantaneous locomotive velocity against speed limits."""

    train_id: str
    current_speed: float
    governing_limit: float
    margin_kmh: float
    severity: OverspeedSeverity
    governing_name: str
    is_tsr: bool
    active_tsr: Optional[SlowOrder] = None
    status_label: str = ""
    color_pair: int = 1


# Fallback speed limits derived from OSE Network Statement (https://www.hellenicrailways.gr/etaireia/dilosi-diktyou):
#   - PATHE Mainline (Tithorea-Domokos bypass / Thessaly plain): 160.0 km/h passenger cap
#   - Proastiakos (Airport-SKA-Kiato express median): 120.0 km/h
#   - Regional single-track branches (Chalkida 90.0, Thessaly 80.0, W. Macedonia 70.0 km/h)
#   - Mountain and urban segments: 60.0-110.0 km/h
DEFAULT_CORRIDOR_SPEED_RULES: Dict[str, CorridorSpeedRule] = {
    "pathe-mainline": CorridorSpeedRule(
        corridor_id="pathe-mainline",
        name="PATHE Mainline",
        default_speed_kmh=160.0,
        description="Piraeus - Athens - Thessaloniki Mainline",
    ),
    "proastiakos-airport-kiato": CorridorSpeedRule(
        corridor_id="proastiakos-airport-kiato",
        name="Proastiakos Airport - Kiato",
        default_speed_kmh=120.0,
        description="Athens Airport - SKA - Kiato Suburban Corridor",
    ),
    "proastiakos-chalkida": CorridorSpeedRule(
        corridor_id="proastiakos-chalkida",
        name="Proastiakos Chalkida Branch",
        default_speed_kmh=90.0,
        description="Oinoi - Chalkida Branch Line",
    ),
    "thessaloniki-idomeni": CorridorSpeedRule(
        corridor_id="thessaloniki-idomeni",
        name="Thessaloniki - Idomeni",
        default_speed_kmh=120.0,
        description="Idomeni International Border Corridor",
    ),
    "northern-corridor": CorridorSpeedRule(
        corridor_id="northern-corridor",
        name="Northern Corridor",
        default_speed_kmh=90.0,
        description="Thessaloniki - Alexandroupoli Corridor",
    ),
    "thessaly-kalambaka": CorridorSpeedRule(
        corridor_id="thessaly-kalambaka",
        name="Thessaly Kalambaka",
        default_speed_kmh=80.0,
        description="Palaiofarsalos - Kalambaka Regional Spur",
    ),
    "thessaly-volos": CorridorSpeedRule(
        corridor_id="thessaly-volos",
        name="Thessaly Volos",
        default_speed_kmh=80.0,
        description="Larissa - Volos Regional Branch",
    ),
    "western-macedonia": CorridorSpeedRule(
        corridor_id="western-macedonia",
        name="Western Macedonia",
        default_speed_kmh=70.0,
        description="Platy - Edessa - Florina Mountain Branch",
    ),
}


def _normalize_corridor_key(corridor_id: Optional[str]) -> Optional[str]:
    """Normalize Hellenic Railways corridor strings and aliases to standard corridor IDs."""
    if not corridor_id:
        return None
    raw = corridor_id.strip().lower()
    if raw in DEFAULT_CORRIDOR_SPEED_RULES:
        return raw

    clean = raw.replace("-", "").replace("_", "")
    if any(k in clean for k in ("pathe", "e85", "mainline", "athina", "thess")):
        return "pathe-mainline"
    if any(k in clean for k in ("airport", "kiato", "pirair")):
        return "proastiakos-airport-kiato"
    if "chalk" in clean:
        return "proastiakos-chalkida"
    if "idomeni" in clean:
        return "thessaloniki-idomeni"
    if "kalambaka" in clean or "farsala" in clean:
        return "thessaly-kalambaka"
    if "volos" in clean:
        return "thessaly-volos"
    if any(k in clean for k in ("edessa", "florina", "veria", "westmacedonia")):
        return "western-macedonia"
    if any(k in clean for k in ("northern", "thrace", "strymon", "alex")):
        return "northern-corridor"
    if "proast" in clean:
        return "proastiakos-airport-kiato"
    return raw


def parse_speed_limits_json(
    payload: Union[Dict[str, Any], str]
) -> Tuple[Dict[str, CorridorSpeedRule], List[SlowOrder]]:
    """Parse speed limits and slow orders JSON payload into domain models.

    Args:
        payload: Dict or JSON string conforming to speed_limits.json schema.

    Returns:
        Tuple of (corridor_rules_dict, slow_orders_list).
    """
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except Exception:
            return dict(DEFAULT_CORRIDOR_SPEED_RULES), []
    elif isinstance(payload, dict):
        data = payload
    else:
        return dict(DEFAULT_CORRIDOR_SPEED_RULES), []

    corridor_rules: Dict[str, CorridorSpeedRule] = dict(DEFAULT_CORRIDOR_SPEED_RULES)
    for c_item in data.get("corridors", []):
        c_id = str(c_item.get("id") or "").strip()
        if not c_id:
            continue
        c_name = str(c_item.get("name") or c_id)
        c_def_spd = float(c_item.get("default_speed_kmh", 120.0))
        seg_list: List[CorridorSegmentLimit] = []
        for s_item in c_item.get("segments", []):
            s_id = str(s_item.get("id") or "")
            s_name = str(s_item.get("name") or s_id)
            s_spd = float(s_item.get("max_speed_kmh", c_def_spd))
            s_pts = [
                GeoPoint(lat=float(coord[0]), lng=float(coord[1]))
                for coord in s_item.get("points", [])
                if len(coord) >= 2
            ]
            s_buf = float(s_item.get("buffer_m", 120.0))
            seg_list.append(
                CorridorSegmentLimit.create(
                    id=s_id,
                    corridor_id=c_id,
                    name=s_name,
                    max_speed_kmh=s_spd,
                    points=s_pts,
                    buffer_m=s_buf,
                )
            )
        corridor_rules[c_id] = CorridorSpeedRule(
            corridor_id=c_id,
            name=c_name,
            default_speed_kmh=c_def_spd,
            segments=tuple(seg_list),
            description=str(c_item.get("description") or ""),
        )

    slow_orders: List[SlowOrder] = []
    for tsr_item in data.get("slow_orders", []):
        t_id = str(tsr_item.get("id") or "")
        t_corr = str(tsr_item.get("corridor_id") or "")
        t_name = str(tsr_item.get("name") or t_id)
        t_spd = float(tsr_item.get("max_speed_kmh", 40.0))
        t_active = bool(tsr_item.get("active", True))
        t_reason = str(tsr_item.get("reason") or "Track Maintenance")
        t_buf = float(tsr_item.get("buffer_m", 200.0))
        t_pts = [
            GeoPoint(lat=float(coord[0]), lng=float(coord[1]))
            for coord in tsr_item.get("points", [])
            if len(coord) >= 2
        ]
        slow_orders.append(
            SlowOrder.create(
                id=t_id,
                corridor_id=t_corr,
                name=t_name,
                max_speed_kmh=t_spd,
                points=t_pts,
                reason=t_reason,
                active=t_active,
                buffer_m=t_buf,
            )
        )

    return corridor_rules, slow_orders


def _is_point_on_polyline(point: GeoPoint, pts: Sequence[GeoPoint], buffer_m: float) -> bool:
    """Check if point lies within buffer_m perpendicular distance of a polyline."""
    if len(pts) >= 2:
        for i in range(len(pts) - 1):
            _, perp = project_point_to_segment(point, pts[i], pts[i + 1])
            if perp <= buffer_m:
                return True
    elif len(pts) == 1:
        return haversine_distance_km(point, pts[0]) * 1000.0 <= buffer_m
    return False


def _find_active_tsr(
    point: GeoPoint,
    active_tsrs: Sequence[SlowOrder],
) -> Tuple[Optional[SlowOrder], float]:
    """Find active slow order with lowest speed limit containing point."""
    matched: Optional[SlowOrder] = None
    limit = float("inf")
    for tsr in active_tsrs:
        if not tsr.active:
            continue
        if tsr.bbox and not tsr.bbox.contains(point):
            continue
        if _is_point_on_polyline(point, tsr.points, tsr.buffer_m):
            if tsr.max_speed_kmh < limit:
                limit = tsr.max_speed_kmh
                matched = tsr
    return matched, limit


def _find_segment_limit(
    point: GeoPoint,
    corr_key: Optional[str],
    speed_rules: Dict[str, CorridorSpeedRule],
) -> Tuple[Optional[CorridorSegmentLimit], float, Optional[CorridorSpeedRule]]:
    """Find segment speed limit matching point along corridor or network."""
    matched_seg: Optional[CorridorSegmentLimit] = None
    limit = float("inf")
    corr_rule: Optional[CorridorSpeedRule] = None

    if corr_key and corr_key in speed_rules:
        corr_rule = speed_rules[corr_key]
        for seg in corr_rule.segments:
            if seg.bbox and not seg.bbox.contains(point):
                continue
            if _is_point_on_polyline(point, seg.points, seg.buffer_m):
                if seg.max_speed_kmh < limit:
                    limit = seg.max_speed_kmh
                    matched_seg = seg

    if matched_seg is None and not corr_rule:
        for r in speed_rules.values():
            for seg in r.segments:
                if seg.bbox and not seg.bbox.contains(point):
                    continue
                if _is_point_on_polyline(point, seg.points, seg.buffer_m):
                    if seg.max_speed_kmh < limit:
                        limit = seg.max_speed_kmh
                        matched_seg = seg
                        corr_rule = r

    return matched_seg, limit, corr_rule


def _classify_speed_severity(
    spd: float,
    gov_limit: float,
    margin: float,
    is_tsr: bool,
    overspeed_thresh: float,
    tsr_thresh: float,
) -> Tuple[OverspeedSeverity, str, int]:
    """Classify speed severity tier, badge label, and UI color pair."""
    pair_default = 1
    pair_train_warn = 8
    pair_train_alert = 9

    if is_tsr:
        if margin >= tsr_thresh:
            return OverspeedSeverity.TSR_VIOLATION, f"[TSR OVERSPEED +{int(round(margin))}]", pair_train_alert
        if margin >= 0.0:
            return OverspeedSeverity.ADVISORY, f"[TSR WARN +{int(round(margin))}]", pair_train_warn
        return OverspeedSeverity.NORMAL, f"[TSR: {int(round(gov_limit))} km/h]", pair_train_warn

    if margin >= overspeed_thresh:
        return OverspeedSeverity.OVERSPEED, f"[OVERSPEED +{int(round(margin))}]", pair_train_alert
    if margin >= 0.0:
        return OverspeedSeverity.ADVISORY, f"[WARN +{int(round(margin))}]", pair_train_warn
    return OverspeedSeverity.NORMAL, "", pair_default


def evaluate_train_speed(
    point: GeoPoint,
    current_speed: Optional[float],
    corridor_id: Optional[str],
    rules: Optional[Dict[str, CorridorSpeedRule]] = None,
    active_tsrs: Optional[Sequence[SlowOrder]] = None,
    default_network_limit_kmh: float = 120.0,
    overspeed_threshold_kmh: float = 10.0,
    tsr_overspeed_threshold_kmh: float = 5.0,
    train_id: str = "",
) -> SpeedEnforcementResult:
    """Evaluate train instantaneous velocity against applicable corridor speed limits and active TSRs.

    Args:
        point: Train live geographic coordinate (lat, lng).
        current_speed: Locomotive instantaneous speed in km/h (or None/0.0).
        corridor_id: Active corridor identifier tag (or None).
        rules: Dictionary of corridor speed limit rules indexed by corridor ID.
        active_tsrs: List of currently active Slow Orders.
        default_network_limit_kmh: Fallback speed limit when corridor is unmapped (default: 120.0).
        overspeed_threshold_kmh: Minimum delta to trigger general overspeed alert (default: 10.0).
        tsr_overspeed_threshold_kmh: Minimum delta to trigger TSR overspeed violation (default: 5.0).
        train_id: Train identifier for traceability.

    Returns:
        SpeedEnforcementResult containing severity, governing limit, margin, and status tags.
    """
    speed_rules = rules if rules is not None else DEFAULT_CORRIDOR_SPEED_RULES
    tsr_list = active_tsrs if active_tsrs is not None else ()
    spd = max(0.0, float(current_speed)) if current_speed is not None else 0.0

    # 1. Check for Active Temporary Speed Restrictions (TSRs)
    matched_tsr, tsr_limit = _find_active_tsr(point, tsr_list)

    # 2. Check for Segment-Level Speed Limits
    corr_key = _normalize_corridor_key(corridor_id)
    matched_seg, seg_limit, corr_rule = _find_segment_limit(point, corr_key, speed_rules)

    # 3. Macro Corridor Base Limit & Governing Limit Resolution
    base_limit = corr_rule.default_speed_kmh if corr_rule else default_network_limit_kmh
    gov_limit = min(tsr_limit, seg_limit, base_limit)
    is_tsr = (matched_tsr is not None and gov_limit == matched_tsr.max_speed_kmh)

    if is_tsr and matched_tsr:
        gov_name = f"TSR: {matched_tsr.name}"
    elif matched_seg and gov_limit == matched_seg.max_speed_kmh:
        gov_name = matched_seg.name
    elif corr_rule:
        gov_name = corr_rule.name
    else:
        gov_name = "Network Standard"

    margin = spd - gov_limit
    severity, status_label, pair = _classify_speed_severity(
        spd, gov_limit, margin, is_tsr, overspeed_threshold_kmh, tsr_overspeed_threshold_kmh
    )

    return SpeedEnforcementResult(
        train_id=train_id,
        current_speed=spd,
        governing_limit=gov_limit,
        margin_kmh=margin,
        severity=severity,
        governing_name=gov_name,
        is_tsr=is_tsr,
        active_tsr=matched_tsr,
        status_label=status_label,
        color_pair=pair,
    )


# ---------------------------------------------------------------------------
# Feature F-5.2: Telemetry Drop & Sensor Freezing Watchdog
# ---------------------------------------------------------------------------


class SensorHealthState(Enum):
    """Classification of locomotive telematics and kinematic motion health."""

    NORMAL_MOTION = "NORMAL_MOTION"  # Moving (spd >= 1 km/h), coords advancing, packet <= 30s
    STATION_DWELL = "STATION_DWELL"  # Stopped/idling, within station platform/siding bounds
    SIGNAL_HALT = "SIGNAL_HALT"  # Stopped (spd < 1 km/h), open track (> 250m from station)
    TUNNEL_TRANSIT = "TUNNEL_TRANSIT"  # Safe deadband inside mountain tunnel bore
    STALE = "STALE"  # Telemetry delayed (30s < elapsed <= drop_threshold)
    FROZEN = "FROZEN"  # Spd >= 5 km/h, coords static for >= 60s with fresh packets (<= 15s)
    DROPPED = "DROPPED"  # Complete packet loss exceeding drop/grace threshold


DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC: float = 180.0
DEFAULT_TUNNEL_DROP_MIN_SEC: float = 300.0
DEFAULT_TUNNEL_MARGIN_FACTOR: float = 1.35
DEFAULT_TERMINAL_DWELL_RADIUS_M: float = 600.0
DEFAULT_STATION_DWELL_RADIUS_M: float = 350.0
DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC: float = 60.0
DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH: float = 5.0
DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC: float = 15.0
TERMINAL_STATION_IDS: Set[str] = {"ath", "skg", "ska", "lian", "lar", "dom", "kth"}


@dataclass(frozen=True)
class SensorHealthAssessment:
    """Detailed immutable assessment of a train's telemetry sensor health."""

    state: SensorHealthState
    elapsed_sec: float
    static_duration_sec: float
    displacement_m: float
    current_speed_kmh: float
    nearest_station: Optional[str] = None
    station_distance_m: Optional[float] = None
    dwell_duration_sec: float = 0.0
    is_alert: bool = False
    short_code: str = "RUN"
    description: str = "Normal Motion"

    @property
    def is_critical(self) -> bool:
        """True if the sensor state represents a safety anomaly (FROZEN or DROPPED)."""
        return self.state in (SensorHealthState.FROZEN, SensorHealthState.DROPPED)


def find_nearest_station_aabb(
    point: GeoPoint,
    stations: Sequence[Any],
    max_radius_m: float = 250.0,
) -> Tuple[Optional[Any], Optional[float]]:
    """Find the nearest station within max_radius_m using AABB bounding-box pre-filtering.

    Args:
        point: Query geographic coordinate.
        stations: Candidate station nodes.
        max_radius_m: Maximum search radius in meters (default: 250.0m).

    Returns:
        Tuple of (nearest_station_node, distance_in_meters), or (None, None) if none within max_radius_m.
    """
    if not stations:
        return None, None

    # Precompute lat/lng bounding box degree thresholds
    # 1 deg latitude ≈ 111,320 meters
    delta_lat = max_radius_m / 111320.0
    lat_rad = math.radians(point.lat)
    cos_lat = max(0.01, math.cos(lat_rad))
    delta_lng = max_radius_m / (111320.0 * cos_lat)

    best_stn: Optional[Any] = None
    best_dist_m: Optional[float] = None

    for stn in stations:
        stn_pt: Optional[GeoPoint] = getattr(stn, "point", None)
        if stn_pt is None and isinstance(stn, GeoPoint):
            stn_pt = stn
        if stn_pt is None:
            continue

        # Fast AABB scalar pre-filter
        if abs(point.lat - stn_pt.lat) > delta_lat or abs(point.lng - stn_pt.lng) > delta_lng:
            continue

        dist_m = haversine_distance_km(point, stn_pt) * 1000.0
        if dist_m <= max_radius_m:
            if best_dist_m is None or dist_m < best_dist_m:
                best_dist_m = dist_m
                best_stn = stn

    return best_stn, best_dist_m


def _evaluate_tunnel_health(
    point: GeoPoint,
    spd: float,
    elapsed: float,
    last_moved_time: float,
    ref_now: float,
    tunnels: Optional[Sequence[Any]],
    telemetry_stale_threshold_sec: float,
) -> Optional[SensorHealthAssessment]:
    """Evaluate whether a train is in an active mountain tunnel deadband."""
    tunnel_info = detect_train_tunnel(point, tunnels) if tunnels else None
    if tunnel_info is None:
        return None

    tunnel, dist_along_m, _ = tunnel_info
    tun_len = getattr(tunnel, "length_m", None) or 5000.0
    entry_spd = max(spd, 40.0)  # Conservative floor for freight/crawling transit
    nominal_transit_sec = tun_len / (entry_spd * (1000.0 / 3600.0))
    grace_sec = max(
        DEFAULT_TUNNEL_DROP_MIN_SEC,
        nominal_transit_sec * DEFAULT_TUNNEL_MARGIN_FACTOR,
    )
    tun_name = getattr(tunnel, "name", "Tunnel")

    if elapsed >= grace_sec:
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        age_label = f"{mins}m {secs}s" if mins > 0 else f"{int(elapsed)}s"
        return SensorHealthAssessment(
            state=SensorHealthState.DROPPED,
            elapsed_sec=elapsed,
            static_duration_sec=max(0.0, ref_now - last_moved_time),
            displacement_m=0.0,
            current_speed_kmh=spd,
            nearest_station=tun_name,
            station_distance_m=dist_along_m,
            dwell_duration_sec=0.0,
            is_alert=True,
            short_code="DRP",
            description=f"DROPPED [OVERDUE IN TUNNEL] ({tun_name}, {age_label})",
        )
    if elapsed > telemetry_stale_threshold_sec:
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        age_label = f"{mins}m {secs}s" if mins > 0 else f"{int(elapsed)}s"
        return SensorHealthAssessment(
            state=SensorHealthState.TUNNEL_TRANSIT,
            elapsed_sec=elapsed,
            static_duration_sec=max(0.0, ref_now - last_moved_time),
            displacement_m=0.0,
            current_speed_kmh=spd,
            nearest_station=tun_name,
            station_distance_m=dist_along_m,
            dwell_duration_sec=0.0,
            is_alert=False,
            short_code="TUN",
            description=f"TUNNEL TRANSIT ({tun_name}, {age_label} silent)",
        )
    return None


def _resolve_station_candidate(
    point: GeoPoint,
    stations: Sequence[Any],
    station_dwell_radius_m: float,
    terminal_dwell_radius_m: float,
) -> Tuple[Optional[Any], Optional[float]]:
    """Resolve nearest candidate station node taking terminal radius into account."""
    if not stations:
        return None, None
    stn_candidate, cand_dist = find_nearest_station_aabb(
        point, stations, max_radius_m=terminal_dwell_radius_m
    )
    if stn_candidate is None or cand_dist is None:
        return None, None
    cand_id = str(getattr(stn_candidate, "id", "")).lower()
    effective_radius = (
        terminal_dwell_radius_m
        if cand_id in TERMINAL_STATION_IDS
        else station_dwell_radius_m
    )
    if cand_dist <= effective_radius:
        return stn_candidate, cand_dist
    return None, None


def evaluate_sensor_health(
    point: GeoPoint,
    current_speed: Optional[float],
    last_updated: float,
    last_moved_pt: Optional[GeoPoint],
    last_moved_time: float,
    station_dwell_start: Optional[float],
    stations: Sequence[Any],
    tunnels: Optional[Sequence[Any]] = None,
    now: Optional[float] = None,
    telemetry_drop_threshold_sec: float = DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC,
    telemetry_stale_threshold_sec: float = 30.0,
    sensor_freeze_threshold_sec: float = DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC,
    sensor_freeze_min_speed_kmh: float = DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH,
    sensor_freeze_max_packet_age_sec: float = DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC,
    station_dwell_radius_m: float = DEFAULT_STATION_DWELL_RADIUS_M,
    terminal_dwell_radius_m: float = DEFAULT_TERMINAL_DWELL_RADIUS_M,
    displacement_epsilon_m: float = 10.0,
) -> Tuple[SensorHealthAssessment, Optional[GeoPoint], float, Optional[float]]:
    """Evaluate kinematic continuity, telemetry freshness, and station dwell.

    Args:
        point: Current train geographic location.
        current_speed: Instantaneous velocity in km/h.
        last_updated: Epoch timestamp of the last received position message.
        last_moved_pt: Coordinate anchor where displacement last exceeded noise floor.
        last_moved_time: Epoch timestamp when the train was verified to have displaced.
        station_dwell_start: Epoch timestamp when station dwell began (if dwelling).
        stations: Available railway station nodes.
        tunnels: Available railway tunnel infrastructure features.
        now: Reference timestamp (defaults to time.time()).
        telemetry_drop_threshold_sec: Silence threshold for open-line DROPPED classification.
        telemetry_stale_threshold_sec: Silence threshold for STALE warning (30s).
        sensor_freeze_threshold_sec: Static duration threshold for FROZEN classification (60s).
        sensor_freeze_min_speed_kmh: Minimum speed required for FROZEN qualification (5.0 km/h).
        sensor_freeze_max_packet_age_sec: Maximum packet age for FROZEN qualification (15.0s).
        station_dwell_radius_m: Distance threshold for DWELL classification (350m).
        terminal_dwell_radius_m: Expanded threshold for terminal platforms/sidings (600m).
        displacement_epsilon_m: GNSS multipath noise floor threshold (10m).

    Returns:
        Tuple of (Assessment, updated_last_moved_pt, updated_last_moved_time,
        updated_station_dwell_start).
    """
    ref_now = time.time() if now is None else now
    spd = max(0.0, float(current_speed)) if current_speed is not None else 0.0
    elapsed = max(0.0, ref_now - last_updated)

    # 1. Geodesic Mountain Tunnel Transit Check
    tun_assess = _evaluate_tunnel_health(
        point=point,
        spd=spd,
        elapsed=elapsed,
        last_moved_time=last_moved_time,
        ref_now=ref_now,
        tunnels=tunnels,
        telemetry_stale_threshold_sec=telemetry_stale_threshold_sec,
    )
    if tun_assess is not None:
        return tun_assess, last_moved_pt, last_moved_time, station_dwell_start

    # 2. Station Siding & Terminal Platform Dwell Preemption Check
    nearest_stn, stn_dist = _resolve_station_candidate(
        point=point,
        stations=stations,
        station_dwell_radius_m=station_dwell_radius_m,
        terminal_dwell_radius_m=terminal_dwell_radius_m,
    )

    is_dwelling = nearest_stn is not None and (
        spd <= 5.0 or station_dwell_start is not None
    )
    if is_dwelling and elapsed >= telemetry_drop_threshold_sec:
        stn_name = getattr(
            nearest_stn,
            "name_en",
            getattr(nearest_stn, "name_el", getattr(nearest_stn, "id", "Station")),
        )
        new_dwell_start = (
            station_dwell_start if station_dwell_start is not None else ref_now
        )
        dwell_sec = max(0.0, ref_now - new_dwell_start)
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        age_label = f"{mins}m {secs}s" if mins > 0 else f"{int(elapsed)}s"
        assessment = SensorHealthAssessment(
            state=SensorHealthState.STATION_DWELL,
            elapsed_sec=elapsed,
            static_duration_sec=max(0.0, ref_now - last_moved_time),
            displacement_m=0.0,
            current_speed_kmh=spd,
            nearest_station=stn_name,
            station_distance_m=stn_dist,
            dwell_duration_sec=dwell_sec,
            is_alert=False,
            short_code="DWL",
            description=f"STATION DWELL [IDLE] ({stn_name}, {age_label} silent)",
        )
        return assessment, last_moved_pt, last_moved_time, new_dwell_start

    # 3. Open Line Complete Telemetry Drop Check
    if elapsed >= telemetry_drop_threshold_sec:
        mins = int(elapsed // 60)
        secs = int(elapsed % 60)
        age_label = f"{mins}m {secs}s" if mins > 0 else f"{int(elapsed)}s"
        assessment = SensorHealthAssessment(
            state=SensorHealthState.DROPPED,
            elapsed_sec=elapsed,
            static_duration_sec=max(0.0, ref_now - last_moved_time),
            displacement_m=0.0,
            current_speed_kmh=spd,
            nearest_station=None,
            station_distance_m=None,
            dwell_duration_sec=0.0,
            is_alert=True,
            short_code="DRP",
            description=f"TELEMETRY DROPPED ({age_label} silent)",
        )
        return assessment, last_moved_pt, last_moved_time, station_dwell_start

    # 4. Kinematic Displacement & Anchor Update
    if last_moved_pt is None:
        new_anchor_pt = point
        new_anchor_time = ref_now
        disp_m = 0.0
    else:
        disp_m = haversine_distance_km(point, last_moved_pt) * 1000.0
        if disp_m > displacement_epsilon_m:
            new_anchor_pt = point
            new_anchor_time = ref_now
        else:
            new_anchor_pt = last_moved_pt
            new_anchor_time = last_moved_time

    static_dur = max(0.0, ref_now - new_anchor_time)

    # 5. Dynamic Health Classification (Moving vs Stationary)
    if spd >= 1.0:
        # Moving train: clear any active station dwell
        new_dwell_start = None

        if (
            spd >= sensor_freeze_min_speed_kmh
            and disp_m <= displacement_epsilon_m
            and static_dur >= sensor_freeze_threshold_sec
        ):
            # Candidate for sensor freeze: speed >= min_speed, coordinates static
            if elapsed <= sensor_freeze_max_packet_age_sec:
                # Genuine Sensor Freeze: Positive speed reported with fresh packets, but coordinates static
                assessment = SensorHealthAssessment(
                    state=SensorHealthState.FROZEN,
                    elapsed_sec=elapsed,
                    static_duration_sec=static_dur,
                    displacement_m=disp_m,
                    current_speed_kmh=spd,
                    nearest_station=None,
                    station_distance_m=None,
                    dwell_duration_sec=0.0,
                    is_alert=True,
                    short_code="FRZ",
                    description=f"FROZEN ({int(static_dur)}s static, {spd:.0f} km/h)",
                )
            elif elapsed > telemetry_stale_threshold_sec:
                # Delayed packet delivery while in motion (not a hardware sensor failure)
                assessment = SensorHealthAssessment(
                    state=SensorHealthState.STALE,
                    elapsed_sec=elapsed,
                    static_duration_sec=static_dur,
                    displacement_m=disp_m,
                    current_speed_kmh=spd,
                    nearest_station=None,
                    station_distance_m=None,
                    dwell_duration_sec=0.0,
                    is_alert=False,
                    short_code="STL",
                    description=f"STALE ({int(elapsed)}s delay)",
                )
            else:
                # Minor packet delay or inter-packet jitter within stale window
                assessment = SensorHealthAssessment(
                    state=SensorHealthState.NORMAL_MOTION,
                    elapsed_sec=elapsed,
                    static_duration_sec=static_dur,
                    displacement_m=disp_m,
                    current_speed_kmh=spd,
                    nearest_station=None,
                    station_distance_m=None,
                    dwell_duration_sec=0.0,
                    is_alert=False,
                    short_code="RUN",
                    description=f"Normal Motion ({spd:.0f} km/h)",
                )
        elif elapsed > telemetry_stale_threshold_sec:
            # Delayed packet delivery while in motion
            assessment = SensorHealthAssessment(
                state=SensorHealthState.STALE,
                elapsed_sec=elapsed,
                static_duration_sec=static_dur,
                displacement_m=disp_m,
                current_speed_kmh=spd,
                nearest_station=None,
                station_distance_m=None,
                dwell_duration_sec=0.0,
                is_alert=False,
                short_code="STL",
                description=f"STALE ({int(elapsed)}s delay)",
            )
        else:
            # Normal motion
            assessment = SensorHealthAssessment(
                state=SensorHealthState.NORMAL_MOTION,
                elapsed_sec=elapsed,
                static_duration_sec=static_dur,
                displacement_m=disp_m,
                current_speed_kmh=spd,
                nearest_station=None,
                station_distance_m=None,
                dwell_duration_sec=0.0,
                is_alert=False,
                short_code="RUN",
                description=f"Normal Motion ({spd:.0f} km/h)",
            )
    else:
        # Stationary train (speed < 1.0 km/h)
        if nearest_stn is not None and stn_dist is not None:
            # Authorized station dwell
            new_dwell_start = (
                station_dwell_start if station_dwell_start is not None else ref_now
            )
            dwell_sec = max(0.0, ref_now - new_dwell_start)
            stn_name = getattr(
                nearest_stn,
                "name_en",
                getattr(nearest_stn, "name_el", getattr(nearest_stn, "id", "Station")),
            )
            if elapsed > telemetry_stale_threshold_sec:
                assessment = SensorHealthAssessment(
                    state=SensorHealthState.STALE,
                    elapsed_sec=elapsed,
                    static_duration_sec=static_dur,
                    displacement_m=disp_m,
                    current_speed_kmh=spd,
                    nearest_station=stn_name,
                    station_distance_m=stn_dist,
                    dwell_duration_sec=dwell_sec,
                    is_alert=False,
                    short_code="STL",
                    description=f"STATION DWELL [STALE] ({stn_name}, {int(dwell_sec)}s)",
                )
            else:
                assessment = SensorHealthAssessment(
                    state=SensorHealthState.STATION_DWELL,
                    elapsed_sec=elapsed,
                    static_duration_sec=static_dur,
                    displacement_m=disp_m,
                    current_speed_kmh=spd,
                    nearest_station=stn_name,
                    station_distance_m=stn_dist,
                    dwell_duration_sec=dwell_sec,
                    is_alert=False,
                    short_code="DWL",
                    description=f"STATION DWELL ({stn_name}, {int(dwell_sec)}s)",
                )
        else:
            # Signal halt / open-line wait outside station
            new_dwell_start = None
            if elapsed > telemetry_stale_threshold_sec:
                assessment = SensorHealthAssessment(
                    state=SensorHealthState.STALE,
                    elapsed_sec=elapsed,
                    static_duration_sec=static_dur,
                    displacement_m=disp_m,
                    current_speed_kmh=spd,
                    nearest_station=None,
                    station_distance_m=None,
                    dwell_duration_sec=0.0,
                    is_alert=False,
                    short_code="STL",
                    description=f"SIGNAL HALT [STALE] ({int(elapsed)}s delay)",
                )
            else:
                assessment = SensorHealthAssessment(
                    state=SensorHealthState.SIGNAL_HALT,
                    elapsed_sec=elapsed,
                    static_duration_sec=static_dur,
                    displacement_m=disp_m,
                    current_speed_kmh=spd,
                    nearest_station=None,
                    station_distance_m=None,
                    dwell_duration_sec=0.0,
                    is_alert=False,
                    short_code="HLT",
                    description="SIGNAL HALT [Open Track]",
                )

    return assessment, new_anchor_pt, new_anchor_time, new_dwell_start


# ---------------------------------------------------------------------------
# F-5.1: Corridor Headway & Opposing Train Conflict Watchdog
# ---------------------------------------------------------------------------

DEFAULT_SINGLE_TRACK_CORRIDORS: FrozenSet[str] = frozenset({
    "proastiakos-chalkida",
    "thessaloniki-idomeni",
    "thessaly-kalambaka",
    "thessaly-volos",
    "western-macedonia",
    "northern-corridor",
})

CORRIDOR_DISPLAY_NAMES: Dict[str, str] = {
    "pathe-mainline": "PATHE Mainline",
    "proastiakos-airport-kiato": "Airport - SKA - Kiato",
    "proastiakos-chalkida": "Chalkida Branch",
    "thessaloniki-idomeni": "Idomeni International",
    "northern-corridor": "Eastern Macedonia & Thrace",
    "thessaly-kalambaka": "Thessaly West Branch",
    "thessaly-volos": "Thessaly East Branch",
    "western-macedonia": "Western Macedonia Branch",
}


class ConflictSeverity(str, Enum):
    """Urgency level of a detected rail conflict."""

    CRITICAL = "CRITICAL"   # Imminent collision / emergency braking limit breached
    WARNING = "WARNING"     # Headway compression or opposing approach within warning horizon
    ADVISORY = "ADVISORY"   # Crossing meet or low-speed junction convergence


class ConflictType(str, Enum):
    """Tactical category of train conflict."""

    OPPOSING_COLLISION = "OPPOSING_COLLISION"   # Reciprocal headings on shared single-line infrastructure
    HEADWAY_VIOLATION = "HEADWAY_VIOLATION"     # Excessive proximity to preceding train in same direction
    CONVERGING_JUNCTION = "CONVERGING_JUNCTION" # Simultaneous approach to converging switch throat
    STATION_MEET = "STATION_MEET"               # Authorized train crossing at station with passing siding


@dataclass(frozen=True)
class ConflictWatchdogConfig:
    """Operational parameters and spatial thresholds for conflict evaluation.

    Attributes:
        opposing_critical_distance_km: Distance threshold in km for critical opposing collision alert.
        opposing_warning_distance_km: Distance threshold in km for warning opposing approach alert.
        headway_critical_distance_km: Distance threshold in km for critical headway violation.
        headway_warning_distance_km: Distance threshold in km for warning headway compression.
        station_siding_radius_m: Radius around station centroid in meters considered passing loop.
        heading_opposing_tolerance_deg: Angular tolerance in degrees around 180° for opposing heading.
        heading_codirectional_tolerance_deg: Angular tolerance in degrees around 0° for co-directional heading.
        min_evaluation_speed_kmh: Minimum train speed in km/h to evaluate kinematic vectors.
        junction_convergence_distance_km: Distance to junction in km for converging approach evaluation.
        junction_eta_window_sec: Difference in ETA in seconds to junction switch throat triggering advisory.
        single_line_working_corridors: Set of corridor IDs currently operating under single-line working.
    """

    opposing_critical_distance_km: float = 10.0
    opposing_warning_distance_km: float = 20.0
    headway_critical_distance_km: float = 1.0
    headway_warning_distance_km: float = 2.5
    station_siding_radius_m: float = 350.0
    heading_opposing_tolerance_deg: float = 35.0
    heading_codirectional_tolerance_deg: float = 40.0
    min_evaluation_speed_kmh: float = 5.0
    junction_convergence_distance_km: float = 5.0
    junction_eta_window_sec: float = 60.0
    single_line_working_corridors: Set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ConflictAlert:
    """Immutable data transfer object representing an active track conflict.

    Attributes:
        alert_id: Deterministic composite identifier (e.g. '1597_1598_OPPOSING').
        train_id_1: Primary train identifier.
        train_number_1: Primary commercial train number.
        train_id_2: Secondary train identifier.
        train_number_2: Secondary commercial train number.
        corridor_id: Corridor identifier (e.g. 'proastiakos-chalkida').
        corridor_name: Human-readable corridor display name.
        conflict_type: Category of conflict (opposing, headway, junction, meet).
        severity: Urgency tier (CRITICAL, WARNING, ADVISORY).
        distance_km: Spatial geodesic separation in kilometers.
        closing_speed_kmh: Mutual closing velocity in km/h.
        time_to_collision_sec: Estimated time to collision in seconds (None if diverging or stationary).
        is_single_track: True if corridor is physically single-track or in single-line working.
        station_proximity_name: Name of nearby station if within passing siding radius.
        description: Operator alert text.
        timestamp: UNIX epoch timestamp when conflict was generated.
    """

    alert_id: str
    train_id_1: str
    train_number_1: str
    train_id_2: str
    train_number_2: str
    corridor_id: str
    corridor_name: str
    conflict_type: ConflictType
    severity: ConflictSeverity
    distance_km: float
    closing_speed_kmh: float
    time_to_collision_sec: Optional[float]
    is_single_track: bool
    station_proximity_name: Optional[str]
    description: str
    timestamp: float


def compute_forward_bearing(p1: GeoPoint, p2: GeoPoint) -> float:
    """Compute spherical forward azimuth bearing in degrees [0.0, 360.0) from p1 to p2.

    Args:
        p1: Origin geographic point.
        p2: Destination geographic point.

    Returns:
        Forward bearing angle in degrees [0.0, 360.0), where 0 is North, 90 is East.
    """
    phi1 = math.radians(p1.lat)
    phi2 = math.radians(p2.lat)
    delta_lambda = math.radians(p2.lng - p1.lng)

    x = math.sin(delta_lambda) * math.cos(phi2)
    y = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    bearing = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    return bearing


def compute_relative_heading_delta(course1: float, course2: float) -> float:
    """Compute normalized angular divergence between two heading vectors in degrees [0.0, 180.0].

    Args:
        course1: Heading angle in degrees for first train.
        course2: Heading angle in degrees for second train.

    Returns:
        Absolute normalized divergence angle in [0.0, 180.0].
    """
    diff = abs(course1 - course2) % 360.0
    return min(diff, 360.0 - diff)


def compute_closing_velocity_kmh(
    p1: GeoPoint,
    speed1_kmh: float,
    course1_deg: float,
    p2: GeoPoint,
    speed2_kmh: float,
    course2_deg: float,
) -> float:
    """Compute mutual closing velocity in km/h between two trains using line-of-sight vector projection.

    A positive value indicates the trains are moving toward each other (distance decreasing).
    A non-positive value indicates the distance between them is constant or expanding.

    Args:
        p1: Location of first train.
        speed1_kmh: Ground velocity of first train in km/h.
        course1_deg: Heading angle of first train in degrees.
        p2: Location of second train.
        speed2_kmh: Ground velocity of second train in km/h.
        course2_deg: Heading angle of second train in degrees.

    Returns:
        Closing velocity in km/h.
    """
    b12 = compute_forward_bearing(p1, p2)
    b21 = compute_forward_bearing(p2, p1)

    alpha1 = compute_relative_heading_delta(course1_deg, b12)
    alpha2 = compute_relative_heading_delta(course2_deg, b21)

    v1_proj = speed1_kmh * math.cos(math.radians(alpha1))
    v2_proj = speed2_kmh * math.cos(math.radians(alpha2))

    return v1_proj + v2_proj


def find_station_passing_meet(
    pos1: Any,
    pos2: Any,
    stations: Optional[Sequence[Any]],
    radius_m: float = 350.0,
) -> Optional[str]:
    """Check if either or both trains are safely dwelling within a station passing siding.

    Args:
        pos1: Position of first train.
        pos2: Position of second train.
        stations: Optional list of station nodes.
        radius_m: Siding / platform tolerance radius in meters.

    Returns:
        Canonical station name if an authorized passing meet is detected; None otherwise.
    """
    if not stations:
        return None

    spd1 = float(getattr(pos1, "speed", 0.0) or 0.0)
    spd2 = float(getattr(pos2, "speed", 0.0) or 0.0)

    # If both trains are in rapid motion (> 15 km/h), this is not an active siding meet
    if spd1 > 15.0 and spd2 > 15.0:
        return None

    pt1 = GeoPoint(lat=float(pos1.lat), lng=float(pos1.lng))
    pt2 = GeoPoint(lat=float(pos2.lat), lng=float(pos2.lng))

    # Check if either train is stopped/dwelling near a station
    if spd1 <= 5.0:
        stn1, d1 = find_nearest_station_aabb(pt1, stations, max_radius_m=radius_m)
        if stn1 is not None:
            return getattr(stn1, "name_en", getattr(stn1, "name_el", getattr(stn1, "id", "Station")))

    if spd2 <= 5.0:
        stn2, d2 = find_nearest_station_aabb(pt2, stations, max_radius_m=radius_m)
        if stn2 is not None:
            return getattr(stn2, "name_en", getattr(stn2, "name_el", getattr(stn2, "id", "Station")))

    # Check if both trains are inside the same station zone
    stn_both1, _ = find_nearest_station_aabb(pt1, stations, max_radius_m=radius_m * 1.5)
    stn_both2, _ = find_nearest_station_aabb(pt2, stations, max_radius_m=radius_m * 1.5)
    if (
        stn_both1 is not None
        and stn_both2 is not None
        and getattr(stn_both1, "id", None) == getattr(stn_both2, "id", None)
    ):
        return getattr(stn_both1, "name_en", getattr(stn_both1, "name_el", "Station"))

    return None


def _evaluate_corridor_pair_conflict(
    t1: Any,
    t2: Any,
    cid: str,
    c_display_name: str,
    is_single: bool,
    cfg: ConflictWatchdogConfig,
    stations: Optional[Sequence[Any]],
    eval_time: float,
) -> Optional[ConflictAlert]:
    """Evaluate kinematic conflict between a single pair of trains on the same corridor."""
    p1 = GeoPoint(lat=float(t1.lat), lng=float(t1.lng))
    p2 = GeoPoint(lat=float(t2.lat), lng=float(t2.lng))
    spd1 = float(getattr(t1, "speed", 0.0) or 0.0)
    spd2 = float(getattr(t2, "speed", 0.0) or 0.0)
    c1_raw = getattr(t1, "course", None)
    c1 = float(c1_raw if c1_raw is not None else getattr(t1, "track_heading", 0.0) or 0.0)
    c2_raw = getattr(t2, "course", None)
    c2 = float(c2_raw if c2_raw is not None else getattr(t2, "track_heading", 0.0) or 0.0)
    t1_id = str(getattr(t1, "id", "t1"))
    t1_no = str(getattr(t1, "train_number", t1_id))
    t2_id = str(getattr(t2, "id", "t2"))
    t2_no = str(getattr(t2, "train_number", t2_id))

    dist_km = haversine_distance_km(p1, p2)
    max_check_dist = max(cfg.opposing_warning_distance_km, cfg.headway_warning_distance_km)
    if dist_km > max_check_dist:
        return None

    heading_delta = compute_relative_heading_delta(c1, c2)
    closing_spd = compute_closing_velocity_kmh(p1, spd1, c1, p2, spd2, c2)

    ttc_sec: Optional[float] = None
    if closing_spd > 1.0:
        ttc_sec = (dist_km / closing_spd) * 3600.0

    meet_stn = find_station_passing_meet(t1, t2, stations, radius_m=cfg.station_siding_radius_m)

    # 1. Opposing Collision Condition
    is_opposing = heading_delta >= (180.0 - cfg.heading_opposing_tolerance_deg)
    if is_opposing and (is_single or cid in cfg.single_line_working_corridors):
        if meet_stn is not None:
            alert_id = f"{min(t1_id, t2_id)}_{max(t1_id, t2_id)}_STATION_MEET"
            return ConflictAlert(
                alert_id=alert_id,
                train_id_1=t1_id,
                train_number_1=t1_no,
                train_id_2=t2_id,
                train_number_2=t2_no,
                corridor_id=cid,
                corridor_name=c_display_name,
                conflict_type=ConflictType.STATION_MEET,
                severity=ConflictSeverity.ADVISORY,
                distance_km=dist_km,
                closing_speed_kmh=closing_spd,
                time_to_collision_sec=ttc_sec,
                is_single_track=is_single,
                station_proximity_name=meet_stn,
                description=f"Passing meet at {meet_stn} ({t1_no} <-> {t2_no}, {dist_km:.1f}km)",
                timestamp=eval_time,
            )
        if closing_spd > 5.0 and dist_km <= cfg.opposing_critical_distance_km:
            alert_id = f"{min(t1_id, t2_id)}_{max(t1_id, t2_id)}_OPPOSING"
            return ConflictAlert(
                alert_id=alert_id,
                train_id_1=t1_id,
                train_number_1=t1_no,
                train_id_2=t2_id,
                train_number_2=t2_no,
                corridor_id=cid,
                corridor_name=c_display_name,
                conflict_type=ConflictType.OPPOSING_COLLISION,
                severity=ConflictSeverity.CRITICAL,
                distance_km=dist_km,
                closing_speed_kmh=closing_spd,
                time_to_collision_sec=ttc_sec,
                is_single_track=is_single,
                station_proximity_name=None,
                description=(
                    f"CRITICAL OPPOSING CONFLICT: {t1_no} <-> {t2_no} on {c_display_name} "
                    f"({dist_km:.1f}km, TTC {int(ttc_sec or 0)}s)"
                ),
                timestamp=eval_time,
            )
        if closing_spd > 5.0 and dist_km <= cfg.opposing_warning_distance_km:
            alert_id = f"{min(t1_id, t2_id)}_{max(t1_id, t2_id)}_OPPOSING"
            return ConflictAlert(
                alert_id=alert_id,
                train_id_1=t1_id,
                train_number_1=t1_no,
                train_id_2=t2_id,
                train_number_2=t2_no,
                corridor_id=cid,
                corridor_name=c_display_name,
                conflict_type=ConflictType.OPPOSING_COLLISION,
                severity=ConflictSeverity.WARNING,
                distance_km=dist_km,
                closing_speed_kmh=closing_spd,
                time_to_collision_sec=ttc_sec,
                is_single_track=is_single,
                station_proximity_name=None,
                description=(
                    f"Opposing approach: {t1_no} <-> {t2_no} on {c_display_name} "
                    f"({dist_km:.1f}km)"
                ),
                timestamp=eval_time,
            )

    # 2. Co-directional Headway Violation Condition
    is_codirectional = heading_delta <= cfg.heading_codirectional_tolerance_deg
    if is_codirectional:
        is_active_motion = closing_spd > 0.0 or spd1 > 1.0 or spd2 > 1.0
        if dist_km <= cfg.headway_critical_distance_km and is_active_motion:
            alert_id = f"{min(t1_id, t2_id)}_{max(t1_id, t2_id)}_HEADWAY"
            return ConflictAlert(
                alert_id=alert_id,
                train_id_1=t1_id,
                train_number_1=t1_no,
                train_id_2=t2_id,
                train_number_2=t2_no,
                corridor_id=cid,
                corridor_name=c_display_name,
                conflict_type=ConflictType.HEADWAY_VIOLATION,
                severity=ConflictSeverity.CRITICAL,
                distance_km=dist_km,
                closing_speed_kmh=closing_spd,
                time_to_collision_sec=ttc_sec,
                is_single_track=is_single,
                station_proximity_name=meet_stn,
                description=(
                    f"CRITICAL HEADWAY BREACH: {t1_no} / {t2_no} on {c_display_name} "
                    f"({dist_km:.2f}km)"
                ),
                timestamp=eval_time,
            )
        if dist_km <= cfg.headway_warning_distance_km and is_active_motion:
            alert_id = f"{min(t1_id, t2_id)}_{max(t1_id, t2_id)}_HEADWAY"
            return ConflictAlert(
                alert_id=alert_id,
                train_id_1=t1_id,
                train_number_1=t1_no,
                train_id_2=t2_id,
                train_number_2=t2_no,
                corridor_id=cid,
                corridor_name=c_display_name,
                conflict_type=ConflictType.HEADWAY_VIOLATION,
                severity=ConflictSeverity.WARNING,
                distance_km=dist_km,
                closing_speed_kmh=closing_spd,
                time_to_collision_sec=ttc_sec,
                is_single_track=is_single,
                station_proximity_name=meet_stn,
                description=(
                    f"Headway compression: {t1_no} / {t2_no} on {c_display_name} "
                    f"({dist_km:.1f}km)"
                ),
                timestamp=eval_time,
            )

    return None


def evaluate_train_conflicts(
    active_trains: Sequence[Any],
    tracks_data: Optional[Dict[str, Any]] = None,
    stations: Optional[Sequence[Any]] = None,
    config: Optional[ConflictWatchdogConfig] = None,
    now: Optional[float] = None,
) -> List[ConflictAlert]:
    """Evaluate pairwise spatial and kinematic conflicts across active trains.

    Identifies:
    1. Opposing collision risks on single-track lines and single-line working zones.
    2. Headway compression and braking curve violations for co-directional trains.
    3. Authorized station passing meets (suppressed to ADVISORY).

    Args:
        active_trains: Collection of active TrainPosition objects.
        tracks_data: Optional track corridors dictionary or parsed JSON.
        stations: Optional collection of station nodes.
        config: Optional configuration thresholds.
        now: Reference timestamp in seconds.

    Returns:
        List of generated ConflictAlert objects ordered by severity (CRITICAL first).
    """
    if len(active_trains) < 2:
        return []

    cfg = config if config is not None else ConflictWatchdogConfig()
    eval_time = now if now is not None else time.time()

    # Pre-parse double_track map from tracks_data if provided
    double_track_map: Dict[str, bool] = {}
    if tracks_data:
        if "corridors" in tracks_data and isinstance(tracks_data["corridors"], list):
            for c in tracks_data["corridors"]:
                if isinstance(c, dict) and "id" in c:
                    double_track_map[c["id"]] = bool(c.get("double_track", False))
        else:
            for cid, obj in tracks_data.items():
                if hasattr(obj, "double_track"):
                    double_track_map[cid] = bool(obj.double_track)

    # 1. Group active trains by corridor
    corridor_buckets: Dict[str, List[Any]] = {}
    for train in active_trains:
        cid = getattr(train, "corridor", None)
        if not cid:
            cid = "pathe-mainline"
        norm_cid = _normalize_corridor_key(str(cid)) or str(cid)
        corridor_buckets.setdefault(norm_cid, []).append(train)

    alerts: List[ConflictAlert] = []

    # 2. Iterate pairwise within each corridor
    for cid, trains in corridor_buckets.items():
        if len(trains) < 2:
            continue

        is_single = False
        if cid in cfg.single_line_working_corridors:
            is_single = True
        elif cid in double_track_map:
            is_single = not double_track_map[cid]
        elif cid in DEFAULT_SINGLE_TRACK_CORRIDORS:
            is_single = True

        c_display_name = CORRIDOR_DISPLAY_NAMES.get(cid, cid.replace("-", " ").title())

        n = len(trains)
        for i in range(n):
            t1 = trains[i]
            for j in range(i + 1, n):
                t2 = trains[j]
                conflict = _evaluate_corridor_pair_conflict(
                    t1, t2, cid, c_display_name, is_single, cfg, stations, eval_time
                )
                if conflict is not None:
                    alerts.append(conflict)

    # Sort alerts: CRITICAL first, then WARNING, then ADVISORY; tie-broken by distance
    severity_order = {
        ConflictSeverity.CRITICAL: 0,
        ConflictSeverity.WARNING: 1,
        ConflictSeverity.ADVISORY: 2,
    }
    alerts.sort(key=lambda a: (severity_order.get(a.severity, 99), a.distance_km))

    return alerts


