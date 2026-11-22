"""Unit tests for railmon conflicts CLI subcommand and formatters (F-5.1)."""

import argparse
import io
import json
import sys
import time
import unittest
from typing import List

from storage.cli_formatters import format_conflict_json, format_conflict_table
from train_tracker import run_conflicts_mode
from tui.geo import (
    ConflictAlert,
    ConflictSeverity,
    ConflictType,
)


def _make_sample_alert(
    alert_id: str = "T1_T2_OPPOSING",
    sev: ConflictSeverity = ConflictSeverity.CRITICAL,
    ctype: ConflictType = ConflictType.OPPOSING_COLLISION,
    corridor: str = "proastiakos-chalkida",
) -> ConflictAlert:
    return ConflictAlert(
        alert_id=alert_id,
        train_id_1="T1",
        train_number_1="1597",
        train_id_2="T2",
        train_number_2="1598",
        corridor_id=corridor,
        corridor_name="Chalkida Branch",
        conflict_type=ctype,
        severity=sev,
        distance_km=2.1,
        closing_speed_kmh=180.0,
        time_to_collision_sec=42.0,
        is_single_track=True,
        station_proximity_name=None,
        description="Critical Opposing Collision Vector",
        timestamp=time.time(),
    )


class TestCliConflicts(unittest.TestCase):
    """Test suite for CLI formatters and command execution of railmon conflicts."""

    def test_format_conflict_table_empty(self) -> None:
        """Verify empty table message when zero conflicts exist."""
        table_utf8 = format_conflict_table([], ascii_mode=False)
        self.assertIn("Zero active train conflicts", table_utf8)
        self.assertIn("═", table_utf8)

        table_ascii = format_conflict_table([], ascii_mode=True)
        self.assertIn("Zero active train conflicts", table_ascii)
        self.assertIn("=", table_ascii)

    def test_format_conflict_table_populated(self) -> None:
        """Verify aligned columns and metrics when conflicts exist."""
        alert = _make_sample_alert()
        table = format_conflict_table([alert], ascii_mode=False)

        self.assertIn("CRITICAL", table)
        self.assertIn("OPPOSING_COLLISIO", table)
        self.assertIn("1597", table)
        self.assertIn("1598", table)
        self.assertIn("2.1km", table)
        self.assertIn("180km/h", table)
        self.assertIn("42s", table)
        self.assertIn("SINGLE", table)
        self.assertIn("Active Conflicts: 1 (Critical: 1", table)

    def test_format_conflict_json(self) -> None:
        """Verify valid JSON schema and numeric field serialization."""
        alert = _make_sample_alert()
        out = format_conflict_json([alert])
        data = json.loads(out)

        self.assertEqual(data["count"], 1)
        self.assertEqual(len(data["active_conflicts"]), 1)
        item = data["active_conflicts"][0]
        self.assertEqual(item["alert_id"], "T1_T2_OPPOSING")
        self.assertEqual(item["severity"], "CRITICAL")
        self.assertEqual(item["distance_km"], 2.1)
        self.assertEqual(item["time_to_collision_sec"], 42.0)
        self.assertTrue(item["is_single_track"])

    def test_run_conflicts_mode_execution(self) -> None:
        """Verify run_conflicts_mode CLI handler runs and exits with code 0."""
        args = argparse.Namespace(
            mode="conflicts",
            corridor=None,
            critical_only=False,
            json=False,
            ascii=False,
        )

        captured_stdout = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured_stdout
            ret = run_conflicts_mode(args)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(ret, 0)
        output = captured_stdout.getvalue()
        self.assertIn("HELLENIC RAILWAYS - FLEET HEADWAY & CONFLICT AUDIT", output)

    def test_run_conflicts_mode_json(self) -> None:
        """Verify run_conflicts_mode outputs valid JSON when requested."""
        args = argparse.Namespace(
            mode="conflicts",
            corridor=None,
            critical_only=False,
            json=True,
            ascii=False,
        )

        captured_stdout = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = captured_stdout
            ret = run_conflicts_mode(args)
        finally:
            sys.stdout = old_stdout

        self.assertEqual(ret, 0)
        output = captured_stdout.getvalue()
        parsed = json.loads(output)
        self.assertIn("active_conflicts", parsed)
        self.assertIn("count", parsed)


if __name__ == "__main__":
    unittest.main()
