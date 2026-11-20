"""
Historical data repository and analytical query engine for Hellenic Railways telemetry.
"""

import csv
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import PurePath
from typing import Any, Dict, Generator, List, Optional, TextIO, Tuple, Union

from storage.db import Database

logger = logging.getLogger("railmon." + __name__)
from storage.models import (
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    SpeedProfilePoint,
    StationDwellRecord,
    TelemetryHistoryRow,
    TripSummary,
)


def validate_export_path(
    path: Union[str, os.PathLike],
    base_dir: Optional[str] = None,
) -> str:
    """
    Validate and sanitize an export file destination path.

    Enforces security baselines against directory traversal (e.g. '../'),
    canonicalizes paths via os.path.abspath, and verifies that the destination
    parent directory exists and is writable.

    Args:
        path: Destination file path string or PathLike object.
        base_dir: Optional base directory to restrict exports within.

    Returns:
        Canonical absolute path string.

    Raises:
        ValueError: If path is empty, contains null bytes, contains directory
                    traversal sequences, or is an existing directory.
        PermissionError: If the parent directory or existing target file is not writable.
    """
    if path is None:
        raise ValueError("Export path cannot be None")

    raw_path = str(path).strip()
    if not raw_path:
        raise ValueError("Export path cannot be empty")

    if "\0" in raw_path:
        raise ValueError("Export path contains invalid null bytes")

    # Guard against directory traversal ('..')
    normalized_parts = PurePath(raw_path.replace("\\", "/")).parts
    if ".." in normalized_parts:
        raise ValueError(f"Directory traversal ('..') prohibited in export path: {raw_path}")

    abs_path = os.path.abspath(raw_path)
    parent_dir = os.path.dirname(abs_path)

    if not os.path.exists(parent_dir):
        raise ValueError(f"Export destination directory does not exist: '{parent_dir}'")

    if not os.path.isdir(parent_dir):
        raise ValueError(f"Export parent path is not a directory: '{parent_dir}'")

    if not os.access(parent_dir, os.W_OK):
        raise PermissionError(f"Export destination directory is not writable: '{parent_dir}'")

    if os.path.exists(abs_path):
        if os.path.isdir(abs_path):
            raise ValueError(f"Export path points to an existing directory: '{abs_path}'")
        if not os.access(abs_path, os.W_OK):
            raise PermissionError(f"Target export file is not writable: '{abs_path}'")

    if base_dir is not None:
        canonical_base = os.path.realpath(os.path.abspath(base_dir))
        canonical_target = os.path.realpath(abs_path)
        if not (canonical_target == canonical_base or canonical_target.startswith(canonical_base + os.sep)):
            raise ValueError(f"Export path escapes allowed base directory: '{raw_path}'")

    return abs_path


def sanitize_train_identifier(raw_identifier: Optional[str]) -> str:
    """
    Sanitize a train number or ID into a filesystem-safe token ([A-Za-z0-9_-]+).

    Replaces whitespace and punctuation with underscores, collapses consecutive
    underscores, and falls back to 'unknown' if the input contains no valid characters.

    Args:
        raw_identifier: Raw train_number or position.id string.

    Returns:
        Filesystem-safe train identifier token (max 32 chars).
    """
    if not raw_identifier:
        return "unknown"
    cleaned_chars: List[str] = []
    for ch in str(raw_identifier).strip():
        if ch.isalnum() or ch in ("-", "_"):
            cleaned_chars.append(ch)
        else:
            cleaned_chars.append("_")
    token = "".join(cleaned_chars).strip("_")
    while "__" in token:
        token = token.replace("__", "_")
    return token[:32] if token else "unknown"


def _epoch_to_iso_utc(epoch_sec: float) -> str:
    """Convert a POSIX epoch timestamp in seconds to an ISO-8601 UTC string with 'Z' suffix."""
    try:
        return datetime.fromtimestamp(float(epoch_sec), tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OSError, OverflowError):
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _extract_in_memory_trajectory_points(record: Any) -> List[Dict[str, Any]]:
    """
    Extract chronological trajectory points from an in-memory TrainRecord's trail
    and current TrainPosition.
    """
    pos = record.position
    train_number = str(pos.train_number or pos.id or "Unknown").strip() or "Unknown"
    delay_min = int(pos.delay) if pos.delay is not None else 0
    course = float(pos.course) if pos.course is not None else 0.0
    track_heading = float(pos.track_heading) if pos.track_heading is not None else course

    points: List[Dict[str, Any]] = []
    trail = getattr(record, "trail", None) or ()
    for crumb in trail:
        points.append(
            {
                "train_number": train_number,
                "schedule_id": pos.schedule_id,
                "recorded_at": _epoch_to_iso_utc(crumb.timestamp),
                "timestamp_epoch": float(crumb.timestamp),
                "lat": float(crumb.lat),
                "lng": float(crumb.lng),
                "speed": max(0.0, float(crumb.speed if crumb.speed is not None else 0.0)),
                "course": course,
                "track_heading": track_heading,
                "altitude": pos.altitude,
                "delay_min": delay_min,
                "status": pos.status,
                "progress": pos.progress,
                "chainage_m": pos.chainage_m,
                "dist_to_dest_m": pos.distance_to_destination_m,
                "next_station": pos.next_station,
                "corridor": pos.corridor,
            }
        )

    last_updated = float(getattr(record, "last_updated", 0.0) or 0.0)
    curr_iso = pos.timestamp if (pos.timestamp and str(pos.timestamp).strip()) else _epoch_to_iso_utc(last_updated)
    curr_spd = max(0.0, float(pos.speed if pos.speed is not None else 0.0))

    if not points:
        points.append(
            {
                "train_number": train_number,
                "schedule_id": pos.schedule_id,
                "recorded_at": curr_iso,
                "timestamp_epoch": last_updated,
                "lat": float(pos.lat),
                "lng": float(pos.lng),
                "speed": curr_spd,
                "course": course,
                "track_heading": track_heading,
                "altitude": pos.altitude,
                "delay_min": delay_min,
                "status": pos.status,
                "progress": pos.progress,
                "chainage_m": pos.chainage_m,
                "dist_to_dest_m": pos.distance_to_destination_m,
                "next_station": pos.next_station,
                "corridor": pos.corridor,
            }
        )
    else:
        last_pt = points[-1]
        pos_shifted = (
            abs(last_pt["lat"] - float(pos.lat)) > 1e-6
            or abs(last_pt["lng"] - float(pos.lng)) > 1e-6
        )
        if pos_shifted:
            points.append(
                {
                    "train_number": train_number,
                    "schedule_id": pos.schedule_id,
                    "recorded_at": curr_iso,
                    "timestamp_epoch": max(last_updated, last_pt["timestamp_epoch"]),
                    "lat": float(pos.lat),
                    "lng": float(pos.lng),
                    "speed": curr_spd,
                    "course": course,
                    "track_heading": track_heading,
                    "altitude": pos.altitude,
                    "delay_min": delay_min,
                    "status": pos.status,
                    "progress": pos.progress,
                    "chainage_m": pos.chainage_m,
                    "dist_to_dest_m": pos.distance_to_destination_m,
                    "next_station": pos.next_station,
                    "corridor": pos.corridor,
                }
            )

    return points


