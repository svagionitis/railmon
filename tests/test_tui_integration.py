"""Headless integration tests for Hellenic Railways TUI pipeline.

Verifies end-to-end data flow:
  MockTelemetryEngine -> StreamWorker -> TrainStateStore -> GeoProjector -> TUIRenderer
without requiring an interactive terminal or live network connection.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

from tui.geo import GREECE_PRESET, GeoProjector, Viewport
from tui.mock_engine import MockTelemetryEngine, load_stations, load_tracks
from tui.renderer import PaneGeometry, TUIRenderer
from tui.state import ConnectionStatus, TrainStateStore
from tui.stream_worker import StreamWorker


class MockCursesWindow:
    """Mock curses window simulating character grid operations without an interactive TTY."""

    def __init__(self, height: int = 35, width: int = 120):
        self.height = height
        self.width = width
        self.strings: list = []
        self.erased = False
        self.boxed = False
        self.refreshed = False

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.erased = True

    def box(self):
        self.boxed = True

    def addstr(self, y, x, text, attr=0):
        self.strings.append((y, x, text, attr))

    def addch(self, y, x, ch, attr=0):
        self.strings.append((y, x, chr(ch), attr))

    def noutrefresh(self):
        self.refreshed = True

    def subwin(self, *args):
        return self


class TestTUIHeadlessIntegration(unittest.TestCase):
    """End-to-end headless pipeline integration test suite."""

    def setUp(self):
        self.stations = list(load_stations().values())
        self.tracks = list(load_tracks().values())
        self.engine = MockTelemetryEngine()
        self.store = TrainStateStore()

    def test_mock_engine_to_store_flow(self):
        """TC-INT-01: MockTelemetryEngine updates flow through StreamWorker into TrainStateStore."""
        worker = StreamWorker(store=self.store, mock_engine=self.engine)
        worker.start()

        try:
            # Poll store until positions arrive
            start_time = time.monotonic()
            while time.monotonic() - start_time < 3.0:
                snapshot = self.store.get_snapshot()
                if len(snapshot.trains) > 0:
                    break
                time.sleep(0.05)

            snapshot = self.store.get_snapshot()
            self.assertGreater(len(snapshot.trains), 0, "Store should contain active trains")
            self.assertEqual(snapshot.connection_status, ConnectionStatus.CONNECTED)

            # Verify that at least one train has breadcrumbs
            found_trail = False
            for train_record in snapshot.trains.values():
                if len(train_record.trail) > 0:
                    found_trail = True
                    break
            self.assertTrue(found_trail, "Train records should maintain breadcrumbs")
        finally:
            worker.stop()

    def test_headless_render_pipeline(self):
        """TC-INT-02: Render full snapshot across all panels with zero exceptions."""
        # Step simulation directly into store
        batch = self.engine.step(1.0)
        self.store.update_positions(batch)
        self.store.select_next()

        snapshot = self.store.get_snapshot()
        self.assertGreater(len(snapshot.trains), 0)
        self.assertIsNotNone(snapshot.selected_train)

        # Initialize projector and renderer
        viewport = Viewport(
            center=GREECE_PRESET.center,
            zoom=1.0,
            width_cells=75,
            height_cells=30,
        )
        projector = GeoProjector(viewport=viewport)
        renderer = TUIRenderer(stations=self.stations, tracks=self.tracks)

        # Mock curses windows
        geometry = PaneGeometry.calculate(height=35, width=120)
        windows = {
            "header": MockCursesWindow(geometry.header_h, geometry.header_w),
            "map": MockCursesWindow(geometry.map_h, geometry.map_w),
            "roster": MockCursesWindow(geometry.roster_h, geometry.roster_w),
            "inspector": MockCursesWindow(geometry.inspector_h, geometry.inspector_w),
            "status": MockCursesWindow(geometry.status_h, geometry.status_w),
        }

        # Render complete dashboard
        renderer.render_all(
            windows=windows,
            geometry=geometry,
            snapshot=snapshot,
            projector=projector,
            search_buffer="",
            show_help=False,
            paused=False,
        )

        # Assert header window received title
        self.assertTrue(windows["header"].erased)
        self.assertGreater(len(windows["header"].strings), 0)

        # Assert map window received draw commands
        self.assertTrue(windows["map"].erased)
        self.assertTrue(windows["map"].boxed)
        self.assertGreater(len(windows["map"].strings), 0)

        # Assert roster window received train entries
        self.assertTrue(windows["roster"].boxed)
        self.assertGreater(len(windows["roster"].strings), 0)

        # Assert inspector received selected train details
        self.assertTrue(windows["inspector"].boxed)
        self.assertGreater(len(windows["inspector"].strings), 0)

    def test_full_lifecycle_clean_exit(self):
        """TC-INT-03: Complete lifecycle startup, ingestion, and clean teardown."""
        worker = StreamWorker(store=self.store, mock_engine=self.engine)
        self.assertFalse(worker.is_running)

        worker.start()
        self.assertTrue(worker.is_running)

        # Let ingestion run briefly
        time.sleep(0.1)

        worker.stop()
        self.assertFalse(worker.is_running)
        self.assertEqual(
            self.store.get_snapshot().connection_status,
            ConnectionStatus.DISCONNECTED,
        )


if __name__ == "__main__":
    unittest.main()
