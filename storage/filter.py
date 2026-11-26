"""
Deadband filtering and coordinate compression for high-frequency telemetry.
"""

from dataclasses import dataclass
import math
import threading
import time
from typing import Dict, List, Optional, Sequence, Tuple

from train_tracker import TrainPosition


@dataclass(frozen=True)
class DeadbandConfig:
    """Configurable thresholds for telemetry deadband suppression."""

    min_distance_m: float = 15.0              # Position movement threshold in meters
    min_delay_delta_min: int = 1              # Delay change threshold in minutes
    min_speed_delta_kmh: float = 5.0          # Speed change threshold in km/h
    max_heartbeat_interval_sec: float = 60.0  # Periodic snapshot heartbeat
    track_status_change: bool = True          # Force admit if status changed
    track_station_change: bool = True         # Force admit if next_station changed
    track_schedule_change: bool = True        # Force admit if schedule_id changed
    stale_timeout_sec: float = 1800.0         # Max inactivity before vehicle state is evicted (30m)
    stale_prune_interval_sec: float = 300.0   # Cadence for periodic stale state cleanup (5m)


@dataclass
class _LastRecordedState:
    """Internal state cache tracking the last persisted attributes of a train."""

    timestamp: float       # Epoch seconds
    lat: float
    lng: float
    speed: float
    delay: int
    status: str
    next_station: Optional[str]
    schedule_id: Optional[str]


@dataclass(frozen=True)
class DeadbandBatchEvaluation:
    """Immutable staged result of pure deadband evaluation prior to database commit."""

    admitted: Tuple[TrainPosition, ...]
    staged_states: Tuple[Tuple[str, _LastRecordedState], ...]
    inspected_count: int
    admitted_count: int
    suppressed_count: int


