#!/usr/bin/env python3
"""
railmon - Hellenic Railways (railway.gov.gr) Live Train Tracker & Terminal Map.
Python Client, Real-time SSE Monitor, and Curses TUI.
"""

import argparse
import json
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Optional


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


def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser for railmon."""
    parser = argparse.ArgumentParser(
        prog="railmon",
        description="railmon - Hellenic Railways (railway.gov.gr) Live Train Tracker & Terminal Map.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  railmon --tui          # Launch live interactive terminal map
  railmon --tui --demo   # Launch offline interactive demo simulator
  railmon                # Plain console live stream monitor
  railmon --demo         # Plain console offline mock stream
  railmon schedules      # List today's upcoming scheduled train trips
        """,
    )
    parser.add_argument(
        "mode",
        nargs="?",
        default="stream",
        choices=["stream", "schedules"],
        help="Operational mode: 'stream' for live position monitoring, 'schedules' for upcoming trips (default: stream).",
    )
    parser.add_argument(
        "--tui",
        action="store_true",
        help="Launch full-screen interactive curses terminal map dashboard.",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run offline using synthetic telemetry simulator (no live network connection required).",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=20.0,
        help="Target frame rate for TUI rendering loop (default: 20.0, range: 5.0-60.0).",
    )
    parser.add_argument(
        "--reconnect-delay",
        type=float,
        default=3.0,
        help="Reconnection backoff interval in seconds for live SSE stream (default: 3.0).",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )
    return parser


def run_tui_mode(
    demo: bool = False, fps: float = 20.0, reconnect_delay: float = 3.0
) -> None:
    """Launch the interactive curses terminal map application.

    Args:
        demo: If True, run offline mock simulation; otherwise connect to live SSE stream.
        fps: Target frames per second for curses refresh loop.
        reconnect_delay: Initial reconnect delay in seconds for live stream interruptions.
    """
    try:
        from tui.app import run_tui
        from tui.mock_engine import MockTelemetryEngine, load_stations, load_tracks
        from tui.state import TrainStateStore
        from tui.stream_worker import StreamWorker
    except (ImportError, ModuleNotFoundError) as err:
        if sys.platform == "win32" and "curses" in str(err).lower():
            print(
                "\n[ERROR] The interactive terminal dashboard (--tui) requires 'windows-curses' on Windows.\n"
                "Please install it with:\n"
                "    pip install windows-curses\n",
                file=sys.stderr,
            )
            return
        raise

    # 1. Initialize thread-safe state store
    store = TrainStateStore()

    # 2. Load static rail infrastructure
    stations = list(load_stations().values())
    tracks = list(load_tracks().values())

    # 3. Configure ingestion worker
    if demo:
        engine = MockTelemetryEngine()
        worker = StreamWorker(store=store, mock_engine=engine)
    else:
        client = RailwayGovClient()
        worker = StreamWorker(
            store=store,
            client=client,
            base_backoff_sec=reconnect_delay,
        )

    # 4. Start background ingestion thread
    worker.start()

    # 5. Execute curses event loop with terminal protection
    try:
        run_tui(
            store=store,
            worker=worker,
            stations=stations,
            tracks=tracks,
            target_fps=fps,
        )
    finally:
        worker.stop()


def run_plain_stream_mode(demo: bool = False, reconnect_delay: float = 3.0) -> None:
    """Stream train positions to stdout in formatted tabular text.

    Args:
        demo: If True, stream from MockTelemetryEngine; otherwise stream from live SSE feed.
        reconnect_delay: Reconnection interval for live SSE feed.
    """
    if demo:
        from tui.mock_engine import MockTelemetryEngine

        engine = MockTelemetryEngine()
        stream = engine.stream_positions(interval_sec=1.0)
        source_label = "Offline Mock Simulation"
    else:
        client = RailwayGovClient()
        stream = client.stream_train_positions(reconnect_delay=reconnect_delay)
        source_label = "Live SSE Stream (/api/train-stream)"

    print("=" * 70)
    print("  railway.gov.gr Live Train Tracker - CLI Monitor")
    print(f"  Source: {source_label}")
    print("=" * 70)
    print("Press Ctrl+C to stop.\n", flush=True)

    try:
        for batch in stream:
            print(f"\n[{time.strftime('%H:%M:%S')}] Live Trains on Network: {len(batch)}")
            print(
                f"{'Train':<8} {'Type':<12} {'Route':<32} {'Speed':<10} {'Delay':<8} {'Position (Lat, Lng)':<22}"
            )
            print("-" * 95)

            for t in batch:
                route = (
                    f"{t.origin or '—'} -> {t.destination or '—'}"
                    if (t.origin or t.destination)
                    else (t.corridor or "Shunting/Yard")
                )
                print(
                    f"{t.train_number:<8} "
                    f"{(t.service_type or t.train_type or 'N/A')[:11]:<12} "
                    f"{route[:30]:<32} "
                    f"{t.speed:>5.1f} km/h "
                    f"{f'+{t.delay}m' if t.delay else 'On-time':<8} "
                    f"({t.lat:.4f}, {t.lng:.4f})"
                )
            sys.stdout.flush()
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")


def run_schedules_mode() -> None:
    """Fetch and display today's upcoming train schedules."""
    client = RailwayGovClient()
    print("Fetching today's upcoming train schedules...\n")
    schedules = client.get_upcoming_schedules()
    print(f"Total upcoming schedules: {len(schedules)}")
    for s in schedules[:15]:
        orig = s.get("origin")
        dest = s.get("destination")
        orig_name = orig.get("nameGreek") if isinstance(orig, dict) else orig or "N/A"
        dest_name = dest.get("nameGreek") if isinstance(dest, dict) else dest or "N/A"
        print(
            f"• Train {str(s.get('trainNumber')):>5} | {orig_name} -> {dest_name} | Dep: {s.get('scheduledDeparture', 'N/A')}"
        )


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entry point.

    Args:
        argv: Optional list of command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for clean execution).
    """
    if sys.platform == "win32":
        # Ensure Greek characters and unicode box/Braille symbols output cleanly on Windows console
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleCP(65001)
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.mode == "schedules":
            run_schedules_mode()
        elif args.tui:
            run_tui_mode(
                demo=args.demo,
                fps=args.fps,
                reconnect_delay=args.reconnect_delay,
            )
        else:
            run_plain_stream_mode(
                demo=args.demo,
                reconnect_delay=args.reconnect_delay,
            )
        return 0
    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
        return 0


def demo_cli() -> None:
    """Legacy entry point preserving backwards compatibility."""
    main()


if __name__ == "__main__":
    sys.exit(main())
