"""Unit tests for headless CLI speed-limits and slow-orders commands (F-5.3)."""

import io
import json
import sys
import unittest
from contextlib import redirect_stdout

from train_tracker import build_parser, main


class TestCLISpeedLimits(unittest.TestCase):
    """Test suite for 'railmon speed-limits' and 'railmon slow-orders' CLI subcommands."""

    def test_speed_limits_table_output(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["speed-limits"])
        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("HELLENIC RAILWAYS - CORRIDOR SPEED LIMITS & RESTRICTIONS", output)
        self.assertIn("pathe-mainline", output)
        self.assertIn("160.0 km/h", output)
        self.assertIn("Active Corridors:", output)

    def test_speed_limits_corridor_filter(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["speed-limits", "--corridor", "pathe"])
        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("pathe-mainline", output)
        self.assertNotIn("thessaly-kalambaka", output)

    def test_speed_limits_json_output(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["speed-limits", "--json"])
        self.assertEqual(ret, 0)
        output = buf.getvalue().strip()
        data = json.loads(output)
        self.assertIn("corridors", data)
        self.assertIn("slow_orders", data)
        self.assertTrue(len(data["corridors"]) >= 5)
        self.assertTrue(len(data["slow_orders"]) >= 2)

    def test_slow_orders_table_output(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["slow-orders"])
        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("ACTIVE TEMPORARY SPEED RESTRICTIONS", output)
        self.assertIn("TSR-PATHE-SPERCHEIOS", output)
        self.assertIn("40.0 km/h", output)
        self.assertIn("Total Active Slow Orders:", output)

    def test_slow_orders_json_output(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["slow-orders", "--json"])
        self.assertEqual(ret, 0)
        output = buf.getvalue().strip()
        data = json.loads(output)
        self.assertIsInstance(data, list)
        self.assertTrue(len(data) >= 3)
        self.assertEqual(data[0]["id"], "TSR-PATHE-SPERCHEIOS")

    def test_slow_orders_corridor_filter(self) -> None:
        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = main(["slow-orders", "--corridor", "proastiakos"])
        self.assertEqual(ret, 0)
        output = buf.getvalue()
        self.assertIn("TSR-PROAST-CORINTH-CA", output)
        self.assertNotIn("TSR-PATHE-SPERCHEIOS", output)


if __name__ == "__main__":
    unittest.main()
