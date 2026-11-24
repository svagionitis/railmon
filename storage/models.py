"""
Domain models and dataclasses for SQLite persistence.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, Optional, Tuple

from train_tracker import TrainPosition


def iso_utc_now() -> str:
    """Return current UTC timestamp in ISO 8601 format with 'Z' suffix."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class TrainRecordDB:
    """Persistent vehicle entity in the trains table."""

    train_number: str
    locomotive_number: Optional[str] = None
    default_name: Optional[str] = None
    train_type: Optional[str] = None
    service_type: Optional[str] = None
    first_observed_at: str = ""
    last_observed_at: str = ""

    def to_db_row(self) -> Tuple[Any, ...]:
        """Convert dataclass to parameter tuple matching INSERT/UPDATE statement."""
        return (
            self.train_number,
            self.locomotive_number,
            self.default_name,
            self.train_type,
            self.service_type,
            self.first_observed_at,
            self.last_observed_at,
        )


@dataclass(frozen=True)
class ScheduledTripDB:
    """Scheduled service trip for a given date in the scheduled_trips table."""

    schedule_id: str
    train_number: str
    origin: Optional[str] = None
    destination: Optional[str] = None
    corridor: Optional[str] = None
    service_date: str = ""
    scheduled_departure: Optional[str] = None
    scheduled_arrival: Optional[str] = None
    raw_json: Optional[str] = None
    created_at: str = ""

    def to_db_row(self) -> Tuple[Any, ...]:
        """Convert dataclass to parameter tuple matching INSERT statement."""
        return (
            self.schedule_id,
            self.train_number,
            self.origin,
            self.destination,
            self.corridor,
            self.service_date,
            self.scheduled_departure,
            self.scheduled_arrival,
            self.raw_json,
        )


@dataclass(frozen=True)
class ScheduleStopDB:
    """Intermediate scheduled timetable stop in the schedule_stops table."""

    schedule_id: str
    stop_sequence: int
    station_name: str
    station_id: Optional[str] = None
    scheduled_arrival: Optional[str] = None
    scheduled_departure: Optional[str] = None
    actual_arrival: Optional[str] = None
    actual_departure: Optional[str] = None
    platform: Optional[str] = None
    delay_sec: int = 0
    id: Optional[int] = None

    def to_db_row(self) -> Tuple[Any, ...]:
        """Convert dataclass to parameter tuple matching INSERT statement."""
        return (
            self.schedule_id,
            self.stop_sequence,
            self.station_id,
            self.station_name,
            self.scheduled_arrival,
            self.scheduled_departure,
            self.actual_arrival,
            self.actual_departure,
            self.platform,
            self.delay_sec,
        )


