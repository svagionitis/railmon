"""
Unit tests for Database connection management, WAL pragmas, and transactions.
"""

import os
import sqlite3
import tempfile
import unittest

from storage.db import Database


class TestStorageDatabase(unittest.TestCase):
    """Test suite for Database connection lifecycle, WAL pragmas, and transactions."""

    def test_in_memory_database(self):
        db = Database(":memory:")
        try:
            with db.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO trains (train_number, first_observed_at, last_observed_at)
                    VALUES ('2232', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z');
                    """
                )

            with db.connection() as conn:
                row = conn.execute(
                    "SELECT train_number FROM trains WHERE train_number = '2232';"
                ).fetchone()
                self.assertIsNotNone(row)
                self.assertEqual(row["train_number"], "2232")
        finally:
            db.close()

    def test_file_database_wal_pragmas(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test_railway.db")
            db = Database(db_path)
            try:
                with db.connection() as conn:
                    # In WAL mode, journal_mode pragma returns 'wal'
                    cur = conn.execute("PRAGMA journal_mode;")
                    journal_mode = cur.fetchone()[0]
                    self.assertEqual(journal_mode.lower(), "wal")

                    cur = conn.execute("PRAGMA synchronous;")
                    # NORMAL synchronous level is 1
                    sync_mode = cur.fetchone()[0]
                    self.assertEqual(sync_mode, 1)

                    cur = conn.execute("PRAGMA foreign_keys;")
                    fk_mode = cur.fetchone()[0]
                    self.assertEqual(fk_mode, 1)

                # WAL checkpoint test
                db.checkpoint_wal("TRUNCATE")

                # Disk size test
                size = db.get_size_bytes()
                self.assertGreater(size, 0)
            finally:
                db.close()

    def test_transaction_rollback_on_exception(self):
        db = Database(":memory:")
        try:
            with self.assertRaises(RuntimeError):
                with db.transaction() as cur:
                    cur.execute(
                        """
                        INSERT INTO trains (train_number, first_observed_at, last_observed_at)
                        VALUES ('9999', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z');
                        """
                    )
                    raise RuntimeError("Simulated transaction failure")

            with db.connection() as conn:
                row = conn.execute(
                    "SELECT train_number FROM trains WHERE train_number = '9999';"
                ).fetchone()
                self.assertIsNone(row)
        finally:
            db.close()

    def test_parent_directory_auto_created(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested_db_path = os.path.join(tmpdir, "nested", "sub", "test.db")
            db = Database(nested_db_path)
            try:
                self.assertTrue(os.path.exists(os.path.dirname(nested_db_path)))
                self.assertTrue(os.path.exists(nested_db_path))
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
