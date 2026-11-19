"""Unit tests for Rail Network Topology and Offline Mock Telemetry Simulator."""

import unittest
from tui.geo import GREECE_PRESET, GeoPoint
from tui.mock_engine import (
    DEFAULT_SNAP_THRESHOLD_M,
    MockTelemetryEngine,
    SimulatedTrain,
    StationNode,
    TrackPolyline,
    TrainMotionState,
    calculate_bearing,
    haversine_distance,
    load_stations,
    load_tracks,
    project_point_to_segment,
    snap_to_track_network,
)
from train_tracker import TrainPosition


class TestRailNetworkTopology(unittest.TestCase):
    """Test station metadata, vector track schemas, and geographic validity."""

    def setUp(self):
        self.stations = load_stations()
        self.tracks = load_tracks()

    def test_stations_schema_and_keys(self):
        """Verify all stations in stations.json have complete and valid schema attributes."""
        self.assertGreaterEqual(len(self.stations), 25, "Expected at least 25 core stations")

        valid_corridors = {"PATHE", "PROASTIAKOS_ATH", "THESSALY", "NORTHERN", "MACEDONIA"}

        for station_id, st in self.stations.items():
            self.assertIsInstance(st, StationNode)
            self.assertEqual(st.id, station_id)
            self.assertTrue(len(st.id) >= 2, f"Invalid station ID: {st.id}")
            self.assertTrue(len(st.name_el) > 0, f"Empty Greek name for station: {st.id}")
            self.assertTrue(len(st.name_en) > 0, f"Empty English name for station: {st.id}")
            self.assertIn(
                st.corridor,
                valid_corridors,
                f"Station {st.id} has unrecognized corridor: {st.corridor}",
            )
            self.assertIsInstance(st.is_hub, bool)

    def test_station_coordinates_within_greece(self):
        """Verify all stations fall within Greek national geographic bounds."""
        for station_id, st in self.stations.items():
            # Allow small tolerance around Greek bounds
            self.assertTrue(
                37.40 <= st.point.lat <= 41.80,
                f"Station {station_id} latitude out of bounds: {st.point.lat}",
            )
            self.assertTrue(
                21.30 <= st.point.lng <= 26.80,
                f"Station {station_id} longitude out of bounds: {st.point.lng}",
            )

    def test_track_segments_validity(self):
        """Verify all corridors in tracks.json have valid polylines and lengths."""
        expected_corridors = [
            "pathe-mainline",
            "proastiakos-airport-kiato",
            "proastiakos-chalkida",
            "thessaloniki-idomeni",
            "northern-corridor",
            "thessaly-kalambaka",
            "thessaly-volos",
            "western-macedonia",
        ]

        for cid in expected_corridors:
            self.assertIn(cid, self.tracks, f"Missing track corridor: {cid}")
            track = self.tracks[cid]
            self.assertIsInstance(track, TrackPolyline)
            self.assertGreaterEqual(
                len(track.points), 2, f"Corridor {cid} has fewer than 2 points"
            )
            self.assertGreater(
                track.total_length_m, 1000.0, f"Corridor {cid} length too small"
            )

            # Cumulative distances must be strictly monotonically increasing
            for i in range(1, len(track.cumulative_distances_m)):
                self.assertGreater(
                    track.cumulative_distances_m[i],
                    track.cumulative_distances_m[i - 1],
                    f"Non-increasing distance in {cid} at segment {i}",
                )

    def test_high_density_vertex_counts_and_chord_bounds(self):
        """Verify high-density track polyline vertex counts and segment chord bounds."""
        total_vertices = sum(len(t.points) for t in self.tracks.values())
        self.assertGreaterEqual(
            total_vertices,
            300,
            f"Expected at least 300 total track vertices across corridors, got {total_vertices}",
        )
        pathe = self.tracks["pathe-mainline"]
        self.assertGreaterEqual(
            len(pathe.points),
            100,
            f"Expected at least 100 vertices on pathe-mainline, got {len(pathe.points)}",
        )

    def test_all_stations_anchored_to_track_vertices(self):
        """Verify every station in stations.json is anchored directly to a track vertex (<= 50m)."""
        self.assertGreaterEqual(
            len(self.stations),
            61,
            f"Expected at least 61 stations (including intermediate nodes), got {len(self.stations)}",
        )
        all_track_points = [pt for track in self.tracks.values() for pt in track.points]

        for station_id, st in self.stations.items():
            min_dist_m = min(haversine_distance(st.point, pt) for pt in all_track_points)
            self.assertLessEqual(
                min_dist_m,
                50.0,
                f"Station {station_id} ({st.name_en}) is {min_dist_m:.1f}m away from nearest track vertex",
            )

    def test_boeotia_and_attica_case_study_alignments(self):
        """Verify Case Study A (Eleonas/Tanagra) and Case Study B (Afidnes/Sfendali) track alignments."""
        pathe_pts = self.tracks["pathe-mainline"].points
        chalkida_pts = self.tracks["proastiakos-chalkida"].points

        case_study_waypoints = {
            "Afidnes (AFI)": GeoPoint(lat=38.1877, lng=23.8446),
            "Sfendali (SFE)": GeoPoint(lat=38.2356, lng=23.7845),
            "Tanagra (TAN)": GeoPoint(lat=38.3400, lng=23.5814),
            "Eleonas (ELE)": GeoPoint(lat=38.3747, lng=23.4635),
        }
        for label, target_pt in case_study_waypoints.items():
            min_dist_m = min(haversine_distance(target_pt, pt) for pt in pathe_pts)
            self.assertLessEqual(
                min_dist_m,
                50.0,
                f"PATHE mainline misses {label} by {min_dist_m:.1f}m",
            )

        # Shared trunk from SKA (38.0744, 23.7381) to Inoi (38.3194, 23.6067) must be identical
        ska_pt = GeoPoint(lat=38.0744, lng=23.7381)
        inoi_pt = GeoPoint(lat=38.3194, lng=23.6067)

        pathe_ska_idx = pathe_pts.index(ska_pt)
        pathe_inoi_idx = pathe_pts.index(inoi_pt)
        chalkida_ska_idx = chalkida_pts.index(ska_pt)
        chalkida_inoi_idx = chalkida_pts.index(inoi_pt)

        pathe_trunk = pathe_pts[pathe_ska_idx : pathe_inoi_idx + 1]
        chalkida_trunk = chalkida_pts[chalkida_ska_idx : chalkida_inoi_idx + 1]
        self.assertEqual(
            pathe_trunk,
            chalkida_trunk,
            "PATHE mainline and Proastiakos Chalkida must share identical vertices between SKA and Inoi",
        )
        self.assertGreaterEqual(len(pathe_trunk), 15)


