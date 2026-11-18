"""
Database schema DDL, index definitions, and migration engine.
"""

import sqlite3
from typing import List, Tuple

CURRENT_SCHEMA_VERSION = 1

SCHEMA_V1_STATEMENTS: List[str] = [
    # 0. Schema version tracking
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        description TEXT NOT NULL
    );
    """,
    # 1. Trains registry
    """
    CREATE TABLE IF NOT EXISTS trains (
        train_number TEXT PRIMARY KEY,
        locomotive_number TEXT,
        default_name TEXT,
        train_type TEXT,
        service_type TEXT,
        first_observed_at TIMESTAMP NOT NULL,
        last_observed_at TIMESTAMP NOT NULL
    );
    """,
    # 2. Scheduled service trips
    """
    CREATE TABLE IF NOT EXISTS scheduled_trips (
        schedule_id TEXT PRIMARY KEY,
        train_number TEXT NOT NULL,
        origin TEXT,
        destination TEXT,
        corridor TEXT,
        service_date TEXT NOT NULL,
        scheduled_departure TIMESTAMP,
        scheduled_arrival TIMESTAMP,
        raw_json TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (train_number) REFERENCES trains(train_number) ON DELETE CASCADE
    );
    """,
    # 3. Intermediate timetable stops
    """
    CREATE TABLE IF NOT EXISTS schedule_stops (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        schedule_id TEXT NOT NULL,
        stop_sequence INTEGER NOT NULL,
        station_id TEXT,
        station_name TEXT NOT NULL,
        scheduled_arrival TIMESTAMP,
        scheduled_departure TIMESTAMP,
        actual_arrival TIMESTAMP,
        actual_departure TIMESTAMP,
        platform TEXT,
        delay_sec INTEGER DEFAULT 0,
        FOREIGN KEY (schedule_id) REFERENCES scheduled_trips(schedule_id) ON DELETE CASCADE,
        UNIQUE(schedule_id, stop_sequence)
    );
    """,
    # 4. Telemetry history (Time-series)
    """
    CREATE TABLE IF NOT EXISTS telemetry_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        train_number TEXT NOT NULL,
        schedule_id TEXT,
        recorded_at TIMESTAMP NOT NULL,
        lat REAL NOT NULL,
        lng REAL NOT NULL,
        speed REAL NOT NULL,
        course REAL NOT NULL,
        track_heading REAL,
        altitude REAL,
        delay_min INTEGER DEFAULT 0,
        status TEXT,
        progress REAL,
        chainage_m INTEGER,
        dist_to_dest_m INTEGER,
        next_station TEXT,
        corridor TEXT,
        FOREIGN KEY (train_number) REFERENCES trains(train_number),
        FOREIGN KEY (schedule_id) REFERENCES scheduled_trips(schedule_id)
    );
    """,
    # 5. Recording session audit log
    """
    CREATE TABLE IF NOT EXISTS recording_sessions (
        session_id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at TIMESTAMP NOT NULL,
        ended_at TIMESTAMP,
        mode TEXT NOT NULL,
        total_records_written INTEGER DEFAULT 0,
        client_version TEXT
    );
    """,
    # Composite B-tree indexes
    "CREATE INDEX IF NOT EXISTS idx_telemetry_train_time ON telemetry_history (train_number, recorded_at);",
    "CREATE INDEX IF NOT EXISTS idx_telemetry_schedule_time ON telemetry_history (schedule_id, recorded_at);",
    "CREATE INDEX IF NOT EXISTS idx_telemetry_time ON telemetry_history (recorded_at);",
    "CREATE INDEX IF NOT EXISTS idx_telemetry_corridor_time ON telemetry_history (corridor, recorded_at);",
    "CREATE INDEX IF NOT EXISTS idx_trips_date ON scheduled_trips (service_date, train_number);",
]


class SchemaManager:
    """Manages schema creation, version checking, and database migrations."""

    @classmethod
    def initialize_schema(cls, conn: sqlite3.Connection) -> int:
        """Apply pending migrations up to CURRENT_SCHEMA_VERSION.

        Returns:
            The schema version of the database after initialization/migration.
        """
        conn.execute("PRAGMA foreign_keys = ON;")
        current_v = cls.get_schema_version(conn)

        if current_v == 0:
            with conn:
                for statement in SCHEMA_V1_STATEMENTS:
                    conn.execute(statement)
                conn.execute(
                    "INSERT INTO schema_migrations (version, description) VALUES (?, ?);",
                    (1, "Initial V1 schema setup"),
                )
            return 1

        return current_v

    @classmethod
    def get_schema_version(cls, conn: sqlite3.Connection) -> int:
        """Query current applied schema version."""
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations';"
            )
            if not cursor.fetchone():
                return 0
            cursor.execute("SELECT MAX(version) FROM schema_migrations;")
            res = cursor.fetchone()
            return int(res[0]) if res and res[0] is not None else 0
        finally:
            cursor.close()

    @classmethod
    def verify_integrity(cls, conn: sqlite3.Connection) -> Tuple[bool, List[str]]:
        """Run PRAGMA integrity_check and foreign_key_check.

        Returns:
            Tuple of (is_healthy, list_of_error_strings).
        """
        errors: List[str] = []
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA integrity_check;")
            rows = cursor.fetchall()
            for r in rows:
                if r[0] != "ok":
                    errors.append(f"Integrity check failure: {r[0]}")

            cursor.execute("PRAGMA foreign_key_check;")
            fk_rows = cursor.fetchall()
            for fk in fk_rows:
                errors.append(
                    f"Foreign key violation in table {fk[0]} rowid {fk[1]} referencing {fk[2]}"
                )

            return len(errors) == 0, errors
        finally:
            cursor.close()
