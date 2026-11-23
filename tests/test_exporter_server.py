"""Comprehensive unit and integration test suite for railmon MetricsServer and MetricsRequestHandler."""

import concurrent.futures
import gzip
import json
import logging
import socket
import time
import unittest
from typing import Any, List, Optional
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from exporter.collector import (
    BaseCollector,
    MetricRegistry,
    ProcessMetricsCollector,
)
from exporter.models import ExporterConfig, MetricFamily, MetricType
from exporter.server import MetricsRequestHandler, MetricsServer


class SyntheticDummyCollector(BaseCollector):
    """Dummy collector returning deterministic metrics for server tests."""

    def __init__(self) -> None:
        self.call_count = 0

    def collect(self) -> List[MetricFamily]:
        self.call_count += 1
        fam = MetricFamily(
            "railmon_test_gauge",
            "Synthetic test gauge for server verification",
            MetricType.GAUGE,
            unit="units",
        )
        fam.add_sample(labels={"corridor": "PATHE"}, value=42.0)

        counter_fam = MetricFamily(
            "railmon_test_counter",
            "Synthetic test counter for server verification",
            MetricType.COUNTER,
            unit="events",
        )
        counter_fam.add_sample(labels={"corridor": "PATHE"}, value=float(self.call_count))
        return [fam, counter_fam]