class TestGeodesicMath(unittest.TestCase):
    """Test Haversine distance, forward bearing, and polyline interpolation."""

    def test_haversine_distance_calculation(self):
        """Verify Haversine distance formula with known points."""
        p_athens = GeoPoint(lat=37.9926, lng=23.7203)
        p_larissa = GeoPoint(lat=39.6385, lng=22.4222)

        # Great circle distance Athens to Larissa is approximately 213 km
        dist_m = haversine_distance(p_athens, p_larissa)
        dist_km = dist_m / 1000.0

        self.assertAlmostEqual(dist_km, 213.0, delta=10.0)

        # Distance to itself must be zero
        self.assertEqual(haversine_distance(p_athens, p_athens), 0.0)

    def test_bearing_azimuth_cardinal_directions(self):
        """Verify forward bearing calculations for cardinal directions."""
        origin = GeoPoint(lat=38.0, lng=24.0)

        p_north = GeoPoint(lat=39.0, lng=24.0)
        p_south = GeoPoint(lat=37.0, lng=24.0)
        p_east = GeoPoint(lat=38.0, lng=25.0)
        p_west = GeoPoint(lat=38.0, lng=23.0)

        b_north = calculate_bearing(origin, p_north)
        b_south = calculate_bearing(origin, p_south)
        b_east = calculate_bearing(origin, p_east)
        b_west = calculate_bearing(origin, p_west)

        self.assertAlmostEqual(b_north, 0.0, delta=0.5)
        self.assertAlmostEqual(b_south, 180.0, delta=0.5)
        self.assertAlmostEqual(b_east, 90.0, delta=1.0)
        self.assertAlmostEqual(b_west, 270.0, delta=1.0)

    def test_polyline_interpolation_endpoints(self):
        """Verify polyline interpolation at bounds and intermediate distances."""
        coords = [
            (38.0, 23.0),
            (38.5, 23.5),
            (39.0, 24.0),
        ]
        poly = TrackPolyline.from_coordinates("test-poly", "Test Polyline", coords)

        # At distance 0 -> matches start point
        p_start, h_start = poly.interpolate_at_distance(0.0)
        self.assertAlmostEqual(p_start.lat, 38.0)
        self.assertAlmostEqual(p_start.lng, 23.0)

        # At total length -> matches end point
        p_end, h_end = poly.interpolate_at_distance(poly.total_length_m)
        self.assertAlmostEqual(p_end.lat, 39.0)
        self.assertAlmostEqual(p_end.lng, 24.0)

        # Intermediate distance -> between bounds
        p_mid, h_mid = poly.interpolate_at_distance(poly.total_length_m / 2.0)
        self.assertTrue(38.0 < p_mid.lat < 39.0)
        self.assertTrue(23.0 < p_mid.lng < 24.0)

    def test_project_point_to_segment_orthogonal_and_clamped_endpoints(self):
        """Verify orthogonal point-to-segment projection for interior, clamped, and degenerate segments."""
        p0 = GeoPoint(lat=38.0, lng=23.0)
        p1 = GeoPoint(lat=38.0, lng=23.1)

        # 1. Interior point (0 < t < 1): 0.0045 deg (~500.4m) due north of midpoint (38.0, 23.05)
        query_mid = GeoPoint(lat=38.0045, lng=23.05)
        snapped_mid, dist_mid = project_point_to_segment(query_mid, p0, p1)
        self.assertAlmostEqual(snapped_mid.lat, 38.0, places=6)
        self.assertAlmostEqual(snapped_mid.lng, 23.05, places=6)
        self.assertAlmostEqual(dist_mid, 500.4, delta=3.0)

        # 2. Clamped before start (t = 0.0): west of p0
        query_west = GeoPoint(lat=38.0, lng=22.95)
        snapped_west, dist_west = project_point_to_segment(query_west, p0, p1)
        self.assertEqual(snapped_west, p0)
        self.assertGreater(dist_west, 4000.0)

        # 3. Clamped past end (t = 1.0): east of p1
        query_east = GeoPoint(lat=38.0, lng=23.15)
        snapped_east, dist_east = project_point_to_segment(query_east, p0, p1)
        self.assertEqual(snapped_east, p1)
        self.assertGreater(dist_east, 4000.0)

        # 4. Degenerate zero-length segment (p0 == p1)
        snapped_deg, dist_deg = project_point_to_segment(query_mid, p0, p0)
        self.assertEqual(snapped_deg, p0)
        self.assertAlmostEqual(dist_deg, haversine_distance(query_mid, p0), delta=5.0)

    def test_track_polyline_project_point_and_snap_to_network(self):
        """Verify polyline projection and tolerance-bounded network snapping."""
        tracks_dict = load_tracks()
        tracks_list = list(tracks_dict.values())
        pathe = tracks_dict["pathe-mainline"]

        # Case Study A: Point offset ~334m north of Eleonas (38.3747°N, 23.4635°E)
        eleonas_offset = GeoPoint(lat=38.3747 + 0.0030, lng=23.4635)
        snapped_pt, perp_dist_m, seg_heading = pathe.project_point(eleonas_offset)

        self.assertTrue(250.0 <= perp_dist_m <= 360.0, f"Unexpected perp_dist_m: {perp_dist_m}")
        self.assertTrue(0.0 <= seg_heading < 360.0)

        # Re-projecting the snapped point onto pathe-mainline must yield ~0.0m distance
        _, re_dist_m, _ = pathe.project_point(snapped_pt)
        self.assertLessEqual(re_dist_m, 0.05)

        # Network snapping within default 1500m threshold must snap to centerline
        self.assertEqual(DEFAULT_SNAP_THRESHOLD_M, 1500.0)
        net_snapped = snap_to_track_network(eleonas_offset, tracks_list)
        self.assertAlmostEqual(net_snapped.lat, snapped_pt.lat, places=6)
        self.assertAlmostEqual(net_snapped.lng, snapped_pt.lng, places=6)

        # Corridor hint resolution
        hint_snapped = snap_to_track_network(
            eleonas_offset, tracks_list, corridor_hint="PATHE"
        )
        self.assertAlmostEqual(hint_snapped.lat, snapped_pt.lat, places=6)
        self.assertAlmostEqual(hint_snapped.lng, snapped_pt.lng, places=6)

        # Off-corridor point (> 1500m threshold, e.g., 15km away in the Aegean Sea) must remain raw
        off_network_pt = GeoPoint(lat=38.3000, lng=24.2000)
        unsnapped = snap_to_track_network(
            off_network_pt, tracks_list, max_snap_distance_m=1500.0
        )
        self.assertEqual(unsnapped, off_network_pt)


