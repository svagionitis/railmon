"""Unit tests for embedded web assets and GeoJSON caching subsystem."""

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


if __name__ == "__main__":
    unittest.main()
