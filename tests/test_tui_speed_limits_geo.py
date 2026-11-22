"""Unit tests for railway corridor speed limits and slow order spatial evaluation (F-5.3)."""

import json
import unittest
from typing import List

from tui.geo import (
    BoundingBox,
    CorridorSegmentLimit,
    CorridorSpeedRule,
    DEFAULT_CORRIDOR_SPEED_RULES,
    GeoPoint,
    OverspeedSeverity,
    SlowOrder,
    SpeedEnforcementResult,
    SpeedLimitZoneType,
    evaluate_train_speed,
    parse_speed_limits_json,
)
from tui.mock_engine import load_slow_orders, load_speed_limits


class TestSpeedLimitsGeoModels(unittest.TestCase):
    """Test suite for SpeedLimit domain models, JSON parser, and spatial evaluator."""

    def test_enums(self) -> None:
        self.assertIn(SpeedLimitZoneType.CORRIDOR_BASE, SpeedLimitZoneType)
        self.assertIn(SpeedLimitZoneType.SEGMENT_LIMIT, SpeedLimitZoneType)
        self.assertIn(SpeedLimitZoneType.SLOW_ORDER_TSR, SpeedLimitZoneType)

        self.assertIn(OverspeedSeverity.NORMAL, OverspeedSeverity)
        self.assertIn(OverspeedSeverity.ADVISORY, OverspeedSeverity)
        self.assertIn(OverspeedSeverity.OVERSPEED, OverspeedSeverity)
        self.assertIn(OverspeedSeverity.TSR_VIOLATION, OverspeedSeverity)

    def test_slow_order_create(self) -> None:
        pts = [
            GeoPoint(lat=38.8620, lng=22.4200),
            GeoPoint(lat=38.8750, lng=22.4300),
        ]
        tsr = SlowOrder.create(
            id="TSR-TEST",
            corridor_id="pathe-mainline",
            name="Test Spercheios TSR",
            max_speed_kmh=40.0,
            points=pts,
            reason="Track Maintenance",
            active=True,
            buffer_m=200.0,
        )
        self.assertEqual(tsr.id, "TSR-TEST")
        self.assertEqual(tsr.corridor_id, "pathe-mainline")
        self.assertEqual(tsr.max_speed_kmh, 40.0)
        self.assertEqual(len(tsr.points), 2)
        self.assertIsNotNone(tsr.bbox)
        assert tsr.bbox is not None
        self.assertTrue(tsr.bbox.contains(pts[0]))
        self.assertTrue(tsr.bbox.contains(pts[1]))

    def test_corridor_segment_limit_create(self) -> None:
        pts = [
            GeoPoint(lat=37.9482, lng=23.6425),
            GeoPoint(lat=37.9926, lng=23.7203),
        ]
        seg = CorridorSegmentLimit.create(
            id="SEG-URBAN",
            corridor_id="pathe-mainline",
            name="Athens Urban",
            max_speed_kmh=80.0,
            points=pts,
            buffer_m=120.0,
        )
        self.assertEqual(seg.id, "SEG-URBAN")
        self.assertEqual(seg.max_speed_kmh, 80.0)
        self.assertIsNotNone(seg.bbox)
        assert seg.bbox is not None
        self.assertTrue(seg.bbox.contains(pts[0]))

    def test_parse_speed_limits_json(self) -> None:
        payload = {
            "corridors": [
                {
                    "id": "pathe-mainline",
                    "name": "PATHE Mainline",
                    "default_speed_kmh": 160.0,
                    "segments": [
                        {
                            "id": "pathe-urban",
                            "name": "Urban",
                            "max_speed_kmh": 80.0,
                            "points": [[37.9482, 23.6425], [37.9926, 23.7203]],
                        }
                    ],
                }
            ],
            "slow_orders": [
                {
                    "id": "TSR-1",
                    "corridor_id": "pathe-mainline",
                    "name": "Slow 1",
                    "max_speed_kmh": 40.0,
                    "points": [[38.862, 22.42], [38.875, 22.43]],
                    "reason": "Test",
                    "active": True,
                }
            ],
        }
        rules, tsrs = parse_speed_limits_json(payload)
        self.assertIn("pathe-mainline", rules)
        self.assertEqual(rules["pathe-mainline"].default_speed_kmh, 160.0)
        self.assertEqual(len(rules["pathe-mainline"].segments), 1)
        self.assertEqual(len(tsrs), 1)
        self.assertEqual(tsrs[0].id, "TSR-1")

    def test_parse_speed_limits_json_invalid(self) -> None:
        rules, tsrs = parse_speed_limits_json("invalid json")
        self.assertTrue(len(rules) > 0)
        self.assertEqual(tsrs, [])

    def test_load_speed_limits_fixture(self) -> None:
        rules, tsrs = load_speed_limits()
        self.assertIn("pathe-mainline", rules)
        self.assertIn("proastiakos-airport-kiato", rules)
        self.assertTrue(len(tsrs) >= 3)
        active_tsrs = load_slow_orders()
        self.assertTrue(len(active_tsrs) >= 3)