class DeadbandFilter:
    """
    Evaluates train telemetry against spatial, temporal, and operational thresholds
    to suppress uninformative stationary records and reduce storage volume by 40-60%.
    """

    def __init__(self, config: Optional[DeadbandConfig] = None) -> None:
        self.config = config or DeadbandConfig()
        self._lock = threading.Lock()
        self._states: Dict[str, _LastRecordedState] = {}
        self._total_inspected: int = 0
        self._total_admitted: int = 0
        self._total_suppressed: int = 0
        self._total_evicted: int = 0
        self._last_prune_ts: float = 0.0

    def _prune_stale_locked(
        self,
        current_time: float,
        max_age_sec: Optional[float] = None,
        force: bool = False,
    ) -> int:
        """Evict stale vehicle states under active self._lock."""
        if (
            not force
            and (current_time - self._last_prune_ts) < self.config.stale_prune_interval_sec
        ):
            return 0
        cutoff = max_age_sec if max_age_sec is not None else self.config.stale_timeout_sec
        stale_keys = [
            tid for tid, state in self._states.items()
            if (current_time - state.timestamp) > cutoff
        ]
        for tid in stale_keys:
            del self._states[tid]
        self._total_evicted += len(stale_keys)
        self._last_prune_ts = current_time
        return len(stale_keys)

    def prune_stale_states(
        self,
        max_age_sec: Optional[float] = None,
        now_epoch: Optional[float] = None,
    ) -> int:
        """
        Evict cached train states that have not received telemetry updates within max_age_sec.
        Returns count of evicted vehicle states.
        """
        current_time = now_epoch if now_epoch is not None else time.time()
        with self._lock:
            return self._prune_stale_locked(
                current_time=current_time,
                max_age_sec=max_age_sec,
                force=True,
            )

    @staticmethod
    def haversine_distance(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
        """Calculate great-circle distance between two coordinates in meters."""
        r = 6371000.0  # Mean Earth radius in meters
        phi1 = math.radians(lat1)
        phi2 = math.radians(lat2)
        delta_phi = math.radians(lat2 - lat1)
        delta_lambda = math.radians(lng2 - lng1)

        a = (
            math.sin(delta_phi / 2.0) ** 2
            + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
        )
        a_clamped = min(1.0, max(0.0, a))
        c = 2.0 * math.atan2(math.sqrt(a_clamped), math.sqrt(1.0 - a_clamped))
        return r * c

    def _evaluate_position_against_state(
        self,
        pos: TrainPosition,
        last: Optional[_LastRecordedState],
        current_time: float,
    ) -> Tuple[bool, Optional[str]]:
        """Pure deadband rule evaluation with zero state or counter mutation."""
        if last is None:
            return True, "initial"
        if self.config.track_status_change and pos.status != last.status:
            return True, "status"
        if self.config.track_station_change and pos.next_station != last.next_station:
            return True, "station"
        if self.config.track_schedule_change and pos.schedule_id != last.schedule_id:
            return True, "schedule"
        if abs(pos.delay - last.delay) >= self.config.min_delay_delta_min:
            return True, "delay"
        dist_m = self.haversine_distance(last.lat, last.lng, pos.lat, pos.lng)
        if dist_m >= self.config.min_distance_m:
            return True, "distance"
        if abs(pos.speed - last.speed) >= self.config.min_speed_delta_kmh:
            return True, "speed"
        elapsed = current_time - last.timestamp
        if elapsed >= self.config.max_heartbeat_interval_sec:
            return True, "heartbeat"
        return False, None

    @staticmethod
    def _state_key(pos: TrainPosition) -> str:
        """Return a consist-aware cache key so coupled units sharing train_number stay isolated."""
        unit_id = (pos.id or "").strip()
        loco = (pos.locomotive_number or "").strip()
        if unit_id or loco:
            return f"{pos.train_number}|{unit_id}|{loco}"
        return pos.train_number

    @staticmethod
    def _build_state(pos: TrainPosition, current_time: float) -> _LastRecordedState:
        """Construct an internal state snapshot for an admitted TrainPosition."""
        return _LastRecordedState(
            timestamp=current_time,
            lat=pos.lat,
            lng=pos.lng,
            speed=pos.speed,
            delay=pos.delay,
            status=pos.status,
            next_station=pos.next_station,
            schedule_id=pos.schedule_id,
        )

    def evaluate_batch(
        self,
        positions: Sequence[TrainPosition],
        now_epoch: Optional[float] = None,
    ) -> DeadbandBatchEvaluation:
        """Purely evaluate a telemetry batch without mutating filter state or counters."""
        current_time = now_epoch if now_epoch is not None else time.time()
        with self._lock:
            working_states: Dict[str, _LastRecordedState] = {}
            for p in positions:
                key = self._state_key(p)
                if key in self._states:
                    working_states[key] = self._states[key]

        admitted: List[TrainPosition] = []
        staged_updates: Dict[str, _LastRecordedState] = {}
        suppressed_count = 0

        for pos in positions:
            train_key = self._state_key(pos)
            last = working_states.get(train_key)
            should_save, _ = self._evaluate_position_against_state(pos, last, current_time)
            if should_save:
                new_state = self._build_state(pos, current_time)
                working_states[train_key] = new_state
                staged_updates[train_key] = new_state
                admitted.append(pos)
            else:
                suppressed_count += 1

        return DeadbandBatchEvaluation(
            admitted=tuple(admitted),
            staged_states=tuple(staged_updates.items()),
            inspected_count=len(positions),
            admitted_count=len(admitted),
            suppressed_count=suppressed_count,
        )

    def commit_batch(
        self,
        evaluation: DeadbandBatchEvaluation,
        now_epoch: Optional[float] = None,
    ) -> None:
        """Promote staged deadband states and counters strictly AFTER SQLite commit succeeds."""
        current_time = now_epoch if now_epoch is not None else time.time()
        with self._lock:
            self._prune_stale_locked(current_time=current_time, force=False)
            for train_key, new_state in evaluation.staged_states:
                self._states[train_key] = new_state
            self._total_inspected += evaluation.inspected_count
            self._total_admitted += evaluation.admitted_count
            self._total_suppressed += evaluation.suppressed_count

    def should_record(
        self, pos: TrainPosition, now_epoch: Optional[float] = None
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluate if a TrainPosition meets admission criteria for storage.

        Returns:
            Tuple of (should_record: bool, reason: Optional[str])
            Reason is one of: 'initial', 'distance', 'delay', 'status', 'station',
                              'schedule', 'speed', 'heartbeat', or None if suppressed.
        """
        current_time = now_epoch if now_epoch is not None else time.time()
        train_key = self._state_key(pos)

        with self._lock:
            self._prune_stale_locked(current_time=current_time, force=False)
            self._total_inspected += 1
            last = self._states.get(train_key)
            should_save, reason = self._evaluate_position_against_state(pos, last, current_time)
            if should_save:
                self._admit_locked(train_key, pos, current_time)
                return True, reason

            self._total_suppressed += 1
            return False, None

    def filter_batch(
        self, positions: List[TrainPosition], now_epoch: Optional[float] = None
    ) -> List[TrainPosition]:
        """Filter a batch of positions, returning only admitted entities."""
        return [p for p in positions if self.should_record(p, now_epoch=now_epoch)[0]]

    def _admit(self, train_id: str, pos: TrainPosition, current_time: float) -> None:
        """Update internal cache with newly admitted telemetry state."""
        with self._lock:
            self._admit_locked(train_id, pos, current_time)

    def _admit_locked(self, train_id: str, pos: TrainPosition, current_time: float) -> None:
        """Internal helper to mutate state cache under active lock."""
        self._states[train_id] = self._build_state(pos, current_time)
        self._total_admitted += 1

    def reset(self) -> None:
        """Clear all cached vehicle states and reset metric counters."""
        with self._lock:
            self._states.clear()
            self._total_inspected = 0
            self._total_admitted = 0
            self._total_suppressed = 0
            self._total_evicted = 0
            self._last_prune_ts = 0.0

    @property
    def stats(self) -> Dict[str, float]:
        """Return compression and admission metrics."""
        with self._lock:
            ratio = (
                (self._total_suppressed / self._total_inspected * 100.0)
                if self._total_inspected > 0
                else 0.0
            )
            return {
                "total_inspected": float(self._total_inspected),
                "total_admitted": float(self._total_admitted),
                "total_suppressed": float(self._total_suppressed),
                "total_evicted": float(self._total_evicted),
                "suppression_ratio_pct": round(ratio, 2),
                "tracked_trains": float(len(self._states)),
            }
