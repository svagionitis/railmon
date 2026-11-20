"""Unit and integration tests for Phase 5 interactive controls, following, and filtering.

Tests camera panning kinematics, zoom scaling, corridor presets, train selection cycling,
real-time auto-follow camera tracking, modal search filter editing, and help dialog.
"""

import curses
import time
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition
from tui.app import (
    MOUSE_LEFT_CLICK_MASK,
    MOUSE_LEFT_DOUBLE_CLICK_MASK,
    MOUSE_SCROLL_DOWN_MASK,
    MOUSE_SCROLL_UP_MASK,
    PRESET_CONFIGS,
    InputMode,
    TUIApp,
)
from tui.geo import (
    GREECE_PRESET,
    NORTHERN_GREECE_PRESET,
    PATHE_PRESET,
    PROASTIAKOS_PRESET,
    BoundingBox,
    GeoPoint,
    GeoProjector,
    Viewport,
)
from tui.mock_engine import StationNode
from tui.renderer import PaneGeometry, TUIRenderer
from tui.state import (
    BreadcrumbMode,
    ConnectionStatus,
    InspectorMode,
    RosterSortMode,
    StationLabelMode,
    TrackColorMode,
    TrainStateStore,
)
from tui.stream_worker import StreamWorker


def create_test_position(
    train_id: str,
    train_number: str = "1597",
    lat: float = 38.0,
    lng: float = 23.7,
    corridor: str = "PATHE",
    speed: float = 100.0,
) -> TrainPosition:
    """Helper to instantiate a valid TrainPosition for control tests."""
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120010",
        name=train_number,
        train_type="InterCity",
        service_type="Passenger",
        status="Κανονική" if speed > 5.0 else "Στάση",
        origin="ΑΘΗΝΑ",
        destination="ΘΕΣΣΑΛΟΝΙΚΗ",
        next_station="SKA",
        speed=speed,
        course=0.0,
        track_heading=0.0,
        lat=lat,
        lng=lng,
        altitude=45.0,
        delay=2,
        progress=0.4,
        chainage_m=12000,
        corridor=corridor,
        distance_to_destination_m=480000,
        timestamp="2026-11-18T12:00:00Z",
        schedule_id="sch-101",
    )


class MockCursesWindow:
    """Mock curses window simulating character grid operations without an interactive TTY."""

    def __init__(self, height: int = 30, width: int = 100):
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

    def clear(self):
        self.calls.append(("clear",))

    def refresh(self):
        self.calls.append(("refresh",))

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

    def subwin(self, *args):
        return self


