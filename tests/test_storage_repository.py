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
    SpeedProfilePoint,
    StationDwellRecord,
    TelemetryHistoryRow,
    TripSummary,
)
from storage.recorder import RecorderConfig, TelemetryRecorder
from storage.repository import HistoricalRepository
from train_tracker import TrainPosition


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


if __name__ == "__main__":
    unittest.main()
