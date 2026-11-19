"""
Unit tests for Database connection management, WAL pragmas, and transactions.
"""

import os
import sqlite3
import tempfile
import threading
import unittest

from storage.db import Database
from storage.recorder import RecorderConfig, TelemetryRecorder
from train_tracker import TrainPosition


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

    def test_file_database_reuses_thread_local_connection(self):
        """File-backed Database must reuse the thread's persistent sqlite3.Connection across calls."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "reuse.db")
            db = Database(db_path)
            try:
                with db.connection() as conn1:
                    pass
                with db.connection() as conn2:
                    pass
                with db.transaction() as cur:
                    tx_conn = cur.connection

                self.assertIs(conn1, conn2)
                self.assertIs(conn1, tx_conn)
            finally:
                db.close()

    def test_file_database_isolates_connections_across_threads(self):
        """Different threads must receive isolated connections, and close() must clean up all."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "thread_isolated.db")
            db = Database(db_path)
            try:
                with db.connection() as main_conn:
                    pass

                worker_conns = []

                def worker():
                    with db.connection() as w_conn1:
                        worker_conns.append(w_conn1)
                    with db.connection() as w_conn2:
                        worker_conns.append(w_conn2)

                t = threading.Thread(target=worker)
                t.start()
                t.join(timeout=2.0)

                self.assertEqual(len(worker_conns), 2)
                self.assertIs(worker_conns[0], worker_conns[1])
                self.assertIsNot(main_conn, worker_conns[0])
            finally:
                db.close()

            self.assertEqual(len(getattr(db, "_open_connections", set())), 0)

    def test_connection_configures_busy_timeout_pragma(self):
        """Every SQLite connection must configure PRAGMA busy_timeout = 5000."""
        mem_db = Database(":memory:")
        try:
            with mem_db.connection() as conn:
                timeout_ms = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
                self.assertEqual(timeout_ms, 5000)
        finally:
            mem_db.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "busy_timeout.db")
            file_db = Database(db_path)
            try:
                with file_db.connection() as conn:
                    timeout_ms = conn.execute("PRAGMA busy_timeout;").fetchone()[0]
                    self.assertEqual(timeout_ms, 5000)
            finally:
                file_db.close()

    def test_transaction_uses_begin_immediate(self):
        """Database.transaction() must acquire a transaction immediately on entry via BEGIN IMMEDIATE."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "begin_imm.db")
            db = Database(db_path)
            try:
                with db.transaction() as cur:
                    # With BEGIN IMMEDIATE executed on entry, in_transaction is immediately True
                    # before any DML statement is executed by the caller.
                    self.assertTrue(cur.connection.in_transaction)
                    cur.execute("SELECT COUNT(*) FROM trains;")
                with db.connection() as conn:
                    self.assertFalse(conn.in_transaction)
            finally:
                db.close()

    def test_in_memory_database_cross_thread_sharing_and_recorder(self):
        """TelemetryRecorder background thread must write to :memory: Database without ProgrammingError."""
        db = Database(":memory:")
        config = RecorderConfig(
            batch_size=5,
            flush_interval_sec=0.05,
            enable_deadband=False,
        )
        recorder = TelemetryRecorder(db, config=config)
        try:
            recorder.start()
            positions = [
                TrainPosition(
                    id=f"mem-{i}",
                    train_number=f"MEM-{i}",
                    locomotive_number="120-001",
                    name=f"Train {i}",
                    train_type="IC",
                    service_type="Passenger",
                    status="Normal",
                    origin="ATH",
                    destination="SKG",
                    next_station="LAR",
                    speed=100.0,
                    course=10.0,
                    track_heading=10.0,
                    lat=38.0 + i * 0.01,
                    lng=23.7 + i * 0.01,
                    altitude=100.0,
                    delay=0,
                    progress=0.5,
                    chainage_m=10000,
                    corridor="ATH-SKG",
                    distance_to_destination_m=20000,
                    timestamp="2026-11-18T12:00:00Z",
                    schedule_id=f"SCH-{i}",
                )
                for i in range(5)
            ]
            recorder.enqueue_batch(positions)

            # Perform concurrent reads from MainThread while recorder flushes on worker thread
            for _ in range(5):
                with db.connection() as conn:
                    conn.execute("SELECT COUNT(*) FROM telemetry_history;").fetchone()

            recorder.stop(timeout_sec=2.0)

            with db.connection() as conn:
                total = conn.execute("SELECT COUNT(*) FROM telemetry_history;").fetchone()[0]
                self.assertEqual(total, 5)
        finally:
            if recorder.is_running:
                recorder.stop(timeout_sec=2.0)
            db.close()

    def test_checkpoint_wal_accepts_all_valid_modes_case_insensitively(self):
        """checkpoint_wal must accept PASSIVE, FULL, RESTART, and TRUNCATE (case-insensitive)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "checkpoint_modes.db")
            db = Database(db_path)
            try:
                for mode in ("PASSIVE", "FULL", "RESTART", "TRUNCATE", "passive", " truncate "):
                    db.checkpoint_wal(mode)
            finally:
                db.close()

    def test_checkpoint_wal_rejects_invalid_or_injected_modes(self):
        """checkpoint_wal must reject invalid modes and SQL injection payloads with ValueError."""
        mem_db = Database(":memory:")
        try:
            for bad_mode in ("INVALID", "", "   ", "PASSIVE); DROP TABLE trains; --", None, 123):
                with self.assertRaises(ValueError):
                    mem_db.checkpoint_wal(bad_mode)  # type: ignore[arg-type]
        finally:
            mem_db.close()

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "checkpoint_inject.db")
            db = Database(db_path)
            try:
                with db.transaction() as cur:
                    cur.execute(
                        """
                        INSERT INTO trains (train_number, first_observed_at, last_observed_at)
                        VALUES ('IC50', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z');
                        """
                    )

                for bad_mode in ("INVALID", "", "   ", "PASSIVE); DROP TABLE trains; --", None):
                    with self.assertRaises(ValueError):
                        db.checkpoint_wal(bad_mode)  # type: ignore[arg-type]

                with db.connection() as conn:
                    count = conn.execute("SELECT COUNT(*) FROM trains;").fetchone()[0]
                    self.assertEqual(count, 1)
            finally:
                db.close()

    def test_database_startup_sets_user_version_and_verifies_integrity(self):
        """Database initialization must set PRAGMA user_version = 1 and expose verify_integrity/optimize."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "user_ver.db")
            db = Database(db_path)
            try:
                with db.connection() as conn:
                    user_ver = conn.execute("PRAGMA user_version;").fetchone()[0]
                    self.assertEqual(user_ver, 1)
                healthy, errors = db.verify_integrity()
                self.assertTrue(healthy)
                self.assertEqual(errors, [])
                db.optimize()
            finally:
                db.close()

            # Re-open existing database idempotently
            db2 = Database(db_path)
            try:
                with db2.connection() as conn:
                    self.assertEqual(conn.execute("PRAGMA user_version;").fetchone()[0], 1)
                healthy, errors = db2.verify_integrity()
                self.assertTrue(healthy)
                self.assertEqual(errors, [])
            finally:
                db2.close()

    def test_database_startup_fails_fast_on_integrity_violation(self):
        """Database startup must fail fast with sqlite3.DatabaseError if integrity check fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "corrupt_fk.db")
            db = Database(db_path)
            db.close()

            raw_conn = sqlite3.connect(db_path)
            raw_conn.execute("PRAGMA foreign_keys = OFF;")
            with raw_conn:
                raw_conn.execute(
                    """
                    INSERT INTO scheduled_trips (schedule_id, train_number, service_date)
                    VALUES ('bad-trip', 'nonexistent-train', '2026-11-18');
                    """
                )
            raw_conn.close()

            with self.assertRaises(sqlite3.DatabaseError):
                Database(db_path)

    def test_posix_database_permissions_0600(self):
        """Database and WAL companion files must have owner-only 0600 permissions on POSIX."""
        if os.name == "nt":
            self.skipTest("POSIX permissions test skipped on Windows NT")

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "secure_perms.db")
            db = Database(db_path)
            try:
                # 1. Main DB file must be 0600
                st_mode = os.stat(db_path).st_mode & 0o777
                self.assertEqual(oct(st_mode), "0o600")

                # 2. Write a transaction to spawn WAL and SHM files
                with db.transaction() as cur:
                    cur.execute(
                        """
                        INSERT INTO trains (train_number, first_observed_at, last_observed_at)
                        VALUES ('8888', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z');
                        """
                    )
                db.checkpoint_wal("PASSIVE")

                for suffix in ("-wal", "-shm"):
                    sidecar = f"{db_path}{suffix}"
                    if os.path.exists(sidecar):
                        s_mode = os.stat(sidecar).st_mode & 0o777
                        self.assertEqual(oct(s_mode), "0o600", f"{sidecar} is not 0600")
            finally:
                db.close()

    def test_harden_existing_permissive_database_file(self):
        """Pre-existing database file with 0644 mode must be hardened to 0600 on POSIX."""
        if os.name == "nt":
            self.skipTest("POSIX permissions test skipped on Windows NT")

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "legacy_0644.db")
            # Create file with permissive 0644
            with open(db_path, "w") as f:
                f.write("")
            os.chmod(db_path, 0o644)
            self.assertEqual(oct(os.stat(db_path).st_mode & 0o777), "0o644")

            db = Database(db_path)
            try:
                self.assertEqual(oct(os.stat(db_path).st_mode & 0o777), "0o600")
            finally:
                db.close()

    def test_nested_parent_directory_permissions(self):
        """Newly created parent directories under umask(0o077) must have 0700 permissions on POSIX."""
        if os.name == "nt":
            self.skipTest("POSIX permissions test skipped on Windows NT")

        with tempfile.TemporaryDirectory() as tmpdir:
            nested_parent = os.path.join(tmpdir, "secure_folder", "sub_folder")
            nested_db = os.path.join(nested_parent, "test.db")
            db = Database(nested_db)
            try:
                self.assertTrue(os.path.exists(nested_db))
                parent_mode = os.stat(nested_parent).st_mode & 0o777
                self.assertEqual(oct(parent_mode), "0o700")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
