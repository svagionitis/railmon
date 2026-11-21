"""Unit tests for F-7.2: CLI Infrastructure GIS inspection (railmon infrastructure / geo).

Validates CLI subparser flags, filtering by asset type and corridor, tabular formatting,
GeoJSON FeatureCollection emission, and structured JSON summaries.
"""

import io
import json
import unittest

from train_tracker import build_parser, run_infrastructure_mode


class TestCLIInfrastructure(unittest.TestCase):
    """Test suite for CLI infrastructure GIS inspection subcommand."""

    def test_subparser_arguments(self) -> None:
        """Verify subparser flags: --type, --corridor, --json, --geojson, --ascii."""
        parser = build_parser()
        args = parser.parse_args(["infrastructure", "--type", "tunnel", "--corridor", "pathe", "--json"])
        self.assertEqual(args.mode, "infrastructure")
        self.assertEqual(args.feature_type, "tunnel")
        self.assertEqual(args.corridor, "pathe")
        self.assertTrue(args.json)

    def test_subparser_aliases(self) -> None:
        """Verify aliases 'infra' and 'geo' route to the same command."""
        parser = build_parser()
        args1 = parser.parse_args(["infra", "--geojson"])
        self.assertEqual(args1.mode, "infra")
        self.assertTrue(args1.geojson)

        args2 = parser.parse_args(["geo", "--type", "bridge"])
        self.assertEqual(args2.mode, "geo")
        self.assertEqual(args2.feature_type, "bridge")

    def test_cli_table_output(self) -> None:
        """Verify default tabular execution produces formatted text table with exit code 0."""
        parser = build_parser()
        args = parser.parse_args(["infrastructure"])

        buf = io.StringIO()
        import sys
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = run_infrastructure_mode(args)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("HELLENIC RAILWAYS - INFRASTRUCTURE GIS ASSETS", output)
        self.assertIn("Kallidromo Tunnel", output)
        self.assertIn("TUNNEL", output)
        self.assertIn("BRIDGE", output)
        self.assertIn("SIGNAL", output)

    def test_cli_type_filter_tunnels_only(self) -> None:
        """Verify --type tunnel filters out bridges and signals."""
        parser = build_parser()
        args = parser.parse_args(["infrastructure", "--type", "tunnel"])

        buf = io.StringIO()
        import sys
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = run_infrastructure_mode(args)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("Kallidromo Tunnel", output)
        self.assertIn("Mount Othrys Tunnel", output)
        self.assertNotIn("Gorgopotamos Viaduct", output)
        self.assertNotIn("SKAU Central Interlocking", output)

    def test_cli_json_output(self) -> None:
        """Verify --json outputs valid JSON summary list."""
        parser = build_parser()
        args = parser.parse_args(["infrastructure", "--type", "bridge", "--json"])

        buf = io.StringIO()
        import sys
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = run_infrastructure_mode(args)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(ret, 0)
        raw_json = buf.getvalue()
        data = json.loads(raw_json)
        self.assertIsInstance(data, list)
        self.assertGreater(len(data), 0)
        self.assertTrue(all(item["type"] == "bridge" for item in data))

    def test_cli_geojson_output(self) -> None:
        """Verify --geojson outputs standard GeoJSON FeatureCollection."""
        parser = build_parser()
        args = parser.parse_args(["infrastructure", "--geojson"])

        buf = io.StringIO()
        import sys
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ret = run_infrastructure_mode(args)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(ret, 0)
        raw_json = buf.getvalue()
        data = json.loads(raw_json)
        self.assertEqual(data.get("type"), "FeatureCollection")
        self.assertIsInstance(data.get("features"), list)
        self.assertGreater(len(data["features"]), 10)


if __name__ == "__main__":
    unittest.main()
