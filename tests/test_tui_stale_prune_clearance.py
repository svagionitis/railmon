"""Unit tests for P0-9: Stale Train Pruning, Live Stream Eviction & Screen Warning Clearance."""

import unittest

from train_tracker import TrainPosition
from tui.geo import (
    ConflictSeverity,
    ConflictType,
    ConflictWatchdogConfig,
    GeoPoint,
    SensorHealthState,
    evaluate_sensor_health,
)
from tui.mock_engine import StationNode
from tui.state import (
    DEFAULT_LIVE_STALE_TIMEOUT_SEC,
    DEFAULT_REPLAY_STALE_TIMEOUT_SEC,
    TrainStateStore,
)


def _make_test_position(
    train_id: str = "T-100",
    train_number: str = "1597",
    lat: float = 38.0,
    lng: float = 23.7,
    speed: float = 80.0,
    course: float = 0.0,
    corridor: str = "pathe-mainline",
) -> TrainPosition:
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="460-101",
        name=train_number,
        train_type="Passenger",
        service_type="Intercity",
        status="In Transit" if speed > 1.0 else "Stationary",
        origin="Athens",
        destination="Thessaloniki",
        next_station="SKA",
        speed=speed,
        course=course,
        track_heading=course,
        lat=lat,
        lng=lng,
        altitude=50.0,
        delay=0,
        progress=0.1,
        chainage_m=5000,
        corridor=corridor,
        distance_to_destination_m=450000,
        timestamp="2026-11-23T12:00:00Z",
        schedule_id="sch-1",
    )


