"""
Automated unit tests for F-9.2: Empirical Segment Transit Time & ETA Engine.
Validates statistical transit distributions, hybrid kinematic/empirical models,
halted train p90 predictions, and CLI output formatters.
"""

from datetime import datetime, timezone
import json
import sqlite3
import unittest

from storage.cli_formatters import format_segment_transit_table
from storage.db import Database
from storage.models import (
    ETAPrediction,
    ETAPredictionSource,
    SegmentTransitReport,
    SegmentTransitStats,
)
from storage.repository import HistoricalRepository
from storage.schema import SCHEMA_V1_STATEMENTS
from tui.geo import GeoPoint
from tui.mock_engine import StationNode
from tui.renderer import format_next_station_eta


class TestETAEngine(unittest.TestCase):
    """Test suite for empirical segment transit time queries, models, and ETA formatters."""

    def setUp(self):
        self.db = Database(":memory:")
        with self.db.connection() as conn:
            for stmt in SCHEMA_V1_STATEMENTS:
                conn.execute(stmt)
        self.repo = HistoricalRepository(self.db)

    def tearDown(self):
        self.db.close()

    def test_segment_transit_stats_model_and_serialization(self):
        """Test SegmentTransitStats dataclass fields and to_dict() serialization."""
        stats = SegmentTransitStats(
            from_station="Lianokladi",
            to_station="Domokos",
            corridor="PATHE",
            sample_count=25,
            min_duration_seconds=980.5,
            p50_duration_seconds=1185.0,
            p90_duration_seconds=1570.2,
            max_duration_seconds=1890.0,
            avg_speed_kmh=78.4,
            avg_delay_delta_minutes=2.3,
            distance_km=28.5,
        )
        d = stats.to_dict()
        self.assertEqual(d["from_station"], "Lianokladi")
        self.assertEqual(d["to_station"], "Domokos")
        self.assertEqual(d["corridor"], "PATHE")
        self.assertEqual(d["sample_count"], 25)
        self.assertEqual(d["min_duration_seconds"], 980.5)
        self.assertEqual(d["p50_duration_seconds"], 1185.0)
        self.assertEqual(d["p90_duration_seconds"], 1570.2)
        self.assertEqual(d["avg_speed_kmh"], 78.4)
        self.assertEqual(d["distance_km"], 28.5)

    def test_segment_transit_report_serialization(self):
        """Test SegmentTransitReport model and JSON-serializable to_dict()."""
        stats = SegmentTransitStats(
            from_station="Athens",
            to_station="SKA",
            corridor="PATHE",
            sample_count=40,
            min_duration_seconds=435.0,
            p50_duration_seconds=520.0,
            p90_duration_seconds=730.0,
            max_duration_seconds=890.0,
            avg_speed_kmh=68.5,
            avg_delay_delta_minutes=0.8,
        )
        report = SegmentTransitReport(
            corridor="PATHE",
            time_window_start="2026-11-01T00:00:00Z",
            time_window_end="2026-11-20T23:59:59Z",
            total_trips_analyzed=40,
            total_segments_analyzed=1,
            segments=(stats,),
        )
        d = report.to_dict()
        self.assertEqual(d["corridor"], "PATHE")
        self.assertEqual(d["total_trips_analyzed"], 40)
        self.assertEqual(len(d["segments"]), 1)
        self.assertEqual(d["segments"][0]["from_station"], "Athens")

    def test_repository_get_segment_transit_times(self):
        """Test analytical query extracting empirical segment runtimes from recorded telemetry."""
        with self.db.connection() as conn:
            # Seed trains first for foreign key integrity
            for i in range(1, 4):
                conn.execute(
                    """
                    INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at)
                    VALUES (?, 'Intercity', '2026-11-20T00:00:00Z', '2026-11-20T23:59:59Z')
                    """,
                    (f"159{i}",),
                )
                sched_id = f"TRIP_{i}"
                conn.execute(
                    """
                    INSERT INTO scheduled_trips (schedule_id, train_number, corridor, service_date)
                    VALUES (?, ?, 'PATHE', '2026-11-20')
                    """,
                    (sched_id, f"159{i}"),
                )
                conn.execute(
                    """
                    INSERT INTO telemetry_history (
                        train_number, schedule_id, corridor, next_station,
                        recorded_at, lat, lng, speed, course, delay_min
                    ) VALUES
                    (?, ?, 'PATHE', 'Larissa', '2026-11-20T10:00:00Z', 39.638, 22.419, 0.0, 0.0, 0),
                    (?, ?, 'PATHE', 'Katerini', '2026-11-20T10:35:00Z', 40.271, 22.505, 120.0, 0.0, 2);
                    """,
                    (f"159{i}", sched_id, f"159{i}", sched_id),
                )

        report = self.repo.get_segment_transit_times(corridor="PATHE", min_trips=2)
        self.assertGreaterEqual(report.total_trips_analyzed, 1)
        self.assertGreaterEqual(len(report.segments), 1)

        seg = report.segments[0]
        self.assertEqual(seg.from_station, "Larissa")
        self.assertEqual(seg.to_station, "Katerini")
        self.assertEqual(seg.sample_count, 3)
        # 35 minutes = 2100 seconds
        self.assertEqual(seg.p50_duration_seconds, 2100.0)

    def test_format_next_station_eta_empirical_and_halted(self):
        """Test hybrid kinematic/empirical formatting and halted train p90 prediction."""
        station = StationNode(
            id="DOM",
            code="1025",
            name_el="Δομοκός",
            name_en="Domokos",
            point=GeoPoint(lat=39.126, lng=22.302),
            corridor="PATHE",
            is_hub=True,
        )
        # Train ~14.2 km south of Domokos
        train_pt = GeoPoint(lat=39.000, lng=22.302)

        stats = SegmentTransitStats(
            from_station="Lianokladi",
            to_station="Domokos",
            corridor="PATHE",
            sample_count=30,
            min_duration_seconds=900.0,
            p50_duration_seconds=1200.0,  # 20 min
            p90_duration_seconds=1800.0,  # 30 min
            max_duration_seconds=2200.0,
            avg_speed_kmh=85.0,
            avg_delay_delta_minutes=1.5,
            distance_km=28.0,
        )

        # 1. Moving train: should output blended empirical/kinematic ETA
        moving_out = format_next_station_eta(
            "Δομοκός",
            train_point=train_pt,
            station_node=station,
            speed_kmh=90.0,
            segment_stats=stats,
        )
        self.assertIn("Δομοκός", moving_out)
        self.assertIn("km", moving_out)
        self.assertIn("~", moving_out)

        # 2. Halted train en route: should output conservative p90 estimate
        halted_out = format_next_station_eta(
            "Δομοκός",
            train_point=train_pt,
            station_node=station,
            speed_kmh=0.0,
            segment_stats=stats,
        )
        self.assertIn("Δομοκός", halted_out)
        self.assertIn("[p90]", halted_out)

        # 3. Halted at platform (d < 0.25 km): should output AT
        platform_pt = GeoPoint(lat=39.1259, lng=22.3021)
        at_out = format_next_station_eta(
            "Δομοκός",
            train_point=platform_pt,
            station_node=station,
            speed_kmh=0.0,
            segment_stats=stats,
        )
        self.assertIn("AT", at_out)

    def test_format_segment_transit_table(self):
        """Test terminal table formatter with UTF-8 and ASCII mode."""
        stats = SegmentTransitStats(
            from_station="Λιανοκλάδι",
            to_station="Δομοκός",
            corridor="PATHE",
            sample_count=25,
            min_duration_seconds=980.0,
            p50_duration_seconds=1185.0,
            p90_duration_seconds=1570.0,
            max_duration_seconds=1890.0,
            avg_speed_kmh=78.4,
            avg_delay_delta_minutes=2.3,
        )
        report = SegmentTransitReport(
            corridor="PATHE",
            time_window_start="2026-11-01T00:00:00Z",
            time_window_end="2026-11-24T23:59:59Z",
            total_trips_analyzed=25,
            total_segments_analyzed=1,
            segments=(stats,),
        )

        utf8_tbl = format_segment_transit_table(report, use_ascii=False, use_color=False)
        self.assertIn("EMPIRICAL SEGMENT TRANSIT ANALYSIS", utf8_tbl)
        self.assertIn("Λιανοκλάδι", utf8_tbl)
        self.assertIn("Δομοκός", utf8_tbl)
        self.assertIn("19m 45s", utf8_tbl)  # 1185s = 19m 45s

        ascii_tbl = format_segment_transit_table(report, use_ascii=True, use_color=False)
        self.assertNotIn("Λιανοκλάδι", ascii_tbl)
        self.assertIn("Lianokladi", ascii_tbl)
        self.assertTrue(all(ord(c) < 128 for c in ascii_tbl))

    def _seed_file_db_for_cli(self, db_path: str) -> None:
        """Seed a temporary file-backed SQLite database with Greek station segment transitions."""
        file_db = Database(db_path)
        try:
            with file_db.connection() as conn:
                for i in range(1, 4):
                    conn.execute(
                        """
                        INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at)
                        VALUES (?, 'Intercity', '2026-11-20T00:00:00Z', '2026-11-20T23:59:59Z')
                        """,
                        (f"159{i}",),
                    )
                    sched_id = f"CLI_TRIP_{i}"
                    conn.execute(
                        """
                        INSERT INTO scheduled_trips (schedule_id, train_number, corridor, service_date)
                        VALUES (?, ?, 'PATHE', '2026-11-20')
                        """,
                        (sched_id, f"159{i}"),
                    )
                    conn.execute(
                        """
                        INSERT INTO telemetry_history (
                            train_number, schedule_id, corridor, next_station,
                            recorded_at, lat, lng, speed, course, delay_min
                        ) VALUES
                        (?, ?, 'PATHE', 'Λάρισα', '2026-11-20T10:00:00Z', 39.638, 22.419, 0.0, 0.0, 0),
                        (?, ?, 'PATHE', 'Κατερίνη', '2026-11-20T10:35:00Z', 40.271, 22.505, 120.0, 0.0, 2);
                        """,
                        (f"159{i}", sched_id, f"159{i}", sched_id),
                    )
                conn.commit()
        finally:
            file_db.close()

    def test_cli_segments_execution_and_json_output(self):
        """Verify 'railmon history segments' CLI dispatch in UTF-8, ASCII, and JSON modes."""
        from contextlib import redirect_stdout
        import io
        import os
        import tempfile
        from train_tracker import build_parser, run_history_mode

        parser = build_parser()
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "segments_cli.db")
            self._seed_file_db_for_cli(db_path)

            # 1. Default UTF-8 table output
            args_utf8 = parser.parse_args(
                ["history", "--db", db_path, "segments", "--min-trips", "2"]
            )
            buf_utf8 = io.StringIO()
            with redirect_stdout(buf_utf8):
                rc_utf8 = run_history_mode(args_utf8)
            self.assertEqual(rc_utf8, 0)
            out_utf8 = buf_utf8.getvalue()
            self.assertIn("EMPIRICAL SEGMENT TRANSIT ANALYSIS", out_utf8)
            self.assertIn("Λάρισα", out_utf8)
            self.assertIn("Κατερίνη", out_utf8)

            # 2. 7-bit ASCII fallback output
            args_ascii = parser.parse_args(
                ["history", "--db", db_path, "segments", "--min-trips", "2", "--ascii"]
            )
            buf_ascii = io.StringIO()
            with redirect_stdout(buf_ascii):
                rc_ascii = run_history_mode(args_ascii)
            self.assertEqual(rc_ascii, 0)
            out_ascii = buf_ascii.getvalue()
            self.assertIn("EMPIRICAL SEGMENT TRANSIT ANALYSIS", out_ascii)
            self.assertTrue(all(ord(c) < 128 for c in out_ascii))

            # 3. Machine-readable UTF-8 JSON output (ensure_ascii=False)
            args_json = parser.parse_args(
                ["history", "--db", db_path, "segments", "--min-trips", "2", "--json"]
            )
            buf_json = io.StringIO()
            with redirect_stdout(buf_json):
                rc_json = run_history_mode(args_json)
            self.assertEqual(rc_json, 0)
            raw_json = buf_json.getvalue()
            self.assertIn("Λάρισα", raw_json)
            parsed = json.loads(raw_json)
            self.assertEqual(len(parsed["segments"]), 1)
            self.assertEqual(parsed["segments"][0]["from_station"], "Λάρισα")
            self.assertEqual(parsed["segments"][0]["to_station"], "Κατερίνη")


if __name__ == "__main__":
    unittest.main()
