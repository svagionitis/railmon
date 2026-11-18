"""
Railway.gov.gr Live Train Tracker - Python Client and CLI
Connects to the official Hellenic Railways (OSE) train tracker platform.
"""

import json
import time
import urllib.request
import urllib.error
import ssl
from dataclasses import dataclass
from typing import Generator, Dict, Any, List, Optional


BASE_URL = "https://railway.gov.gr"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://railway.gov.gr/train-tracker",
}


@dataclass
class TrainPosition:
    id: str
    train_number: str
    locomotive_number: Optional[str]
    name: Optional[str]
    train_type: Optional[str]
    service_type: Optional[str]
    status: str
    origin: Optional[str]
    destination: Optional[str]
    next_station: Optional[str]
    speed: float  # km/h
    course: float  # degrees
    track_heading: float
    lat: float
    lng: float
    altitude: Optional[float]
    delay: int  # minutes
    progress: Optional[float]  # 0.0 to 1.0
    chainage_m: Optional[int]
    corridor: Optional[str]
    distance_to_destination_m: Optional[int]
    timestamp: Optional[str]
    schedule_id: Optional[str]

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TrainPosition":
        speed = float(data.get("speed") or 0.0)
        # Determine human-readable status matching frontend logic
        status = "Κανονική" if speed > 5.0 else "Στάση"
        
        return cls(
            id=str(data.get("id") or data.get("_id") or ""),
            train_number=str(data.get("trainNumber") or data.get("name") or "Unknown"),
            locomotive_number=data.get("locomotiveNumber"),
            name=data.get("name"),
            train_type=data.get("trainType"),
            service_type=data.get("serviceType"),
            status=status,
            origin=data.get("origin"),
            destination=data.get("destination"),
            next_station=data.get("nextStation"),
            speed=speed,
            course=float(data.get("course") or data.get("bearing") or 0.0),
            track_heading=float(data.get("trackHeading") or 0.0),
            lat=float(data.get("lat") or 0.0),
            lng=float(data.get("lng") or 0.0),
            altitude=data.get("altitude"),
            delay=int(data.get("delay") or 0),
            progress=data.get("progress"),
            chainage_m=data.get("chainage_m"),
            corridor=data.get("corridor"),
            distance_to_destination_m=data.get("distanceToDestination_m"),
            timestamp=data.get("timestamp"),
            schedule_id=data.get("scheduleId")
        )


