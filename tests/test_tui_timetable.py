"""Unit tests for F-6.2: In-TUI Train Timetable & Intermediate Stop Inspector modal.

Validates modal opening/closing toggles ('i'/'I'), train selection prerequisites,
vertical keyboard scrolling, mouse wheel scrolling and outside click dismissal,
curses modal rendering, auto-scrolling to active stop, and asynchronous caching.
"""

import curses
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition
from tui.app import (
    MOUSE_LEFT_CLICK_MASK,
    MOUSE_SCROLL_DOWN_MASK,
    MOUSE_SCROLL_UP_MASK,
    InputMode,
    TUIApp,
)
from tui.geo import GeoPoint, GeoProjector, Viewport
from tui.mock_engine import generate_mock_train_schedule, load_stations, load_tracks
from tui.renderer import (
    PaneGeometry,
    TUIRenderer,
    calculate_timetable_visible_rows,
)
from tui.state import (
    StateSnapshot,
    TimetableStopDTO,
    TrainRecord,
    TrainStateStore,
    TrainTimetableDTO,
)


def create_test_position(
    train_id: str = "T1",
    train_number: str = "1597",
    lat: float = 38.1,
    lng: float = 23.8,
) -> TrainPosition:
    """Helper creating a valid TrainPosition."""
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="460105",
        name=train_number,
        train_type="Proastiakos",
        service_type="Suburban",
        status="RUNNING",
        origin="Athens",
        destination="Chalkida",
        next_station="Afidnes",
        speed=95.0,
        course=0.0,
        track_heading=0.0,
        lat=lat,
        lng=lng,
        altitude=150.0,
        delay=2,
        progress=0.35,
        chainage_m=28000,
        corridor="PATHE",
        distance_to_destination_m=55000,
        timestamp="2026-11-21T14:30:00Z",
        schedule_id="sch-1597",
    )


class MockWindow:
    """Mock curses window recording drawing operations."""

    def __init__(self, height: int = 30, width: int = 100) -> None:
        self.height = height
        self.width = width
        self.calls: list = []
        self.strings: list = []

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.calls.append(("erase",))

    def noutrefresh(self):
        self.calls.append(("noutrefresh",))

    def addstr(self, *args):
        self.calls.append(("addstr", args))
        if len(args) >= 3 and isinstance(args[2], str):
            self.strings.append(args[2])

    def addnstr(self, *args):
        self.calls.append(("addnstr", args))
        if len(args) >= 3 and isinstance(args[2], str):
            self.strings.append(args[2])


