"""
storage - High-performance SQLite historical storage subsystem for Hellenic Railways telemetry.
"""

from storage.cli_formatters import (
    format_dwell_table,
    format_file_size,
    format_punctuality_table,
    format_sessions_table,
    format_stats_table,
    format_train_history_table,
    format_trips_table,
)
from storage.db import Database
from storage.filter import DeadbandConfig, DeadbandFilter
from storage.models import (
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    ScheduleStopDB,
    ScheduledTripDB,
    SpeedProfilePoint,
    StationDwellRecord,
    TelemetryHistoryRow,
    TrainRecordDB,
    TripSummary,
)
from storage.recorder import RecorderConfig, TelemetryRecorder
from storage.repository import HistoricalRepository
from storage.schema import SchemaManager

__all__ = [
    "Database",
    "SchemaManager",
    "TrainRecordDB",
    "ScheduledTripDB",
    "ScheduleStopDB",
    "TelemetryHistoryRow",
    "RecordingSessionDB",
    "TripSummary",
    "CorridorPunctualitySummary",
    "StationDwellRecord",
    "SpeedProfilePoint",
    "DatabaseStats",
    "DeadbandConfig",
    "DeadbandFilter",
    "RecorderConfig",
    "TelemetryRecorder",
    "HistoricalRepository",
    "format_file_size",
    "format_trips_table",
    "format_train_history_table",
    "format_punctuality_table",
    "format_dwell_table",
    "format_stats_table",
    "format_sessions_table",
]
