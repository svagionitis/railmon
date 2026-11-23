"""Unit tests for F-5.2 Sensor Health state tracking, persistence, and timeout sweeps."""

import time
import unittest

from train_tracker import TrainPosition
from tui.geo import (
    GeoPoint,
    SensorHealthAssessment,
    SensorHealthState,
)
from tui.mock_engine import StationNode
from tui.state import TrainRecord, TrainStateStore


def _make_pos(
    train_id: str = "train-1",
    train_number: str = "IC 52",
    speed: float = 120.0,
    lat: float = 39.5,
    lng: float = 22.3,
    delay: float = 0.0,
    corridor: str = "PATHE",
) -> TrainPosition:
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120-001",
        name=train_number,
        train_type="InterCity",
        service_type="Passenger",
        status="Κανονική" if speed > 5.0 else "Στάση",
        origin="Athens",
        destination="Thessaloniki",
        next_station="SKA",
        speed=speed,
        course=15.0,
        track_heading=15.0,
        lat=lat,
        lng=lng,
        altitude=45.0,
        delay=delay,
        progress=0.15,
        chainage_m=12000,
        corridor=corridor,
        distance_to_destination_m=480000,
        timestamp="2026-11-18T12:00:00Z",
        schedule_id="sch-101",
    )


class TestSensorHealthState(unittest.TestCase):
    """Test suite for TrainRecord cloning, TrainStateStore periodic health sweeps, and snapshot metrics."""

    def setUp(self) -> None:
        self.store = TrainStateStore()
        # Set reference stations
        stn_larissa = StationNode(
            id="LAR",
            code=1100,
            name_el="Λάρισα",
            name_en="Larissa",
            point=GeoPoint(lat=39.6385, lng=22.4241),
            corridor="PATHE",
            is_hub=True,
        )
        self.store.set_stations([stn_larissa])

    def test_train_record_clone_preserves_sensor_health_fields(self) -> None:
        """Verify TrainRecord.clone preserves all F-5.2 kinematic and sensor health attributes."""
        pos = _make_pos(train_id="train-1", train_number="IC52", speed=120.0)
        rec = TrainRecord(
            position=pos,
            last_moved_pt=GeoPoint(lat=39.4, lng=22.3),
            last_moved_time=12345.0,
            station_dwell_start=12300.0,
            sensor_health=SensorHealthAssessment(
                state=SensorHealthState.NORMAL_MOTION,
                elapsed_sec=2.0,
                static_duration_sec=0.0,
                displacement_m=100.0,
                current_speed_kmh=120.0,
                short_code="RUN",
            ),
        )
        cloned = rec.clone()
        self.assertEqual(cloned.last_moved_pt, rec.last_moved_pt)
        self.assertEqual(cloned.last_moved_time, rec.last_moved_time)
        self.assertEqual(cloned.station_dwell_start, rec.station_dwell_start)
        self.assertEqual(cloned.sensor_health, rec.sensor_health)

    def test_upsert_train_assigns_sensor_health(self) -> None:
        """Verify _upsert_train_position initializes and updates sensor_health."""
        pos = _make_pos(train_id="train-1", train_number="IC50", speed=140.0)
        now = time.time()
        self.store.update_positions([pos])
        rec = self.store.get_train("train-1")
        self.assertIsNotNone(rec)
        assert rec is not None
        self.assertIsNotNone(rec.sensor_health)
        assert rec.sensor_health is not None
        self.assertEqual(rec.sensor_health.state, SensorHealthState.NORMAL_MOTION)
        self.assertEqual(rec.sensor_health.short_code, "RUN")

    def test_snapshot_aggregates_dropped_and_frozen_counts(self) -> None:
        """Verify StateSnapshot counts and train ID tuples for dropped and frozen anomalies."""
        now = time.time()
        # Train 1: Dropped (last updated 150s ago)
        pos1 = _make_pos(train_id="t-drop", train_number="52", speed=100.0)
        # Train 2: Frozen (static coords for 50s with positive speed)
        pos2 = _make_pos(train_id="t-frz", train_number="1599", speed=130.0, lat=38.7, lng=22.4)
        # Train 3: Normal moving
        pos3 = _make_pos(train_id="t-norm", train_number="1597", speed=140.0, lat=39.6, lng=22.4)

        self.store.update_positions([pos1, pos2, pos3])

        # Manually alter timing to simulate anomaly states
        with self.store._lock:
            # t-drop: last updated 190s ago (exceeds 180s baseline)
            self.store._trains["t-drop"].last_updated = now - 190.0

            # t-frz: last moved 50s ago, fresh packet 1s ago
            self.store._trains["t-frz"].last_updated = now - 1.0
            self.store._trains["t-frz"].last_moved_time = now - 50.0

        snapshot = self.store.get_snapshot(now=now)
        self.assertEqual(snapshot.dropped_count, 1)
        self.assertEqual(snapshot.frozen_count, 1)
        self.assertIn("t-drop", snapshot.dropped_train_ids)
        self.assertIn("t-frz", snapshot.frozen_train_ids)

    def test_evaluate_all_sensor_health_updates_without_packets(self) -> None:
        """Verify evaluate_all_sensor_health updates states when time passes with no incoming packets."""
        pos = _make_pos(train_id="t-quiet", train_number="50", speed=120.0)
        t0 = 1000.0
        self.store.update_positions([pos], virtual_now=t0)

        # 40 seconds later: should transition to STALE
        self.store.evaluate_all_sensor_health(now=t0 + 40.0)
        rec = self.store.get_train("t-quiet")
        assert rec is not None and rec.sensor_health is not None
        self.assertEqual(rec.sensor_health.state, SensorHealthState.STALE)

        # 190 seconds later: should transition to DROPPED
        self.store.evaluate_all_sensor_health(now=t0 + 190.0)
        rec = self.store.get_train("t-quiet")
        assert rec is not None and rec.sensor_health is not None
        self.assertEqual(rec.sensor_health.state, SensorHealthState.DROPPED)
        self.assertTrue(rec.sensor_health.is_alert)


if __name__ == "__main__":
    unittest.main()
