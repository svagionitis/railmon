"""Unit tests for train_tracker CLI argument parsing, routing, and lifecycle."""

import io
import unittest
from unittest.mock import MagicMock, patch

import train_tracker
from train_tracker import build_parser, main, run_tui_mode


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
        self.assertEqual(args.fps, 20.0)
        self.assertEqual(args.reconnect_delay, 3.0)

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
            self.assertIn("railmon 1.0.0", mock_stdout.getvalue())


class TestCLIDispatchAndLifecycle(unittest.TestCase):
    """Test suite for CLI dispatch routing and process lifecycle safety."""

    @patch("train_tracker.run_tui_mode")
    def test_main_routes_to_tui_live(self, mock_run_tui):
        """TC-CLI-02b: main() routes --tui to run_tui_mode(demo=False)."""
        ret = main(["--tui", "--fps", "25.0"])
        self.assertEqual(ret, 0)
        mock_run_tui.assert_called_once_with(
            demo=False, fps=25.0, reconnect_delay=3.0
        )

    @patch("train_tracker.run_tui_mode")
    def test_main_routes_to_tui_demo(self, mock_run_tui):
        """TC-CLI-03b: main() routes --tui --demo to run_tui_mode(demo=True)."""
        ret = main(["--tui", "--demo"])
        self.assertEqual(ret, 0)
        mock_run_tui.assert_called_once_with(
            demo=True, fps=20.0, reconnect_delay=3.0
        )

    @patch("train_tracker.run_plain_stream_mode")
    def test_main_routes_to_plain_stream(self, mock_plain):
        """TC-CLI-04b: main() routes default to run_plain_stream_mode."""
        ret = main([])
        self.assertEqual(ret, 0)
        mock_plain.assert_called_once_with(demo=False, reconnect_delay=3.0)

    @patch("train_tracker.run_plain_stream_mode")
    def test_main_routes_to_plain_demo(self, mock_plain):
        """TC-CLI-04c: main() routes --demo to run_plain_stream_mode(demo=True)."""
        ret = main(["--demo", "--reconnect-delay", "4.0"])
        self.assertEqual(ret, 0)
        mock_plain.assert_called_once_with(demo=True, reconnect_delay=4.0)

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

    @patch("tui.app.run_tui", side_effect=RuntimeError("Simulated Curses crash"))
    @patch("tui.stream_worker.StreamWorker.start")
    @patch("tui.stream_worker.StreamWorker.stop")
    def test_tui_worker_cleanup_on_error(self, mock_worker_stop, mock_worker_start, mock_run_tui):
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

    @patch("sys.platform", "win32")
    def test_run_tui_mode_windows_missing_curses(self):
        """Verify friendly error message on Windows when windows-curses is missing."""
        orig_import = __import__

        def fake_import(name, *args, **kwargs):
            if "tui.app" in name or name in ("curses", "_curses"):
                raise ModuleNotFoundError("No module named '_curses'")
            return orig_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=fake_import):
            with patch("sys.stderr", new_callable=io.StringIO) as mock_stderr:
                train_tracker.run_tui_mode(demo=True)
                self.assertIn("windows-curses", mock_stderr.getvalue())


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


if __name__ == "__main__":
    unittest.main()
