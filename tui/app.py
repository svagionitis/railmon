"""Main Curses application controller and double-buffered event loop.

Manages terminal window initialization, input polling, modal event dispatch
(navigation, interactive search filtering, and help reference dialog),
scale-aware camera kinematics, dynamic resizing, frame-rate synchronization
(15-20 FPS), and clean terminal restoration.
"""

import curses
from enum import Enum, auto
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("railmon." + __name__)

from tui.geo import (
    BoundingBox,
    GREECE_PRESET,
    NORTHERN_GREECE_PRESET,
    PATHE_PRESET,
    PROASTIAKOS_PRESET,
    GeoPoint,
    GeoProjector,
    Viewport,
)
from tui.mock_engine import StationNode, TrackPolyline, load_stations, load_tracks
from tui.renderer import HelpOverlay, PaneGeometry, PlaybackState, TUIRenderer
from tui.state import ConnectionStatus, InputMode, StateSnapshot, TrainStateStore
from tui.stream_worker import StreamWorker


class CycleDirection(Enum):
    """Direction for cycling cursor selection through active train roster."""

    FORWARD = auto()
    BACKWARD = auto()


# Greek railway corridor preset definitions: key -> (BoundingBox, corridor_filter, label)
PRESET_CONFIGS: Dict[int, Tuple[BoundingBox, Optional[str], str]] = {
    ord("0"): (GREECE_PRESET, None, "National Network"),
    ord("1"): (PROASTIAKOS_PRESET, "PROASTIAKOS", "Proastiakos"),
    ord("2"): (PATHE_PRESET, "PATHE", "PATHE Mainline"),
    ord("3"): (NORTHERN_GREECE_PRESET, "NORTHERN", "Northern Greece"),
}


