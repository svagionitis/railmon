"""Unit tests for Rail Network Topology and Offline Mock Telemetry Simulator."""

import unittest
from tui.geo import GREECE_PRESET, GeoPoint
from tui.mock_engine import (
    MockTelemetryEngine,
    SimulatedTrain,
    StationNode,
    TrackPolyline,
    TrainMotionState,
    calculate_bearing,
    haversine_distance,
    load_stations,
    load_tracks,
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
