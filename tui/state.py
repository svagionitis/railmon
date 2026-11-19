"""
Thread-safe state repository and reactive models for live train telemetry and UI state.
"""

from collections import deque
from dataclasses import dataclass, field
from enum import Enum, auto
import threading
import time
from typing import Deque, Dict, List, Optional, Tuple

from train_tracker import TrainPosition
from tui.geo import (
    GeoPoint,
    NORTHERN_GREECE_PRESET,
    PATHE_PRESET,
    PROASTIAKOS_PRESET,
)


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


@dataclass(frozen=True)
class HistoricalBreadcrumb:
    """A historical geographic point recorded during train movement."""

    lat: float
    lng: float
    timestamp: float
    speed: float


@dataclass
class TrainRecord:
    """Stateful wrapper around TrainPosition maintaining motion history."""

    position: TrainPosition
    trail: Deque[HistoricalBreadcrumb] = field(default_factory=lambda: deque(maxlen=15))
    first_seen: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    update_count: int = 1

    def append_breadcrumb(self, pos: TrainPosition, threshold_deg: float = 1e-5) -> None:
        """Record a historical coordinate if position has shifted significantly."""
        now = time.time()
        if not self.trail or (
            abs(self.trail[-1].lat - pos.lat) > threshold_deg
            or abs(self.trail[-1].lng - pos.lng) > threshold_deg
        ):
            self.trail.append(
                HistoricalBreadcrumb(
                    lat=pos.lat,
                    lng=pos.lng,
                    timestamp=now,
                    speed=pos.speed,
                )
            )
        self.position = pos
        self.last_updated = now
        self.update_count += 1

    def is_stale(self, threshold_sec: float = 1800.0) -> bool:
        """Check if train has stopped receiving updates beyond threshold (default: 30 minutes)."""
        return (time.time() - self.last_updated) > threshold_sec


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
    service_type = (p.service_type or "").strip().upper()
    train_type = (p.train_type or "").strip().upper()
    origin = (p.origin or "").strip().upper()
    destination = (p.destination or "").strip().upper()
    next_station = (p.next_station or "").strip().upper()

    # Known corridor keywords and aliases
    proast_corridors = ("PROASTIAKOS", "PROASTIAKOS_ATH", "PIRAIR", "KIATO", "AERODROMIO", "AIRPORT", "CHALKIDA")
    pathe_corridors = ("PATHE", "E85", "MAINLINE")
    northern_corridors = ("NORTHERN", "MACEDONIA", "THRACE", "STRYMONAS", "ALEX", "IDOMENI")
    thessaly_corridors = ("THESSALY", "VOLOS", "KALAMBAKA")

    # 1. When raw corridor string is present, use explicit corridor tag routing
    if corridor:
        if target in corridor:
            return True
        if any(c in corridor for c in proast_corridors):
            return target in ("PROASTIAKOS", "PROASTIAKOS_ATH", "SUBURBAN")
        if any(c in corridor for c in pathe_corridors):
            return target in ("PATHE", "PATHE_MAINLINE", "MAINLINE")
        if any(c in corridor for c in northern_corridors):
            return target in ("NORTHERN", "NORTHERN_GREECE", "NORTH")
        if any(c in corridor for c in thessaly_corridors):
            return target in ("THESSALY",)
        return False

    # 2. When raw corridor is absent (real-world SSE telemetry), infer from metadata & geography
    has_coords = (p.lat != 0.0 or p.lng != 0.0)
    pt = GeoPoint(lat=p.lat, lng=p.lng) if has_coords else None

    is_suburban = (
        "SUBURBAN" in service_type
        or "PROAST" in service_type
        or "SUBURBAN" in train_type
        or "PROAST" in train_type
        or any(st in origin or st in destination or st in next_station for st in ("ΑΕΡΟΔΡΟΜΙΟ", "ΚΙΑΤΟ", "ΧΑΛΚΙΔΑ", "ΛΙΟΣΙΑ", "ΝΕΡΑΤΖΙΩΤΙΣΣΑ"))
    )
    is_intercity = (
        "INTERCITY" in service_type
        or "IC" in service_type
        or "INTERCITY" in train_type
        or "IC" in train_type
    )

    if target in ("PROASTIAKOS", "PROASTIAKOS_ATH", "SUBURBAN"):
        if is_suburban:
            return True
        if is_intercity:
            return False
        if pt and PROASTIAKOS_PRESET.contains(pt):
            return True
        return False

    if target in ("PATHE", "PATHE_MAINLINE", "MAINLINE"):
        if is_intercity:
            return True
        if is_suburban:
            return False
        if pt and PATHE_PRESET.contains(pt):
            return True
        return False

    if target in ("NORTHERN", "NORTHERN_GREECE", "NORTH"):
        if is_suburban:
            return False
        if any(st in origin or st in destination or st in next_station for st in ("ΣΕΡΡΕΣ", "ΔΡΑΜΑ", "ΞΑΝΘΗ", "ΚΟΜΟΤΗΝΗ", "ΑΛΕΞΑΝΔΡΟΥΠΟΛΗ", "ΦΛΩΡΙΝΑ", "ΕΔΕΣΣΑ")):
            return True
        if pt and NORTHERN_GREECE_PRESET.contains(pt):
            return True
        return False

    if target in ("THESSALY",):
        if is_suburban:
            return False
        if any(st in origin or st in destination or st in next_station for st in ("ΒΟΛΟΣ", "ΚΑΛΑΜΠΑΚΑ", "ΚΑΡΔΙΤΣΑ", "ΤΡΙΚΑΛΑ")):
            return True
        return False

    return False


