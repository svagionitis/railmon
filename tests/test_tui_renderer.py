"""Unit tests for TUI layout geometry, multi-pane rendering, and curses application controller."""

import curses
import time
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition
from tui.app import TUIApp
from tui.geo import BrailleCanvas, GREECE_PRESET, GeoPoint, GeoProjector, Viewport
from tui.mock_engine import StationNode, TrackPolyline, load_stations, load_tracks
from tui.renderer import (
    MIN_COLS,
    MIN_ROWS,
    PAIR_TRAIN_ALERT,
    PAIR_TRAIN_FREIGHT,
    PAIR_TRAIN_HALTED,
    PAIR_TRAIN_ON_TIME,
    PAIR_TRAIN_PROAST,
    PAIR_TRAIN_WARN,
    PaneGeometry,
    StationBoardEntry,
    TUIRenderer,
    _INITIALIZED_COLOR_PAIRS,
    calculate_scroll_offset,
    compute_station_board_entries,
    format_corridor_display_name,
    format_next_station_eta,
    format_progress_bar,
    format_scale_bar,
    format_speed_sparkline,
    format_telemetry_age,
    format_wgs84_coords,
    get_compass_bearing,
    get_delay_color_pair,
    get_heading_glyph,
    get_string_display_width,
    get_train_color_pair,
    normalize_station_key,
    safe_addstr,
    safe_color_pair,
    truncate_to_display_width,
    PAIR_DEFAULT,
    SPARKLINE_BARS_UTF8,
    SPARKLINE_BARS_ASCII,
    TREND_UP_UTF8,
    TREND_DOWN_UTF8,
    TREND_STEADY_UTF8,
    TREND_UP_ASCII,
    TREND_DOWN_ASCII,
    TREND_STEADY_ASCII,
)
from tui.state import (
    BreadcrumbMode,
    ConnectionStatus,
    InspectorMode,
    RosterSortMode,
    StateSnapshot,
    TrainRecord,
    TrainStateStore,
)
from tui.stream_worker import StreamWorker


def create_test_position(
    train_id: str,
    train_number: str = "1597",
    train_type: str = "InterCity",
    speed: float = 100.0,
    delay: float = 0.0,
    course: float = 0.0,
    track_heading: float = 0.0,
    lat: float = 38.0,
    lng: float = 23.7,
    origin: str = "ΑΘΗΝΑ",
    destination: str = "ΘΕΣΣΑΛΟΝΙΚΗ",
    progress: float = 0.5,
    corridor: str = "PATHE",
) -> TrainPosition:
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120010",
        name=train_number,
        train_type=train_type,
        service_type="Passenger",
        status="Κανονική" if speed > 5.0 else "Στάση",
        origin=origin,
        destination=destination,
        next_station="SKA",
        speed=speed,
        course=course,
        track_heading=track_heading,
        lat=lat,
        lng=lng,
        altitude=45.0,
        delay=delay,
        progress=progress,
        chainage_m=12000,
        corridor=corridor,
        distance_to_destination_m=480000,
        timestamp="2026-11-18T12:00:00Z",
        schedule_id="sch-101",
    )


class MockCursesWindow:
    """Mock curses window simulating character grid operations without an interactive TTY."""

    def __init__(self, height: int, width: int):
        self.height = height
        self.width = width
        self.calls: list = []
        self.strings: list = []
        self.erased = False
        self.boxed = False
        self.refreshed = False

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.erased = True
        self.calls.append(("erase",))

    def box(self):
        self.boxed = True
        self.calls.append(("box",))

    def border(self, ls=0, rs=0, ts=0, bs=0, tl=0, tr=0, bl=0, br=0):
        self.boxed = True
        self.calls.append(("border", ls, rs, ts, bs, tl, tr, bl, br))

    def addstr(self, y, x, text, attr=0):
        self.strings.append((y, x, text, attr))
        self.calls.append(("addstr", y, x, text, attr))

    def addch(self, y, x, ch, attr=0):
        self.strings.append((y, x, chr(ch), attr))
        self.calls.append(("addch", y, x, ch, attr))

    def noutrefresh(self):
        self.refreshed = True
        self.calls.append(("noutrefresh",))

    def subwin(self, h, w, y, x):
        return MockCursesWindow(h, w)


class TestPaneGeometry(unittest.TestCase):
    """Test screen geometry partitioning and dimension clamping."""

    def test_pane_geometry_standard_80x24(self):
        """Verify partition bounds for standard 80x24 terminal."""
        geom = PaneGeometry.calculate(24, 80)
        self.assertTrue(geom.is_valid)
        self.assertEqual(geom.header_y, 0)
        self.assertEqual(geom.header_h, 1)
        self.assertEqual(geom.header_w, 80)

        self.assertEqual(geom.status_y, 23)
        self.assertEqual(geom.status_h, 1)
        self.assertEqual(geom.status_w, 80)

        usable_h = 24 - 2  # 22
        self.assertEqual(geom.map_h, usable_h)
        # 80 * 0.65 = 52. Clamped between 48 and (80 - 32 = 48) -> 48 cols
        self.assertEqual(geom.map_w, 48)
        self.assertEqual(geom.roster_w, 32)
        self.assertEqual(geom.inspector_w, 32)

        self.assertEqual(geom.roster_y, 1)
        self.assertEqual(geom.roster_h + geom.inspector_h, usable_h)

    def test_pane_geometry_undersized(self):
        """Terminal smaller than minimum requirements marks geometry invalid."""
        geom1 = PaneGeometry.calculate(20, 80)
        self.assertFalse(geom1.is_valid)

        geom2 = PaneGeometry.calculate(24, 75)
        self.assertFalse(geom2.is_valid)

        geom3 = PaneGeometry.calculate(15, 60)
        self.assertFalse(geom3.is_valid)

    def test_pane_geometry_large_terminal(self):
        """On a 160x50 terminal, layout allocates ~65% width to map."""
        geom = PaneGeometry.calculate(50, 160)
        self.assertTrue(geom.is_valid)
        usable_h = 50 - 2  # 48
        self.assertEqual(geom.map_h, 48)

        # 160 * 0.65 = 104
        self.assertEqual(geom.map_w, 104)
        self.assertEqual(geom.roster_w, 56)
        self.assertEqual(geom.inspector_w, 56)

        self.assertEqual(geom.roster_h + geom.inspector_h, 48)


