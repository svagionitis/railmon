"""Unit and integration tests for Phase 5 interactive controls, following, and filtering.

Tests camera panning kinematics, zoom scaling, corridor presets, train selection cycling,
real-time auto-follow camera tracking, modal search filter editing, and help dialog.
"""

import curses
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition
from tui.app import PRESET_CONFIGS, InputMode, TUIApp
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
from tui.renderer import PaneGeometry, TUIRenderer
from tui.state import ConnectionStatus, TrainStateStore
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
            target_fps=20.0,
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


if __name__ == "__main__":
    unittest.main()
