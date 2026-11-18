"""
Historical data repository and analytical query engine for Hellenic Railways telemetry.
"""

import csv
from datetime import datetime
import json
import os
from typing import Any, Dict, Generator, List, Optional, TextIO, Union

from storage.db import Database
from storage.models import (
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    SpeedProfilePoint,
    StationDwellRecord,
    TelemetryHistoryRow,
    TripSummary,
)


class HistoricalRepository:
    """
    High-level query API for historical telemetry, timetables, and performance analytics.
    Thread-safe for concurrent read execution under SQLite WAL mode.
    """

    def __init__(self, db: Database):
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
            parent = os.path.dirname(os.path.abspath(target))
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            with open(target, "w", newline="", encoding="utf-8") as f:
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