class TestTUITimetableModal(unittest.TestCase):
    """Test suite for TUI timetable inspector modal."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stations = list(load_stations().values())
        cls.tracks = list(load_tracks().values())

    def _create_app(self) -> TUIApp:
        store = TrainStateStore()
        mock_worker = MagicMock()
        mock_worker.is_running = True
        mock_worker.is_paused = False
        mock_stdscr = MockWindow(30, 100)
        app = TUIApp(
            stdscr=mock_stdscr,
            store=store,
            worker=mock_worker,
            stations=self.stations,
            tracks=self.tracks,
            ascii_mode=False,
        )
        app.geometry = PaneGeometry.calculate(30, 100)
        return app

    def test_tc_tui_sch_01_modal_toggle_with_selected_train(self) -> None:
        """TC-TUI-SCH-01: Pressing 'i' with a train selected opens the timetable modal."""
        app = self._create_app()
        pos = create_test_position("T1", "1597")
        app.store.update_positions([pos])
        app.store.select_by_id("T1")

        self.assertFalse(app.show_timetable)
        self.assertEqual(app.input_mode, InputMode.NORMAL)

        # Press 'i'
        handled = app.process_input(ord("i"))
        self.assertTrue(handled)
        self.assertTrue(app.show_timetable)
        self.assertEqual(app.input_mode, InputMode.TIMETABLE)
        self.assertIsNotNone(app._active_timetable)

    def test_tc_tui_sch_02_modal_toggle_without_selected_train(self) -> None:
        """TC-TUI-SCH-02: Pressing 'i' with no train selected triggers toast and remains in NORMAL mode."""
        app = self._create_app()
        self.assertIsNone(app.store.get_snapshot().selected_train)

        handled = app.process_input(ord("i"))
        self.assertTrue(handled)
        self.assertFalse(app.show_timetable)
        self.assertEqual(app.input_mode, InputMode.NORMAL)
        self.assertIsNotNone(app.toast_message)
        self.assertIn("Select a train first", app.toast_message)

    def test_tc_tui_sch_03_modal_dismissal_keys(self) -> None:
        """TC-TUI-SCH-03: Dismiss timetable modal using Esc, q, and i."""
        app = self._create_app()
        pos = create_test_position("T1", "1597")
        app.store.update_positions([pos])
        app.store.select_by_id("T1")

        # Open modal
        app.process_input(ord("i"))
        self.assertTrue(app.show_timetable)

        # Dismiss with Esc (27)
        app.process_input(27)
        self.assertFalse(app.show_timetable)
        self.assertEqual(app.input_mode, InputMode.NORMAL)

        # Reopen and dismiss with 'q'
        app.process_input(ord("i"))
        self.assertTrue(app.show_timetable)
        app.process_input(ord("q"))
        self.assertFalse(app.show_timetable)

        # Reopen and dismiss with 'i'
        app.process_input(ord("i"))
        self.assertTrue(app.show_timetable)
        app.process_input(ord("i"))
        self.assertFalse(app.show_timetable)

    def test_tc_tui_sch_04_vertical_scrolling_keys(self) -> None:
        """TC-TUI-SCH-04: Test vertical navigation keys in timetable mode."""
        app = self._create_app()
        pos = create_test_position("T1", "1597")
        app.store.update_positions([pos])
        app.store.select_by_id("T1")

        app.process_input(ord("i"))
        self.assertTrue(app.show_timetable)

        # Initial offset
        initial_offset = app.timetable_scroll_offset

        # Scroll down with 'j'
        app.process_input(ord("j"))
        self.assertGreaterEqual(app.timetable_scroll_offset, initial_offset)

        # Scroll down with KEY_DOWN
        prev_offset = app.timetable_scroll_offset
        app.process_input(curses.KEY_DOWN)
        self.assertGreaterEqual(app.timetable_scroll_offset, prev_offset)

        # Scroll up with 'k'
        app.process_input(ord("k"))
        self.assertLessEqual(app.timetable_scroll_offset, app.timetable_scroll_offset + 1)

        # Home key resets to 0
        app.process_input(getattr(curses, "KEY_HOME", -1))
        self.assertEqual(app.timetable_scroll_offset, 0)

    def test_tc_tui_sch_05_mouse_scrolling_and_click_dismiss(self) -> None:
        """TC-TUI-SCH-05: Test mouse scroll and click outside dismissal in timetable mode."""
        app = self._create_app()
        pos = create_test_position("T1", "1597")
        app.store.update_positions([pos])
        app.store.select_by_id("T1")

        app.process_input(ord("i"))
        self.assertTrue(app.show_timetable)

        # Wheel down
        with patch("curses.getmouse", return_value=(0, 50, 15, 0, MOUSE_SCROLL_DOWN_MASK)):
            app._handle_mouse_input()
        self.assertGreaterEqual(app.timetable_scroll_offset, 0)

        # Wheel up
        with patch("curses.getmouse", return_value=(0, 50, 15, 0, MOUSE_SCROLL_UP_MASK)):
            app._handle_mouse_input()

        # Click outside modal (top left corner x=0, y=0)
        with patch("curses.getmouse", return_value=(0, 0, 0, 0, MOUSE_LEFT_CLICK_MASK)):
            app._handle_mouse_input()
        self.assertFalse(app.show_timetable)
        self.assertEqual(app.input_mode, InputMode.NORMAL)

    def test_tc_tui_sch_06_render_timetable_modal_layout(self) -> None:
        """TC-TUI-SCH-06: Test TUIRenderer.render_timetable_modal drawing on mock curses window."""
        renderer = TUIRenderer(stations=self.stations, tracks=self.tracks, ascii_mode=False)
        win = MockWindow(28, 90)
        timetable = generate_mock_train_schedule("1597")

        renderer.render_timetable_modal(
            win=win,
            height=28,
            width=90,
            timetable=timetable,
            scroll_offset=0,
        )

        # Verify border and header were drawn
        found_title = any("TIMETABLE & ITINERARY" in s for s in win.strings)
        self.assertTrue(found_title, f"Expected title in rendered strings: {win.strings}")
        found_header = any("STATION" in s for s in win.strings)
        self.assertTrue(found_header)

    def test_tc_tui_sch_07_render_timetable_modal_loading_and_error(self) -> None:
        """TC-TUI-SCH-07: Test modal rendering during async loading and error states."""
        renderer = TUIRenderer(stations=self.stations, tracks=self.tracks, ascii_mode=False)
        win_loading = MockWindow(24, 80)
        renderer.render_timetable_modal(win_loading, 24, 80, is_loading=True)
        self.assertTrue(any("Loading timetable" in s for s in win_loading.strings))

        win_err = MockWindow(24, 80)
        renderer.render_timetable_modal(win_err, 24, 80, error_msg="HTTP 503")
        self.assertTrue(any("HTTP 503" in s for s in win_err.strings))

    def test_tc_tui_sch_08_async_live_fetching_and_caching(self) -> None:
        """TC-TUI-SCH-08: Test live async fetcher background thread and in-memory TTL caching."""
        app = self._create_app()
        # Simulate live worker with client
        app.worker.client = MagicMock()
        app.worker.mock_engine = None

        pos = create_test_position("T1", "1597")
        app.store.update_positions([pos])
        app.store.select_by_id("T1")

        mock_timetable = generate_mock_train_schedule("1597")
        mock_client = MagicMock()
        mock_client.get_schedule_by_train_number.return_value = mock_timetable

        with patch("train_tracker.RailwayGovClient", return_value=mock_client):
            app._fetch_timetable_for_train("1597", app.store.get_snapshot().selected_train)
            import time
            for _ in range(50):
                if "1597" in app._timetable_cache:
                    break
                time.sleep(0.01)

        self.assertIn("1597", app._timetable_cache)
        self.assertEqual(app._active_timetable["trainNumber"], "1597")
        self.assertFalse(app._timetable_loading)

        # Re-fetch should hit cache without creating client or calling API again
        with patch("train_tracker.RailwayGovClient") as mock_cls:
            app._fetch_timetable_for_train("1597", app.store.get_snapshot().selected_train)
            mock_cls.assert_not_called()

    def test_tc_tui_sch_09_render_live_api_payload_modal(self) -> None:
        """TC-TUI-SCH-09: Test modal rendering with real-world live API payload."""
        renderer = TUIRenderer(stations=self.stations, tracks=self.tracks, ascii_mode=False)
        win = MockWindow(28, 90)
        live_payload = {
            "scheduleId": "6a94743cd16c475ca1449ec2",
            "scheduleNumber": "1239-20261121-2332",
            "trainNumber": "1239",
            "serviceType": "Suburban",
            "corridor": "PIRAIR",
            "scheduledDeparture": "2026-11-21T21:32:00.000Z",
            "scheduledArrival": "2026-11-21T22:26:00.000Z",
            "origin": {"name": "Αεροδρόμιο", "nameGreek": "Αεροδρόμιο"},
            "destination": {"name": "Ταύρος", "nameGreek": "Ταύρος"},
            "stops": [
                {
                    "_id": "0",
                    "stationId": "6928c3c150ab5bc72b39d006",
                    "order": 1,
                    "scheduledArrival": "2026-11-21T21:37:00.000Z",
                    "scheduledDeparture": "2026-11-21T21:37:00.000Z",
                    "actualArrival": None,
                    "actualDeparture": None,
                    "status": "scheduled",
                    "platform": None,
                    "dwellTime": 0,
                    "name": "Κορωπί",
                },
                {
                    "_id": "1",
                    "stationId": "6928c3c150ab5bc72b39cec9",
                    "order": 2,
                    "scheduledArrival": "2026-11-21T21:43:00.000Z",
                    "scheduledDeparture": "2026-11-21T21:43:00.000Z",
                    "actualArrival": None,
                    "actualDeparture": None,
                    "status": "scheduled",
                    "platform": None,
                    "dwellTime": 0,
                    "name": "Παιανία-Κάντζα",
                },
            ],
        }

        renderer.render_timetable_modal(
            win=win,
            height=28,
            width=90,
            timetable=live_payload,
            scroll_offset=0,
        )

        all_rendered = " ".join(win.strings)
        # Check station names rendered cleanly
        self.assertIn("Koropi", all_rendered)
        self.assertIn("Paiania-Kantza", all_rendered)
        # Check time rendered as Athens local time
        self.assertIn("23:37", all_rendered)
        self.assertIn("23:43", all_rendered)
        # Check hex ObjectId and year strings are NOT present
        self.assertNotIn("6928c3c150ab5bc72b3", all_rendered)
        self.assertNotIn("2026-", all_rendered)
        # Check title contains route endpoints
        self.assertIn("TIMETABLE & ITINERARY: TRAIN 1239", all_rendered)
        self.assertTrue("Athens" in all_rendered or "Aerodromio" in all_rendered)


    def test_tc_tui_sch_10_calculate_timetable_visible_rows(self) -> None:
        """TC-TUI-SCH-10: Test calculation of visible stops rows across varying terminal heights."""
        # Minimum clamped terminal height (< 14 -> box_h=12 -> 12-5=7)
        self.assertEqual(calculate_timetable_visible_rows(10), 7)
        # Compact terminal height (15 -> box_h=13 -> 13-5=8)
        self.assertEqual(calculate_timetable_visible_rows(15), 8)
        # Standard terminal height (24 -> box_h=22 -> 22-5=17)
        self.assertEqual(calculate_timetable_visible_rows(24), 17)
        # Large terminal height (50 -> box_h=22 (capped) -> 22-5=17)
        self.assertEqual(calculate_timetable_visible_rows(50), 17)

    def test_tc_tui_sch_11_viewport_synchronized_keyboard_scrolling(self) -> None:
        """TC-TUI-SCH-11: Verify max_offset prevents ghost scrolling when stops <= visible_rows."""
        app = self._create_app()
        # Mock timetable with 14 stops (visible_rows = 17 on 30x100 terminal)
        stops_14 = [{"stationId": f"stn_{i}", "name": f"Station {i}", "order": i} for i in range(14)]
        mock_sched = {"trainNumber": "1239", "stops": stops_14}
        app._active_timetable = mock_sched
        app.input_mode = InputMode.TIMETABLE
        app.show_timetable = True
        app.timetable_scroll_offset = 0

        # Press 'j' / KEY_DOWN: max_offset must be max(0, 14 - 17) = 0. Offset remains 0 (no ghost scrolling).
        app._handle_timetable_input(ord("j"))
        self.assertEqual(app.timetable_scroll_offset, 0)
        app._handle_timetable_input(curses.KEY_DOWN)
        self.assertEqual(app.timetable_scroll_offset, 0)

        # Mock timetable with 25 stops: max_offset must be max(0, 25 - 17) = 8.
        stops_25 = [{"stationId": f"stn_{i}", "name": f"Station {i}", "order": i} for i in range(25)]
        app._active_timetable = {"trainNumber": "IC52", "stops": stops_25}

        for _ in range(15):
            app._handle_timetable_input(ord("j"))
        # Clamped precisely at max_offset = 8
        self.assertEqual(app.timetable_scroll_offset, 8)

        # Scroll back up with 'k'
        app._handle_timetable_input(ord("k"))
        self.assertEqual(app.timetable_scroll_offset, 7)

    def test_tc_tui_sch_12_page_up_page_down_and_end_navigation(self) -> None:
        """TC-TUI-SCH-12: Test Page Down, Page Up, End, and Home keys with synchronized bounds."""
        app = self._create_app()
        stops_30 = [{"stationId": f"stn_{i}", "name": f"Station {i}", "order": i} for i in range(30)]
        app._active_timetable = {"trainNumber": "IC52", "stops": stops_30}
        app.input_mode = InputMode.TIMETABLE
        app.show_timetable = True
        app.timetable_scroll_offset = 0

        # On 30-row terminal, visible_rows = 17, max_offset = 30 - 17 = 13.
        # page_step = max(1, 17 - 2) = 15.
        app._handle_timetable_input(getattr(curses, "KEY_NPAGE", -1))
        self.assertEqual(app.timetable_scroll_offset, 13)

        app._handle_timetable_input(getattr(curses, "KEY_PPAGE", -1))
        self.assertEqual(app.timetable_scroll_offset, 0)

        app._handle_timetable_input(getattr(curses, "KEY_END", -1))
        self.assertEqual(app.timetable_scroll_offset, 13)

        app._handle_timetable_input(getattr(curses, "KEY_HOME", -1))
        self.assertEqual(app.timetable_scroll_offset, 0)

    def test_tc_tui_sch_13_mouse_wheel_clamping_to_visible_rows(self) -> None:
        """TC-TUI-SCH-13: Mouse wheel scrolling clamps to stops_count - visible_rows."""
        app = self._create_app()
        stops_20 = [{"stationId": f"stn_{i}", "name": f"Station {i}", "order": i} for i in range(20)]
        app._active_timetable = {"trainNumber": "IC52", "stops": stops_20}
        app.input_mode = InputMode.TIMETABLE
        app.show_timetable = True
        app.timetable_scroll_offset = 0

        # visible_rows = 17, max_offset = 3
        with patch("curses.getmouse", return_value=(0, 50, 15, 0, MOUSE_SCROLL_DOWN_MASK)):
            app._handle_mouse_input()
            self.assertEqual(app.timetable_scroll_offset, 2)
            app._handle_mouse_input()
            self.assertEqual(app.timetable_scroll_offset, 3)
            # Cannot exceed max_offset 3
            app._handle_mouse_input()
            self.assertEqual(app.timetable_scroll_offset, 3)

        with patch("curses.getmouse", return_value=(0, 50, 15, 0, MOUSE_SCROLL_UP_MASK)):
            app._handle_mouse_input()
            self.assertEqual(app.timetable_scroll_offset, 1)

    def test_tc_tui_sch_14_user_scroll_persists_across_async_fetch(self) -> None:
        """TC-TUI-SCH-14: User scrolling sets flag and prevents async completion from resetting offset."""
        app = self._create_app()
        stops_25 = [{"stationId": f"stn_{i}", "name": f"Station {i}", "order": i, "is_current": (i == 10)} for i in range(25)]
        mock_sched = {"trainNumber": "1597", "stops": stops_25}

        app._active_timetable = mock_sched
        app.input_mode = InputMode.TIMETABLE
        app.show_timetable = True
        app.timetable_scroll_offset = 0
        self.assertFalse(app._timetable_user_scrolled)

        # User scrolls manually
        app._handle_timetable_input(ord("j"))
        self.assertTrue(app._timetable_user_scrolled)
        app.timetable_scroll_offset = 5

        # When _fetch_timetable_for_train or auto-scroll completes, offset must remain 5
        app._auto_scroll_to_current_stop()
        # Direct _auto_scroll_to_current_stop updates offset, but _fetch_timetable_for_train guards it:
        app.timetable_scroll_offset = 5
        app.worker.client = MagicMock()
        mock_client = MagicMock()
        mock_client.get_schedule_by_train_number.return_value = mock_sched
        with patch("train_tracker.RailwayGovClient", return_value=mock_client):
            app._fetch_timetable_for_train("1597", None)
            import time
            for _ in range(50):
                if not app._timetable_loading:
                    break
                time.sleep(0.01)

        self.assertEqual(app.timetable_scroll_offset, 5)

    def test_tc_tui_sch_15_live_telemetry_next_station_auto_scroll(self) -> None:
        """TC-TUI-SCH-15: Auto-scroll matches live telemetry next_station and centers viewport."""
        app = self._create_app()
        pos = create_test_position("T1", "1597")
        pos.next_station = "Θήβα"
        app.store.update_positions([pos])
        app.store.select_by_id("T1")

        stops = [
            {"stationId": f"stn_{i}", "name": f"Station {i}", "order": i} for i in range(10)
        ]
        stops.append({"stationId": "stn_thb", "name": "Thiva", "order": 10})
        for i in range(11, 25):
            stops.append({"stationId": f"stn_{i}", "name": f"Station {i}", "order": i})

        app._active_timetable = {"trainNumber": "1597", "stops": stops}
        app.input_mode = InputMode.TIMETABLE
        app.show_timetable = True
        app._timetable_user_scrolled = False

        app._auto_scroll_to_current_stop()
        # Thiva is index 10. visible_rows = 17, visible_rows // 2 = 8.
        # target_offset = 10 - 8 = 2.
        self.assertEqual(app.timetable_scroll_offset, 2)

    def test_tc_tui_sch_16_footer_range_and_all_stops_indicators(self) -> None:
        """TC-TUI-SCH-16: Footer displays 'All N stops displayed' or 'Scroll (Stops start-end of total)'."""
        renderer = TUIRenderer(stations=self.stations, tracks=self.tracks, ascii_mode=False)

        # 1. Fits in viewport (14 stops <= 17 visible rows)
        win1 = MockWindow(24, 80)
        timetable_small = {
            "trainNumber": "1239",
            "stops": [{"stationId": f"s_{i}", "name": f"Stop {i}", "order": i} for i in range(14)],
        }
        renderer.render_timetable_modal(win1, 24, 80, timetable=timetable_small, scroll_offset=0)
        all_txt1 = " ".join(win1.strings)
        self.assertIn("All 14 stops displayed", all_txt1)
        self.assertNotIn("of 14", all_txt1)

        # 2. Exceeds viewport (25 stops > 17 visible rows)
        win2 = MockWindow(24, 80)
        timetable_large = {
            "trainNumber": "IC52",
            "stops": [{"stationId": f"s_{i}", "name": f"Stop {i}", "order": i} for i in range(25)],
        }
        renderer.render_timetable_modal(win2, 24, 80, timetable=timetable_large, scroll_offset=0)
        all_txt2 = " ".join(win2.strings)
        self.assertIn("Scroll (Stops 1-17 of 25)", all_txt2)

        # At scroll offset 8 (bottom)
        win3 = MockWindow(24, 80)
        renderer.render_timetable_modal(win3, 24, 80, timetable=timetable_large, scroll_offset=8)
        all_txt3 = " ".join(win3.strings)
        self.assertIn("Scroll (Stops 9-25 of 25)", all_txt3)


if __name__ == "__main__":
    unittest.main()