class TestFormattingAndLookups(unittest.TestCase):
    """Test directional glyphs, progress bars, Unicode widths, and scroll offsets."""

    def test_heading_glyph_octants(self):
        """Verify all 8 compass octants map to the correct Unicode arrow glyphs."""
        self.assertEqual(get_heading_glyph(0.0), "▲")  # North
        self.assertEqual(get_heading_glyph(45.0), "◥")  # NE
        self.assertEqual(get_heading_glyph(90.0), "►")  # East
        self.assertEqual(get_heading_glyph(135.0), "◢")  # SE
        self.assertEqual(get_heading_glyph(180.0), "▼")  # South
        self.assertEqual(get_heading_glyph(225.0), "◣")  # SW
        self.assertEqual(get_heading_glyph(270.0), "◄")  # West
        self.assertEqual(get_heading_glyph(315.0), "◤")  # NW
        self.assertEqual(get_heading_glyph(360.0), "▲")  # North wrap
        self.assertEqual(get_heading_glyph(None), "▲")  # None fallback

    def test_compass_bearing_formatting(self):
        """Verify compass bearing text formatting."""
        self.assertIn("342.5°", get_compass_bearing(342.5))
        self.assertIn("NW", get_compass_bearing(342.5))
        self.assertEqual(get_compass_bearing(None), "N/A")

    def test_delay_color_mapping(self):
        """Verify delay classification thresholds."""
        self.assertEqual(get_delay_color_pair(0.0), PAIR_TRAIN_ON_TIME)
        self.assertEqual(get_delay_color_pair(3.0), PAIR_TRAIN_ON_TIME)
        self.assertEqual(get_delay_color_pair(3.1), PAIR_TRAIN_WARN)
        self.assertEqual(get_delay_color_pair(10.0), PAIR_TRAIN_WARN)
        self.assertEqual(get_delay_color_pair(10.1), PAIR_TRAIN_ALERT)
        self.assertEqual(get_delay_color_pair(25.0), PAIR_TRAIN_ALERT)

    def test_delay_string_formatting(self):
        """Verify positive (+7m), negative (-7m), and on-time (0m) delay formatting."""
        for delay, expected in [(7, "+7m"), (-7, "-7m"), (77, "+77m"), (-77, "-77m"), (0, "0m")]:
            res = f"+{delay}m" if delay > 0 else (f"{delay}m" if delay < 0 else "0m")
            self.assertEqual(res, expected)

    def test_train_color_pair_resolution(self):
        """Verify train record color resolution based on category, speed, and delay."""
        # 1. Halted train
        pos_halted = create_test_position("1", train_number="1597", speed=0.0)
        rec_halted = TrainRecord(position=pos_halted)
        self.assertEqual(get_train_color_pair(rec_halted), PAIR_TRAIN_HALTED)

        # 2. Major delay
        pos_delayed = create_test_position("2", train_number="1598", speed=100.0, delay=15.0)
        rec_delayed = TrainRecord(position=pos_delayed)
        self.assertEqual(get_train_color_pair(rec_delayed), PAIR_TRAIN_ALERT)

        # 3. Minor delay
        pos_warn = create_test_position("3", train_number="1599", speed=100.0, delay=5.0)
        rec_warn = TrainRecord(position=pos_warn)
        self.assertEqual(get_train_color_pair(rec_warn), PAIR_TRAIN_WARN)

        # 4. Proastiakos / Suburban
        pos_proast = create_test_position("4", train_number="2232", train_type="Proastiakos", speed=90.0, delay=0.0)
        rec_proast = TrainRecord(position=pos_proast)
        self.assertEqual(get_train_color_pair(rec_proast), PAIR_TRAIN_PROAST)

        # 5. Freight
        pos_freight = create_test_position("5", train_number="80500", train_type="Freight", speed=70.0, delay=0.0)
        rec_freight = TrainRecord(position=pos_freight)
        self.assertEqual(get_train_color_pair(rec_freight), PAIR_TRAIN_FREIGHT)

    def test_roster_scroll_offset_bounds(self):
        """Verify windowed scrolling offset keeps selected cursor visible."""
        # Case 1: Items fit within visible window
        self.assertEqual(calculate_scroll_offset(selected_index=3, total_items=5, visible_rows=10), 0)

        # Case 2: Selected item near top
        self.assertEqual(calculate_scroll_offset(selected_index=1, total_items=30, visible_rows=10), 0)

        # Case 3: Selected item in middle (should center: 15 - 5 = 10)
        self.assertEqual(calculate_scroll_offset(selected_index=15, total_items=30, visible_rows=10), 10)

        # Case 4: Selected item near bottom (should clamp: 30 - 10 = 20)
        self.assertEqual(calculate_scroll_offset(selected_index=28, total_items=30, visible_rows=10), 20)

    def test_route_progress_bar_string(self):
        """Verify progress meter formatting."""
        p0 = format_progress_bar(0.0, 20)
        self.assertIn("  0%", p0)
        self.assertTrue(p0.startswith("["))

        p50 = format_progress_bar(0.5, 20)
        self.assertIn(" 50%", p50)
        self.assertIn(">", p50)

        p100 = format_progress_bar(1.0, 20)
        self.assertIn("100%", p100)
        self.assertIn("=", p100)

        p_none = format_progress_bar(None, 20)
        self.assertIn("  0%", p_none)

    def test_greek_unicode_display_width(self):
        """Verify Greek and ASCII characters evaluate to correct monospace column widths."""
        self.assertEqual(get_string_display_width("Athens"), 6)
        self.assertEqual(get_string_display_width("Αθήνα"), 5)
        self.assertEqual(get_string_display_width("Θεσσαλονίκη (Thessaloniki)"), 26)

    def test_truncate_to_display_width(self):
        """Verify string truncation with width bounds."""
        s = "Athens Central Station"
        self.assertEqual(truncate_to_display_width(s, 10), "Athens Ce…")
        self.assertEqual(truncate_to_display_width(s, 30), s)

    def test_format_speed_sparkline(self):
        """TC-F21-02: Verify speed sparkline generation, dynamic scaling, and trend arrow calculation."""
        # 1. Empty speeds -> baseline character + steady trend
        spark_empty = format_speed_sparkline([], max_bars=10, ascii_mode=False)
        self.assertEqual(spark_empty, f"{SPARKLINE_BARS_UTF8[0]} {TREND_STEADY_UTF8}")

        spark_empty_ascii = format_speed_sparkline([], max_bars=10, ascii_mode=True)
        self.assertEqual(spark_empty_ascii, f"{SPARKLINE_BARS_ASCII[0]} {TREND_STEADY_ASCII}")

        # 2. Steady speed (delta <= 3.0) -> steady trend arrow
        spark_steady = format_speed_sparkline([100.0, 100.5, 101.0], max_bars=10, ascii_mode=False)
        self.assertTrue(spark_steady.endswith(f" {TREND_STEADY_UTF8}"))

        # 3. Accelerating (delta > +3.0) -> up trend arrow
        spark_accel = format_speed_sparkline([60.0, 90.0, 130.0], max_bars=10, ascii_mode=False)
        self.assertTrue(spark_accel.endswith(f" {TREND_UP_UTF8}"))
        spark_accel_ascii = format_speed_sparkline([60.0, 90.0, 130.0], max_bars=10, ascii_mode=True)
        self.assertTrue(spark_accel_ascii.endswith(f" {TREND_UP_ASCII}"))

        # 4. Decelerating (delta < -3.0) -> down trend arrow
        spark_decel = format_speed_sparkline([140.0, 100.0, 50.0], max_bars=10, ascii_mode=False)
        self.assertTrue(spark_decel.endswith(f" {TREND_DOWN_UTF8}"))
        spark_decel_ascii = format_speed_sparkline([140.0, 100.0, 50.0], max_bars=10, ascii_mode=True)
        self.assertTrue(spark_decel_ascii.endswith(f" {TREND_DOWN_ASCII}"))

        # 5. Speeds exceeding baseline 160.0 km/h -> dynamic ceiling expansion without overflow
        spark_high = format_speed_sparkline([120.0, 160.0, 200.0], max_bars=10, ascii_mode=False)
        self.assertEqual(len(spark_high.split()[0]), 3)
        self.assertEqual(spark_high.split()[0][-1], SPARKLINE_BARS_UTF8[-1])

        # 6. Strict ASCII purity check in ascii_mode
        spark_ascii_all = format_speed_sparkline([0.0, 50.0, 100.0, 150.0], max_bars=10, ascii_mode=True)
        for ch in spark_ascii_all:
            self.assertLess(ord(ch), 128)

    def test_format_telemetry_age(self):
        """TC-F21-03: Verify telemetry age elapsed time calculation and warning color attributes."""
        now = 1000.0

        # 1. Fresh telemetry (<= 30s) -> PAIR_DEFAULT
        str1, pair1 = format_telemetry_age(last_updated=995.0, update_count=42, now=now)
        self.assertEqual(str1, "5s ago (#42)")
        self.assertEqual(pair1, PAIR_DEFAULT)

        # 2. Warning range (31s - 120s) -> PAIR_TRAIN_WARN
        str2, pair2 = format_telemetry_age(last_updated=955.0, update_count=42, now=now)
        self.assertEqual(str2, "45s ago (#42)")
        self.assertEqual(pair2, PAIR_TRAIN_WARN)

        # 3. Warning range with minutes (e.g. 75s)
        str3, pair3 = format_telemetry_age(last_updated=925.0, update_count=42, now=now)
        self.assertEqual(str3, "1m 15s ago (#42)")
        self.assertEqual(pair3, PAIR_TRAIN_WARN)

        # 4. Critical stale threshold (> 120s) -> PAIR_TRAIN_ALERT with [STALE]
        str4, pair4 = format_telemetry_age(last_updated=850.0, update_count=42, now=now)
        self.assertEqual(str4, "2m ago [STALE]")
        self.assertEqual(pair4, PAIR_TRAIN_ALERT)

        # 5. Over 1 hour staleness
        str5, pair5 = format_telemetry_age(last_updated=now - 3700.0, update_count=42, now=now)
        self.assertEqual(str5, "1h ago [STALE]")
        self.assertEqual(pair5, PAIR_TRAIN_ALERT)

        # 6. Negative age guardrail (future timestamp / clock skew)
        str6, pair6 = format_telemetry_age(last_updated=now + 50.0, update_count=1, now=now)
        self.assertEqual(str6, "0s ago (#1)")
        self.assertEqual(pair6, PAIR_DEFAULT)

    def test_format_next_station_eta(self):
        """TC-F22-03..05: Verify next-station geodesic distance and kinematic ETA formatting."""
        larissa_node = StationNode(
            id="LAR",
            code="1080",
            name_el="Λάρισα",
            name_en="Larissa",
            point=GeoPoint(lat=39.6385, lng=22.4222),
            corridor="PATHE",
            is_hub=True,
        )
        # Train ~14.2 km south of Larissa along meridian (14.2 / 111.195 = 0.1277 deg lat)
        train_pt = GeoPoint(lat=39.6385 - 0.1277, lng=22.4222)

        # 1. Moving at 142.0 km/h over 14.2 km -> ~6m ETA
        res_moving = format_next_station_eta(
            "Λάρισα",
            train_point=train_pt,
            station_node=larissa_node,
            speed_kmh=142.0,
        )
        self.assertEqual(res_moving, "Λάρισα (14.2 km, ~6m)")

        # 2. Moving slowly over long distance (>= 120 min) -> ~XhYYm format
        far_pt = GeoPoint(lat=37.9926, lng=23.7203)  # ~195 km from Larissa
        res_long = format_next_station_eta(
            "Larissa",
            train_point=far_pt,
            station_node=larissa_node,
            speed_kmh=80.0,
        )
        self.assertIn("km, ~2h", res_long)

        # 3. Moving right outside station (< 0.1 km) -> <1m
        near_pt = GeoPoint(lat=39.6380, lng=22.4222)
        res_imminent = format_next_station_eta(
            "Λάρισα",
            train_point=near_pt,
            station_node=larissa_node,
            speed_kmh=60.0,
        )
        self.assertEqual(res_imminent, "Λάρισα (0.1 km, <1m)")

        # 4. Halted en route (speed < 5.0 km/h, d >= 0.25 km) -> distance without ETA
        res_halted = format_next_station_eta(
            "Λάρισα",
            train_point=train_pt,
            station_node=larissa_node,
            speed_kmh=0.0,
        )
        self.assertEqual(res_halted, "Λάρισα (14.2 km)")

        # 5. Halted at platform (speed < 5.0 km/h, d < 0.25 km) -> AT indicator
        res_at = format_next_station_eta(
            "Λάρισα",
            train_point=near_pt,
            station_node=larissa_node,
            speed_kmh=0.0,
        )
        self.assertEqual(res_at, "Λάρισα (0.1 km, AT)")

        # 6. Unknown station node or uninitialized (0.0, 0.0) GPS -> raw station name
        self.assertEqual(
            format_next_station_eta("Καλαμάτα", train_point=train_pt, station_node=None, speed_kmh=100.0),
            "Καλαμάτα",
        )
        self.assertEqual(
            format_next_station_eta(
                "Λάρισα",
                train_point=GeoPoint(0.0, 0.0),
                station_node=larissa_node,
                speed_kmh=100.0,
            ),
            "Λάρισα",
        )

        # 7. None or empty station -> 'N/A'
        self.assertEqual(format_next_station_eta(None), "N/A")
        self.assertEqual(format_next_station_eta("   "), "N/A")

    def test_format_wgs84_coords(self):
        """TC-F22-06..07: Verify WGS84 coordinate formatting in UTF-8 and ASCII modes."""
        # 1. Standard Greek railway coordinate in UTF-8 and ASCII modes
        self.assertEqual(
            format_wgs84_coords(39.6384, 22.4192, ascii_mode=False),
            "39.6384°N, 22.4192°E",
        )
        ascii_coords = format_wgs84_coords(39.6384, 22.4192, ascii_mode=True)
        self.assertEqual(ascii_coords, "39.6384d N, 22.4192d E")
        for ch in ascii_coords:
            self.assertLess(ord(ch), 128)

        # 2. Southern and Western hemisphere coordinates
        self.assertEqual(
            format_wgs84_coords(-33.8688, -151.2093, ascii_mode=False),
            "33.8688°S, 151.2093°W",
        )
        self.assertEqual(
            format_wgs84_coords(-33.8688, -151.2093, ascii_mode=True),
            "33.8688d S, 151.2093d W",
        )

        # 3. Uninitialized (0.0, 0.0), None, or NaN coordinates -> 'N/A'
        self.assertEqual(format_wgs84_coords(0.0, 0.0), "N/A")
        self.assertEqual(format_wgs84_coords(None, 23.7), "N/A")
        self.assertEqual(format_wgs84_coords(38.0, None), "N/A")
        self.assertEqual(format_wgs84_coords(float("nan"), 23.7), "N/A")


