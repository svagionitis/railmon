"""Hellenic Railways (railway.gov.gr) domain-to-GTFS-Realtime v2.0 mapper.

Translates TrainStateStore snapshots, live TrainPosition batches, timetable
itineraries, and safety watchdog conflicts into GTFS-RT FeedMessage DTOs with
bounded caching and zero third-party dependencies.
"""

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
import json
import logging
import math
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
import unicodedata
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from gtfs_rt.models import (
    GTFS_RT_VERSION,
    Alert,
    AlertCause,
    AlertEffect,
    AlertSeverity,
    EntitySelector,
    FeedEntity,
    FeedHeader,
    FeedKind,
    FeedMessage,
    GtfsRtConfig,
    Incrementality,
    Position,
    ScheduleRel,
    StopIdField,
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
from storage.repository import parse_iso_to_epoch
from train_tracker import TrainPosition
from tui.geo import GeoPoint
from tui.mock_engine import StationNode, haversine_distance, load_stations
from tui.state import StateSnapshot, TrainRecord, _train_matches_corridor

logger = logging.getLogger("railmon.gtfs_rt.mapper")

_RE_TEST_TRAIN = re.compile(r"^T\d+$", re.IGNORECASE)
_RE_PARENS = re.compile(r"\s*\([^)]*\)\s*")
_RE_NON_ALNUM = re.compile(r"[^0-9a-zA-Z\u0370-\u03ff\u1f00-\u1fff]+")
_TRACKER_URL = "https://railway.gov.gr/train-tracker"

_FREIGHT_TOKENS: Tuple[str, ...] = ("freight", "εμπορ", "cargo", "maintenance", "συντηρ")
_PROAST_TOKENS: Tuple[str, ...] = (
    "proast",
    "suburban",
    "pirair",
    "kiato",
    "chalkida",
    "airport",
    "προαστ",
)
_IC_TOKENS: Tuple[str, ...] = ("intercity", "ic", "express", "etr")


def _resolve_athens_tz() -> tzinfo:
    """Resolve Europe/Athens tzinfo with fixed UTC+2 fallback if tzdata is absent."""
    try:
        return ZoneInfo("Europe/Athens")
    except (ZoneInfoNotFoundError, KeyError, OSError):
        return timezone(timedelta(hours=2))


ATHENS_TZ: tzinfo = _resolve_athens_tz()


def norm_station_key(name: str) -> str:
    """Normalize Greek or Latin station name into a diacritic-free lookup key."""
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFD", str(name).strip())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    folded = stripped.casefold()
    return _RE_NON_ALNUM.sub(" ", folded).strip()


class StationIndex:
    """O(1) diacritic-insensitive lookup index over Hellenic Railways stations."""

    def __init__(self, stations: Optional[Sequence[StationNode]] = None) -> None:
        self._by_key: Dict[str, StationNode] = {}
        nodes = list(stations) if stations is not None else list(load_stations().values())
        for stn in nodes:
            self._register(stn)

    def _register(self, stn: StationNode) -> None:
        """Register station under ID, numeric code, Greek, English, and base aliases."""
        candidates: List[str] = [
            stn.id,
            stn.code,
            stn.name_el,
            stn.name_en,
            _RE_PARENS.sub(" ", stn.name_el).strip(),
            _RE_PARENS.sub(" ", stn.name_en).strip(),
        ]
        for raw in candidates:
            key = norm_station_key(raw)
            if key and key not in self._by_key:
                self._by_key[key] = stn

    def resolve(self, query: Optional[str]) -> Optional[StationNode]:
        """Resolve a station name, code, or ID to a StationNode."""
        if not query:
            return None
        key = norm_station_key(query)
        if not key:
            return None
        found = self._by_key.get(key)
        if found is not None:
            return found
        base = norm_station_key(_RE_PARENS.sub(" ", query))
        return self._by_key.get(base) if base else None

    @staticmethod
    def stop_id_for(stn: StationNode, field: StopIdField) -> str:
        """Extract configured GTFS stop_id attribute from a StationNode."""
        if field == StopIdField.CODE and stn.code:
            return str(stn.code)
        return str(stn.id)


ScheduleFetcher = Callable[[Optional[str], str], Optional[Dict[str, Any]]]


class ScheduleCache:
    """Thread-safe bounded LRU/TTL cache for upstream train timetable itineraries."""

    def __init__(
        self,
        fetcher: Optional[ScheduleFetcher] = None,
        max_entries: int = 256,
        ttl_sec: float = 300.0,
        neg_ttl_sec: float = 60.0,
    ) -> None:
        self._fetcher = fetcher
        self._max_entries = max(1, int(max_entries))
        self._ttl_sec = max(1.0, float(ttl_sec))
        self._neg_ttl_sec = max(1.0, float(neg_ttl_sec))
        self._lock = threading.RLock()
        self._entries: "OrderedDict[str, Tuple[float, Optional[Dict[str, Any]]]]" = OrderedDict()

    def get(
        self,
        schedule_id: Optional[str],
        train_number: str,
        now: Optional[float] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return cached schedule itinerary or invoke fetcher if enabled and expired."""
        clean_num = (train_number or "").strip()
        if not clean_num or _RE_TEST_TRAIN.match(clean_num):
            return None

        cache_key = (schedule_id or "").strip() or clean_num
        ts = float(now) if now is not None else time.time()

        with self._lock:
            hit = self._entries.get(cache_key)
            if hit is not None:
                expires_at, payload = hit
                if ts < expires_at:
                    self._entries.move_to_end(cache_key)
                    return payload
                del self._entries[cache_key]

        if self._fetcher is None:
            return None

        fetched: Optional[Dict[str, Any]] = None
        try:
            fetched = self._fetcher(schedule_id, clean_num)
        except Exception as exc:
            logger.debug("Schedule enrichment failed for %s: %s", cache_key, exc)

        ttl = self._ttl_sec if fetched is not None else self._neg_ttl_sec
        with self._lock:
            self._entries[cache_key] = (ts + ttl, fetched)
            self._entries.move_to_end(cache_key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return fetched

    def put(
        self,
        key: str,
        payload: Dict[str, Any],
        now: Optional[float] = None,
    ) -> None:
        """Manually insert a pre-fetched schedule payload (e.g. from SQLite)."""
        ts = float(now) if now is not None else time.time()
        with self._lock:
            self._entries[key] = (ts + self._ttl_sec, payload)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def size(self) -> int:
        """Return current number of cached entries."""
        with self._lock:
            return len(self._entries)


def _pos_epoch(pos: TrainPosition, fallback: float) -> int:
    """Resolve POSIX UTC integer seconds for a TrainPosition."""
    if pos.timestamp:
        parsed = parse_iso_to_epoch(pos.timestamp)
        if parsed > 0.0 and math.isfinite(parsed):
            return int(parsed)
    return max(0, int(fallback))


def _resolve_bearing(pos: TrainPosition) -> Optional[float]:
    """Select finite track-aligned bearing or compass course in [0.0, 360.0)."""
    hdg = pos.track_heading
    crs = pos.course
    if hdg is not None and math.isfinite(hdg) and abs(hdg) > 1e-6:
        return float(hdg % 360.0)
    if crs is not None and math.isfinite(crs):
        return float(crs % 360.0)
    if hdg is not None and math.isfinite(hdg):
        return float(hdg % 360.0)
    return None


def _resolve_route_id(pos: TrainPosition) -> str:
    """Map Hellenic Railways corridor and service type to canonical GTFS route_id."""
    corr = (pos.corridor or "").strip().upper()
    combined = f"{corr} {pos.service_type or ''} {pos.train_type or ''}".casefold()

    if any(tok in combined for tok in _PROAST_TOKENS):
        sub = corr if corr in ("PIRAIR", "KIATO", "CHALKIDA", "KIAAIGIO") else "ATH"
        return f"OSE-PROASTIAKOS-{sub}"
    if any(tok in combined for tok in _IC_TOKENS):
        return "OSE-PATHE-IC"
    if corr in ("PATHE", "E85", "MAINLINE"):
        return "OSE-PATHE-REG"
    if corr:
        safe_corr = _RE_NON_ALNUM.sub("-", corr).strip("-") or "NETWORK"
        return f"OSE-REGIONAL-{safe_corr}"
    return "OSE-RAIL"


def _local_service_date(epoch_sec: int) -> str:
    """Format POSIX epoch seconds as YYYYMMDD in Europe/Athens agency timezone."""
    dt_local = datetime.fromtimestamp(epoch_sec, tz=timezone.utc).astimezone(ATHENS_TZ)
    return dt_local.strftime("%Y%m%d")


def _local_start_time(iso_ts: Optional[str]) -> Optional[str]:
    """Convert ISO-8601 UTC scheduled departure timestamp into Athens HH:MM:SS."""
    if not iso_ts:
        return None
    epoch = parse_iso_to_epoch(iso_ts)
    if epoch <= 0.0 or not math.isfinite(epoch):
        return None
    dt_local = datetime.fromtimestamp(epoch, tz=timezone.utc).astimezone(ATHENS_TZ)
    return dt_local.strftime("%H:%M:%S")


def _build_bilingual(el_text: str, en_text: str) -> TranslatedString:
    """Construct a bilingual Greek/English GTFS-RT TranslatedString."""
    return TranslatedString(
        translation=(
            Translation(text=el_text, language="el"),
            Translation(text=en_text, language="en"),
        )
    )


class GtfsRtMapper:
    """Translates railmon telemetry snapshots and positions into GTFS-RT FeedMessages."""

    def __init__(
        self,
        config: Optional[GtfsRtConfig] = None,
        station_index: Optional[StationIndex] = None,
        schedule_cache: Optional[ScheduleCache] = None,
    ) -> None:
        self.config = config or GtfsRtConfig()
        self.stations = station_index or StationIndex()
        self.schedules = schedule_cache or ScheduleCache()

    def _is_eligible(self, pos: TrainPosition) -> bool:
        """Check coordinate validity, corridor filter, and passenger eligibility."""
        if not (math.isfinite(pos.lat) and math.isfinite(pos.lng)):
            return False
        if not (-90.0 <= pos.lat <= 90.0 and -180.0 <= pos.lng <= 180.0):
            return False
        if abs(pos.lat) < 1e-9 and abs(pos.lng) < 1e-9:
            return False

        if self.config.corridor_filter:
            if not _train_matches_corridor(pos, self.config.corridor_filter):
                return False

        combined = f"{pos.train_type or ''} {pos.service_type or ''}".casefold()
        if not self.config.include_freight and any(tok in combined for tok in _FREIGHT_TOKENS):
            return False

        if not self.config.include_unassigned:
            is_unassigned_type = (pos.train_type or "").strip().casefold() == "unassigned"
            lacks_service_meta = (
                not pos.schedule_id and not pos.origin and not pos.destination
            )
            if is_unassigned_type or lacks_service_meta:
                return False

        return True

    def _trip_id_for(self, pos: TrainPosition, ref_epoch: int) -> str:
        """Compute stable canonical GTFS trip_id for a TrainPosition."""
        if pos.schedule_id and pos.schedule_id.strip():
            return pos.schedule_id.strip()
        service_date = _local_service_date(ref_epoch)
        clean_num = (pos.train_number or pos.id or "UNKNOWN").strip()
        return f"{clean_num}-{service_date}"

    def _build_trip_desc(
        self,
        pos: TrainPosition,
        ref_epoch: int,
        sched: Optional[Dict[str, Any]] = None,
    ) -> TripDescriptor:
        """Build GTFS-RT TripDescriptor aligned to Europe/Athens service date."""
        start_time: Optional[str] = None
        if sched and sched.get("scheduledDeparture"):
            start_time = _local_start_time(str(sched["scheduledDeparture"]))
        return TripDescriptor(
            trip_id=self._trip_id_for(pos, ref_epoch),
            start_time=start_time,
            start_date=_local_service_date(ref_epoch),
            schedule_relationship=ScheduleRel.SCHEDULED,
            route_id=_resolve_route_id(pos),
        )

    @staticmethod
    def _build_vehicle_desc(pos: TrainPosition) -> VehicleDescriptor:
        """Build GTFS-RT VehicleDescriptor preserving transponder and locomotive IDs."""
        vid = (pos.id or pos.train_number or "unknown").strip()
        label = (pos.train_number or vid).strip()
        plate = pos.locomotive_number.strip() if pos.locomotive_number else None
        return VehicleDescriptor(id=vid, label=label, license_plate=plate)

    def _infer_stop_status(
        self,
        pos: TrainPosition,
        stn: Optional[StationNode],
    ) -> Tuple[VehicleStopStatus, Optional[str]]:
        """Infer VehicleStopStatus and GTFS stop_id from station proximity and velocity."""
        if stn is None:
            return VehicleStopStatus.IN_TRANSIT_TO, None

        stop_id = self.stations.stop_id_for(stn, self.config.stop_id_field)
        dist_m = haversine_distance(GeoPoint(lat=pos.lat, lng=pos.lng), stn.point)
        speed_kmh = max(0.0, float(pos.speed)) if math.isfinite(pos.speed) else 0.0

        if speed_kmh < 5.0 and dist_m <= self.config.station_proximity_m:
            return VehicleStopStatus.STOPPED_AT, stop_id
        if dist_m <= self.config.incoming_proximity_m:
            return VehicleStopStatus.INCOMING_AT, stop_id
        return VehicleStopStatus.IN_TRANSIT_TO, stop_id

    def _build_vehicle_entity(
        self,
        rec: TrainRecord,
        now_ts: float,
        sched: Optional[Dict[str, Any]],
    ) -> FeedEntity:
        """Translate a TrainRecord into a GTFS-RT VehiclePosition FeedEntity."""
        pos = rec.position
        evt_epoch = _pos_epoch(pos, rec.last_updated or now_ts)
        speed_kmh = max(0.0, float(pos.speed)) if math.isfinite(pos.speed) else 0.0
        speed_mps = speed_kmh / 3.6
        odometer = float(pos.chainage_m) if (pos.chainage_m is not None and pos.chainage_m >= 0) else None

        stn = self.stations.resolve(pos.next_station)
        stop_status, stop_id = self._infer_stop_status(pos, stn)

        vp = VehiclePosition(
            trip=self._build_trip_desc(pos, evt_epoch, sched),
            position=Position(
                latitude=float(pos.lat),
                longitude=float(pos.lng),
                bearing=_resolve_bearing(pos),
                odometer=odometer,
                speed=speed_mps,
            ),
            current_status=stop_status,
            timestamp=evt_epoch,
            stop_id=stop_id,
            vehicle=self._build_vehicle_desc(pos),
        )
        entity_key = (pos.id or pos.train_number or "unknown").strip()
        return FeedEntity(id=f"vp:{entity_key}", vehicle=vp)

    def _dedup_trip_records(
        self,
        records: Sequence[TrainRecord],
        now_ts: float,
    ) -> List[TrainRecord]:
        """Deduplicate coupled consists sharing one canonical trip_id."""
        best_by_trip: Dict[str, Tuple[int, str, TrainRecord]] = {}
        for rec in records:
            evt_epoch = _pos_epoch(rec.position, rec.last_updated or now_ts)
            tid = self._trip_id_for(rec.position, evt_epoch)
            key_id = (rec.position.id or rec.position.train_number or "").strip()
            existing = best_by_trip.get(tid)
            if existing is None or (evt_epoch, key_id) > (existing[0], existing[1]):
                best_by_trip[tid] = (evt_epoch, key_id, rec)
        return [entry[2] for _, entry in sorted(best_by_trip.items())]

    def _stops_from_schedule(
        self,
        sched: Dict[str, Any],
        delay_sec: int,
    ) -> Tuple[StopTimeUpdate, ...]:
        """Build ordered StopTimeUpdate tuples from an enriched timetable dictionary."""
        raw_stops = sched.get("routeStations") or sched.get("stops") or []
        if not isinstance(raw_stops, list):
            return ()

        updates: List[StopTimeUpdate] = []
        for idx, item in enumerate(raw_stops, start=1):
            if not isinstance(item, dict):
                continue
            seq_val = item.get("stop_sequence", item.get("order", item.get("index", idx)))
            seq = int(seq_val) if isinstance(seq_val, int) and seq_val >= 0 else idx
            stn_name = str(item.get("name") or item.get("station_name") or item.get("nameGreek") or "")
            stn = self.stations.resolve(stn_name)
            stop_id = (
                self.stations.stop_id_for(stn, self.config.stop_id_field)
                if stn is not None
                else (str(item.get("stationId") or item.get("station_id") or "").strip() or None)
            )
            raw_status = str(item.get("status") or "").strip().casefold()
            if raw_status in ("skipped", "cancelled", "canceled"):
                updates.append(
                    StopTimeUpdate(
                        stop_sequence=seq,
                        stop_id=stop_id,
                        schedule_relationship=StopScheduleRel.SKIPPED,
                    )
                )
                continue

            arr_evt = self._stop_event(item.get("scheduledArrival") or item.get("scheduled_arrival"), delay_sec)
            dep_evt = self._stop_event(item.get("scheduledDeparture") or item.get("scheduled_departure"), delay_sec)
            if arr_evt is None and dep_evt is None:
                arr_evt = StopTimeEvent(delay=delay_sec)
            updates.append(
                StopTimeUpdate(
                    stop_sequence=seq,
                    stop_id=stop_id,
                    arrival=arr_evt,
                    departure=dep_evt,
                    schedule_relationship=StopScheduleRel.SCHEDULED,
                )
            )
        return tuple(updates)

    @staticmethod
    def _stop_event(iso_ts: Any, delay_sec: int) -> Optional[StopTimeEvent]:
        """Build a StopTimeEvent shifting scheduled ISO time by signed delay seconds."""
        if not iso_ts:
            return None
        epoch = parse_iso_to_epoch(iso_ts)
        if epoch <= 0.0 or not math.isfinite(epoch):
            return None
        return StopTimeEvent(delay=delay_sec, time=int(epoch) + delay_sec)

    def _stops_kinematic(
        self,
        pos: TrainPosition,
        evt_epoch: int,
        delay_sec: int,
    ) -> Tuple[StopTimeUpdate, ...]:
        """Build zero-network kinematic StopTimeUpdate predictions for next stop & terminus."""
        updates: List[StopTimeUpdate] = []
        speed_kmh = max(0.0, float(pos.speed)) if math.isfinite(pos.speed) else 0.0

        next_stn = self.stations.resolve(pos.next_station)
        next_stop_id: Optional[str] = None
        if next_stn is not None:
            next_stop_id = self.stations.stop_id_for(next_stn, self.config.stop_id_field)
            dist_m = haversine_distance(GeoPoint(lat=pos.lat, lng=pos.lng), next_stn.point)
            pred_time: Optional[int] = None
            if speed_kmh >= 5.0:
                pred_time = evt_epoch + int(round((dist_m / 1000.0) / speed_kmh * 3600.0))
            elif dist_m <= self.config.station_proximity_m:
                pred_time = evt_epoch
            updates.append(
                StopTimeUpdate(
                    stop_id=next_stop_id,
                    arrival=StopTimeEvent(delay=delay_sec, time=pred_time, uncertainty=60),
                    schedule_relationship=StopScheduleRel.SCHEDULED,
                )
            )

        dest_stn = self.stations.resolve(pos.destination)
        if dest_stn is not None:
            dest_stop_id = self.stations.stop_id_for(dest_stn, self.config.stop_id_field)
            if dest_stop_id != next_stop_id:
                dist_dest = (
                    float(pos.distance_to_destination_m)
                    if pos.distance_to_destination_m is not None and pos.distance_to_destination_m >= 0
                    else haversine_distance(GeoPoint(lat=pos.lat, lng=pos.lng), dest_stn.point)
                )
                dest_time: Optional[int] = None
                if speed_kmh >= 5.0:
                    dest_time = evt_epoch + int(round((dist_dest / 1000.0) / speed_kmh * 3600.0))
                updates.append(
                    StopTimeUpdate(
                        stop_id=dest_stop_id,
                        arrival=StopTimeEvent(delay=delay_sec, time=dest_time, uncertainty=180),
                        schedule_relationship=StopScheduleRel.SCHEDULED,
                    )
                )
        return tuple(updates)

    def _build_trip_entity(
        self,
        rec: TrainRecord,
        now_ts: float,
        sched: Optional[Dict[str, Any]],
    ) -> FeedEntity:
        """Translate a deduplicated TrainRecord into a GTFS-RT TripUpdate FeedEntity."""
        pos = rec.position
        evt_epoch = _pos_epoch(pos, rec.last_updated or now_ts)
        delay_sec = int(pos.delay) * 60
        trip_desc = self._build_trip_desc(pos, evt_epoch, sched)

        stop_updates = (
            self._stops_from_schedule(sched, delay_sec)
            if sched is not None
            else ()
        )
        if not stop_updates:
            stop_updates = self._stops_kinematic(pos, evt_epoch, delay_sec)

        tu = TripUpdate(
            trip=trip_desc,
            stop_time_update=stop_updates,
            vehicle=self._build_vehicle_desc(pos),
            timestamp=evt_epoch,
            delay=delay_sec,
        )
        tid = trip_desc.trip_id or (pos.train_number or pos.id)
        return FeedEntity(id=f"tu:{tid}", trip_update=tu)

    def _build_delay_alert(
        self,
        rec: TrainRecord,
        now_ts: float,
        sched: Optional[Dict[str, Any]],
    ) -> Optional[FeedEntity]:
        """Synthesize a bilingual SIGNIFICANT_DELAYS Alert when train delay meets threshold."""
        pos = rec.position
        if pos.delay < self.config.delay_alert_min:
            return None

        evt_epoch = _pos_epoch(pos, rec.last_updated or now_ts)
        trip_desc = self._build_trip_desc(pos, evt_epoch, sched)
        tid = trip_desc.trip_id or pos.train_number
        sev = AlertSeverity.SEVERE if pos.delay >= 30 else AlertSeverity.WARNING
        route_str = f"{pos.origin or '?'} -> {pos.destination or '?'}"

        alert = Alert(
            active_period=(TimeRange(start=evt_epoch),),
            informed_entity=(
                EntitySelector(
                    agency_id=self.config.agency_id,
                    route_id=trip_desc.route_id,
                    route_type=2,
                    trip=trip_desc,
                ),
            ),
            cause=AlertCause.UNKNOWN_CAUSE,
            effect=AlertEffect.SIGNIFICANT_DELAYS,
            url=_build_bilingual(_TRACKER_URL, _TRACKER_URL),
            header_text=_build_bilingual(
                f"Αμαξοστοιχία {pos.train_number}: Καθυστέρηση +{pos.delay} λεπτά",
                f"Train {pos.train_number}: Delayed +{pos.delay} minutes",
            ),
            description_text=_build_bilingual(
                f"Η αμαξοστοιχία {pos.train_number} ({route_str}) παρουσιάζει καθυστέρηση {pos.delay} λεπτών.",
                f"Train {pos.train_number} ({route_str}) is currently running {pos.delay} minutes late.",
            ),
            severity_level=sev,
        )
        return FeedEntity(id=f"al:delay:{tid}", alert=alert)

    def _build_conflict_alert(self, conflict: Any, now_ts: float) -> Optional[FeedEntity]:
        """Synthesize a bilingual Alert from a CRITICAL or WARNING ConflictAlert."""
        sev_raw = getattr(conflict, "severity", None)
        sev_name = sev_raw.name if hasattr(sev_raw, "name") else str(sev_raw or "")
        if sev_name.upper() not in ("CRITICAL", "WARNING"):
            return None

        alert_id = str(getattr(conflict, "alert_id", "") or "conflict").strip()
        corridor = str(
            getattr(conflict, "corridor_name", getattr(conflict, "corridor_id", "RAIL"))
        ).strip()
        safe_corr = _RE_NON_ALNUM.sub("-", corridor.upper()).strip("-") or "RAIL"
        desc_en = str(
            getattr(conflict, "description", getattr(conflict, "message", "Operational corridor advisory"))
        )
        detected = int(
            float(
                getattr(conflict, "timestamp", getattr(conflict, "detected_at", now_ts))
                or now_ts
            )
        )
        alert_sev = AlertSeverity.SEVERE if sev_name.upper() == "CRITICAL" else AlertSeverity.WARNING

        alert = Alert(
            active_period=(TimeRange(start=detected),),
            informed_entity=(
                EntitySelector(
                    agency_id=self.config.agency_id,
                    route_id=f"OSE-{safe_corr}",
                    route_type=2,
                ),
            ),
            cause=AlertCause.TECHNICAL_PROBLEM,
            effect=AlertEffect.MODIFIED_SERVICE,
            url=_build_bilingual(_TRACKER_URL, _TRACKER_URL),
            header_text=_build_bilingual(
                f"Επιχειρησιακή ειδοποίηση γραμμής ({corridor})",
                f"Operational corridor advisory ({corridor})",
            ),
            description_text=_build_bilingual(
                f"Εντοπίστηκε κυκλοφοριακή ειδοποίηση στον διάδρομο {corridor}: {desc_en}",
                desc_en,
            ),
            severity_level=alert_sev,
        )
        return FeedEntity(id=f"al:conflict:{alert_id}", alert=alert)

    def _lookup_schedule(
        self,
        pos: TrainPosition,
        now_ts: float,
        overrides: Optional[Dict[str, Dict[str, Any]]],
    ) -> Optional[Dict[str, Any]]:
        """Resolve optional enriched schedule dictionary for a TrainPosition."""
        if overrides:
            if pos.schedule_id and pos.schedule_id in overrides:
                return overrides[pos.schedule_id]
            if pos.train_number in overrides:
                return overrides[pos.train_number]
        if self.config.enrich_schedules:
            return self.schedules.get(pos.schedule_id, pos.train_number, now=now_ts)
        return None

    def _collect_entities(
        self,
        records: Sequence[TrainRecord],
        conflicts: Sequence[Any],
        feed_kind: FeedKind,
        now_ts: float,
        overrides: Optional[Dict[str, Dict[str, Any]]],
    ) -> Tuple[FeedEntity, ...]:
        """Assemble deterministic, sorted FeedEntity tuple for requested feed_kind."""
        eligible = [rec for rec in records if self._is_eligible(rec.position)]
        deduped = self._dedup_trip_records(eligible, now_ts)
        entities: List[FeedEntity] = []

        if feed_kind in (FeedKind.COMBINED, FeedKind.VEHICLES):
            for rec in eligible:
                sched = self._lookup_schedule(rec.position, now_ts, overrides)
                entities.append(self._build_vehicle_entity(rec, now_ts, sched))

        if feed_kind in (FeedKind.COMBINED, FeedKind.TRIPS):
            for rec in deduped:
                sched = self._lookup_schedule(rec.position, now_ts, overrides)
                entities.append(self._build_trip_entity(rec, now_ts, sched))

        if self.config.include_alerts and feed_kind in (FeedKind.COMBINED, FeedKind.ALERTS):
            for rec in deduped:
                sched = self._lookup_schedule(rec.position, now_ts, overrides)
                delay_ent = self._build_delay_alert(rec, now_ts, sched)
                if delay_ent is not None:
                    entities.append(delay_ent)
            for conflict in conflicts:
                conf_ent = self._build_conflict_alert(conflict, now_ts)
                if conf_ent is not None:
                    entities.append(conf_ent)

        entities.sort(key=lambda e: e.id)
        return tuple(entities)

    def build_feed(
        self,
        snapshot: StateSnapshot,
        feed_kind: FeedKind = FeedKind.COMBINED,
        now: Optional[float] = None,
    ) -> FeedMessage:
        """Build a GTFS-RT FeedMessage from an immutable TrainStateStore snapshot."""
        now_ts = float(now) if now is not None else time.time()
        entities = self._collect_entities(
            records=list(snapshot.trains.values()),
            conflicts=snapshot.active_conflicts,
            feed_kind=feed_kind,
            now_ts=now_ts,
            overrides=None,
        )
        header = FeedHeader(
            gtfs_realtime_version=GTFS_RT_VERSION,
            incrementality=Incrementality.FULL_DATASET,
            timestamp=max(0, int(now_ts)),
            feed_version=self.config.feed_version,
        )
        return FeedMessage(header=header, entity=entities)

    def build_from_positions(
        self,
        positions: Sequence[TrainPosition],
        conflicts: Sequence[Any] = (),
        feed_kind: FeedKind = FeedKind.COMBINED,
        now: Optional[float] = None,
        schedules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> FeedMessage:
        """Build a GTFS-RT FeedMessage directly from a TrainPosition sequence."""
        now_ts = float(now) if now is not None else time.time()
        records = [
            TrainRecord(position=pos, first_seen=now_ts, last_updated=now_ts)
            for pos in positions
        ]
        entities = self._collect_entities(
            records=records,
            conflicts=conflicts,
            feed_kind=feed_kind,
            now_ts=now_ts,
            overrides=schedules,
        )
        header = FeedHeader(
            gtfs_realtime_version=GTFS_RT_VERSION,
            incrementality=Incrementality.FULL_DATASET,
            timestamp=max(0, int(now_ts)),
            feed_version=self.config.feed_version,
        )
        return FeedMessage(header=header, entity=entities)


@dataclass(frozen=True)
class CachedFeed:
    """Serialized GTFS-RT protobuf and ProtoJSON cache entry for a snapshot version."""

    version: int
    feed_kind: FeedKind
    pb_bytes: bytes
    json_bytes: bytes
    etag_pb: str
    etag_json: str
    entity_count: int
    timestamp: int
    build_ms: float


class FeedCache:
    """Thread-safe version-keyed serialization cache avoiding redundant encoding."""

    def __init__(self, mapper: GtfsRtMapper) -> None:
        self._mapper = mapper
        self._lock = threading.RLock()
        self._active_version: int = -1
        self._entries: Dict[FeedKind, CachedFeed] = {}

    def get_or_build(
        self,
        snapshot: StateSnapshot,
        feed_kind: FeedKind,
        now: Optional[float] = None,
    ) -> CachedFeed:
        """Return cached serialized feed for snapshot.version or build once."""
        with self._lock:
            if snapshot.version == self._active_version and feed_kind in self._entries:
                return self._entries[feed_kind]

        t0 = time.perf_counter()
        msg = self._mapper.build_feed(snapshot, feed_kind=feed_kind, now=now)
        pb_bytes = msg.to_proto()
        json_bytes = json.dumps(msg.to_dict(), ensure_ascii=False, indent=2).encode("utf-8")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0

        ts = msg.header.timestamp or int(now if now is not None else time.time())
        cached = CachedFeed(
            version=snapshot.version,
            feed_kind=feed_kind,
            pb_bytes=pb_bytes,
            json_bytes=json_bytes,
            etag_pb=f'"gtfsrt-{snapshot.version}-{feed_kind.value}-pb"',
            etag_json=f'"gtfsrt-{snapshot.version}-{feed_kind.value}-json"',
            entity_count=len(msg.entity),
            timestamp=ts,
            build_ms=round(elapsed_ms, 3),
        )

        with self._lock:
            if snapshot.version != self._active_version:
                self._entries.clear()
                self._active_version = snapshot.version
            self._entries[feed_kind] = cached
        return cached
