"""Unit tests for the TUI geospatial projection and Braille canvas engine."""

import math
import os
import unittest
from unittest.mock import patch
from tui.geo import (
    ASCII_BRAILLE_TABLE,
    BRAILLE_BASE,
    DOT_MASKS,
    KM_PER_DEGREE_LAT,
    SCALE_BAR_INTERVALS_KM,
    BoundingBox,
    BrailleCanvas,
    GeoPoint,
    GeoProjector,
    GREECE_PRESET,
    NORTHERN_GREECE_PRESET,
    PATHE_PRESET,
    PROASTIAKOS_PRESET,
    ScaleBarSpec,
    Viewport,
    haversine_distance_km,
    is_utf8_supported,
)


class TestBrailleCanvas(unittest.TestCase):
    """Test suite for BrailleCanvas bitmask manipulation and rasterization."""

    def test_canvas_invalid_dimensions(self):
        """Verify invalid dimensions raise ValueError."""
        with self.assertRaises(ValueError):
            BrailleCanvas(0, 10)
        with self.assertRaises(ValueError):
            BrailleCanvas(10, -1)

    def test_braille_dot_masks(self):
        """Verify each of the 8 dots maps to its expected Unicode Braille pattern."""
        expected_chars = {
            (0, 0): "⠁",  # dot 1
            (0, 1): "⠂",  # dot 2
            (0, 2): "⠄",  # dot 3
            (0, 3): "⡀",  # dot 7
            (1, 0): "⠈",  # dot 4
            (1, 1): "⠐",  # dot 5
            (1, 2): "⠠",  # dot 6
            (1, 3): "⢀",  # dot 8
        }

        for (sub_x, sub_y), expected_char in expected_chars.items():
            canvas = BrailleCanvas(1, 1)
            canvas.set_pixel(sub_x, sub_y)
            rows = canvas.render_rows()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0], expected_char, f"Mismatch for sub-pixel ({sub_x}, {sub_y})")

    def test_all_dots_and_empty(self):
        """Verify blank and completely filled cells match Braille standards."""
        canvas = BrailleCanvas(1, 1)
        self.assertEqual(canvas.render_rows()[0], chr(BRAILLE_BASE))  # '⠀' (empty)

        for sx in range(2):
            for sy in range(4):
                canvas.set_pixel(sx, sy)

        self.assertEqual(canvas.render_rows()[0], chr(BRAILLE_BASE + 0xFF))  # '⣿' (all 8 dots)

        canvas.clear()
        self.assertEqual(canvas.render_rows()[0], chr(BRAILLE_BASE))

    def test_subpixel_get_set_clear(self):
        """Verify setting and clearing individual pixels."""
        canvas = BrailleCanvas(4, 4)
        self.assertFalse(canvas.get_pixel(3, 7))

        canvas.set_pixel(3, 7)
        self.assertTrue(canvas.get_pixel(3, 7))

        canvas.clear_pixel(3, 7)
        self.assertFalse(canvas.get_pixel(3, 7))

    def test_subpixel_out_of_bounds(self):
        """Verify out-of-bounds coordinates are handled safely without raising exceptions."""
        canvas = BrailleCanvas(2, 2)
        canvas.set_pixel(-1, 0)
        canvas.set_pixel(100, 100)
        canvas.clear_pixel(-5, -5)
        self.assertFalse(canvas.get_pixel(-1, 0))
        self.assertFalse(canvas.get_pixel(100, 100))

        # Entire canvas should remain empty
        for row in canvas.render_rows():
            self.assertEqual(row, chr(BRAILLE_BASE) * 2)

    def test_bresenham_line_drawing(self):
        """Verify Bresenham line rasterization for cardinal and diagonal lines."""
        canvas = BrailleCanvas(3, 3)

        # Horizontal line from (0, 0) to (5, 0)
        canvas.draw_line(0, 0, 5, 0)
        for x in range(6):
            self.assertTrue(canvas.get_pixel(x, 0), f"Pixel ({x}, 0) should be set")
        self.assertFalse(canvas.get_pixel(0, 1))

        # Vertical line from (2, 0) to (2, 11)
        canvas.clear()
        canvas.draw_line(2, 0, 2, 11)
        for y in range(12):
            self.assertTrue(canvas.get_pixel(2, y), f"Pixel (2, {y}) should be set")
        self.assertFalse(canvas.get_pixel(3, 5))

        # Diagonal line
        canvas.clear()
        canvas.draw_line(0, 0, 4, 4)
        for i in range(5):
            self.assertTrue(canvas.get_pixel(i, i), f"Diagonal ({i}, {i}) should be set")

    def test_polyline_drawing(self):
        """Verify drawing connected polylines."""
        canvas = BrailleCanvas(5, 5)
        points = [(0, 0), (4, 0), (4, 4)]
        canvas.draw_polyline(points)

        # First segment
        for x in range(5):
            self.assertTrue(canvas.get_pixel(x, 0))
        # Second segment
        for y in range(5):
            self.assertTrue(canvas.get_pixel(4, y))

    def test_render_to_string(self):
        """Verify multi-line string rendering."""
        canvas = BrailleCanvas(2, 2)
        canvas.set_pixel(0, 0)
        canvas.set_pixel(2, 4)
        s = canvas.render_to_string()
        lines = s.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "⠁⠀")
        self.assertEqual(lines[1], "⠀⠁")


