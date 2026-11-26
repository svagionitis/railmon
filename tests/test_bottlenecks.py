"""
Unit and integration tests for F-9.1: Corridor Bottleneck & Station Dwell Delay Heatmaps.
"""

from datetime import datetime, timezone
import json
import os
import tempfile
import unittest

from storage.cli_formatters import format_bottleneck_bar, format_bottlenecks_table
from storage.db import Database
from storage.models import (
    BottleneckNode,
    BottleneckReport,
    BottleneckSeverity,
    ScheduledTripDB,
    TrainRecordDB,
)
from storage.repository import HistoricalRepository
from train_tracker import TrainPosition, build_parser, run_history_mode


class TestBottlenecks(unittest.TestCase):
    """Test suite covering bottleneck domain models, SQL queries, formatter, and CLI."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "test_bottlenecks.db")
        self.db = Database(self.db_path)
        self.repo = HistoricalRepository(self.db)
        self.parser = build_parser()

    def tearDown(self) -> None:
        self.db.close()
        self.temp_dir.cleanup()

    def _seed_telemetry_data(self) -> None:
        """Seed synthetic trips with known delays and dwell times."""
        with self.db.connection() as conn:
            cur = conn.cursor()
            # 1. Register trains
            cur.executemany(
                """
                INSERT INTO trains (
                    train_number, train_type, service_type, first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    ("IC50", "Intercity", "intercity", "2026-11-20T10:00:00Z", "2026-11-20T14:00:00Z"),
                    ("IC52", "Intercity", "intercity", "2026-11-20T12:00:00Z", "2026-11-20T16:00:00Z"),
                    ("IC54", "Intercity", "intercity", "2026-11-20T14:00:00Z", "2026-11-20T18:00:00Z"),
                ],
            )

            # 2. Register trips
            cur.executemany(
                """
                INSERT INTO scheduled_trips (
                    schedule_id, train_number, origin, destination, corridor, service_date
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("SCHED_50", "IC50", "Athens", "Thessaloniki", "PATHE", "2026-11-20"),
                    ("SCHED_52", "IC52", "Athens", "Thessaloniki", "PATHE", "2026-11-20"),
                    ("SCHED_54", "IC54", "Athens", "Thessaloniki", "PATHE", "2026-11-20"),
                ],
            )

        # 3. Add telemetry observations
        # Train IC50, IC52, IC54 at Larissa and Platy
        rows = [
            # IC50 Larissa (300s dwell, delay grows 5 -> 12)
            ("IC50", "SCHED_50", 0.0, 5, "Larissa", 39.638, 22.422, 0.0, "2026-11-20T11:00:00Z", "PATHE"),
            ("IC50", "SCHED_50", 0.0, 12, "Larissa", 39.638, 22.422, 0.0, "2026-11-20T11:05:00Z", "PATHE"),
            # IC52 Larissa (240s dwell, delay grows 8 -> 16)
            ("IC52", "SCHED_52", 0.0, 8, "Larissa", 39.638, 22.422, 0.0, "2026-11-20T13:00:00Z", "PATHE"),
            ("IC52", "SCHED_52", 0.0, 16, "Larissa", 39.638, 22.422, 0.0, "2026-11-20T13:04:00Z", "PATHE"),
            # IC54 Larissa (360s dwell, delay grows 10 -> 22)
            ("IC54", "SCHED_54", 0.0, 10, "Larissa", 39.638, 22.422, 0.0, "2026-11-20T15:00:00Z", "PATHE"),
            ("IC54", "SCHED_54", 0.0, 22, "Larissa", 39.638, 22.422, 0.0, "2026-11-20T15:06:00Z", "PATHE"),

            # IC50 Platy (60s dwell, delay unchanged 12 -> 12)
            ("IC50", "SCHED_50", 0.0, 12, "Platy", 40.542, 22.532, 0.0, "2026-11-20T11:45:00Z", "PATHE"),
            ("IC50", "SCHED_50", 0.0, 12, "Platy", 40.542, 22.532, 0.0, "2026-11-20T11:46:00Z", "PATHE"),
            # IC52 Platy (60s dwell, delay unchanged 16 -> 16)
            ("IC52", "SCHED_52", 0.0, 16, "Platy", 40.542, 22.532, 0.0, "2026-11-20T13:45:00Z", "PATHE"),
            ("IC52", "SCHED_52", 0.0, 16, "Platy", 40.542, 22.532, 0.0, "2026-11-20T13:46:00Z", "PATHE"),
            # IC54 Platy (60s dwell, delay unchanged 22 -> 22)
            ("IC54", "SCHED_54", 0.0, 22, "Platy", 40.542, 22.532, 0.0, "2026-11-20T15:45:00Z", "PATHE"),
            ("IC54", "SCHED_54", 0.0, 22, "Platy", 40.542, 22.532, 0.0, "2026-11-20T15:46:00Z", "PATHE"),
        ]

        with self.db.connection() as conn:
            cur = conn.cursor()
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, speed, delay_min, next_station,
                    lat, lng, course, recorded_at, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()

    def test_bottleneck_node_dto_serialization(self) -> None:
        """Verify BottleneckNode and BottleneckReport DTOs serialize cleanly."""
        node = BottleneckNode(
            rank=1,
            name="Larissa",
            node_type="STATION",
            total_trips=15,
            avg_dwell_seconds=245.5,
            median_delay_minutes=7.4,
            p90_delay_minutes=18.2,
            max_delay_minutes=42.0,
            delay_delta_minutes=6.5,
            bottleneck_score=88.5,
            severity=BottleneckSeverity.CRITICAL,
        )
        d = node.to_dict()
        self.assertEqual(d["rank"], 1)
        self.assertEqual(d["name"], "Larissa")
        self.assertEqual(d["severity"], "CRITICAL")
        self.assertEqual(d["bottleneck_score"], 88.5)

        report = BottleneckReport(
            corridor="PATHE",
            time_window_start="2026-11-01T00:00:00Z",
            time_window_end="2026-11-20T23:59:59Z",
            total_trips_analyzed=15,
            total_observations_analyzed=1200,
            nodes=(node,),
        )
        rd = report.to_dict()
        self.assertEqual(rd["corridor"], "PATHE")
        self.assertEqual(len(rd["bottlenecks"]), 1)
        self.assertEqual(rd["bottlenecks"][0]["rank"], 1)

    def test_get_corridor_bottlenecks_empty_db(self) -> None:
        """Verify empty database returns empty report without crashing."""
        report = self.repo.get_corridor_bottlenecks()
        self.assertEqual(report.total_trips_analyzed, 0)
        self.assertEqual(len(report.nodes), 0)

    def test_get_corridor_bottlenecks_aggregation(self) -> None:
        """Verify analytical aggregation ranks Larissa higher than Platy."""
        self._seed_telemetry_data()
        report = self.repo.get_corridor_bottlenecks(min_trips=3)
        self.assertGreaterEqual(len(report.nodes), 2)

        # Larissa should be ranked #1
        node1 = report.nodes[0]
        self.assertEqual(node1.name, "Larissa")
        self.assertEqual(node1.node_type, "STATION")
        self.assertEqual(node1.total_trips, 3)
        self.assertGreaterEqual(node1.avg_dwell_seconds, 200.0)
        self.assertGreater(node1.delay_delta_minutes, 0.0)
        self.assertGreater(node1.bottleneck_score, 50.0)

        # Platy should be ranked below Larissa
        node2 = report.nodes[1]
        self.assertEqual(node2.name, "Platy")
        self.assertLess(node2.bottleneck_score, node1.bottleneck_score)
        self.assertLess(node2.delay_delta_minutes, 1.0)

    def test_get_corridor_bottlenecks_filtering(self) -> None:
        """Verify corridor and min_trips filtering."""
        self._seed_telemetry_data()

        # Non-matching corridor should return 0 nodes
        report_other = self.repo.get_corridor_bottlenecks(corridor="Peloponnese")
        self.assertEqual(len(report_other.nodes), 0)

        # High min_trips threshold should prune nodes
        report_pruned = self.repo.get_corridor_bottlenecks(min_trips=10)
        self.assertEqual(len(report_pruned.nodes), 0)

    def test_format_bottleneck_bar(self) -> None:
        """Verify heatmap bar formatting in UTF-8 and ASCII modes."""
        bar_utf8 = format_bottleneck_bar(80.0, BottleneckSeverity.HIGH, width=10, use_ascii=False, use_color=False)
        self.assertIn("████████░░", bar_utf8)
        self.assertIn("[HIGH]", bar_utf8)

        bar_ascii = format_bottleneck_bar(50.0, BottleneckSeverity.MED, width=10, use_ascii=True, use_color=False)
        self.assertIn("#####-----", bar_ascii)
        self.assertIn("[MED]", bar_ascii)
        # Verify 7-bit ASCII
        for ch in bar_ascii:
            self.assertLess(ord(ch), 128)

    def test_format_bottlenecks_table(self) -> None:
        """Verify terminal table rendering in UTF-8 and ASCII."""
        self._seed_telemetry_data()
        report = self.repo.get_corridor_bottlenecks(min_trips=3)

        table_utf8 = format_bottlenecks_table(report, use_ascii=False, use_color=False)
        self.assertIn("CORRIDOR BOTTLENECK ANALYSIS", table_utf8)
        self.assertIn("Larissa", table_utf8)
        self.assertIn("Platy", table_utf8)

        table_ascii = format_bottlenecks_table(report, use_ascii=True, use_color=False)
        self.assertIn("CORRIDOR BOTTLENECK ANALYSIS", table_ascii)
        for ch in table_ascii:
            self.assertLess(ord(ch), 128)

    def test_cli_bottlenecks_execution(self) -> None:
        """Verify 'railmon history bottlenecks' CLI command dispatch (UTF-8 and ASCII)."""
        import io
        from contextlib import redirect_stdout

        self._seed_telemetry_data()

        for extra_flags in ([], ["--ascii"]):
            args = self.parser.parse_args(
                ["history", "--db", self.db_path, "bottlenecks", *extra_flags]
            )
            buf = io.StringIO()
            with redirect_stdout(buf):
                ret = run_history_mode(args)
            self.assertEqual(ret, 0)
            out = buf.getvalue()
            self.assertIn("CORRIDOR BOTTLENECK ANALYSIS", out)
            self.assertIn("Larissa", out)
            if "--ascii" in extra_flags:
                self.assertTrue(all(ord(ch) < 128 for ch in out))

    def test_cli_bottlenecks_json_output(self) -> None:
        """Verify 'railmon history bottlenecks --json' outputs valid JSON."""
        self._seed_telemetry_data()

        args = self.parser.parse_args(["history", "--db", self.db_path, "bottlenecks", "--json"])
        import io
        from contextlib import redirect_stdout

        buf = io.StringIO()
        with redirect_stdout(buf):
            ret = run_history_mode(args)

        self.assertEqual(ret, 0)
        parsed = json.loads(buf.getvalue())
        self.assertIn("bottlenecks", parsed)
        self.assertGreaterEqual(len(parsed["bottlenecks"]), 2)

    def test_bottlenecks_percentiles_use_dwell_events_population_linear_p90(self) -> None:
        """TC-STO-22: F-9.1 median/p90 come strictly from dwell_events (min <= median <= p90 <= max, linear p90)."""
        with self.db.transaction() as cur:
            cur.execute(
                "INSERT OR IGNORE INTO trains (train_number, first_observed_at, last_observed_at) "
                "VALUES ('P90-T', '2026-11-26T08:00:00.000000Z', '2026-11-26T12:00:00.000000Z');"
            )
            # Insert 4 distinct dwell events at StationX with departure delays [2, 4, 6, 20]
            # plus 50 raw moving breadcrumbs with huge delay=99 that must NOT pollute median/p90!
            rows = []
            for idx, dep_dly in enumerate([2, 4, 6, 20]):
                hour = 8 + idx
                rows.append(("P90-T", f"2026-11-26T{hour:02d}:00:00.000000Z", 39.6, 22.4, 0.0, 0.0, dep_dly, "StationX", "ATH-SKG"))
                rows.append(("P90-T", f"2026-11-26T{hour:02d}:01:00.000000Z", 39.6, 22.4, 0.0, 0.0, dep_dly, "StationX", "ATH-SKG"))
                rows.append(("P90-T", f"2026-11-26T{hour:02d}:02:00.000000Z", 39.6, 22.4, 120.0, 0.0, 99, "StationX", "ATH-SKG"))
            for sec in range(50):
                rows.append(("P90-T", f"2026-11-26T11:30:{sec:02d}.000000Z", 39.6, 22.4, 120.0, 0.0, 99, "StationX", "ATH-SKG"))
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, recorded_at, lat, lng, speed, course, delay_min, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                rows,
            )

        report = self.repo.get_corridor_bottlenecks(corridor="ATH-SKG", min_trips=2)
        node = next(n for n in report.nodes if n.name == "StationX")
        # Dwell departure delays are [2, 4, 6, 20]: median=5.0, linear p90 = 6 + 0.7*(20-6) = 15.8, max=20.0
        self.assertEqual(node.max_delay_minutes, 20.0)
        self.assertAlmostEqual(node.median_delay_minutes, 5.0, places=2)
        self.assertAlmostEqual(node.p90_delay_minutes, 15.8, places=2)
        self.assertLessEqual(node.median_delay_minutes, node.p90_delay_minutes)
        self.assertLessEqual(node.p90_delay_minutes, node.max_delay_minutes)


if __name__ == "__main__":
    unittest.main()
