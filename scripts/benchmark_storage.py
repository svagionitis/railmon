#!/usr/bin/env python3
"""Hellenic Railways (railway.gov.gr) SQLite Storage Benchmarking Harness.

Executes write throughput, read latency under load, deadband suppression efficiency,
and WAL checkpointing performance evaluations.
Outputs tabular summaries and optional JSON benchmark artifacts.
"""

import argparse
from datetime import datetime, timezone
import json
import os
import statistics
import sys
import tempfile
import time
from typing import Any, Dict, List

# Ensure repository root is on sys.path for direct script execution
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from storage.db import Database
from storage.filter import DeadbandConfig, DeadbandFilter
from storage.models import TrainPosition
from storage.recorder import RecorderConfig, TelemetryRecorder
from storage.repository import HistoricalRepository


def _make_bench_position(
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


def run_write_benchmark(
    num_records: int = 10000,
    batch_sizes: List[int] = [10, 50, 100, 500],
) -> List[Dict[str, Any]]:
    """Benchmark write throughput across various batch commit sizes."""
    results = []

    for batch_size in batch_sizes:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, f"bench_write_{batch_size}.db")
            db = Database(db_path)
            db.init_schema()

            config = RecorderConfig(
                batch_size=batch_size,
                flush_interval_sec=1.0,
                enable_deadband=False,
                mode="benchmark",
            )
            recorder = TelemetryRecorder(db, config)
            recorder.start()

            # Pre-generate synthetic positions
            records_chunk = [
                _make_bench_position(
                    train_number=f"IC{50 + (i % 10)}",
                    lat=38.0 + (i * 0.0001),
                    lng=23.7 + (i * 0.0001),
                    speed=100.0,
                    delay=i % 15,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                for i in range(num_records)
            ]

            start_time = time.monotonic()
            chunk_size = 50
            for i in range(0, num_records, chunk_size):
                recorder.enqueue_batch(records_chunk[i : i + chunk_size])

            recorder.stop()
            elapsed = time.monotonic() - start_time

            db_size_bytes = os.path.getsize(db_path) if os.path.exists(db_path) else 0
            wal_path = f"{db_path}-wal"
            wal_size_bytes = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
            total_bytes = db_size_bytes + wal_size_bytes

            throughput = num_records / max(elapsed, 0.001)
            bytes_per_row = total_bytes / num_records if num_records > 0 else 0

            results.append({
                "batch_size": batch_size,
                "num_records": num_records,
                "elapsed_sec": round(elapsed, 3),
                "throughput_rows_per_sec": round(throughput, 1),
                "bytes_per_row": round(bytes_per_row, 1),
                "db_size_kb": round(db_size_bytes / 1024, 1),
                "wal_size_kb": round(wal_size_bytes / 1024, 1),
            })
            db.close()

    return results


def run_deadband_benchmark(
    stationary_duration_steps: int = 600,
    moving_duration_steps: int = 600,
) -> Dict[str, Any]:
    """Quantify storage savings achieved by the deadband filter."""
    deadband_filter = DeadbandFilter(
        DeadbandConfig(
            min_distance_m=15.0,
            min_delay_delta_min=1,
            min_speed_delta_kmh=5.0,
            max_heartbeat_interval_sec=60.0,
        )
    )

    base_time = 1700000000.0
    total_positions = 0
    passed_positions = 0

    # 1. Stationary simulation (train dwell at station for 600 seconds)
    for i in range(stationary_duration_steps):
        pos = _make_bench_position(
            train_number="IC52",
            lat=38.2000,
            lng=23.7000,
            speed=0.0,
            delay=3,
            timestamp=datetime.fromtimestamp(base_time + i, timezone.utc).isoformat(),
        )
        total_positions += 1
        admitted, _ = deadband_filter.should_record(pos, now_epoch=base_time + i)
        if admitted:
            passed_positions += 1

    stationary_passed = passed_positions
    base_time += stationary_duration_steps

    # 2. Moving simulation (train traveling at 120 km/h = ~33 m/s)
    for i in range(moving_duration_steps):
        pos = _make_bench_position(
            train_number="IC52",
            lat=38.2000 + (i * 0.0003),
            lng=23.7000 + (i * 0.0003),
            speed=120.0,
            delay=3 + (i // 300),
            timestamp=datetime.fromtimestamp(base_time + i, timezone.utc).isoformat(),
        )
        total_positions += 1
        admitted, _ = deadband_filter.should_record(pos, now_epoch=base_time + i)
        if admitted:
            passed_positions += 1

    moving_passed = passed_positions - stationary_passed
    reduction_pct = 100.0 * (1.0 - (passed_positions / total_positions)) if total_positions > 0 else 0.0

    return {
        "total_emitted_positions": total_positions,
        "total_persisted_positions": passed_positions,
        "stationary_emitted": stationary_duration_steps,
        "stationary_persisted": stationary_passed,
        "moving_emitted": moving_duration_steps,
        "moving_persisted": moving_passed,
        "reduction_percentage": round(reduction_pct, 2),
    }


def run_read_latency_benchmark(num_records: int = 10000) -> Dict[str, Any]:
    """Measure analytical query latencies (p50, p95, p99) over indexed historical data."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "bench_read.db")
        db = Database(db_path)
        db.init_schema()
        repo = HistoricalRepository(db)

        # Batch seed telemetry rows
        batch = []
        base_time = 1700000000
        for i in range(num_records):
            train_num = f"IC{50 + (i % 10)}"
            corridor = "Ath-Thess-Ath" if (i % 2 == 0) else "Thess-Alex"
            ts = datetime.fromtimestamp(base_time + (i * 2), timezone.utc).isoformat()
            batch.append((
                train_num,
                None,
                ts,
                38.0 + (i * 0.0001),
                23.7 + (i * 0.0001),
                110.0 + (i % 30),
                45.0,
                45.0,
                150.0,
                i % 12,
                "Κανονική",
                0.5,
                i * 100,
                50000 - (i * 100),
                "Larissa",
                corridor,
            ))

        # Register train numbers in trains table to satisfy foreign keys
        distinct_trains = [
            (
                f"IC{50 + (i % 10)}",
                datetime.fromtimestamp(base_time, timezone.utc).isoformat(),
                datetime.fromtimestamp(base_time + (num_records * 2), timezone.utc).isoformat(),
            )
            for i in range(10)
        ]
        with db.transaction() as cursor:
            cursor.executemany(
                """
                INSERT OR IGNORE INTO trains (train_number, first_observed_at, last_observed_at)
                VALUES (?, ?, ?);
                """,
                distinct_trains,
            )
            cursor.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed,
                    course, track_heading, altitude, delay_min, status, progress,
                    chainage_m, dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                batch,
            )

        query_latencies: Dict[str, List[float]] = {
            "get_train_history": [],
            "get_corridor_punctuality": [],
            "export_trip_geojson": [],
            "get_database_stats": [],
        }

        # 1. Train History
        for _ in range(30):
            t0 = time.monotonic()
            repo.get_train_history("IC50", limit=200)
            query_latencies["get_train_history"].append((time.monotonic() - t0) * 1000)

        # 2. Corridor Punctuality
        for _ in range(30):
            t0 = time.monotonic()
            repo.get_corridor_punctuality("Ath-Thess-Ath")
            query_latencies["get_corridor_punctuality"].append((time.monotonic() - t0) * 1000)

        # 3. GeoJSON Export
        for _ in range(30):
            t0 = time.monotonic()
            repo.export_trip_geojson("IC50")
            query_latencies["export_trip_geojson"].append((time.monotonic() - t0) * 1000)

        # 4. Database Stats
        for _ in range(30):
            t0 = time.monotonic()
            repo.get_database_stats()
            query_latencies["get_database_stats"].append((time.monotonic() - t0) * 1000)

        stats_summary = {}
        for q_name, lats in query_latencies.items():
            lats_sorted = sorted(lats)
            idx95 = min(int(len(lats_sorted) * 0.95), len(lats_sorted) - 1)
            stats_summary[q_name] = {
                "mean_ms": round(statistics.mean(lats), 2),
                "p50_ms": round(statistics.median(lats), 2),
                "p95_ms": round(lats_sorted[idx95], 2),
                "p99_ms": round(lats_sorted[-1], 2),
            }

        db.close()
        return stats_summary


def print_benchmark_report(
    write_results: List[Dict[str, Any]],
    deadband_results: Dict[str, Any],
    read_results: Dict[str, Any],
) -> None:
    """Format and print benchmark results to terminal."""
    print("=" * 80)
    print("HELLENIC RAILWAYS SQLITE STORAGE BENCHMARK REPORT")
    print("=" * 80)

    print("\n1. WRITE THROUGHPUT VS BATCH COMMIT SIZE")
    print("-" * 80)
    print(f"{'Batch Size':<12} {'Elapsed (s)':<14} {'Rows / Sec':<16} {'Bytes / Row':<14} {'DB Size':<12}")
    print("-" * 80)
    for res in write_results:
        print(
            f"{res['batch_size']:<12} "
            f"{res['elapsed_sec']:<14.3f} "
            f"{res['throughput_rows_per_sec']:<16.1f} "
            f"{res['bytes_per_row']:<14.1f} "
            f"{res['db_size_kb']:<12.1f} KB"
        )

    print("\n2. DEADBAND FILTER SUPPRESSION EFFICIENCY")
    print("-" * 80)
    print(f"Total Emitted Positions:     {deadband_results['total_emitted_positions']:,}")
    print(f"Total Persisted Positions:   {deadband_results['total_persisted_positions']:,}")
    print(f"Stationary Positions:        {deadband_results['stationary_emitted']:,} emitted -> {deadband_results['stationary_persisted']:,} persisted")
    print(f"Moving Positions:            {deadband_results['moving_emitted']:,} emitted -> {deadband_results['moving_persisted']:,} persisted")
    print(f"Storage Footprint Reduction: {deadband_results['reduction_percentage']}%")

    print("\n3. ANALYTICAL QUERY LATENCIES")
    print("-" * 80)
    print(f"{'Query Function':<28} {'Mean (ms)':<12} {'p50 (ms)':<12} {'p95 (ms)':<12} {'p99 (ms)':<12}")
    print("-" * 80)
    for q_name, lat in read_results.items():
        print(
            f"{q_name:<28} "
            f"{lat['mean_ms']:<12.2f} "
            f"{lat['p50_ms']:<12.2f} "
            f"{lat['p95_ms']:<12.2f} "
            f"{lat['p99_ms']:<12.2f}"
        )
    print("=" * 80)


# Backward compatibility shims for external tools and historical docs
run_write_throughput_benchmark = run_write_benchmark
run_deadband_efficiency_benchmark = run_deadband_benchmark
run_read_benchmark = run_read_latency_benchmark


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hellenic Railways SQLite Storage Benchmark")
    parser.add_argument("--json", type=str, default="", help="Path to export benchmark results as JSON")
    parser.add_argument("--records", type=int, default=5000, help="Number of records for write benchmark")
    args = parser.parse_args()

    writes = run_write_benchmark(num_records=args.records, batch_sizes=[10, 50, 100, 500])
    deadband = run_deadband_benchmark(stationary_duration_steps=600, moving_duration_steps=600)
    reads = run_read_latency_benchmark(num_records=min(args.records, 10000))

    print_benchmark_report(writes, deadband, reads)

    if args.json:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "write_benchmarks": writes,
            "deadband_efficiency": deadband,
            "read_latencies": reads,
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
        print(f"\n[Artifact] Benchmark results exported to {args.json}")
