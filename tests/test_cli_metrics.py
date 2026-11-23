"""Comprehensive unit and integration test suite for railmon metrics CLI subcommand and Web Daemon cross-exposing."""

import gzip
import io
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from server.daemon import WebMapServer
from server.models import ServerConfig
from server.sse import SSEBroadcaster
from train_tracker import (
    TelemetrySource,
    TrainPosition,
    _dispatch_cli_mode,
    _print_metrics_banner,
    build_parser,
    main,
    run_metrics_mode,
)
from tui.state import TrainStateStore


class TestCliMetrics(unittest.TestCase):
    """Test suite validating argument parsing, execution routing, and daemon lifecycle for railmon metrics."""

    def setUp(self) -> None:
        """Initialize parser and temporary test directory."""
        self.parser = build_parser()
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        """Clean up temporary resources."""
        self.temp_dir.cleanup()

    def test_default_argument_parsing(self) -> None:
        """TC-CLI-MET-01: Verify default argument parsing for 'metrics' subcommand."""
        args = self.parser.parse_args(["metrics"])
        self.assertEqual(args.mode, "metrics")
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9100)
        self.assertFalse(args.demo)
        self.assertEqual(args.db, "railway_history.db")
        self.assertFalse(args.no_db)
        self.assertEqual(args.reconnect_delay, 3.0)
        self.assertFalse(args.no_filter_deadband)
        self.assertFalse(args.no_gzip)
        self.assertEqual(args.scrape_timeout, 10.0)

    def test_aliases_argument_parsing(self) -> None:
        """TC-CLI-MET-02: Verify aliases 'exporter' and 'prometheus' parse into metrics mode."""
        args_exporter = self.parser.parse_args(["exporter"])
        self.assertEqual(args_exporter.mode, "exporter")

        args_prom = self.parser.parse_args(["prometheus"])
        self.assertEqual(args_prom.mode, "prometheus")

    def test_custom_flags_argument_parsing(self) -> None:
        """TC-CLI-MET-03: Verify parsing of all custom CLI flags."""
        cmd = [
            "metrics",
            "--host",
            "0.0.0.0",
            "--port",
            "9200",
            "--demo",
            "--db",
            "custom.db",
            "--no-db",
            "--reconnect-delay",
            "5.0",
            "--no-filter-deadband",
            "--no-gzip",
            "--scrape-timeout",
            "15.0",
        ]
        args = self.parser.parse_args(cmd)
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9200)
        self.assertTrue(args.demo)
        self.assertEqual(args.db, "custom.db")
        self.assertTrue(args.no_db)
        self.assertEqual(args.reconnect_delay, 5.0)
        self.assertTrue(args.no_filter_deadband)
        self.assertTrue(args.no_gzip)
        self.assertEqual(args.scrape_timeout, 15.0)

    def test_invalid_port_validation(self) -> None:
        """TC-CLI-MET-04: Reject out-of-range ports with exit code 2."""
        stderr_buf = io.StringIO()
        with patch("sys.stderr", stderr_buf):
            # Port 0 is rejected by run_metrics_mode
            args_low = self.parser.parse_args(["metrics", "--port", "0"])
            code_low = run_metrics_mode(args_low)
            self.assertEqual(code_low, 2)
            self.assertIn("Invalid port: 0", stderr_buf.getvalue())

        stderr_buf2 = io.StringIO()
        with patch("sys.stderr", stderr_buf2):
            # Port 70000 is rejected
            args_high = self.parser.parse_args(["metrics", "--port", "70000"])
            code_high = run_metrics_mode(args_high)
            self.assertEqual(code_high, 2)
            self.assertIn("Invalid port: 70000", stderr_buf2.getvalue())

    def test_dispatch_routing(self) -> None:
        """TC-CLI-MET-05: _dispatch_cli_mode routes metrics, exporter, prometheus to run_metrics_mode."""
        with patch("train_tracker.run_metrics_mode") as mock_run:
            mock_run.return_value = 0

            args1 = self.parser.parse_args(["metrics"])
            self.assertEqual(_dispatch_cli_mode(args1, is_tui=False), 0)

            args2 = self.parser.parse_args(["exporter"])
            self.assertEqual(_dispatch_cli_mode(args2, is_tui=False), 0)

            args3 = self.parser.parse_args(["prometheus"])
            self.assertEqual(_dispatch_cli_mode(args3, is_tui=False), 0)

            self.assertEqual(mock_run.call_count, 3)

    def test_security_warning_on_wildcard_host(self) -> None:
        """TC-CLI-MET-06: Binding to 0.0.0.0 logs prominent security warning to stderr."""
        stderr_buf = io.StringIO()
        args = self.parser.parse_args(["metrics", "--host", "0.0.0.0", "--port", "9199", "--demo", "--no-db"])
        stop_event = threading.Event()
        stop_event.set()

        with patch("sys.stderr", stderr_buf), \
             patch("train_tracker.install_signal_handlers") as mock_signals:
            mock_signals.return_value.__enter__.return_value = stop_event

            code = run_metrics_mode(args)
            self.assertEqual(code, 0)
            self.assertIn("SECURITY NOTICE", stderr_buf.getvalue())
            self.assertIn("0.0.0.0", stderr_buf.getvalue())

    def test_port_contention_handling(self) -> None:
        """TC-CLI-MET-07: Port contention traps EADDRINUSE and returns exit code 1."""
        import errno

        stderr_buf = io.StringIO()
        args = self.parser.parse_args(["metrics", "--port", "9100", "--demo"])
        with patch("sys.stderr", stderr_buf), patch(
            "exporter.server.MetricsServer"
        ) as mock_server_cls:
            err = OSError(errno.EADDRINUSE, "Address already in use")
            err.errno = errno.EADDRINUSE
            mock_server_cls.side_effect = err
            code = run_metrics_mode(args)
            self.assertEqual(code, 1)
            self.assertIn("already in use", stderr_buf.getvalue())
            self.assertIn("9100", stderr_buf.getvalue())

    def test_print_metrics_banner_output(self) -> None:
        """TC-CLI-MET-08: Formatted startup banner prints all exporter endpoints and config."""
        mock_server = MagicMock()
        mock_server.server_address = ("127.0.0.1", 9100)
        mock_server.server_port = 9100
        mock_server.config.gzip_compression = True

        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            _print_metrics_banner(
                mock_server,
                source=TelemetrySource.LIVE,
                db_path="railway_history.db",
            )

        output = stdout_buf.getvalue()
        self.assertIn("RAILMON PROMETHEUS & OPENMETRICS OPERATIONAL EXPORTER", output)
        self.assertIn("http://127.0.0.1:9100/metrics", output)
        self.assertIn("http://127.0.0.1:9100/health", output)
        self.assertIn("Telemetry Mode     : LIVE", output)
        self.assertIn("Gzip Compression   : ENABLED", output)
        self.assertIn("Storage Telemetry  : ENABLED (database: 'railway_history.db')", output)
        self.assertIn("scrape_configs:", output)
        self.assertIn("targets: [\"127.0.0.1:9100\"]", output)

    def test_run_metrics_mode_demo_lifecycle(self) -> None:
        """TC-CLI-MET-09: Demo mode launches synthetic simulation and shuts down cleanly."""
        args = self.parser.parse_args(["metrics", "--demo", "--port", "9197", "--no-db"])

        stop_event = threading.Event()

        def _stop_later() -> None:
            time.sleep(0.05)
            stop_event.set()

        threading.Thread(target=_stop_later, daemon=True).start()

        with patch("train_tracker.install_signal_handlers") as mock_signals:
            mock_signals.return_value.__enter__.return_value = stop_event

            stdout_buf = io.StringIO()
            with patch("sys.stdout", stdout_buf):
                code = run_metrics_mode(args)
                self.assertEqual(code, 0)
                self.assertIn("Prometheus exporter daemon shut down cleanly", stdout_buf.getvalue())

    def test_main_dispatch_to_metrics_mode(self) -> None:
        """Verify main(['metrics', ...]) dispatches cleanly."""
        with patch("train_tracker.run_metrics_mode") as mock_run:
            mock_run.return_value = 0
            code = main(["metrics", "--port", "9150"])
            self.assertEqual(code, 0)
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            self.assertEqual(args.mode, "metrics")
            self.assertEqual(args.port, 9150)


