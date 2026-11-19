"""
End-to-end integration tests for Phase 4: CLI, StreamWorker ingestion, TUI indicators, and Historical queries.
"""

import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from storage.db import Database
from storage.models import (
    CorridorPunctualitySummary,
    RecordingSessionDB,
    TelemetryHistoryRow,
    TrainPosition,
    TripSummary,
)
from storage.recorder import RecorderConfig, TelemetryRecorder
from storage.repository import HistoricalRepository
from train_tracker import build_parser, run_history_mode, run_record_mode
from tui.renderer import TUIRenderer
from tui.state import ConnectionStatus, StateSnapshot, TrainStateStore
from tui.stream_worker import StreamWorker


def _sample_position(
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


class TestStreamWorkerRecorderIntegration(unittest.TestCase):
    """Test suite verifying StreamWorker forwarding to TelemetryRecorder."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "worker_int.db")
        self.db = Database(self.db_path)
        self.db.init_schema()

    def tearDown(self):
        if hasattr(self, "db"):
            self.db.close()
        self.temp_dir.cleanup()

    def test_worker_forwards_batches_to_recorder(self):
        """TC-INT-P4-01: StreamWorker forwards received telemetry batches to TelemetryRecorder."""
        import threading
        store = TrainStateStore()
        config = RecorderConfig(
            batch_size=5,
            flush_interval_sec=0.1,
            enable_deadband=False,
            mode="demo",
        )
        recorder = TelemetryRecorder(self.db, config)
        recorder.start()

        stream_done = threading.Event()

        def _finite_stream():
            for i in range(10):
                yield [_sample_position("IC50", lat=38.0 + i * 0.01, lng=23.7, speed=100.0)]
            stream_done.wait(timeout=2.0)

        worker = StreamWorker(
            store=store,
            custom_stream=_finite_stream(),
            recorder=recorder,
        )
        worker.start()
        time.sleep(0.4)
        stream_done.set()

        worker.stop()
        recorder.stop()

        repo = HistoricalRepository(self.db)
        history = repo.get_train_history("IC50")
        self.assertEqual(len(history), 10)
        self.assertEqual(store.get_snapshot().total_messages_received, 10)


class TestTUIHeaderRecordingBadge(unittest.TestCase):
    """Test suite verifying TUI visual recording indicator [● REC]."""

    def setUp(self):
        self.renderer = TUIRenderer()

    def test_header_shows_recording_badge_when_active(self):
        """TC-INT-P4-02: Header renders [● REC] badge when snapshot.is_recording is True."""
        store = TrainStateStore()
        store.set_recording_status(True, "railway_history.db")
        snapshot = store.get_snapshot()
        self.assertTrue(snapshot.is_recording)

        mock_win = MagicMock()
        mock_win.getmaxyx.return_value = (1, 120)

        self.renderer.render_header(mock_win, snapshot, width=120)

        rendered_texts = [call[0][2] for call in mock_win.addstr.call_args_list]
        found_badge = any("[● REC]" in t for t in rendered_texts)
        self.assertTrue(found_badge, f"Badge not found in rendered texts: {rendered_texts}")

    def test_header_omits_recording_badge_when_inactive(self):
        """TC-INT-P4-03: Header omits [● REC] badge when snapshot.is_recording is False."""
        store = TrainStateStore()
        store.set_recording_status(False)
        snapshot = store.get_snapshot()
        self.assertFalse(snapshot.is_recording)

        mock_win = MagicMock()
        mock_win.getmaxyx.return_value = (1, 120)

        self.renderer.render_header(mock_win, snapshot, width=120)

        rendered_texts = [call[0][2] for call in mock_win.addstr.call_args_list]
        found_badge = any("[● REC]" in t for t in rendered_texts)
        self.assertFalse(found_badge)


class TestHeadlessRecordDaemon(unittest.TestCase):
    """Test suite for run_record_mode headless daemon."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "daemon_test.db")

    def tearDown(self):
        if hasattr(self, "db"):
            self.db.close()
        self.temp_dir.cleanup()

    def test_run_record_mode_demo_lifecycle(self):
        """TC-INT-P4-04: run_record_mode initializes, persists demo feed, and stops gracefully."""
        # Run demo generator with simulated interrupt after 3 batches
        call_count = 0

        def _mock_stream(*args, **kwargs):
            nonlocal call_count
            for i in range(5):
                call_count += 1
                yield [_sample_position(f"TRAIN_{i}", lat=38.0 + i * 0.05, lng=23.7)]
                if call_count >= 3:
                    raise KeyboardInterrupt()

        with patch("train_tracker.RailwayGovClient.stream_train_positions", side_effect=_mock_stream):
            with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
                ret = run_record_mode(
                    db_path=self.db_path,
                    demo=False,
                    enable_deadband=True,
                    batch_size=2,
                    flush_interval_sec=0.1,
                    quiet=False,
                )
                self.assertEqual(ret, 0)
                out_str = mock_out.getvalue()
                self.assertIn("railmon - Hellenic Railways Telemetry Ingestion Daemon", out_str)
                self.assertIn("Shutting down recorder", out_str)
                self.assertIn("Session #", out_str)

        db = Database(self.db_path)
        try:
            repo = HistoricalRepository(db)
            stats = repo.get_database_stats()
            self.assertGreater(stats.total_telemetry_rows, 0)
        finally:
            db.close()


class TestHistoryCLIIntegration(unittest.TestCase):
    """Test suite for run_history_mode querying seeded SQLite databases."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "history_seeded.db")
        self.db = Database(self.db_path)
        self.db.init_schema()

        # Seed telemetry rows
        recorder = TelemetryRecorder(
            self.db,
            RecorderConfig(batch_size=10, flush_interval_sec=0.1, enable_deadband=False),
        )
        recorder.start()
        # Seed 4 points for IC50
        recorder.enqueue_batch([
            _sample_position("IC50", lat=38.0, lng=23.7, speed=0.0, station="Larissa", timestamp="2026-11-18T08:00:00Z"),
            _sample_position("IC50", lat=38.0, lng=23.7, speed=0.0, station="Larissa", timestamp="2026-11-18T08:05:00Z"),
            _sample_position("IC50", lat=38.1, lng=23.7, speed=120.0, station="Katerini", timestamp="2026-11-18T08:30:00Z"),
            _sample_position("IC50", lat=38.2, lng=23.7, speed=140.0, station="Platy", timestamp="2026-11-18T09:00:00Z"),
        ])
        time.sleep(0.3)
        recorder.stop()

        self.parser = build_parser()

    def tearDown(self):
        if hasattr(self, "db"):
            self.db.close()
        self.temp_dir.cleanup()

    def test_history_missing_database_error(self):
        """TC-INT-P4-05: Missing database prints error to stderr and returns code 1."""
        args = self.parser.parse_args(["history", "stats", "--db", "nonexistent.db"])
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            ret = run_history_mode(args)
            self.assertEqual(ret, 1)
            self.assertIn("not found", mock_err.getvalue())

    def test_history_missing_action(self):
        """TC-INT-P4-06: Running history without action prints helpful guidance."""
        args = self.parser.parse_args(["history", "--db", self.db_path])
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = run_history_mode(args)
            self.assertEqual(ret, 0)
            self.assertIn("Please specify a history action", mock_out.getvalue())

    def test_history_stats(self):
        """TC-INT-P4-07: history stats outputs table with row counts."""
        args = self.parser.parse_args(["history", "stats", "--db", self.db_path])
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = run_history_mode(args)
            self.assertEqual(ret, 0)
            out = mock_out.getvalue()
            self.assertIn("SQLite Telemetry Storage Summary", out)
            self.assertIn("Total Telemetry Rows:", out)

    def test_history_train(self):
        """TC-INT-P4-08: history train IC50 displays telemetry rows."""
        args = self.parser.parse_args(["history", "train", "IC50", "--db", self.db_path])
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = run_history_mode(args)
            self.assertEqual(ret, 0)
            out = mock_out.getvalue()
            self.assertIn("Telemetry History for Train IC50", out)
            self.assertIn("Larissa", out)

    def test_history_punctuality(self):
        """TC-INT-P4-09: history punctuality displays corridor metrics."""
        args = self.parser.parse_args(["history", "punctuality", "--db", self.db_path])
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = run_history_mode(args)
            self.assertEqual(ret, 0)
            out = mock_out.getvalue()
            self.assertIn("Corridor Punctuality Report", out)
            self.assertIn("Ath-Thess-Ath", out)

    def test_history_dwell(self):
        """TC-INT-P4-10: history dwell IC50 displays station dwell intervals."""
        args = self.parser.parse_args([
            "history", "dwell", "IC50",
            "--speed-threshold", "2.0",
            "--min-dwell", "20",
            "--db", self.db_path,
        ])
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = run_history_mode(args)
            self.assertEqual(ret, 0)
            out = mock_out.getvalue()
            self.assertIn("Station Dwell Profile for Train IC50", out)
            self.assertIn("Larissa", out)

    def test_history_export_geojson_file(self):
        """TC-INT-P4-11: history export IC50 writes RFC 7946 GeoJSON to file."""
        out_file = os.path.join(self.temp_dir.name, "exported_run.geojson")
        args = self.parser.parse_args([
            "history", "export", "IC50",
            "--format", "geojson",
            "--out", out_file,
            "--db", self.db_path,
        ])
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = run_history_mode(args)
            self.assertEqual(ret, 0)
            self.assertIn("Exported GeoJSON", mock_out.getvalue())

        self.assertTrue(os.path.exists(out_file))
        with open(out_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data.get("type"), "FeatureCollection")
        self.assertGreater(len(data.get("features", [])), 0)

    def test_history_export_csv_file(self):
        """TC-INT-P4-12: history export IC50 writes CSV to file."""
        out_file = os.path.join(self.temp_dir.name, "exported_run.csv")
        args = self.parser.parse_args([
            "history", "export", "IC50",
            "--format", "csv",
            "--out", out_file,
            "--db", self.db_path,
        ])
        with patch("sys.stdout", new_callable=io.StringIO) as mock_out:
            ret = run_history_mode(args)
            self.assertEqual(ret, 0)
            self.assertIn("Exported", mock_out.getvalue())

        self.assertTrue(os.path.exists(out_file))
        with open(out_file, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("train_number", content)
        self.assertIn("IC50", content)

    def test_history_export_rejects_path_traversal(self):
        """TC-INT-P4-13: history export rejects path traversal with error code 1."""
        args_geojson = self.parser.parse_args([
            "history", "export", "IC50",
            "--format", "geojson",
            "--out", "../evil_traversal.geojson",
            "--db", self.db_path,
        ])
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            ret = run_history_mode(args_geojson)
            self.assertEqual(ret, 1)
            self.assertIn("Directory traversal ('..') prohibited", mock_err.getvalue())

        args_csv = self.parser.parse_args([
            "history", "export", "IC50",
            "--format", "csv",
            "--out", "../evil_traversal.csv",
            "--db", self.db_path,
        ])
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            ret = run_history_mode(args_csv)
            self.assertEqual(ret, 1)
            self.assertIn("Directory traversal ('..') prohibited", mock_err.getvalue())

    def test_history_export_rejects_missing_directory(self):
        """TC-INT-P4-14: history export rejects nonexistent destination directories with error code 1."""
        bad_target = os.path.join(self.temp_dir.name, "missing_sub_folder", "out.geojson")
        args = self.parser.parse_args([
            "history", "export", "IC50",
            "--format", "geojson",
            "--out", bad_target,
            "--db", self.db_path,
        ])
        with patch("sys.stderr", new_callable=io.StringIO) as mock_err:
            ret = run_history_mode(args)
            self.assertEqual(ret, 1)
            self.assertIn("Export destination directory does not exist", mock_err.getvalue())


if __name__ == "__main__":
    unittest.main()
