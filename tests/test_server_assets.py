"""Unit tests for embedded web assets and GeoJSON caching subsystem."""

import ast
import json
import tempfile
import unittest
from pathlib import Path

from server.assets import GeoAssetCache, get_geojson_asset, get_spa_html
from server.models import ServerConfig, TileProvider


class TestServerAssets(unittest.TestCase):
    """Test suite validating GeoAssetCache and HTML template generation."""

    def test_geo_asset_cache_loads_tracks(self) -> None:
        """TC-AST-01: GeoAssetCache loads tracks.json into valid GeoJSON FeatureCollection."""
        cache = GeoAssetCache()
        raw_json = cache.get_tracks_geojson()
        self.assertIsInstance(raw_json, str)

        data = json.loads(raw_json)
        self.assertEqual(data.get("type"), "FeatureCollection")
        self.assertIn("features", data)
        self.assertGreater(len(data["features"]), 0)

    def test_geo_asset_cache_loads_stations(self) -> None:
        """TC-AST-02: GeoAssetCache loads stations.json into valid GeoJSON FeatureCollection."""
        cache = GeoAssetCache()
        raw_json = cache.get_stations_geojson()
        data = json.loads(raw_json)
        self.assertEqual(data.get("type"), "FeatureCollection")
        self.assertIn("features", data)
        self.assertGreater(len(data["features"]), 0)

    def test_geo_asset_cache_loads_infra_and_speed_limits(self) -> None:
        """TC-AST-03: GeoAssetCache loads infrastructure and speed limits datasets."""
        cache = GeoAssetCache()
        infra_data = json.loads(cache.get_infrastructure_geojson())
        self.assertEqual(infra_data.get("type"), "FeatureCollection")

        speeds_data = json.loads(cache.get_speed_limits_json())
        self.assertTrue(isinstance(speeds_data, dict) or isinstance(speeds_data, list))

    def test_geo_asset_cache_missing_file_fallback(self) -> None:
        """TC-AST-04: GeoAssetCache safely returns empty FeatureCollection on missing file."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            empty_cache = GeoAssetCache(data_dir=Path(tmp_dir))
            fallback_json = empty_cache.get_asset_json_string("non_existent.json")
            data = json.loads(fallback_json)
            self.assertEqual(data, {"type": "FeatureCollection", "features": []})

    def test_get_geojson_asset_convenience_function(self) -> None:
        """TC-AST-05: get_geojson_asset resolves standard API endpoints to dataset files."""
        for endpoint in ("/api/tracks", "/api/stations", "/api/infrastructure", "/api/speed-limits"):
            raw_text = get_geojson_asset(endpoint)
            self.assertTrue(len(raw_text) > 0)
            data = json.loads(raw_text)
            self.assertIsNotNone(data)

    def test_get_spa_html_template_generation(self) -> None:
        """TC-AST-06: get_spa_html generates complete HTML with configured tile providers."""
        # Default Dark Matter
        html_dark = get_spa_html()
        self.assertIn("<!DOCTYPE html>", html_dark)
        self.assertIn("<title>RAILMON", html_dark)
        self.assertIn("leaflet.js", html_dark)
        self.assertIn("dark_all", html_dark)

        # Light Positron
        cfg_light = ServerConfig(tile_provider=TileProvider.CARTO_LIGHT)
        html_light = get_spa_html(cfg_light)
        self.assertIn("light_all", html_light)

        # OpenStreetMap
        cfg_osm = ServerConfig(tile_provider=TileProvider.OSM)
        html_osm = get_spa_html(cfg_osm)
        self.assertIn("tile.openstreetmap.org", html_osm)

    def test_html_shell_structure_tc_ast_07(self) -> None:
        """TC-AST-07: HTML shell structure contains doctype, viewport, Leaflet CDN links."""
        html = get_spa_html()
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn('<html lang="en">', html)
        self.assertIn('<meta charset="utf-8">', html)
        self.assertIn('name="viewport"', html)
        self.assertIn("unpkg.com/leaflet@1.9.4/dist/leaflet.css", html)
        self.assertIn("unpkg.com/leaflet@1.9.4/dist/leaflet.js", html)
        self.assertIn("<header>", html)
        self.assertIn("<footer>", html)
        self.assertIn('id="map"', html)
        self.assertIn('id="sidebar"', html)

    def test_tile_provider_injection_tc_ast_08(self) -> None:
        """TC-AST-08: Tile provider URL and legal attribution are cleanly injected."""
        cfg_dark = ServerConfig(tile_provider=TileProvider.CARTO_DARK)
        html_dark = get_spa_html(cfg_dark)
        self.assertIn("dark_all/{z}/{x}/{y}{r}.png", html_dark)
        self.assertIn("CARTO", html_dark)

        cfg_osm = ServerConfig(tile_provider=TileProvider.OSM)
        html_osm = get_spa_html(cfg_osm)
        self.assertIn("tile.openstreetmap.org/{z}/{x}/{y}.png", html_osm)
        self.assertIn("OpenStreetMap", html_osm)

    def test_svg_locomotive_marker_tc_ast_09(self) -> None:
        """TC-AST-09: Heading-oriented SVG locomotive glyph and CSS classes are defined."""
        html = get_spa_html()
        self.assertIn("custom-train-marker", html)
        self.assertIn("loco-glyph-wrap", html)
        self.assertIn("loco-svg", html)
        self.assertIn("loco-body", html)
        # Verify directional heading chevron path
        self.assertIn("M12 2L4 19L12 15L20 19L12 2Z", html)
        self.assertIn("transform: rotate", html)
        self.assertIn("createTrainIcon", html)

    def test_css_design_tokens_tc_ast_10(self) -> None:
        """TC-AST-10: Dark theme CSS custom properties and color tokens are defined."""
        html = get_spa_html()
        self.assertIn("--bg-dark: #0a0e17;", html)
        self.assertIn("--azure-ic: #0ea5e9;", html)
        self.assertIn("--emerald-pro: #10b981;", html)
        self.assertIn("--amber-reg: #f59e0b;", html)
        self.assertIn("--crimson-delay: #ef4444;", html)
        self.assertIn("--line-pathe:", html)

    def test_sse_stream_listener_tc_ast_11(self) -> None:
        """TC-AST-11: EventSource subscribes to /api/stream and registers event listeners."""
        html = get_spa_html()
        self.assertIn("new EventSource('/api/stream')", html)
        self.assertIn("addEventListener('init'", html)
        self.assertIn("addEventListener('trainPositionsUx'", html)
        self.assertIn("addEventListener('conflictAlert'", html)
        self.assertIn("reconnectDelay", html)

    def test_geojson_fetch_endpoints_tc_ast_12(self) -> None:
        """TC-AST-12: Client script fetches /api/tracks, /api/stations, and /api/infrastructure."""
        html = get_spa_html()
        self.assertIn("fetch('/api/tracks')", html)
        self.assertIn("fetch('/api/stations')", html)
        self.assertIn("fetch('/api/infrastructure')", html)
        self.assertIn("fetch('/api/health')", html)

    def test_sparkline_generator_tc_ast_13(self) -> None:
        """TC-AST-13: Speed sparkline SVG generator logic is embedded."""
        html = get_spa_html()
        self.assertIn("generateSparkline", html)
        self.assertIn("sparkline-svg", html)
        self.assertIn("<polyline", html)
        self.assertIn('stroke="#0ea5e9"', html)

    def test_delay_styling_classes_tc_ast_14(self) -> None:
        """TC-AST-14: Delay threshold badges and animated CSS pulse classes are defined."""
        html = get_spa_html()
        self.assertIn("delay-ontime", html)
        self.assertIn("delay-minor", html)
        self.assertIn("delay-severe", html)
        self.assertIn("marker-delayed", html)
        self.assertIn("@keyframes locoDelayPulse", html)

    def test_offline_fallback_mechanism_tc_ast_15(self) -> None:
        """TC-AST-15: Map detects tile loading failure and enables offline grid styling."""
        html = get_spa_html()
        self.assertIn("offline-grid", html)
        self.assertIn("tileerror", html)
        self.assertIn("offlineFallbackActive", html)

    def test_roster_and_hud_containers_tc_ast_16(self) -> None:
        """TC-AST-16: Operator HUD elements, roster list, search box, and inspector card exist."""
        html = get_spa_html()
        self.assertIn('id="rosterList"', html)
        self.assertIn('id="searchInput"', html)
        self.assertIn('id="activeCount"', html)
        self.assertIn('id="delayCount"', html)
        self.assertIn('id="inspectorCard"', html)
        self.assertIn('id="connPill"', html)

    def test_conflict_banner_container_tc_ast_17(self) -> None:
        """TC-AST-17: Watchdog conflict banner with pulse animation and dismiss button exists."""
        html = get_spa_html()
        self.assertIn('id="conflictBanner"', html)
        self.assertIn('id="conflictText"', html)
        self.assertIn("dismissConflictBanner", html)
        self.assertIn("@keyframes alertPulse", html)
        self.assertIn("WATCHDOG CONFLICT ALERT", html)

    def test_zero_pypi_pure_stdlib_generation_tc_ast_18(self) -> None:
        """TC-AST-18: Verify server/assets.py imports exclusively from Python stdlib and internal modules."""
        target_file = Path(__file__).resolve().parent.parent / "server" / "assets.py"
        tree = ast.parse(target_file.read_text(encoding="utf-8"))

        imported_modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_modules.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imported_modules.add(node.module.split(".")[0])

        allowed_stdlib = {"json", "logging", "pathlib", "threading", "typing"}
        allowed_internal = {"server"}
        allowed = allowed_stdlib | allowed_internal

        for mod in imported_modules:
            self.assertIn(
                mod,
                allowed,
                f"Prohibited external dependency '{mod}' found in server/assets.py",
            )

    def test_geo_asset_cache_corrupt_and_zero_byte_fault_injection(self) -> None:
        """TC-AST-19 / TC-EDGE-10: Corrupt and 0-byte JSON files return safe empty FeatureCollection."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            # Create a 0-byte file
            zero_byte_file = tmp_path / "zero_byte.json"
            zero_byte_file.write_text("", encoding="utf-8")

            # Create a corrupted malformed JSON file
            corrupt_file = tmp_path / "corrupt.json"
            corrupt_file.write_text("{malformed: json, [unclosed", encoding="utf-8")

            cache = GeoAssetCache(data_dir=tmp_path)

            res_zero = json.loads(cache.get_asset_json_string("zero_byte.json"))
            self.assertEqual(res_zero, {"type": "FeatureCollection", "features": []})

            res_corrupt = json.loads(cache.get_asset_json_string("corrupt.json"))
            self.assertEqual(res_corrupt, {"type": "FeatureCollection", "features": []})


if __name__ == "__main__":
    unittest.main()
