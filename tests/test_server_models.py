"""Unit tests for server domain models, configuration, and types."""

import unittest
from queue import Queue

from server.models import (
    ClientConnection,
    ServerConfig,
    ServerHealthMetrics,
    SSEMessage,
    TileProvider,
)


class TestServerModels(unittest.TestCase):
    """Test suite validating server models and configuration contracts."""

    def test_tile_provider_from_string(self) -> None:
        """TC-MOD-01: TileProvider parses strings case-insensitively with default fallback."""
        self.assertEqual(TileProvider.from_string("carto-dark"), TileProvider.CARTO_DARK)
        self.assertEqual(TileProvider.from_string("CARTO-LIGHT"), TileProvider.CARTO_LIGHT)
        self.assertEqual(TileProvider.from_string("  osm  "), TileProvider.OSM)
        self.assertEqual(TileProvider.from_string("invalid_provider"), TileProvider.CARTO_DARK)
        self.assertEqual(TileProvider.from_string(""), TileProvider.CARTO_DARK)

    def test_tile_provider_urls_and_attributions(self) -> None:
        """TC-MOD-02: TileProvider provides valid tile URLs and copyright attributions."""
        for provider in TileProvider:
            self.assertTrue(provider.tile_url.startswith("https://"))
            self.assertIn("{z}/{x}/{y}", provider.tile_url)
            self.assertTrue(len(provider.attribution) > 0)

    def test_server_config_defaults(self) -> None:
        """TC-MOD-03: ServerConfig defaults to secure localhost settings."""
        cfg = ServerConfig()
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 8080)
        self.assertFalse(cfg.cors_enabled)
        self.assertEqual(cfg.tile_provider, TileProvider.CARTO_DARK)
        self.assertFalse(cfg.demo_mode)
        self.assertFalse(cfg.record_enabled)
        self.assertIsNone(cfg.db_path)
        self.assertIsNone(cfg.replay_db)
        self.assertEqual(cfg.replay_speed, 1.0)
        self.assertFalse(cfg.open_browser)

        d = cfg.to_dict()
        self.assertEqual(d["host"], "127.0.0.1")
        self.assertEqual(d["port"], 8080)
        self.assertEqual(d["tile_provider"], "carto-dark")

    def test_server_config_validation(self) -> None:
        """TC-MOD-04: ServerConfig enforces port bounds and positive replay speed."""
        with self.assertRaises(ValueError):
            ServerConfig(port=0)

        with self.assertRaises(ValueError):
            ServerConfig(port=65536)

        with self.assertRaises(ValueError):
            ServerConfig(replay_speed=0.0)

        with self.assertRaises(ValueError):
            ServerConfig(replay_speed=-1.5)

    def test_client_connection_lifecycle(self) -> None:
        """TC-MOD-05: ClientConnection initializes unique ID, queue, and tracks activity."""
        conn1 = ClientConnection(remote_addr="192.168.1.50")
        conn2 = ClientConnection(remote_addr="127.0.0.1")
        self.assertNotEqual(conn1.client_id, conn2.client_id)
        self.assertEqual(conn1.remote_addr, "192.168.1.50")
        self.assertIsInstance(conn1.queue, Queue)
        self.assertEqual(conn1.frames_sent, 0)
        self.assertEqual(conn1.frames_dropped, 0)

        old_time = conn1.last_event_at
        conn1.touch()
        self.assertGreaterEqual(conn1.last_event_at, old_time)

    def test_sse_message_wire_format(self) -> None:
        """TC-MOD-06: SSEMessage formats RFC-compliant text/event-stream wire strings."""
        # Simple event
        msg1 = SSEMessage(event="trainPositionsUx", data='[{"id":"1599"}]')
        wire1 = msg1.to_wire_format()
        self.assertEqual(
            wire1,
            'event: trainPositionsUx\ndata: [{"id":"1599"}]\n\n',
        )

        # Multi-line data with ID and retry
        msg2 = SSEMessage(
            event="customAlert",
            data="line1\nline2",
            msg_id="evt-42",
            retry_ms=5000,
        )
        wire2 = msg2.to_wire_format()
        expected2 = "id: evt-42\nevent: customAlert\nretry: 5000\ndata: line1\ndata: line2\n\n"
        self.assertEqual(wire2, expected2)

    def test_server_health_metrics_export(self) -> None:
        """TC-MOD-07: ServerHealthMetrics exports clean dictionary."""
        metrics = ServerHealthMetrics(
            uptime_seconds=125.456,
            active_clients=3,
            total_clients_served=15,
            total_broadcasts=250,
            total_frames_sent=750,
            total_frames_dropped=5,
        )
        d = metrics.to_dict()
        self.assertEqual(d["uptime_seconds"], 125.46)
        self.assertEqual(d["active_clients"], 3)
        self.assertEqual(d["total_clients_served"], 15)
        self.assertEqual(d["total_broadcasts"], 250)
        self.assertEqual(d["total_frames_sent"], 750)
        self.assertEqual(d["total_frames_dropped"], 5)


if __name__ == "__main__":
    unittest.main()
