"""
Unit tests for deadband filtering and telemetry coordinate compression.
"""

import math
import unittest

from storage.filter import DeadbandConfig, DeadbandFilter
from train_tracker import TrainPosition


def _create_sample_position(
    train_number: str = "IC52",
    lat: float = 38.0,
    lng: float = 23.5,
    speed: float = 80.0,
    delay: int = 0,
    status: str = "Κανονική",
    next_station: str = "Λειανοκλάδι",
    schedule_id: str = "SCHED-1",
) -> TrainPosition:
    """Helper to instantiate test TrainPosition entities."""
    return TrainPosition(
        id=f"id-{train_number}",
        train_number=train_number,
        locomotive_number="120-001",
        name=f"Intercity {train_number}",
        train_type="Intercity",
        service_type="Passenger",
        status=status,
        origin="Αθήνα",
        destination="Θεσσαλονίκη",
        next_station=next_station,
        speed=speed,
        course=15.0,
        track_heading=15.0,
        lat=lat,
        lng=lng,
        altitude=150.0,
        delay=delay,
        progress=0.35,
        chainage_m=120000,
        corridor="ATH-SKG",
        distance_to_destination_m=380000,
        timestamp="2026-11-18T12:00:00Z",
        schedule_id=schedule_id,
    )


class TestDeadbandFilter(unittest.TestCase):
    """Test suite for DeadbandFilter admission rules, Haversine math, and metrics."""

    def setUp(self):
        self.config = DeadbandConfig(
            min_distance_m=15.0,
            min_delay_delta_min=1,
            min_speed_delta_kmh=5.0,
            max_heartbeat_interval_sec=60.0,
        )
        self.filter = DeadbandFilter(self.config)

    def test_initial_observation_always_recorded(self):
        """First observation for any vehicle must be admitted with reason 'initial'."""
        pos = _create_sample_position("IC52")
        should_save, reason = self.filter.should_record(pos, now_epoch=1000.0)

        self.assertTrue(should_save)
        self.assertEqual(reason, "initial")
        self.assertIn(DeadbandFilter._state_key(pos), self.filter._states)

    def test_stationary_train_under_distance_threshold_suppressed(self):
        """Stationary movement below min_distance_m must be suppressed."""
        pos1 = _create_sample_position("IC52", lat=38.000000, lng=23.500000)
        self.filter.should_record(pos1, now_epoch=1000.0)

        # Move by ~5 meters north (1 deg lat ~= 111,000m -> 0.00005 deg ~= 5.55m)
        pos2 = _create_sample_position("IC52", lat=38.000045, lng=23.500000)
        should_save, reason = self.filter.should_record(pos2, now_epoch=1005.0)

        self.assertFalse(should_save)
        self.assertIsNone(reason)
        self.assertEqual(self.filter.stats["total_suppressed"], 1.0)

    def test_moving_train_exceeding_distance_threshold_admitted(self):
        """Movement exceeding min_distance_m (15m) must be admitted with reason 'distance'."""
        pos1 = _create_sample_position("IC52", lat=38.000000, lng=23.500000)
        self.filter.should_record(pos1, now_epoch=1000.0)

        # Move by ~55 meters north (0.0005 deg ~= 55.5m)
        pos2 = _create_sample_position("IC52", lat=38.000500, lng=23.500000)
        should_save, reason = self.filter.should_record(pos2, now_epoch=1005.0)

        self.assertTrue(should_save)
        self.assertEqual(reason, "distance")

    def test_delay_change_triggers_admission_even_if_stationary(self):
        """Delay change >= 1 min must trigger admission even if train is motionless."""
        pos1 = _create_sample_position("IC52", delay=0)
        self.filter.should_record(pos1, now_epoch=1000.0)

        # Identical location, but delay increases by 1 minute
        pos2 = _create_sample_position("IC52", delay=1)
        should_save, reason = self.filter.should_record(pos2, now_epoch=1005.0)

        self.assertTrue(should_save)
        self.assertEqual(reason, "delay")

    def test_status_transition_triggers_admission(self):
        """Status change (e.g. Κανονική -> Στάση) must trigger admission."""
        pos1 = _create_sample_position("IC52", status="Κανονική")
        self.filter.should_record(pos1, now_epoch=1000.0)

        # Train stops at signal
        pos2 = _create_sample_position("IC52", status="Στάση")
        should_save, reason = self.filter.should_record(pos2, now_epoch=1005.0)

        self.assertTrue(should_save)
        self.assertEqual(reason, "status")

    def test_next_station_change_triggers_admission(self):
        """Next station change must trigger admission."""
        pos1 = _create_sample_position("IC52", next_station="Λειανοκλάδι")
        self.filter.should_record(pos1, now_epoch=1000.0)

        pos2 = _create_sample_position("IC52", next_station="Τιθορέα")
        should_save, reason = self.filter.should_record(pos2, now_epoch=1005.0)

        self.assertTrue(should_save)
        self.assertEqual(reason, "station")

    def test_schedule_change_triggers_admission(self):
        """Schedule change must trigger admission."""
        pos1 = _create_sample_position("IC52", schedule_id="SCHED-1")
        self.filter.should_record(pos1, now_epoch=1000.0)

        pos2 = _create_sample_position("IC52", schedule_id="SCHED-2")
        should_save, reason = self.filter.should_record(pos2, now_epoch=1005.0)

        self.assertTrue(should_save)
        self.assertEqual(reason, "schedule")

    def test_speed_change_triggers_admission(self):
        """Speed delta >= 5 km/h must trigger admission."""
        pos1 = _create_sample_position("IC52", speed=100.0)
        self.filter.should_record(pos1, now_epoch=1000.0)

        # Slows down to 90 km/h (delta = 10 km/h >= 5.0)
        pos2 = _create_sample_position("IC52", speed=90.0)
        should_save, reason = self.filter.should_record(pos2, now_epoch=1005.0)

        self.assertTrue(should_save)
        self.assertEqual(reason, "speed")

    def test_heartbeat_triggers_admission_after_timeout(self):
        """Stationary train must be admitted after max_heartbeat_interval_sec."""
        pos1 = _create_sample_position("IC52")
        self.filter.should_record(pos1, now_epoch=1000.0)

        # 30 seconds later -> suppressed
        pos2 = _create_sample_position("IC52")
        should_save, _ = self.filter.should_record(pos2, now_epoch=1030.0)
        self.assertFalse(should_save)

        # 61 seconds after pos1 -> heartbeat triggered
        pos3 = _create_sample_position("IC52")
        should_save, reason = self.filter.should_record(pos3, now_epoch=1061.0)
        self.assertTrue(should_save)
        self.assertEqual(reason, "heartbeat")

    def test_haversine_formula_accuracy(self):
        """Haversine calculation must be accurate against known geodesic distances."""
        # Athens (37.9838° N, 23.7275° E) to Thessaloniki (40.6401° N, 22.9444° E)
        dist_m = DeadbandFilter.haversine_distance(37.9838, 23.7275, 40.6401, 22.9444)
        dist_km = dist_m / 1000.0

        # Known geodesic distance is approximately 302 km
        self.assertAlmostEqual(dist_km, 302.5, delta=3.0)

        # Zero distance check
        self.assertEqual(DeadbandFilter.haversine_distance(38.0, 23.0, 38.0, 23.0), 0.0)

    def test_filter_batch_and_statistics(self):
        """filter_batch must filter out duplicate snapshots and report correct statistics."""
        trains = [
            _create_sample_position("T1", lat=38.0, lng=23.0),
            _create_sample_position("T2", lat=38.1, lng=23.1),
        ]
        # Initial batch -> both admitted
        admitted = self.filter.filter_batch(trains, now_epoch=1000.0)
        self.assertEqual(len(admitted), 2)

        # Second batch with no movement -> both suppressed
        admitted_2 = self.filter.filter_batch(trains, now_epoch=1005.0)
        self.assertEqual(len(admitted_2), 0)

        stats = self.filter.stats
        self.assertEqual(stats["total_inspected"], 4.0)
        self.assertEqual(stats["total_admitted"], 2.0)
        self.assertEqual(stats["total_suppressed"], 2.0)
        self.assertEqual(stats["suppression_ratio_pct"], 50.0)
        self.assertEqual(stats["tracked_trains"], 2.0)

    def test_filter_reset(self):
        """reset() must clear state cache and metrics."""
        pos = _create_sample_position("IC52")
        self.filter.should_record(pos, now_epoch=1000.0)
        self.assertEqual(len(self.filter._states), 1)

        self.filter.reset()
        self.assertEqual(len(self.filter._states), 0)
        self.assertEqual(self.filter.stats["total_inspected"], 0.0)
        self.assertEqual(self.filter.stats["total_evicted"], 0.0)

    def test_deadband_config_defaults(self):
        """TC-P2-07: DeadbandConfig defines 1800s timeout and 300s prune interval by default."""
        cfg = DeadbandConfig()
        self.assertEqual(cfg.stale_timeout_sec, 1800.0)
        self.assertEqual(cfg.stale_prune_interval_sec, 300.0)

    def test_prune_stale_states_manual(self):
        """TC-P2-07: prune_stale_states evicts vehicles exceeding max_age_sec."""
        pos1 = _create_sample_position("IC52")
        pos2 = _create_sample_position("IC53")
        self.filter.should_record(pos1, now_epoch=1000.0)
        self.filter.should_record(pos2, now_epoch=1500.0)

        # At t=1700, IC52 is 700s old (> 600s); IC53 is 200s old (<= 600s)
        evicted = self.filter.prune_stale_states(max_age_sec=600.0, now_epoch=1700.0)
        self.assertEqual(evicted, 1)
        self.assertNotIn(DeadbandFilter._state_key(pos1), self.filter._states)
        self.assertIn(DeadbandFilter._state_key(pos2), self.filter._states)
        self.assertEqual(self.filter.stats["total_evicted"], 1.0)
        self.assertEqual(self.filter.stats["tracked_trains"], 1.0)

    def test_auto_prune_stale_states_on_should_record(self):
        """TC-P2-08: should_record auto-prunes stale states when prune_interval has elapsed."""
        cfg = DeadbandConfig(stale_timeout_sec=600.0, stale_prune_interval_sec=300.0)
        filt = DeadbandFilter(cfg)

        pos1 = _create_sample_position("IC52")
        pos2 = _create_sample_position("IC53")
        filt.should_record(pos1, now_epoch=1000.0)
        filt.should_record(pos2, now_epoch=1050.0)
        self.assertEqual(len(filt._states), 2)

        # Ingestion at t=1700 triggers auto-prune (1700 - 0 >= 300; both > 600s old)
        pos3 = _create_sample_position("IC54")
        admitted, reason = filt.should_record(pos3, now_epoch=1700.0)

        self.assertTrue(admitted)
        self.assertEqual(reason, "initial")
        self.assertNotIn(DeadbandFilter._state_key(pos1), filt._states)
        self.assertNotIn(DeadbandFilter._state_key(pos2), filt._states)
        self.assertIn(DeadbandFilter._state_key(pos3), filt._states)
        self.assertEqual(filt.stats["total_evicted"], 2.0)
        self.assertEqual(filt.stats["tracked_trains"], 1.0)

    def test_reemergent_train_admitted_as_initial(self):
        """TC-P2-09: Vehicle evicted after dormancy is safely admitted as 'initial' when re-observed."""
        pos = _create_sample_position("IC52", lat=38.0, lng=23.5)
        # First observation -> admitted initial
        adm1, r1 = self.filter.should_record(pos, now_epoch=1000.0)
        self.assertTrue(adm1)
        self.assertEqual(r1, "initial")

        # Evict IC52
        self.filter.prune_stale_states(max_age_sec=600.0, now_epoch=2000.0)
        self.assertNotIn("IC52", self.filter._states)

        # Stationary re-appearance at t=2500 -> admitted as 'initial'
        adm2, r2 = self.filter.should_record(pos, now_epoch=2500.0)
        self.assertTrue(adm2)
        self.assertEqual(r2, "initial")

    def test_evaluate_batch_is_pure_with_intra_batch_staging_until_commit(self):
        """TC-STO-17: evaluate_batch performs intra-batch staging with zero pre-commit mutation until commit_batch."""
        p1 = _create_sample_position("IC52", lat=38.000000, lng=23.500000, status="Κανονική")
        # +5.5m -> suppressed against p1
        p2 = _create_sample_position("IC52", lat=38.000045, lng=23.500000, status="Κανονική")
        # +33m -> admitted against p1
        p3 = _create_sample_position("IC52", lat=38.000300, lng=23.500000, status="Κανονική")
        # +5m from p3 -> suppressed against staged p3
        p4 = _create_sample_position("IC52", lat=38.000345, lng=23.500000, status="Κανονική")
        # Status transition -> admitted against staged p3
        p5 = _create_sample_position("IC52", lat=38.000345, lng=23.500000, status="Στάση")

        evaluation = self.filter.evaluate_batch([p1, p2, p3, p4, p5], now_epoch=1000.0)
        self.assertEqual(evaluation.inspected_count, 5)
        self.assertEqual(evaluation.admitted_count, 3)
        self.assertEqual(evaluation.suppressed_count, 2)
        self.assertEqual(len(evaluation.admitted), 3)
        self.assertEqual(evaluation.admitted[0], p1)
        self.assertEqual(evaluation.admitted[1], p3)
        self.assertEqual(evaluation.admitted[2], p5)

        # ZERO mutation to live filter state or stats before commit_batch()
        self.assertEqual(len(self.filter._states), 0)
        self.assertEqual(self.filter.stats["total_inspected"], 0.0)
        self.assertEqual(self.filter.stats["total_admitted"], 0.0)
        self.assertEqual(self.filter.stats["total_suppressed"], 0.0)

        # Re-evaluating before commit produces identical result
        eval_retry = self.filter.evaluate_batch([p1, p2, p3, p4, p5], now_epoch=1000.0)
        self.assertEqual(eval_retry.admitted_count, 3)

        # Promote post-commit
        self.filter.commit_batch(evaluation, now_epoch=1000.0)
        p5_key = DeadbandFilter._state_key(p5)
        self.assertIn(p5_key, self.filter._states)
        self.assertEqual(self.filter._states[p5_key].status, "Στάση")
        self.assertEqual(self.filter.stats["total_inspected"], 5.0)
        self.assertEqual(self.filter.stats["total_admitted"], 3.0)
        self.assertEqual(self.filter.stats["total_suppressed"], 2.0)

        # Subsequent duplicate of p5 is now suppressed
        eval_after = self.filter.evaluate_batch([p5], now_epoch=1005.0)
        self.assertEqual(eval_after.admitted_count, 0)
        self.assertEqual(eval_after.suppressed_count, 1)

    def test_consist_aware_deadband_isolates_coupled_units_sharing_train_number(self):
        """TC-STO-21: Coupled consists sharing train_number maintain independent deadband baselines."""
        from dataclasses import replace

        base = _create_sample_position(
            "2232",
            lat=38.0000,
            lng=23.5000,
            speed=80.0,
            delay=0,
            status="Κανονική",
            next_station="Λειανοκλάδι",
            schedule_id="SCHED-2232",
        )
        unit_a = replace(base, id="unit-a", locomotive_number="460103")
        unit_b = replace(base, id="unit-b", locomotive_number="460108")
        # Both units must be admitted as initial despite identical train_number & coordinates
        ev = self.filter.evaluate_batch([unit_a, unit_b], now_epoch=1000.0)
        self.assertEqual(ev.admitted_count, 2)
        self.filter.commit_batch(ev, now_epoch=1000.0)
        self.assertEqual(len(self.filter._states), 2)

        # Stationary repeats for both units are independently suppressed
        ev2 = self.filter.evaluate_batch([unit_a, unit_b], now_epoch=1005.0)
        self.assertEqual(ev2.admitted_count, 0)
        self.assertEqual(ev2.suppressed_count, 2)


if __name__ == "__main__":
    unittest.main()