class TestMockTelemetryEngine(unittest.TestCase):
    """Test simulated train kinematics, state transitions, and generator streaming."""

    def setUp(self):
        self.engine = MockTelemetryEngine()

    def test_roster_initialization(self):
        """Verify standard fleet of trains is instantiated across corridors."""
        self.assertGreaterEqual(len(self.engine.trains), 10, "Fleet must have at least 10 trains")

        train_ids = {t.id for t in self.engine.trains}
        expected_ids = {
            "mock-ic-50",
            "mock-ic-51",
            "mock-ic-52",
            "mock-ic-53",
            "mock-frg-80512",
            "mock-pro-1597",
            "mock-pro-2232",
            "mock-pro-1314",
            "mock-reg-1731",
            "mock-reg-884",
            "mock-int-361",
        }
        for exp in expected_ids:
            self.assertIn(exp, train_ids)

    def test_train_kinematic_movement(self):
        """Verify train steps advance distance and calculate valid speed and progress."""
        train = self.engine.trains[0]
        initial_dist = train.current_distance_m

        # Step forward 10 seconds
        pos = train.step(10.0)

        self.assertIsInstance(pos, TrainPosition)
        self.assertGreater(train.current_distance_m, initial_dist)
        self.assertGreater(pos.speed, 0.0)
        self.assertEqual(pos.status, "Κανονική")
        self.assertGreaterEqual(pos.progress, 0.0)
        self.assertLessEqual(pos.progress, 1.0)
        self.assertGreaterEqual(pos.course, 0.0)
        self.assertLess(pos.course, 360.0)

    def test_train_terminal_turnaround(self):
        """Verify train reaches terminus, enters dwell, and reverses direction."""
        track = self.engine.tracks["proastiakos-chalkida"]
        train = SimulatedTrain(
            id="test-turnaround",
            train_number="TEST 1",
            locomotive_number="999999",
            train_type="Proastiakos",
            service_type="Προαστιακός",
            origin="Αθήνα",
            destination="Χαλκίδα",
            corridor="PROASTIAKOS",
            track=track,
            cruise_speed_kmh=100.0,
            acceleration_mps2=1.0,
            deceleration_mps2=1.0,
            dwell_duration_sec=5.0,
            terminal_dwell_sec=10.0,
            current_distance_m=track.total_length_m - 200.0,  # Near terminus
            direction=1,
        )

        # Advance until deceleration and dwell trigger
        for _ in range(30):
            train.step(1.0)
            if train.motion_state == TrainMotionState.TERMINAL_DWELL:
                break

        self.assertEqual(train.motion_state, TrainMotionState.TERMINAL_DWELL)
        self.assertEqual(train.current_speed_kmh, 0.0)

        # Step past terminal dwell duration
        train.step(12.0)

        # Direction must now be reversed (-1)
        self.assertEqual(train.direction, -1)
        self.assertEqual(train.origin, "Χαλκίδα")
        self.assertEqual(train.destination, "Αθήνα")

    def test_engine_step_and_reset(self):
        """Verify engine step updates all trains and reset restores initial positions."""
        train0 = self.engine.trains[0]
        init_dist = train0.current_distance_m

        # Step simulation by 5 seconds
        positions = self.engine.step(5.0)
        self.assertEqual(len(positions), len(self.engine.trains))
        self.assertNotEqual(train0.current_distance_m, init_dist)

        # Reset simulation
        self.engine.reset()
        self.assertAlmostEqual(train0.current_distance_m, init_dist, delta=1.0)

    def test_stream_positions_generator(self):
        """Verify stream_positions generator yields TrainPosition batches."""
        gen = self.engine.stream_positions(interval_sec=0.01)

        batch = next(gen)
        self.assertIsInstance(batch, list)
        self.assertGreater(len(batch), 0)

        sample = batch[0]
        self.assertIsInstance(sample, TrainPosition)
        self.assertTrue(sample.id.startswith("mock-"))
        self.assertIsNotNone(sample.lat)
        self.assertIsNotNone(sample.lng)
        self.assertIsNotNone(sample.timestamp)


if __name__ == "__main__":
    unittest.main()
