"""Main Curses application controller and double-buffered event loop.

Manages terminal window initialization, input polling, dynamic resizing,
frame-rate synchronization (15-20 FPS), and clean terminal restoration.
"""

import curses
import os
import signal
import time
from typing import Any, Dict, Optional, Sequence

from tui.geo import GREECE_PRESET, GeoPoint, GeoProjector, Viewport
from tui.mock_engine import StationNode, TrackPolyline, load_stations, load_tracks
from tui.renderer import PaneGeometry, TUIRenderer
from tui.state import StateSnapshot, TrainStateStore
from tui.stream_worker import StreamWorker


class TUIApp:
    """Application coordinator managing curses lifecycle and multi-pane rendering."""

    def __init__(
        self,
        stdscr: Any,
        store: TrainStateStore,
        worker: StreamWorker,
        stations: Optional[Sequence[StationNode]] = None,
        tracks: Optional[Sequence[TrackPolyline]] = None,
        target_fps: float = 20.0,
    ):
        """Initialize the TUI application controller.

        Args:
            stdscr: Root curses window provided by curses.wrapper.
            store: Active thread-safe TrainStateStore repository.
            worker: Running StreamWorker ingestion thread.
            stations: List or sequence of StationNode entities.
            tracks: List or sequence of TrackPolyline entities.
            target_fps: Target frames per second (default: 20.0).
        """
        self.stdscr = stdscr
        self.store = store
        self.worker = worker
        self.target_fps = max(5.0, min(60.0, target_fps))
        self.frame_interval = 1.0 / self.target_fps
        self.running = False

        # Load geospatial network data if not injected
        if stations is None:
            stations = list(load_stations().values())
        if tracks is None:
            tracks = list(load_tracks().values())

        self.stations = stations
        self.tracks = tracks

        # Initialize renderer and viewport projector
        self.renderer = TUIRenderer(stations=self.stations, tracks=self.tracks)

        # Base viewport covers Greece corridor
        viewport = Viewport(
            center=GREECE_PRESET.center,
            zoom=1.0,
            width_cells=80,
            height_cells=24,
            aspect_ratio=2.0,
        )
        self.projector = GeoProjector(viewport)

        # Active curses windows and layout geometry
        self.windows: Dict[str, Any] = {}
        self.geometry: PaneGeometry = PaneGeometry.calculate(24, 80)

        # Telemetry ingestion rate tracking
        self._last_msg_count = 0
        self._last_rate_time = time.monotonic()
        self._current_msg_rate = 0.0

    def setup_terminal(self) -> None:
        """Configure curses terminal modes for responsive, non-blocking UI."""
        curses.curs_set(0)  # Hide hardware cursor
        curses.cbreak()  # Pass raw keypresses immediately
        curses.noecho()  # Do not echo key inputs
        self.stdscr.keypad(True)  # Enable arrow & function keys
        self.stdscr.nodelay(True)  # Non-blocking getch()
        self.renderer.init_colors()

    def rebuild_windows(self) -> None:
        """Calculate screen dimensions and allocate curses subwindows."""
        max_y, max_x = self.stdscr.getmaxyx()
        self.geometry = PaneGeometry.calculate(max_y, max_x)

        # Clear existing window dictionary
        self.windows.clear()
        self.windows["root"] = self.stdscr

        if not self.geometry.is_valid:
            return

        g = self.geometry
        try:
            self.windows["header"] = self.stdscr.subwin(g.header_h, g.header_w, g.header_y, g.header_x)
            self.windows["map"] = self.stdscr.subwin(g.map_h, g.map_w, g.map_y, g.map_x)
            self.windows["roster"] = self.stdscr.subwin(g.roster_h, g.roster_w, g.roster_y, g.roster_x)
            self.windows["inspector"] = self.stdscr.subwin(g.inspector_h, g.inspector_w, g.inspector_y, g.inspector_x)
            self.windows["status"] = self.stdscr.subwin(g.status_h, g.status_w, g.status_y, g.status_x)

            # Update projector inner canvas dimensions
            inner_w = max(1, g.map_w - 2)
            inner_h = max(1, g.map_h - 2)
            self.projector.viewport.width_cells = inner_w
            self.projector.viewport.height_cells = inner_h
        except curses.error:
            # Fall back to root window if subwin allocation fails during rapid resize
            self.windows.clear()
            self.windows["root"] = self.stdscr

    def handle_resize(self) -> None:
        """Respond to terminal resize signal by rebuilding geometry and repainting."""
        try:
            curses.endwin()
            self.stdscr.refresh()
        except curses.error:
            pass

        self.rebuild_windows()
        self.stdscr.clear()

    def _update_message_rate(self, total_messages: int) -> None:
        """Compute messages per second ingestion frequency."""
        now = time.monotonic()
        dt = now - self._last_rate_time
        if dt >= 1.0:
            d_msgs = total_messages - self._last_msg_count
            self._current_msg_rate = max(0.0, d_msgs / dt)
            self._last_msg_count = total_messages
            self._last_rate_time = now

    def process_input(self, key: int) -> bool:
        """Handle single keyboard input event.

        Args:
            key: Curses key code returned from getch().

        Returns:
            True to continue application loop; False to exit.
        """
        if key in (ord("q"), ord("Q"), 27, 3):  # 'q', 'Q', ESC, or Ctrl+C
            return False

        if key == curses.KEY_RESIZE:
            self.handle_resize()
            return True

        # Phase 5 will add interactive navigation (pan, zoom, follow, filter) here
        return True

    def run_loop(self) -> None:
        """Execute the double-buffered application event loop at target FPS."""
        self.setup_terminal()
        self.rebuild_windows()
        self.running = True

        try:
            while self.running:
                frame_start = time.monotonic()

                # 1. Sample user keyboard input (non-blocking)
                try:
                    key = self.stdscr.getch()
                except curses.error:
                    key = -1

                if key != -1:
                    if not self.process_input(key):
                        self.running = False
                        break

                # 2. Extract atomic snapshot without lock contention
                snapshot = self.store.get_snapshot()
                self._update_message_rate(snapshot.total_messages_received)

                # 3. Synchronize auto-follow mode center coordinates
                if snapshot.follow_mode and snapshot.selected_train:
                    pos = snapshot.selected_train.position
                    self.projector.viewport.center = GeoPoint(lat=pos.lat, lng=pos.lng)

                # 4. Render all dashboard panes
                self.renderer.render_all(
                    windows=self.windows,
                    geometry=self.geometry,
                    snapshot=snapshot,
                    projector=self.projector,
                    msg_rate=self._current_msg_rate,
                )

                # 5. Flush frame using double buffering (noutrefresh + doupdate)
                if self.geometry.is_valid:
                    for win in self.windows.values():
                        try:
                            win.noutrefresh()
                        except curses.error:
                            pass
                else:
                    try:
                        self.stdscr.noutrefresh()
                    except curses.error:
                        pass

                try:
                    curses.doupdate()
                except curses.error:
                    pass

                # 6. Precise frame-rate sleep
                elapsed = time.monotonic() - frame_start
                sleep_sec = max(0.001, self.frame_interval - elapsed)
                time.sleep(sleep_sec)

        finally:
            self.running = False
            try:
                curses.curs_set(1)  # Restore cursor visibility
            except curses.error:
                pass


def run_tui(
    store: TrainStateStore,
    worker: StreamWorker,
    stations: Optional[Sequence[StationNode]] = None,
    tracks: Optional[Sequence[TrackPolyline]] = None,
    target_fps: float = 20.0,
) -> None:
    """Safely start the Curses TUI application with automatic terminal restoration.

    Args:
        store: Initialized TrainStateStore.
        worker: Initialized StreamWorker daemon thread.
        stations: Optional list of StationNode objects.
        tracks: Optional list of TrackPolyline objects.
        target_fps: Rendering target frames per second.
    """
    def _curses_main(stdscr: Any) -> None:
        app = TUIApp(
            stdscr=stdscr,
            store=store,
            worker=worker,
            stations=stations,
            tracks=tracks,
            target_fps=target_fps,
        )
        app.run_loop()

    try:
        curses.wrapper(_curses_main)
    finally:
        worker.stop()