class TUIApp:
    """Application coordinator managing curses lifecycle, modal input, and rendering."""

    def __init__(
        self,
        stdscr: Any,
        store: TrainStateStore,
        worker: StreamWorker,
        stations: Optional[Sequence[StationNode]] = None,
        tracks: Optional[Sequence[TrackPolyline]] = None,
        target_fps: float = 15.0,
        stop_event: Optional[threading.Event] = None,
        ascii_mode: bool = False,
    ):
        """Initialize the TUI application controller.

        Args:
            stdscr: Root curses window provided by curses.wrapper.
            store: Active thread-safe TrainStateStore repository.
            worker: Running StreamWorker ingestion thread.
            stations: List or sequence of StationNode entities.
            tracks: List or sequence of TrackPolyline entities.
            target_fps: Target frames per second (default: 15.0).
            stop_event: Optional external shutdown event triggered by OS signals.
            ascii_mode: Whether to force standard 7-bit ASCII rendering fallback.
        """
        self.stdscr = stdscr
        self.store = store
        self.worker = worker
        self.target_fps = max(1.0, min(15.0, float(target_fps)))
        self.frame_interval = 1.0 / self.target_fps
        self.running = False
        self.stop_event = stop_event
        self.ascii_mode = ascii_mode

        # Interactive state & modal input
        self.input_mode: InputMode = InputMode.NORMAL
        self.search_buffer: str = ""
        self._pre_search_query: str = ""
        self.show_help: bool = False
        self.paused: bool = False

        # Render cycle optimization & dirty-checking state
        self._force_redraw: bool = True
        self._last_rendered_version: Optional[int] = None
        self._last_viewport_state: Optional[Tuple[float, float, float, int, int]] = None
        self._last_clock_sec: Optional[int] = None
        self._last_rendered_msg_rate: Optional[float] = None
        self._last_modal_state: Optional[Tuple[InputMode, str, bool, bool]] = None
        self._render_count: int = 0
        self._skip_count: int = 0

        # Load geospatial network data if not injected
        if stations is None:
            stations = list(load_stations().values())
        if tracks is None:
            tracks = list(load_tracks().values())

        self.stations = stations
        self.tracks = tracks

        # Initialize renderer and viewport projector
        self.renderer = TUIRenderer(stations=self.stations, tracks=self.tracks, ascii_mode=self.ascii_mode)

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

        # Periodic stale train cleanup (defense-in-depth against stream stall)
        self._last_prune_time = time.monotonic()
        self._prune_interval_sec: float = 60.0

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

    @property
    def render_count(self) -> int:
        """Total number of rendered frames executed."""
        return self._render_count

    @property
    def skip_count(self) -> int:
        """Total number of unchanged idle frames skipped."""
        return self._skip_count

    def handle_resize(self) -> None:
        """Respond to terminal resize signal by rebuilding geometry and repainting."""
        try:
            curses.endwin()
            self.stdscr.refresh()
        except curses.error:
            pass

        self.rebuild_windows()
        self.stdscr.clear()
        self._force_redraw = True

    def _update_message_rate(self, total_messages: int) -> None:
        """Compute messages per second ingestion frequency."""
        now = time.monotonic()
        dt = now - self._last_rate_time
        if dt >= 1.0:
            d_msgs = total_messages - self._last_msg_count
            self._current_msg_rate = max(0.0, d_msgs / dt)
            self._last_msg_count = total_messages
            self._last_rate_time = now

    # ------------------------------------------------------------------
    # Camera Kinematics & Viewport Transformations
    # ------------------------------------------------------------------

    def pan(self, dx_cells: int, dy_cells: int) -> None:
        """Pan viewport by character cell offsets with conformal scale compensation.

        Translates discrete terminal cell displacement (2 horizontal sub-pixels per
        column, 4 vertical sub-pixels per row) into continuous WGS84 degree shifts
        using the projector's active conformal scales.

        Args:
            dx_cells: Horizontal column offset (positive = East, negative = West).
            dy_cells: Vertical row offset (positive = South, negative = North).
        """
        viewport = self.projector.viewport
        scale_x = self.projector.get_scale_x()
        scale_y = self.projector.get_scale_y()
        cos_phi0 = self.projector._cos_phi0

        d_lng = (dx_cells * 2.0) / (scale_x * cos_phi0)
        d_lat = -(dy_cells * 4.0) / scale_y

        new_lat = max(34.0, min(43.0, viewport.center.lat + d_lat))
        new_lng = max(19.0, min(29.0, viewport.center.lng + d_lng))

        viewport.center = GeoPoint(lat=new_lat, lng=new_lng)

        # Manual camera panning immediately disengages auto-follow mode
        self.store.set_follow_mode(False)

    def zoom(self, factor: float) -> None:
        """Scale viewport zoom magnification multiplicatively, clamped to limits.

        Args:
            factor: Multiplier for zoom (>1.0 to zoom in, <1.0 to zoom out).
        """
        new_zoom = max(0.2, min(25.0, self.projector.viewport.zoom * factor))
        self.projector.viewport.zoom = new_zoom

    def apply_preset(self, key: int) -> None:
        """Fit viewport to a curated Greek railway corridor preset and update filters.

        Args:
            key: Key code corresponding to preset ('0', '1', '2', or '3').
        """
        if key not in PRESET_CONFIGS:
            return
        bbox, corridor, _ = PRESET_CONFIGS[key]
        self.projector.fit_bounding_box(bbox)
        self.store.set_corridor_filter(corridor)

        # Disengage follow mode if currently followed train is outside the new preset bounds
        snapshot = self.store.get_snapshot()
        if snapshot.follow_mode and snapshot.selected_train:
            pos = snapshot.selected_train.position
            if not bbox.contains(GeoPoint(lat=pos.lat, lng=pos.lng)):
                self.store.set_follow_mode(False)

    def cycle_selection(
        self,
        direction: CycleDirection = CycleDirection.FORWARD,
        *,
        forward: Optional[bool] = None,
    ) -> None:
        """Cycle cursor to next or previous train in filtered roster.

        Args:
            direction: Direction to cycle selection (FORWARD or BACKWARD).
            forward: Optional legacy boolean flag (True -> FORWARD, False -> BACKWARD).
        """
        if forward is not None:
            direction = CycleDirection.FORWARD if forward else CycleDirection.BACKWARD
        elif isinstance(direction, bool):
            direction = CycleDirection.FORWARD if direction else CycleDirection.BACKWARD

        if direction == CycleDirection.FORWARD:
            self.store.select_next()
        else:
            self.store.select_prev()

    def toggle_follow(self) -> None:
        """Toggle real-time auto-follow camera tracking for selected train."""
        snapshot = self.store.get_snapshot()
        if not snapshot.selected_train:
            # If no train is currently selected, select the first active train
            selected_id = self.store.select_next()
            if selected_id:
                self.store.set_follow_mode(True)
        else:
            self.store.toggle_follow_mode()

    def clear_all(self) -> None:
        """Clear search query, corridor filter, and selection."""
        self.store.set_filter_query("")
        self.store.set_corridor_filter(None)
        self.store.clear_selection()
        self.search_buffer = ""
        self._pre_search_query = ""

    def toggle_pause(self) -> None:
        """Toggle paused state for telemetry ingestion."""
        self.paused = not self.paused
        if self.paused:
            self.worker.pause()
            self.store.set_connection_status(ConnectionStatus.PAUSED, "Paused by user")
        else:
            self.worker.resume()
            self.store.set_connection_status(ConnectionStatus.CONNECTED, "Live stream active")

    # ------------------------------------------------------------------
    # Modal Event Dispatch Handlers
    # ------------------------------------------------------------------

    def _handle_normal_input(self, key: int) -> bool:
        """Handle input event in NORMAL navigation mode.

        Args:
            key: Curses key code.

        Returns:
            True to continue application loop; False to exit.
        """
        # Clean exit
        if key in (ord("q"), ord("Q"), 27, 3):  # 'q', 'Q', ESC, or Ctrl+C
            return False

        # Pan calculations (step sized ~8% of viewport dimensions)
        step_x = max(2, int(self.projector.viewport.width_cells * 0.08))
        step_y = max(1, int(self.projector.viewport.height_cells * 0.08))

        if key in (ord("h"), curses.KEY_LEFT):
            self.pan(-step_x, 0)
        elif key in (ord("l"), curses.KEY_RIGHT):
            self.pan(step_x, 0)
        elif key in (ord("k"), curses.KEY_UP):
            self.pan(0, -step_y)
        elif key in (ord("j"), curses.KEY_DOWN):
            self.pan(0, step_y)

        # Zoom scaling
        elif key in (ord("+"), ord("=")):
            self.zoom(1.25)
        elif key in (ord("-"), ord("_")):
            self.zoom(1.0 / 1.25)

        # Preset shortcuts (0, 1, 2, 3)
        elif key in PRESET_CONFIGS:
            self.apply_preset(key)

        # Train selection cycling
        elif key in (ord("\t"), ord("n")):
            self.cycle_selection(CycleDirection.FORWARD)
        elif key in (curses.KEY_BTAB, ord("p"), ord("P")):
            self.cycle_selection(CycleDirection.BACKWARD)

        # Auto-follow camera toggle
        elif key in (ord("f"), ord("F")):
            self.toggle_follow()

        # Clear filters and selection
        elif key in (ord("c"), ord("C")):
            self.clear_all()

        # Search filter prompt activation
        elif key == ord("/"):
            self._pre_search_query = self.store.get_snapshot().filter_query
            self.search_buffer = self._pre_search_query
            self.input_mode = InputMode.SEARCH

        # Toggle help dialog
        elif key == ord("?"):
            self.show_help = not self.show_help
            self.input_mode = InputMode.HELP if self.show_help else InputMode.NORMAL

        # Pause / resume telemetry
        elif key == ord(" "):
            self.toggle_pause()

        return True

    def _handle_search_input(self, key: int) -> bool:
        """Handle input event in SEARCH filter editing mode.

        Args:
            key: Curses key code.

        Returns:
            True to continue application loop; False to exit.
        """
        # Commit query and return to NORMAL mode
        if key in (10, 13, curses.KEY_ENTER):
            self.store.set_filter_query(self.search_buffer)
            self.input_mode = InputMode.NORMAL
            return True

        # Cancel search and revert to previous query
        if key in (27, 3):  # ESC or Ctrl+C
            self.store.set_filter_query(self._pre_search_query)
            self.search_buffer = self._pre_search_query
            self.input_mode = InputMode.NORMAL
            return True

        # Backspace handling
        if key in (curses.KEY_BACKSPACE, 127, 8):
            if self.search_buffer:
                self.search_buffer = self.search_buffer[:-1]
                self.store.set_filter_query(self.search_buffer)
            return True

        # Printable character insertion (ASCII and Unicode)
        if key >= 32:
            try:
                ch = chr(key)
                if ch.isprintable():
                    self.search_buffer += ch
                    self.store.set_filter_query(self.search_buffer)
            except (ValueError, OverflowError):
                pass
            return True

        return True

    def process_input(self, key: int) -> bool:
        """Route keyboard input event based on active InputMode.

        Args:
            key: Curses key code returned from getch().

        Returns:
            True to continue application loop; False to exit.
        """
        if key == curses.KEY_RESIZE:
            self.handle_resize()
            return True

        # In HELP mode, any key closes help dialog
        if self.input_mode == InputMode.HELP:
            self.show_help = False
            self.input_mode = InputMode.NORMAL
            return True

        # In SEARCH mode, route to search input editor
        if self.input_mode == InputMode.SEARCH:
            return self._handle_search_input(key)

        # In NORMAL mode, route to standard navigation handlers
        return self._handle_normal_input(key)

    def run_loop(self) -> None:
        """Execute the double-buffered application event loop at target FPS."""
        self.setup_terminal()
        self.rebuild_windows()
        self.running = True

        try:
            while self.running and (self.stop_event is None or not self.stop_event.is_set()):
                frame_start = time.monotonic()
                is_dirty = self._force_redraw

                # 1. Sample user keyboard input (non-blocking)
                try:
                    key = self.stdscr.getch()
                except curses.error:
                    key = -1

                if key != -1:
                    is_dirty = True
                    if not self.process_input(key):
                        self.running = False
                        break

                # 1b. Periodic stale train cleanup (defense-in-depth against stream stall)
                if (frame_start - self._last_prune_time) >= self._prune_interval_sec:
                    pruned = self.store.prune_stale_trains()
                    self._last_prune_time = frame_start
                    if pruned > 0:
                        is_dirty = True

                # 2. Extract atomic snapshot without lock contention
                snapshot = self.store.get_snapshot()
                self._update_message_rate(snapshot.total_messages_received)

                # 3. Synchronize auto-follow mode center coordinates
                if snapshot.follow_mode and snapshot.selected_train:
                    pos = snapshot.selected_train.position
                    self.projector.viewport.center = GeoPoint(lat=pos.lat, lng=pos.lng)

                # 4. Extract current signatures for dirty check
                current_sec = int(time.time())
                vp = self.projector.viewport
                current_vp = (
                    round(vp.center.lat, 6),
                    round(vp.center.lng, 6),
                    round(vp.zoom, 4),
                    vp.width_cells,
                    vp.height_cells,
                )
                current_modal = (
                    self.input_mode,
                    self.search_buffer,
                    self.show_help,
                    self.paused,
                )

                if (
                    is_dirty
                    or snapshot.version != self._last_rendered_version
                    or current_vp != self._last_viewport_state
                    or current_sec != self._last_clock_sec
                    or self._current_msg_rate != self._last_rendered_msg_rate
                    or current_modal != self._last_modal_state
                ):
                    # 4a. Render all dashboard panes
                    self.renderer.render_all(
                        windows=self.windows,
                        geometry=self.geometry,
                        snapshot=snapshot,
                        projector=self.projector,
                        msg_rate=self._current_msg_rate,
                        input_mode=self.input_mode,
                        search_buffer=self.search_buffer,
                        help_overlay=HelpOverlay.VISIBLE if self.show_help else HelpOverlay.HIDDEN,
                        playback_state=PlaybackState.PAUSED if self.paused else PlaybackState.RUNNING,
                    )

                    # 4b. Flush frame using double buffering (noutrefresh + doupdate)
                    if self.geometry.is_valid:
                        for win in self.windows.values():
                            if win is not self.stdscr:
                                try:
                                    win.noutrefresh()
                                except curses.error:
                                    pass
                        if self.show_help:
                            try:
                                self.stdscr.noutrefresh()
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

                    # 4c. Update cache signatures
                    self._force_redraw = False
                    self._last_rendered_version = snapshot.version
                    self._last_viewport_state = current_vp
                    self._last_clock_sec = current_sec
                    self._last_rendered_msg_rate = self._current_msg_rate
                    self._last_modal_state = current_modal
                    self._render_count += 1
                else:
                    self._skip_count += 1

                # 5. Precise frame-rate sleep
                elapsed = time.monotonic() - frame_start
                sleep_sec = max(0.001, self.frame_interval - elapsed)
                time.sleep(sleep_sec)

        finally:
            self.running = False
            try:
                curses.curs_set(1)  # Restore cursor visibility
            except curses.error:
                pass


