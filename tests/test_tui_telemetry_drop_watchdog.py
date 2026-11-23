"""Unit tests for P0-10: Telemetry Drop Watchdog Resilience."""

import unittest

from train_tracker import TrainPosition
from tui.geo import (
    DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC,
    GeoPoint,
    InfrastructureType,
    SensorHealthState,
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


if __name__ == "__main__":
    unittest.main()
