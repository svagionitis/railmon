"""
Unit tests for CLI storage integration: argument parsing, subcommands, and terminal formatters.
"""

import io
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from storage.cli_formatters import (
    format_dwell_table,
    format_file_size,
    format_punctuality_table,
    format_sessions_table,
    format_stats_table,
    format_train_history_table,
    format_trips_table,
)
from storage.models import (
    CorridorPunctualitySummary,
    DatabaseStats,
    RecordingSessionDB,
    StationDwellRecord,
    TelemetryHistoryRow,
    TripSummary,
)
from train_tracker import build_parser, main


class TestCLIStorageParsing(unittest.TestCase):
    """Test suite for Phase 4 storage flags and subcommands."""

    def setUp(self):
        self.parser = build_parser()

    def test_global_storage_flags(self):
        """TC-CLI-P4-01: Global --record and --db flags parse correctly."""
        args = self.parser.parse_args(["--record", "--db", "custom.db", "--no-filter-deadband"])
        self.assertTrue(args.record)
        self.assertEqual(args.db, "custom.db")
        self.assertTrue(args.no_filter_deadband)
        self.assertEqual(args.mode, "stream")

    def test_record_subcommand_defaults(self):
        """TC-CLI-P4-02: 'record' subcommand parses with default arguments."""
        args = self.parser.parse_args(["record"])
        self.assertEqual(args.mode, "record")
        self.assertEqual(args.db, "railway_history.db")
        self.assertFalse(args.demo)
        self.assertEqual(args.batch_size, 50)
        self.assertEqual(args.flush_interval, 2.0)
        self.assertFalse(args.quiet)

    def test_record_subcommand_custom_options(self):
        """TC-CLI-P4-03: 'record' subcommand parses with customized options."""
        args = self.parser.parse_args([
            "record",
            "--demo",
            "--db", "custom_rec.db",
            "--batch-size", "25",
            "--flush-interval", "1.5",
            "--quiet",
            "--no-filter-deadband",
        ])
        self.assertEqual(args.mode, "record")
        self.assertEqual(args.db, "custom_rec.db")
        self.assertTrue(args.demo)
        self.assertEqual(args.batch_size, 25)
        self.assertEqual(args.flush_interval, 1.5)
        self.assertTrue(args.quiet)
        self.assertTrue(args.no_filter_deadband)

    def test_history_trips_subcommand(self):
        """TC-CLI-P4-04: 'history trips' parses date, corridor, and limit."""
        args = self.parser.parse_args([
            "history", "trips",
            "--date", "2026-11-18",
            "--corridor", "Ath-Thess-Ath",
            "--limit", "15",
        ])
        self.assertEqual(args.mode, "history")
        self.assertEqual(args.history_action, "trips")
        self.assertEqual(args.date, "2026-11-18")
        self.assertEqual(args.corridor, "Ath-Thess-Ath")
        self.assertEqual(args.limit, 15)

    def test_history_train_subcommand(self):
        """TC-CLI-P4-05: 'history train' parses train_number and timestamps."""
        args = self.parser.parse_args([
            "history", "train", "IC50",
            "--start", "2026-11-18T06:00:00Z",
            "--end", "2026-11-18T12:00:00Z",
            "--limit", "100",
            "--desc",
        ])
        self.assertEqual(args.mode, "history")
        self.assertEqual(args.history_action, "train")
        self.assertEqual(args.train_number, "IC50")
        self.assertEqual(args.start, "2026-11-18T06:00:00Z")
        self.assertEqual(args.end, "2026-11-18T12:00:00Z")
        self.assertEqual(args.limit, 100)
        self.assertTrue(args.desc)

    def test_history_punctuality_subcommand(self):
        """TC-CLI-P4-06: 'history punctuality' parses corridor and thresholds."""
        args = self.parser.parse_args([
            "history", "punctuality",
            "--corridor", "PATHE",
            "--start-date", "2026-11-01",
            "--end-date", "2026-11-18",
            "--threshold", "10",
        ])
        self.assertEqual(args.mode, "history")
        self.assertEqual(args.history_action, "punctuality")
        self.assertEqual(args.corridor, "PATHE")
        self.assertEqual(args.start_date, "2026-11-01")
        self.assertEqual(args.end_date, "2026-11-18")
        self.assertEqual(args.threshold, 10)

    def test_history_dwell_subcommand(self):
        """TC-CLI-P4-07: 'history dwell' parses train_number, speed, and min_dwell."""
        args = self.parser.parse_args([
            "history", "dwell", "1597",
            "--speed-threshold", "1.5",
            "--min-dwell", "45",
        ])
        self.assertEqual(args.mode, "history")
        self.assertEqual(args.history_action, "dwell")
        self.assertEqual(args.train_number, "1597")
        self.assertEqual(args.speed_threshold, 1.5)
        self.assertEqual(args.min_dwell, 45)

    def test_history_stats_and_sessions(self):
        """TC-CLI-P4-08: 'history stats' and 'history sessions' parse correctly."""
        args_stats = self.parser.parse_args(["history", "stats"])
        self.assertEqual(args_stats.mode, "history")
        self.assertEqual(args_stats.history_action, "stats")

        args_sess = self.parser.parse_args(["history", "sessions", "--limit", "30"])
        self.assertEqual(args_sess.mode, "history")
        self.assertEqual(args_sess.history_action, "sessions")
        self.assertEqual(args_sess.limit, 30)

    def test_history_export_subcommand(self):
        """TC-CLI-P4-09: 'history export' parses format, geometry, and output file."""
        args = self.parser.parse_args([
            "history", "export", "2232",
            "--format", "geojson",
            "--feature-type", "LineString",
            "--out", "run.geojson",
        ])
        self.assertEqual(args.mode, "history")
        self.assertEqual(args.history_action, "export")
        self.assertEqual(args.train_number, "2232")
        self.assertEqual(args.format, "geojson")
        self.assertEqual(args.feature_type, "LineString")
        self.assertEqual(args.out, "run.geojson")


