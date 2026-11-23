"""Unit tests for GTFS-Realtime v2.0 DTOs, binary protobuf, and ProtoJSON codecs."""

import unittest

from gtfs_rt.models import (
    Alert,
    AlertCause,
    AlertEffect,
    AlertSeverity,
    CongestionLevel,
    EntitySelector,
    FeedEntity,
    FeedHeader,
    FeedMessage,
    GtfsRtConfig,
    Incrementality,
    Position,
    ScheduleRel,
    StopScheduleRel,
    StopTimeEvent,
    StopTimeUpdate,
    TimeRange,
    TranslatedString,
    Translation,
    TripDescriptor,
    TripUpdate,
    VehicleDescriptor,
    VehiclePosition,
    VehicleStopStatus,
)
from gtfs_rt.wire import WireDecodeError, encode_string


class TestGtfsModels(unittest.TestCase):
    """Validate GTFS-RT v2.0 binary round-trips, proto2 presence, and ProtoJSON."""

    def test_full_feed_message_binary_roundtrip(self):
        """TC-GTFS-MOD-01: Complete FeedMessage with VehiclePosition, TripUpdate, and Alert round-trips."""
        trip = TripDescriptor(
            trip_id="6a94745bd16c475ca144e488",
            start_time="17:58:00",
            start_date="20261123",
            schedule_relationship=ScheduleRel.SCHEDULED,
            route_id="OSE-PATHE-IC",
            direction_id=1,
        )
        veh = VehicleDescriptor(id="6b044438", label="IC56", license_plate="220005")
        vp = VehiclePosition(
            trip=trip,
            position=Position(
                latitude=39.98123,
                longitude=22.62886,
                bearing=305.25,
                odometer=365600.0,
                speed=39.44,
            ),
            current_stop_sequence=4,
            current_status=VehicleStopStatus.INCOMING_AT,
            timestamp=1795469558,
            congestion_level=CongestionLevel.RUNNING_SMOOTHLY,
            stop_id="KAT",
            vehicle=veh,
        )
        tu = TripUpdate(
            trip=trip,
            stop_time_update=(
                StopTimeUpdate(
                    stop_sequence=4,
                    stop_id="KAT",
                    arrival=StopTimeEvent(delay=-120, time=1795470000, uncertainty=30),
                    departure=StopTimeEvent(delay=-60, time=1795470120, uncertainty=30),
                    schedule_relationship=StopScheduleRel.SCHEDULED,
                ),
                StopTimeUpdate(
                    stop_sequence=5,
                    stop_id="PLA",
                    schedule_relationship=StopScheduleRel.SKIPPED,
                ),
            ),
            vehicle=veh,
            timestamp=1795469558,
            delay=-120,
        )
        alert = Alert(
            active_period=(TimeRange(start=1795469000, end=1795475000),),
            informed_entity=(
                EntitySelector(
                    agency_id="OSE",
                    route_id="OSE-PATHE-IC",
                    route_type=2,
                    trip=trip,
                    stop_id="KAT",
                    direction_id=1,
                ),
            ),
            cause=AlertCause.TECHNICAL_PROBLEM,
            effect=AlertEffect.SIGNIFICANT_DELAYS,
            url=TranslatedString((Translation("https://railway.gov.gr/train-tracker", "en"),)),
            header_text=TranslatedString(
                (
                    Translation("Καθυστέρηση IC56", "el"),
                    Translation("IC56 Delay", "en"),
                )
            ),
            description_text=TranslatedString((Translation("Signal fault near Larissa.", "en"),)),
            severity_level=AlertSeverity.SEVERE,
        )

        feed = FeedMessage(
            header=FeedHeader(
                gtfs_realtime_version="2.0",
                incrementality=Incrementality.FULL_DATASET,
                timestamp=1795469559,
                feed_version="2026.11",
            ),
            entity=(
                FeedEntity(id="al:1", alert=alert),
                FeedEntity(id="tu:1", trip_update=tu),
                FeedEntity(id="vp:1", vehicle=vp),
            ),
        )

        raw = feed.to_proto()
        decoded = FeedMessage.from_proto(raw)

        self.assertEqual(decoded.header, feed.header)
        self.assertEqual(len(decoded.entity), 3)
        self.assertEqual(decoded.entity[0], feed.entity[0])
        self.assertEqual(decoded.entity[1], feed.entity[1])

        dec_vp = decoded.entity[2].vehicle
        self.assertIsNotNone(dec_vp)
        assert dec_vp is not None and dec_vp.position is not None
        self.assertAlmostEqual(dec_vp.position.latitude, 39.98123, places=4)
        self.assertAlmostEqual(dec_vp.position.longitude, 22.62886, places=4)
        self.assertAlmostEqual(dec_vp.position.bearing or 0.0, 305.25, places=2)
        self.assertAlmostEqual(dec_vp.position.odometer or 0.0, 365600.0, places=3)
        self.assertAlmostEqual(dec_vp.position.speed or 0.0, 39.44, places=2)
        self.assertEqual(dec_vp.current_status, VehicleStopStatus.INCOMING_AT)
        self.assertEqual(decoded.entity[1].trip_update.delay, -120)

    def test_proto2_zero_value_explicit_presence(self):
        """TC-GTFS-MOD-02: Zero-valued optional fields (delay=0, INCOMING_AT=0) are explicitly preserved."""
        tu = TripUpdate(trip=TripDescriptor(trip_id="T1"), delay=0)
        dec_tu = TripUpdate.from_proto(tu.to_proto())
        self.assertEqual(dec_tu.delay, 0)

        vp = VehiclePosition(current_status=VehicleStopStatus.INCOMING_AT, current_stop_sequence=0)
        dec_vp = VehiclePosition.from_proto(vp.to_proto())
        self.assertEqual(dec_vp.current_status, VehicleStopStatus.INCOMING_AT)
        self.assertEqual(dec_vp.current_stop_sequence, 0)

        ent = FeedEntity(id="del:1", is_deleted=True)
        dec_ent = FeedEntity.from_proto(ent.to_proto())
        self.assertTrue(dec_ent.is_deleted)

    def test_unknown_extension_fields_ignored_cleanly(self):
        """TC-GTFS-MOD-03: Unknown extension tags (1000..1999) are skipped without error."""
        hdr = FeedHeader(gtfs_realtime_version="2.0", timestamp=1700000000)
        raw_with_ext = hdr.to_proto() + encode_string(1001, "custom-agency-ext")
        decoded = FeedHeader.from_proto(raw_with_ext)
        self.assertEqual(decoded.gtfs_realtime_version, "2.0")
        self.assertEqual(decoded.timestamp, 1700000000)

    def test_protojson_mapping_conventions(self):
        """TC-GTFS-MOD-04: ProtoJSON emits camelCase keys, stringified uint64/int64, and enum names."""
        feed = FeedMessage(
            header=FeedHeader(timestamp=1795469559),
            entity=(
                FeedEntity(
                    id="vp:ic52",
                    vehicle=VehiclePosition(
                        trip=TripDescriptor(trip_id="IC52", route_id="OSE-PATHE-IC"),
                        position=Position(latitude=38.0, longitude=23.7, speed=25.0),
                        current_status=VehicleStopStatus.IN_TRANSIT_TO,
                        timestamp=1795469558,
                    ),
                ),
            ),
        )
        as_dict = feed.to_dict()
        self.assertEqual(as_dict["header"]["gtfsRealtimeVersion"], "2.0")
        self.assertEqual(as_dict["header"]["incrementality"], "FULL_DATASET")
        self.assertEqual(as_dict["header"]["timestamp"], "1795469559")
        vp_dict = as_dict["entity"][0]["vehicle"]
        self.assertEqual(vp_dict["currentStatus"], "IN_TRANSIT_TO")
        self.assertEqual(vp_dict["timestamp"], "1795469558")

    def test_validation_guards_on_models_and_config(self):
        """TC-GTFS-MOD-05: Out-of-range coordinates, ports, and missing required fields raise."""
        with self.assertRaises(ValueError):
            Position(latitude=95.0, longitude=23.7)
        with self.assertRaises(ValueError):
            Position(latitude=38.0, longitude=-181.0)
        with self.assertRaises(ValueError):
            Position(latitude=38.0, longitude=23.7, speed=-1.0)
        with self.assertRaises(ValueError):
            Position(latitude=38.0, longitude=23.7, bearing=361.0)
        with self.assertRaises(ValueError):
            FeedEntity(id="   ")
        with self.assertRaises(ValueError):
            GtfsRtConfig(port=0)
        with self.assertRaises(ValueError):
            GtfsRtConfig(port=70000)
        with self.assertRaises(ValueError):
            GtfsRtConfig(refresh_interval_sec=0.0)
        with self.assertRaises(ValueError):
            GtfsRtConfig(station_proximity_m=500.0, incoming_proximity_m=100.0)
        with self.assertRaises(WireDecodeError):
            FeedMessage.from_proto(b"")
        with self.assertRaises(WireDecodeError):
            FeedHeader.from_proto(b"")


if __name__ == "__main__":
    unittest.main()
