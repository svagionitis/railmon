"""Comprehensive unit and integration test suite for railmon WebMapServer and WebMapRequestHandler."""

import http.client
import json
import socket
import time
import unittest
from typing import Any, Optional
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from server.daemon import WebMapServer
from server.models import ServerConfig, TileProvider
from server.sse import SSEBroadcaster
from train_tracker import TrainPosition
from tui.geo import ConflictAlert, ConflictSeverity, ConflictType
from tui.state import TrainStateStore


def _create_sample_train(
    train_id: str = "ic-50",
    train_number: str = "IC 50",
    lat: float = 38.0,
    lng: float = 23.7,
    speed: float = 120.0,
    delay: float = 2.0,
    corridor: str = "PATHE",
    origin: str = "Athens",
    destination: str = "Thessaloniki",
) -> TrainPosition:
    """Helper to instantiate a valid TrainPosition for testing."""
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120-001",
        name=train_number,
        train_type="InterCity",
        service_type="Passenger",
        status="Κανονική" if speed > 5.0 else "Στάση",
        origin=origin,
        destination=destination,
        next_station="SKA",
        speed=speed,
        course=15.0,
        track_heading=15.0,
        lat=lat,
        lng=lng,
        altitude=45.0,
        delay=int(delay),
        progress=0.15,
        chainage_m=12000,
        corridor=corridor,
        distance_to_destination_m=480000,
        timestamp="2026-11-23T10:00:00Z",
        schedule_id="sch-101",
    )


