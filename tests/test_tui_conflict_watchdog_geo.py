"""Unit tests for F-5.1 Corridor Headway & Opposing Train Conflict Watchdog kinematics and geo algorithms."""

import unittest
from typing import List, Optional

from train_tracker import TrainPosition
from tui.geo import (
    ConflictAlert,
    ConflictSeverity,
    ConflictType,
    ConflictWatchdogConfig,
    GeoPoint,
    compute_closing_velocity_kmh,
    compute_forward_bearing,
    compute_relative_heading_delta,
    evaluate_train_conflicts,
    find_station_passing_meet,
    haversine_distance_km,
    resolve_train_corridor,
)
from tui.mock_engine import StationNode, load_tracks


def _make_pos(
    train_id: str,
    train_number: str,
    lat: float,
    lng: float,
    speed: float,
    course: float,
    corridor: Optional[str] = None,
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


class TestConflictWatchdogGeo(unittest.TestCase):
    """Test suite for kinematic vector calculations and conflict evaluation."""

    def setUp(self) -> None:
        self.config = ConflictWatchdogConfig()
        # Reference station
        self.stn_avlis = StationNode(
            id="AVL",
            code=1201,
            name_el="Αυλίδα",
            name_en="Avlis",
            point=GeoPoint(lat=38.4180, lng=23.6120),
            corridor="proastiakos-chalkida",
            is_hub=False,
        )
        self.stn_larissa = StationNode(
            id="LAR",
            code=1100,
            name_el="Λάρισα",
            name_en="Larissa",
            point=GeoPoint(lat=39.6385, lng=22.4241),
            corridor="pathe-mainline",
            is_hub=True,
        )
        self.stn_athens = StationNode(
            id="ath",
            code=1001,
            name_el="Αθήνα",
            name_en="Athens Central",
            point=GeoPoint(lat=37.9926, lng=23.7203),
            corridor="pathe-mainline",
            is_hub=True,
        )

    def test_compute_forward_bearing(self) -> None:
        """Verify spherical forward azimuth calculations."""
        # Due North
        p1 = GeoPoint(lat=38.0, lng=23.0)
        p_north = GeoPoint(lat=39.0, lng=23.0)
        self.assertAlmostEqual(compute_forward_bearing(p1, p_north), 0.0, delta=0.5)

        # Due East
        p_east = GeoPoint(lat=38.0, lng=24.0)
        self.assertAlmostEqual(compute_forward_bearing(p1, p_east), 90.0, delta=0.5)

        # Due South
        p_south = GeoPoint(lat=37.0, lng=23.0)
        self.assertAlmostEqual(compute_forward_bearing(p1, p_south), 180.0, delta=0.5)

        # Due West
        p_west = GeoPoint(lat=38.0, lng=22.0)
        self.assertAlmostEqual(compute_forward_bearing(p1, p_west), 270.0, delta=0.5)

    def test_compute_relative_heading_delta(self) -> None:
        """Verify normalized angular divergence calculation."""
        # Co-directional
        self.assertAlmostEqual(compute_relative_heading_delta(0.0, 10.0), 10.0)
        self.assertAlmostEqual(compute_relative_heading_delta(355.0, 5.0), 10.0)

        # Opposing
        self.assertAlmostEqual(compute_relative_heading_delta(0.0, 180.0), 180.0)
        self.assertAlmostEqual(compute_relative_heading_delta(350.0, 170.0), 180.0)
        self.assertAlmostEqual(compute_relative_heading_delta(45.0, 225.0), 180.0)

        # Orthogonal
        self.assertAlmostEqual(compute_relative_heading_delta(0.0, 90.0), 90.0)

    def test_compute_closing_velocity_kmh(self) -> None:
        """Verify mutual closing speed projections."""
        p1 = GeoPoint(lat=38.0, lng=23.0)
        p2 = GeoPoint(lat=38.1, lng=23.0)  # p2 is due North of p1

        # Opposing trains moving toward each other: p1 moving North (0°), p2 moving South (180°)
        v_close = compute_closing_velocity_kmh(p1, 100.0, 0.0, p2, 120.0, 180.0)
        self.assertAlmostEqual(v_close, 220.0, delta=1.0)

        # Opposing trains moving away from each other: p1 moving South (180°), p2 moving North (0°)
        v_diverge = compute_closing_velocity_kmh(p1, 100.0, 180.0, p2, 120.0, 0.0)
        self.assertAlmostEqual(v_diverge, -220.0, delta=1.0)

        # Following trains: p1 trailing at 140 km/h, p2 leading at 100 km/h
        v_following = compute_closing_velocity_kmh(p1, 140.0, 0.0, p2, 100.0, 0.0)
        self.assertAlmostEqual(v_following, 40.0, delta=1.0)

    def test_single_track_opposing_collision_critical(self) -> None:
        """Verify critical alert generated for opposing trains on single-track corridor."""
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

        dist_km = haversine_distance_km(
            GeoPoint(lat=t1.lat, lng=t1.lng),
            GeoPoint(lat=t2.lat, lng=t2.lng),
        )
        self.assertLess(dist_km, 10.0)

        alerts = evaluate_train_conflicts(
            active_trains=[t1, t2],
            stations=[self.stn_avlis],
            config=self.config,
        )

        # Should generate a conflict alert
        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.severity, ConflictSeverity.CRITICAL)
        self.assertEqual(alert.conflict_type, ConflictType.OPPOSING_COLLISION)
        self.assertTrue(alert.is_single_track)
        self.assertIsNotNone(alert.time_to_collision_sec)
        self.assertGreater(alert.closing_speed_kmh, 150.0)
        self.assertIn("1597", alert.description)
        self.assertIn("1598", alert.description)

    def test_double_track_opposing_trains_suppressed(self) -> None:
        """Verify normal two-way operations on double track do not generate false collision alarms."""
        t1 = _make_pos(
            train_id="IC52",
            train_number="IC 52",
            lat=39.5000,
            lng=22.4000,
            speed=158.0,
            course=355.0,
            corridor="pathe-mainline",
        )
        t2 = _make_pos(
            train_id="IC59",
            train_number="IC 59",
            lat=39.5500,
            lng=22.4050,
            speed=152.0,
            course=175.0,
            corridor="pathe-mainline",
        )

        alerts = evaluate_train_conflicts(
            active_trains=[t1, t2],
            stations=[self.stn_larissa],
            config=self.config,
        )

        # Because pathe-mainline is double-track and not in single-line working, no opposing collision
        self.assertEqual(len(alerts), 0)

    def test_mainline_single_line_working_triggers_critical(self) -> None:
        """Verify that single-line working flag on double-track corridor activates opposing collision checks."""
        t1 = _make_pos(
            train_id="IC52",
            train_number="IC 52",
            lat=39.5000,
            lng=22.4000,
            speed=158.0,
            course=355.0,
            corridor="pathe-mainline",
        )
        t2 = _make_pos(
            train_id="IC59",
            train_number="IC 59",
            lat=39.5500,
            lng=22.4050,
            speed=152.0,
            course=175.0,
            corridor="pathe-mainline",
        )

        # Enable single-line working for PATHE
        cfg = ConflictWatchdogConfig(
            single_line_working_corridors={"pathe-mainline"}
        )
        alerts = evaluate_train_conflicts(
            active_trains=[t1, t2],
            stations=[self.stn_larissa],
            config=cfg,
        )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, ConflictSeverity.CRITICAL)
        self.assertEqual(alerts[0].conflict_type, ConflictType.OPPOSING_COLLISION)

    def test_station_passing_meet_suppression(self) -> None:
        """Verify opposing meet where one train is halted at a station siding is classified as STATION_MEET."""
        # Train 1 is dwelling at Avlis station (speed 0, within 100m of Avlis centroid)
        t1_dwelling = _make_pos(
            train_id="T1597",
            train_number="1597",
            lat=self.stn_avlis.point.lat,
            lng=self.stn_avlis.point.lng,
            speed=0.0,
            course=30.0,
            corridor="proastiakos-chalkida",
        )
        # Train 2 is approaching station from 3 km away
        t2_approaching = _make_pos(
            train_id="T1598",
            train_number="1598",
            lat=self.stn_avlis.point.lat + 0.025,
            lng=self.stn_avlis.point.lng + 0.015,
            speed=80.0,
            course=210.0,
            corridor="proastiakos-chalkida",
        )

        meet_stn = find_station_passing_meet(
            t1_dwelling, t2_approaching, stations=[self.stn_avlis], radius_m=350.0
        )
        self.assertEqual(meet_stn, "Avlis")

        alerts = evaluate_train_conflicts(
            active_trains=[t1_dwelling, t2_approaching],
            stations=[self.stn_avlis],
            config=self.config,
        )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, ConflictSeverity.ADVISORY)
        self.assertEqual(alerts[0].conflict_type, ConflictType.STATION_MEET)
        self.assertEqual(alerts[0].station_proximity_name, "Avlis")

    def test_headway_violation_co_directional(self) -> None:
        """Verify headway compression and critical headway alerts for co-directional trains."""
        # Leading train
        t_lead = _make_pos(
            train_id="IC52",
            train_number="IC 52",
            lat=39.5200,
            lng=22.4000,
            speed=100.0,
            course=355.0,
            corridor="pathe-mainline",
        )
        # Trailing train 1.8 km behind (Warning threshold: 1.0 to 2.5 km)
        t_trail_warn = _make_pos(
            train_id="IC54",
            train_number="IC 54",
            lat=39.5040,
            lng=22.3990,
            speed=140.0,
            course=355.0,
            corridor="pathe-mainline",
        )

        alerts = evaluate_train_conflicts(
            active_trains=[t_lead, t_trail_warn],
            stations=[],
            config=self.config,
        )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, ConflictSeverity.WARNING)
        self.assertEqual(alerts[0].conflict_type, ConflictType.HEADWAY_VIOLATION)

        # Critical headway breach (0.5 km apart)
        t_trail_crit = _make_pos(
            train_id="IC54",
            train_number="IC 54",
            lat=39.5160,
            lng=22.3998,
            speed=120.0,
            course=355.0,
            corridor="pathe-mainline",
        )
        alerts_crit = evaluate_train_conflicts(
            active_trains=[t_lead, t_trail_crit],
            stations=[],
            config=self.config,
        )
        self.assertEqual(len(alerts_crit), 1)
        self.assertEqual(alerts_crit[0].severity, ConflictSeverity.CRITICAL)
        self.assertEqual(alerts_crit[0].conflict_type, ConflictType.HEADWAY_VIOLATION)

    def test_different_corridors_isolation(self) -> None:
        """Verify trains on different corridors in geographic proximity do not trigger false conflicts."""
        # Train on PATHE near Inoi junction
        t_pathe = _make_pos(
            train_id="IC52",
            train_number="IC 52",
            lat=38.3242,
            lng=23.3186,
            speed=140.0,
            course=350.0,
            corridor="pathe-mainline",
        )
        # Train on Chalkida branch near Inoi junction
        t_chalkida = _make_pos(
            train_id="1597",
            train_number="1597",
            lat=38.3245,
            lng=23.3190,
            speed=60.0,
            course=170.0,
            corridor="proastiakos-chalkida",
        )

        alerts = evaluate_train_conflicts(
            active_trains=[t_pathe, t_chalkida],
            stations=[],
            config=self.config,
        )
        # Partitioned into different corridors -> no conflict
        self.assertEqual(len(alerts), 0)

    def test_station_parallel_track_headway_demoted_to_advisory(self) -> None:
        """TC-CNF-01: Verify headway breach on adjacent station tracks is demoted to ADVISORY."""
        t1 = _make_pos(
            train_id="2232",
            train_number="2232",
            lat=37.9926,
            lng=23.7203,
            speed=2.0,
            course=355.0,
            corridor="pathe-mainline",
        )
        # Train on adjacent platform track (30m away)
        t2 = _make_pos(
            train_id="1597",
            train_number="1597",
            lat=37.9928,
            lng=23.7205,
            speed=0.0,
            course=355.0,
            corridor="pathe-mainline",
        )

        alerts = evaluate_train_conflicts(
            active_trains=[t1, t2],
            stations=[self.stn_athens],
            config=self.config,
        )

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.severity, ConflictSeverity.ADVISORY)
        self.assertEqual(alert.conflict_type, ConflictType.STATION_MEET)
        self.assertEqual(alert.station_proximity_name, "Athens Central")
        self.assertIn("Station headway alignment at Athens Central", alert.description)

    def test_open_track_headway_active_closing_critical(self) -> None:
        """TC-CNF-02: Verify rapid closing speed on open single line triggers CRITICAL."""
        t_lead = _make_pos(
            train_id="T1597",
            train_number="1597",
            lat=38.4100,
            lng=23.6050,
            speed=60.0,
            course=30.0,
            corridor="proastiakos-chalkida",
        )
        t_trail = _make_pos(
            train_id="T1599",
            train_number="1599",
            lat=38.4050,
            lng=23.6020,
            speed=100.0,
            course=30.0,
            corridor="proastiakos-chalkida",
        )

        alerts = evaluate_train_conflicts(
            active_trains=[t_lead, t_trail],
            stations=[],
            config=self.config,
        )

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.severity, ConflictSeverity.CRITICAL)
        self.assertEqual(alert.conflict_type, ConflictType.HEADWAY_VIOLATION)
        self.assertIn("CRITICAL HEADWAY BREACH", alert.description)

    def test_double_track_headway_closing_speed_gating(self) -> None:
        """TC-CNF-06: Verify headway when spacing is expanding/steady is demoted to WARNING."""
        # Leading train at 120 km/h, trailing train slower at 90 km/h (closing speed <= 0)
        t_lead = _make_pos(
            train_id="IC52",
            train_number="IC 52",
            lat=39.5200,
            lng=22.4000,
            speed=120.0,
            course=355.0,
            corridor="pathe-mainline",
        )
        t_trail = _make_pos(
            train_id="IC54",
            train_number="IC 54",
            lat=39.5165,
            lng=22.3998,
            speed=90.0,
            course=355.0,
            corridor="pathe-mainline",
        )

        alerts = evaluate_train_conflicts(
            active_trains=[t_lead, t_trail],
            stations=[],
            config=self.config,
        )

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        # Not CRITICAL because trailing train is moving slower than leading train
        self.assertEqual(alert.severity, ConflictSeverity.WARNING)
        self.assertEqual(alert.conflict_type, ConflictType.HEADWAY_VIOLATION)

    def test_resolve_train_corridor_geometric_snapping(self) -> None:
        """TC-CNF-03: Verify untagged trains dynamically snap to track polylines."""
        tracks = load_tracks()
        # Near Larissa on PATHE mainline
        t_larissa = _make_pos(
            train_id="IC52",
            train_number="IC 52",
            lat=39.6400,
            lng=22.4200,
            speed=80.0,
            course=0.0,
            corridor=None,
        )
        # Near Volos on Thessaly East Branch (60 km away)
        t_volos = _make_pos(
            train_id="1574",
            train_number="1574",
            lat=39.3620,
            lng=22.9420,
            speed=60.0,
            course=90.0,
            corridor=None,
        )

        corr_larissa = resolve_train_corridor(t_larissa, tracks_data=tracks)
        corr_volos = resolve_train_corridor(t_volos, tracks_data=tracks)

        self.assertEqual(corr_larissa, "pathe-mainline")
        self.assertEqual(corr_volos, "thessaly-volos")

        # In evaluate_train_conflicts with tracks, they are partitioned cleanly
        alerts = evaluate_train_conflicts(
            active_trains=[t_larissa, t_volos],
            tracks_data=tracks,
            stations=[],
            config=self.config,
        )
        self.assertEqual(len(alerts), 0)

    def test_resolve_train_corridor_unassigned_isolation(self) -> None:
        """TC-CNF-04: Off-grid untagged trains map to unassigned and are isolated."""
        tracks = load_tracks()
        # Train far in the Aegean Sea
        t_offgrid = _make_pos(
            train_id="LOST1",
            train_number="LOST",
            lat=37.0,
            lng=25.0,
            speed=0.0,
            course=0.0,
            corridor=None,
        )
        cid = resolve_train_corridor(t_offgrid, tracks_data=tracks)
        self.assertEqual(cid, "unassigned")

        alerts = evaluate_train_conflicts(
            active_trains=[t_offgrid],
            tracks_data=tracks,
            stations=[],
            config=self.config,
        )
        self.assertEqual(len(alerts), 0)

    def test_hub_throat_passing_loop_extent(self) -> None:
        """TC-CNF-05: Hub terminal throat uses 650m radius (exceeding intermediate 350m)."""
        # Train dwelling 520m North of Athens Central centroid
        t_dwelling = _make_pos(
            train_id="2232",
            train_number="2232",
            lat=self.stn_athens.point.lat + 0.0045,  # ~500m North
            lng=self.stn_athens.point.lng,
            speed=0.0,
            course=0.0,
            corridor="pathe-mainline",
        )
        # Train approaching station from 3 km away
        t_approaching = _make_pos(
            train_id="1597",
            train_number="1597",
            lat=self.stn_athens.point.lat + 0.025,
            lng=self.stn_athens.point.lng,
            speed=70.0,
            course=180.0,
            corridor="pathe-mainline",
        )

        meet_stn = find_station_passing_meet(
            t_dwelling, t_approaching, stations=[self.stn_athens], radius_m=350.0, terminal_radius_m=650.0
        )
        self.assertEqual(meet_stn, "Athens Central")

        # Under single-line working on PATHE, station meet demotes to ADVISORY
        cfg = ConflictWatchdogConfig(single_line_working_corridors={"pathe-mainline"})
        alerts = evaluate_train_conflicts(
            active_trains=[t_dwelling, t_approaching],
            stations=[self.stn_athens],
            config=cfg,
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, ConflictSeverity.ADVISORY)
        self.assertEqual(alerts[0].conflict_type, ConflictType.STATION_MEET)

    def test_stale_ghost_train_demoted_to_advisory(self) -> None:
        """TC-CNF-07: Opposing approach against stale/dropped train is demoted to ADVISORY."""
        now = 1700000000.0
        t1_live = _make_pos(
            train_id="1597",
            train_number="1597",
            lat=38.4000,
            lng=23.6000,
            speed=90.0,
            course=30.0,
            corridor="proastiakos-chalkida",
        )
        # Simulate last_updated on live train
        t1_live.last_updated = now  # type: ignore[attr-defined]

        # Stale train silent for 120s
        t2_stale = _make_pos(
            train_id="1598",
            train_number="1598",
            lat=38.4180,
            lng=23.6120,
            speed=0.0,
            course=210.0,
            corridor="proastiakos-chalkida",
        )
        t2_stale.last_updated = now - 120.0  # type: ignore[attr-defined]

        alerts = evaluate_train_conflicts(
            active_trains=[t1_live, t2_stale],
            stations=[],
            config=self.config,
            now=now,
        )

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        self.assertEqual(alert.severity, ConflictSeverity.ADVISORY)
        self.assertIn("Stale telemetry track occupancy caution", alert.description)


if __name__ == "__main__":
    unittest.main()
