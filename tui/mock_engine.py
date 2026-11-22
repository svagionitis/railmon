"""Offline Mock Telemetry Simulator & Railway Network Topology Engine.

Provides topology loaders for Greek railway stations and tracks, geodesic
kinematics models (Haversine distance, forward bearing, polyline interpolation),
and a multi-train simulation engine generating continuous TrainPosition telemetry.
"""

import bisect
import difflib
import json
import math
import random
import threading
import time
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Sequence, Tuple

from train_tracker import TrainPosition
from tui.geo import (
    CorridorSpeedRule,
    GeoPoint,
    InfrastructureFeature,
    SlowOrder,
    parse_infrastructure_geojson,
    parse_speed_limits_json,
)

# Earth radius in meters (WGS84 mean radius)
EARTH_RADIUS_M = 6371000.0

# Default maximum orthogonal snapping distance in meters for visual map-matching
DEFAULT_SNAP_THRESHOLD_M = 1500.0

# Default data file paths
DATA_DIR = Path(__file__).parent / "data"
DEFAULT_STATIONS_FILE = DATA_DIR / "stations.json"
DEFAULT_TRACKS_FILE = DATA_DIR / "tracks.json"
DEFAULT_INFRASTRUCTURE_FILE = DATA_DIR / "infrastructure.json"
DEFAULT_SPEED_LIMITS_FILE = DATA_DIR / "speed_limits.json"


def haversine_distance(p1: GeoPoint, p2: GeoPoint) -> float:
    """Calculate the great-circle distance between two GeoPoints in meters.

    Uses the standard Haversine formula on a spherical Earth model.
    """
    phi1 = math.radians(p1.lat)
    phi2 = math.radians(p2.lat)
    delta_phi = math.radians(p2.lat - p1.lat)
    delta_lambda = math.radians(p2.lng - p1.lng)

    a = (
        math.sin(delta_phi / 2.0) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2.0) ** 2
    )
    # Clamp to avoid precision issues outside [0, 1]
    a = min(1.0, max(0.0, a))
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))
    return EARTH_RADIUS_M * c


def calculate_bearing(p1: GeoPoint, p2: GeoPoint) -> float:
    """Calculate the forward azimuth / heading from p1 to p2 in degrees [0, 360).

    0° indicates North, 90° East, 180° South, 270° West.
    """
    phi1 = math.radians(p1.lat)
    phi2 = math.radians(p2.lat)
    delta_lambda = math.radians(p2.lng - p1.lng)

    y = math.sin(delta_lambda) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - (
        math.sin(phi1) * math.cos(phi2) * math.cos(delta_lambda)
    )

    bearing = math.degrees(math.atan2(y, x))
    return (bearing + 360.0) % 360.0


def project_point_to_segment(
    point: GeoPoint, p0: GeoPoint, p1: GeoPoint
) -> Tuple[GeoPoint, float]:
    """Orthogonally project a geographic point onto a line segment [p0, p1].

    Uses a local Equirectangular tangent-plane approximation scaled at the
    segment's mean latitude to compute the clamped projection parameter
    t in [0.0, 1.0], the interpolated GeoPoint on [p0, p1], and the
    perpendicular distance in meters.

    Args:
        point: Query geographic coordinate (lat, lng).
        p0: Segment start coordinate (lat, lng).
        p1: Segment end coordinate (lat, lng).

    Returns:
        Tuple of (snapped_geopoint, perpendicular_distance_m).
    """
    mean_lat_rad = math.radians((p0.lat + p1.lat) * 0.5)
    m_per_deg_lat = (math.pi / 180.0) * EARTH_RADIUS_M
    m_per_deg_lng = m_per_deg_lat * math.cos(mean_lat_rad)

    ux = (p1.lng - p0.lng) * m_per_deg_lng
    uy = (p1.lat - p0.lat) * m_per_deg_lat

    vx = (point.lng - p0.lng) * m_per_deg_lng
    vy = (point.lat - p0.lat) * m_per_deg_lat

    seg_len_sq = ux * ux + uy * uy
    if seg_len_sq <= 1e-12:
        return p0, math.hypot(vx, vy)

    t = (vx * ux + vy * uy) / seg_len_sq
    t = max(0.0, min(1.0, t))

    snapped_lat = p0.lat + t * (p1.lat - p0.lat)
    snapped_lng = p0.lng + t * (p1.lng - p0.lng)

    dx = vx - t * ux
    dy = vy - t * uy
    perp_dist_m = math.hypot(dx, dy)

    return GeoPoint(lat=snapped_lat, lng=snapped_lng), perp_dist_m


@dataclass(frozen=True)
class StationNode:
    """Represents a physical railway station node.

    Attributes:
        id: Unique uppercase station identifier (e.g. 'ATH', 'SKG').
        code: Official numerical station code.
        name_el: Station name in Greek.
        name_en: Station name in Latin/English transliteration.
        point: WGS84 geographic coordinate.
        corridor: Primary railway corridor identifier.
        is_hub: Flag indicating major interchange or terminal station.
        altitude_m: Altitude in meters above sea level (optional).
    """

    id: str
    code: str
    name_el: str
    name_en: str
    point: GeoPoint
    corridor: str
    is_hub: bool
    altitude_m: Optional[float] = None


