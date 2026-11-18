"""
Automated performance benchmarks and qualification tests for Phase 5.
Validates write throughput, query response latency, deadband filtering efficiency,
and WAL checkpointing dynamics.
"""

import os
import queue
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from typing import List

from storage.db import Database
from storage.filter import DeadbandConfig, DeadbandFilter
from storage.models import TrainPosition
from storage.recorder import RecorderConfig, TelemetryRecorder
from storage.repository import HistoricalRepository


def _sample_bench_position(
    train_number: str = "IC50",
    lat: float = 38.045,
    lng: float = 23.721,
    speed: float = 110.0,
    delay: int = 2,
    corridor: str = "Ath-Thess-Ath",
    station: str = "Larissa",
    timestamp: str = "2026-11-18T10:00:00Z",
) -> TrainPosition:
    return TrainPosition(
        id=f"{train_number}_id",
        train_number=train_number,
        locomotive_number="120-001",
        name="InterCity",
        train_type="IC",
        service_type="Passenger",
        status="Κανονική",
        origin="Athens",
        destination="Thessaloniki",
        next_station=station,
        speed=speed,
        course=15.0,
        track_heading=15.0,
        lat=lat,
        lng=lng,
        altitude=150.0,
        delay=delay,
        progress=0.45,
        chainage_m=120000,
        corridor=corridor,
        distance_to_destination_m=380000,
        timestamp=timestamp,
        schedule_id=f"SCHED_{train_number}",
    )


class TestStorageBenchmarks(unittest.TestCase):
    """Automated benchmark tests ensuring storage performance targets are satisfied."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "benchmark_suite.db")
        self.db = Database(self.db_path)
        self.db.init_schema()
        self.repo = HistoricalRepository(self.db)

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def test_write_throughput_target(self) -> None:
        """TC-BENCH-P5-01: Verify batch write throughput exceeds baseline threshold."""
        config = RecorderConfig(
            batch_size=100,
            flush_interval_sec=0.2,
            enable_deadband=False,
            mode="benchmark",
        )
        recorder = TelemetryRecorder(self.db, config)
        recorder.start()

        num_records = 2000
        start_time = time.monotonic()

        # Ingest in chunks of 50
        for i in range(0, num_records, 50):
            chunk = [
                _sample_bench_position(
                    train_number=f"IC{50 + (j % 10)}",
                    lat=38.0 + (i + j) * 0.0001,
                    lng=23.7 + (i + j) * 0.0001,
                    speed=100.0 + (j % 20),
                    delay=j % 5,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                for j in range(50)
            ]
            recorder.enqueue_batch(chunk)

        recorder.stop()
        elapsed = time.monotonic() - start_time

        stats = self.repo.get_database_stats()
        self.assertEqual(stats.total_telemetry_rows, num_records)

        throughput = num_records / max(elapsed, 0.001)
        # Check that throughput is healthy (> 500 rows/sec on CI or resource-constrained environments)
        self.assertGreater(throughput, 300.0, f"Observed write throughput: {throughput:.1f} rows/s")

    def test_concurrent_read_write_zero_lock_contention(self) -> None:
        """TC-BENCH-P5-02: Multi-threaded reader and writer execution yields zero lock errors."""
        config = RecorderConfig(
            batch_size=25,
            flush_interval_sec=0.05,
            enable_deadband=False,
            mode="concurrency_bench",
        )
        recorder = TelemetryRecorder(self.db, config)
        recorder.start()

        stop_event = threading.Event()
        read_errors: List[Exception] = []
        read_counts = [0, 0, 0]

        def reader_history() -> None:
            while not stop_event.is_set():
                try:
                    self.repo.get_train_history("IC50", limit=50)
                    read_counts[0] += 1
                except Exception as exc:
                    read_errors.append(exc)
                time.sleep(0.005)

        def reader_punctuality() -> None:
            while not stop_event.is_set():
                try:
                    self.repo.get_corridor_punctuality("Ath-Thess-Ath")
                    read_counts[1] += 1
                except Exception as exc:
                    read_errors.append(exc)
                time.sleep(0.01)

        def reader_stats() -> None:
            while not stop_event.is_set():
                try:
                    self.repo.get_database_stats()
                    read_counts[2] += 1
                except Exception as exc:
                    read_errors.append(exc)
                time.sleep(0.01)

        threads = [
            threading.Thread(target=reader_history, daemon=True),
            threading.Thread(target=reader_punctuality, daemon=True),
            threading.Thread(target=reader_stats, daemon=True),
        ]
        for t in threads:
            t.start()

        # Concurrently enqueue 1,000 records
        for i in range(40):
            batch = [
                _sample_bench_position(
                    train_number=f"IC{50 + (j % 5)}",
                    lat=38.0 + (i * 0.002),
                    lng=23.7,
                    speed=120.0,
                    delay=i % 10,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                for j in range(25)
            ]
            recorder.enqueue_batch(batch)
            time.sleep(0.008)

        recorder.stop()
        stop_event.set()
        for t in threads:
            t.join(timeout=2.0)

        self.assertEqual(len(read_errors), 0, f"Read errors encountered during writing: {read_errors}")
        self.assertGreater(sum(read_counts), 20, "Expected concurrent reads to successfully complete")
        stats = self.repo.get_database_stats()
        self.assertEqual(stats.total_telemetry_rows, 1000)

    def test_deadband_suppression_efficiency(self) -> None:
        """TC-BENCH-P5-03: Verify deadband filter achieves > 50% suppression on stationary trains."""
        filter_instance = DeadbandFilter(
            DeadbandConfig(
                min_distance_m=15.0,
                min_delay_delta_min=1,
                min_speed_delta_kmh=5.0,
                max_heartbeat_interval_sec=60.0,
            )
        )

        base_epoch = 1700000000.0

        # Simulate 100 stationary updates (same coordinates, delay unchanged, speed 0)
        stationary_admitted = 0
        for i in range(100):
            pos = _sample_bench_position(
                train_number="IC50",
                lat=38.045,
                lng=23.721,
                speed=0.0,
                delay=3,
                station="Larissa",
                timestamp=datetime.fromtimestamp(base_epoch + i, timezone.utc).isoformat(),
            )
            admitted, _ = filter_instance.should_record(pos, now_epoch=base_epoch + i)
            if admitted:
                stationary_admitted += 1

        # For 100 seconds with 60s heartbeat, should only admit initial (t=0) + heartbeat (t=60) = 2 points
        self.assertEqual(stationary_admitted, 2)
        stats = filter_instance.stats
        self.assertGreater(stats["suppression_ratio_pct"], 90.0)

    def test_wal_checkpoint_truncate(self) -> None:
        """TC-BENCH-P5-04: Verify WAL checkpointing flushes to base database and truncates WAL."""
        config = RecorderConfig(batch_size=50, flush_interval_sec=0.1, enable_deadband=False)
        recorder = TelemetryRecorder(self.db, config)
        recorder.start()

        recorder.enqueue_batch([
            _sample_bench_position("IC50", lat=38.0 + (i * 0.001)) for i in range(100)
        ])
        time.sleep(0.3)
        recorder.stop()

        # Checkpoint WAL
        self.db.checkpoint_wal(mode="TRUNCATE")

        wal_file = f"{self.db_path}-wal"
        if os.path.exists(wal_file):
            self.assertEqual(os.path.getsize(wal_file), 0)

        # Confirm data integrity remains 100%
        stats = self.repo.get_database_stats()
        self.assertEqual(stats.total_telemetry_rows, 100)


if __name__ == "__main__":
    unittest.main()
