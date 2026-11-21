"""Unit tests for F-7.1: TUI Locomotive Forward Cab Video Stream Launcher ('w' / 'W').

Validates TUI keybindings, selection prerequisite validation, asynchronous launcher
dispatch, debounce locks, demo mock streams, and status bar toast notifications.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import CabStreamInfo, TrainPosition
from tui.app import InputMode, TUIApp
from tui.mock_engine import load_stations, load_tracks
from tui.state import TrainStateStore


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


def create_test_position(
    train_id: str = "T1",
    train_number: str = "1597",
) -> TrainPosition:
    """Helper creating a test TrainPosition."""
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
        lat=38.1,
        lng=23.8,
        altitude=150.0,
        delay=2,
        progress=0.35,
        chainage_m=28000,
        corridor="PATHE",
        distance_to_destination_m=55000,
        timestamp="2026-11-21T14:30:00Z",
        schedule_id="sch-1597",
    )


class TestTUICabStream(unittest.TestCase):
    """Test suite for TUI cab video stream launcher interaction."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.stations = list(load_stations().values())
        cls.tracks = list(load_tracks().values())

    def _create_app(self, is_demo: bool = False) -> TUIApp:
        store = TrainStateStore()
        mock_worker = MagicMock()
        mock_worker.is_running = True
        mock_worker.is_paused = False
        if is_demo:
            mock_worker.mock_engine = MagicMock()
            mock_worker.client = None
        else:
            mock_worker.mock_engine = None
            mock_worker.client = MagicMock()

        mock_stdscr = MockWindow(30, 100)
        app = TUIApp(
            stdscr=mock_stdscr,
            store=store,
            worker=mock_worker,
            stations=self.stations,
            tracks=self.tracks,
            ascii_mode=False,
        )
        return app

    def test_key_w_without_selection_shows_error_toast(self) -> None:
        app = self._create_app()
        app.store.clear()
        self.assertIsNone(app.store.get_snapshot().selected_train)

        # Dispatch 'w' key
        app.process_input(ord("w"))

        toast, is_error = app.get_active_toast()
        self.assertIsNotNone(toast)
        self.assertTrue(is_error)
        self.assertIn("Select a train first", toast or "")

    def test_key_upper_w_without_selection_shows_error_toast(self) -> None:
        app = self._create_app()
        app.store.clear()

        # Dispatch 'W' key
        app.process_input(ord("W"))

        toast, is_error = app.get_active_toast()
        self.assertIsNotNone(toast)
        self.assertTrue(is_error)
        self.assertIn("Select a train first", toast or "")

    @patch("train_tracker.launch_cab_stream")
    def test_key_w_with_selection_launches_stream(self, mock_launch: MagicMock) -> None:
        mock_launch.return_value = (True, "mpv", "Launched mpv for train 1597.")
        app = self._create_app(is_demo=True)
        pos = create_test_position("T1", "1597")
        app.store.update_positions([pos])
        app.store.select_by_id("T1")

        # Press 'w'
        app.process_input(ord("w"))

        # Wait briefly for background daemon thread
        time.sleep(0.1)

        mock_launch.assert_called_once()
        toast, is_error = app.get_active_toast()
        self.assertIsNotNone(toast)
        self.assertFalse(is_error)
        self.assertIn("Launched cab stream (mpv): 1597", toast or "")

    @patch("train_tracker.launch_cab_stream")
    def test_debounce_lock_prevents_duplicate_launches(self, mock_launch: MagicMock) -> None:
        app = self._create_app(is_demo=True)
        pos = create_test_position("T1", "1597")
        app.store.update_positions([pos])
        app.store.select_by_id("T1")

        # Simulate launch already in progress
        with app._stream_launch_lock:
            app._active_stream_launches.add("1597")

        app.process_input(ord("w"))

        # Should not launch again
        mock_launch.assert_not_called()
        toast, _ = app.get_active_toast()
        self.assertIn("already in progress", toast or "")

    def test_unavailable_stream_shows_warning_toast(self) -> None:
        app = self._create_app(is_demo=False)
        app.worker.client.get_cab_stream_url.return_value = CabStreamInfo.unavailable("884", "Camera offline.")
        pos = create_test_position("T2", "884")
        app.store.update_positions([pos])
        app.store.select_by_id("T2")

        app.process_input(ord("w"))
        time.sleep(0.1)

        toast, is_error = app.get_active_toast()
        self.assertTrue(is_error)
        self.assertIn("Camera offline", toast or "")
        pos = create_test_position("T2", "884")
        app.store.update_positions([pos])
        app.store.select_by_id("T2")

        app.process_input(ord("w"))
        time.sleep(0.1)

        toast, is_error = app.get_active_toast()
        self.assertTrue(is_error)
        self.assertIn("Camera offline", toast or "")


if __name__ == "__main__":
    unittest.main()
