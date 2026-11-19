"""Multi-pane Curses layout and rendering engine for live train map dashboard.

Provides terminal window geometry calculations, color palette management,
Braille track rasterization blitting, station collision avoidance,
directional heading glyphs, windowed scrolling train rosters, and
telemetry inspection panels.
"""

from dataclasses import dataclass
from enum import Enum, auto
import curses
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
import unicodedata

from tui.geo import BrailleCanvas, GeoPoint, GeoProjector
from tui.mock_engine import StationNode, TrackPolyline
from tui.state import ConnectionStatus, InputMode, StateSnapshot, TrainRecord


class HelpOverlay(Enum):
    """Visibility state of the modal keyboard help overlay."""

    HIDDEN = auto()
    VISIBLE = auto()


class PlaybackState(Enum):
    """Execution state of the live telemetry ingestion stream."""

    RUNNING = auto()
    PAUSED = auto()


# Minimum terminal geometry requirements
MIN_COLS = 80
MIN_ROWS = 24

# Curses color pair indices
PAIR_DEFAULT = 1
PAIR_HEADER = 2
PAIR_BORDER = 3
PAIR_TRACK = 4
PAIR_STATION = 5
PAIR_STATION_DIM = 6
PAIR_TRAIN_ON_TIME = 7
PAIR_TRAIN_WARN = 8
PAIR_TRAIN_ALERT = 9
PAIR_TRAIN_PROAST = 10
PAIR_TRAIN_FREIGHT = 11
PAIR_TRAIN_HALTED = 12
PAIR_ROSTER_SELECT = 13
PAIR_TRAIL = 14
PAIR_STATUS_BAR = 15
PAIR_CONNECTED = 16

# Directional compass glyphs and bearings
HEADING_GLYPHS = ("▲", "◥", "►", "◢", "▼", "◣", "◄", "◤")
HEADING_GLYPHS_ASCII = ("^", ">", ">", "v", "v", "<", "<", "^")
COMPASS_POINTS_16 = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


@dataclass(frozen=True)
class RenderGlyphs:
    """Character glyph set for TUI rendering."""

    station_hub: str
    station_normal: str
    trail_dot: str
    callout_arrow: str
    search_cursor: str
    degree_sym: str
    ellipsis: str
    badge_rec: str
    badge_paused: str
    badge_live: str
    badge_connecting: str
    badge_reconnecting: str
    badge_conn_error: str
    badge_demo: str
    warn_icon: str


GLYPHS_UTF8 = RenderGlyphs(
    station_hub="◉",
    station_normal="◉",
    trail_dot="·",
    callout_arrow="◄",
    search_cursor="█",
    degree_sym="°",
    ellipsis="…",
    badge_rec=" [● REC]",
    badge_paused=" ❚❚ PAUSED ",
    badge_live=" ● LIVE ",
    badge_connecting=" ○ CONNECTING... ",
    badge_reconnecting=" ⟳ RECONNECTING... ",
    badge_conn_error=" ✕ CONN ERROR ",
    badge_demo=" ● DEMO MODE ",
    warn_icon="⚠️  ",
)

GLYPHS_ASCII = RenderGlyphs(
    station_hub="#",
    station_normal="+",
    trail_dot=".",
    callout_arrow="<",
    search_cursor="_",
    degree_sym="d",
    ellipsis="...",
    badge_rec=" [* REC]",
    badge_paused=" || PAUSED ",
    badge_live=" * LIVE ",
    badge_connecting=" .. CONNECTING... ",
    badge_reconnecting=" >> RECONNECTING... ",
    badge_conn_error=" [X] CONN ERROR ",
    badge_demo=" * DEMO MODE ",
    warn_icon="[!] ",
)


@dataclass(frozen=True)
class PaneGeometry:
    """Coordinate boundaries and dimensions for all dashboard panes.

    Attributes:
        term_height: Total terminal height in rows.
        term_width: Total terminal width in columns.
        is_valid: True if terminal dimensions meet or exceed MIN_COLS and MIN_ROWS.
        header_y: Header window origin row.
        header_x: Header window origin column.
        header_h: Header window height.
        header_w: Header window width.
        map_y: Map viewport origin row.
        map_x: Map viewport origin column.
        map_h: Map viewport height.
        map_w: Map viewport width.
        roster_y: Roster pane origin row.
        roster_x: Roster pane origin column.
        roster_h: Roster pane height.
        roster_w: Roster pane width.
        inspector_y: Inspector pane origin row.
        inspector_x: Inspector pane origin column.
        inspector_h: Inspector pane height.
        inspector_w: Inspector pane width.
        status_y: Status bar origin row.
        status_x: Status bar origin column.
        status_h: Status bar height.
        status_w: Status bar width.
    """

    term_height: int
    term_width: int
    is_valid: bool

    header_y: int
    header_x: int
    header_h: int
    header_w: int

    map_y: int
    map_x: int
    map_h: int
    map_w: int

    roster_y: int
    roster_x: int
    roster_h: int
    roster_w: int

    inspector_y: int
    inspector_x: int
    inspector_h: int
    inspector_w: int

    status_y: int
    status_x: int
    status_h: int
    status_w: int

    @classmethod
    def calculate(cls, height: int, width: int) -> "PaneGeometry":
        """Compute responsive pane partitions from terminal dimensions.

        Args:
            height: Total terminal row count.
            width: Total terminal column count.

        Returns:
            Calculated PaneGeometry instance.
        """
        is_valid = width >= MIN_COLS and height >= MIN_ROWS

        header_h = 1
        status_h = 1
        usable_h = max(0, height - header_h - status_h)

        # Horizontal split: Map gets ~65%, clamped between 48 and (width - 32)
        min_map_w = min(48, width)
        min_side_w = 32
        ideal_map_w = int(width * 0.65)
        map_w = max(min_map_w, min(width - min_side_w, ideal_map_w))
        map_w = max(1, min(width, map_w))
        side_w = max(0, width - map_w)

        # Vertical split of sidebar: Roster gets ~52% of usable height
        min_roster_h = 8
        min_insp_h = 7
        ideal_roster_h = int(usable_h * 0.52)
        roster_h = max(min_roster_h, min(usable_h - min_insp_h, ideal_roster_h))
        roster_h = max(1, min(usable_h, roster_h))
        inspector_h = max(0, usable_h - roster_h)

        return cls(
            term_height=height,
            term_width=width,
            is_valid=is_valid,
            header_y=0,
            header_x=0,
            header_h=header_h,
            header_w=width,
            map_y=header_h,
            map_x=0,
            map_h=usable_h,
            map_w=map_w,
            roster_y=header_h,
            roster_x=map_w,
            roster_h=roster_h,
            roster_w=side_w,
            inspector_y=header_h + roster_h,
            inspector_x=map_w,
            inspector_h=inspector_h,
            inspector_w=side_w,
            status_y=height - status_h,
            status_x=0,
            status_h=status_h,
            status_w=width,
        )