class TestCLIRouting(unittest.TestCase):
    """Test suite for main() routing to Phase 4 handlers."""

    @patch("train_tracker.run_record_mode", return_value=0)
    def test_main_routes_to_record_mode(self, mock_rec):
        """TC-CLI-P4-10: main(['record']) invokes run_record_mode."""
        ret = main(["record", "--demo", "--db", "test_rec.db"])
        self.assertEqual(ret, 0)
        mock_rec.assert_called_once_with(
            db_path="test_rec.db",
            demo=True,
            enable_deadband=True,
            batch_size=50,
            flush_interval_sec=2.0,
            quiet=False,
            reconnect_delay=3.0,
        )

    @patch("train_tracker.run_history_mode", return_value=0)
    def test_main_routes_to_history_mode(self, mock_hist):
        """TC-CLI-P4-11: main(['history', 'stats']) invokes run_history_mode."""
        ret = main(["history", "stats", "--db", "seeded.db"])
        self.assertEqual(ret, 0)
        mock_hist.assert_called_once()

    @patch("train_tracker.run_tui_mode")
    def test_main_routes_to_tui_with_record(self, mock_tui):
        """TC-CLI-P4-12: main(['--tui', '--record']) passes record=True."""
        ret = main(["--tui", "--record", "--db", "tui_hist.db"])
        self.assertEqual(ret, 0)
        mock_tui.assert_called_once_with(
            demo=False,
            fps=20.0,
            reconnect_delay=3.0,
            record=True,
            db_path="tui_hist.db",
            enable_deadband=True,
        )

    @patch("train_tracker.run_plain_stream_mode")
    def test_main_routes_to_stream_with_record(self, mock_stream):
        """TC-CLI-P4-13: main(['--record']) passes record=True to stream."""
        ret = main(["--record", "--db", "stream_rec.db"])
        self.assertEqual(ret, 0)
        mock_stream.assert_called_once_with(
            demo=False,
            reconnect_delay=3.0,
            record=True,
            db_path="stream_rec.db",
            enable_deadband=True,
        )


