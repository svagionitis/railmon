"""Main Curses application controller and double-buffered event loop.

Manages terminal window initialization, input polling, modal event dispatch
(navigation, interactive search filtering, and help reference dialog),
scale-aware camera kinematics, dynamic resizing, frame-rate synchronization
(15-20 FPS), and clean terminal restoration.
"""

import curses
from datetime import datetime, timezone
from enum import Enum, auto
import json
import logging
import os
import signal
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple, Union

logger = logging.getLogger("railmon." + __name__)

from storage.cli_formatters import (
    resolve_timetable_station_name,
    transliterate_greek_to_latin,
)
from storage.db import Database
from storage.repository import (
    HistoricalRepository,
    export_in_memory_csv,
    export_in_memory_geojson,
    sanitize_train_identifier,
    validate_export_path,
)
from tui.geo import (
    BoundingBox,
    BrailleCanvas,
    GREECE_PRESET,
    InfrastructureFeature,
    NORTHERN_GREECE_PRESET,
    PATHE_PRESET,
    PROASTIAKOS_PRESET,
    GeoPoint,
    GeoProjector,
    Viewport,
    haversine_distance_km,
)
from tui.mock_engine import (
    StationNode,
    TrackPolyline,
    generate_mock_train_schedule,
    load_infrastructure,
    load_stations,
    load_tracks,
    snap_to_track_network,
)
from tui.renderer import (
    HelpOverlay,
    PaneGeometry,
    PlaybackState,
    TUIRenderer,
    calculate_scroll_offset,
    calculate_timetable_visible_rows,
    compute_station_board_entries,
    normalize_station_key,
)
from tui.state import (
    BreadcrumbMode,
    ConnectionStatus,
    GhostVisibilityMode,
    InfrastructureLayerMode,
    InputMode,
    InspectorMode,
    RosterSortMode,
    StateSnapshot,
    StationLabelMode,
    TrackColorMode,
    TrainStateStore,
)
from tui.theme import ThemeProfile, get_theme_spec
from tui.stream_worker import ReplayWorker, StreamWorker


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

# Platform-agnostic curses key sets (supporting standard ASCII and PDCurses/Windows numpad codes)
ZOOM_IN_KEYS = tuple(
    k
    for k in (
        ord("+"),
        ord("="),
        getattr(curses, "PADPLUS", None),
        getattr(curses, "SHF_PADPLUS", None),
        getattr(curses, "CTL_PADPLUS", None),
    )
    if k is not None
)

ZOOM_OUT_KEYS = tuple(
    k
    for k in (
        ord("-"),
        ord("_"),
        getattr(curses, "PADMINUS", None),
        getattr(curses, "SHF_PADMINUS", None),
        getattr(curses, "CTL_PADMINUS", None),
    )
    if k is not None
)

SEARCH_OPEN_KEYS = tuple(
    k
    for k in (
        ord("/"),
        getattr(curses, "PADSLASH", None),
    )
    if k is not None
)

ENTER_KEYS = tuple(
    k
    for k in (
        10,
        13,
        curses.KEY_ENTER,
        getattr(curses, "PADENTER", None),
    )
    if k is not None
)

# Map Windows/PDCurses numpad key codes to their ASCII characters for search input
NUMPAD_CHAR_MAP: Dict[int, str] = {
    k: ch
    for k, ch in (
        (getattr(curses, "PAD0", None), "0"),
        (getattr(curses, "PADPLUS", None), "+"),
        (getattr(curses, "PADMINUS", None), "-"),
        (getattr(curses, "PADSLASH", None), "/"),
        (getattr(curses, "PADSTAR", None), "*"),
        (getattr(curses, "PADSTOP", None), "."),
    )
    if k is not None
}

# Platform-safe curses mouse event bitmasks
MOUSE_LEFT_CLICK_MASK: int = (
    getattr(curses, "BUTTON1_CLICKED", 0)
    | getattr(curses, "BUTTON1_PRESSED", 0)
)
MOUSE_LEFT_DOUBLE_CLICK_MASK: int = getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
MOUSE_SCROLL_UP_MASK: int = getattr(curses, "BUTTON4_PRESSED", 0) | getattr(
    curses, "BUTTON4_CLICKED", 0
)
# Note: Some older ncurses Python builds omit BUTTON5_PRESSED; 0x200000 is standard NCURSES_BUTTON_PRESSED(5)
MOUSE_SCROLL_DOWN_MASK: int = (
    getattr(curses, "BUTTON5_PRESSED", 0x200000)
    | getattr(curses, "BUTTON5_CLICKED", 0)
)


