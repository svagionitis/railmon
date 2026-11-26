"""GTFS-Realtime v2.0 domain DTOs, enums, binary protobuf and ProtoJSON codecs.

Mirrors the official Google Transit `gtfs-realtime.proto` (v2.0, proto2)
specification with frozen immutable dataclasses and zero third-party packages.
"""

from dataclasses import dataclass
from enum import Enum, IntEnum
import math
from typing import Any, Dict, List, Optional, Tuple, Type, TypeVar, Union

from gtfs_rt.wire import (
    WireDecodeError,
    WireType,
    decode_double,
    decode_float,
    decode_utf8,
    encode_bool,
    encode_double,
    encode_enum,
    encode_float,
    encode_int32,
    encode_int64,
    encode_string,
    encode_submsg,
    encode_uint32,
    encode_uint64,
    iter_wire_fields,
    to_signed_int32,
    to_signed_int64,
)
from railmon_http import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    validate_allowed_hosts,
    validate_positive_finite_float,
    validate_positive_int,
)

GTFS_RT_VERSION: str = "2.0"

_E = TypeVar("_E", bound=IntEnum)


class Incrementality(IntEnum):
    """FeedHeader.Incrementality enum (gtfs-realtime.proto)."""

    FULL_DATASET = 0
    DIFFERENTIAL = 1


class ScheduleRel(IntEnum):
    """TripDescriptor.ScheduleRelationship enum (gtfs-realtime.proto)."""

    SCHEDULED = 0
    ADDED = 1
    UNSCHEDULED = 2
    CANCELED = 3
    REPLACEMENT = 5
    DUPLICATED = 6
    DELETED = 7


class StopScheduleRel(IntEnum):
    """StopTimeUpdate.ScheduleRelationship enum (gtfs-realtime.proto)."""

    SCHEDULED = 0
    SKIPPED = 1
    NO_DATA = 2
    UNSCHEDULED = 3


class VehicleStopStatus(IntEnum):
    """VehiclePosition.VehicleStopStatus enum (gtfs-realtime.proto)."""

    INCOMING_AT = 0
    STOPPED_AT = 1
    IN_TRANSIT_TO = 2


class CongestionLevel(IntEnum):
    """VehiclePosition.CongestionLevel enum (gtfs-realtime.proto)."""

    UNKNOWN_CONGESTION_LEVEL = 0
    RUNNING_SMOOTHLY = 1
    STOP_AND_GO = 2
    CONGESTION = 3
    SEVERE_CONGESTION = 4


class AlertCause(IntEnum):
    """Alert.Cause enum (gtfs-realtime.proto)."""

    UNKNOWN_CAUSE = 1
    OTHER_CAUSE = 2
    TECHNICAL_PROBLEM = 3
    STRIKE = 4
    DEMONSTRATION = 5
    ACCIDENT = 6
    HOLIDAY = 7
    WEATHER = 8
    MAINTENANCE = 9
    CONSTRUCTION = 10
    POLICE_ACTIVITY = 11
    MEDICAL_EMERGENCY = 12


class AlertEffect(IntEnum):
    """Alert.Effect enum (gtfs-realtime.proto)."""

    NO_SERVICE = 1
    REDUCED_SERVICE = 2
    SIGNIFICANT_DELAYS = 3
    DETOUR = 4
    ADDITIONAL_SERVICE = 5
    MODIFIED_SERVICE = 6
    OTHER_EFFECT = 7
    UNKNOWN_EFFECT = 8
    STOP_MOVED = 9
    NO_EFFECT = 10
    ACCESSIBILITY_ISSUE = 11


class AlertSeverity(IntEnum):
    """Alert.SeverityLevel enum (gtfs-realtime.proto)."""

    UNKNOWN_SEVERITY = 1
    INFO = 2
    WARNING = 3
    SEVERE = 4


class FeedKind(str, Enum):
    """Supported GTFS-RT entity scopes."""

    COMBINED = "combined"
    VEHICLES = "vehicles"
    TRIPS = "trips"
    ALERTS = "alerts"


class OutputFormat(str, Enum):
    """Supported GTFS-RT serialization formats."""

    PROTOBUF = "pb"
    JSON = "json"


class ExportMode(str, Enum):
    """Execution modes for railmon export --gtfs-rt."""

    SNAPSHOT = "snapshot"
    WATCH = "watch"
    SERVE = "serve"
    REPLAY = "replay"


class StopIdField(str, Enum):
    """Station attribute emitted as GTFS stop_id."""

    ID = "id"
    CODE = "code"


def _expect_int(wt: WireType, raw: Union[int, bytes], field_no: int) -> int:
    """Validate VARINT wire type and return integer value."""
    if wt != WireType.VARINT or not isinstance(raw, int):
        raise WireDecodeError(f"Expected VARINT for field {field_no}, got {wt.name}")
    return raw


def _expect_bytes(wt: WireType, raw: Union[int, bytes], field_no: int) -> bytes:
    """Validate LEN wire type and return raw byte slice."""
    if wt != WireType.LEN or not isinstance(raw, bytes):
        raise WireDecodeError(f"Expected LEN for field {field_no}, got {wt.name}")
    return raw