class TestGeoProjector(unittest.TestCase):
    """Test suite for geospatial projections and coordinate conversions."""

    def setUp(self):
        self.viewport = Viewport(
            center=GREECE_PRESET.center,
            zoom=1.0,
            width_cells=80,
            height_cells=30,
            aspect_ratio=2.0,
        )
        self.projector = GeoProjector(self.viewport)

    def test_center_projection(self):
        """Verify the viewport center projects exactly to the canvas center."""
        px, py = self.projector.geo_to_subpixel(self.viewport.center)
        expected_px = self.projector.sub_width // 2
        expected_py = self.projector.sub_height // 2

        self.assertEqual(px, expected_px)
        self.assertEqual(py, expected_py)

    def test_projection_round_trip(self):
        """Verify forward and inverse projection accuracy for key Greek railway locations."""
        test_points = [
            ("Athens Central", GeoPoint(lat=37.9926, lng=23.7203)),
            ("SKA Hub", GeoPoint(lat=38.0744, lng=23.7381)),
            ("Larissa", GeoPoint(lat=39.6385, lng=22.4222)),
            ("Thessaloniki", GeoPoint(lat=40.6441, lng=22.9298)),
            ("Patras", GeoPoint(lat=38.2466, lng=21.7346)),
            ("Chalkida", GeoPoint(lat=38.4619, lng=23.5933)),
        ]

        for name, point in test_points:
            px, py = self.projector.geo_to_subpixel(point)
            recovered = self.projector.subpixel_to_geo(px, py)

            # Rounding to integer sub-pixels introduces small quantization error (< 0.05 degrees)
            self.assertAlmostEqual(
                point.lat,
                recovered.lat,
                delta=0.06,
                msg=f"Latitude mismatch for {name}",
            )
            self.assertAlmostEqual(
                point.lng,
                recovered.lng,
                delta=0.06,
                msg=f"Longitude mismatch for {name}",
            )

    def test_aspect_ratio_scaling(self):
        """Verify aspect ratio scaling compensates for non-square terminal cells."""
        self.assertAlmostEqual(self.projector.subpixel_aspect_ratio, 1.0, places=5)

        # 3 deg north vs 3 deg east relative to center (reduces integer quantization noise)
        center = self.viewport.center
        pt_north = GeoPoint(lat=center.lat + 3.0, lng=center.lng)
        pt_east = GeoPoint(lat=center.lat, lng=center.lng + 3.0)

        cx, cy = self.projector.geo_to_subpixel(center)
        nx, ny = self.projector.geo_to_subpixel(pt_north)
        ex, ey = self.projector.geo_to_subpixel(pt_east)

        delta_x = abs(ex - cx)
        delta_y = abs(ny - cy)

        # With aspect_ratio = 2.0, subpixel_aspect_ratio = 2.0 / 2.0 = 1.0 (each 2x4 subpixel is square).
        # delta_x = scale_x * cos(39.5) * 3.0
        # delta_y = (scale_x / 1.0) * 3.0
        # Expected subpixel ratio delta_x / delta_y = cos(39.5) approx 0.7716
        observed_subpixel_ratio = delta_x / delta_y
        expected_subpixel_ratio = self.projector._cos_phi0
        self.assertAlmostEqual(observed_subpixel_ratio, expected_subpixel_ratio, delta=0.03)

        # In character cells (col = px / 2, row = py / 4):
        # delta_col / delta_row = (delta_x / 2) / (delta_y / 4) = 2.0 * (delta_x / delta_y) = 2.0 * cos(39.5)
        observed_cell_ratio = (delta_x / 2.0) / (delta_y / 4.0)
        expected_cell_ratio = 2.0 * self.projector._cos_phi0
        self.assertAlmostEqual(observed_cell_ratio, expected_cell_ratio, delta=0.06)

        # Custom font aspect ratio (2.4 -> subpixel_aspect_ratio = 1.2)
        custom_vp = Viewport(
            center=GREECE_PRESET.center,
            zoom=1.0,
            width_cells=80,
            height_cells=30,
            aspect_ratio=2.4,
        )
        custom_proj = GeoProjector(custom_vp)
        self.assertAlmostEqual(custom_proj.subpixel_aspect_ratio, 1.2, places=5)
        self.assertAlmostEqual(
            custom_proj.get_scale_x() / custom_proj.get_scale_y(),
            1.2,
            places=5,
        )

    def test_national_preset_fits_compact_80x24_viewport(self):
        """Verify GREECE_PRESET fits within both axes at zoom=1.0 on compact 80x24 map panes."""
        compact_vp = Viewport(
            center=GREECE_PRESET.center,
            zoom=1.0,
            width_cells=50,
            height_cells=20,
            aspect_ratio=2.0,
        )
        compact_proj = GeoProjector(compact_vp)

        sw = GeoPoint(lat=GREECE_PRESET.min_lat, lng=GREECE_PRESET.min_lng)
        ne = GeoPoint(lat=GREECE_PRESET.max_lat, lng=GREECE_PRESET.max_lng)

        sw_px, sw_py = compact_proj.geo_to_subpixel(sw)
        ne_px, ne_py = compact_proj.geo_to_subpixel(ne)

        self.assertGreaterEqual(sw_px, 0)
        self.assertLessEqual(ne_px, compact_proj.sub_width)
        self.assertGreaterEqual(ne_py, 0)
        self.assertLessEqual(sw_py, compact_proj.sub_height)

    def test_resolve_track_cell_subpixel_boundary_tiebreak(self):
        """Verify resolve_track_cell resolves 1-subpixel cell boundary rounding onto active track cells."""
        canvas = BrailleCanvas(width_cells=20, height_cells=15)
        # Draw a vertical track line along subpixel px=20 (which belongs to cell col=10, left dot)
        canvas.draw_line(20, 10, 20, 40)

        # Find the geographic coordinate corresponding to (px=19, py=20)
        # px=19 is in cell col=9 (right dot), 1 subpixel left of the drawn line at px=20 (col=10)
        boundary_pt = self.projector.subpixel_to_geo(19, 20)
        px, py = self.projector.geo_to_subpixel(boundary_pt)
        self.assertEqual((px, py), (19, 20))

        # Raw geo_to_cell lands in empty cell (col=9, row=5)
        raw_col, raw_row = self.projector.geo_to_cell(boundary_pt)
        self.assertEqual((raw_col, raw_row), (9, 5))
        self.assertEqual(canvas.buffer[raw_row * canvas.width_cells + raw_col], 0)

        # resolve_track_cell detects the adjacent track dot at px=20 (col=10, row=5) and snaps to it
        resolved_col, resolved_row = self.projector.resolve_track_cell(boundary_pt, canvas)
        self.assertEqual((resolved_col, resolved_row), (10, 5))
        self.assertNotEqual(canvas.buffer[resolved_row * canvas.width_cells + resolved_col], 0)

        # When canvas is None or no track dot is in the 1-subpixel neighborhood, returns primary cell
        self.assertEqual(self.projector.resolve_track_cell(boundary_pt, None), (9, 5))
        far_pt = self.projector.subpixel_to_geo(10, 20)
        self.assertEqual(self.projector.resolve_track_cell(far_pt, canvas), (5, 5))

    def test_visibility_and_cell_mapping(self):
        """Verify visibility check and character cell mapping."""
        center = self.viewport.center
        self.assertTrue(self.projector.is_visible(center))

        col, row = self.projector.geo_to_cell(center)
        self.assertEqual(col, self.viewport.width_cells // 2)
        self.assertEqual(row, self.viewport.height_cells // 2)

        # Far away point (e.g. London)
        london = GeoPoint(lat=51.5074, lng=-0.1278)
        self.assertFalse(self.projector.is_visible(london))

    def test_corridor_presets(self):
        """Verify that predefined corridor bounding boxes contain their primary stations."""
        athens = GeoPoint(lat=37.9926, lng=23.7203)
        airport = GeoPoint(lat=37.9365, lng=23.9452)
        kiato = GeoPoint(lat=38.0125, lng=22.7483)
        thessaloniki = GeoPoint(lat=40.6441, lng=22.9298)

        # All Greece
        self.assertTrue(GREECE_PRESET.contains(athens))
        self.assertTrue(GREECE_PRESET.contains(thessaloniki))
        self.assertTrue(GREECE_PRESET.contains(kiato))

        # Proastiakos
        self.assertTrue(PROASTIAKOS_PRESET.contains(athens))
        self.assertTrue(PROASTIAKOS_PRESET.contains(airport))
        self.assertFalse(PROASTIAKOS_PRESET.contains(thessaloniki))

        # PATHE Mainline
        self.assertTrue(PATHE_PRESET.contains(athens))
        self.assertTrue(PATHE_PRESET.contains(thessaloniki))

        # Northern Greece
        self.assertTrue(NORTHERN_GREECE_PRESET.contains(thessaloniki))
        self.assertFalse(NORTHERN_GREECE_PRESET.contains(athens))

    def test_fit_bounding_box(self):
        """Verify fit_bounding_box centers and adjusts zoom appropriately."""
        self.projector.fit_bounding_box(PROASTIAKOS_PRESET)

        self.assertAlmostEqual(self.viewport.center.lat, PROASTIAKOS_PRESET.center.lat, delta=0.01)
        self.assertAlmostEqual(self.viewport.center.lng, PROASTIAKOS_PRESET.center.lng, delta=0.01)
        # Because Proastiakos span is smaller than Greece span, zoom must be > 1.0
        self.assertGreater(self.viewport.zoom, 1.0)

    def test_haversine_distance_km(self):
        """TC-F22-01: Verify Haversine great-circle distance in kilometers for Greek railway nodes."""
        athens = GeoPoint(lat=37.9926, lng=23.7203)
        ska = GeoPoint(lat=38.0744, lng=23.7381)
        larissa = GeoPoint(lat=39.6385, lng=22.4222)
        thessaloniki = GeoPoint(lat=40.6441, lng=22.9298)

        # 1. Identity distance is 0.0 km
        self.assertAlmostEqual(haversine_distance_km(athens, athens), 0.0, places=9)

        # 2. Symmetry
        d_ath_lar = haversine_distance_km(athens, larissa)
        d_lar_ath = haversine_distance_km(larissa, athens)
        self.assertAlmostEqual(d_ath_lar, d_lar_ath, places=9)

        # 3. 1 degree latitude along a meridian is ~111.195 km (2 * pi * 6371 / 360)
        p0 = GeoPoint(lat=38.0, lng=23.0)
        p1 = GeoPoint(lat=39.0, lng=23.0)
        self.assertAlmostEqual(haversine_distance_km(p0, p1), 111.195, delta=0.05)

        # 4. Known Greek railway distances: Athens -> SKA (~9.23 km), Athens -> Thessaloniki (~302.3 km)
        self.assertAlmostEqual(haversine_distance_km(athens, ska), 9.23, delta=0.15)
        self.assertAlmostEqual(haversine_distance_km(athens, thessaloniki), 302.3, delta=1.5)

    def test_get_km_per_cell_and_conformal_latitude_scaling(self):
        """TC-F34-01 & TC-F34-02: Verify conformal km_per_cell formula, zoom scaling, and latitude variation."""
        self.viewport.center = GeoPoint(lat=39.5, lng=22.5)
        self.viewport.zoom = 1.0
        km_z1 = self.projector.get_km_per_cell()
        expected_z1 = (
            2.0
            / (self.projector.get_scale_x() * math.cos(math.radians(39.5)))
            * KM_PER_DEGREE_LAT
        )
        self.assertAlmostEqual(km_z1, expected_z1, places=6)

        # Doubling zoom halves km_per_cell
        self.viewport.zoom = 2.0
        km_z2 = self.projector.get_km_per_cell()
        self.assertAlmostEqual(km_z2, km_z1 / 2.0, places=6)

        # Panning from Athens (37.9926 deg N) to Northern Greece (41.30 deg N) scales with 1/cos(phi_0)
        self.viewport.zoom = 1.0
        self.viewport.center = GeoPoint(lat=37.9926, lng=23.7203)
        km_athens = self.projector.get_km_per_cell()
        self.viewport.center = GeoPoint(lat=41.30, lng=23.7203)
        km_north = self.projector.get_km_per_cell()
        expected_ratio = math.cos(math.radians(37.9926)) / math.cos(math.radians(41.30))
        self.assertAlmostEqual(km_north / km_athens, expected_ratio, places=6)

        # Polar latitude defensive clamping prevents ZeroDivisionError
        self.viewport.center = GeoPoint(lat=90.0, lng=0.0)
        km_pole = self.projector.get_km_per_cell()
        self.assertTrue(math.isfinite(km_pole))
        self.assertGreater(km_pole, 0.0)

    def test_compute_scale_bar_snapping_across_zoom_range(self):
        """TC-F34-03: Verify compute_scale_bar snaps to distinct standard intervals across 1.25x zoom steps."""
        self.viewport.center = GREECE_PRESET.center
        # Generate consecutive 1.25x zoom steps matching TUIApp.zoom(1.25) / zoom(1/1.25)
        zoom_levels = [1.0 * (1.25 ** k) for k in range(-7, 13)]
        specs = []
        for z in zoom_levels:
            self.viewport.zoom = z
            spec = self.projector.compute_scale_bar(min_cells=12, max_cells=18)
            self.assertIsInstance(spec, ScaleBarSpec)
            self.assertIn(spec.distance_km, SCALE_BAR_INTERVALS_KM)
            self.assertGreaterEqual(spec.width_cells, 12)
            self.assertLessEqual(spec.width_cells, 18)
            self.assertGreater(spec.km_per_cell, 0.0)
            specs.append(spec)

        # Every single 1.25x zoom step across the active range must decrease distance_km
        for prev_spec, next_spec in zip(specs, specs[1:]):
            self.assertGreater(
                prev_spec.distance_km,
                next_spec.distance_km,
                f"Expected distance_km to change on 1.25x zoom step, got {prev_spec} -> {next_spec}",
            )


class TestBrailleCanvasASCIIFallback(unittest.TestCase):
    """Test suite for BrailleCanvas 7-bit ASCII fallback and encoding detection."""

    def test_ascii_braille_table_properties(self):
        """Verify ASCII_BRAILLE_TABLE contains 256 pure 7-bit ASCII entries."""
        self.assertEqual(len(ASCII_BRAILLE_TABLE), 256)
        for i, char in enumerate(ASCII_BRAILLE_TABLE):
            self.assertEqual(len(char), 1)
            self.assertLess(ord(char), 128, f"Non-ASCII character '{char}' at index {i}")

        # Blank mask must be space
        self.assertEqual(ASCII_BRAILLE_TABLE[0], " ")
        # Single dot masks must be '.'
        self.assertEqual(ASCII_BRAILLE_TABLE[0x01], ".")
        self.assertEqual(ASCII_BRAILLE_TABLE[0x08], ".")

    def test_canvas_ascii_mode_rasterization(self):
        """Verify BrailleCanvas rasterizes to pure ASCII when ascii_mode=True."""
        canvas = BrailleCanvas(width_cells=5, height_cells=3, ascii_mode=True)
        # Draw a horizontal line across the middle
        canvas.draw_line(0, 5, 9, 5)

        rows = canvas.render_rows()
        self.assertEqual(len(rows), 3)
        for row in rows:
            self.assertEqual(len(row), 5)
            for ch in row:
                self.assertLess(ord(ch), 128)

        # Middle row should contain track characters ('-' or '+')
        self.assertTrue(any(ch in "-+" for ch in rows[1]))

    def test_canvas_render_rows_override(self):
        """Verify ascii_mode parameter override in render_rows."""
        canvas = BrailleCanvas(width_cells=2, height_cells=2, ascii_mode=False)
        canvas.set_pixel(0, 0)

        # Normal Braille
        braille_rows = canvas.render_rows(ascii_mode=False)
        self.assertEqual(braille_rows[0][0], "⠁")

        # Overridden to ASCII
        ascii_rows = canvas.render_rows(ascii_mode=True)
        self.assertEqual(ascii_rows[0][0], ".")

    def test_is_utf8_supported_detection(self):
        """Verify is_utf8_supported correctly detects stream encoding and overrides."""
        class MockStream:
            def __init__(self, encoding):
                self.encoding = encoding

        # UTF-8 streams
        self.assertTrue(is_utf8_supported(MockStream("utf-8")))
        self.assertTrue(is_utf8_supported(MockStream("UTF8")))

        # Non-UTF-8 streams
        self.assertFalse(is_utf8_supported(MockStream("us-ascii")))
        self.assertFalse(is_utf8_supported(MockStream("ascii")))
        self.assertFalse(is_utf8_supported(MockStream("iso-8859-1")))
        self.assertFalse(is_utf8_supported(MockStream("cp1252")))

        # Environment variable overrides
        with patch.dict(os.environ, {"RAILMON_ASCII": "1"}):
            self.assertFalse(is_utf8_supported(MockStream("utf-8")))

        with patch.dict(os.environ, {"TERM": "ascii"}):
            self.assertFalse(is_utf8_supported(MockStream("utf-8")))


if __name__ == "__main__":
    unittest.main()