def is_tty_supported() -> bool:
    """Validate whether stdout supports interactive full-screen curses rendering.

    Returns:
        bool: True if sys.stdout is an interactive terminal with an active TTY;
              False if redirected to a pipe, regular file, or null device.
    """
    return bool(hasattr(sys.stdout, "isatty") and sys.stdout.isatty())


def run_tui(
    store: TrainStateStore,
    worker: StreamWorker,
    stations: Optional[Sequence[StationNode]] = None,
    tracks: Optional[Sequence[TrackPolyline]] = None,
    target_fps: float = 15.0,
    stop_event: Optional[threading.Event] = None,
    ascii_mode: bool = False,
) -> None:
    """Safely start the Curses TUI application with automatic terminal restoration.

    Args:
        store: Initialized TrainStateStore.
        worker: Initialized StreamWorker daemon thread.
        stations: Optional list of StationNode objects.
        tracks: Optional list of TrackPolyline objects.
        target_fps: Rendering target frames per second.
        stop_event: Optional external shutdown event triggered by OS signals.
        ascii_mode: Whether to force standard 7-bit ASCII rendering fallback.

    Raises:
        curses.error: If standard output is not an interactive TTY or curses initialization fails.
    """
    if not is_tty_supported():
        raise curses.error("Standard output is not an interactive TTY")

    def _curses_main(stdscr: Any) -> None:
        app = TUIApp(
            stdscr=stdscr,
            store=store,
            worker=worker,
            stations=stations,
            tracks=tracks,
            target_fps=target_fps,
            stop_event=stop_event,
            ascii_mode=ascii_mode,
        )
        app.run_loop()

    try:
        curses.wrapper(_curses_main)
    finally:
        worker.stop()
