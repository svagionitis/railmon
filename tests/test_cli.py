"""Unit tests for train_tracker CLI argument parsing, routing, and lifecycle."""

import io
import unittest
from unittest.mock import MagicMock, patch

import train_tracker
from train_tracker import (
    DeadbandFilterMode,
    GlyphMode,
    RecordingMode,
    TelemetrySource,
    build_parser,
    main,
    run_tui_mode,
)


class TestCLIArgumentParser(unittest.TestCase):
    """Test suite for CLI argument parser configuration and defaults."""

    def setUp(self):
        self.parser = build_parser()

    def test_default_arguments(self):
        """TC-CLI-01: Default arguments specify live stream without TUI."""
        args = self.parser.parse_args([])
        self.assertEqual(args.mode, "stream")
        self.assertFalse(args.tui)
        self.assertFalse(args.demo)
        self.assertFalse(args.ascii)
        self.assertEqual(args.fps, 15.0)
        self.assertEqual(args.reconnect_delay, 3.0)

    def test_ascii_flag(self):
        """TC-CLI-01B: --ascii flag parsed correctly."""
        args = self.parser.parse_args(["--ascii"])
        self.assertTrue(args.ascii)

    def test_tui_flag_live(self):
        """TC-CLI-02: --tui sets tui=True and mode=stream."""
        args = self.parser.parse_args(["--tui"])
        self.assertEqual(args.mode, "stream")
        self.assertTrue(args.tui)
        self.assertFalse(args.demo)

    def test_tui_flag_demo(self):
        """TC-CLI-03: --tui and --demo enable offline interactive TUI."""
        args = self.parser.parse_args(["--tui", "--demo"])
        self.assertEqual(args.mode, "stream")
        self.assertTrue(args.tui)
        self.assertTrue(args.demo)

    def test_demo_flag_plain(self):
        """TC-CLI-04: --demo without --tui specifies plain text simulation stream."""
        args = self.parser.parse_args(["--demo"])
        self.assertEqual(args.mode, "stream")
        self.assertFalse(args.tui)
        self.assertTrue(args.demo)

    def test_schedules_mode(self):
        """TC-CLI-05: Positional argument schedules sets mode=schedules."""
        args = self.parser.parse_args(["schedules"])
        self.assertEqual(args.mode, "schedules")
        self.assertFalse(args.tui)

    def test_custom_fps(self):
        """TC-CLI-06: Custom --fps float value parsed correctly."""
        args = self.parser.parse_args(["--tui", "--fps", "30.0"])
        self.assertEqual(args.fps, 30.0)

    def test_custom_reconnect_delay(self):
        """TC-CLI-07: Custom --reconnect-delay parsed correctly."""
        args = self.parser.parse_args(["--reconnect-delay", "5.5"])
        self.assertEqual(args.reconnect_delay, 5.5)

    def test_invalid_mode(self):
        """TC-CLI-08: Invalid positional mode raises SystemExit with error."""
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as cm:
                self.parser.parse_args(["unknown_mode"])
            self.assertEqual(cm.exception.code, 2)

    def test_help_flag(self):
        """TC-CLI-09: --help prints help text and exits with code 0."""
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with self.assertRaises(SystemExit) as cm:
                self.parser.parse_args(["--help"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn("Hellenic Railways", mock_stdout.getvalue())

    def test_version_flag(self):
        """TC-CLI-10: --version prints version and exits with code 0."""
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            with self.assertRaises(SystemExit) as cm:
                self.parser.parse_args(["--version"])
            self.assertEqual(cm.exception.code, 0)
            self.assertIn(f"railmon {train_tracker.__version__}", mock_stdout.getvalue())

    def test_log_file_and_level_flags(self):
        """TC-CLI-11: --log-file and --log-level parsed correctly."""
        args = self.parser.parse_args(["--log-file", "audit.log", "--log-level", "DEBUG"])
        self.assertEqual(args.log_file, "audit.log")
        self.assertEqual(args.log_level, "DEBUG")

    def test_debug_flag(self):
        """TC-CLI-12: --debug flag enables debug shortcut."""
        args = self.parser.parse_args(["--debug"])
        self.assertTrue(args.debug)

    def test_replay_flags(self):
        """TC-F42-08a: --replay and --replay-speed flags parsed correctly."""
        args = self.parser.parse_args(["--tui", "--replay", "my_history.db", "--replay-speed", "15"])
        self.assertEqual(args.replay, "my_history.db")
        self.assertEqual(args.replay_speed, 15.0)


class TestCLIDispatchAndLifecycle(unittest.TestCase):
    """Test suite for CLI dispatch routing and process lifecycle safety."""

    @patch("train_tracker.run_tui_mode")
    def test_main_routes_to_tui_live(self, mock_run_tui):
        """TC-CLI-02b: main() routes --tui to run_tui_mode(source=TelemetrySource.LIVE)."""
        ret = main(["--tui", "--fps", "25.0"])
        self.assertEqual(ret, 0)
        mock_run_tui.assert_called_once_with(
            source=TelemetrySource.LIVE, fps=25.0, reconnect_delay=3.0, glyph_mode=GlyphMode.AUTO
        )

    @patch("train_tracker.run_tui_mode")
    def test_main_routes_to_tui_demo(self, mock_run_tui):
        """TC-CLI-03b: main() routes --tui --demo to run_tui_mode(source=TelemetrySource.DEMO)."""
        ret = main(["--tui", "--demo"])
        self.assertEqual(ret, 0)
        mock_run_tui.assert_called_once_with(
            source=TelemetrySource.DEMO, fps=15.0, reconnect_delay=3.0, glyph_mode=GlyphMode.AUTO
        )

    @patch("train_tracker.run_plain_stream_mode")
    def test_main_routes_to_plain_stream(self, mock_plain):
        """TC-CLI-04b: main() routes default to run_plain_stream_mode."""
        ret = main([])
        self.assertEqual(ret, 0)
        mock_plain.assert_called_once_with(source=TelemetrySource.LIVE, reconnect_delay=3.0)

    @patch("train_tracker.run_plain_stream_mode")
    def test_main_routes_to_plain_demo(self, mock_plain):
        """TC-CLI-04c: main() routes --demo to run_plain_stream_mode(source=TelemetrySource.DEMO)."""
        ret = main(["--demo", "--reconnect-delay", "4.0"])
        self.assertEqual(ret, 0)
        mock_plain.assert_called_once_with(source=TelemetrySource.DEMO, reconnect_delay=4.0)

    @patch("train_tracker.run_schedules_mode")
    def test_main_routes_to_schedules(self, mock_schedules):
        """TC-CLI-05b: main() routes schedules to run_schedules_mode()."""
        ret = main(["schedules"])
        self.assertEqual(ret, 0)
        mock_schedules.assert_called_once()

    @patch("train_tracker.run_plain_stream_mode", side_effect=KeyboardInterrupt)
    def test_keyboard_interrupt_plain(self, mock_plain):
        """TC-CLI-11: KeyboardInterrupt is handled cleanly and returns 0."""
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            ret = main([])
            self.assertEqual(ret, 0)
            self.assertIn("Process interrupted by user.", mock_stdout.getvalue())

    @patch("train_tracker.is_tty_supported", return_value=True)
    @patch("tui.app.run_tui", side_effect=RuntimeError("Simulated Curses crash"))
    @patch("tui.stream_worker.StreamWorker.start")
    @patch("tui.stream_worker.StreamWorker.stop")
    def test_tui_worker_cleanup_on_error(self, mock_worker_stop, mock_worker_start, mock_run_tui, mock_is_tty):
        """TC-CLI-12: worker.stop() is guaranteed to execute even if run_tui raises."""
        with self.assertRaises(RuntimeError):
            run_tui_mode(demo=True)
        mock_worker_start.assert_called_once()
        mock_worker_stop.assert_called_once()

    @patch("train_tracker.main", return_value=0)
    def test_legacy_demo_cli_compatibility(self, mock_main):
        """Verify legacy demo_cli() delegates to main()."""
        train_tracker.demo_cli()
        mock_main.assert_called_once()

    @patch("train_tracker.is_tty_supported", return_value=True)
    @patch("train_tracker.run_plain_stream_mode")
    @patch("sys.platform", "win32")
    def test_run_tui_mode_windows_missing_curses(self, mock_plain, mock_is_tty):
        """Verify friendly error message and fallback on Windows when windows-curses is missing."""
        orig_import = __import__

        def fake_import(name, *args, **kwargs):
            if "tui.app" in name or name in ("curses", "_curses"):
                raise ModuleNotFoundError("No module named '_curses'")
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                train_tracker.run_tui_mode(demo=True)
                self.assertIn("windows-curses", mock_stderr.getvalue())
                mock_plain.assert_called_once_with(
                    source=TelemetrySource.DEMO,
                    reconnect_delay=3.0,
                    recording=RecordingMode.DISABLED,
                    db_path="railway_history.db",
                    deadband=DeadbandFilterMode.ENABLED,
                    retention_days=None,
                    archive_path=None,
                )


class TestCLISchedulesResilience(unittest.TestCase):
    """Test suite for railmon schedules CLI resilience and error handling."""

    @patch("train_tracker.RailwayGovClient.get_upcoming_schedules")
    def test_run_schedules_mode_success(self, mock_schedules):
        """TC-CLI-13: run_schedules_mode prints upcoming timetables and returns 0."""
        mock_schedules.return_value = [
            {
                "trainNumber": "1597",
                "origin": {"nameGreek": "Αθήνα"},
                "destination": {"nameGreek": "Θεσσαλονίκη"},
                "scheduledDeparture": "07:15",
            }
        ]
        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            ret = train_tracker.run_schedules_mode()
            self.assertEqual(ret, 0)
            out = mock_stdout.getvalue()
            self.assertIn("Train  1597", out)
            self.assertIn("Αθήνα -> Θεσσαλονίκη", out)

    @patch("train_tracker.RailwayGovClient.get_upcoming_schedules")
    def test_run_schedules_mode_network_failure(self, mock_schedules):
        """TC-CLI-14: Network failure outputs clean error to stderr without traceback and returns 1."""
        mock_schedules.side_effect = train_tracker.RailwayAPIError("Connection refused by peer")
        with patch("sys.stdout", new_callable=io.StringIO):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                ret = train_tracker.run_schedules_mode()
                self.assertEqual(ret, 1)
                self.assertIn("[Error] Failed to fetch train timetables", mock_stderr.getvalue())
                self.assertIn("Connection refused by peer", mock_stderr.getvalue())

    @patch("train_tracker.RailwayGovClient.get_upcoming_schedules")
    def test_main_schedules_returns_exit_code_1_on_failure(self, mock_schedules):
        """TC-CLI-15: main(['schedules']) returns 1 when schedule retrieval fails."""
        mock_schedules.side_effect = train_tracker.RailwayAPIError("HTTP 503 Server Error")
        with patch("sys.stdout", new_callable=io.StringIO):
            with patch("sys.stderr", new_callable=io.StringIO):
                ret = train_tracker.main(["schedules"])
                self.assertEqual(ret, 1)


class TestCLITTYAndHeadlessFallback(unittest.TestCase):
    """Test suite for TTY detection, curses.error fallback, and headless CLI flags."""

    def test_is_tty_supported_true_and_false(self):
        """TC-TTY-01: is_tty_supported reflects sys.stdout.isatty status."""
        with patch("sys.stdout.isatty", return_value=True):
            self.assertTrue(train_tracker.is_tty_supported())

        with patch("sys.stdout.isatty", return_value=False):
            self.assertFalse(train_tracker.is_tty_supported())

        mock_stdout = object()
        with patch("sys.stdout", mock_stdout):
            self.assertFalse(train_tracker.is_tty_supported())

    def test_run_tui_raises_curses_error_on_non_tty(self):
        """TC-TTY-02: tui.app.run_tui raises curses.error when sys.stdout is not a TTY."""
        try:
            import curses
            from tui.app import run_tui
        except ImportError:
            self.skipTest("curses not available in environment")

        mock_store = MagicMock()
        mock_worker = MagicMock()
        with patch("tui.app.is_tty_supported", return_value=False):
            with self.assertRaises(curses.error) as ctx:
                run_tui(store=mock_store, worker=mock_worker)
            self.assertIn("not an interactive TTY", str(ctx.exception))

    @patch("train_tracker.run_plain_stream_mode")
    @patch("train_tracker.is_tty_supported", return_value=False)
    def test_run_tui_mode_falls_back_on_non_tty(self, mock_is_tty, mock_plain):
        """TC-TTY-03: run_tui_mode falls back to run_plain_stream_mode when stdout is not a TTY."""
        with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            train_tracker.run_tui_mode(
                source=TelemetrySource.DEMO,
                reconnect_delay=5.0,
                recording=RecordingMode.ENABLED,
                db_path="test_non_tty.db",
                deadband=DeadbandFilterMode.DISABLED,
                retention_days=14,
                archive_path="test_arch.db",
            )
            self.assertIn("Standard output is not a TTY", mock_stderr.getvalue())
            mock_plain.assert_called_once_with(
                source=TelemetrySource.DEMO,
                reconnect_delay=5.0,
                recording=RecordingMode.ENABLED,
                db_path="test_non_tty.db",
                deadband=DeadbandFilterMode.DISABLED,
                retention_days=14,
                archive_path="test_arch.db",
            )

    @patch("train_tracker.run_plain_stream_mode")
    @patch("train_tracker.is_tty_supported", return_value=True)
    def test_run_tui_mode_falls_back_on_curses_error(self, mock_is_tty, mock_plain):
        """TC-TTY-04: run_tui_mode catches curses.error and falls back to plain stream."""
        try:
            import curses
            curses_err = curses.error("setupterm: could not find terminal")
        except ImportError:
            curses_err = Exception("curses error")

        with patch("tui.app.run_tui", side_effect=curses_err):
            with patch("tui.stream_worker.StreamWorker.start") as mock_start:
                with patch("tui.stream_worker.StreamWorker.stop") as mock_stop:
                    with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                        train_tracker.run_tui_mode(
                            source=TelemetrySource.DEMO,
                            reconnect_delay=4.0,
                            recording=RecordingMode.DISABLED,
                        )
                        self.assertIn("Terminal environment does not support curses", mock_stderr.getvalue())
                        mock_start.assert_called_once()
                        mock_stop.assert_called_once()
                        mock_plain.assert_called_once_with(
                            source=TelemetrySource.DEMO,
                            reconnect_delay=4.0,
                            recording=RecordingMode.DISABLED,
                            db_path="railway_history.db",
                            deadband=DeadbandFilterMode.ENABLED,
                            retention_days=None,
                            archive_path=None,
                        )

    @patch("train_tracker.run_plain_stream_mode")
    @patch("train_tracker.run_tui_mode")
    def test_headless_and_daemon_flags_override_tui(self, mock_tui, mock_plain):
        """TC-TTY-05: --headless and --daemon bypass TUI and dispatch to plain stream."""
        # 1. --headless overrides --tui
        ret = main(["--tui", "--headless", "--demo"])
        self.assertEqual(ret, 0)
        mock_tui.assert_not_called()
        mock_plain.assert_called_once_with(source=TelemetrySource.DEMO, reconnect_delay=3.0)
        mock_plain.reset_mock()

        # 2. --daemon overrides --tui
        ret = main(["--tui", "--daemon"])
        self.assertEqual(ret, 0)
        mock_tui.assert_not_called()
        mock_plain.assert_called_once_with(source=TelemetrySource.LIVE, reconnect_delay=3.0)
        mock_plain.reset_mock()

        # 3. standalone --headless
        ret = main(["--headless"])
        self.assertEqual(ret, 0)
        mock_tui.assert_not_called()
        mock_plain.assert_called_once_with(source=TelemetrySource.LIVE, reconnect_delay=3.0)

    @patch("train_tracker.run_plain_stream_mode")
    @patch("train_tracker.run_tui_mode")
    def test_headless_with_record_forwards_storage_parameters(self, mock_tui, mock_plain):
        """TC-TTY-06: --headless with --record passes storage options to run_plain_stream_mode."""
        ret = main([
            "--tui",
            "--headless",
            "--record",
            "--db",
            "custom.db",
            "--no-filter-deadband",
            "--retention-days",
            "7",
            "--archive-db",
            "my_arch.db",
        ])
        self.assertEqual(ret, 0)
        mock_tui.assert_not_called()
        mock_plain.assert_called_once_with(
            source=TelemetrySource.LIVE,
            reconnect_delay=3.0,
            recording=RecordingMode.ENABLED,
            db_path="custom.db",
            deadband=DeadbandFilterMode.DISABLED,
            retention_days=7,
            archive_path="my_arch.db",
        )


class TestCLIASCIIFallbackRouting(unittest.TestCase):
    """Test suite for --ascii CLI argument routing and run_tui_mode propagation."""

    @patch("train_tracker.run_tui_mode")
    def test_main_tui_forwards_ascii_flag(self, mock_tui):
        """TC-ASCII-01: Verify main passes ascii_mode=True when --ascii is provided."""
        ret = main(["--tui", "--ascii"])
        self.assertEqual(ret, 0)
        mock_tui.assert_called_once_with(
            source=TelemetrySource.LIVE,
            fps=15.0,
            reconnect_delay=3.0,
            glyph_mode=GlyphMode.ASCII,
        )

    @patch("train_tracker.run_tui_mode")
    def test_main_tui_default_ascii_false(self, mock_tui):
        """TC-ASCII-02: Verify main does not pass ascii_mode by default, allowing auto-detection."""
        ret = main(["--tui"])
        self.assertEqual(ret, 0)
        mock_tui.assert_called_once_with(
            source=TelemetrySource.LIVE,
            fps=15.0,
            reconnect_delay=3.0,
            glyph_mode=GlyphMode.AUTO,
        )

    @patch("train_tracker.is_tty_supported", return_value=True)
    @patch("tui.app.run_tui")
    def test_run_tui_mode_forwards_ascii_mode(self, mock_run_tui, mock_tty):
        """TC-ASCII-03: Verify run_tui_mode passes resolved ascii_mode to run_tui."""
        run_tui_mode(demo=True, ascii_mode=True)
        self.assertTrue(mock_run_tui.called)
        _, kwargs = mock_run_tui.call_args
        self.assertTrue(kwargs.get("ascii_mode"))

    @patch("train_tracker.run_tui_mode")
    def test_main_replay_routes_to_tui_mode_and_validates_missing_db(self, mock_run_tui):
        """TC-F42-08b: Verify --replay validates DB file existence and dispatches TelemetrySource.REPLAY."""
        import os
        import tempfile

        # 1. Non-existent database file returns exit code 1 with helpful stderr message
        with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
            ret_missing = main(["--tui", "--replay", "nonexistent_dvr_file_12345.db"])
            self.assertEqual(ret_missing, 1)
            self.assertIn("not found", mock_stderr.getvalue())
            mock_run_tui.assert_not_called()

        # 2. Existing database file routes to run_tui_mode(source=TelemetrySource.REPLAY) even without explicit --tui
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            tmp_db = tf.name
        try:
            ret_ok = main(["--replay", tmp_db, "--replay-speed", "15"])
            self.assertEqual(ret_ok, 0)
            mock_run_tui.assert_called_once_with(
                source=TelemetrySource.REPLAY,
                fps=15.0,
                reconnect_delay=3.0,
                recording=RecordingMode.DISABLED,
                db_path=tmp_db,
                glyph_mode=GlyphMode.AUTO,
                replay_speed=15.0,
            )
        finally:
            if os.path.exists(tmp_db):
                os.remove(tmp_db)


if __name__ == "__main__":
    unittest.main()
