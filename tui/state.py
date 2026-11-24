"""
Thread-safe state repository and reactive models for live train telemetry and UI state.
"""

from collections import OrderedDict, deque
from dataclasses import dataclass, field, replace
from enum import Enum, auto
import threading
import time
from typing import Any, Deque, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple

from train_tracker import DelayUpdate, GhostTrain, TrainHistoryPoint, TrainPosition
from tui.geo import (
    DEFAULT_CORRIDOR_SPEED_RULES,
    DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC,
    DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC,
    DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH,
    DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC,
    ConflictAlert,
    ConflictSeverity,
    ConflictWatchdogConfig,
    CorridorSpeedRule,
    GeoPoint,
    InfrastructureFeature,
    InfrastructureType,
    NORTHERN_GREECE_PRESET,
    OverspeedSeverity,
    PATHE_PRESET,
    PROASTIAKOS_PRESET,
    SensorHealthAssessment,
    SensorHealthState,
    SlowOrder,
    SpeedEnforcementResult,
    evaluate_sensor_health,
    evaluate_train_conflicts,
    evaluate_train_speed,
)
from tui.theme import ThemeProfile, get_next_theme


class ConnectionStatus(Enum):
    """Network connection state of the telemetry stream."""

    DISCONNECTED = auto()  # Stream is stopped or uninitialized
    CONNECTING = auto()  # Opening socket / establishing handshake
    CONNECTED = auto()  # Actively receiving valid position events
    RECONNECTING = auto()  # Connection dropped; waiting on backoff timer
    PAUSED = auto()  # Stream paused by user or backgrounding
    ERROR = auto()  # Terminal unrecoverable error


class InputMode(Enum):
    """Input handling modes for application event loop."""

    NORMAL = auto()  # General navigation, camera pan/zoom, presets, selection
    SEARCH = auto()  # Interactive live filter text input in status bar
    HELP = auto()  # Fullscreen keybinding reference dialog overlay
    TIMETABLE = auto()  # Intermediate Stop & Timetable Modal Overlay (F-6.2)


class RosterSortMode(Enum):
    """Ordering criteria for the active train roster table."""

    TRAIN_ID = auto()  # Ascending natural alphanumeric train number (e.g. 50 before 1597)
    SPEED = auto()  # Descending speed in km/h (fastest first), tie-broken by TRAIN_ID
    DELAY = auto()  # Descending delay in minutes (most delayed first), tie-broken by TRAIN_ID


class BreadcrumbMode(Enum):
    """Visibility policy for historical breadcrumb trails on the map canvas."""

    ALL = auto()  # Draw breadcrumb dots for all active filtered trains
    SELECTED = auto()  # Draw breadcrumb dots only for snapshot.selected_train
    OFF = auto()  # Hide all historical breadcrumb trails


class InspectorMode(Enum):
    """Display mode for the bottom-right Inspector pane."""

    TRAIN = auto()  # Default: Selected Train Telemetry Inspector
    STATION = auto()  # Station Departure Board & Proximity Inspector


class StationLabelMode(Enum):
    """Visibility policy for station text labels on the map canvas."""

    HUBS_AND_SECONDARY = auto()  # Default: All stations labeled (with collision avoidance)
    HUBS_ONLY = auto()  # Only major interchange/terminal hub labels (stn.is_hub)
    MARKERS_ONLY = auto()  # Draw station markers without text labels


class TrackColorMode(Enum):
    """Colorization policy for Braille railway track polylines on the map canvas."""

    UNIFORM = auto()  # Default: Uniform dim-white tracks (PAIR_TRACK)
    CORRIDOR = auto()  # Corridor-colored tracks (PATHE=White, PROASTIAKOS=Cyan, Regional=Yellow)


class InfrastructureLayerMode(Enum):
    """Visibility policy for railway infrastructure GIS overlays on the map canvas."""

    ALL = auto()             # Display tunnels, bridges, and interlocking signals
    TUNNELS_BRIDGES = auto() # Display tunnels and bridges only (signals hidden)
    OFF = auto()             # Hide all infrastructure overlays


class GhostVisibilityMode(Enum):
    """Visibility policy for untracked scheduled ghost runs in TUI roster and boards (F-10.1)."""

    SHOW = auto()  # Default: display active non-superseded ghost runs
    HIDE = auto()  # Hide ghost runs from TUI roster and Station Departure Board


# Supported playback speed multipliers for SQLite Historical DVR Mode (F-4.2)
REPLAY_SPEED_MULTIPLIERS: Tuple[float, ...] = (1.0, 5.0, 15.0, 60.0)
DEFAULT_MAX_TRAIL_LEN: int = 25
DEFAULT_GHOST_STALE_TIMEOUT_SEC: float = 300.0
MAX_PENDING_HISTORY_TRAINS: int = 500
PENDING_HISTORY_TTL_SEC: float = 300.0


@dataclass(frozen=True)
class HistoricalBreadcrumb:
    """A historical geographic point recorded during train movement."""

    lat: float
    lng: float
    timestamp: float
    speed: float


@dataclass(frozen=True)
class TimetableStopDTO:
    """Normalized intermediate timetable stop record."""

    stop_sequence: int
    station_id: str
    station_name_el: str
    station_name_en: str
    scheduled_arrival: Optional[str] = None
    actual_arrival: Optional[str] = None
    scheduled_departure: Optional[str] = None
    actual_departure: Optional[str] = None
    platform: Optional[str] = None
    delay_minutes: int = 0
    status: str = "SCHEDULED"
    is_passed: bool = False
    is_current: bool = False


@dataclass(frozen=True)
class TrainTimetableDTO:
    """Complete scheduled itinerary for a train service."""

    train_number: str
    schedule_id: str
    train_type: str
    service_type: str
    origin_name: str
    destination_name: str
    corridor: str
    stops: Tuple[TimetableStopDTO, ...]
    current_delay_min: int = 0
    fetched_at: float = 0.0


DEFAULT_LIVE_STALE_TIMEOUT_SEC: float = 90.0
DEFAULT_REPLAY_STALE_TIMEOUT_SEC: float = 1800.0


@dataclass
class TrainRecord:
    """Stateful wrapper around TrainPosition maintaining motion history."""

    position: TrainPosition
    trail: Deque[HistoricalBreadcrumb] = field(default_factory=lambda: deque(maxlen=15))
    first_seen: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    update_count: int = 1
    speed_result: Optional[SpeedEnforcementResult] = None
    last_moved_pt: Optional[GeoPoint] = None
    last_moved_time: float = field(default_factory=time.time)
    station_dwell_start: Optional[float] = None
    sensor_health: Optional[SensorHealthAssessment] = None
    active_conflict: Optional[ConflictAlert] = None

    def clone(self) -> "TrainRecord":
        """Create a thread-isolated shallow copy with an independent trail deque."""
        return TrainRecord(
            position=self.position,
            trail=deque(self.trail, maxlen=self.trail.maxlen),
            first_seen=self.first_seen,
            last_updated=self.last_updated,
            update_count=self.update_count,
            speed_result=self.speed_result,
            last_moved_pt=self.last_moved_pt,
            last_moved_time=self.last_moved_time,
            station_dwell_start=self.station_dwell_start,
            sensor_health=self.sensor_health,
            active_conflict=self.active_conflict,
        )

    def append_breadcrumb(
        self,
        pos: TrainPosition,
        threshold_deg: float = 1e-5,
        now: Optional[float] = None,
    ) -> None:
        """Record a historical coordinate if position has shifted significantly."""
        ts = float(now) if now is not None else time.time()
        speed_val = float(pos.speed) if pos.speed is not None else 0.0
        if not self.trail or (
            abs(self.trail[-1].lat - pos.lat) > threshold_deg
            or abs(self.trail[-1].lng - pos.lng) > threshold_deg
        ):
            self.trail.append(
                HistoricalBreadcrumb(
                    lat=pos.lat,
                    lng=pos.lng,
                    timestamp=ts,
                    speed=speed_val,
                )
            )
        self.position = pos
        self.last_updated = ts
        self.update_count += 1

    def get_speed_history(self, max_samples: int = 15) -> List[float]:
        """Extract chronological speed samples from historical trail and current position.

        Args:
            max_samples: Maximum number of recent velocity readings to return.

        Returns:
            List of non-negative float speed values in km/h.
        """
        speeds: List[float] = [
            max(0.0, float(crumb.speed if crumb.speed is not None else 0.0))
            for crumb in self.trail
        ]
        curr_spd = max(0.0, float(self.position.speed)) if self.position.speed is not None else None
        if curr_spd is not None:
            if not speeds:
                speeds.append(curr_spd)
            elif abs(speeds[-1] - curr_spd) > 0.01:
                speeds.append(curr_spd)
        elif not speeds:
            speeds.append(0.0)

        if len(speeds) > max_samples:
            return speeds[-max_samples:]
        return speeds

    def is_stale(self, threshold_sec: float = DEFAULT_LIVE_STALE_TIMEOUT_SEC) -> bool:
        """Check if train has stopped receiving updates beyond threshold (default: 90 seconds)."""
        return (time.time() - self.last_updated) > threshold_sec


@dataclass
class GhostTrainRecord:
    """Stateful wrapper around a GhostTrain maintaining observation metadata (F-10.1)."""

    ghost: GhostTrain
    first_seen: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    update_count: int = 1

    def clone(self) -> "GhostTrainRecord":
        """Create a thread-isolated copy for immutable StateSnapshot."""
        return GhostTrainRecord(
            ghost=self.ghost,
            first_seen=self.first_seen,
            last_updated=self.last_updated,
            update_count=self.update_count,
        )


