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
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union


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
