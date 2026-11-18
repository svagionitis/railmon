"""Unit tests for the TUI geospatial projection and Braille canvas engine."""

import unittest
from tui.geo import (
    BRAILLE_BASE,
    DOT_MASKS,
    GeoPoint,
    BoundingBox,
    Viewport,
    GeoProjector,
    BrailleCanvas,
    GREECE_PRESET,
    PROASTIAKOS_PRESET,
    PATHE_PRESET,
    NORTHERN_GREECE_PRESET,
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
        # 3 deg north vs 3 deg east relative to center (reduces integer quantization noise)
        center = self.viewport.center
        pt_north = GeoPoint(lat=center.lat + 3.0, lng=center.lng)
        pt_east = GeoPoint(lat=center.lat, lng=center.lng + 3.0)

        cx, cy = self.projector.geo_to_subpixel(center)
        nx, ny = self.projector.geo_to_subpixel(pt_north)
        ex, ey = self.projector.geo_to_subpixel(pt_east)

        delta_x = abs(ex - cx)
        delta_y = abs(ny - cy)

        # delta_x = scale_x * cos(39.5) * 3.0
        # delta_y = (scale_x / 2.0) * 3.0
        # Expected ratio delta_x / delta_y = 2.0 * cos(39.5) approx 2.0 * 0.7716 = 1.5432
        observed_ratio = delta_x / delta_y
        expected_ratio = 2.0 * self.projector._cos_phi0
        self.assertAlmostEqual(observed_ratio, expected_ratio, delta=0.05)

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


if __name__ == "__main__":
    unittest.main()
