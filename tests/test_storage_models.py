"""
Unit tests for storage domain models and dataclass transformations.
"""

import unittest
import warnings
from datetime import datetime, timezone

from storage.models import (
    RecordingSessionDB,
    ScheduleStopDB,
    ScheduledTripDB,
    TelemetryHistoryRow,
    TrainRecordDB,
    iso_utc_now,
)
from train_tracker import TrainPosition


class TestStorageModels(unittest.TestCase):
    """Test suite for persistence dataclasses and tuple serialization."""

    def test_train_record_db_serialization(self):
        train = TrainRecordDB(
            train_number="1597",
            locomotive_number="120010",
            default_name="Intercity 1597",
            train_type="passenger",
            service_type="Intercity",
            first_observed_at="2026-11-18T10:00:00Z",
            last_observed_at="2026-11-18T14:30:00Z",
        )
        row = train.to_db_row()
        self.assertEqual(len(row), 7)
        self.assertEqual(row[0], "1597")
        self.assertEqual(row[1], "120010")
        self.assertEqual(row[2], "Intercity 1597")
        self.assertEqual(row[3], "passenger")
        self.assertEqual(row[4], "Intercity")
        self.assertEqual(row[5], "2026-11-18T10:00:00Z")
        self.assertEqual(row[6], "2026-11-18T14:30:00Z")

    def test_scheduled_trip_db_serialization(self):
        trip = ScheduledTripDB(
            schedule_id="sched-101",
            train_number="1597",
            origin="Αθήνα",
            destination="Θεσσαλονίκη",
            corridor="PATHE",
            service_date="2026-11-18",
            scheduled_departure="2026-11-18T07:22:00Z",
            scheduled_arrival="2026-11-18T11:45:00Z",
            raw_json='{"stops": 12}',
        )
        row = trip.to_db_row()
        self.assertEqual(len(row), 9)
        self.assertEqual(row[0], "sched-101")
        self.assertEqual(row[1], "1597")
        self.assertEqual(row[2], "Αθήνα")
        self.assertEqual(row[3], "Θεσσαλονίκη")
        self.assertEqual(row[4], "PATHE")
        self.assertEqual(row[5], "2026-11-18")
        self.assertEqual(row[6], "2026-11-18T07:22:00Z")
        self.assertEqual(row[7], "2026-11-18T11:45:00Z")
        self.assertEqual(row[8], '{"stops": 12}')

    def test_schedule_stop_db_serialization(self):
        stop = ScheduleStopDB(
            schedule_id="sched-101",
            stop_sequence=3,
            station_name="Λιανοκλάδι",
            station_id="st-lianokladi",
            scheduled_arrival="2026-11-18T09:15:00Z",
            scheduled_departure="2026-11-18T09:18:00Z",
            actual_arrival="2026-11-18T09:19:00Z",
            actual_departure="2026-11-18T09:21:00Z",
            platform="2",
            delay_sec=180,
        )
        row = stop.to_db_row()
        self.assertEqual(len(row), 10)
        self.assertEqual(row[0], "sched-101")
        self.assertEqual(row[1], 3)
        self.assertEqual(row[2], "st-lianokladi")
        self.assertEqual(row[3], "Λιανοκλάδι")
        self.assertEqual(row[4], "2026-11-18T09:15:00Z")
        self.assertEqual(row[5], "2026-11-18T09:18:00Z")
        self.assertEqual(row[6], "2026-11-18T09:19:00Z")
        self.assertEqual(row[7], "2026-11-18T09:21:00Z")
        self.assertEqual(row[8], "2",)
        self.assertEqual(row[9], 180)

    def test_telemetry_history_row_from_train_position(self):
        pos = TrainPosition(
            id="pos-999",
            train_number="2232",
            locomotive_number="460103",
            name="2232",
            train_type="passenger",
            service_type="Suburban",
            status="Κανονική",
            origin="Άνω Λιόσια",
            destination="Αεροδρόμιο",
            next_station="Νερατζιώτισσα",
            speed=95.5,
            course=115.0,
            track_heading=114.8,
            lat=38.0508,
            lng=23.7824,
            altitude=230.0,
            delay=3,
            progress=0.45,
            chainage_m=29300,
            corridor="PIRAIR",
            distance_to_destination_m=15000,
            timestamp="2026-11-18T12:00:00.000Z",
            schedule_id="sched-suburban-1",
        )

        row = TelemetryHistoryRow.from_train_position(pos)
        self.assertEqual(row.train_number, "2232")
        self.assertEqual(row.schedule_id, "sched-suburban-1")
        self.assertEqual(row.recorded_at, "2026-11-18T12:00:00.000000Z")
        self.assertEqual(row.lat, 38.0508)
        self.assertEqual(row.lng, 23.7824)
        self.assertEqual(row.speed, 95.5)
        self.assertEqual(row.course, 115.0)
        self.assertEqual(row.track_heading, 114.8)
        self.assertEqual(row.altitude, 230.0)
        self.assertEqual(row.delay_min, 3)
        self.assertEqual(row.status, "Κανονική")
        self.assertEqual(row.progress, 0.45)
        self.assertEqual(row.chainage_m, 29300)
        self.assertEqual(row.dist_to_dest_m, 15000)
        self.assertEqual(row.next_station, "Νερατζιώτισσα")
        self.assertEqual(row.corridor, "PIRAIR")

        db_row = row.to_db_row()
        self.assertEqual(len(db_row), 18)
        self.assertEqual(db_row[0], "2232")
        self.assertEqual(db_row[1], "sched-suburban-1")
        self.assertEqual(db_row[2], "2026-11-18T12:00:00.000000Z")
        self.assertEqual(db_row[3], 38.0508)
        self.assertEqual(db_row[4], 23.7824)
        self.assertEqual(db_row[5], 95.5)

    def test_recording_session_db_serialization(self):
        session = RecordingSessionDB(
            started_at="2026-11-18T12:00:00Z",
            ended_at="2026-11-18T13:00:00Z",
            mode="live",
            total_records_written=1250,
            client_version="1.0.0",
        )
        row = session.to_db_row()
        self.assertEqual(len(row), 5)
        self.assertEqual(row[0], "2026-11-18T12:00:00Z")
        self.assertEqual(row[1], "2026-11-18T13:00:00Z")
        self.assertEqual(row[2], "live")
        self.assertEqual(row[3], 1250)
        self.assertEqual(row[4], "1.0.0")

    def test_iso_utc_now_format_and_no_warnings(self):
        """iso_utc_now() must emit RFC3339/ISO 8601 UTC string without DeprecationWarning."""
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            ts = iso_utc_now()

        # Zero deprecation warnings in Python 3.12+
        deprecation_warnings = [
            w for w in captured if issubclass(w.category, DeprecationWarning)
        ]
        self.assertEqual(len(deprecation_warnings), 0)

        # Output format must end in Z and be parseable
        self.assertTrue(ts.endswith("Z"))
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        self.assertEqual(parsed.tzinfo, timezone.utc)
        now_dt = datetime.now(timezone.utc)
        self.assertLess(abs((now_dt - parsed).total_seconds()), 5.0)

    def test_telemetry_history_row_fallback_timestamp(self):
        """from_train_position without timestamp must generate valid UTC timestamp via iso_utc_now."""
        pos = TrainPosition(
            id="pos-no-ts",
            train_number="IC52",
            locomotive_number="120001",
            name="Intercity 52",
            train_type="passenger",
            service_type="Intercity",
            status="Κανονική",
            origin="Αθήνα",
            destination="Θεσσαλονίκη",
            next_station="Οινόη",
            speed=120.0,
            course=350.0,
            track_heading=349.5,
            lat=38.3200,
            lng=23.6500,
            altitude=140.0,
            delay=0,
            progress=0.15,
            chainage_m=62000,
            corridor="PATHE",
            distance_to_destination_m=438000,
            timestamp=None,
            schedule_id=None,
        )

        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            row = TelemetryHistoryRow.from_train_position(pos, recorded_at=None)

        deprecation_warnings = [
            w for w in captured if issubclass(w.category, DeprecationWarning)
        ]
        self.assertEqual(len(deprecation_warnings), 0)
        self.assertTrue(row.recorded_at.endswith("Z"))
        parsed = datetime.fromisoformat(row.recorded_at.replace("Z", "+00:00"))
        now_dt = datetime.now(timezone.utc)
        self.assertLess(abs((now_dt - parsed).total_seconds()), 5.0)

    def test_canonical_utc_parser_matrix_python_310_safe(self):
        """TC-STO-09: canonicalize_iso_utc and parse_iso_utc_datetime normalize all valid ISO formats to 27-char UTC."""
        from storage.models import (
            canonicalize_iso_utc,
            normalize_end_bound,
            normalize_start_bound,
            parse_iso_utc_datetime,
        )

        cases = [
            ("2026-11-20T10:00:00Z", "2026-11-20T10:00:00.000000Z"),
            ("2026-11-20T10:00:00z", "2026-11-20T10:00:00.000000Z"),
            ("2026-11-20T10:00:00+00:00", "2026-11-20T10:00:00.000000Z"),
            ("2026-11-20T12:00:00+02:00", "2026-11-20T10:00:00.000000Z"),
            ("2026-11-20T04:30:00-0530", "2026-11-20T10:00:00.000000Z"),
            ("2026-11-20 10:00:00", "2026-11-20T10:00:00.000000Z"),
            ("2026-11-20T10:00:00.5Z", "2026-11-20T10:00:00.500000Z"),
            ("2026-11-20T10:00:00.50Z", "2026-11-20T10:00:00.500000Z"),
            ("2026-11-20T10:00:00.500Z", "2026-11-20T10:00:00.500000Z"),
            ("2026-11-20T10:00:00.123456Z", "2026-11-20T10:00:00.123456Z"),
            ("2026-11-20T10:00:00.123456789Z", "2026-11-20T10:00:00.123456Z"),
            (datetime(2026, 11, 20, 10, 0, 0, tzinfo=timezone.utc), "2026-11-20T10:00:00.000000Z"),
            (1795168800.25, "2026-11-20T10:00:00.250000Z"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                out = canonicalize_iso_utc(raw)
                self.assertEqual(len(out), 27)
                self.assertEqual(out, expected)

        # Reject invalid types / non-finite numbers cleanly
        for bad in (None, "", "   ", "not-a-timestamp", True, False, float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_iso_utc_datetime(bad))

        # Date-only bound expansion
        self.assertEqual(normalize_start_bound("2026-11-20"), "2026-11-20T00:00:00.000000Z")
        self.assertEqual(normalize_end_bound("2026-11-20"), "2026-11-20T23:59:59.999999Z")
        self.assertIsNone(normalize_start_bound(None))
        self.assertIsNone(normalize_end_bound("  "))
        self.assertEqual(len(iso_utc_now()), 27)


if __name__ == "__main__":
    unittest.main()
