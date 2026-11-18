"""Unit tests for TUI layout geometry, multi-pane rendering, and curses application controller."""

import curses
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition
from tui.app import TUIApp
from tui.geo import GREECE_PRESET, GeoPoint, GeoProjector, Viewport
from tui.mock_engine import StationNode, TrackPolyline
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
    TUIRenderer,
    calculate_scroll_offset,
    format_progress_bar,
    get_compass_bearing,
    get_delay_color_pair,
    get_heading_glyph,
    get_string_display_width,
    get_train_color_pair,
    truncate_to_display_width,
)
from tui.state import ConnectionStatus, StateSnapshot, TrainRecord, TrainStateStore
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
            target_fps=20.0,
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


if __name__ == "__main__":
    unittest.main()