@dataclass(frozen=True)
class StateSnapshot:
    """Immutable point-in-time snapshot consumed by the Curses renderer."""

    trains: Dict[str, TrainRecord]
    filtered_trains: Tuple[TrainRecord, ...]
    selected_train: Optional[TrainRecord]
    selected_index: int
    connection_status: ConnectionStatus
    status_message: str
    total_messages_received: int
    last_stream_update: float
    follow_mode: bool
    filter_query: str
    corridor_filter: Optional[str]
    version: int
    is_recording: bool = False
    recording_db: Optional[str] = None
    sort_mode: RosterSortMode = RosterSortMode.TRAIN_ID
    breadcrumb_mode: BreadcrumbMode = BreadcrumbMode.ALL
    inspector_mode: InspectorMode = InspectorMode.TRAIN
    selected_station_id: Optional[str] = None
    station_label_mode: StationLabelMode = StationLabelMode.HUBS_AND_SECONDARY
    track_color_mode: TrackColorMode = TrackColorMode.UNIFORM
    infrastructure_mode: InfrastructureLayerMode = InfrastructureLayerMode.ALL
    theme_profile: ThemeProfile = ThemeProfile.AZURE
    is_replay: bool = False
    replay_db: Optional[str] = None
    replay_virtual_time: Optional[float] = None
    replay_start_time: Optional[float] = None
    replay_end_time: Optional[float] = None
    replay_speed: float = 1.0
    watched_train_ids: FrozenSet[str] = field(default_factory=frozenset)
    selected_speed_result: Optional[SpeedEnforcementResult] = None
    overspeed_count: int = 0
    active_tsr_count: int = 0
    overspeed_train_ids: Tuple[str, ...] = ()
    selected_sensor_health: Optional[SensorHealthAssessment] = None
    frozen_count: int = 0
    dropped_count: int = 0
    stale_count: int = 0
    frozen_train_ids: Tuple[str, ...] = ()
    dropped_train_ids: Tuple[str, ...] = ()
    active_conflicts: Tuple[ConflictAlert, ...] = ()
    critical_conflict_count: int = 0
    warning_conflict_count: int = 0
    advisory_conflict_count: int = 0
    selected_conflict: Optional[ConflictAlert] = None
    duplicate_train_numbers: FrozenSet[str] = field(default_factory=frozenset)
    ghost_trains: Dict[str, GhostTrainRecord] = field(default_factory=dict)
    filtered_ghosts: Tuple[GhostTrainRecord, ...] = ()
    selected_ghost: Optional[GhostTrainRecord] = None
    ghost_visibility_mode: GhostVisibilityMode = GhostVisibilityMode.SHOW
    ghost_count: int = 0
    history_hydrated_trains: int = 0
    delay_updates_applied: int = 0
    stream_event_counts: Tuple[Tuple[str, int], ...] = ()


# Known Hellenic Railways corridor keywords and aliases
_PROAST_CORRIDORS: Tuple[str, ...] = (
    "PROASTIAKOS", "PROASTIAKOS_ATH", "PIRAIR", "KIATO", "AERODROMIO", "AIRPORT", "CHALKIDA"
)
_PATHE_CORRIDORS: Tuple[str, ...] = ("PATHE", "E85", "MAINLINE")
_NORTHERN_CORRIDORS: Tuple[str, ...] = ("NORTHERN", "MACEDONIA", "THRACE", "STRYMONAS", "ALEX", "IDOMENI")
_THESSALY_CORRIDORS: Tuple[str, ...] = ("THESSALY", "VOLOS", "KALAMBAKA")
_PROAST_STATIONS: Tuple[str, ...] = ("ΑΕΡΟΔΡΟΜΙΟ", "ΚΙΑΤΟ", "ΧΑΛΚΙΔΑ", "ΛΙΟΣΙΑ", "ΝΕΡΑΤΖΙΩΤΙΣΣΑ")
_NORTHERN_STATIONS: Tuple[str, ...] = (
    "ΣΕΡΡΕΣ", "ΔΡΑΜΑ", "ΞΑΝΘΗ", "ΚΟΜΟΤΗΝΗ", "ΑΛΕΞΑΝΔΡΟΥΠΟΛΗ", "ΦΛΩΡΙΝΑ", "ΕΔΕΣΣΑ"
)
_THESSALY_STATIONS: Tuple[str, ...] = ("ΒΟΛΟΣ", "ΚΑΛΑΜΠΑΚΑ", "ΚΑΡΔΙΤΣΑ", "ΤΡΙΚΑΛΑ")


def _matches_explicit_corridor_tag(corridor: str, target: str) -> bool:
    """Match when telemetry record includes an explicit corridor string."""
    if target in corridor:
        return True
    if any(c in corridor for c in _PROAST_CORRIDORS):
        return target in ("PROASTIAKOS", "PROASTIAKOS_ATH", "SUBURBAN")
    if any(c in corridor for c in _PATHE_CORRIDORS):
        return target in ("PATHE", "PATHE_MAINLINE", "MAINLINE")
    if any(c in corridor for c in _NORTHERN_CORRIDORS):
        return target in ("NORTHERN", "NORTHERN_GREECE", "NORTH")
    if any(c in corridor for c in _THESSALY_CORRIDORS):
        return target in ("THESSALY",)
    return False


def _has_station_keyword(
    origin: str,
    destination: str,
    next_station: str,
    keywords: Tuple[str, ...],
) -> bool:
    """Return True if any station keyword appears in origin, destination, or next_station."""
    return any(
        st in origin or st in destination or st in next_station
        for st in keywords
    )


def _train_matches_corridor(p: TrainPosition, target: str) -> bool:
    """Check if train position belongs to target corridor filter.

    Supports:
    - Direct exact and substring matches on p.corridor
    - Hellenic Railways corridor aliases (e.g. PIRAIR, KIATO for PROASTIAKOS; E85 for PATHE)
    - Service and train type classifications (e.g. Suburban for PROASTIAKOS, Intercity for PATHE)
    - Route / terminal keyword matching
    - Geographic bounding box spatial fallback for real live telemetry with missing corridor tags
    """
    target = target.strip().upper()
    corridor = (p.corridor or "").strip().upper()

    # 1. When raw corridor string is present, use explicit corridor tag routing
    if corridor:
        return _matches_explicit_corridor_tag(corridor, target)

    # 2. When raw corridor is absent (real-world SSE telemetry), infer from metadata & geography
    service_type = (p.service_type or "").strip().upper()
    train_type = (p.train_type or "").strip().upper()
    origin = (p.origin or "").strip().upper()
    destination = (p.destination or "").strip().upper()
    next_station = (p.next_station or "").strip().upper()

    has_coords = (abs(p.lat) > 1e-9 or abs(p.lng) > 1e-9)
    pt = GeoPoint(lat=p.lat, lng=p.lng) if has_coords else None

    combined_type = f"{service_type} {train_type}"
    is_suburban = (
        "SUBURBAN" in combined_type
        or "PROAST" in combined_type
        or _has_station_keyword(origin, destination, next_station, _PROAST_STATIONS)
    )
    is_intercity = "INTERCITY" in combined_type or "IC" in service_type or "IC" in train_type

    if target in ("PROASTIAKOS", "PROASTIAKOS_ATH", "SUBURBAN"):
        return is_suburban or (not is_intercity and pt is not None and PROASTIAKOS_PRESET.contains(pt))

    if target in ("PATHE", "PATHE_MAINLINE", "MAINLINE"):
        return is_intercity or (not is_suburban and pt is not None and PATHE_PRESET.contains(pt))

    if target in ("NORTHERN", "NORTHERN_GREECE", "NORTH") and not is_suburban:
        return _has_station_keyword(origin, destination, next_station, _NORTHERN_STATIONS) or (
            pt is not None and NORTHERN_GREECE_PRESET.contains(pt)
        )

    if target in ("THESSALY",) and not is_suburban:
        return _has_station_keyword(origin, destination, next_station, _THESSALY_STATIONS)

    return False


def _natural_sort_key(rec: TrainRecord) -> Tuple[int, str, str, str]:
    """Helper to sort trains by number naturally (e.g. 50 before 1597), breaking ties

    deterministically via locomotive number and transponder ID.
    """
    tn = (rec.position.train_number or "").strip()
    loco = (rec.position.locomotive_number or "").strip()
    tid = str(rec.position.id or "").strip()
    digits = "".join(ch for ch in tn if ch.isdigit())
    if digits:
        return (0, f"{int(digits):010d}_{tn}", loco, tid)
    return (1, tn or tid, loco, tid)


def _speed_sort_key(rec: TrainRecord) -> Tuple[float, Tuple[int, str, str, str]]:
    """Sort trains by descending speed, with natural train number as tie-breaker."""
    spd = float(rec.position.speed) if rec.position.speed is not None else 0.0
    return (-spd, _natural_sort_key(rec))


def _delay_sort_key(rec: TrainRecord) -> Tuple[float, Tuple[int, str, str, str]]:
    """Sort trains by descending delay, with natural train number as tie-breaker."""
    dly = float(rec.position.delay) if rec.position.delay is not None else 0.0
    return (-dly, _natural_sort_key(rec))


def _ghost_matches_corridor(ghost: GhostTrain, target: str) -> bool:
    """Check if a coordinate-less ghost run matches a corridor filter."""
    norm_target = target.strip().upper()
    corridor = (ghost.corridor or "").strip().upper()
    if corridor:
        return _matches_explicit_corridor_tag(corridor, norm_target)

    service_type = (ghost.service_type or "").strip().upper()
    category = (ghost.category or "").strip().upper()
    origin = (ghost.origin or "").strip().upper()
    destination = (ghost.destination or "").strip().upper()

    combined_type = f"{service_type} {category}"
    is_suburban = (
        "SUBURBAN" in combined_type
        or "PROAST" in combined_type
        or "ΠΡΟΑΣΤ" in combined_type
        or _has_station_keyword(origin, destination, "", _PROAST_STATIONS)
    )
    is_intercity = "INTERCITY" in combined_type or "IC" in service_type

    if norm_target in ("PROASTIAKOS", "PROASTIAKOS_ATH", "SUBURBAN"):
        return is_suburban
    if norm_target in ("PATHE", "PATHE_MAINLINE", "MAINLINE"):
        return is_intercity
    if norm_target in ("NORTHERN", "NORTHERN_GREECE", "NORTH") and not is_suburban:
        return _has_station_keyword(origin, destination, "", _NORTHERN_STATIONS)
    if norm_target in ("THESSALY",) and not is_suburban:
        return _has_station_keyword(origin, destination, "", _THESSALY_STATIONS)
    return False


