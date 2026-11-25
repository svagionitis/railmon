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

    def test_segment_transit_dense_multi_breadcrumb_run(self):
        """TC-STO-01: Realistic dense breadcrumbs compute true ~35m transit, cruise speed, and delay delta."""
        with self.db.transaction() as cur:
            for i in range(1, 4):
                tno = f"IC5{i}"
                sid = f"DENSE_{i}"
                cur.execute(
                    """
                    INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at)
                    VALUES (?, 'Intercity', '2026-11-20T09:00:00.000000Z', '2026-11-20T12:00:00.000000Z')
                    """,
                    (tno,),
                )
                cur.execute(
                    """
                    INSERT INTO scheduled_trips (schedule_id, train_number, corridor, service_date)
                    VALUES (?, ?, 'PATHE', '2026-11-20')
                    """,
                    (sid, tno),
                )
                cur.executemany(
                    """
                    INSERT INTO telemetry_history (
                        train_number, schedule_id, corridor, next_station,
                        recorded_at, lat, lng, speed, course, delay_min
                    ) VALUES (?, ?, 'PATHE', ?, ?, ?, ?, ?, 0.0, ?)
                    """,
                    [
                        (tno, sid, "Larissa", "2026-11-20T09:59:58Z", 39.638, 22.419, 0.0, 0),
                        (tno, sid, "Larissa", "2026-11-20T10:00:00Z", 39.638, 22.419, 5.0, 0),
                        (tno, sid, "Katerini", "2026-11-20T10:00:02Z", 39.640, 22.420, 15.0, 0),
                        (tno, sid, "Katerini", "2026-11-20T10:15:00Z", 39.900, 22.460, 140.0, 2),
                        (tno, sid, "Katerini", "2026-11-20T10:30:00Z", 40.200, 22.495, 145.0, 4),
                        (tno, sid, "Katerini", "2026-11-20T10:35:00Z", 40.271, 22.505, 0.0, 5),
                        # Trailing 2s departure flip to Platy must be discarded (< 60s)
                        (tno, sid, "Platy", "2026-11-20T10:35:02Z", 40.272, 22.506, 20.0, 5),
                    ],
                )

        report = self.repo.get_segment_transit_times(corridor="PATHE", min_trips=3)
        self.assertEqual(len(report.segments), 1)
        seg = report.segments[0]
        self.assertEqual(seg.from_station, "Larissa")
        self.assertEqual(seg.to_station, "Katerini")
        self.assertEqual(seg.sample_count, 3)
        self.assertEqual(seg.p50_duration_seconds, 2100.0)
        self.assertGreaterEqual(seg.avg_speed_kmh, 95.0)
        self.assertAlmostEqual(seg.avg_delay_delta_minutes, 5.0, places=1)

    def test_segment_transit_null_schedule_multiday_and_loop_isolation(self):
        """TC-STO-02: Null-schedule multi-day & same-day loop runs produce independent samples with 0 cross-day merges."""
        with self.db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at)
                VALUES ('SUB1', 'Suburban', '2026-11-20T07:00:00.000000Z', '2026-11-21T10:00:00.000000Z')
                """
            )
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, corridor, next_station,
                    recorded_at, lat, lng, speed, course, delay_min
                ) VALUES ('SUB1', NULL, 'PRO', ?, ?, 38.0, 23.7, ?, 0.0, ?)
                """,
                [
                    # Day 1 Loop 1: ATH -> SKA = 720s (+2m)
                    ("ATH", "2026-11-20T08:00:00Z", 0.0, 2),
                    ("SKA", "2026-11-20T08:00:02Z", 30.0, 2),
                    ("SKA", "2026-11-20T08:06:00Z", 90.0, 3),
                    ("SKA", "2026-11-20T08:12:00Z", 0.0, 4),
                    # Return leg + Day 1 Loop 2: ATH -> SKA = 900s (-2m)
                    ("ATH", "2026-11-20T08:25:00Z", 0.0, 4),
                    ("SKA", "2026-11-20T08:25:02Z", 40.0, 4),
                    ("SKA", "2026-11-20T08:32:00Z", 100.0, 3),
                    ("SKA", "2026-11-20T08:40:00Z", 0.0, 2),
                    # Day 2 (23h gap): ATH -> SKA = 660s (+1m); must NOT create 23h SKA -> ATH
                    ("ATH", "2026-11-21T08:00:00Z", 0.0, 0),
                    ("SKA", "2026-11-21T08:00:02Z", 35.0, 0),
                    ("SKA", "2026-11-21T08:11:00Z", 95.0, 1),
                ],
            )

        report = self.repo.get_segment_transit_times(corridor="PRO", min_trips=1)
        by_pair = {(s.from_station, s.to_station): s for s in report.segments}
        self.assertIn(("ATH", "SKA"), by_pair)
        ath_ska = by_pair[("ATH", "SKA")]
        self.assertEqual(ath_ska.sample_count, 3)
        self.assertEqual(ath_ska.min_duration_seconds, 660.0)
        self.assertEqual(ath_ska.p50_duration_seconds, 720.0)
        self.assertEqual(ath_ska.max_duration_seconds, 900.0)
        self.assertGreaterEqual(report.total_trips_analyzed, 2)
        # Ensure SKA -> ATH only captured the 13m same-day return, NOT the 23h overnight gap
        self.assertEqual(by_pair[("SKA", "ATH")].sample_count, 1)
        self.assertEqual(by_pair[("SKA", "ATH")].max_duration_seconds, 780.0)

    def test_segment_transit_negative_delay_recovery_and_bounce_discard(self):
        """TC-STO-03: Signed delay recovery is preserved and <60s GPS next_station bounces are discarded."""
        with self.db.transaction() as cur:
            cur.execute(
                """
                INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at)
                VALUES ('IC60', 'Intercity', '2026-11-20T10:00:00.000000Z', '2026-11-20T11:00:00.000000Z')
                """
            )
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, corridor, next_station,
                    recorded_at, lat, lng, speed, course, delay_min
                ) VALUES ('IC60', NULL, 'PATHE', ?, ?, 39.6, 22.4, ?, 0.0, ?)
                """,
                [
                    ("Larissa", "2026-11-20T10:00:00Z", 0.0, 10),
                    # 4-second GPS bounce to Katerini and back -> must NOT emit 4s or fabricated 60s
                    ("Katerini", "2026-11-20T10:00:04Z", 80.0, 10),
                    ("Larissa", "2026-11-20T10:00:08Z", 85.0, 10),
                    # Real transit making up 4 minutes of delay (10 -> 6)
                    ("Katerini", "2026-11-20T10:25:08Z", 140.0, 6),
                ],
            )

        report = self.repo.get_segment_transit_times(corridor="PATHE", min_trips=1)
        self.assertEqual(len(report.segments), 1)
        seg = report.segments[0]
        self.assertEqual(seg.from_station, "Larissa")
        self.assertEqual(seg.to_station, "Katerini")
        self.assertEqual(seg.sample_count, 1)
        self.assertEqual(seg.p50_duration_seconds, 1500.0)
        self.assertAlmostEqual(seg.avg_delay_delta_minutes, -4.0, places=1)

    def test_segment_transit_percentiles_and_full_end_date_inclusion(self):
        """TC-STO-04: Exact median/linear p90 percentiles and inclusive end_date through 23:59:59.999999Z."""
        durations = [600, 900, 1200, 1800]
        with self.db.transaction() as cur:
            for idx, dur in enumerate(durations, start=1):
                tno = f"P{idx}"
                cur.execute(
                    """
                    INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at)
                    VALUES (?, 'Intercity', '2026-11-20T00:00:00.000000Z', '2026-11-20T23:59:59.999999Z')
                    """,
                    (tno,),
                )
                end_min = dur // 60
                start_ts = "2026-11-20T23:29:59.500000Z" if idx == 4 else f"2026-11-20T10:00:00.000000Z"
                end_ts = (
                    "2026-11-20T23:59:59.500000Z"
                    if idx == 4
                    else f"2026-11-20T10:{end_min:02d}:00.000000Z"
                )
                cur.executemany(
                    """
                    INSERT INTO telemetry_history (
                        train_number, schedule_id, corridor, next_station,
                        recorded_at, lat, lng, speed, course, delay_min
                    ) VALUES (?, NULL, 'PATHE', ?, ?, 39.6, 22.4, 110.0, 0.0, 0)
                    """,
                    [
                        (tno, "Athens", start_ts),
                        (tno, "SKA", end_ts),
                    ],
                )

        report = self.repo.get_segment_transit_times(
            corridor="PATHE",
            start_date="2026-11-20",
            end_date="2026-11-20",
            min_trips=4,
        )
        self.assertEqual(len(report.segments), 1)
        seg = report.segments[0]
        self.assertEqual(seg.sample_count, 4)
        self.assertAlmostEqual(seg.p50_duration_seconds, 1050.0, places=1)
        self.assertAlmostEqual(seg.p90_duration_seconds, 1620.0, places=1)


if __name__ == "__main__":
    unittest.main()