class RailwayGovClient:
    """Client for the Hellenic Railways train-tracker APIs."""

    def __init__(self, base_url: str = BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.ssl_ctx = ssl.create_default_context()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"

        req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=15) as resp:
            content = resp.read().decode("utf-8")
            return json.loads(content)

    def get_upcoming_schedules(self) -> List[Dict[str, Any]]:
        """Fetch today's upcoming scheduled train trips."""
        res = self._get("/api/public/schedules/upcoming")
        return res.get("schedules", [])

    def get_schedule_by_id(self, schedule_id: str) -> Dict[str, Any]:
        """Fetch full itinerary for a schedule."""
        return self._get(f"/api/public/schedules/{schedule_id}")

    def get_schedule_by_train_number(self, train_number: str) -> Dict[str, Any]:
        """Fetch schedule by commercial train number (e.g. '1597')."""
        return self._get(f"/api/public/train-schedules/by-train-number/{train_number}")

    def get_station(self, station_id: str) -> Dict[str, Any]:
        """Fetch station metadata."""
        return self._get(f"/api/public/stations/{station_id}")

    def get_station_departures(self, station_id: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetch departure board for a station."""
        res = self._get(f"/api/public/stations/{station_id}/departures", {"limit": limit})
        return res.get("departures", [])

    def get_station_arrivals(self, station_id: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetch arrival board for a station."""
        res = self._get(f"/api/public/stations/{station_id}/arrivals", {"limit": limit})
        return res.get("arrivals", [])

    def get_rail_infrastructure(self, types: str = "station,track", limit: int = 1000) -> Dict[str, Any]:
        """Fetch GeoJSON rail network features (stations, tracks, signals)."""
        return self._get("/api/msd/geo", {"mode": "public", "types": types, "limit": limit})

    def stream_train_positions(
        self,
        event_filter: str = "trainPositionsUx",
        reconnect_delay: float = 3.0
    ) -> Generator[List[TrainPosition], None, None]:
        """
        Stream live train positions via Server-Sent Events (SSE).
        Yields a list of TrainPosition objects whenever an update arrives.
        """
        stream_url = f"{self.base_url}/api/train-stream"
        headers = {**DEFAULT_HEADERS, "Accept": "text/event-stream"}

        while True:
            try:
                req = urllib.request.Request(stream_url, headers=headers)
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=30) as resp:
                    current_event = "message"
                    data_buffer = []

                    for raw_line in resp:
                        line = raw_line.decode("utf-8", errors="ignore").strip()

                        if not line:
                            # End of event block
                            if data_buffer:
                                payload_str = "".join(data_buffer)
                                data_buffer = []

                                if current_event == event_filter:
                                    try:
                                        payload = json.loads(payload_str)
                                        raw_positions = payload.get("positions", [])
                                        trains = [
                                            TrainPosition.from_dict(p)
                                            for p in raw_positions
                                            if p.get("lat") and p.get("lng")
                                        ]
                                        yield trains
                                    except json.JSONDecodeError:
                                        pass
                            current_event = "message"
                            continue

                        if line.startswith("event:"):
                            current_event = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            data_buffer.append(line.split(":", 1)[1].strip())

            except (urllib.error.URLError, TimeoutError) as e:
                print(f"[Stream Warning] Connection interrupted: {e}. Reconnecting in {reconnect_delay}s...")
                time.sleep(reconnect_delay)


def demo_cli():
    import sys
    client = RailwayGovClient()

    print("=" * 70)
    print("  railway.gov.gr Live Train Tracker - CLI Monitor")
    print("=" * 70)

    mode = sys.argv[1] if len(sys.argv) > 1 else "stream"

    if mode == "schedules":
        print("Fetching today's upcoming train schedules...\n")
        schedules = client.get_upcoming_schedules()
        print(f"Total upcoming schedules: {len(schedules)}")
        for s in schedules[:15]:
            orig = s.get("origin")
            dest = s.get("destination")
            orig_name = orig.get("nameGreek") if isinstance(orig, dict) else orig or "N/A"
            dest_name = dest.get("nameGreek") if isinstance(dest, dict) else dest or "N/A"
            print(f"• Train {str(s.get('trainNumber')):>5} | {orig_name} -> {dest_name} | Dep: {s.get('scheduledDeparture', 'N/A')}")
        return

    print("Connecting to live Server-Sent Events (SSE) feed: /api/train-stream ...")
    print("Press Ctrl+C to stop.\n")

    try:
        for batch in client.stream_train_positions():
            print(f"\n[{time.strftime('%H:%M:%S')}] Live Trains on Network: {len(batch)}")
            print(f"{'Train':<8} {'Type':<12} {'Route':<32} {'Speed':<10} {'Delay':<8} {'Position (Lat, Lng)':<22}")
            print("-" * 95)
            
            for t in batch:
                route = f"{t.origin or '—'} -> {t.destination or '—'}" if (t.origin or t.destination) else (t.corridor or "Shunting/Yard")
                print(
                    f"{t.train_number:<8} "
                    f"{(t.service_type or t.train_type or 'N/A')[:11]:<12} "
                    f"{route[:30]:<32} "
                    f"{t.speed:>5.1f} km/h "
                    f"{f'+{t.delay}m' if t.delay else 'On-time':<8} "
                    f"({t.lat:.4f}, {t.lng:.4f})"
                )
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


if __name__ == "__main__":
    demo_cli()
