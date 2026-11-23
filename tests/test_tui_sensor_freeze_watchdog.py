"""
Unit and integration test suite for P0-11: Sensor Freeze Watchdog Resilience.

Verifies packet freshness qualification, velocity threshold gating, station crawl filtering,
and StateStore configuration plumbing.
"""

import time
import unittest

from train_tracker import TrainPosition
from tui.geo import (
    DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC,
    DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH,
    DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC,
    GeoPoint,
    SensorHealthState,
    evaluate_sensor_health,
)
from tui.mock_engine import MockTelemetryEngine, load_stations
from tui.state import ConnectionStatus, TrainStateStore


def _make_pos(
    train_id: str = "train-1",
    train_number: str = "IC 52",
    speed: float = 120.0,
    lat: float = 38.7412,
    lng: float = 22.4810,
    delay: int = 0,
    corridor: str = "pathe-mainline",
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
        distance_to_destination_m=350000,
        timestamp=None,
        schedule_id=None,
    )


class TestSensorFreezeWatchdog(unittest.TestCase):
    """Test suite validating resilient sensor freeze detection and false alarm mitigation."""

    def setUp(self) -> None:
        self.stations = list(load_stations().values())

    def test_tc_frz_01_genuine_sensor_freeze(self) -> None:
        """TC-FRZ-01: Fresh telemetry asserting speed >= 5 km/h with static coordinates triggers FROZEN."""
        now = 2000.0
        pt = GeoPoint(lat=38.7412, lng=22.4810)
        assessment, anchor_pt, anchor_time, _ = evaluate_sensor_health(
            point=pt,
            current_speed=110.0,
            last_updated=now - 1.0,  # 1s old <= 15s fresh threshold
            last_moved_pt=pt,
            last_moved_time=now - 65.0,  # 65s static >= 60s freeze threshold
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.FROZEN)
        self.assertEqual(assessment.short_code, "FRZ")
        self.assertTrue(assessment.is_alert)
        self.assertTrue(assessment.is_critical)
        self.assertIn("FROZEN", assessment.description)
        self.assertIn("65s static", assessment.description)
        self.assertIn("110 km/h", assessment.description)

    def test_tc_frz_02_network_delay_inhibits_sensor_freeze(self) -> None:
        """TC-FRZ-02: Stalled telemetry delivery (> 15s elapsed) must be classified as STALE, not FROZEN."""
        now = 2000.0
        pt = GeoPoint(lat=38.7412, lng=22.4810)
        # Train was running at 110 km/h, but packet delivery delayed 50s
        assessment, _, _, _ = evaluate_sensor_health(
            point=pt,
            current_speed=110.0,
            last_updated=now - 50.0,  # 50s old > 15s fresh threshold, > 30s stale
            last_moved_pt=pt,
            last_moved_time=now - 65.0,  # 65s static
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.STALE)
        self.assertEqual(assessment.short_code, "STL")
        self.assertFalse(assessment.is_alert)
        self.assertFalse(assessment.is_critical)
        self.assertIn("STALE", assessment.description)

    def test_tc_frz_03_station_approach_crawl_filtering(self) -> None:
        """TC-FRZ-03: Platform approach crawl (speed < 5.0 km/h) never triggers FROZEN."""
        now = 2000.0
        # Athens Central terminal platform coordinate
        pt = GeoPoint(lat=37.9922, lng=23.7212)
        assessment, _, _, _ = evaluate_sensor_health(
            point=pt,
            current_speed=3.5,  # Crawl speed < 5.0 km/h
            last_updated=now - 1.0,  # Fresh packet
            last_moved_pt=pt,
            last_moved_time=now - 70.0,  # 70s static under platform canopy
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertNotEqual(assessment.state, SensorHealthState.FROZEN)
        self.assertEqual(assessment.state, SensorHealthState.NORMAL_MOTION)
        self.assertEqual(assessment.short_code, "RUN")
        self.assertFalse(assessment.is_alert)

    def test_tc_frz_04_recovery_on_coordinate_advance(self) -> None:
        """TC-FRZ-04: Advancing coordinates (> 10m) clears FROZEN state immediately."""
        now = 2000.0
        anchor_pt = GeoPoint(lat=38.7412, lng=22.4810)
        # Advanced by ~100m north
        advanced_pt = GeoPoint(lat=38.7421, lng=22.4810)

        assessment, new_anchor, new_time, _ = evaluate_sensor_health(
            point=advanced_pt,
            current_speed=110.0,
            last_updated=now - 1.0,
            last_moved_pt=anchor_pt,
            last_moved_time=now - 80.0,  # was previously frozen
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.NORMAL_MOTION)
        self.assertEqual(assessment.short_code, "RUN")
        self.assertFalse(assessment.is_alert)
        self.assertEqual(new_anchor, advanced_pt)
        self.assertEqual(new_time, now)

    def test_tc_frz_05_borderline_packet_freshness(self) -> None:
        """TC-FRZ-05: Packet age between fresh cutoff (15s) and stale threshold (30s) avoids FROZEN."""
        now = 2000.0
        pt = GeoPoint(lat=38.7412, lng=22.4810)
        assessment, _, _, _ = evaluate_sensor_health(
            point=pt,
            current_speed=80.0,
            last_updated=now - 20.0,  # 20s old > 15s fresh cutoff, but <= 30s stale
            last_moved_pt=pt,
            last_moved_time=now - 65.0,  # 65s static
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        # Not frozen because packet is not fresh enough to prove hardware sensor freeze
        self.assertNotEqual(assessment.state, SensorHealthState.FROZEN)
        self.assertFalse(assessment.is_alert)
        self.assertEqual(assessment.state, SensorHealthState.NORMAL_MOTION)

    def test_tc_frz_06_store_configuration_plumbing(self) -> None:
        """TC-FRZ-06: TrainStateStore getters and setters adjust sensor freeze parameters correctly."""
        store = TrainStateStore()

        # Defaults
        self.assertEqual(
            store.get_sensor_freeze_threshold(), DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC
        )
        self.assertEqual(
            store.get_sensor_freeze_min_speed(), DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH
        )
        self.assertEqual(
            store.get_sensor_freeze_max_packet_age(),
            DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC,
        )

        # Dynamic updates
        store.set_sensor_freeze_threshold(45.0)
        self.assertEqual(store.get_sensor_freeze_threshold(), 45.0)

        store.set_sensor_freeze_min_speed(10.0)
        self.assertEqual(store.get_sensor_freeze_min_speed(), 10.0)

        store.set_sensor_freeze_max_packet_age(20.0)
        self.assertEqual(store.get_sensor_freeze_max_packet_age(), 20.0)

        # Defensive bounds clamping
        store.set_sensor_freeze_threshold(1.0)  # Min clamp: 5.0s
        self.assertEqual(store.get_sensor_freeze_threshold(), 5.0)

        store.set_sensor_freeze_min_speed(0.1)  # Min clamp: 1.0 km/h
        self.assertEqual(store.get_sensor_freeze_min_speed(), 1.0)

        store.set_sensor_freeze_max_packet_age(0.2)  # Min clamp: 1.0s
        self.assertEqual(store.get_sensor_freeze_max_packet_age(), 1.0)

    def test_tc_frz_07_store_snapshot_freeze_evaluation(self) -> None:
        """TC-FRZ-07: TrainStateStore snapshot accurately evaluates frozen vs delayed trains."""
        store = TrainStateStore()
        now = time.time()
        pos_frz = _make_pos("train-frz", "IC52", speed=130.0)
        pos_delayed = _make_pos("train-del", "IC54", speed=130.0)

        store.update_positions([pos_frz, pos_delayed])
        store.set_connection_status(ConnectionStatus.CONNECTED)

        with store._lock:
            # Train 1: Authentic freeze (packets fresh, static 70s)
            store._trains["train-frz"].last_moved_time = now - 70.0
            store._trains["train-frz"].last_updated = now - 1.0

            # Train 2: Delayed telemetry (packets 50s old, static 70s)
            store._trains["train-del"].last_moved_time = now - 70.0
            store._trains["train-del"].last_updated = now - 50.0

        snapshot = store.get_snapshot(now=now)

        frz_rec = snapshot.trains["train-frz"]
        del_rec = snapshot.trains["train-del"]

        self.assertEqual(frz_rec.sensor_health.state, SensorHealthState.FROZEN)
        self.assertTrue(frz_rec.sensor_health.is_alert)
        self.assertEqual(snapshot.frozen_count, 1)

        self.assertEqual(del_rec.sensor_health.state, SensorHealthState.STALE)
        self.assertFalse(del_rec.sensor_health.is_alert)

    def test_tc_frz_08_mock_engine_simulation_integration(self) -> None:
        """TC-FRZ-08: MockTelemetryEngine.simulate_sensor_freeze triggers FROZEN after threshold."""
        engine = MockTelemetryEngine()
        store = TrainStateStore()
        target_train = engine.trains[0]
        train_id = target_train.id

        # Ingest initial positions
        initial_batch = engine.step(1.0)
        store.update_positions(initial_batch)

        # Enable sensor freeze in mock engine
        success = engine.simulate_sensor_freeze(train_id, frozen=True)
        self.assertTrue(success)

        # Step forward by 1s intervals
        start_time = time.time()
        for i in range(65):
            virtual_now = start_time + float(i + 1)
            batch = engine.step(1.0)
            store.update_positions(batch, virtual_now=virtual_now)

        snapshot = store.get_snapshot(now=start_time + 65.0)
        rec = snapshot.trains[train_id]

        # After 65 seconds of simulated frozen coords with fresh frames arriving every second
        self.assertEqual(rec.sensor_health.state, SensorHealthState.FROZEN)
        self.assertTrue(rec.sensor_health.is_alert)
        self.assertEqual(rec.sensor_health.short_code, "FRZ")

        # Disable sensor freeze
        engine.simulate_sensor_freeze(train_id, frozen=False)
        # Advance motion
        batch = engine.step(1.0)
        store.update_positions(batch, virtual_now=start_time + 66.0)
        snapshot = store.get_snapshot(now=start_time + 66.0)
        rec_cleared = snapshot.trains[train_id]
        self.assertEqual(rec_cleared.sensor_health.state, SensorHealthState.NORMAL_MOTION)
        self.assertFalse(rec_cleared.sensor_health.is_alert)


if __name__ == "__main__":
    unittest.main()
