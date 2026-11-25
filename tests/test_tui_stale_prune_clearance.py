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
    """Test suite validating state-aware stale pruning (P0-9/P0-17), immediate conflict eviction, and station dwell preemption."""

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

    def test_live_stale_timeout_default_600s(self) -> None:
        """TC-PRN-01: Default stale timeout is 600s for live mode, toggling to 1800s in replay mode."""
        self.assertEqual(self.store.get_stale_timeout(), DEFAULT_LIVE_STALE_TIMEOUT_SEC)
        self.assertEqual(self.store.get_stale_timeout(), 600.0)

        # Toggle to replay mode
        self.store.set_replay_status(is_replay=True, virtual_time=1000.0)
        self.assertEqual(self.store.get_stale_timeout(), DEFAULT_REPLAY_STALE_TIMEOUT_SEC)
        self.assertEqual(self.store.get_stale_timeout(), 1800.0)

        # Toggle back to live mode
        self.store.set_replay_status(is_replay=False)
        self.assertEqual(self.store.get_stale_timeout(), DEFAULT_LIVE_STALE_TIMEOUT_SEC)
        self.assertEqual(self.store.get_stale_timeout(), 600.0)

        # Explicit custom timeout override
        self.store.set_stale_timeout(45.0)
        self.assertEqual(self.store.get_stale_timeout(), 45.0)

    def test_prune_evicts_departed_train(self) -> None:
        """TC-PRN-02: Open-line trains absent past the 600s cutoff are evicted from state store."""
        t0 = 1000.0
        pos = _make_test_position("T-101", "IC52")
        self.store.update_positions([pos], virtual_now=t0)

        self.assertIsNotNone(self.store.get_train("T-101"))
        snap_before = self.store.get_snapshot(now=t0)
        self.assertEqual(len(snap_before.trains), 1)

        # Advance by 590s (within 600s live eviction window — survives past 180s DROPPED threshold)
        pruned_590 = self.store.prune_stale_trains(now=t0 + 590.0)
        self.assertEqual(pruned_590, 0)
        self.assertIsNotNone(self.store.get_train("T-101"))

        # Advance past 600s cutoff (e.g. 605s)
        pruned_605 = self.store.prune_stale_trains(now=t0 + 605.0)
        self.assertEqual(pruned_605, 1)
        self.assertIsNone(self.store.get_train("T-101"))

        snap_after = self.store.get_snapshot(now=t0 + 605.0)
        self.assertEqual(len(snap_after.trains), 0)

    def test_prune_clears_active_conflict_immediately(self) -> None:
        """TC-PRN-03: Stale train demotes conflict to ADVISORY at >60s and pruning clears it completely."""
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

        snap = self.store.get_snapshot(now=t0)
        self.assertGreater(snap.critical_conflict_count + snap.warning_conflict_count, 0)
        self.assertGreater(len(snap.active_conflicts), 0)

        # Update T-2 with fresh telemetry at t0 + 100s, leaving T-1 stale (age 100s > 60s threshold)
        t2_fresh = _make_test_position("T-2", "1598", lat=38.04, lng=23.70, speed=60.0, course=180.0)
        self.store.update_positions([t2_fresh], virtual_now=t0 + 100.0)
        snap_stale = self.store.get_snapshot(now=t0 + 100.0)
        self.assertEqual(snap_stale.critical_conflict_count, 0)
        self.assertEqual(snap_stale.advisory_conflict_count, 1)

        # Keep T-2 fresh at t0 + 605s and prune T-1 (age 605s > 600s cutoff)
        self.store.update_positions([t2_fresh], virtual_now=t0 + 605.0)
        pruned = self.store.prune_stale_trains(now=t0 + 605.0)
        self.assertEqual(pruned, 1)
        self.assertIsNone(self.store.get_train("T-1"))
        self.assertIsNotNone(self.store.get_train("T-2"))

        # Snapshot must reflect zero conflicts immediately
        snap_pruned = self.store.get_snapshot(now=t0 + 605.0)
        self.assertEqual(snap_pruned.critical_conflict_count, 0)
        self.assertEqual(snap_pruned.warning_conflict_count, 0)
        self.assertEqual(snap_pruned.advisory_conflict_count, 0)
        self.assertEqual(len(snap_pruned.active_conflicts), 0)

    def test_prune_clears_dropped_and_frozen_snapshot_counts(self) -> None:
        """TC-PRN-04: Open-line train reaches DROPPED at 190s and pruning past 600s clears all tallies."""
        t0 = 3000.0
        # Stationary train out on open line (no stations nearby)
        t_drop = _make_test_position("T-DROP", "9999", lat=39.5, lng=22.5, speed=0.0)
        self.store.update_positions([t_drop], virtual_now=t0)

        # At 190s (past 180s drop threshold, before 600s eviction), train MUST reach DROPPED
        self.assertEqual(self.store.prune_stale_trains(now=t0 + 190.0), 0)
        snap_190 = self.store.get_snapshot(now=t0 + 190.0)
        self.assertEqual(len(snap_190.trains), 1)
        self.assertEqual(snap_190.dropped_count, 1)

        # Advance past 600s and prune
        pruned = self.store.prune_stale_trains(now=t0 + 605.0)
        self.assertEqual(pruned, 1)

        snap_after = self.store.get_snapshot(now=t0 + 605.0)
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

        snap = self.store.get_snapshot(now=t0)
        self.assertEqual(snap.selected_train.position.id, "T-SEL")
        self.assertTrue(snap.follow_mode)
        self.assertIn("T-SEL", snap.watched_train_ids)

        # Prune stale train past 600s live cutoff
        pruned = self.store.prune_stale_trains(now=t0 + 605.0)
        self.assertEqual(pruned, 1)

        snap_after = self.store.get_snapshot(now=t0 + 605.0)
        self.assertIsNone(snap_after.selected_train)
        self.assertFalse(snap_after.follow_mode)
        self.assertNotIn("T-SEL", snap_after.watched_train_ids)


if __name__ == "__main__":
    unittest.main()
