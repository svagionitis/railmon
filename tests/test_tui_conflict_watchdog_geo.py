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
    _normalize_corridor_key,
    compute_closing_velocity_kmh,
    compute_forward_bearing,
    compute_relative_heading_delta,
    evaluate_train_conflicts,
    find_station_passing_meet,
    haversine_distance_km,
    resolve_train_corridor,
)
from tui.mock_engine import MockTelemetryEngine, StationNode, load_tracks
from tui.state import TrainRecord


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

    def test_tc_wdg_07_normalize_corridor_key_anti_hijack_matrix(self) -> None:
        """TC-WDG-07: Single-track corridors and live API codes never hijack to double-track mainlines."""
        expected = {
            "PATHE": "pathe-mainline",
            "pathe-mainline": "pathe-mainline",
            "E85": "pathe-mainline",
            "MAINLINE": "pathe-mainline",
            "ATHLIA": "pathe-mainline",
            "THESLAR": "pathe-mainline",
            "Athens - Thessaloniki": "pathe-mainline",
            "PIRAIR": "proastiakos-airport-kiato",
            "KIATO": "proastiakos-airport-kiato",
            "Airport - Kiato": "proastiakos-airport-kiato",
            "Athina - Airport": "proastiakos-airport-kiato",
            "Athina - Kiato": "proastiakos-airport-kiato",
            "proastiakos-chalkida": "proastiakos-chalkida",
            "proastiakos_chalkida": "proastiakos-chalkida",
            "Proastiakos Chalkida": "proastiakos-chalkida",
            "CHALKIDA": "proastiakos-chalkida",
            "Athina - Chalkida": "proastiakos-chalkida",
            "Athens - Chalkida": "proastiakos-chalkida",
            "ATH-HAL": "proastiakos-chalkida",
            "thessaloniki-idomeni": "thessaloniki-idomeni",
            "thessaloniki_idomeni": "thessaloniki-idomeni",
            "Thessaloniki - Idomeni": "thessaloniki-idomeni",
            "IDOMENI": "thessaloniki-idomeni",
            "thessaly-volos": "thessaly-volos",
            "thessaly_volos": "thessaly-volos",
            "Thessaly Volos": "thessaly-volos",
            "Larissa - Volos": "thessaly-volos",
            "thessaly-kalambaka": "thessaly-kalambaka",
            "thessaly_kalambaka": "thessaly-kalambaka",
            "Thessaly Kalambaka": "thessaly-kalambaka",
            "Palaiofarsalos - Kalambaka": "thessaly-kalambaka",
            "THESFLOR": "western-macedonia",
            "Thessaloniki - Edessa - Florina": "western-macedonia",
            "Platy - Florina": "western-macedonia",
            "MACEDONIA": "western-macedonia",
            "Thessaloniki - Alexandroupoli": "northern-corridor",
            "Thessaloniki-Alexandroupoli": "northern-corridor",
            "ALEXOR": "northern-corridor",
            "Eastern Macedonia & Thrace": "northern-corridor",
            "THESSALY": "thessaly",
            "PROASTIAKOS": "proastiakos",
            "PROASTIAKOS_ATH": "proastiakos",
            "NORTHERN": "northern",
        }
        for raw, exp in expected.items():
            with self.subTest(raw=raw):
                self.assertEqual(_normalize_corridor_key(raw), exp)

    def test_tc_wdg_08_mock_engine_all_11_trains_resolve_exact_track(self) -> None:
        """TC-WDG-08: All 11 MockTelemetryEngine fleet trains resolve to their exact physical track."""
        tracks = load_tracks()
        engine = MockTelemetryEngine()
        self.assertEqual(len(engine.trains), 11)
        for sim in engine.trains:
            pos = sim.step(1.0)
            resolved = resolve_train_corridor(pos, tracks_data=tracks)
            with self.subTest(train=sim.id, tag=pos.corridor):
                self.assertEqual(resolved, sim.track.corridor_id)

    def test_tc_wdg_09_single_track_head_on_collision_restored(self) -> None:
        """TC-WDG-09: Opposing trains on misclassified single-track corridors trigger CRITICAL; Kiato stays suppressed."""
        tracks = load_tracks()
        single_track_scenarios = [
            ("THESSALY (Volos)", "THESSALY", 39.5000, 22.6100, 39.4700, 22.6500, 130.0, 310.0, "thessaly-volos"),
            ("thessaloniki_idomeni", "thessaloniki_idomeni", 41.0500, 22.5480, 41.0800, 22.5360, 340.0, 160.0, "thessaloniki-idomeni"),
            ("Thessaloniki - Alexandroupoli", "Thessaloniki - Alexandroupoli", 41.1200, 24.3000, 41.1300, 24.3400, 75.0, 255.0, "northern-corridor"),
            ("THESFLOR", "THESFLOR", 40.7600, 21.7750, 40.7400, 21.7500, 220.0, 40.0, "western-macedonia"),
            ("PROASTIAKOS (Chalkida)", "PROASTIAKOS", 38.3925, 23.6115, 38.4180, 23.6050, 350.0, 170.0, "proastiakos-chalkida"),
        ]
        for label, corr_tag, lat1, lng1, lat2, lng2, c1, c2, expected_cid in single_track_scenarios:
            with self.subTest(scenario=label):
                t1 = _make_pos("T1", "101", lat1, lng1, speed=80.0, course=c1, corridor=corr_tag)
                t2 = _make_pos("T2", "102", lat2, lng2, speed=80.0, course=c2, corridor=corr_tag)
                alerts = evaluate_train_conflicts([t1, t2], tracks_data=tracks, stations=[], config=self.config)
                self.assertEqual(len(alerts), 1)
                self.assertEqual(alerts[0].severity, ConflictSeverity.CRITICAL)
                self.assertEqual(alerts[0].conflict_type, ConflictType.OPPOSING_COLLISION)
                self.assertTrue(alerts[0].is_single_track)
                self.assertEqual(alerts[0].corridor_id, expected_cid)

        # Opposing trains tagged PROASTIAKOS on double-track Airport-Kiato (near Megara) stay suppressed
        k1 = _make_pos("K1", "1201", 37.9944, 23.3361, speed=100.0, course=250.0, corridor="PROASTIAKOS")
        k2 = _make_pos("K2", "1202", 37.9840, 23.3020, speed=100.0, course=70.0, corridor="PROASTIAKOS")
        self.assertEqual(evaluate_train_conflicts([k1, k2], tracks_data=tracks, stations=[], config=self.config), [])

    def test_tc_wdg_10_shared_trunk_tie_breaking_ska_and_kilkis(self) -> None:
        """TC-WDG-10: Shared-trunk ties at SKA and Kilkis disambiguate cleanly via route metadata."""
        tracks = load_tracks()
        # SKA Hub (38.0744, 23.7381): shared by PATHE, Airport-Kiato, and Chalkida
        p_ska_chal = TrainPosition(
            id="s1", train_number="1314", locomotive_number="460", name="1314",
            train_type="Proastiakos", service_type="Suburban", status="In Transit",
            origin="Αθήνα", destination="Χαλκίδα", next_station="Αυλώνας",
            speed=60.0, course=30.0, track_heading=30.0, lat=38.0744, lng=23.7381,
            altitude=130.0, delay=0, progress=0.2, chainage_m=0, corridor="PROASTIAKOS",
            distance_to_destination_m=60000, timestamp="2026-11-25T12:00:00Z", schedule_id="s1",
        )
        p_ska_kiato = TrainPosition(
            id="s2", train_number="2232", locomotive_number="460", name="2232",
            train_type="Proastiakos", service_type="Suburban", status="In Transit",
            origin="Αεροδρόμιο", destination="Κιάτο", next_station="Άνω Λιόσια",
            speed=60.0, course=270.0, track_heading=270.0, lat=38.0744, lng=23.7381,
            altitude=130.0, delay=0, progress=0.3, chainage_m=0, corridor="PROASTIAKOS",
            distance_to_destination_m=90000, timestamp="2026-11-25T12:00:00Z", schedule_id="s2",
        )
        p_ska_ic = TrainPosition(
            id="s3", train_number="IC 52", locomotive_number="120", name="IC 52",
            train_type="InterCity", service_type="Passenger", status="In Transit",
            origin="Αθήνα", destination="Θεσσαλονίκη", next_station="Οινόη",
            speed=90.0, course=30.0, track_heading=30.0, lat=38.0744, lng=23.7381,
            altitude=130.0, delay=0, progress=0.1, chainage_m=0, corridor=None,
            distance_to_destination_m=480000, timestamp="2026-11-25T12:00:00Z", schedule_id="s3",
        )
        self.assertEqual(resolve_train_corridor(p_ska_chal, tracks_data=tracks), "proastiakos-chalkida")
        self.assertEqual(resolve_train_corridor(p_ska_kiato, tracks_data=tracks), "proastiakos-airport-kiato")
        self.assertEqual(resolve_train_corridor(p_ska_ic, tracks_data=tracks), "pathe-mainline")

        # Kilkis (40.9917, 22.8750): shared by thessaloniki-idomeni and northern-corridor
        p_kil_ido = TrainPosition(
            id="k1", train_number="361", locomotive_number="120", name="361",
            train_type="International", service_type="Passenger", status="In Transit",
            origin="Θεσσαλονίκη", destination="Ειδομένη", next_station="Πολύκαστρο",
            speed=80.0, course=300.0, track_heading=300.0, lat=40.9917, lng=22.8750,
            altitude=260.0, delay=0, progress=0.5, chainage_m=0, corridor="NORTHERN",
            distance_to_destination_m=40000, timestamp="2026-11-25T12:00:00Z", schedule_id="k1",
        )
        p_kil_alex = TrainPosition(
            id="k2", train_number="600", locomotive_number="220", name="600",
            train_type="Regional", service_type="Passenger", status="In Transit",
            origin="Θεσσαλονίκη", destination="Αλεξανδρούπολη", next_station="Σέρρες",
            speed=80.0, course=45.0, track_heading=45.0, lat=40.9917, lng=22.8750,
            altitude=260.0, delay=0, progress=0.2, chainage_m=0, corridor="NORTHERN",
            distance_to_destination_m=280000, timestamp="2026-11-25T12:00:00Z", schedule_id="k2",
        )
        self.assertEqual(resolve_train_corridor(p_kil_ido, tracks_data=tracks), "thessaloniki-idomeni")
        self.assertEqual(resolve_train_corridor(p_kil_alex, tracks_data=tracks), "northern-corridor")

    def test_tc_wdg_11_unknown_corridor_fail_safe_single_track(self) -> None:
        """TC-WDG-11: Unrecognized regional corridors fail-safely default to single-track."""
        t1 = _make_pos("U1", "901", 35.1000, 25.1000, speed=70.0, course=0.0, corridor="unknown-regional-spur")
        t2 = _make_pos("U2", "902", 35.1300, 25.1000, speed=70.0, course=180.0, corridor="unknown-regional-spur")
        alerts = evaluate_train_conflicts([t1, t2], tracks_data={}, stations=[], config=self.config)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0].severity, ConflictSeverity.CRITICAL)
        self.assertEqual(alerts[0].conflict_type, ConflictType.OPPOSING_COLLISION)
        self.assertTrue(alerts[0].is_single_track)

    def test_tc_wdg_15_stale_ghost_envelope_zero_false_distant_headway(self) -> None:
        """TC-WDG-15: Co-directional trains 15 km apart with one stale train produce 0 alerts (not STALE_HEADWAY)."""
        now = 1700000000.0
        t1 = _make_pos("IC50", "IC 50", 39.4000, 22.3500, speed=140.0, course=0.0, corridor="pathe-mainline")
        t2 = _make_pos("IC52", "IC 52", 39.5350, 22.3500, speed=140.0, course=0.0, corridor="pathe-mainline")
        # Distance ~15 km (> 2.5 km headway warning, <= 20 km outer check)
        alerts = evaluate_train_conflicts(
            [(t1, now), (t2, now - 90.0)],
            stations=[],
            config=self.config,
            now=now,
        )
        self.assertEqual(alerts, [])

    def test_tc_wdg_16_stale_ghost_envelope_zero_false_diverging_opposing(self) -> None:
        """TC-WDG-16: Diverging opposing trains with one stale train produce 0 alerts (not STALE_OBSTRUCTION)."""
        now = 1700000000.0
        # t1 south moving South (180°), t2 north moving North (0°) -> closing_spd < 0
        t1 = _make_pos("1597", "1597", 38.3800, 23.6000, speed=80.0, course=180.0, corridor="proastiakos-chalkida")
        t2 = _make_pos("1598", "1598", 38.4500, 23.6000, speed=80.0, course=0.0, corridor="proastiakos-chalkida")
        alerts = evaluate_train_conflicts(
            [(t1, now), (t2, now - 90.0)],
            stations=[],
            config=self.config,
            now=now,
        )
        self.assertEqual(alerts, [])

    def test_tc_wdg_17_conflict_input_contract_matrix_and_nan_rejection(self) -> None:
        """TC-WDG-17: TrainRecord, (pos, ts) tuple, last_updated_by_id, and legacy attr all trigger stale demotion; NaN/bool ignored."""
        now = 1700000000.0
        p1 = _make_pos("1597", "1597", 38.4000, 23.6000, speed=90.0, course=30.0, corridor="proastiakos-chalkida")
        p2 = _make_pos("1598", "1598", 38.4180, 23.6120, speed=0.0, course=210.0, corridor="proastiakos-chalkida")

        # 1. Sequence[TrainRecord]
        r1 = TrainRecord(position=p1, first_seen=now, last_updated=now)
        r2 = TrainRecord(position=p2, first_seen=now - 120.0, last_updated=now - 120.0)
        alerts_rec = evaluate_train_conflicts([r1, r2], stations=[], config=self.config, now=now)
        self.assertEqual(len(alerts_rec), 1)
        self.assertEqual(alerts_rec[0].severity, ConflictSeverity.ADVISORY)
        self.assertTrue(alerts_rec[0].alert_id.endswith("_STALE_OBSTRUCTION"))

        # 2. Sequence[Tuple[TrainPosition, float]]
        alerts_tup = evaluate_train_conflicts([(p1, now), (p2, now - 120.0)], stations=[], config=self.config, now=now)
        self.assertEqual(alerts_tup[0].severity, ConflictSeverity.ADVISORY)

        # 3. Sequence[TrainPosition] + last_updated_by_id
        alerts_map = evaluate_train_conflicts(
            [p1, p2],
            stations=[],
            config=self.config,
            now=now,
            last_updated_by_id={"1597": now, "1598": now - 120.0},
        )
        self.assertEqual(alerts_map[0].severity, ConflictSeverity.ADVISORY)

        # 4. Non-finite / bool timestamps rejected cleanly (do not crash or falsely demote)
        alerts_bad_ts = evaluate_train_conflicts(
            [(p1, float("nan")), (p2, True)],  # type: ignore[list-item]
            stations=[],
            config=self.config,
            now=now,
        )
        self.assertEqual(len(alerts_bad_ts), 1)
        self.assertEqual(alerts_bad_ts[0].severity, ConflictSeverity.CRITICAL)

    def test_tc_wdg_19_gis_polyline_alignment_and_terminal_ids(self) -> None:
        """TC-WDG-19: All TSRs, tunnels, and TERMINAL_STATION_IDS align with tracks.json / stations.json."""
        import json
        from pathlib import Path
        from tui.geo import (
            TERMINAL_STATION_IDS,
            project_point_to_corridor_chainage,
        )

        data_dir = Path(__file__).resolve().parent.parent / "tui" / "data"
        tracks = json.loads((data_dir / "tracks.json").read_text(encoding="utf-8"))
        speed_limits = json.loads((data_dir / "speed_limits.json").read_text(encoding="utf-8"))
        infra = json.loads((data_dir / "infrastructure.json").read_text(encoding="utf-8"))
        stations = json.loads((data_dir / "stations.json").read_text(encoding="utf-8"))

        station_ids = {s["id"].lower() for s in stations.get("stations", [])}
        for tid in TERMINAL_STATION_IDS:
            self.assertIn(tid, station_ids, f"Unknown terminal station id: {tid}")

        for tsr in speed_limits.get("slow_orders", []):
            cid = tsr["corridor_id"]
            for pt in tsr.get("points", []):
                proj = project_point_to_corridor_chainage(
                    GeoPoint(lat=pt[0], lng=pt[1]), cid, tracks
                )
                self.assertIsNotNone(proj)
                assert proj is not None
                self.assertLessEqual(
                    proj[1],
                    35.0,
                    f"TSR {tsr['id']} vertex {pt} drifted {proj[1]:.1f}m off {cid}",
                )

        corridor_ids = [c["id"] for c in tracks.get("corridors", [])]
        for feat in infra.get("features", []):
            props = feat.get("properties", {})
            if props.get("type") != "tunnel":
                continue
            for coord in feat.get("geometry", {}).get("coordinates", []):
                gp = GeoPoint(lat=coord[1], lng=coord[0])
                best_d = min(
                    proj[1]
                    for cid in corridor_ids
                    for proj in [project_point_to_corridor_chainage(gp, cid, tracks)]
                    if proj is not None
                )
                self.assertLessEqual(
                    best_d,
                    35.0,
                    f"Tunnel {props.get('id')} vertex drifted {best_d:.1f}m off tracks.json",
                )

    def test_tc_wdg_20_shared_trunk_and_double_track_chalkida(self) -> None:
        """TC-WDG-20: Chalkida train on SKA-Inoi double-track trunk avoids opposing false alarm; shared single-track trunk detects conflict."""
        from tui.mock_engine import load_tracks

        tracks = load_tracks()
        now = 1700000000.0
        # Two Chalkida trains on SKA-Inoi double-track trunk (38.15..38.20) moving opposite directions
        c1 = _make_pos("ch1", "1550", 38.150, 23.720, speed=90.0, course=0.0, corridor="proastiakos-chalkida")
        c2 = _make_pos("ch2", "1551", 38.190, 23.720, speed=90.0, course=180.0, corridor="proastiakos-chalkida")
        alerts_double = evaluate_train_conflicts(
            [(c1, now), (c2, now)], tracks_data=tracks, stations=[], config=self.config, now=now
        )
        self.assertEqual(
            [a for a in alerts_double if a.conflict_type == ConflictType.OPPOSING_COLLISION],
            [],
        )

        # Shared single-track trunk north of Thessaloniki (northern-corridor vs thessaloniki-idomeni)
        n1 = _make_pos("n1", "IC600", 40.750, 22.860, speed=90.0, course=0.0, corridor="northern-corridor")
        n2 = _make_pos("n2", "IC335", 40.780, 22.860, speed=90.0, course=180.0, corridor="thessaloniki-idomeni")
        alerts_shared = evaluate_train_conflicts(
            [(n1, now), (n2, now)], tracks_data=tracks, stations=[], config=self.config, now=now
        )
        self.assertEqual(len(alerts_shared), 1)
        self.assertEqual(alerts_shared[0].conflict_type, ConflictType.OPPOSING_COLLISION)

    def test_tc_wdg_21_watchdog_hysteresis_bell_escalation_f10_and_pause(self) -> None:
        """TC-WDG-21: FROZEN hysteresis, (alert_id, severity) bell escalation, F-10.1 strict ID, and PAUSED pruning suspension."""
        from train_tracker import DelayUpdate
        from tui.geo import SensorHealthState, evaluate_sensor_health
        from tui.state import ConnectionStatus, TrainStateStore

        now = 1700000000.0
        anchor = GeoPoint(lat=38.5000, lng=22.9000)
        # 25m jitter while previously FROZEN stays FROZEN (hysteresis >= 50m)
        jittered = GeoPoint(lat=38.50022, lng=22.9000)
        assess, _, _, _ = evaluate_sensor_health(
            point=jittered,
            current_speed=80.0,
            last_updated=now,
            last_moved_pt=anchor,
            last_moved_time=now - 120.0,
            station_dwell_start=None,
            stations=[],
            now=now,
            previous_state=SensorHealthState.FROZEN,
        )
        self.assertEqual(assess.state, SensorHealthState.FROZEN)

        # F-10.1 strict train_id precedence + PAUSED pruning suspension
        store = TrainStateStore()
        p_a = _make_pos("consist-a", "2232", 38.400, 23.600, speed=80.0, course=30.0, corridor="proastiakos-chalkida")
        p_b = _make_pos("consist-b", "2232", 38.415, 23.610, speed=80.0, course=210.0, corridor="proastiakos-chalkida")
        store.update_positions([p_a, p_b])
        store.apply_delay_updates([DelayUpdate(train_id="consist-a", train_number="2232", delay=12)], now=now + 1.0)
        self.assertEqual(store.get_train("consist-a").position.delay, 12)  # type: ignore[union-attr]
        self.assertEqual(store.get_train("consist-b").position.delay, 0)  # type: ignore[union-attr]

        # PAUSED suspends automatic stale-train pruning
        store.set_connection_status(ConnectionStatus.PAUSED, "Paused")
        self.assertEqual(store.prune_stale_trains(now=now + 10000.0), 0)
        self.assertIsNotNone(store.get_train("consist-a"))


if __name__ == "__main__":
    unittest.main()