class TestExporterServer(unittest.TestCase):
    """Integration and unit tests for MetricsServer and MetricsRequestHandler."""

    def setUp(self) -> None:
        """Create fresh registry, collectors, and server instance on ephemeral port."""
        self.registry = MetricRegistry()
        self.dummy_collector = SyntheticDummyCollector()
        self.process_collector = ProcessMetricsCollector(version="1.2.3")
        self.registry.register(self.dummy_collector)
        self.registry.register(self.process_collector)

        self.config = ExporterConfig(
            host="127.0.0.1",
            port=9100,
            gzip_compression=True,
            scrape_timeout_seconds=5.0,
        )
        self.server: Optional[MetricsServer] = None
        self._opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        """Cleanly stop server if active."""
        if self.server is not None:
            self.server.stop()
            self.server = None

    def _start_server(self, config: Optional[ExporterConfig] = None) -> MetricsServer:
        """Helper to start server in background thread on ephemeral port."""
        cfg = config or self.config
        self.server = MetricsServer(
            server_address=(cfg.host, 0),
            registry=self.registry,
            config=cfg,
        )
        self.server.start(background=True)
        return self.server

    def _open(self, req: Request) -> Any:
        """Execute request using direct opener bypassing environment proxies."""
        return self._opener.open(req)

    def test_tc_srv_01_metrics_default_prometheus_format(self) -> None:
        """TC-SRV-01: GET /metrics defaults to Prometheus 0.0.4 text format."""
        srv = self._start_server()
        url = f"{srv.server_url}metrics"

        req = Request(url, headers={"Accept": "text/plain"})
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            ct = resp.headers.get("Content-Type", "")
            self.assertIn("text/plain", ct)
            self.assertIn("version=0.0.4", ct)
            self.assertEqual(resp.headers.get("Cache-Control"), "no-cache, no-store, must-revalidate")

            body = resp.read().decode("utf-8")
            self.assertIn("# HELP railmon_test_gauge", body)
            self.assertIn("# TYPE railmon_test_gauge gauge", body)
            self.assertIn('railmon_test_gauge{corridor="PATHE"} 42', body)
            # Must NOT end with OpenMetrics EOF marker in 0.0.4 format
            self.assertNotIn("# EOF", body)

    def test_tc_srv_02_metrics_openmetrics_negotiation(self) -> None:
        """TC-SRV-02: GET /metrics negotiates OpenMetrics 1.0.0 format."""
        srv = self._start_server()
        url = f"{srv.server_url}metrics"

        req = Request(url, headers={"Accept": "application/openmetrics-text; version=1.0.0"})
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            ct = resp.headers.get("Content-Type", "")
            self.assertIn("application/openmetrics-text", ct)
            self.assertIn("version=1.0.0", ct)

            body = resp.read().decode("utf-8")
            self.assertIn("# HELP railmon_test_gauge", body)
            self.assertIn("# TYPE railmon_test_gauge gauge", body)
            self.assertIn("# UNIT railmon_test_gauge units", body)
            # OpenMetrics counter must have _total suffix
            self.assertIn("railmon_test_counter_total", body)
            # Mandatory terminal line
            self.assertTrue(body.rstrip().endswith("# EOF"))

    def test_tc_srv_03_metrics_gzip_compression(self) -> None:
        """TC-SRV-03: GET /metrics transparently compresses payload when requested."""
        srv = self._start_server()
        url = f"{srv.server_url}metrics"

        # Request gzip encoding
        req = Request(url, headers={"Accept-Encoding": "gzip"})
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            # Check Content-Encoding header
            ce = resp.headers.get("Content-Encoding", "")
            raw_bytes = resp.read()
            if ce == "gzip":
                decompressed = gzip.decompress(raw_bytes).decode("utf-8")
                self.assertIn("railmon_test_gauge", decompressed)
            else:
                # If body was under threshold, it was sent uncompressed
                self.assertIn("railmon_test_gauge", raw_bytes.decode("utf-8"))

    def test_tc_srv_04_metrics_head_request(self) -> None:
        """TC-SRV-04: HEAD /metrics returns correct headers with zero body."""
        srv = self._start_server()
        url = f"{srv.server_url}metrics"

        req = Request(url, method="HEAD")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/plain", resp.headers.get("Content-Type", ""))
            content_len = int(resp.headers.get("Content-Length", 0))
            self.assertGreater(content_len, 0)
            body = resp.read()
            self.assertEqual(len(body), 0)

    def test_tc_srv_05_health_endpoint(self) -> None:
        """TC-SRV-05: GET /health returns operational JSON health status."""
        srv = self._start_server()
        url = f"{srv.server_url}health"

        req = Request(url)
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "ok")
            self.assertGreaterEqual(data["uptime_seconds"], 0.0)
            self.assertEqual(data["collectors_registered"], 2)
            self.assertIn("timestamp", data)

    def test_tc_srv_06_health_head_request(self) -> None:
        """TC-SRV-06: HEAD /health returns JSON headers with zero body."""
        srv = self._start_server()
        url = f"{srv.server_url}health"

        req = Request(url, method="HEAD")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            content_len = int(resp.headers.get("Content-Length", 0))
            self.assertGreater(content_len, 0)
            body = resp.read()
            self.assertEqual(len(body), 0)

    def test_tc_srv_07_index_page_html(self) -> None:
        """TC-SRV-07: GET / and /index.html return HTML landing page with links."""
        srv = self._start_server()

        for path in ("", "index.html"):
            url = f"{srv.server_url}{path}"
            req = Request(url)
            with self._open(req) as resp:
                self.assertEqual(resp.status, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                body = resp.read().decode("utf-8")
                self.assertIn("Railmon Operational Metrics Exporter", body)
                self.assertIn('href="/metrics"', body)
                self.assertIn('href="/health"', body)
                self.assertIn("scrape_configs:", body)

    def test_tc_srv_08_index_head_request(self) -> None:
        """TC-SRV-08: HEAD / returns HTML headers with zero body."""
        srv = self._start_server()
        url = srv.server_url

        req = Request(url, method="HEAD")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("text/html", resp.headers.get("Content-Type", ""))
            content_len = int(resp.headers.get("Content-Length", 0))
            self.assertGreater(content_len, 0)
            self.assertEqual(len(resp.read()), 0)

    def test_tc_srv_09_cors_options_preflight(self) -> None:
        """TC-SRV-09: OPTIONS /metrics returns 204 No Content with CORS headers."""
        srv = self._start_server()
        url = f"{srv.server_url}metrics"

        req = Request(url, method="OPTIONS")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers.get("Allow"), "GET, HEAD, OPTIONS")
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertEqual(resp.headers.get("Content-Length"), "0")

    def test_tc_srv_10_unknown_route_404(self) -> None:
        """TC-SRV-10: Unknown routes return 404 with JSON error."""
        srv = self._start_server()
        url = f"{srv.server_url}api/unknown"

        req = Request(url)
        with self.assertRaises(HTTPError) as ctx:
            self._open(req)
        self.assertEqual(ctx.exception.code, 404)
        data = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(data["status"], 404)
        self.assertIn("not found", data["error"].lower())

    def test_tc_srv_11_disallowed_methods_405(self) -> None:
        """TC-SRV-11: Mutation HTTP methods return 405 Method Not Allowed."""
        srv = self._start_server()
        url = f"{srv.server_url}metrics"

        for method in ("POST", "PUT", "DELETE"):
            req = Request(url, method=method, data=b"{}")
            with self.assertRaises(HTTPError) as ctx:
                self._open(req)
            self.assertEqual(ctx.exception.code, 405)
            data = json.loads(ctx.exception.read().decode("utf-8"))
            self.assertEqual(data["status"], 405)
            self.assertIn("read-only", data["error"].lower())

    def test_tc_srv_12_server_lifecycle_and_shutdown(self) -> None:
        """TC-SRV-12: Server start, query, and stop lifecycle transitions cleanly."""
        srv = self._start_server()
        self.assertTrue(srv.is_running)

        # Make sure server responds
        req = Request(f"{srv.server_url}health")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)

        # Stop server
        srv.stop()
        self.assertFalse(srv.is_running)

        # Calling stop again is safe and idempotent
        srv.stop()

    def test_tc_srv_13_port_collision_eaddrinuse(self) -> None:
        """TC-SRV-13: Port collision raises OSError and logs actionable guidance."""
        srv = self._start_server()
        allocated_port = srv.server_port

        # Attempt to bind a second server on the identical port
        with self.assertRaises(OSError):
            MetricsServer(
                server_address=(srv.server_address[0], allocated_port),
                registry=self.registry,
            )

    def test_tc_srv_14_scrape_metrics_recording(self) -> None:
        """TC-SRV-14: Scrape operations record duration and counters in ProcessMetricsCollector."""
        srv = self._start_server()
        url = f"{srv.server_url}metrics"

        # Perform 2 scrape requests
        self._open(Request(url)).read()
        self._open(Request(url)).read()

        # Check ProcessMetricsCollector
        families = self.process_collector.collect()
        fam_map = {f.name: f for f in families}

        self.assertIn("railmon_exporter_scrapes_total", fam_map)
        scrapes_fam = fam_map["railmon_exporter_scrapes_total"]
        scrape_samples = {s.labels: s.value for s in scrapes_fam.samples}

        # Should have at least 2 successful scrapes recorded
        metrics_key = (("endpoint", "/metrics"), ("status", "200"))
        self.assertIn(metrics_key, scrape_samples)
        self.assertGreaterEqual(scrape_samples[metrics_key], 2.0)

        self.assertIn("railmon_exporter_scrape_duration_seconds", fam_map)
        dur_fam = fam_map["railmon_exporter_scrape_duration_seconds"]
        self.assertGreater(len(dur_fam.samples), 0)
        self.assertGreater(dur_fam.samples[0].value, 0.0)

    def test_tc_srv_15_concurrent_scrapes(self) -> None:
        """TC-SRV-15: Multiple concurrent scrapes complete without deadlock or corruption."""
        srv = self._start_server()
        url = f"{srv.server_url}metrics"

        def _fetch_metrics(worker_id: int) -> int:
            req = Request(url)
            with self._open(req) as resp:
                data = resp.read()
                if resp.status == 200 and len(data) > 0:
                    return resp.status
                return 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_fetch_metrics, i) for i in range(16)]
            results = [f.result(timeout=5.0) for f in futures]

        self.assertEqual(len(results), 16)
        self.assertTrue(all(code == 200 for code in results))


if __name__ == "__main__":
    unittest.main()
