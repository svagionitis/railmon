"""Adversarial DoS, Slowloris, keep-alive, and concurrency-cap tests for P1-10 (railmon_http)."""

import http.client
import io
import json
import socket
import struct
import threading
import time
import unittest
from typing import Any, Callable, List, Tuple
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from exporter.collector import BaseCollector, MetricRegistry, ProcessMetricsCollector
from exporter.models import ExporterConfig, MetricFamily, MetricType
from exporter.server import MetricsServer
from gtfs_rt.models import GtfsRtConfig
from gtfs_rt.server import GtfsRtServer
from railmon_http import (
    BoundedHTTPRequestHandler,
    BoundedHTTPServer,
    HttpLimitsConfig,
)
from server.daemon import WebMapServer
from server.models import ServerConfig
from server.sse import SSEBroadcaster
from tui.state import TrainStateStore


def _wait_until(predicate: Callable[[], bool], timeout: float = 3.0, step: float = 0.02) -> bool:
    """Poll predicate until True or monotonic timeout expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()


class _EchoHandler(BoundedHTTPRequestHandler):
    """Minimal HTTP/1.1 handler for isolated BoundedHTTPServer tests."""

    def do_GET(self) -> None:
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _SlowCollector(BaseCollector):
    """Test collector sleeping past scrape_timeout_seconds to trigger HTTP 504."""

    def __init__(self, delay_sec: float) -> None:
        self._delay_sec = delay_sec

    def collect(self) -> List[MetricFamily]:
        time.sleep(self._delay_sec)
        fam = MetricFamily("railmon_slow_gauge", "Slow test metric", MetricType.GAUGE)
        fam.add_sample(value=1.0)
        return [fam]


def _start_daemon_suite(
    max_connections: int = 3,
    request_timeout_sec: float = 2.0,
) -> List[Tuple[str, Any, str]]:
    """Spin up all three production HTTP daemons on ephemeral loopback ports."""
    store = TrainStateStore()

    web = WebMapServer(
        server_address=("127.0.0.1", 0),
        store=store,
        broadcaster=SSEBroadcaster(max_clients=max(1, max_connections)),
        config=ServerConfig(
            host="127.0.0.1",
            port=8080,
            max_connections=max_connections,
            max_sse_clients=max(1, max_connections),
            request_timeout_sec=request_timeout_sec,
            sse_write_timeout_sec=request_timeout_sec,
        ),
    )
    web.start(background=True)

    reg = MetricRegistry()
    reg.register(ProcessMetricsCollector(version="1.0.0"))
    metrics = MetricsServer(
        server_address=("127.0.0.1", 0),
        registry=reg,
        config=ExporterConfig(
            host="127.0.0.1",
            port=9100,
            scrape_timeout_seconds=request_timeout_sec,
            max_connections=max_connections,
        ),
    )
    metrics.start(background=True)

    gtfs = GtfsRtServer(
        server_address=("127.0.0.1", 0),
        store=store,
        config=GtfsRtConfig(
            host="127.0.0.1",
            port=8085,
            max_connections=max_connections,
            request_timeout_sec=request_timeout_sec,
        ),
    )
    gtfs.start(background=True)

    return [
        ("WebMapServer", web, "/api/health"),
        ("MetricsServer", metrics, "/health"),
        ("GtfsRtServer", gtfs, "/health"),
    ]


class TestHttpBoundsAndSlowlorisHardening(unittest.TestCase):
    """Comprehensive DoS, Slowloris, keep-alive, and concurrency tests across all 3 daemons."""

    def setUp(self) -> None:
        self._opener = build_opener(ProxyHandler({}))

    def test_tc_http_01_limits_and_daemon_config_validation(self) -> None:
        """TC-HTTP-01: HttpLimitsConfig and all 3 daemon configs reject invalid/bool/non-finite limits."""
        for bad_int in (0, -1, -100, True, False):
            with self.assertRaises(ValueError):
                HttpLimitsConfig(max_connections=bad_int)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                HttpLimitsConfig(max_keepalive_requests=bad_int)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                HttpLimitsConfig(max_reject_workers=bad_int)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                HttpLimitsConfig(listen_backlog=bad_int)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                ServerConfig(max_connections=bad_int)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                ServerConfig(max_sse_clients=bad_int)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                ExporterConfig(max_connections=bad_int)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                GtfsRtConfig(max_connections=bad_int)  # type: ignore[arg-type]

        with self.assertRaises(ValueError):
            HttpLimitsConfig(max_header_bytes=512)

        for bad_float in (0.0, -1.0, float("nan"), float("inf"), float("-inf"), True, False):
            with self.assertRaises(ValueError):
                HttpLimitsConfig(request_timeout_sec=bad_float)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                HttpLimitsConfig(header_timeout_sec=bad_float)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                HttpLimitsConfig(keepalive_timeout_sec=bad_float)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                HttpLimitsConfig(sse_write_timeout_sec=bad_float)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                ServerConfig(replay_speed=bad_float)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                ServerConfig(request_timeout_sec=bad_float)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                ServerConfig(sse_write_timeout_sec=bad_float)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                ExporterConfig(scrape_timeout_seconds=bad_float)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                GtfsRtConfig(request_timeout_sec=bad_float)  # type: ignore[arg-type]

        # Clamping helper respects short request_timeout_sec across header & keep-alive timers
        clamped = HttpLimitsConfig.for_request_timeout(0.4, max_connections=8)
        self.assertEqual(clamped.max_connections, 8)
        self.assertAlmostEqual(clamped.request_timeout_sec, 0.4)
        self.assertAlmostEqual(clamped.header_timeout_sec, 0.4)
        self.assertAlmostEqual(clamped.keepalive_timeout_sec, 0.4)
        self.assertAlmostEqual(clamped.sse_write_timeout_sec, 0.4)

    def test_tc_http_02_and_03_connection_cap_503_and_slot_recovery(self) -> None:
        """TC-HTTP-02 & TC-HTTP-03: All 3 daemons cap connections with clean 503 and recover slots."""
        daemons = _start_daemon_suite(max_connections=3, request_timeout_sec=5.0)
        try:
            for name, srv, path in daemons:
                with self.subTest(daemon=name):
                    holders: List[socket.socket] = []
                    try:
                        for _ in range(3):
                            sock = socket.create_connection(
                                ("127.0.0.1", srv.server_port), timeout=2.0
                            )
                            sock.sendall(f"GET {path} HTTP/1.1\r\nX-Hold: ".encode("ascii"))
                            holders.append(sock)

                        self.assertTrue(
                            _wait_until(lambda s=srv: s.active_connections == 3, timeout=2.0),
                            f"{name} did not reach active_connections == 3",
                        )

                        # 3 overflow requests via urllib + 3 via http.client -> all 6 clean HTTP 503
                        for _ in range(3):
                            req = Request(f"{srv.server_url}{path.lstrip('/')}")
                            with self.assertRaises(HTTPError) as ctx:
                                self._opener.open(req, timeout=2.0)
                            self.assertEqual(ctx.exception.code, 503)
                            self.assertEqual(ctx.exception.headers.get("Retry-After"), "5")
                            self.assertEqual(ctx.exception.headers.get("Connection"), "close")
                            self.assertEqual(
                                ctx.exception.headers.get("X-Content-Type-Options"), "nosniff"
                            )
                            payload = json.loads(ctx.exception.read().decode("utf-8"))
                            self.assertEqual(payload["status"], 503)

                        for _ in range(3):
                            conn = http.client.HTTPConnection(
                                "127.0.0.1", srv.server_port, timeout=2.0
                            )
                            try:
                                conn.request("GET", path)
                                resp = conn.getresponse()
                                self.assertEqual(resp.status, 503)
                                self.assertEqual(resp.getheader("Retry-After"), "5")
                                self.assertEqual(resp.getheader("Connection"), "close")
                                payload = json.loads(resp.read().decode("utf-8"))
                                self.assertEqual(payload["status"], 503)
                            finally:
                                conn.close()

                        self.assertLessEqual(srv.active_connections, 3)
                        self.assertLessEqual(srv.peak_connections, 3)
                        self.assertGreaterEqual(srv.rejected_connections, 6)
                    finally:
                        for sock in holders:
                            sock.close()

                    # TC-HTTP-03: Once held connections close, semaphore slots recover immediately
                    self.assertTrue(
                        _wait_until(lambda s=srv: s.active_connections == 0, timeout=2.0),
                        f"{name} leaked semaphore slots: active={srv.active_connections}",
                    )
                    with self._opener.open(f"{srv.server_url}{path.lstrip('/')}", timeout=2.0) as ok_resp:
                        self.assertEqual(ok_resp.status, 200)
                        data = json.loads(ok_resp.read().decode("utf-8"))
                        self.assertEqual(data["max_http_connections"], 3)
                        self.assertGreaterEqual(data["rejected_http_connections"], 6)
        finally:
            for _, srv, _ in daemons:
                srv.stop()

    def test_tc_http_04_extreme_flood_rejector_fallback_no_accept_stall(self) -> None:
        """TC-HTTP-04: Saturating workers + rejector lane bounds OS threads and keeps accept loop live."""
        limits = HttpLimitsConfig(
            max_connections=2,
            max_reject_workers=2,
            request_timeout_sec=3.0,
            header_timeout_sec=3.0,
            keepalive_timeout_sec=1.0,
        )
        srv = BoundedHTTPServer(("127.0.0.1", 0), _EchoHandler, http_limits=limits)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        holders: List[socket.socket] = []
        flood: List[socket.socket] = []
        try:
            base_threads = threading.active_count()
            for _ in range(2):
                s = socket.create_connection(("127.0.0.1", srv.server_port), timeout=2.0)
                s.sendall(b"GET / HTTP/1.1\r\nX-Hold: ")
                holders.append(s)

            self.assertTrue(_wait_until(lambda: srv.active_connections == 2, timeout=2.0))

            # Flood 20 silent raw sockets simultaneously
            max_observed_threads = threading.active_count()
            for _ in range(20):
                s = socket.create_connection(("127.0.0.1", srv.server_port), timeout=2.0)
                flood.append(s)
                cur = threading.active_count()
                if cur > max_observed_threads:
                    max_observed_threads = cur

            self.assertTrue(_wait_until(lambda: srv.rejected_connections >= 20, timeout=2.0))
            # Hard upper bound: base + max_connections(2) + max_reject_workers(2) + small margin(2)
            self.assertLessEqual(max_observed_threads, base_threads + 2 + 2 + 2)
            self.assertLessEqual(srv.peak_connections, 2)

            for s in holders:
                s.close()
            holders.clear()

            self.assertTrue(_wait_until(lambda: srv.active_connections == 0, timeout=2.0))
            with self._opener.open(f"http://127.0.0.1:{srv.server_port}/", timeout=1.0) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(json.loads(resp.read().decode("utf-8")), {"ok": True})
        finally:
            for s in holders + flood:
                try:
                    s.close()
                except OSError:
                    pass
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2.0)

    def test_tc_http_05_silent_zero_byte_slowloris_timeout_all_daemons(self) -> None:
        """TC-HTTP-05: 0-byte silent Slowloris sockets expire within timeout and free all slots."""
        daemons = _start_daemon_suite(max_connections=4, request_timeout_sec=0.35)
        try:
            for name, srv, path in daemons:
                with self.subTest(daemon=name):
                    socks: List[socket.socket] = []
                    t0 = time.monotonic()
                    try:
                        for _ in range(3):
                            socks.append(
                                socket.create_connection(("127.0.0.1", srv.server_port), timeout=2.0)
                            )
                        self.assertTrue(
                            _wait_until(lambda s=srv: s.timed_out_connections >= 3, timeout=2.0),
                            f"{name} did not time out silent Slowloris sockets",
                        )
                        elapsed = time.monotonic() - t0
                        self.assertGreaterEqual(elapsed, 0.25)
                        self.assertLess(elapsed, 1.8)
                        self.assertTrue(
                            _wait_until(lambda s=srv: s.active_connections == 0, timeout=1.5),
                            f"{name} did not release slots after timeout",
                        )
                    finally:
                        for s in socks:
                            s.close()

                    with self._opener.open(f"{srv.server_url}{path.lstrip('/')}", timeout=2.0) as resp:
                        self.assertEqual(resp.status, 200)
        finally:
            for _, srv, _ in daemons:
                srv.stop()

    def test_tc_http_06_header_trickle_slowloris_defeated_by_monotonic_deadline(self) -> None:
        """TC-HTTP-06: 1-byte-per-50ms header trickle is terminated strictly at header_timeout_sec."""
        daemons = _start_daemon_suite(max_connections=4, request_timeout_sec=0.40)
        try:
            for name, srv, path in daemons:
                with self.subTest(daemon=name):
                    sock = socket.create_connection(("127.0.0.1", srv.server_port), timeout=2.0)
                    t0 = time.monotonic()
                    try:
                        sock.sendall(f"GET {path} HTTP/1.1\r\nX-Slow: ".encode("ascii"))
                        # Continuously trickle 1 byte every 50ms for up to 1.5s
                        for _ in range(30):
                            time.sleep(0.05)
                            try:
                                sock.sendall(b"a")
                            except OSError:
                                break
                            if srv.active_connections == 0 and srv.timed_out_connections >= 1:
                                break
                        self.assertTrue(
                            _wait_until(
                                lambda s=srv: s.active_connections == 0 and s.timed_out_connections >= 1,
                                timeout=1.5,
                            ),
                            f"{name} allowed trickle Slowloris to stay open",
                        )
                        elapsed = time.monotonic() - t0
                        self.assertLess(elapsed, 1.4)
                    finally:
                        sock.close()
        finally:
            for _, srv, _ in daemons:
                srv.stop()

    def test_tc_http_07_oversized_header_flood_rejected_with_431(self) -> None:
        """TC-HTTP-07: Headers exceeding max_header_bytes return HTTP 431 and close connection."""
        limits = HttpLimitsConfig(
            max_connections=4,
            request_timeout_sec=2.0,
            max_header_bytes=2048,
        )
        srv = BoundedHTTPServer(("127.0.0.1", 0), _EchoHandler, http_limits=limits)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            sock = socket.create_connection(("127.0.0.1", srv.server_port), timeout=2.0)
            try:
                # 8 headers x 400 bytes = 3200 bytes > 2048 cap (each line < 64 KiB)
                req_lines = ["GET / HTTP/1.1", "Host: 127.0.0.1"]
                for i in range(8):
                    req_lines.append(f"X-Pad-{i}: " + ("A" * 400))
                raw_req = ("\r\n".join(req_lines) + "\r\n\r\n").encode("ascii")
                sock.sendall(raw_req)
                raw_resp = b""
                while b"\r\n\r\n" not in raw_resp:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    raw_resp += chunk
                resp_text = raw_resp.decode("utf-8", errors="replace")
                self.assertIn("431 Request Header Fields Too Large", resp_text)
                self.assertIn("Connection: close", resp_text)
                self.assertIn("X-Content-Type-Options: nosniff", resp_text)
            finally:
                sock.close()
            self.assertTrue(_wait_until(lambda: srv.active_connections == 0, timeout=1.5))
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2.0)

    def test_tc_http_08_keepalive_idle_expiration_releases_slot_cleanly(self) -> None:
        """TC-HTTP-08: Idle HTTP/1.1 keep-alive connection expires without counting as Slowloris."""
        limits = HttpLimitsConfig(
            max_connections=4,
            request_timeout_sec=2.0,
            header_timeout_sec=2.0,
            keepalive_timeout_sec=0.30,
        )
        srv = BoundedHTTPServer(("127.0.0.1", 0), _EchoHandler, http_limits=limits)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=2.0)
            try:
                conn.request("GET", "/", headers={"Connection": "keep-alive"})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                resp.read()
                # Leave socket open and idle past keepalive_timeout_sec=0.30s
                self.assertTrue(_wait_until(lambda: srv.active_connections == 0, timeout=1.5))
                # Clean keep-alive idle expiration must NOT be counted as Slowloris timeout
                self.assertEqual(srv.timed_out_connections, 0)
            finally:
                conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2.0)

    def test_tc_http_09_keepalive_max_requests_cap_forces_close(self) -> None:
        """TC-HTTP-09: Server enforces max_keepalive_requests=3 and emits Connection: close on #3."""
        limits = HttpLimitsConfig(
            max_connections=4,
            request_timeout_sec=2.0,
            keepalive_timeout_sec=2.0,
            max_keepalive_requests=3,
        )
        srv = BoundedHTTPServer(("127.0.0.1", 0), _EchoHandler, http_limits=limits)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=2.0)
            try:
                for idx in range(1, 4):
                    conn.request("GET", "/", headers={"Connection": "keep-alive"})
                    resp = conn.getresponse()
                    self.assertEqual(resp.status, 200)
                    resp.read()
                    if idx < 3:
                        self.assertNotEqual(
                            (resp.getheader("Connection") or "").lower(), "close"
                        )
                    else:
                        self.assertEqual(
                            (resp.getheader("Connection") or "").lower(), "close"
                        )
                self.assertTrue(_wait_until(lambda: srv.active_connections == 0, timeout=1.5))
            finally:
                conn.close()
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=2.0)

    def test_tc_http_10_metrics_server_live_scrape_timeout_504(self) -> None:
        """TC-HTTP-10: MetricsServer enforces ExporterConfig.scrape_timeout_seconds with HTTP 504."""
        reg = MetricRegistry()
        proc = ProcessMetricsCollector(version="1.0.0")
        reg.register(_SlowCollector(delay_sec=0.35))
        reg.register(proc)

        srv = MetricsServer(
            server_address=("127.0.0.1", 0),
            registry=reg,
            config=ExporterConfig(
                host="127.0.0.1",
                port=9100,
                scrape_timeout_seconds=0.15,
                max_connections=4,
            ),
        )
        srv.start(background=True)
        try:
            with self.assertRaises(HTTPError) as ctx:
                self._opener.open(f"{srv.server_url}metrics", timeout=3.0)
            self.assertEqual(ctx.exception.code, 504)
            self.assertEqual(ctx.exception.headers.get("Connection"), "close")
            body = json.loads(ctx.exception.read().decode("utf-8"))
            self.assertEqual(body["status"], 504)
            self.assertGreaterEqual(srv.timed_out_connections, 1)

            self.assertTrue(_wait_until(lambda: srv.active_connections == 0, timeout=1.5))
            # ProcessMetricsCollector recorded status="504"
            fams = {f.name: f for f in proc.collect()}
            labels = {s.labels: s.value for s in fams["railmon_exporter_scrapes"].samples}
            self.assertIn((("endpoint", "/metrics"), ("status", "504")), labels)
        finally:
            srv.stop()

    def test_tc_http_12_sse_capacity_isolation_preserves_rest_metrics_gtfs(self) -> None:
        """TC-HTTP-12: SSE max_clients cap returns 503 while preserving REST, /metrics, and /gtfs-rt."""
        store = TrainStateStore()
        broadcaster = SSEBroadcaster(max_clients=2)
        srv = WebMapServer(
            server_address=("127.0.0.1", 0),
            store=store,
            broadcaster=broadcaster,
            config=ServerConfig(
                host="127.0.0.1",
                port=8080,
                max_connections=6,
                max_sse_clients=2,
                request_timeout_sec=3.0,
                sse_write_timeout_sec=3.0,
            ),
        )
        srv.start(background=True)
        sse_conns: List[http.client.HTTPConnection] = []
        try:
            # Open 2 persistent SSE streams (fills SSE cap = 2, leaves 4 HTTP slots free!)
            for _ in range(2):
                c = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=3.0)
                c.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
                r = c.getresponse()
                self.assertEqual(r.status, 200)
                # Read first line of init frame to confirm handshake completed
                self.assertEqual(r.readline().decode("utf-8").strip(), "event: init")
                sse_conns.append(c)

            self.assertEqual(broadcaster.client_count, 2)

            # 3rd SSE subscriber is cleanly rejected with HTTP 503 + Retry-After + Connection: close
            c3 = http.client.HTTPConnection("127.0.0.1", srv.server_port, timeout=3.0)
            try:
                c3.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
                r3 = c3.getresponse()
                self.assertEqual(r3.status, 503)
                self.assertEqual(r3.getheader("Retry-After"), "5")
                self.assertEqual(r3.getheader("Connection"), "close")
                body3 = json.loads(r3.read().decode("utf-8"))
                self.assertEqual(body3["error"], 503)
            finally:
                c3.close()

            self.assertEqual(broadcaster.total_clients_rejected, 1)

            # REST, /api/health, /metrics, and /gtfs-rt/feed.pb all stay healthy (200 OK)
            for endpoint in ("api/trains", "api/health", "metrics", "gtfs-rt/feed.pb"):
                with self._opener.open(f"{srv.server_url}{endpoint}", timeout=3.0) as resp:
                    self.assertEqual(resp.status, 200, f"Endpoint /{endpoint} starved by SSE cap")
                    resp.read()
        finally:
            for c in sse_conns:
                c.close()
            srv.stop()

    def test_tc_http_13_stalled_sse_consumer_evicted_by_write_timeout(self) -> None:
        """TC-HTTP-13: Stalled SSE consumer triggers sse_write_timeout_sec, unregisters, and frees slot."""
        store = TrainStateStore()
        broadcaster = SSEBroadcaster(max_clients=4)
        srv = WebMapServer(
            server_address=("127.0.0.1", 0),
            store=store,
            broadcaster=broadcaster,
            config=ServerConfig(
                host="127.0.0.1",
                port=8080,
                max_connections=6,
                max_sse_clients=4,
                request_timeout_sec=2.0,
                sse_write_timeout_sec=0.30,
            ),
        )
        srv.start(background=True)
        sock = socket.create_connection(("127.0.0.1", srv.server_port), timeout=2.0)
        try:
            # Shrink client receive buffer and stop reading after headers so kernel send buffer fills
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
            except OSError:
                pass
            sock.sendall(b"GET /api/stream HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            self.assertTrue(_wait_until(lambda: broadcaster.client_count == 1, timeout=2.0))

            # Broadcast large payloads without reading on client socket until send buffer stalls
            big_payload = json.dumps({"pad": "X" * 65536})
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline and broadcaster.client_count > 0:
                broadcaster.broadcast("trainPositionsUx", big_payload)
                time.sleep(0.02)

            self.assertEqual(
                broadcaster.client_count,
                0,
                "Stalled zero-window SSE consumer was not evicted on sse_write_timeout_sec",
            )
            self.assertTrue(_wait_until(lambda: srv.active_connections == 0, timeout=2.0))
            self.assertGreaterEqual(srv.timed_out_connections, 1)
        finally:
            sock.close()
            srv.stop()

    def test_tc_http_14_unified_handle_error_suppresses_stderr_tracebacks(self) -> None:
        """TC-HTTP-14: Abrupt TCP RST and timeouts emit zero tracebacks to stderr across all 3 daemons."""
        daemons = _start_daemon_suite(max_connections=4, request_timeout_sec=0.25)
        stderr_buf = io.StringIO()
        try:
            with patch("sys.stderr", stderr_buf):
                for name, srv, path in daemons:
                    # 1. Instant RST disconnect
                    s_rst = socket.create_connection(("127.0.0.1", srv.server_port), timeout=2.0)
                    s_rst.sendall(f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n".encode("ascii"))
                    s_rst.setsockopt(
                        socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0)
                    )
                    s_rst.close()

                    # 2. Silent Slowloris timeout
                    s_to = socket.create_connection(("127.0.0.1", srv.server_port), timeout=2.0)
                    self.assertTrue(
                        _wait_until(lambda s=srv: s.timed_out_connections >= 1, timeout=2.0),
                        f"{name} did not record timeout",
                    )
                    s_to.close()
                    self.assertTrue(_wait_until(lambda s=srv: s.active_connections == 0, timeout=1.5))

            self.assertNotIn("Traceback (most recent call last)", stderr_buf.getvalue())
        finally:
            for _, srv, _ in daemons:
                srv.stop()


if __name__ == "__main__":
    unittest.main()
