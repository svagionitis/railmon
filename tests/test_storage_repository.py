"""
Unit and integration tests for HistoricalRepository and query engine.
"""

import csv
import io
import os
import shutil
import tempfile
import threading
import time
import unittest

from storage.db import Database
from storage.models import (
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    ReplayBounds,
    ReplayObservation,
    SpeedProfilePoint,
    StationDwellRecord,
    TelemetryHistoryRow,
    TripSummary,
)
from storage.recorder import RecorderConfig, TelemetryRecorder
from storage.repository import (
    HistoricalRepository,
    export_in_memory_csv,
    export_in_memory_geojson,
    parse_iso_to_epoch,
    sanitize_train_identifier,
    validate_export_path,
)
from train_tracker import TrainPosition
from tui.state import HistoricalBreadcrumb, TrainRecord


class TestHistoricalRepository(unittest.TestCase):
    """Test suite for HistoricalRepository query methods and exporters."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_repo.db")
        self.db = Database(self.db_path)
        self.repo = HistoricalRepository(self.db)
        self._populate_seed_data()

    def tearDown(self):
        self.db.close()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _populate_seed_data(self):
        """Seed database with realistic multi-train, multi-corridor telemetry data."""
        with self.db.transaction() as cur:
            # 1. Register trains
            cur.execute(
                """
                INSERT INTO trains (train_number, default_name, first_observed_at, last_observed_at)
                VALUES ('1597', 'Intercity 50', '2026-11-18T08:00:00Z', '2026-11-18T12:00:00Z'),
                       ('2232', 'Regional 2232', '2026-11-18T09:00:00Z', '2026-11-18T11:00:00Z');
                """
            )

            # 2. Register scheduled trip
            cur.execute(
                """
                INSERT INTO scheduled_trips (schedule_id, train_number, origin, destination, corridor, service_date)
                VALUES ('SCH-1597', '1597', 'ATH', 'SKG', 'ATH-SKG', '2026-11-18');
                """
            )

            # 3. Insert telemetry for train 1597 (moving, varying speed and delays)
            # Time window: 10:00 to 10:04 (4 points)
            telemetry_1597 = [
                ('1597', 'SCH-1597', '2026-11-18T10:00:00Z', 38.001, 23.701, 0.0, 180.0, 180.0, 50.0, 0, 'Normal', 0.0, 0, 500000, 'ATHENS', 'ATH-SKG'),
                ('1597', 'SCH-1597', '2026-11-18T10:01:00Z', 38.020, 23.715, 60.0, 180.0, 180.0, 55.0, 2, 'Normal', 0.1, 5000, 495000, 'SKA', 'ATH-SKG'),
                ('1597', 'SCH-1597', '2026-11-18T10:02:00Z', 38.050, 23.730, 120.0, 185.0, 185.0, 70.0, 4, 'Normal', 0.2, 15000, 485000, 'OINOI', 'ATH-SKG'),
                ('1597', 'SCH-1597', '2026-11-18T10:03:00Z', 38.100, 23.750, 140.0, 185.0, 185.0, 90.0, 6, 'Normal', 0.3, 25000, 475000, 'THIVA', 'ATH-SKG'),
            ]
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course,
                    track_heading, altitude, delay_min, status, progress, chainage_m,
                    dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                telemetry_1597,
            )

            # 4. Insert telemetry for train 2232 (dwelling at a station)
            # Station dwell at 'CHALKIS' from 10:10:00 to 10:11:30 (90 seconds dwell, speed <= 1.0)
            telemetry_2232 = [
                ('2232', None, '2026-11-18T10:09:00Z', 38.460, 23.590, 45.0, 90.0, 90.0, 20.0, 1, 'Normal', 0.8, 8000, 2000, 'CHALKIS', 'ATH-HAL'),
                ('2232', None, '2026-11-18T10:10:00Z', 38.463, 23.595, 0.5, 90.0, 90.0, 20.0, 1, 'Stationary', 0.9, 9000, 1000, 'CHALKIS', 'ATH-HAL'),
                ('2232', None, '2026-11-18T10:11:30Z', 38.463, 23.595, 0.0, 90.0, 90.0, 20.0, 3, 'Stationary', 0.9, 9000, 1000, 'CHALKIS', 'ATH-HAL'),
                ('2232', None, '2026-11-18T10:12:00Z', 38.468, 23.600, 35.0, 95.0, 95.0, 22.0, 4, 'Normal', 1.0, 10000, 0, 'TERMINAL', 'ATH-HAL'),
            ]
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course,
                    track_heading, altitude, delay_min, status, progress, chainage_m,
                    dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                telemetry_2232,
            )

            # 5. Insert recording session
            cur.execute(
                """
                INSERT INTO recording_sessions (started_at, ended_at, mode, total_records_written, client_version)
                VALUES ('2026-11-18T08:00:00Z', '2026-11-18T12:00:00Z', 'live', 8, '1.0.0');
                """
            )

    def test_get_train_history_basic(self):
        """Verify fetching chronologically ordered history for a specific train."""
        history = self.repo.get_train_history("1597")
        self.assertEqual(len(history), 4)
        self.assertEqual(history[0].train_number, "1597")
        self.assertEqual(history[0].recorded_at, "2026-11-18T10:00:00Z")
        self.assertEqual(history[-1].recorded_at, "2026-11-18T10:03:00Z")
        self.assertEqual(history[1].speed, 60.0)
        self.assertEqual(history[1].corridor, "ATH-SKG")

    def test_get_train_history_time_window(self):
        """Verify time window filtering on telemetry history."""
        # Query interval covering only the 2 middle points
        history = self.repo.get_train_history(
            "1597",
            start_time="2026-11-18T10:01:00Z",
            end_time="2026-11-18T10:02:00Z",
        )
        self.assertEqual(len(history), 2)
        self.assertEqual(history[0].recorded_at, "2026-11-18T10:01:00Z")
        self.assertEqual(history[1].recorded_at, "2026-11-18T10:02:00Z")

    def test_get_train_history_limit_and_order(self):
        """Verify limit and ordering parameters."""
        history_desc = self.repo.get_train_history("1597", limit=2, order="DESC")
        self.assertEqual(len(history_desc), 2)
        self.assertEqual(history_desc[0].recorded_at, "2026-11-18T10:03:00Z")
        self.assertEqual(history_desc[1].recorded_at, "2026-11-18T10:02:00Z")

        with self.assertRaises(ValueError):
            self.repo.get_train_history("1597", order="INVALID")

        with self.assertRaises(ValueError):
            self.repo.get_train_history("1597", limit=-1)

    def test_iter_train_history_streaming(self):
        """Verify generator-based streaming of telemetry history."""
        rows = list(self.repo.iter_train_history("1597", chunk_size=2))
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0].train_number, "1597")
        self.assertEqual(rows[3].train_number, "1597")

        with self.assertRaises(ValueError):
            list(self.repo.iter_train_history("1597", chunk_size=0))

    def test_get_active_trips_aggregation(self):
        """Verify aggregation of recorded trips grouped by train and service date."""
        trips = self.repo.get_active_trips(service_date="2026-11-18")
        self.assertEqual(len(trips), 2)

        trip_1597 = next(t for t in trips if t.train_number == "1597")
        self.assertEqual(trip_1597.total_points, 4)
        self.assertEqual(trip_1597.corridor, "ATH-SKG")
        self.assertEqual(trip_1597.origin, "ATH")
        self.assertEqual(trip_1597.destination, "SKG")
        self.assertEqual(trip_1597.max_delay_min, 6)
        self.assertEqual(trip_1597.max_speed_kmh, 140.0)
        self.assertAlmostEqual(trip_1597.avg_speed_kmh, 80.0, places=1)

        # Filter by corridor
        trips_hal = self.repo.get_active_trips(corridor="ATH-HAL")
        self.assertEqual(len(trips_hal), 1)
        self.assertEqual(trips_hal[0].train_number, "2232")

    def test_get_corridor_punctuality_metrics(self):
        """Verify punctuality and delay compliance metrics by corridor."""
        punctuality = self.repo.get_corridor_punctuality(on_time_threshold_min=5)
        self.assertEqual(len(punctuality), 2)

        # Corridor ATH-HAL: 4 observations with delays (1, 1, 3, 4) -> all <= 5 min -> 100%
        hal = next(p for p in punctuality if p.corridor == "ATH-HAL")
        self.assertEqual(hal.total_observations, 4)
        self.assertEqual(hal.on_time_count, 4)
        self.assertEqual(hal.delayed_count, 0)
        self.assertEqual(hal.punctuality_rate_pct, 100.0)

        # Corridor ATH-SKG: 4 observations with delays (0, 2, 4, 6) -> 3 <= 5, 1 > 5 -> 75%
        skg = next(p for p in punctuality if p.corridor == "ATH-SKG")
        self.assertEqual(skg.total_observations, 4)
        self.assertEqual(skg.on_time_count, 3)
        self.assertEqual(skg.delayed_count, 1)
        self.assertEqual(skg.punctuality_rate_pct, 75.0)

    def test_get_speed_profile(self):
        """Verify speed vs. chainage and elevation profile generation."""
        profile = self.repo.get_speed_profile("1597")
        self.assertEqual(len(profile), 4)
        self.assertEqual(profile[0].chainage_m, 0)
        self.assertEqual(profile[0].speed_kmh, 0.0)
        self.assertEqual(profile[0].altitude, 50.0)
        self.assertEqual(profile[3].chainage_m, 25000)
        self.assertEqual(profile[3].speed_kmh, 140.0)

    def test_station_dwell_times_calculation(self):
        """Verify detection of station dwell events based on stationary speed windows."""
        # Train 2232 stopped at CHALKIS from 10:10:00 to 10:11:30 (90 seconds dwell)
        dwells = self.repo.get_station_dwell_times(
            "2232",
            speed_threshold_kmh=2.0,
            min_dwell_sec=20,
        )
        self.assertEqual(len(dwells), 1)
        dwell = dwells[0]
        self.assertEqual(dwell.train_number, "2232")
        self.assertEqual(dwell.station_name, "CHALKIS")
        self.assertEqual(dwell.dwell_seconds, 90)
        self.assertEqual(dwell.arrival_delay_min, 1)
        self.assertEqual(dwell.departure_delay_min, 3)

    def test_station_dwell_times_multiple_visits_same_station(self):
        """Verify multiple visits to the same station across trips/days do not collapse."""
        with self.db.transaction() as cur:
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course,
                    track_heading, altitude, delay_min, status, progress, chainage_m,
                    dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                [
                    ('2232', None, '2026-11-19T10:09:00Z', 38.460, 23.590, 50.0, 90.0, 90.0, 20.0, 4, 'Normal', 0.8, 8000, 2000, 'CHALKIS', 'ATH-HAL'),
                    ('2232', None, '2026-11-19T10:10:00Z', 38.463, 23.595, 0.0, 90.0, 90.0, 20.0, 5, 'Stationary', 0.9, 9000, 1000, 'CHALKIS', 'ATH-HAL'),
                    ('2232', None, '2026-11-19T10:12:00Z', 38.463, 23.595, 0.2, 90.0, 90.0, 20.0, 6, 'Stationary', 0.9, 9000, 1000, 'CHALKIS', 'ATH-HAL'),
                    ('2232', None, '2026-11-19T10:13:00Z', 38.468, 23.600, 40.0, 95.0, 95.0, 22.0, 6, 'Normal', 1.0, 10000, 0, 'TERMINAL', 'ATH-HAL'),
                ],
            )

        dwells = self.repo.get_station_dwell_times(
            "2232",
            speed_threshold_kmh=2.0,
            min_dwell_sec=20,
        )
        self.assertEqual(len(dwells), 2)
        self.assertEqual(dwells[0].station_name, "CHALKIS")
        self.assertEqual(dwells[0].arrival_time, "2026-11-18T10:10:00Z")
        self.assertEqual(dwells[0].departure_time, "2026-11-18T10:11:30Z")
        self.assertEqual(dwells[0].dwell_seconds, 90)
        self.assertEqual(dwells[0].arrival_delay_min, 1)
        self.assertEqual(dwells[0].departure_delay_min, 3)

        self.assertEqual(dwells[1].station_name, "CHALKIS")
        self.assertEqual(dwells[1].arrival_time, "2026-11-19T10:10:00Z")
        self.assertEqual(dwells[1].departure_time, "2026-11-19T10:12:00Z")
        self.assertEqual(dwells[1].dwell_seconds, 120)
        self.assertEqual(dwells[1].arrival_delay_min, 5)
        self.assertEqual(dwells[1].departure_delay_min, 6)

    def test_station_dwell_times_temporal_gap_separation(self):
        """Verify stationary records separated by >30 minutes without moving frames split into distinct dwells."""
        with self.db.transaction() as cur:
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course,
                    track_heading, altitude, delay_min, status, progress, chainage_m,
                    dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                [
                    ('2232', None, '2026-11-18T14:00:00Z', 38.463, 23.595, 0.0, 90.0, 90.0, 20.0, 2, 'Stationary', 0.9, 9000, 1000, 'CHALKIS', 'ATH-HAL'),
                    ('2232', None, '2026-11-18T14:01:00Z', 38.463, 23.595, 0.0, 90.0, 90.0, 20.0, 2, 'Stationary', 0.9, 9000, 1000, 'CHALKIS', 'ATH-HAL'),
                ],
            )

        dwells = self.repo.get_station_dwell_times(
            "2232",
            speed_threshold_kmh=2.0,
            min_dwell_sec=20,
        )
        self.assertEqual(len(dwells), 2)
        self.assertEqual(dwells[0].dwell_seconds, 90)
        self.assertEqual(dwells[1].dwell_seconds, 60)

    def test_export_trip_geojson_linestring(self):
        """Verify RFC 7946 GeoJSON LineString trajectory export."""
        geojson = self.repo.export_trip_geojson("1597", feature_type="LineString")
        self.assertEqual(geojson["type"], "FeatureCollection")
        self.assertEqual(len(geojson["features"]), 1)

        feature = geojson["features"][0]
        self.assertEqual(feature["type"], "Feature")
        self.assertEqual(feature["id"], "1597")
        self.assertEqual(feature["geometry"]["type"], "LineString")

        coords = feature["geometry"]["coordinates"]
        self.assertEqual(len(coords), 4)
        # Verify [lng, lat, alt]
        self.assertEqual(coords[0], [23.701, 38.001, 50.0])
        self.assertEqual(coords[3], [23.750, 38.100, 90.0])

        props = feature["properties"]
        self.assertEqual(props["train_number"], "1597")
        self.assertEqual(props["total_points"], 4)
        self.assertEqual(props["max_speed_kmh"], 140.0)
        self.assertEqual(len(props["timestamps"]), 4)

    def test_export_trip_geojson_points(self):
        """Verify RFC 7946 GeoJSON Point collection export."""
        geojson = self.repo.export_trip_geojson("1597", feature_type="Point")
        self.assertEqual(geojson["type"], "FeatureCollection")
        self.assertEqual(len(geojson["features"]), 4)

        feature = geojson["features"][0]
        self.assertEqual(feature["geometry"]["type"], "Point")
        self.assertEqual(feature["geometry"]["coordinates"], [23.701, 38.001, 50.0])
        self.assertEqual(feature["properties"]["train_number"], "1597")
        self.assertEqual(feature["properties"]["speed_kmh"], 0.0)

    def test_export_trip_geojson_empty_and_invalid(self):
        """Verify GeoJSON export handles empty results and rejects invalid feature types."""
        empty = self.repo.export_trip_geojson("NON_EXISTENT")
        self.assertEqual(empty, {"type": "FeatureCollection", "features": []})

        with self.assertRaises(ValueError):
            self.repo.export_trip_geojson("1597", feature_type="Polygon")

    def test_export_csv_telemetry(self):
        """Verify telemetry CSV export to StringIO."""
        buf = io.StringIO()
        count = self.repo.export_csv(
            query_type="telemetry",
            target=buf,
            params={"train_number": "1597"},
        )
        self.assertEqual(count, 4)

        buf.seek(0)
        reader = csv.reader(buf)
        header = next(reader)
        self.assertIn("train_number", header)
        self.assertIn("recorded_at", header)
        self.assertIn("lat", header)
        self.assertIn("lng", header)

        rows = list(reader)
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[0][1], "1597")

    def test_export_csv_trips(self):
        """Verify trips CSV export."""
        buf = io.StringIO()
        count = self.repo.export_csv(
            query_type="trips",
            target=buf,
            params={"service_date": "2026-11-18"},
        )
        self.assertEqual(count, 2)

        buf.seek(0)
        reader = csv.reader(buf)
        header = next(reader)
        self.assertIn("service_date", header)
        self.assertIn("max_speed_kmh", header)

    def test_export_csv_punctuality_and_dwell(self):
        """Verify punctuality and dwell CSV export."""
        buf_p = io.StringIO()
        count_p = self.repo.export_csv(query_type="punctuality", target=buf_p)
        self.assertEqual(count_p, 2)

        buf_d = io.StringIO()
        count_d = self.repo.export_csv(
            query_type="dwell",
            target=buf_d,
            params={"train_number": "2232"},
        )
        self.assertEqual(count_d, 1)

    def test_export_csv_to_file(self):
        """Verify exporting CSV directly to a filesystem path."""
        target_path = os.path.join(self.temp_dir, "export_test.csv")
        count = self.repo.export_csv(
            query_type="telemetry",
            target=target_path,
            params={"train_number": "1597"},
        )
        self.assertEqual(count, 4)
        self.assertTrue(os.path.exists(target_path))
        with open(target_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        self.assertEqual(len(lines), 5)  # 1 header + 4 rows

    def test_export_csv_invalid_args(self):
        """Verify error handling on invalid CSV export parameters."""
        buf = io.StringIO()
        with self.assertRaises(ValueError):
            self.repo.export_csv(query_type="unsupported", target=buf)

        with self.assertRaises(ValueError):
            # Telemetry requires 'train_number'
            self.repo.export_csv(query_type="telemetry", target=buf, params={})

    def test_get_recording_sessions(self):
        """Verify retrieval of recording session audit logs."""
        sessions = self.repo.get_recording_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].mode, "live")
        self.assertEqual(sessions[0].total_records_written, 8)

    def test_get_database_stats(self):
        """Verify database health and sizing statistics."""
        stats = self.repo.get_database_stats()
        self.assertEqual(stats.total_telemetry_rows, 8)
        self.assertEqual(stats.total_trains, 2)
        self.assertEqual(stats.total_trips, 1)
        self.assertEqual(stats.total_sessions, 1)
        self.assertTrue(stats.wal_mode_enabled)
        self.assertGreater(stats.file_size_bytes, 0)
        self.assertIsNotNone(stats.earliest_record)
        self.assertIsNotNone(stats.latest_record)

    def test_concurrent_query_during_writes(self):
        """Verify concurrent queries succeed with zero locking errors while writes are ongoing."""
        # Ensure CONCURRENT_TRAIN exists in trains table for foreign key integrity
        with self.db.transaction() as cur:
            cur.execute(
                """
                INSERT OR IGNORE INTO trains (train_number, default_name, first_observed_at, last_observed_at)
                VALUES ('CONCURRENT_TRAIN', 'Concurrent Test', '2026-11-18T12:00:00Z', '2026-11-18T12:00:00Z');
                """
            )

        stop_event = threading.Event()
        write_errors = []
        read_errors = []

        def writer_worker():
            idx = 0
            while not stop_event.is_set():
                idx += 1
                try:
                    with self.db.transaction() as cur:
                        cur.execute(
                            """
                            INSERT INTO telemetry_history (
                                train_number, recorded_at, lat, lng, speed, course
                            ) VALUES (?, ?, ?, ?, ?, ?);
                            """,
                            (
                                "CONCURRENT_TRAIN",
                                f"2026-11-18T12:00:{idx % 60:02d}Z",
                                38.0 + (idx * 0.001),
                                23.7 + (idx * 0.001),
                                50.0,
                                90.0,
                            ),
                        )
                    time.sleep(0.01)
                except Exception as e:
                    write_errors.append(e)

        def reader_worker():
            for _ in range(20):
                try:
                    self.repo.get_train_history("1597")
                    self.repo.get_corridor_punctuality()
                    self.repo.get_database_stats()
                    time.sleep(0.01)
                except Exception as e:
                    read_errors.append(e)

        writer_thread = threading.Thread(target=writer_worker, daemon=True)
        reader_thread = threading.Thread(target=reader_worker, daemon=True)

        writer_thread.start()
        reader_thread.start()

        reader_thread.join(timeout=5.0)
        stop_event.set()
        writer_thread.join(timeout=5.0)

        self.assertEqual(len(write_errors), 0, f"Write errors occurred: {write_errors}")
        self.assertEqual(len(read_errors), 0, f"Read errors occurred: {read_errors}")

    def test_archive_before_moves_expired_rows_and_metadata_to_archive_db(self):
        """archive_before must copy aged telemetry and parent metadata to archive DB before deleting from hot DB."""
        archive_db_path = os.path.join(self.temp_dir, "test_repo_archive.db")

        with self.db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO scheduled_trips (schedule_id, train_number, origin, destination, corridor, service_date)
                VALUES ('SCH-OLD', '1597', 'ATH', 'SKG', 'ATH-SKG', '2026-10-01');
                """
            )
            cur.execute(
                """
                INSERT INTO schedule_stops (schedule_id, stop_sequence, station_id, station_name, delay_sec)
                VALUES ('SCH-OLD', 1, 'ST-ATH', 'ATHENS', 0);
                """
            )
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course,
                    track_heading, altitude, delay_min, status, progress, chainage_m,
                    dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                [
                    ('1597', 'SCH-OLD', '2026-10-01T08:00:00Z', 38.001, 23.701, 50.0, 180.0, 180.0, 50.0, 0, 'Normal', 0.1, 1000, 499000, 'SKA', 'ATH-SKG'),
                    ('1597', 'SCH-OLD', '2026-10-01T08:05:00Z', 38.020, 23.715, 90.0, 180.0, 180.0, 55.0, 1, 'Normal', 0.2, 5000, 495000, 'OINOI', 'ATH-SKG'),
                ],
            )

        archived_count = self.repo.archive_before(
            "2026-11-01T00:00:00Z",
            archive_path=archive_db_path,
        )
        self.assertEqual(archived_count, 2)
        self.assertTrue(os.path.exists(archive_db_path))

        # Verify hot database evicted the 2 aged rows and old unreferenced trip, keeping the 8 active rows
        hot_stats = self.repo.get_database_stats()
        self.assertEqual(hot_stats.total_telemetry_rows, 8)
        self.assertEqual(hot_stats.total_trips, 1)
        hot_1597 = self.repo.get_train_history("1597")
        self.assertEqual(len(hot_1597), 4)
        self.assertTrue(all(r.recorded_at >= "2026-11-18" for r in hot_1597))

        # Verify archive database contains the 2 archived rows + parent train, trip, and stop
        archive_db = Database(archive_db_path)
        try:
            archive_repo = HistoricalRepository(archive_db)
            arch_rows = archive_repo.get_train_history("1597")
            self.assertEqual(len(arch_rows), 2)
            self.assertEqual(arch_rows[0].recorded_at, "2026-10-01T08:00:00Z")
            self.assertEqual(arch_rows[1].recorded_at, "2026-10-01T08:05:00Z")

            arch_trips = archive_repo.get_active_trips(service_date="2026-10-01")
            self.assertEqual(len(arch_trips), 1)
            self.assertEqual(arch_trips[0].schedule_id, "SCH-OLD")
            self.assertEqual(arch_trips[0].origin, "ATH")

            with archive_db.connection() as arch_conn:
                cur = arch_conn.cursor()
                cur.execute("SELECT COUNT(*) FROM schedule_stops WHERE schedule_id = 'SCH-OLD'")
                self.assertEqual(cur.fetchone()[0], 1)
        finally:
            archive_db.close()

    def test_archive_before_is_idempotent_and_accumulates_windows(self):
        """archive_before must accumulate sequential cutoff windows into the same archive DB without loss."""
        with self.db.transaction() as cur:
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course,
                    track_heading, altitude, delay_min, status, progress, chainage_m,
                    dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                [
                    ('1597', None, '2026-10-01T08:00:00Z', 38.001, 23.701, 50.0, 180.0, 180.0, 50.0, 0, 'Normal', 0.1, 1000, 499000, 'SKA', 'ATH-SKG'),
                    ('1597', None, '2026-10-20T08:00:00Z', 38.020, 23.715, 90.0, 180.0, 180.0, 55.0, 1, 'Normal', 0.2, 5000, 495000, 'OINOI', 'ATH-SKG'),
                ],
            )

        # First window (< 2026-10-15) using default archive path (<stem>_archive.db)
        count1 = self.repo.archive_before("2026-10-15T00:00:00Z")
        self.assertEqual(count1, 1)

        # Re-running with same cutoff archives 0 rows
        count_repeat = self.repo.archive_before("2026-10-15T00:00:00Z")
        self.assertEqual(count_repeat, 0)

        # Second window (< 2026-11-01) appends to existing archive DB
        count2 = self.repo.archive_before("2026-11-01T00:00:00Z")
        self.assertEqual(count2, 1)

        default_archive_path = self.repo.resolve_archive_path()
        archive_db = Database(default_archive_path)
        try:
            archive_repo = HistoricalRepository(archive_db)
            arch_rows = archive_repo.get_train_history("1597")
            self.assertEqual(len(arch_rows), 2)
        finally:
            archive_db.close()

    def test_archive_before_validates_cutoff_and_same_db_path(self):
        """archive_before must reject empty cutoff timestamps or archiving into the active DB path."""
        with self.assertRaises(ValueError):
            self.repo.archive_before("")
        with self.assertRaises(ValueError):
            self.repo.archive_before("   ")
        with self.assertRaises(ValueError):
            self.repo.archive_before("2026-11-01T00:00:00Z", archive_path=self.db_path)

    def test_validate_export_path_success(self):
        """validate_export_path must return canonical absolute path for safe destinations."""
        safe_path = os.path.join(self.temp_dir, "export.csv")
        result = validate_export_path(safe_path)
        self.assertEqual(result, os.path.abspath(safe_path))

        rel_path = "valid_relative.geojson"
        result_rel = validate_export_path(rel_path)
        self.assertEqual(result_rel, os.path.abspath(rel_path))

    def test_validate_export_path_rejects_traversal(self):
        """validate_export_path must reject '..' directory traversal attempts."""
        traversal_attempts = [
            "../bad.csv",
            "../../etc/passwd",
            os.path.join("sub", "..", "..", "bad.json"),
            r"..\escaped.csv",
            r"sub\..\..\escaped.json",
            "..",
        ]
        for bad in traversal_attempts:
            with self.subTest(path=bad):
                with self.assertRaises(ValueError) as ctx:
                    validate_export_path(bad)
                self.assertIn("traversal", str(ctx.exception).lower())

    def test_validate_export_path_rejects_missing_directory(self):
        """validate_export_path must reject paths whose parent directory does not exist."""
        nonexistent = os.path.join(self.temp_dir, "no_such_dir_12345", "out.csv")
        with self.assertRaises(ValueError) as ctx:
            validate_export_path(nonexistent)
        self.assertIn("does not exist", str(ctx.exception))

    def test_validate_export_path_rejects_existing_directory(self):
        """validate_export_path must reject paths that point to an existing directory."""
        with self.assertRaises(ValueError) as ctx:
            validate_export_path(self.temp_dir)
        self.assertIn("existing directory", str(ctx.exception))

    def test_validate_export_path_rejects_empty_and_null_bytes(self):
        """validate_export_path must reject empty inputs and null bytes."""
        with self.assertRaises(ValueError):
            validate_export_path("")
        with self.assertRaises(ValueError):
            validate_export_path("   ")
        with self.assertRaises(ValueError):
            validate_export_path(None)
        with self.assertRaises(ValueError) as ctx:
            validate_export_path("test\0out.csv")
        self.assertIn("null bytes", str(ctx.exception).lower())

    def test_validate_export_path_base_dir_boundary(self):
        """validate_export_path with base_dir must prevent escaping the root boundary."""
        allowed_dir = os.path.join(self.temp_dir, "allowed")
        os.makedirs(allowed_dir, exist_ok=True)

        valid = os.path.join(allowed_dir, "file.csv")
        self.assertEqual(validate_export_path(valid, base_dir=allowed_dir), os.path.abspath(valid))

        outside = os.path.join(self.temp_dir, "outside.csv")
        with self.assertRaises(ValueError) as ctx:
            validate_export_path(outside, base_dir=allowed_dir)
        self.assertIn("escapes allowed base directory", str(ctx.exception))

    def test_export_csv_with_path_traversal_fails(self):
        """HistoricalRepository.export_csv must fail when target contains directory traversal."""
        with self.assertRaises(ValueError) as ctx:
            self.repo.export_csv(
                query_type="telemetry",
                target="../evil_export.csv",
                params={"train_number": "1597"},
            )
        self.assertIn("traversal", str(ctx.exception).lower())

    def test_resolve_archive_path_rejects_traversal(self):
        """resolve_archive_path must reject custom archive paths with traversal tokens."""
        with self.assertRaises(ValueError) as ctx:
            self.repo.resolve_archive_path("../bad_archive.db")
        self.assertIn("traversal", str(ctx.exception).lower())

    def test_sanitize_train_identifier(self):
        """TC-F41-01: sanitize_train_identifier scrubs arbitrary train identifiers into safe filename tokens."""
        self.assertEqual(sanitize_train_identifier("1597"), "1597")
        self.assertEqual(sanitize_train_identifier("IC 50"), "IC_50")
        self.assertEqual(sanitize_train_identifier("  PR-2232/A  "), "PR-2232_A")
        self.assertEqual(sanitize_train_identifier("../../etc/passwd"), "etc_passwd")
        self.assertEqual(sanitize_train_identifier(""), "unknown")
        self.assertEqual(sanitize_train_identifier("   "), "unknown")
        self.assertEqual(sanitize_train_identifier(None), "unknown")
        self.assertEqual(sanitize_train_identifier("///...///"), "unknown")
        self.assertEqual(len(sanitize_train_identifier("A" * 60)), 32)

    def test_export_in_memory_geojson_and_csv(self):
        """TC-F41-02 & TC-F41-03: Verify RFC 7946 GeoJSON and 17-column CSV synthesis from in-memory TrainRecord."""
        pos1 = TrainPosition(
            id="t-1597",
            train_number="1597",
            locomotive_number="120-024",
            name="1597",
            train_type="IC",
            service_type="InterCity",
            status="Κανονική",
            origin="ΑΘΗΝΑ",
            destination="ΘΕΣΣΑΛΟΝΙΚΗ",
            next_station="Λάρισα",
            speed=110.0,
            course=340.0,
            track_heading=340.0,
            lat=38.001,
            lng=23.701,
            altitude=50.0,
            delay=4,
            progress=0.35,
            chainage_m=15000,
            corridor="PATHE",
            distance_to_destination_m=300000,
            timestamp="2026-11-20T14:00:00Z",
            schedule_id="SCH-1597",
        )
        record = TrainRecord(position=pos1)
        record.trail.clear()
        record.trail.append(
            HistoricalBreadcrumb(lat=38.001, lng=23.701, timestamp=1795183200.0, speed=95.0)
        )
        record.trail.append(
            HistoricalBreadcrumb(lat=38.050, lng=23.730, timestamp=1795183260.0, speed=120.0)
        )
        # Advance current position beyond last trail breadcrumb
        pos_latest = TrainPosition(
            id="t-1597",
            train_number="1597",
            locomotive_number="120-024",
            name="1597",
            train_type="IC",
            service_type="InterCity",
            status="Κανονική",
            origin="ΑΘΗΝΑ",
            destination="ΘΕΣΣΑΛΟΝΙΚΗ",
            next_station="Λάρισα",
            speed=142.0,
            course=342.0,
            track_heading=342.0,
            lat=38.100,
            lng=23.750,
            altitude=70.0,
            delay=6,
            progress=0.40,
            chainage_m=25000,
            corridor="PATHE",
            distance_to_destination_m=290000,
            timestamp="2026-11-20T14:02:00Z",
            schedule_id="SCH-1597",
        )
        record.position = pos_latest
        record.last_updated = 1795183320.0

        # 1. LineString GeoJSON export (2 trail breadcrumbs + 1 shifted current position = 3 points)
        geojson_ls = export_in_memory_geojson(record, feature_type="LineString")
        self.assertEqual(geojson_ls["type"], "FeatureCollection")
        self.assertEqual(len(geojson_ls["features"]), 1)
        feat = geojson_ls["features"][0]
        self.assertEqual(feat["id"], "1597")
        self.assertEqual(feat["geometry"]["type"], "LineString")
        self.assertEqual(len(feat["geometry"]["coordinates"]), 3)
        self.assertEqual(feat["geometry"]["coordinates"][0], [23.701, 38.001, 70.0])
        self.assertEqual(feat["geometry"]["coordinates"][2], [23.750, 38.100, 70.0])
        self.assertEqual(feat["properties"]["total_points"], 3)
        self.assertEqual(feat["properties"]["max_speed_kmh"], 142.0)
        self.assertEqual(feat["properties"]["source"], "in_memory_tui_snapshot")

        # 2. Point GeoJSON export
        geojson_pt = export_in_memory_geojson(record, feature_type="Point")
        self.assertEqual(len(geojson_pt["features"]), 3)
        self.assertEqual(geojson_pt["features"][0]["geometry"]["type"], "Point")

        # 3. Single-point record LineString export duplicates vertex for RFC 7946 Section 3.1.4 compliance
        single_rec = TrainRecord(position=pos1)
        single_rec.trail.clear()
        single_ls = export_in_memory_geojson(single_rec, feature_type="LineString")
        self.assertEqual(len(single_ls["features"][0]["geometry"]["coordinates"]), 2)
        self.assertEqual(single_ls["features"][0]["properties"]["total_points"], 1)

        # 4. Invalid feature_type raises ValueError; None record returns empty FeatureCollection
        with self.assertRaises(ValueError):
            export_in_memory_geojson(record, feature_type="Polygon")
        self.assertEqual(export_in_memory_geojson(None), {"type": "FeatureCollection", "features": []})

        # 5. CSV export to StringIO and file
        csv_buf = io.StringIO()
        row_count = export_in_memory_csv(record, csv_buf)
        self.assertEqual(row_count, 3)
        csv_buf.seek(0)
        reader = list(csv.reader(csv_buf))
        self.assertEqual(len(reader), 4)  # 1 header + 3 rows
        self.assertEqual(reader[0][1], "train_number")
        self.assertEqual(reader[1][1], "1597")

        csv_file = os.path.join(self.temp_dir, "in_mem_1597.csv")
        self.assertEqual(export_in_memory_csv(record, csv_file), 3)
        self.assertTrue(os.path.exists(csv_file))
        self.assertEqual(export_in_memory_csv(None, io.StringIO()), 0)

    def test_get_replay_bounds_and_observations_and_snapshot_at(self):
        """TC-F42-01 & TC-F42-02: Verify get_replay_bounds, get_replay_observations, and get_replay_snapshot_at."""
        from datetime import datetime, timezone

        # 1. parse_iso_to_epoch handles ISO strings, space-separated strings, datetime objects, floats, and invalid values
        ep_z = parse_iso_to_epoch("2026-11-18T10:00:00Z")
        ep_space = parse_iso_to_epoch("2026-11-18 10:00:00")
        ep_dt = parse_iso_to_epoch(datetime(2026, 11, 18, 10, 0, 0, tzinfo=timezone.utc))
        self.assertAlmostEqual(ep_z, ep_space, places=3)
        self.assertAlmostEqual(ep_z, ep_dt, places=3)
        self.assertEqual(parse_iso_to_epoch(12345.5), 12345.5)
        self.assertEqual(parse_iso_to_epoch(""), 0.0)
        self.assertEqual(parse_iso_to_epoch(None), 0.0)
        self.assertEqual(parse_iso_to_epoch("not-a-timestamp"), 0.0)

        # 2. get_replay_bounds on seeded DB (earliest: 10:00:00Z, latest: 10:12:00Z, 8 rows, 2 trains)
        bounds = self.repo.get_replay_bounds()
        self.assertIsNotNone(bounds)
        self.assertIsInstance(bounds, ReplayBounds)
        self.assertEqual(bounds.min_recorded_at, "2026-11-18T10:00:00Z")
        self.assertEqual(bounds.max_recorded_at, "2026-11-18T10:12:00Z")
        self.assertEqual(bounds.total_rows, 8)
        self.assertEqual(bounds.total_trains, 2)
        self.assertAlmostEqual(bounds.duration_sec, 720.0, places=2)

        # 3. get_replay_observations with include_start=False vs include_start=True
        obs_excl = self.repo.get_replay_observations(
            "2026-11-18T10:00:00Z",
            "2026-11-18T10:02:00Z",
            include_start=False,
        )
        self.assertEqual(len(obs_excl), 2)
        self.assertEqual(obs_excl[0].recorded_at, "2026-11-18T10:01:00Z")
        self.assertEqual(obs_excl[1].recorded_at, "2026-11-18T10:02:00Z")
        # Enriched from trains and scheduled_trips tables
        self.assertEqual(obs_excl[0].position.name, "Intercity 50")
        self.assertEqual(obs_excl[0].position.origin, "ATH")
        self.assertEqual(obs_excl[0].position.destination, "SKG")
        self.assertEqual(obs_excl[0].position.corridor, "ATH-SKG")

        obs_incl = self.repo.get_replay_observations(
            "2026-11-18T10:00:00Z",
            "2026-11-18T10:02:00Z",
            include_start=True,
        )
        self.assertEqual(len(obs_incl), 3)
        self.assertEqual(obs_incl[0].recorded_at, "2026-11-18T10:00:00Z")

        # 4. get_replay_snapshot_at reconstructs lookback window up to target_epoch
        target_ep = parse_iso_to_epoch("2026-11-18T10:10:00Z")
        snap_obs = self.repo.get_replay_snapshot_at(target_ep, lookback_sec=900.0)
        # Should include all 4 rows of 1597 (10:00..10:03) and first 2 rows of 2232 (10:09..10:10) = 6 rows
        self.assertEqual(len(snap_obs), 6)
        self.assertIsInstance(snap_obs[0], ReplayObservation)
        self.assertEqual(snap_obs[-1].position.train_number, "2232")
        self.assertEqual(snap_obs[-1].recorded_at, "2026-11-18T10:10:00Z")

        # 5. Empty DB returns None for get_replay_bounds
        empty_db_path = os.path.join(self.temp_dir, "empty_replay.db")
        empty_db = Database(empty_db_path)
        try:
            empty_repo = HistoricalRepository(empty_db)
            self.assertIsNone(empty_repo.get_replay_bounds())
        finally:
            empty_db.close()


if __name__ == "__main__":
    unittest.main()
