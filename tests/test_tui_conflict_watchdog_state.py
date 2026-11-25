"""Unit tests for F-5.1 Corridor Headway & Opposing Train Conflict Watchdog state store and snapshot lifecycle."""

import unittest

from train_tracker import DelayUpdate, TrainPosition
from tui.geo import (
    ConflictSeverity,
    ConflictType,
    ConflictWatchdogConfig,
    GeoPoint,
)
from tui.mock_engine import StationNode
from tui.state import TrainStateStore


def _make_pos(
    train_id: str,
    train_number: str,
    lat: float,
    lng: float,
    speed: float,
    course: float,
    corridor: str,
) -> TrainPosition:
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120-001",
        name=train_number,
        train_type="Intercity",
        service_type="Passenger",
        status="In Transit" if speed > 1.0 else "Stationary",
        origin="Athens",
        destination="Thessaloniki",
        next_station="Avlis",
        speed=speed,
        course=course,
        track_heading=course,
        lat=lat,
        lng=lng,
        altitude=20.0,
        delay=0.0,
        progress=0.5,
        chainage_m=10000,
        corridor=corridor,
        distance_to_destination_m=20000,
        timestamp="2026-11-22T12:00:00Z",
        schedule_id="sch-1",
    )


class TestConflictWatchdogState(unittest.TestCase):
    """Test suite for state store conflict caching, snapshot exposure, and bell latching."""

    def setUp(self) -> None:
        self.store = TrainStateStore()
        self.stn_avlis = StationNode(
            id="AVL",
            code=1201,
            name_el="Αυλίδα",
            name_en="Avlis",
            point=GeoPoint(lat=38.4180, lng=23.6120),
            corridor="proastiakos-chalkida",
            is_hub=False,
        )
        self.store.set_stations([self.stn_avlis])

    def test_state_store_evaluates_conflicts_on_update(self) -> None:
        """Verify conflict evaluation populates records and snapshot."""
        t1 = _make_pos(
            train_id="T1597",
            train_number="1597",
            lat=38.4000,
            lng=23.6000,
            speed=92.0,
            course=30.0,
            corridor="proastiakos-chalkida",
        )
        t2 = _make_pos(
            train_id="T1598",
            train_number="1598",
            lat=38.4180,
            lng=23.6120,
            speed=88.0,
            course=210.0,
            corridor="proastiakos-chalkida",
        )

        self.store.update_positions([t1, t2])
        snap = self.store.get_snapshot()

        self.assertEqual(snap.critical_conflict_count, 1)
        self.assertEqual(snap.warning_conflict_count, 0)
        self.assertEqual(len(snap.active_conflicts), 1)

        alert = snap.active_conflicts[0]
        self.assertEqual(alert.severity, ConflictSeverity.CRITICAL)
        self.assertEqual(alert.conflict_type, ConflictType.OPPOSING_COLLISION)

        # Check records have active_conflict populated
        rec1 = self.store.get_train("T1597")
        rec2 = self.store.get_train("T1598")
        self.assertIsNotNone(rec1)
        self.assertIsNotNone(rec2)
        assert rec1 is not None and rec2 is not None
        self.assertIsNotNone(rec1.active_conflict)
        self.assertIsNotNone(rec2.active_conflict)
        self.assertEqual(rec1.active_conflict.alert_id, alert.alert_id)

    def test_selected_conflict_in_snapshot(self) -> None:
        """Verify selected_conflict reflects the selected train's conflict."""
        t1 = _make_pos(
            train_id="T1597",
            train_number="1597",
            lat=38.4000,
            lng=23.6000,
            speed=92.0,
            course=30.0,
            corridor="proastiakos-chalkida",
        )
        t2 = _make_pos(
            train_id="T1598",
            train_number="1598",
            lat=38.4180,
            lng=23.6120,
            speed=88.0,
            course=210.0,
            corridor="proastiakos-chalkida",
        )

        self.store.update_positions([t1, t2])
        self.store.select_by_id("T1597")

        snap = self.store.get_snapshot()
        self.assertIsNotNone(snap.selected_conflict)
        assert snap.selected_conflict is not None
        self.assertEqual(snap.selected_conflict.train_number_1, "1597")

    def test_edge_triggered_conflict_bell_latch(self) -> None:
        """Verify bell triggers on conflict onset and resets on consumption."""
        t1 = _make_pos(
            train_id="T1597",
            train_number="1597",
            lat=38.4000,
            lng=23.6000,
            speed=92.0,
            course=30.0,
            corridor="proastiakos-chalkida",
        )
        t2 = _make_pos(
            train_id="T1598",
            train_number="1598",
            lat=38.4180,
            lng=23.6120,
            speed=88.0,
            course=210.0,
            corridor="proastiakos-chalkida",
        )

        # Ingest new conflict
        self.store.update_positions([t1, t2])

        # First consumption should return True
        self.assertTrue(self.store.consume_conflict_bell())

        # Second consumption on same frame without state change should return False
        self.assertFalse(self.store.consume_conflict_bell())

        # Ingest subsequent frame with same conflict
        self.store.update_positions([t1, t2])
        # Bell should NOT re-trigger continuously
        self.assertFalse(self.store.consume_conflict_bell())

    def test_conflict_cleared_when_trains_separate(self) -> None:
        """Verify conflicts clear when trains are no longer in proximity."""
        t1 = _make_pos(
            train_id="T1597",
            train_number="1597",
            lat=38.4000,
            lng=23.6000,
            speed=92.0,
            course=30.0,
            corridor="proastiakos-chalkida",
        )
        t2 = _make_pos(
            train_id="T1598",
            train_number="1598",
            lat=38.4180,
            lng=23.6120,
            speed=88.0,
            course=210.0,
            corridor="proastiakos-chalkida",
        )

        self.store.update_positions([t1, t2])
        self.assertEqual(self.store.get_snapshot().critical_conflict_count, 1)

        # Move t2 far away (e.g. 50 km)
        t2_far = _make_pos(
            train_id="T1598",
            train_number="1598",
            lat=38.8500,
            lng=23.6120,
            speed=88.0,
            course=210.0,
            corridor="proastiakos-chalkida",
        )
        self.store.update_positions([t1, t2_far])
        snap = self.store.get_snapshot()
        self.assertEqual(snap.critical_conflict_count, 0)
        self.assertEqual(len(snap.active_conflicts), 0)

        rec1 = self.store.get_train("T1597")
        self.assertIsNotNone(rec1)
        assert rec1 is not None
        self.assertIsNone(rec1.active_conflict)

    def test_station_advisory_conflict_does_not_trip_bell(self) -> None:
        """Verify that ADVISORY station conflicts do not latch the audible conflict bell."""
        stn_athens = StationNode(
            id="ath",
            code=1001,
            name_el="Αθήνα",
            name_en="Athens Central",
            point=GeoPoint(lat=37.9926, lng=23.7203),
            corridor="pathe-mainline",
            is_hub=True,
        )
        self.store.set_stations([stn_athens])

        t1 = _make_pos(
            train_id="2232",
            train_number="2232",
            lat=37.9926,
            lng=23.7203,
            speed=2.0,
            course=355.0,
            corridor="pathe-mainline",
        )
        t2 = _make_pos(
            train_id="1597",
            train_number="1597",
            lat=37.9928,
            lng=23.7205,
            speed=0.0,
            course=355.0,
            corridor="pathe-mainline",
        )

        self.store.update_positions([t1, t2])
        snap = self.store.get_snapshot()

        # Station headway is ADVISORY
        self.assertEqual(snap.critical_conflict_count, 0)
        self.assertEqual(snap.warning_conflict_count, 0)
        self.assertEqual(snap.advisory_conflict_count, 1)

        # Bell latch MUST NOT be set for ADVISORY
        self.assertFalse(self.store.consume_conflict_bell())

    def test_get_and_set_conflict_config(self) -> None:
        """Verify conflict config getter and setter on TrainStateStore."""
        cfg = self.store.get_conflict_config()
        self.assertIsInstance(cfg, ConflictWatchdogConfig)
        self.assertEqual(cfg.headway_critical_distance_km, 1.0)

        new_cfg = ConflictWatchdogConfig(headway_critical_distance_km=0.8)
        self.store.set_conflict_config(new_cfg)
        self.assertEqual(self.store.get_conflict_config().headway_critical_distance_km, 0.8)

    def test_tc_wdg_13_state_store_stale_ghost_preemption_end_to_end(self) -> None:
        """TC-WDG-13: Real TrainPosition records in TrainStateStore demote 120s-stale opposing ghost to ADVISORY."""
        self.store.set_stations([])
        t0 = 10000.0
        t1 = _make_pos("T1597", "1597", 38.4000, 23.6000, speed=90.0, course=30.0, corridor="proastiakos-chalkida")
        t2 = _make_pos("T1598", "1598", 38.4180, 23.6120, speed=0.0, course=210.0, corridor="proastiakos-chalkida")

        self.store.update_positions([t1, t2], virtual_now=t0)
        snap0 = self.store.get_snapshot(now=t0)
        self.assertEqual(snap0.critical_conflict_count, 1)
        self.assertTrue(self.store.consume_conflict_bell())

        # Advance 120s: only T1597 refreshes; T1598 is silent 120s (> 60s threshold)
        self.store.update_positions([t1], virtual_now=t0 + 120.0)
        snap1 = self.store.get_snapshot(now=t0 + 120.0)
        self.assertEqual(snap1.critical_conflict_count, 0)
        self.assertEqual(snap1.warning_conflict_count, 0)
        self.assertEqual(snap1.advisory_conflict_count, 1)
        self.assertEqual(len(snap1.active_conflicts), 1)
        self.assertTrue(snap1.active_conflicts[0].alert_id.endswith("_STALE_OBSTRUCTION"))
        self.assertFalse(self.store.consume_conflict_bell())

    def test_tc_wdg_14_state_store_stale_headway_demotion_and_recovery(self) -> None:
        """TC-WDG-14: Stale lead train demotes headway breach to ADVISORY and recovers immediately on fresh GPS."""
        self.store.set_stations([])
        t0 = 20000.0
        t_lead = _make_pos("T-LEAD", "IC 50", 39.5200, 22.4000, speed=80.0, course=0.0, corridor="pathe-mainline")
        t_trail = _make_pos("T-TRAIL", "IC 52", 39.5165, 22.4000, speed=120.0, course=0.0, corridor="pathe-mainline")

        self.store.update_positions([t_lead, t_trail], virtual_now=t0)
        snap0 = self.store.get_snapshot(now=t0)
        self.assertEqual(snap0.critical_conflict_count, 1)
        self.assertTrue(self.store.consume_conflict_bell())

        # Lead train silent 75s (> 60s threshold) -> demoted to ADVISORY _STALE_HEADWAY
        self.store.update_positions([t_trail], virtual_now=t0 + 75.0)
        snap_stale = self.store.get_snapshot(now=t0 + 75.0)
        self.assertEqual(snap_stale.critical_conflict_count, 0)
        self.assertEqual(snap_stale.advisory_conflict_count, 1)
        self.assertTrue(snap_stale.active_conflicts[0].alert_id.endswith("_STALE_HEADWAY"))
        self.assertFalse(self.store.consume_conflict_bell())

        # Lead train sends fresh GPS at t0 + 80s -> immediately re-evaluates to live CRITICAL
        self.store.update_positions([t_lead, t_trail], virtual_now=t0 + 80.0)
        snap_rec = self.store.get_snapshot(now=t0 + 80.0)
        self.assertEqual(snap_rec.critical_conflict_count, 1)
        self.assertEqual(snap_rec.advisory_conflict_count, 0)

    def test_tc_wdg_18_delay_updates_preserve_conflict_staleness(self) -> None:
        """TC-WDG-18: F-10.1 apply_delay_updates() must not refresh last_updated or clear stale-ghost demotion."""
        self.store.set_stations([])
        t0 = 30000.0
        t1 = _make_pos("T1597", "1597", 38.4000, 23.6000, speed=90.0, course=30.0, corridor="proastiakos-chalkida")
        t2 = _make_pos("T1598", "1598", 38.4180, 23.6120, speed=0.0, course=210.0, corridor="proastiakos-chalkida")
        self.store.update_positions([t1, t2], virtual_now=t0)
        _ = self.store.consume_conflict_bell()

        # At t0 + 90s: T1597 updates GPS; T1598 only gets a delay delta (no GPS update)
        self.store.update_positions([t1], virtual_now=t0 + 90.0)
        delay_upd = DelayUpdate(
            train_id="T1598",
            train_number="1598",
            schedule_id="sch-1",
            delay=5,
        )
        applied = self.store.apply_delay_updates([delay_upd], now=t0 + 90.0)
        self.assertGreaterEqual(applied, 1)

        rec2 = self.store.get_train("T1598")
        assert rec2 is not None
        self.assertAlmostEqual(rec2.last_updated, t0)

        snap = self.store.get_snapshot(now=t0 + 90.0)
        self.assertEqual(snap.critical_conflict_count, 0)
        self.assertEqual(snap.advisory_conflict_count, 1)
        self.assertTrue(snap.active_conflicts[0].alert_id.endswith("_STALE_OBSTRUCTION"))


if __name__ == "__main__":
    unittest.main()
