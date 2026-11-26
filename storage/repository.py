"""
Historical data repository and analytical query engine for Hellenic Railways telemetry.
"""

import csv
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import PurePath
import sqlite3
import statistics
import threading
from typing import Any, Dict, Generator, List, Optional, Sequence, TextIO, Tuple, Union

from storage.db import Database
from storage.models import (
    BottleneckNode,
    BottleneckReport,
    BottleneckSeverity,
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    ReplayBounds,
    ReplayObservation,
    SegmentTransitReport,
    SegmentTransitStats,
    SpeedProfilePoint,
    StationDwellRecord,
    TelemetryHistoryRow,
    TripSummary,
    canonicalize_iso_utc,
    format_utc_iso,
    iso_utc_now,
    normalize_end_bound,
    normalize_start_bound,
    parse_iso_utc_datetime,
)
from train_tracker import TrainPosition

logger = logging.getLogger("railmon." + __name__)

MIN_SEGMENT_TRANSIT_SEC: float = 60.0       # Discard ~2s flip ticks, trailing flips, and GPS bounces
MAX_SEGMENT_TRANSIT_SEC: float = 21600.0    # 6h upper sanity ceiling for inter-station transit
NULL_SCHEDULE_TRIP_GAP_SEC: float = 3600.0  # 1h silence splits separate trips when schedule_id IS NULL
SEGMENT_CRUISE_MIN_SPEED_KMH: float = 2.0   # Exclude stationary dwell rows from segment cruise speed
DEFAULT_ARCHIVE_CHUNK_SIZE: int = 5000      # Bounded rows per archival transaction chunk
SQLITE_IN_CLAUSE_CHUNK_SIZE: int = 500      # Strictly below SQLITE_MAX_VARIABLE_NUMBER (999)


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
    """Convert a POSIX epoch timestamp in seconds to canonical 27-char UTC ISO-8601 (.ffffffZ)."""
    try:
        fval = float(epoch_sec)
        if not math.isfinite(fval):
            return iso_utc_now()
        return format_utc_iso(datetime.fromtimestamp(fval, tz=timezone.utc))
    except (ValueError, TypeError, OSError, OverflowError):
        return iso_utc_now()


def parse_iso_to_epoch(val: Any) -> float:
    """Convert an ISO-8601 timestamp string, datetime, or epoch number to UTC POSIX epoch float."""
    if val is None or isinstance(val, bool):
        return 0.0
    if isinstance(val, (int, float)):
        fval = float(val)
        return fval if math.isfinite(fval) else 0.0
    parsed = parse_iso_utc_datetime(val)
    if parsed is None:
        return 0.0
    return parsed.timestamp()


def _canon_ts_sql(col: str) -> str:
    """Return a SQLite expression normalizing legacy/canonical UTC ISO-8601 TEXT to 27-char .ffffffZ."""
    return (
        f"CASE "
        f"WHEN LENGTH({col}) = 27 AND SUBSTR({col}, 20, 1) = '.' AND SUBSTR({col}, 27, 1) = 'Z' "
        f"  THEN {col} "
        f"WHEN LENGTH({col}) = 20 AND SUBSTR({col}, 20, 1) = 'Z' "
        f"  THEN SUBSTR({col}, 1, 19) || '.000000Z' "
        f"WHEN SUBSTR({col}, 20, 1) = '.' AND SUBSTR({col}, -1, 1) = 'Z' AND LENGTH({col}) BETWEEN 22 AND 26 "
        f"  THEN SUBSTR(SUBSTR({col}, 1, LENGTH({col}) - 1) || '000000', 1, 26) || 'Z' "
        f"ELSE COALESCE(STRFTIME('%Y-%m-%dT%H:%M:%f', {col}) || '000Z', {col}) "
        f"END"
    )


def _append_recorded_at_filters(
    clauses: List[str],
    params: List[Any],
    col: str,
    start_bound: Optional[str],
    end_bound: Optional[str],
    include_start: bool = True,
    include_end: bool = True,
) -> None:
    """Append sargable coarse B-tree envelope + exact canonical timestamp predicates."""
    canon_expr = _canon_ts_sql(col)
    if start_bound is not None:
        start_canon = normalize_start_bound(start_bound)
        if start_canon:
            clauses.append(f"{col} >= ?")
            params.append(start_canon[:19])
            op = ">=" if include_start else ">"
            clauses.append(f"({canon_expr}) {op} ?")
            params.append(start_canon)
    if end_bound is not None:
        end_canon = normalize_end_bound(end_bound)
        if end_canon:
            clauses.append(f"{col} <= ?")
            params.append(f"{end_canon[:19]}Z")
            op = "<=" if include_end else "<"
            clauses.append(f"({canon_expr}) {op} ?")
            params.append(end_canon)


def _iter_chunks(
    items: Sequence[Any],
    chunk_size: int = SQLITE_IN_CLAUSE_CHUNK_SIZE,
) -> Generator[Sequence[Any], None, None]:
    """Yield bounded contiguous slices of items to stay under SQLITE_MAX_VARIABLE_NUMBER."""
    step = max(1, int(chunk_size))
    for idx in range(0, len(items), step):
        yield items[idx : idx + step]


def _compute_percentile(sorted_values: Sequence[float], fraction: float) -> float:
    """Compute linear-interpolated percentile on a non-empty sorted numeric sequence."""
    n = len(sorted_values)
    if n == 0:
        return 0.0
    if n == 1:
        return float(sorted_values[0])
    pos = max(0.0, min(float(n - 1), float(fraction) * float(n - 1)))
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return float(sorted_values[lo])
    weight = pos - float(lo)
    return float(sorted_values[lo]) + weight * (float(sorted_values[hi]) - float(sorted_values[lo]))