class TestTUIRendererComponents(unittest.TestCase):
    """Test renderer multi-pane drawing against mock curses windows."""

    def setUp(self):
        self.stations = [
            StationNode(
                id="ATH",
                code="1001",
                name_el="Αθήνα",
                name_en="Athens",
                point=GeoPoint(lat=37.9926, lng=23.7203),
                corridor="PATHE",
                is_hub=True,
            ),
            StationNode(
                id="SKA",
                code="1005",
                name_el="ΣΚΑ",
                name_en="SKA",
                point=GeoPoint(lat=38.0744, lng=23.7381),
                corridor="PATHE",
                is_hub=True,
            ),
            StationNode(
                id="INO",
                code="1015",
                name_el="Οινόη",
                name_en="Inoi",
                point=GeoPoint(lat=38.3194, lng=23.6067),
                corridor="PATHE",
                is_hub=False,
            ),
        ]
        self.tracks = [
            TrackPolyline(
                corridor_id="pathe",
                name="PATHE",
                points=[
                    GeoPoint(lat=37.9926, lng=23.7203),
                    GeoPoint(lat=38.0744, lng=23.7381),
                    GeoPoint(lat=38.3194, lng=23.6067),
                ],
                cumulative_distances_m=[0.0, 10000.0, 30000.0],
                total_length_m=30000.0,
            )
        ]
        self.renderer = TUIRenderer(stations=self.stations, tracks=self.tracks)

        # Create sample train positions and state store
        pos1 = create_test_position(
            train_id="1",
            train_number="1597",
            train_type="InterCity",
            speed=118.0,
            course=340.0,
            track_heading=340.0,
            lat=38.05,
            lng=23.73,
            delay=4.0,
            origin="ΑΘΗΝΑ",
            destination="ΘΕΣΣΑΛΟΝΙΚΗ",
            progress=0.62,
        )
        pos2 = create_test_position(
            train_id="2",
            train_number="2232",
            train_type="Proastiakos",
            speed=90.0,
            course=180.0,
            lat=38.10,
            lng=23.74,
            delay=0.0,
        )
        self.store = TrainStateStore()
        self.store.update_positions([pos1, pos2])
        self.store.select_next()  # Select train 1597

    def test_render_undersized_banner(self):
        """Verify undersized window draws warning box without errors."""
        win = MockCursesWindow(20, 60)
        self.renderer.render_undersized(win, 20, 60)
        self.assertTrue(win.erased)
        self.assertTrue(win.boxed)
        rendered_texts = " ".join(s[2] for s in win.strings)
        self.assertIn("TERMINAL WINDOW TOO SMALL", rendered_texts)
        self.assertIn(str(MIN_COLS), rendered_texts)
        self.assertIn(str(MIN_ROWS), rendered_texts)

    def test_render_header(self):
        """Verify header renders title, corridor, counts, and clock."""
        win = MockCursesWindow(1, 100)
        snapshot = self.store.get_snapshot()
        self.renderer.render_header(win, snapshot, 100)
        rendered_texts = " ".join(s[2] for s in win.strings)
        self.assertIn("HELLENIC RAILWAYS", rendered_texts)
        self.assertIn("Active: 2", rendered_texts)
        self.assertIn("Delayed: 1", rendered_texts)

    def test_render_map(self):
        """Verify map viewport renders box, tracks, station markers, and train arrows."""
        win = MockCursesWindow(25, 70)
        snapshot = self.store.get_snapshot()
        viewport = Viewport(
            center=GeoPoint(lat=38.05, lng=23.73),
            zoom=1.5,
            width_cells=68,
            height_cells=23,
            aspect_ratio=2.0,
        )
        projector = GeoProjector(viewport)

        self.renderer.render_map(win, snapshot, projector)
        self.assertTrue(win.boxed)
        rendered_texts = " ".join(s[2] for s in win.strings)
        self.assertIn("Map Viewport:", rendered_texts)
        # Station markers
        self.assertTrue(any("◉" in s[2] for s in win.strings))
        # Directional train glyphs or selected train tag
        self.assertIn("1597", rendered_texts)

    def test_render_roster(self):
        """Verify roster displays table header and train row entries."""
        win = MockCursesWindow(15, 35)
        snapshot = self.store.get_snapshot()
        self.renderer.render_roster(win, snapshot)
        self.assertTrue(win.boxed)
        rendered_texts = " ".join(s[2] for s in win.strings)
        self.assertIn("TRAIN", rendered_texts)
        self.assertIn("1597", rendered_texts)
        self.assertIn("2232", rendered_texts)
        # Check cursor on selected train
        self.assertTrue(any(">" in s[2] for s in win.strings))

    def test_render_inspector(self):
        """Verify telemetry inspector details selected train attributes."""
        win = MockCursesWindow(12, 35)
        snapshot = self.store.get_snapshot()
        self.renderer.render_inspector(win, snapshot)
        self.assertTrue(win.boxed)
        rendered_texts = " ".join(s[2] for s in win.strings)
        self.assertIn("1597", rendered_texts)
        self.assertIn("Speed:", rendered_texts)
        self.assertIn("Delay:", rendered_texts)
        self.assertIn("Progress:", rendered_texts)
        self.assertIn("Heading:", rendered_texts)

    def test_render_inspector_empty_selection(self):
        """Verify inspector renders empty state message when no train is selected."""
        win = MockCursesWindow(12, 35)
        self.store.clear_selection()
        snapshot = self.store.get_snapshot()
        self.renderer.render_inspector(win, snapshot)
        rendered_texts = " ".join(s[2] for s in win.strings)
        self.assertIn("No train selected", rendered_texts)

    def test_render_inspector_live_sparkline_and_telemetry_age(self):
        """TC-F21-04: Verify inspector pane renders Speed Trend and Telemetry rows with proper attributes."""
        win = MockCursesWindow(14, 40)
        snapshot = self.store.get_snapshot()
        self.renderer.render_inspector(win, snapshot)

        rendered_texts = " ".join(s[2] for s in win.strings)
        self.assertIn("Speed Trend:", rendered_texts)
        self.assertIn("Telemetry:", rendered_texts)
        self.assertIn("ago", rendered_texts)

        # Stale train triggers PAIR_TRAIN_ALERT on the Telemetry row
        selected = snapshot.selected_train
        assert selected is not None
        self.store._trains[selected.position.id].last_updated = time.time() - 300.0
        snapshot_stale = self.store.get_snapshot()

        win_stale = MockCursesWindow(14, 40)
        self.renderer.render_inspector(win_stale, snapshot_stale)
        stale_texts = " ".join(s[2] for s in win_stale.strings)
        self.assertIn("[STALE]", stale_texts)

    def test_render_status_bar(self):
        """Verify status bar displays keybindings and connection badge."""
        win = MockCursesWindow(1, 100)
        snapshot = self.store.get_snapshot()
        self.renderer.render_status_bar(win, snapshot, 100, msg_rate=3.5)
        rendered_texts = " ".join(s[2] for s in win.strings)
        self.assertIn("[q] Quit", rendered_texts)
        self.assertIn("DEMO MODE", rendered_texts)

    def test_station_label_collision_suppression(self):
        """Verify overlapping stations suppress secondary labels while preserving hub station labels."""
        hub = StationNode(
            id="HUB",
            code="1000",
            name_el="Κεντρικός",
            name_en="Hub Central",
            point=GeoPoint(lat=38.0, lng=23.7),
            corridor="PATHE",
            is_hub=True,
        )
        secondary = StationNode(
            id="SEC",
            code="1001",
            name_el="Δευτερεύων",
            name_en="Secondary Close",
            point=GeoPoint(lat=38.0, lng=23.7001),  # Virtually identical coordinates
            corridor="PATHE",
            is_hub=False,
        )
        renderer = TUIRenderer(stations=[secondary, hub], tracks=[])
        win = MockCursesWindow(20, 60)
        viewport = Viewport(center=GeoPoint(lat=38.0, lng=23.7), zoom=1.0, width_cells=58, height_cells=18)
        projector = GeoProjector(viewport)

        renderer.render_map(win, self.store.get_snapshot(), projector)
        rendered_texts = " ".join(s[2] for s in win.strings)

        # Hub label must be drawn
        self.assertIn("Hub Central", rendered_texts)
        # Secondary overlapping label must be suppressed
        self.assertNotIn("Secondary Close", rendered_texts)

    def test_render_all_panes(self):
        """Verify render_all dispatches to all subwindows in geometry."""
        geom = PaneGeometry.calculate(30, 100)
        windows = {
            "root": MockCursesWindow(30, 100),
            "header": MockCursesWindow(geom.header_h, geom.header_w),
            "map": MockCursesWindow(geom.map_h, geom.map_w),
            "roster": MockCursesWindow(geom.roster_h, geom.roster_w),
            "inspector": MockCursesWindow(geom.inspector_h, geom.inspector_w),
            "status": MockCursesWindow(geom.status_h, geom.status_w),
        }
        snapshot = self.store.get_snapshot()
        viewport = Viewport(center=GeoPoint(lat=38.0, lng=23.7), width_cells=geom.map_w - 2, height_cells=geom.map_h - 2)
        projector = GeoProjector(viewport)

        self.renderer.render_all(windows, geom, snapshot, projector, msg_rate=2.0)
        self.assertTrue(windows["header"].erased)
        self.assertTrue(windows["map"].boxed)
        self.assertTrue(windows["roster"].boxed)
        self.assertTrue(windows["inspector"].boxed)