def export_in_memory_geojson(
    record: Any,
    feature_type: str = "LineString",
) -> Dict[str, Any]:
    """
    Synthesize an RFC 7946 compliant GeoJSON FeatureCollection from an in-memory TrainRecord.

    Used by F-4.1 when exporting a train trajectory in live or --demo TUI mode without
    active SQLite recording (or before the background recorder flushes its first batch).

    Args:
        record: TrainRecord containing .trail (Deque[HistoricalBreadcrumb]) and .position (TrainPosition).
        feature_type: 'LineString' (continuous track geometry) or 'Point' (individual breadcrumbs).

    Returns:
        Dictionary matching GeoJSON FeatureCollection schema.
    """
    ft_lower = feature_type.strip().lower()
    if ft_lower not in ("linestring", "point"):
        raise ValueError(
            f"Unsupported feature_type '{feature_type}': must be 'LineString' or 'Point'"
        )

    if record is None or getattr(record, "position", None) is None:
        return {"type": "FeatureCollection", "features": []}

    pos = record.position
    train_number = str(pos.train_number or pos.id or "Unknown").strip() or "Unknown"
    points = _extract_in_memory_trajectory_points(record)
    if not points:
        return {"type": "FeatureCollection", "features": []}

    if ft_lower == "linestring":
        coordinates = [
            [pt["lng"], pt["lat"]]
            if pt["altitude"] is None
            else [pt["lng"], pt["lat"], pt["altitude"]]
            for pt in points
        ]
        # RFC 7946 Section 3.1.4 requires at least 2 positions in a LineString coordinates array
        if len(coordinates) == 1:
            coordinates = [coordinates[0], list(coordinates[0])]

        speeds = [float(pt["speed"]) for pt in points]
        delays = [int(pt["delay_min"]) for pt in points]
        timestamps = [str(pt["recorded_at"]) for pt in points]

        feature = {
            "type": "Feature",
            "id": train_number,
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates,
            },
            "properties": {
                "train_number": train_number,
                "corridor": pos.corridor,
                "origin": pos.origin,
                "destination": pos.destination,
                "next_station": pos.next_station,
                "status": pos.status,
                "source": "in_memory_tui_snapshot",
                "start_time": timestamps[0],
                "end_time": timestamps[-1],
                "total_points": len(points),
                "max_speed_kmh": max(speeds),
                "avg_speed_kmh": round(sum(speeds) / len(speeds), 1),
                "max_delay_min": max(delays),
                "timestamps": timestamps,
                "speeds_kmh": speeds,
                "delays_min": delays,
            },
        }
        return {"type": "FeatureCollection", "features": [feature]}

    features: List[Dict[str, Any]] = []
    for pt in points:
        coord = (
            [pt["lng"], pt["lat"]]
            if pt["altitude"] is None
            else [pt["lng"], pt["lat"], pt["altitude"]]
        )
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": coord,
                },
                "properties": {
                    "train_number": pt["train_number"],
                    "recorded_at": pt["recorded_at"],
                    "speed_kmh": pt["speed"],
                    "course": pt["course"],
                    "delay_min": pt["delay_min"],
                    "status": pt["status"],
                    "next_station": pt["next_station"],
                    "corridor": pt["corridor"],
                    "chainage_m": pt["chainage_m"],
                    "source": "in_memory_tui_snapshot",
                },
            }
        )
    return {"type": "FeatureCollection", "features": features}


def export_in_memory_csv(
    record: Any,
    target: Union[str, TextIO],
) -> int:
    """
    Export an in-memory TrainRecord trajectory directly to a CSV file or file-like stream.

    Writes the same 17-column telemetry CSV format as HistoricalRepository.export_csv('telemetry').

    Args:
        record: TrainRecord containing .trail and .position.
        target: Destination file path string or open TextIO stream.

    Returns:
        Number of data rows exported.
    """
    if isinstance(target, str):
        clean_target = validate_export_path(target)
        with open(clean_target, "w", newline="", encoding="utf-8") as f:
            return _write_in_memory_csv_stream(record, f)
    return _write_in_memory_csv_stream(record, target)