class TestSpeedEvaluationMath(unittest.TestCase):
    """Test suite for evaluate_train_speed kinematic safety rules."""

    def setUp(self) -> None:
        self.rules, self.tsrs = load_speed_limits()

    def test_normal_speed_under_corridor_limit(self) -> None:
        # Train on open plains of Thessaly at 140 km/h (Limit: 160 km/h)
        pt = GeoPoint(lat=39.5050, lng=22.3680)
        res = evaluate_train_speed(
            point=pt,
            current_speed=140.0,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="IC52",
        )
        self.assertEqual(res.severity, OverspeedSeverity.NORMAL)
        self.assertEqual(res.governing_limit, 160.0)
        self.assertEqual(res.current_speed, 140.0)
        self.assertAlmostEqual(res.margin_kmh, -20.0)
        self.assertEqual(res.color_pair, 1)  # PAIR_DEFAULT
        self.assertFalse(res.is_tsr)

    def test_advisory_margin_near_limit(self) -> None:
        # Train at 164.0 km/h on 160 km/h line (delta +4 km/h < 10 km/h)
        pt = GeoPoint(lat=39.5050, lng=22.3680)
        res = evaluate_train_speed(
            point=pt,
            current_speed=164.0,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="IC52",
        )
        self.assertEqual(res.severity, OverspeedSeverity.ADVISORY)
        self.assertEqual(res.governing_limit, 160.0)
        self.assertAlmostEqual(res.margin_kmh, 4.0)
        self.assertEqual(res.color_pair, 8)  # PAIR_TRAIN_WARN
        self.assertEqual(res.status_label, "[WARN +4]")

    def test_general_overspeed_exceedance(self) -> None:
        # Train at 174.0 km/h on 160 km/h line (delta +14 km/h >= 10 km/h)
        pt = GeoPoint(lat=39.5050, lng=22.3680)
        res = evaluate_train_speed(
            point=pt,
            current_speed=174.0,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="IC52",
        )
        self.assertEqual(res.severity, OverspeedSeverity.OVERSPEED)
        self.assertEqual(res.governing_limit, 160.0)
        self.assertAlmostEqual(res.margin_kmh, 14.0)
        self.assertEqual(res.color_pair, 9)  # PAIR_TRAIN_ALERT
        self.assertEqual(res.status_label, "[OVERSPEED +14]")

    def test_segment_level_speed_limit(self) -> None:
        # Train in Athens urban section (Piraeus-Athens limit 80 km/h) running at 95 km/h
        pt = GeoPoint(lat=37.9730, lng=23.6940)
        res = evaluate_train_speed(
            point=pt,
            current_speed=95.0,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="1597",
        )
        self.assertEqual(res.severity, OverspeedSeverity.OVERSPEED)
        self.assertEqual(res.governing_limit, 80.0)
        self.assertAlmostEqual(res.margin_kmh, 15.0)
        self.assertEqual(res.status_label, "[OVERSPEED +15]")
        self.assertEqual(res.color_pair, 9)

    def test_tsr_zone_compliant(self) -> None:
        # Train in Spercheios TSR zone (limit 40 km/h) operating at 38 km/h
        pt = GeoPoint(lat=38.8680, lng=22.4250)
        res = evaluate_train_speed(
            point=pt,
            current_speed=38.0,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="1599",
        )
        self.assertEqual(res.severity, OverspeedSeverity.NORMAL)
        self.assertTrue(res.is_tsr)
        self.assertEqual(res.governing_limit, 40.0)
        self.assertEqual(res.status_label, "[TSR: 40 km/h]")
        self.assertEqual(res.color_pair, 8)  # PAIR_TRAIN_WARN

    def test_tsr_zone_advisory_margin(self) -> None:
        # Train in Spercheios TSR (limit 40 km/h) at 43.0 km/h (delta +3 < 5)
        pt = GeoPoint(lat=38.8680, lng=22.4250)
        res = evaluate_train_speed(
            point=pt,
            current_speed=43.0,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="1599",
        )
        self.assertEqual(res.severity, OverspeedSeverity.ADVISORY)
        self.assertTrue(res.is_tsr)
        self.assertEqual(res.governing_limit, 40.0)
        self.assertEqual(res.status_label, "[TSR WARN +3]")
        self.assertEqual(res.color_pair, 8)

    def test_tsr_zone_violation_exceedance(self) -> None:
        # Train in Spercheios TSR (limit 40 km/h) running at 54.0 km/h (delta +14 >= 5)
        pt = GeoPoint(lat=38.8680, lng=22.4250)
        res = evaluate_train_speed(
            point=pt,
            current_speed=54.0,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="1599",
        )
        self.assertEqual(res.severity, OverspeedSeverity.TSR_VIOLATION)
        self.assertTrue(res.is_tsr)
        self.assertEqual(res.governing_limit, 40.0)
        self.assertEqual(res.status_label, "[TSR OVERSPEED +14]")
        self.assertEqual(res.color_pair, 9)  # PAIR_TRAIN_ALERT

    def test_unmapped_corridor_fallback(self) -> None:
        # Unmapped corridor with no segment match falls back to 120.0 km/h
        pt = GeoPoint(lat=35.0, lng=25.0)
        res = evaluate_train_speed(
            point=pt,
            current_speed=135.0,
            corridor_id="unknown-corridor",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="999",
        )
        self.assertEqual(res.governing_limit, 120.0)
        self.assertEqual(res.severity, OverspeedSeverity.OVERSPEED)
        self.assertAlmostEqual(res.margin_kmh, 15.0)

    def test_zero_speed_or_none(self) -> None:
        # Train stationary at 0 km/h or None speed
        pt = GeoPoint(lat=38.8680, lng=22.4250)
        res = evaluate_train_speed(
            point=pt,
            current_speed=0.0,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="1599",
        )
        self.assertEqual(res.severity, OverspeedSeverity.NORMAL)
        self.assertEqual(res.current_speed, 0.0)

        res_none = evaluate_train_speed(
            point=pt,
            current_speed=None,
            corridor_id="pathe-mainline",
            rules=self.rules,
            active_tsrs=self.tsrs,
            train_id="1599",
        )
        self.assertEqual(res_none.severity, OverspeedSeverity.NORMAL)
        self.assertEqual(res_none.current_speed, 0.0)


if __name__ == "__main__":
    unittest.main()
