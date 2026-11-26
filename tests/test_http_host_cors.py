"""Regression tests for P1-11: Host header DNS-rebinding defense & opt-in CORS across all 3 HTTP daemons."""

import ast
import json
import pathlib
import socket
import sys
import unittest
from typing import Dict, List, Optional, Tuple
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from exporter.collector import MetricRegistry, ProcessMetricsCollector
from exporter.models import ExporterConfig
from exporter.server import MetricsServer
from gtfs_rt.models import GtfsRtConfig
from gtfs_rt.server import GtfsRtServer
import railmon_http
from railmon_http import (
    BoundedHTTPRequestHandler,
    BoundedHTTPServer,
    HostValidationPolicy,
    is_loopback_bind_host,
    normalize_allowed_host,
    parse_host_header,
    validate_allowed_hosts,
)
from server.daemon import WebMapServer
from server.models import ServerConfig
from server.sse import SSEBroadcaster
import train_tracker
from tui.state import TrainStateStore


def _raw_http_request(
    host: str,
    port: int,
    raw_bytes: bytes,
    timeout: float = 3.0,
) -> Tuple[int, Dict[str, str], bytes]:
    """Send a raw HTTP request over TCP and parse (status_code, headers_lower, body)."""
    with socket.create_connection((host, port), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall(raw_bytes)
        chunks: List[bytes] = []
        while True:
            try:
                data = sock.recv(4096)
            except TimeoutError:
                break
            if not data:
                break
            chunks.append(data)
    raw = b"".join(chunks)
    sep = raw.find(b"\r\n\r\n")
    if sep == -1:
        raise AssertionError(f"Malformed HTTP response without header terminator: {raw!r}")
    head_text = raw[:sep].decode("iso-8859-1")
    body = raw[sep + 4 :]
    lines = head_text.split("\r\n")
    status_code = int(lines[0].split(" ", 2)[1])
    headers: Dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return status_code, headers, body


class _SimpleEchoHandler(BoundedHTTPRequestHandler):
    """Minimal HTTP/1.1 handler for isolated HostValidationPolicy tests."""

    def do_GET(self) -> None:
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", "11")
        self.send_header("Connection", "close")
        self.end_headers()


class TestHostParsingAndPolicyUnit(unittest.TestCase):
    """Unit tests for loopback detection, RFC 9110 Host parsing, and allowlist validation."""

    def test_tc_host_01_is_loopback_bind_host(self) -> None:
        """TC-HOST-01: Loopback bind detection handles IPv4/8, IPv6, IPv4-mapped, and localhost."""
        for addr in ("127.0.0.1", "127.0.0.2", "127.255.255.254", "localhost", "LOCALHOST", "sub.localhost", "::1", "[::1]", "::ffff:127.0.0.1"):
            self.assertTrue(is_loopback_bind_host(addr), f"Expected loopback for {addr!r}")
        for addr in ("0.0.0.0", "::", "", "192.168.1.10", "10.0.0.1", "example.com", "evil.attacker.com"):
            self.assertFalse(is_loopback_bind_host(addr), f"Expected non-loopback for {addr!r}")

    def test_tc_host_02_parse_host_header(self) -> None:
        """TC-HOST-02: Strict RFC 9110 Host parser accepts valid forms and rejects malformed/obfuscated inputs."""
        self.assertEqual(parse_host_header("127.0.0.1"), ("127.0.0.1", None))
        self.assertEqual(parse_host_header("127.0.0.1:9100"), ("127.0.0.1", 9100))
        self.assertEqual(parse_host_header("LOCALHOST:8080"), ("localhost", 8080))
        self.assertEqual(parse_host_header("localhost."), ("localhost", None))
        self.assertEqual(parse_host_header("[::1]"), ("::1", None))
        self.assertEqual(parse_host_header("[0:0:0:0:0:0:0:1]:8085"), ("::1", 8085))
        self.assertEqual(parse_host_header("railmon.internal:443"), ("railmon.internal", 443))

        malformed = [
            "",
            "   ",
            "127.0.0.1 ",
            " 127.0.0.1",
            "127.0.0.1\r\nX-Injected: 1",
            "127.0.0.1@evil.com",
            "127.0.0.1/metrics",
            "127.0.0.1?foo=1",
            "127.0.0.1#frag",
            "127.0.0.1\\foo",
            "::1",
            "[::1]:0",
            "[::1]:65536",
            "[::1]:abc",
            "[::1]extra",
            "[::1%eth0]:8080",
            "localhost:0",
            "localhost:65536",
            "localhost:-1",
            "localhost:+80",
            "localhost:",
            "0177.0.0.1",
            "2130706433",
            "0x7f000001",
            "127.1",
            ".leadingdot.com",
        ]
        for raw in malformed:
            self.assertIsNone(parse_host_header(raw), f"Expected None for malformed Host {raw!r}")

    def test_tc_host_03_validate_allowed_hosts(self) -> None:
        """TC-HOST-03: validate_allowed_hosts canonicalizes, deduplicates, and rejects wildcards/ports."""
        res = validate_allowed_hosts(["Railmon.Local.", "[0:0:0:0:0:0:0:1]", "192.168.1.50", "railmon.local"])
        self.assertEqual(res, ("railmon.local", "::1", "192.168.1.50"))

        with self.assertRaises(ValueError):
            validate_allowed_hosts("railmon.local")  # type: ignore[arg-type]

        for bad in ("*", "*.example.com", "railmon.local:8080", "bad host", "a/b", "a@b", "", "0177.0.0.1", "::1%lo"):
            with self.assertRaises(ValueError, msg=f"Should reject {bad!r}"):
                normalize_allowed_host(bad)

    def test_tc_host_12_wildcard_bind_default_passthrough(self) -> None:
        """TC-HOST-12: Wildcard bind 0.0.0.0 / :: without allowed_hosts does not block LAN/Docker scrapers."""
        p_v4 = HostValidationPolicy.for_bind_address("0.0.0.0", 9100)
        p_v6 = HostValidationPolicy.for_bind_address("::", 9100)
        self.assertFalse(p_v4.enforce)
        self.assertFalse(p_v6.enforce)

        p_strict = HostValidationPolicy.for_bind_address(
            "0.0.0.0", 9100, extra_allowed_hosts=("railmon.internal",)
        )
        self.assertTrue(p_strict.enforce)
        self.assertEqual(p_strict.allowed_hosts, frozenset({"railmon.internal"}))
        self.assertEqual(p_strict.bound_port, 9100)


class TestLiveHostAndCorsEnforcement(unittest.TestCase):
    """Live TCP socket tests across MetricsServer, WebMapServer, and GtfsRtServer."""

    def setUp(self) -> None:
        self._servers: List[BoundedHTTPServer] = []
        self._opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        for srv in reversed(self._servers):
            stop_fn = getattr(srv, "stop", None)
            if callable(stop_fn):
                stop_fn()
            else:
                srv.shutdown()
                srv.server_close()

    def _start_metrics(self, config: Optional[ExporterConfig] = None) -> MetricsServer:
        registry = MetricRegistry()
        registry.register(ProcessMetricsCollector(version="test"))
        srv = MetricsServer(("127.0.0.1", 0), registry=registry, config=config)
        srv.start(background=True)
        self._servers.append(srv)
        return srv

    def _start_web(self, config: Optional[ServerConfig] = None) -> Tuple[WebMapServer, SSEBroadcaster]:
        store = TrainStateStore()
        broadcaster = SSEBroadcaster(max_clients=8)
        srv = WebMapServer(("127.0.0.1", 0), store=store, broadcaster=broadcaster, config=config)
        srv.start(background=True)
        self._servers.append(srv)
        return srv, broadcaster

    def _start_gtfs(self, config: Optional[GtfsRtConfig] = None) -> GtfsRtServer:
        store = TrainStateStore()
        srv = GtfsRtServer(("127.0.0.1", 0), store=store, config=config)
        srv.start(background=True)
        self._servers.append(srv)
        return srv

    def test_tc_host_04_dns_rebinding_metrics_server(self) -> None:
        """TC-HOST-04: MetricsServer rejects untrusted Host with 421, Connection: close, no CORS, no reflection."""
        srv = self._start_metrics(ExporterConfig(cors_enabled=True))
        port = srv.server_port
        status, headers, body = _raw_http_request(
            "127.0.0.1",
            port,
            f"GET /metrics HTTP/1.1\r\nHost: evil.attacker.com:{port}\r\n\r\n".encode("ascii"),
        )
        self.assertEqual(status, 421)
        self.assertEqual(headers.get("connection"), "close")
        self.assertIsNone(headers.get("access-control-allow-origin"))
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertNotIn(b"evil.attacker.com", body)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["status"], 421)
        self.assertEqual(srv.rejected_host_requests, 1)

    def test_tc_host_05_dns_rebinding_web_server_and_sse(self) -> None:
        """TC-HOST-05: WebMapServer rejects DNS rebinding on REST, OPTIONS, and /api/stream before dispatch."""
        srv, broadcaster = self._start_web(ServerConfig(cors_enabled=True))
        port = srv.server_port

        for req_line in (
            f"GET /api/trains HTTP/1.1\r\nHost: evil.attacker.com:{port}\r\n\r\n",
            f"GET /api/stream HTTP/1.1\r\nHost: evil.attacker.com:{port}\r\n\r\n",
            f"OPTIONS /api/trains HTTP/1.1\r\nHost: evil.attacker.com:{port}\r\n\r\n",
        ):
            status, headers, _ = _raw_http_request("127.0.0.1", port, req_line.encode("ascii"))
            self.assertEqual(status, 421)
            self.assertIsNone(headers.get("access-control-allow-origin"))
            self.assertEqual(headers.get("connection"), "close")

        self.assertEqual(broadcaster.client_count, 0)
        self.assertEqual(srv.rejected_host_requests, 3)

    def test_tc_host_06_dns_rebinding_gtfs_server(self) -> None:
        """TC-HOST-06: GtfsRtServer rejects DNS rebinding on ProtoJSON and Protobuf feeds with 421."""
        srv = self._start_gtfs(GtfsRtConfig(cors_enabled=True))
        port = srv.server_port

        for path in ("/gtfs-rt/feed.json", "/gtfs-rt/feed.pb"):
            status, headers, _ = _raw_http_request(
                "127.0.0.1",
                port,
                f"GET {path} HTTP/1.1\r\nHost: evil.attacker.com:{port}\r\n\r\n".encode("ascii"),
            )
            self.assertEqual(status, 421)
            self.assertIsNone(headers.get("access-control-allow-origin"))

        self.assertEqual(srv.rejected_host_requests, 2)

    def test_tc_host_07_missing_and_duplicate_host_headers(self) -> None:
        """TC-HOST-07: Missing or duplicate Host headers return 400 Bad Request across all 3 daemons."""
        m_srv = self._start_metrics()
        w_srv, _ = self._start_web()
        g_srv = self._start_gtfs()

        for srv, path in (
            (m_srv, "/metrics"),
            (w_srv, "/api/health"),
            (g_srv, "/health"),
        ):
            port = srv.server_port
            st_missing, hdr_missing, _ = _raw_http_request(
                "127.0.0.1", port, f"GET {path} HTTP/1.1\r\n\r\n".encode("ascii")
            )
            self.assertEqual(st_missing, 400)
            self.assertEqual(hdr_missing.get("connection"), "close")

            st_dup, hdr_dup, _ = _raw_http_request(
                "127.0.0.1",
                port,
                f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nHost: evil.com:{port}\r\n\r\n".encode("ascii"),
            )
            self.assertEqual(st_dup, 400)
            self.assertEqual(hdr_dup.get("connection"), "close")

    def test_tc_host_08_malformed_and_obfuscated_host_headers(self) -> None:
        """TC-HOST-08: Malformed and obfuscated numeric Host headers are rejected with 400."""
        srv = self._start_metrics()
        port = srv.server_port
        for bad_host in (
            "127.0.0.1@evil.com",
            "127.0.0.1/metrics",
            "0177.0.0.1",
            "2130706433",
            "127.1",
        ):
            status, headers, _ = _raw_http_request(
                "127.0.0.1",
                port,
                f"GET /metrics HTTP/1.1\r\nHost: {bad_host}\r\n\r\n".encode("ascii"),
            )
            self.assertEqual(status, 400, f"Expected 400 for Host {bad_host!r}, got {status}")
            self.assertEqual(headers.get("connection"), "close")

    def test_tc_host_09_port_mismatch_and_valid_loopback_variants(self) -> None:
        """TC-HOST-09: Wrong port returns 421; canonical loopback variants return 200."""
        srv = self._start_metrics()
        port = srv.server_port
        wrong_port = port - 1 if port > 1 else port + 1

        st_wrong, _, _ = _raw_http_request(
            "127.0.0.1",
            port,
            f"GET /health HTTP/1.1\r\nHost: 127.0.0.1:{wrong_port}\r\nConnection: close\r\n\r\n".encode("ascii"),
        )
        self.assertEqual(st_wrong, 421)

        for valid_host in (
            "127.0.0.1",
            f"127.0.0.1:{port}",
            "localhost",
            f"localhost:{port}",
            "LOCALHOST.",
            f"[::1]:{port}",
        ):
            st_ok, _, _ = _raw_http_request(
                "127.0.0.1",
                port,
                f"GET /health HTTP/1.1\r\nHost: {valid_host}\r\nConnection: close\r\n\r\n".encode("ascii"),
            )
            self.assertEqual(st_ok, 200, f"Expected 200 for valid loopback Host {valid_host!r}")

    def test_tc_host_10_head_rejection_emits_zero_body(self) -> None:
        """TC-HOST-10: HEAD request rejected with 421 sends Content-Length header and zero body bytes."""
        srv = self._start_metrics()
        port = srv.server_port
        status, headers, body = _raw_http_request(
            "127.0.0.1",
            port,
            f"HEAD /metrics HTTP/1.1\r\nHost: evil.attacker.com:{port}\r\n\r\n".encode("ascii"),
        )
        self.assertEqual(status, 421)
        self.assertGreater(int(headers.get("content-length", "0")), 0)
        self.assertEqual(body, b"")

    def test_tc_host_11_explicit_allowed_hosts(self) -> None:
        """TC-HOST-11: Explicit allowed_hosts permits configured hostnames and still blocks untrusted ones."""
        srv = self._start_metrics(ExporterConfig(allowed_hosts=("railmon.internal",)))
        port = srv.server_port

        st_ok, _, _ = _raw_http_request(
            "127.0.0.1",
            port,
            f"GET /health HTTP/1.1\r\nHost: railmon.internal:{port}\r\nConnection: close\r\n\r\n".encode("ascii"),
        )
        self.assertEqual(st_ok, 200)

        st_bad, _, _ = _raw_http_request(
            "127.0.0.1",
            port,
            f"GET /health HTTP/1.1\r\nHost: other.internal:{port}\r\nConnection: close\r\n\r\n".encode("ascii"),
        )
        self.assertEqual(st_bad, 421)

    def test_tc_host_13_metrics_server_opt_in_cors_toggle(self) -> None:
        """TC-HOST-13: MetricsServer omits CORS headers by default and emits them only when cors_enabled=True."""
        # 1. Default secure (cors_enabled=False)
        srv_default = self._start_metrics()
        for path, method in (
            ("metrics", "GET"),
            ("health", "GET"),
            ("", "GET"),
            ("metrics", "OPTIONS"),
        ):
            req = Request(f"{srv_default.server_url}{path}", method=method)
            with self._opener.open(req, timeout=3.0) as resp:
                self.assertIsNone(
                    resp.headers.get("Access-Control-Allow-Origin"),
                    f"CORS must be omitted by default on {method} /{path}",
                )
        with self.assertRaises(HTTPError) as ctx_404:
            self._opener.open(Request(f"{srv_default.server_url}nonexistent"), timeout=3.0)
        self.assertEqual(ctx_404.exception.code, 404)
        self.assertIsNone(ctx_404.exception.headers.get("Access-Control-Allow-Origin"))

        # 2. Opt-in (cors_enabled=True)
        srv_cors = self._start_metrics(ExporterConfig(cors_enabled=True))
        for path, method in (("metrics", "GET"), ("metrics", "OPTIONS")):
            req = Request(f"{srv_cors.server_url}{path}", method=method)
            with self._opener.open(req, timeout=3.0) as resp:
                self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

    def test_tc_host_14_gtfs_server_opt_in_cors_default(self) -> None:
        """TC-HOST-14: GtfsRtServer omits CORS headers by default and emits them when cors_enabled=True."""
        srv_default = self._start_gtfs()
        self.assertFalse(srv_default.config.cors_enabled)
        for path, method in (("gtfs-rt/feed.json", "GET"), ("gtfs-rt/feed.json", "OPTIONS")):
            req = Request(f"{srv_default.server_url}{path}", method=method)
            with self._opener.open(req, timeout=3.0) as resp:
                self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))

        srv_cors = self._start_gtfs(GtfsRtConfig(cors_enabled=True))
        req = Request(f"{srv_cors.server_url}gtfs-rt/feed.json")
        with self._opener.open(req, timeout=3.0) as resp:
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

    def test_tc_host_15_cli_cors_and_allowed_hosts_flags(self) -> None:
        """TC-HOST-15: CLI parsers accept --cors (metrics) and --allowed-hosts, rejecting invalid values."""
        parser = train_tracker.build_parser()

        m_default = parser.parse_args(["metrics"])
        self.assertFalse(m_default.cors)
        self.assertIsNone(m_default.allowed_hosts)

        m_custom = parser.parse_args(
            ["metrics", "--cors", "--allowed-hosts", "railmon.local, 192.168.1.50"]
        )
        self.assertTrue(m_custom.cors)
        self.assertEqual(m_custom.allowed_hosts, "railmon.local, 192.168.1.50")

        s_custom = parser.parse_args(["serve", "--allowed-hosts", "map.internal"])
        self.assertEqual(s_custom.allowed_hosts, "map.internal")

        e_custom = parser.parse_args(["gtfs-rt", "--serve", "--allowed-hosts", "gtfs.internal"])
        self.assertEqual(e_custom.allowed_hosts, "gtfs.internal")
        cfg = train_tracker._build_gtfs_config(e_custom)
        self.assertEqual(cfg.allowed_hosts, ("gtfs.internal",))

        # Invalid wildcard in --allowed-hosts rejected cleanly (exit code 2)
        bad_metrics = parser.parse_args(["metrics", "--allowed-hosts", "*.evil.com"])
        self.assertEqual(train_tracker.run_metrics_mode(bad_metrics), 2)

        bad_serve = parser.parse_args(["serve", "--allowed-hosts", "bad,,host"])
        self.assertEqual(train_tracker.run_serve_mode(bad_serve), 2)

        bad_export = parser.parse_args(["gtfs-rt", "--serve", "--allowed-hosts", "*.evil.com"])
        with self.assertRaises(ValueError):
            train_tracker._build_gtfs_config(bad_export)

    def test_tc_host_16_railmon_http_leaf_purity(self) -> None:
        """TC-HOST-16: railmon_http.py remains a pure Python stdlib acyclic leaf module."""
        src = pathlib.Path(railmon_http.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        stdlib_names = getattr(sys, "stdlib_module_names", frozenset())
        imported: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.extend(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module.split(".")[0])
        for mod in imported:
            self.assertIn(mod, stdlib_names, f"Non-stdlib import '{mod}' detected in railmon_http.py")


if __name__ == "__main__":
    unittest.main()
