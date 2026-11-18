#!/usr/bin/env python3
"""
railmon - Hellenic Railways (railway.gov.gr) Live Train Tracker & Terminal Map.
Python Client, Real-time SSE Monitor, and Curses TUI.
"""

import argparse
import contextlib
import json
import logging
import os
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Generator, List, Optional

logger = logging.getLogger("railmon." + __name__)


@contextlib.contextmanager
def install_signal_handlers(
    stop_event: Optional[threading.Event] = None,
) -> Generator[threading.Event, None, None]:
    """Register SIGINT, SIGTERM, and POSIX SIGHUP handlers to set a shared stop_event."""
    event = stop_event or threading.Event()

    if threading.current_thread() is not threading.main_thread():
        yield event
        return

    signals_to_trap: List[int] = [signal.SIGINT, signal.SIGTERM]
    if sys.platform != "win32" and hasattr(signal, "SIGHUP"):
        signals_to_trap.append(getattr(signal, "SIGHUP"))

    previous_handlers: Dict[int, Any] = {}

    def _handle_signal(signum: int, _frame: Any) -> None:
        try:
            sig_name = signal.Signals(signum).name
        except ValueError:
            sig_name = str(signum)
        logger.info("Received signal %s (%d); initiating graceful shutdown...", sig_name, signum)
        event.set()

    for sig in signals_to_trap:
        try:
            previous_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, _handle_signal)
        except (OSError, ValueError):
            pass

    try:
        yield event
    finally:
        for sig, prev in previous_handlers.items():
            try:
                signal.signal(sig, prev)
            except (OSError, ValueError):
                pass


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
        reconnect_delay: float = 3.0,
        stop_event: Optional[threading.Event] = None,
        timeout: float = 10.0,
    ) -> Generator[List[TrainPosition], None, None]:
        """
        Stream live train positions via Server-Sent Events (SSE).
        Yields a list of TrainPosition objects whenever an update arrives.

        Args:
            event_filter: SSE event name to parse (default: 'trainPositionsUx').
            reconnect_delay: Seconds to wait before reconnecting on error. If <= 0,
                connection exceptions are raised directly to the caller (e.g. StreamWorker).
            stop_event: Optional threading.Event to signal immediate stream termination.
            timeout: Socket read timeout in seconds (default: 10.0).
        """
        stream_url = f"{self.base_url}/api/train-stream"
        headers = {**DEFAULT_HEADERS, "Accept": "text/event-stream"}

        while stop_event is None or not stop_event.is_set():
            try:
                req = urllib.request.Request(stream_url, headers=headers)
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=timeout) as resp:
                    current_event = "message"
                    data_buffer: List[str] = []

                    for raw_line in resp:
                        if stop_event is not None and stop_event.is_set():
                            return

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
                                            if p.get("lat") is not None and p.get("lng") is not None
                                        ]
                                        yield trains
                                    except json.JSONDecodeError as exc:
                                        logger.debug("Skipping malformed SSE JSON payload: %s", exc)
                            current_event = "message"
                            continue

                        if line.startswith("event:"):
                            current_event = line.split(":", 1)[1].strip()
                        elif line.startswith("data:"):
                            data_buffer.append(line.split(":", 1)[1].strip())

                    # If upstream closed the response stream cleanly without stop_event
                    if reconnect_delay <= 0:
                        return

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if stop_event is not None and stop_event.is_set():
                    return
                if reconnect_delay <= 0:
                    raise
                logger.warning(
                    "SSE connection interrupted: %s. Reconnecting in %.1fs...",
                    e,
                    reconnect_delay,
                )
                if stop_event is not None:
                    if stop_event.wait(timeout=reconnect_delay):
                        return
                else:
                    time.sleep(reconnect_delay)