def _expect_f32(wt: WireType, raw: Union[int, bytes], field_no: int) -> float:
    """Validate FIXED32 wire type and decode IEEE-754 float."""
    if wt != WireType.FIXED32 or not isinstance(raw, bytes):
        raise WireDecodeError(f"Expected FIXED32 for field {field_no}, got {wt.name}")
    return decode_float(raw)


def _expect_f64(wt: WireType, raw: Union[int, bytes], field_no: int) -> float:
    """Validate FIXED64 wire type and decode IEEE-754 double."""
    if wt != WireType.FIXED64 or not isinstance(raw, bytes):
        raise WireDecodeError(f"Expected FIXED64 for field {field_no}, got {wt.name}")
    return decode_double(raw)


def _decode_enum(enum_cls: Type[_E], raw: int, default: _E) -> _E:
    """Safely convert decoded protobuf varint into an IntEnum member."""
    signed = to_signed_int32(raw)
    try:
        return enum_cls(signed)
    except ValueError:
        return default


@dataclass(frozen=True)
class GtfsRtConfig:
    """Immutable runtime configuration for GTFS-RT mapping, file export, and HTTP serving."""

    agency_id: str = "OSE"
    feed_version: Optional[str] = None
    include_unassigned: bool = False
    include_freight: bool = False
    include_alerts: bool = True
    include_ghosts: bool = True
    delay_alert_min: int = 15
    station_proximity_m: float = 250.0
    incoming_proximity_m: float = 1500.0
    stop_id_field: StopIdField = StopIdField.ID
    enrich_schedules: bool = False
    corridor_filter: Optional[str] = None
    host: str = "127.0.0.1"
    port: int = 8085
    refresh_interval_sec: float = 10.0
    gzip_compression: bool = True
    cors_enabled: bool = False
    demo_mode: bool = False
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC
    allowed_hosts: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate configuration bounds and numeric invariants."""
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ValueError(f"Port must be between 1 and 65535, got {self.port}")
        if not isinstance(self.cors_enabled, bool):
            raise ValueError(f"cors_enabled must be a bool, got {self.cors_enabled!r}")
        if not math.isfinite(self.refresh_interval_sec) or self.refresh_interval_sec <= 0.0:
            raise ValueError(f"refresh_interval_sec must be > 0, got {self.refresh_interval_sec}")
        if isinstance(self.delay_alert_min, bool) or self.delay_alert_min < 1:
            raise ValueError(f"delay_alert_min must be >= 1, got {self.delay_alert_min}")
        if not math.isfinite(self.station_proximity_m) or self.station_proximity_m <= 0.0:
            raise ValueError("station_proximity_m must be finite and > 0")
        if (
            not math.isfinite(self.incoming_proximity_m)
            or self.incoming_proximity_m < self.station_proximity_m
        ):
            raise ValueError("incoming_proximity_m must be >= station_proximity_m")
        validate_positive_int("max_connections", self.max_connections, minimum=1)
        validate_positive_finite_float("request_timeout_sec", self.request_timeout_sec)
        object.__setattr__(self, "allowed_hosts", validate_allowed_hosts(self.allowed_hosts))


@dataclass(frozen=True)
class FeedHeader:
    """GTFS-RT FeedHeader message (tags 1..4)."""

    gtfs_realtime_version: str = GTFS_RT_VERSION
    incrementality: Incrementality = Incrementality.FULL_DATASET
    timestamp: Optional[int] = None
    feed_version: Optional[str] = None

    def __post_init__(self) -> None:
        """Validate required version string and non-negative timestamp."""
        if not self.gtfs_realtime_version:
            raise ValueError("FeedHeader.gtfs_realtime_version must be non-empty")
        if self.timestamp is not None and self.timestamp < 0:
            raise ValueError(f"FeedHeader.timestamp must be >= 0, got {self.timestamp}")

    def to_proto(self) -> bytes:
        """Serialize FeedHeader to proto2 binary bytes."""
        parts: List[bytes] = [
            encode_string(1, self.gtfs_realtime_version),
            encode_enum(2, int(self.incrementality)),
        ]
        if self.timestamp is not None:
            parts.append(encode_uint64(3, self.timestamp))
        if self.feed_version is not None:
            parts.append(encode_string(4, self.feed_version))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "FeedHeader":
        """Decode FeedHeader from proto2 binary bytes."""
        version: Optional[str] = None
        inc = Incrementality.FULL_DATASET
        ts: Optional[int] = None
        feed_ver: Optional[str] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                version = decode_utf8(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                inc = _decode_enum(
                    Incrementality, _expect_int(wt, raw, 2), Incrementality.FULL_DATASET
                )
            elif fno == 3:
                ts = _expect_int(wt, raw, 3)
            elif fno == 4:
                feed_ver = decode_utf8(_expect_bytes(wt, raw, 4))
        if version is None:
            raise WireDecodeError("FeedHeader missing required field 1 (gtfs_realtime_version)")
        return cls(
            gtfs_realtime_version=version,
            incrementality=inc,
            timestamp=ts,
            feed_version=feed_ver,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert FeedHeader to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {
            "gtfsRealtimeVersion": self.gtfs_realtime_version,
            "incrementality": self.incrementality.name,
        }
        if self.timestamp is not None:
            data["timestamp"] = str(self.timestamp)
        if self.feed_version is not None:
            data["feedVersion"] = self.feed_version
        return data


@dataclass(frozen=True)
class TripDescriptor:
    """GTFS-RT TripDescriptor message (tags 1..6)."""

    trip_id: Optional[str] = None
    start_time: Optional[str] = None
    start_date: Optional[str] = None
    schedule_relationship: Optional[ScheduleRel] = None
    route_id: Optional[str] = None
    direction_id: Optional[int] = None

    def to_proto(self) -> bytes:
        """Serialize TripDescriptor to proto2 binary bytes."""
        parts: List[bytes] = []
        if self.trip_id is not None:
            parts.append(encode_string(1, self.trip_id))
        if self.start_time is not None:
            parts.append(encode_string(2, self.start_time))
        if self.start_date is not None:
            parts.append(encode_string(3, self.start_date))
        if self.schedule_relationship is not None:
            parts.append(encode_enum(4, int(self.schedule_relationship)))
        if self.route_id is not None:
            parts.append(encode_string(5, self.route_id))
        if self.direction_id is not None:
            parts.append(encode_uint32(6, self.direction_id))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "TripDescriptor":
        """Decode TripDescriptor from proto2 binary bytes."""
        trip_id: Optional[str] = None
        start_time: Optional[str] = None
        start_date: Optional[str] = None
        sched_rel: Optional[ScheduleRel] = None
        route_id: Optional[str] = None
        dir_id: Optional[int] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                trip_id = decode_utf8(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                start_time = decode_utf8(_expect_bytes(wt, raw, 2))
            elif fno == 3:
                start_date = decode_utf8(_expect_bytes(wt, raw, 3))
            elif fno == 4:
                sched_rel = _decode_enum(
                    ScheduleRel, _expect_int(wt, raw, 4), ScheduleRel.SCHEDULED
                )
            elif fno == 5:
                route_id = decode_utf8(_expect_bytes(wt, raw, 5))
            elif fno == 6:
                dir_id = _expect_int(wt, raw, 6)
        return cls(
            trip_id=trip_id,
            start_time=start_time,
            start_date=start_date,
            schedule_relationship=sched_rel,
            route_id=route_id,
            direction_id=dir_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert TripDescriptor to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {}
        if self.trip_id is not None:
            data["tripId"] = self.trip_id
        if self.route_id is not None:
            data["routeId"] = self.route_id
        if self.direction_id is not None:
            data["directionId"] = self.direction_id
        if self.start_time is not None:
            data["startTime"] = self.start_time
        if self.start_date is not None:
            data["startDate"] = self.start_date
        if self.schedule_relationship is not None:
            data["scheduleRelationship"] = self.schedule_relationship.name
        return data


@dataclass(frozen=True)
class VehicleDescriptor:
    """GTFS-RT VehicleDescriptor message (tags 1..3)."""

    id: Optional[str] = None
    label: Optional[str] = None
    license_plate: Optional[str] = None

    def to_proto(self) -> bytes:
        """Serialize VehicleDescriptor to proto2 binary bytes."""
        parts: List[bytes] = []
        if self.id is not None:
            parts.append(encode_string(1, self.id))
        if self.label is not None:
            parts.append(encode_string(2, self.label))
        if self.license_plate is not None:
            parts.append(encode_string(3, self.license_plate))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "VehicleDescriptor":
        """Decode VehicleDescriptor from proto2 binary bytes."""
        vid: Optional[str] = None
        label: Optional[str] = None
        plate: Optional[str] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                vid = decode_utf8(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                label = decode_utf8(_expect_bytes(wt, raw, 2))
            elif fno == 3:
                plate = decode_utf8(_expect_bytes(wt, raw, 3))
        return cls(id=vid, label=label, license_plate=plate)

    def to_dict(self) -> Dict[str, Any]:
        """Convert VehicleDescriptor to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {}
        if self.id is not None:
            data["id"] = self.id
        if self.label is not None:
            data["label"] = self.label
        if self.license_plate is not None:
            data["licensePlate"] = self.license_plate
        return data