@dataclass(frozen=True)
class TelemetryHistoryRow:
    """High-frequency spatial and operational telemetry snapshot in telemetry_history."""

    train_number: str
    recorded_at: str
    lat: float
    lng: float
    speed: float
    course: float
    schedule_id: Optional[str] = None
    track_heading: Optional[float] = None
    altitude: Optional[float] = None
    delay_min: int = 0
    status: str = "Κανονική"
    progress: Optional[float] = None
    chainage_m: Optional[int] = None
    dist_to_dest_m: Optional[int] = None
    next_station: Optional[str] = None
    corridor: Optional[str] = None
    id: Optional[int] = None

    @classmethod
    def from_db_row(cls, row: Any) -> "TelemetryHistoryRow":
        """Instantiate a TelemetryHistoryRow from a sqlite3.Row or dict-like object."""
        rec_at = row["recorded_at"]
        if hasattr(rec_at, "isoformat"):
            rec_at_str = rec_at.isoformat().replace("+00:00", "Z")
        else:
            rec_at_str = str(rec_at).replace("+00:00", "Z")

        keys = row.keys() if hasattr(row, "keys") else row

        return cls(
            train_number=str(row["train_number"]),
            recorded_at=rec_at_str,
            lat=float(row["lat"]),
            lng=float(row["lng"]),
            speed=float(row["speed"]),
            course=float(row["course"]),
            schedule_id=row["schedule_id"] if row["schedule_id"] is not None else None,
            track_heading=float(row["track_heading"]) if row["track_heading"] is not None else None,
            altitude=float(row["altitude"]) if row["altitude"] is not None else None,
            delay_min=int(row["delay_min"]) if row["delay_min"] is not None else 0,
            status=str(row["status"]) if row["status"] is not None else "Κανονική",
            progress=float(row["progress"]) if row["progress"] is not None else None,
            chainage_m=int(row["chainage_m"]) if row["chainage_m"] is not None else None,
            dist_to_dest_m=int(row["dist_to_dest_m"]) if row["dist_to_dest_m"] is not None else None,
            next_station=row["next_station"] if row["next_station"] is not None else None,
            corridor=row["corridor"] if row["corridor"] is not None else None,
            id=int(row["id"]) if "id" in keys and row["id"] is not None else None,
        )

    @classmethod
    def from_train_position(
        cls, pos: TrainPosition, recorded_at: Optional[str] = None
    ) -> "TelemetryHistoryRow":
        """Instantiate a database row from a live TrainPosition telemetry entity."""
        timestamp = recorded_at or pos.timestamp or iso_utc_now()
        return cls(
            train_number=pos.train_number,
            schedule_id=pos.schedule_id,
            recorded_at=timestamp,
            lat=pos.lat,
            lng=pos.lng,
            speed=pos.speed,
            course=pos.course,
            track_heading=pos.track_heading,
            altitude=pos.altitude,
            delay_min=pos.delay,
            status=pos.status,
            progress=pos.progress,
            chainage_m=pos.chainage_m,
            dist_to_dest_m=pos.distance_to_destination_m,
            next_station=pos.next_station,
            corridor=pos.corridor,
        )

    def to_db_row(self) -> Tuple[Any, ...]:
        """Convert dataclass to parameter tuple matching INSERT statement."""
        return (
            self.train_number,
            self.schedule_id,
            self.recorded_at,
            self.lat,
            self.lng,
            self.speed,
            self.course,
            self.track_heading,
            self.altitude,
            self.delay_min,
            self.status,
            self.progress,
            self.chainage_m,
            self.dist_to_dest_m,
            self.next_station,
            self.corridor,
        )


@dataclass(frozen=True)
class RecordingSessionDB:
    """Metadata tracking recording session lifecycles in recording_sessions."""

    started_at: str
    mode: str
    client_version: str
    ended_at: Optional[str] = None
    total_records_written: int = 0
    session_id: Optional[int] = None

    @classmethod
    def from_db_row(cls, row: Any) -> "RecordingSessionDB":
        """Instantiate a RecordingSessionDB from a sqlite3.Row or dict-like object."""
        started = row["started_at"]
        started_str = (
            started.isoformat().replace("+00:00", "Z")
            if hasattr(started, "isoformat")
            else str(started).replace("+00:00", "Z")
        )
        ended = row["ended_at"]
        ended_str = (
            ended.isoformat().replace("+00:00", "Z")
            if hasattr(ended, "isoformat")
            else (str(ended).replace("+00:00", "Z") if ended is not None else None)
        )
        keys = row.keys() if hasattr(row, "keys") else row

        return cls(
            started_at=started_str,
            mode=str(row["mode"]),
            client_version=str(row["client_version"]),
            ended_at=ended_str,
            total_records_written=int(row["total_records_written"] or 0),
            session_id=int(row["session_id"]) if "session_id" in keys and row["session_id"] is not None else None,
        )

    def to_db_row(self) -> Tuple[Any, ...]:
        """Convert dataclass to parameter tuple matching INSERT statement."""
        return (
            self.started_at,
            self.ended_at,
            self.mode,
            self.total_records_written,
            self.client_version,
        )