def build_parser() -> argparse.ArgumentParser:
    """Construct command-line argument parser for railmon supporting stream, schedules, record, and history."""
    parser = argparse.ArgumentParser(
        prog="railmon",
        description="railmon - Hellenic Railways (railway.gov.gr) Live Train Tracker & Terminal Map.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  railmon --tui                       # Launch live interactive terminal map
  railmon --tui --record              # Launch live terminal map and record to SQLite
  railmon record                      # Run headless telemetry recording daemon
  railmon record --demo               # Headless recording of synthetic demo feed
  railmon history trips               # Inspect recorded train trips in database
  railmon history train IC50          # Show trajectory breadcrumbs for train IC50
  railmon history punctuality         # Corridor punctuality report
  railmon history stats               # SQLite database diagnostics and volume
  railmon history export IC50 -o out.json  # Export train run to GeoJSON (RFC 7946)
        """,
    )
    # Common flags on top-level
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
        "--db",
        type=str,
        default="railway_history.db",
        help="Path to SQLite historical database (default: railway_history.db).",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="Enable background telemetry recording to SQLite in stream and --tui modes.",
    )
    parser.add_argument(
        "--no-filter-deadband",
        action="store_true",
        help="Disable deadband filtering (record every raw position update without compression).",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )

    subparsers = parser.add_subparsers(dest="mode")
    parser.set_defaults(mode="stream")

    # 1. stream (default)
    subparsers.add_parser(
        "stream",
        help="Stream live train positions to console in formatted tabular text (default).",
    )

    # 2. schedules
    subparsers.add_parser(
        "schedules",
        help="Fetch and display today's upcoming train timetables.",
    )

    # Shared parser for subparsers accepting --db
    db_parent = argparse.ArgumentParser(add_help=False)
    db_parent.add_argument("--db", default=argparse.SUPPRESS, help="SQLite database file path.")

    # 3. record
    p_rec = subparsers.add_parser(
        "record",
        parents=[db_parent],
        help="Run headless background telemetry recording daemon.",
    )
    p_rec.add_argument("--demo", action="store_true", default=argparse.SUPPRESS, help="Record synthetic demo telemetry.")
    p_rec.add_argument("--no-filter-deadband", action="store_true", default=argparse.SUPPRESS, help="Disable deadband filtering.")
    p_rec.add_argument("--batch-size", type=int, default=50, help="Maximum buffered rows before commit (default: 50).")
    p_rec.add_argument("--flush-interval", type=float, default=2.0, help="Maximum buffer age in seconds before flush (default: 2.0).")
    p_rec.add_argument("--quiet", action="store_true", help="Suppress periodic terminal status updates.")

    # 4. history
    p_hist = subparsers.add_parser(
        "history",
        parents=[db_parent],
        help="Query, inspect, and export historical telemetry from SQLite database.",
    )
    hist_subs = p_hist.add_subparsers(dest="history_action")

    # history trips
    p_trips = hist_subs.add_parser("trips", parents=[db_parent], help="List recorded train trips.")
    p_trips.add_argument("--date", type=str, help="Filter by date (YYYY-MM-DD).")
    p_trips.add_argument("--corridor", type=str, help="Filter by corridor name.")
    p_trips.add_argument("--limit", type=int, default=50, help="Max trips to return (default: 50).")

    # history train
    p_train = hist_subs.add_parser("train", parents=[db_parent], help="Inspect telemetry breadcrumbs for a train.")
    p_train.add_argument("train_number", type=str, help="Train number (e.g. 1597, IC50).")
    p_train.add_argument("--start", type=str, help="Start ISO timestamp filter.")
    p_train.add_argument("--end", type=str, help="End ISO timestamp filter.")
    p_train.add_argument("--limit", type=int, default=50, help="Max rows to return (default: 50).")
    p_train.add_argument("--desc", action="store_true", help="Order descending (newest first).")

    # history punctuality
    p_punc = hist_subs.add_parser("punctuality", parents=[db_parent], help="Corridor punctuality and delay compliance report.")
    p_punc.add_argument("--corridor", type=str, help="Filter by corridor name.")
    p_punc.add_argument("--start-date", type=str, help="Start date (YYYY-MM-DD).")
    p_punc.add_argument("--end-date", type=str, help="End date (YYYY-MM-DD).")
    p_punc.add_argument("--threshold", type=int, default=5, help="On-time threshold in minutes (default: 5).")

    # history dwell
    p_dwell = hist_subs.add_parser("dwell", parents=[db_parent], help="Station stop and dwell duration analysis.")
    p_dwell.add_argument("train_number", type=str, help="Train number.")
    p_dwell.add_argument("--start", type=str, help="Start timestamp filter.")
    p_dwell.add_argument("--end", type=str, help="End timestamp filter.")
    p_dwell.add_argument("--speed-threshold", type=float, default=2.0, help="Speed threshold in km/h (default: 2.0).")
    p_dwell.add_argument("--min-dwell", type=int, default=30, help="Minimum dwell seconds to detect stop (default: 30).")

    # history stats
    hist_subs.add_parser("stats", parents=[db_parent], help="Database storage diagnostics, total rows, and span.")

    # history sessions
    p_sess = hist_subs.add_parser("sessions", parents=[db_parent], help="Audit log of recording sessions.")
    p_sess.add_argument("--limit", type=int, default=20, help="Max sessions to list (default: 20).")

    # history export
    p_exp = hist_subs.add_parser("export", parents=[db_parent], help="Export train trajectory coordinates to GeoJSON or CSV.")
    p_exp.add_argument("train_number", type=str, help="Train identifier (e.g. 1597).")
    p_exp.add_argument("--format", choices=["geojson", "csv"], default="geojson", help="Export format (default: geojson).")
    p_exp.add_argument("--feature-type", choices=["LineString", "Point"], default="LineString", help="GeoJSON geometry type.")
    p_exp.add_argument("--out", "-o", type=str, help="Destination output file path.")
    p_exp.add_argument("--start", type=str, help="Start timestamp filter.")
    p_exp.add_argument("--end", type=str, help="End timestamp filter.")

    return parser


def run_tui_mode(
    demo: bool = False,
    fps: float = 20.0,
    reconnect_delay: float = 3.0,
    record: bool = False,
    db_path: str = "railway_history.db",
    enable_deadband: bool = True,
) -> None:
    """Launch the interactive curses terminal map application.

    Args:
        demo: If True, run offline mock simulation; otherwise connect to live SSE stream.
        fps: Target frames per second for curses refresh loop.
        reconnect_delay: Initial reconnect delay in seconds for live stream interruptions.
        record: If True, persist incoming telemetry to SQLite in background.
        db_path: Destination SQLite database file path when recording is active.
        enable_deadband: If True, apply deadband filter to eliminate stationary coordinate noise.
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

    db = None
    recorder = None
    if record:
        from storage.db import Database
        from storage.recorder import RecorderConfig, TelemetryRecorder

        db = Database(db_path)
        db.init_schema()
        recorder = TelemetryRecorder(
            db,
            RecorderConfig(
                mode="demo" if demo else "live",
                enable_deadband=enable_deadband,
            ),
        )
        recorder.start()

    # 1. Initialize thread-safe state store
    store = TrainStateStore()
    if record:
        store.set_recording_status(True, db_path)

    # 2. Load static rail infrastructure
    stations = list(load_stations().values())
    tracks = list(load_tracks().values())

    # 3. Configure ingestion worker
    if demo:
        engine = MockTelemetryEngine()
        worker = StreamWorker(store=store, mock_engine=engine, recorder=recorder)
    else:
        client = RailwayGovClient()
        worker = StreamWorker(
            store=store,
            client=client,
            base_backoff_sec=reconnect_delay,
            recorder=recorder,
        )

    # 4. Start background ingestion thread
    worker.start()

    # 5. Execute curses event loop with terminal protection
    with install_signal_handlers() as stop_event:
        try:
            run_tui(
                store=store,
                worker=worker,
                stations=stations,
                tracks=tracks,
                target_fps=fps,
                stop_event=stop_event,
            )
        finally:
            stop_event.set()
            worker.stop()
            if recorder:
                recorder.stop()
            if db:
                db.close()


def run_plain_stream_mode(
    demo: bool = False,
    reconnect_delay: float = 3.0,
    record: bool = False,
    db_path: str = "railway_history.db",
    enable_deadband: bool = True,
) -> None:
    """Stream train positions to stdout in formatted tabular text.

    Args:
        demo: If True, stream from MockTelemetryEngine; otherwise stream from live SSE feed.
        reconnect_delay: Reconnection interval for live SSE feed.
        record: If True, persist incoming telemetry to SQLite in background.
        db_path: Destination SQLite database file path when recording is active.
        enable_deadband: If True, apply deadband filter to eliminate stationary coordinate noise.
    """
    db = None
    recorder = None
    if record:
        from storage.db import Database
        from storage.recorder import RecorderConfig, TelemetryRecorder

        db = Database(db_path)
        db.init_schema()
        recorder = TelemetryRecorder(
            db,
            RecorderConfig(
                mode="demo" if demo else "live",
                enable_deadband=enable_deadband,
            ),
        )
        recorder.start()

    with install_signal_handlers() as stop_event:
        if demo:
            from tui.mock_engine import MockTelemetryEngine

            engine = MockTelemetryEngine()
            stream = engine.stream_positions(interval_sec=1.0, stop_event=stop_event)
            source_label = "Offline Mock Simulation"
        else:
            client = RailwayGovClient()
            stream = client.stream_train_positions(
                reconnect_delay=reconnect_delay,
                stop_event=stop_event,
            )
            source_label = "Live SSE Stream (/api/train-stream)"

        rec_label = f" | SQLite Recording: {db_path}" if record else ""
        print("=" * 70)
        print("  railway.gov.gr Live Train Tracker - CLI Monitor")
        print(f"  Source: {source_label}{rec_label}")
        print("=" * 70)
        print("Press Ctrl+C to stop.\n", flush=True)

        try:
            for batch in stream:
                if stop_event.is_set():
                    break
                if recorder:
                    recorder.enqueue_batch(batch)

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
                if stop_event.wait(timeout=1.0):
                    break
            print("\nMonitoring stopped.")
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")
        finally:
            if recorder:
                recorder.stop()
            if db:
                db.close()


def run_record_mode(
    db_path: str = "railway_history.db",
    demo: bool = False,
    enable_deadband: bool = True,
    batch_size: int = 50,
    flush_interval_sec: float = 2.0,
    quiet: bool = False,
    reconnect_delay: float = 3.0,
) -> int:
    """Execute dedicated headless recording daemon with terminal metrics display.

    Args:
        db_path: Destination SQLite database file path.
        demo: If True, stream from MockTelemetryEngine; otherwise stream from live SSE feed.
        enable_deadband: If True, apply coordinate deadband filtering.
        batch_size: Maximum buffer size before batch commit.
        flush_interval_sec: Maximum latency in seconds before buffer commit.
        quiet: If True, suppress periodic status row logs.
        reconnect_delay: Reconnection interval for live SSE feed.

    Returns:
        Exit code (0 on success).
    """
    from storage.cli_formatters import format_file_size
    from storage.db import Database
    from storage.recorder import RecorderConfig, TelemetryRecorder

    db = Database(db_path)
    db.init_schema()

    config = RecorderConfig(
        batch_size=batch_size,
        flush_interval_sec=flush_interval_sec,
        enable_deadband=enable_deadband,
        mode="demo" if demo else "live",
    )
    recorder = TelemetryRecorder(db, config)
    recorder.start()

    source_label = "Offline Mock Simulation" if demo else "Live SSE Stream (/api/train-stream)"
    deadband_label = "ENABLED (distance >= 15m, delay delta >= 1m)" if enable_deadband else "DISABLED (recording all raw coordinates)"

    print("=" * 70)
    print("  railmon - Hellenic Railways Telemetry Ingestion Daemon")
    print(f"  Source: {source_label}")
    print(f"  Database: {db_path} (WAL mode)")
    print(f"  Deadband Compression: {deadband_label}")
    print("=" * 70)
    now_str = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{now_str}] Session #{recorder.session_id} initialized. Recording started. Press Ctrl+C to stop.\n")

    if not quiet:
        print(f"  {'Timestamp':<11} | {'Trains':>6} | {'Batch Rows':>10} | {'Total Stored':>12} | {'Compressed':>10} | {'DB Size':>9}")
        print("-" * 75)

    start_time = time.time()
    last_stat_time = time.time()

    with install_signal_handlers() as stop_event:
        try:
            if demo:
                from tui.mock_engine import MockTelemetryEngine

                engine = MockTelemetryEngine()
                stream = engine.stream_positions(interval_sec=1.0, stop_event=stop_event)
            else:
                client = RailwayGovClient()
                stream = client.stream_train_positions(
                    reconnect_delay=reconnect_delay,
                    stop_event=stop_event,
                )

            for batch in stream:
                if stop_event.is_set():
                    break
                recorder.enqueue_batch(batch)

                now = time.time()
                if not quiet and (now - last_stat_time) >= 5.0:
                    stats = recorder.get_stats()
                    filter_stats = stats.get("filter") or {}
                    ratio = filter_stats.get("suppression_ratio_pct", 0.0)
                    try:
                        db_size = os.path.getsize(db_path)
                    except OSError:
                        db_size = 0
                    ts_str = time.strftime("%H:%M:%S")
                    print(
                        f" {ts_str:<11} | {len(batch):>6} | {len(batch):>10} | "
                        f"{stats.get('total_records_written', 0):>12} | {ratio:>9.1f}% | {format_file_size(db_size):>9}"
                    )
                    sys.stdout.flush()
                    last_stat_time = now

        except KeyboardInterrupt:
            pass
        finally:
            print(f"\n[{time.strftime('%Y-%m-%d %H:%M:%S')}] Shutting down recorder... draining queue and flushing buffer...")
            recorder.stop(timeout_sec=5.0)
            db.close()
            duration = time.time() - start_time
            stats = recorder.get_stats()
            filter_stats = stats.get("filter") or {}
            suppressed = filter_stats.get("suppressed_count", 0)
            ratio = filter_stats.get("suppression_ratio_pct", 0.0)
            try:
                db_size = os.path.getsize(db_path)
            except OSError:
                db_size = 0

            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Session #{recorder.session_id} finalized.")
            print(f"  - Duration: {duration:.1f}s")
            print(f"  - Total Telemetry Rows Stored: {stats.get('total_records_written', 0):,d}")
            if enable_deadband:
                print(f"  - Positions Filtered (Deadband): {suppressed:,d} ({ratio:.1f}% compression ratio)")
            print(f"  - Final Database File Size: {format_file_size(db_size)}")

    return 0


