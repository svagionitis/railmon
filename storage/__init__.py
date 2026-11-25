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
from storage.filter import DeadbandBatchEvaluation, DeadbandConfig, DeadbandFilter
from storage.models import (
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    ReplayBounds,
    ReplayObservation,
    ScheduleStopDB,
    ScheduledTripDB,
    SpeedProfilePoint,
    StationDwellRecord,
    TelemetryHistoryRow,
    TrainRecordDB,
    TripSummary,
    canonicalize_iso_utc,
    format_utc_iso,
    iso_utc_now,
    normalize_end_bound,
    normalize_start_bound,
    parse_iso_utc_datetime,
)
from storage.recorder import RecorderConfig, TelemetryRecorder
from storage.repository import (
    HistoricalRepository,
    export_in_memory_csv,
    export_in_memory_geojson,
    parse_iso_to_epoch,
    sanitize_train_identifier,
    validate_export_path,
)
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
    "ReplayBounds",
    "ReplayObservation",
    "DeadbandConfig",
    "DeadbandBatchEvaluation",
    "DeadbandFilter",
    "format_utc_iso",
    "iso_utc_now",
    "parse_iso_utc_datetime",
    "canonicalize_iso_utc",
    "normalize_start_bound",
    "normalize_end_bound",
    "RecorderConfig",
    "TelemetryRecorder",
    "HistoricalRepository",
    "validate_export_path",
    "sanitize_train_identifier",
    "parse_iso_to_epoch",
    "export_in_memory_geojson",
    "export_in_memory_csv",
    "format_file_size",
    "format_trips_table",
    "format_train_history_table",
    "format_punctuality_table",
    "format_dwell_table",
    "format_stats_table",
    "format_sessions_table",
]
