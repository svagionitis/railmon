"""
Unit tests for storage domain models and dataclass transformations.
"""

import unittest
from datetime import datetime

from storage.models import (
    RecordingSessionDB,
    ScheduleStopDB,
    ScheduledTripDB,
    TelemetryHistoryRow,
    TrainRecordDB,
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
        self.assertEqual(row.recorded_at, "2026-11-18T12:00:00.000Z")
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
        self.assertEqual(len(db_row), 16)
        self.assertEqual(db_row[0], "2232")
        self.assertEqual(db_row[1], "sched-suburban-1")
        self.assertEqual(db_row[2], "2026-11-18T12:00:00.000Z")
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


if __name__ == "__main__":
    unittest.main()
