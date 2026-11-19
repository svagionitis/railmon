"""Unit tests for railmon logging hierarchy, RotatingFileHandler, and reporting discipline."""

import logging
import os
import shutil
import stat
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

import train_tracker
from train_tracker import (
    BACKUP_COUNT,
    LOG_FORMAT,
    MAX_LOG_BYTES,
    RailwayGovClient,
    build_parser,
    main,
    resolve_default_log_path,
    setup_logging,
)
from tui.state import ConnectionStatus, TrainStateStore
from tui.stream_worker import StreamWorker


class TestLoggingSubsystem(unittest.TestCase):
    """Test suite for path resolution, RotatingFileHandler, and permission hardening."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="railmon_test_log_")
        self.railmon_logger = logging.getLogger("railmon")

    def tearDown(self):
        for h in list(self.railmon_logger.handlers):
            self.railmon_logger.removeHandler(h)
            h.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_default_log_path_posix_default(self):
        """TC-LOG-01: POSIX default resolves to ~/.local/state/railmon/railmon.log when XDG_STATE_HOME unset."""
        with patch("sys.platform", "linux"):
            with patch.dict(os.environ, {}, clear=True):
                expected = os.path.join(os.path.expanduser("~/.local/state"), "railmon", "railmon.log")
                self.assertEqual(resolve_default_log_path(), expected)

    def test_default_log_path_posix_xdg(self):
        """TC-LOG-02: POSIX resolves to $XDG_STATE_HOME/railmon/railmon.log when XDG_STATE_HOME is set."""
        fake_xdg = os.path.join(self.test_dir, "custom_xdg")
        with patch("sys.platform", "linux"):
            with patch.dict(os.environ, {"XDG_STATE_HOME": fake_xdg}, clear=True):
                expected = os.path.join(os.path.abspath(fake_xdg), "railmon", "railmon.log")
                self.assertEqual(resolve_default_log_path(), expected)

    def test_default_log_path_windows(self):
        """TC-LOG-03: Windows resolves to %LOCALAPPDATA%\\railmon\\logs\\railmon.log."""
        fake_appdata = r"C:\Users\tester\AppData\Local"
        with patch("sys.platform", "win32"):
            with patch.dict(os.environ, {"LOCALAPPDATA": fake_appdata}, clear=True):
                expected = os.path.join(fake_appdata, "railmon", "logs", "railmon.log")
                self.assertEqual(resolve_default_log_path(), expected)

    def test_setup_logging_rotating_handler(self):
        """TC-LOG-04: setup_logging configures RotatingFileHandler with 10MB bounds and 3 backups."""
        log_path = os.path.join(self.test_dir, "sub", "test.log")
        logger = setup_logging(log_file=log_path, log_level="DEBUG", is_tui=False)

        self.assertEqual(logger.level, logging.DEBUG)
        self.assertFalse(logger.propagate)
        self.assertEqual(len(logger.handlers), 1)

        handler = logger.handlers[0]
        self.assertIsInstance(handler, logging.handlers.RotatingFileHandler)
        self.assertEqual(handler.maxBytes, MAX_LOG_BYTES)
        self.assertEqual(handler.maxBytes, 10_485_760)
        self.assertEqual(handler.backupCount, BACKUP_COUNT)
        self.assertEqual(handler.backupCount, 3)
        self.assertEqual(handler.encoding, "utf-8")
        self.assertEqual(handler.formatter._fmt, LOG_FORMAT)

    def test_setup_logging_posix_permissions(self):
        """TC-LOG-05: POSIX log file permissions are hardened to 0600 and dir to 0700."""
        if sys.platform == "win32":
            self.skipTest("POSIX permission hardening test skipped on Windows")

        log_path = os.path.join(self.test_dir, "secure_dir", "audit.log")
        setup_logging(log_file=log_path, log_level="INFO")

        # Verify file exists and has 0600 mode
        self.assertTrue(os.path.exists(log_path))
        file_mode = stat.S_IMODE(os.stat(log_path).st_mode)
        self.assertEqual(file_mode, 0o600)

        # Verify directory has 0700 mode
        dir_mode = stat.S_IMODE(os.stat(os.path.dirname(log_path)).st_mode)
        self.assertEqual(dir_mode, 0o700)

    def test_setup_logging_tui_mode_suppression(self):
        """TC-LOG-06: Interactive TUI mode suppresses console StreamHandler attachments."""
        log_path = os.path.join(self.test_dir, "tui.log")
        logger = setup_logging(log_file=log_path, log_level="INFO", is_tui=True, enable_console=True)

        # Should only have file handler, zero pure StreamHandlers
        for h in logger.handlers:
            self.assertNotEqual(type(h), logging.StreamHandler)
        self.assertEqual(len(logger.handlers), 1)

    def test_logger_hierarchy_propagation(self):
        """TC-LOG-07: Child module loggers propagate to railmon RotatingFileHandler without bubbling to root."""
        log_path = os.path.join(self.test_dir, "hierarchy.log")
        setup_logging(log_file=log_path, log_level="INFO")

        recorder_logger = logging.getLogger("railmon.storage.recorder")
        worker_logger = logging.getLogger("railmon.tui.stream_worker")
        tracker_logger = logging.getLogger("railmon.train_tracker")

        test_msg_1 = "RECORDER_TEST_EVENT_99"
        test_msg_2 = "WORKER_TEST_EVENT_88"
        test_msg_3 = "TRACKER_TEST_EVENT_77"

        recorder_logger.info(test_msg_1)
        worker_logger.warning(test_msg_2)
        tracker_logger.info(test_msg_3)

        # Flush handlers
        for h in self.railmon_logger.handlers:
            h.flush()

        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn(test_msg_1, content)
        self.assertIn(test_msg_2, content)
        self.assertIn(test_msg_3, content)
        self.assertIn("railmon.storage.recorder", content)
        self.assertIn("railmon.tui.stream_worker", content)
        self.assertIn("railmon.train_tracker", content)


class TestDisconnectionReportingDiscipline(unittest.TestCase):
    """Test suite verifying Rule 4 upstream retry backoff logging discipline."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="railmon_disc_test_")
        self.log_path = os.path.join(self.test_dir, "disconnection.log")
        self.logger = setup_logging(log_file=self.log_path, log_level="DEBUG")
        self.store = TrainStateStore()

    def tearDown(self):
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)
            h.close()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_stream_worker_disconnection_reporting_discipline(self):
        """TC-LOG-08: StreamWorker logs 1st drop at WARNING, retries at DEBUG, and reconnect at INFO."""
        call_count = 0

        def failing_stream():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise ConnectionResetError(f"Simulated network drop {call_count}")
            # Successful reconnect on 3rd attempt
            yield []

        worker = StreamWorker(
            store=self.store,
            custom_stream=failing_stream,
            base_backoff_sec=0.01,
            max_backoff_sec=0.05,
        )

        worker.start()
        # Wait until stream executes at least 3 attempts
        deadline = time.time() + 2.0
        while call_count < 3 and time.time() < deadline:
            time.sleep(0.05)
        worker.stop()

        for h in self.logger.handlers:
            h.flush()

        with open(self.log_path, "r", encoding="utf-8") as f:
            logs = f.readlines()

        warning_entries = [l for l in logs if "[WARNING]" in l]
        debug_entries = [l for l in logs if "[DEBUG]" in l]
        info_entries = [l for l in logs if "[INFO]" in l]

        # 1st drop must be logged at WARNING
        self.assertTrue(any("Simulated network drop 1" in l for l in warning_entries))

        # 2nd drop must be logged at DEBUG
        self.assertTrue(any("Simulated network drop 2" in l for l in debug_entries))

        # Re-establishment must be logged at INFO with outage duration
        self.assertTrue(any("connection re-established after" in l for l in info_entries))


class TestCLILoggingFlags(unittest.TestCase):
    """Test suite for CLI --log-file, --log-level, and --debug flags."""

    def setUp(self):
        self.parser = build_parser()

    def test_cli_log_file_argument(self):
        """TC-LOG-09: --log-file parses custom path correctly."""
        args = self.parser.parse_args(["--log-file", "/tmp/custom.log"])
        self.assertEqual(args.log_file, "/tmp/custom.log")

    def test_cli_log_level_and_debug_flags(self):
        """TC-LOG-10: --log-level and --debug flags parse correctly."""
        args1 = self.parser.parse_args(["--log-level", "WARNING"])
        self.assertEqual(args1.log_level, "WARNING")
        self.assertFalse(args1.debug)

        args2 = self.parser.parse_args(["--debug"])
        self.assertTrue(args2.debug)


if __name__ == "__main__":
    unittest.main()
