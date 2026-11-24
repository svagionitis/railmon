"""Unit and integration tests for GTFS-RT file export, SQLite bridge, and inspector."""

import os
from pathlib import Path
import tempfile
import unittest

import gtfs_rt
from gtfs_rt.exporter import (
    export_feed_dir,
    export_feed_file,
    export_from_db,
    inspect_pb_file,
    load_db_snapshot,
    write_atomic_bytes,
)
from gtfs_rt.mapper import GtfsRtMapper
from gtfs_rt.models import FeedKind, FeedMessage, GtfsRtConfig, OutputFormat
from storage.db import Database
from tui.mock_engine import MockTelemetryEngine


def _seed_sample_db(db_path: str) -> None:
    """Populate a temporary SQLite WAL database with trains, trips, stops, and telemetry."""
    db = Database(db_path)
    try:
        db.init_schema()
        with db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO trains (
                    train_number, locomotive_number, default_name, train_type,
                    service_type, first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "IC52",
                    "120-018",
                    "InterCity 52",
                    "InterCity",
                    "InterCity",
                    "2026-11-23T15:00:00Z",
                    "2026-11-23T17:00:00Z",
                ),
            )
            cur.execute(
                """
                INSERT INTO scheduled_trips (
                    schedule_id, train_number, origin, destination, corridor,
                    service_date, scheduled_departure, scheduled_arrival
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "sched-ic52-hist",
                    "IC52",
                    "Αθήνα",
                    "Θεσσαλονίκη",
                    "E85",
                    "2026-11-23",
                    "2026-11-23T15:00:00Z",
                    "2026-11-23T19:30:00Z",
                ),
            )
            cur.execute(
                """
                INSERT INTO schedule_stops (
                    schedule_id, stop_sequence, station_id, station_name,
                    scheduled_arrival, scheduled_departure, delay_sec
                ) VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "sched-ic52-hist",
                    1,
                    "LAR",
                    "Λάρισα",
                    "2026-11-23T17:30:00Z",
                    "2026-11-23T17:33:00Z",
                    480,
                ),
            )
            cur.execute(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course,
                    track_heading, altitude, delay_min, status, progress, chainage_m,
                    dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    "IC52",
                    "sched-ic52-hist",
                    "2026-11-23T16:59:30Z",
                    39.4821,
                    22.3400,
                    142.0,
                    340.0,
                    341.0,
                    110.0,
                    8,
                    "Κανονική",
                    0.65,
                    310000,
                    175000,
                    "Λάρισα",
                    "E85",
                ),
            )
    finally:
        db.close()