def _natural_sort_key(rec: TrainRecord) -> Tuple[int, str]:
    """Helper to sort trains by number naturally (e.g. 50 before 1597)."""
    tn = (rec.position.train_number or "").strip()
    digits = "".join(ch for ch in tn if ch.isdigit())
    if digits:
        return (0, f"{int(digits):010d}_{tn}")
    return (1, tn or rec.position.id)


class TrainStateStore:
    """Thread-safe state repository for live train positions and UI view state."""

    def __init__(self, stale_timeout_sec: float = 1800.0, max_trail_len: int = 15):
        self._lock = threading.RLock()
        self._stale_timeout_sec = stale_timeout_sec
        self._max_trail_len = max_trail_len

        # Telemetry data
        self._trains: Dict[str, TrainRecord] = {}
        self._total_messages: int = 0
        self._last_update_ts: float = 0.0
        self._last_prune_ts: float = 0.0
        self._total_trains_pruned: int = 0
        self._version: int = 0

        # UI & View state
        self._selected_id: Optional[str] = None
        self._follow_mode: bool = False
        self._filter_query: str = ""
        self._corridor_filter: Optional[str] = None
        self._connection_status: ConnectionStatus = ConnectionStatus.DISCONNECTED
        self._status_message: str = "Ready"
        self._is_recording: bool = False
        self._recording_db: Optional[str] = None

    # ------------------------------------------------------------------
    # Telemetry Ingestion (Called by StreamWorker Thread)
    # ------------------------------------------------------------------

    def update_positions(self, positions: List[TrainPosition]) -> None:
        """
        Ingest a batch of train positions with mutex protection.
        Updates existing records, creates new records, appends breadcrumbs,
        and increments the store version counter.
        """
        with self._lock:
            now = time.time()
            for pos in positions:
                if not pos.id:
                    continue
                if pos.id in self._trains:
                    self._trains[pos.id].append_breadcrumb(pos)
                else:
                    record = TrainRecord(
                        position=pos,
                        trail=deque(maxlen=self._max_trail_len),
                        first_seen=now,
                        last_updated=now,
                        update_count=1,
                    )
                    record.trail.append(
                        HistoricalBreadcrumb(
                            lat=pos.lat,
                            lng=pos.lng,
                            timestamp=now,
                            speed=pos.speed,
                        )
                    )
                    self._trains[pos.id] = record

            self._total_messages += 1
            self._last_update_ts = now
            self._version += 1

    def prune_stale_trains(self, max_age_sec: Optional[float] = None) -> int:
        """
        Remove trains that have not received telemetry updates within max_age_sec.
        Returns the number of pruned trains.
        """
        cutoff = max_age_sec if max_age_sec is not None else self._stale_timeout_sec
        with self._lock:
            now = time.time()
            stale_ids = [
                tid for tid, rec in self._trains.items()
                if (now - rec.last_updated) > cutoff
            ]
            for tid in stale_ids:
                del self._trains[tid]
                if self._selected_id == tid:
                    self._selected_id = None
                    self._follow_mode = False

            if stale_ids:
                self._version += 1
                self._total_trains_pruned += len(stale_ids)
            self._last_prune_ts = now
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

    # ------------------------------------------------------------------
    # UI State & Cursor Navigation (Called by UI Thread)
    # ------------------------------------------------------------------

    def get_snapshot(self) -> StateSnapshot:
        """
        Produce a frozen, consistent point-in-time snapshot for UI rendering.
        Operates with minimal lock hold duration.
        """
        with self._lock:
            trains_copy = dict(self._trains)
            filtered = self._compute_filtered_list(trains_copy)

            sel_idx = -1
            selected_record = None
            if self._selected_id and self._selected_id in trains_copy:
                selected_record = trains_copy[self._selected_id]
                for idx, rec in enumerate(filtered):
                    if rec.position.id == self._selected_id:
                        sel_idx = idx
                        break

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

    def select_next(self) -> Optional[str]:
        """Move cursor selection to next train in filtered roster (with wrap-around)."""
        with self._lock:
            filtered = self._compute_filtered_list(self._trains)
            if not filtered:
                self._selected_id = None
                self._follow_mode = False
                return None

            curr_idx = self._get_current_index(filtered)
            next_idx = (curr_idx + 1) % len(filtered)
            self._selected_id = filtered[next_idx].position.id
            self._version += 1
            return self._selected_id

    def select_prev(self) -> Optional[str]:
        """Move cursor selection to previous train in filtered roster (with wrap-around)."""
        with self._lock:
            filtered = self._compute_filtered_list(self._trains)
            if not filtered:
                self._selected_id = None
                self._follow_mode = False
                return None

            curr_idx = self._get_current_index(filtered)
            if curr_idx < 0:
                prev_idx = len(filtered) - 1
            else:
                prev_idx = (curr_idx - 1 + len(filtered)) % len(filtered)
            self._selected_id = filtered[prev_idx].position.id
            self._version += 1
            return self._selected_id

    def select_by_id(self, train_id: str) -> bool:
        """Explicitly select a train by its identifier."""
        with self._lock:
            if train_id in self._trains:
                self._selected_id = train_id
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

    def clear(self) -> None:
        """Reset state store completely."""
        with self._lock:
            self._trains.clear()
            self._selected_id = None
            self._follow_mode = False
            self._total_messages = 0
            self._last_update_ts = 0.0
            self._version += 1

    # ------------------------------------------------------------------
    # Internal Filter & Sorting Logic
    # ------------------------------------------------------------------

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
                    f"{p.train_number or ''} {p.name or ''} {p.origin or ''} "
                    f"{p.destination or ''} {p.next_station or ''} "
                    f"{p.service_type or ''} {p.train_type or ''} {p.status or ''} "
                    f"{p.corridor or ''}"
                ).lower()
                if q not in searchable:
                    continue

            results.append(rec)

        results.sort(key=_natural_sort_key)
        return results

    def _get_current_index(self, filtered: List[TrainRecord]) -> int:
        if not self._selected_id:
            return -1
        for idx, rec in enumerate(filtered):
            if rec.position.id == self._selected_id:
                return idx
        return -1
