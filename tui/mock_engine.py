"""Offline Mock Telemetry Simulator & Railway Network Topology Engine.

Provides topology loaders for Greek railway stations and tracks, geodesic
kinematics models (Haversine distance, forward bearing, polyline interpolation),
and a multi-train simulation engine generating continuous TrainPosition telemetry.
"""

import bisect
import json
import math
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Dict, Generator, List, Optional, Tuple

from train_tracker import TrainPosition
from tui.geo import GeoPoint

# Earth radius in meters (WGS84 mean radius)
EARTH_RADIUS_M = 6371000.0

# Default data file paths
DATA_DIR = Path(__file__).parent / "data"
DEFAULT_STATIONS_FILE = DATA_DIR / "stations.json"
DEFAULT_TRACKS_FILE = DATA_DIR / "tracks.json"


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

    def __post_init__(self):
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
    ):
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
