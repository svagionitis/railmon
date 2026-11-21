"""Unit tests for railway infrastructure GIS models and spatial math (F-7.2)."""

import unittest
from typing import List

from tui.geo import (
    BoundingBox,
    GeoPoint,
    InfrastructureFeature,
    InfrastructureType,
    PortalNode,
    SignalAspect,
    detect_train_tunnel,
    is_point_in_tunnel,
    parse_infrastructure_geojson,
    project_point_to_segment,
)
from tui.mock_engine import load_infrastructure


class TestInfrastructureGeoModels(unittest.TestCase):
    """Test suite for InfrastructureFeature and spatial point projection."""

    def test_create_tunnel_feature(self) -> None:
        pts = [
            GeoPoint(lat=38.7270, lng=22.5585),
            GeoPoint(lat=38.7620, lng=22.5320),
            GeoPoint(lat=38.7960, lng=22.5015),
        ]
        portals = [
            PortalNode(name="South", point=pts[0]),
            PortalNode(name="North", point=pts[-1]),
        ]
        tun = InfrastructureFeature.create_tunnel(
            id="tun-test",
            name="Test Tunnel",
            corridor_id="pathe-mainline",
            points=pts,
            portals=portals,
            length_m=9038.0,
            description="Test bore",
        )
        self.assertEqual(tun.id, "tun-test")
        self.assertEqual(tun.name, "Test Tunnel")
        self.assertEqual(tun.feature_type, InfrastructureType.TUNNEL)
        self.assertEqual(len(tun.points), 3)
        self.assertEqual(len(tun.portals), 2)
        self.assertEqual(tun.length_m, 9038.0)
        self.assertIsNotNone(tun.bbox)
        assert tun.bbox is not None
        self.assertTrue(tun.bbox.contains(pts[1]))

    def test_create_bridge_feature(self) -> None:
        pts = [
            GeoPoint(lat=37.9242, lng=22.9700),
            GeoPoint(lat=37.9236, lng=22.9306),
        ]
        brg = InfrastructureFeature.create_bridge(
            id="brg-test",
            name="Test Bridge",
            corridor_id="proastiakos",
            points=pts,
            length_m=120.0,
        )
        self.assertEqual(brg.feature_type, InfrastructureType.BRIDGE)
        self.assertEqual(brg.length_m, 120.0)
        self.assertEqual(len(brg.points), 2)

    def test_create_signal_feature(self) -> None:
        pt = GeoPoint(lat=38.0744, lng=23.7381)
        sig = InfrastructureFeature.create_signal(
            id="sig-test",
            name="SKA Interlocking",
            corridor_id="pathe-mainline",
            point=pt,
            signal_aspect=SignalAspect.INTERLOCKING,
        )
        self.assertEqual(sig.feature_type, InfrastructureType.SIGNAL)
        self.assertEqual(sig.signal_aspect, SignalAspect.INTERLOCKING)
        self.assertEqual(sig.point, pt)


class TestPointInTunnelDetection(unittest.TestCase):
    """Test suite for spatial in-tunnel kinematic detection algorithms."""

    def setUp(self) -> None:
        self.infrastructure = load_infrastructure()
        self.tunnels = [
            f for f in self.infrastructure.values() if f.feature_type == InfrastructureType.TUNNEL
        ]

    def test_detect_train_inside_kallidromo_tunnel(self) -> None:
        # Mid-tunnel coordinate along Kallidromo
        query_pt = GeoPoint(lat=38.7620, lng=22.5320)
        res = detect_train_tunnel(query_pt, self.tunnels)
        self.assertIsNotNone(res)
        assert res is not None
        tun, dist_along_m, ratio = res
        self.assertEqual(tun.id, "tun-kallidromo")
        self.assertGreater(dist_along_m, 3500.0)
        self.assertLess(dist_along_m, 6000.0)
        self.assertAlmostEqual(ratio, 0.50, delta=0.15)

    def test_detect_train_inside_othrys_tunnel(self) -> None:
        # Mid-tunnel coordinate along Mount Othrys
        query_pt = GeoPoint(lat=39.0050, lng=22.3680)
        res = detect_train_tunnel(query_pt, self.tunnels)
        self.assertIsNotNone(res)
        assert res is not None
        tun, dist_along_m, ratio = res
        self.assertEqual(tun.id, "tun-othrys")
        self.assertGreater(dist_along_m, 2000.0)
        self.assertLess(dist_along_m, 6000.0)

    def test_reject_train_outside_any_tunnel(self) -> None:
        # Athens Station (open air)
        athens_pt = GeoPoint(lat=37.9926, lng=23.7203)
        res = detect_train_tunnel(athens_pt, self.tunnels)
        self.assertIsNone(res)

        # Thessaloniki Station (open air)
        thess_pt = GeoPoint(lat=40.6440, lng=22.9280)
        res = detect_train_tunnel(thess_pt, self.tunnels)
        self.assertIsNone(res)

    def test_is_point_in_tunnel_direct(self) -> None:
        kallidromo = self.infrastructure["tun-kallidromo"]
        in_pt = GeoPoint(lat=38.7620, lng=22.5320)
        out_pt = GeoPoint(lat=38.5000, lng=22.8000)

        self.assertTrue(is_point_in_tunnel(in_pt, kallidromo))
        self.assertFalse(is_point_in_tunnel(out_pt, kallidromo))


class TestGeoJsonParsing(unittest.TestCase):
    """Test suite for GeoJSON parsing robustness and edge cases."""

    def test_parse_valid_geojson_payload(self) -> None:
        payload = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "id": "tun-1",
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [[22.55, 38.72], [22.53, 38.76]],
                    },
                    "properties": {
                        "name": "Custom Tunnel",
                        "type": "tunnel",
                        "corridor": "corridor-1",
                        "length_m": 4500,
                        "portals": [
                            {"name": "P1", "coordinates": [22.55, 38.72]},
                            {"name": "P2", "coordinates": [22.53, 38.76]},
                        ],
                    },
                },
                {
                    "type": "Feature",
                    "id": "sig-1",
                    "geometry": {"type": "Point", "coordinates": [22.55, 38.72]},
                    "properties": {
                        "name": "Custom Signal",
                        "type": "signal",
                        "aspect": "interlocking",
                        "corridor": "corridor-1",
                    },
                },
            ],
        }
        features = parse_infrastructure_geojson(payload)
        self.assertEqual(len(features), 2)
        tun = features[0]
        sig = features[1]
        self.assertEqual(tun.feature_type, InfrastructureType.TUNNEL)
        self.assertEqual(len(tun.portals), 2)
        self.assertEqual(sig.feature_type, InfrastructureType.SIGNAL)
        self.assertEqual(sig.signal_aspect, SignalAspect.INTERLOCKING)

    def test_parse_malformed_geojson_handled_gracefully(self) -> None:
        self.assertEqual(parse_infrastructure_geojson("not-json"), [])
        self.assertEqual(parse_infrastructure_geojson({}), [])
        self.assertEqual(parse_infrastructure_geojson({"type": "FeatureCollection", "features": "invalid"}), [])


if __name__ == "__main__":
    unittest.main()