@dataclass(frozen=True)
class Position:
    """GTFS-RT Position message (tags 1..5; speed in m/s, odometer in m)."""

    latitude: float
    longitude: float
    bearing: Optional[float] = None
    odometer: Optional[float] = None
    speed: Optional[float] = None

    def __post_init__(self) -> None:
        """Enforce finite WGS84 bounds and physical kinematic ranges."""
        if not math.isfinite(self.latitude) or not (-90.0 <= self.latitude <= 90.0):
            raise ValueError(f"Position.latitude out of [-90, 90]: {self.latitude}")
        if not math.isfinite(self.longitude) or not (-180.0 <= self.longitude <= 180.0):
            raise ValueError(f"Position.longitude out of [-180, 180]: {self.longitude}")
        if self.bearing is not None:
            if not math.isfinite(self.bearing) or not (0.0 <= self.bearing <= 360.0):
                raise ValueError(f"Position.bearing out of [0, 360]: {self.bearing}")
        if self.odometer is not None:
            if not math.isfinite(self.odometer) or self.odometer < 0.0:
                raise ValueError(f"Position.odometer must be finite and >= 0: {self.odometer}")
        if self.speed is not None:
            if not math.isfinite(self.speed) or self.speed < 0.0:
                raise ValueError(f"Position.speed must be finite and >= 0: {self.speed}")

    def to_proto(self) -> bytes:
        """Serialize Position to proto2 binary bytes."""
        parts: List[bytes] = [
            encode_float(1, self.latitude),
            encode_float(2, self.longitude),
        ]
        if self.bearing is not None:
            parts.append(encode_float(3, self.bearing))
        if self.odometer is not None:
            parts.append(encode_double(4, self.odometer))
        if self.speed is not None:
            parts.append(encode_float(5, self.speed))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "Position":
        """Decode Position from proto2 binary bytes."""
        lat: Optional[float] = None
        lng: Optional[float] = None
        bearing: Optional[float] = None
        odometer: Optional[float] = None
        speed: Optional[float] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                lat = _expect_f32(wt, raw, 1)
            elif fno == 2:
                lng = _expect_f32(wt, raw, 2)
            elif fno == 3:
                bearing = _expect_f32(wt, raw, 3)
            elif fno == 4:
                odometer = _expect_f64(wt, raw, 4)
            elif fno == 5:
                speed = _expect_f32(wt, raw, 5)
        if lat is None or lng is None:
            raise WireDecodeError("Position missing required latitude (1) or longitude (2)")
        return cls(
            latitude=lat,
            longitude=lng,
            bearing=bearing,
            odometer=odometer,
            speed=speed,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert Position to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {
            "latitude": round(self.latitude, 6),
            "longitude": round(self.longitude, 6),
        }
        if self.bearing is not None:
            data["bearing"] = round(self.bearing, 2)
        if self.odometer is not None:
            data["odometer"] = round(self.odometer, 1)
        if self.speed is not None:
            data["speed"] = round(self.speed, 3)
        return data


@dataclass(frozen=True)
class VehiclePosition:
    """GTFS-RT VehiclePosition message (tags 1..8)."""

    trip: Optional[TripDescriptor] = None
    position: Optional[Position] = None
    current_stop_sequence: Optional[int] = None
    current_status: Optional[VehicleStopStatus] = None
    timestamp: Optional[int] = None
    congestion_level: Optional[CongestionLevel] = None
    stop_id: Optional[str] = None
    vehicle: Optional[VehicleDescriptor] = None

    def to_proto(self) -> bytes:
        """Serialize VehiclePosition to proto2 binary bytes."""
        parts: List[bytes] = []
        if self.trip is not None:
            parts.append(encode_submsg(1, self.trip.to_proto()))
        if self.position is not None:
            parts.append(encode_submsg(2, self.position.to_proto()))
        if self.current_stop_sequence is not None:
            parts.append(encode_uint32(3, self.current_stop_sequence))
        if self.current_status is not None:
            parts.append(encode_enum(4, int(self.current_status)))
        if self.timestamp is not None:
            parts.append(encode_uint64(5, self.timestamp))
        if self.congestion_level is not None:
            parts.append(encode_enum(6, int(self.congestion_level)))
        if self.stop_id is not None:
            parts.append(encode_string(7, self.stop_id))
        if self.vehicle is not None:
            parts.append(encode_submsg(8, self.vehicle.to_proto()))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "VehiclePosition":
        """Decode VehiclePosition from proto2 binary bytes."""
        trip: Optional[TripDescriptor] = None
        position: Optional[Position] = None
        stop_seq: Optional[int] = None
        status: Optional[VehicleStopStatus] = None
        ts: Optional[int] = None
        cong: Optional[CongestionLevel] = None
        stop_id: Optional[str] = None
        vehicle: Optional[VehicleDescriptor] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                trip = TripDescriptor.from_proto(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                position = Position.from_proto(_expect_bytes(wt, raw, 2))
            elif fno == 3:
                stop_seq = _expect_int(wt, raw, 3)
            elif fno == 4:
                status = _decode_enum(
                    VehicleStopStatus, _expect_int(wt, raw, 4), VehicleStopStatus.IN_TRANSIT_TO
                )
            elif fno == 5:
                ts = _expect_int(wt, raw, 5)
            elif fno == 6:
                cong = _decode_enum(
                    CongestionLevel,
                    _expect_int(wt, raw, 6),
                    CongestionLevel.UNKNOWN_CONGESTION_LEVEL,
                )
            elif fno == 7:
                stop_id = decode_utf8(_expect_bytes(wt, raw, 7))
            elif fno == 8:
                vehicle = VehicleDescriptor.from_proto(_expect_bytes(wt, raw, 8))
        return cls(
            trip=trip,
            position=position,
            current_stop_sequence=stop_seq,
            current_status=status,
            timestamp=ts,
            congestion_level=cong,
            stop_id=stop_id,
            vehicle=vehicle,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert VehiclePosition to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {}
        if self.trip is not None:
            data["trip"] = self.trip.to_dict()
        if self.vehicle is not None:
            data["vehicle"] = self.vehicle.to_dict()
        if self.position is not None:
            data["position"] = self.position.to_dict()
        if self.current_stop_sequence is not None:
            data["currentStopSequence"] = self.current_stop_sequence
        if self.current_status is not None:
            data["currentStatus"] = self.current_status.name
        if self.timestamp is not None:
            data["timestamp"] = str(self.timestamp)
        if self.congestion_level is not None:
            data["congestionLevel"] = self.congestion_level.name
        if self.stop_id is not None:
            data["stopId"] = self.stop_id
        return data


@dataclass(frozen=True)
class StopTimeEvent:
    """GTFS-RT TripUpdate.StopTimeEvent message (tags 1..3)."""

    delay: Optional[int] = None
    time: Optional[int] = None
    uncertainty: Optional[int] = None

    def to_proto(self) -> bytes:
        """Serialize StopTimeEvent to proto2 binary bytes."""
        parts: List[bytes] = []
        if self.delay is not None:
            parts.append(encode_int32(1, self.delay))
        if self.time is not None:
            parts.append(encode_int64(2, self.time))
        if self.uncertainty is not None:
            parts.append(encode_int32(3, self.uncertainty))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "StopTimeEvent":
        """Decode StopTimeEvent from proto2 binary bytes."""
        delay: Optional[int] = None
        evt_time: Optional[int] = None
        uncertainty: Optional[int] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                delay = to_signed_int32(_expect_int(wt, raw, 1))
            elif fno == 2:
                evt_time = to_signed_int64(_expect_int(wt, raw, 2))
            elif fno == 3:
                uncertainty = to_signed_int32(_expect_int(wt, raw, 3))
        return cls(delay=delay, time=evt_time, uncertainty=uncertainty)

    def to_dict(self) -> Dict[str, Any]:
        """Convert StopTimeEvent to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {}
        if self.delay is not None:
            data["delay"] = self.delay
        if self.time is not None:
            data["time"] = str(self.time)
        if self.uncertainty is not None:
            data["uncertainty"] = self.uncertainty
        return data


@dataclass(frozen=True)
class StopTimeUpdate:
    """GTFS-RT TripUpdate.StopTimeUpdate message (tags 1..5)."""

    stop_sequence: Optional[int] = None
    arrival: Optional[StopTimeEvent] = None
    departure: Optional[StopTimeEvent] = None
    stop_id: Optional[str] = None
    schedule_relationship: Optional[StopScheduleRel] = None

    def to_proto(self) -> bytes:
        """Serialize StopTimeUpdate to proto2 binary bytes."""
        parts: List[bytes] = []
        if self.stop_sequence is not None:
            parts.append(encode_uint32(1, self.stop_sequence))
        if self.arrival is not None:
            parts.append(encode_submsg(2, self.arrival.to_proto()))
        if self.departure is not None:
            parts.append(encode_submsg(3, self.departure.to_proto()))
        if self.stop_id is not None:
            parts.append(encode_string(4, self.stop_id))
        if self.schedule_relationship is not None:
            parts.append(encode_enum(5, int(self.schedule_relationship)))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "StopTimeUpdate":
        """Decode StopTimeUpdate from proto2 binary bytes."""
        seq: Optional[int] = None
        arr: Optional[StopTimeEvent] = None
        dep: Optional[StopTimeEvent] = None
        stop_id: Optional[str] = None
        sched_rel: Optional[StopScheduleRel] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                seq = _expect_int(wt, raw, 1)
            elif fno == 2:
                arr = StopTimeEvent.from_proto(_expect_bytes(wt, raw, 2))
            elif fno == 3:
                dep = StopTimeEvent.from_proto(_expect_bytes(wt, raw, 3))
            elif fno == 4:
                stop_id = decode_utf8(_expect_bytes(wt, raw, 4))
            elif fno == 5:
                sched_rel = _decode_enum(
                    StopScheduleRel, _expect_int(wt, raw, 5), StopScheduleRel.SCHEDULED
                )
        return cls(
            stop_sequence=seq,
            arrival=arr,
            departure=dep,
            stop_id=stop_id,
            schedule_relationship=sched_rel,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert StopTimeUpdate to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {}
        if self.stop_sequence is not None:
            data["stopSequence"] = self.stop_sequence
        if self.stop_id is not None:
            data["stopId"] = self.stop_id
        if self.arrival is not None:
            data["arrival"] = self.arrival.to_dict()
        if self.departure is not None:
            data["departure"] = self.departure.to_dict()
        if self.schedule_relationship is not None:
            data["scheduleRelationship"] = self.schedule_relationship.name
        return data


@dataclass(frozen=True)
class TripUpdate:
    """GTFS-RT TripUpdate message (tags 1..5)."""

    trip: TripDescriptor
    stop_time_update: Tuple[StopTimeUpdate, ...] = ()
    vehicle: Optional[VehicleDescriptor] = None
    timestamp: Optional[int] = None
    delay: Optional[int] = None

    def to_proto(self) -> bytes:
        """Serialize TripUpdate to proto2 binary bytes."""
        parts: List[bytes] = [encode_submsg(1, self.trip.to_proto())]
        for stu in self.stop_time_update:
            parts.append(encode_submsg(2, stu.to_proto()))
        if self.vehicle is not None:
            parts.append(encode_submsg(3, self.vehicle.to_proto()))
        if self.timestamp is not None:
            parts.append(encode_uint64(4, self.timestamp))
        if self.delay is not None:
            parts.append(encode_int32(5, self.delay))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "TripUpdate":
        """Decode TripUpdate from proto2 binary bytes."""
        trip: Optional[TripDescriptor] = None
        updates: List[StopTimeUpdate] = []
        vehicle: Optional[VehicleDescriptor] = None
        ts: Optional[int] = None
        delay: Optional[int] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                trip = TripDescriptor.from_proto(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                updates.append(StopTimeUpdate.from_proto(_expect_bytes(wt, raw, 2)))
            elif fno == 3:
                vehicle = VehicleDescriptor.from_proto(_expect_bytes(wt, raw, 3))
            elif fno == 4:
                ts = _expect_int(wt, raw, 4)
            elif fno == 5:
                delay = to_signed_int32(_expect_int(wt, raw, 5))
        if trip is None:
            raise WireDecodeError("TripUpdate missing required field 1 (trip)")
        return cls(
            trip=trip,
            stop_time_update=tuple(updates),
            vehicle=vehicle,
            timestamp=ts,
            delay=delay,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert TripUpdate to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {"trip": self.trip.to_dict()}
        if self.vehicle is not None:
            data["vehicle"] = self.vehicle.to_dict()
        if self.stop_time_update:
            data["stopTimeUpdate"] = [s.to_dict() for s in self.stop_time_update]
        if self.timestamp is not None:
            data["timestamp"] = str(self.timestamp)
        if self.delay is not None:
            data["delay"] = self.delay
        return data


@dataclass(frozen=True)
class TimeRange:
    """GTFS-RT TimeRange message (tags 1..2)."""

    start: Optional[int] = None
    end: Optional[int] = None

    def to_proto(self) -> bytes:
        """Serialize TimeRange to proto2 binary bytes."""
        parts: List[bytes] = []
        if self.start is not None:
            parts.append(encode_uint64(1, self.start))
        if self.end is not None:
            parts.append(encode_uint64(2, self.end))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "TimeRange":
        """Decode TimeRange from proto2 binary bytes."""
        start: Optional[int] = None
        end: Optional[int] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                start = _expect_int(wt, raw, 1)
            elif fno == 2:
                end = _expect_int(wt, raw, 2)
        return cls(start=start, end=end)

    def to_dict(self) -> Dict[str, Any]:
        """Convert TimeRange to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {}
        if self.start is not None:
            data["start"] = str(self.start)
        if self.end is not None:
            data["end"] = str(self.end)
        return data


@dataclass(frozen=True)
class EntitySelector:
    """GTFS-RT EntitySelector message (tags 1..6)."""

    agency_id: Optional[str] = None
    route_id: Optional[str] = None
    route_type: Optional[int] = None
    trip: Optional[TripDescriptor] = None
    stop_id: Optional[str] = None
    direction_id: Optional[int] = None

    def to_proto(self) -> bytes:
        """Serialize EntitySelector to proto2 binary bytes."""
        parts: List[bytes] = []
        if self.agency_id is not None:
            parts.append(encode_string(1, self.agency_id))
        if self.route_id is not None:
            parts.append(encode_string(2, self.route_id))
        if self.route_type is not None:
            parts.append(encode_int32(3, self.route_type))
        if self.trip is not None:
            parts.append(encode_submsg(4, self.trip.to_proto()))
        if self.stop_id is not None:
            parts.append(encode_string(5, self.stop_id))
        if self.direction_id is not None:
            parts.append(encode_uint32(6, self.direction_id))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "EntitySelector":
        """Decode EntitySelector from proto2 binary bytes."""
        agency_id: Optional[str] = None
        route_id: Optional[str] = None
        route_type: Optional[int] = None
        trip: Optional[TripDescriptor] = None
        stop_id: Optional[str] = None
        dir_id: Optional[int] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                agency_id = decode_utf8(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                route_id = decode_utf8(_expect_bytes(wt, raw, 2))
            elif fno == 3:
                route_type = to_signed_int32(_expect_int(wt, raw, 3))
            elif fno == 4:
                trip = TripDescriptor.from_proto(_expect_bytes(wt, raw, 4))
            elif fno == 5:
                stop_id = decode_utf8(_expect_bytes(wt, raw, 5))
            elif fno == 6:
                dir_id = _expect_int(wt, raw, 6)
        return cls(
            agency_id=agency_id,
            route_id=route_id,
            route_type=route_type,
            trip=trip,
            stop_id=stop_id,
            direction_id=dir_id,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert EntitySelector to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {}
        if self.agency_id is not None:
            data["agencyId"] = self.agency_id
        if self.route_id is not None:
            data["routeId"] = self.route_id
        if self.route_type is not None:
            data["routeType"] = self.route_type
        if self.trip is not None:
            data["trip"] = self.trip.to_dict()
        if self.stop_id is not None:
            data["stopId"] = self.stop_id
        if self.direction_id is not None:
            data["directionId"] = self.direction_id
        return data


@dataclass(frozen=True)
class Translation:
    """GTFS-RT TranslatedString.Translation message (tags 1..2)."""

    text: str
    language: Optional[str] = None

    def to_proto(self) -> bytes:
        """Serialize Translation to proto2 binary bytes."""
        parts: List[bytes] = [encode_string(1, self.text)]
        if self.language is not None:
            parts.append(encode_string(2, self.language))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "Translation":
        """Decode Translation from proto2 binary bytes."""
        text: Optional[str] = None
        lang: Optional[str] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                text = decode_utf8(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                lang = decode_utf8(_expect_bytes(wt, raw, 2))
        if text is None:
            raise WireDecodeError("Translation missing required field 1 (text)")
        return cls(text=text, language=lang)

    def to_dict(self) -> Dict[str, Any]:
        """Convert Translation to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {"text": self.text}
        if self.language is not None:
            data["language"] = self.language
        return data


@dataclass(frozen=True)
class TranslatedString:
    """GTFS-RT TranslatedString message (tag 1)."""

    translation: Tuple[Translation, ...]

    def to_proto(self) -> bytes:
        """Serialize TranslatedString to proto2 binary bytes."""
        return b"".join(encode_submsg(1, item.to_proto()) for item in self.translation)

    @classmethod
    def from_proto(cls, buf: bytes) -> "TranslatedString":
        """Decode TranslatedString from proto2 binary bytes."""
        items: List[Translation] = []
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                items.append(Translation.from_proto(_expect_bytes(wt, raw, 1)))
        return cls(translation=tuple(items))

    def to_dict(self) -> Dict[str, Any]:
        """Convert TranslatedString to canonical ProtoJSON mapping."""
        return {"translation": [item.to_dict() for item in self.translation]}


@dataclass(frozen=True)
class Alert:
    """GTFS-RT Alert message (tags 1, 5, 6, 7, 8, 10, 11, 14)."""

    active_period: Tuple[TimeRange, ...] = ()
    informed_entity: Tuple[EntitySelector, ...] = ()
    cause: Optional[AlertCause] = None
    effect: Optional[AlertEffect] = None
    url: Optional[TranslatedString] = None
    header_text: Optional[TranslatedString] = None
    description_text: Optional[TranslatedString] = None
    severity_level: Optional[AlertSeverity] = None

    def to_proto(self) -> bytes:
        """Serialize Alert to proto2 binary bytes."""
        parts: List[bytes] = []
        for period in self.active_period:
            parts.append(encode_submsg(1, period.to_proto()))
        for selector in self.informed_entity:
            parts.append(encode_submsg(5, selector.to_proto()))
        if self.cause is not None:
            parts.append(encode_enum(6, int(self.cause)))
        if self.effect is not None:
            parts.append(encode_enum(7, int(self.effect)))
        if self.url is not None:
            parts.append(encode_submsg(8, self.url.to_proto()))
        if self.header_text is not None:
            parts.append(encode_submsg(10, self.header_text.to_proto()))
        if self.description_text is not None:
            parts.append(encode_submsg(11, self.description_text.to_proto()))
        if self.severity_level is not None:
            parts.append(encode_enum(14, int(self.severity_level)))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "Alert":
        """Decode Alert from proto2 binary bytes."""
        periods: List[TimeRange] = []
        entities: List[EntitySelector] = []
        cause: Optional[AlertCause] = None
        effect: Optional[AlertEffect] = None
        url: Optional[TranslatedString] = None
        header: Optional[TranslatedString] = None
        desc: Optional[TranslatedString] = None
        sev: Optional[AlertSeverity] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                periods.append(TimeRange.from_proto(_expect_bytes(wt, raw, 1)))
            elif fno == 5:
                entities.append(EntitySelector.from_proto(_expect_bytes(wt, raw, 5)))
            elif fno == 6:
                cause = _decode_enum(AlertCause, _expect_int(wt, raw, 6), AlertCause.UNKNOWN_CAUSE)
            elif fno == 7:
                effect = _decode_enum(
                    AlertEffect, _expect_int(wt, raw, 7), AlertEffect.UNKNOWN_EFFECT
                )
            elif fno == 8:
                url = TranslatedString.from_proto(_expect_bytes(wt, raw, 8))
            elif fno == 10:
                header = TranslatedString.from_proto(_expect_bytes(wt, raw, 10))
            elif fno == 11:
                desc = TranslatedString.from_proto(_expect_bytes(wt, raw, 11))
            elif fno == 14:
                sev = _decode_enum(
                    AlertSeverity, _expect_int(wt, raw, 14), AlertSeverity.UNKNOWN_SEVERITY
                )
        return cls(
            active_period=tuple(periods),
            informed_entity=tuple(entities),
            cause=cause,
            effect=effect,
            url=url,
            header_text=header,
            description_text=desc,
            severity_level=sev,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert Alert to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {}
        if self.active_period:
            data["activePeriod"] = [p.to_dict() for p in self.active_period]
        if self.informed_entity:
            data["informedEntity"] = [e.to_dict() for e in self.informed_entity]
        if self.cause is not None:
            data["cause"] = self.cause.name
        if self.effect is not None:
            data["effect"] = self.effect.name
        if self.url is not None:
            data["url"] = self.url.to_dict()
        if self.header_text is not None:
            data["headerText"] = self.header_text.to_dict()
        if self.description_text is not None:
            data["descriptionText"] = self.description_text.to_dict()
        if self.severity_level is not None:
            data["severityLevel"] = self.severity_level.name
        return data


@dataclass(frozen=True)
class FeedEntity:
    """GTFS-RT FeedEntity message (tags 1..5)."""

    id: str
    is_deleted: Optional[bool] = None
    trip_update: Optional[TripUpdate] = None
    vehicle: Optional[VehiclePosition] = None
    alert: Optional[Alert] = None

    def __post_init__(self) -> None:
        """Validate non-empty entity identifier."""
        if not self.id or not self.id.strip():
            raise ValueError("FeedEntity.id must be a non-empty string")

    def to_proto(self) -> bytes:
        """Serialize FeedEntity to proto2 binary bytes."""
        parts: List[bytes] = [encode_string(1, self.id)]
        if self.is_deleted is not None:
            parts.append(encode_bool(2, self.is_deleted))
        if self.trip_update is not None:
            parts.append(encode_submsg(3, self.trip_update.to_proto()))
        if self.vehicle is not None:
            parts.append(encode_submsg(4, self.vehicle.to_proto()))
        if self.alert is not None:
            parts.append(encode_submsg(5, self.alert.to_proto()))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "FeedEntity":
        """Decode FeedEntity from proto2 binary bytes."""
        eid: Optional[str] = None
        deleted: Optional[bool] = None
        tu: Optional[TripUpdate] = None
        vp: Optional[VehiclePosition] = None
        alert: Optional[Alert] = None
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                eid = decode_utf8(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                deleted = bool(_expect_int(wt, raw, 2))
            elif fno == 3:
                tu = TripUpdate.from_proto(_expect_bytes(wt, raw, 3))
            elif fno == 4:
                vp = VehiclePosition.from_proto(_expect_bytes(wt, raw, 4))
            elif fno == 5:
                alert = Alert.from_proto(_expect_bytes(wt, raw, 5))
        if eid is None:
            raise WireDecodeError("FeedEntity missing required field 1 (id)")
        return cls(
            id=eid,
            is_deleted=deleted,
            trip_update=tu,
            vehicle=vp,
            alert=alert,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Convert FeedEntity to canonical ProtoJSON mapping."""
        data: Dict[str, Any] = {"id": self.id}
        if self.is_deleted is not None:
            data["isDeleted"] = self.is_deleted
        if self.trip_update is not None:
            data["tripUpdate"] = self.trip_update.to_dict()
        if self.vehicle is not None:
            data["vehicle"] = self.vehicle.to_dict()
        if self.alert is not None:
            data["alert"] = self.alert.to_dict()
        return data


@dataclass(frozen=True)
class FeedMessage:
    """Top-level GTFS-RT FeedMessage container (tags 1..2)."""

    header: FeedHeader
    entity: Tuple[FeedEntity, ...] = ()

    def to_proto(self) -> bytes:
        """Serialize FeedMessage to proto2 binary bytes."""
        parts: List[bytes] = [encode_submsg(1, self.header.to_proto())]
        for item in self.entity:
            parts.append(encode_submsg(2, item.to_proto()))
        return b"".join(parts)

    @classmethod
    def from_proto(cls, buf: bytes) -> "FeedMessage":
        """Decode FeedMessage from proto2 binary bytes."""
        header: Optional[FeedHeader] = None
        entities: List[FeedEntity] = []
        for fno, wt, raw in iter_wire_fields(buf):
            if fno == 1:
                header = FeedHeader.from_proto(_expect_bytes(wt, raw, 1))
            elif fno == 2:
                entities.append(FeedEntity.from_proto(_expect_bytes(wt, raw, 2)))
        if header is None:
            raise WireDecodeError("FeedMessage missing required field 1 (header)")
        return cls(header=header, entity=tuple(entities))

    def to_dict(self) -> Dict[str, Any]:
        """Convert FeedMessage to canonical ProtoJSON mapping."""
        return {
            "header": self.header.to_dict(),
            "entity": [item.to_dict() for item in self.entity],
        }
