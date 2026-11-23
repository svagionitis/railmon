"""Multi-pane Curses layout and rendering engine for live train map dashboard.

Provides terminal window geometry calculations, color palette management,
Braille track rasterization blitting, station collision avoidance,
directional heading glyphs, windowed scrolling train rosters, and
telemetry inspection panels.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum, auto
import curses
import math
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
import unicodedata

from tui.geo import (
    BrailleCanvas,
    ConflictAlert,
    ConflictSeverity,
    ConflictType,
    GeoPoint,
    GeoProjector,
    InfrastructureFeature,
    InfrastructureType,
    OverspeedSeverity,
    SCALE_BAR_INTERVALS_KM,
    ScaleBarSpec,
    SensorHealthAssessment,
    SensorHealthState,
    detect_train_tunnel,
    haversine_distance_km,
)
from tui.mock_engine import (
    StationNode,
    TrackPolyline,
    build_station_index,
    calculate_bearing,
    load_infrastructure,
    normalize_station_key,
    snap_to_track_network,
)
from tui.state import (
    BreadcrumbMode,
    ConnectionStatus,
    InfrastructureLayerMode,
    InputMode,
    InspectorMode,
    RosterSortMode,
    StateSnapshot,
    StationLabelMode,
    TrackColorMode,
    TrainRecord,
)
from tui.theme import (
    PAIR_BORDER,
    PAIR_CONNECTED,
    PAIR_DEFAULT,
    PAIR_HEADER,
    PAIR_ROSTER_SELECT,
    PAIR_STATION,
    PAIR_STATION_DIM,
    PAIR_STATUS_BAR,
    PAIR_TRACK,
    PAIR_TRAIL,
    PAIR_TRAIN_ALERT,
    PAIR_TRAIN_FREIGHT,
    PAIR_TRAIN_HALTED,
    PAIR_TRAIN_ON_TIME,
    PAIR_TRAIN_PROAST,
    PAIR_TRAIN_WARN,
    ThemeProfile,
    get_theme_spec,
)
from storage.cli_formatters import (
    format_timetable_time,
    resolve_timetable_station_name,
    transliterate_greek_to_latin,
)


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
    sort_asc: str
    sort_desc: str
    scale_left: str = "├"
    scale_right: str = "┤"
    scale_bar: str = "─"
    tunnel_portal: str = "∩"
    bridge_marker: str = "═"
    signal_junction: str = "◆"


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
    sort_asc="▲",
    sort_desc="▼",
    scale_left="├",
    scale_right="┤",
    scale_bar="─",
    tunnel_portal="∩",
    bridge_marker="═",
    signal_junction="◆",
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
    sort_asc="^",
    sort_desc="v",
    scale_left="|",
    scale_right="|",
    scale_bar="-",
    tunnel_portal="n",
    bridge_marker="=",
    signal_junction="S",
)

# Sparkline glyph bars and trend arrow constants
SPARKLINE_BARS_UTF8: Tuple[str, ...] = (" ", "▂", "▃", "▄", "▅", "▆", "▇", "█")
SPARKLINE_BARS_ASCII: Tuple[str, ...] = ("_", ".", "-", "=", "+", "*", "#", "@")

TREND_UP_UTF8: str = "▲"
TREND_DOWN_UTF8: str = "▼"
TREND_STEADY_UTF8: str = "─"

TREND_UP_ASCII: str = "^"
TREND_DOWN_ASCII: str = "v"
TREND_STEADY_ASCII: str = "-"


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


def format_speed_sparkline(
    speeds: Sequence[float],
    max_bars: int = 10,
    ascii_mode: bool = False,
    v_max_floor: float = 160.0,
) -> str:
    """Render a compact speed sparkline with instantaneous kinematic trend arrow.

    Args:
        speeds: Chronological sequence of speed samples in km/h.
        max_bars: Maximum number of bar characters to render.
        ascii_mode: Whether to restrict output to 7-bit ASCII characters.
        v_max_floor: Baseline normalization speed ceiling in km/h (default: 160.0 km/h).

    Returns:
        Formatted string containing sparkline bars and trailing trend arrow (e.g. ' ▂▃▅▆▇██ ▲').
    """
    if not speeds:
        empty_bar = "_" if ascii_mode else " "
        trend_steady = TREND_STEADY_ASCII if ascii_mode else TREND_STEADY_UTF8
        return f"{empty_bar} {trend_steady}"

    # Select recent window
    window = list(speeds[-max_bars:]) if len(speeds) > max_bars else list(speeds)
    v_max = max(v_max_floor, max(window))

    bar_glyphs = SPARKLINE_BARS_ASCII if ascii_mode else SPARKLINE_BARS_UTF8
    num_levels = len(bar_glyphs)

    bars: List[str] = []
    for spd in window:
        clamped_spd = max(0.0, float(spd))
        ratio = clamped_spd / v_max if v_max > 0.0 else 0.0
        idx = min(num_levels - 1, int(ratio * num_levels))
        bars.append(bar_glyphs[idx])

    # Compute trend arrow from last two points
    if len(window) >= 2:
        delta_v = window[-1] - window[-2]
    else:
        delta_v = 0.0

    if delta_v > 3.0:
        arrow = TREND_UP_ASCII if ascii_mode else TREND_UP_UTF8
    elif delta_v < -3.0:
        arrow = TREND_DOWN_ASCII if ascii_mode else TREND_DOWN_UTF8
    else:
        arrow = TREND_STEADY_ASCII if ascii_mode else TREND_STEADY_UTF8

    return f"{''.join(bars)} {arrow}"


def format_telemetry_age(
    last_updated: float,
    update_count: int,
    now: Optional[float] = None,
) -> Tuple[str, int]:
    """Calculate elapsed telemetry age and resolve warning color attributes.

    Args:
        last_updated: Epoch timestamp of the last received position message.
        update_count: Total position updates received for this train.
        now: Current epoch timestamp (defaults to time.time()).

    Returns:
        Tuple of (formatted_age_string, curses_color_pair_index).
    """
    current_time = now if now is not None else time.time()
    age_sec = max(0, int(current_time - last_updated))

    if age_sec < 60:
        age_str = f"{age_sec}s ago"
    elif age_sec < 3600:
        mins = age_sec // 60
        secs = age_sec % 60
        age_str = f"{mins}m {secs}s ago" if secs > 0 and mins < 10 else f"{mins}m ago"
    else:
        hours = age_sec // 3600
        mins = (age_sec % 3600) // 60
        age_str = f"{hours}h {mins}m ago"

    if age_sec > 120:
        short_age = f"{age_sec // 60}m ago" if age_sec < 3600 else f"{age_sec // 3600}h ago"
        return f"{short_age} [STALE]", PAIR_TRAIN_ALERT
    elif age_sec > 30:
        return f"{age_str} (#{update_count})", PAIR_TRAIN_WARN
    else:
        return f"{age_str} (#{update_count})", PAIR_DEFAULT


# Alias build_station_index for backward compatibility with existing tests
_build_station_index = build_station_index


def format_next_station_eta(
    next_station: Optional[str],
    train_point: Optional[GeoPoint] = None,
    station_node: Optional[StationNode] = None,
    speed_kmh: Optional[float] = None,
) -> str:
    """Format the next station readout with geodesic distance and kinematic ETA.

    Args:
        next_station: Display name of the upcoming station from telemetry.
        train_point: Current WGS84 GeoPoint of the train.
        station_node: Resolved StationNode for next_station, if matched.
        speed_kmh: Instantaneous train speed in km/h.

    Returns:
        Formatted Next Station string for the Inspector pane.
    """
    if not next_station or not next_station.strip():
        return "N/A"

    clean_name = next_station.strip()
    if (
        station_node is None
        or train_point is None
        or (abs(train_point.lat) < 1e-9 and abs(train_point.lng) < 1e-9)
        or not (math.isfinite(train_point.lat) and math.isfinite(train_point.lng))
    ):
        return clean_name

    d_km = haversine_distance_km(train_point, station_node.point)
    spd = float(speed_kmh) if (speed_kmh is not None and math.isfinite(speed_kmh)) else 0.0

    if spd >= 5.0:
        raw_eta_min = (d_km / spd) * 60.0
        eta_min = int(round(raw_eta_min))
        if d_km < 0.1 or eta_min <= 0:
            eta_str = "<1m"
        elif eta_min >= 120:
            hours = eta_min // 60
            mins = eta_min % 60
            eta_str = f"~{hours}h{mins:02d}m" if mins > 0 else f"~{hours}h"
        else:
            eta_str = f"~{eta_min}m"
        return f"{clean_name} ({d_km:.1f} km, {eta_str})"

    if d_km < 0.25:
        return f"{clean_name} ({d_km:.1f} km, AT)"
    return f"{clean_name} ({d_km:.1f} km)"


def format_wgs84_coords(
    lat: Optional[float],
    lng: Optional[float],
    ascii_mode: bool = False,
) -> str:
    """Format a WGS84 latitude/longitude pair into a human-readable degree readout.

    Args:
        lat: Latitude in decimal degrees (-90.0 to +90.0).
        lng: Longitude in decimal degrees (-180.0 to +180.0).
        ascii_mode: Whether to format degrees using 7-bit ASCII ('d ').

    Returns:
        Formatted coordinate string (e.g. '39.6384°N, 22.4192°E' or '39.6384d N, 22.4192d E'),
        or 'N/A' if coordinates are missing or uninitialized (0.0, 0.0).
    """
    if lat is None or lng is None:
        return "N/A"
    if not (math.isfinite(lat) and math.isfinite(lng)):
        return "N/A"
    if abs(lat) < 1e-9 and abs(lng) < 1e-9:
        return "N/A"

    lat_hemi = "N" if lat >= 0.0 else "S"
    lng_hemi = "E" if lng >= 0.0 else "W"
    abs_lat = abs(float(lat))
    abs_lng = abs(float(lng))

    if ascii_mode:
        return f"{abs_lat:.4f}d {lat_hemi}, {abs_lng:.4f}d {lng_hemi}"
    return f"{abs_lat:.4f}°{lat_hemi}, {abs_lng:.4f}°{lng_hemi}"


def format_scale_bar(
    distance_km: int,
    width_cells: int = 15,
    ascii_mode: bool = False,
) -> str:
    """Format a conformal map scale bar string of the specified character cell width.

    Examples:
        - UTF-8 (width_cells=15, distance_km=25): '├─── 25 km ───┤'
        - UTF-8 (width_cells=17, distance_km=25): '├──── 25 km ────┤'
        - ASCII (width_cells=15, distance_km=25): '|--- 25 km ---|'
        - ASCII (width_cells=17, distance_km=25): '|---- 25 km ----|'

    Args:
        distance_km: Cartographic distance label in kilometers.
        width_cells: Target total character cell width (minimum label + 4).
        ascii_mode: Whether to use 7-bit ASCII ('|', '-') instead of UTF-8 ('├', '─', '┤').

    Returns:
        Formatted scale bar string.
    """
    glyphs = GLYPHS_ASCII if ascii_mode else GLYPHS_UTF8
    label = f" {int(distance_km)} km "
    min_w = len(label) + 4
    effective_w = max(min_w, int(width_cells))
    total_dashes = effective_w - 2 - len(label)
    left_dashes = total_dashes // 2
    right_dashes = total_dashes - left_dashes
    return (
        f"{glyphs.scale_left}"
        f"{glyphs.scale_bar * left_dashes}"
        f"{label}"
        f"{glyphs.scale_bar * right_dashes}"
        f"{glyphs.scale_right}"
    )


DEFAULT_STATION_PROXIMITY_KM: float = 80.0


@dataclass(frozen=True)
class StationBoardEntry:
    """Computed proximity and estimated arrival row for a train relative to a StationNode."""

    train_id: str
    train_number: str
    direction: str
    distance_km: float
    dist_str: str
    eta_str: str
    delay_min: int
    delay_str: str
    is_matched_route: bool
    is_selected: bool
    color_pair: int


def format_corridor_display_name(corridor: Optional[str]) -> str:
    """Map an internal station corridor code to a human-readable display label.

    Args:
        corridor: Raw corridor identifier (e.g. 'PATHE', 'PROASTIAKOS_ATH', 'NORTHERN').

    Returns:
        Human-readable corridor title string.
    """
    if not corridor or not corridor.strip():
        return "National Network"
    norm = corridor.strip().upper()
    if norm in ("PATHE", "PATHE_MAINLINE", "E85"):
        return "PATHE Mainline"
    if "PROAST" in norm or norm in ("SUBURBAN", "PIRAIR", "KIATO", "CHALKIDA"):
        return "Proastiakos Suburban"
    if norm in ("NORTHERN", "NORTHERN_GREECE", "NORTH"):
        return "Northern Greece"
    if norm == "MACEDONIA":
        return "Macedonia Corridor"
    if norm == "THESSALY":
        return "Thessaly Corridor"
    return corridor.strip().title()


def get_track_corridor_color_pair(corridor_id: Optional[str]) -> int:
    """Resolve the curses color pair for a track polyline corridor in TrackColorMode.CORRIDOR.

    Mapping per F-3.3 specification:
        - PATHE ('pathe', 'pathe-mainline', 'e85', 'mainline'): PAIR_TRACK (White)
        - PROASTIAKOS ('proastiakos-*', 'suburban', 'pirair', 'kiato', 'chalkida'): PAIR_TRAIN_PROAST (Cyan)
        - NORTHERN / THESSALY / Regional ('northern-*', 'thessaly-*', 'thessaloniki-idomeni',
          'western-macedonia', 'macedonia', 'thrace'): PAIR_STATION_DIM (Yellow)

    Args:
        corridor_id: Corridor identifier or name from TrackPolyline.

    Returns:
        Curses color pair constant (PAIR_TRACK, PAIR_TRAIN_PROAST, or PAIR_STATION_DIM).
    """
    if not corridor_id or not corridor_id.strip():
        return PAIR_TRACK
    norm = corridor_id.strip().upper()
    if any(
        k in norm
        for k in ("PROAST", "SUBURBAN", "PIRAIR", "KIATO", "AERODROMIO", "AIRPORT", "CHALKIDA")
    ):
        return PAIR_TRAIN_PROAST
    if any(
        k in norm
        for k in (
            "NORTHERN",
            "THESSALY",
            "MACEDONIA",
            "THRACE",
            "IDOMENI",
            "VOLOS",
            "KALAMBAKA",
            "FLORINA",
            "ALEX",
        )
    ):
        return PAIR_STATION_DIM
    return PAIR_TRACK


def _natural_train_id_sort_key(train_number: str, train_id: str) -> Tuple[int, str]:
    """Natural alphanumeric sort key for train numbers on the station proximity board."""
    tn = (train_number or "").strip()
    digits = "".join(ch for ch in tn if ch.isdigit())
    if digits:
        return (0, f"{int(digits):010d}_{tn}")
    return (1, tn or train_id)


def compute_station_board_entries(
    station: StationNode,
    trains: Sequence[TrainRecord],
    resolve_station_fn: Callable[[Optional[str]], Optional[StationNode]],
    selected_train_id: Optional[str] = None,
    proximity_radius_km: float = DEFAULT_STATION_PROXIMITY_KM,
) -> List[StationBoardEntry]:
    """Compute and sort approaching / at-station trains for a given StationNode.

    Includes trains within proximity_radius_km (default 80.0 km) of the station OR
    whose next_station, origin, or destination resolves to the station, sorted by
    ascending Haversine distance with natural train number tie-breaking.

    Args:
        station: Target StationNode for the departure/proximity board.
        trains: Sequence of active TrainRecord items to evaluate.
        resolve_station_fn: Callable resolving a station name/code/ID to a StationNode.
        selected_train_id: Optional ID of the currently selected train in the roster.
        proximity_radius_km: Geodesic capture radius in kilometers (default: 80.0 km).

    Returns:
        Sorted list of StationBoardEntry objects.
    """
    entries: List[StationBoardEntry] = []
    stn_norm_el = normalize_station_key(station.name_el)
    stn_norm_en = normalize_station_key(station.name_en)

    for record in trains:
        pos = record.position
        if not (math.isfinite(pos.lat) and math.isfinite(pos.lng)):
            continue
        if abs(pos.lat) < 1e-9 and abs(pos.lng) < 1e-9:
            continue

        train_pt = GeoPoint(lat=pos.lat, lng=pos.lng)
        d_km = haversine_distance_km(train_pt, station.point)

        # Check if next_station, origin, or destination matches this station
        is_matched_route = False
        for endpoint_name in (pos.next_station, pos.origin, pos.destination):
            if not endpoint_name:
                continue
            resolved = resolve_station_fn(endpoint_name)
            if resolved is not None and resolved.id == station.id:
                is_matched_route = True
                break
            norm_ep = normalize_station_key(endpoint_name)
            if norm_ep and (
                (stn_norm_el and stn_norm_el in norm_ep)
                or (stn_norm_en and stn_norm_en in norm_ep)
            ):
                is_matched_route = True
                break

        if d_km > proximity_radius_km and not is_matched_route:
            continue

        spd = float(pos.speed) if (pos.speed is not None and math.isfinite(pos.speed)) else 0.0

        # 1. Direction (3-char cardinal or 'HLT')
        if spd < 1.0:
            direction = "HLT"
        else:
            heading_deg = pos.track_heading if pos.track_heading is not None else pos.course
            if heading_deg is None or not math.isfinite(heading_deg):
                heading_deg = calculate_bearing(train_pt, station.point)
            idx = int((heading_deg + 11.25) % 360 // 22.5)
            direction = COMPASS_POINTS_16[idx]

        # 2. Compact distance string
        if d_km < 100.0:
            dist_str = f"{d_km:.1f}km"
        else:
            dist_str = f"{int(round(d_km))}km"

        # 3. Kinematic ETA string
        if d_km < 0.25 and spd < 5.0:
            eta_str = "AT"
        elif spd >= 5.0:
            eta_min = int(round((d_km / spd) * 60.0))
            if d_km < 0.1 or eta_min <= 0:
                eta_str = "<1m"
            elif eta_min >= 120:
                hours = eta_min // 60
                mins = eta_min % 60
                eta_str = f"~{hours}h{mins:02d}" if mins > 0 else f"~{hours}h"
            else:
                eta_str = f"~{eta_min}m"
        else:
            eta_str = "--"

        # 4. Delay string
        dly_val = float(pos.delay) if (pos.delay is not None and math.isfinite(pos.delay)) else 0.0
        dly_int = int(round(dly_val))
        delay_str = f"+{dly_int}m" if dly_int > 0 else (f"{dly_int}m" if dly_int < 0 else "0m")

        train_num = (pos.train_number or pos.id).strip()
        is_selected = selected_train_id is not None and pos.id == selected_train_id
        color_pair = get_train_color_pair(record)

        entries.append(
            StationBoardEntry(
                train_id=pos.id,
                train_number=train_num,
                direction=direction,
                distance_km=d_km,
                dist_str=dist_str,
                eta_str=eta_str,
                delay_min=dly_int,
                delay_str=delay_str,
                is_matched_route=is_matched_route,
                is_selected=is_selected,
                color_pair=color_pair,
            )
        )

    entries.sort(
        key=lambda e: (
            round(e.distance_km, 3),
            _natural_train_id_sort_key(e.train_number, e.train_id),
        )
    )
    return entries


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
_THEME_ATTR_OVERRIDES: Dict[int, int] = {}


def safe_color_pair(pair_idx: int, fallback_attr: int = curses.A_NORMAL) -> int:
    """Safely return curses color pair attribute or fallback if curses not initialized or pair failed.

    Args:
        pair_idx: Curses color pair index.
        fallback_attr: Curses attribute to return if colors unavailable or uninitialized.

    Returns:
        Color pair attribute integer or fallback, combined with active theme attribute overrides.
    """
    global _INITIALIZED_COLOR_PAIRS, _THEME_ATTR_OVERRIDES
    attr_override = _THEME_ATTR_OVERRIDES.get(pair_idx, 0)
    try:
        if curses.has_colors() and pair_idx in _INITIALIZED_COLOR_PAIRS:
            return curses.color_pair(pair_idx) | attr_override
    except curses.error:
        pass
    return fallback_attr | attr_override


def calculate_timetable_visible_rows(term_height: int) -> int:
    """Calculate the number of stop rows visible in the centered timetable modal dialog.

    Args:
        term_height: Available terminal height in rows.

    Returns:
        Integer count of visible stop rows (minimum 1).
    """
    box_h = min(22, max(12, term_height - 2))
    return max(1, box_h - 5)


class TUIRenderer:
    """Multi-pane Curses drawing engine for live train map dashboard."""

    def __init__(
        self,
        stations: Optional[Sequence[StationNode]] = None,
        tracks: Optional[Sequence[TrackPolyline]] = None,
        ascii_mode: bool = False,
        infrastructure: Optional[Sequence[InfrastructureFeature]] = None,
    ) -> None:
        """Initialize renderer with static railway network vector data.

        Args:
            stations: List or sequence of StationNode objects.
            tracks: List or sequence of TrackPolyline objects.
            ascii_mode: Whether to force standard 7-bit ASCII rendering fallback.
            infrastructure: List or sequence of InfrastructureFeature objects.
        """
        self.stations: Sequence[StationNode] = stations or []
        self.station_index: Dict[str, StationNode] = _build_station_index(self.stations)
        self.tracks: Sequence[TrackPolyline] = tracks or []
        if infrastructure is None:
            self.infrastructure: Sequence[InfrastructureFeature] = list(load_infrastructure().values())
        else:
            self.infrastructure = list(infrastructure)
        self.tunnels: Sequence[InfrastructureFeature] = [
            f for f in self.infrastructure if f.feature_type == InfrastructureType.TUNNEL
        ]
        self.ascii_mode: bool = ascii_mode
        self.glyphs: RenderGlyphs = GLYPHS_ASCII if ascii_mode else GLYPHS_UTF8
        self.active_theme: ThemeProfile = ThemeProfile.AZURE

    def resolve_station(self, name_or_id: Optional[str]) -> Optional[StationNode]:
        """Resolve a station name, Greek/English alias, code, or ID to a StationNode."""
        if not name_or_id:
            return None
        key = normalize_station_key(name_or_id)
        if not key:
            return None
        if key in self.station_index:
            return self.station_index[key]
        if "(" in key:
            base_key = key.split("(", 1)[0].strip()
            if base_key in self.station_index:
                return self.station_index[base_key]
        return None

    def apply_theme(self, profile: ThemeProfile) -> bool:
        """Re-bind curses color pairs and attribute overrides for designated theme profile.

        Args:
            profile: Target ThemeProfile enum.

        Returns:
            True if colors were successfully initialized/re-bound, False if curses has no colors.
        """
        global _INITIALIZED_COLOR_PAIRS, _THEME_ATTR_OVERRIDES
        if not isinstance(profile, ThemeProfile):
            raise TypeError(f"Expected ThemeProfile enum, got {type(profile).__name__}: {profile}")

        self.active_theme = profile
        spec = get_theme_spec(profile)

        _THEME_ATTR_OVERRIDES.clear()
        for pair_id, p_def in spec.pairs.items():
            if p_def.attr_override is not None:
                _THEME_ATTR_OVERRIDES[pair_id] = p_def.attr_override

        try:
            if not curses.has_colors():
                return False
        except curses.error:
            return False

        try:
            curses.start_color()
        except curses.error:
            return False

        try:
            curses.use_default_colors()
        except curses.error:
            pass

        num_colors = getattr(curses, "COLORS", 8)
        use_256 = num_colors >= 256
        max_pairs = getattr(curses, "COLOR_PAIRS", 64)

        _INITIALIZED_COLOR_PAIRS.clear()

        for pair_idx, p_def in spec.pairs.items():
            if pair_idx >= max_pairs:
                continue
            fg = p_def.fg_256 if use_256 else p_def.fg_16
            bg = p_def.bg_256 if use_256 else p_def.bg_16
            try:
                curses.init_pair(pair_idx, fg, bg)
                _INITIALIZED_COLOR_PAIRS.add(pair_idx)
            except curses.error:
                # Silently degrade: safe_color_pair will return fallback_attr
                pass

        return True

    def init_colors(self) -> None:
        """Initialize standard curses color pairs and semantic visual attributes with error bounds."""
        self.apply_theme(self.active_theme)

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

    def _render_header_mode_badge(
        self,
        win: Any,
        snapshot: StateSnapshot,
        left_w: int,
        width: int,
    ) -> int:
        """Draw the recording or historical DVR mode badge in the top header strip."""
        if getattr(snapshot, "is_replay", False) and width > left_w + 14:
            spd_int = int(round(getattr(snapshot, "replay_speed", 1.0) or 1.0))
            is_paused = snapshot.connection_status == ConnectionStatus.PAUSED
            if is_paused:
                icon = "||" if self.ascii_mode else "❚❚"
                b_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD
            else:
                icon = ">" if self.ascii_mode else "▶"
                b_attr = safe_color_pair(PAIR_TRAIN_PROAST, curses.A_BOLD) | curses.A_BOLD
            dvr_text = f" [{icon} DVR {spd_int}x]"
            safe_addstr(win, 0, left_w, dvr_text, b_attr, ascii_mode=self.ascii_mode)
            return left_w + get_string_display_width(dvr_text)

        if snapshot.is_recording and width > left_w + 12:
            rec_text = self.glyphs.badge_rec
            rec_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            safe_addstr(win, 0, left_w, rec_text, rec_attr, ascii_mode=self.ascii_mode)
            return left_w + get_string_display_width(rec_text)

        return left_w

    @staticmethod
    def _format_header_clock(snapshot: StateSnapshot, available_w: int) -> str:
        """Format the right-aligned header clock (virtual UTC clock in replay mode, else wall-clock)."""
        if getattr(snapshot, "is_replay", False) and getattr(snapshot, "replay_virtual_time", None) is not None:
            v_epoch = float(snapshot.replay_virtual_time or 0.0)
            dt_utc = datetime.fromtimestamp(v_epoch, tz=timezone.utc)
            if available_w >= 26:
                return dt_utc.strftime(" %Y-%m-%d %H:%M:%S UTC ")
            if available_w >= 15:
                return dt_utc.strftime(" %H:%M:%S UTC ")
            return dt_utc.strftime(" %H:%M:%S ")
        return time.strftime(" %H:%M:%S ")

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

        # Draw left header elements
        safe_addstr(win, 0, 0, left_text, header_attr, ascii_mode=self.ascii_mode)
        left_w = get_string_display_width(left_text)

        cnt_attr = warn_attr if delayed_count > 0 else dim_attr
        safe_addstr(win, 0, left_w, counts_text, cnt_attr, ascii_mode=self.ascii_mode)
        left_w += get_string_display_width(counts_text)

        # Draw recording or DVR replay status badge
        left_w = self._render_header_mode_badge(win, snapshot, left_w, width)

        # Draw right-aligned clock (virtual UTC clock in DVR mode, else wall-clock)
        time_str = self._format_header_clock(snapshot, width - left_w)
        time_x = width - get_string_display_width(time_str)
        if time_x > left_w:
            safe_addstr(win, 0, time_x, time_str, dim_attr, ascii_mode=self.ascii_mode)

    def _render_map_tracks(
        self,
        win: Any,
        canvas: BrailleCanvas,
        projector: GeoProjector,
        inner_w: int,
        inner_h: int,
        track_color_mode: TrackColorMode,
        track_attr: int,
    ) -> None:
        """Rasterize and blit railway track polylines onto the map curses window."""
        if track_color_mode != TrackColorMode.CORRIDOR:
            for track in self.tracks:
                sub_points = [projector.geo_to_subpixel(pt) for pt in track.points]
                canvas.draw_polyline(sub_points)
            for r_idx, row_str in enumerate(canvas.render_rows()):
                safe_addstr(win, 1 + r_idx, 1, row_str, track_attr, max_w=inner_w, ascii_mode=self.ascii_mode)
            return

        cell_pairs: List[int] = [PAIR_TRACK] * (inner_w * inner_h)
        for track in self.tracks:
            sub_points = [projector.geo_to_subpixel(pt) for pt in track.points]
            track_canvas = BrailleCanvas(
                width_cells=inner_w, height_cells=inner_h, ascii_mode=self.ascii_mode
            )
            track_canvas.draw_polyline(sub_points)
            pair = get_track_corridor_color_pair(track.corridor_id or track.name)
            t_buf = track_canvas.buffer
            c_buf = canvas.buffer
            for idx, bits in enumerate(t_buf):
                if bits:
                    c_buf[idx] |= bits
                    if pair != PAIR_TRACK or cell_pairs[idx] == PAIR_TRACK:
                        cell_pairs[idx] = pair

        pair_attr_map = {
            PAIR_TRACK: track_attr,
            PAIR_TRAIN_PROAST: safe_color_pair(PAIR_TRAIN_PROAST, curses.A_NORMAL),
            PAIR_STATION_DIM: safe_color_pair(PAIR_STATION_DIM, curses.A_DIM) | curses.A_DIM,
        }
        for r_idx, row_str in enumerate(canvas.render_rows()):
            self._blit_colored_track_row(
                win, r_idx, row_str, cell_pairs, inner_w, pair_attr_map, track_attr
            )

    def _blit_colored_track_row(
        self,
        win: Any,
        r_idx: int,
        row_str: str,
        cell_pairs: List[int],
        inner_w: int,
        pair_attr_map: Dict[int, int],
        default_attr: int,
    ) -> None:
        """Blit a single rasterized Braille row using run-length color spans."""
        row_offset = r_idx * inner_w
        run_start = 0
        run_pair = cell_pairs[row_offset] if inner_w > 0 else PAIR_TRACK
        for c_idx in range(1, inner_w):
            c_pair = cell_pairs[row_offset + c_idx]
            if c_pair != run_pair:
                safe_addstr(
                    win,
                    1 + r_idx,
                    1 + run_start,
                    row_str[run_start:c_idx],
                    pair_attr_map.get(run_pair, default_attr),
                    max_w=c_idx - run_start,
                    ascii_mode=self.ascii_mode,
                )
                run_start = c_idx
                run_pair = c_pair
        if inner_w > run_start:
            safe_addstr(
                win,
                1 + r_idx,
                1 + run_start,
                row_str[run_start:inner_w],
                pair_attr_map.get(run_pair, default_attr),
                max_w=inner_w - run_start,
                ascii_mode=self.ascii_mode,
            )

    def _render_map_breadcrumbs(
        self,
        win: Any,
        snapshot: StateSnapshot,
        projector: GeoProjector,
        canvas: BrailleCanvas,
        inner_w: int,
        inner_h: int,
        breadcrumb_mode: BreadcrumbMode,
        trail_attr: int,
    ) -> None:
        """Overlay historical breadcrumb trail dots onto the map window."""
        if breadcrumb_mode == BreadcrumbMode.OFF:
            return
        if breadcrumb_mode == BreadcrumbMode.SELECTED:
            sel = snapshot.selected_train
            if sel is None or not any(r.position.id == sel.position.id for r in snapshot.filtered_trains):
                return
            trail_records: Sequence[TrainRecord] = (sel,)
        else:
            trail_records = snapshot.filtered_trains

        trail_glyph = self.glyphs.trail_dot
        for record in trail_records:
            corridor_hint = record.position.corridor
            for crumb in tuple(record.trail):
                snapped_pt = snap_to_track_network(
                    GeoPoint(lat=crumb.lat, lng=crumb.lng),
                    self.tracks,
                    corridor_hint=corridor_hint,
                )
                col, row = projector.resolve_track_cell(snapped_pt, canvas)
                if 0 <= col < inner_w and 0 <= row < inner_h:
                    try:
                        win.addstr(1 + row, 1 + col, trail_glyph, trail_attr)
                    except curses.error:
                        pass

    def _render_map_stations(
        self,
        win: Any,
        snapshot: StateSnapshot,
        projector: GeoProjector,
        canvas: BrailleCanvas,
        inner_w: int,
        inner_h: int,
        station_label_mode: StationLabelMode,
        occupied_cells: Set[Tuple[int, int]],
        station_attr: int,
        station_dim_attr: int,
    ) -> None:
        """Overlay railway stations and labels with greedy collision avoidance."""
        inspector_mode = getattr(snapshot, "inspector_mode", InspectorMode.TRAIN)
        selected_stn_id = (
            getattr(snapshot, "selected_station_id", None)
            if inspector_mode == InspectorMode.STATION
            else None
        )
        sorted_stations = sorted(
            self.stations,
            key=lambda s: (s.id != selected_stn_id, not s.is_hub),
        )

        for stn in sorted_stations:
            col, row = projector.resolve_track_cell(stn.point, canvas)
            if not (0 <= col < inner_w and 0 <= row < inner_h):
                continue

            is_active_station = selected_stn_id is not None and stn.id == selected_stn_id
            glyph = self.glyphs.station_hub if stn.is_hub else self.glyphs.station_normal
            marker_attr = (
                (station_attr | curses.A_REVERSE | curses.A_BOLD)
                if is_active_station
                else station_attr
            )
            try:
                win.addstr(1 + row, 1 + col, glyph, marker_attr)
            except curses.error:
                pass
            occupied_cells.add((col, row))

            should_draw_label = (
                is_active_station
                if station_label_mode == StationLabelMode.MARKERS_ONLY
                else (
                    (stn.is_hub or is_active_station)
                    if station_label_mode == StationLabelMode.HUBS_ONLY
                    else True
                )
            )
            if not should_draw_label:
                continue

            name = stn.name_en or stn.name_el or stn.id
            label = f" [{name}]" if is_active_station else f" {name}"
            label_w = get_string_display_width(label)
            label_start_x = col + 1

            if label_start_x + label_w <= inner_w and not any(
                (label_start_x + dx, row) in occupied_cells for dx in range(label_w)
            ):
                lbl_attr = (
                    (station_attr | curses.A_REVERSE | curses.A_BOLD)
                    if is_active_station
                    else (station_attr if stn.is_hub else station_dim_attr)
                )
                safe_addstr(
                    win,
                    1 + row,
                    1 + label_start_x,
                    label,
                    lbl_attr,
                    max_w=inner_w - label_start_x,
                    ascii_mode=self.ascii_mode,
                )
                for dx in range(label_w):
                    occupied_cells.add((label_start_x + dx, row))

    def _render_map_infrastructure(
        self,
        win: Any,
        snapshot: StateSnapshot,
        projector: GeoProjector,
        canvas: BrailleCanvas,
        inner_w: int,
        inner_h: int,
        occupied_cells: Set[Tuple[int, int]],
    ) -> None:
        """Overlay railway infrastructure assets (portals, bridges, signals)."""
        infra_mode = getattr(snapshot, "infrastructure_mode", InfrastructureLayerMode.ALL)
        if infra_mode == InfrastructureLayerMode.OFF:
            return

        portal_attr = safe_color_pair(PAIR_STATION_DIM, curses.A_BOLD) | curses.A_BOLD
        bridge_attr = safe_color_pair(PAIR_TRAIN_PROAST, curses.A_BOLD) | curses.A_BOLD
        signal_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD

        portal_glyph = self.glyphs.tunnel_portal
        bridge_glyph = self.glyphs.bridge_marker
        signal_glyph = self.glyphs.signal_junction

        draw_signals = (infra_mode == InfrastructureLayerMode.ALL)

        for feature in self.infrastructure:
            ftype = feature.feature_type
            if ftype == InfrastructureType.SIGNAL and not draw_signals:
                continue

            if ftype == InfrastructureType.TUNNEL:
                for portal in feature.portals:
                    col, row = projector.resolve_track_cell(portal.point, canvas)
                    if 0 <= col < inner_w and 0 <= row < inner_h:
                        if (col, row) not in occupied_cells:
                            try:
                                win.addstr(1 + row, 1 + col, portal_glyph, portal_attr)
                                occupied_cells.add((col, row))
                            except curses.error:
                                pass
            elif ftype == InfrastructureType.BRIDGE:
                col, row = projector.resolve_track_cell(feature.point, canvas)
                if 0 <= col < inner_w and 0 <= row < inner_h:
                    if (col, row) not in occupied_cells:
                        try:
                            win.addstr(1 + row, 1 + col, bridge_glyph, bridge_attr)
                            occupied_cells.add((col, row))
                        except curses.error:
                            pass
            elif ftype == InfrastructureType.SIGNAL and draw_signals:
                col, row = projector.resolve_track_cell(feature.point, canvas)
                if 0 <= col < inner_w and 0 <= row < inner_h:
                    if (col, row) not in occupied_cells:
                        try:
                            win.addstr(1 + row, 1 + col, signal_glyph, signal_attr)
                            occupied_cells.add((col, row))
                        except curses.error:
                            pass

    def _render_map_trains(
        self,
        win: Any,
        snapshot: StateSnapshot,
        projector: GeoProjector,
        canvas: BrailleCanvas,
        inner_w: int,
        inner_h: int,
    ) -> None:
        """Overlay live train heading glyphs and selected train callout label."""
        selected_record = snapshot.selected_train
        for record in snapshot.filtered_trains:
            pos = record.position
            snapped_pt = snap_to_track_network(
                GeoPoint(lat=pos.lat, lng=pos.lng),
                self.tracks,
                corridor_hint=pos.corridor,
            )
            col, row = projector.resolve_track_cell(snapped_pt, canvas)
            if not (0 <= col < inner_w and 0 <= row < inner_h):
                continue

            is_selected = selected_record is not None and pos.id == selected_record.position.id
            heading_deg = pos.track_heading if pos.track_heading is not None else pos.course
            glyph = get_heading_glyph(heading_deg, ascii_mode=self.ascii_mode)
            c_alert = getattr(record, "active_conflict", None)
            if c_alert and c_alert.severity == ConflictSeverity.CRITICAL:
                train_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            elif c_alert and c_alert.severity == ConflictSeverity.WARNING:
                train_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD
            else:
                train_attr = safe_color_pair(get_train_color_pair(record), curses.A_BOLD)
            if is_selected:
                train_attr |= curses.A_REVERSE | curses.A_BOLD

            try:
                win.addstr(1 + row, 1 + col, glyph, train_attr)
            except curses.error:
                pass

            if is_selected:
                tn = pos.train_number or pos.id
                spd_str = f"{int(round(pos.speed or 0))}km/h"
                dly_val = int(round(pos.delay or 0))
                dly_str = f"+{dly_val}m" if dly_val > 0 else (f"{dly_val}m" if dly_val < 0 else "0m")
                tag = f" {self.glyphs.callout_arrow} [{tn}] ({spd_str}, {dly_str})"
                safe_addstr(
                    win,
                    1 + row,
                    1 + col + 1,
                    tag,
                    train_attr,
                    max_w=inner_w - (col + 1),
                    ascii_mode=self.ascii_mode,
                )

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

        try:
            if self.ascii_mode:
                win.border(ord('|'), ord('|'), ord('-'), ord('-'), ord('+'), ord('+'), ord('+'), ord('+'))
            else:
                win.box()
        except curses.error:
            pass

        center = projector.viewport.center
        follow_str = ""
        if snapshot.follow_mode and snapshot.selected_train:
            tn = snapshot.selected_train.position.train_number or snapshot.selected_train.position.id
            follow_str = f" | Follow: #{tn}"
        breadcrumb_mode = getattr(snapshot, "breadcrumb_mode", BreadcrumbMode.ALL)
        trails_label = (
            "SEL"
            if breadcrumb_mode == BreadcrumbMode.SELECTED
            else ("OFF" if breadcrumb_mode == BreadcrumbMode.OFF else "ALL")
        )
        station_label_mode = getattr(
            snapshot, "station_label_mode", StationLabelMode.HUBS_AND_SECONDARY
        )
        track_color_mode = getattr(snapshot, "track_color_mode", TrackColorMode.UNIFORM)
        layer_str = (
            " | Labels: HUBS"
            if station_label_mode == StationLabelMode.HUBS_ONLY
            else (" | Labels: OFF" if station_label_mode == StationLabelMode.MARKERS_ONLY else "")
        )
        if track_color_mode == TrackColorMode.CORRIDOR:
            layer_str += " | Tracks: COLOR"
        infra_mode = getattr(snapshot, "infrastructure_mode", InfrastructureLayerMode.ALL)
        if infra_mode == InfrastructureLayerMode.ALL:
            layer_str += " | GIS: ALL"
        elif infra_mode == InfrastructureLayerMode.TUNNELS_BRIDGES:
            layer_str += " | GIS: TUNNELS"
        d_sym = self.glyphs.degree_sym
        title = (
            f" Map Viewport: {center.lat:.2f}{d_sym}N, {center.lng:.2f}{d_sym}E"
            f" | Zoom: {projector.viewport.zoom:.1f}x | Trails: {trails_label}{layer_str}{follow_str} "
        )
        safe_addstr(win, 0, 2, f"[{title}]", border_attr | curses.A_BOLD, max_w=max_x - 4, ascii_mode=self.ascii_mode)

        inner_h = max_y - 2
        inner_w = max_x - 2

        # 1. Rasterize tracks using BrailleCanvas
        canvas = BrailleCanvas(width_cells=inner_w, height_cells=inner_h, ascii_mode=self.ascii_mode)
        self._render_map_tracks(win, canvas, projector, inner_w, inner_h, track_color_mode, track_attr)

        # 2. Overlay historical breadcrumbs
        self._render_map_breadcrumbs(
            win, snapshot, projector, canvas, inner_w, inner_h, breadcrumb_mode, trail_attr
        )

        # 3. Overlay scale bar reservation and stations
        occupied_cells: Set[Tuple[int, int]] = set()
        scale_bar_str: Optional[str] = None
        if max_y >= 4 and max_x >= 16:
            max_bar_w = min(18, max_x - 4)
            scale_spec = projector.compute_scale_bar(min_cells=min(12, max_bar_w), max_cells=max_bar_w)
            candidate_bar = format_scale_bar(
                scale_spec.distance_km,
                width_cells=scale_spec.width_cells,
                ascii_mode=self.ascii_mode,
            )
            bar_w = get_string_display_width(candidate_bar)
            if bar_w <= max_x - 4:
                scale_bar_str = candidate_bar
                if 0 <= inner_h - 1 < inner_h:
                    for dx in range(bar_w):
                        occupied_cells.add((1 + dx, inner_h - 1))

        self._render_map_stations(
            win,
            snapshot,
            projector,
            canvas,
            inner_w,
            inner_h,
            station_label_mode,
            occupied_cells,
            station_attr,
            station_dim_attr,
        )

        # 4. Overlay railway infrastructure assets (portals, bridges, signals)
        self._render_map_infrastructure(
            win,
            snapshot,
            projector,
            canvas,
            inner_w,
            inner_h,
            occupied_cells,
        )

        if scale_bar_str is not None:
            safe_addstr(
                win,
                max_y - 2,
                2,
                scale_bar_str,
                border_attr | curses.A_BOLD,
                max_w=max_x - 4,
                ascii_mode=self.ascii_mode,
            )

        # 4. Overlay Live Trains and Directional Heading Glyphs
        self._render_map_trains(win, snapshot, projector, canvas, inner_w, inner_h)

    def _format_roster_type(self, train_type: Optional[str]) -> str:
        """Format 2-character type abbreviation for active trains roster."""
        t_type = (train_type or "IC")[:4].upper()
        if "PROAST" in t_type or "SUB" in t_type:
            return "PR"
        if "PASS" in t_type or "INTER" in t_type:
            return "IC"
        if "FREI" in t_type or "CARG" in t_type:
            return "FR"
        if "SHUN" in t_type:
            return "SH"
        return t_type[:2] if len(t_type) >= 2 else t_type

    def _resolve_roster_status_and_pair(
        self, record: TrainRecord, is_overspeed: bool
    ) -> Tuple[str, int]:
        """Resolve 3-letter status mnemonic and color pair for active trains roster."""
        conflict = getattr(record, "active_conflict", None)
        if conflict is not None:
            if conflict.severity == ConflictSeverity.CRITICAL:
                code = "!OP" if conflict.conflict_type == ConflictType.OPPOSING_COLLISION else "!HW"
                return code, PAIR_TRAIN_ALERT
            if conflict.severity == ConflictSeverity.WARNING:
                code = "?OP" if conflict.conflict_type == ConflictType.OPPOSING_COLLISION else "?HW"
                return code, PAIR_TRAIN_WARN
            if conflict.severity == ConflictSeverity.ADVISORY or conflict.conflict_type == ConflictType.STATION_MEET:
                return "MET", PAIR_TRAIN_PROAST

        pos = record.position
        st_code = "RUN" if (pos.speed is None or pos.speed >= 1.0) else "HLT"
        health = getattr(record, "sensor_health", None)
        if health is not None:
            st_code = health.short_code
            if health.is_critical:
                return st_code, PAIR_TRAIN_ALERT
            if health.state == SensorHealthState.STATION_DWELL:
                return st_code, PAIR_TRAIN_PROAST
            if health.state in (SensorHealthState.TUNNEL_TRANSIT, SensorHealthState.STALE):
                return st_code, PAIR_TRAIN_WARN

        if is_overspeed:
            return st_code, PAIR_TRAIN_ALERT
        return st_code, get_train_color_pair(record)

    def _format_roster_header(self, sort_mode: RosterSortMode) -> str:
        """Construct table header string with sort direction indicator."""
        if sort_mode == RosterSortMode.SPEED:
            return f" #   TRAIN  TYPE  SPD{self.glyphs.sort_desc}  DLY  ST"
        if sort_mode == RosterSortMode.DELAY:
            return f" #   TRAIN  TYPE  SPD   DLY{self.glyphs.sort_desc} ST"
        return f" #   TRAIN{self.glyphs.sort_asc} TYPE  SPD   DLY  ST"


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

        sort_mode = getattr(snapshot, "sort_mode", RosterSortMode.TRAIN_ID)
        table_hdr = self._format_roster_header(sort_mode)
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

            watched_ids = getattr(snapshot, "watched_train_ids", ())
            pos_id_norm = str(pos.id or "").strip().upper()
            pos_num_norm = str(pos.train_number or "").strip().upper()
            is_watched = bool(pos_id_norm in watched_ids or pos_num_norm in watched_ids)

            cursor_mark = ">" if is_selected else " "
            watch_mark = ("*" if self.ascii_mode else "●") if is_watched else " "
            seq_num = f"{item_idx + 1:2d}"
            train_num = f"{(pos.train_number or pos.id)[:5]:<5}"
            t_type = self._format_roster_type(pos.train_type)

            spd = int(round(pos.speed or 0))
            overspeed_ids = getattr(snapshot, "overspeed_train_ids", ())
            is_overspeed = pos.id in overspeed_ids
            spd_mark = ("!" if not self.ascii_mode else "*") if is_overspeed else " "
            spd_str = f"{spd_mark}{spd:<3d}" if is_overspeed else f"{spd:4d}"

            dly = int(round(pos.delay or 0))
            dly_str = f"+{dly}m" if dly > 0 else (f"{dly}m" if dly < 0 else " 0m")

            st_code, pair = self._resolve_roster_status_and_pair(record, is_overspeed)
            row_line = f"{cursor_mark}{watch_mark}{seq_num}  {train_num:<6}{t_type:<5}{spd_str:>4} {dly_str:>5}  {st_code}"
            row_attr = select_attr if is_selected else safe_color_pair(pair, curses.A_NORMAL)
            safe_addstr(win, 2 + r_idx, 1, row_line, row_attr, max_w=max_x - 2, ascii_mode=self.ascii_mode)

    def render_station_inspector(
        self,
        win: Any,
        snapshot: StateSnapshot,
    ) -> None:
        """Render the Station Departure Board & Proximity Inspector mode.

        Args:
            win: Inspector curses window.
            snapshot: Current StateSnapshot with selected_station_id.
        """
        win.erase()
        max_y, max_x = win.getmaxyx()
        if max_y < 3 or max_x < 3:
            return

        border_attr = safe_color_pair(PAIR_BORDER, curses.A_NORMAL)
        title_attr = safe_color_pair(PAIR_HEADER, curses.A_BOLD) | curses.A_BOLD
        val_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL)
        dim_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL) | curses.A_DIM
        select_attr = safe_color_pair(PAIR_ROSTER_SELECT, curses.A_REVERSE) | curses.A_BOLD

        try:
            if self.ascii_mode:
                win.border(ord('|'), ord('|'), ord('-'), ord('-'), ord('+'), ord('+'), ord('+'), ord('+'))
            else:
                win.box()
        except curses.error:
            pass

        station = self.resolve_station(getattr(snapshot, "selected_station_id", None))
        if station is None:
            safe_addstr(win, 0, 2, "[ STATION BOARD ]", border_attr | curses.A_BOLD, max_w=max_x - 4, ascii_mode=self.ascii_mode)
            safe_addstr(win, 2, 2, "Station not found.", dim_attr, max_w=max_x - 4, ascii_mode=self.ascii_mode)
            return

        stn_label = station.name_en or station.name_el or station.id
        if "(" in stn_label and max_x < 36:
            stn_label = stn_label.split("(", 1)[0].strip() or stn_label
        if max_x < 32:
            framed_title = f"[STATION BOARD: {stn_label}]"
        else:
            framed_title = f"[ STATION BOARD: {stn_label} ]"
        safe_addstr(win, 0, 2, framed_title, border_attr | curses.A_BOLD, max_w=max_x - 4, ascii_mode=self.ascii_mode)

        inner_h = max_y - 2
        if inner_h <= 0:
            return

        corridor_str = format_corridor_display_name(station.corridor)
        coords_str = format_wgs84_coords(station.point.lat, station.point.lng, ascii_mode=self.ascii_mode)

        if inner_h >= 1:
            safe_addstr(win, 1, 2, "Corridor:", title_attr, max_w=9, ascii_mode=self.ascii_mode)
            safe_addstr(win, 1, 12, corridor_str, val_attr, max_w=max_x - 13, ascii_mode=self.ascii_mode)
        if inner_h >= 2:
            safe_addstr(win, 2, 2, "Coords:", title_attr, max_w=9, ascii_mode=self.ascii_mode)
            safe_addstr(win, 2, 12, coords_str, val_attr, max_w=max_x - 13, ascii_mode=self.ascii_mode)

        selected_tid = snapshot.selected_train.position.id if snapshot.selected_train else None
        entries = compute_station_board_entries(
            station=station,
            trains=snapshot.filtered_trains,
            resolve_station_fn=self.resolve_station,
            selected_train_id=selected_tid,
        )

        # Leave a blank visual spacer row on taller windows (inner_h >= 10), pack tightly on 80x24 (inner_h = 9)
        sec_hdr_y = 4 if inner_h >= 10 else 3
        tbl_hdr_y = sec_hdr_y + 1
        rows_start_y = tbl_hdr_y + 1

        if sec_hdr_y <= inner_h:
            safe_addstr(
                win,
                sec_hdr_y,
                2 if max_x >= 30 else 1,
                "APPROACHING / AT STATION:",
                title_attr,
                max_w=max_x - 3,
                ascii_mode=self.ascii_mode,
            )

        compact_cols = max_x < 32
        col_x = 1 if compact_cols else 2
        if tbl_hdr_y <= inner_h:
            tbl_hdr = "TRAIN DIR DIST   ETA  DLY" if compact_cols else "TRAIN  DIR  DIST   ETA   DLY"
            safe_addstr(win, tbl_hdr_y, col_x, tbl_hdr, title_attr, max_w=max_x - col_x - 1, ascii_mode=self.ascii_mode)

        if rows_start_y > inner_h:
            return

        if not entries:
            safe_addstr(
                win,
                rows_start_y,
                col_x,
                "No trains within 80km.",
                dim_attr,
                max_w=max_x - col_x - 1,
                ascii_mode=self.ascii_mode,
            )
            return

        max_rows = max(0, (inner_h - rows_start_y) + 1)
        for idx, entry in enumerate(entries[:max_rows]):
            y = rows_start_y + idx
            if compact_cols:
                row_text = (
                    f"{entry.train_number[:5]:<5} {entry.direction:<3} "
                    f"{entry.dist_str:<6} {entry.eta_str:<4} {entry.delay_str:>4}"
                )
            else:
                row_text = (
                    f"{entry.train_number[:5]:<6} {entry.direction:<3}  "
                    f"{entry.dist_str:<6} {entry.eta_str:<5} {entry.delay_str:>3}"
                )
            row_attr = select_attr if entry.is_selected else safe_color_pair(entry.color_pair, curses.A_NORMAL)
            safe_addstr(win, y, col_x, row_text, row_attr, max_w=max_x - col_x - 1, ascii_mode=self.ascii_mode)

    @staticmethod
    def _format_inspector_speed(
        pos: Any,
        record: Any,
        snapshot: Any,
        val_attr: int,
        dim_attr: int,
    ) -> Tuple[str, int, str, int]:
        """Format locomotive speed display, color attribute, line limit, and limit color attribute."""
        spd_val = pos.speed or 0.0
        spd_display = f"{spd_val:5.1f} km/h"
        spd_attr = val_attr
        limit_display = "N/A"
        limit_attr = dim_attr

        speed_result = getattr(snapshot, "selected_speed_result", None) or getattr(record, "speed_result", None)
        if speed_result is None:
            return spd_display, spd_attr, limit_display, limit_attr

        spd_val = speed_result.current_speed
        gov_limit = speed_result.governing_limit

        if speed_result.severity == OverspeedSeverity.TSR_VIOLATION:
            spd_display = f"{spd_val:5.1f} km/h [TSR OVERSPEED]"
            spd_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            limit_display = f"{gov_limit:3.0f} km/h [{speed_result.governing_name}]"
            limit_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
        elif speed_result.severity == OverspeedSeverity.OVERSPEED:
            delta_int = int(round(speed_result.margin_kmh))
            spd_display = f"{spd_val:5.1f} km/h [OVERSPEED +{delta_int}]"
            spd_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            limit_display = f"{gov_limit:3.0f} km/h ({speed_result.governing_name})"
            limit_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD)
        elif speed_result.is_tsr:
            spd_display = f"{spd_val:5.1f} km/h [TSR: {int(round(gov_limit))} km/h]"
            spd_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD)
            limit_display = f"{gov_limit:3.0f} km/h [{speed_result.governing_name}]"
            limit_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_NORMAL)
        elif speed_result.severity == OverspeedSeverity.ADVISORY:
            delta_int = int(round(speed_result.margin_kmh))
            spd_display = f"{spd_val:5.1f} km/h [WARN +{delta_int}]"
            spd_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD)
            limit_display = f"{gov_limit:3.0f} km/h ({speed_result.governing_name})"
            limit_attr = val_attr
        else:
            spd_display = f"{spd_val:5.1f} km/h"
            spd_attr = val_attr
            limit_display = f"{gov_limit:3.0f} km/h ({speed_result.governing_name})"
            limit_attr = val_attr

        return spd_display, spd_attr, limit_display, limit_attr

    def _resolve_inspector_delay_display(self, pos: Any) -> Tuple[str, int]:
        """Resolve inspector delay display text and color attribute."""
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
        return dly_display, delay_attr

    def _resolve_inspector_telemetry_and_sensor(
        self,
        record: TrainRecord,
        ref_now: Optional[float],
        val_attr: int,
    ) -> Tuple[str, int, str, int]:
        """Resolve telemetry age and sensor health strings and color attributes."""
        telemetry_str, telemetry_pair = format_telemetry_age(
            record.last_updated,
            record.update_count,
            now=ref_now,
        )
        telemetry_attr = safe_color_pair(
            telemetry_pair,
            curses.A_BOLD if telemetry_pair != PAIR_DEFAULT else curses.A_NORMAL,
        )
        if telemetry_pair != PAIR_DEFAULT:
            telemetry_attr |= curses.A_BOLD

        health = getattr(record, "sensor_health", None)
        if health is not None:
            if health.state == SensorHealthState.FROZEN:
                telemetry_str = f"{int(health.elapsed_sec)}s ago [FROZEN]"
                telemetry_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            elif health.state == SensorHealthState.DROPPED:
                telemetry_str = f"{telemetry_str} [DROPPED]"
                telemetry_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            elif health.state == SensorHealthState.TUNNEL_TRANSIT:
                telemetry_str = f"{telemetry_str} [TUNNEL]"
                telemetry_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD
            elif health.state == SensorHealthState.STALE:
                telemetry_str = f"{telemetry_str} [STALE]"
                telemetry_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD

        sensor_health_str = "NORMAL"
        sensor_health_attr = val_attr
        if health is not None:
            sensor_health_str = health.description
            if health.state in (SensorHealthState.FROZEN, SensorHealthState.DROPPED):
                sensor_health_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            elif health.state == SensorHealthState.STATION_DWELL:
                sensor_health_attr = safe_color_pair(PAIR_TRAIN_PROAST, curses.A_BOLD) | curses.A_BOLD
            elif health.state in (SensorHealthState.TUNNEL_TRANSIT, SensorHealthState.STALE):
                sensor_health_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD

        return telemetry_str, telemetry_attr, sensor_health_str, sensor_health_attr

    def _resolve_inspector_status_text(
        self,
        record: TrainRecord,
        train_pt: GeoPoint,
        val_attr: int,
    ) -> Tuple[str, int]:
        """Resolve inspector operational and tunnel status text and color attribute."""
        pos = record.position
        health = getattr(record, "sensor_health", None)
        tunnel_info = detect_train_tunnel(train_pt, self.tunnels)
        if tunnel_info is not None:
            active_tun, _dist_m, _pct = tunnel_info
            len_km = (active_tun.length_m / 1000.0) if active_tun.length_m else 0.0
            return (
                f"RUNNING [TUNNEL: {active_tun.name} ({len_km:.1f}km)]",
                safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD,
            )
        if health is not None:
            if health.state == SensorHealthState.TUNNEL_TRANSIT:
                return (
                    f"TUNNEL TRANSIT [{health.nearest_station or 'Tunnel'}]",
                    safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD,
                )
            if health.state == SensorHealthState.FROZEN:
                return (
                    f"SENSOR FREEZE [{int(health.static_duration_sec)}s static]",
                    safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD,
                )
            if health.state == SensorHealthState.DROPPED:
                return (
                    f"SIGNAL LOST [{int(health.elapsed_sec)}s silent]",
                    safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD,
                )
            if health.state == SensorHealthState.STATION_DWELL:
                return (
                    f"STATION DWELL [{health.nearest_station or 'Platform'}]",
                    safe_color_pair(PAIR_TRAIN_PROAST, curses.A_NORMAL),
                )
        return f"{pos.status or 'In Service'}", val_attr

    def render_inspector(
        self,
        win: Any,
        snapshot: StateSnapshot,
    ) -> None:
        """Render the Selected Train Telemetry or Station Departure Board Inspector pane.

        Args:
            win: Inspector curses window.
            snapshot: Current StateSnapshot.
        """
        if (
            getattr(snapshot, "inspector_mode", InspectorMode.TRAIN) == InspectorMode.STATION
            and getattr(snapshot, "selected_station_id", None)
        ):
            self.render_station_inspector(win, snapshot)
            return

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

        dly_display, delay_attr = self._resolve_inspector_delay_display(pos)
        spd_display, spd_attr, limit_display, limit_attr = self._format_inspector_speed(
            pos, record, snapshot, val_attr, dim_attr
        )

        origin = pos.origin or "N/A"
        dest = pos.destination or "N/A"
        route_str = f"{origin} -> {dest}"

        chainage_km = (pos.chainage_m / 1000.0) if pos.chainage_m is not None else None
        chainage_str = f"{chainage_km:6.1f} km" if chainage_km is not None else "N/A"

        progress_str = format_progress_bar(pos.progress, max_x - 14)

        speed_history = record.get_speed_history(max_samples=10)
        sparkline_str = format_speed_sparkline(speed_history, max_bars=10, ascii_mode=self.ascii_mode)
        ref_now = (
            getattr(snapshot, "replay_virtual_time", None)
            if getattr(snapshot, "is_replay", False)
            else None
        )
        (
            telemetry_str,
            telemetry_attr,
            sensor_health_str,
            sensor_health_attr,
        ) = self._resolve_inspector_telemetry_and_sensor(record, ref_now, val_attr)

        matched_station = self.resolve_station(pos.next_station)
        train_pt = GeoPoint(lat=pos.lat, lng=pos.lng)
        next_station_str = format_next_station_eta(
            pos.next_station,
            train_point=train_pt,
            station_node=matched_station,
            speed_kmh=pos.speed,
        )
        coords_str = format_wgs84_coords(pos.lat, pos.lng, ascii_mode=self.ascii_mode)

        has_cam = any(
            k in str(pos.train_number or "").upper() for k in ("IC", "159", "223", "131")
        ) or (pos.train_type in ("Intercity", "Proastiakos", "Suburban"))
        cam_display = "ONLINE [w]" if has_cam else "UNAVAILABLE"

        status_text, status_attr = self._resolve_inspector_status_text(record, train_pt, val_attr)

        rows = [
            ("Train No:", f"{pos.train_number or pos.id} ({pos.train_type or 'Passenger'})", val_attr),
            ("Route:", route_str, val_attr),
            ("Next Station:", next_station_str, val_attr),
            ("Speed:", spd_display, spd_attr),
            ("Speed Trend:", sparkline_str, val_attr),
            ("Delay:", dly_display, delay_attr),
            ("Telemetry:", telemetry_str, telemetry_attr),
            ("Progress:", progress_str, val_attr),
            ("Coordinates:", coords_str, val_attr),
            ("Heading:", compass_str, val_attr),
            ("Sensor:", sensor_health_str, sensor_health_attr),
            ("Line Limit:", limit_display, limit_attr),
            ("Locomotive:", f"{pos.locomotive_number or 'N/A'}", val_attr),
            ("Cab Camera:", cam_display, val_attr),
            ("Status:", status_text, status_attr),
            ("Chainage:", chainage_str, val_attr),
        ]

        conflict = getattr(record, "active_conflict", None)
        if conflict is not None:
            c_mark = "!" if self.ascii_mode else "⚠"
            if conflict.severity == ConflictSeverity.CRITICAL:
                c_display = f"[{c_mark} CRITICAL] {conflict.conflict_type.value}: {conflict.train_number_2} ({conflict.distance_km:.1f}km)"
                c_attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_BOLD) | curses.A_BOLD
            elif conflict.severity == ConflictSeverity.WARNING:
                c_display = f"[{c_mark} WARNING] {conflict.conflict_type.value}: {conflict.train_number_2} ({conflict.distance_km:.1f}km)"
                c_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD
            else:
                tag = "MEET" if conflict.conflict_type == ConflictType.STATION_MEET else "ADVISORY"
                c_display = f"[{tag}] {conflict.description}"
                if len(c_display) > 52:
                    c_display = f"[{tag}] {conflict.station_proximity_name or conflict.conflict_type.value} ({conflict.distance_km:.2f}km)"
                c_attr = safe_color_pair(PAIR_TRAIN_PROAST, curses.A_NORMAL)
            rows.insert(3, ("Conflict:", c_display, c_attr))
            if conflict.time_to_collision_sec is not None:
                ttc_display = f"~{int(conflict.time_to_collision_sec)}s (Rel: {conflict.closing_speed_kmh:.0f} km/h)"
                rows.insert(4, ("TTC / Rel:", ttc_display, c_attr))

        inner_h = max_y - 2
        for r_idx, (label, value, attr) in enumerate(rows):
            if r_idx >= inner_h:
                break
            y = 1 + r_idx
            safe_addstr(win, y, 2, f"{label:<13}", title_attr, max_w=13, ascii_mode=self.ascii_mode)
            safe_addstr(win, y, 16, value, attr, max_w=max_x - 17, ascii_mode=self.ascii_mode)

    def _resolve_replay_status_badge(
        self,
        snapshot: StateSnapshot,
        playback_state: PlaybackState,
        connected_attr: int,
        warn_attr: int,
        alert_attr: int,
    ) -> Tuple[str, int]:
        """Format the right-aligned status bar badge when SQLite historical DVR replay is active."""
        if snapshot.connection_status == ConnectionStatus.ERROR:
            return self.glyphs.badge_conn_error, alert_attr

        v_now = float(getattr(snapshot, "replay_virtual_time", None) or 0.0)
        v_start = float(getattr(snapshot, "replay_start_time", None) or v_now)
        v_end = float(getattr(snapshot, "replay_end_time", None) or v_now)
        span = max(0.0, v_end - v_start)
        pct = int(round(((v_now - v_start) / span) * 100)) if span > 1e-6 else 100
        pct = max(0, min(100, pct))

        hh_mm_ss = (
            datetime.fromtimestamp(v_now, tz=timezone.utc).strftime("%H:%M:%S")
            if v_now > 1e-6
            else "--:--:--"
        )
        spd_int = int(round(getattr(snapshot, "replay_speed", 1.0) or 1.0))
        is_paused = (
            playback_state == PlaybackState.PAUSED
            or snapshot.connection_status == ConnectionStatus.PAUSED
        )
        if is_paused:
            icon = "||" if self.ascii_mode else "❚❚"
            return f" {icon} DVR {spd_int}x ({hh_mm_ss} | {pct}%) ", warn_attr
        icon = ">" if self.ascii_mode else "▶"
        return f" {icon} DVR {spd_int}x ({hh_mm_ss} | {pct}%) ", connected_attr

    def _resolve_status_badge(
        self,
        snapshot: StateSnapshot,
        playback_state: PlaybackState,
        msg_rate: float,
        connected_attr: int,
        status_attr: int,
        warn_attr: int,
        alert_attr: int,
    ) -> Tuple[str, int]:
        """Determine the right-hand status bar connectivity or DVR replay badge and color attribute."""
        if getattr(snapshot, "is_replay", False):
            return self._resolve_replay_status_badge(
                snapshot, playback_state, connected_attr, warn_attr, alert_attr
            )
        if playback_state == PlaybackState.PAUSED:
            return self.glyphs.badge_paused, warn_attr

        status = snapshot.connection_status
        if status == ConnectionStatus.CONNECTED:
            overspeed_cnt = getattr(snapshot, "overspeed_count", 0)
            frozen_cnt = getattr(snapshot, "frozen_count", 0)
            dropped_cnt = getattr(snapshot, "dropped_count", 0)
            crit_conflicts = getattr(snapshot, "critical_conflict_count", 0)
            warn_conflicts = getattr(snapshot, "warning_conflict_count", 0)
            alert_items = []
            if crit_conflicts > 0:
                alert_items.append(f"{crit_conflicts} CRIT CONFLICT")
            elif warn_conflicts > 0:
                alert_items.append(f"{warn_conflicts} CONFLICT")
            if overspeed_cnt > 0:
                alert_items.append(f"{overspeed_cnt} OVERSPEED")
            if frozen_cnt > 0:
                alert_items.append(f"{frozen_cnt} FROZEN")
            if dropped_cnt > 0:
                alert_items.append(f"{dropped_cnt} DROPPED")
            if alert_items:
                mark = "!" if self.ascii_mode else "⚠"
                return f"[{mark} {', '.join(alert_items)}] {self.glyphs.badge_live}({msg_rate:3.1f} msg/s) ", alert_attr
            return f"{self.glyphs.badge_live}({msg_rate:3.1f} msg/s) ", connected_attr
        if status == ConnectionStatus.CONNECTING:
            return self.glyphs.badge_connecting, status_attr
        if status == ConnectionStatus.RECONNECTING:
            return self.glyphs.badge_reconnecting, warn_attr
        if status == ConnectionStatus.ERROR:
            return self.glyphs.badge_conn_error, alert_attr
        return self.glyphs.badge_demo, connected_attr

    @staticmethod
    def _format_status_shortcuts(snapshot: StateSnapshot) -> str:
        """Build the bottom status bar keybindings cheat sheet string."""
        follow_str = (
            "[f] Unfollow"
            if (snapshot.follow_mode and snapshot.selected_train)
            else "[f] Follow"
        )
        if getattr(snapshot, "is_replay", False):
            return (
                f" [q] Quit  [h/j/k/l] Pan  [+/-] Zoom  [[/]] Scrub  [</>] Speed  [Space] Pause  "
                f"[Tab] Select  [s] Sort  [b] Trails  [e] Export  [m] Stations  {follow_str}  [F10/K] Theme  [/] Filter  [?] Help"
            )
        return (
            f" [q] Quit  [h/j/k/l] Pan  [+/-] Zoom  [Tab] Select  [s] Sort  [b] Trails  "
            f"[e] Export  [i] Timetable  [w] Cab View  [v] Labels  [t] Tracks  [m] Stations  {follow_str}  [F10/K] Theme  [/] Filter  [?] Help  [0..3] Presets"
        )

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
        toast_message: Optional[str] = None,
        toast_is_error: bool = False,
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
            toast_message: Optional transient toast notification banner text.
            toast_is_error: Whether toast_message represents a warning/error state.
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

        badge, b_attr = self._resolve_status_badge(
            snapshot,
            playback_state,
            msg_rate,
            connected_attr,
            status_attr,
            warn_attr,
            alert_attr,
        )
        badge_w = get_string_display_width(badge)
        badge_x = max(0, width - badge_w)

        if toast_message and toast_message.strip():
            toast_attr = warn_attr if toast_is_error else connected_attr
            toast_text = f" {toast_message.strip()} "
            safe_addstr(
                win,
                0,
                0,
                toast_text,
                toast_attr,
                max_w=max(0, width - badge_w - 1),
                ascii_mode=self.ascii_mode,
            )
        elif getattr(snapshot, "critical_conflict_count", 0) > 0 and snapshot.active_conflicts:
            crit_c = next(
                (c for c in snapshot.active_conflicts if c.severity == ConflictSeverity.CRITICAL),
                snapshot.active_conflicts[0],
            )
            mark = "!" if self.ascii_mode else "⚠"
            ttc_str = (
                f", TTC {int(crit_c.time_to_collision_sec)}s"
                if crit_c.time_to_collision_sec is not None
                else ""
            )
            c_text = (
                f" [{mark} CRITICAL CONFLICT: {crit_c.train_number_1} <-> {crit_c.train_number_2} "
                f"on {crit_c.corridor_name} ({crit_c.distance_km:.1f}km{ttc_str})] "
            )
            safe_addstr(
                win,
                0,
                0,
                c_text,
                alert_attr,
                max_w=max(0, width - badge_w - 1),
                ascii_mode=self.ascii_mode,
            )
        else:
            shortcuts = self._format_status_shortcuts(snapshot)
            safe_addstr(
                win,
                0,
                0,
                shortcuts,
                status_attr,
                max_w=max(0, width - max(24, badge_w + 1)),
                ascii_mode=self.ascii_mode,
            )

        safe_addstr(win, 0, badge_x, badge, b_attr, ascii_mode=self.ascii_mode)

    def render_help_modal(self, win: Any, height: int, width: int) -> None:
        """Render centered keybinding guide and symbols reference dialog overlay.

        Args:
            win: Root curses window to overlay onto.
            height: Total terminal screen height.
            width: Total terminal screen width.
        """
        box_h = min(25, max(17, height - 2))
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
                ("+ / =, - / _, Wheel", "Zoom In / Zoom Out (0.2x to 25.0x)"),
                ("0, 1, 2, 3", "Presets (0: Greece, 1: Suburban, 2: PATHE, 3: North)"),
                ("v / V, t / T, g / G", "Station labels / Track colors / Cycle GIS layers"),
            ]),
            ("TRAIN SELECTION & TRACKING", [
                ("Tab, n / p, Click", "Select Next / Previous or click train / station"),
                ("s / S", "Cycle train roster sorting (Train ID, Speed, Delay)"),
                ("b / B", "Cycle breadcrumb trails (All, Selected, Off)"),
                ("m / M", "Cycle station departure board / Return to train view"),
                ("i / I", "Inspect timetable & intermediate stops for selected train"),
                ("w / W", "Launch live locomotive forward cab video stream"),
                ("a / A", "Toggle proximity & delay push alerting watchdog"),
                ("f / F, Dbl-Click", "Toggle auto-follow camera lock (or recenter map)"),
                ("c / C", "Clear search filter, corridor filter & selection"),
            ]),
            ("SEARCH & SYSTEM CONTROLS", [
                ("/, Esc", "Enter search filter mode / Cancel and return"),
                ("e / E, Space", "Export train GeoJSON / CSV / Pause stream or DVR"),
                ("[ / ], < / >, ?, q", "DVR scrub -/+5m & speed (1x..60x) / Help / Quit"),
                ("F10 / K", "Cycle Color Themes (Azure, Amber, Matrix, Solarized, Mono)"),
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
                (
                    "n Tun  = Brg  S Sig" if self.ascii_mode else "∩ Tun  ═ Brg  ◆ Sig",
                    "Tunnels (Portals), Bridges & Interlockings",
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

    def _find_timetable_approaching_index(
        self,
        stops: Sequence[Any],
        next_station: Optional[str],
    ) -> Optional[int]:
        """Locate approaching station index in stops sequence matching next_station."""
        if not next_station:
            return None
        norm_target_latin = normalize_station_key(transliterate_greek_to_latin(str(next_station)))
        norm_raw_target = normalize_station_key(str(next_station))
        for i, st in enumerate(stops):
            st_name = resolve_timetable_station_name(st, is_ascii=True, empty_dash="", single_name=True)
            raw_name = st.get("name", "") if isinstance(st, dict) else getattr(st, "station_name_el", "")
            norm_st_latin = normalize_station_key(transliterate_greek_to_latin(st_name))
            norm_raw_latin = normalize_station_key(transliterate_greek_to_latin(str(raw_name)))
            norm_raw = normalize_station_key(str(raw_name))
            if (
                (norm_target_latin and (norm_target_latin in norm_st_latin or norm_target_latin in norm_raw_latin))
                or (norm_raw_target and (norm_raw_target in norm_raw or norm_raw_target in normalize_station_key(st_name)))
            ):
                return i
        return None

    def _format_timetable_stop_row(
        self,
        stop: Any,
        abs_idx: int,
        approaching_idx: Optional[int],
        box_w: int,
    ) -> Tuple[str, int]:
        """Format an individual timetable calling point row string and color attribute."""
        time_dash = "--:--"
        if isinstance(stop, dict):
            is_passed = stop.get("is_passed", False) or stop.get("status") == "DEPARTED"
            is_current = stop.get("is_current", False) or stop.get("status") == "APPROACH"
            sched_arr = format_timetable_time(stop.get("scheduledArrival"), time_dash)
            act_arr = format_timetable_time(stop.get("actualArrival"), time_dash)
            sched_dep = format_timetable_time(stop.get("scheduledDeparture"), time_dash)
            act_dep = format_timetable_time(stop.get("actualDeparture"), time_dash)
            plat = str(stop.get("platform") or "-")
        else:
            is_passed = getattr(stop, "is_passed", False)
            is_current = getattr(stop, "is_current", False)
            sched_arr = format_timetable_time(getattr(stop, "scheduled_arrival", None), time_dash)
            act_arr = format_timetable_time(getattr(stop, "actual_arrival", None), time_dash)
            sched_dep = format_timetable_time(getattr(stop, "scheduled_departure", None), time_dash)
            act_dep = format_timetable_time(getattr(stop, "actual_departure", None), time_dash)
            plat = str(getattr(stop, "platform", None) or "-")

        if approaching_idx is not None and not is_passed and not is_current:
            is_passed = (abs_idx < approaching_idx)
            is_current = (abs_idx == approaching_idx)

        stn_name = resolve_timetable_station_name(
            stop,
            is_ascii=self.ascii_mode,
            empty_dash="-",
            single_name=True,
        )

        ind = "[X]" if is_passed else ((" > " if self.ascii_mode else " ► ") if is_current else "   ")
        if is_current:
            row_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD
        elif is_passed:
            row_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL) | curses.A_DIM
        else:
            row_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL)

        stop_line = f" {ind:<4} {stn_name[:19]:<20} {sched_arr[:5]:^10} {act_arr[:5]:^11} {sched_dep[:5]:^10} {act_dep[:5]:^8} {plat:^4}"[: box_w - 4]
        return stop_line, row_attr

    def _format_timetable_title(self, timetable: Any, max_len: int) -> str:
        """Format modal header title with train number and route endpoints."""
        if not timetable:
            return " TIMETABLE & ITINERARY "[:max_len]
        train_no = str(
            getattr(timetable, "train_number", None)
            or (timetable.get("trainNumber") if isinstance(timetable, dict) else "")
            or (timetable.get("train_number") if isinstance(timetable, dict) else "")
            or ""
        )
        if isinstance(timetable, dict):
            orig_raw = timetable.get("origin")
            dest_raw = timetable.get("destination")
            orig = resolve_timetable_station_name(orig_raw, is_ascii=self.ascii_mode, empty_dash="", single_name=True) if orig_raw else ""
            dest = resolve_timetable_station_name(dest_raw, is_ascii=self.ascii_mode, empty_dash="", single_name=True) if dest_raw else ""
        else:
            orig = getattr(timetable, "origin_name", "")
            dest = getattr(timetable, "destination_name", "")
        arrow = "->" if self.ascii_mode else "→"
        orig_dest = f" ({orig} {arrow} {dest})" if orig or dest else ""
        return f" TIMETABLE & ITINERARY: TRAIN {train_no}{orig_dest} "[:max_len]

    def render_timetable_modal(
        self,
        win: Any,
        height: int,
        width: int,
        timetable: Any = None,
        scroll_offset: int = 0,
        is_loading: bool = False,
        error_msg: Optional[str] = None,
        next_station: Optional[str] = None,
    ) -> None:
        """Render centered train timetable and intermediate stop inspector modal dialog.

        Args:
            win: Root curses window to overlay onto.
            height: Total terminal screen height.
            width: Total terminal screen width.
            timetable: Timetable dictionary or TrainTimetableDTO.
            scroll_offset: Vertical stop scrolling offset.
            is_loading: True if timetable is currently being fetched asynchronously.
            error_msg: Optional error string if timetable retrieval failed.
            next_station: Name of approaching station from live telemetry for progress marking.
        """
        box_h = min(22, max(12, height - 2))
        box_w = min(82, max(46, width - 4))
        start_y = max(1, (height - box_h) // 2)
        start_x = max(1, (width - box_w) // 2)

        border_attr = safe_color_pair(PAIR_BORDER, curses.A_BOLD) | curses.A_BOLD
        header_attr = safe_color_pair(PAIR_HEADER, curses.A_BOLD) | curses.A_BOLD
        text_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL)
        dim_attr = safe_color_pair(PAIR_DEFAULT, curses.A_NORMAL) | curses.A_DIM
        current_attr = safe_color_pair(PAIR_TRAIN_WARN, curses.A_BOLD) | curses.A_BOLD
        footer_attr = safe_color_pair(PAIR_ROSTER_SELECT, curses.A_BOLD) | curses.A_BOLD

        # Background fill
        for row in range(box_h):
            safe_addstr(win, start_y + row, start_x, " " * box_w, text_attr, ascii_mode=self.ascii_mode)

        # Border
        top_border = "+" + "-" * (box_w - 2) + "+"
        bot_border = "+" + "-" * (box_w - 2) + "+"
        safe_addstr(win, start_y, start_x, top_border, border_attr, ascii_mode=self.ascii_mode)
        safe_addstr(win, start_y + box_h - 1, start_x, bot_border, border_attr, ascii_mode=self.ascii_mode)
        for r in range(1, box_h - 1):
            safe_addstr(win, start_y + r, start_x, "|", border_attr, ascii_mode=self.ascii_mode)
            safe_addstr(win, start_y + r, start_x + box_w - 1, "|", border_attr, ascii_mode=self.ascii_mode)

        # Title
        title = self._format_timetable_title(timetable, box_w - 4)
        t_x = max(start_x + 2, start_x + (box_w - len(title)) // 2)
        safe_addstr(win, start_y, t_x, title, header_attr, ascii_mode=self.ascii_mode)

        if is_loading:
            loading_txt = "Loading timetable from railway.gov.gr..."
            safe_addstr(win, start_y + box_h // 2, max(start_x + 2, start_x + (box_w - len(loading_txt)) // 2), loading_txt, current_attr, ascii_mode=self.ascii_mode)
            return

        if error_msg:
            err_txt = f"[Error: {error_msg}]"[: box_w - 4]
            safe_addstr(win, start_y + box_h // 2, max(start_x + 2, start_x + (box_w - len(err_txt)) // 2), err_txt, current_attr, ascii_mode=self.ascii_mode)
            return

        stops = []
        if isinstance(timetable, dict):
            stops = timetable.get("stops", [])
        elif hasattr(timetable, "stops"):
            stops = list(getattr(timetable, "stops", []))

        if not stops:
            empty_txt = "No intermediate stops returned for this service."
            safe_addstr(win, start_y + box_h // 2, max(start_x + 2, start_x + (box_w - len(empty_txt)) // 2), empty_txt, dim_attr, ascii_mode=self.ascii_mode)
            return

        # Header row
        header_line = f" {'ST':<4} {'STATION':<20} {'SCHED ARR':^10} {'ACTUAL ARR':^11} {'SCHED DEP':^10} {'ACT DEP':^8} {'PLAT':^4}"[: box_w - 4]
        safe_addstr(win, start_y + 1, start_x + 2, header_line, header_attr, ascii_mode=self.ascii_mode)
        safe_addstr(win, start_y + 2, start_x + 1, "-" * (box_w - 2), border_attr, ascii_mode=self.ascii_mode)

        # Stops
        visible_rows = calculate_timetable_visible_rows(height)
        max_offset = max(0, len(stops) - visible_rows)
        offset = max(0, min(max_offset, scroll_offset))

        approaching_idx = self._find_timetable_approaching_index(stops, next_station)

        for idx, stop in enumerate(stops[offset : offset + visible_rows]):
            row_y = start_y + 3 + idx
            if row_y >= start_y + box_h - 2:
                break
            abs_idx = offset + idx
            stop_line, row_attr = self._format_timetable_stop_row(stop, abs_idx, approaching_idx, box_w)
            safe_addstr(win, row_y, start_x + 2, stop_line, row_attr, ascii_mode=self.ascii_mode)

        # Footer
        footer_divider = "-" * (box_w - 2)
        safe_addstr(win, start_y + box_h - 2, start_x + 1, footer_divider, border_attr, ascii_mode=self.ascii_mode)
        last_visible = min(len(stops), offset + visible_rows)
        if len(stops) <= visible_rows:
            scroll_info = f"All {len(stops)} stops displayed"
        else:
            scroll_info = f"[▲ / ▼, j / k] Scroll (Stops {offset + 1}-{last_visible} of {len(stops)})"
        footer_txt = f" [Esc / i] Close    {scroll_info} "[: box_w - 4]
        safe_addstr(win, start_y + box_h - 2, start_x + 2, footer_txt, footer_attr, ascii_mode=self.ascii_mode)

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
        toast_message: Optional[str] = None,
        toast_is_error: bool = False,
        search_mode: Optional[bool] = None,
        show_help: Optional[bool] = None,
        paused: Optional[bool] = None,
        timetable_overlay: bool = False,
        timetable_data: Any = None,
        timetable_scroll_offset: int = 0,
        timetable_loading: bool = False,
        timetable_error: Optional[str] = None,
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
            toast_message: Optional transient toast notification banner text.
            toast_is_error: Whether toast_message represents a warning/error state.
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
                toast_message=toast_message,
                toast_is_error=toast_is_error,
            )

        if help_overlay == HelpOverlay.VISIBLE and "root" in windows:
            self.render_help_modal(windows["root"], geometry.term_height, geometry.term_width)
        elif timetable_overlay and "root" in windows:
            next_stn = None
            if snapshot.selected_train and snapshot.selected_train.position:
                next_stn = snapshot.selected_train.position.next_station
            self.render_timetable_modal(
                windows["root"],
                geometry.term_height,
                geometry.term_width,
                timetable=timetable_data,
                scroll_offset=timetable_scroll_offset,
                is_loading=timetable_loading,
                error_msg=timetable_error,
                next_station=next_stn,
            )
