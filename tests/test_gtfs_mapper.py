"""Unit tests for Hellenic Railways GTFS-RT domain mapper and caches (gtfs_rt.mapper)."""

import unittest

from gtfs_rt.mapper import (
    FeedCache,
    GtfsRtMapper,
    ScheduleCache,
    StationIndex,
    norm_station_key,
)
from gtfs_rt.models import (
    AlertCause,
    AlertEffect,
    AlertSeverity,
    FeedKind,
    GtfsRtConfig,
    StopIdField,
    StopScheduleRel,
    VehicleStopStatus,
)
from train_tracker import TrainPosition
from tui.geo import ConflictAlert, ConflictSeverity, ConflictType
from tui.mock_engine import MockTelemetryEngine
from tui.state import TrainStateStore


def _make_pos(**overrides) -> TrainPosition:
    """Create a valid default Hellenic Railways TrainPosition for mapper tests."""
    defaults = dict(
        id="6b044438ebbd3d8ac01b25eb",
        train_number="IC56",
        locomotive_number="120-024",
        name="InterCity 56",
        train_type="intercity",
        service_type="InterCity",
        status="Κανονική",
        origin="Αθήνα",
        destination="Θεσσαλονίκη",
        next_station="Λάρισα",
        speed=144.0,  # 40.0 m/s
        course=330.0,
        track_heading=340.5,
        lat=39.4000,
        lng=22.4000,
        altitude=120.0,
        delay=5,  # +300 s
        progress=0.5,
        chainage_m=320000,
        corridor="E85",
        distance_to_destination_m=150000,
        timestamp="2026-11-23T22:30:00Z",  # 2026-11-24 00:30 EET in Athens!
        schedule_id="6a94745bd16c475ca144e488",
    )
    defaults.update(overrides)
    return TrainPosition(**defaults)