class TestStalePruneClearance(unittest.TestCase):
    """Test suite validating 90s stale pruning, immediate conflict eviction, and station dwell preemption."""

    def setUp(self) -> None:
        self.store = TrainStateStore()
        self.stn_athens = StationNode(
            id="ATH",
            code="1001",
            name_el="Αθήνα",
            name_en="Athens Central",
            point=GeoPoint(lat=37.9922, lng=23.7208),
            corridor="pathe-mainline",
            is_hub=True,
        )
        self.store.set_stations([self.stn_athens])

    def test_live_stale_timeout_default_90s(self) -> None:
        """TC-PRN-01: Default stale timeout is 90s for live mode, toggling to 1800s in replay mode."""
        self.assertEqual(self.store.get_stale_timeout(), DEFAULT_LIVE_STALE_TIMEOUT_SEC)
        self.assertEqual(self.store.get_stale_timeout(), 90.0)

        # Toggle to replay mode
        self.store.set_replay_status(is_replay=True, virtual_time=1000.0)
        self.assertEqual(self.store.get_stale_timeout(), DEFAULT_REPLAY_STALE_TIMEOUT_SEC)
        self.assertEqual(self.store.get_stale_timeout(), 1800.0)

        # Toggle back to live mode
        self.store.set_replay_status(is_replay=False)
        self.assertEqual(self.store.get_stale_timeout(), DEFAULT_LIVE_STALE_TIMEOUT_SEC)
        self.assertEqual(self.store.get_stale_timeout(), 90.0)

        # Explicit custom timeout override
        self.store.set_stale_timeout(45.0)
        self.assertEqual(self.store.get_stale_timeout(), 45.0)

    def test_prune_evicts_departed_train(self) -> None:
        """TC-PRN-02: Trains absent past the 90s cutoff are evicted from state store."""
        t0 = 1000.0
        pos = _make_test_position("T-101", "IC52")
        self.store.update_positions([pos], virtual_now=t0)

        self.assertIsNotNone(self.store.get_train("T-101"))
        snap_before = self.store.get_snapshot()
        self.assertEqual(len(snap_before.trains), 1)

        # Advance by 80s (within 90s grace window)
        pruned_80 = self.store.prune_stale_trains(now=t0 + 80.0)
        self.assertEqual(pruned_80, 0)
        self.assertIsNotNone(self.store.get_train("T-101"))

        # Advance past 90s cutoff (e.g. 95s)
        pruned_95 = self.store.prune_stale_trains(now=t0 + 95.0)
        self.assertEqual(pruned_95, 1)
        self.assertIsNone(self.store.get_train("T-101"))

        snap_after = self.store.get_snapshot()
        self.assertEqual(len(snap_after.trains), 0)

    def test_prune_clears_active_conflict_immediately(self) -> None:
        """TC-PRN-03: Pruning a train immediately purges active conflicts and resets latches."""
        t0 = 2000.0
        # Two opposing trains on single-track section
        cfg = ConflictWatchdogConfig(
            opposing_critical_distance_km=15.0,
            opposing_warning_distance_km=25.0,
            single_line_working_corridors={"pathe-mainline"},
        )
        self.store.set_conflict_config(cfg)

        t1 = _make_test_position("T-1", "1597", lat=38.00, lng=23.70, speed=60.0, course=0.0)
        t2 = _make_test_position("T-2", "1598", lat=38.05, lng=23.70, speed=60.0, course=180.0)
        self.store.update_positions([t1, t2], virtual_now=t0)

        snap = self.store.get_snapshot()
        self.assertGreater(snap.critical_conflict_count + snap.warning_conflict_count, 0)
        self.assertGreater(len(snap.active_conflicts), 0)

        # Update T-2 with fresh telemetry at t0 + 100s, leaving T-1 un-updated (completed at t0)
        t2_fresh = _make_test_position("T-2", "1598", lat=38.04, lng=23.70, speed=60.0, course=180.0)
        self.store.update_positions([t2_fresh], virtual_now=t0 + 100.0)

        # Prune stale trains at t0 + 100s (T-1 has age 100s > 90s cutoff)
        pruned = self.store.prune_stale_trains(now=t0 + 100.0)
        self.assertEqual(pruned, 1)
        self.assertIsNone(self.store.get_train("T-1"))
        self.assertIsNotNone(self.store.get_train("T-2"))

        # Snapshot must reflect zero conflicts immediately
        snap_pruned = self.store.get_snapshot()
        self.assertEqual(snap_pruned.critical_conflict_count, 0)
        self.assertEqual(snap_pruned.warning_conflict_count, 0)
        self.assertEqual(len(snap_pruned.active_conflicts), 0)

    def test_prune_clears_dropped_and_frozen_snapshot_counts(self) -> None:
        """TC-PRN-04: Pruning stale trains immediately clears dropped and frozen anomaly tallies."""
        t0 = 3000.0
        # Stationary train out on open line (no stations nearby)
        t_drop = _make_test_position("T-DROP", "9999", lat=39.5, lng=22.5, speed=0.0)
        self.store.update_positions([t_drop], virtual_now=t0)

        # Before 90s, at 80s, verify train exists
        snap_80 = self.store.get_snapshot()
        self.assertEqual(len(snap_80.trains), 1)

        # Advance past 90s and prune
        pruned = self.store.prune_stale_trains(now=t0 + 95.0)
        self.assertEqual(pruned, 1)

        snap_after = self.store.get_snapshot()
        self.assertEqual(snap_after.dropped_count, 0)
        self.assertEqual(snap_after.frozen_count, 0)
        self.assertEqual(len(snap_after.trains), 0)

    def test_station_dwell_suppresses_drop_alarm(self) -> None:
        """TC-PRN-05: Stationary train resting at station siding does not trigger panic drop alert."""
        t0 = 4000.0
        # Place train stationary right at Athens Central (lat=37.9922, lng=23.7208)
        pt = GeoPoint(lat=37.9922, lng=23.7208)

        # Evaluate after 150 seconds of silence (> 120s drop threshold)
        assessment, _, _, _ = evaluate_sensor_health(
            point=pt,
            current_speed=0.0,
            last_updated=t0,
            last_moved_pt=pt,
            last_moved_time=t0,
            station_dwell_start=t0,
            stations=[self.stn_athens],
            now=t0 + 150.0,
            telemetry_drop_threshold_sec=120.0,
        )

        self.assertEqual(assessment.state, SensorHealthState.STATION_DWELL)
        self.assertFalse(assessment.is_alert)
        self.assertEqual(assessment.short_code, "DWL")
        self.assertIn("STATION DWELL [IDLE]", assessment.description)

    def test_prune_resets_selected_train_and_watch_state(self) -> None:
        """TC-PRN-06: Pruned train resets selected_id, follow_mode, and watched_train_ids."""
        t0 = 5000.0
        t_sel = _make_test_position("T-SEL", "IC50")
        self.store.update_positions([t_sel], virtual_now=t0)

        self.assertTrue(self.store.select_by_id("T-SEL"))
        self.store.set_follow_mode(True)
        self.assertTrue(self.store.toggle_watch_train("T-SEL"))

        snap = self.store.get_snapshot()
        self.assertEqual(snap.selected_train.position.id, "T-SEL")
        self.assertTrue(snap.follow_mode)
        self.assertIn("T-SEL", snap.watched_train_ids)

        # Prune stale train
        pruned = self.store.prune_stale_trains(now=t0 + 100.0)
        self.assertEqual(pruned, 1)

        snap_after = self.store.get_snapshot()
        self.assertIsNone(snap_after.selected_train)
        self.assertFalse(snap_after.follow_mode)
        self.assertNotIn("T-SEL", snap_after.watched_train_ids)


if __name__ == "__main__":
    unittest.main()