def get_string_display_width(text: str) -> int:
    """Calculate the monospace terminal column display width of a string.

    Args:
        text: String containing ASCII, Greek, or Unicode glyphs.

    Returns:
        Integer cell width on standard terminal emulators.
    """
    width = 0
    for char in text:
        east_asian = unicodedata.east_asian_width(char)
        if east_asian in ("F", "W"):
            width += 2
        else:
            width += 1
    return width


def truncate_to_display_width(text: str, max_width: int, ellipsis: str = "…") -> str:
    """Truncate a string to fit within a designated terminal cell width.

    Args:
        text: Target text string.
        max_width: Maximum allowed terminal column cells.
        ellipsis: Truncation indicator character.

    Returns:
        Truncated string fitting within max_width cells.
    """
    if max_width <= 0:
        return ""
    if get_string_display_width(text) <= max_width:
        return text

    ell_w = get_string_display_width(ellipsis)
    target_w = max(0, max_width - ell_w)

    current_w = 0
    chars: List[str] = []
    for char in text:
        w = 2 if unicodedata.east_asian_width(char) in ("F", "W") else 1
        if current_w + w > target_w:
            break
        chars.append(char)
        current_w += w

    return "".join(chars) + ellipsis


def get_heading_glyph(heading: Optional[float], ascii_mode: bool = False) -> str:
    """Map a heading degree to one of 8 compass directional glyphs.

    Args:
        heading: Compass angle in degrees (0-360 clockwise from North).
        ascii_mode: Whether to return standard 7-bit ASCII arrows (^>v<).

    Returns:
        One of the 8 heading glyphs.
    """
    glyphs = HEADING_GLYPHS_ASCII if ascii_mode else HEADING_GLYPHS
    if heading is None:
        return glyphs[0]
    octant = int((heading + 22.5) % 360 // 45)
    return glyphs[octant]


def get_compass_bearing(heading: Optional[float], ascii_mode: bool = False) -> str:
    """Format heading angle into degrees and 16-point cardinal direction text.

    Args:
        heading: Compass angle in degrees.
        ascii_mode: Whether to format degrees in ASCII ("d") or Unicode ("°").

    Returns:
        Formatted string, e.g. "342.5° (NNW)", "342.5d (NNW)", or "N/A".
    """
    if heading is None:
        return "N/A"
    deg_sym = "d" if ascii_mode else "°"
    idx = int((heading + 11.25) % 360 // 22.5)
    return f"{heading:5.1f}{deg_sym} ({COMPASS_POINTS_16[idx]})"


def get_delay_color_pair(delay_min: float) -> int:
    """Determine the curses color pair for a train's delay status.

    Args:
        delay_min: Delay in minutes.

    Returns:
        Curses color pair index: ON_TIME (<=3m), WARN (4-10m), or ALERT (>10m).
    """
    if delay_min > 10.0:
        return PAIR_TRAIN_ALERT
    if delay_min > 3.0:
        return PAIR_TRAIN_WARN
    return PAIR_TRAIN_ON_TIME


def get_train_color_pair(record: TrainRecord) -> int:
    """Resolve visual color pair for a train record based on speed, delay, and service type.

    Args:
        record: Active TrainRecord entity.

    Returns:
        Curses color pair index.
    """
    pos = record.position
    if pos.speed is not None and pos.speed < 1.0:
        return PAIR_TRAIN_HALTED

    delay = pos.delay or 0.0
    if delay > 10.0:
        return PAIR_TRAIN_ALERT
    if delay > 3.0:
        return PAIR_TRAIN_WARN

    train_type = (pos.train_type or "").upper()
    service_type = (pos.service_type or "").upper()
    corridor = (pos.corridor or "").upper()

    if (
        "PROAST" in train_type
        or "SUBURBAN" in train_type
        or "PROAST" in service_type
        or "SUBURBAN" in service_type
        or "PROAST" in corridor
        or corridor in ("PIRAIR", "KIATO", "AERODROMIO", "CHALKIDA")
    ):
        return PAIR_TRAIN_PROAST
    if "FREIGHT" in train_type or "CARGO" in train_type or "ΕΜΠΟΡ" in service_type:
        return PAIR_TRAIN_FREIGHT

    return PAIR_TRAIN_ON_TIME


def calculate_scroll_offset(selected_index: int, total_items: int, visible_rows: int) -> int:
    """Calculate the starting scroll index to keep selected item in view.

    Args:
        selected_index: Index of currently selected item (-1 if none).
        total_items: Total count of items in list.
        visible_rows: Maximum visible rows in table pane.

    Returns:
        Zero-based index of first visible row.
    """
    if total_items <= visible_rows or selected_index < 0:
        return 0

    half_window = visible_rows // 2
    offset = selected_index - half_window
    max_offset = total_items - visible_rows

    return max(0, min(offset, max_offset))


def format_progress_bar(progress: Optional[float], bar_width: int) -> str:
    """Construct an ASCII/Unicode route completion progress bar.

    Args:
        progress: Progress ratio from 0.0 to 1.0.
        bar_width: Total character width available for the progress meter.

    Returns:
        Formatted bar string, e.g. "[======>---]  62%".
    """
    if progress is None:
        p_val = 0.0
    else:
        p_val = max(0.0, min(1.0, float(progress)))

    pct_str = f" {int(round(p_val * 100)):3d}%"
    bracket_space = 2  # '[' and ']'
    inner_width = max(3, bar_width - len(pct_str) - bracket_space)

    fill_count = int(round(p_val * inner_width))
    if fill_count <= 0:
        bar_content = "-" * inner_width
    elif fill_count >= inner_width:
        bar_content = "=" * inner_width
    else:
        bar_content = "=" * (fill_count - 1) + ">" + "-" * (inner_width - fill_count)

    return f"[{bar_content}]{pct_str}"


def safe_addstr(
    win: Any,
    y: int,
    x: int,
    text: str,
    attr: int = 0,
    max_w: Optional[int] = None,
    ascii_mode: bool = False,
) -> None:
    """Safely write a string to a curses window, avoiding border wrap and errors.

    Args:
        win: Curses window object.
        y: Row coordinate.
        x: Column coordinate.
        text: String to write.
        attr: Curses attribute mask.
        max_w: Optional explicit column limit.
        ascii_mode: Whether to format truncation ellipsis in ASCII ("...") or Unicode ("…").
    """
    try:
        max_y, max_x = win.getmaxyx()
    except Exception:
        return

    if y < 0 or y >= max_y or x < 0 or x >= max_x:
        return

    available_w = max_x - x
    if max_w is not None:
        available_w = min(available_w, max_w)

    if available_w <= 0:
        return

    display_w = get_string_display_width(text)
    if display_w > available_w:
        el = "..." if ascii_mode else "…"
        text = truncate_to_display_width(text, available_w, ellipsis=el)

    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        # Curses raises error when writing to bottom-right cell; safely ignore
        pass


_INITIALIZED_COLOR_PAIRS: Set[int] = set()


def safe_color_pair(pair_idx: int, fallback_attr: int = curses.A_NORMAL) -> int:
    """Safely return curses color pair attribute or fallback if curses not initialized or pair failed.

    Args:
        pair_idx: Curses color pair index.
        fallback_attr: Curses attribute to return if colors unavailable or uninitialized.

    Returns:
        Color pair attribute integer or fallback.
    """
    global _INITIALIZED_COLOR_PAIRS
    try:
        if curses.has_colors() and pair_idx in _INITIALIZED_COLOR_PAIRS:
            return curses.color_pair(pair_idx)
    except curses.error:
        pass
    return fallback_attr


class TUIRenderer:
    """Multi-pane Curses drawing engine for live train map dashboard."""

    def __init__(
        self,
        stations: Optional[Sequence[StationNode]] = None,
        tracks: Optional[Sequence[TrackPolyline]] = None,
        ascii_mode: bool = False,
    ):
        """Initialize renderer with static railway network vector data.

        Args:
            stations: List or sequence of StationNode objects.
            tracks: List or sequence of TrackPolyline objects.
            ascii_mode: Whether to force standard 7-bit ASCII rendering fallback.
        """
        self.stations: Sequence[StationNode] = stations or []
        self.tracks: Sequence[TrackPolyline] = tracks or []
        self.ascii_mode: bool = ascii_mode
        self.glyphs: RenderGlyphs = GLYPHS_ASCII if ascii_mode else GLYPHS_UTF8

    def init_colors(self) -> None:
        """Initialize standard curses color pairs and semantic visual attributes with error bounds."""
        global _INITIALIZED_COLOR_PAIRS
        _INITIALIZED_COLOR_PAIRS.clear()

        try:
            if not curses.has_colors():
                return
        except curses.error:
            return

        try:
            curses.start_color()
        except curses.error:
            return

        try:
            curses.use_default_colors()
            bg = -1
        except curses.error:
            bg = curses.COLOR_BLACK

        max_pairs = getattr(curses, "COLOR_PAIRS", 64)

        pairs = [
            (PAIR_DEFAULT, curses.COLOR_WHITE, bg),
            (PAIR_HEADER, curses.COLOR_CYAN, bg),
            (PAIR_BORDER, curses.COLOR_BLUE, bg),
            (PAIR_TRACK, curses.COLOR_WHITE, bg),
            (PAIR_STATION, curses.COLOR_YELLOW, bg),
            (PAIR_STATION_DIM, curses.COLOR_YELLOW, bg),
            (PAIR_TRAIN_ON_TIME, curses.COLOR_GREEN, bg),
            (PAIR_TRAIN_WARN, curses.COLOR_YELLOW, bg),
            (PAIR_TRAIN_ALERT, curses.COLOR_RED, bg),
            (PAIR_TRAIN_PROAST, curses.COLOR_CYAN, bg),
            (PAIR_TRAIN_FREIGHT, curses.COLOR_MAGENTA, bg),
            (PAIR_TRAIN_HALTED, curses.COLOR_WHITE, bg),
            (PAIR_ROSTER_SELECT, curses.COLOR_BLACK, curses.COLOR_CYAN),
            (PAIR_TRAIL, curses.COLOR_GREEN, bg),
            (PAIR_STATUS_BAR, curses.COLOR_BLACK, curses.COLOR_WHITE),
            (PAIR_CONNECTED, curses.COLOR_GREEN, bg),
        ]

        for pair_idx, fg, pair_bg in pairs:
            if pair_idx >= max_pairs:
                continue
            try:
                curses.init_pair(pair_idx, fg, pair_bg)
                _INITIALIZED_COLOR_PAIRS.add(pair_idx)
            except curses.error:
                # Silently degrade: safe_color_pair will return fallback_attr
                pass

    def render_undersized(self, win: Any, height: int, width: int) -> None:
        """Render centered warning banner when terminal size falls below minimum.

        Args:
            win: Root curses window.
            height: Current terminal height.
            width: Current terminal width.
        """
        win.erase()
        border_attr = safe_color_pair(PAIR_BORDER, curses.A_NORMAL)
        alert_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD)

        try:
            if self.ascii_mode:
                win.border(ord('|'), ord('|'), ord('-'), ord('-'), ord('+'), ord('+'), ord('+'), ord('+'))
            else:
                win.box()
        except curses.error:
            pass

        warn_title = f"{self.glyphs.warn_icon}TERMINAL WINDOW TOO SMALL"
        lines = [
            (warn_title, alert_attr | curses.A_BOLD),
            ("", 0),
            (f"Current Size:  {width} columns x {height} rows", border_attr),
            (f"Required Size: {MIN_COLS} columns x {MIN_ROWS} rows", border_attr | curses.A_BOLD),
            ("", 0),
            ("Please resize your terminal window to continue...", border_attr),
        ]

        start_y = max(1, (height - len(lines)) // 2)
        for i, (text, attr) in enumerate(lines):
            y = start_y + i
            if 0 <= y < height:
                text_w = get_string_display_width(text)
                x = max(1, (width - text_w) // 2)
                safe_addstr(win, y, x, text, attr, ascii_mode=self.ascii_mode)

    def render_header(self, win: Any, snapshot: StateSnapshot, width: int) -> None:
        """Render the top full-width header strip.

        Args:
            win: Header curses window.
            snapshot: Current StateSnapshot.
            width: Total window width.
        """
        win.erase()
        header_attr = safe_color_pair(PAIR_HEADER, curses.A_BOLD) | curses.A_BOLD
        dim_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL) | curses.A_DIM
        warn_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD

        # Title & Corridor
        corridor_name = snapshot.corridor_filter or "National Network"
        left_text = f" RAILMON | HELLENIC RAILWAYS (railway.gov.gr) | View: {corridor_name}"

        # Train counts
        total_active = len(snapshot.filtered_trains)
        delayed_count = sum(
            1 for t in snapshot.filtered_trains
            if t.position.delay is not None and t.position.delay > 3.0
        )
        delay_pct = int(round((delayed_count / total_active) * 100)) if total_active > 0 else 0

        counts_text = f" | Active: {total_active} | Delayed: {delayed_count} ({delay_pct}%)"

        # System time clock
        time_str = time.strftime(" %H:%M:%S ")

        # Draw left header elements
        safe_addstr(win, 0, 0, left_text, header_attr, ascii_mode=self.ascii_mode)
        left_w = get_string_display_width(left_text)

        cnt_attr = warn_attr if delayed_count > 0 else dim_attr
        safe_addstr(win, 0, left_w, counts_text, cnt_attr, ascii_mode=self.ascii_mode)
        left_w += get_string_display_width(counts_text)

        # Draw recording status badge if recording is active
        if snapshot.is_recording and width > left_w + 12:
            rec_text = self.glyphs.badge_rec
            rec_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            safe_addstr(win, 0, left_w, rec_text, rec_attr, ascii_mode=self.ascii_mode)
            left_w += get_string_display_width(rec_text)

        # Draw right-aligned clock
        time_x = width - get_string_display_width(time_str)
        if time_x > left_w:
            safe_addstr(win, 0, time_x, time_str, dim_attr, ascii_mode=self.ascii_mode)

    def render_map(
        self,
        win: Any,
        snapshot: StateSnapshot,
        projector: GeoProjector,
    ) -> None:
        """Render the map viewport including Braille tracks, stations, and trains.

        Args:
            win: Map curses window.
            snapshot: Current StateSnapshot.
            projector: GeoProjector configured for canvas viewport.
        """
        win.erase()
        max_y, max_x = win.getmaxyx()
        if max_y < 3 or max_x < 3:
            return

        border_attr = safe_color_pair(PAIR_BORDER, curses.A_NORMAL)
        track_attr = safe_color_pair(PAIR_TRACK, curses.A_DIM) | curses.A_DIM
        station_attr = safe_color_pair(PAIR_STATION, curses.A_BOLD) | curses.A_BOLD
        station_dim_attr = safe_color_pair(PAIR_STATION_DIM, curses.A_NORMAL) | curses.A_DIM
        trail_attr = safe_color_pair(PAIR_TRAIL, curses.A_DIM) | curses.A_DIM

        # Draw border
        try:
            if self.ascii_mode:
                win.border(ord('|'), ord('|'), ord('-'), ord('-'), ord('+'), ord('+'), ord('+'), ord('+'))
            else:
                win.box()
        except curses.error:
            pass

        # Title badge
        center = projector.viewport.center
        follow_str = ""
        if snapshot.follow_mode and snapshot.selected_train:
            tn = snapshot.selected_train.position.train_number or snapshot.selected_train.position.id
            follow_str = f" | Follow: #{tn}"
        d_sym = self.glyphs.degree_sym
        title = f" Map Viewport: {center.lat:.2f}{d_sym}N, {center.lng:.2f}{d_sym}E | Zoom: {projector.viewport.zoom:.1f}x{follow_str} "
        safe_addstr(win, 0, 2, f"[{title}]", border_attr | curses.A_BOLD, max_w=max_x - 4, ascii_mode=self.ascii_mode)

        inner_h = max_y - 2
        inner_w = max_x - 2

        # 1. Rasterize tracks using BrailleCanvas
        canvas = BrailleCanvas(width_cells=inner_w, height_cells=inner_h, ascii_mode=self.ascii_mode)
        for track in self.tracks:
            sub_points = [projector.geo_to_subpixel(pt) for pt in track.points]
            canvas.draw_polyline(sub_points)

        # Blit canvas rows to curses buffer
        rows = canvas.render_rows()
        for r_idx, row_str in enumerate(rows):
            safe_addstr(win, 1 + r_idx, 1, row_str, track_attr, max_w=inner_w, ascii_mode=self.ascii_mode)

        # 2. Overlay historical breadcrumbs
        trail_glyph = self.glyphs.trail_dot
        for record in snapshot.trains.values():
            for crumb in record.trail:
                col, row = projector.geo_to_cell(GeoPoint(lat=crumb.lat, lng=crumb.lng))
                if 0 <= col < inner_w and 0 <= row < inner_h:
                    try:
                        win.addstr(1 + row, 1 + col, trail_glyph, trail_attr)
                    except curses.error:
                        pass

        # 3. Overlay Stations with greedy collision avoidance
        occupied_cells: Set[Tuple[int, int]] = set()
        # Sort stations so hub stations take priority over secondary ones
        sorted_stations = sorted(self.stations, key=lambda s: not s.is_hub)

        for stn in sorted_stations:
            col, row = projector.geo_to_cell(stn.point)
            if not (0 <= col < inner_w and 0 <= row < inner_h):
                continue

            # Draw station marker glyph
            glyph = self.glyphs.station_hub if stn.is_hub else self.glyphs.station_normal
            try:
                win.addstr(1 + row, 1 + col, glyph, station_attr)
            except curses.error:
                pass
            occupied_cells.add((col, row))

            # Determine label text and width
            name = stn.name_en or stn.name_el or stn.id
            label = f" {name}"
            label_w = get_string_display_width(label)
            label_start_x = col + 1

            # Check for collision with existing text
            has_collision = False
            for dx in range(label_w):
                if (label_start_x + dx, row) in occupied_cells:
                    has_collision = True
                    break

            if not has_collision and label_start_x + label_w <= inner_w:
                safe_addstr(
                    win,
                    1 + row,
                    1 + label_start_x,
                    label,
                    station_attr if stn.is_hub else station_dim_attr,
                    max_w=inner_w - label_start_x,
                    ascii_mode=self.ascii_mode,
                )
                for dx in range(label_w):
                    occupied_cells.add((label_start_x + dx, row))

        # 4. Overlay Live Trains and Directional Heading Glyphs
        selected_record = snapshot.selected_train
        for record in snapshot.filtered_trains:
            pos = record.position
            col, row = projector.geo_to_cell(GeoPoint(lat=pos.lat, lng=pos.lng))
            if not (0 <= col < inner_w and 0 <= row < inner_h):
                continue

            is_selected = selected_record is not None and record.position.id == selected_record.position.id
            heading_deg = pos.track_heading if pos.track_heading is not None else pos.course
            glyph = get_heading_glyph(heading_deg, ascii_mode=self.ascii_mode)
            pair = get_train_color_pair(record)
            train_attr = safe_color_pair(pair, curses.A_BOLD)

            if is_selected:
                train_attr |= curses.A_REVERSE | curses.A_BOLD

            try:
                win.addstr(1 + row, 1 + col, glyph, train_attr)
            except curses.error:
                pass

            # If selected, render callout label
            if is_selected:
                tn = pos.train_number or pos.id
                spd_str = f"{int(round(pos.speed or 0))}km/h"
                dly_val = int(round(pos.delay or 0))
                dly_str = f"+{dly_val}m" if dly_val > 0 else (f"{dly_val}m" if dly_val < 0 else "0m")
                arrow = self.glyphs.callout_arrow
                tag = f" {arrow} [{tn}] ({spd_str}, {dly_str})"
                safe_addstr(win, 1 + row, 1 + col + 1, tag, train_attr, max_w=inner_w - (col + 1), ascii_mode=self.ascii_mode)

    def render_roster(
        self,
        win: Any,
        snapshot: StateSnapshot,
    ) -> None:
        """Render the Active Trains Roster pane with scrollable table.

        Args:
            win: Roster curses window.
            snapshot: Current StateSnapshot.
        """
        win.erase()
        max_y, max_x = win.getmaxyx()
        if max_y < 3 or max_x < 3:
            return

        border_attr = safe_color_pair(PAIR_BORDER, curses.A_NORMAL)
        header_attr = safe_color_pair(PAIR_HEADER, curses.A_BOLD) | curses.A_BOLD
        select_attr = safe_color_pair(PAIR_ROSTER_SELECT, curses.A_REVERSE) | curses.A_BOLD

        try:
            if self.ascii_mode:
                win.border(ord('|'), ord('|'), ord('-'), ord('-'), ord('+'), ord('+'), ord('+'), ord('+'))
            else:
                win.box()
        except curses.error:
            pass

        train_count = len(snapshot.filtered_trains)
        filter_tag = f" (Filter: '{snapshot.filter_query}')" if snapshot.filter_query else ""
        title = f" ACTIVE TRAINS ({train_count}){filter_tag} "
        safe_addstr(win, 0, 2, f"[{title}]", border_attr | curses.A_BOLD, max_w=max_x - 4, ascii_mode=self.ascii_mode)

        # Table header row
        # Layout: [Cursor:2][#:3][TRAIN:6][TYPE:5][SPD:5][DLY:5][ST:4]
        table_hdr = " #   TRAIN  TYPE  SPD   DLY  ST"
        safe_addstr(win, 1, 1, table_hdr, header_attr, max_w=max_x - 2, ascii_mode=self.ascii_mode)

        inner_h = max_y - 2
        visible_rows = max(0, inner_h - 1)  # exclude table header
        if visible_rows <= 0 or train_count == 0:
            if train_count == 0:
                safe_addstr(win, 2, 2, "No trains match filter.", border_attr, ascii_mode=self.ascii_mode)
            return

        offset = calculate_scroll_offset(snapshot.selected_index, train_count, visible_rows)

        for r_idx in range(visible_rows):
            item_idx = offset + r_idx
            if item_idx >= train_count:
                break

            record = snapshot.filtered_trains[item_idx]
            pos = record.position
            is_selected = item_idx == snapshot.selected_index

            cursor_mark = ">" if is_selected else " "
            seq_num = f"{item_idx + 1:2d}"
            train_num = f"{(pos.train_number or pos.id)[:5]:<5}"

            # Format type abbreviation
            t_type = (pos.train_type or "IC")[:4].upper()
            if "PROAST" in t_type or "SUB" in t_type:
                t_type = "PR"
            elif "PASS" in t_type or "INTER" in t_type:
                t_type = "IC"
            elif "FREI" in t_type or "CARG" in t_type:
                t_type = "FR"
            elif "SHUN" in t_type:
                t_type = "SH"

            spd = int(round(pos.speed or 0))
            spd_str = f"{spd:3d}"

            dly = int(round(pos.delay or 0))
            dly_str = f"+{dly}m" if dly > 0 else (f"{dly}m" if dly < 0 else " 0m")

            st_code = "RUN"
            if pos.speed is not None and pos.speed < 1.0:
                st_code = "HLT"

            row_line = f"{cursor_mark} {seq_num}  {train_num:<6}{t_type:<5}{spd_str:>4} {dly_str:>5}  {st_code}"
            pair = get_train_color_pair(record)
            row_attr = select_attr if is_selected else safe_color_pair(pair, curses.A_NORMAL)
            safe_addstr(win, 2 + r_idx, 1, row_line, row_attr, max_w=max_x - 2, ascii_mode=self.ascii_mode)

    def render_inspector(
        self,
        win: Any,
        snapshot: StateSnapshot,
    ) -> None:
        """Render the Selected Train Telemetry Inspector pane.

        Args:
            win: Inspector curses window.
            snapshot: Current StateSnapshot.
        """
        win.erase()
        max_y, max_x = win.getmaxyx()
        if max_y < 3 or max_x < 3:
            return

        border_attr = safe_color_pair(PAIR_BORDER, curses.A_NORMAL)
        title_attr = safe_color_pair(PAIR_HEADER, curses.A_BOLD) | curses.A_BOLD
        val_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL)
        dim_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL) | curses.A_DIM

        try:
            if self.ascii_mode:
                win.border(ord('|'), ord('|'), ord('-'), ord('-'), ord('+'), ord('+'), ord('+'), ord('+'))
            else:
                win.box()
        except curses.error:
            pass

        title = " SELECTED TRAIN TELEMETRY "
        safe_addstr(win, 0, 2, f"[{title}]", border_attr | curses.A_BOLD, max_w=max_x - 4, ascii_mode=self.ascii_mode)

        record = snapshot.selected_train
        if record is None:
            empty_msg1 = "No train selected."
            select_hint = "[UP/DOWN]" if self.ascii_mode else "[↑/↓]"
            empty_msg2 = f"Use [Tab] or {select_hint} to select a train."
            safe_addstr(win, 2, max(2, (max_x - len(empty_msg1)) // 2), empty_msg1, dim_attr, ascii_mode=self.ascii_mode)
            safe_addstr(win, 3, max(2, (max_x - len(empty_msg2)) // 2), empty_msg2, dim_attr, ascii_mode=self.ascii_mode)
            return

        pos = record.position
        heading_deg = pos.track_heading if pos.track_heading is not None else pos.course
        compass_str = get_compass_bearing(heading_deg, ascii_mode=self.ascii_mode)

        delay_val = pos.delay or 0.0
        delay_pair = get_delay_color_pair(delay_val)
        delay_attr = safe_color_pair(delay_pair, curses.A_BOLD) | curses.A_BOLD
        dly_int = int(round(delay_val))
        dly_display = f"+{dly_int} min" if dly_int > 0 else (f"{dly_int} min" if dly_int < 0 else "0 min")
        if delay_val > 10.0:
            dly_display += " (Major Delay)"
        elif delay_val > 3.0:
            dly_display += " (Minor Delay)"
        else:
            dly_display += " (On Time)"

        spd_val = pos.speed or 0.0
        spd_display = f"{spd_val:5.1f} km/h"

        origin = pos.origin or "N/A"
        dest = pos.destination or "N/A"
        route_str = f"{origin} -> {dest}"

        chainage_km = (pos.chainage_m / 1000.0) if pos.chainage_m is not None else None
        chainage_str = f"{chainage_km:6.1f} km" if chainage_km is not None else "N/A"

        progress_str = format_progress_bar(pos.progress, max_x - 14)

        rows = [
            ("Train No:", f"{pos.train_number or pos.id} ({pos.train_type or 'Passenger'})", val_attr),
            ("Locomotive:", f"{pos.locomotive_number or 'N/A'}", val_attr),
            ("Status:", f"{pos.status or 'In Service'}", val_attr),
            ("Route:", route_str, val_attr),
            ("Next Station:", f"{pos.next_station or 'N/A'}", val_attr),
            ("Speed:", spd_display, val_attr),
            ("Delay:", dly_display, delay_attr),
            ("Heading:", compass_str, val_attr),
            ("Chainage:", chainage_str, val_attr),
            ("Progress:", progress_str, val_attr),
        ]

        inner_h = max_y - 2
        for r_idx, (label, value, attr) in enumerate(rows):
            if r_idx >= inner_h:
                break
            y = 1 + r_idx
            safe_addstr(win, y, 2, f"{label:<13}", title_attr, max_w=13, ascii_mode=self.ascii_mode)
            safe_addstr(win, y, 16, value, attr, max_w=max_x - 17, ascii_mode=self.ascii_mode)

    def render_status_bar(
        self,
        win: Any,
        snapshot: StateSnapshot,
        width: int,
        msg_rate: float = 0.0,
        input_mode: InputMode = InputMode.NORMAL,
        search_buffer: str = "",
        playback_state: PlaybackState = PlaybackState.RUNNING,
        *,
        search_mode: Optional[bool] = None,
        paused: Optional[bool] = None,
    ) -> None:
        """Render the bottom status bar and keybindings cheat sheet.

        Args:
            win: Status bar curses window.
            snapshot: Current StateSnapshot.
            width: Total window width.
            msg_rate: Telemetry ingestion message rate in msg/sec.
            input_mode: Current input mode (NORMAL or SEARCH).
            search_buffer: Current search text buffer.
            playback_state: Stream execution state (RUNNING or PAUSED).
            search_mode: Legacy boolean flag for search input mode.
            paused: Legacy boolean flag for paused stream state.
        """
        if search_mode is not None:
            input_mode = InputMode.SEARCH if search_mode else InputMode.NORMAL
        elif isinstance(input_mode, bool):
            input_mode = InputMode.SEARCH if input_mode else InputMode.NORMAL

        if paused is not None:
            playback_state = PlaybackState.PAUSED if paused else PlaybackState.RUNNING
        elif isinstance(playback_state, bool):
            playback_state = PlaybackState.PAUSED if playback_state else PlaybackState.RUNNING

        win.erase()
        status_attr = safe_color_pair(PAIR_STATUS_BAR, curses.A_REVERSE)
        connected_attr = safe_color_pair(PAIR_CONNECTED, curses.A_BOLD) | curses.A_BOLD
        warn_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD
        alert_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
        prompt_attr = safe_color_pair(PAIR_ROSTER_SELECT, curses.A_BOLD) | curses.A_BOLD

        # Background bar fill
        safe_addstr(win, 0, 0, " " * width, status_attr, ascii_mode=self.ascii_mode)

        if input_mode == InputMode.SEARCH:
            # Interactive search input prompt
            cursor = self.glyphs.search_cursor
            prompt_text = f" Search filter: {search_buffer}{cursor}"
            hint_text = "(Enter: Apply, Esc: Cancel) "
            safe_addstr(win, 0, 0, prompt_text, prompt_attr, max_w=max(0, width - len(hint_text)), ascii_mode=self.ascii_mode)
            hint_x = max(0, width - len(hint_text))
            safe_addstr(win, 0, hint_x, hint_text, status_attr, ascii_mode=self.ascii_mode)
            return

        # Keybindings cheat sheet
        follow_str = "[f] Unfollow" if (snapshot.follow_mode and snapshot.selected_train) else "[f] Follow"
        shortcuts = f" [q] Quit  [h/j/k/l] Pan  [+/-] Zoom  [Tab] Select  {follow_str}  [/] Filter  [?] Help  [0..3] Presets"
        safe_addstr(win, 0, 0, shortcuts, status_attr, max_w=max(0, width - 24), ascii_mode=self.ascii_mode)

        # Connection status badge
        if playback_state == PlaybackState.PAUSED:
            badge = self.glyphs.badge_paused
            b_attr = warn_attr
        else:
            status = snapshot.connection_status
            if status == ConnectionStatus.CONNECTED:
                badge = f"{self.glyphs.badge_live}({msg_rate:3.1f} msg/s) "
                b_attr = connected_attr
            elif status == ConnectionStatus.CONNECTING:
                badge = self.glyphs.badge_connecting
                b_attr = status_attr
            elif status == ConnectionStatus.RECONNECTING:
                badge = self.glyphs.badge_reconnecting
                b_attr = warn_attr
            elif status == ConnectionStatus.ERROR:
                badge = self.glyphs.badge_conn_error
                b_attr = alert_attr
            else:
                badge = self.glyphs.badge_demo
                b_attr = connected_attr

        badge_w = get_string_display_width(badge)
        badge_x = max(0, width - badge_w)
        safe_addstr(win, 0, badge_x, badge, b_attr, ascii_mode=self.ascii_mode)

    def render_help_modal(self, win: Any, height: int, width: int) -> None:
        """Render centered keybinding guide and symbols reference dialog overlay.

        Args:
            win: Root curses window to overlay onto.
            height: Total terminal screen height.
            width: Total terminal screen width.
        """
        box_h = min(22, max(16, height - 2))
        box_w = min(74, max(46, width - 4))
        start_y = max(1, (height - box_h) // 2)
        start_x = max(1, (width - box_w) // 2)

        border_attr = safe_color_pair(PAIR_BORDER, curses.A_BOLD) | curses.A_BOLD
        header_attr = safe_color_pair(PAIR_HEADER, curses.A_BOLD) | curses.A_BOLD
        text_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL)
        key_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD
        dim_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL) | curses.A_DIM

        # Draw box background fill
        for row in range(box_h):
            safe_addstr(win, start_y + row, start_x, " " * box_w, text_attr)

        # Draw border
        top_border = "+" + "-" * (box_w - 2) + "+"
        bot_border = "+" + "-" * (box_w - 2) + "+"
        safe_addstr(win, start_y, start_x, top_border, border_attr)
        safe_addstr(win, start_y + box_h - 1, start_x, bot_border, border_attr)
        for r in range(1, box_h - 1):
            safe_addstr(win, start_y + r, start_x, "|", border_attr)
            safe_addstr(win, start_y + r, start_x + box_w - 1, "|", border_attr)

        # Title
        title = " RAILMON - KEYBOARD CONTROLS & SYSTEM REFERENCE "
        t_x = max(start_x + 2, start_x + (box_w - len(title)) // 2)
        safe_addstr(win, start_y, t_x, title, header_attr)

        help_sections = [
            ("NAVIGATION & VIEWPORT", [
                ("h/j/k/l, Arrows", "Pan viewport West / South / North / East"),
                ("+ / =, - / _", "Zoom In / Zoom Out (0.2x to 25.0x)"),
                ("0, 1, 2, 3", "Presets (0: Greece, 1: Suburban, 2: PATHE, 3: North)"),
            ]),
            ("TRAIN SELECTION & TRACKING", [
                ("Tab, n / p", "Select Next / Previous train in active roster"),
                ("f / F", "Toggle real-time auto-follow camera lock"),
                ("c / C", "Clear search filter, corridor filter & selection"),
            ]),
            ("SEARCH & SYSTEM CONTROLS", [
                ("/, Esc", "Enter search filter mode / Cancel and return"),
                ("Space", "Pause / resume live telemetry ingestion"),
                ("?, q", "Toggle this help dialog / Clean exit dashboard"),
            ]),
            ("SYMBOLS & COLOR LEGEND", [
                (
                    "^>v< Headings" if self.ascii_mode else "▲►▼◄ Headings",
                    "Green: On-time (<=3m)  Yellow: Delay (4-10m)",
                ),
                (
                    "#/+ Station  . Trail" if self.ascii_mode else "◉ Station   · Trail",
                    "Red: Alert (>10m)    Cyan: Suburban  Magenta: Cargo",
                ),
            ]),
        ]

        curr_y = start_y + 1
        for sec_title, items in help_sections:
            if curr_y >= start_y + box_h - 2:
                break
            safe_addstr(win, curr_y, start_x + 2, f"[{sec_title}]", header_attr, max_w=box_w - 4, ascii_mode=self.ascii_mode)
            curr_y += 1
            for k_label, desc in items:
                if curr_y >= start_y + box_h - 2:
                    break
                safe_addstr(win, curr_y, start_x + 3, f"{k_label:<19}", key_attr, max_w=19, ascii_mode=self.ascii_mode)
                safe_addstr(win, curr_y, start_x + 22, desc, text_attr, max_w=box_w - 24, ascii_mode=self.ascii_mode)
                curr_y += 1

        footer_msg = " [Press ? or any key to close] "
        f_x = max(start_x + 2, start_x + (box_w - len(footer_msg)) // 2)
        safe_addstr(win, start_y + box_h - 1, f_x, footer_msg, dim_attr, ascii_mode=self.ascii_mode)

    def render_all(
        self,
        windows: Dict[str, Any],
        geometry: PaneGeometry,
        snapshot: StateSnapshot,
        projector: GeoProjector,
        msg_rate: float = 0.0,
        input_mode: InputMode = InputMode.NORMAL,
        search_buffer: str = "",
        help_overlay: HelpOverlay = HelpOverlay.HIDDEN,
        playback_state: PlaybackState = PlaybackState.RUNNING,
        *,
        search_mode: Optional[bool] = None,
        show_help: Optional[bool] = None,
        paused: Optional[bool] = None,
    ) -> None:
        """Render all dashboard panes to their respective subwindow buffers.

        Args:
            windows: Dictionary of active curses subwindows ('header', 'map', etc.).
            geometry: Current calculated screen partition bounds.
            snapshot: Atomic point-in-time state snapshot from TrainStateStore.
            projector: GeoProjector configured for the map canvas viewport.
            msg_rate: Ingestion frequency in messages per second.
            input_mode: Current input mode (NORMAL or SEARCH).
            search_buffer: Current search text buffer.
            help_overlay: Modal help overlay visibility (HIDDEN or VISIBLE).
            playback_state: Live telemetry ingestion state (RUNNING or PAUSED).
            search_mode: Legacy boolean flag for search input mode.
            show_help: Legacy boolean flag for help dialog visibility.
            paused: Legacy boolean flag for paused ingestion state.
        """
        if search_mode is not None:
            input_mode = InputMode.SEARCH if search_mode else InputMode.NORMAL
        elif isinstance(input_mode, bool):
            input_mode = InputMode.SEARCH if input_mode else InputMode.NORMAL

        if show_help is not None:
            help_overlay = HelpOverlay.VISIBLE if show_help else HelpOverlay.HIDDEN
        elif isinstance(help_overlay, bool):
            help_overlay = HelpOverlay.VISIBLE if help_overlay else HelpOverlay.HIDDEN

        if paused is not None:
            playback_state = PlaybackState.PAUSED if paused else PlaybackState.RUNNING
        elif isinstance(playback_state, bool):
            playback_state = PlaybackState.PAUSED if playback_state else PlaybackState.RUNNING

        if not geometry.is_valid:
            if "root" in windows:
                self.render_undersized(windows["root"], geometry.term_height, geometry.term_width)
            return

        if "header" in windows:
            self.render_header(windows["header"], snapshot, geometry.header_w)
        if "map" in windows:
            self.render_map(windows["map"], snapshot, projector)
        if "roster" in windows:
            self.render_roster(windows["roster"], snapshot)
        if "inspector" in windows:
            self.render_inspector(windows["inspector"], snapshot)
        if "status" in windows:
            self.render_status_bar(
                windows["status"],
                snapshot,
                geometry.status_w,
                msg_rate=msg_rate,
                input_mode=input_mode,
                search_buffer=search_buffer,
                playback_state=playback_state,
            )

        if help_overlay == HelpOverlay.VISIBLE and "root" in windows:
            self.render_help_modal(windows["root"], geometry.term_height, geometry.term_width)