@dataclass(frozen=True)
class TripSummary:
    """Aggregated summary of a distinct train service recorded in telemetry history."""

    train_number: str
    service_date: str
    first_seen: str
    last_seen: str
    total_points: int
    max_delay_min: int
    avg_delay_min: float
    max_speed_kmh: float
    avg_speed_kmh: float
    schedule_id: Optional[str] = None
    corridor: Optional[str] = None
    origin: Optional[str] = None
    destination: Optional[str] = None

    @classmethod
    def from_db_row(cls, row: Any) -> "TripSummary":
        """Instantiate a TripSummary from a sqlite3.Row or dict-like object."""
        first_seen = row["first_seen"]
        first_seen_str = (
            first_seen.isoformat().replace("+00:00", "Z")
            if hasattr(first_seen, "isoformat")
            else str(first_seen).replace("+00:00", "Z")
        )
        last_seen = row["last_seen"]
        last_seen_str = (
            last_seen.isoformat().replace("+00:00", "Z")
            if hasattr(last_seen, "isoformat")
            else str(last_seen).replace("+00:00", "Z")
        )
        keys = row.keys() if hasattr(row, "keys") else row

        return cls(
            train_number=str(row["train_number"]),
            service_date=str(row["service_date"]),
            first_seen=first_seen_str,
            last_seen=last_seen_str,
            total_points=int(row["total_points"]),
            max_delay_min=int(row["max_delay_min"] or 0),
            avg_delay_min=float(row["avg_delay_min"] or 0.0),
            max_speed_kmh=float(row["max_speed_kmh"] or 0.0),
            avg_speed_kmh=float(row["avg_speed_kmh"] or 0.0),
            schedule_id=row["schedule_id"]
            if "schedule_id" in keys and row["schedule_id"] is not None
            else None,
            corridor=row["corridor"]
            if "corridor" in keys and row["corridor"] is not None
            else None,
            origin=row["origin"]
            if "origin" in keys and row["origin"] is not None
            else None,
            destination=row["destination"]
            if "destination" in keys and row["destination"] is not None
            else None,
        )


