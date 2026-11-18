"""
Domain models and dataclasses for SQLite persistence.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from train_tracker import TrainPosition


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
    def from_train_position(
        cls, pos: TrainPosition, recorded_at: Optional[str] = None
    ) -> "TelemetryHistoryRow":
        """Instantiate a database row from a live TrainPosition telemetry entity."""
        timestamp = recorded_at or pos.timestamp or (datetime.utcnow().isoformat() + "Z")
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

    def to_db_row(self) -> Tuple[Any, ...]:
        """Convert dataclass to parameter tuple matching INSERT statement."""
        return (
            self.started_at,
            self.ended_at,
            self.mode,
            self.total_records_written,
            self.client_version,
        )
