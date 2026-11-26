"""CLI tests for `railmon export --gtfs-rt` and `railmon gtfs-rt` (F-8.3)."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock

from gtfs_rt.models import FeedMessage
import train_tracker


class TestCliExportGtfsRt(unittest.TestCase):
    """Verify CLI parser registration, snapshot, inspect, DB, and validation flows."""

    def test_parser_accepts_export_and_gtfs_rt_alias(self):
        """TC-CLI-GTFS-01: Parser supports `export --gtfs-rt` and `gtfs-rt` alias flags."""
        parser = train_tracker.build_parser()

        args1 = parser.parse_args(
            ["export", "--gtfs-rt", "--demo", "--feed", "vehicles", "--out", "out.pb"]
        )
        self.assertEqual(args1.mode, "export")
        self.assertTrue(args1.gtfs_rt)
        self.assertTrue(args1.demo)
        self.assertEqual(args1.feed, "vehicles")
        self.assertEqual(args1.out, "out.pb")

        args2 = parser.parse_args(["gtfs-rt", "--serve", "--port", "8090", "--cors"])
        self.assertEqual(args2.mode, "gtfs-rt")
        self.assertTrue(args2.serve)
        self.assertEqual(args2.port, 8090)
        self.assertTrue(args2.cors)

    def test_export_without_gtfs_rt_flag_returns_exit_2(self):
        """TC-CLI-GTFS-02: Bare `railmon export` without --gtfs-rt prints guidance and exits 2."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc = train_tracker.main(["export", "--demo"])
        self.assertEqual(rc, 2)
        self.assertIn("--gtfs-rt", stderr.getvalue())

    def test_snapshot_pb_file_and_inspect_roundtrip(self):
        """TC-CLI-GTFS-03: Demo snapshot exports valid .pb and --inspect decodes it to JSON."""
        with tempfile.TemporaryDirectory() as tmp:
            out_file = os.path.join(tmp, "demo_feed.pb")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = train_tracker.main(
                    ["export", "--gtfs-rt", "--demo", "--out", out_file]
                )
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(out_file))

            with open(out_file, "rb") as fh:
                feed = FeedMessage.from_proto(fh.read())
            self.assertGreater(len(feed.entity), 0)

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc_insp = train_tracker.main(
                    ["export", "--gtfs-rt", "--inspect", out_file]
                )
            self.assertEqual(rc_insp, 0)
            parsed = json.loads(stdout.getvalue())
            self.assertEqual(parsed["summary"]["entityCount"], len(feed.entity))

    def test_demo_json_stdout_and_out_dir_bundle(self):
        """TC-CLI-GTFS-04: --format json prints ProtoJSON to stdout; --out-dir writes 4 files."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = train_tracker.main(["gtfs-rt", "--demo", "--format", "json"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["header"]["gtfsRealtimeVersion"], "2.0")
        self.assertGreater(len(payload["entity"]), 0)

        with tempfile.TemporaryDirectory() as tmp:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc_dir = train_tracker.main(
                    ["export", "--gtfs-rt", "--demo", "--out-dir", tmp]
                )
            self.assertEqual(rc_dir, 0)
            self.assertTrue(os.path.isfile(os.path.join(tmp, "feed.pb")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "vehicle_positions.pb")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "trip_updates.pb")))
            self.assertTrue(os.path.isfile(os.path.join(tmp, "alerts.pb")))

    def test_tty_binary_guard_blocks_raw_pb_to_terminal(self):
        """TC-CLI-GTFS-05: Refuses raw binary protobuf to interactive TTY unless --out - is given."""
        stderr = io.StringIO()
        with mock.patch.object(train_tracker, "is_tty_supported", return_value=True):
            with contextlib.redirect_stderr(stderr):
                rc = train_tracker.main(["export", "--gtfs-rt", "--demo"])
        self.assertEqual(rc, 2)
        self.assertIn("interactive terminal", stderr.getvalue())

    def test_invalid_port_and_missing_db_return_error_codes(self):
        """TC-CLI-GTFS-06: Invalid port returns exit 2; missing --from-db returns exit 1."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc_port = train_tracker.main(
                ["export", "--gtfs-rt", "--serve", "--port", "99999"]
            )
        self.assertEqual(rc_port, 2)

        stderr2 = io.StringIO()
        with contextlib.redirect_stderr(stderr2):
            rc_db = train_tracker.main(
                ["export", "--gtfs-rt", "--from-db", "nonexistent_railmon.db"]
            )
        self.assertEqual(rc_db, 1)

    def test_watch_requires_output_target_and_from_db_cli_works(self):
        """TC-CLI-GTFS-07: --watch without --out/--out-dir returns 2; --from-db exports valid JSON."""
        from tests.test_gtfs_exporter import _seed_sample_db

        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            rc_watch = train_tracker.main(["export", "--gtfs-rt", "--demo", "--watch"])
        self.assertEqual(rc_watch, 2)

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "hist.db")
            _seed_sample_db(db_path)
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc_hist = train_tracker.main(
                    [
                        "export",
                        "--gtfs-rt",
                        "--from-db",
                        db_path,
                        "--at",
                        "2026-11-23T17:00:00Z",
                        "--format",
                        "json",
                    ]
                )
            self.assertEqual(rc_hist, 0)
            doc = json.loads(stdout.getvalue())
            ids = {e["id"] for e in doc["entity"]}
            self.assertIn("vp:IC52", ids)
            self.assertIn("tu:sched-ic52-hist", ids)

    def test_from_db_out_dir_exports_populated_pb_and_json_bundles(self):
        """TC-CLI-GTFS-08: --from-db --out-dir writes populated split .pb and .json feeds with schedules."""
        from tests.test_gtfs_exporter import _seed_sample_db

        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "hist.db")
            _seed_sample_db(db_path)

            pb_dir = os.path.join(tmp, "pb_out")
            os.makedirs(pb_dir, exist_ok=True)
            stderr_pb = io.StringIO()
            with contextlib.redirect_stderr(stderr_pb):
                rc_pb = train_tracker.main(
                    [
                        "export",
                        "--gtfs-rt",
                        "--from-db",
                        db_path,
                        "--at",
                        "2026-11-23T17:00:00Z",
                        "--out-dir",
                        pb_dir,
                    ]
                )
            self.assertEqual(rc_pb, 0)

            with open(os.path.join(pb_dir, "feed.pb"), "rb") as fh:
                feed_msg = FeedMessage.from_proto(fh.read())
            with open(os.path.join(pb_dir, "vehicle_positions.pb"), "rb") as fh:
                vp_msg = FeedMessage.from_proto(fh.read())
            with open(os.path.join(pb_dir, "trip_updates.pb"), "rb") as fh:
                tu_msg = FeedMessage.from_proto(fh.read())
            with open(os.path.join(pb_dir, "alerts.pb"), "rb") as fh:
                al_msg = FeedMessage.from_proto(fh.read())

            feed_ids = {e.id for e in feed_msg.entity}
            self.assertIn("vp:IC52", feed_ids)
            self.assertIn("tu:sched-ic52-hist", feed_ids)

            vp_ids = {e.id for e in vp_msg.entity}
            self.assertEqual(vp_ids, {"vp:IC52"})

            tu_by_id = {e.id: e for e in tu_msg.entity}
            self.assertIn("tu:sched-ic52-hist", tu_by_id)
            tu_entity = tu_by_id["tu:sched-ic52-hist"].trip_update
            self.assertIsNotNone(tu_entity)
            assert tu_entity is not None
            self.assertEqual(len(tu_entity.stop_time_update), 1)
            self.assertEqual(tu_entity.stop_time_update[0].stop_id, "LAR")

            # All 4 split feeds share the identical historical snapshot timestamp
            self.assertEqual(
                {
                    feed_msg.header.timestamp,
                    vp_msg.header.timestamp,
                    tu_msg.header.timestamp,
                    al_msg.header.timestamp,
                },
                {feed_msg.header.timestamp},
            )

            # Verify --format json --out-dir produces populated ProtoJSON files
            json_dir = os.path.join(tmp, "json_out")
            os.makedirs(json_dir, exist_ok=True)
            stderr_json = io.StringIO()
            with contextlib.redirect_stderr(stderr_json):
                rc_json = train_tracker.main(
                    [
                        "export",
                        "--gtfs-rt",
                        "--from-db",
                        db_path,
                        "--at",
                        "2026-11-23T17:00:00Z",
                        "--format",
                        "json",
                        "--out-dir",
                        json_dir,
                    ]
                )
            self.assertEqual(rc_json, 0)

            for fname, expected_id in (
                ("feed.json", "vp:IC52"),
                ("vehicle_positions.json", "vp:IC52"),
                ("trip_updates.json", "tu:sched-ic52-hist"),
            ):
                with open(os.path.join(json_dir, fname), "r", encoding="utf-8") as fh:
                    doc = json.load(fh)
                ids = {e["id"] for e in doc["entity"]}
                self.assertIn(expected_id, ids)
            self.assertTrue(os.path.isfile(os.path.join(json_dir, "alerts.json")))

    def test_tc_http_15_gtfs_serve_http_limit_flags_and_validation(self):
        """TC-HTTP-15 (export): Parse and validate --max-connections and --request-timeout."""
        parser = train_tracker.build_parser()
        args = parser.parse_args(
            ["gtfs-rt", "--serve", "--max-connections", "32", "--request-timeout", "6.5"]
        )
        self.assertEqual(args.max_connections, 32)
        self.assertAlmostEqual(args.request_timeout, 6.5)

        for bad_flags in (
            ["gtfs-rt", "--serve", "--max-connections", "0"],
            ["gtfs-rt", "--serve", "--request-timeout", "0"],
            ["gtfs-rt", "--serve", "--request-timeout", "nan"],
            ["gtfs-rt", "--serve", "--request-timeout", "inf"],
        ):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = train_tracker.main(bad_flags)
            self.assertEqual(rc, 2, f"Expected exit code 2 for {bad_flags}")
            self.assertIn("[ERROR]", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