@dataclass(frozen=True)
class CorridorPunctualitySummary:
    """Operational punctuality and delay metrics for a railway corridor."""

    corridor: str
    total_trips: int
    total_observations: int
    avg_delay_min: float
    max_delay_min: int
    on_time_count: int
    delayed_count: int
    severe_delay_count: int
    punctuality_rate_pct: float
    start_date: Optional[str] = None
    end_date: Optional[str] = None

    @classmethod
    def from_db_row(
        cls,
        row: Any,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> "CorridorPunctualitySummary":
        """Instantiate a CorridorPunctualitySummary from a sqlite3.Row or dict-like object."""
        return cls(
            corridor=str(row["corridor"]),
            total_trips=int(row["total_trips"]),
            total_observations=int(row["total_observations"]),
            avg_delay_min=float(row["avg_delay_min"] or 0.0),
            max_delay_min=int(row["max_delay_min"] or 0),
            on_time_count=int(row["on_time_count"] or 0),
            delayed_count=int(row["delayed_count"] or 0),
            severe_delay_count=int(row["severe_delay_count"] or 0),
            punctuality_rate_pct=float(row["punctuality_rate_pct"] or 0.0),
            start_date=start_date,
            end_date=end_date,
        )


@dataclass(frozen=True)
class StationDwellRecord:
    """Station stop and dwell duration analysis derived from telemetry speed profiles."""

    train_number: str
    station_name: str
    arrival_time: str
    departure_time: str
    dwell_seconds: int
    arrival_delay_min: int
    departure_delay_min: int

    @classmethod
    def from_db_row(cls, row: Any) -> "StationDwellRecord":
        """Instantiate a StationDwellRecord from a sqlite3.Row or dict-like object."""
        arr = row["arrival_time"]
        arr_str = (
            arr.isoformat().replace("+00:00", "Z")
            if hasattr(arr, "isoformat")
            else str(arr).replace("+00:00", "Z")
        )
        dep = row["departure_time"]
        dep_str = (
            dep.isoformat().replace("+00:00", "Z")
            if hasattr(dep, "isoformat")
            else str(dep).replace("+00:00", "Z")
        )

        return cls(
            train_number=str(row["train_number"]),
            station_name=str(row["station_name"]),
            arrival_time=arr_str,
            departure_time=dep_str,
            dwell_seconds=int(row["dwell_seconds"] or 0),
            arrival_delay_min=int(row["arrival_delay_min"] or 0),
            departure_delay_min=int(row["departure_delay_min"] or 0),
        )


@dataclass(frozen=True)
class SpeedProfilePoint:
    """Spatial and kinematic point along a railway track for speed/chainage profiling."""

    recorded_at: str
    speed_kmh: float
    lat: float
    lng: float
    chainage_m: Optional[int] = None
    dist_to_dest_m: Optional[int] = None
    altitude: Optional[float] = None

    @classmethod
    def from_db_row(cls, row: Any) -> "SpeedProfilePoint":
        """Instantiate a SpeedProfilePoint from a sqlite3.Row or dict-like object."""
        rec = row["recorded_at"]
        rec_str = (
            rec.isoformat().replace("+00:00", "Z")
            if hasattr(rec, "isoformat")
            else str(rec).replace("+00:00", "Z")
        )
        keys = row.keys() if hasattr(row, "keys") else row

        return cls(
            recorded_at=rec_str,
            speed_kmh=float(row["speed"]),
            lat=float(row["lat"]),
            lng=float(row["lng"]),
            chainage_m=int(row["chainage_m"])
            if "chainage_m" in keys and row["chainage_m"] is not None
            else None,
            dist_to_dest_m=int(row["dist_to_dest_m"])
            if "dist_to_dest_m" in keys and row["dist_to_dest_m"] is not None
            else None,
            altitude=float(row["altitude"])
            if "altitude" in keys and row["altitude"] is not None
            else None,
        )


@dataclass(frozen=True)
class DatabaseStats:
    """High-level storage and audit metrics for the SQLite database."""

    total_telemetry_rows: int
    total_trains: int
    total_trips: int
    total_sessions: int
    file_size_bytes: int
    earliest_record: Optional[str]
    latest_record: Optional[str]
    wal_mode_enabled: bool


@dataclass(frozen=True)
class ReplayBounds:
    """Temporal boundaries and observation counts for SQLite historical DVR replay."""

    min_recorded_at: str
    max_recorded_at: str
    min_epoch: float
    max_epoch: float
    total_rows: int
    total_trains: int

    @property
    def duration_sec(self) -> float:
        """Total timeline span in seconds."""
        return max(0.0, self.max_epoch - self.min_epoch)


@dataclass(frozen=True)
class ReplayObservation:
    """A single historical telemetry observation enriched with vehicle and trip metadata."""

    position: TrainPosition
    recorded_at: str
    recorded_epoch: float


class BottleneckSeverity(Enum):
    """Severity classification for network bottlenecks."""

    LOW = "LOW"
    MED = "MED"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class BottleneckNode:
    """Analytical summary of delay accumulation at a station node or corridor segment."""

    rank: int
    name: str
    node_type: str  # "STATION" or "SEGMENT"
    total_trips: int
    avg_dwell_seconds: float
    median_delay_minutes: float
    p90_delay_minutes: float
    max_delay_minutes: float
    delay_delta_minutes: float  # Delay gained specifically at this node
    bottleneck_score: float  # Normalized 0.0 - 100.0 index
    severity: BottleneckSeverity

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "rank": self.rank,
            "name": self.name,
            "type": self.node_type,
            "total_trips": self.total_trips,
            "avg_dwell_seconds": round(self.avg_dwell_seconds, 1),
            "median_delay_minutes": round(self.median_delay_minutes, 1),
            "p90_delay_minutes": round(self.p90_delay_minutes, 1),
            "max_delay_minutes": round(self.max_delay_minutes, 1),
            "delay_delta_minutes": round(self.delay_delta_minutes, 1),
            "bottleneck_score": round(self.bottleneck_score, 1),
            "severity": self.severity.value,
        }


@dataclass(frozen=True)
class BottleneckReport:
    """Aggregate corridor bottleneck report containing ranked nodes."""

    corridor: Optional[str]
    time_window_start: Optional[str]
    time_window_end: Optional[str]
    total_trips_analyzed: int
    total_observations_analyzed: int
    nodes: Tuple[BottleneckNode, ...]

    def to_dict(self) -> Dict[str, Any]:
        """Convert entire report to dictionary."""
        return {
            "corridor": self.corridor or "ALL",
            "time_window_start": self.time_window_start,
            "time_window_end": self.time_window_end,
            "total_trips_analyzed": self.total_trips_analyzed,
            "total_observations_analyzed": self.total_observations_analyzed,
            "bottlenecks": [n.to_dict() for n in self.nodes],
        }