def _ghost_natural_sort_key(rec: GhostTrainRecord) -> Tuple[int, str, str]:
    """Natural alphanumeric sort key for ghost trains."""
    tn = (rec.ghost.train_number or "").strip()
    gid = str(rec.ghost.id or "").strip()
    digits = "".join(ch for ch in tn if ch.isdigit())
    if digits:
        return (0, f"{int(digits):010d}_{tn}", gid)
    return (1, tn or gid, gid)


def _ghost_delay_sort_key(rec: GhostTrainRecord) -> Tuple[float, Tuple[int, str, str]]:
    """Sort ghost trains by descending delay with natural train number tie-breaker."""
    return (-float(rec.ghost.delay), _ghost_natural_sort_key(rec))


def _dedup_merge_breadcrumbs(
    existing: Sequence[HistoricalBreadcrumb],
    incoming: Sequence[HistoricalBreadcrumb],
    max_len: int,
    threshold_deg: float = 1e-5,
) -> Deque[HistoricalBreadcrumb]:
    """Chronologically merge and deduplicate breadcrumbs within a bounded deque."""
    combined = sorted(
        list(existing) + list(incoming),
        key=lambda crumb: crumb.timestamp,
    )
    deduped: List[HistoricalBreadcrumb] = []
    for crumb in combined:
        if deduped:
            prev = deduped[-1]
            same_pos = (
                abs(prev.lat - crumb.lat) <= threshold_deg
                and abs(prev.lng - crumb.lng) <= threshold_deg
            )
            if same_pos and abs(prev.timestamp - crumb.timestamp) < 0.5:
                continue
        deduped.append(crumb)
    if len(deduped) > max_len:
        deduped = deduped[-max_len:]
    return deque(deduped, maxlen=max_len)



def _tally_snapshot_train_metrics(
    trains: Dict[str, TrainRecord],
) -> Tuple[List[str], List[str], List[str], int]:
    """Tally overspeed, frozen, dropped train IDs and stale count in a single pass."""
    overspeed_ids: List[str] = []
    frozen_ids: List[str] = []
    dropped_ids: List[str] = []
    stale_cnt: int = 0

    for tid, rec in trains.items():
        if rec.speed_result and rec.speed_result.severity in (
            OverspeedSeverity.OVERSPEED,
            OverspeedSeverity.TSR_VIOLATION,
        ):
            overspeed_ids.append(tid)

        sh = rec.sensor_health
        if sh:
            if sh.state == SensorHealthState.FROZEN:
                frozen_ids.append(tid)
            elif sh.state == SensorHealthState.DROPPED:
                dropped_ids.append(tid)
            elif sh.state == SensorHealthState.STALE:
                stale_cnt += 1

    return overspeed_ids, frozen_ids, dropped_ids, stale_cnt


def _tally_conflicts_by_severity(
    conflicts: Dict[str, ConflictAlert],
) -> Tuple[int, int, int]:
    """Count critical, warning, and advisory conflicts in a single pass."""
    critical, warning, advisory = 0, 0, 0
    for a in conflicts.values():
        if a.severity == ConflictSeverity.CRITICAL:
            critical += 1
        elif a.severity == ConflictSeverity.WARNING:
            warning += 1
        elif a.severity == ConflictSeverity.ADVISORY:
            advisory += 1
    return critical, warning, advisory


