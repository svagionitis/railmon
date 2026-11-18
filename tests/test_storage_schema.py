"""
Unit tests for SQLite schema creation, migrations, indexes, and integrity checks.
"""

import sqlite3
import unittest

from storage.schema import CURRENT_SCHEMA_VERSION, SchemaManager


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


if __name__ == "__main__":
    unittest.main()
