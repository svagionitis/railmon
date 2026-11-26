"""
Unit tests for SQLite schema creation, migrations, indexes, and integrity checks.
"""

import sqlite3
import unittest
from unittest.mock import patch

from storage.schema import CURRENT_SCHEMA_VERSION, SCHEMA_V1_STATEMENTS, SchemaManager


class TestStorageSchema(unittest.TestCase):
    """Test suite for database schema DDL, foreign keys, and indexes."""

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()

    def test_schema_initialization_on_empty_db(self):
        version = SchemaManager.initialize_schema(self.conn)
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(SchemaManager.get_schema_version(self.conn), CURRENT_SCHEMA_VERSION)

        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;")
        tables = [r[0] for r in cursor.fetchall()]
        cursor.close()

        expected_tables = {
            "schema_migrations",
            "trains",
            "scheduled_trips",
            "schedule_stops",
            "telemetry_history",
            "recording_sessions",
        }
        for table in expected_tables:
            self.assertIn(table, tables)

    def test_schema_initialization_is_idempotent(self):
        v1 = SchemaManager.initialize_schema(self.conn)
        v2 = SchemaManager.initialize_schema(self.conn)
        self.assertEqual(v1, v2)
        self.assertEqual(v2, CURRENT_SCHEMA_VERSION)

    def test_indexes_are_created(self):
        SchemaManager.initialize_schema(self.conn)
        cursor = self.conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='index' ORDER BY name;")
        indexes = [r[0] for r in cursor.fetchall()]
        cursor.close()

        expected_indexes = [
            "idx_telemetry_corridor_time",
            "idx_telemetry_schedule_time",
            "idx_telemetry_time",
            "idx_telemetry_train_time",
            "idx_trips_date",
        ]
        for idx in expected_indexes:
            self.assertIn(idx, indexes)

    def test_foreign_key_enforcement(self):
        SchemaManager.initialize_schema(self.conn)
        self.conn.execute("PRAGMA foreign_keys = ON;")

        # Inserting a trip with non-existent train_number must fail
        with self.assertRaises(sqlite3.IntegrityError):
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO scheduled_trips (schedule_id, train_number, service_date)
                    VALUES ('s-1', 'nonexistent_train', '2026-11-18');
                    """
                )

        # Inserting train first, then trip must succeed
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO trains (train_number, first_observed_at, last_observed_at)
                VALUES ('1597', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z');
                """
            )
            self.conn.execute(
                """
                INSERT INTO scheduled_trips (schedule_id, train_number, service_date)
                VALUES ('s-1', '1597', '2026-11-18');
                """
            )

        # Deleting train must cascade delete the scheduled_trip
        with self.conn:
            self.conn.execute("DELETE FROM trains WHERE train_number = '1597';")

        cursor = self.conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM scheduled_trips WHERE schedule_id = 's-1';")
        count = cursor.fetchone()[0]
        cursor.close()
        self.assertEqual(count, 0)

    def test_schedule_stops_unique_sequence_constraint(self):
        SchemaManager.initialize_schema(self.conn)
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO trains (train_number, first_observed_at, last_observed_at)
                VALUES ('1597', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z');
                """
            )
            self.conn.execute(
                """
                INSERT INTO scheduled_trips (schedule_id, train_number, service_date)
                VALUES ('s-1', '1597', '2026-11-18');
                """
            )
            self.conn.execute(
                """
                INSERT INTO schedule_stops (schedule_id, stop_sequence, station_name)
                VALUES ('s-1', 1, 'Αθήνα');
                """
            )

        # Duplicate (schedule_id, stop_sequence) must fail
        with self.assertRaises(sqlite3.IntegrityError):
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO schedule_stops (schedule_id, stop_sequence, station_name)
                    VALUES ('s-1', 1, 'ΣΚΑ');
                    """
                )

    def test_verify_integrity(self):
        SchemaManager.initialize_schema(self.conn)
        healthy, errors = SchemaManager.verify_integrity(self.conn)
        self.assertTrue(healthy)
        self.assertEqual(len(errors), 0)

    def test_schema_initialization_sets_pragma_user_version(self):
        """initialize_schema must set SQLite header PRAGMA user_version = CURRENT_SCHEMA_VERSION."""
        version = SchemaManager.initialize_schema(self.conn)
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        user_ver = self.conn.execute("PRAGMA user_version;").fetchone()[0]
        self.assertEqual(user_ver, CURRENT_SCHEMA_VERSION)
        self.assertEqual(SchemaManager.get_user_version(self.conn), CURRENT_SCHEMA_VERSION)

    def test_initialize_schema_backfills_pragma_user_version_on_legacy_db(self):
        """Pre-P1-2 databases with schema_migrations=1 and user_version=0 must be upgraded to user_version=1."""
        with self.conn:
            for statement in SCHEMA_V1_STATEMENTS:
                self.conn.execute(statement)
            self.conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (?, ?);",
                (1, "Initial V1 schema setup"),
            )
        self.assertEqual(self.conn.execute("PRAGMA user_version;").fetchone()[0], 0)

        version = SchemaManager.initialize_schema(self.conn)
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            self.conn.execute("PRAGMA user_version;").fetchone()[0],
            CURRENT_SCHEMA_VERSION,
        )
        count = self.conn.execute("SELECT COUNT(*) FROM schema_migrations;").fetchone()[0]
        self.assertEqual(count, CURRENT_SCHEMA_VERSION)

    def test_initialize_schema_verifies_integrity_and_raises_on_fk_violation(self):
        """initialize_schema with verify_integrity=True must raise DatabaseError on FK violation."""
        SchemaManager.initialize_schema(self.conn)
        self.conn.execute("PRAGMA foreign_keys = OFF;")
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO scheduled_trips (schedule_id, train_number, service_date)
                VALUES ('orphan-trip', 'missing-train', '2026-11-18');
                """
            )

        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            SchemaManager.initialize_schema(self.conn, verify_integrity=True)
        self.assertIn("Foreign key violation", str(ctx.exception))

    def test_initialize_schema_raises_on_corrupted_integrity_check(self):
        """initialize_schema must raise sqlite3.DatabaseError when verify_integrity reports corruption."""
        with patch.object(
            SchemaManager,
            "verify_integrity",
            return_value=(False, ["Integrity check failure: *** in database main ***"]),
        ):
            with self.assertRaises(sqlite3.DatabaseError) as ctx:
                SchemaManager.initialize_schema(self.conn)
            self.assertIn("Integrity check failure", str(ctx.exception))

    def test_optimize_and_init_db_helper(self):
        """init_db helper and SchemaManager.optimize must initialize schema and run PRAGMA optimize."""
        from storage.schema import init_db

        version = init_db(self.conn)
        self.assertEqual(version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(
            self.conn.execute("PRAGMA user_version;").fetchone()[0],
            CURRENT_SCHEMA_VERSION,
        )
        SchemaManager.optimize(self.conn)

    def test_v1_to_v2_migration_idempotent_with_backfill_and_warm_quick_check(self):
        """TC-DB-04: V1 -> V2 migration adds vehicle_id/recorded_epoch, backfills epoch, and warm-opens with quick_check."""
        # Seed a pure V1 database with one legacy telemetry row
        with self.conn:
            for statement in SCHEMA_V1_STATEMENTS:
                self.conn.execute(statement)
            self.conn.execute(
                "INSERT INTO schema_migrations (version, description) VALUES (1, 'V1');"
            )
            self.conn.execute("PRAGMA user_version = 1;")
            self.conn.execute(
                "INSERT INTO trains (train_number, first_observed_at, last_observed_at) "
                "VALUES ('IC52', '2026-11-26T10:00:00.000000Z', '2026-11-26T10:00:00.000000Z');"
            )
            self.conn.execute(
                "INSERT INTO telemetry_history (train_number, recorded_at, lat, lng, speed, course) "
                "VALUES ('IC52', '2026-11-26T10:00:00.000000Z', 38.0, 23.7, 100.0, 0.0);"
            )

        ver = SchemaManager.initialize_schema(self.conn)
        self.assertEqual(ver, 2)
        self.assertEqual(self.conn.execute("PRAGMA user_version;").fetchone()[0], 2)

        cols = {
            r[1] for r in self.conn.execute("PRAGMA table_info(telemetry_history);").fetchall()
        }
        self.assertIn("vehicle_id", cols)
        self.assertIn("recorded_epoch", cols)

        row = self.conn.execute(
            "SELECT recorded_epoch FROM telemetry_history WHERE train_number = 'IC52';"
        ).fetchone()
        self.assertIsNotNone(row[0])
        self.assertGreater(float(row[0]), 1_700_000_000.0)

        # Warm re-open must NOT invoke full verify_integrity (uses quick_check(1))
        with patch.object(
            SchemaManager,
            "verify_integrity",
            side_effect=AssertionError("warm open must not run full integrity_check"),
        ):
            ver2 = SchemaManager.initialize_schema(self.conn)
            self.assertEqual(ver2, 2)


if __name__ == "__main__":
    unittest.main()
