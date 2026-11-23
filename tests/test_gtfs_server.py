"""Integration tests for standalone GtfsRtServer and WebMapServer /gtfs-rt/* mount."""

import gzip
import json
import socket
import unittest
import urllib.error
import urllib.request

from gtfs_rt.models import FeedMessage, GtfsRtConfig
from gtfs_rt.server import GtfsRtServer, etag_matches, resolve_gtfs_route
from server.daemon import WebMapServer
from server.models import ServerConfig
from server.sse import SSEBroadcaster
from tui.mock_engine import MockTelemetryEngine
from tui.state import TrainStateStore


class TestGtfsServer(unittest.TestCase):
    """Verify standalone GTFS-RT HTTP daemon and railmon serve cross-exposure."""

    def setUp(self) -> None:
        self.store = TrainStateStore()
        self.store.update_positions(MockTelemetryEngine().step(1.0))
        self.config = GtfsRtConfig(host="127.0.0.1", port=8085, cors_enabled=True)
        self.server = GtfsRtServer(
            server_address=("127.0.0.1", 0),
            store=self.store,
            config=self.config,
        )
        self.server.start(background=True)
        self.base_url = self.server.server_url.rstrip("/")

    def tearDown(self) -> None:
        self.server.stop()

    def test_binary_protobuf_endpoints_decode_cleanly(self):
        """TC-GTFS-SRV-01: All four .pb routes return valid decodable GTFS-RT v2.0 payloads."""
        routes = [
            "/gtfs-rt/feed.pb",
            "/gtfs-rt/vehicle-positions.pb",
            "/gtfs-rt/trip-updates.pb",
            "/gtfs-rt/alerts.pb",
        ]
        for path in routes:
            with urllib.request.urlopen(f"{self.base_url}{path}", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "application/x-protobuf")
                self.assertEqual(resp.headers.get("X-GTFS-RT-Version"), "2.0")
                self.assertIsNotNone(resp.headers.get("ETag"))
                body = resp.read()
                decoded = FeedMessage.from_proto(body)
                self.assertEqual(decoded.header.gtfs_realtime_version, "2.0")

    def test_json_routes_query_param_and_accept_negotiation(self):
        """TC-GTFS-SRV-02: .json routes, ?format=json, and Accept: application/json return ProtoJSON."""
        with urllib.request.urlopen(f"{self.base_url}/gtfs-rt/feed.json", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["header"]["gtfsRealtimeVersion"], "2.0")
            self.assertGreater(len(data["entity"]), 0)

        with urllib.request.urlopen(
            f"{self.base_url}/gtfs-rt/vehicle-positions.pb?format=json", timeout=5
        ) as resp:
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))

        req = urllib.request.Request(
            f"{self.base_url}/gtfs-rt/trip-updates.pb",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))

    def test_etag_conditional_get_returns_304_not_modified(self):
        """TC-GTFS-SRV-03: Sending matching If-None-Match ETag yields HTTP 304."""
        with urllib.request.urlopen(f"{self.base_url}/gtfs-rt/feed.pb", timeout=5) as resp:
            etag = resp.headers.get("ETag")
            self.assertIsNotNone(etag)

        req = urllib.request.Request(
            f"{self.base_url}/gtfs-rt/feed.pb",
            headers={"If-None-Match": str(etag)},
        )
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 304)
        self.assertTrue(etag_matches(str(etag), str(etag)))

    def test_gzip_compression_and_head_options_405_404(self):
        """TC-GTFS-SRV-04: Supports gzip, HEAD, OPTIONS, rejects POST (405) and unknown paths (404)."""
        req = urllib.request.Request(
            f"{self.base_url}/gtfs-rt/feed.pb",
            headers={"Accept-Encoding": "gzip"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.headers.get("Content-Encoding"), "gzip")
            raw = gzip.decompress(resp.read())
            self.assertEqual(FeedMessage.from_proto(raw).header.gtfs_realtime_version, "2.0")

        # HEAD returns zero body
        head_req = urllib.request.Request(f"{self.base_url}/gtfs-rt/feed.pb", method="HEAD")
        with urllib.request.urlopen(head_req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.read(), b"")

        # OPTIONS preflight
        opt_req = urllib.request.Request(f"{self.base_url}/gtfs-rt/feed.pb", method="OPTIONS")
        with urllib.request.urlopen(opt_req, timeout=5) as resp:
            self.assertEqual(resp.status, 204)
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")

        # POST rejected with 405
        post_req = urllib.request.Request(f"{self.base_url}/gtfs-rt/feed.pb", method="POST", data=b"x")
        with self.assertRaises(urllib.error.HTTPError) as ctx_405:
            urllib.request.urlopen(post_req, timeout=5)
        self.assertEqual(ctx_405.exception.code, 405)

        # Path traversal / unknown route rejected with 404
        with self.assertRaises(urllib.error.HTTPError) as ctx_404:
            urllib.request.urlopen(f"{self.base_url}/gtfs-rt/../../etc/passwd", timeout=5)
        self.assertEqual(ctx_404.exception.code, 404)

    def test_health_index_and_abrupt_client_disconnect(self):
        """TC-GTFS-SRV-05: /health and / serve cleanly, and abrupt socket drop does not crash server."""
        with urllib.request.urlopen(f"{self.base_url}/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(health["status"], "ok")
            self.assertEqual(health["gtfs_rt_version"], "2.0")

        with urllib.request.urlopen(f"{self.base_url}/", timeout=5) as resp:
            html = resp.read().decode("utf-8")
            self.assertIn("/gtfs-rt/feed.pb", html)

        # Abrupt socket close before reading response
        sock = socket.create_connection(("127.0.0.1", self.server.server_port), timeout=5)
        sock.sendall(b"GET /gtfs-rt/feed.pb HTTP/1.1\r\nHost: localhost\r\n\r\n")
        sock.close()

        # Server remains healthy immediately after abrupt disconnect
        with urllib.request.urlopen(f"{self.base_url}/health", timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_web_map_daemon_cross_exposes_gtfs_rt_endpoints(self):
        """TC-GTFS-SRV-06: WebMapServer (railmon serve) cross-exposes /gtfs-rt/*.pb and .json."""
        web = WebMapServer(
            server_address=("127.0.0.1", 0),
            store=self.store,
            broadcaster=SSEBroadcaster(),
            config=ServerConfig(host="127.0.0.1", port=8080, cors_enabled=True),
        )
        web.start(background=True)
        try:
            url = web.server_url.rstrip("/")
            with urllib.request.urlopen(f"{url}/gtfs-rt/feed.pb", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("Content-Type"), "application/x-protobuf")
                decoded = FeedMessage.from_proto(resp.read())
                self.assertGreater(len(decoded.entity), 0)

            with urllib.request.urlopen(f"{url}/gtfs-rt/vehicle-positions.json", timeout=5) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertEqual(data["header"]["gtfsRealtimeVersion"], "2.0")
        finally:
            web.stop()

    def test_resolve_gtfs_route_unknown_returns_none(self):
        """TC-GTFS-SRV-07: Unknown paths map to None cleanly."""
        self.assertIsNone(resolve_gtfs_route("/gtfs-rt/unknown.pb"))


if __name__ == "__main__":
    unittest.main()