class TestTUIControls(unittest.TestCase):
    """Test suite for Phase 5 interactive controls, following & filtering."""

    def setUp(self):
        self.stdscr = MockCursesWindow(height=30, width=100)
        self.store = TrainStateStore()
        self.worker = MagicMock(spec=StreamWorker)
        self.app = TUIApp(
            stdscr=self.stdscr,
            store=self.store,
            worker=self.worker,
            stations=[],
            tracks=[],
            target_fps=15.0,
        )
        # Allocate geometry and windows
        self.app.rebuild_windows()

        # Seed store with three test trains
        self.t1 = create_test_position("t1", "1597", lat=38.0, lng=23.7, corridor="PATHE")
        self.t2 = create_test_position("t2", "2232", lat=38.1, lng=23.5, corridor="PROASTIAKOS")
        self.t3 = create_test_position("t3", "50", lat=40.5, lng=22.9, corridor="PATHE")
        self.store.update_positions([self.t1, self.t2, self.t3])

    # ------------------------------------------------------------------
    # Panning & Camera Kinematics Tests
    # ------------------------------------------------------------------

    def test_pan_cardinal_directions(self):
        """Verify h/j/k/l and arrow keys shift center coordinates correctly."""
        initial_center = self.app.projector.viewport.center

        # Pan West (h)
        self.app.process_input(ord("h"))
        w_center = self.app.projector.viewport.center
        self.assertLess(w_center.lng, initial_center.lng)
        self.assertAlmostEqual(w_center.lat, initial_center.lat, places=4)

        # Pan East (KEY_RIGHT)
        self.app.process_input(curses.KEY_RIGHT)
        e_center = self.app.projector.viewport.center
        self.assertAlmostEqual(e_center.lng, initial_center.lng, places=4)

        # Pan North (k)
        self.app.process_input(ord("k"))
        n_center = self.app.projector.viewport.center
        self.assertGreater(n_center.lat, initial_center.lat)
        self.assertAlmostEqual(n_center.lng, initial_center.lng, places=4)

        # Pan South (KEY_DOWN)
        self.app.process_input(curses.KEY_DOWN)
        s_center = self.app.projector.viewport.center
        self.assertAlmostEqual(s_center.lat, initial_center.lat, places=4)

    def test_pan_scale_invariance(self):
        """Verify higher zoom levels produce smaller geographic pan displacements."""
        self.app.projector.viewport.zoom = 1.0
        c0 = self.app.projector.viewport.center
        self.app.pan(10, 0)
        c1 = self.app.projector.viewport.center
        delta_lng_1x = c1.lng - c0.lng

        # Reset and pan at 4.0x zoom
        self.app.projector.viewport.center = c0
        self.app.projector.viewport.zoom = 4.0
        self.app.pan(10, 0)
        c2 = self.app.projector.viewport.center
        delta_lng_4x = c2.lng - c0.lng

        # At 4x zoom, geographic displacement must be exactly 1/4th
        self.assertAlmostEqual(delta_lng_4x, delta_lng_1x / 4.0, places=5)

    def test_pan_exact_cell_displacement_conformal(self):
        """Verify pan(dx_cells, dy_cells) shifts projected features by exact 1:1 character cell increments."""
        test_pt = self.app.projector.viewport.center
        px0, py0 = self.app.projector.geo_to_subpixel(test_pt)

        # Pan camera +5 columns East and -3 rows North
        self.app.pan(dx_cells=5, dy_cells=-3)
        px1, py1 = self.app.projector.geo_to_subpixel(test_pt)

        # Panning camera East (+5 cols = +10 subpixels) shifts stationary feature West (-10 subpixels)
        # Panning camera North (-3 rows = -12 subpixels) shifts stationary feature South (+12 subpixels)
        self.assertEqual(px1 - px0, -10)
        self.assertEqual(py1 - py0, 12)

    def test_pan_boundary_clamping(self):
        """Verify pan operations clamp within Greece boundary limits."""
        # Pan extremely far North
        for _ in range(50):
            self.app.pan(0, -100)
        self.assertLessEqual(self.app.projector.viewport.center.lat, 43.0)

        # Pan extremely far South
        for _ in range(100):
            self.app.pan(0, 100)
        self.assertGreaterEqual(self.app.projector.viewport.center.lat, 34.0)

        # Pan extremely far West
        for _ in range(100):
            self.app.pan(-100, 0)
        self.assertGreaterEqual(self.app.projector.viewport.center.lng, 19.0)

        # Pan extremely far East
        for _ in range(100):
            self.app.pan(100, 0)
        self.assertLessEqual(self.app.projector.viewport.center.lng, 29.0)

    def test_pan_disengages_follow_mode(self):
        """Verify any manual camera pan immediately disengages auto-follow mode."""
        self.store.select_by_id("t1")
        self.store.set_follow_mode(True)
        self.assertTrue(self.store.get_snapshot().follow_mode)

        # Manual pan West
        self.app.process_input(ord("h"))
        self.assertFalse(self.store.get_snapshot().follow_mode)

    # ------------------------------------------------------------------
    # Zooming Tests
    # ------------------------------------------------------------------

    def test_zoom_in_and_out(self):
        """Verify +/= zooms in and -/_ zooms out."""
        z0 = self.app.projector.viewport.zoom

        # Zoom in (+)
        self.app.process_input(ord("+"))
        self.assertAlmostEqual(self.app.projector.viewport.zoom, z0 * 1.25, places=4)

        # Zoom out (-)
        self.app.process_input(ord("-"))
        self.assertAlmostEqual(self.app.projector.viewport.zoom, z0, places=4)

        # Zoom in (= alternate key)
        self.app.process_input(ord("="))
        self.assertAlmostEqual(self.app.projector.viewport.zoom, z0 * 1.25, places=4)

        # Zoom out (_ alternate key)
        self.app.process_input(ord("_"))
        self.assertAlmostEqual(self.app.projector.viewport.zoom, z0, places=4)

        # Zoom in/out via Windows PDCurses numpad keys (PADPLUS / PADMINUS) if defined
        pad_plus = getattr(curses, "PADPLUS", None)
        pad_minus = getattr(curses, "PADMINUS", None)
        if pad_plus is not None and pad_minus is not None:
            self.app.process_input(pad_plus)
            self.assertAlmostEqual(self.app.projector.viewport.zoom, z0 * 1.25, places=4)
            self.app.process_input(pad_minus)
            self.assertAlmostEqual(self.app.projector.viewport.zoom, z0, places=4)

    def test_zoom_clamping(self):
        """Verify zoom scales clamp between 0.2 and 25.0."""
        # Zoom in beyond max
        for _ in range(30):
            self.app.zoom(2.0)
        self.assertEqual(self.app.projector.viewport.zoom, 25.0)

        # Zoom out beyond min
        for _ in range(50):
            self.app.zoom(0.5)
        self.assertEqual(self.app.projector.viewport.zoom, 0.2)

    # ------------------------------------------------------------------
    # Corridor Presets Tests
    # ------------------------------------------------------------------

    def test_preset_shortcuts(self):
        """Verify 0..3 keys activate corridor bounding boxes and filters."""
        # Preset 1: Suburban / Proastiakos
        self.app.process_input(ord("1"))
        snap1 = self.store.get_snapshot()
        self.assertEqual(snap1.corridor_filter, "PROASTIAKOS")
        self.assertAlmostEqual(self.app.projector.viewport.center.lat, PROASTIAKOS_PRESET.center.lat, places=2)
        self.assertAlmostEqual(self.app.projector.viewport.center.lng, PROASTIAKOS_PRESET.center.lng, places=2)

        # Preset 2: PATHE Mainline
        self.app.process_input(ord("2"))
        snap2 = self.store.get_snapshot()
        self.assertEqual(snap2.corridor_filter, "PATHE")
        self.assertAlmostEqual(self.app.projector.viewport.center.lat, PATHE_PRESET.center.lat, places=2)

        # Preset 3: Northern Greece
        self.app.process_input(ord("3"))
        snap3 = self.store.get_snapshot()
        self.assertEqual(snap3.corridor_filter, "NORTHERN")
        self.assertAlmostEqual(self.app.projector.viewport.center.lat, NORTHERN_GREECE_PRESET.center.lat, places=2)

        # Preset 0: All Greece (clears corridor filter)
        self.app.process_input(ord("0"))
        snap0 = self.store.get_snapshot()
        self.assertIsNone(snap0.corridor_filter)
        self.assertAlmostEqual(self.app.projector.viewport.center.lat, GREECE_PRESET.center.lat, places=2)

    def test_preset_shortcuts_with_live_telemetry(self):
        """Verify 0..3 keys filter real-world trains with PIRAIR or missing corridor tags."""
        self.store.clear()
        t_suburban = TrainPosition.from_dict({
            "id": "t-sub",
            "trainNumber": "2232",
            "serviceType": "Suburban",
            "lat": 38.050842,
            "lng": 23.782449,
            "speed": 95.0,
            "corridor": "PIRAIR",
        })
        t_ic = TrainPosition.from_dict({
            "id": "t-ic",
            "trainNumber": "52",
            "serviceType": "Intercity",
            "lat": 39.638,
            "lng": 22.422,
            "speed": 130.0,
            "corridor": None,
        })
        t_north = TrainPosition.from_dict({
            "id": "t-north",
            "trainNumber": "3672",
            "serviceType": "Regional",
            "origin": "Thessaloniki",
            "destination": "Serres",
            "lat": 41.09,
            "lng": 23.55,
            "speed": 75.0,
            "corridor": None,
        })
        self.store.update_positions([t_suburban, t_ic, t_north])

        # Preset 1: Proastiakos
        self.app.process_input(ord("1"))
        train_numbers = [t.position.train_number for t in self.store.get_snapshot().filtered_trains]
        self.assertIn("2232", train_numbers)
        self.assertNotIn("3672", train_numbers)

        # Preset 2: PATHE
        self.app.process_input(ord("2"))
        train_numbers = [t.position.train_number for t in self.store.get_snapshot().filtered_trains]
        self.assertIn("52", train_numbers)
        self.assertNotIn("3672", train_numbers)

        # Preset 3: Northern
        self.app.process_input(ord("3"))
        train_numbers = [t.position.train_number for t in self.store.get_snapshot().filtered_trains]
        self.assertIn("3672", train_numbers)
        self.assertNotIn("2232", train_numbers)

        # Preset 0: All Greece
        self.app.process_input(ord("0"))
        train_numbers = [t.position.train_number for t in self.store.get_snapshot().filtered_trains]
        self.assertEqual(len(train_numbers), 3)

    # ------------------------------------------------------------------
    # Train Selection & Auto-Follow Tests
    # ------------------------------------------------------------------

    def test_train_selection_cycling(self):
        """Verify Tab/n and KEY_BTAB/p cycle train selection with circular wrap-around."""
        # Initial: no selection
        self.assertIsNone(self.store.get_snapshot().selected_train)

        # Tab selects first train in sorted order ('50' is first naturally)
        self.app.process_input(ord("\t"))
        self.assertEqual(self.store.get_snapshot().selected_index, 0)

        # 'n' selects next train (index 1)
        self.app.process_input(ord("n"))
        self.assertEqual(self.store.get_snapshot().selected_index, 1)

        # 'n' selects next train (index 2)
        self.app.process_input(ord("n"))
        self.assertEqual(self.store.get_snapshot().selected_index, 2)

        # Tab wraps around to 0
        self.app.process_input(ord("\t"))
        self.assertEqual(self.store.get_snapshot().selected_index, 0)

        # 'p' cycles backward to last item (index 2)
        self.app.process_input(ord("p"))
        self.assertEqual(self.store.get_snapshot().selected_index, 2)

        # KEY_BTAB cycles backward to index 1
        self.app.process_input(curses.KEY_BTAB)
        self.assertEqual(self.store.get_snapshot().selected_index, 1)

    def test_follow_mode_toggle_and_auto_select(self):
        """Verify 'f' toggles follow mode and auto-selects first train if none selected."""
        # When no train is selected, 'f' selects first train and enables follow mode
        self.assertIsNone(self.store.get_snapshot().selected_train)
        self.app.process_input(ord("f"))

        snap1 = self.store.get_snapshot()
        self.assertIsNotNone(snap1.selected_train)
        self.assertTrue(snap1.follow_mode)

        # Pressing 'f' again toggles follow mode off
        self.app.process_input(ord("f"))
        snap2 = self.store.get_snapshot()
        self.assertFalse(snap2.follow_mode)
        # Selected train remains selected
        self.assertIsNotNone(snap2.selected_train)

    def test_clear_all_shortcut(self):
        """Verify 'c' clears search filter, corridor filter, and selection."""
        self.store.select_by_id("t1")
        self.store.set_corridor_filter("PATHE")
        self.store.set_filter_query("1597")
        self.app.search_buffer = "1597"

        self.app.process_input(ord("c"))

        snap = self.store.get_snapshot()
        self.assertIsNone(snap.selected_train)
        self.assertIsNone(snap.corridor_filter)
        self.assertEqual(snap.filter_query, "")
        self.assertEqual(self.app.search_buffer, "")

    def test_pause_telemetry_toggle(self):
        """Verify Space toggles pause state, invokes StreamWorker.pause/resume, and updates connection status."""
        self.assertFalse(self.app.paused)
        self.app.process_input(ord(" "))
        self.assertTrue(self.app.paused)
        self.worker.pause.assert_called_once()
        self.assertEqual(self.store.get_snapshot().connection_status, ConnectionStatus.PAUSED)

        self.app.process_input(ord(" "))
        self.assertFalse(self.app.paused)
        self.worker.resume.assert_called_once()
        self.assertEqual(self.store.get_snapshot().connection_status, ConnectionStatus.CONNECTED)

    # ------------------------------------------------------------------
    # Search Filter Editor Lifecycle Tests
    # ------------------------------------------------------------------

    def test_search_mode_lifecycle(self):
        """Verify / enters search mode, typing filters incrementally, and Enter commits."""
        self.assertEqual(self.app.input_mode, InputMode.NORMAL)

        # 1. Press '/' to enter search mode
        self.app.process_input(ord("/"))
        self.assertEqual(self.app.input_mode, InputMode.SEARCH)
        self.assertEqual(self.app.search_buffer, "")

        # 2. Type '1', '5', '9', '7'
        for ch in "1597":
            self.app.process_input(ord(ch))

        self.assertEqual(self.app.search_buffer, "1597")
        # Store is filtered incrementally
        snap = self.store.get_snapshot()
        self.assertEqual(len(snap.filtered_trains), 1)
        self.assertEqual(snap.filtered_trains[0].position.train_number, "1597")

        # 3. Press Enter to commit
        self.app.process_input(10)
        self.assertEqual(self.app.input_mode, InputMode.NORMAL)
        # Filter remains applied in store
        self.assertEqual(self.store.get_snapshot().filter_query, "1597")

    def test_search_backspace_and_revert(self):
        """Verify backspace removes characters and Esc cancels and restores query."""
        # Start with pre-existing filter
        self.store.set_filter_query("2232")

        # Press '/' to start editing
        self.app.process_input(ord("/"))
        self.assertEqual(self.app.search_buffer, "2232")

        # Backspace twice
        self.app.process_input(curses.KEY_BACKSPACE)
        self.app.process_input(curses.KEY_BACKSPACE)
        self.assertEqual(self.app.search_buffer, "22")
        self.assertEqual(self.store.get_snapshot().filter_query, "22")

        # Type '5'
        self.app.process_input(ord("5"))
        self.assertEqual(self.app.search_buffer, "225")

        # Press ESC to cancel
        self.app.process_input(27)
        self.assertEqual(self.app.input_mode, InputMode.NORMAL)
        # Query reverts to pre-search value '2232'
        self.assertEqual(self.store.get_snapshot().filter_query, "2232")

    # ------------------------------------------------------------------
    # Help Modal Dialog Tests
    # ------------------------------------------------------------------

    def test_help_modal_toggle(self):
        """Verify '?' toggles help modal and any key closes it."""
        self.assertFalse(self.app.show_help)
        self.assertEqual(self.app.input_mode, InputMode.NORMAL)

        # Press '?' opens help
        self.app.process_input(ord("?"))
        self.assertTrue(self.app.show_help)
        self.assertEqual(self.app.input_mode, InputMode.HELP)

        # Pressing any key in HELP mode closes it
        self.app.process_input(ord("x"))
        self.assertFalse(self.app.show_help)
        self.assertEqual(self.app.input_mode, InputMode.NORMAL)

    # ------------------------------------------------------------------
    # Renderer Integration Tests
    # ------------------------------------------------------------------

    def test_render_status_bar_search_mode(self):
        """Verify status bar renders interactive search prompt when in search mode."""
        win = MockCursesWindow(height=1, width=100)
        snapshot = self.store.get_snapshot()

        self.app.renderer.render_status_bar(
            win=win,
            snapshot=snapshot,
            width=100,
            search_mode=True,
            search_buffer="IC 50",
        )
        rendered_texts = [s[2] for s in win.strings]
        combined = " ".join(rendered_texts)
        self.assertIn("Search filter: IC 50█", combined)
        self.assertIn("Enter: Apply, Esc: Cancel", combined)

    def test_render_help_modal_overlay(self):
        """Verify help modal overlay renders box borders, sections, and shortcuts."""
        win = MockCursesWindow(height=30, width=100)
        self.app.renderer.render_help_modal(win, height=30, width=100)

        rendered_texts = [s[2] for s in win.strings]
        combined = " ".join(rendered_texts)
        self.assertIn("KEYBOARD CONTROLS & SYSTEM REFERENCE", combined)
        self.assertIn("NAVIGATION & VIEWPORT", combined)
        self.assertIn("TRAIN SELECTION & TRACKING", combined)
        self.assertIn("Press ? or any key to close", combined)

    def test_app_periodic_prune_in_run_loop(self):
        """TC-P2-06: TUIApp.run_loop periodically invokes store.prune_stale_trains."""
        pos = create_test_position("t-stale")
        self.store.update_positions([pos])
        self.store._trains["t-stale"].last_updated = time.time() - 2000.0

        self.app._prune_interval_sec = 0.001
        self.app._last_prune_time = time.monotonic() - 1.0

        call_count = 0
        def fake_getch():
            nonlocal call_count
            call_count += 1
            if call_count > 1:
                return ord('q')
            return -1

        self.app.stdscr.getch = fake_getch
        self.app.setup_terminal = MagicMock()
        self.app.renderer.render_all = MagicMock()

        self.app.run_loop()

        self.assertNotIn("t-stale", self.store.get_snapshot().trains)
        self.assertEqual(self.store._total_trains_pruned, 1)

    def test_target_fps_clamping(self):
        """TC-P2-2-01: target_fps is clamped to [1.0, 15.0] range."""
        app_default = TUIApp(stdscr=self.stdscr, store=self.store, worker=self.worker)
        self.assertEqual(app_default.target_fps, 15.0)

        app_over = TUIApp(stdscr=self.stdscr, store=self.store, worker=self.worker, target_fps=60.0)
        self.assertEqual(app_over.target_fps, 15.0)

        app_20 = TUIApp(stdscr=self.stdscr, store=self.store, worker=self.worker, target_fps=20.0)
        self.assertEqual(app_20.target_fps, 15.0)

        app_mid = TUIApp(stdscr=self.stdscr, store=self.store, worker=self.worker, target_fps=10.0)
        self.assertEqual(app_mid.target_fps, 10.0)

        app_under = TUIApp(stdscr=self.stdscr, store=self.store, worker=self.worker, target_fps=0.1)
        self.assertEqual(app_under.target_fps, 1.0)

    @patch("time.time", return_value=1700000000.0)
    @patch("curses.doupdate")
    def test_run_loop_skips_unchanged_idle_frames(self, mock_doupdate, mock_time):
        """TC-P2-2-02: run_loop renders once on initial frame and skips unchanged idle frames."""
        call_count = 0
        def fake_getch():
            nonlocal call_count
            call_count += 1
            if call_count >= 4:
                return ord('q')
            return -1

        self.app.stdscr.getch = fake_getch
        self.app.setup_terminal = MagicMock()
        self.app.renderer.render_all = MagicMock()

        self.app.run_loop()

        # 4 iterations: iteration 1 renders, iterations 2 & 3 skip, iteration 4 processes 'q' and exits
        self.assertEqual(self.app.render_count, 1)
        self.assertEqual(self.app.skip_count, 2)
        self.assertEqual(self.app.renderer.render_all.call_count, 1)
        self.assertEqual(mock_doupdate.call_count, 1)

    @patch("time.time", return_value=1700000000.0)
    @patch("curses.doupdate")
    def test_run_loop_renders_when_telemetry_updates_version(self, mock_doupdate, mock_time):
        """TC-P2-2-03: run_loop triggers render when snapshot.version changes."""
        call_count = 0
        def fake_getch():
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                # Telemetry update arrives from StreamWorker
                new_pos = create_test_position("t-new", "999", lat=39.0, lng=22.0)
                self.store.update_positions([new_pos])
            elif call_count >= 4:
                return ord('q')
            return -1

        self.app.stdscr.getch = fake_getch
        self.app.setup_terminal = MagicMock()
        self.app.renderer.render_all = MagicMock()

        self.app.run_loop()

        # Iteration 1 (initial) and iteration 2 (new telemetry version) render; iteration 3 skips
        self.assertEqual(self.app.render_count, 2)
        self.assertEqual(self.app.skip_count, 1)
        self.assertEqual(self.app.renderer.render_all.call_count, 2)

    @patch("curses.doupdate")
    def test_run_loop_renders_when_clock_second_advances(self, mock_doupdate):
        """TC-P2-2-04: run_loop triggers render when the 1 Hz header clock second rolls over."""
        simulated_time = 1700000000.0
        def fake_time():
            return simulated_time

        call_count = 0
        def fake_getch():
            nonlocal call_count, simulated_time
            call_count += 1
            if call_count == 2:
                # Advance time by 1.1s so current_sec increments
                simulated_time += 1.1
            elif call_count >= 4:
                return ord('q')
            return -1

        self.app.stdscr.getch = fake_getch
        self.app.setup_terminal = MagicMock()
        self.app.renderer.render_all = MagicMock()

        with patch("time.time", side_effect=fake_time):
            self.app.run_loop()

        # Iteration 1 (initial) renders, iteration 2 (clock tick) renders, iteration 3 skips
        self.assertEqual(self.app.render_count, 2)
        self.assertEqual(self.app.skip_count, 1)
        self.assertEqual(self.app.renderer.render_all.call_count, 2)

    @patch("time.time", return_value=1700000000.0)
    @patch("curses.doupdate")
    def test_run_loop_renders_on_input_or_resize(self, mock_doupdate, mock_time):
        """TC-P2-2-05: run_loop renders on user keyboard input and handle_resize sets _force_redraw."""
        self.app.handle_resize()
        self.assertTrue(self.app._force_redraw)

        call_count = 0
        def fake_getch():
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                return ord('l')  # Pan camera right
            elif call_count >= 4:
                return ord('q')
            return -1

        self.app.stdscr.getch = fake_getch
        self.app.setup_terminal = MagicMock()
        self.app.renderer.render_all = MagicMock()

        self.app.run_loop()

        # Iteration 1 (initial force_redraw) renders, iteration 2 (key 'l') renders, iteration 3 skips
        self.assertEqual(self.app.render_count, 2)
        self.assertEqual(self.app.skip_count, 1)
        self.assertEqual(self.app.renderer.render_all.call_count, 2)

    def test_key_s_cycles_roster_sort_mode_in_normal_mode(self):
        """Verify pressing 's' or 'S' in NORMAL mode cycles roster sort mode (TRAIN_ID -> SPEED -> DELAY -> TRAIN_ID)."""
        self.assertEqual(self.store.get_snapshot().sort_mode, RosterSortMode.TRAIN_ID)

        self.assertTrue(self.app.process_input(ord("s")))
        self.assertEqual(self.store.get_snapshot().sort_mode, RosterSortMode.SPEED)

        self.assertTrue(self.app.process_input(ord("S")))
        self.assertEqual(self.store.get_snapshot().sort_mode, RosterSortMode.DELAY)

        self.assertTrue(self.app.process_input(ord("s")))
        self.assertEqual(self.store.get_snapshot().sort_mode, RosterSortMode.TRAIN_ID)

    def test_key_s_in_search_mode_appends_character_without_sorting(self):
        """Verify pressing 's' while in SEARCH mode edits the search query rather than cycling sort mode."""
        self.app.process_input(ord("/"))
        self.assertEqual(self.app.input_mode, InputMode.SEARCH)

        self.app.process_input(ord("s"))
        self.assertEqual(self.app.search_buffer, "s")
        self.assertEqual(self.store.get_snapshot().sort_mode, RosterSortMode.TRAIN_ID)

    def test_key_b_cycles_breadcrumb_mode_in_normal_mode(self):
        """Verify pressing 'b' or 'B' in NORMAL mode cycles breadcrumb mode (ALL -> SELECTED -> OFF -> ALL)."""
        self.assertEqual(self.store.get_snapshot().breadcrumb_mode, BreadcrumbMode.ALL)

        self.assertTrue(self.app.process_input(ord("b")))
        self.assertEqual(self.store.get_snapshot().breadcrumb_mode, BreadcrumbMode.SELECTED)

        self.assertTrue(self.app.process_input(ord("B")))
        self.assertEqual(self.store.get_snapshot().breadcrumb_mode, BreadcrumbMode.OFF)

        self.assertTrue(self.app.process_input(ord("b")))
        self.assertEqual(self.store.get_snapshot().breadcrumb_mode, BreadcrumbMode.ALL)

    def test_key_b_in_search_mode_appends_character_without_toggling_breadcrumbs(self):
        """Verify pressing 'b' while in SEARCH mode edits the search query rather than toggling breadcrumbs."""
        self.app.process_input(ord("/"))
        self.assertEqual(self.app.input_mode, InputMode.SEARCH)

        self.app.process_input(ord("b"))
        self.assertEqual(self.app.search_buffer, "b")
        self.assertEqual(self.store.get_snapshot().breadcrumb_mode, BreadcrumbMode.ALL)

    def test_key_m_cycles_station_inspector_and_upper_m_exits(self):
        """TC-F32-05 & TC-F32-06: Verify 'm' cycles Station Departure Board and 'M' / 'c' exits to TRAIN mode."""
        self.assertEqual(self.store.get_snapshot().inspector_mode, InspectorMode.TRAIN)
        self.assertIsNone(self.store.get_snapshot().selected_station_id)

        # Select a train whose next_station is 'SKA'
        self.store.select_by_id("t1")
        self.assertTrue(self.app.process_input(ord("m")))
        snap1 = self.store.get_snapshot()
        self.assertEqual(snap1.inspector_mode, InspectorMode.STATION)
        self.assertEqual(snap1.selected_station_id, "SKA")

        # Next 'm' advances to the next hub station
        self.assertTrue(self.app.process_input(ord("m")))
        snap2 = self.store.get_snapshot()
        self.assertEqual(snap2.inspector_mode, InspectorMode.STATION)
        self.assertIsNotNone(snap2.selected_station_id)
        self.assertNotEqual(snap2.selected_station_id, "SKA")

        # Uppercase 'M' immediately exits STATION mode back to TRAIN mode
        self.assertTrue(self.app.process_input(ord("M")))
        snap3 = self.store.get_snapshot()
        self.assertEqual(snap3.inspector_mode, InspectorMode.TRAIN)
        self.assertIsNone(snap3.selected_station_id)

        # Re-enter via 'M' when in TRAIN mode, then reset with 'c' (clear_all)
        self.assertTrue(self.app.process_input(ord("M")))
        self.assertEqual(self.store.get_snapshot().inspector_mode, InspectorMode.STATION)
        self.assertTrue(self.app.process_input(ord("c")))
        self.assertEqual(self.store.get_snapshot().inspector_mode, InspectorMode.TRAIN)
        self.assertIsNone(self.store.get_snapshot().selected_station_id)

    def test_key_m_in_search_mode_appends_character_without_toggling_station_board(self):
        """TC-F32-05: Verify pressing 'm' in SEARCH mode appends to search_buffer without switching inspector mode."""
        self.app.process_input(ord("/"))
        self.assertEqual(self.app.input_mode, InputMode.SEARCH)

        self.app.process_input(ord("m"))
        self.assertEqual(self.app.search_buffer, "m")
        self.assertEqual(self.store.get_snapshot().inspector_mode, InspectorMode.TRAIN)
        self.assertIsNone(self.store.get_snapshot().selected_station_id)

    def test_key_v_cycles_station_label_mode_in_normal_mode(self):
        """TC-F33-03a: Verify pressing 'v' / 'V' in NORMAL mode cycles StationLabelMode."""
        self.assertEqual(
            self.store.get_snapshot().station_label_mode,
            StationLabelMode.HUBS_AND_SECONDARY,
        )

        self.assertTrue(self.app.process_input(ord("v")))
        self.assertEqual(
            self.store.get_snapshot().station_label_mode,
            StationLabelMode.HUBS_ONLY,
        )

        self.assertTrue(self.app.process_input(ord("V")))
        self.assertEqual(
            self.store.get_snapshot().station_label_mode,
            StationLabelMode.MARKERS_ONLY,
        )

        self.assertTrue(self.app.process_input(ord("v")))
        self.assertEqual(
            self.store.get_snapshot().station_label_mode,
            StationLabelMode.HUBS_AND_SECONDARY,
        )

    def test_key_t_toggles_track_color_mode_in_normal_mode(self):
        """TC-F33-03b: Verify pressing 't' / 'T' in NORMAL mode toggles TrackColorMode."""
        self.assertEqual(
            self.store.get_snapshot().track_color_mode,
            TrackColorMode.UNIFORM,
        )

        self.assertTrue(self.app.process_input(ord("t")))
        self.assertEqual(
            self.store.get_snapshot().track_color_mode,
            TrackColorMode.CORRIDOR,
        )

        self.assertTrue(self.app.process_input(ord("T")))
        self.assertEqual(
            self.store.get_snapshot().track_color_mode,
            TrackColorMode.UNIFORM,
        )

    def test_keys_v_and_t_in_search_mode_append_characters_without_toggling_layers(self):
        """TC-F33-03c: Verify 'v' and 't' in SEARCH mode append to search_buffer without changing map layers."""
        self.app.process_input(ord("/"))
        self.assertEqual(self.app.input_mode, InputMode.SEARCH)

        self.app.process_input(ord("v"))
        self.app.process_input(ord("t"))
        self.assertEqual(self.app.search_buffer, "vt")
        self.assertEqual(
            self.store.get_snapshot().station_label_mode,
            StationLabelMode.HUBS_AND_SECONDARY,
        )
        self.assertEqual(
            self.store.get_snapshot().track_color_mode,
            TrackColorMode.UNIFORM,
        )

    # ------------------------------------------------------------------
    # Terminal Mouse Interaction Tests (F-3.1)
    # ------------------------------------------------------------------

    @patch("tui.app.curses.mousemask")
    @patch("tui.app.curses.curs_set")
    @patch("tui.app.curses.cbreak")
    @patch("tui.app.curses.noecho")
    def test_setup_terminal_enables_mousemask_and_handles_curses_error(
        self, _mock_noecho, _mock_cbreak, _mock_curs, mock_mousemask
    ):
        """TC-F31-01: Verify setup_terminal initializes curses.mousemask and degrades gracefully on curses.error."""
        self.stdscr.keypad = MagicMock()
        self.stdscr.nodelay = MagicMock()
        self.app.renderer.init_colors = MagicMock()

        self.app.setup_terminal()
        expected_mask = getattr(curses, "ALL_MOUSE_EVENTS", 0) | getattr(
            curses, "REPORT_MOUSE_POSITION", 0
        )
        mock_mousemask.assert_called_once_with(expected_mask)

        # Simulate a terminal where mousemask raises curses.error
        mock_mousemask.side_effect = curses.error("mouse not supported")
        self.app.setup_terminal()  # Must not raise

    @patch("tui.app.curses.getmouse")
    def test_mouse_roster_click_selects_train_with_scroll_offset(self, mock_getmouse):
        """TC-F31-02: Verify left-clicking rows in the Roster pane selects the mapped train (including scroll offset)."""
        g = self.app.geometry
        # Filtered trains sorted naturally: index 0 -> '50' (t3), index 1 -> '1597' (t1), index 2 -> '2232' (t2)
        # Click row 0 of roster table (my = g.roster_y + 2)
        mock_getmouse.return_value = (0, g.roster_x + 3, g.roster_y + 2, 0, MOUSE_LEFT_CLICK_MASK)
        self.assertTrue(self.app.process_input(curses.KEY_MOUSE))
        self.assertEqual(self.store.get_snapshot().selected_train.position.id, "t3")

        # Click row 2 of roster table (my = g.roster_y + 4) -> index 2 ('2232' / t2)
        mock_getmouse.return_value = (0, g.roster_x + 3, g.roster_y + 4, 0, MOUSE_LEFT_CLICK_MASK)
        self.assertTrue(self.app.process_input(curses.KEY_MOUSE))
        self.assertEqual(self.store.get_snapshot().selected_train.position.id, "t2")
        self.assertFalse(self.store.get_snapshot().follow_mode)

        # Clicking table header (my = g.roster_y + 1) or out-of-range row does not change selection
        mock_getmouse.return_value = (0, g.roster_x + 3, g.roster_y + 1, 0, MOUSE_LEFT_CLICK_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertEqual(self.store.get_snapshot().selected_train.position.id, "t2")

        mock_getmouse.return_value = (0, g.roster_x + 3, g.roster_y + 10, 0, MOUSE_LEFT_CLICK_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertEqual(self.store.get_snapshot().selected_train.position.id, "t2")

        # Populate 25 trains so scroll_offset > 0 when selecting near the bottom
        extra_trains = [
            create_test_position(f"tx-{i}", f"{3000 + i}", lat=38.5, lng=23.0)
            for i in range(25)
        ]
        self.store.update_positions(extra_trains)
        self.store.select_by_id("tx-20")
        snap = self.store.get_snapshot()
        visible_rows = max(0, (g.roster_h - 2) - 1)
        from tui.renderer import calculate_scroll_offset
        offset = calculate_scroll_offset(snap.selected_index, len(snap.filtered_trains), visible_rows)
        self.assertGreater(offset, 0)

        # Click visible row r_idx = 1 -> should select filtered_trains[offset + 1]
        expected_id = snap.filtered_trains[offset + 1].position.id
        mock_getmouse.return_value = (0, g.roster_x + 4, g.roster_y + 2 + 1, 0, MOUSE_LEFT_CLICK_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertEqual(self.store.get_snapshot().selected_train.position.id, expected_id)

    @patch("tui.app.curses.getmouse")
    def test_mouse_roster_double_click_selects_and_engages_follow_mode(self, mock_getmouse):
        """TC-F31-03: Verify double-clicking a row in the Roster pane selects the train and engages follow mode."""
        g = self.app.geometry
        # Double-click row 1 (my = g.roster_y + 3 -> index 1 -> '1597' / t1)
        mock_getmouse.return_value = (
            0,
            g.roster_x + 5,
            g.roster_y + 3,
            0,
            MOUSE_LEFT_DOUBLE_CLICK_MASK,
        )
        self.assertTrue(self.app.process_input(curses.KEY_MOUSE))
        snap = self.store.get_snapshot()
        self.assertIsNotNone(snap.selected_train)
        self.assertEqual(snap.selected_train.position.id, "t1")
        self.assertTrue(snap.follow_mode)

    @patch("tui.app.curses.getmouse")
    def test_mouse_roster_scroll_wheel_cycles_selection(self, mock_getmouse):
        """TC-F31-04: Verify scroll wheel up/down inside the Roster pane cycles train selection backward/forward."""
        g = self.app.geometry
        # Scroll wheel down -> selects index 0 ('50' / t3)
        mock_getmouse.return_value = (0, g.roster_x + 2, g.roster_y + 2, 0, MOUSE_SCROLL_DOWN_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertEqual(self.store.get_snapshot().selected_index, 0)

        # Scroll wheel down -> selects index 1 ('1597' / t1)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertEqual(self.store.get_snapshot().selected_index, 1)

        # Scroll wheel up -> selects index 0 ('50' / t3)
        mock_getmouse.return_value = (0, g.roster_x + 2, g.roster_y + 2, 0, MOUSE_SCROLL_UP_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertEqual(self.store.get_snapshot().selected_index, 0)

    @patch("tui.app.curses.getmouse")
    def test_mouse_map_click_hit_tests_train_within_manhattan_radius_1(self, mock_getmouse):
        """TC-F31-05: Verify clicking on map within Manhattan radius <= 1 of a train selects it."""
        g = self.app.geometry
        # Compute projected cell (col, row) for train t3 (lat=40.5, lng=22.9)
        t3_col, t3_row = self.app.projector.geo_to_cell(GeoPoint(lat=self.t3.lat, lng=self.t3.lng))
        mx_exact = g.map_x + 1 + t3_col
        my_exact = g.map_y + 1 + t3_row

        # 1. Click at Manhattan distance 2 (should miss)
        mock_getmouse.return_value = (0, mx_exact + 2, my_exact, 0, MOUSE_LEFT_CLICK_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertIsNone(self.store.get_snapshot().selected_train)

        # 2. Click at Manhattan distance 1 (adjacent column mx_exact + 1, my_exact) -> hits t3
        mock_getmouse.return_value = (0, mx_exact + 1, my_exact, 0, MOUSE_LEFT_CLICK_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertIsNotNone(self.store.get_snapshot().selected_train)
        self.assertEqual(self.store.get_snapshot().selected_train.position.id, "t3")

        # 3. Double-click directly on t1 (d_M = 0) -> selects t1 and enables follow mode
        t1_col, t1_row = self.app.projector.geo_to_cell(GeoPoint(lat=self.t1.lat, lng=self.t1.lng))
        mock_getmouse.return_value = (
            0,
            g.map_x + 1 + t1_col,
            g.map_y + 1 + t1_row,
            0,
            MOUSE_LEFT_DOUBLE_CLICK_MASK,
        )
        self.app.process_input(curses.KEY_MOUSE)
        snap = self.store.get_snapshot()
        self.assertEqual(snap.selected_train.position.id, "t1")
        self.assertTrue(snap.follow_mode)

    @patch("tui.app.curses.getmouse")
    def test_mouse_map_click_on_station_opens_station_departure_board_and_clicks_entry(self, mock_getmouse):
        """TC-F31-06: Verify clicking a StationNode marker on the map engages InspectorMode.STATION."""
        lar_station = StationNode(
            id="LAR",
            code="1001",
            name_el="Λάρισα",
            name_en="Larissa",
            point=GeoPoint(lat=39.6385, lng=22.4222),
            corridor="PATHE",
            is_hub=True,
        )
        self.app.stations = [lar_station]
        self.app.renderer.stations = [lar_station]
        from tui.renderer import _build_station_index
        self.app.renderer.station_index = _build_station_index([lar_station])

        g = self.app.geometry
        s_col, s_row = self.app.projector.geo_to_cell(lar_station.point)
        mock_getmouse.return_value = (
            0,
            g.map_x + 1 + s_col,
            g.map_y + 1 + s_row,
            0,
            MOUSE_LEFT_CLICK_MASK,
        )
        self.app.process_input(curses.KEY_MOUSE)
        snap = self.store.get_snapshot()
        self.assertEqual(snap.inspector_mode, InspectorMode.STATION)
        self.assertEqual(snap.selected_station_id, "LAR")

        # Add a train near Larissa and click its row in the Station Board Inspector pane
        t_lar = create_test_position("t-lar", "1598", lat=39.6390, lng=22.4222)
        self.store.update_positions([t_lar])
        inner_h = g.inspector_h - 2
        sec_hdr_y = 4 if inner_h >= 10 else 3
        first_entry_y = g.inspector_y + sec_hdr_y + 2
        mock_getmouse.return_value = (
            0,
            g.inspector_x + 4,
            first_entry_y,
            0,
            MOUSE_LEFT_DOUBLE_CLICK_MASK,
        )
        self.app.process_input(curses.KEY_MOUSE)
        snap2 = self.store.get_snapshot()
        self.assertEqual(snap2.selected_train.position.id, "t-lar")
        self.assertTrue(snap2.follow_mode)
        self.assertEqual(snap2.inspector_mode, InspectorMode.TRAIN)

    @patch("tui.app.curses.getmouse")
    def test_mouse_map_double_click_empty_canvas_recenters_viewport_and_disengages_follow(
        self, mock_getmouse
    ):
        """TC-F31-07: Verify double-clicking empty map canvas recenters via subpixel_to_geo and disables follow."""
        self.store.select_by_id("t1")
        self.store.set_follow_mode(True)
        self.assertTrue(self.store.get_snapshot().follow_mode)

        g = self.app.geometry
        # Pick an empty cell in the top-left corner of the map canvas (col=3, row=3) far from any train
        target_col, target_row = 3, 3
        expected_geo = self.app.projector.subpixel_to_geo(target_col * 2 + 1, target_row * 4 + 2)

        mock_getmouse.return_value = (
            0,
            g.map_x + 1 + target_col,
            g.map_y + 1 + target_row,
            0,
            MOUSE_LEFT_DOUBLE_CLICK_MASK,
        )
        self.app.process_input(curses.KEY_MOUSE)

        self.assertFalse(self.store.get_snapshot().follow_mode)
        new_center = self.app.projector.viewport.center
        self.assertAlmostEqual(new_center.lat, expected_geo.lat, places=4)
        self.assertAlmostEqual(new_center.lng, expected_geo.lng, places=4)

    @patch("tui.app.curses.getmouse")
    def test_mouse_map_scroll_wheel_zooms_in_and_out(self, mock_getmouse):
        """TC-F31-08: Verify scroll wheel up/down inside the Map pane zooms in (1.25x) and out (0.8x)."""
        g = self.app.geometry
        z0 = self.app.projector.viewport.zoom

        # Wheel up on map -> zoom(1.25)
        mock_getmouse.return_value = (0, g.map_x + 10, g.map_y + 5, 0, MOUSE_SCROLL_UP_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertAlmostEqual(self.app.projector.viewport.zoom, z0 * 1.25, places=4)

        # Wheel down on map -> zoom(0.8)
        mock_getmouse.return_value = (0, g.map_x + 10, g.map_y + 5, 0, MOUSE_SCROLL_DOWN_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertAlmostEqual(self.app.projector.viewport.zoom, z0, places=4)

    @patch("tui.app.curses.getmouse")
    def test_mouse_events_in_help_and_search_modes_handled_safely(self, mock_getmouse):
        """TC-F31-09: Verify mouse clicks close HELP modal and never corrupt search_buffer in SEARCH mode."""
        # 1. Open HELP mode, click to dismiss
        self.app.process_input(ord("?"))
        self.assertEqual(self.app.input_mode, InputMode.HELP)
        self.assertTrue(self.app.show_help)

        mock_getmouse.return_value = (0, 20, 10, 0, MOUSE_LEFT_CLICK_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertEqual(self.app.input_mode, InputMode.NORMAL)
        self.assertFalse(self.app.show_help)

        # 2. Enter SEARCH mode, type 'IC', trigger KEY_MOUSE -> search_buffer remains 'IC'
        self.app.process_input(ord("/"))
        self.app.process_input(ord("I"))
        self.app.process_input(ord("C"))
        self.assertEqual(self.app.search_buffer, "IC")

        g = self.app.geometry
        mock_getmouse.return_value = (0, g.map_x + 10, g.map_y + 5, 0, MOUSE_SCROLL_UP_MASK)
        self.app.process_input(curses.KEY_MOUSE)
        self.assertEqual(self.app.search_buffer, "IC")

        # 3. If curses.getmouse() raises curses.error, process_input returns True gracefully
        mock_getmouse.side_effect = curses.error("empty mouse queue")
        self.assertTrue(self.app.process_input(curses.KEY_MOUSE))


if __name__ == "__main__":
    unittest.main()