@dataclass
class TrackPolyline:
    """Represents a continuous track polyline with precomputed cumulative distances.

    Attributes:
        corridor_id: Unique corridor key (e.g. 'pathe-mainline').
        name: Human-readable corridor name.
        points: Ordered sequence of GeoPoint coordinates.
        cumulative_distances_m: Precomputed cumulative distance array in meters.
        total_length_m: Total length of the track polyline in meters.
    """

    corridor_id: str
    name: str
    points: List[GeoPoint]
    cumulative_distances_m: List[float]
    total_length_m: float

    @classmethod
    def from_coordinates(
        cls, corridor_id: str, name: str, coords: List[Tuple[float, float]]
    ) -> "TrackPolyline":
        """Construct a TrackPolyline from a list of (lat, lng) tuples."""
        if len(coords) < 2:
            raise ValueError("A track polyline requires at least 2 coordinate points.")

        points = [GeoPoint(lat=c[0], lng=c[1]) for c in coords]
        cum_dist = [0.0]
        running_dist = 0.0

        for i in range(1, len(points)):
            d = haversine_distance(points[i - 1], points[i])
            running_dist += d
            cum_dist.append(running_dist)

        return cls(
            corridor_id=corridor_id,
            name=name,
            points=points,
            cumulative_distances_m=cum_dist,
            total_length_m=running_dist,
        )

    def interpolate_at_distance(self, distance_m: float) -> Tuple[GeoPoint, float]:
        """Interpolate geographic coordinate and tangent heading at a given distance.

        Args:
            distance_m: Distance along polyline from start in meters.

        Returns:
            Tuple of (GeoPoint, heading_degrees).
        """
        # Clamp distance within [0, total_length_m]
        d = max(0.0, min(self.total_length_m, distance_m))

        if d <= 0.0:
            heading = calculate_bearing(self.points[0], self.points[1])
            return self.points[0], heading

        if d >= self.total_length_m:
            heading = calculate_bearing(self.points[-2], self.points[-1])
            return self.points[-1], heading

        # Find segment index using binary search
        idx = bisect.bisect_right(self.cumulative_distances_m, d)
        seg_idx = max(0, idx - 1)
        next_idx = min(len(self.points) - 1, seg_idx + 1)

        d0 = self.cumulative_distances_m[seg_idx]
        d1 = self.cumulative_distances_m[next_idx]
        seg_len = d1 - d0

        if seg_len <= 1e-6:
            heading = calculate_bearing(self.points[seg_idx], self.points[next_idx])
            return self.points[seg_idx], heading

        alpha = (d - d0) / seg_len
        p0 = self.points[seg_idx]
        p1 = self.points[next_idx]

        lat = p0.lat + alpha * (p1.lat - p0.lat)
        lng = p0.lng + alpha * (p1.lng - p0.lng)
        heading = calculate_bearing(p0, p1)

        return GeoPoint(lat=lat, lng=lng), heading

    def project_point(self, point: GeoPoint) -> Tuple[GeoPoint, float, float]:
        """Project a geographic coordinate onto the closest segment of this polyline.

        Args:
            point: Query GeoPoint (lat, lng) to project onto the track.

        Returns:
            Tuple of (snapped_point, perpendicular_distance_m, segment_heading_deg).
        """
        best_point = self.points[0]
        best_dist_m = float("inf")
        best_heading = (
            calculate_bearing(self.points[0], self.points[1])
            if len(self.points) > 1
            else 0.0
        )

        for i in range(1, len(self.points)):
            p0 = self.points[i - 1]
            p1 = self.points[i]
            snapped_pt, dist_m = project_point_to_segment(point, p0, p1)
            if dist_m < best_dist_m:
                best_dist_m = dist_m
                best_point = snapped_pt
                best_heading = calculate_bearing(p0, p1)

        return best_point, best_dist_m, best_heading


def snap_to_track_network(
    point: GeoPoint,
    tracks: Sequence[TrackPolyline],
    corridor_hint: Optional[str] = None,
    max_snap_distance_m: float = DEFAULT_SNAP_THRESHOLD_M,
) -> GeoPoint:
    """Snap a geographic coordinate to the nearest track polyline if within threshold.

    Evaluates orthogonal projection across the provided track polylines and returns
    the snapped centerline coordinate if the minimum perpendicular distance is within
    max_snap_distance_m. If no track segment lies within the threshold (or tracks is
    empty), returns the original GeoPoint unmodified.

    Args:
        point: Raw geographic coordinate (lat, lng).
        tracks: Sequence of available TrackPolyline corridors.
        corridor_hint: Optional corridor ID or name hint. Even when provided, all
            tracks are checked if the hinted corridor is not an exact ID match or if
            a closer shared-trunk segment exists within threshold.
        max_snap_distance_m: Maximum perpendicular distance in meters allowed for
            snapping (defaults to 1500.0m).

    Returns:
        Snapped GeoPoint on the nearest track segment, or unmodified point.
    """
    if not tracks or max_snap_distance_m < 0.0:
        return point

    best_point = point
    best_dist_m = float("inf")

    if corridor_hint:
        hint_lower = corridor_hint.strip().lower()
        hinted_tracks = [
            t
            for t in tracks
            if t.corridor_id.lower() == hint_lower
            or hint_lower in t.corridor_id.lower()
        ]
        for track in hinted_tracks:
            snapped_pt, dist_m, _ = track.project_point(point)
            if dist_m < best_dist_m:
                best_dist_m = dist_m
                best_point = snapped_pt
        if best_dist_m <= max_snap_distance_m:
            return best_point

    for track in tracks:
        snapped_pt, dist_m, _ = track.project_point(point)
        if dist_m < best_dist_m:
            best_dist_m = dist_m
            best_point = snapped_pt

    if best_dist_m <= max_snap_distance_m:
        return best_point
    return point


def load_stations(path: Optional[str] = None) -> Dict[str, StationNode]:
    """Load and parse railway stations from JSON file.

    Args:
        path: Path to stations.json. Defaults to package data directory.

    Returns:
        Dictionary mapping station ID to StationNode.
    """
    file_path = Path(path) if path else DEFAULT_STATIONS_FILE
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    stations = {}
    for item in data.get("stations", []):
        st = StationNode(
            id=item["id"],
            code=str(item.get("code", "")),
            name_el=item.get("name_el", ""),
            name_en=item.get("name_en", ""),
            point=GeoPoint(lat=float(item["lat"]), lng=float(item["lng"])),
            corridor=item.get("corridor", ""),
            is_hub=bool(item.get("is_hub", False)),
            altitude_m=float(item["altitude_m"]) if item.get("altitude_m") is not None else None,
        )
        stations[st.id] = st

    return stations


def normalize_station_key(name: str) -> str:
    """Normalize a station identifier or Greek/Latin name for diacritic-insensitive lookup.

    Strips Unicode combining accents/tonos (NFD category 'Mn'), converts to lowercase,
    and collapses surrounding whitespace.

    Args:
        name: Raw station name, code, or identifier.

    Returns:
        Normalized lookup key string.
    """
    if not name:
        return ""
    decomposed = unicodedata.normalize("NFD", name.strip())
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", stripped).casefold().strip()


def build_station_index(stations: Sequence[StationNode]) -> Dict[str, StationNode]:
    """Build a multi-key lookup dictionary mapping Greek, English, ID, and alias keys to StationNode.

    Args:
        stations: Sequence of StationNode objects.

    Returns:
        Dictionary mapping normalized keys to their corresponding StationNode.
    """
    index: Dict[str, StationNode] = {}
    for stn in stations:
        candidates: List[str] = [stn.id, stn.code, stn.name_el, stn.name_en]
        for raw_name in (stn.name_el, stn.name_en):
            if "(" in raw_name:
                base = raw_name.split("(", 1)[0].strip()
                if base:
                    candidates.append(base)
            if "-" in raw_name:
                for part in raw_name.split("-"):
                    part_clean = part.strip()
                    if len(part_clean) >= 3:
                        candidates.append(part_clean)

        for candidate in candidates:
            key = normalize_station_key(candidate)
            if key and key not in index:
                index[key] = stn

        if stn.id.upper() == "AIA":
            for alias in ("αεροδρομιο", "airport", "athens airport"):
                if alias not in index:
                    index[alias] = stn

    return index


