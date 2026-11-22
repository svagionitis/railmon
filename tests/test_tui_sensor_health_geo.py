"""Unit tests for F-5.2 Sensor Health & Telemetry Watchdog kinematics and geo algorithms."""

import time
import unittest

from tui.geo import (
    GeoPoint,
    SensorHealthAssessment,
    SensorHealthState,
    evaluate_sensor_health,
    find_nearest_station_aabb,
    haversine_distance_km,
)
from tui.mock_engine import StationNode


class TestSensorHealthGeo(unittest.TestCase):
    """Test suite for spatial proximity, displacement tracking, and sensor health classification."""

    def setUp(self) -> None:
        # Reference stations in Greece
        self.stn_larissa = StationNode(
            id="LAR",
            code=1100,
            name_el="Λάρισα",
            name_en="Larissa",
            point=GeoPoint(lat=39.6385, lng=22.4241),
            corridor="PATHE",
            is_hub=True,
        )
        self.stn_domokos = StationNode(
            id="DOM",
            code=1101,
            name_el="Δομοκός",
            name_en="Domokos",
            point=GeoPoint(lat=39.1245, lng=22.3120),
            corridor="PATHE",
            is_hub=False,
        )
        self.stn_athens = StationNode(
            id="ATH",
            code=1000,
            name_el="Αθήνα",
            name_en="Athens",
            point=GeoPoint(lat=37.9922, lng=23.7208),
            corridor="PATHE",
            is_hub=True,
        )
        self.stations = [self.stn_larissa, self.stn_domokos, self.stn_athens]

    def test_find_nearest_station_aabb_within_radius(self) -> None:
        """Verify AABB fast pre-filter detects station within 250m centroid."""
        # Query point 50 meters north of Larissa station
        query_pt = GeoPoint(lat=39.6389, lng=22.4241)
        stn, dist_m = find_nearest_station_aabb(query_pt, self.stations, max_radius_m=250.0)
        self.assertIsNotNone(stn)
        self.assertIsNotNone(dist_m)
        assert stn is not None and dist_m is not None
        self.assertEqual(stn.id, "LAR")
        self.assertLessEqual(dist_m, 250.0)

    def test_find_nearest_station_aabb_outside_radius(self) -> None:
        """Verify AABB rejects station beyond search radius."""
        # Query point 2 km away from Larissa
        query_pt = GeoPoint(lat=39.6580, lng=22.4241)
        stn, dist_m = find_nearest_station_aabb(query_pt, self.stations, max_radius_m=250.0)
        self.assertIsNone(stn)
        self.assertIsNone(dist_m)

    def test_find_nearest_station_empty_stations(self) -> None:
        """Verify handling when stations list is empty."""
        query_pt = GeoPoint(lat=39.6385, lng=22.4241)
        stn, dist_m = find_nearest_station_aabb(query_pt, [], max_radius_m=250.0)
        self.assertIsNone(stn)
        self.assertIsNone(dist_m)

    def test_evaluate_normal_motion(self) -> None:
        """Verify actively moving train with fresh telemetry evaluates to NORMAL_MOTION."""
        now = 1000.0
        pt = GeoPoint(lat=39.5000, lng=22.3500)
        anchor_pt = GeoPoint(lat=39.4900, lng=22.3500)  # > 1 km away
        assessment, new_anchor, new_time, dwell = evaluate_sensor_health(
            point=pt,
            current_speed=140.0,
            last_updated=now - 2.0,
            last_moved_pt=anchor_pt,
            last_moved_time=now - 10.0,
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.NORMAL_MOTION)
        self.assertEqual(assessment.short_code, "RUN")
        self.assertFalse(assessment.is_alert)
        self.assertFalse(assessment.is_critical)
        self.assertEqual(new_anchor, pt)
        self.assertEqual(new_time, now)
        self.assertIsNone(dwell)

    def test_evaluate_station_dwell(self) -> None:
        """Verify stationary train within 250m of station centroid evaluates to STATION_DWELL."""
        now = 1000.0
        # Exactly at Larissa station
        pt = GeoPoint(lat=39.6385, lng=22.4241)
        assessment, _, _, dwell_start = evaluate_sensor_health(
            point=pt,
            current_speed=0.0,
            last_updated=now - 5.0,
            last_moved_pt=pt,
            last_moved_time=now - 60.0,
            station_dwell_start=now - 60.0,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.STATION_DWELL)
        self.assertEqual(assessment.short_code, "DWL")
        self.assertEqual(assessment.nearest_station, "Larissa")
        self.assertAlmostEqual(assessment.dwell_duration_sec, 60.0)
        self.assertFalse(assessment.is_alert)
        self.assertFalse(assessment.is_critical)
        self.assertEqual(dwell_start, now - 60.0)

    def test_evaluate_signal_halt(self) -> None:
        """Verify stationary train on open track (> 250m from stations) evaluates to SIGNAL_HALT."""
        now = 1000.0
        # In the middle of Thessalian plains, > 5km from any station
        pt = GeoPoint(lat=39.5000, lng=22.3500)
        assessment, _, _, dwell_start = evaluate_sensor_health(
            point=pt,
            current_speed=0.0,
            last_updated=now - 3.0,
            last_moved_pt=pt,
            last_moved_time=now - 20.0,
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.SIGNAL_HALT)
        self.assertEqual(assessment.short_code, "HLT")
        self.assertFalse(assessment.is_alert)
        self.assertIsNone(dwell_start)

    def test_evaluate_stale_warning(self) -> None:
        """Verify packet delayed by > 30s evaluates to STALE warning."""
        now = 1000.0
        pt = GeoPoint(lat=39.5000, lng=22.3500)
        assessment, _, _, _ = evaluate_sensor_health(
            point=pt,
            current_speed=120.0,
            last_updated=now - 45.0,  # 45s silence (> 30s)
            last_moved_pt=pt,
            last_moved_time=now - 10.0,
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.STALE)
        self.assertEqual(assessment.short_code, "STL")
        self.assertFalse(assessment.is_alert)
        self.assertFalse(assessment.is_critical)

    def test_evaluate_sensor_freeze_anomaly(self) -> None:
        """Verify speed > 0 km/h with static coordinates for >= 45s triggers FROZEN alert."""
        now = 1000.0
        # Coordinate unchanged for 48s while speed is 142 km/h
        pt = GeoPoint(lat=38.7412, lng=22.4810)
        assessment, _, _, _ = evaluate_sensor_health(
            point=pt,
            current_speed=142.0,
            last_updated=now - 1.0,  # Fresh packet
            last_moved_pt=pt,
            last_moved_time=now - 48.0,  # Static for 48s (>= 45s)
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.FROZEN)
        self.assertEqual(assessment.short_code, "FRZ")
        self.assertTrue(assessment.is_alert)
        self.assertTrue(assessment.is_critical)
        self.assertIn("FROZEN", assessment.description)

    def test_evaluate_sensor_freeze_reset_on_advancing_motion(self) -> None:
        """Verify sensor freeze clears immediately when train displaces by > 10m."""
        now = 1000.0
        anchor_pt = GeoPoint(lat=38.7412, lng=22.4810)
        # Advance train by 100m north (approx 0.0009 deg)
        advanced_pt = GeoPoint(lat=38.7421, lng=22.4810)
        disp = haversine_distance_km(anchor_pt, advanced_pt) * 1000.0
        self.assertGreater(disp, 10.0)

        assessment, new_anchor, new_time, _ = evaluate_sensor_health(
            point=advanced_pt,
            current_speed=142.0,
            last_updated=now - 1.0,
            last_moved_pt=anchor_pt,
            last_moved_time=now - 60.0,  # was static
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.NORMAL_MOTION)
        self.assertEqual(assessment.short_code, "RUN")
        self.assertFalse(assessment.is_alert)
        self.assertEqual(new_anchor, advanced_pt)
        self.assertEqual(new_time, now)

    def test_evaluate_telemetry_drop_anomaly(self) -> None:
        """Verify telemetry silence >= 120s triggers DROPPED critical alert."""
        now = 1000.0
        pt = GeoPoint(lat=39.1245, lng=22.3120)
        assessment, _, _, _ = evaluate_sensor_health(
            point=pt,
            current_speed=118.0,
            last_updated=now - 125.0,  # 125s silence (>= 120s)
            last_moved_pt=pt,
            last_moved_time=now - 125.0,
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.DROPPED)
        self.assertEqual(assessment.short_code, "DRP")
        self.assertTrue(assessment.is_alert)
        self.assertTrue(assessment.is_critical)
        self.assertIn("DROPPED", assessment.description)

    def test_multipath_jitter_suppression(self) -> None:
        """Verify small GPS multipath jitter (<= 10m) does not advance the anchor or clear freeze."""
        now = 1000.0
        anchor_pt = GeoPoint(lat=39.6385, lng=22.4241)
        # 3 meters jitter
        jitter_pt = GeoPoint(lat=39.63852, lng=22.4241)
        disp = haversine_distance_km(anchor_pt, jitter_pt) * 1000.0
        self.assertLess(disp, 10.0)

        # Still within freeze window if speed > 1
        assessment, new_anchor, new_time, _ = evaluate_sensor_health(
            point=jitter_pt,
            current_speed=50.0,
            last_updated=now - 1.0,
            last_moved_pt=anchor_pt,
            last_moved_time=now - 50.0,
            station_dwell_start=None,
            stations=self.stations,
            now=now,
        )
        self.assertEqual(assessment.state, SensorHealthState.FROZEN)
        self.assertEqual(new_anchor, anchor_pt)
        self.assertEqual(new_time, now - 50.0)


if __name__ == "__main__":
    unittest.main()