def run_history_mode(args: argparse.Namespace) -> int:
    """Query, inspect, and export historical telemetry from SQLite.

    Args:
        args: Parsed command-line arguments for 'history' subcommand.

    Returns:
        Exit code (0 on success).
    """
    from storage.cli_formatters import (
        format_dwell_table,
        format_punctuality_table,
        format_sessions_table,
        format_stats_table,
        format_train_history_table,
        format_trips_table,
    )
    from storage.db import Database
    from storage.repository import HistoricalRepository

    db_path = getattr(args, "db", "railway_history.db")
    if not os.path.exists(db_path):
        print(
            f"[Error] SQLite database file '{db_path}' not found.\n"
            f"Record telemetry first with:\n"
            f"    railmon record\n"
            f"    railmon --record\n"
            f"    railmon --tui --record",
            file=sys.stderr,
        )
        return 1

    action = getattr(args, "history_action", None)
    if not action:
        print(
            "Please specify a history action: trips, train, punctuality, dwell, stats, sessions, export.\n"
            "Use 'railmon history --help' for details."
        )
        return 0

    db = Database(db_path)
    repo = HistoricalRepository(db)

    if action == "trips":
        trips = repo.get_active_trips(date=args.date, corridor=args.corridor, limit=args.limit)
        print(format_trips_table(trips, date=args.date, corridor=args.corridor, db_path=db_path))

    elif action == "train":
        order = "DESC" if getattr(args, "desc", False) else "ASC"
        rows = repo.get_train_history(
            train_number=args.train_number,
            start_time=args.start,
            end_time=args.end,
            limit=args.limit,
            order=order,
        )
        print(format_train_history_table(rows, train_number=args.train_number))

    elif action == "punctuality":
        summaries = repo.get_corridor_punctuality(
            corridor=args.corridor,
            start_date=args.start_date,
            end_date=args.end_date,
            on_time_threshold_min=args.threshold,
        )
        print(format_punctuality_table(summaries, threshold_min=args.threshold))

    elif action == "dwell":
        dwells = repo.get_station_dwell_times(
            train_number=args.train_number,
            start_time=args.start,
            end_time=args.end,
            speed_threshold_kmh=args.speed_threshold,
            min_dwell_sec=args.min_dwell,
        )
        print(format_dwell_table(dwells, train_number=args.train_number))

    elif action == "stats":
        stats = repo.get_database_stats()
        print(format_stats_table(stats, db_path=db_path))

    elif action == "sessions":
        sessions = repo.get_recording_sessions(limit=args.limit)
        print(format_sessions_table(sessions))

    elif action == "export":
        fmt = getattr(args, "format", "geojson").lower()
        if fmt == "geojson":
            data = repo.export_trip_geojson(
                train_number=args.train_number,
                start_time=args.start,
                end_time=args.end,
                feature_type=args.feature_type,
            )
            out_str = json.dumps(data, indent=2, ensure_ascii=False)
            if args.out:
                with open(args.out, "w", encoding="utf-8") as f:
                    f.write(out_str)
                count = len(data.get("features", []))
                print(f"Exported GeoJSON ({count} features) to {args.out}")
            else:
                print(out_str)
        elif fmt == "csv":
            if args.out:
                count = repo.export_csv(
                    query_type="telemetry",
                    target=args.out,
                    params={
                        "train_number": args.train_number,
                        "start_time": args.start,
                        "end_time": args.end,
                    },
                )
                print(f"Exported {count} CSV records to {args.out}")
            else:
                repo.export_csv(
                    query_type="telemetry",
                    target=sys.stdout,
                    params={
                        "train_number": args.train_number,
                        "start_time": args.start,
                        "end_time": args.end,
                    },
                )
    return 0


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
        elif args.mode == "record":
            enable_deadband = not getattr(args, "no_filter_deadband", False)
            return run_record_mode(
                db_path=getattr(args, "db", "railway_history.db"),
                demo=getattr(args, "demo", False),
                enable_deadband=enable_deadband,
                batch_size=getattr(args, "batch_size", 50),
                flush_interval_sec=getattr(args, "flush_interval", 2.0),
                quiet=getattr(args, "quiet", False),
                reconnect_delay=getattr(args, "reconnect_delay", 3.0),
            )
        elif args.mode == "history":
            return run_history_mode(args)
        elif args.tui:
            is_rec = getattr(args, "record", False)
            if is_rec:
                enable_deadband = not getattr(args, "no_filter_deadband", False)
                run_tui_mode(
                    demo=args.demo,
                    fps=args.fps,
                    reconnect_delay=args.reconnect_delay,
                    record=True,
                    db_path=getattr(args, "db", "railway_history.db"),
                    enable_deadband=enable_deadband,
                )
            else:
                run_tui_mode(
                    demo=args.demo,
                    fps=args.fps,
                    reconnect_delay=args.reconnect_delay,
                )
        else:
            is_rec = getattr(args, "record", False)
            if is_rec:
                enable_deadband = not getattr(args, "no_filter_deadband", False)
                run_plain_stream_mode(
                    demo=args.demo,
                    reconnect_delay=args.reconnect_delay,
                    record=True,
                    db_path=getattr(args, "db", "railway_history.db"),
                    enable_deadband=enable_deadband,
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