class TestWebDaemonCrossExposing(unittest.TestCase):
    """Integration test suite validating Prometheus cross-exposing on WebMapServer."""

    @classmethod
    def setUpClass(cls) -> None:
        """Spin up WebMapServer on an ephemeral port with populated domain state."""
        cls.store = TrainStateStore()
        cls.broadcaster = SSEBroadcaster()
        cls.config = ServerConfig(
            host="127.0.0.1",
            port=8080,
            cors_enabled=True,
            demo_mode=True,
        )

        test_pos = TrainPosition(
            id="IC52",
            train_number="IC52",
            locomotive_number="120-002",
            name="IC52",
            train_type="IC",
            service_type="Passenger",
            status="Κανονική",
            origin="Athens",
            destination="Thessaloniki",
            next_station="Lianokladi",
            speed=145.0,
            course=0.0,
            track_heading=0.0,
            lat=38.8,
            lng=22.4,
            altitude=150.0,
            delay=3,
            progress=0.45,
            chainage_m=210000,
            corridor="PATHE",
            distance_to_destination_m=290000,
            timestamp="2026-11-23T12:00:00Z",
            schedule_id="sch-52",
        )
        cls.store.update_positions([test_pos])

        cls.server = WebMapServer(
            server_address=("127.0.0.1", 0),
            store=cls.store,
            broadcaster=cls.broadcaster,
            config=cls.config,
        )
        cls.port = cls.server.server_port
        cls.server.start(background=True)
        time.sleep(0.05)

    @classmethod
    def tearDownClass(cls) -> None:
        """Tear down WebMapServer daemon cleanly."""
        cls.server.stop()

    def _make_request(
        self,
        path: str,
        method: str = "GET",
        headers: dict = None,
    ):
        """Helper to execute HTTP requests through standard library urllib."""
        opener = build_opener(ProxyHandler({}))
        url = f"http://127.0.0.1:{self.port}{path}"
        req = Request(url=url, headers=headers or {}, method=method)
        return opener.open(req, timeout=5.0)

    def test_web_daemon_metrics_endpoint_prometheus_004(self) -> None:
        """TC-CLI-MET-10: WebMapServer exposes /metrics with Prometheus 0.0.4 text format."""
        resp = self._make_request("/metrics", headers={"Accept": "text/plain; version=0.0.4"})
        self.assertEqual(resp.status, 200)
        ct = resp.headers.get("Content-Type", "")
        self.assertIn("text/plain", ct)
        body = resp.read().decode("utf-8")
        self.assertIn("# HELP railmon_active_trains", body)
        self.assertIn("# TYPE railmon_active_trains gauge", body)
        self.assertIn('railmon_active_trains{corridor="PATHE"} 1', body)
        self.assertIn('railmon_train_speed_kmh{corridor="PATHE",train_number="IC52",type="IC"} 145', body)

    def test_web_daemon_openmetrics_negotiation(self) -> None:
        """TC-CLI-MET-11: WebMapServer negotiates OpenMetrics 1.0.0 with # EOF trailer."""
        headers = {"Accept": "application/openmetrics-text; version=1.0.0"}
        resp = self._make_request("/metrics", headers=headers)
        self.assertEqual(resp.status, 200)
        ct = resp.headers.get("Content-Type", "")
        self.assertIn("application/openmetrics-text", ct)
        body = resp.read().decode("utf-8")
        self.assertTrue(body.endswith("# EOF\n"))
        self.assertIn('railmon_active_trains{corridor="PATHE"} 1', body)

    def test_web_daemon_gzip_compression(self) -> None:
        """TC-CLI-MET-12: WebMapServer transparently gzips /metrics payload when requested."""
        headers = {
            "Accept": "text/plain",
            "Accept-Encoding": "gzip",
        }
        resp = self._make_request("/metrics", headers=headers)
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
        raw_compressed = resp.read()
        decompressed = gzip.decompress(raw_compressed).decode("utf-8")
        self.assertIn("railmon_active_trains", decompressed)

    def test_web_daemon_head_metrics(self) -> None:
        """TC-CLI-MET-13: HEAD /metrics returns HTTP 200 headers with zero body."""
        resp = self._make_request("/metrics", method="HEAD")
        self.assertEqual(resp.status, 200)
        ct = resp.headers.get("Content-Type", "")
        self.assertIn("text/plain", ct)
        body = resp.read()
        self.assertEqual(len(body), 0)

    def test_web_daemon_api_metrics_alias(self) -> None:
        """TC-CLI-MET-14: /api/metrics alias also resolves to operational metrics."""
        resp = self._make_request("/api/metrics")
        self.assertEqual(resp.status, 200)
        body = resp.read().decode("utf-8")
        self.assertIn("railmon_active_trains", body)


if __name__ == "__main__":
    unittest.main()
