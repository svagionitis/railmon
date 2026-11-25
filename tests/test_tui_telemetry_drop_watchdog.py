"""Unit tests for P0-10: Telemetry Drop Watchdog Resilience."""

import unittest

from train_tracker import TrainPosition
from tui.geo import (
    DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC,
    GeoPoint,
    InfrastructureType,
    SensorHealthState,
    compute_tunnel_grace_sec,
    evaluate_sensor_health,
)
from tui.mock_engine import StationNode, load_infrastructure
from tui.state import TrainStateStore


def _make_pos(
    train_id: str = "t-1",
    train_number: str = "IC52",
    speed: float = 120.0,
    lat: float = 39.5,
    lng: float = 22.3,
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
        next_station="Lianokladi",
        speed=speed,
        course=15.0,
        track_heading=15.0,
        lat=lat,
        lng=lng,
        altitude=45.0,
        delay=0.0,
        progress=0.15,
        chainage_m=12000,
        corridor="pathe-mainline",
        distance_to_destination_m=480000,
        timestamp="2026-11-23T12:00:00Z",
        schedule_id="sch-101",
    )


class TestTelemetryDropWatchdog(unittest.TestCase):
    """Test suite validating resilient drop watchdog, tunnel deadbands, and station preemption."""

    def setUp(self) -> None:
        self.stn_athens = StationNode(
            id="ath",
            code=1000,
            name_el="Αθήνα",
            name_en="Athens Central",
            point=GeoPoint(lat=37.9922, lng=23.7208),
            corridor="pathe-mainline",
            is_hub=True,
        )
        self.stn_thessaloniki = StationNode(
            id="skg",
            code=2000,
            name_el="Θεσσαλονίκη",
            name_en="Thessaloniki",
            point=GeoPoint(lat=40.6442, lng=22.9298),
            corridor="pathe-mainline",
            is_hub=True,
        )
        self.stn_local = StationNode(
            id="tith",
            code=1050,
            name_el="Τιθορέα",
            name_en="Tithorea",
            point=GeoPoint(lat=38.6015, lng=22.6680),
            corridor="pathe-mainline",
            is_hub=False,
        )
        self.stations = [self.stn_athens, self.stn_thessaloniki, self.stn_local]

        # Load infrastructure tunnels
        infra = load_infrastructure()
        self.tunnels = [
            f
            for f in infra.values()
            if getattr(f, "feature_type", None) == InfrastructureType.TUNNEL
        ]

    def test_tc_drp_01_open_line_default_180s_threshold(self) -> None:
        """TC-DRP-01: Verify open line silence at 150s is STALE, and at 181s triggers DROPPED."""
        now = 2000.0
        open_line_pt = GeoPoint(lat=39.5000, lng=22.3500)

        # 1. At 150 seconds silence: should be STALE (STL), not DROPPED (is_alert=False)
        stale_assess, _, _, _ = evaluate_sensor_health(
            point=open_line_pt,
            current_speed=120.0,
            last_updated=now - 150.0,
            last_moved_pt=open_line_pt,
            last_moved_time=now - 10.0,  # Recent kinematic motion prevents SENSOR FREEZE
            station_dwell_start=None,
            stations=self.stations,
            tunnels=self.tunnels,
            now=now,
        )
        self.assertEqual(stale_assess.state, SensorHealthState.STALE)
        self.assertEqual(stale_assess.short_code, "STL")
        self.assertFalse(stale_assess.is_alert)
        self.assertFalse(stale_assess.is_critical)

        # 2. At 181 seconds silence: exceeds 180s baseline, should transition to DROPPED (DRP)
        drop_assess, _, _, _ = evaluate_sensor_health(
            point=open_line_pt,
            current_speed=120.0,
            last_updated=now - 181.0,
            last_moved_pt=open_line_pt,
            last_moved_time=now - 181.0,
            station_dwell_start=None,
            stations=self.stations,
            tunnels=self.tunnels,
            now=now,
        )
        self.assertEqual(drop_assess.state, SensorHealthState.DROPPED)
        self.assertEqual(drop_assess.short_code, "DRP")
        self.assertTrue(drop_assess.is_alert)
        self.assertTrue(drop_assess.is_critical)
        self.assertIn("DROPPED", drop_assess.description)

    def test_tc_drp_02_kallidromo_tunnel_transit_grace(self) -> None:
        """TC-DRP-02: Place train inside Kallidromo Tunnel; 220s evaluates to TUNNEL_TRANSIT."""
        now = 2000.0
        # Coordinate inside Kallidromo Tunnel bore (lat=38.7620, lng=22.5320)
        kallidromo_mid_pt = GeoPoint(lat=38.7620, lng=22.5320)

        assess, _, _, _ = evaluate_sensor_health(
            point=kallidromo_mid_pt,
            current_speed=160.0,
            last_updated=now - 220.0,  # 220s silence (> 180s baseline, but < 300s grace)
            last_moved_pt=kallidromo_mid_pt,
            last_moved_time=now - 220.0,
            station_dwell_start=None,
            stations=self.stations,
            tunnels=self.tunnels,
            now=now,
        )
        self.assertEqual(assess.state, SensorHealthState.TUNNEL_TRANSIT)
        self.assertEqual(assess.short_code, "TUN")
        self.assertFalse(assess.is_alert)
        self.assertFalse(assess.is_critical)
        self.assertIn("TUNNEL TRANSIT", assess.description)
        self.assertIn("Kallidromo", assess.description)

    def test_tc_drp_03_tunnel_overdue_drop_escalation(self) -> None:
        """TC-DRP-03: Inside Kallidromo, silence exceeding grace period escalates to DROPPED."""
        now = 2000.0
        kallidromo_mid_pt = GeoPoint(lat=38.7620, lng=22.5320)

        # 400s silence exceeds Kallidromo grace window (~300s)
        assess, _, _, _ = evaluate_sensor_health(
            point=kallidromo_mid_pt,
            current_speed=160.0,
            last_updated=now - 400.0,
            last_moved_pt=kallidromo_mid_pt,
            last_moved_time=now - 400.0,
            station_dwell_start=None,
            stations=self.stations,
            tunnels=self.tunnels,
            now=now,
        )
        self.assertEqual(assess.state, SensorHealthState.DROPPED)
        self.assertEqual(assess.short_code, "DRP")
        self.assertTrue(assess.is_alert)
        self.assertTrue(assess.is_critical)
        self.assertIn("OVERDUE IN TUNNEL", assess.description)

    def test_tc_drp_04_terminal_hub_expanded_radius_dwell(self) -> None:
        """TC-DRP-04: Train 450m from Athens Central evaluates to STATION_DWELL after 240s."""
        now = 2000.0
        # Athens Central: lat=37.9922, lng=23.7208. Point ~450m north: lat ≈ 37.9962
        athens_platform_pt = GeoPoint(lat=37.9962, lng=23.7208)

        assess, _, _, dwell_start = evaluate_sensor_health(
            point=athens_platform_pt,
            current_speed=0.0,
            last_updated=now - 240.0,  # 4 minutes of silence at terminal platform
            last_moved_pt=athens_platform_pt,
            last_moved_time=now - 240.0,
            station_dwell_start=None,
            stations=self.stations,
            tunnels=self.tunnels,
            now=now,
        )
        self.assertEqual(assess.state, SensorHealthState.STATION_DWELL)
        self.assertEqual(assess.short_code, "DWL")
        self.assertFalse(assess.is_alert)
        self.assertFalse(assess.is_critical)
        self.assertIn("Athens Central", assess.description)
        self.assertIsNotNone(dwell_start)

    def test_tc_drp_05_terminal_approach_crawl_suppression(self) -> None:
        """TC-DRP-05: Crawling train (2.5 km/h) 300m from SKG suppresses drop after 200s silence."""
        now = 2000.0
        # Thessaloniki: lat=40.6442, lng=22.9298. Point ~300m west: lng ≈ 22.9262
        skg_throat_pt = GeoPoint(lat=40.6442, lng=22.9262)

        assess, _, _, _ = evaluate_sensor_health(
            point=skg_throat_pt,
            current_speed=2.5,  # Crawling into terminal platform throat
            last_updated=now - 200.0,  # 200s silence
            last_moved_pt=skg_throat_pt,
            last_moved_time=now - 200.0,
            station_dwell_start=None,
            stations=self.stations,
            tunnels=self.tunnels,
            now=now,
        )
        self.assertEqual(assess.state, SensorHealthState.STATION_DWELL)
        self.assertEqual(assess.short_code, "DWL")
        self.assertFalse(assess.is_alert)
        self.assertFalse(assess.is_critical)

    def test_tc_drp_06_terminal_bell_suppression_on_tunnel_dwell(self) -> None:
        """TC-DRP-06: Transitions to TUNNEL_TRANSIT and STATION_DWELL yield 0 dropped trains."""
        store = TrainStateStore()
        store.set_stations(self.stations)
        store.set_tunnels(self.tunnels)

        t0 = 1000.0
        # Train 1 inside Kallidromo
        pos1 = _make_pos(
            train_id="t-tun", train_number="IC50", speed=160.0, lat=38.7620, lng=22.5320
        )
        # Train 2 at Athens Central platform
        pos2 = _make_pos(
            train_id="t-dwl", train_number="IC52", speed=0.0, lat=37.9940, lng=23.7208
        )

        store.update_positions([pos1, pos2], virtual_now=t0)

        # Advance 210s later (both silent)
        t_future = t0 + 210.0
        store.evaluate_all_sensor_health(now=t_future)
        snap = store.get_snapshot(now=t_future)

        # Both should be safely in transit/dwell, NOT dropped
        self.assertEqual(snap.dropped_count, 0)
        self.assertEqual(len(snap.dropped_train_ids), 0)

        rec_tun = store.get_train("t-tun")
        assert rec_tun is not None and rec_tun.sensor_health is not None
        self.assertEqual(rec_tun.sensor_health.state, SensorHealthState.TUNNEL_TRANSIT)
        self.assertFalse(rec_tun.sensor_health.is_alert)

        rec_dwl = store.get_train("t-dwl")
        assert rec_dwl is not None and rec_dwl.sensor_health is not None
        self.assertEqual(rec_dwl.sensor_health.state, SensorHealthState.STATION_DWELL)
        self.assertFalse(rec_dwl.sensor_health.is_alert)

    def test_tc_drp_07_state_store_tunnel_injection(self) -> None:
        """TC-DRP-07: Verify TrainStateStore initializes tunnels and provides setters/getters."""
        store = TrainStateStore()
        tuns = store.get_tunnels()
        self.assertGreater(len(tuns), 0)
        tun_names = [t.name for t in tuns]
        self.assertIn("Kallidromo Tunnel", tun_names)

        # Test threshold getters/setters
        self.assertEqual(
            store.get_telemetry_drop_threshold(), DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC
        )
        store.set_telemetry_drop_threshold(240.0)
        self.assertEqual(store.get_telemetry_drop_threshold(), 240.0)

    def test_tc_drp_08_open_track_passing_station_fast(self) -> None:
        """TC-DRP-08: Passing (140 km/h) outside platform does not latch dwell on drop."""
        now = 2000.0
        # 400m from local station (which has 350m radius) at 140 km/h
        open_pt = GeoPoint(lat=38.6050, lng=22.6680)

        assess, _, _, _ = evaluate_sensor_health(
            point=open_pt,
            current_speed=140.0,
            last_updated=now - 200.0,
            last_moved_pt=open_pt,
            last_moved_time=now - 200.0,
            station_dwell_start=None,
            stations=self.stations,
            tunnels=self.tunnels,
            now=now,
        )
        # Outside 350m local radius and speed > 5 km/h -> genuine open-line drop
        self.assertEqual(assess.state, SensorHealthState.DROPPED)
        self.assertTrue(assess.is_alert)

    def test_tc_wdg_01_open_line_drop_reachable_under_5s_pruning(self) -> None:
        """TC-WDG-01: Open-line silent train survives 5s periodic pruning to reach DROPPED (180s) and evicts >600s."""
        store = TrainStateStore()
        store.set_stations(self.stations)
        store.set_tunnels(self.tunnels)
        t0 = 10000.0
        pos = _make_pos(train_id="t-open", train_number="IC52", speed=140.0, lat=39.5, lng=22.35)
        store.update_positions([pos], virtual_now=t0)

        for dt in range(5, 600, 5):
            self.assertEqual(store.prune_stale_trains(now=t0 + dt), 0, f"Prematurely pruned at dt={dt}s")
            if dt == 35:
                snap = store.get_snapshot(now=t0 + dt)
                self.assertEqual(snap.trains["t-open"].sensor_health.state, SensorHealthState.STALE)
                self.assertEqual(snap.dropped_count, 0)
            elif dt == 185:
                snap = store.get_snapshot(now=t0 + dt)
                self.assertEqual(snap.trains["t-open"].sensor_health.state, SensorHealthState.DROPPED)
                self.assertTrue(snap.trains["t-open"].sensor_health.is_alert)
                self.assertEqual(snap.dropped_count, 1)
            elif dt == 595:
                snap = store.get_snapshot(now=t0 + dt)
                self.assertEqual(snap.dropped_count, 1)

        self.assertEqual(store.prune_stale_trains(now=t0 + 605.0), 1)
        snap_end = store.get_snapshot(now=t0 + 605.0)
        self.assertEqual(snap_end.dropped_count, 0)
        self.assertNotIn("t-open", snap_end.trains)

    def test_tc_wdg_02_kallidromo_passenger_tunnel_lifecycle_under_5s_pruning(self) -> None:
        """TC-WDG-02: Kallidromo 160 km/h train survives 5s pruning through TUN -> OVERDUE IN TUNNEL -> eviction."""
        store = TrainStateStore()
        store.set_stations(self.stations)
        store.set_tunnels(self.tunnels)
        t0 = 20000.0
        pos = _make_pos(train_id="t-kal-ic", train_number="IC50", speed=160.0, lat=38.7620, lng=22.5320)
        store.update_positions([pos], virtual_now=t0)

        for dt in range(5, 600, 5):
            self.assertEqual(store.prune_stale_trains(now=t0 + dt), 0, f"Prematurely pruned at dt={dt}s")
            if dt in (35, 295):
                snap = store.get_snapshot(now=t0 + dt)
                self.assertEqual(snap.trains["t-kal-ic"].sensor_health.state, SensorHealthState.TUNNEL_TRANSIT)
                self.assertEqual(snap.dropped_count, 0)
            elif dt in (305, 595):
                snap = store.get_snapshot(now=t0 + dt)
                sh = snap.trains["t-kal-ic"].sensor_health
                self.assertEqual(sh.state, SensorHealthState.DROPPED)
                self.assertTrue(sh.is_alert)
                self.assertIn("OVERDUE IN TUNNEL", sh.description)
                self.assertEqual(snap.dropped_count, 1)

        self.assertEqual(store.prune_stale_trains(now=t0 + 605.0), 1)
        self.assertIsNone(store.get_train("t-kal-ic"))

    def test_tc_wdg_03_kallidromo_freight_and_slow_tunnel_extension(self) -> None:
        """TC-WDG-03: Freight (80 km/h, grace=549s) and slow (40 km/h, grace=1098s) transits are not pruned at 600s."""
        store = TrainStateStore()
        store.set_stations(self.stations)
        store.set_tunnels(self.tunnels)
        t0 = 30000.0
        pos_freight = _make_pos(train_id="t-frg", train_number="80512", speed=80.0, lat=38.7620, lng=22.5320)
        pos_slow = _make_pos(train_id="t-slow", train_number="80514", speed=40.0, lat=38.7620, lng=22.5320)
        store.update_positions([pos_freight, pos_slow], virtual_now=t0)

        # At 540s: both still in TUNNEL_TRANSIT and neither pruned
        self.assertEqual(store.prune_stale_trains(now=t0 + 540.0), 0)
        snap_540 = store.get_snapshot(now=t0 + 540.0)
        self.assertEqual(snap_540.trains["t-frg"].sensor_health.state, SensorHealthState.TUNNEL_TRANSIT)
        self.assertEqual(snap_540.trains["t-slow"].sensor_health.state, SensorHealthState.TUNNEL_TRANSIT)

        # At 605s (past 600s base TTL!): freight is OVERDUE IN TUNNEL (retained until ~849s), slow is still TUN
        self.assertEqual(store.prune_stale_trains(now=t0 + 605.0), 0)
        snap_605 = store.get_snapshot(now=t0 + 605.0)
        self.assertEqual(snap_605.trains["t-frg"].sensor_health.state, SensorHealthState.DROPPED)
        self.assertIn("OVERDUE IN TUNNEL", snap_605.trains["t-frg"].sensor_health.description)
        self.assertEqual(snap_605.trains["t-slow"].sensor_health.state, SensorHealthState.TUNNEL_TRANSIT)

        # At 855s: freight evicted (> 849.1s), slow still in TUNNEL_TRANSIT (< 1098.1s)
        self.assertEqual(store.prune_stale_trains(now=t0 + 855.0), 1)
        self.assertIsNone(store.get_train("t-frg"))
        self.assertIsNotNone(store.get_train("t-slow"))

        # At 1105s: slow escalates to OVERDUE IN TUNNEL; retained until ~1398s
        self.assertEqual(store.prune_stale_trains(now=t0 + 1105.0), 0)
        snap_1105 = store.get_snapshot(now=t0 + 1105.0)
        self.assertEqual(snap_1105.trains["t-slow"].sensor_health.state, SensorHealthState.DROPPED)
        self.assertIn("OVERDUE IN TUNNEL", snap_1105.trains["t-slow"].sensor_health.description)

        # At 1405s: slow cleanly evicted (> 1398.1s, <= 1800s cap)
        self.assertEqual(store.prune_stale_trains(now=t0 + 1405.0), 1)
        self.assertIsNone(store.get_train("t-slow"))

    def test_tc_wdg_04_terminated_station_dwell_prompt_eviction(self) -> None:
        """TC-WDG-04: Silent stationary train at terminal stays DWL through 235s and evicts at 245s."""
        store = TrainStateStore()
        store.set_stations(self.stations)
        store.set_tunnels(self.tunnels)
        t0 = 40000.0
        pos_ath = _make_pos(train_id="t-ath", train_number="IC54", speed=0.0, lat=37.9922, lng=23.7208)
        store.update_positions([pos_ath], virtual_now=t0)

        for dt in range(5, 240, 5):
            self.assertEqual(store.prune_stale_trains(now=t0 + dt), 0)
        snap_235 = store.get_snapshot(now=t0 + 235.0)
        self.assertEqual(snap_235.trains["t-ath"].sensor_health.state, SensorHealthState.STATION_DWELL)
        self.assertEqual(snap_235.dropped_count, 0)

        self.assertEqual(store.prune_stale_trains(now=t0 + 245.0), 1)
        self.assertIsNone(store.get_train("t-ath"))

    def test_tc_wdg_05_dynamic_drop_threshold_floor_safety(self) -> None:
        """TC-WDG-05: Raising telemetry_drop_threshold_sec to 400s extends open-line eviction floor to 700s."""
        store = TrainStateStore()
        store.set_stations(self.stations)
        store.set_tunnels(self.tunnels)
        store.set_telemetry_drop_threshold(400.0)
        t0 = 50000.0
        pos = _make_pos(train_id="t-hi", train_number="IC56", speed=120.0, lat=39.5, lng=22.35)
        store.update_positions([pos], virtual_now=t0)

        # Retained past 600s and reaches DROPPED at 405s; stays through 695s, evicted at 705s
        self.assertEqual(store.prune_stale_trains(now=t0 + 405.0), 0)
        snap_405 = store.get_snapshot(now=t0 + 405.0)
        self.assertEqual(snap_405.trains["t-hi"].sensor_health.state, SensorHealthState.DROPPED)
        self.assertEqual(store.prune_stale_trains(now=t0 + 695.0), 0)
        self.assertEqual(store.prune_stale_trains(now=t0 + 705.0), 1)

    def test_tc_wdg_06_custom_override_and_compute_tunnel_grace_hygiene(self) -> None:
        """TC-WDG-06: Explicit stale_timeout_sec/max_age_sec overrides work and compute_tunnel_grace_sec handles NaN/Inf/bool."""
        custom_store = TrainStateStore(stale_timeout_sec=30.0)
        t0 = 60000.0
        pos = _make_pos(train_id="t-cust", train_number="IC58", speed=120.0, lat=39.5, lng=22.35)
        custom_store.update_positions([pos], virtual_now=t0)
        self.assertEqual(custom_store.prune_stale_trains(now=t0 + 25.0), 0)
        self.assertEqual(custom_store.prune_stale_trains(now=t0 + 35.0), 1)

        # Explicit max_age_sec overrides default live state-aware TTL
        default_store = TrainStateStore()
        default_store.update_positions([pos], virtual_now=t0)
        self.assertEqual(default_store.prune_stale_trains(max_age_sec=15.0, now=t0 + 20.0), 1)

        # NaN / Inf / bool / negative / None length and speed inputs never crash
        class _DummyTunnel:
            def __init__(self, length_m: object) -> None:
                self.length_m = length_m

        for bad_len in (None, 0, -100.0, float("nan"), float("inf"), True):
            for bad_spd in (None, 0.0, -20.0, float("nan"), float("inf"), False):
                grace = compute_tunnel_grace_sec(_DummyTunnel(bad_len), bad_spd)  # type: ignore[arg-type]
                self.assertGreaterEqual(grace, 300.0)


if __name__ == "__main__":
    unittest.main()