class TrainStateStore:
    """Thread-safe state repository for live train positions and UI view state."""

    def __init__(
        self,
        stale_timeout_sec: float = DEFAULT_LIVE_STALE_TIMEOUT_SEC,
        max_trail_len: int = DEFAULT_MAX_TRAIL_LEN,
        initial_theme: Optional[ThemeProfile] = None,
        ghost_stale_timeout_sec: float = DEFAULT_GHOST_STALE_TIMEOUT_SEC,
    ) -> None:
        self._lock = threading.RLock()
        self._stale_timeout_sec = stale_timeout_sec
        self._max_trail_len = max_trail_len
        self._ghost_stale_timeout_sec = max(10.0, float(ghost_stale_timeout_sec))

        # Telemetry data
        self._trains: Dict[str, TrainRecord] = {}
        self._ghosts: Dict[str, GhostTrainRecord] = {}
        self._pending_history: "OrderedDict[str, Tuple[float, Tuple[HistoricalBreadcrumb, ...]]]" = (
            OrderedDict()
        )
        self._total_messages: int = 0
        self._last_update_ts: float = 0.0
        self._last_prune_ts: float = 0.0
        self._total_trains_pruned: int = 0
        self._history_hydrated_trains: int = 0
        self._delay_updates_applied: int = 0
        self._stream_event_counts: Dict[str, int] = {}
        self._version: int = 0

        # UI & View state
        self._selected_id: Optional[str] = None
        self._follow_mode: bool = False
        self._filter_query: str = ""
        self._corridor_filter: Optional[str] = None
        self._sort_mode: RosterSortMode = RosterSortMode.TRAIN_ID
        self._breadcrumb_mode: BreadcrumbMode = BreadcrumbMode.ALL
        self._ghost_visibility_mode: GhostVisibilityMode = GhostVisibilityMode.SHOW
        self._inspector_mode: InspectorMode = InspectorMode.TRAIN
        self._selected_station_id: Optional[str] = None
        self._station_cycle_anchor_id: Optional[str] = None
        self._station_label_mode: StationLabelMode = StationLabelMode.HUBS_AND_SECONDARY
        self._track_color_mode: TrackColorMode = TrackColorMode.UNIFORM
        self._infrastructure_mode: InfrastructureLayerMode = InfrastructureLayerMode.ALL
        self._theme_profile: ThemeProfile = initial_theme if initial_theme is not None else ThemeProfile.AZURE
        self._connection_status: ConnectionStatus = ConnectionStatus.DISCONNECTED
        self._status_message: str = "Ready"
        self._is_recording: bool = False
        self._recording_db: Optional[str] = None
        self._is_replay: bool = False
        self._replay_db: Optional[str] = None
        self._replay_virtual_time: Optional[float] = None
        self._replay_start_time: Optional[float] = None
        self._replay_end_time: Optional[float] = None
        self._replay_speed: float = 1.0
        self._watched_train_ids: Set[str] = set()

        # Speed limits and slow orders (F-5.3)
        self._speed_rules: Dict[str, CorridorSpeedRule] = dict(DEFAULT_CORRIDOR_SPEED_RULES)
        self._active_tsrs: List[SlowOrder] = []
        try:
            from tui.mock_engine import load_speed_limits
            loaded_rules, loaded_tsrs = load_speed_limits()
            if loaded_rules:
                self._speed_rules = loaded_rules
            self._active_tsrs = loaded_tsrs
        except (OSError, ValueError, KeyError, TypeError):
            self._speed_rules = dict(DEFAULT_CORRIDOR_SPEED_RULES)
            self._active_tsrs = []

        # Station catalog for F-5.2 Sensor Health
        self._stations: List[Any] = []
        try:
            from tui.mock_engine import load_stations

            self._stations = list(load_stations().values())
        except (OSError, ValueError, KeyError, TypeError):
            self._stations = []

        # Tunnel infrastructure and drop threshold for P0-10 Resilient Drop Watchdog
        self._telemetry_drop_threshold_sec: float = DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC
        self._tunnels: List[InfrastructureFeature] = []
        try:
            from tui.mock_engine import load_infrastructure

            infra = load_infrastructure()
            self._tunnels = [
                f
                for f in infra.values()
                if getattr(f, "feature_type", None) == InfrastructureType.TUNNEL
            ]
        except (OSError, ValueError, KeyError, TypeError):
            self._tunnels = []

        # Sensor freeze configuration for P0-11 Resilient Freeze Watchdog
        self._sensor_freeze_threshold_sec: float = DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC
        self._sensor_freeze_min_speed_kmh: float = DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH
        self._sensor_freeze_max_packet_age_sec: float = (
            DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC
        )

        # Corridor headway & opposing conflict watchdog (F-5.1)
        self._conflict_config: ConflictWatchdogConfig = ConflictWatchdogConfig()
        self._tracks_data: Optional[Dict[str, Any]] = None
        try:
            from tui.mock_engine import load_tracks

            self._tracks_data = load_tracks()
        except (OSError, ValueError, KeyError, TypeError):
            self._tracks_data = None
        self._active_conflicts: Dict[str, ConflictAlert] = {}
        self._previous_conflict_keys: Set[str] = set()
        self._new_conflict_bell_pending: bool = False

    # ------------------------------------------------------------------
    # Telemetry Ingestion (Called by StreamWorker / ReplayWorker Thread)
    # ------------------------------------------------------------------

    def _upsert_train_position(self, pos: TrainPosition, ts: float) -> None:
        """Insert or update a single TrainPosition at timestamp ts (caller must hold self._lock)."""
        if not pos.id:
            return
        pt = GeoPoint(lat=pos.lat, lng=pos.lng)
        speed_res = evaluate_train_speed(
            point=pt,
            current_speed=pos.speed,
            corridor_id=pos.corridor,
            rules=self._speed_rules,
            active_tsrs=self._active_tsrs,
            train_id=pos.train_number or pos.id,
        )
        if pos.id in self._trains:
            rec = self._trains[pos.id]
            rec.append_breadcrumb(pos, now=ts)
            rec.speed_result = speed_res
            health, new_moved_pt, new_moved_time, new_dwell_start = evaluate_sensor_health(
                point=pt,
                current_speed=pos.speed,
                last_updated=ts,
                last_moved_pt=rec.last_moved_pt,
                last_moved_time=rec.last_moved_time,
                station_dwell_start=rec.station_dwell_start,
                stations=self._stations,
                tunnels=self._tunnels,
                now=ts,
                telemetry_drop_threshold_sec=self._telemetry_drop_threshold_sec,
                sensor_freeze_threshold_sec=self._sensor_freeze_threshold_sec,
                sensor_freeze_min_speed_kmh=self._sensor_freeze_min_speed_kmh,
                sensor_freeze_max_packet_age_sec=self._sensor_freeze_max_packet_age_sec,
            )
            rec.last_moved_pt = new_moved_pt
            rec.last_moved_time = new_moved_time
            rec.station_dwell_start = new_dwell_start
            rec.sensor_health = health
        else:
            health, new_moved_pt, new_moved_time, new_dwell_start = evaluate_sensor_health(
                point=pt,
                current_speed=pos.speed,
                last_updated=ts,
                last_moved_pt=None,
                last_moved_time=ts,
                station_dwell_start=None,
                stations=self._stations,
                tunnels=self._tunnels,
                now=ts,
                telemetry_drop_threshold_sec=self._telemetry_drop_threshold_sec,
                sensor_freeze_threshold_sec=self._sensor_freeze_threshold_sec,
                sensor_freeze_min_speed_kmh=self._sensor_freeze_min_speed_kmh,
                sensor_freeze_max_packet_age_sec=self._sensor_freeze_max_packet_age_sec,
            )
            record = TrainRecord(
                position=pos,
                trail=deque(maxlen=self._max_trail_len),
                first_seen=ts,
                last_updated=ts,
                update_count=1,
                speed_result=speed_res,
                last_moved_pt=new_moved_pt,
                last_moved_time=new_moved_time,
                station_dwell_start=new_dwell_start,
                sensor_health=health,
            )
            staged = self._pending_history.pop(pos.id, None)
            live_crumb = HistoricalBreadcrumb(
                lat=pos.lat,
                lng=pos.lng,
                timestamp=ts,
                speed=float(pos.speed) if pos.speed is not None else 0.0,
            )
            if staged is not None and (ts - staged[0]) <= PENDING_HISTORY_TTL_SEC:
                record.trail = _dedup_merge_breadcrumbs(
                    existing=staged[1],
                    incoming=(live_crumb,),
                    max_len=self._max_trail_len,
                )
            else:
                record.trail.append(live_crumb)
            self._trains[pos.id] = record
        self._maybe_promote_selected_ghost(pos)

    def _active_live_identifiers(self) -> Tuple[FrozenSet[str], FrozenSet[str]]:
        """Return (live_schedule_ids, live_train_numbers_upper) across active GPS trains."""
        sched_ids: Set[str] = set()
        train_nums: Set[str] = set()
        for rec in self._trains.values():
            sid = (rec.position.schedule_id or "").strip()
            if sid:
                sched_ids.add(sid)
            tn = (rec.position.train_number or "").strip().upper()
            if tn:
                train_nums.add(tn)
        return frozenset(sched_ids), frozenset(train_nums)

    @staticmethod
    def _is_ghost_superseded(
        ghost: GhostTrain,
        live_sched_ids: FrozenSet[str],
        live_train_nums: FrozenSet[str],
    ) -> bool:
        """Return True if a live GPS train already covers this ghost run."""
        sid = (ghost.schedule_id or "").strip()
        if sid and sid in live_sched_ids:
            return True
        tn = (ghost.train_number or "").strip().upper()
        if tn and tn in live_train_nums:
            return True
        return False

    def _maybe_promote_selected_ghost(self, pos: TrainPosition) -> None:
        """Promote active selection from a ghost run to a matching live GPS train."""
        if not self._selected_id or self._selected_id not in self._ghosts:
            return
        sel_ghost = self._ghosts[self._selected_id].ghost
        pos_sid = (pos.schedule_id or "").strip()
        ghost_sid = (sel_ghost.schedule_id or "").strip()
        same_sched = bool(pos_sid and ghost_sid and pos_sid == ghost_sid)
        same_num = bool(
            pos.train_number
            and sel_ghost.train_number
            and pos.train_number.strip().upper() == sel_ghost.train_number.strip().upper()
        )
        if same_sched or same_num:
            self._selected_id = pos.id

    def _visible_non_superseded_ghosts(self) -> Dict[str, GhostTrainRecord]:
        """Return cloned map of active ghost runs not superseded by live GPS trains."""
        live_sids, live_nums = self._active_live_identifiers()
        return {
            gid: grec.clone()
            for gid, grec in self._ghosts.items()
            if not self._is_ghost_superseded(grec.ghost, live_sids, live_nums)
        }

    def record_stream_event(self, event_name: str) -> None:
        """Increment cumulative counter for a multiplexed SSE event channel (F-10.1)."""
        clean_name = str(event_name or "").strip()
        if not clean_name:
            return
        with self._lock:
            self._stream_event_counts[clean_name] = (
                self._stream_event_counts.get(clean_name, 0) + 1
            )

    def hydrate_train_history(
        self,
        history: Sequence[Tuple[str, Sequence[TrainHistoryPoint]]],
        now: Optional[float] = None,
    ) -> int:
        """Hydrate live train trails or stage pending history for unseen trains (F-10.1)."""
        with self._lock:
            ref_now = float(now) if now is not None else time.time()
            hydrated_count = 0

            for train_id, points in history:
                tid = str(train_id or "").strip()
                if not tid or not points:
                    continue
                incoming = tuple(
                    HistoricalBreadcrumb(
                        lat=pt.lat,
                        lng=pt.lng,
                        timestamp=pt.timestamp_epoch,
                        speed=max(0.0, float(pt.speed)),
                    )
                    for pt in points
                )
                if tid in self._trains:
                    rec = self._trains[tid]
                    rec.trail = _dedup_merge_breadcrumbs(
                        existing=list(rec.trail),
                        incoming=incoming,
                        max_len=self._max_trail_len,
                    )
                    hydrated_count += 1
                else:
                    prev = self._pending_history.get(tid)
                    prior_crumbs = prev[1] if prev is not None else ()
                    merged = _dedup_merge_breadcrumbs(
                        existing=prior_crumbs,
                        incoming=incoming,
                        max_len=self._max_trail_len,
                    )
                    self._pending_history[tid] = (ref_now, tuple(merged))
                    self._pending_history.move_to_end(tid)
                    while len(self._pending_history) > MAX_PENDING_HISTORY_TRAINS:
                        self._pending_history.popitem(last=False)
                    hydrated_count += 1

            if hydrated_count > 0:
                self._history_hydrated_trains += hydrated_count
                self._total_messages += 1
                self._last_update_ts = max(self._last_update_ts, ref_now)
                self._version += 1
            return hydrated_count

    def update_ghost_trains(
        self,
        ghosts: Sequence[GhostTrain],
        now: Optional[float] = None,
    ) -> int:
        """Replace active scheduleCards ghost snapshot while preserving metadata (F-10.1)."""
        with self._lock:
            ref_now = float(now) if now is not None else time.time()
            old_selected_ghost = (
                self._ghosts.get(self._selected_id)
                if self._selected_id is not None
                else None
            )

            new_ghosts: Dict[str, GhostTrainRecord] = {}
            for ghost in ghosts:
                gid = ghost.id.strip()
                if not gid:
                    continue
                prev = self._ghosts.get(gid)
                if prev is not None:
                    new_ghosts[gid] = GhostTrainRecord(
                        ghost=ghost,
                        first_seen=prev.first_seen,
                        last_updated=ref_now,
                        update_count=prev.update_count + 1,
                    )
                else:
                    new_ghosts[gid] = GhostTrainRecord(
                        ghost=ghost,
                        first_seen=ref_now,
                        last_updated=ref_now,
                        update_count=1,
                    )

            self._ghosts = new_ghosts

            if old_selected_ghost is not None and self._selected_id not in self._ghosts:
                promoted_id = self._find_live_match_for_ghost(old_selected_ghost.ghost)
                self._selected_id = promoted_id
                if promoted_id is None:
                    self._follow_mode = False

            self._total_messages += 1
            self._last_update_ts = max(self._last_update_ts, ref_now)
            self._version += 1
            return len(new_ghosts)

    def _find_live_match_for_ghost(self, ghost: GhostTrain) -> Optional[str]:
        """Locate an active live GPS train matching a vanished ghost run."""
        g_sid = (ghost.schedule_id or "").strip()
        g_num = (ghost.train_number or "").strip().upper()
        for tid, rec in self._trains.items():
            p_sid = (rec.position.schedule_id or "").strip()
            if g_sid and p_sid and g_sid == p_sid:
                return tid
            p_num = (rec.position.train_number or "").strip().upper()
            if g_num and p_num and g_num == p_num:
                return tid
        return None

    @staticmethod
    def _delay_update_matches_live(upd: DelayUpdate, pos: TrainPosition) -> bool:
        """Determine whether a DelayUpdate applies to a live TrainPosition."""
        if upd.train_id and pos.id == upd.train_id:
            return True
        if upd.schedule_id and pos.schedule_id and pos.schedule_id == upd.schedule_id:
            return True
        if upd.train_number and pos.train_number:
            return pos.train_number.strip().upper() == upd.train_number.strip().upper()
        return False

    @staticmethod
    def _delay_update_matches_ghost(upd: DelayUpdate, ghost: GhostTrain) -> bool:
        """Determine whether a DelayUpdate applies to a GhostTrain."""
        if upd.schedule_id and ghost.schedule_id and ghost.schedule_id == upd.schedule_id:
            return True
        if upd.train_number and ghost.train_number:
            return ghost.train_number.strip().upper() == upd.train_number.strip().upper()
        return False

    def apply_delay_updates(
        self,
        updates: Sequence[DelayUpdate],
        now: Optional[float] = None,
    ) -> int:
        """Patch delays in-place across all matching live consists and ghosts (F-10.1).

        Preserves TrainRecord.last_updated so incremental delay deltas never mask GPS dropouts.
        """
        with self._lock:
            ref_now = float(now) if now is not None else time.time()
            modified = 0

            for upd in updates:
                for rec in self._trains.values():
                    if self._delay_update_matches_live(upd, rec.position):
                        if rec.position.delay != upd.delay:
                            rec.position = replace(rec.position, delay=upd.delay)
                            modified += 1
                for grec in self._ghosts.values():
                    if self._delay_update_matches_ghost(upd, grec.ghost):
                        if grec.ghost.delay != upd.delay:
                            grec.ghost = replace(grec.ghost, delay=upd.delay)
                            modified += 1

            self._total_messages += 1
            self._last_update_ts = max(self._last_update_ts, ref_now)
            if modified > 0:
                self._delay_updates_applied += modified
                self._version += 1
            return modified

    def _recompute_conflicts(self, now: float) -> None:
        """Evaluate and cache pairwise conflicts across all active trains (caller holds self._lock)."""
        active_positions = [rec.position for rec in self._trains.values()]
        alerts = evaluate_train_conflicts(
            active_trains=active_positions,
            tracks_data=self._tracks_data,
            stations=self._stations,
            config=self._conflict_config,
            now=now,
        )
        for rec in self._trains.values():
            rec.active_conflict = None

        new_conflicts_by_id: Dict[str, ConflictAlert] = {}
        for alert in alerts:
            new_conflicts_by_id[alert.alert_id] = alert
            if alert.train_id_1 in self._trains:
                if (
                    self._trains[alert.train_id_1].active_conflict is None
                    or alert.severity == ConflictSeverity.CRITICAL
                ):
                    self._trains[alert.train_id_1].active_conflict = alert
            if alert.train_id_2 in self._trains:
                if (
                    self._trains[alert.train_id_2].active_conflict is None
                    or alert.severity == ConflictSeverity.CRITICAL
                ):
                    self._trains[alert.train_id_2].active_conflict = alert

        # Check for newly elevated conflicts (edge-triggered bell latch)
        for aid, alert in new_conflicts_by_id.items():
            if aid not in self._previous_conflict_keys and alert.severity in (
                ConflictSeverity.CRITICAL,
                ConflictSeverity.WARNING,
            ):
                self._new_conflict_bell_pending = True

        self._previous_conflict_keys = set(new_conflicts_by_id.keys())
        self._active_conflicts = new_conflicts_by_id

    def consume_conflict_bell(self) -> bool:
        """Atomically query and reset the edge-triggered conflict bell latch."""
        with self._lock:
            pending = self._new_conflict_bell_pending
            self._new_conflict_bell_pending = False
            return pending

    def get_conflict_config(self) -> ConflictWatchdogConfig:
        """Return the current operational conflict watchdog parameters."""
        with self._lock:
            return self._conflict_config

    def set_conflict_config(self, config: ConflictWatchdogConfig) -> None:
        """Update operational parameters and thresholds for the conflict watchdog."""
        with self._lock:
            self._conflict_config = config
            self._version += 1

    def set_tracks_data(self, tracks: Dict[str, Any]) -> None:
        """Explicitly provide or update track corridors for conflict spatial analysis."""
        with self._lock:
            self._tracks_data = tracks
            self._version += 1

    def update_positions(
        self,
        positions: List[TrainPosition],
        virtual_now: Optional[float] = None,
    ) -> None:
        """
        Ingest a batch of train positions with mutex protection.
        Updates existing records, creates new records, appends breadcrumbs,
        and increments the store version counter.
        """
        with self._lock:
            now = float(virtual_now) if virtual_now is not None else time.time()
            for pos in positions:
                self._upsert_train_position(pos, now)

            self._recompute_conflicts(now)

            if virtual_now is not None:
                self._replay_virtual_time = now
            self._total_messages += 1
            self._last_update_ts = now
            self._version += 1

    def ingest_replay_observations(
        self,
        observations: Sequence[Tuple[TrainPosition, float]],
        virtual_now: float,
    ) -> None:
        """
        Ingest a chronological batch of (TrainPosition, recorded_epoch) observations
        during historical DVR playback and advance replay_virtual_time.
        """
        with self._lock:
            for pos, obs_epoch in observations:
                self._upsert_train_position(pos, float(obs_epoch))
            self._recompute_conflicts(float(virtual_now))
            self._replay_virtual_time = float(virtual_now)
            if observations:
                self._total_messages += 1
                self._last_update_ts = float(virtual_now)
            self._version += 1

    def replace_replay_state(
        self,
        observations: Sequence[Tuple[TrainPosition, float]],
        virtual_now: float,
    ) -> None:
        """
        Atomically rebuild the active train roster and breadcrumb trails from a point-in-time
        lookback window (used when initializing DVR replay or scrubbing with '[' / ']').
        Preserves active UI filters and preserves train selection if the train remains active.
        """
        with self._lock:
            self._trains.clear()
            for pos, obs_epoch in observations:
                self._upsert_train_position(pos, float(obs_epoch))

            self._recompute_conflicts(float(virtual_now))

            if self._selected_id and self._selected_id not in self._trains:
                self._selected_id = None
                self._follow_mode = False

            self._replay_virtual_time = float(virtual_now)
            self._last_update_ts = float(virtual_now)
            self._total_messages += 1
            self._version += 1

    def get_stale_timeout(self) -> float:
        """Return the current stale train eviction threshold in seconds."""
        with self._lock:
            return self._stale_timeout_sec

    def set_stale_timeout(self, timeout_sec: float) -> None:
        """Update the stale train eviction threshold in seconds."""
        with self._lock:
            self._stale_timeout_sec = max(1.0, float(timeout_sec))
            self._version += 1

    def prune_stale_trains(
        self,
        max_age_sec: Optional[float] = None,
        now: Optional[float] = None,
    ) -> int:
        """
        Remove trains that have not received telemetry updates within max_age_sec.
        Atomically clears associated conflicts, resets orphaned selections,
        and returns the number of pruned trains.
        """
        cutoff = max_age_sec if max_age_sec is not None else self._stale_timeout_sec
        with self._lock:
            if now is not None:
                ref_now = float(now)
            elif self._is_replay and self._replay_virtual_time is not None:
                ref_now = self._replay_virtual_time
            else:
                ref_now = time.time()
            stale_ids = [
                tid for tid, rec in self._trains.items()
                if (ref_now - rec.last_updated) > cutoff
            ]
            # Evict expired pending trainHistory staging entries and stale ghosts (F-10.1)
            expired_pending = [
                tid
                for tid, (staged_at, _) in self._pending_history.items()
                if (ref_now - staged_at) > PENDING_HISTORY_TTL_SEC
            ]
            for tid in expired_pending:
                del self._pending_history[tid]

            stale_ghost_ids = [
                gid
                for gid, grec in self._ghosts.items()
                if (ref_now - grec.last_updated) > self._ghost_stale_timeout_sec
            ]
            for gid in stale_ghost_ids:
                del self._ghosts[gid]
                if self._selected_id == gid:
                    self._selected_id = None
                    self._follow_mode = False

            if not stale_ids:
                if stale_ghost_ids:
                    self._version += 1
                self._last_prune_ts = ref_now
                return 0

            stale_set = set(stale_ids)
            for tid in stale_ids:
                del self._trains[tid]
                if self._selected_id == tid:
                    self._selected_id = None
                    self._follow_mode = False

            # Clean up watched IDs for pruned trains
            self._watched_train_ids.difference_update(stale_set)

            # Immediately purge active conflicts referencing any pruned train
            orphaned_conflict_keys = [
                cid for cid, c in self._active_conflicts.items()
                if c.train_id_1 in stale_set or c.train_id_2 in stale_set
            ]
            for cid in orphaned_conflict_keys:
                del self._active_conflicts[cid]
                self._previous_conflict_keys.discard(cid)

            # Re-evaluate conflicts across remaining active trains
            self._recompute_conflicts(ref_now)

            self._version += 1
            self._total_trains_pruned += len(stale_ids)
            self._last_prune_ts = ref_now
            return len(stale_ids)

    def set_connection_status(self, status: ConnectionStatus, message: str = "") -> None:
        """Update stream connectivity status and descriptive status message."""
        with self._lock:
            self._connection_status = status
            if message:
                self._status_message = message
            self._version += 1

    def set_recording_status(self, is_recording: bool, db_path: Optional[str] = None) -> None:
        """Update background telemetry persistence status and database path."""
        with self._lock:
            self._is_recording = is_recording
            self._recording_db = db_path
            self._version += 1

    def set_replay_status(
        self,
        is_replay: bool,
        db_path: Optional[str] = None,
        virtual_time: Optional[float] = None,
        start_time: Optional[float] = None,
        end_time: Optional[float] = None,
        speed: Optional[float] = None,
    ) -> None:
        """Update SQLite historical DVR replay metadata and virtual clock state."""
        with self._lock:
            was_replay = self._is_replay
            self._is_replay = bool(is_replay)
            if self._is_replay and not was_replay:
                if self._stale_timeout_sec == DEFAULT_LIVE_STALE_TIMEOUT_SEC:
                    self._stale_timeout_sec = DEFAULT_REPLAY_STALE_TIMEOUT_SEC
            elif not self._is_replay and was_replay:
                if self._stale_timeout_sec == DEFAULT_REPLAY_STALE_TIMEOUT_SEC:
                    self._stale_timeout_sec = DEFAULT_LIVE_STALE_TIMEOUT_SEC
            if db_path is not None or not is_replay:
                self._replay_db = db_path
            if virtual_time is not None or not is_replay:
                self._replay_virtual_time = float(virtual_time) if virtual_time is not None else None
            if start_time is not None or not is_replay:
                self._replay_start_time = float(start_time) if start_time is not None else None
            if end_time is not None or not is_replay:
                self._replay_end_time = float(end_time) if end_time is not None else None
            if speed is not None:
                self._replay_speed = max(0.1, float(speed))
            elif not is_replay:
                self._replay_speed = 1.0
            self._version += 1

    def set_speed_rules(
        self,
        rules: Dict[str, CorridorSpeedRule],
        tsrs: Sequence[SlowOrder],
    ) -> None:
        """Update active corridor speed limit rules and slow orders, re-evaluating active trains."""
        with self._lock:
            self._speed_rules = dict(rules)
            self._active_tsrs = list(tsrs)
            for rec in self._trains.values():
                pt = GeoPoint(lat=rec.position.lat, lng=rec.position.lng)
                rec.speed_result = evaluate_train_speed(
                    point=pt,
                    current_speed=rec.position.speed,
                    corridor_id=rec.position.corridor,
                    rules=self._speed_rules,
                    active_tsrs=self._active_tsrs,
                    train_id=rec.position.train_number or rec.position.id,
                )
            self._version += 1

    def set_stations(self, stations: Sequence[Any]) -> None:
        """Update reference railway station list for station dwell detection."""
        with self._lock:
            self._stations = list(stations)
            self._version += 1

    def set_tunnels(self, tunnels: Sequence[InfrastructureFeature]) -> None:
        """Set candidate railway tunnel GIS features for sensor health deadband calculation."""
        with self._lock:
            self._tunnels = list(tunnels)
            self._version += 1

    def get_tunnels(self) -> Sequence[InfrastructureFeature]:
        """Get candidate railway tunnel GIS features."""
        with self._lock:
            return list(self._tunnels)

    def set_telemetry_drop_threshold(self, threshold_sec: float) -> None:
        """Set silence duration threshold for complete telemetry drop classification."""
        with self._lock:
            self._telemetry_drop_threshold_sec = max(10.0, float(threshold_sec))
            self._version += 1

    def get_telemetry_drop_threshold(self) -> float:
        """Get silence duration threshold for complete telemetry drop classification."""
        with self._lock:
            return self._telemetry_drop_threshold_sec

    def set_sensor_freeze_threshold(self, threshold_sec: float) -> None:
        """Set static duration threshold for frozen sensor classification."""
        with self._lock:
            self._sensor_freeze_threshold_sec = max(5.0, float(threshold_sec))
            self._version += 1

    def get_sensor_freeze_threshold(self) -> float:
        """Get static duration threshold for frozen sensor classification."""
        with self._lock:
            return self._sensor_freeze_threshold_sec

    def set_sensor_freeze_min_speed(self, speed_kmh: float) -> None:
        """Set minimum speed required for sensor freeze qualification."""
        with self._lock:
            self._sensor_freeze_min_speed_kmh = max(1.0, float(speed_kmh))
            self._version += 1

    def get_sensor_freeze_min_speed(self) -> float:
        """Get minimum speed required for sensor freeze qualification."""
        with self._lock:
            return self._sensor_freeze_min_speed_kmh

    def set_sensor_freeze_max_packet_age(self, age_sec: float) -> None:
        """Set maximum packet age for sensor freeze qualification."""
        with self._lock:
            self._sensor_freeze_max_packet_age_sec = max(1.0, float(age_sec))
            self._version += 1

    def get_sensor_freeze_max_packet_age(self) -> float:
        """Get maximum packet age for sensor freeze qualification."""
        with self._lock:
            return self._sensor_freeze_max_packet_age_sec

    def evaluate_all_sensor_health(self, now: Optional[float] = None) -> None:
        """Run temporal and kinematic health evaluation across all tracked trains."""
        with self._lock:
            ref_now = now if now is not None else time.time()
            changed = False
            for rec in self._trains.values():
                pt = GeoPoint(lat=rec.position.lat, lng=rec.position.lng)
                prev_state = rec.sensor_health.state if rec.sensor_health else None
                health, new_moved_pt, new_moved_time, new_dwell_start = evaluate_sensor_health(
                    point=pt,
                    current_speed=rec.position.speed,
                    last_updated=rec.last_updated,
                    last_moved_pt=rec.last_moved_pt,
                    last_moved_time=rec.last_moved_time,
                    station_dwell_start=rec.station_dwell_start,
                    stations=self._stations,
                    tunnels=self._tunnels,
                    now=ref_now,
                    telemetry_drop_threshold_sec=self._telemetry_drop_threshold_sec,
                    sensor_freeze_threshold_sec=self._sensor_freeze_threshold_sec,
                    sensor_freeze_min_speed_kmh=self._sensor_freeze_min_speed_kmh,
                    sensor_freeze_max_packet_age_sec=self._sensor_freeze_max_packet_age_sec,
                )
                rec.last_moved_pt = new_moved_pt
                rec.last_moved_time = new_moved_time
                rec.station_dwell_start = new_dwell_start
                rec.sensor_health = health
                if prev_state != health.state:
                    changed = True
            if changed:
                self._version += 1

    # ------------------------------------------------------------------
    # UI State & Cursor Navigation (Called by UI Thread)
    # ------------------------------------------------------------------

    def get_snapshot(self, now: Optional[float] = None) -> StateSnapshot:
        """
        Produce a frozen, consistent point-in-time snapshot for UI rendering.
        Operates with minimal lock hold duration.
        """
        with self._lock:
            if now is not None:
                ref_now = float(now)
            elif self._is_replay and self._replay_virtual_time is not None:
                ref_now = self._replay_virtual_time
            else:
                ref_now = time.time()
            for rec in self._trains.values():
                pt = GeoPoint(lat=rec.position.lat, lng=rec.position.lng)
                health, new_moved_pt, new_moved_time, new_dwell_start = evaluate_sensor_health(
                    point=pt,
                    current_speed=rec.position.speed,
                    last_updated=rec.last_updated,
                    last_moved_pt=rec.last_moved_pt,
                    last_moved_time=rec.last_moved_time,
                    station_dwell_start=rec.station_dwell_start,
                    stations=self._stations,
                    tunnels=self._tunnels,
                    now=ref_now,
                    telemetry_drop_threshold_sec=self._telemetry_drop_threshold_sec,
                    sensor_freeze_threshold_sec=self._sensor_freeze_threshold_sec,
                    sensor_freeze_min_speed_kmh=self._sensor_freeze_min_speed_kmh,
                    sensor_freeze_max_packet_age_sec=self._sensor_freeze_max_packet_age_sec,
                )
                rec.last_moved_pt = new_moved_pt
                rec.last_moved_time = new_moved_time
                rec.station_dwell_start = new_dwell_start
                rec.sensor_health = health

            self._recompute_conflicts(ref_now)

            trains_copy = {tid: rec.clone() for tid, rec in self._trains.items()}
            filtered = self._compute_filtered_list(trains_copy)

            ghost_trains_copy = self._visible_non_superseded_ghosts()
            filtered_ghosts = self._compute_filtered_ghosts(ghost_trains_copy)

            sel_idx = -1
            selected_record: Optional[TrainRecord] = None
            selected_ghost: Optional[GhostTrainRecord] = None
            if self._selected_id and self._selected_id in trains_copy:
                selected_record = trains_copy[self._selected_id]
                for idx, rec in enumerate(filtered):
                    if rec.position.id == self._selected_id:
                        sel_idx = idx
                        break
            elif self._selected_id and self._selected_id in ghost_trains_copy:
                selected_ghost = ghost_trains_copy[self._selected_id]
                for g_idx, grec in enumerate(filtered_ghosts):
                    if grec.ghost.id == self._selected_id:
                        sel_idx = len(filtered) + g_idx
                        break

            overspeed_ids, frozen_ids, dropped_ids, stale_cnt = _tally_snapshot_train_metrics(trains_copy)
            active_tsr_cnt = sum(1 for t in self._active_tsrs if t.active)

            dup_counts: Dict[str, int] = {}
            for rec in trains_copy.values():
                tn = (rec.position.train_number or "").strip()
                if tn:
                    dup_counts[tn] = dup_counts.get(tn, 0) + 1
            duplicate_train_numbers = frozenset(tn for tn, count in dup_counts.items() if count > 1)

            sel_speed_res = selected_record.speed_result if selected_record else None
            sel_sensor_health = selected_record.sensor_health if selected_record else None
            sel_conflict = selected_record.active_conflict if selected_record else None

            critical_conflicts, warning_conflicts, advisory_conflicts = _tally_conflicts_by_severity(
                self._active_conflicts
            )

            return StateSnapshot(
                trains=trains_copy,
                filtered_trains=tuple(filtered),
                selected_train=selected_record,
                selected_index=sel_idx,
                connection_status=self._connection_status,
                status_message=self._status_message,
                total_messages_received=self._total_messages,
                last_stream_update=self._last_update_ts,
                follow_mode=self._follow_mode,
                filter_query=self._filter_query,
                corridor_filter=self._corridor_filter,
                version=self._version,
                is_recording=self._is_recording,
                recording_db=self._recording_db,
                sort_mode=self._sort_mode,
                breadcrumb_mode=self._breadcrumb_mode,
                inspector_mode=self._inspector_mode,
                selected_station_id=self._selected_station_id,
                station_label_mode=self._station_label_mode,
                track_color_mode=self._track_color_mode,
                infrastructure_mode=self._infrastructure_mode,
                theme_profile=self._theme_profile,
                is_replay=self._is_replay,
                replay_db=self._replay_db,
                replay_virtual_time=self._replay_virtual_time,
                replay_start_time=self._replay_start_time,
                replay_end_time=self._replay_end_time,
                replay_speed=self._replay_speed,
                watched_train_ids=frozenset(self._watched_train_ids),
                selected_speed_result=sel_speed_res,
                overspeed_count=len(overspeed_ids),
                active_tsr_count=active_tsr_cnt,
                overspeed_train_ids=tuple(overspeed_ids),
                selected_sensor_health=sel_sensor_health,
                frozen_count=len(frozen_ids),
                dropped_count=len(dropped_ids),
                stale_count=stale_cnt,
                frozen_train_ids=tuple(frozen_ids),
                dropped_train_ids=tuple(dropped_ids),
                active_conflicts=tuple(self._active_conflicts.values()),
                critical_conflict_count=critical_conflicts,
                warning_conflict_count=warning_conflicts,
                advisory_conflict_count=advisory_conflicts,
                selected_conflict=sel_conflict,
                duplicate_train_numbers=duplicate_train_numbers,
                ghost_trains=ghost_trains_copy,
                filtered_ghosts=tuple(filtered_ghosts),
                selected_ghost=selected_ghost,
                ghost_visibility_mode=self._ghost_visibility_mode,
                ghost_count=len(ghost_trains_copy),
                history_hydrated_trains=self._history_hydrated_trains,
                delay_updates_applied=self._delay_updates_applied,
                stream_event_counts=tuple(sorted(self._stream_event_counts.items())),
            )

    def get_train(self, train_id: str) -> Optional[TrainRecord]:
        """Retrieve a specific train record by ID, or None if not present."""
        with self._lock:
            return self._trains.get(train_id)

    def get_selected_train(self) -> Optional[TrainRecord]:
        """Retrieve currently selected train record, or None if none selected."""
        with self._lock:
            if self._selected_id:
                return self._trains.get(self._selected_id)
            return None

    def _combined_roster_ids(self) -> List[str]:
        """Return ordered selectable IDs across filtered live trains and visible ghosts."""
        filtered_live = self._compute_filtered_list(self._trains)
        visible_ghosts = self._visible_non_superseded_ghosts()
        filtered_ghosts = self._compute_filtered_ghosts(visible_ghosts)
        return [rec.position.id for rec in filtered_live] + [
            grec.ghost.id for grec in filtered_ghosts
        ]

    def select_next(self) -> Optional[str]:
        """Move cursor selection to next train or visible ghost in roster (with wrap-around)."""
        with self._lock:
            roster_ids = self._combined_roster_ids()
            if not roster_ids:
                self._selected_id = None
                self._follow_mode = False
                return None

            curr_idx = roster_ids.index(self._selected_id) if self._selected_id in roster_ids else -1
            next_idx = (curr_idx + 1) % len(roster_ids)
            next_id = roster_ids[next_idx]
            self._selected_id = next_id
            if next_id not in self._trains:
                self._follow_mode = False
            self._version += 1
            return self._selected_id

    def select_prev(self) -> Optional[str]:
        """Move cursor selection to previous train or visible ghost in roster (with wrap-around)."""
        with self._lock:
            roster_ids = self._combined_roster_ids()
            if not roster_ids:
                self._selected_id = None
                self._follow_mode = False
                return None

            curr_idx = roster_ids.index(self._selected_id) if self._selected_id in roster_ids else -1
            if curr_idx < 0:
                prev_idx = len(roster_ids) - 1
            else:
                prev_idx = (curr_idx - 1 + len(roster_ids)) % len(roster_ids)
            prev_id = roster_ids[prev_idx]
            self._selected_id = prev_id
            if prev_id not in self._trains:
                self._follow_mode = False
            self._version += 1
            return self._selected_id

    def select_by_id(self, train_id: str) -> bool:
        """Explicitly select a live train or visible ghost run by its identifier."""
        with self._lock:
            if train_id in self._trains:
                self._selected_id = train_id
                self._version += 1
                return True
            if self._ghost_visibility_mode == GhostVisibilityMode.SHOW:
                visible_ghosts = self._visible_non_superseded_ghosts()
                if train_id in visible_ghosts:
                    self._selected_id = train_id
                    self._follow_mode = False
                    self._version += 1
                    return True
            return False

    def clear_selection(self) -> None:
        """Deselect currently active train."""
        with self._lock:
            self._selected_id = None
            self._follow_mode = False
            self._version += 1

    def toggle_follow_mode(self) -> bool:
        """Toggle follow-mode lock for currently selected train."""
        with self._lock:
            if self._selected_id and self._selected_id in self._trains:
                self._follow_mode = not self._follow_mode
            else:
                self._follow_mode = False
            self._version += 1
            return self._follow_mode

    def set_follow_mode(self, enabled: bool) -> None:
        """Explicitly enable or disable follow-mode."""
        with self._lock:
            if enabled and self._selected_id and self._selected_id in self._trains:
                self._follow_mode = True
            else:
                self._follow_mode = False
            self._version += 1

    def set_filter_query(self, query: str) -> None:
        """Update active search filter query."""
        with self._lock:
            self._filter_query = query.strip()
            self._version += 1

    def set_corridor_filter(self, corridor: Optional[str]) -> None:
        """Filter trains by railway corridor ('PATHE', 'PROASTIAKOS', etc.)."""
        with self._lock:
            self._corridor_filter = corridor.strip() if corridor else None
            self._version += 1

    def set_sort_mode(self, mode: RosterSortMode) -> None:
        """Explicitly set the active roster sort ordering mode."""
        if not isinstance(mode, RosterSortMode):
            raise TypeError(f"Expected RosterSortMode, got {type(mode).__name__}")
        with self._lock:
            if self._sort_mode != mode:
                self._sort_mode = mode
                self._version += 1

    def cycle_sort_mode(self) -> RosterSortMode:
        """Cycle active roster sort mode: TRAIN_ID -> SPEED -> DELAY -> TRAIN_ID."""
        cycle_order = (
            RosterSortMode.TRAIN_ID,
            RosterSortMode.SPEED,
            RosterSortMode.DELAY,
        )
        with self._lock:
            curr_idx = cycle_order.index(self._sort_mode)
            self._sort_mode = cycle_order[(curr_idx + 1) % len(cycle_order)]
            self._version += 1
            return self._sort_mode

    def set_breadcrumb_mode(self, mode: BreadcrumbMode) -> None:
        """Explicitly set the active historical breadcrumb trail visibility mode."""
        if not isinstance(mode, BreadcrumbMode):
            raise TypeError(f"Expected BreadcrumbMode, got {type(mode).__name__}")
        with self._lock:
            if self._breadcrumb_mode != mode:
                self._breadcrumb_mode = mode
                self._version += 1

    def cycle_breadcrumb_mode(self) -> BreadcrumbMode:
        """Cycle historical breadcrumb trail visibility: ALL -> SELECTED -> OFF -> ALL."""
        cycle_order = (
            BreadcrumbMode.ALL,
            BreadcrumbMode.SELECTED,
            BreadcrumbMode.OFF,
        )
        with self._lock:
            curr_idx = cycle_order.index(self._breadcrumb_mode)
            self._breadcrumb_mode = cycle_order[(curr_idx + 1) % len(cycle_order)]
            self._version += 1
            return self._breadcrumb_mode

    def set_inspector_mode(self, mode: InspectorMode) -> None:
        """Explicitly set the active Inspector pane display mode."""
        if not isinstance(mode, InspectorMode):
            raise TypeError(f"Expected InspectorMode, got {type(mode).__name__}")
        with self._lock:
            if self._inspector_mode != mode:
                self._inspector_mode = mode
                if mode == InspectorMode.TRAIN:
                    self._selected_station_id = None
                    self._station_cycle_anchor_id = None
                self._version += 1

    def set_selected_station(self, station_id: Optional[str]) -> None:
        """Select a station by ID and engage STATION inspector mode (or return to TRAIN if None)."""
        norm_id = station_id.strip().upper() if (station_id and station_id.strip()) else None
        target_mode = InspectorMode.STATION if norm_id is not None else InspectorMode.TRAIN
        with self._lock:
            if self._selected_station_id != norm_id or self._inspector_mode != target_mode:
                self._selected_station_id = norm_id
                self._inspector_mode = target_mode
                self._station_cycle_anchor_id = norm_id
                self._version += 1

    def cycle_station_board(
        self,
        station_ids: Sequence[str],
        initial_station_id: Optional[str] = None,
    ) -> Optional[str]:
        """Cycle through station IDs in STATION inspector mode, wrapping back to TRAIN mode.

        When entering from InspectorMode.TRAIN:
        - Selects initial_station_id if provided and non-empty (else station_ids[0]),
          records it as the cycle anchor, and switches to InspectorMode.STATION.
        When already in InspectorMode.STATION:
        - Advances to the next station in station_ids.
        - Once the cycle wraps back to the anchor station (having visited all stations in
          station_ids), resets selected_station_id to None and returns to InspectorMode.TRAIN.

        Args:
            station_ids: Ordered sequence of candidate hub station IDs to cycle through.
            initial_station_id: Optional preferred starting station ID when entering from TRAIN mode.

        Returns:
            Newly selected station ID string, or None when returning to InspectorMode.TRAIN.
        """
        cleaned_ids: List[str] = []
        for sid in station_ids:
            norm = sid.strip().upper()
            if norm and norm not in cleaned_ids:
                cleaned_ids.append(norm)

        with self._lock:
            if not cleaned_ids:
                if self._inspector_mode != InspectorMode.TRAIN or self._selected_station_id is not None:
                    self._inspector_mode = InspectorMode.TRAIN
                    self._selected_station_id = None
                    self._station_cycle_anchor_id = None
                    self._version += 1
                return None

            init_norm = (
                initial_station_id.strip().upper()
                if (initial_station_id and initial_station_id.strip())
                else None
            )

            # Transition 1: TRAIN -> STATION
            if self._inspector_mode == InspectorMode.TRAIN or self._selected_station_id is None:
                start_id = init_norm if (init_norm and init_norm in cleaned_ids) else (init_norm or cleaned_ids[0])
                self._selected_station_id = start_id
                self._station_cycle_anchor_id = start_id if start_id in cleaned_ids else cleaned_ids[0]
                self._inspector_mode = InspectorMode.STATION
                self._version += 1
                return self._selected_station_id

            # Transition 2: Already in STATION mode -> advance or wrap back to TRAIN
            if self._selected_station_id not in cleaned_ids:
                next_id = cleaned_ids[0]
                self._selected_station_id = next_id
                self._station_cycle_anchor_id = next_id
                self._version += 1
                return self._selected_station_id

            curr_idx = cleaned_ids.index(self._selected_station_id)
            next_idx = (curr_idx + 1) % len(cleaned_ids)
            next_id = cleaned_ids[next_idx]
            anchor_id = self._station_cycle_anchor_id if self._station_cycle_anchor_id in cleaned_ids else cleaned_ids[0]

            if next_id == anchor_id:
                # Completed full cycle through all candidate hub stations -> return to TRAIN mode
                self._selected_station_id = None
                self._station_cycle_anchor_id = None
                self._inspector_mode = InspectorMode.TRAIN
                self._version += 1
                return None

            self._selected_station_id = next_id
            self._version += 1
            return self._selected_station_id

    def set_station_label_mode(self, mode: StationLabelMode) -> None:
        """Explicitly set the active station label visibility mode."""
        if not isinstance(mode, StationLabelMode):
            raise TypeError(f"Expected StationLabelMode, got {type(mode).__name__}")
        with self._lock:
            if self._station_label_mode != mode:
                self._station_label_mode = mode
                self._version += 1

    def cycle_station_label_mode(self) -> StationLabelMode:
        """Cycle station label visibility: HUBS_AND_SECONDARY -> HUBS_ONLY -> MARKERS_ONLY -> HUBS_AND_SECONDARY."""
        cycle_order = (
            StationLabelMode.HUBS_AND_SECONDARY,
            StationLabelMode.HUBS_ONLY,
            StationLabelMode.MARKERS_ONLY,
        )
        with self._lock:
            curr_idx = cycle_order.index(self._station_label_mode)
            self._station_label_mode = cycle_order[(curr_idx + 1) % len(cycle_order)]
            self._version += 1
            return self._station_label_mode

    def set_track_color_mode(self, mode: TrackColorMode) -> None:
        """Explicitly set the active Braille track colorization mode."""
        if not isinstance(mode, TrackColorMode):
            raise TypeError(f"Expected TrackColorMode, got {type(mode).__name__}")
        with self._lock:
            if self._track_color_mode != mode:
                self._track_color_mode = mode
                self._version += 1

    def cycle_track_color_mode(self) -> TrackColorMode:
        """Toggle Braille track colorization mode: UNIFORM <-> CORRIDOR."""
        cycle_order = (
            TrackColorMode.UNIFORM,
            TrackColorMode.CORRIDOR,
        )
        with self._lock:
            curr_idx = cycle_order.index(self._track_color_mode)
            self._track_color_mode = cycle_order[(curr_idx + 1) % len(cycle_order)]
            self._version += 1
            return self._track_color_mode

    def set_infrastructure_mode(self, mode: InfrastructureLayerMode) -> None:
        """Explicitly set infrastructure GIS overlay mode."""
        with self._lock:
            if self._infrastructure_mode != mode:
                self._infrastructure_mode = mode
                self._version += 1

    def cycle_infrastructure_mode(self) -> InfrastructureLayerMode:
        """Cycle railway infrastructure GIS overlay mode: ALL -> TUNNELS_BRIDGES -> OFF."""
        cycle_order = (
            InfrastructureLayerMode.ALL,
            InfrastructureLayerMode.TUNNELS_BRIDGES,
            InfrastructureLayerMode.OFF,
        )
        with self._lock:
            curr_idx = cycle_order.index(self._infrastructure_mode)
            self._infrastructure_mode = cycle_order[(curr_idx + 1) % len(cycle_order)]
            self._version += 1
            return self._infrastructure_mode

    def set_theme_profile(self, profile: ThemeProfile) -> None:
        """Explicitly set active terminal color theme profile."""
        if not isinstance(profile, ThemeProfile):
            raise TypeError(f"Expected ThemeProfile enum, got {type(profile).__name__}: {profile}")
        with self._lock:
            if self._theme_profile != profile:
                self._theme_profile = profile
                self._version += 1

    def cycle_theme_profile(self) -> ThemeProfile:
        """Cycle to next terminal color theme profile: AZURE -> AMBER -> MATRIX -> SOLARIZED -> MONOCHROME."""
        with self._lock:
            self._theme_profile = get_next_theme(self._theme_profile)
            self._version += 1
            return self._theme_profile

    def set_ghost_visibility_mode(self, mode: GhostVisibilityMode) -> None:
        """Explicitly set ghost scheduled run visibility mode (F-10.1)."""
        if not isinstance(mode, GhostVisibilityMode):
            raise TypeError(f"Expected GhostVisibilityMode, got {type(mode).__name__}")
        with self._lock:
            if self._ghost_visibility_mode != mode:
                self._ghost_visibility_mode = mode
                if mode == GhostVisibilityMode.HIDE and self._selected_id in self._ghosts:
                    self._selected_id = None
                    self._follow_mode = False
                self._version += 1

    def toggle_ghost_visibility(self) -> GhostVisibilityMode:
        """Toggle visibility of untracked scheduled ghost runs: SHOW <-> HIDE (F-10.1)."""
        with self._lock:
            next_mode = (
                GhostVisibilityMode.HIDE
                if self._ghost_visibility_mode == GhostVisibilityMode.SHOW
                else GhostVisibilityMode.SHOW
            )
            self._ghost_visibility_mode = next_mode
            if next_mode == GhostVisibilityMode.HIDE and self._selected_id in self._ghosts:
                self._selected_id = None
                self._follow_mode = False
            self._version += 1
            return self._ghost_visibility_mode

    def clear(self) -> None:
        """Reset state store completely."""
        with self._lock:
            self._trains.clear()
            self._ghosts.clear()
            self._pending_history.clear()
            self._selected_id = None
            self._follow_mode = False
            self._inspector_mode = InspectorMode.TRAIN
            self._selected_station_id = None
            self._station_cycle_anchor_id = None
            self._total_messages = 0
            self._last_update_ts = 0.0
            self._history_hydrated_trains = 0
            self._delay_updates_applied = 0
            self._stream_event_counts.clear()
            self._version += 1

    # ------------------------------------------------------------------
    # Internal Filter & Sorting Logic
    # ------------------------------------------------------------------

    def _compute_filtered_ghosts(
        self,
        ghosts: Dict[str, GhostTrainRecord],
    ) -> List[GhostTrainRecord]:
        """Filter and sort active non-superseded ghost runs for the roster pane."""
        if self._ghost_visibility_mode == GhostVisibilityMode.HIDE:
            return []

        results: List[GhostTrainRecord] = []
        q = self._filter_query.lower()

        for grec in ghosts.values():
            g = grec.ghost
            if self._corridor_filter and not _ghost_matches_corridor(g, self._corridor_filter):
                continue
            if q:
                searchable = (
                    f"{g.train_number or ''} {g.id or ''} {g.name or ''} "
                    f"{g.origin or ''} {g.destination or ''} "
                    f"{g.service_type or ''} {g.category or ''} "
                    f"{g.corridor or ''} ghost"
                ).lower()
                if q not in searchable:
                    continue
            results.append(grec)

        if self._sort_mode == RosterSortMode.DELAY:
            results.sort(key=_ghost_delay_sort_key)
        else:
            results.sort(key=_ghost_natural_sort_key)
        return results

    def _compute_filtered_list(self, trains: Dict[str, TrainRecord]) -> List[TrainRecord]:
        """Filter and sort active trains by commercial train number and corridor."""
        results = []
        q = self._filter_query.lower()

        for rec in trains.values():
            p = rec.position

            # 1. Corridor check
            if self._corridor_filter:
                if not _train_matches_corridor(p, self._corridor_filter):
                    continue

            # 2. Text query matching
            if q:
                searchable = (
                    f"{p.train_number or ''} {p.locomotive_number or ''} {p.id or ''} "
                    f"{p.name or ''} {p.origin or ''} "
                    f"{p.destination or ''} {p.next_station or ''} "
                    f"{p.service_type or ''} {p.train_type or ''} {p.status or ''} "
                    f"{p.corridor or ''}"
                ).lower()
                if q not in searchable:
                    continue

            results.append(rec)

        if self._sort_mode == RosterSortMode.SPEED:
            results.sort(key=_speed_sort_key)
        elif self._sort_mode == RosterSortMode.DELAY:
            results.sort(key=_delay_sort_key)
        else:
            results.sort(key=_natural_sort_key)
        return results

    def _get_current_index(self, filtered: List[TrainRecord]) -> int:
        if not self._selected_id:
            return -1
        for idx, rec in enumerate(filtered):
            if rec.position.id == self._selected_id:
                return idx
        return -1

    def toggle_watch_train(self, train_id: str) -> bool:
        """Toggle watchdog push alerting on a train ID. Returns True if now watched, False if removed."""
        with self._lock:
            raw_id = str(train_id).strip()
            if not raw_id:
                return False
            for existing in list(self._watched_train_ids):
                if existing == raw_id or existing.upper() == raw_id.upper():
                    self._watched_train_ids.remove(existing)
                    self._version += 1
                    return False
            self._watched_train_ids.add(raw_id)
            self._version += 1
            return True

    def is_train_watched(self, train_id: str) -> bool:
        """Check if train ID is currently watched."""
        with self._lock:
            target = str(train_id).strip().upper()
            return any(w.upper() == target for w in self._watched_train_ids)

    def get_watched_train_ids(self) -> FrozenSet[str]:
        """Return frozen copy of all currently watched train identifiers."""
        with self._lock:
            return frozenset(self._watched_train_ids)
