"""
Unit tests for asynchronous telemetry recorder, batching, session auditing, and WAL concurrency.
"""

import os
import shutil
import tempfile
import threading
import time
import unittest

from storage.db import Database
from storage.filter import DeadbandConfig
from storage.recorder import RecorderConfig, TelemetryRecorder
from train_tracker import TrainPosition


def _create_sample_position(
    train_number: str = "IC52",
    lat: float = 38.0,
    lng: float = 23.5,
    speed: float = 80.0,
    delay: int = 0,
    status: str = "Κανονική",
    next_station: str = "Λειανοκλάδι",
    schedule_id: str = "SCHED-1",
    timestamp: str = "2026-11-18T12:00:00Z",
) -> TrainPosition:
    """Helper to instantiate test TrainPosition entities."""
    return TrainPosition(
        id=f"id-{train_number}",
        train_number=train_number,
        locomotive_number="120-001",
        name=f"Intercity {train_number}",
        train_type="Intercity",
        service_type="Passenger",
        status=status,
        origin="Αθήνα",
        destination="Θεσσαλονίκη",
        next_station=next_station,
        speed=speed,
        course=15.0,
        track_heading=15.0,
        lat=lat,
        lng=lng,
        altitude=150.0,
        delay=delay,
        progress=0.35,
        chainage_m=120000,
        corridor="ATH-SKG",
        distance_to_destination_m=380000,
        timestamp=timestamp,
        schedule_id=schedule_id,
    )