class TestWebMapServerDaemon(unittest.TestCase):
    """Test suite covering WebMapServer lifecycle, REST endpoints, CORS, and SSE streaming."""

    def setUp(self) -> None:
        """Create fresh store, broadcaster, and default configuration."""
        self.store = TrainStateStore()
        self.broadcaster = SSEBroadcaster()
        self.config = ServerConfig(
            host="127.0.0.1",
            port=8080,
            cors_enabled=False,
            tile_provider=TileProvider.CARTO_DARK,
        )
        self.server: Optional[WebMapServer] = None
        self._opener = build_opener(ProxyHandler({}))

    def tearDown(self) -> None:
        """Cleanly shut down server if still running."""
        if self.server is not None:
            self.server.stop()
            self.server = None

    def _open(self, req: Request) -> Any:
        """Open request using proxy-free opener to prevent localhost proxy interference."""
        return self._opener.open(req)

    def _start_server(self, config: Optional[ServerConfig] = None) -> WebMapServer:
        """Helper to instantiate and start server in background thread."""
        cfg = config or self.config
        self.server = WebMapServer(
            server_address=(cfg.host, 0),
            store=self.store,
            broadcaster=self.broadcaster,
            config=cfg,
        )
        self.server.start(background=True)
        # Wait until server thread begins servicing
        time.sleep(0.05)
        return self.server

    def test_server_ephemeral_port_startup_and_shutdown(self) -> None:
        """TC-SRV-01: Server starts cleanly on ephemeral port (port 0) and stops without hanging."""
        server = self._start_server()
        port = server.server_port
        self.assertGreater(port, 0)
        self.assertTrue(server.is_running)
        self.assertIn(f":{port}/", server.server_url)

        server.stop()
        self.assertFalse(server.is_running)

    def test_root_html_endpoint(self) -> None:
        """TC-SRV-02: GET / and GET /index.html return 200 text/html with embedded SPA content."""
        server = self._start_server()
        url = server.server_url

        for path in ("/", "/index.html"):
            req = Request(f"{url}{path.lstrip('/')}")
            with self._open(req) as resp:
                self.assertEqual(resp.status, 200)
                content_type = resp.headers.get("Content-Type", "")
                self.assertIn("text/html", content_type)
                body = resp.read().decode("utf-8")
                self.assertIn("<!DOCTYPE html>", body)
                self.assertIn("RAILMON", body)
                self.assertIn("leaflet", body)

    def test_trains_endpoint(self) -> None:
        """TC-SRV-03: GET /api/trains returns 200 application/json array of active train snapshots."""
        train1 = _create_sample_train(train_id="ic-50", speed=142.5, delay=4.0)
        train2 = _create_sample_train(train_id="ic-52", speed=95.0, delay=0.0)
        self.store.update_positions([train1, train2])

        server = self._start_server()
        req = Request(f"{server.server_url}api/trains")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 2)

            train_ids = {t["id"] for t in data}
            self.assertIn("ic-50", train_ids)
            self.assertIn("ic-52", train_ids)

            # Check individual fields
            t50 = next(t for t in data if t["id"] == "ic-50")
            self.assertEqual(t50["speed"], 142.5)
            self.assertEqual(t50["delay"], 4.0)
            self.assertIn("heading", t50)
            self.assertIn("corridor", t50)
            self.assertIn("next_station", t50)

    def test_static_geo_endpoints(self) -> None:
        """TC-SRV-04: GET /api/{tracks,stations,infrastructure,speed-limits} returns 200 with valid data."""
        server = self._start_server()
        url = server.server_url

        geo_routes = [
            ("api/tracks", "application/geo+json"),
            ("api/stations", "application/geo+json"),
            ("api/infrastructure", "application/geo+json"),
            ("api/speed-limits", "application/json"),
        ]

        for path, expected_ct in geo_routes:
            req = Request(f"{url}{path}")
            with self._open(req) as resp:
                self.assertEqual(resp.status, 200, f"Failed on route {path}")
                ct = resp.headers.get("Content-Type", "")
                self.assertIn(expected_ct, ct, f"Wrong Content-Type on route {path}")
                data = json.loads(resp.read().decode("utf-8"))
                self.assertIsInstance(data, dict, f"Data not a dict on route {path}")

    def test_conflicts_endpoint(self) -> None:
        """TC-SRV-05: GET /api/conflicts returns 200 application/json array of active conflict alerts."""
        train1 = _create_sample_train(
            train_id="ic-50", lat=38.000, lng=23.700, speed=120.0
        )
        train2 = _create_sample_train(
            train_id="ic-52", lat=38.003, lng=23.702, speed=90.0
        )
        self.store.update_positions([train1, train2])

        server = self._start_server()
        req = Request(f"{server.server_url}api/conflicts")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/json", resp.headers.get("Content-Type", ""))
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIsInstance(data, list)
            self.assertEqual(len(data), 1)
            self.assertIn(data[0]["train_a"], ("ic-50", "ic-52"))
            self.assertIn(data[0]["train_b"], ("ic-50", "ic-52"))
            self.assertLess(data[0]["distance_km"], 2.0)

    def test_health_endpoint(self) -> None:
        """TC-SRV-06: GET /api/health returns 200 operational metrics snapshot."""
        train = _create_sample_train(train_id="ic-50")
        self.store.update_positions([train])

        server = self._start_server()
        req = Request(f"{server.server_url}api/health")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "healthy")
            self.assertIn("uptime_seconds", data)
            self.assertIn("active_clients", data)
            self.assertEqual(data["active_trains_count"], 1)

    def test_train_history_endpoint(self) -> None:
        """TC-SRV-07: GET /api/history/train/{id} returns GeoJSON LineString; returns 404 if missing."""
        train_p1 = _create_sample_train(train_id="ic-50", lat=38.0, lng=23.7)
        self.store.update_positions([train_p1])

        train_p2 = _create_sample_train(train_id="ic-50", lat=38.05, lng=23.75)
        self.store.update_positions([train_p2])

        server = self._start_server()
        url = server.server_url

        # 1. Existing train
        req = Request(f"{url}api/history/train/ic-50")
        with self._open(req) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("application/geo+json", resp.headers.get("Content-Type", ""))
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["type"], "Feature")
            self.assertEqual(data["geometry"]["type"], "LineString")
            self.assertGreaterEqual(len(data["geometry"]["coordinates"]), 1)

        # 2. Non-existent train returns 404
        req_missing = Request(f"{url}api/history/train/missing-999")
        with self.assertRaises(HTTPError) as ctx:
            self._open(req_missing)
        self.assertEqual(ctx.exception.code, 404)

    def test_cors_headers_toggle(self) -> None:
        """TC-SRV-08: Access-Control-Allow-Origin emitted only when cors_enabled is True."""
        # 1. Default (cors_enabled=False)
        cfg_no_cors = ServerConfig(host="127.0.0.1", port=8080, cors_enabled=False)
        server_no_cors = self._start_server(cfg_no_cors)
        req = Request(f"{server_no_cors.server_url}api/health")
        with self._open(req) as resp:
            self.assertIsNone(resp.headers.get("Access-Control-Allow-Origin"))
        server_no_cors.stop()

        # 2. Enabled (cors_enabled=True)
        cfg_cors = ServerConfig(host="127.0.0.1", port=8080, cors_enabled=True)
        server_cors = self._start_server(cfg_cors)
        req = Request(f"{server_cors.server_url}api/health")
        with self._open(req) as resp:
            self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
            self.assertIn(
                "GET, HEAD, OPTIONS",
                resp.headers.get("Access-Control-Allow-Methods", ""),
            )
        server_cors.stop()

    def test_head_and_options_methods(self) -> None:
        """TC-SRV-09 & TC-SRV-10: HEAD returns headers with empty body; OPTIONS returns 204 preflight."""
        cfg_cors = ServerConfig(host="127.0.0.1", port=8080, cors_enabled=True)
        server = self._start_server(cfg_cors)
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)

        # 1. HEAD request
        conn.request("HEAD", "/api/health")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("application/json", resp.getheader("Content-Type", ""))
        body = resp.read()
        self.assertEqual(len(body), 0)

        # 2. OPTIONS request
        conn.request("OPTIONS", "/api/trains")
        resp_opt = conn.getresponse()
        self.assertEqual(resp_opt.status, 204)
        self.assertEqual(resp_opt.getheader("Access-Control-Allow-Origin"), "*")
        conn.close()

    def test_unsupported_methods_return_405(self) -> None:
        """TC-SRV-11: Mutating methods (POST, PUT, DELETE) return 405 Method Not Allowed JSON."""
        server = self._start_server()

        for method in ("POST", "PUT", "DELETE"):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
            conn.request(method, "/api/trains", body=b"{}")
            resp = conn.getresponse()
            self.assertEqual(resp.status, 405)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["error"], 405)
            self.assertIn("Method Not Allowed", data["message"])
            conn.close()

    def test_unknown_path_returns_404(self) -> None:
        """TC-SRV-12: Non-existent routes return 404 Not Found JSON."""
        server = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port)

        conn.request("GET", "/api/nonexistent-endpoint")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 404)
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["error"], 404)
        self.assertIn("not found", data["message"].lower())
        conn.close()

    def test_sse_stream_initial_snapshot_and_disconnect(self) -> None:
        """TC-SRV-13 & TC-SRV-14: SSE subscription receives 'init' frame and unregisters cleanly on close."""
        train = _create_sample_train(train_id="ic-50", speed=110.0)
        self.store.update_positions([train])

        server = self._start_server()
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5.0)
        conn.request("GET", "/api/stream", headers={"Accept": "text/event-stream"})
        resp = conn.getresponse()

        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type", ""))
        self.assertEqual(server.broadcaster.client_count, 1)

        # Read the initial "init" event frame
        first_line = resp.readline().decode("utf-8").strip()
        self.assertEqual(first_line, "event: init")

        data_line = resp.readline().decode("utf-8").strip()
        self.assertTrue(data_line.startswith("data: "))
        payload = json.loads(data_line[len("data: ") :])
        self.assertIn("trains", payload)
        self.assertEqual(len(payload["trains"]), 1)
        self.assertEqual(payload["trains"][0]["id"], "ic-50")

        # Broadcast live delta message and verify arrival
        delta_payload = json.dumps([{"id": "ic-50", "speed": 125.0}])
        server.broadcaster.broadcast("trainPositionsUx", delta_payload)

        # Read blank line after init, then next event
        resp.readline()  # separator line
        delta_event = resp.readline().decode("utf-8").strip()
        self.assertEqual(delta_event, "event: trainPositionsUx")

        delta_data = resp.readline().decode("utf-8").strip()
        self.assertTrue(delta_data.startswith("data: "))
        self.assertIn("125.0", delta_data)

        # Close client socket
        conn.close()

        # Trigger cleanup in service_actions and verify client unregistration
        time.sleep(0.1)
        server.service_actions()

    def test_eaddrinuse_diagnostic_handling(self) -> None:
        """TC-SRV-15: Attempting to bind an occupied port raises OSError (EADDRINUSE)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        occupied_port = sock.getsockname()[1]

        try:
            with self.assertRaises(OSError):
                WebMapServer(
                    server_address=("127.0.0.1", occupied_port),
                    store=self.store,
                    broadcaster=self.broadcaster,
                )
        finally:
            sock.close()


if __name__ == "__main__":
    unittest.main()
