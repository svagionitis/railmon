"""Unit tests for railmon sensor-health CLI command (F-5.2)."""

import argparse
import io
import json
import sys
import unittest
from unittest.mock import patch

from train_tracker import build_parser, run_sensor_health_mode


class TestCLISensorHealth(unittest.TestCase):
    """Test suite for headless sensor health query CLI interface."""

    def test_cli_parser_sensor_health_command(self) -> None:
        """Verify build_parser registers sensor-health and aliases."""
        parser = build_parser()
        args = parser.parse_args(["sensor-health", "--alerts-only", "-c", "PATHE", "--json"])
        self.assertEqual(args.mode, "sensor-health")
        self.assertTrue(args.alerts_only)
        self.assertEqual(args.corridor, "PATHE")
        self.assertTrue(args.json)

        # Alias: health
        args_alias = parser.parse_args(["health"])
        self.assertEqual(args_alias.mode, "health")

    def test_run_sensor_health_tabular_output(self) -> None:
        """Verify run_sensor_health_mode generates an aligned human-readable table."""
        args = argparse.Namespace(
            mode="sensor-health",
            alerts_only=False,
            corridor=None,
            json=False,
            ascii=False,
            demo=True,
        )
        stdout_buf = io.StringIO()
        with patch.object(sys, "stdout", stdout_buf):
            ret = run_sensor_health_mode(args)

        self.assertEqual(ret, 0)
        output = stdout_buf.getvalue()
        self.assertIn("HELLENIC RAILWAYS - FLEET TELEMETRY & SENSOR HEALTH", output)
        self.assertIn("TRAIN", output)
        self.assertIn("SPEED", output)
        self.assertIn("STATUS", output)
        self.assertIn("Total Trains:", output)

    def test_run_sensor_health_json_output(self) -> None:
        """Verify run_sensor_health_mode generates valid structured JSON."""
        args = argparse.Namespace(
            mode="sensor-health",
            alerts_only=False,
            corridor="PATHE",
            json=True,
            ascii=False,
            demo=True,
        )
        stdout_buf = io.StringIO()
        with patch.object(sys, "stdout", stdout_buf):
            ret = run_sensor_health_mode(args)

        self.assertEqual(ret, 0)
        output = stdout_buf.getvalue()
        data = json.loads(output)
        self.assertIn("timestamp", data)
        self.assertIn("trains", data)
        self.assertIsInstance(data["trains"], list)
        self.assertGreater(len(data["trains"]), 0)
        first_train = data["trains"][0]
        self.assertIn("train_number", first_train)
        self.assertIn("status_code", first_train)
        self.assertIn("health_state", first_train)


if __name__ == "__main__":
    unittest.main()
