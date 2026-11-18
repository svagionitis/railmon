"""Geospatial projection engine and Braille sub-pixel raster canvas.

Provides mathematical transformations between WGS84 geographic coordinates
and terminal screen space with font aspect ratio compensation, alongside
a 2x4 sub-pixel Braille rasterizer for rendering railway tracks.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Tuple


# Base Unicode code point for Braille Patterns block (U+2800)
BRAILLE_BASE = 0x2800

# Sub-pixel to Braille bitmask lookup table:
# Column 0: sub_x = 0; Column 1: sub_x = 1
# Rows 0..3: sub_y in 0..3
DOT_MASKS: Tuple[Tuple[int, ...], Tuple[int, ...]] = (
    (0x01, 0x02, 0x04, 0x40),  # col 0: dots 1, 2, 3, 7
    (0x08, 0x10, 0x20, 0x80),  # col 1: dots 4, 5, 6, 8
)


@dataclass(frozen=True)
class GeoPoint:
    """Geographic coordinate in decimal degrees (WGS84).

    Attributes:
        lat: Latitude in decimal degrees (e.g. 37.9838 for Athens).
        lng: Longitude in decimal degrees (e.g. 23.7275 for Athens).
    """
    lat: float
    lng: float


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

    def __init__(self, viewport: Viewport, standard_parallel: float = 39.5):
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

    def get_scale_x(self) -> float:
        """Calculate the horizontal scale in sub-pixels per degree of longitude.

        Base scale fits the Greek national bounding box (span ~5.2 deg lng)
        across the canvas width at zoom=1.0.

        Returns:
            Scale factor in sub-pixels per degree longitude.
        """
        base_span_lng = GREECE_PRESET.span_lng
        return (self.sub_width / base_span_lng) * self.viewport.zoom

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
        scale_y = scale_x / self.viewport.aspect_ratio

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
        scale_y = scale_x / self.viewport.aspect_ratio

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
        effective_scale = min(scale_x_needed, scale_y_needed * self.viewport.aspect_ratio)

        base_scale = self.sub_width / GREECE_PRESET.span_lng
        self.viewport.zoom = max(0.2, effective_scale / base_scale)


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

    def __init__(self, width_cells: int, height_cells: int):
        """Initialize the Braille canvas with dimensions in character cells.

        Args:
            width_cells: Terminal columns allocated for the canvas.
            height_cells: Terminal rows allocated for the canvas.
        """
        if width_cells <= 0 or height_cells <= 0:
            raise ValueError("Canvas dimensions must be positive integers.")

        self.width_cells = width_cells
        self.height_cells = height_cells
        self.sub_width = width_cells * 2
        self.sub_height = height_cells * 4
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

    def render_rows(self) -> List[str]:
        """Convert the canvas buffer into a list of terminal row strings.

        Returns:
            List of Unicode strings representing character cells row-by-row.
        """
        rows: List[str] = []
        for r in range(self.height_cells):
            row_chars: List[str] = []
            row_offset = r * self.width_cells
            for c in range(self.width_cells):
                cell_bits = self.buffer[row_offset + c]
                row_chars.append(chr(BRAILLE_BASE + cell_bits))
            rows.append("".join(row_chars))
        return rows

    def render_to_string(self) -> str:
        """Render the complete canvas as a multi-line string joined by newlines.

        Returns:
            Multi-line string representing the rendered Braille grid.
        """
        return "\n".join(self.render_rows())