def _write_in_memory_csv_stream(record: Any, output: TextIO) -> int:
    """Write 17-column telemetry CSV rows for an in-memory TrainRecord to an open TextIO stream."""
    headers = [
        "id",
        "train_number",
        "schedule_id",
        "recorded_at",
        "lat",
        "lng",
        "speed",
        "course",
        "track_heading",
        "altitude",
        "delay_min",
        "status",
        "progress",
        "chainage_m",
        "dist_to_dest_m",
        "next_station",
        "corridor",
    ]
    writer = csv.writer(output)
    writer.writerow(headers)
    if record is None or getattr(record, "position", None) is None:
        return 0

    points = _extract_in_memory_trajectory_points(record)
    for idx, pt in enumerate(points, start=1):
        writer.writerow(
            [
                idx,
                pt["train_number"],
                pt["schedule_id"],
                pt["recorded_at"],
                pt["lat"],
                pt["lng"],
                pt["speed"],
                pt["course"],
                pt["track_heading"],
                pt["altitude"],
                pt["delay_min"],
                pt["status"],
                pt["progress"],
                pt["chainage_m"],
                pt["dist_to_dest_m"],
                pt["next_station"],
                pt["corridor"],
            ]
        )
    return len(points)


class HistoricalRepository:
    """
    High-level query API for historical telemetry, timetables, and performance analytics.
    Thread-safe for concurrent read execution under SQLite WAL mode.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    # -------------------------------------------------------------------------
    # 1. Telemetry Retrieval & Trajectory Replay
    # -------------------------------------------------------------------------

    def get_train_history(
        self,
        train_number: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        limit: Optional[int] = None,
        order: str = "ASC",
    ) -> List[TelemetryHistoryRow]:
        """
        Retrieve chronologically ordered telemetry breadcrumbs for a specific train.

        Args:
            train_number: Hellenic Railways train identifier (e.g. '1597', 'IC50').
            start_time: Optional ISO timestamp lower bound (inclusive).
            end_time: Optional ISO timestamp upper bound (inclusive).
            limit: Maximum number of rows to return (None for all).
            order: 'ASC' for chronological replay, 'DESC' for most recent first.

        Returns:
            List of TelemetryHistoryRow instances.
        """
        order_upper = order.strip().upper()
        if order_upper not in ("ASC", "DESC"):
            raise ValueError(f"Invalid order '{order}': must be 'ASC' or 'DESC'")

        query_parts = [
            "SELECT id, train_number, schedule_id, recorded_at, lat, lng, speed, course,",
            "       track_heading, altitude, delay_min, status, progress, chainage_m,",
            "       dist_to_dest_m, next_station, corridor",
            "FROM telemetry_history",
            "WHERE train_number = ?",
        ]
        params: List[Any] = [train_number]

        if start_time is not None:
            query_parts.append("AND recorded_at >= ?")
            params.append(start_time)

        if end_time is not None:
            query_parts.append("AND recorded_at <= ?")
            params.append(end_time)

        query_parts.append(f"ORDER BY recorded_at {order_upper}")

        if limit is not None:
            if limit < 0:
                raise ValueError("Limit must be non-negative")
            query_parts.append("LIMIT ?")
            params.append(limit)

        sql = " ".join(query_parts) + ";"

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                return [TelemetryHistoryRow.from_db_row(row) for row in cur.fetchall()]
            finally:
                cur.close()

    def iter_train_history(
        self,
        train_number: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        chunk_size: int = 2000,
    ) -> Generator[TelemetryHistoryRow, None, None]:
        """
        Stream large telemetry sequences using a memory-efficient cursor generator.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")

        query_parts = [
            "SELECT id, train_number, schedule_id, recorded_at, lat, lng, speed, course,",
            "       track_heading, altitude, delay_min, status, progress, chainage_m,",
            "       dist_to_dest_m, next_station, corridor",
            "FROM telemetry_history",
            "WHERE train_number = ?",
        ]
        params: List[Any] = [train_number]

        if start_time is not None:
            query_parts.append("AND recorded_at >= ?")
            params.append(start_time)

        if end_time is not None:
            query_parts.append("AND recorded_at <= ?")
            params.append(end_time)

        query_parts.append("ORDER BY recorded_at ASC;")
        sql = " ".join(query_parts)

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                while True:
                    rows = cur.fetchmany(chunk_size)
                    if not rows:
                        break
                    for row in rows:
                        yield TelemetryHistoryRow.from_db_row(row)
            finally:
                cur.close()

    # -------------------------------------------------------------------------
    # 2. Trip & Service Discovery
    # -------------------------------------------------------------------------

    def get_active_trips(
        self,
        service_date: Optional[str] = None,
        corridor: Optional[str] = None,
        limit: int = 100,
    ) -> List[TripSummary]:
        """
        Discover recorded train trips grouped by train number and calendar date.
        Derives operational statistics (speed, delay, start/end) directly from history.

        Args:
            service_date: ISO date string 'YYYY-MM-DD' (None returns all dates).
            corridor: Filter by specific railway corridor.
            limit: Max trips to return.
        """
        if limit < 0:
            raise ValueError("Limit must be non-negative")

        query = """
        SELECT 
            th.train_number,
            th.schedule_id,
            DATE(th.recorded_at) AS service_date,
            th.corridor,
            MIN(th.recorded_at) AS first_seen,
            MAX(th.recorded_at) AS last_seen,
            COUNT(*) AS total_points,
            MAX(th.delay_min) AS max_delay_min,
            ROUND(AVG(th.delay_min), 1) AS avg_delay_min,
            ROUND(MAX(th.speed), 1) AS max_speed_kmh,
            ROUND(AVG(th.speed), 1) AS avg_speed_kmh,
            st.origin,
            st.destination
        FROM telemetry_history th
        LEFT JOIN scheduled_trips st ON th.schedule_id = st.schedule_id
        WHERE (? IS NULL OR DATE(th.recorded_at) = ?)
          AND (? IS NULL OR th.corridor = ?)
        GROUP BY th.train_number, DATE(th.recorded_at), th.corridor
        ORDER BY first_seen DESC
        LIMIT ?;
        """
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(query, (service_date, service_date, corridor, corridor, limit))
                return [TripSummary.from_db_row(row) for row in cur.fetchall()]
            finally:
                cur.close()

    # -------------------------------------------------------------------------
    # 3. Punctuality & Performance Analytics
    # -------------------------------------------------------------------------

    def get_corridor_punctuality(
        self,
        corridor: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        on_time_threshold_min: int = 5,
    ) -> List[CorridorPunctualitySummary]:
        """
        Calculate punctuality rates and delay distributions aggregated by corridor.

        Args:
            corridor: Filter by corridor (None returns all corridors).
            start_date: ISO date 'YYYY-MM-DD' or timestamp lower bound.
            end_date: ISO date 'YYYY-MM-DD' or timestamp upper bound.
            on_time_threshold_min: Max minutes of delay considered 'on-time' (default: 5).

        Returns:
            List of CorridorPunctualitySummary records sorted by punctuality percentage.
        """
        query = """
        SELECT 
            corridor,
            COUNT(DISTINCT train_number) AS total_trips,
            COUNT(*) AS total_observations,
            ROUND(AVG(delay_min), 1) AS avg_delay_min,
            MAX(delay_min) AS max_delay_min,
            SUM(CASE WHEN delay_min <= ? THEN 1 ELSE 0 END) AS on_time_count,
            SUM(CASE WHEN delay_min > ? THEN 1 ELSE 0 END) AS delayed_count,
            SUM(CASE WHEN delay_min >= 30 THEN 1 ELSE 0 END) AS severe_delay_count,
            ROUND(100.0 * SUM(CASE WHEN delay_min <= ? THEN 1 ELSE 0 END) / COUNT(*), 1) AS punctuality_rate_pct
        FROM telemetry_history
        WHERE corridor IS NOT NULL
          AND (? IS NULL OR corridor = ?)
          AND (? IS NULL OR recorded_at >= ?)
          AND (? IS NULL OR recorded_at <= ?)
        GROUP BY corridor
        ORDER BY punctuality_rate_pct DESC, avg_delay_min ASC;
        """
        params = (
            on_time_threshold_min,
            on_time_threshold_min,
            on_time_threshold_min,
            corridor,
            corridor,
            start_date,
            start_date,
            end_date,
            end_date,
        )

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(query, params)
                return [
                    CorridorPunctualitySummary.from_db_row(
                        row, start_date=start_date, end_date=end_date
                    )
                    for row in cur.fetchall()
                ]
            finally:
                cur.close()

    # -------------------------------------------------------------------------
    # 4. Kinematic & Station Stop Profiling
    # -------------------------------------------------------------------------

    def get_speed_profile(
        self,
        train_number: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> List[SpeedProfilePoint]:
        """
        Extract speed, elevation, and chainage profile along track distance.
        """
        query_parts = [
            "SELECT recorded_at, speed, lat, lng, chainage_m, dist_to_dest_m, altitude",
            "FROM telemetry_history",
            "WHERE train_number = ?",
        ]
        params: List[Any] = [train_number]

        if start_time is not None:
            query_parts.append("AND recorded_at >= ?")
            params.append(start_time)

        if end_time is not None:
            query_parts.append("AND recorded_at <= ?")
            params.append(end_time)

        query_parts.append("ORDER BY recorded_at ASC;")
        sql = " ".join(query_parts)

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                return [SpeedProfilePoint.from_db_row(row) for row in cur.fetchall()]
            finally:
                cur.close()

    def get_station_dwell_times(
        self,
        train_number: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        speed_threshold_kmh: float = 2.0,
        min_dwell_sec: int = 20,
    ) -> List[StationDwellRecord]:
        """
        Detect station dwell events by identifying contiguous stationary intervals
        grouped by reported next_station name.
        """
        query = """
        WITH annotated AS (
            SELECT
                id,
                train_number,
                schedule_id,
                next_station AS station_name,
                recorded_at,
                COALESCE(delay_min, 0) AS delay_min,
                CASE WHEN speed <= ? AND next_station IS NOT NULL THEN 1 ELSE 0 END AS is_stopped,
                LAG(CASE WHEN speed <= ? AND next_station IS NOT NULL THEN 1 ELSE 0 END, 1, 0)
                    OVER (ORDER BY recorded_at ASC, id ASC) AS prev_stopped,
                LAG(next_station) OVER (ORDER BY recorded_at ASC, id ASC) AS prev_station,
                LAG(schedule_id) OVER (ORDER BY recorded_at ASC, id ASC) AS prev_schedule,
                LAG(recorded_at) OVER (ORDER BY recorded_at ASC, id ASC) AS prev_recorded_at
            FROM telemetry_history
            WHERE train_number = ?
              AND (? IS NULL OR recorded_at >= ?)
              AND (? IS NULL OR recorded_at <= ?)
        ),
        islands AS (
            SELECT
                id,
                train_number,
                station_name,
                recorded_at,
                delay_min,
                SUM(
                    CASE
                        WHEN prev_stopped = 0
                          OR prev_station IS NOT station_name
                          OR COALESCE(prev_schedule, '') != COALESCE(schedule_id, '')
                          OR (JULIANDAY(recorded_at) - JULIANDAY(prev_recorded_at)) * 86400.0 > 1800.0
                        THEN 1
                        ELSE 0
                    END
                ) OVER (ORDER BY recorded_at ASC, id ASC) AS island_id
            FROM annotated
            WHERE is_stopped = 1
        ),
        island_delays AS (
            SELECT
                train_number,
                station_name,
                recorded_at,
                island_id,
                FIRST_VALUE(delay_min) OVER (
                    PARTITION BY island_id ORDER BY recorded_at ASC, id ASC
                ) AS arrival_delay_min,
                FIRST_VALUE(delay_min) OVER (
                    PARTITION BY island_id ORDER BY recorded_at DESC, id DESC
                ) AS departure_delay_min
            FROM islands
        )
        SELECT
            train_number,
            station_name,
            MIN(recorded_at) AS arrival_time,
            MAX(recorded_at) AS departure_time,
            CAST(ROUND((JULIANDAY(MAX(recorded_at)) - JULIANDAY(MIN(recorded_at))) * 86400.0) AS INTEGER) AS dwell_seconds,
            MIN(arrival_delay_min) AS arrival_delay_min,
            MIN(departure_delay_min) AS departure_delay_min
        FROM island_delays
        GROUP BY train_number, station_name, island_id
        HAVING dwell_seconds >= ?
        ORDER BY arrival_time ASC;
        """
        params = (
            speed_threshold_kmh,
            speed_threshold_kmh,
            train_number,
            start_time,
            start_time,
            end_time,
            end_time,
            min_dwell_sec,
        )

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(query, params)
                return [StationDwellRecord.from_db_row(row) for row in cur.fetchall()]
            finally:
                cur.close()

    # -------------------------------------------------------------------------
    # 5. GIS & Data Export Engine
    # -------------------------------------------------------------------------

    def export_trip_geojson(
        self,
        train_number: str,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        feature_type: str = "LineString",
    ) -> Dict[str, Any]:
        """
        Export train trajectory as an RFC 7946 compliant GeoJSON FeatureCollection.

        Args:
            train_number: Train identifier.
            start_time: Optional start timestamp.
            end_time: Optional end timestamp.
            feature_type: 'LineString' (continuous track geometry) or 'Point' (individual breadcrumbs).

        Returns:
            Dictionary matching GeoJSON FeatureCollection schema.
        """
        ft_lower = feature_type.strip().lower()
        if ft_lower not in ("linestring", "point"):
            raise ValueError(
                f"Unsupported feature_type '{feature_type}': must be 'LineString' or 'Point'"
            )

        rows = self.get_train_history(
            train_number=train_number,
            start_time=start_time,
            end_time=end_time,
            order="ASC",
        )

        if not rows:
            return {"type": "FeatureCollection", "features": []}

        if ft_lower == "linestring":
            coordinates = [
                [r.lng, r.lat] if r.altitude is None else [r.lng, r.lat, r.altitude]
                for r in rows
            ]
            feature = {
                "type": "Feature",
                "id": train_number,
                "geometry": {
                    "type": "LineString",
                    "coordinates": coordinates,
                },
                "properties": {
                    "train_number": train_number,
                    "corridor": rows[0].corridor,
                    "start_time": rows[0].recorded_at,
                    "end_time": rows[-1].recorded_at,
                    "total_points": len(rows),
                    "max_speed_kmh": max(r.speed for r in rows),
                    "avg_speed_kmh": round(sum(r.speed for r in rows) / len(rows), 1),
                    "max_delay_min": max(r.delay_min for r in rows),
                    "timestamps": [r.recorded_at for r in rows],
                    "speeds_kmh": [r.speed for r in rows],
                    "delays_min": [r.delay_min for r in rows],
                },
            }
            return {"type": "FeatureCollection", "features": [feature]}

        # Point feature collection
        features = []
        for r in rows:
            coord = [r.lng, r.lat] if r.altitude is None else [r.lng, r.lat, r.altitude]
            features.append(
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": coord,
                    },
                    "properties": {
                        "train_number": r.train_number,
                        "recorded_at": r.recorded_at,
                        "speed_kmh": r.speed,
                        "course": r.course,
                        "delay_min": r.delay_min,
                        "status": r.status,
                        "next_station": r.next_station,
                        "corridor": r.corridor,
                        "chainage_m": r.chainage_m,
                    },
                }
            )

        return {"type": "FeatureCollection", "features": features}

    def export_csv(
        self,
        query_type: str,
        target: Union[str, TextIO],
        params: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Export query results directly to a CSV file or file-like stream.

        Args:
            query_type: One of 'telemetry', 'trips', 'punctuality', 'dwell'.
            target: Destination file path string or open TextIO stream.
            params: Query filter dictionary (e.g. {'train_number': '1597'}).

        Returns:
            Number of data rows exported.
        """
        params = params or {}
        qtype = query_type.strip().lower()

        if isinstance(target, str):
            clean_target = validate_export_path(target)
            with open(clean_target, "w", newline="", encoding="utf-8") as f:
                return self._write_csv_data(qtype, f, params)
        else:
            return self._write_csv_data(qtype, target, params)

    def _write_csv_data(
        self, qtype: str, output: TextIO, params: Dict[str, Any]
    ) -> int:
        writer = csv.writer(output)
        row_count = 0

        if qtype == "telemetry":
            train_number = params.get("train_number")
            if not train_number:
                raise ValueError("params['train_number'] is required for telemetry CSV export")
            rows = self.get_train_history(
                train_number=str(train_number),
                start_time=params.get("start_time"),
                end_time=params.get("end_time"),
                limit=params.get("limit"),
                order=params.get("order", "ASC"),
            )
            headers = [
                "id",
                "train_number",
                "schedule_id",
                "recorded_at",
                "lat",
                "lng",
                "speed",
                "course",
                "track_heading",
                "altitude",
                "delay_min",
                "status",
                "progress",
                "chainage_m",
                "dist_to_dest_m",
                "next_station",
                "corridor",
            ]
            writer.writerow(headers)
            for r in rows:
                writer.writerow(
                    [
                        r.id,
                        r.train_number,
                        r.schedule_id,
                        r.recorded_at,
                        r.lat,
                        r.lng,
                        r.speed,
                        r.course,
                        r.track_heading,
                        r.altitude,
                        r.delay_min,
                        r.status,
                        r.progress,
                        r.chainage_m,
                        r.dist_to_dest_m,
                        r.next_station,
                        r.corridor,
                    ]
                )
                row_count += 1

        elif qtype == "trips":
            trips = self.get_active_trips(
                service_date=params.get("service_date"),
                corridor=params.get("corridor"),
                limit=params.get("limit", 1000),
            )
            headers = [
                "train_number",
                "service_date",
                "schedule_id",
                "corridor",
                "origin",
                "destination",
                "first_seen",
                "last_seen",
                "total_points",
                "max_delay_min",
                "avg_delay_min",
                "max_speed_kmh",
                "avg_speed_kmh",
            ]
            writer.writerow(headers)
            for t in trips:
                writer.writerow(
                    [
                        t.train_number,
                        t.service_date,
                        t.schedule_id,
                        t.corridor,
                        t.origin,
                        t.destination,
                        t.first_seen,
                        t.last_seen,
                        t.total_points,
                        t.max_delay_min,
                        t.avg_delay_min,
                        t.max_speed_kmh,
                        t.avg_speed_kmh,
                    ]
                )
                row_count += 1

        elif qtype in ("punctuality", "corridor_punctuality"):
            summaries = self.get_corridor_punctuality(
                corridor=params.get("corridor"),
                start_date=params.get("start_date"),
                end_date=params.get("end_date"),
                on_time_threshold_min=params.get("on_time_threshold_min", 5),
            )
            headers = [
                "corridor",
                "total_trips",
                "total_observations",
                "avg_delay_min",
                "max_delay_min",
                "on_time_count",
                "delayed_count",
                "severe_delay_count",
                "punctuality_rate_pct",
                "start_date",
                "end_date",
            ]
            writer.writerow(headers)
            for p in summaries:
                writer.writerow(
                    [
                        p.corridor,
                        p.total_trips,
                        p.total_observations,
                        p.avg_delay_min,
                        p.max_delay_min,
                        p.on_time_count,
                        p.delayed_count,
                        p.severe_delay_count,
                        p.punctuality_rate_pct,
                        p.start_date,
                        p.end_date,
                    ]
                )
                row_count += 1

        elif qtype in ("dwell", "station_dwell"):
            train_number = params.get("train_number")
            if not train_number:
                raise ValueError("params['train_number'] is required for dwell CSV export")
            dwells = self.get_station_dwell_times(
                train_number=str(train_number),
                start_time=params.get("start_time"),
                end_time=params.get("end_time"),
                speed_threshold_kmh=params.get("speed_threshold_kmh", 2.0),
                min_dwell_sec=params.get("min_dwell_sec", 20),
            )
            headers = [
                "train_number",
                "station_name",
                "arrival_time",
                "departure_time",
                "dwell_seconds",
                "arrival_delay_min",
                "departure_delay_min",
            ]
            writer.writerow(headers)
            for d in dwells:
                writer.writerow(
                    [
                        d.train_number,
                        d.station_name,
                        d.arrival_time,
                        d.departure_time,
                        d.dwell_seconds,
                        d.arrival_delay_min,
                        d.departure_delay_min,
                    ]
                )
                row_count += 1

        else:
            raise ValueError(f"Unsupported CSV query_type '{qtype}'")

        return row_count

    # -------------------------------------------------------------------------
    # 6. Audit & System Health
    # -------------------------------------------------------------------------

    def get_recording_sessions(self, limit: int = 50) -> List[RecordingSessionDB]:
        """Retrieve recent recorder session audit logs."""
        query = """
        SELECT session_id, started_at, ended_at, mode, total_records_written, client_version
        FROM recording_sessions
        ORDER BY session_id DESC
        LIMIT ?;
        """
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(query, (limit,))
                return [RecordingSessionDB.from_db_row(row) for row in cur.fetchall()]
            finally:
                cur.close()

    def get_database_stats(self) -> DatabaseStats:
        """Calculate total rows, file size, date bounds, and storage metrics."""
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                # 1. Total telemetry rows, date bounds
                cur.execute(
                    "SELECT COUNT(*), MIN(recorded_at), MAX(recorded_at) FROM telemetry_history;"
                )
                row = cur.fetchone()
                total_telemetry = row[0] or 0
                earliest = row[1]
                if hasattr(earliest, "isoformat"):
                    earliest = earliest.isoformat()
                latest = row[2]
                if hasattr(latest, "isoformat"):
                    latest = latest.isoformat()

                # 2. Total trains
                cur.execute("SELECT COUNT(*) FROM trains;")
                total_trains = cur.fetchone()[0] or 0

                # 3. Total trips
                cur.execute("SELECT COUNT(*) FROM scheduled_trips;")
                total_trips = cur.fetchone()[0] or 0

                # 4. Total sessions
                cur.execute("SELECT COUNT(*) FROM recording_sessions;")
                total_sessions = cur.fetchone()[0] or 0

                # 5. Check WAL mode
                cur.execute("PRAGMA journal_mode;")
                journal_mode = str(cur.fetchone()[0]).lower()
                wal_enabled = journal_mode == "wal"
            finally:
                cur.close()

        file_size_bytes = self.db.get_size_bytes()

        return DatabaseStats(
            total_telemetry_rows=total_telemetry,
            total_trains=total_trains,
            total_trips=total_trips,
            total_sessions=total_sessions,
            file_size_bytes=file_size_bytes,
            earliest_record=str(earliest) if earliest is not None else None,
            latest_record=str(latest) if latest is not None else None,
            wal_mode_enabled=wal_enabled,
        )

    def resolve_archive_path(self, archive_path: Optional[str] = None) -> str:
        """Resolve and validate target SQLite archive database path."""
        if archive_path is not None and archive_path.strip():
            candidate = archive_path.strip()
            if candidate != ":memory:" and not candidate.startswith("file::memory:"):
                resolved = validate_export_path(candidate)
            else:
                resolved = candidate
        elif self.db._is_memory:
            raise ValueError("archive_path must be explicitly provided for in-memory databases")
        else:
            base, ext = os.path.splitext(self.db.db_path)
            resolved = f"{base}_archive{ext or '.db'}"

        if not self.db._is_memory and not resolved.startswith("file::memory:") and resolved != ":memory:":
            if os.path.abspath(resolved) == os.path.abspath(self.db.db_path):
                raise ValueError("archive_path cannot be identical to the active database path")
        return resolved

    @staticmethod
    def _iso_or_str(val: Any) -> str:
        """Normalize datetime or raw value to UTC ISO-8601 string with Z suffix."""
        return val.isoformat().replace("+00:00", "Z") if hasattr(val, "isoformat") else str(val)

    @staticmethod
    def _iso_or_raw(val: Any) -> Any:
        """Normalize datetime to UTC ISO-8601 string or preserve raw/None value."""
        return val.isoformat().replace("+00:00", "Z") if hasattr(val, "isoformat") else val

    def _collect_archive_payload(
        self,
        cutoff_clean: str,
        cutoff_date: str,
    ) -> Tuple[List[Any], List[Any], List[Any], List[Any]]:
        """Read aged telemetry rows and referenced parent records from hot database."""
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT
                        id, train_number, schedule_id, recorded_at, lat, lng, speed, course,
                        track_heading, altitude, delay_min, status, progress, chainage_m,
                        dist_to_dest_m, next_station, corridor
                    FROM telemetry_history
                    WHERE recorded_at < ?
                    ORDER BY recorded_at ASC, id ASC;
                    """,
                    (cutoff_clean,),
                )
                telemetry_rows = [tuple(r) for r in cur.fetchall()]
                if not telemetry_rows:
                    return [], [], [], []

                train_numbers = sorted({str(r[1]) for r in telemetry_rows if r[1] is not None})
                schedule_ids = {str(r[2]) for r in telemetry_rows if r[2] is not None}

                if len(cutoff_date) == 10:
                    cur.execute(
                        """
                        SELECT schedule_id, train_number
                        FROM scheduled_trips
                        WHERE service_date < ?;
                        """,
                        (cutoff_date,),
                    )
                    for row in cur.fetchall():
                        schedule_ids.add(str(row[0]))
                        if row[1] is not None:
                            train_numbers.append(str(row[1]))
                    train_numbers = sorted(set(train_numbers))

                train_rows = self._fetch_trains_for_archive(cur, train_numbers)
                trip_rows, stop_rows = self._fetch_schedules_for_archive(cur, sorted(schedule_ids))
            finally:
                cur.close()

        normalized_telemetry = [
            (
                r[0], r[1], r[2], self._iso_or_str(r[3]), r[4], r[5], r[6], r[7],
                r[8], r[9], r[10], r[11], r[12], r[13], r[14], r[15], r[16],
            )
            for r in telemetry_rows
        ]
        return normalized_telemetry, train_rows, trip_rows, stop_rows

    def _fetch_trains_for_archive(
        self,
        cur: Any,
        train_numbers: List[str],
    ) -> List[Any]:
        """Fetch referenced train records for archival."""
        if not train_numbers:
            return []
        placeholders = ",".join("?" for _ in train_numbers)
        cur.execute(
            f"""
            SELECT
                train_number, locomotive_number, default_name, train_type,
                service_type, first_observed_at, last_observed_at
            FROM trains
            WHERE train_number IN ({placeholders});
            """,
            train_numbers,
        )
        return [
            (r[0], r[1], r[2], r[3], r[4], self._iso_or_str(r[5]), self._iso_or_str(r[6]))
            for r in cur.fetchall()
        ]

    def _fetch_schedules_for_archive(
        self,
        cur: Any,
        sorted_schedule_ids: List[str],
    ) -> Tuple[List[Any], List[Any]]:
        """Fetch referenced scheduled_trips and schedule_stops for archival."""
        if not sorted_schedule_ids:
            return [], []
        sched_placeholders = ",".join("?" for _ in sorted_schedule_ids)
        cur.execute(
            f"""
            SELECT
                schedule_id, train_number, origin, destination, corridor,
                service_date, scheduled_departure, scheduled_arrival, raw_json, created_at
            FROM scheduled_trips
            WHERE schedule_id IN ({sched_placeholders});
            """,
            sorted_schedule_ids,
        )
        trip_rows = [
            (
                r[0], r[1], r[2], r[3], r[4], r[5],
                self._iso_or_raw(r[6]), self._iso_or_raw(r[7]), r[8], self._iso_or_raw(r[9]),
            )
            for r in cur.fetchall()
        ]

        cur.execute(
            f"""
            SELECT
                schedule_id, stop_sequence, station_id, station_name,
                scheduled_arrival, scheduled_departure, actual_arrival,
                actual_departure, platform, delay_sec
            FROM schedule_stops
            WHERE schedule_id IN ({sched_placeholders})
            ORDER BY schedule_id ASC, stop_sequence ASC;
            """,
            sorted_schedule_ids,
        )
        stop_rows = [
            (
                r[0], r[1], r[2], r[3],
                self._iso_or_raw(r[4]), self._iso_or_raw(r[5]),
                self._iso_or_raw(r[6]), self._iso_or_raw(r[7]), r[8], r[9],
            )
            for r in cur.fetchall()
        ]
        return trip_rows, stop_rows

    def archive_before(
        self,
        cutoff_iso: str,
        archive_path: Optional[str] = None,
    ) -> int:
        """
        Archive historical telemetry records older than cutoff_iso (exclusive)
        along with referenced trains, scheduled_trips, and schedule_stops into
        an archive SQLite database before evicting them from the hot database.

        Args:
            cutoff_iso: ISO-8601 UTC timestamp cutoff (e.g., '2026-11-01T00:00:00Z').
            archive_path: Target archive SQLite database path (defaults to <db_stem>_archive.db).

        Returns:
            Number of telemetry_history rows archived and removed from the hot database.
        """
        if not cutoff_iso or not cutoff_iso.strip():
            raise ValueError("cutoff_iso must be a non-empty ISO-8601 timestamp string")

        cutoff_clean = cutoff_iso.strip()
        cutoff_date = cutoff_clean[:10]
        target_archive_path = self.resolve_archive_path(archive_path)

        # 1. Read aged telemetry rows and referenced parent records from hot database
        normalized_telemetry, train_rows, trip_rows, stop_rows = self._collect_archive_payload(
            cutoff_clean, cutoff_date
        )
        if not normalized_telemetry:
            return 0

        # 2. Persist rows into Archive SQLite database first (Zero-Data-Loss guarantee)
        archive_db = Database(target_archive_path)
        try:
            with archive_db.transaction() as arch_cur:
                if train_rows:
                    arch_cur.executemany(
                        """
                        INSERT INTO trains (
                            train_number, locomotive_number, default_name, train_type, service_type,
                            first_observed_at, last_observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(train_number) DO UPDATE SET
                            locomotive_number = COALESCE(excluded.locomotive_number, trains.locomotive_number),
                            default_name = COALESCE(excluded.default_name, trains.default_name),
                            train_type = COALESCE(excluded.train_type, trains.train_type),
                            service_type = COALESCE(excluded.service_type, trains.service_type),
                            first_observed_at = MIN(trains.first_observed_at, excluded.first_observed_at),
                            last_observed_at = MAX(trains.last_observed_at, excluded.last_observed_at);
                        """,
                        train_rows,
                    )

                if trip_rows:
                    arch_cur.executemany(
                        """
                        INSERT INTO scheduled_trips (
                            schedule_id, train_number, origin, destination, corridor,
                            service_date, scheduled_departure, scheduled_arrival, raw_json, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(schedule_id) DO UPDATE SET
                            origin = COALESCE(scheduled_trips.origin, excluded.origin),
                            destination = COALESCE(scheduled_trips.destination, excluded.destination),
                            corridor = COALESCE(scheduled_trips.corridor, excluded.corridor);
                        """,
                        trip_rows,
                    )

                if stop_rows:
                    arch_cur.executemany(
                        """
                        INSERT OR IGNORE INTO schedule_stops (
                            schedule_id, stop_sequence, station_id, station_name,
                            scheduled_arrival, scheduled_departure, actual_arrival,
                            actual_departure, platform, delay_sec
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                        """,
                        stop_rows,
                    )

                arch_cur.executemany(
                    """
                    INSERT OR IGNORE INTO telemetry_history (
                        id, train_number, schedule_id, recorded_at, lat, lng, speed, course,
                        track_heading, altitude, delay_min, status, progress, chainage_m,
                        dist_to_dest_m, next_station, corridor
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    normalized_telemetry,
                )
            archive_db.checkpoint_wal("TRUNCATE")
        finally:
            archive_db.close()

        # 3. Evict archived telemetry rows and unreferenced old trips from hot database
        with self.db.transaction() as hot_cur:
            hot_cur.execute(
                "DELETE FROM telemetry_history WHERE recorded_at < ?;",
                (cutoff_clean,),
            )
            deleted_count = (
                hot_cur.rowcount
                if hot_cur.rowcount is not None and hot_cur.rowcount >= 0
                else len(normalized_telemetry)
            )

            if len(cutoff_date) == 10:
                hot_cur.execute(
                    """
                    DELETE FROM scheduled_trips
                    WHERE service_date < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM telemetry_history
                          WHERE telemetry_history.schedule_id = scheduled_trips.schedule_id
                      );
                    """,
                    (cutoff_date,),
                )

        return deleted_count