class TestGtfsMapper(unittest.TestCase):
    """Verify domain translation, unit conversions, timezone, dedup, and alerts."""

    def test_unit_conversions_and_athens_service_date_rollover(self):
        """TC-GTFS-MAP-01: Converts km/h->m/s, min->sec, and UTC 22:30 -> next day in Europe/Athens."""
        mapper = GtfsRtMapper()
        pos = _make_pos(delay=-2, speed=108.0)  # -120s early, 30.0 m/s
        feed = mapper.build_from_positions([pos], feed_kind=FeedKind.COMBINED, now=1795473000.0)

        self.assertEqual(feed.header.gtfs_realtime_version, "2.0")
        by_id = {e.id: e for e in feed.entity}
        vp = by_id["vp:6b044438ebbd3d8ac01b25eb"].vehicle
        tu = by_id["tu:6a94745bd16c475ca144e488"].trip_update

        assert vp is not None and vp.position is not None and vp.trip is not None
        assert tu is not None
        self.assertAlmostEqual(vp.position.speed or 0.0, 30.0, places=3)
        self.assertAlmostEqual(vp.position.bearing or 0.0, 340.5, places=2)
        self.assertAlmostEqual(vp.position.odometer or 0.0, 320000.0, places=1)
        self.assertEqual(vp.trip.route_id, "OSE-PATHE-IC")
        # 2026-11-23T22:30:00Z is 2026-11-24 00:30 EET (+02:00)
        self.assertEqual(vp.trip.start_date, "20261124")
        self.assertEqual(tu.delay, -120)

    def test_coupled_consist_deduplicates_trip_updates_only(self):
        """TC-GTFS-MAP-02: Coupled EMUs emit distinct VehiclePositions but one deduplicated TripUpdate."""
        p1 = _make_pos(
            id="unit-a",
            train_number="1597",
            locomotive_number="460103",
            schedule_id="sched-1597",
            corridor="PIRAIR",
            service_type="Suburban",
            delay=4,
            timestamp="2026-11-23T18:00:00Z",
        )
        p2 = _make_pos(
            id="unit-b",
            train_number="1597",
            locomotive_number="460108",
            schedule_id="sched-1597",
            corridor="PIRAIR",
            service_type="Suburban",
            delay=6,
            timestamp="2026-11-23T18:00:05Z",  # Fresher unit
        )
        mapper = GtfsRtMapper()
        feed = mapper.build_from_positions([p1, p2], feed_kind=FeedKind.COMBINED, now=1795456810.0)

        vps = [e for e in feed.entity if e.vehicle is not None]
        tus = [e for e in feed.entity if e.trip_update is not None]
        self.assertEqual(len(vps), 2)
        self.assertEqual({e.id for e in vps}, {"vp:unit-a", "vp:unit-b"})
        self.assertEqual(len(tus), 1)
        self.assertEqual(tus[0].id, "tu:sched-1597")
        assert tus[0].trip_update is not None
        self.assertEqual(tus[0].trip_update.delay, 360)  # Selected fresher unit-b (+6 min)

    def test_vehicle_stop_status_inference_and_stop_id_field(self):
        """TC-GTFS-MAP-03: Station proximity + speed drive STOPPED_AT, INCOMING_AT, IN_TRANSIT_TO."""
        idx = StationIndex()
        ath = idx.resolve("Αθήνα")
        self.assertIsNotNone(ath)
        assert ath is not None

        mapper_code = GtfsRtMapper(config=GtfsRtConfig(stop_id_field=StopIdField.CODE))

        # 1. Stopped right at Athens platform
        p_stop = _make_pos(
            id="t1",
            next_station="Αθήνα",
            lat=ath.point.lat,
            lng=ath.point.lng,
            speed=0.0,
        )
        # 2. Approaching within ~800m (approx +0.007 deg lat)
        p_inc = _make_pos(
            id="t2",
            next_station="Athens",
            lat=ath.point.lat + 0.007,
            lng=ath.point.lng,
            speed=60.0,
        )
        # 3. Far away (> 1500m)
        p_far = _make_pos(
            id="t3",
            next_station="Αθήνα",
            lat=ath.point.lat + 0.2,
            lng=ath.point.lng,
            speed=120.0,
        )

        feed = mapper_code.build_from_positions([p_stop, p_inc, p_far], feed_kind=FeedKind.VEHICLES)
        by_id = {e.id: e.vehicle for e in feed.entity}
        assert by_id["vp:t1"] and by_id["vp:t2"] and by_id["vp:t3"]
        self.assertEqual(by_id["vp:t1"].current_status, VehicleStopStatus.STOPPED_AT)
        self.assertEqual(by_id["vp:t1"].stop_id, ath.code)
        self.assertEqual(by_id["vp:t2"].current_status, VehicleStopStatus.INCOMING_AT)
        self.assertEqual(by_id["vp:t3"].current_status, VehicleStopStatus.IN_TRANSIT_TO)

    def test_passenger_freight_unassigned_and_corridor_filtering(self):
        """TC-GTFS-MAP-04: Freight and unassigned units excluded by default, opt-in via config."""
        passenger = _make_pos(id="pass-1", corridor="E85")
        freight = _make_pos(
            id="frg-1",
            train_number="80512",
            train_type="Freight",
            service_type="Εμπορικά",
        )
        unassigned = _make_pos(
            id="shunt-1",
            train_number="220029",
            train_type="unassigned",
            schedule_id=None,
            origin=None,
            destination=None,
        )

        default_feed = GtfsRtMapper().build_from_positions(
            [passenger, freight, unassigned], feed_kind=FeedKind.VEHICLES
        )
        self.assertEqual([e.id for e in default_feed.entity], ["vp:pass-1"])

        all_feed = GtfsRtMapper(
            config=GtfsRtConfig(include_freight=True, include_unassigned=True)
        ).build_from_positions([passenger, freight, unassigned], feed_kind=FeedKind.VEHICLES)
        self.assertEqual(len(all_feed.entity), 3)

        proast_only = GtfsRtMapper(
            config=GtfsRtConfig(corridor_filter="PROASTIAKOS")
        ).build_from_positions([passenger], feed_kind=FeedKind.VEHICLES)
        self.assertEqual(len(proast_only.entity), 0)

    def test_bilingual_delay_and_conflict_alerts(self):
        """TC-GTFS-MAP-05: Severe delays and safety conflicts synthesize bilingual el/en Alerts."""
        delayed = _make_pos(id="ic50", train_number="IC50", delay=35, schedule_id="sched-50")
        conflict = ConflictAlert(
            alert_id="CONF-1",
            train_id_1="ic50",
            train_number_1="IC50",
            train_id_2="ic51",
            train_number_2="IC51",
            corridor_id="pathe-mainline",
            corridor_name="PATHE Mainline",
            conflict_type=ConflictType.OPPOSING_COLLISION,
            severity=ConflictSeverity.CRITICAL,
            distance_km=1.8,
            closing_speed_kmh=240.0,
            time_to_collision_sec=27.0,
            is_single_track=True,
            station_proximity_name=None,
            description="Opposing mainline trains closing near Domokos",
            timestamp=1795469500.0,
        )
        feed = GtfsRtMapper(config=GtfsRtConfig(delay_alert_min=15)).build_from_positions(
            [delayed],
            conflicts=[conflict],
            feed_kind=FeedKind.ALERTS,
            now=1795469550.0,
        )
        by_id = {e.id: e.alert for e in feed.entity}
        self.assertIn("al:delay:sched-50", by_id)
        self.assertIn("al:conflict:CONF-1", by_id)

        d_alert = by_id["al:delay:sched-50"]
        assert d_alert is not None and d_alert.header_text is not None
        self.assertEqual(d_alert.effect, AlertEffect.SIGNIFICANT_DELAYS)
        self.assertEqual(d_alert.severity_level, AlertSeverity.SEVERE)
        langs = {t.language for t in d_alert.header_text.translation}
        self.assertEqual(langs, {"el", "en"})

        c_alert = by_id["al:conflict:CONF-1"]
        assert c_alert is not None
        self.assertEqual(c_alert.cause, AlertCause.TECHNICAL_PROBLEM)
        self.assertEqual(c_alert.effect, AlertEffect.MODIFIED_SERVICE)

    def test_enriched_schedule_stops_and_bounded_cache(self):
        """TC-GTFS-MAP-06: ScheduleCache bounds entries, skips T-test trains, and maps SKIPPED stops."""
        calls = []

        def fake_fetcher(sid, train_num):
            calls.append((sid, train_num))
            return {
                "scheduledDeparture": "2026-11-23T15:58:00.000Z",
                "stops": [
                    {
                        "order": 1,
                        "name": "Λάρισα",
                        "scheduledArrival": "2026-11-23T18:00:00.000Z",
                        "scheduledDeparture": "2026-11-23T18:03:00.000Z",
                        "status": "scheduled",
                    },
                    {
                        "order": 2,
                        "name": "Κατερίνη",
                        "status": "skipped",
                    },
                ],
            }

        cache = ScheduleCache(fetcher=fake_fetcher, max_entries=2, ttl_sec=60.0)
        self.assertIsNone(cache.get(None, "T101", now=100.0))
        self.assertEqual(len(calls), 0)

        # Populate and test LRU eviction bound (max_entries=2)
        cache.get("s1", "1", now=100.0)
        cache.get("s2", "2", now=101.0)
        cache.get("s3", "3", now=102.0)
        self.assertEqual(cache.size(), 2)

        mapper = GtfsRtMapper(
            config=GtfsRtConfig(enrich_schedules=True),
            schedule_cache=cache,
        )
        pos = _make_pos(schedule_id="s-rich", delay=10)
        feed = mapper.build_from_positions([pos], feed_kind=FeedKind.TRIPS, now=105.0)
        tu = feed.entity[0].trip_update
        assert tu is not None
        self.assertEqual(len(tu.stop_time_update), 2)
        self.assertEqual(tu.stop_time_update[0].stop_sequence, 1)
        assert tu.stop_time_update[0].arrival is not None
        self.assertEqual(tu.stop_time_update[0].arrival.delay, 600)
        self.assertEqual(tu.stop_time_update[1].schedule_relationship, StopScheduleRel.SKIPPED)

    def test_feed_cache_reuses_same_snapshot_version_and_invalidates(self):
        """TC-GTFS-MAP-07: FeedCache caches serialized bytes per snapshot version and invalidates on update."""
        store = TrainStateStore()
        engine = MockTelemetryEngine()
        store.update_positions(engine.step(1.0))

        cache = FeedCache(GtfsRtMapper())
        snap1 = store.get_snapshot()
        c1 = cache.get_or_build(snap1, FeedKind.COMBINED)
        c2 = cache.get_or_build(snap1, FeedKind.COMBINED)
        self.assertIs(c1, c2)
        self.assertGreater(c1.entity_count, 0)

        store.update_positions(engine.step(2.0))
        snap2 = store.get_snapshot()
        c3 = cache.get_or_build(snap2, FeedKind.COMBINED)
        self.assertNotEqual(c3.etag_pb, c1.etag_pb)
        self.assertEqual(norm_station_key("Αθήνα (Σ.Σ. Λαρίσης)"), "αθηνα σ σ λαρισησ")


if __name__ == "__main__":
    unittest.main()