class TestCLIFormatters(unittest.TestCase):
    """Test suite for CLI terminal output formatting."""

    def test_format_file_size(self):
        """TC-FMT-01: format_file_size handles B, KB, MB, GB."""
        self.assertEqual(format_file_size(500), "500 B")
        self.assertEqual(format_file_size(2048), "2.00 KB")
        self.assertEqual(format_file_size(1048576 * 5), "5.00 MB")
        self.assertEqual(format_file_size(1073741824 * 3), "3.00 GB")

    def test_format_trips_table(self):
        """TC-FMT-02: format_trips_table renders header and rows."""
        trips = [
            TripSummary(
                train_number="IC50",
                service_date="2026-11-18",
                first_seen="2026-11-18T06:22:10Z",
                last_seen="2026-11-18T10:35:40Z",
                total_points=1420,
                max_delay_min=12,
                avg_delay_min=4.2,
                max_speed_kmh=158.4,
                avg_speed_kmh=112.0,
                corridor="Ath-Thess-Ath",
            )
        ]
        out = format_trips_table(trips, date="2026-11-18", corridor="All")
        self.assertIn("Recorded Trips for 2026-11-18", out)
        self.assertIn("IC50", out)
        self.assertIn("Ath-Thess-Ath", out)
        self.assertIn("1,420", out.replace("1420", "1,420"))

        empty_out = format_trips_table([], date="2026-11-18")
        self.assertIn("No matching trips found", empty_out)

    def test_format_train_history_table(self):
        """TC-FMT-03: format_train_history_table renders trajectory breadcrumbs."""
        rows = [
            TelemetryHistoryRow(
                train_number="1597",
                recorded_at="2026-11-18T08:15:00Z",
                lat=38.0450,
                lng=23.7210,
                speed=112.5,
                course=352.0,
                delay_min=2,
                status="Κανονική",
                chainage_m=18400,
                next_station="SKG Main",
            )
        ]
        out = format_train_history_table(rows, train_number="1597")
        self.assertIn("Telemetry History for Train 1597", out)
        self.assertIn("38.0450", out)
        self.assertIn("112.5 km/h", out)
        self.assertIn("Κανονική", out)
        self.assertIn("18,400 m", out)

        empty_out = format_train_history_table([], train_number="1597")
        self.assertIn("No historical telemetry points found", empty_out)

    def test_format_punctuality_table(self):
        """TC-FMT-04: format_punctuality_table renders compliance and delay stats."""
        punc = [
            CorridorPunctualitySummary(
                corridor="Ath-Thess-Ath",
                total_trips=18,
                total_observations=22450,
                avg_delay_min=5.4,
                max_delay_min=42,
                on_time_count=15,
                delayed_count=3,
                severe_delay_count=1,
                punctuality_rate_pct=83.3,
            )
        ]
        out = format_punctuality_table(punc, threshold_min=5)
        self.assertIn("Corridor Punctuality Report", out)
        self.assertIn("Ath-Thess-Ath", out)
        self.assertIn("83.3%", out)
        self.assertIn("Network Average Punctuality: 83.30%", out)

        empty_out = format_punctuality_table([])
        self.assertIn("No corridor punctuality data available", empty_out)

    def test_format_dwell_table(self):
        """TC-FMT-05: format_dwell_table renders stops and dwell intervals."""
        dwells = [
            StationDwellRecord(
                train_number="IC50",
                station_name="Larissa",
                arrival_time="2026-11-18T08:02:40Z",
                departure_time="2026-11-18T08:08:15Z",
                dwell_seconds=335,
                arrival_delay_min=3,
                departure_delay_min=5,
            )
        ]
        out = format_dwell_table(dwells, train_number="IC50")
        self.assertIn("Station Dwell Profile for Train IC50", out)
        self.assertIn("Larissa", out)
        self.assertIn("5m 35s", out)
        self.assertIn("+3m", out)
        self.assertIn("+5m", out)

        empty_out = format_dwell_table([], train_number="IC50")
        self.assertIn("No station stops or dwell events detected", empty_out)

    def test_format_stats_table(self):
        """TC-FMT-06: format_stats_table renders database diagnostics."""
        stats = DatabaseStats(
            total_telemetry_rows=124590,
            total_trains=48,
            total_trips=162,
            total_sessions=12,
            file_size_bytes=14979661,
            earliest_record="2026-11-15T08:00:12Z",
            latest_record="2026-11-18T21:30:45Z",
            wal_mode_enabled=True,
        )
        out = format_stats_table(stats, db_path="railway_history.db")
        self.assertIn("SQLite Telemetry Storage Summary", out)
        self.assertIn("WAL (Write-Ahead Logging)", out)
        self.assertIn("124,590", out)
        self.assertIn("14.29 MB", out)

    def test_format_sessions_table(self):
        """TC-FMT-07: format_sessions_table renders audit records."""
        sessions = [
            RecordingSessionDB(
                session_id=1,
                started_at="2026-11-18T20:00:00Z",
                ended_at="2026-11-18T21:00:00Z",
                mode="live",
                total_records_written=5420,
                client_version="1.0.0",
            )
        ]
        out = format_sessions_table(sessions)
        self.assertIn("Audit Recording Sessions", out)
        self.assertIn("5,420", out)
        self.assertIn("live", out)

        empty_out = format_sessions_table([])
        self.assertIn("No audit recording sessions found", empty_out)


if __name__ == "__main__":
    unittest.main()