class TUIApp:
    """Application coordinator managing curses lifecycle, modal input, and rendering."""

    def __init__(
        self,
        stdscr: Any,
        store: TrainStateStore,
        worker: Union[StreamWorker, ReplayWorker],
        stations: Optional[Sequence[StationNode]] = None,
        tracks: Optional[Sequence[TrackPolyline]] = None,
        target_fps: float = 15.0,
        stop_event: Optional[threading.Event] = None,
        ascii_mode: bool = False,
        export_dir: Optional[str] = None,
        infrastructure: Optional[Sequence[InfrastructureFeature]] = None,
        initial_theme: Optional[ThemeProfile] = None,
    ) -> None:
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
            export_dir: Optional base directory for in-TUI trajectory exports (defaults to '.').
            infrastructure: List or sequence of InfrastructureFeature entities.
        """
        self.stdscr = stdscr
        self.store = store
        self.worker = worker
        self.target_fps = max(1.0, min(15.0, float(target_fps)))
        self.frame_interval = 1.0 / self.target_fps
        self.running = False
        self.stop_event = stop_event
        self.ascii_mode = ascii_mode
        self.export_dir: str = export_dir if export_dir is not None else "."

        # Interactive state & modal input
        self.input_mode: InputMode = InputMode.NORMAL
        self.search_buffer: str = ""
        self._pre_search_query: str = ""
        self.show_help: bool = False
        self.show_timetable: bool = False
        self.timetable_scroll_offset: int = 0
        self._timetable_cache: Dict[str, Tuple[float, Any]] = {}
        self._timetable_loading: bool = False
        self._timetable_error: Optional[str] = None
        self._active_timetable: Optional[Any] = None
        self._timetable_user_scrolled: bool = False
        self.paused: bool = False

        # Transient status bar toast notification state (F-4.1)
        self.toast_duration_sec: float = 4.0
        self.toast_message: Optional[str] = None
        self.toast_expiry: float = 0.0
        self.toast_is_error: bool = False

        # Cab video stream asynchronous launcher debounce state (F-7.1)
        self._stream_launch_lock: threading.Lock = threading.Lock()
        self._active_stream_launches: set = set()

        # In-TUI Train Watchdog evaluators (F-6.3)
        self._tui_watch_evaluators: Dict[str, Any] = {}
        self._last_watch_eval_ts: float = 0.0

        # Render cycle optimization & dirty-checking state
        self._force_redraw: bool = True
        self._last_rendered_version: Optional[int] = None
        self._last_viewport_state: Optional[Tuple[float, float, float, int, int]] = None
        self._last_clock_sec: Optional[int] = None
        self._last_rendered_msg_rate: Optional[float] = None
        self._last_modal_state: Optional[Tuple[Any, ...]] = None
        self._render_count: int = 0
        self._skip_count: int = 0

        # Load geospatial network data if not injected
        if stations is None:
            stations = list(load_stations().values())
        if tracks is None:
            tracks = list(load_tracks().values())
        if infrastructure is None:
            infrastructure = list(load_infrastructure().values())

        self.stations = stations
        self.tracks = tracks
        self.infrastructure = infrastructure
        self.store.set_stations(self.stations)
        self._prev_sensor_alert_count: int = 0

        # Initialize renderer and viewport projector
        self.renderer = TUIRenderer(
            stations=self.stations,
            tracks=self.tracks,
            ascii_mode=self.ascii_mode,
            infrastructure=self.infrastructure,
        )
        if initial_theme is not None:
            self.store.set_theme_profile(initial_theme)
            self.renderer.active_theme = initial_theme

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
        self._prune_interval_sec: float = 5.0

    def setup_terminal(self) -> None:
        """Configure curses terminal modes for responsive, non-blocking UI."""
        curses.curs_set(0)  # Hide hardware cursor
        curses.cbreak()  # Pass raw keypresses immediately
        curses.noecho()  # Do not echo key inputs
        self.stdscr.keypad(True)  # Enable arrow & function keys
        self.stdscr.nodelay(True)  # Non-blocking getch()
        try:
            mouse_mask = getattr(curses, "ALL_MOUSE_EVENTS", 0) | getattr(
                curses, "REPORT_MOUSE_POSITION", 0
            )
            curses.mousemask(mouse_mask)
        except curses.error:
            pass  # Gracefully degrade on terminals without mouse support
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
        if self.show_timetable and self._active_timetable:
            stops_count = 0
            if isinstance(self._active_timetable, dict):
                stops_count = len(self._active_timetable.get("stops", []))
            elif hasattr(self._active_timetable, "stops"):
                stops_count = len(getattr(self._active_timetable, "stops", []))
            visible_rows = calculate_timetable_visible_rows(self.geometry.term_height)
            max_offset = max(0, stops_count - visible_rows)
            self.timetable_scroll_offset = max(0, min(max_offset, self.timetable_scroll_offset))
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
        if snapshot.selected_train is None and snapshot.selected_ghost is not None:
            self._set_toast("[Ghost run has no live GPS coordinates to follow]", is_error=True)
            return
        if not snapshot.selected_train:
            # If no train is currently selected, select the first active train
            selected_id = self.store.select_next()
            if selected_id:
                updated = self.store.get_snapshot()
                if updated.selected_train is not None:
                    self.store.set_follow_mode(True)
                elif updated.selected_ghost is not None:
                    self._set_toast(
                        "[Ghost run has no live GPS coordinates to follow]",
                        is_error=True,
                    )
        else:
            self.store.toggle_follow_mode()

    def clear_all(self) -> None:
        """Clear search query, corridor filter, station board mode, and selection."""
        self.store.set_filter_query("")
        self.store.set_corridor_filter(None)
        self.store.set_inspector_mode(InspectorMode.TRAIN)
        self.store.clear_selection()
        self.search_buffer = ""
        self._pre_search_query = ""

    def cycle_sort(self) -> RosterSortMode:
        """Cycle active train roster sorting criterion (Train ID -> Speed -> Delay)."""
        return self.store.cycle_sort_mode()

    def cycle_breadcrumbs(self) -> BreadcrumbMode:
        """Cycle historical breadcrumb trail visibility mode (ALL -> SELECTED -> OFF)."""
        return self.store.cycle_breadcrumb_mode()

    def toggle_ghost_trains(self) -> GhostVisibilityMode:
        """Toggle visibility of untracked scheduled ghost runs (scheduleCards) (F-10.1)."""
        mode = self.store.toggle_ghost_visibility()
        snap = self.store.get_snapshot()
        if mode == GhostVisibilityMode.SHOW:
            self._set_toast(f"[Ghosts: SHOW ({snap.ghost_count} active)]")
        else:
            self._set_toast("[Ghosts: HIDDEN]")
        return mode

    def cycle_station_labels(self) -> StationLabelMode:
        """Cycle station label visibility mode (HUBS_AND_SECONDARY -> HUBS_ONLY -> MARKERS_ONLY)."""
        return self.store.cycle_station_label_mode()

    def toggle_track_colors(self) -> TrackColorMode:
        """Toggle corridor-colored Braille track rendering (UNIFORM <-> CORRIDOR)."""
        return self.store.cycle_track_color_mode()

    def cycle_infrastructure_layers(self) -> InfrastructureLayerMode:
        """Cycle railway infrastructure GIS overlay mode: ALL -> TUNNELS_BRIDGES -> OFF."""
        mode = self.store.cycle_infrastructure_mode()
        self._force_redraw = True
        if mode == InfrastructureLayerMode.ALL:
            self._set_toast("[✓ Infrastructure GIS: All (Tunnels, Bridges & Signals)]")
        elif mode == InfrastructureLayerMode.TUNNELS_BRIDGES:
            self._set_toast("[✓ Infrastructure GIS: Tunnels & Bridges]")
        else:
            self._set_toast("[✓ Infrastructure GIS: Off]")
        return mode

    def cycle_theme(self) -> ThemeProfile:
        """Cycle to next terminal color theme and rebind curses palette (F-7.3)."""
        new_theme = self.store.cycle_theme_profile()
        self.renderer.apply_theme(new_theme)
        spec = get_theme_spec(new_theme)
        self._set_toast(f"[✓ Theme: {spec.display_name}]")
        if getattr(self, "stdscr", None) is not None:
            try:
                self.stdscr.touchwin()
            except curses.error:
                pass
        self._force_redraw = True
        return new_theme

    def _get_candidate_stations(self, corridor_filter: Optional[str]) -> List[StationNode]:
        """Return ordered hub stations matching the active corridor filter (or all stations if none are hubs)."""
        if not self.stations:
            loaded = list(load_stations().values())
            self.stations = loaded
            if not self.renderer.stations:
                self.renderer.stations = loaded
                from tui.renderer import _build_station_index
                self.renderer.station_index = _build_station_index(loaded)
        if not self.stations:
            return []
        hubs = [s for s in self.stations if s.is_hub]
        base_pool = hubs if hubs else list(self.stations)
        if not corridor_filter:
            return base_pool

        target = corridor_filter.strip().upper()
        filtered: List[StationNode] = []
        for stn in base_pool:
            c = (stn.corridor or "").strip().upper()
            if target in c:
                filtered.append(stn)
            elif target in ("PROASTIAKOS", "SUBURBAN") and (
                "PROAST" in c or stn.id in ("PIR", "ATH", "SKA", "INO")
            ):
                filtered.append(stn)
            elif target in ("NORTHERN", "NORTHERN_GREECE", "NORTH") and (
                c in ("NORTHERN", "MACEDONIA") or stn.id == "SKG"
            ):
                filtered.append(stn)
            elif target in ("PATHE", "MAINLINE") and c == "PATHE":
                filtered.append(stn)
        return filtered if filtered else base_pool

    def cycle_station_inspector(self, exit_if_active: bool = False) -> Optional[str]:
        """Toggle or cycle the Station Departure Board & Proximity Inspector mode.

        Args:
            exit_if_active: If True and already in STATION mode, immediately return to TRAIN mode.

        Returns:
            Active selected_station_id, or None if in TRAIN mode.
        """
        snapshot = self.store.get_snapshot()
        if exit_if_active and snapshot.inspector_mode == InspectorMode.STATION:
            self.store.set_inspector_mode(InspectorMode.TRAIN)
            return None

        candidates = self._get_candidate_stations(snapshot.corridor_filter)
        if not candidates:
            self.store.set_inspector_mode(InspectorMode.TRAIN)
            return None

        candidate_ids = [s.id for s in candidates]
        initial_id: Optional[str] = None

        if snapshot.inspector_mode == InspectorMode.TRAIN or snapshot.selected_station_id is None:
            # 1. If a train is selected and its next_station resolves to a known station, start there
            if snapshot.selected_train and snapshot.selected_train.position.next_station:
                resolved = self.renderer.resolve_station(snapshot.selected_train.position.next_station)
                if resolved is not None:
                    initial_id = resolved.id
                    if initial_id not in candidate_ids:
                        candidate_ids.insert(0, initial_id)
            # 2. Otherwise start at the candidate hub station nearest to the current viewport center
            if initial_id is None:
                center_pt = self.projector.viewport.center
                nearest_stn = min(
                    candidates,
                    key=lambda s: haversine_distance_km(center_pt, s.point),
                )
                initial_id = nearest_stn.id

        return self.store.cycle_station_board(candidate_ids, initial_station_id=initial_id)

    def toggle_pause(self) -> None:
        """Toggle paused state for telemetry ingestion or historical DVR playback."""
        self.paused = not self.paused
        snapshot = self.store.get_snapshot()
        if self.paused:
            self.worker.pause()
            msg = "Replay paused" if snapshot.is_replay else "Paused by user"
            self.store.set_connection_status(ConnectionStatus.PAUSED, msg)
        else:
            self.worker.resume()
            msg = (
                f"DVR Replay ({int(snapshot.replay_speed)}x)"
                if snapshot.is_replay
                else "Live stream active"
            )
            self.store.set_connection_status(ConnectionStatus.CONNECTED, msg)

    # ------------------------------------------------------------------
    # Historical DVR Timeline Scrubbing & Speed Controls (F-4.2)
    # ------------------------------------------------------------------

    def scrub_replay(self, delta_sec: float) -> Optional[float]:
        """Scrub historical DVR timeline by delta_sec (e.g. -300.0s for '[' or +300.0s for ']')."""
        snapshot = self.store.get_snapshot()
        if not snapshot.is_replay or not hasattr(self.worker, "seek_relative"):
            return None

        seek_fn = getattr(self.worker, "seek_relative")
        new_epoch = float(seek_fn(delta_sec))
        updated_snap = self.store.get_snapshot()
        start_t = updated_snap.replay_start_time or new_epoch
        end_t = updated_snap.replay_end_time or new_epoch
        span = max(0.0, end_t - start_t)
        pct = int(round(((new_epoch - start_t) / span) * 100)) if span > 1e-6 else 100
        pct = max(0, min(100, pct))

        ts_str = datetime.fromtimestamp(new_epoch, tz=timezone.utc).strftime("%H:%M:%S UTC")
        mins = int(round(delta_sec / 60.0))
        dir_str = f"+{mins}m" if mins > 0 else f"{mins}m"
        if delta_sec < 0.0:
            icon = "<<" if self.ascii_mode else "⏪"
        else:
            icon = ">>" if self.ascii_mode else "⏩"

        self._set_toast(f"[{icon} Scrubbed {dir_str} -> {ts_str} ({pct}%)]", is_error=False)
        return new_epoch

    def change_replay_speed(self, faster: bool = True) -> Optional[float]:
        """Step historical DVR playback speed multiplier up ('>') or down ('<') across 1x..60x."""
        snapshot = self.store.get_snapshot()
        if not snapshot.is_replay or not hasattr(self.worker, "cycle_speed"):
            return None

        cycle_fn = getattr(self.worker, "cycle_speed")
        new_speed = float(cycle_fn(faster=faster))
        icon = ">" if self.ascii_mode else "▶"
        self._set_toast(f"[{icon} Replay speed: {int(new_speed)}x]", is_error=False)
        return new_speed

    # ------------------------------------------------------------------
    # In-TUI Trajectory Snapshot Export & Toast Notifications (F-4.1)
    # ------------------------------------------------------------------

    def _set_toast(
        self,
        message: str,
        duration_sec: Optional[float] = None,
        is_error: bool = False,
    ) -> None:
        """Set a transient status bar toast notification message."""
        dur = self.toast_duration_sec if duration_sec is None else max(0.1, float(duration_sec))
        self.toast_message = message
        self.toast_expiry = time.monotonic() + dur
        self.toast_is_error = is_error
        self._force_redraw = True

    def get_active_toast(self, now: Optional[float] = None) -> Tuple[Optional[str], bool]:
        """Return (toast_message, toast_is_error) if a toast is currently active, else (None, False)."""
        if self.toast_message is None:
            return None, False
        current_mono = time.monotonic() if now is None else now
        if current_mono >= self.toast_expiry:
            self.toast_message = None
            self.toast_is_error = False
            return None, False
        return self.toast_message, self.toast_is_error

    def _try_export_from_sqlite(
        self,
        db_path: str,
        train_no: str,
        norm_fmt: str,
        clean_path: str,
    ) -> bool:
        """Attempt to export train trajectory from SQLite HistoricalRepository if rows exist."""
        if not os.path.exists(db_path):
            return False

        db = Database(db_path, auto_migrate=False)
        try:
            repo = HistoricalRepository(db)
            if norm_fmt == "geojson":
                geojson_data = repo.export_trip_geojson(train_number=train_no)
                if not geojson_data.get("features"):
                    return False
                out_str = json.dumps(geojson_data, indent=2, ensure_ascii=False)
                with open(clean_path, "w", encoding="utf-8") as f:
                    f.write(out_str)
                return True

            history_rows = repo.get_train_history(train_number=train_no)
            if not history_rows:
                return False
            repo.export_csv(
                query_type="telemetry",
                target=clean_path,
                params={"train_number": train_no},
            )
            return True
        finally:
            db.close()

    def export_selected_train_snapshot(self, fmt: str = "geojson") -> Optional[str]:
        """Export the selected train's trajectory to a timestamped GeoJSON or CSV file.

        Resolves trajectory data from SQLite HistoricalRepository when background recording
        (--record) is active and has persisted rows for the train; otherwise synthesizes
        the trajectory directly from the in-memory TrainRecord breadcrumb trail.

        Args:
            fmt: Export format, either 'geojson' (default, bound to 'e') or 'csv' (bound to 'E').

        Returns:
            Canonical absolute path of the exported file on success, or None if export failed.
        """
        norm_fmt = fmt.strip().lower()
        if norm_fmt not in ("geojson", "csv"):
            raise ValueError(f"Unsupported export format '{fmt}': must be 'geojson' or 'csv'")

        snapshot = self.store.get_snapshot()
        selected = snapshot.selected_train
        if selected is None:
            if snapshot.selected_ghost is not None:
                self._set_toast(
                    "[! Cannot export trajectory: ghost run has no GPS telemetry]",
                    is_error=True,
                )
            else:
                self._set_toast("[! No train selected to export]", is_error=True)
            return None

        pos = selected.position
        train_no = (pos.train_number or pos.id or "unknown").strip() or "unknown"
        safe_train_no = sanitize_train_identifier(train_no)
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        ext = "geojson" if norm_fmt == "geojson" else "csv"
        filename = f"railmon_train_{safe_train_no}_{timestamp_str}.{ext}"
        raw_target = os.path.join(self.export_dir, filename)

        try:
            clean_path = validate_export_path(raw_target, base_dir=self.export_dir)
            exported_from_db = False

            active_db = (
                snapshot.recording_db
                if (snapshot.is_recording and snapshot.recording_db)
                else (snapshot.replay_db if snapshot.is_replay else None)
            )
            if active_db:
                exported_from_db = self._try_export_from_sqlite(
                    db_path=active_db,
                    train_no=train_no,
                    norm_fmt=norm_fmt,
                    clean_path=clean_path,
                )

            if not exported_from_db:
                if norm_fmt == "geojson":
                    geojson_data = export_in_memory_geojson(selected)
                    out_str = json.dumps(geojson_data, indent=2, ensure_ascii=False)
                    with open(clean_path, "w", encoding="utf-8") as f:
                        f.write(out_str)
                else:
                    export_in_memory_csv(selected, clean_path)

            ok_icon = "OK" if self.ascii_mode else "✓"
            self._set_toast(f"[{ok_icon} Exported {filename}]", is_error=False)
            logger.info("Exported train %s trajectory snapshot to %s", train_no, clean_path)
            return clean_path

        except (ValueError, PermissionError, OSError) as exc:
            logger.error("Failed to export trajectory snapshot for train %s: %s", train_no, exc)
            self._set_toast(f"[! Export failed: {exc}]", is_error=True)
            return None

    # ------------------------------------------------------------------
    # Modal Event Dispatch Handlers
    # ------------------------------------------------------------------

    def _handle_dvr_and_export_input(self, key: int) -> bool:
        """Dispatch Tier 4 trajectory snapshot export ('e'/'E') and DVR controls ('[', ']', '<', '>')."""
        if key == ord("e"):
            self.export_selected_train_snapshot(fmt="geojson")
            return True
        if key == ord("E"):
            self.export_selected_train_snapshot(fmt="csv")
            return True
        if key == ord("["):
            self.scrub_replay(-300.0)
            return True
        if key == ord("]"):
            self.scrub_replay(300.0)
            return True
        if key in (ord("<"), ord(",")):
            self.change_replay_speed(faster=False)
            return True
        if key in (ord(">"), ord(".")):
            self.change_replay_speed(faster=True)
            return True
        return False

    def _handle_navigation_input(self, key: int) -> bool:
        """Handle panning, zooming, and corridor preset shortcuts."""
        step_x = max(2, int(self.projector.viewport.width_cells * 0.08))
        step_y = max(1, int(self.projector.viewport.height_cells * 0.08))

        if key in (ord("h"), curses.KEY_LEFT):
            self.pan(-step_x, 0)
            return True
        elif key in (ord("l"), curses.KEY_RIGHT):
            self.pan(step_x, 0)
            return True
        elif key in (ord("k"), curses.KEY_UP):
            self.pan(0, -step_y)
            return True
        elif key in (ord("j"), curses.KEY_DOWN):
            self.pan(0, step_y)
            return True
        elif key in ZOOM_IN_KEYS:
            self.zoom(1.25)
            return True
        elif key in ZOOM_OUT_KEYS:
            self.zoom(1.0 / 1.25)
            return True
        elif key in PRESET_CONFIGS:
            self.apply_preset(key)
            return True
        return False

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

        # Panning, zooming, and corridor preset navigation
        if self._handle_navigation_input(key):
            return True

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

        # Cycle train roster sorting (Train ID, Speed, Delay)
        elif key in (ord("s"), ord("S")):
            self.cycle_sort()

        # Cycle historical breadcrumb trail mode (All, Selected, Off)
        elif key in (ord("b"), ord("B")):
            self.cycle_breadcrumbs()

        # Cycle station label visibility mode (All, Hubs Only, Markers Only)
        elif key in (ord("v"), ord("V")):
            self.cycle_station_labels()

        # Toggle corridor-colored Braille track rendering (Uniform, Corridor)
        elif key in (ord("t"), ord("T")):
            self.toggle_track_colors()

        # Cycle Infrastructure GIS Layers: Tunnels, Bridges & Signals (F-7.2)
        elif key in (ord("g"), ord("G")):
            self.cycle_infrastructure_layers()

        # Cycle Station Departure Board & Proximity Inspector Mode
        elif key == ord("m"):
            self.cycle_station_inspector(exit_if_active=False)
        elif key == ord("M"):
            self.cycle_station_inspector(exit_if_active=True)

        # Toggle Train Timetable & Intermediate Stop Inspector modal (F-6.2)
        elif key in (ord("i"), ord("I")):
            self.toggle_timetable_modal()

        # Launch Live Locomotive Forward Cab Video Stream (F-7.1)
        elif key in (ord("w"), ord("W")):
            self.launch_cab_stream_for_selected_train()

        # Toggle Proximity & Delay Push Alerting Watchdog for Selected Train (F-6.3)
        elif key in (ord("a"), ord("A")):
            self.toggle_watch_selected_train()

        # Cycle Terminal Color Theme Profile (F-7.3)
        elif key in (getattr(curses, "KEY_F10", 274), ord("K")):
            self.cycle_theme()

        # Toggle untracked scheduled ghost runs visibility (F-10.1)
        elif key in (ord("u"), ord("U")):
            self.toggle_ghost_trains()

        # Tier 4 export ('e'/'E') & DVR timeline scrubbing / speed ('[', ']', '<', '>')
        elif self._handle_dvr_and_export_input(key):
            pass

        # Search filter prompt activation
        elif key in SEARCH_OPEN_KEYS:
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
        if key in ENTER_KEYS:
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

        # Printable character insertion (ASCII, Unicode, and numpad keys; ignore curses special keys >= 256)
        if key in NUMPAD_CHAR_MAP:
            self.search_buffer += NUMPAD_CHAR_MAP[key]
            self.store.set_filter_query(self.search_buffer)
            return True

        if 32 <= key < 256 or (key > getattr(curses, "KEY_MAX", 548)):
            try:
                ch = chr(key)
                if ch.isprintable():
                    self.search_buffer += ch
                    self.store.set_filter_query(self.search_buffer)
            except (ValueError, OverflowError):
                pass
            return True

        return True

    # ------------------------------------------------------------------
    # Train Timetable & Intermediate Stop Inspector (F-6.2)
    # ------------------------------------------------------------------

    def toggle_timetable_modal(self) -> None:
        """Toggle the Train Timetable & Intermediate Stop Inspector modal dialog."""
        if self.input_mode == InputMode.TIMETABLE:
            self.show_timetable = False
            self.input_mode = InputMode.NORMAL
            self._force_redraw = True
            return

        snapshot = self.store.get_snapshot()
        selected_record: Any = snapshot.selected_train
        train_num: Optional[str] = None
        if snapshot.selected_train is not None:
            train_num = (
                snapshot.selected_train.position.train_number
                or snapshot.selected_train.position.id
            )
        elif snapshot.selected_ghost is not None:
            selected_record = snapshot.selected_ghost
            train_num = (
                snapshot.selected_ghost.ghost.train_number
                or snapshot.selected_ghost.ghost.id
            )

        if not train_num:
            self._set_toast("[Select a train first to inspect timetable]", is_error=True)
            return

        self.show_timetable = True
        self.input_mode = InputMode.TIMETABLE
        self.timetable_scroll_offset = 0
        self._timetable_user_scrolled = False
        self._force_redraw = True
        self._fetch_timetable_for_train(str(train_num), selected_record)

    def _auto_scroll_to_current_stop(self) -> None:
        """Auto-adjust timetable_scroll_offset to vertically center the active calling point."""
        if not self._active_timetable:
            return
        stops = []
        if isinstance(self._active_timetable, dict):
            stops = self._active_timetable.get("stops", [])
        elif hasattr(self._active_timetable, "stops"):
            stops = list(getattr(self._active_timetable, "stops", []))

        if not stops:
            return

        current_idx = 0
        found = False
        for i, s in enumerate(stops):
            if isinstance(s, dict) and (s.get("is_current") or s.get("status") == "APPROACH"):
                current_idx = i
                found = True
                break
            elif hasattr(s, "is_current") and getattr(s, "is_current", False):
                current_idx = i
                found = True
                break

        if not found:
            snapshot = self.store.get_snapshot()
            if snapshot.selected_train and snapshot.selected_train.position.next_station:
                next_st = str(snapshot.selected_train.position.next_station)
                norm_target_latin = normalize_station_key(transliterate_greek_to_latin(next_st))
                norm_raw_target = normalize_station_key(next_st)
                for i, s in enumerate(stops):
                    st_name = resolve_timetable_station_name(s, is_ascii=True, empty_dash="", single_name=True)
                    raw_name = s.get("name", "") if isinstance(s, dict) else getattr(s, "station_name_el", "")
                    norm_st_latin = normalize_station_key(transliterate_greek_to_latin(st_name))
                    norm_raw_latin = normalize_station_key(transliterate_greek_to_latin(str(raw_name)))
                    norm_raw = normalize_station_key(str(raw_name))
                    if (
                        (norm_target_latin and (norm_target_latin in norm_st_latin or norm_target_latin in norm_raw_latin))
                        or (norm_raw_target and (norm_raw_target in norm_raw or norm_raw_target in normalize_station_key(st_name)))
                    ):
                        current_idx = i
                        found = True
                        break

        term_h = self.geometry.term_height if (hasattr(self, "geometry") and self.geometry) else 24
        visible_rows = calculate_timetable_visible_rows(term_h)
        max_offset = max(0, len(stops) - visible_rows)
        self.timetable_scroll_offset = max(0, min(max_offset, current_idx - (visible_rows // 2)))

    def _fetch_timetable_for_train(self, train_num: str, record: Any) -> None:
        """Fetch timetable from cache, mock engine, or upstream REST API asynchronously."""
        clean_num = train_num.strip().upper()

        cached = self._timetable_cache.get(clean_num)
        if cached and (time.time() - cached[0] < 300.0):
            self._active_timetable = cached[1]
            self._timetable_loading = False
            self._timetable_error = None
            if not self._timetable_user_scrolled:
                self._auto_scroll_to_current_stop()
            self._force_redraw = True
            return

        snapshot = self.store.get_snapshot()
        is_demo_or_replay = (
            getattr(snapshot, "is_replay", False)
            or (hasattr(self.worker, "mock_engine") and getattr(self.worker, "mock_engine") is not None)
            or getattr(self.worker, "client", None) is None
        )

        if is_demo_or_replay:
            try:
                mock_sched = generate_mock_train_schedule(clean_num)
                self._timetable_cache[clean_num] = (time.time(), mock_sched)
                self._active_timetable = mock_sched
                self._timetable_loading = False
                self._timetable_error = None
                if not self._timetable_user_scrolled:
                    self._auto_scroll_to_current_stop()
            except Exception as err:
                self._timetable_error = str(err)
                self._timetable_loading = False
            self._force_redraw = True
            return

        self._timetable_loading = True
        self._timetable_error = None
        self._active_timetable = None
        self._force_redraw = True

        def _worker() -> None:
            try:
                from train_tracker import RailwayGovClient
                client = RailwayGovClient()
                is_ghost_rec = hasattr(record, "ghost") and getattr(record, "ghost", None) is not None
                ghost_sched_id = getattr(record.ghost, "schedule_id", None) if is_ghost_rec else None
                data: Optional[Dict[str, Any]] = None
                if is_ghost_rec and ghost_sched_id:
                    try:
                        cand = client.get_schedule_by_id(str(ghost_sched_id))
                        if isinstance(cand, dict) and cand.get("stops"):
                            data = cand
                    except Exception as sched_exc:
                        logger.debug("Schedule lookup by ID %s failed: %s", ghost_sched_id, sched_exc)
                if not data:
                    data = client.get_schedule_by_train_number(clean_num)
                if not data or not data.get("stops"):
                    data = generate_mock_train_schedule(clean_num)
                self._timetable_cache[clean_num] = (time.time(), data)
                self._active_timetable = data
                self._timetable_loading = False
                self._timetable_error = None
                if not self._timetable_user_scrolled:
                    self._auto_scroll_to_current_stop()
            except Exception as err:
                try:
                    data = generate_mock_train_schedule(clean_num)
                    self._timetable_cache[clean_num] = (time.time(), data)
                    self._active_timetable = data
                    self._timetable_loading = False
                    self._timetable_error = None
                    if not self._timetable_user_scrolled:
                        self._auto_scroll_to_current_stop()
                except Exception:
                    self._timetable_error = str(err)
                    self._timetable_loading = False
            self._force_redraw = True

        t = threading.Thread(target=_worker, name=f"timetable-fetch-{clean_num}", daemon=True)
        t.start()

    def _handle_timetable_input(self, key: int) -> bool:
        """Handle input event in TIMETABLE modal inspection mode.

        Args:
            key: Curses key code.

        Returns:
            True to continue application loop; False to exit.
        """
        # Dismiss modal on Esc (27), 'q', 'Q', 'i', 'I', or Enter
        if key in (27, ord("q"), ord("Q"), ord("i"), ord("I"), 10, 13):
            self.show_timetable = False
            self.input_mode = InputMode.NORMAL
            self._force_redraw = True
            return True

        stops_count = 0
        if self._active_timetable:
            if isinstance(self._active_timetable, dict):
                stops_count = len(self._active_timetable.get("stops", []))
            elif hasattr(self._active_timetable, "stops"):
                stops_count = len(getattr(self._active_timetable, "stops", []))

        term_h = self.geometry.term_height if (hasattr(self, "geometry") and self.geometry) else 24
        visible_rows = calculate_timetable_visible_rows(term_h)
        max_offset = max(0, stops_count - visible_rows)
        page_step = max(1, visible_rows - 2)

        # Scroll Up
        if key in (curses.KEY_UP, ord("k"), ord("K")):
            self._timetable_user_scrolled = True
            self.timetable_scroll_offset = max(0, self.timetable_scroll_offset - 1)
            self._force_redraw = True
        # Scroll Down
        elif key in (curses.KEY_DOWN, ord("j"), ord("J")):
            self._timetable_user_scrolled = True
            self.timetable_scroll_offset = min(max_offset, self.timetable_scroll_offset + 1)
            self._force_redraw = True
        # Page Up
        elif key == getattr(curses, "KEY_PPAGE", -1):
            self._timetable_user_scrolled = True
            self.timetable_scroll_offset = max(0, self.timetable_scroll_offset - page_step)
            self._force_redraw = True
        # Page Down
        elif key == getattr(curses, "KEY_NPAGE", -1):
            self._timetable_user_scrolled = True
            self.timetable_scroll_offset = min(max_offset, self.timetable_scroll_offset + page_step)
            self._force_redraw = True
        # Home
        elif key == getattr(curses, "KEY_HOME", -1):
            self._timetable_user_scrolled = True
            self.timetable_scroll_offset = 0
            self._force_redraw = True
        # End
        elif key == getattr(curses, "KEY_END", -1):
            self._timetable_user_scrolled = True
            self.timetable_scroll_offset = max_offset
            self._force_redraw = True

        return True

    # ------------------------------------------------------------------
    # Live Locomotive Forward Cab Video Stream Launcher (F-7.1)
    # ------------------------------------------------------------------

    def launch_cab_stream_for_selected_train(self) -> None:
        """Trigger asynchronous resolution and external player launch for selected train."""
        snapshot = self.store.get_snapshot()
        if not snapshot.selected_train:
            if snapshot.selected_ghost is not None:
                self._set_toast(
                    "Cab video unavailable for untracked ghost run",
                    is_error=True,
                )
            else:
                self._set_toast("Select a train first to launch cab video stream", is_error=True)
            return

        train_num = str(
            snapshot.selected_train.position.train_number
            or snapshot.selected_train.position.id
        ).strip()

        is_demo_or_replay = (
            getattr(snapshot, "is_replay", False)
            or (hasattr(self.worker, "mock_engine") and getattr(self.worker, "mock_engine") is not None)
            or getattr(self.worker, "client", None) is None
        )

        with self._stream_launch_lock:
            if train_num in self._active_stream_launches:
                self._set_toast(f"Cab stream launch already in progress for train {train_num}")
                return
            self._active_stream_launches.add(train_num)

        self._set_toast(f"Resolving cab stream: {train_num}...")

        client = getattr(self.worker, "client", None)
        if not is_demo_or_replay and client is None:
            from train_tracker import RailwayGovClient
            client = RailwayGovClient()

        def _worker() -> None:
            try:
                if is_demo_or_replay:
                    from tui.mock_engine import generate_mock_cab_stream
                    info = generate_mock_cab_stream(train_num)
                else:
                    info = client.get_cab_stream_url(train_num)

                if not info.has_stream or not info.playlist_url:
                    err = info.error_message or f"No forward cab video stream available for train {train_num}"
                    self._set_toast(f"⚠ {err}", is_error=True)
                    return

                from train_tracker import launch_cab_stream
                success, player, msg = launch_cab_stream(info)
                if success:
                    self._set_toast(f"✓ Launched cab stream ({player}): {train_num}", is_error=False)
                else:
                    self._set_toast(f"✗ Failed to launch player: {msg}", is_error=True)
            except Exception as exc:
                self._set_toast(f"✗ Cab stream error: {str(exc)}", is_error=True)
            finally:
                with self._stream_launch_lock:
                    self._active_stream_launches.discard(train_num)

        thread = threading.Thread(target=_worker, name=f"cab-stream-{train_num}", daemon=True)
        thread.start()

    # ------------------------------------------------------------------
    # In-TUI Train Watchdog Alerting Integration (F-6.3)
    # ------------------------------------------------------------------

    def toggle_watch_selected_train(self) -> Optional[bool]:
        """Toggle push alerting watchdog on the currently selected train."""
        snapshot = self.store.get_snapshot()
        selected = snapshot.selected_train
        if selected is None:
            self._set_toast("[Select a train first to set watch alert]", is_error=True)
            return None

        pos = selected.position
        train_no = str(pos.train_number or pos.id or "").strip()
        is_now_watched = self.store.toggle_watch_train(pos.id)
        if is_now_watched:
            self._set_toast(
                f"[✓ Alert Watch ON: {train_no} (Notifying on delay & proximity)]",
                is_error=False,
            )
            try:
                from railmon_watchdog import dispatch_terminal_bell

                dispatch_terminal_bell()
            except Exception as exc:
                logger.debug("Failed to emit audio bell on watch toggle: %s", exc)
        else:
            self._set_toast(f"[Watch Alert OFF: {train_no}]", is_error=False)
        return is_now_watched

    def _evaluate_in_tui_watch_alerts(self, snapshot: StateSnapshot) -> None:
        """Evaluate active watchdog rules for watched trains and trigger in-TUI toasts/alerts."""
        from railmon_watchdog import (
            WatchConfig,
            WatchdogEvaluator,
            dispatch_desktop_notification,
            dispatch_terminal_bell,
        )

        watched = getattr(snapshot, "watched_train_ids", frozenset())
        if not watched:
            self._tui_watch_evaluators.clear()
            return

        # Prune un-watched evaluators
        for train_id in list(self._tui_watch_evaluators.keys()):
            if train_id not in watched:
                del self._tui_watch_evaluators[train_id]

        for train_id in watched:
            record = snapshot.trains.get(train_id)
            if not record:
                for t_id, rec in snapshot.trains.items():
                    if t_id.upper() == train_id.upper():
                        record = rec
                        break
            if not record:
                continue

            pos = record.position
            train_num = str(pos.train_number or pos.id or "").strip()

            if train_id not in self._tui_watch_evaluators:
                dest_stn = pos.destination or None
                cfg = WatchConfig(
                    train_number=train_num,
                    destination_station=dest_stn,
                    notify_eta_minutes=15.0,
                    notify_distance_km=15.0,
                    delay_delta_threshold_min=3.0,
                    cooldown_delay_alert_sec=120.0,
                )
                self._tui_watch_evaluators[train_id] = WatchdogEvaluator(cfg)

            evaluator = self._tui_watch_evaluators[train_id]
            events = evaluator.evaluate_position(pos)
            for ev in events:
                self._set_toast(f"[!] {ev.headline}: {ev.message}", duration_sec=5.0, is_error=True)
                dispatch_terminal_bell()
                dispatch_desktop_notification(ev.headline, ev.message)

    # ------------------------------------------------------------------
    # Mouse Interaction & Spatial Hit-Testing (F-3.1)
    # ------------------------------------------------------------------

    def _build_hit_test_canvas(self, inner_w: int, inner_h: int) -> BrailleCanvas:
        """Construct a BrailleCanvas with rasterized tracks for accurate cell hit-testing."""
        canvas = BrailleCanvas(
            width_cells=max(1, inner_w),
            height_cells=max(1, inner_h),
            ascii_mode=self.ascii_mode,
        )
        for track in self.tracks:
            sub_points = [self.projector.geo_to_subpixel(pt) for pt in track.points]
            canvas.draw_polyline(sub_points)
        return canvas

    def _hit_test_map_train(
        self,
        snapshot: StateSnapshot,
        col: int,
        row: int,
        inner_w: int,
        inner_h: int,
        canvas: BrailleCanvas,
    ) -> Optional[str]:
        """Hit-test inner map cell (col, row) against projected trains (Manhattan radius <= 1)."""
        click_px = col * 2 + 1
        click_py = row * 4 + 2
        best_match: Optional[Tuple[int, int, int, str]] = None

        for idx, record in enumerate(snapshot.filtered_trains):
            pos = record.position
            raw_pt = GeoPoint(lat=pos.lat, lng=pos.lng)
            snapped_pt = snap_to_track_network(
                raw_pt, self.tracks, corridor_hint=pos.corridor
            )
            t_col, t_row = self.projector.resolve_track_cell(snapped_pt, canvas)
            if not (0 <= t_col < inner_w and 0 <= t_row < inner_h):
                continue

            d_manhattan = abs(t_col - col) + abs(t_row - row)
            if d_manhattan <= 1:
                t_px, t_py = self.projector.geo_to_subpixel(snapped_pt)
                d_sub_sq = (t_px - click_px) ** 2 + (t_py - click_py) ** 2
                candidate = (d_manhattan, d_sub_sq, idx, pos.id)
                if best_match is None or candidate < best_match:
                    best_match = candidate

        return best_match[3] if best_match is not None else None

    def _hit_test_map_station(
        self,
        col: int,
        row: int,
        inner_w: int,
        inner_h: int,
        canvas: BrailleCanvas,
    ) -> Optional[str]:
        """Hit-test inner map cell (col, row) against projected StationNode markers (Manhattan radius <= 1)."""
        click_px = col * 2 + 1
        click_py = row * 4 + 2
        best_match: Optional[Tuple[int, bool, int, str]] = None

        for stn in self.stations:
            s_col, s_row = self.projector.resolve_track_cell(stn.point, canvas)
            if not (0 <= s_col < inner_w and 0 <= s_row < inner_h):
                continue

            d_manhattan = abs(s_col - col) + abs(s_row - row)
            if d_manhattan <= 1:
                s_px, s_py = self.projector.geo_to_subpixel(stn.point)
                d_sub_sq = (s_px - click_px) ** 2 + (s_py - click_py) ** 2
                candidate = (d_manhattan, not stn.is_hub, d_sub_sq, stn.id)
                if best_match is None or candidate < best_match:
                    best_match = candidate

        return best_match[3] if best_match is not None else None

    def _handle_roster_mouse(
        self,
        my: int,
        bstate: int,
        snapshot: StateSnapshot,
    ) -> None:
        """Handle mouse click, double-click, and scroll wheel events inside the Roster pane."""
        if bstate & MOUSE_SCROLL_UP_MASK:
            self.cycle_selection(CycleDirection.BACKWARD)
            return
        if bstate & MOUSE_SCROLL_DOWN_MASK:
            self.cycle_selection(CycleDirection.FORWARD)
            return

        is_double = bool(bstate & MOUSE_LEFT_DOUBLE_CLICK_MASK)
        is_click = is_double or bool(bstate & MOUSE_LEFT_CLICK_MASK)
        if not is_click:
            return

        g = self.geometry
        inner_h = g.roster_h - 2
        visible_rows = max(0, inner_h - 1)
        live_count = len(snapshot.filtered_trains)
        ghosts = getattr(snapshot, "filtered_ghosts", ())
        total_count = live_count + len(ghosts)
        if visible_rows <= 0 or total_count == 0:
            return

        r_idx = my - (g.roster_y + 2)
        if not (0 <= r_idx < visible_rows):
            return

        offset = calculate_scroll_offset(snapshot.selected_index, total_count, visible_rows)
        item_idx = offset + r_idx
        if 0 <= item_idx < live_count:
            train_id = snapshot.filtered_trains[item_idx].position.id
            self.store.select_by_id(train_id)
            if is_double:
                self.store.set_follow_mode(True)
        elif live_count <= item_idx < total_count:
            ghost_id = ghosts[item_idx - live_count].ghost.id
            self.store.select_by_id(ghost_id)

    def _handle_map_mouse(
        self,
        mx: int,
        my: int,
        bstate: int,
        snapshot: StateSnapshot,
    ) -> None:
        """Handle mouse click, double-click, and scroll wheel zoom inside the Map pane."""
        if bstate & MOUSE_SCROLL_UP_MASK:
            self.zoom(1.25)
            return
        if bstate & MOUSE_SCROLL_DOWN_MASK:
            self.zoom(0.8)
            return

        is_double = bool(bstate & MOUSE_LEFT_DOUBLE_CLICK_MASK)
        is_click = is_double or bool(bstate & MOUSE_LEFT_CLICK_MASK)
        if not is_click:
            return

        g = self.geometry
        if not (g.map_x < mx < g.map_x + g.map_w - 1 and g.map_y < my < g.map_y + g.map_h - 1):
            return

        inner_w = max(1, g.map_w - 2)
        inner_h = max(1, g.map_h - 2)
        col = mx - g.map_x - 1
        row = my - g.map_y - 1

        canvas = self._build_hit_test_canvas(inner_w, inner_h)
        hit_train_id = self._hit_test_map_train(snapshot, col, row, inner_w, inner_h, canvas)

        if is_double:
            if hit_train_id is not None:
                self.store.select_by_id(hit_train_id)
                self.store.set_follow_mode(True)
            else:
                target_pt = self.projector.subpixel_to_geo(col * 2 + 1, row * 4 + 2)
                new_lat = max(34.0, min(43.0, target_pt.lat))
                new_lng = max(19.0, min(29.0, target_pt.lng))
                self.projector.viewport.center = GeoPoint(lat=new_lat, lng=new_lng)
                self.store.set_follow_mode(False)
            return

        if hit_train_id is not None:
            self.store.select_by_id(hit_train_id)
            return

        hit_station_id = self._hit_test_map_station(col, row, inner_w, inner_h, canvas)
        if hit_station_id is not None:
            self.store.set_selected_station(hit_station_id)

    def _handle_inspector_mouse(
        self,
        my: int,
        bstate: int,
        snapshot: StateSnapshot,
    ) -> None:
        """Handle mouse clicks and wheel scrolling in the Station Board Inspector pane."""
        if snapshot.inspector_mode != InspectorMode.STATION or not snapshot.selected_station_id:
            return

        if bstate & (MOUSE_SCROLL_UP_MASK | MOUSE_SCROLL_DOWN_MASK):
            self.cycle_station_inspector(exit_if_active=False)
            return

        is_double = bool(bstate & MOUSE_LEFT_DOUBLE_CLICK_MASK)
        is_click = is_double or bool(bstate & MOUSE_LEFT_CLICK_MASK)
        if not is_click:
            return

        station = self.renderer.resolve_station(snapshot.selected_station_id)
        if station is None:
            return

        g = self.geometry
        inner_h = g.inspector_h - 2
        if inner_h <= 0:
            return

        sec_hdr_y = 4 if inner_h >= 10 else 3
        rows_start_y = sec_hdr_y + 2
        if rows_start_y > inner_h:
            return

        max_rows = max(0, (inner_h - rows_start_y) + 1)
        row_idx = (my - g.inspector_y) - rows_start_y
        if not (0 <= row_idx < max_rows):
            return

        selected_tid = (
            snapshot.selected_train.position.id
            if snapshot.selected_train
            else (snapshot.selected_ghost.ghost.id if snapshot.selected_ghost else None)
        )
        entries = compute_station_board_entries(
            station=station,
            trains=snapshot.filtered_trains,
            resolve_station_fn=self.renderer.resolve_station,
            selected_train_id=selected_tid,
            ghosts=getattr(snapshot, "filtered_ghosts", ()),
        )
        if 0 <= row_idx < len(entries):
            target_tid = entries[row_idx].train_id
            self.store.select_by_id(target_tid)
            if is_double:
                if target_tid in snapshot.trains:
                    self.store.set_follow_mode(True)
                self.store.set_inspector_mode(InspectorMode.TRAIN)

    def _handle_mouse_input(self) -> bool:
        """Decode curses mouse event and route to Roster, Map, or Inspector region handler."""
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except (curses.error, ValueError, TypeError):
            return True

        if self.input_mode == InputMode.HELP:
            if bstate & (MOUSE_LEFT_CLICK_MASK | MOUSE_LEFT_DOUBLE_CLICK_MASK):
                self.show_help = False
                self.input_mode = InputMode.NORMAL
            return True

        if self.input_mode == InputMode.TIMETABLE:
            term_h = self.geometry.term_height if (hasattr(self, "geometry") and self.geometry) else 24
            visible_rows = calculate_timetable_visible_rows(term_h)
            stops_count = 0
            if self._active_timetable:
                if isinstance(self._active_timetable, dict):
                    stops_count = len(self._active_timetable.get("stops", []))
                elif hasattr(self._active_timetable, "stops"):
                    stops_count = len(getattr(self._active_timetable, "stops", []))
            max_offset = max(0, stops_count - visible_rows)

            if bstate & MOUSE_SCROLL_UP_MASK:
                self._timetable_user_scrolled = True
                self.timetable_scroll_offset = max(0, self.timetable_scroll_offset - 2)
                self._force_redraw = True
                return True
            if bstate & MOUSE_SCROLL_DOWN_MASK:
                self._timetable_user_scrolled = True
                self.timetable_scroll_offset = min(max_offset, self.timetable_scroll_offset + 2)
                self._force_redraw = True
                return True
            if bstate & (MOUSE_LEFT_CLICK_MASK | MOUSE_LEFT_DOUBLE_CLICK_MASK):
                term_w = self.geometry.term_width
                box_h = min(22, max(12, term_h - 2))
                box_w = min(82, max(46, term_w - 4))
                start_y = max(1, (term_h - box_h) // 2)
                start_x = max(1, (term_w - box_w) // 2)
                if my < start_y or my >= start_y + box_h or mx < start_x or mx >= start_x + box_w:
                    self.show_timetable = False
                    self.input_mode = InputMode.NORMAL
                    self._force_redraw = True
                return True
            return True

        g = self.geometry
        if not g.is_valid:
            return True

        snapshot = self.store.get_snapshot()

        # Region 1: Roster Pane
        if (
            g.roster_x <= mx < g.roster_x + g.roster_w
            and g.roster_y <= my < g.roster_y + g.roster_h
        ):
            self._handle_roster_mouse(my, bstate, snapshot)
            return True

        # Region 2: Map Pane
        if (
            g.map_x <= mx < g.map_x + g.map_w
            and g.map_y <= my < g.map_y + g.map_h
        ):
            self._handle_map_mouse(mx, my, bstate, snapshot)
            return True

        # Region 3: Inspector Pane
        if (
            g.inspector_x <= mx < g.inspector_x + g.inspector_w
            and g.inspector_y <= my < g.inspector_y + g.inspector_h
        ):
            self._handle_inspector_mouse(my, bstate, snapshot)
            return True

        return True

    def process_input(self, key: int) -> bool:
        """Route keyboard or mouse input event based on active InputMode.

        Args:
            key: Curses key code returned from getch().

        Returns:
            True to continue application loop; False to exit.
        """
        if key == curses.KEY_RESIZE:
            self.handle_resize()
            return True

        if key == getattr(curses, "KEY_MOUSE", -2):
            return self._handle_mouse_input()

        # In HELP mode, any key closes help dialog
        if self.input_mode == InputMode.HELP:
            self.show_help = False
            self.input_mode = InputMode.NORMAL
            return True

        # In TIMETABLE mode, route to timetable modal input handler (F-6.2)
        if self.input_mode == InputMode.TIMETABLE:
            return self._handle_timetable_input(key)

        # In SEARCH mode, route to search input editor
        if self.input_mode == InputMode.SEARCH:
            return self._handle_search_input(key)

        # In NORMAL mode, route to standard navigation handlers
        return self._handle_normal_input(key)

    def _check_audio_bell_triggers(self, snapshot: StateSnapshot) -> None:
        """Trigger terminal bell audio on elevated sensor anomalies or conflict alerts."""
        curr_sensor_alerts = getattr(snapshot, "frozen_count", 0) + getattr(snapshot, "dropped_count", 0)
        has_sensor_alert = curr_sensor_alerts > self._prev_sensor_alert_count
        self._prev_sensor_alert_count = curr_sensor_alerts
        has_conflict_alert = self.store.consume_conflict_bell()

        if has_sensor_alert or has_conflict_alert:
            try:
                from railmon_watchdog import dispatch_terminal_bell

                dispatch_terminal_bell()
            except (OSError, RuntimeError, ImportError) as exc:
                logger.debug("Failed to dispatch terminal bell: %s", exc)

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
                if snapshot.is_replay and isinstance(self.worker, ReplayWorker):
                    self.paused = self.worker.is_paused
                self._update_message_rate(snapshot.total_messages_received)

                # 3. Synchronize auto-follow mode center coordinates
                if snapshot.follow_mode and snapshot.selected_train:
                    pos = snapshot.selected_train.position
                    self.projector.viewport.center = GeoPoint(lat=pos.lat, lng=pos.lng)

                # 3b. Evaluate In-TUI Watchdog Alerts (F-6.3)
                if snapshot.watched_train_ids and (frame_start - self._last_watch_eval_ts) >= 1.0:
                    self._last_watch_eval_ts = frame_start
                    self._evaluate_in_tui_watch_alerts(snapshot)

                # 3c. Audio bell triggers for sensor health and track conflicts (F-5.1 & F-5.2)
                self._check_audio_bell_triggers(snapshot)

                # 4. Extract current signatures for dirty check
                current_sec = (
                    int(snapshot.replay_virtual_time)
                    if (snapshot.is_replay and snapshot.replay_virtual_time is not None)
                    else int(time.time())
                )
                vp = self.projector.viewport
                current_vp = (
                    round(vp.center.lat, 6),
                    round(vp.center.lng, 6),
                    round(vp.zoom, 4),
                    vp.width_cells,
                    vp.height_cells,
                )
                active_toast_msg, active_toast_err = self.get_active_toast(frame_start)
                current_modal = (
                    self.input_mode,
                    self.search_buffer,
                    self.show_help,
                    self.show_timetable,
                    self.timetable_scroll_offset,
                    self._timetable_loading,
                    self.paused,
                    active_toast_msg,
                    active_toast_err,
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
                        toast_message=active_toast_msg,
                        toast_is_error=active_toast_err,
                        timetable_overlay=self.show_timetable,
                        timetable_data=self._active_timetable,
                        timetable_scroll_offset=self.timetable_scroll_offset,
                        timetable_loading=self._timetable_loading,
                        timetable_error=self._timetable_error,
                    )

                    # 4b. Flush frame using double buffering (noutrefresh + doupdate)
                    if self.geometry.is_valid:
                        for win in self.windows.values():
                            if win is not self.stdscr:
                                try:
                                    win.noutrefresh()
                                except curses.error:
                                    pass
                        if self.show_help or self.show_timetable:
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
    worker: Union[StreamWorker, ReplayWorker],
    stations: Optional[Sequence[StationNode]] = None,
    tracks: Optional[Sequence[TrackPolyline]] = None,
    target_fps: float = 15.0,
    stop_event: Optional[threading.Event] = None,
    ascii_mode: bool = False,
    infrastructure: Optional[Sequence[InfrastructureFeature]] = None,
    initial_theme: Optional[ThemeProfile] = None,
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
        infrastructure: Optional list of InfrastructureFeature objects.

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
            infrastructure=infrastructure,
            initial_theme=initial_theme,
        )
        app.run_loop()

    try:
        curses.wrapper(_curses_main)
    finally:
        worker.stop()