class TestTUIAppLifecycle(unittest.TestCase):
    """Test TUIApp initialization, input handling, and terminal management."""

    def setUp(self):
        self.root_win = MockCursesWindow(30, 100)
        self.store = TrainStateStore()
        self.worker = MagicMock(spec=StreamWorker)
        self.app = TUIApp(
            stdscr=self.root_win,
            store=self.store,
            worker=self.worker,
            stations=[],
            tracks=[],
            target_fps=15.0,
        )

    def test_app_rebuild_windows(self):
        """Verify subwindow allocation on valid dimensions."""
        self.app.rebuild_windows()
        self.assertTrue(self.app.geometry.is_valid)
        self.assertIn("header", self.app.windows)
        self.assertIn("map", self.app.windows)
        self.assertIn("roster", self.app.windows)
        self.assertIn("inspector", self.app.windows)
        self.assertIn("status", self.app.windows)

    def test_app_input_processing(self):
        """Verify exit key triggers return False, and KEY_RESIZE triggers rebuild."""
        self.assertFalse(self.app.process_input(ord("q")))
        self.assertFalse(self.app.process_input(ord("Q")))
        self.assertFalse(self.app.process_input(27))  # ESC
        self.assertFalse(self.app.process_input(3))   # Ctrl+C

        # Resize key
        with patch.object(self.app, "handle_resize") as mock_resize:
            res = self.app.process_input(curses.KEY_RESIZE)
            self.assertTrue(res)
            mock_resize.assert_called_once()

    def test_app_message_rate_calculation(self):
        """Verify message rate calculation with time progression."""
        self.app._last_rate_time = 100.0
        self.app._last_msg_count = 10

        with patch("time.monotonic", return_value=102.0):  # 2.0s later
            self.app._update_message_rate(total_messages=20)  # +10 messages
            self.assertEqual(self.app._current_msg_rate, 5.0)  # 10 / 2 = 5 msg/s