def _compute_archive_row_digest(payload_16: Tuple[Any, ...]) -> str:
    """Compute deterministic SHA-256 content digest for a 16-column archived telemetry payload."""
    serialized = json.dumps(payload_16, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _build_replay_observation(row: Any) -> ReplayObservation:
    """Construct a ReplayObservation and enriched TrainPosition from a joined SQLite row."""
    rec_raw = row["recorded_at"]
    rec_iso = canonicalize_iso_utc(rec_raw)
    rec_epoch = parse_iso_to_epoch(rec_raw)

    train_no = str(row["train_number"]).strip() or "Unknown"
    spd = max(0.0, float(row["speed"] if row["speed"] is not None else 0.0))
    crs = float(row["course"] if row["course"] is not None else 0.0)
    trk_hdg = float(row["track_heading"]) if row["track_heading"] is not None else crs
    alt = float(row["altitude"]) if row["altitude"] is not None else None
    dly = int(row["delay_min"]) if row["delay_min"] is not None else 0
    st = str(row["status"]) if row["status"] is not None else ("Κανονική" if spd > 5.0 else "Στάση")
    prog = float(row["progress"]) if row["progress"] is not None else None
    ch_m = int(row["chainage_m"]) if row["chainage_m"] is not None else None
    dist_m = int(row["dist_to_dest_m"]) if row["dist_to_dest_m"] is not None else None

    pos = TrainPosition(
        id=train_no,
        train_number=train_no,
        locomotive_number=str(row["locomotive_number"]) if row["locomotive_number"] is not None else None,
        name=str(row["default_name"]) if row["default_name"] is not None else train_no,
        train_type=str(row["train_type"]) if row["train_type"] is not None else None,
        service_type=str(row["service_type"]) if row["service_type"] is not None else None,
        status=st,
        origin=str(row["origin"]) if row["origin"] is not None else None,
        destination=str(row["destination"]) if row["destination"] is not None else None,
        next_station=str(row["next_station"]) if row["next_station"] is not None else None,
        speed=spd,
        course=crs,
        track_heading=trk_hdg,
        lat=float(row["lat"]),
        lng=float(row["lng"]),
        altitude=alt,
        delay=dly,
        progress=prog,
        chainage_m=ch_m,
        corridor=str(row["corridor"]) if row["corridor"] is not None else None,
        distance_to_destination_m=dist_m,
        timestamp=rec_iso,
        schedule_id=str(row["schedule_id"]) if row["schedule_id"] is not None else None,
    )
    return ReplayObservation(
        position=pos,
        recorded_at=rec_iso,
        recorded_epoch=rec_epoch,
    )


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


_CSV_FORMULA_PREFIXES: Tuple[str, ...] = ("=", "+", "-", "@", "\t", "\r")
_CSV_LEADING_TRIGGER_CHARS: Tuple[str, ...] = ("=", "+", "-", "@")


def _sanitize_csv_cell(value: Any) -> Any:
    """Neutralize spreadsheet formula injection (CWE-1236) on string CSV cells.

    Numeric types (int/float) and None are returned unchanged so negative numbers
    remain valid numeric values, while string cells beginning with formula triggers
    (=, +, -, @, tab, CR, or leading whitespace followed by =/+/-/@) are prefixed
    with a single quote.
    """
    if not isinstance(value, str) or not value:
        return value
    stripped = value.lstrip(" \t\r\n")
    if value[0] in _CSV_FORMULA_PREFIXES or (
        stripped and stripped[0] in _CSV_LEADING_TRIGGER_CHARS
    ):
        return f"'{value}"
    return value


def _sanitize_csv_row(row: Sequence[Any]) -> List[Any]:
    """Sanitize every cell in a CSV output row against formula injection."""
    return [_sanitize_csv_cell(cell) for cell in row]


def export_in_memory_csv(
    record: Any,
    target: Union[str, os.PathLike[str], TextIO],
) -> int:
    """
    Export an in-memory TrainRecord trajectory directly to a CSV file or file-like stream.

    Writes the same 17-column telemetry CSV format as HistoricalRepository.export_csv('telemetry').

    Args:
        record: TrainRecord containing .trail and .position.
        target: Destination file path string/PathLike or open TextIO stream.

    Returns:
        Number of data rows exported.
    """
    if isinstance(target, (str, os.PathLike)):
        clean_target = validate_export_path(os.fspath(target))
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
            _sanitize_csv_row(
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
        )
    return len(points)


class HistoricalRepository:
    """
    High-level query API for historical telemetry, timetables, and performance analytics.
    Thread-safe for concurrent read execution under SQLite WAL mode.
    """

    def __init__(self, db: Database) -> None:
        self.db = db
        self._segment_stats_cache: Dict[str, Tuple[float, Dict[Tuple[str, str], SegmentTransitStats]]] = {}

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

        canon_expr = _canon_ts_sql("recorded_at")
        where_clauses: List[str] = ["train_number = ?"]
        params: List[Any] = [train_number]
        _append_recorded_at_filters(
            where_clauses,
            params,
            col="recorded_at",
            start_bound=start_time,
            end_bound=end_time,
            include_start=True,
            include_end=True,
        )

        query_parts = [
            "SELECT id, train_number, schedule_id, recorded_at, lat, lng, speed, course,",
            "       track_heading, altitude, delay_min, status, progress, chainage_m,",
            "       dist_to_dest_m, next_station, corridor",
            "FROM telemetry_history",
            "WHERE " + " AND ".join(where_clauses),
            f"ORDER BY ({canon_expr}) {order_upper}, id {order_upper}",
        ]

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
        order: str = "ASC",
        limit: Optional[int] = None,
    ) -> Generator[TelemetryHistoryRow, None, None]:
        """
        Stream large telemetry sequences using a memory-efficient cursor generator.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        order_upper = order.upper()
        if order_upper not in ("ASC", "DESC"):
            raise ValueError("Order must be 'ASC' or 'DESC'")
        if limit is not None and limit < 0:
            raise ValueError("Limit must be non-negative")

        canon_expr = _canon_ts_sql("recorded_at")
        where_clauses: List[str] = ["train_number = ?"]
        params: List[Any] = [train_number]
        _append_recorded_at_filters(
            where_clauses,
            params,
            col="recorded_at",
            start_bound=start_time,
            end_bound=end_time,
            include_start=True,
            include_end=True,
        )

        limit_sql = ""
        if limit is not None:
            limit_sql = " LIMIT ?"
            params.append(limit)

        sql = (
            "SELECT id, train_number, schedule_id, recorded_at, lat, lng, speed, course, "
            "       track_heading, altitude, delay_min, status, progress, chainage_m, "
            "       dist_to_dest_m, next_station, corridor "
            "FROM telemetry_history "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY ({canon_expr}) {order_upper}, id {order_upper}{limit_sql};"
        )

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

    def get_replay_bounds(
        self,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Optional[ReplayBounds]:
        """
        Resolve earliest and latest telemetry timestamps and observation counts for DVR replay.

        Args:
            start_time: Optional ISO-8601 lower bound filter.
            end_time: Optional ISO-8601 upper bound filter.

        Returns:
            ReplayBounds if the database contains matching telemetry rows, else None.
        """
        canon_expr = _canon_ts_sql("recorded_at")
        where_clauses: List[str] = []
        params: List[Any] = []
        _append_recorded_at_filters(
            where_clauses,
            params,
            col="recorded_at",
            start_bound=start_time,
            end_bound=end_time,
            include_start=True,
            include_end=True,
        )
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
        sql = (
            "SELECT "
            f"MIN({canon_expr}) AS min_ts, "
            f"MAX({canon_expr}) AS max_ts, "
            "COUNT(*) AS total_rows, "
            "COUNT(DISTINCT train_number) AS total_trains "
            "FROM telemetry_history "
            f"{where_sql};"
        )
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                row = cur.fetchone()
            finally:
                cur.close()

        if row is None or not row["total_rows"] or row["min_ts"] is None or row["max_ts"] is None:
            return None

        min_iso = self._iso_or_str(row["min_ts"])
        max_iso = self._iso_or_str(row["max_ts"])
        min_ep = parse_iso_to_epoch(min_iso)
        max_ep = max(min_ep, parse_iso_to_epoch(max_iso))

        return ReplayBounds(
            min_recorded_at=min_iso,
            max_recorded_at=max_iso,
            min_epoch=min_ep,
            max_epoch=max_ep,
            total_rows=int(row["total_rows"]),
            total_trains=int(row["total_trains"] or 0),
        )

    def get_replay_observations(
        self,
        start_time: str,
        end_time: str,
        include_start: bool = False,
    ) -> List[ReplayObservation]:
        """
        Retrieve chronologically ordered telemetry observations within a replay time window,
        enriched with vehicle metadata from `trains` and route endpoints from `scheduled_trips`.

        Args:
            start_time: Window start ISO-8601 UTC timestamp.
            end_time: Window end ISO-8601 UTC timestamp (inclusive).
            include_start: If True, uses `recorded_at >= start_time`; else `recorded_at > start_time`.

        Returns:
            List of ReplayObservation objects ordered by (canonical recorded_at ASC, id ASC).
        """
        canon_expr = _canon_ts_sql("th.recorded_at")
        where_clauses: List[str] = []
        params: List[Any] = []
        _append_recorded_at_filters(
            where_clauses,
            params,
            col="th.recorded_at",
            start_bound=start_time,
            end_bound=end_time,
            include_start=include_start,
            include_end=True,
        )
        where_sql = " AND ".join(where_clauses)
        sql = (
            "SELECT "
            "th.id, th.train_number, th.schedule_id, th.recorded_at, th.lat, th.lng, "
            "th.speed, th.course, th.track_heading, th.altitude, th.delay_min, th.status, "
            "th.progress, th.chainage_m, th.dist_to_dest_m, th.next_station, "
            "COALESCE(th.corridor, st.corridor) AS corridor, "
            "t.locomotive_number, t.default_name, t.train_type, t.service_type, "
            "st.origin, st.destination "
            "FROM telemetry_history th "
            "LEFT JOIN trains t ON th.train_number = t.train_number "
            "LEFT JOIN scheduled_trips st ON th.schedule_id = st.schedule_id "
            f"WHERE {where_sql} "
            f"ORDER BY ({canon_expr}) ASC, th.id ASC;"
        )
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(sql, params)
                return [_build_replay_observation(r) for r in cur.fetchall()]
            finally:
                cur.close()

    def get_replay_snapshot_at(
        self,
        target_epoch: float,
        lookback_sec: float = 900.0,
    ) -> List[ReplayObservation]:
        """
        Retrieve all telemetry observations within [target_epoch - lookback_sec, target_epoch]
        to reconstruct point-in-time train positions and breadcrumb trails after a timeline seek.

        Args:
            target_epoch: Target virtual clock UTC epoch timestamp in seconds.
            lookback_sec: Lookback window in seconds (default: 900.0s / 15m to cover deadband heartbeats).

        Returns:
            Chronological list of ReplayObservation instances up to target_epoch.
        """
        safe_lookback = max(1.0, float(lookback_sec))
        start_iso = _epoch_to_iso_utc(float(target_epoch) - safe_lookback)
        end_iso = _epoch_to_iso_utc(float(target_epoch))
        return self.get_replay_observations(start_iso, end_iso, include_start=True)

    def _fetch_gtfs_schedules(
        self,
        schedule_ids: List[str],
    ) -> Dict[str, Dict[str, Any]]:
        """Load scheduled_trips and schedule_stops metadata for GTFS-RT TripUpdates."""
        if not schedule_ids:
            return {}
        schedules: Dict[str, Dict[str, Any]] = {}

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                for id_chunk in _iter_chunks(schedule_ids, SQLITE_IN_CLAUSE_CHUNK_SIZE):
                    placeholders = ",".join("?" for _ in id_chunk)
                    cur.execute(
                        f"SELECT schedule_id, train_number, origin, destination, corridor, "
                        f"scheduled_departure, scheduled_arrival "
                        f"FROM scheduled_trips WHERE schedule_id IN ({placeholders});",
                        tuple(id_chunk),
                    )
                    for r in cur.fetchall():
                        sid = str(r["schedule_id"])
                        schedules[sid] = {
                            "scheduleId": sid,
                            "trainNumber": str(r["train_number"]),
                            "origin": r["origin"],
                            "destination": r["destination"],
                            "corridor": r["corridor"],
                            "scheduledDeparture": self._iso_or_raw(r["scheduled_departure"]),
                            "scheduledArrival": self._iso_or_raw(r["scheduled_arrival"]),
                            "stops": [],
                        }

                    cur.execute(
                        f"SELECT schedule_id, stop_sequence, station_id, station_name, "
                        f"scheduled_arrival, scheduled_departure, actual_arrival, "
                        f"actual_departure, platform, delay_sec "
                        f"FROM schedule_stops WHERE schedule_id IN ({placeholders}) "
                        f"ORDER BY schedule_id ASC, stop_sequence ASC;",
                        tuple(id_chunk),
                    )
                    for s in cur.fetchall():
                        sid = str(s["schedule_id"])
                        if sid not in schedules:
                            schedules[sid] = {"scheduleId": sid, "stops": []}
                        schedules[sid]["stops"].append(
                            {
                                "stop_sequence": int(s["stop_sequence"]),
                                "station_id": s["station_id"],
                                "station_name": str(s["station_name"]),
                                "scheduled_arrival": self._iso_or_raw(s["scheduled_arrival"]),
                                "scheduled_departure": self._iso_or_raw(s["scheduled_departure"]),
                                "actual_arrival": self._iso_or_raw(s["actual_arrival"]),
                                "actual_departure": self._iso_or_raw(s["actual_departure"]),
                                "platform": s["platform"],
                                "delay_sec": int(s["delay_sec"] or 0),
                            }
                        )
            finally:
                cur.close()
        return schedules

    def get_gtfs_snapshot(
        self,
        at_time: Optional[str] = None,
        window_sec: int = 120,
    ) -> Tuple[List[TrainPosition], Dict[str, Dict[str, Any]], float]:
        """Reconstruct a point-in-time fleet snapshot and timetables for GTFS-RT export.

        Args:
            at_time: Optional ISO-8601 UTC target timestamp (defaults to latest recorded row).
            window_sec: Lookback window in seconds to capture active trains (default: 120).

        Returns:
            Tuple of (latest_positions, schedules_by_id, snapshot_epoch).
        """
        if window_sec <= 0:
            raise ValueError(f"window_sec must be > 0, got {window_sec}")

        if at_time is not None and at_time.strip():
            target_epoch = parse_iso_to_epoch(at_time.strip())
            if target_epoch <= 0.0:
                raise ValueError(f"Invalid ISO-8601 timestamp for --at: '{at_time}'")
        else:
            bounds = self.get_replay_bounds()
            if bounds is None:
                return [], {}, 0.0
            target_epoch = bounds.max_epoch

        observations = self.get_replay_snapshot_at(
            target_epoch=target_epoch,
            lookback_sec=float(window_sec),
        )
        if not observations:
            return [], {}, target_epoch

        latest_by_train: Dict[str, TrainPosition] = {}
        for obs in observations:
            latest_by_train[obs.position.train_number] = obs.position

        positions = list(latest_by_train.values())
        sched_ids = sorted({p.schedule_id for p in positions if p.schedule_id})
        schedules = self._fetch_gtfs_schedules(sched_ids)
        return positions, schedules, target_epoch

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

        where_clauses: List[str] = []
        params: List[Any] = []
        if service_date is not None and service_date.strip():
            day_str = service_date.strip()
            _append_recorded_at_filters(
                where_clauses,
                params,
                col="th.recorded_at",
                start_bound=normalize_start_bound(day_str),
                end_bound=normalize_end_bound(day_str),
                include_start=True,
                include_end=True,
            )
        if corridor is not None and corridor.strip():
            where_clauses.append("th.corridor = ?")
            params.append(corridor.strip())

        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        query = f"""
        SELECT 
            th.train_number,
            th.schedule_id,
            SUBSTR(th.recorded_at, 1, 10) AS service_date,
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
        {where_sql}
        GROUP BY th.train_number, SUBSTR(th.recorded_at, 1, 10), th.corridor
        ORDER BY first_seen DESC
        LIMIT ?;
        """
        params.append(limit)
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(query, params)
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
            start_date: ISO date 'YYYY-MM-DD' or timestamp lower bound (inclusive).
            end_date: ISO date 'YYYY-MM-DD' or timestamp upper bound (inclusive through 23:59:59.999999Z).
            on_time_threshold_min: Max minutes of delay considered 'on-time' (default: 5).

        Returns:
            List of CorridorPunctualitySummary records sorted by punctuality percentage.
        """
        where_clauses: List[str] = ["corridor IS NOT NULL"]
        where_params: List[Any] = []
        if corridor is not None:
            where_clauses.append("corridor = ?")
            where_params.append(corridor)
        _append_recorded_at_filters(
            where_clauses,
            where_params,
            col="recorded_at",
            start_bound=start_date,
            end_bound=end_date,
            include_start=True,
            include_end=True,
        )

        query = (
            "SELECT "
            "corridor, "
            "COUNT(DISTINCT train_number) AS total_trips, "
            "COUNT(*) AS total_observations, "
            "ROUND(AVG(delay_min), 1) AS avg_delay_min, "
            "MAX(delay_min) AS max_delay_min, "
            "SUM(CASE WHEN delay_min <= ? THEN 1 ELSE 0 END) AS on_time_count, "
            "SUM(CASE WHEN delay_min > ? THEN 1 ELSE 0 END) AS delayed_count, "
            "SUM(CASE WHEN delay_min >= 30 THEN 1 ELSE 0 END) AS severe_delay_count, "
            "ROUND(100.0 * SUM(CASE WHEN delay_min <= ? THEN 1 ELSE 0 END) / COUNT(*), 1) AS punctuality_rate_pct "
            "FROM telemetry_history "
            f"WHERE {' AND '.join(where_clauses)} "
            "GROUP BY corridor "
            "ORDER BY punctuality_rate_pct DESC, avg_delay_min ASC;"
        )
        params: List[Any] = [
            on_time_threshold_min,
            on_time_threshold_min,
            on_time_threshold_min,
            *where_params,
        ]

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
        canon_expr = _canon_ts_sql("recorded_at")
        where_clauses: List[str] = ["train_number = ?"]
        params: List[Any] = [train_number]
        _append_recorded_at_filters(
            where_clauses,
            params,
            col="recorded_at",
            start_bound=start_time,
            end_bound=end_time,
            include_start=True,
            include_end=True,
        )

        sql = (
            "SELECT recorded_at, speed, lat, lng, chainage_m, dist_to_dest_m, altitude "
            "FROM telemetry_history "
            f"WHERE {' AND '.join(where_clauses)} "
            f"ORDER BY ({canon_expr}) ASC, id ASC;"
        )

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
        canon_expr = _canon_ts_sql("recorded_at")
        where_clauses: List[str] = ["train_number = ?"]
        where_params: List[Any] = [train_number]
        _append_recorded_at_filters(
            where_clauses,
            where_params,
            col="recorded_at",
            start_bound=start_time,
            end_bound=end_time,
            include_start=True,
            include_end=True,
        )
        where_sql = " AND ".join(where_clauses)
        query = f"""
        WITH base AS (
            SELECT
                id,
                train_number,
                schedule_id,
                next_station AS station_name,
                ({canon_expr}) AS canon_ts,
                speed,
                COALESCE(delay_min, 0) AS delay_min
            FROM telemetry_history
            WHERE {where_sql}
        ),
        annotated AS (
            SELECT
                id,
                train_number,
                schedule_id,
                station_name,
                canon_ts AS recorded_at,
                delay_min,
                CASE WHEN speed <= ? AND station_name IS NOT NULL THEN 1 ELSE 0 END AS is_stopped,
                LAG(CASE WHEN speed <= ? AND station_name IS NOT NULL THEN 1 ELSE 0 END, 1, 0)
                    OVER (ORDER BY canon_ts ASC, id ASC) AS prev_stopped,
                LAG(station_name) OVER (ORDER BY canon_ts ASC, id ASC) AS prev_station,
                LAG(schedule_id) OVER (ORDER BY canon_ts ASC, id ASC) AS prev_schedule,
                LAG(canon_ts) OVER (ORDER BY canon_ts ASC, id ASC) AS prev_recorded_at
            FROM base
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
        params: List[Any] = [
            *where_params,
            speed_threshold_kmh,
            speed_threshold_kmh,
            min_dwell_sec,
        ]

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(query, params)
                return [StationDwellRecord.from_db_row(row) for row in cur.fetchall()]
            finally:
                cur.close()

    @staticmethod
    def _build_analytics_filters(
        corridor: Optional[str],
        start_ts: Optional[str],
        end_ts: Optional[str],
        train_type: Optional[str],
    ) -> Tuple[str, List[Any]]:
        """Build dynamic sargable WHERE clause and bound params for corridor analytics queries."""
        clauses: List[str] = ["1 = 1"]
        params: List[Any] = []
        if corridor:
            corr_like = f"%{corridor}%"
            clauses.append("(st.corridor LIKE ? OR th.corridor LIKE ?)")
            params.extend([corr_like, corr_like])
        _append_recorded_at_filters(
            clauses,
            params,
            col="th.recorded_at",
            start_bound=start_ts,
            end_bound=end_ts,
            include_start=True,
            include_end=True,
        )
        if train_type:
            type_like = f"%{train_type}%"
            clauses.append("(t.train_type LIKE ? OR t.service_type LIKE ?)")
            params.extend([type_like, type_like])
        return " AND ".join(clauses), params

    def get_corridor_bottlenecks(
        self,
        corridor: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        train_type: Optional[str] = None,
        min_trips: int = 3,
        limit: int = 20,
    ) -> BottleneckReport:
        """
        Aggregate and rank corridor bottlenecks across stations and track segments.

        Evaluates station dwell times, delay deltas (delay accumulated while stopped or in transit),
        and maximum delays, normalizing them into a 0.0-100.0 Bottleneck Index.

        Args:
            corridor: Optional corridor filter (e.g. 'PATHE', 'Proastiakos').
            start_date: Optional start date/timestamp ISO string filter.
            end_date: Optional end date/timestamp ISO string filter.
            train_type: Optional train type filter ('intercity', 'suburban', etc.).
            min_trips: Minimum evaluated trips per node to exclude statistical outliers.
            limit: Maximum bottleneck entries to return.

        Returns:
            BottleneckReport containing metadata and ranked BottleneckNode entries.
        """
        start_ts = normalize_start_bound(start_date)
        end_ts = normalize_end_bound(end_date)
        where_sql, base_params = self._build_analytics_filters(
            corridor=corridor,
            start_ts=start_ts,
            end_ts=end_ts,
            train_type=train_type,
        )
        canon_expr = _canon_ts_sql("th.recorded_at")

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                # 1. Total trips and observations analyzed
                count_query = f"""
                SELECT
                    COUNT(DISTINCT th.train_number || '_' || COALESCE(th.schedule_id, '')) AS total_trips,
                    COUNT(th.id) AS total_obs
                FROM telemetry_history th
                LEFT JOIN scheduled_trips st ON th.schedule_id = st.schedule_id
                LEFT JOIN trains t ON th.train_number = t.train_number
                WHERE {where_sql};
                """
                cur.execute(count_query, base_params)
                cnt_row = cur.fetchone()
                total_trips = cnt_row[0] if cnt_row else 0
                total_obs = cnt_row[1] if cnt_row else 0

                if total_obs == 0:
                    return BottleneckReport(
                        corridor=corridor,
                        time_window_start=start_ts,
                        time_window_end=end_ts,
                        total_trips_analyzed=0,
                        total_observations_analyzed=0,
                        nodes=(),
                    )

                # 2. Extract dwell events and delay deltas per station node
                # Island clustering on stopped intervals (speed <= 2.0 km/h)
                dwell_query = f"""
                WITH base_rows AS (
                    SELECT
                        th.id,
                        th.train_number,
                        th.schedule_id,
                        th.next_station AS station_name,
                        ({canon_expr}) AS canon_ts,
                        th.speed,
                        COALESCE(th.delay_min, 0) AS delay_min
                    FROM telemetry_history th
                    LEFT JOIN scheduled_trips st ON th.schedule_id = st.schedule_id
                    LEFT JOIN trains t ON th.train_number = t.train_number
                    WHERE {where_sql}
                ),
                annotated AS (
                    SELECT
                        id,
                        train_number,
                        schedule_id,
                        station_name,
                        canon_ts AS recorded_at,
                        delay_min,
                        CASE WHEN speed <= 2.0 AND station_name IS NOT NULL AND station_name != '' THEN 1 ELSE 0 END AS is_stopped,
                        LAG(CASE WHEN speed <= 2.0 AND station_name IS NOT NULL AND station_name != '' THEN 1 ELSE 0 END, 1, 0)
                            OVER (PARTITION BY train_number, schedule_id ORDER BY canon_ts ASC, id ASC) AS prev_stopped,
                        LAG(station_name) OVER (PARTITION BY train_number, schedule_id ORDER BY canon_ts ASC, id ASC) AS prev_station,
                        LAG(canon_ts) OVER (PARTITION BY train_number, schedule_id ORDER BY canon_ts ASC, id ASC) AS prev_recorded_at
                    FROM base_rows
                ),
                islands AS (
                    SELECT
                        id,
                        train_number,
                        schedule_id,
                        station_name,
                        recorded_at,
                        delay_min,
                        SUM(
                            CASE
                                WHEN prev_stopped = 0
                                  OR prev_station IS NOT station_name
                                  OR (JULIANDAY(recorded_at) - JULIANDAY(prev_recorded_at)) * 86400.0 > 1800.0
                                THEN 1
                                ELSE 0
                            END
                        ) OVER (PARTITION BY train_number, schedule_id ORDER BY recorded_at ASC, id ASC) AS island_id
                    FROM annotated
                    WHERE is_stopped = 1
                ),
                island_delays AS (
                    SELECT
                        train_number,
                        schedule_id,
                        station_name,
                        recorded_at,
                        island_id,
                        FIRST_VALUE(delay_min) OVER (
                            PARTITION BY train_number, schedule_id, island_id ORDER BY recorded_at ASC, id ASC
                        ) AS arrival_delay_min,
                        FIRST_VALUE(delay_min) OVER (
                            PARTITION BY train_number, schedule_id, island_id ORDER BY recorded_at DESC, id DESC
                        ) AS departure_delay_min
                    FROM islands
                ),
                dwell_events AS (
                    SELECT
                        train_number,
                        schedule_id,
                        station_name,
                        CAST(ROUND((JULIANDAY(MAX(recorded_at)) - JULIANDAY(MIN(recorded_at))) * 86400.0) AS INTEGER) AS dwell_seconds,
                        MIN(arrival_delay_min) AS arr_delay,
                        MIN(departure_delay_min) AS dep_delay,
                        MAX(0, MIN(departure_delay_min) - MIN(arrival_delay_min)) AS delay_added
                    FROM island_delays
                    GROUP BY train_number, schedule_id, station_name, island_id
                    HAVING dwell_seconds >= 20
                )
                SELECT
                    station_name,
                    dwell_seconds,
                    dep_delay,
                    delay_added
                FROM dwell_events;
                """
                cur.execute(dwell_query, base_params)
                dwell_rows = cur.fetchall()

                # Aggregate strictly over the dwell_events population (no raw-telemetry OOM
                # and guaranteed mathematical consistency: min <= median <= p90 <= max).
                station_events: Dict[str, List[Tuple[float, float, float]]] = {}
                for row in dwell_rows:
                    stn_name = str(row[0])
                    dwell_sec = float(row[1])
                    dep_dly = float(row[2])
                    dly_added = float(row[3])
                    station_events.setdefault(stn_name, []).append(
                        (dwell_sec, dep_dly, dly_added)
                    )

                candidate_nodes: List[Dict[str, Any]] = []

                for stn_name, events in station_events.items():
                    tr_cnt = len(events)
                    if tr_cnt < min_trips:
                        continue

                    dwells = [e[0] for e in events]
                    dep_delays = sorted(e[1] for e in events)
                    deltas = [e[2] for e in events]

                    avg_dwell = sum(dwells) / tr_cnt
                    avg_dly_added = sum(deltas) / tr_cnt
                    max_dly = dep_delays[-1]
                    med_dly = float(statistics.median(dep_delays))
                    p90_dly = _compute_percentile(dep_delays, 0.90)

                    # Bottleneck scoring formula:
                    # w1 = 0.50 (delay delta), w2 = 0.30 (dwell), w3 = 0.20 (max delay)
                    # Baseline references: delta=10m, dwell=300s, max=45m
                    c_delta = min(1.0, max(0.0, avg_dly_added / 10.0))
                    c_dwell = min(1.0, max(0.0, avg_dwell / 300.0))
                    c_max = min(1.0, max(0.0, max_dly / 45.0))
                    raw_score = (0.50 * c_delta + 0.30 * c_dwell + 0.20 * c_max) * 100.0
                    bottleneck_score = round(min(100.0, max(0.0, raw_score)), 1)

                    if bottleneck_score >= 85.0:
                        sev = BottleneckSeverity.CRITICAL
                    elif bottleneck_score >= 60.0:
                        sev = BottleneckSeverity.HIGH
                    elif bottleneck_score >= 25.0:
                        sev = BottleneckSeverity.MED
                    else:
                        sev = BottleneckSeverity.LOW

                    candidate_nodes.append({
                        "name": stn_name,
                        "type": "STATION",
                        "trips": tr_cnt,
                        "avg_dwell": avg_dwell,
                        "med_delay": med_dly,
                        "p90_delay": p90_dly,
                        "max_delay": max_dly,
                        "delay_delta": avg_dly_added,
                        "score": bottleneck_score,
                        "severity": sev,
                    })

                # Sort by score descending, tie-broken by max_delay descending
                candidate_nodes.sort(key=lambda x: (x["score"], x["max_delay"]), reverse=True)

                nodes_list: List[BottleneckNode] = []
                for idx, c in enumerate(candidate_nodes[:limit], start=1):
                    nodes_list.append(
                        BottleneckNode(
                            rank=idx,
                            name=c["name"],
                            node_type=c["type"],
                            total_trips=c["trips"],
                            avg_dwell_seconds=c["avg_dwell"],
                            median_delay_minutes=c["med_delay"],
                            p90_delay_minutes=c["p90_delay"],
                            max_delay_minutes=c["max_delay"],
                            delay_delta_minutes=c["delay_delta"],
                            bottleneck_score=c["score"],
                            severity=c["severity"],
                        )
                    )

                return BottleneckReport(
                    corridor=corridor,
                    time_window_start=start_ts,
                    time_window_end=end_ts,
                    total_trips_analyzed=total_trips,
                    total_observations_analyzed=total_obs,
                    nodes=tuple(nodes_list),
                )
            finally:
                cur.close()

    def _extract_segment_transition_samples(
        self,
        cur: Any,
        where_sql: str,
        base_params: Sequence[Any],
    ) -> Dict[Tuple[str, str, str], List[Dict[str, float]]]:
        """Extract contiguous next_station run-island transit samples from telemetry cursor."""
        canon_expr = _canon_ts_sql("th.recorded_at")
        segment_query = f"""
        WITH base_rows AS (
            SELECT
                th.id,
                th.train_number,
                COALESCE(th.schedule_id, '') AS sched_id,
                COALESCE(NULLIF(st.corridor, ''), NULLIF(th.corridor, ''), 'UNKNOWN') AS corr,
                TRIM(th.next_station) AS current_next,
                ({canon_expr}) AS canon_ts,
                th.speed,
                COALESCE(th.delay_min, 0) AS delay
            FROM telemetry_history th
            LEFT JOIN scheduled_trips st ON th.schedule_id = st.schedule_id
            LEFT JOIN trains t ON th.train_number = t.train_number
            WHERE {where_sql}
              AND th.next_station IS NOT NULL
              AND TRIM(th.next_station) != ''
        ),
        lagged_rows AS (
            SELECT
                id,
                train_number,
                sched_id,
                corr,
                current_next,
                canon_ts,
                speed,
                delay,
                LAG(current_next) OVER (
                    PARTITION BY train_number, sched_id
                    ORDER BY canon_ts ASC, id ASC
                ) AS prev_next,
                LAG(canon_ts) OVER (
                    PARTITION BY train_number, sched_id
                    ORDER BY canon_ts ASC, id ASC
                ) AS prev_ts
            FROM base_rows
        ),
        sessionized AS (
            SELECT
                id,
                train_number,
                sched_id,
                corr,
                current_next,
                canon_ts,
                speed,
                delay,
                SUM(
                    CASE
                        WHEN prev_ts IS NULL THEN 1
                        WHEN sched_id = ''
                          AND (JULIANDAY(canon_ts) - JULIANDAY(prev_ts)) * 86400.0 > ?
                            THEN 1
                        WHEN sched_id != ''
                          AND (JULIANDAY(canon_ts) - JULIANDAY(prev_ts)) * 86400.0 > ?
                            THEN 1
                        ELSE 0
                    END
                ) OVER (
                    PARTITION BY train_number, sched_id
                    ORDER BY canon_ts ASC, id ASC
                ) AS trip_session_id,
                SUM(
                    CASE
                        WHEN prev_ts IS NULL
                          OR prev_next IS NOT current_next
                          OR (sched_id = '' AND (JULIANDAY(canon_ts) - JULIANDAY(prev_ts)) * 86400.0 > ?)
                          OR (sched_id != '' AND (JULIANDAY(canon_ts) - JULIANDAY(prev_ts)) * 86400.0 > ?)
                        THEN 1
                        ELSE 0
                    END
                ) OVER (
                    PARTITION BY train_number, sched_id
                    ORDER BY canon_ts ASC, id ASC
                ) AS run_id
            FROM lagged_rows
        ),
        run_annotated AS (
            SELECT
                train_number,
                sched_id,
                trip_session_id,
                run_id,
                corr,
                current_next AS station_name,
                canon_ts,
                speed,
                FIRST_VALUE(delay) OVER (
                    PARTITION BY train_number, sched_id, trip_session_id, run_id
                    ORDER BY canon_ts DESC, id DESC
                ) AS end_delay
            FROM sessionized
        ),
        station_runs AS (
            SELECT
                train_number,
                sched_id,
                trip_session_id,
                run_id,
                MAX(corr) AS corr,
                station_name,
                MIN(canon_ts) AS run_start,
                MAX(canon_ts) AS run_end,
                COALESCE(AVG(CASE WHEN speed > ? THEN speed END), AVG(speed)) AS cruise_speed,
                MIN(end_delay) AS run_end_delay
            FROM run_annotated
            GROUP BY train_number, sched_id, trip_session_id, run_id, station_name
        ),
        transitions AS (
            SELECT
                LAG(station_name) OVER (
                    PARTITION BY train_number, sched_id, trip_session_id
                    ORDER BY run_id ASC
                ) AS from_stn,
                station_name AS to_stn,
                corr,
                LAG(run_end) OVER (
                    PARTITION BY train_number, sched_id, trip_session_id
                    ORDER BY run_id ASC
                ) AS transit_start,
                run_end AS transit_end,
                cruise_speed AS segment_speed,
                LAG(run_end_delay) OVER (
                    PARTITION BY train_number, sched_id, trip_session_id
                    ORDER BY run_id ASC
                ) AS start_delay,
                run_end_delay AS end_delay
            FROM station_runs
        )
        SELECT
            from_stn,
            to_stn,
            corr,
            transit_start,
            transit_end,
            segment_speed,
            start_delay,
            end_delay
        FROM transitions
        WHERE from_stn IS NOT NULL
          AND from_stn != to_stn
        ORDER BY from_stn ASC, to_stn ASC;
        """
        query_params: List[Any] = [
            *base_params,
            NULL_SCHEDULE_TRIP_GAP_SEC,
            MAX_SEGMENT_TRANSIT_SEC,
            NULL_SCHEDULE_TRIP_GAP_SEC,
            MAX_SEGMENT_TRANSIT_SEC,
            SEGMENT_CRUISE_MIN_SPEED_KMH,
        ]
        cur.execute(segment_query, query_params)
        raw_rows = cur.fetchall()

        samples: Dict[Tuple[str, str, str], List[Dict[str, float]]] = {}
        for row in raw_rows:
            f_stn = str(row[0]).strip()
            t_stn = str(row[1]).strip()
            c_name = str(row[2])
            t1 = parse_iso_utc_datetime(row[3])
            t2 = parse_iso_utc_datetime(row[4])
            if t1 is None or t2 is None:
                continue

            duration_sec = (t2 - t1).total_seconds()
            if (
                not math.isfinite(duration_sec)
                or duration_sec < MIN_SEGMENT_TRANSIT_SEC
                or duration_sec > MAX_SEGMENT_TRANSIT_SEC
            ):
                continue

            s_spd = float(row[5]) if row[5] is not None else 0.0
            d_start = float(row[6]) if row[6] is not None else 0.0
            d_end = float(row[7]) if row[7] is not None else 0.0

            key = (f_stn, t_stn, c_name)
            if key not in samples:
                samples[key] = []
            samples[key].append({
                "duration": duration_sec,
                "speed": s_spd,
                "delay_delta": d_end - d_start,
            })
        return samples

    def get_segment_transit_times(
        self,
        corridor: Optional[str] = None,
        from_station: Optional[str] = None,
        to_station: Optional[str] = None,
        train_type: Optional[str] = None,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        min_trips: int = 3,
        limit: int = 50,
    ) -> SegmentTransitReport:
        """
        Aggregate and profile empirical station-to-station track segment transit durations.

        Identifies directed transitions between contiguous reporting-station run islands along
        train journeys, computing statistical runtimes (min, median p50, 90th percentile p90,
        max), cruise average speeds, and signed delay accumulation deltas.

        Args:
            corridor: Optional corridor filter (e.g. 'PATHE', 'Proastiakos').
            from_station: Optional origin station name filter (substring/diacritic-insensitive).
            to_station: Optional destination station name filter.
            train_type: Optional train service type filter ('intercity', 'suburban', etc.).
            start_date: Optional start date/timestamp ISO string filter.
            end_date: Optional end date/timestamp ISO string filter.
            min_trips: Minimum evaluated trips per segment to exclude statistical outliers.
            limit: Maximum segment entries to return.

        Returns:
            SegmentTransitReport containing metadata and statistical SegmentTransitStats entries.
        """
        start_ts = normalize_start_bound(start_date)
        end_ts = normalize_end_bound(end_date)
        where_sql, base_params = self._build_analytics_filters(
            corridor=corridor,
            start_ts=start_ts,
            end_ts=end_ts,
            train_type=train_type,
        )

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                # 1. Total trips and observations analyzed
                count_query = f"""
                SELECT
                    COUNT(DISTINCT th.train_number || '_' || COALESCE(NULLIF(th.schedule_id, ''), SUBSTR(th.recorded_at, 1, 10))) AS total_trips,
                    COUNT(th.id) AS total_obs
                FROM telemetry_history th
                LEFT JOIN scheduled_trips st ON th.schedule_id = st.schedule_id
                LEFT JOIN trains t ON th.train_number = t.train_number
                WHERE {where_sql};
                """
                cur.execute(count_query, base_params)
                cnt_row = cur.fetchone()
                total_trips = cnt_row[0] if cnt_row else 0
                total_obs = cnt_row[1] if cnt_row else 0

                if total_obs == 0:
                    return SegmentTransitReport(
                        corridor=corridor,
                        time_window_start=start_ts,
                        time_window_end=end_ts,
                        total_trips_analyzed=0,
                        total_segments_analyzed=0,
                        segments=(),
                    )

                # 2. Extract consecutive station run-island transitions per trip
                segment_samples = self._extract_segment_transition_samples(
                    cur, where_sql, base_params
                )

                # 3. Filter by from_station, to_station, and min_trips
                candidate_segments: List[SegmentTransitStats] = []
                for (f_stn, t_stn, c_name), samples in segment_samples.items():
                    if from_station and from_station.lower() not in f_stn.lower():
                        continue
                    if to_station and to_station.lower() not in t_stn.lower():
                        continue
                    if len(samples) < min_trips:
                        continue

                    durations = sorted(s["duration"] for s in samples)
                    speeds = [s["speed"] for s in samples if s["speed"] > 0.0]
                    delay_deltas = [s["delay_delta"] for s in samples]

                    n_dur = len(durations)
                    min_dur = durations[0]
                    p50_dur = float(statistics.median(durations))
                    p90_dur = _compute_percentile(durations, 0.90)
                    max_dur = durations[-1]

                    avg_speed = sum(speeds) / len(speeds) if speeds else 80.0
                    avg_dd = sum(delay_deltas) / len(delay_deltas) if delay_deltas else 0.0

                    candidate_segments.append(
                        SegmentTransitStats(
                            from_station=f_stn,
                            to_station=t_stn,
                            corridor=c_name,
                            sample_count=n_dur,
                            min_duration_seconds=min_dur,
                            p50_duration_seconds=p50_dur,
                            p90_duration_seconds=p90_dur,
                            max_duration_seconds=max_dur,
                            avg_speed_kmh=avg_speed,
                            avg_delay_delta_minutes=avg_dd,
                        )
                    )

                candidate_segments.sort(
                    key=lambda s: (s.sample_count, s.p50_duration_seconds),
                    reverse=True,
                )

                final_segments = tuple(candidate_segments[:limit])
                return SegmentTransitReport(
                    corridor=corridor,
                    time_window_start=start_ts,
                    time_window_end=end_ts,
                    total_trips_analyzed=total_trips,
                    total_segments_analyzed=len(final_segments),
                    segments=final_segments,
                )
            finally:
                cur.close()

    def get_segment_stats_map(
        self,
        corridor: Optional[str] = None,
        ttl_seconds: float = 300.0,
    ) -> Dict[Tuple[str, str], SegmentTransitStats]:
        """
        Return a cached lookup dictionary mapping (from_station_norm, to_station_norm) to SegmentTransitStats.

        Used for low-latency (< 1ms) real-time ETA predictions inside TUI rendering cycles.

        Args:
            corridor: Optional corridor name filter.
            ttl_seconds: In-memory cache time-to-live in seconds (default: 300.0s).

        Returns:
            Dictionary of (from_station_key, to_station_key) -> SegmentTransitStats.
        """
        import time

        cache_key = corridor or "ALL"
        now = time.time()
        if cache_key in self._segment_stats_cache:
            cached_time, cached_map = self._segment_stats_cache[cache_key]
            if now - cached_time < ttl_seconds:
                return cached_map

        report = self.get_segment_transit_times(corridor=corridor, min_trips=1, limit=500)
        res_map: Dict[Tuple[str, str], SegmentTransitStats] = {}
        for s in report.segments:
            k = (s.from_station.strip().lower(), s.to_station.strip().lower())
            res_map[k] = s

        self._segment_stats_cache[cache_key] = (now, res_map)
        return res_map


    # -------------------------------------------------------------------------
    # 5. GIS & Data Export Engine
    # -------------------------------------------------------------------------

    @staticmethod
    def _safe_finite(val: Any, default: float = 0.0) -> float:
        """Return float(val) if finite, otherwise default (ensures allow_nan=False JSON)."""
        try:
            f = float(val)
        except (TypeError, ValueError):
            return default
        return f if math.isfinite(f) else default

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

        raw_rows = self.get_train_history(
            train_number=train_number,
            start_time=start_time,
            end_time=end_time,
            order="ASC",
        )
        rows = [
            r for r in raw_rows
            if math.isfinite(r.lat) and math.isfinite(r.lng)
        ]

        if not rows:
            return {"type": "FeatureCollection", "features": []}

        # Enforce uniform coordinate dimensionality (all 3D only if every row has finite altitude)
        use_3d = all(
            r.altitude is not None and math.isfinite(r.altitude)
            for r in rows
        )

        if ft_lower == "linestring":
            coordinates = [
                [r.lng, r.lat, r.altitude] if use_3d else [r.lng, r.lat]
                for r in rows
            ]
            speeds = [self._safe_finite(r.speed) for r in rows]
            if len(coordinates) == 1:
                # RFC 7946 §3.1.4 requires >= 2 positions for LineString; emit valid Point fallback
                geometry: Dict[str, Any] = {
                    "type": "Point",
                    "coordinates": coordinates[0],
                }
            else:
                geometry = {
                    "type": "LineString",
                    "coordinates": coordinates,
                }
            props: Dict[str, Any] = {
                "train_number": train_number,
                "corridor": rows[0].corridor,
                "start_time": rows[0].recorded_at,
                "end_time": rows[-1].recorded_at,
                "total_points": len(rows),
                "max_speed_kmh": max(speeds),
                "avg_speed_kmh": round(sum(speeds) / len(speeds), 1),
                "max_delay_min": max(r.delay_min for r in rows),
                "timestamps": [r.recorded_at for r in rows],
                "speeds_kmh": speeds,
                "delays_min": [r.delay_min for r in rows],
            }
            if len(coordinates) == 1:
                props["geometry_note"] = "single_point_fallback"
            feature = {
                "type": "Feature",
                "id": train_number,
                "geometry": geometry,
                "properties": props,
            }
            return {"type": "FeatureCollection", "features": [feature]}

        # Point feature collection
        features = []
        for r in rows:
            coord = [r.lng, r.lat, r.altitude] if use_3d else [r.lng, r.lat]
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
                        "speed_kmh": self._safe_finite(r.speed),
                        "course": self._safe_finite(r.course),
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
        target: Union[str, os.PathLike[str], TextIO],
        params: Optional[Dict[str, Any]] = None,
    ) -> int:
        """
        Export query results directly to a CSV file or file-like stream.

        Args:
            query_type: One of 'telemetry', 'trips', 'punctuality', 'dwell'.
            target: Destination file path string/PathLike or open TextIO stream.
            params: Query filter dictionary (e.g. {'train_number': '1597'}).

        Returns:
            Number of data rows exported.
        """
        params = params or {}
        qtype = query_type.strip().lower()

        if isinstance(target, (str, os.PathLike)):
            clean_target = validate_export_path(os.fspath(target))
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
            rows = self.iter_train_history(
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
                    _sanitize_csv_row(
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
                    _sanitize_csv_row(
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
                    _sanitize_csv_row(
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
                    _sanitize_csv_row(
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
        canon_expr = _canon_ts_sql("recorded_at")
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                # 1. Total telemetry rows, date bounds
                cur.execute(
                    f"SELECT COUNT(*), MIN({canon_expr}), MAX({canon_expr}) FROM telemetry_history;"
                )
                row = cur.fetchone()
                total_telemetry = row[0] or 0
                earliest = self._iso_or_raw(row[1])
                latest = self._iso_or_raw(row[2])

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
        """Normalize datetime or raw value to canonical 27-char UTC ISO-8601 string."""
        return canonicalize_iso_utc(val)

    @staticmethod
    def _iso_or_raw(val: Any) -> Any:
        """Normalize non-None timestamp value to canonical 27-char UTC ISO-8601 string."""
        if val is None:
            return None
        return canonicalize_iso_utc(val)

    def _collect_archive_payload(
        self,
        cutoff_clean: str,
        cutoff_date: str,
        chunk_size: int = DEFAULT_ARCHIVE_CHUNK_SIZE,
    ) -> Tuple[List[Tuple[Any, ...]], List[Tuple[Any, ...]], List[Tuple[Any, ...]], List[Tuple[Any, ...]]]:
        """Read one bounded chunk of aged telemetry rows and referenced parents from hot database."""
        canon_expr = _canon_ts_sql("recorded_at")
        where_clauses: List[str] = []
        where_params: List[Any] = []
        _append_recorded_at_filters(
            where_clauses,
            where_params,
            col="recorded_at",
            start_bound=None,
            end_bound=cutoff_clean,
            include_start=True,
            include_end=False,
        )
        where_sql = " AND ".join(where_clauses)

        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    f"""
                    SELECT
                        id, train_number, schedule_id, recorded_at, lat, lng, speed, course,
                        track_heading, altitude, delay_min, status, progress, chainage_m,
                        dist_to_dest_m, next_station, corridor, vehicle_id, recorded_epoch
                    FROM telemetry_history
                    WHERE {where_sql}
                    ORDER BY ({canon_expr}) ASC, id ASC
                    LIMIT ?;
                    """,
                    [*where_params, int(chunk_size)],
                )
                telemetry_rows = [tuple(r) for r in cur.fetchall()]
                if not telemetry_rows:
                    return [], [], [], []

                train_numbers = sorted({str(r[1]) for r in telemetry_rows if r[1] is not None})
                schedule_ids = sorted({str(r[2]) for r in telemetry_rows if r[2] is not None})
                train_rows = self._fetch_trains_for_archive(cur, train_numbers)
                trip_rows, stop_rows = self._fetch_schedules_for_archive(cur, schedule_ids)
            finally:
                cur.close()

        normalized_telemetry: List[Tuple[Any, ...]] = [
            (
                int(r[0]), r[1], r[2], self._iso_or_str(r[3]), r[4], r[5], r[6], r[7],
                r[8], r[9], r[10], r[11], r[12], r[13], r[14], r[15], r[16], r[17], r[18],
            )
            for r in telemetry_rows
        ]
        return normalized_telemetry, train_rows, trip_rows, stop_rows

    def _fetch_trains_for_archive(
        self,
        cur: Any,
        train_numbers: List[str],
    ) -> List[Tuple[Any, ...]]:
        """Fetch referenced train records for archival in bounded IN-clause chunks."""
        if not train_numbers:
            return []
        results: List[Tuple[Any, ...]] = []
        for id_chunk in _iter_chunks(train_numbers, SQLITE_IN_CLAUSE_CHUNK_SIZE):
            placeholders = ",".join("?" for _ in id_chunk)
            cur.execute(
                f"""
                SELECT
                    train_number, locomotive_number, default_name, train_type,
                    service_type, first_observed_at, last_observed_at
                FROM trains
                WHERE train_number IN ({placeholders});
                """,
                tuple(id_chunk),
            )
            for r in cur.fetchall():
                results.append(
                    (r[0], r[1], r[2], r[3], r[4], self._iso_or_str(r[5]), self._iso_or_str(r[6]))
                )
        return results

    def _fetch_schedules_for_archive(
        self,
        cur: Any,
        sorted_schedule_ids: List[str],
    ) -> Tuple[List[Tuple[Any, ...]], List[Tuple[Any, ...]]]:
        """Fetch referenced scheduled_trips and schedule_stops in bounded IN-clause chunks."""
        if not sorted_schedule_ids:
            return [], []
        trip_rows: List[Tuple[Any, ...]] = []
        stop_rows: List[Tuple[Any, ...]] = []
        for id_chunk in _iter_chunks(sorted_schedule_ids, SQLITE_IN_CLAUSE_CHUNK_SIZE):
            sched_placeholders = ",".join("?" for _ in id_chunk)
            cur.execute(
                f"""
                SELECT
                    schedule_id, train_number, origin, destination, corridor,
                    service_date, scheduled_departure, scheduled_arrival, raw_json, created_at
                FROM scheduled_trips
                WHERE schedule_id IN ({sched_placeholders});
                """,
                tuple(id_chunk),
            )
            for r in cur.fetchall():
                trip_rows.append(
                    (
                        r[0], r[1], r[2], r[3], r[4], r[5],
                        self._iso_or_raw(r[6]), self._iso_or_raw(r[7]), r[8], self._iso_or_raw(r[9]),
                    )
                )

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
                tuple(id_chunk),
            )
            for r in cur.fetchall():
                stop_rows.append(
                    (
                        r[0], r[1], r[2], r[3],
                        self._iso_or_raw(r[4]), self._iso_or_raw(r[5]),
                        self._iso_or_raw(r[6]), self._iso_or_raw(r[7]), r[8], r[9],
                    )
                )
        return trip_rows, stop_rows

    @staticmethod
    def _upsert_archive_parents(
        arch_cur: Any,
        train_rows: Sequence[Tuple[Any, ...]],
        trip_rows: Sequence[Tuple[Any, ...]],
        stop_rows: Sequence[Tuple[Any, ...]],
    ) -> None:
        """Upsert referenced trains, scheduled_trips, and schedule_stops into archive DB."""
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

    def _write_archive_chunk(
        self,
        archive_db: Database,
        normalized_telemetry: Sequence[Tuple[Any, ...]],
        train_rows: Sequence[Tuple[Any, ...]],
        trip_rows: Sequence[Tuple[Any, ...]],
        stop_rows: Sequence[Tuple[Any, ...]],
    ) -> None:
        """Persist one chunk into archive DB using provenance deduplication and fresh archive PKs."""
        with archive_db.transaction() as arch_cur:
            self._upsert_archive_parents(arch_cur, train_rows, trip_rows, stop_rows)
            arch_cur.execute(
                """
                CREATE TABLE IF NOT EXISTS archive_provenance (
                    source_row_id INTEGER NOT NULL,
                    row_digest TEXT NOT NULL,
                    PRIMARY KEY (source_row_id, row_digest)
                );
                """
            )
            new_rows: List[Tuple[Any, ...]] = []
            for full_row in normalized_telemetry:
                src_id = int(full_row[0])
                payload_16 = full_row[1:]
                digest = _compute_archive_row_digest(payload_16)
                arch_cur.execute(
                    """
                    INSERT OR IGNORE INTO archive_provenance (source_row_id, row_digest)
                    VALUES (?, ?);
                    """,
                    (src_id, digest),
                )
                if arch_cur.rowcount == 1:
                    new_rows.append(payload_16)

            if new_rows:
                arch_cur.executemany(
                    """
                    INSERT INTO telemetry_history (
                        train_number, schedule_id, recorded_at, lat, lng, speed, course,
                        track_heading, altitude, delay_min, status, progress, chainage_m,
                        dist_to_dest_m, next_station, corridor, vehicle_id, recorded_epoch
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    new_rows,
                )

    def _delete_archived_ids(self, archived_ids: Sequence[int]) -> int:
        """Evict strictly the exact archived telemetry row IDs from the hot database."""
        if not archived_ids:
            return 0
        deleted = 0
        with self.db.transaction() as hot_cur:
            for id_slice in _iter_chunks(archived_ids, SQLITE_IN_CLAUSE_CHUNK_SIZE):
                placeholders = ",".join("?" for _ in id_slice)
                hot_cur.execute(
                    f"DELETE FROM telemetry_history WHERE id IN ({placeholders});",
                    tuple(id_slice),
                )
                if hot_cur.rowcount is not None and hot_cur.rowcount > 0:
                    deleted += hot_cur.rowcount
        return deleted

    def _archive_and_prune_old_trips(self, archive_db: Database, cutoff_date: str) -> None:
        """Archive unreferenced old scheduled_trips and evict them safely from hot DB."""
        if len(cutoff_date) != 10:
            return
        with self.db.connection() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    SELECT schedule_id, train_number
                    FROM scheduled_trips
                    WHERE service_date < ?
                      AND NOT EXISTS (
                          SELECT 1 FROM telemetry_history
                          WHERE telemetry_history.schedule_id = scheduled_trips.schedule_id
                      );
                    """,
                    (cutoff_date,),
                )
                old_trips = cur.fetchall()
                if not old_trips:
                    return
                sched_ids = sorted({str(r[0]) for r in old_trips if r[0] is not None})
                train_nums = sorted({str(r[1]) for r in old_trips if r[1] is not None})
                train_rows = self._fetch_trains_for_archive(cur, train_nums)
                trip_rows, stop_rows = self._fetch_schedules_for_archive(cur, sched_ids)
            finally:
                cur.close()

        with archive_db.transaction() as arch_cur:
            self._upsert_archive_parents(arch_cur, train_rows, trip_rows, stop_rows)

        with self.db.transaction() as hot_cur:
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

    def archive_before(
        self,
        cutoff_iso: str,
        archive_path: Optional[str] = None,
        chunk_size: int = DEFAULT_ARCHIVE_CHUNK_SIZE,
        stop_event: Optional[threading.Event] = None,
    ) -> int:
        """
        Archive historical telemetry records older than cutoff_iso (exclusive)
        along with referenced trains, scheduled_trips, and schedule_stops into
        an archive SQLite database in bounded chunks before evicting strictly
        the archived row IDs from the hot database.

        Args:
            cutoff_iso: ISO-8601 UTC timestamp cutoff (e.g., '2026-11-01T00:00:00Z').
            archive_path: Target archive SQLite database path (defaults to <db_stem>_archive.db).
            chunk_size: Maximum telemetry rows archived per transaction chunk.
            stop_event: Optional cooperative cancellation event checked between chunks.

        Returns:
            Number of telemetry_history rows archived and removed from the hot database.
        """
        if not cutoff_iso or not cutoff_iso.strip():
            raise ValueError("cutoff_iso must be a non-empty ISO-8601 timestamp string")
        if chunk_size <= 0:
            raise ValueError("chunk_size must be a positive integer")

        cutoff_dt = parse_iso_utc_datetime(cutoff_iso.strip())
        if cutoff_dt is None:
            raise ValueError(f"Invalid ISO-8601 timestamp for cutoff_iso: '{cutoff_iso}'")

        cutoff_canon = format_utc_iso(cutoff_dt)
        cutoff_date = cutoff_canon[:10]
        target_archive_path = self.resolve_archive_path(archive_path)

        archive_db: Optional[Database] = None
        total_deleted = 0
        interrupted = False

        try:
            while stop_event is None or not stop_event.is_set():
                normalized_telemetry, train_rows, trip_rows, stop_rows = self._collect_archive_payload(
                    cutoff_canon,
                    cutoff_date,
                    chunk_size=chunk_size,
                )
                if not normalized_telemetry:
                    break

                if archive_db is None:
                    archive_db = Database(target_archive_path)

                self._write_archive_chunk(
                    archive_db=archive_db,
                    normalized_telemetry=normalized_telemetry,
                    train_rows=train_rows,
                    trip_rows=trip_rows,
                    stop_rows=stop_rows,
                )

                archived_ids = [int(r[0]) for r in normalized_telemetry]
                chunk_deleted = self._delete_archived_ids(archived_ids)
                total_deleted += chunk_deleted

                if chunk_deleted == 0 or len(normalized_telemetry) < chunk_size:
                    break
            else:
                interrupted = True

            if archive_db is not None:
                if not interrupted:
                    self._archive_and_prune_old_trips(archive_db, cutoff_date)
                archive_db.checkpoint_wal("TRUNCATE")
        finally:
            if archive_db is not None:
                archive_db.close()

        if total_deleted > 0:
            try:
                self.db.checkpoint_wal("PASSIVE")
            except (sqlite3.Error, OSError) as exc:
                logger.warning("Hot DB passive WAL checkpoint after archival failed: %s", exc)

        return total_deleted