def resolve_station_for_cli(
    query: str,
    stations: Optional[Dict[str, StationNode]] = None,
) -> Tuple[Optional[StationNode], List[str]]:
    """Resolve a user-provided station query to a canonical StationNode with suggestions on miss.

    Resolution order:
    1. Exact match in index (handles ID, code, normalized full Greek/English names, base names).
    2. Prefix match across index keys (minimum 3 characters).
    3. Substring match across Greek and Latin names.
    4. Close matches via difflib as helpful suggestions.

    Args:
        query: User input query (Greek, Latin, ID, or code).
        stations: Optional dictionary of station ID to StationNode. Loaded if None.

    Returns:
        Tuple of (matched_node, suggestions_list).
    """
    if not query or not query.strip():
        return None, []

    if stations is None:
        stations = load_stations()

    stn_list = list(stations.values())
    index = build_station_index(stn_list)
    norm_query = normalize_station_key(query)

    # 1. Exact match in index
    if norm_query in index:
        return index[norm_query], []

    # 2. Prefix match in index (minimum 3 characters)
    if len(norm_query) >= 3:
        for key, stn in index.items():
            if key.startswith(norm_query):
                return stn, []

    # 3. Substring match across station English and Greek names
    if len(norm_query) >= 3:
        for stn in stn_list:
            norm_en = normalize_station_key(stn.name_en)
            norm_el = normalize_station_key(stn.name_el)
            if norm_query in norm_en or norm_query in norm_el:
                return stn, []

    # 4. Generate suggestions if not matched
    norm_to_display: Dict[str, str] = {}
    for stn in stn_list:
        display = f"{stn.name_en} [{stn.id}]"
        norm_to_display[normalize_station_key(stn.name_en)] = display
        norm_to_display[normalize_station_key(stn.name_el)] = f"{stn.name_el} / {stn.name_en} [{stn.id}]"
        norm_to_display[stn.id.lower()] = display

    close_keys = difflib.get_close_matches(norm_query, list(norm_to_display.keys()), n=5, cutoff=0.4)
    suggestions: List[str] = []
    for k in close_keys:
        disp = norm_to_display[k]
        if disp not in suggestions:
            suggestions.append(disp)

    # Fallback to hub stations if no close suggestions
    if not suggestions:
        suggestions = [f"{stn.name_en} [{stn.id}]" for stn in stn_list if stn.is_hub][:5]

    return None, suggestions