class TestRendererSafeColorPairsAndASCIIFallback(unittest.TestCase):
    """Test suite for guarded init_pair color initialization and 7-bit ASCII rendering."""

    def setUp(self):
        _INITIALIZED_COLOR_PAIRS.clear()
        self.stations = [
            StationNode(
                id="ATH",
                code="1001",
                name_el="Athens",
                name_en="Athens",
                point=GeoPoint(lat=37.9926, lng=23.7203),
                corridor="PATHE",
                is_hub=True,
            ),
            StationNode(
                id="SKA",
                code="1005",
                name_el="SKA",
                name_en="SKA",
                point=GeoPoint(lat=38.0744, lng=23.7381),
                corridor="PATHE",
                is_hub=False,
            ),
        ]
        self.tracks = [
            TrackPolyline(
                corridor_id="pathe",
                name="PATHE",
                points=[
                    GeoPoint(lat=37.9926, lng=23.7203),
                    GeoPoint(lat=38.0744, lng=23.7381),
                ],
                cumulative_distances_m=[0.0, 10000.0],
                total_length_m=10000.0,
            )
        ]
        self.renderer = TUIRenderer(
            stations=self.stations,
            tracks=self.tracks,
            ascii_mode=True,
        )

    def test_init_colors_has_colors_false(self):
        """Verify init_colors safely terminates if curses reports no color support."""
        with patch("curses.has_colors", return_value=False):
            with patch("curses.start_color") as mock_start:
                self.renderer.init_colors()
                mock_start.assert_not_called()
                self.assertEqual(len(_INITIALIZED_COLOR_PAIRS), 0)

    def test_init_colors_start_color_raises_curses_error(self):
        """Verify init_colors catches curses.error on start_color."""
        with patch("curses.has_colors", return_value=True):
            with patch("curses.start_color", side_effect=curses.error("start_color failed")):
                self.renderer.init_colors()
                self.assertEqual(len(_INITIALIZED_COLOR_PAIRS), 0)

    def test_init_colors_partial_init_pair_failure(self):
        """Verify init_colors records successful pairs and silently degrades on init_pair errors."""
        def mock_init_pair(pair_idx, fg, bg):
            if pair_idx == 4:
                raise curses.error("Color pair 4 failed")

        with patch("curses.has_colors", return_value=True), \
             patch("curses.start_color"), \
             patch("curses.use_default_colors"), \
             patch("curses.init_pair", side_effect=mock_init_pair), \
             patch("curses.COLOR_PAIRS", 10, create=True):
            self.renderer.init_colors()
            # Pair 4 failed, so it must not be in _INITIALIZED_COLOR_PAIRS
            self.assertNotIn(4, _INITIALIZED_COLOR_PAIRS)
            # Other pairs < 10 must succeed
            self.assertIn(1, _INITIALIZED_COLOR_PAIRS)
            self.assertIn(2, _INITIALIZED_COLOR_PAIRS)
            self.assertIn(3, _INITIALIZED_COLOR_PAIRS)
            # Pairs >= 10 must be skipped
            self.assertNotIn(10, _INITIALIZED_COLOR_PAIRS)
            self.assertNotIn(16, _INITIALIZED_COLOR_PAIRS)

    def test_safe_color_pair_behavior(self):
        """Verify safe_color_pair only returns curses.color_pair when pair was allocated."""
        _INITIALIZED_COLOR_PAIRS.clear()
        _INITIALIZED_COLOR_PAIRS.add(1)

        with patch("curses.has_colors", return_value=True):
            with patch("curses.color_pair", return_value=100) as mock_cp:
                # Pair 1 is initialized
                res1 = safe_color_pair(1, fallback_attr=curses.A_BOLD)
                self.assertEqual(res1, 100)
                mock_cp.assert_called_with(1)

                # Pair 2 is not initialized
                res2 = safe_color_pair(2, fallback_attr=curses.A_BOLD)
                self.assertEqual(res2, curses.A_BOLD)

    def test_get_heading_glyph_and_compass_bearing_ascii(self):
        """Verify heading glyphs and compass bearing format in pure 7-bit ASCII."""
        self.assertEqual(get_heading_glyph(0.0, ascii_mode=True), "^")
        self.assertEqual(get_heading_glyph(90.0, ascii_mode=True), ">")
        self.assertEqual(get_heading_glyph(180.0, ascii_mode=True), "v")
        self.assertEqual(get_heading_glyph(270.0, ascii_mode=True), "<")

        compass = get_compass_bearing(45.0, ascii_mode=True)
        self.assertIn("d", compass)
        self.assertNotIn("°", compass)
        self.assertTrue(all(ord(c) < 128 for c in compass))

    def test_renderer_ascii_mode_no_high_order_characters(self):
        """Verify that all UI panes render exclusively pure 7-bit ASCII characters when ascii_mode=True."""
        pos = create_test_position(
            train_id="1",
            train_number="1597",
            train_type="InterCity",
            speed=95.0,
            delay=2.0,
            origin="Athens",
            destination="Thessaloniki",
            lat=38.0,
            lng=23.7,
        )
        record = TrainRecord(position=pos)
        store = TrainStateStore()
        store.update_positions([pos])
        store.set_connection_status(ConnectionStatus.CONNECTED, "Live feed active")
        snapshot = store.get_snapshot()

        viewport = Viewport(
            center=GeoPoint(lat=38.0, lng=23.7),
            zoom=1.0,
            width_cells=40,
            height_cells=15,
            aspect_ratio=2.0,
        )
        projector = GeoProjector(viewport)

        # 1. Header
        win_hdr = MockCursesWindow(1, 80)
        self.renderer.render_header(win_hdr, snapshot, width=80)
        for _, _, text, _ in win_hdr.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' (ord {ord(ch)}) in header")

        # 2. Map
        win_map = MockCursesWindow(15, 40)
        self.renderer.render_map(win_map, snapshot, projector)
        for _, _, text, _ in win_map.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' (ord {ord(ch)}) in map")

        # 3. Roster
        win_roster = MockCursesWindow(10, 40)
        self.renderer.render_roster(win_roster, snapshot)
        for _, _, text, _ in win_roster.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' (ord {ord(ch)}) in roster")

        # 4. Inspector
        win_insp = MockCursesWindow(12, 40)
        self.renderer.render_inspector(win_insp, snapshot)
        for _, _, text, _ in win_insp.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' (ord {ord(ch)}) in inspector")

        # 5. Status bar (both normal and search modes)
        win_stat = MockCursesWindow(1, 80)
        self.renderer.render_status_bar(win_stat, snapshot, width=80, msg_rate=12.5)
        for _, _, text, _ in win_stat.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' (ord {ord(ch)}) in status bar")

        win_stat_search = MockCursesWindow(1, 80)
        self.renderer.render_status_bar(win_stat_search, snapshot, width=80, search_mode=True, search_buffer="IC")
        for _, _, text, _ in win_stat_search.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' (ord {ord(ch)}) in search bar")

        # 6. Undersized warning banner
        win_small = MockCursesWindow(15, 50)
        self.renderer.render_undersized(win_small, height=15, width=50)
        for _, _, text, _ in win_small.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' (ord {ord(ch)}) in undersized banner")

        # 7. Help dialog
        win_help = MockCursesWindow(24, 80)
        self.renderer.render_help_modal(win_help, height=24, width=80)
        for _, _, text, _ in win_help.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' (ord {ord(ch)}) in help dialog")


