"""Comprehensive unit and integration test suite for railmon serve CLI subcommand."""

import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from server.models import TileProvider
from train_tracker import (
    DeadbandFilterMode,
    RecordingMode,
    TelemetrySource,
    TrainPosition,
    _build_serve_worker,
    _print_serve_banner,
    build_parser,
    main,
    run_serve_mode,
)
from tui.state import TrainStateStore


class TestCliServe(unittest.TestCase):
    """Test suite validating argument parsing, validation, and execution for railmon serve."""

    def setUp(self) -> None:
        """Initialize parser and capture stderr/stdout buffers."""
        self.parser = build_parser()
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        """Clean up temporary directory."""
        self.temp_dir.cleanup()

    def test_default_argument_parsing(self) -> None:
        """TC-CLI-SRV-01: Verify default argument parsing for 'serve' subcommand."""
        args = self.parser.parse_args(["serve"])
        self.assertEqual(args.mode, "serve")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8080)
        self.assertFalse(args.demo)
        self.assertFalse(args.record)
        self.assertEqual(args.db, "railway_history.db")
        self.assertIsNone(args.replay)
        self.assertEqual(args.speed, 1.0)
        self.assertFalse(args.open_browser)
        self.assertFalse(args.cors)
        self.assertEqual(args.tile_provider, "carto-dark")
        self.assertEqual(args.reconnect_delay, 3.0)
        self.assertFalse(args.no_filter_deadband)
        self.assertIsNone(args.retention_days)
        self.assertIsNone(args.archive_path)

    def test_aliases_argument_parsing(self) -> None:
        """TC-CLI-SRV-02: Verify aliases 'server' and 'web' parse into serve mode."""
        args_server = self.parser.parse_args(["server"])
        self.assertEqual(args_server.mode, "server")

        args_web = self.parser.parse_args(["web"])
        self.assertEqual(args_web.mode, "web")

    def test_custom_flags_argument_parsing(self) -> None:
        """TC-CLI-SRV-03: Verify parsing of all custom CLI flags."""
        cmd = [
            "serve",
            "--host",
            "0.0.0.0",
            "--port",
            "9090",
            "--demo",
            "--record",
            "--db",
            "test_history.db",
            "--replay",
            "recorded.db",
            "--speed",
            "2.5",
            "--open-browser",
            "--cors",
            "--tile-provider",
            "osm",
            "--reconnect-delay",
            "5.0",
            "--no-filter-deadband",
            "--retention-days",
            "30",
            "--archive-path",
            "/tmp/archive.jsonl.gz",
        ]
        args = self.parser.parse_args(cmd)
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9090)
        self.assertTrue(args.demo)
        self.assertTrue(args.record)
        self.assertEqual(args.db, "test_history.db")
        self.assertEqual(args.replay, "recorded.db")
        self.assertEqual(args.speed, 2.5)
        self.assertTrue(args.open_browser)
        self.assertTrue(args.cors)
        self.assertEqual(args.tile_provider, "osm")
        self.assertEqual(args.reconnect_delay, 5.0)
        self.assertTrue(args.no_filter_deadband)
        self.assertEqual(args.retention_days, 30)
        self.assertEqual(args.archive_path, "/tmp/archive.jsonl.gz")

    def test_invalid_port_validation(self) -> None:
        """TC-CLI-SRV-04: Port numbers < 1 or > 65535 fail with exit code 2."""
        for invalid_port in [-1, 0, 65536, 100000]:
            args = self.parser.parse_args(["serve", "--port", str(invalid_port)])
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                exit_code = run_serve_mode(args)
            self.assertEqual(exit_code, 2)
            self.assertIn("Invalid port number", stderr_buf.getvalue())

    def test_non_existent_replay_db_validation(self) -> None:
        """TC-CLI-SRV-05: Missing replay database path fails with exit code 2."""
        missing_db = os.path.join(self.temp_dir.name, "does_not_exist.db")
        args = self.parser.parse_args(["serve", "--replay", missing_db])
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            exit_code = run_serve_mode(args)
        self.assertEqual(exit_code, 2)
        self.assertIn("Historical replay database not found", stderr_buf.getvalue())

    def test_invalid_replay_speed_validation(self) -> None:
        """TC-CLI-SRV-06: Non-positive replay speed multiplier fails with exit code 2."""
        for invalid_speed in ["0", "-1", "-0.5"]:
            args = self.parser.parse_args(["serve", "--speed", invalid_speed])
            stderr_buf = io.StringIO()
            with patch("sys.stderr", stderr_buf):
                exit_code = run_serve_mode(args)
            self.assertEqual(exit_code, 2)
            self.assertIn("Invalid replay speed", stderr_buf.getvalue())

    def test_public_network_binding_security_notice(self) -> None:
        """TC-CLI-SRV-07: Binding to 0.0.0.0 or :: logs prominent security warning to stderr."""
        args = self.parser.parse_args(["serve", "--host", "0.0.0.0", "--port", "8080", "--demo"])
        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()

        # Mock WebMapServer to avoid keeping server running
        with patch("server.daemon.WebMapServer") as mock_server_cls:
            mock_inst = MagicMock()
            mock_inst.server_url = "http://127.0.0.1:8080/"
            mock_inst.config.tile_provider.value = "carto-dark"
            mock_inst.config.cors_enabled = False
            mock_inst.config.open_browser = False
            mock_server_cls.return_value = mock_inst

            with patch("sys.stderr", stderr_buf), patch("sys.stdout", stdout_buf), patch(
                "train_tracker.install_signal_handlers"
            ) as mock_sig:
                mock_sig.return_value.__enter__.return_value = threading.Event()
                # Set stop event immediately
                mock_sig.return_value.__enter__.return_value.set()
                exit_code = run_serve_mode(args)

            self.assertEqual(exit_code, 0)
            self.assertIn("SECURITY NOTICE", stderr_buf.getvalue())
            self.assertIn("binding to all network interfaces", stderr_buf.getvalue())

    def test_port_conflict_eaddrinuse_handling(self) -> None:
        """TC-CLI-SRV-08: Port conflict (EADDRINUSE) prints actionable diagnostic and returns 1."""
        args = self.parser.parse_args(["serve", "--port", "8080", "--demo"])
        stderr_buf = io.StringIO()

        with patch("server.daemon.WebMapServer") as mock_server_cls:
            err = OSError(socket.errno.EADDRINUSE, "Address already in use")
            mock_server_cls.side_effect = err

            with patch("sys.stderr", stderr_buf):
                exit_code = run_serve_mode(args)

            self.assertEqual(exit_code, 1)
            output = stderr_buf.getvalue()
            self.assertIn("[ERROR] Port 8080 is already in use", output)
            self.assertIn("Please specify a different port using '--port <number>'", output)

    def test_telemetry_sse_broadcast_bridge(self) -> None:
        """TC-CLI-SRV-09: Intercepted position updates fan out JSON frame to broadcaster."""
        from server.sse import SSEBroadcaster

        store = TrainStateStore()
        broadcaster = SSEBroadcaster()
        client = broadcaster.register_client()

        worker = _build_serve_worker(
            source=TelemetrySource.DEMO,
            store=store,
            broadcaster=broadcaster,
            db_path="",
            reconnect_delay=3.0,
            recorder=None,
        )

        test_pos = TrainPosition(
            id="1599",
            train_number="1599",
            locomotive_number="120-001",
            name="1599",
            train_type="InterCity",
            service_type="Passenger",
            status="Κανονική",
            origin="Athens",
            destination="Thessaloniki",
            next_station="Larissa",
            speed=120.0,
            course=180.0,
            track_heading=180.0,
            lat=39.5,
            lng=22.3,
            altitude=45.0,
            delay=5,
            progress=0.15,
            chainage_m=12000,
            corridor="PATHE",
            distance_to_destination_m=480000,
            timestamp="2026-11-23T11:00:00Z",
            schedule_id="sch-101",
        )

        # Trigger position update on store
        store.update_positions([test_pos])

        # Client should have received framed event in its queue
        self.assertFalse(client.queue.empty())
        event_type, payload_str = client.queue.get_nowait()
        self.assertEqual(event_type, "trainPositionsUx")
        data = json.loads(payload_str)
        self.assertIsInstance(data, list)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["id"], "1599")
        self.assertEqual(data[0]["lat"], 39.5)
        self.assertEqual(data[0]["lng"], 22.3)
        self.assertEqual(data[0]["speed"], 120.0)
        self.assertEqual(data[0]["delay"], 5.0)
        self.assertEqual(data[0]["heading"], 180.0)

        broadcaster.unregister_client(client.client_id)

    def test_print_serve_banner_output(self) -> None:
        """TC-CLI-SRV-10: Formatted startup banner prints all configuration details."""
        mock_server = MagicMock()
        mock_server.server_url = "http://127.0.0.1:8080/"
        mock_server.config.tile_provider.value = "carto-dark"
        mock_server.config.cors_enabled = True
        mock_server.config.open_browser = True

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            _print_serve_banner(
                mock_server,
                source=TelemetrySource.LIVE,
                recording=True,
                db_path="custom.db",
            )

        output = stdout_buf.getvalue()
        self.assertIn("RAILMON EMBEDDED WEB MAP SERVER", output)
        self.assertIn("Telemetry Mode     : LIVE", output)
        self.assertIn("Bound Interface    : http://127.0.0.1:8080/", output)
        self.assertIn("Tile Provider      : carto-dark", output)
        self.assertIn("CORS Enabled       : True", output)
        self.assertIn("SQLite Recording   : ENABLED (database: 'custom.db')", output)
        self.assertIn("Browser Auto-Launch: ENABLED", output)
        self.assertIn("http://127.0.0.1:8080/api/trains", output)

    def test_main_dispatch_to_serve_mode(self) -> None:
        """TC-CLI-SRV-11: main(['serve', ...]) dispatches to run_serve_mode cleanly."""
        with patch("train_tracker.run_serve_mode") as mock_run:
            mock_run.return_value = 0
            code = main(["serve", "--port", "8888"])
            self.assertEqual(code, 0)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args.mode, "serve")
            self.assertEqual(args.port, 8888)

    def test_replay_mode_wiring(self) -> None:
        """TC-CLI-SRV-12: Replay mode instantiates ReplayWorker with valid database."""
        db_path = os.path.join(self.temp_dir.name, "replay.db")
        with open(db_path, "w") as f:
            f.write("")

        from server.sse import SSEBroadcaster

        store = TrainStateStore()
        broadcaster = SSEBroadcaster()

        worker = _build_serve_worker(
            source=TelemetrySource.REPLAY,
            store=store,
            broadcaster=broadcaster,
            db_path=db_path,
            reconnect_delay=3.0,
            recorder=None,
            replay_speed=5.0,
        )

        from tui.stream_worker import ReplayWorker

        self.assertIsInstance(worker, ReplayWorker)
        self.assertEqual(worker.speed_multiplier, 5.0)

    def test_live_serve_lifecycle_and_health_probe(self) -> None:
        """TC-CLI-SRV-13: Full serve mode startup with demo feed and health check on free port."""
        import urllib.request

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        args = self.parser.parse_args(["serve", "--port", str(free_port), "--demo"])
        stop_event = threading.Event()
        exit_holder = []

        def _run() -> None:
            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf), patch(
                "train_tracker.install_signal_handlers"
            ) as mock_sig:
                mock_sig.return_value.__enter__.return_value = stop_event
                code = run_serve_mode(args)
                exit_holder.append(code)

        server_thread = threading.Thread(target=_run, daemon=True)
        server_thread.start()

        # Wait for daemon to bind and service
        health_url = f"http://127.0.0.1:{free_port}/api/health"
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        retries = 30
        resp_data = None
        while retries > 0:
            time.sleep(0.05)
            try:
                with opener.open(health_url, timeout=1.0) as resp:
                    if resp.status == 200:
                        resp_data = json.loads(resp.read().decode("utf-8"))
                        break
            except Exception:
                retries -= 1

        self.assertIsNotNone(resp_data)
        self.assertEqual(resp_data["status"], "healthy")
        self.assertIn("active_trains_count", resp_data)
        self.assertEqual(resp_data["tile_provider"], "carto-dark")

        # Trigger clean stop
        stop_event.set()
        server_thread.join(timeout=3.0)
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(exit_holder, [0])

    def test_cli_missing_replay_file_tc_edge_15(self) -> None:
        """TC-EDGE-15: Specifying a non-existent replay database file returns exit code 2 with error."""
        args = self.parser.parse_args(["serve", "--replay", "/path/to/non_existent_replay.db"])
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = run_serve_mode(args)
        self.assertEqual(ret, 2)
        self.assertIn("Historical replay database not found", stderr_buf.getvalue())

    def test_cli_invalid_replay_speed_tc_edge_14(self) -> None:
        """TC-EDGE-14: Specifying non-positive replay speed returns exit code 2."""
        args = self.parser.parse_args(["serve", "--speed", "0.0"])
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            ret = run_serve_mode(args)
        self.assertEqual(ret, 2)
        self.assertIn("Invalid replay speed", stderr_buf.getvalue())

    def test_cli_public_lan_security_notice_tc_edge_16(self) -> None:
        """TC-EDGE-16: Binding to 0.0.0.0 triggers a prominent [SECURITY NOTICE] on stderr."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            free_port = s.getsockname()[1]

        args = self.parser.parse_args(["serve", "--host", "0.0.0.0", "--port", str(free_port), "--demo"])
        stop_event = threading.Event()
        stop_event.set()  # Stop immediately

        stderr_buf = io.StringIO()
        stdout_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf), patch("sys.stdout", stdout_buf), patch(
            "train_tracker.install_signal_handlers"
        ) as mock_sig:
            mock_sig.return_value.__enter__.return_value = stop_event
            run_serve_mode(args)

        self.assertIn("[SECURITY NOTICE]", stderr_buf.getvalue())


if __name__ == "__main__":
    unittest.main()