class TestGtfsExporter(unittest.TestCase):
    """Verify atomic file writes, path security, SQLite snapshots, and .pb inspection."""

    def test_atomic_write_and_roundtrip_inspect(self):
        """TC-GTFS-EXP-01: export_feed_file writes atomically and inspect_pb_file decodes accurately."""
        positions = MockTelemetryEngine().step(1.0)
        mapper = GtfsRtMapper()
        msg = mapper.build_from_positions(positions, feed_kind=FeedKind.COMBINED)

        with tempfile.TemporaryDirectory() as tmp:
            out_path = os.path.join(tmp, "hellenic.pb")
            saved = export_feed_file(msg, out_path, fmt=OutputFormat.PROTOBUF)
            self.assertTrue(os.path.isfile(saved))
            # No leftover .tmp files
            leftovers = [f for f in os.listdir(tmp) if ".tmp." in f]
            self.assertEqual(leftovers, [])

            report = inspect_pb_file(saved)
            self.assertEqual(report["summary"]["gtfsRealtimeVersion"], "2.0")
            self.assertEqual(report["summary"]["entityCount"], len(msg.entity))
            self.assertGreater(report["summary"]["vehiclePositions"], 0)
            self.assertGreater(report["summary"]["tripUpdates"], 0)

    def test_directory_traversal_rejected_on_export(self):
        """TC-GTFS-EXP-02: Path traversal ('../') is blocked by validate_export_path."""
        with self.assertRaises(ValueError):
            write_atomic_bytes("../escape_feed.pb", b"\x00")
        with self.assertRaises(ValueError):
            export_feed_dir(GtfsRtMapper(), "../escape_dir", fmt=OutputFormat.PROTOBUF)

    def test_split_feed_directory_bundle_export(self):
        """TC-GTFS-EXP-03: export_feed_dir writes feed, vehicle_positions, trip_updates, alerts."""
        positions = MockTelemetryEngine().step(1.0)
        mapper = GtfsRtMapper()
        with tempfile.TemporaryDirectory() as tmp:
            files = export_feed_dir(
                mapper=mapper,
                out_dir=tmp,
                fmt=OutputFormat.PROTOBUF,
                positions=positions,
            )
            self.assertEqual(len(files), 4)
            names = {Path(p).name for p in files}
            self.assertEqual(
                names,
                {"feed.pb", "vehicle_positions.pb", "trip_updates.pb", "alerts.pb"},
            )
            for fpath in files:
                decoded = FeedMessage.from_proto(Path(fpath).read_bytes())
                self.assertEqual(decoded.header.gtfs_realtime_version, "2.0")

    def test_export_from_sqlite_with_at_and_schedule_stops(self):
        """TC-GTFS-EXP-04: Historical SQLite snapshot builds VehiclePosition and enriched TripUpdate."""
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "history.db")
            _seed_sample_db(db_path)

            mapper = GtfsRtMapper(config=GtfsRtConfig(include_alerts=False))
            feed = export_from_db(
                db_path=db_path,
                mapper=mapper,
                feed_kind=FeedKind.COMBINED,
                at_time="2026-11-23T17:00:00Z",
                window_sec=120,
            )
            by_id = {e.id: e for e in feed.entity}
            self.assertIn("vp:IC52", by_id)
            self.assertIn("tu:sched-ic52-hist", by_id)

            tu = by_id["tu:sched-ic52-hist"].trip_update
            assert tu is not None
            self.assertEqual(tu.delay, 480)
            self.assertEqual(len(tu.stop_time_update), 1)
            self.assertEqual(tu.stop_time_update[0].stop_id, "LAR")
            assert tu.trip.start_time is not None

            # Also verify latest-record default (at_time=None)
            feed_latest = export_from_db(db_path=db_path, mapper=mapper)
            self.assertEqual(len(feed_latest.entity), 2)

    def test_missing_db_and_invalid_at_errors(self):
        """TC-GTFS-EXP-05: Missing DB raises FileNotFoundError; invalid --at raises ValueError."""
        mapper = GtfsRtMapper()
        with self.assertRaises(FileNotFoundError):
            export_from_db("does_not_exist_12345.db", mapper)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "history.db")
            _seed_sample_db(db_path)
            with self.assertRaises(ValueError):
                export_from_db(db_path, mapper, at_time="not-a-timestamp")

    def test_load_db_snapshot_and_package_export(self):
        """TC-GTFS-EXP-06: load_db_snapshot loads positions/schedules/epoch and is exported by gtfs_rt."""
        self.assertIs(gtfs_rt.load_db_snapshot, load_db_snapshot)
        with self.assertRaises(FileNotFoundError):
            load_db_snapshot("does_not_exist_98765.db")

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "history.db")
            _seed_sample_db(db_path)

            positions, schedules, ref_ts = load_db_snapshot(
                db_path=db_path,
                at_time="2026-11-23T17:00:00Z",
                window_sec=120,
            )
            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0].train_number, "IC52")
            self.assertIn("sched-ic52-hist", schedules)
            self.assertIsNotNone(ref_ts)

            with self.assertRaises(ValueError):
                load_db_snapshot(db_path=db_path, at_time="not-a-timestamp")

    def test_export_feed_dir_requires_input_and_pins_timestamp(self):
        """TC-GTFS-EXP-07: export_feed_dir fails loud on missing inputs, allows empty list, and pins timestamp."""
        mapper = GtfsRtMapper()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                export_feed_dir(mapper=mapper, out_dir=tmp, fmt=OutputFormat.PROTOBUF)

            empty_files = export_feed_dir(
                mapper=mapper,
                out_dir=tmp,
                fmt=OutputFormat.PROTOBUF,
                positions=[],
            )
            self.assertEqual(len(empty_files), 4)
            timestamps = {
                FeedMessage.from_proto(Path(f).read_bytes()).header.timestamp
                for f in empty_files
            }
            self.assertEqual(len(timestamps), 1)


if __name__ == "__main__":
    unittest.main()