class TestHighZoomTrainTrackAlignment(unittest.TestCase):
    """Test high-zoom train, breadcrumb, and station alignment with Braille track polylines."""

    def setUp(self):
        self.stations = list(load_stations().values())
        self.tracks = list(load_tracks().values())
        self.renderer = TUIRenderer(stations=self.stations, tracks=self.tracks)

    def _build_track_canvas(self, projector: GeoProjector, inner_w: int, inner_h: int) -> BrailleCanvas:
        canvas = BrailleCanvas(width_cells=inner_w, height_cells=inner_h)
        for track in self.tracks:
            sub_points = [projector.geo_to_subpixel(pt) for pt in track.points]
            canvas.draw_polyline(sub_points)
        return canvas

    def test_zoom_9_3x_eleonas_and_afidnes_train_track_alignment(self):
        """Verify live trains near Eleonas (Case Study A) and Afidnes (Case Study B) land on active Braille track cells across high zoom scales."""
        win_h, win_w = 30, 80
        inner_h, inner_w = win_h - 2, win_w - 2

        # Case Study A: IC 56 near Eleonas (Boeotia loop) with ~95m lateral GPS offset
        pos_eleonas = create_test_position(
            train_id="IC56",
            train_number="56",
            train_type="InterCity",
            speed=145.0,
            course=300.0,
            track_heading=300.0,
            lat=38.3755,
            lng=23.4640,
            corridor="PATHE",
        )
        # Case Study B: Suburban 1520 near Afidnes (Northern Attica curve) with ~75m lateral GPS offset
        pos_afidnes = create_test_position(
            train_id="PR1520",
            train_number="1520",
            train_type="Proastiakos",
            speed=105.0,
            course=340.0,
            track_heading=340.0,
            lat=38.1882,
            lng=23.8452,
            corridor="PATHE",
        )

        test_cases = [
            (9.3, GeoPoint(lat=38.28, lng=23.65), [pos_eleonas, pos_afidnes]),
            (15.0, GeoPoint(lat=38.3747, lng=23.4635), [pos_eleonas]),
            (25.0, GeoPoint(lat=38.1877, lng=23.8446), [pos_afidnes]),
        ]

        for zoom_level, center_pt, active_positions in test_cases:
            store = TrainStateStore()
            store.update_positions(active_positions)
            snapshot = store.get_snapshot()

            viewport = Viewport(
                center=center_pt,
                zoom=zoom_level,
                width_cells=inner_w,
                height_cells=inner_h,
                aspect_ratio=2.0,
            )
            projector = GeoProjector(viewport)
            canvas = self._build_track_canvas(projector, inner_w, inner_h)

            win = MockCursesWindow(win_h, win_w)
            self.renderer.render_map(win, snapshot, projector)

            # Extract all single-character heading glyphs drawn on the map window
            drawn_train_cells = [
                (x - 1, y - 1, text)
                for (y, x, text, _) in win.strings
                if text in ("▲", "◥", "►", "◢", "▼", "◣", "◄", "◤")
            ]
            self.assertEqual(
                len(drawn_train_cells),
                len(active_positions),
                f"Expected {len(active_positions)} train glyphs at zoom {zoom_level}x",
            )

            for col, row, glyph in drawn_train_cells:
                self.assertTrue(0 <= col < inner_w and 0 <= row < inner_h)
                track_mask = canvas.buffer[row * inner_w + col]
                self.assertNotEqual(
                    track_mask,
                    0,
                    f"Train glyph '{glyph}' at cell (col={col}, row={row}) at zoom {zoom_level}x "
                    f"landed on an empty cell with 0 Braille track dots!",
                )

    def test_breadcrumb_trail_snaps_to_active_braille_track_cells(self):
        """Verify historical breadcrumb trails snap to active Braille track cells at high zoom."""
        win_h, win_w = 28, 76
        inner_h, inner_w = win_h - 2, win_w - 2

        store = TrainStateStore()
        # Simulate train progression along Tanagra -> Eleonas curve with slight lateral GPS jitter
        steps = [
            (38.3406, 23.5818),  # Near Tanagra (~75m offset)
            (38.3536, 23.5484),  # Mid-segment (~75m offset)
            (38.3672, 23.5085),  # Asopos valley (~85m offset)
            (38.3753, 23.4639),  # Near Eleonas (~75m offset)
        ]
        for lat, lng in steps:
            store.update_positions(
                [
                    create_test_position(
                        train_id="IC56",
                        train_number="56",
                        lat=lat,
                        lng=lng,
                        corridor="PATHE",
                    )
                ]
            )
        snapshot = store.get_snapshot()

        viewport = Viewport(
            center=GeoPoint(lat=38.36, lng=23.52),
            zoom=12.0,
            width_cells=inner_w,
            height_cells=inner_h,
            aspect_ratio=2.0,
        )
        projector = GeoProjector(viewport)
        canvas = self._build_track_canvas(projector, inner_w, inner_h)

        win = MockCursesWindow(win_h, win_w)
        self.renderer.render_map(win, snapshot, projector)

        trail_cells = [
            (x - 1, y - 1)
            for (y, x, text, _) in win.strings
            if text == self.renderer.glyphs.trail_dot
        ]
        self.assertGreaterEqual(len(trail_cells), 3)
        for col, row in trail_cells:
            self.assertNotEqual(
                canvas.buffer[row * inner_w + col],
                0,
                f"Breadcrumb at (col={col}, row={row}) did not snap to an active Braille track cell",
            )

    def test_off_corridor_train_beyond_threshold_renders_at_raw_cell_and_store_unmodified(self):
        """Verify trains >1500m from any corridor render at raw coordinates and TrainStateStore raw coordinates are never mutated."""
        win_h, win_w = 26, 70
        inner_h, inner_w = win_h - 2, win_w - 2

        raw_on_corridor_lat = 38.3755
        raw_on_corridor_lng = 23.4640
        pos_on_corridor = create_test_position(
            train_id="ON_TRACK",
            train_number="56",
            lat=raw_on_corridor_lat,
            lng=raw_on_corridor_lng,
            course=0.0,
            track_heading=0.0,
            corridor="PATHE",
        )

        # Off-corridor point in eastern Attica / Aegean (>25 km away from any track)
        off_lat = 38.2500
        off_lng = 24.1000
        pos_off_corridor = create_test_position(
            train_id="OFF_TRACK",
            train_number="9999",
            lat=off_lat,
            lng=off_lng,
            course=90.0,
            track_heading=90.0,
            corridor="UNKNOWN",
        )

        store = TrainStateStore()
        store.update_positions([pos_on_corridor, pos_off_corridor])
        snapshot = store.get_snapshot()

        viewport = Viewport(
            center=GeoPoint(lat=38.30, lng=23.80),
            zoom=4.0,
            width_cells=inner_w,
            height_cells=inner_h,
            aspect_ratio=2.0,
        )
        projector = GeoProjector(viewport)

        win = MockCursesWindow(win_h, win_w)
        self.renderer.render_map(win, snapshot, projector)

        # 1. Off-corridor train (east arrow '►') must render at exact unsnapped geo_to_cell
        expected_off_col, expected_off_row = projector.geo_to_cell(
            GeoPoint(lat=off_lat, lng=off_lng)
        )
        east_arrows = [
            (x - 1, y - 1) for (y, x, text, _) in win.strings if text == "►"
        ]
        self.assertIn((expected_off_col, expected_off_row), east_arrows)

        # 2. Domain store raw coordinates for the snapped on-corridor train must remain untouched
        after_snapshot = store.get_snapshot()
        stored_pos = after_snapshot.trains["ON_TRACK"].position
        self.assertEqual(stored_pos.lat, raw_on_corridor_lat)
        self.assertEqual(stored_pos.lng, raw_on_corridor_lng)

    def test_render_roster_displays_sort_direction_indicators_utf8_and_ascii(self):
        """Verify roster column header reflects active RosterSortMode in both UTF-8 and ASCII modes."""
        store = TrainStateStore()
        store.update_positions(
            [
                create_test_position("t-1", "1597", speed=110.0, delay=5.0),
                create_test_position("t-2", "IC50", speed=140.0, delay=0.0),
            ]
        )

        utf8_renderer = TUIRenderer(ascii_mode=False)
        ascii_renderer = TUIRenderer(ascii_mode=True)

        # 1. TRAIN_ID sort mode
        snap_id = store.get_snapshot()
        win_utf8 = MockCursesWindow(12, 36)
        win_ascii = MockCursesWindow(12, 36)
        utf8_renderer.render_roster(win_utf8, snap_id)
        ascii_renderer.render_roster(win_ascii, snap_id)
        self.assertTrue(any("TRAIN▲" in text for (_, _, text, _) in win_utf8.strings))
        self.assertTrue(any("TRAIN^" in text for (_, _, text, _) in win_ascii.strings))

        # 2. SPEED sort mode
        store.set_sort_mode(RosterSortMode.SPEED)
        snap_spd = store.get_snapshot()
        win_utf8 = MockCursesWindow(12, 36)
        win_ascii = MockCursesWindow(12, 36)
        utf8_renderer.render_roster(win_utf8, snap_spd)
        ascii_renderer.render_roster(win_ascii, snap_spd)
        self.assertTrue(any("SPD▼" in text for (_, _, text, _) in win_utf8.strings))
        self.assertTrue(any("SPDv" in text for (_, _, text, _) in win_ascii.strings))

        # 3. DELAY sort mode
        store.set_sort_mode(RosterSortMode.DELAY)
        snap_dly = store.get_snapshot()
        win_utf8 = MockCursesWindow(12, 36)
        win_ascii = MockCursesWindow(12, 36)
        utf8_renderer.render_roster(win_utf8, snap_dly)
        ascii_renderer.render_roster(win_ascii, snap_dly)
        self.assertTrue(any("DLY▼" in text for (_, _, text, _) in win_utf8.strings))
        self.assertTrue(any("DLYv" in text for (_, _, text, _) in win_ascii.strings))

    def test_render_map_breadcrumb_modes_all_selected_off_and_filter_exclusion(self):
        """Verify render_map honors BreadcrumbMode (ALL, SELECTED, OFF) and excludes filtered-out trains."""
        win_h, win_w = 24, 80
        inner_h, inner_w = win_h - 2, win_w - 2

        renderer = TUIRenderer(stations=[], tracks=[])
        viewport = Viewport(
            center=GeoPoint(lat=38.0, lng=23.7),
            zoom=4.0,
            width_cells=inner_w,
            height_cells=inner_h,
            aspect_ratio=2.0,
        )
        projector = GeoProjector(viewport)

        # Two trains at well-separated coordinates inside the viewport
        t1_pos = create_test_position("t1", "IC50", lat=37.90, lng=23.60, corridor="PATHE")
        t2_pos = create_test_position("t2", "2232", lat=38.10, lng=23.80, corridor="PROASTIAKOS")

        store = TrainStateStore()
        store.update_positions([t1_pos, t2_pos])

        t1_col, t1_row = projector.geo_to_cell(GeoPoint(lat=37.90, lng=23.60))
        t2_col, t2_row = projector.geo_to_cell(GeoPoint(lat=38.10, lng=23.80))
        self.assertNotEqual((t1_col, t1_row), (t2_col, t2_row))

        # 1. BreadcrumbMode.ALL: both train trail dots are rendered and title shows Trails: ALL
        win_all = MockCursesWindow(win_h, win_w)
        renderer.render_map(win_all, store.get_snapshot(), projector)
        all_texts = " ".join(text for (_, _, text, _) in win_all.strings)
        self.assertIn("Trails: ALL", all_texts)
        all_trail_cells = {
            (x - 1, y - 1)
            for (y, x, text, _) in win_all.strings
            if text == renderer.glyphs.trail_dot
        }
        self.assertIn((t1_col, t1_row), all_trail_cells)
        self.assertIn((t2_col, t2_row), all_trail_cells)

        # 2. BreadcrumbMode.ALL with corridor filter excluding t2: only t1 trail is rendered
        store.set_corridor_filter("PATHE")
        win_filtered = MockCursesWindow(win_h, win_w)
        renderer.render_map(win_filtered, store.get_snapshot(), projector)
        filtered_trail_cells = {
            (x - 1, y - 1)
            for (y, x, text, _) in win_filtered.strings
            if text == renderer.glyphs.trail_dot
        }
        self.assertIn((t1_col, t1_row), filtered_trail_cells)
        self.assertNotIn((t2_col, t2_row), filtered_trail_cells)
        store.set_corridor_filter(None)

        # 3. BreadcrumbMode.SELECTED with no train selected: zero trail dots rendered
        store.set_breadcrumb_mode(BreadcrumbMode.SELECTED)
        win_sel_none = MockCursesWindow(win_h, win_w)
        renderer.render_map(win_sel_none, store.get_snapshot(), projector)
        sel_none_texts = " ".join(text for (_, _, text, _) in win_sel_none.strings)
        self.assertIn("Trails: SEL", sel_none_texts)
        sel_none_trail_cells = {
            (x - 1, y - 1)
            for (y, x, text, _) in win_sel_none.strings
            if text == renderer.glyphs.trail_dot
        }
        self.assertEqual(len(sel_none_trail_cells), 0)

        # 4. BreadcrumbMode.SELECTED with t1 selected: only t1 trail dot rendered
        store.select_by_id("t1")
        win_sel_t1 = MockCursesWindow(win_h, win_w)
        renderer.render_map(win_sel_t1, store.get_snapshot(), projector)
        sel_t1_trail_cells = {
            (x - 1, y - 1)
            for (y, x, text, _) in win_sel_t1.strings
            if text == renderer.glyphs.trail_dot
        }
        self.assertEqual(sel_t1_trail_cells, {(t1_col, t1_row)})

        # 5. BreadcrumbMode.OFF: zero trail dots rendered and title shows Trails: OFF
        store.set_breadcrumb_mode(BreadcrumbMode.OFF)
        win_off = MockCursesWindow(win_h, win_w)
        renderer.render_map(win_off, store.get_snapshot(), projector)
        off_texts = " ".join(text for (_, _, text, _) in win_off.strings)
        self.assertIn("Trails: OFF", off_texts)
        off_trail_cells = {
            (x - 1, y - 1)
            for (y, x, text, _) in win_off.strings
            if text == renderer.glyphs.trail_dot
        }
        self.assertEqual(len(off_trail_cells), 0)

    def test_station_lookup_index_and_inspector_coordinates_eta(self):
        """TC-F22-02 & TC-F22-08: Verify diacritic-insensitive station resolution and Inspector F-2.2 rows."""
        stations_dict = load_stations()
        renderer_utf8 = TUIRenderer(stations=list(stations_dict.values()), ascii_mode=False)
        renderer_ascii = TUIRenderer(stations=list(stations_dict.values()), ascii_mode=True)

        # 1. Resolve by Greek name with tonos, uppercase toneless Greek, English, ID, code, and base name
        for query, expected_id in [
            ("Λάρισα", "LAR"),
            ("ΛΑΡΙΣΑ", "LAR"),
            ("Larissa", "LAR"),
            ("LAR", "LAR"),
            ("1080", "LAR"),
            ("Αθήνα", "ATH"),
            ("ΑΘΗΝΑ", "ATH"),
            ("Athens", "ATH"),
            ("ΣΚΑ", "SKA"),
            ("SKA", "SKA"),
            ("Αεροδρόμιο", "AIA"),
            ("ΑΕΡΟΔΡΟΜΙΟ", "AIA"),
        ]:
            resolved = renderer_utf8.resolve_station(query)
            self.assertIsNotNone(resolved, f"Failed to resolve station query '{query}'")
            assert resolved is not None
            self.assertEqual(resolved.id, expected_id)

        self.assertIsNone(renderer_utf8.resolve_station("Atlantis"))
        self.assertIsNone(renderer_utf8.resolve_station(None))
        self.assertEqual(normalize_station_key("  Λάρισα "), "λαρισα")

        # 2. Verify Inspector renders Next Station with distance & ETA and Coordinates row in UTF-8 and ASCII
        # Train at (39.5108, 22.4222) is ~14.2 km south of Larissa (39.6385, 22.4222) at 142.0 km/h (~6m ETA)
        pos = create_test_position(
            train_id="ic-1599",
            train_number="1599",
            train_type="IC",
            speed=142.0,
            lat=39.5108,
            lng=22.4222,
            delay=9.0,
        )
        pos.next_station = "Λάρισα"
        store = TrainStateStore()
        store.update_positions([pos])
        store.select_by_id("ic-1599")
        snapshot = store.get_snapshot()

        win_utf8 = MockCursesWindow(14, 42)
        renderer_utf8.render_inspector(win_utf8, snapshot)
        utf8_texts = " ".join(text for (_, _, text, _) in win_utf8.strings)
        self.assertIn("Next Station:", utf8_texts)
        self.assertIn("Λάρισα (14.2 km, ~6m)", utf8_texts)
        self.assertIn("Coordinates:", utf8_texts)
        self.assertIn("39.5108°N, 22.4222°E", utf8_texts)

        win_ascii = MockCursesWindow(14, 42)
        renderer_ascii.render_inspector(win_ascii, snapshot)
        ascii_texts = " ".join(text for (_, _, text, _) in win_ascii.strings)
        self.assertIn("Coordinates:", ascii_texts)
        self.assertIn("39.5108d N, 22.4222d E", ascii_texts)

    def test_compute_station_board_entries_and_render_station_inspector(self):
        """TC-F32-03, TC-F32-04, TC-F32-07 & TC-F32-08: Verify Station Departure Board & Proximity Inspector."""
        stations_dict = load_stations()
        stations_list = list(stations_dict.values())
        renderer_utf8 = TUIRenderer(stations=stations_list, ascii_mode=False)
        renderer_ascii = TUIRenderer(stations=stations_list, ascii_mode=True)

        larissa = renderer_utf8.resolve_station("LAR")
        self.assertIsNotNone(larissa)
        assert larissa is not None

        # 1. Train 1598: Halted right at Larissa platform (~0.1 km away, speed 0.0 -> DIR='HLT', ETA='AT')
        t_at = create_test_position(
            train_id="t-1598",
            train_number="1598",
            speed=0.0,
            delay=0.0,
            lat=39.6376,
            lng=22.4222,
            track_heading=180.0,
        )
        t_at.next_station = "Λάρισα"

        # 2. Train 1597: Approaching Larissa from ~4.2 km South at 126 km/h (DIR='NNW', DIST='4.2km', ETA='~2m', DLY='+4m')
        t_near = create_test_position(
            train_id="t-1597",
            train_number="1597",
            speed=126.0,
            delay=4.0,
            lat=39.6007,
            lng=22.4222,
            track_heading=340.0,
        )
        t_near.next_station = "Λάρισα"

        # 3. Train 2591: Approaching Larissa from ~11.8 km North at 101 km/h (DIR='SSE', DIST='11.8km', ETA='~7m', DLY='0m')
        t_mid = create_test_position(
            train_id="t-2591",
            train_number="2591",
            speed=101.0,
            delay=0.0,
            lat=39.7446,
            lng=22.4222,
            track_heading=160.0,
        )
        t_mid.next_station = "Λάρισα"

        # 4. Train IC 52: 185 km away in Athens, but destination is "Λάρισα" (included via route match)
        t_route = create_test_position(
            train_id="t-52",
            train_number="IC 52",
            speed=120.0,
            delay=2.0,
            lat=37.9926,
            lng=23.7203,
            track_heading=350.0,
            destination="Λάρισα",
        )
        t_route.next_station = "ΣΚΑ"

        # 5. Train 4200: 210 km away in Alexandroupoli with unrelated route (excluded: d > 80 km and no route match)
        t_far_unrelated = create_test_position(
            train_id="t-4200",
            train_number="4200",
            speed=90.0,
            delay=0.0,
            lat=40.8472,
            lng=25.8778,
            origin="Ξάνθη",
            destination="Αλεξανδρούπολη",
        )
        t_far_unrelated.next_station = "Κομοτηνή"

        store = TrainStateStore()
        store.update_positions([t_mid, t_far_unrelated, t_near, t_route, t_at])
        store.select_by_id("t-1597")
        store.set_selected_station("LAR")
        snapshot = store.get_snapshot()

        entries = compute_station_board_entries(
            station=larissa,
            trains=snapshot.filtered_trains,
            resolve_station_fn=renderer_utf8.resolve_station,
            selected_train_id="t-1597",
        )

        # Unrelated far train (4200) must be excluded; remaining 4 sorted by ascending distance
        self.assertEqual([e.train_number for e in entries], ["1598", "1597", "2591", "IC 52"])
        self.assertEqual(entries[0].direction, "HLT")
        self.assertEqual(entries[0].dist_str, "0.1km")
        self.assertEqual(entries[0].eta_str, "AT")
        self.assertEqual(entries[0].delay_str, "0m")

        self.assertEqual(entries[1].direction, "NNW")
        self.assertEqual(entries[1].dist_str, "4.2km")
        self.assertEqual(entries[1].eta_str, "~2m")
        self.assertEqual(entries[1].delay_str, "+4m")
        self.assertTrue(entries[1].is_selected)

        self.assertEqual(entries[2].direction, "SSE")
        self.assertEqual(entries[2].dist_str, "11.8km")
        self.assertEqual(entries[2].eta_str, "~7m")

        self.assertTrue(entries[3].is_matched_route)
        self.assertGreater(entries[3].distance_km, 80.0)

        # Render Station Board in wide UTF-8 Inspector window (14x36)
        win_utf8 = MockCursesWindow(14, 36)
        renderer_utf8.render_inspector(win_utf8, snapshot)
        utf8_texts = " ".join(text for (_, _, text, _) in win_utf8.strings)
        self.assertIn("STATION BOARD: Larissa", utf8_texts)
        self.assertIn("PATHE Mainline", utf8_texts)
        self.assertIn("39.6385°N, 22.4222°E", utf8_texts)
        self.assertIn("APPROACHING / AT STATION:", utf8_texts)
        self.assertIn("TRAIN  DIR  DIST   ETA   DLY", utf8_texts)
        self.assertIn("1598", utf8_texts)
        self.assertIn("1597", utf8_texts)
        self.assertIn("2591", utf8_texts)

        # Render Station Board in compact 80x24 Inspector window (11x28)
        win_compact = MockCursesWindow(11, 28)
        renderer_utf8.render_inspector(win_compact, snapshot)
        compact_texts = " ".join(text for (_, _, text, _) in win_compact.strings)
        self.assertIn("STATION BOARD: Larissa", compact_texts)
        self.assertIn("TRAIN DIR DIST   ETA  DLY", compact_texts)
        self.assertIn("1598", compact_texts)
        self.assertIn("1597", compact_texts)

        # Render Station Board in 7-bit ASCII mode and verify strict 7-bit ASCII compliance
        win_ascii = MockCursesWindow(14, 36)
        renderer_ascii.render_inspector(win_ascii, snapshot)
        ascii_texts = " ".join(text for (_, _, text, _) in win_ascii.strings)
        self.assertIn("39.6385d N, 22.4222d E", ascii_texts)
        for _, _, text, _ in win_ascii.strings:
            for ch in text:
                self.assertLess(ord(ch), 128, f"Non-ASCII character '{ch}' in ASCII station board")

        self.assertEqual(format_corridor_display_name("PROASTIAKOS_ATH"), "Proastiakos Suburban")
        self.assertEqual(format_corridor_display_name("NORTHERN"), "Northern Greece")
        self.assertEqual(format_corridor_display_name(None), "National Network")

    def test_format_scale_bar_utf8_and_ascii(self):
        """TC-F34-04: Verify format_scale_bar produces exact UTF-8 and 7-bit ASCII strings and widths."""
        self.assertEqual(format_scale_bar(25, width_cells=15, ascii_mode=False), "├─── 25 km ───┤")
        self.assertEqual(format_scale_bar(25, width_cells=17, ascii_mode=False), "├──── 25 km ────┤")
        self.assertEqual(format_scale_bar(25, width_cells=15, ascii_mode=True), "|--- 25 km ---|")
        self.assertEqual(format_scale_bar(25, width_cells=17, ascii_mode=True), "|---- 25 km ----|")

        for interval_km in (1, 2, 5, 10, 20, 25, 50, 100, 200):
            for width in range(12, 19):
                bar_utf8 = format_scale_bar(interval_km, width_cells=width, ascii_mode=False)
                bar_ascii = format_scale_bar(interval_km, width_cells=width, ascii_mode=True)
                self.assertEqual(len(bar_utf8), width)
                self.assertEqual(len(bar_ascii), width)
                self.assertTrue(bar_utf8.startswith("├") and bar_utf8.endswith("┤"))
                self.assertTrue(bar_ascii.startswith("|") and bar_ascii.endswith("|"))
                self.assertIn(f" {interval_km} km ", bar_utf8)
                self.assertIn(f" {interval_km} km ", bar_ascii)
                for ch in bar_ascii:
                    self.assertLess(ord(ch), 128)

    def test_render_map_scale_bar_overlay_and_zoom_adaptation(self):
        """TC-F34-05, TC-F34-06 & TC-F34-07: Verify dynamic conformal scale bar overlay in render_map."""
        win_h, win_w = 24, 80
        inner_h, inner_w = win_h - 2, win_w - 2
        viewport = Viewport(
            center=GeoPoint(lat=39.50, lng=22.50),
            zoom=1.0,
            width_cells=inner_w,
            height_cells=inner_h,
            aspect_ratio=2.0,
        )
        projector = GeoProjector(viewport)

        # Place a secondary station right at the bottom-left scale bar position (col=2, row=inner_h - 1)
        scale_row_sub_y = (inner_h - 1) * 4 + 2
        scale_col_sub_x = 2 * 2 + 1
        bl_geo = projector.subpixel_to_geo(scale_col_sub_x, scale_row_sub_y)
        bottom_left_station = StationNode(
            id="BLS",
            name_el="Σταθμός Κλίμακας",
            name_en="ScaleCollisionTestStation",
            point=bl_geo,
            corridor="PATHE",
            code="9999",
            is_hub=False,
        )

        renderer_utf8 = TUIRenderer(stations=[bottom_left_station], tracks=[], ascii_mode=False)
        renderer_ascii = TUIRenderer(stations=[bottom_left_station], tracks=[], ascii_mode=True)
        store = TrainStateStore()
        snapshot = store.get_snapshot()

        # 1. UTF-8 render at zoom=1.0: scale bar rendered at row=win_h-2 (22), col=2
        win_z1 = MockCursesWindow(win_h, win_w)
        renderer_utf8.render_map(win_z1, snapshot, projector)
        spec_z1 = projector.compute_scale_bar()
        expected_bar_z1 = format_scale_bar(spec_z1.distance_km, spec_z1.width_cells, ascii_mode=False)

        scale_writes_z1 = [
            text for (y, x, text, _) in win_z1.strings if y == win_h - 2 and x == 2
        ]
        self.assertIn(expected_bar_z1, scale_writes_z1)
        # Station label at bottom-left must be suppressed by scale bar occupied_cells reservation
        all_z1_texts = " ".join(text for (_, _, text, _) in win_z1.strings)
        self.assertNotIn("ScaleCollisionTestStation", all_z1_texts)

        # 2. Zoom in to 10.0x: scale bar dynamically adapts to a smaller distance interval
        projector.viewport.zoom = 10.0
        win_z10 = MockCursesWindow(win_h, win_w)
        renderer_utf8.render_map(win_z10, snapshot, projector)
        spec_z10 = projector.compute_scale_bar()
        self.assertLess(spec_z10.distance_km, spec_z1.distance_km)
        expected_bar_z10 = format_scale_bar(spec_z10.distance_km, spec_z10.width_cells, ascii_mode=False)
        scale_writes_z10 = [
            text for (y, x, text, _) in win_z10.strings if y == win_h - 2 and x == 2
        ]
        self.assertIn(expected_bar_z10, scale_writes_z10)

        # 3. ASCII mode render: pure 7-bit ASCII scale bar at (win_h - 2, 2)
        win_ascii = MockCursesWindow(win_h, win_w)
        renderer_ascii.render_map(win_ascii, snapshot, projector)
        expected_bar_ascii = format_scale_bar(spec_z10.distance_km, spec_z10.width_cells, ascii_mode=True)
        scale_writes_ascii = [
            text for (y, x, text, _) in win_ascii.strings if y == win_h - 2 and x == 2
        ]
        self.assertIn(expected_bar_ascii, scale_writes_ascii)
        for _, _, text, _ in win_ascii.strings:
            for ch in text:
                self.assertLess(ord(ch), 128)


if __name__ == "__main__":
    unittest.main()