def generate_mock_station_board(
    station: StationNode,
    is_arrivals: bool = False,
    limit: int = 15,
) -> List[Dict[str, Any]]:
    """Synthesize realistic scheduled departures or arrivals for offline demonstration.

    Args:
        station: Target StationNode.
        is_arrivals: True for arrival board, False for departure board.
        limit: Maximum records to return.

    Returns:
        List of departure/arrival dictionary records.
    """
    sid = station.id.upper()
    now_hour = 14
    now_min = 25

    # Realistic timetable template definitions per station hub/corridor
    templates: List[Dict[str, Any]] = []

    if sid in ("ATH", "PIR", "SKA", "SKG", "LAR", "LIA", "DOM", "KAI", "PAL", "PLA"):
        # Mainline PATHE stations
        templates.extend([
            {
                "trainNumber": "IC52",
                "serviceType": "Intercity",
                "offset_min": 7,
                "origin": {"id": "ATH", "nameGreek": "Αθήνα", "nameEnglish": "Athens"},
                "destination": {"id": "SKG", "nameGreek": "Θεσσαλονίκη", "nameEnglish": "Thessaloniki"},
                "platform": "1",
                "delayMinutes": 0,
                "status": "ON_TIME",
            },
            {
                "trainNumber": "1597",
                "serviceType": "Suburban",
                "offset_min": 20,
                "origin": {"id": "ATH", "nameGreek": "Αθήνα", "nameEnglish": "Athens"},
                "destination": {"id": "CHA", "nameGreek": "Χαλκίδα", "nameEnglish": "Chalkida"},
                "platform": "3",
                "delayMinutes": 8,
                "status": "DELAYED",
            },
            {
                "trainNumber": "2236",
                "serviceType": "Suburban",
                "offset_min": 40,
                "origin": {"id": "ANO", "nameGreek": "Άνω Λιόσια", "nameEnglish": "Ano Liosia"},
                "destination": {"id": "AIA", "nameGreek": "Αεροδρόμιο", "nameEnglish": "Athens Airport"},
                "platform": "2",
                "delayMinutes": 0,
                "status": "ON_TIME",
            },
            {
                "trainNumber": "IC54",
                "serviceType": "Intercity",
                "offset_min": 57,
                "origin": {"id": "ATH", "nameGreek": "Αθήνα", "nameEnglish": "Athens"},
                "destination": {"id": "KAL", "nameGreek": "Καλαμπάκα", "nameEnglish": "Kalambaka"},
                "platform": "1",
                "delayMinutes": 0,
                "status": "ON_TIME",
            },
            {
                "trainNumber": "1314",
                "serviceType": "Suburban",
                "offset_min": 70,
                "origin": {"id": "PIR", "nameGreek": "Πειραιάς", "nameEnglish": "Piraeus"},
                "destination": {"id": "KIA", "nameGreek": "Κιάτο", "nameEnglish": "Kiato"},
                "platform": "4",
                "delayMinutes": 0,
                "status": "ON_TIME",
            },
            {
                "trainNumber": "IC56",
                "serviceType": "Intercity",
                "offset_min": 95,
                "origin": {"id": "ATH", "nameGreek": "Αθήνα", "nameEnglish": "Athens"},
                "destination": {"id": "SKG", "nameGreek": "Θεσσαλονίκη", "nameEnglish": "Thessaloniki"},
                "platform": "1",
                "delayMinutes": 0,
                "status": "BOARDING",
            },
            {
                "trainNumber": "2240",
                "serviceType": "Suburban",
                "offset_min": 110,
                "origin": {"id": "PIR", "nameGreek": "Πειραιάς", "nameEnglish": "Piraeus"},
                "destination": {"id": "AIA", "nameGreek": "Αεροδρόμιο", "nameEnglish": "Athens Airport"},
                "platform": "2",
                "delayMinutes": 2,
                "status": "ON_TIME",
            },
            {
                "trainNumber": "1599",
                "serviceType": "Suburban",
                "offset_min": 140,
                "origin": {"id": "ATH", "nameGreek": "Αθήνα", "nameEnglish": "Athens"},
                "destination": {"id": "CHA", "nameGreek": "Χαλκίδα", "nameEnglish": "Chalkida"},
                "platform": "3",
                "delayMinutes": 4,
                "status": "DELAYED",
            },
        ])
    else:
        # Generic corridor stations
        templates.extend([
            {
                "trainNumber": "1302",
                "serviceType": "Regional",
                "offset_min": 15,
                "origin": {"id": station.id, "nameGreek": station.name_el, "nameEnglish": station.name_en},
                "destination": {"id": "ATH", "nameGreek": "Αθήνα", "nameEnglish": "Athens"},
                "platform": "1",
                "delayMinutes": 0,
                "status": "ON_TIME",
            },
            {
                "trainNumber": "1305",
                "serviceType": "Regional",
                "offset_min": 45,
                "origin": {"id": "ATH", "nameGreek": "Αθήνα", "nameEnglish": "Athens"},
                "destination": {"id": station.id, "nameGreek": station.name_el, "nameEnglish": station.name_en},
                "platform": "2",
                "delayMinutes": 5,
                "status": "DELAYED",
            },
            {
                "trainNumber": "2308",
                "serviceType": "Suburban",
                "offset_min": 75,
                "origin": {"id": station.id, "nameGreek": station.name_el, "nameEnglish": station.name_en},
                "destination": {"id": "SKG", "nameGreek": "Θεσσαλονίκη", "nameEnglish": "Thessaloniki"},
                "platform": "1",
                "delayMinutes": 0,
                "status": "ON_TIME",
            },
        ])

    results: List[Dict[str, Any]] = []
    for tmpl in templates[:limit]:
        total_min = now_hour * 60 + now_min + tmpl["offset_min"]
        sched_h = (total_min // 60) % 24
        sched_m = total_min % 60
        sched_str = f"{sched_h:02d}:{sched_m:02d}"

        delay = tmpl["delayMinutes"]
        est_min = total_min + delay
        est_h = (est_min // 60) % 24
        est_m = est_min % 60
        est_str = f"{est_h:02d}:{est_m:02d}"

        # If departures and origin == station, or if arrivals and destination == station, adapt appropriately
        orig = tmpl["origin"]
        dest = tmpl["destination"]
        if is_arrivals and orig["id"] == station.id:
            orig, dest = dest, orig
        elif not is_arrivals and dest["id"] == station.id:
            orig, dest = dest, orig

        results.append({
            "trainNumber": tmpl["trainNumber"],
            "serviceType": tmpl["serviceType"],
            "scheduledTime": sched_str,
            "estimatedTime": est_str,
            "origin": orig,
            "destination": dest,
            "platform": tmpl["platform"],
            "delayMinutes": delay,
            "status": tmpl["status"],
        })

    return results


# -----------------------------------------------------------------------------
# Train Timetable & Intermediate Stop Synthesis (F-6.2)
# -----------------------------------------------------------------------------

ACTIVE_MOCK_TRAIN_NUMBERS: Tuple[str, ...] = (
    "IC 50",
    "IC 51",
    "IC 52",
    "IC 53",
    "IC 54",
    "IC 56",
    "1597",
    "1314",
    "2232",
    "2236",
    "1731",
    "884",
    "80512",
)


def get_active_mock_train_numbers() -> List[str]:
    """Return all active commercial train numbers recognized by the mock engine."""
    return list(ACTIVE_MOCK_TRAIN_NUMBERS)


def _resolve_mock_station_sequence(
    clean_num: str,
    stations: Dict[str, StationNode],
) -> Tuple[List[StationNode], str, str, str, str]:
    """Resolve station sequence, train type, service type, origin and destination names."""
    norm = clean_num.replace(" ", "").upper()

    # 1. Proastiakos Chalkida (Athens -> Chalkida)
    if norm.startswith("15") or norm.startswith("13") or "CHAL" in norm:
        stn_ids = ["ATH", "SKA", "AFI", "SFE", "AVL", "INO", "OIN", "CHAL"]
        seq = [stations[sid] for sid in stn_ids if sid in stations]
        return seq, "Proastiakos", "Προαστιακός", "Athens", "Chalkida"

    # 2. Proastiakos Airport - Kiato
    if norm.startswith("22") or norm.startswith("12") or "AIA" in norm:
        stn_ids = ["AIA", "KOR", "KAN", "PAL_SUB", "DPL", "KIF", "NER", "IRA", "SKA", "ALI", "ASP", "MAG", "MEG", "ATH_TH", "COR", "KIA"]
        seq = [stations[sid] for sid in stn_ids if sid in stations]
        return seq, "Proastiakos", "Προαστιακός", "Airport", "Kiato"

    # 3. Thessaly Regional
    if norm in ("1731", "VOL"):
        stn_ids = ["LAR", "VEL", "VOL"]
        seq = [stations[sid] for sid in stn_ids if sid in stations]
        return seq, "Regional", "Περιφερειακό", "Larissa", "Volos"

    # 4. Southbound Intercity (Thessaloniki -> Athens)
    if norm in ("IC51", "IC53", "IC55", "51", "53", "55"):
        stn_ids = ["SKG", "PLA", "KAT", "LAR", "PAL", "DOM", "LIA", "TIT", "LEV", "THI", "SKA", "ATH"]
        seq = [stations[sid] for sid in stn_ids if sid in stations]
        return seq, "IC", "Επιβατικά", "Thessaloniki", "Athens"

    # 5. Default: Northbound Intercity (Athens -> Thessaloniki)
    stn_ids = ["ATH", "SKA", "THI", "LEV", "TIT", "LIA", "DOM", "PAL", "LAR", "KAT", "PLA", "SKG"]
    seq = [stations[sid] for sid in stn_ids if sid in stations]
    t_type = "Freight" if norm.startswith("80") else "IC"
    s_type = "Εμπορικά" if norm.startswith("80") else "Επιβατικά"
    return seq, t_type, s_type, "Athens", "Thessaloniki"


def generate_mock_train_schedule(
    train_number: str,
    stations: Optional[Dict[str, StationNode]] = None,
    trains: Optional[List[Any]] = None,
) -> Dict[str, Any]:
    """Synthesize a complete timetable itinerary and intermediate stops for a train.

    Args:
        train_number: Commercial train number (e.g. '1597', 'IC52', 'IC 52').
        stations: Optional dictionary mapping station ID to StationNode. Loaded if None.
        trains: Optional list of active SimulatedTrain instances.

    Returns:
        Structured timetable dictionary matching /api/public/train-schedules schema.
    """
    if stations is None:
        stations = load_stations()

    clean_num = train_number.strip().upper()
    seq, train_type, service_type, orig_name, dest_name = _resolve_mock_station_sequence(clean_num, stations)

    if not seq:
        # Fallback to default hub stations
        seq = [s for s in stations.values() if s.is_hub][:8]

    # Inspect if matching simulated train exists in active fleet
    active_train: Optional[SimulatedTrain] = None
    if trains is not None:
        for t in trains:
            if t.train_number.replace(" ", "").upper() == clean_num.replace(" ", ""):
                active_train = t
                break

    delay_min = active_train.delay_minutes if active_train else (7 if "52" in clean_num else 0)
    next_stn_name = active_train.next_station_name if active_train else None

    # Base schedule timeline: 14:00 base anchor
    base_hour = 14
    base_min = 28 if "1597" in clean_num else 15
    dwell_min = 2

    # Determine split point for passed vs current vs upcoming stops
    current_idx = 1
    if next_stn_name:
        for idx, stn in enumerate(seq):
            if next_stn_name in stn.name_el or next_stn_name in stn.name_en:
                current_idx = idx
                break
    else:
        # Default ~25-30% progression
        current_idx = max(1, min(len(seq) - 1, len(seq) // 3))

    step_min = 18 if train_type in ("IC", "Freight") else 7
    stops: List[Dict[str, Any]] = []

    for i, stn in enumerate(seq):
        sched_arr_total = base_min + i * step_min
        sched_dep_total = sched_arr_total + dwell_min

        arr_h = (base_hour + (sched_arr_total // 60)) % 24
        arr_m = sched_arr_total % 60
        dep_h = (base_hour + (sched_dep_total // 60)) % 24
        dep_m = sched_dep_total % 60

        sched_arr_str = None if i == 0 else f"{arr_h:02d}:{arr_m:02d}"
        sched_dep_str = None if i == len(seq) - 1 else f"{dep_h:02d}:{dep_m:02d}"

        is_passed = i < current_idx
        is_current = i == current_idx

        if is_passed:
            status = "DEPARTED"
            act_arr_total = sched_arr_total + delay_min
            act_dep_total = sched_dep_total + delay_min
            act_arr_h = (base_hour + (act_arr_total // 60)) % 24
            act_arr_m = act_arr_total % 60
            act_dep_h = (base_hour + (act_dep_total // 60)) % 24
            act_dep_m = act_dep_total % 60
            act_arr_str = None if i == 0 else f"{act_arr_h:02d}:{act_arr_m:02d}"
            act_dep_str = None if i == len(seq) - 1 else f"{act_dep_h:02d}:{act_dep_m:02d}"
            stop_delay = delay_min
        elif is_current:
            status = "APPROACH"
            act_arr_total = sched_arr_total + delay_min
            act_arr_h = (base_hour + (act_arr_total // 60)) % 24
            act_arr_m = act_arr_total % 60
            act_arr_str = f"{act_arr_h:02d}:{act_arr_m:02d}"
            act_dep_str = None
            stop_delay = delay_min
        else:
            status = "SCHEDULED"
            act_arr_str = None
            act_dep_str = None
            stop_delay = 0

        # Alternate platform track 1..4 based on index
        plat_num = str(((i + hash(clean_num)) % 4) + 1)

        stops.append({
            "stopSequence": i + 1,
            "stationId": stn.id,
            "stationNameGreek": stn.name_el,
            "stationNameEnglish": stn.name_en,
            "scheduledArrival": sched_arr_str,
            "actualArrival": act_arr_str,
            "scheduledDeparture": sched_dep_str,
            "actualDeparture": act_dep_str,
            "platform": plat_num,
            "delayMinutes": stop_delay,
            "status": status,
            "is_passed": is_passed,
            "is_current": is_current,
        })

    origin_stn = seq[0]
    dest_stn = seq[-1]

    return {
        "scheduleId": f"sch-mock-{clean_num}",
        "trainNumber": clean_num,
        "trainType": train_type,
        "serviceType": service_type,
        "origin": {
            "id": origin_stn.id,
            "nameGreek": origin_stn.name_el,
            "nameEnglish": origin_stn.name_en,
        },
        "destination": {
            "id": dest_stn.id,
            "nameGreek": dest_stn.name_el,
            "nameEnglish": dest_stn.name_en,
        },
        "corridor": origin_stn.corridor,
        "delayMinutes": delay_min,
        "stops": stops,
    }


def load_tracks(path: Optional[str] = None) -> Dict[str, TrackPolyline]:
    """Load and parse railway track corridors from JSON file.

    Args:
        path: Path to tracks.json. Defaults to package data directory.

    Returns:
        Dictionary mapping corridor ID to TrackPolyline.
    """
    file_path = Path(path) if path else DEFAULT_TRACKS_FILE
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    corridors = {}
    for item in data.get("corridors", []):
        cid = item["id"]
        cname = item.get("name", cid)
        segments = item.get("segments", [])
        if not segments or not segments[0]:
            continue

        # Flatten segments or take the primary mainline segment
        primary_coords = segments[0]
        poly = TrackPolyline.from_coordinates(cid, cname, primary_coords)
        corridors[cid] = poly

    return corridors


def load_infrastructure(path: Optional[str] = None) -> Dict[str, InfrastructureFeature]:
    """Load and parse railway infrastructure assets from GeoJSON file.

    Args:
        path: Path to infrastructure.json. Defaults to package data directory.

    Returns:
        Dictionary mapping infrastructure feature ID to InfrastructureFeature.
    """
    file_path = Path(path) if path else DEFAULT_INFRASTRUCTURE_FILE
    if not file_path.exists():
        return {}
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    parsed = parse_infrastructure_geojson(data)
    return {f.id: f for f in parsed}


def load_speed_limits(
    path: Optional[str] = None,
) -> Tuple[Dict[str, CorridorSpeedRule], List[SlowOrder]]:
    """Load and parse Greek railway corridor speed rules and slow orders.

    Args:
        path: Path to speed_limits.json. Defaults to package data directory.

    Returns:
        Tuple of (corridor_speed_rules_dict, slow_orders_list).
    """
    file_path = Path(path) if path else DEFAULT_SPEED_LIMITS_FILE
    if not file_path.exists():
        from tui.geo import DEFAULT_CORRIDOR_SPEED_RULES
        return dict(DEFAULT_CORRIDOR_SPEED_RULES), []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return parse_speed_limits_json(data)
    except Exception:
        from tui.geo import DEFAULT_CORRIDOR_SPEED_RULES
        return dict(DEFAULT_CORRIDOR_SPEED_RULES), []


def load_slow_orders(path: Optional[str] = None) -> List[SlowOrder]:
    """Load active Slow Orders (Temporary Speed Restrictions / TSRs).

    Args:
        path: Path to speed_limits.json. Defaults to package data directory.

    Returns:
        List of active SlowOrder domain records.
    """
    _, tsrs = load_speed_limits(path)
    return [t for t in tsrs if t.active]


class TrainMotionState(Enum):
    """Kinematic state of a simulated train."""

    RUNNING = "RUNNING"
    DECELERATING = "DECELERATING"
    STOPPED = "STOPPED"
    ACCELERATING = "ACCELERATING"
    TERMINAL_DWELL = "TERMINAL_DWELL"


@dataclass
class SimulatedTrain:
    """Configuration and dynamic kinematic state for a simulated train.

    Attributes:
        id: Unique train identifier.
        train_number: Commercial train number (e.g. 'IC 50', '1597').
        locomotive_number: Locomotive identifier (e.g. '120010').
        train_type: Type of train ('IC', 'Proastiakos', 'Regional', 'Freight').
        service_type: Service category.
        origin: Departure station name.
        destination: Arrival station name.
        corridor: Corridor identifier.
        track: TrackPolyline instance representing the traversed route.
        cruise_speed_kmh: Target cruising speed in km/h.
        acceleration_mps2: Acceleration rate in m/s².
        deceleration_mps2: Braking deceleration rate in m/s² (positive number).
        dwell_duration_sec: Normal station stop dwell time in seconds.
        terminal_dwell_sec: Terminal turnaround dwell time in seconds.
    """

    id: str
    train_number: str
    locomotive_number: str
    train_type: str
    service_type: str
    origin: str
    destination: str
    corridor: str
    track: TrackPolyline
    cruise_speed_kmh: float
    acceleration_mps2: float = 0.5
    deceleration_mps2: float = 0.6
    dwell_duration_sec: float = 40.0
    terminal_dwell_sec: float = 120.0

    # Dynamic mutable kinematic state
    current_distance_m: float = 0.0
    current_speed_kmh: float = 0.0
    direction: int = 1  # 1: forward along track points, -1: reverse
    motion_state: TrainMotionState = TrainMotionState.RUNNING
    dwell_timer_sec: float = 0.0
    delay_minutes: int = 0
    next_station_name: str = ""
    _time_since_delay_update: float = field(default=0.0, init=False, repr=False)
    _initial_distance_m: float = field(default=0.0, init=False, repr=False)
    _initial_direction: int = field(default=1, init=False, repr=False)

    def __post_init__(self) -> None:
        self._initial_distance_m = self.current_distance_m
        self._initial_direction = self.direction
        if self.motion_state == TrainMotionState.RUNNING and self.current_speed_kmh <= 0.0:
            self.current_speed_kmh = self.cruise_speed_kmh

    def reset(self) -> None:
        """Reset train to initial starting position and state."""
        self.current_distance_m = self._initial_distance_m
        self.direction = self._initial_direction
        self.motion_state = TrainMotionState.RUNNING
        self.current_speed_kmh = self.cruise_speed_kmh
        self.dwell_timer_sec = 0.0
        self._time_since_delay_update = 0.0

    def step(self, dt_sec: float) -> TrainPosition:
        """Advance train kinematics by dt_sec and return a domain TrainPosition."""
        if dt_sec <= 0.0:
            dt_sec = 0.001

        # Periodic delay drift
        self._time_since_delay_update += dt_sec
        if self._time_since_delay_update >= 120.0:
            self._time_since_delay_update = 0.0
            drift = random.choice([-1, 0, 0, 0, 1])
            self.delay_minutes = max(0, min(60, self.delay_minutes + drift))

        # State machine processing
        if self.motion_state in (TrainMotionState.STOPPED, TrainMotionState.TERMINAL_DWELL):
            self.current_speed_kmh = 0.0
            self.dwell_timer_sec -= dt_sec
            if self.dwell_timer_sec <= 0.0:
                self.dwell_timer_sec = 0.0
                if self.motion_state == TrainMotionState.TERMINAL_DWELL:
                    # Reverse direction and swap origin/destination
                    self.direction = -self.direction
                    self.origin, self.destination = self.destination, self.origin
                self.motion_state = TrainMotionState.ACCELERATING

        elif self.motion_state == TrainMotionState.ACCELERATING:
            # Acceleration step
            acc_kmh_per_s = self.acceleration_mps2 * 3.6
            self.current_speed_kmh = min(
                self.cruise_speed_kmh, self.current_speed_kmh + acc_kmh_per_s * dt_sec
            )
            dist_delta = (self.current_speed_kmh / 3.6) * dt_sec * self.direction
            self.current_distance_m += dist_delta
            if self.current_speed_kmh >= self.cruise_speed_kmh:
                self.motion_state = TrainMotionState.RUNNING

        elif self.motion_state == TrainMotionState.RUNNING:
            self.current_speed_kmh = self.cruise_speed_kmh
            dist_delta = (self.current_speed_kmh / 3.6) * dt_sec * self.direction
            self.current_distance_m += dist_delta

            # Check distance to terminus for braking initiation
            brake_dist = ((self.cruise_speed_kmh / 3.6) ** 2) / (2.0 * self.deceleration_mps2)
            if self.direction == 1:
                dist_to_end = self.track.total_length_m - self.current_distance_m
                if dist_to_end <= brake_dist:
                    self.motion_state = TrainMotionState.DECELERATING
            else:
                dist_to_start = self.current_distance_m
                if dist_to_start <= brake_dist:
                    self.motion_state = TrainMotionState.DECELERATING

        elif self.motion_state == TrainMotionState.DECELERATING:
            dec_kmh_per_s = self.deceleration_mps2 * 3.6
            self.current_speed_kmh = max(0.0, self.current_speed_kmh - dec_kmh_per_s * dt_sec)
            dist_delta = (self.current_speed_kmh / 3.6) * dt_sec * self.direction
            self.current_distance_m += dist_delta

            if self.current_speed_kmh <= 0.0:
                self.current_speed_kmh = 0.0
                # Check if reached terminal end
                at_end = (self.direction == 1 and self.current_distance_m >= self.track.total_length_m - 50.0) or (
                    self.direction == -1 and self.current_distance_m <= 50.0
                )
                if at_end:
                    self.motion_state = TrainMotionState.TERMINAL_DWELL
                    self.dwell_timer_sec = self.terminal_dwell_sec
                else:
                    self.motion_state = TrainMotionState.STOPPED
                    self.dwell_timer_sec = self.dwell_duration_sec

        # Clamp distance within polyline bounds
        self.current_distance_m = max(0.0, min(self.track.total_length_m, self.current_distance_m))

        # Position and heading calculation
        point, track_heading = self.track.interpolate_at_distance(self.current_distance_m)

        # When moving in reverse direction along polyline, course heading is flipped 180°
        course = (track_heading + 180.0) % 360.0 if self.direction == -1 else track_heading

        # Chainage and distance to destination
        total_len = self.track.total_length_m
        if self.direction == 1:
            dist_to_dest = max(0.0, total_len - self.current_distance_m)
            progress = self.current_distance_m / total_len if total_len > 0 else 0.0
        else:
            dist_to_dest = max(0.0, self.current_distance_m)
            progress = (total_len - self.current_distance_m) / total_len if total_len > 0 else 0.0

        progress = min(1.0, max(0.0, progress))
        status = "Στάση" if self.current_speed_kmh < 5.0 else "Κανονική"
        iso_timestamp = datetime.now(timezone.utc).isoformat()

        return TrainPosition(
            id=self.id,
            train_number=self.train_number,
            locomotive_number=self.locomotive_number,
            name=f"{self.train_type} {self.train_number}",
            train_type=self.train_type,
            service_type=self.service_type,
            status=status,
            origin=self.origin,
            destination=self.destination,
            next_station=self.next_station_name or self.destination,
            speed=round(self.current_speed_kmh, 1),
            course=round(course, 1),
            track_heading=round(track_heading, 1),
            lat=point.lat,
            lng=point.lng,
            altitude=None,
            delay=self.delay_minutes,
            progress=round(progress, 3),
            chainage_m=int(self.current_distance_m),
            corridor=self.corridor,
            distance_to_destination_m=int(dist_to_dest),
            timestamp=iso_timestamp,
            schedule_id=f"sched-{self.id}",
        )


class MockTelemetryEngine:
    """Offline telemetry engine generating realistic synthetic Greek railway movements.

    Serves as an offline simulator during development, demonstrations, or when
    the upstream live SSE stream is unreachable.
    """

    def __init__(
        self,
        stations_path: Optional[str] = None,
        tracks_path: Optional[str] = None,
        time_scale: float = 1.0,
    ) -> None:
        """Initialize mock engine, load topology datasets, and spawn standard train roster."""
        self.stations: Dict[str, StationNode] = load_stations(stations_path)
        self.tracks: Dict[str, TrackPolyline] = load_tracks(tracks_path)
        self.time_scale = max(0.1, time_scale)
        self.trains: List[SimulatedTrain] = self._init_train_roster()

    def _init_train_roster(self) -> List[SimulatedTrain]:
        """Instantiate preconfigured train fleet across main Greek corridors."""
        pathe = self.tracks.get("pathe-mainline")
        pro_kiato = self.tracks.get("proastiakos-airport-kiato")
        pro_chal = self.tracks.get("proastiakos-chalkida")
        ido = self.tracks.get("thessaloniki-idomeni")
        thess_kal = self.tracks.get("thessaly-kalambaka")
        thess_vol = self.tracks.get("thessaly-volos")

        trains = []

        if pathe:
            # IC 50: Northbound Athens -> Thessaloniki
            t_ic50 = SimulatedTrain(
                id="mock-ic-50",
                train_number="IC 50",
                locomotive_number="120005",
                train_type="IC",
                service_type="Επιβατικά",
                origin="Αθήνα",
                destination="Θεσσαλονίκη",
                corridor="PATHE",
                track=pathe,
                cruise_speed_kmh=150.0,
                acceleration_mps2=0.45,
                deceleration_mps2=0.60,
                dwell_duration_sec=60.0,
                current_distance_m=pathe.total_length_m * 0.15,  # Past SKA
                direction=1,
                delay_minutes=2,
                next_station_name="Θήβα",
            )
            trains.append(t_ic50)

            # IC 51: Southbound Thessaloniki -> Athens
            t_ic51 = SimulatedTrain(
                id="mock-ic-51",
                train_number="IC 51",
                locomotive_number="120012",
                train_type="IC",
                service_type="Επιβατικά",
                origin="Θεσσαλονίκη",
                destination="Αθήνα",
                corridor="PATHE",
                track=pathe,
                cruise_speed_kmh=155.0,
                acceleration_mps2=0.45,
                deceleration_mps2=0.60,
                dwell_duration_sec=60.0,
                current_distance_m=pathe.total_length_m * 0.75,  # South of Larissa
                direction=-1,
                delay_minutes=0,
                next_station_name="Λάρισα",
            )
            trains.append(t_ic51)

            # IC 52: Northbound Athens -> Thessaloniki
            t_ic52 = SimulatedTrain(
                id="mock-ic-52",
                train_number="IC 52",
                locomotive_number="120018",
                train_type="IC",
                service_type="Επιβατικά",
                origin="Αθήνα",
                destination="Θεσσαλονίκη",
                corridor="PATHE",
                track=pathe,
                cruise_speed_kmh=140.0,
                acceleration_mps2=0.45,
                deceleration_mps2=0.60,
                dwell_duration_sec=60.0,
                current_distance_m=pathe.total_length_m * 0.45,  # Near Lianokladi
                direction=1,
                delay_minutes=7,
                next_station_name="Λιανοκλάδι",
            )
            trains.append(t_ic52)

            # IC 53: Southbound Thessaloniki -> Athens
            t_ic53 = SimulatedTrain(
                id="mock-ic-53",
                train_number="IC 53",
                locomotive_number="120023",
                train_type="IC",
                service_type="Επιβατικά",
                origin="Θεσσαλονίκη",
                destination="Αθήνα",
                corridor="PATHE",
                track=pathe,
                cruise_speed_kmh=145.0,
                acceleration_mps2=0.45,
                deceleration_mps2=0.60,
                dwell_duration_sec=60.0,
                current_distance_m=pathe.total_length_m * 0.35,  # Near Domokos
                direction=-1,
                delay_minutes=12,
                next_station_name="Δομοκός",
            )
            trains.append(t_ic53)

            # Freight 80512: Northbound Thiva -> Thessaloniki
            t_frg = SimulatedTrain(
                id="mock-frg-80512",
                train_number="80512",
                locomotive_number="120002",
                train_type="Freight",
                service_type="Εμπορικά",
                origin="Θήβα",
                destination="Θεσσαλονίκη",
                corridor="PATHE",
                track=pathe,
                cruise_speed_kmh=75.0,
                acceleration_mps2=0.20,
                deceleration_mps2=0.35,
                dwell_duration_sec=10.0,
                current_distance_m=pathe.total_length_m * 0.28,
                direction=1,
                delay_minutes=0,
                next_station_name="Λιβαδειά",
            )
            trains.append(t_frg)

        if pro_kiato:
            # Proastiakos 1597: Piraeus -> Kiato
            t_pro1597 = SimulatedTrain(
                id="mock-pro-1597",
                train_number="1597",
                locomotive_number="460105",
                train_type="Proastiakos",
                service_type="Προαστιακός",
                origin="Πειραιάς",
                destination="Κιάτο",
                corridor="PROASTIAKOS",
                track=pro_kiato,
                cruise_speed_kmh=105.0,
                acceleration_mps2=0.65,
                deceleration_mps2=0.75,
                dwell_duration_sec=35.0,
                current_distance_m=pro_kiato.total_length_m * 0.60,  # Near Megara
                direction=1,
                delay_minutes=0,
                next_station_name="Μέγαρα",
            )
            trains.append(t_pro1597)

            # Proastiakos 2232: Airport -> Kiato
            t_pro2232 = SimulatedTrain(
                id="mock-pro-2232",
                train_number="2232",
                locomotive_number="460112",
                train_type="Proastiakos",
                service_type="Προαστιακός",
                origin="Αεροδρόμιο",
                destination="Κιάτο",
                corridor="PROASTIAKOS",
                track=pro_kiato,
                cruise_speed_kmh=110.0,
                acceleration_mps2=0.65,
                deceleration_mps2=0.75,
                dwell_duration_sec=35.0,
                current_distance_m=pro_kiato.total_length_m * 0.30,  # Near SKA
                direction=1,
                delay_minutes=3,
                next_station_name="ΣΚΑ",
            )
            trains.append(t_pro2232)

        if pro_chal:
            # Proastiakos 1314: Athens -> Chalkida
            t_pro1314 = SimulatedTrain(
                id="mock-pro-1314",
                train_number="1314",
                locomotive_number="460120",
                train_type="Proastiakos",
                service_type="Προαστιακός",
                origin="Αθήνα",
                destination="Χαλκίδα",
                corridor="PROASTIAKOS",
                track=pro_chal,
                cruise_speed_kmh=95.0,
                acceleration_mps2=0.60,
                deceleration_mps2=0.70,
                dwell_duration_sec=35.0,
                current_distance_m=pro_chal.total_length_m * 0.40,  # Near Avlona
                direction=1,
                delay_minutes=1,
                next_station_name="Αυλώνας",
            )
            trains.append(t_pro1314)

        if thess_vol:
            # Regional 1731: Larissa -> Volos
            t_reg1731 = SimulatedTrain(
                id="mock-reg-1731",
                train_number="1731",
                locomotive_number="621102",
                train_type="Regional",
                service_type="Επιβατικά",
                origin="Λάρισα",
                destination="Βόλος",
                corridor="THESSALY",
                track=thess_vol,
                cruise_speed_kmh=85.0,
                acceleration_mps2=0.40,
                deceleration_mps2=0.50,
                dwell_duration_sec=45.0,
                current_distance_m=thess_vol.total_length_m * 0.50,
                direction=1,
                delay_minutes=0,
                next_station_name="Βελεστίνο",
            )
            trains.append(t_reg1731)

        if thess_kal:
            # Regional 884: Paleofarsalos -> Kalambaka
            t_reg884 = SimulatedTrain(
                id="mock-reg-884",
                train_number="884",
                locomotive_number="621105",
                train_type="Regional",
                service_type="Επιβατικά",
                origin="Παλαιοφάρσαλος",
                destination="Καλαμπάκα",
                corridor="THESSALY",
                track=thess_kal,
                cruise_speed_kmh=80.0,
                acceleration_mps2=0.40,
                deceleration_mps2=0.50,
                dwell_duration_sec=45.0,
                current_distance_m=thess_kal.total_length_m * 0.35,  # Past Karditsa
                direction=1,
                delay_minutes=4,
                next_station_name="Τρίκαλα",
            )
            trains.append(t_reg884)

        if ido:
            # International 361: Thessaloniki -> Idomeni
            t_int361 = SimulatedTrain(
                id="mock-int-361",
                train_number="361",
                locomotive_number="120025",
                train_type="International",
                service_type="Επιβατικά",
                origin="Θεσσαλονίκη",
                destination="Ειδομένη",
                corridor="NORTHERN",
                track=ido,
                cruise_speed_kmh=90.0,
                acceleration_mps2=0.40,
                deceleration_mps2=0.50,
                dwell_duration_sec=45.0,
                current_distance_m=ido.total_length_m * 0.65,
                direction=1,
                delay_minutes=0,
                next_station_name="Ειδομένη",
            )
            trains.append(t_int361)

        return trains

    def step(self, dt_sec: float) -> List[TrainPosition]:
        """Step simulation by dt_sec wall-clock seconds scaled by time_scale."""
        effective_dt = dt_sec * self.time_scale
        positions = []
        for train in self.trains:
            pos = train.step(effective_dt)
            positions.append(pos)
        return positions

    def stream_positions(
        self,
        interval_sec: float = 1.0,
        stop_event: Optional[threading.Event] = None,
    ) -> Generator[List[TrainPosition], None, None]:
        """Streaming generator yielding updated TrainPosition batches.

        Drop-in replacement for RailwayGovClient.stream_train_positions().
        """
        while stop_event is None or not stop_event.is_set():
            yield self.step(interval_sec)
            if stop_event is not None:
                if stop_event.wait(timeout=interval_sec):
                    return
            else:
                time.sleep(interval_sec)

    def reset(self) -> None:
        """Reset all simulated trains to their initial schedule origins."""
        for train in self.trains:
            train.reset()

    def set_time_scale(self, scale: float) -> None:
        """Adjust simulation time scaling factor."""
        self.time_scale = max(0.1, scale)


def generate_mock_cab_stream(train_number: str) -> Any:
    """Generate synthetic forward cab stream metadata for offline simulation.

    Args:
        train_number: Commercial train number (e.g. '1597', 'IC52').

    Returns:
        CabStreamInfo populated with synthetic stream parameters.
    """
    from train_tracker import CabStreamInfo

    clean_num = str(train_number).strip().upper()
    compact = clean_num.replace(" ", "").replace("-", "")

    # Modern EMUs and high-speed trainsets equipped with forward cameras
    has_cam = any(
        k in compact for k in ("IC52", "IC54", "1597", "1599", "2232", "2236", "2238", "1314")
    ) or (compact.isdigit() and int(compact) in (1597, 1599, 2232, 2236, 1314))

    if not has_cam:
        return CabStreamInfo.unavailable(
            train_number=train_number,
            message=f"Locomotive operating train {train_number} is not equipped with an active forward video stream.",
        )

    loco_id = "120-024" if ("52" in compact or "54" in compact) else ("460-105" if "15" in compact else "220-031")
    return CabStreamInfo(
        train_number=train_number,
        has_stream=True,
        status="ONLINE",
        stream_type="hls",
        playlist_url=f"https://stream.railway.gov.gr/hls/mock-loco-{loco_id.replace('-', '')}/live.m3u8",
        locomotive_id=loco_id,
        camera_position="FORWARD_CAB",
        resolution="1080p",
        fps=30,
        bitrate_kbps=4500,
        latency_sec=1.8,
    )