class TestTelemetryRecorder(unittest.TestCase):
    """Test suite for TelemetryRecorder queuing, batching, flushing, and WAL concurrency."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_recorder.db")
        self.db = Database(db_path=self.db_path)

    def tearDown(self):
        if hasattr(self, "recorder") and self.recorder.is_running:
            self.recorder.stop(timeout_sec=2.0)
        self.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_recorder_lifecycle_and_session_creation(self):
        """Starting and stopping recorder must manage audit record in recording_sessions."""
        config = RecorderConfig(mode="live", client_version="1.0.0")
        self.recorder = TelemetryRecorder(self.db, config=config)

        self.recorder.start()
        self.assertTrue(self.recorder.is_running)
        session_id = self.recorder.session_id
        self.assertIsNotNone(session_id)

        # Verify active session in DB
        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM recording_sessions WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["mode"], "live")
            self.assertEqual(row["client_version"], "1.0.0")
            self.assertIsNone(row["ended_at"])

        self.recorder.stop()
        self.assertFalse(self.recorder.is_running)

        # Verify finalized session in DB
        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM recording_sessions WHERE session_id = ?", (session_id,))
            row = cur.fetchone()
            self.assertIsNotNone(row["ended_at"])

    def test_batch_flush_on_count_threshold(self):
        """Buffer must flush immediately when record count reaches batch_size."""
        config = RecorderConfig(
            batch_size=5,
            flush_interval_sec=10.0,  # Large interval so only count triggers flush
            enable_deadband=False,
        )
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        # Enqueue 5 items
        positions = [
            _create_sample_position(f"TR-{i}", lat=38.0 + (i * 0.01))
            for i in range(5)
        ]
        self.recorder.enqueue_batch(positions)

        # Allow worker thread time to process
        time.sleep(0.3)

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS total FROM telemetry_history")
            count = cur.fetchone()["total"]
            self.assertEqual(count, 5)

        self.recorder.stop()

    def test_timeout_flush_on_idle_queue(self):
        """Buffer must flush after flush_interval_sec even if count is below batch_size."""
        config = RecorderConfig(
            batch_size=50,
            flush_interval_sec=0.2,  # Quick flush interval
            enable_deadband=False,
        )
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        positions = [
            _create_sample_position("IC52"),
            _create_sample_position("IC54"),
        ]
        self.recorder.enqueue_batch(positions)

        # Wait for timeout to trigger flush
        deadline = time.time() + 2.0
        count = 0
        while time.time() < deadline:
            with self.db.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) AS total FROM telemetry_history")
                count = cur.fetchone()["total"]
                if count == 2:
                    break
            time.sleep(0.05)

        self.assertEqual(count, 2)

        self.recorder.stop()

    def test_trains_table_upsert_on_telemetry_flush(self):
        """Flushing telemetry must upsert trains metadata without creating duplicates."""
        config = RecorderConfig(batch_size=1, flush_interval_sec=1.0, enable_deadband=False)
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        # First observation of IC52
        pos1 = _create_sample_position("IC52", timestamp="2026-11-18T10:00:00Z")
        self.recorder.enqueue(pos1)
        time.sleep(0.2)

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM trains WHERE train_number = 'IC52'")
            row = cur.fetchone()
            self.assertIsNotNone(row)
            first_obs = row["first_observed_at"]
            if hasattr(first_obs, "hour"):
                self.assertEqual(first_obs.hour, 10)
                self.assertEqual(first_obs.minute, 0)
            else:
                self.assertIn("10:00:00", str(first_obs))

        # Second observation with updated timestamp
        pos2 = _create_sample_position("IC52", timestamp="2026-11-18T10:30:00Z")
        self.recorder.enqueue(pos2)
        time.sleep(0.2)

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS cnt FROM trains WHERE train_number = 'IC52'")
            self.assertEqual(cur.fetchone()["cnt"], 1)

            cur.execute("SELECT * FROM trains WHERE train_number = 'IC52'")
            row = cur.fetchone()
            first_obs = row["first_observed_at"]
            last_obs = row["last_observed_at"]
            if hasattr(first_obs, "hour"):
                self.assertEqual(first_obs.hour, 10)
                self.assertEqual(first_obs.minute, 0)
                self.assertEqual(last_obs.hour, 10)
                self.assertEqual(last_obs.minute, 30)
            else:
                self.assertIn("10:00:00", str(first_obs))
                self.assertIn("10:30:00", str(last_obs))

        self.recorder.stop()

    def test_graceful_shutdown_drains_queue_and_finalizes_session(self):
        """stop() must drain all queued items and update total_records_written."""
        config = RecorderConfig(batch_size=100, flush_interval_sec=10.0, enable_deadband=False)
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        # Enqueue 15 items (well below batch_size)
        positions = [
            _create_sample_position(f"TR-{i}")
            for i in range(15)
        ]
        self.recorder.enqueue_batch(positions)

        # Immediately stop
        self.recorder.stop()

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS total FROM telemetry_history")
            self.assertEqual(cur.fetchone()["total"], 15)

            cur.execute("SELECT total_records_written FROM recording_sessions WHERE session_id = ?", (self.recorder.session_id,))
            self.assertEqual(cur.fetchone()["total_records_written"], 15)

    def test_queue_saturation_drop_handling(self):
        """enqueue() must return False and drop packets when queue_maxsize is exceeded."""
        config = RecorderConfig(queue_maxsize=2, enable_deadband=False)
        self.recorder = TelemetryRecorder(self.db, config=config)
        # Note: Do not start worker thread so queue fills up
        self.recorder._thread = threading.Thread()  # Mock running state flag
        self.recorder.start()

        p1 = _create_sample_position("T1")
        p2 = _create_sample_position("T2")
        p3 = _create_sample_position("T3")

        res1 = self.recorder.enqueue(p1)
        res2 = self.recorder.enqueue(p2)
        res3 = self.recorder.enqueue(p3)

        self.assertTrue(res1)
        self.assertTrue(res2)
        self.assertFalse(res3)  # Dropped because queue is full

        self.recorder.stop()

    def test_deadband_integration_in_recorder(self):
        """Recorder with deadband enabled must suppress stationary snapshots."""
        config = RecorderConfig(
            batch_size=20,
            flush_interval_sec=0.1,
            enable_deadband=True,
            deadband_config=DeadbandConfig(min_distance_m=15.0),
        )
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        # Enqueue 10 identical stationary snapshots for IC52
        for _ in range(10):
            self.recorder.enqueue(_create_sample_position("IC52", lat=38.0, lng=23.5))

        # Sleep to let timeout flush occur
        time.sleep(0.3)
        self.recorder.stop()

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS total FROM telemetry_history WHERE train_number = 'IC52'")
            self.assertEqual(cur.fetchone()["total"], 1)

        stats = self.recorder.get_stats()
        self.assertEqual(stats["filter"]["total_suppressed"], 9.0)

    def test_concurrent_reader_writer_wal_isolation(self):
        """WAL mode must permit concurrent reads while recorder thread continuously writes."""
        config = RecorderConfig(
            batch_size=5,
            flush_interval_sec=0.1,
            enable_deadband=False,
        )
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        read_errors = []
        stop_readers = threading.Event()

        def reader_task():
            while not stop_readers.is_set():
                try:
                    with self.db.connection() as conn:
                        cur = conn.cursor()
                        cur.execute("SELECT COUNT(*) FROM telemetry_history")
                        _ = cur.fetchone()
                except Exception as exc:
                    read_errors.append(exc)
                time.sleep(0.01)

        reader_threads = [
            threading.Thread(target=reader_task)
            for _ in range(3)
        ]
        for t in reader_threads:
            t.start()

        # Producer writes batches
        for batch_idx in range(10):
            positions = [
                _create_sample_position(f"CONC-{batch_idx}-{i}", lat=38.0 + (i * 0.001))
                for i in range(5)
            ]
            self.recorder.enqueue_batch(positions)
            time.sleep(0.02)

        self.recorder.stop()
        stop_readers.set()
        for t in reader_threads:
            t.join()

        self.assertEqual(len(read_errors), 0, f"Concurrent read errors encountered: {read_errors}")

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS total FROM telemetry_history")
            self.assertEqual(cur.fetchone()["total"], 50)

    def test_context_manager_usage(self):
        """TelemetryRecorder should work seamlessly as a Python context manager."""
        with TelemetryRecorder(self.db) as rec:
            self.assertTrue(rec.is_running)
            rec.enqueue(_create_sample_position("CTX-1"))

        self.assertFalse(rec.is_running)

    def test_stop_invokes_wal_checkpoint_truncate(self):
        """stop() must execute PRAGMA wal_checkpoint(TRUNCATE) after finalizing session."""
        from unittest.mock import patch

        config = RecorderConfig(batch_size=50, flush_interval_sec=10.0, enable_deadband=False)
        self.recorder = TelemetryRecorder(self.db, config=config)

        with patch.object(self.db, "checkpoint_wal", wraps=self.db.checkpoint_wal) as mock_ckpt:
            self.recorder.start()
            self.recorder.enqueue_batch([
                _create_sample_position(f"WAL-{i}", lat=38.0 + i * 0.01)
                for i in range(5)
            ])
            self.recorder.stop()
            mock_ckpt.assert_called_once_with("TRUNCATE")

        wal_file = f"{self.db_path}-wal"
        if os.path.exists(wal_file):
            self.assertEqual(os.path.getsize(wal_file), 0)

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS total FROM telemetry_history")
            self.assertEqual(cur.fetchone()["total"], 5)

            cur.execute(
                "SELECT ended_at, total_records_written FROM recording_sessions WHERE session_id = ?",
                (self.recorder.session_id,),
            )
            sess = cur.fetchone()
            self.assertIsNotNone(sess["ended_at"])
            self.assertEqual(sess["total_records_written"], 5)

    def test_disk_full_circuit_breaker_halts_recording_below_100mb(self):
        """TelemetryRecorder must log CRITICAL, trip circuit breaker, and halt writes if free disk < 100 MB."""
        from collections import namedtuple
        from unittest.mock import patch

        DiskUsage = namedtuple("usage", ["total", "used", "free"])
        low_disk = DiskUsage(
            total=10 * 1024 * 1024 * 1024,
            used=10 * 1024 * 1024 * 1024 - 50 * 1024 * 1024,
            free=50 * 1024 * 1024,  # 50 MB (< 100 MB threshold)
        )

        config = RecorderConfig(
            batch_size=2,
            flush_interval_sec=0.1,
            enable_deadband=False,
        )
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        with patch("shutil.disk_usage", return_value=low_disk):
            with self.assertLogs("railmon.storage.recorder", level="CRITICAL") as cm:
                self.recorder.enqueue_batch([
                    _create_sample_position("LOW-1", lat=38.01),
                    _create_sample_position("LOW-2", lat=38.02),
                ])
                time.sleep(0.3)

            self.assertTrue(any("critically low" in msg.lower() or "disk" in msg.lower() for msg in cm.output))
            self.assertTrue(self.recorder.disk_full_tripped)
            # Subsequent enqueue calls must be rejected immediately
            self.assertFalse(self.recorder.enqueue(_create_sample_position("LOW-3")))
            self.assertEqual(self.recorder.enqueue_batch([_create_sample_position("LOW-4")]), 0)

        self.recorder.stop()

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS total FROM telemetry_history")
            self.assertEqual(cur.fetchone()["total"], 0)

    def test_disk_full_circuit_breaker_allows_writes_above_100mb(self):
        """TelemetryRecorder must commit normally when free disk space >= 100 MB."""
        from collections import namedtuple
        from unittest.mock import patch

        DiskUsage = namedtuple("usage", ["total", "used", "free"])
        healthy_disk = DiskUsage(
            total=10 * 1024 * 1024 * 1024,
            used=5 * 1024 * 1024 * 1024,
            free=500 * 1024 * 1024,  # 500 MB (>= 100 MB threshold)
        )

        config = RecorderConfig(
            batch_size=2,
            flush_interval_sec=0.1,
            enable_deadband=False,
        )
        self.recorder = TelemetryRecorder(self.db, config=config)

        with patch("shutil.disk_usage", return_value=healthy_disk):
            self.recorder.start()
            self.recorder.enqueue_batch([
                _create_sample_position("OK-1", lat=38.01),
                _create_sample_position("OK-2", lat=38.02),
            ])
            time.sleep(0.3)
            self.assertFalse(self.recorder.disk_full_tripped)
            self.recorder.stop()

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS total FROM telemetry_history")
            self.assertEqual(cur.fetchone()["total"], 2)

    def test_recorder_retention_days_archives_aged_telemetry_on_start(self):
        """Starting TelemetryRecorder with retention_days must archive aged telemetry into archive DB."""
        from storage.repository import HistoricalRepository

        archive_db_path = os.path.join(self.temp_dir, "recorder_archive.db")

        # Seed aged row (2020-01-01) and recent row
        with self.db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO trains (train_number, default_name, first_observed_at, last_observed_at)
                VALUES ('IC50', 'Intercity 50', '2020-01-01T08:00:00Z', '2026-11-18T12:00:00Z');
                """
            )
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                [
                    ("IC50", None, "2020-01-01T08:00:00Z", 38.0, 23.5, 100.0, 10.0),
                    ("IC50", None, "2020-01-01T08:05:00Z", 38.1, 23.6, 110.0, 12.0),
                ],
            )

        config = RecorderConfig(
            batch_size=1,
            flush_interval_sec=0.1,
            enable_deadband=False,
            retention_days=30,
            archive_path=archive_db_path,
        )
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        # Enqueue a live position
        self.recorder.enqueue(_create_sample_position("IC50", timestamp="2026-11-18T12:00:00Z"))
        time.sleep(0.25)
        self.recorder.stop()

        # Verify hot DB only has the 1 recent row
        hot_repo = HistoricalRepository(self.db)
        hot_rows = hot_repo.get_train_history("IC50")
        self.assertEqual(len(hot_rows), 1)
        self.assertEqual(hot_rows[0].recorded_at, "2026-11-18T12:00:00Z")

        # Verify archive DB received the 2 aged rows from 2020-01-01
        self.assertTrue(os.path.exists(archive_db_path))
        arch_db = Database(archive_db_path)
        try:
            arch_repo = HistoricalRepository(arch_db)
            arch_rows = arch_repo.get_train_history("IC50")
            self.assertEqual(len(arch_rows), 2)
            self.assertEqual(arch_rows[0].recorded_at, "2020-01-01T08:00:00Z")
            self.assertEqual(arch_rows[1].recorded_at, "2020-01-01T08:05:00Z")
        finally:
            arch_db.close()

        stats = self.recorder.get_stats()
        self.assertEqual(stats.get("total_records_archived"), 2)

    def test_recorder_periodic_filter_prune(self):
        """TC-P2-10: TelemetryRecorder worker loop periodically evicts stale deadband filter states."""
        deadband_cfg = DeadbandConfig(stale_timeout_sec=0.2, stale_prune_interval_sec=0.05)
        config = RecorderConfig(
            batch_size=1,
            flush_interval_sec=0.05,
            enable_deadband=True,
            deadband_config=deadband_cfg,
        )
        self.recorder = TelemetryRecorder(self.db, config=config)
        self.recorder.start()

        # Enqueue position to populate filter cache
        self.recorder.enqueue(_create_sample_position("IC50"))
        time.sleep(0.05)

        # Confirm IC50 was cached
        self.assertIn("IC50", self.recorder.filter._states)

        # Wait for periodic worker prune
        deadline = time.time() + 1.5
        while time.time() < deadline:
            if "IC50" not in self.recorder.filter._states:
                break
            time.sleep(0.02)

        self.assertNotIn("IC50", self.recorder.filter._states)
        self.assertGreaterEqual(self.recorder.filter.stats["total_evicted"], 1.0)
        self.recorder.stop()


if __name__ == "__main__":
    unittest.main()
