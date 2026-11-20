"""
Deadband filtering and coordinate compression for high-frequency telemetry.
"""

from dataclasses import dataclass
import math
import threading
import time
from typing import Dict, List, Optional, Tuple

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

    def prune_stale_states(
        self,
        max_age_sec: Optional[float] = None,
        now_epoch: Optional[float] = None,
    ) -> int:
        """
        Evict cached train states that have not received telemetry updates within max_age_sec.
        Returns count of evicted vehicle states.
        """
        cutoff = max_age_sec if max_age_sec is not None else self.config.stale_timeout_sec
        current_time = now_epoch if now_epoch is not None else time.time()
        with self._lock:
            stale_keys = [
                tid for tid, state in self._states.items()
                if (current_time - state.timestamp) > cutoff
            ]
            for tid in stale_keys:
                del self._states[tid]
            self._total_evicted += len(stale_keys)
            self._last_prune_ts = current_time
            return len(stale_keys)

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
        c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
        return r * c

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
        train_id = pos.train_number

        with self._lock:
            # Automatic periodic eviction of stale train states
            if (current_time - self._last_prune_ts) >= self.config.stale_prune_interval_sec:
                stale_keys = [
                    tid for tid, state in self._states.items()
                    if (current_time - state.timestamp) > self.config.stale_timeout_sec
                ]
                for tid in stale_keys:
                    del self._states[tid]
                self._total_evicted += len(stale_keys)
                self._last_prune_ts = current_time

            self._total_inspected += 1
            last = self._states.get(train_id)
            if last is None:
                self._admit_locked(train_id, pos, current_time)
                return True, "initial"

            # 1. Status transition check (e.g. Κανονική -> Στάση)
            if self.config.track_status_change and pos.status != last.status:
                self._admit_locked(train_id, pos, current_time)
                return True, "status"

            # 2. Next station change check
            if self.config.track_station_change and pos.next_station != last.next_station:
                self._admit_locked(train_id, pos, current_time)
                return True, "station"

            # 3. Schedule change check
            if self.config.track_schedule_change and pos.schedule_id != last.schedule_id:
                self._admit_locked(train_id, pos, current_time)
                return True, "schedule"

            # 4. Delay change check
            if abs(pos.delay - last.delay) >= self.config.min_delay_delta_min:
                self._admit_locked(train_id, pos, current_time)
                return True, "delay"

            # 5. Distance displacement check
            dist_m = self.haversine_distance(last.lat, last.lng, pos.lat, pos.lng)
            if dist_m >= self.config.min_distance_m:
                self._admit_locked(train_id, pos, current_time)
                return True, "distance"

            # 6. Speed change check
            if abs(pos.speed - last.speed) >= self.config.min_speed_delta_kmh:
                self._admit_locked(train_id, pos, current_time)
                return True, "speed"

            # 7. Heartbeat interval check
            elapsed = current_time - last.timestamp
            if elapsed >= self.config.max_heartbeat_interval_sec:
                self._admit_locked(train_id, pos, current_time)
                return True, "heartbeat"

            # Suppressed redundant record
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
        self._states[train_id] = _LastRecordedState(
            timestamp=current_time,
            lat=pos.lat,
            lng=pos.lng,
            speed=pos.speed,
            delay=pos.delay,
            status=pos.status,
            next_station=pos.next_station,
            schedule_id=pos.schedule_id,
        )
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
