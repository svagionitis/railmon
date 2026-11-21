#!/usr/bin/env python3
"""
railmon - Hellenic Railways (railway.gov.gr) Live Train Tracker & Terminal Map.
Python Client, Real-time SSE Monitor, and Curses TUI.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import email.utils
from enum import Enum
import json
import logging
import logging.handlers
import math
import os
import random
import signal
import ssl
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Generator, List, Optional, Tuple

__version__ = "1.0.0"
__author__ = "Hellenic Train Tracking Contributors"


class TelemetrySource(Enum):
    """Source stream for train positions."""

    LIVE = "live"
    DEMO = "demo"
    REPLAY = "replay"


class RecordingMode(Enum):
    """SQLite telemetry persistence mode."""

    DISABLED = "disabled"
    ENABLED = "enabled"


class DeadbandFilterMode(Enum):
    """Deadband filtering mode for stationary coordinate noise suppression."""

    ENABLED = "enabled"
    DISABLED = "disabled"


class OutputVerbosity(Enum):
    """Console output verbosity level."""

    NORMAL = "normal"
    QUIET = "quiet"


class GlyphMode(Enum):
    """Terminal character set rendering mode."""

    AUTO = "auto"
    UTF8 = "utf8"
    ASCII = "ascii"

logging.getLogger("railmon").addHandler(logging.NullHandler())
logger = logging.getLogger("railmon.train_tracker")

LOG_FORMAT = "%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s"
MAX_LOG_BYTES = 10_485_760  # 10 MB
BACKUP_COUNT = 3


def resolve_default_log_path() -> str:
    """Determine platform-standard persistent log file destination.

    Returns:
        str: Absolute canonical file path to railmon.log.
    """
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if not local_app_data or not local_app_data.strip():
            local_app_data = os.path.expanduser(r"~\AppData\Local")
        return os.path.join(local_app_data.strip(), "railmon", "logs", "railmon.log")

    xdg_state = os.environ.get("XDG_STATE_HOME")
    if xdg_state and xdg_state.strip():
        base_dir = os.path.abspath(xdg_state.strip())
    else:
        base_dir = os.path.expanduser("~/.local/state")
    return os.path.join(base_dir, "railmon", "railmon.log")


def _ensure_secure_log_path(log_path: str) -> bool:
    """Create parent directories and enforce 0700/0600 POSIX permissions.

    Returns:
        bool: True if target directory exists or was created safely; False on OSError.
    """
    try:
        log_dir = os.path.dirname(os.path.abspath(log_path))
        if not os.path.exists(log_dir):
            old_umask = os.umask(0o077)
            try:
                os.makedirs(log_dir, mode=0o700, exist_ok=True)
            finally:
                os.umask(old_umask)

        if sys.platform != "win32" and os.path.exists(log_path):
            try:
                os.chmod(log_path, 0o600)
            except OSError:
                pass
        return True
    except OSError:
        return False


def setup_logging(
    log_file: Optional[str] = None,
    log_level: str = "INFO",
    is_tui: bool = False,
    enable_console: bool = False,
) -> logging.Logger:
    """Configure structured logging hierarchy for railmon.

    Args:
        log_file: Optional path override. If None, resolves to platform default.
        log_level: Desired log level string (DEBUG, INFO, WARNING, ERROR, CRITICAL).
        is_tui: True if interactive curses mode is active (suppresses console output).
        enable_console: True if diagnostic console stream should be enabled.

    Returns:
        logging.Logger: The configured 'railmon' root logger.
    """
    railmon_logger = logging.getLogger("railmon")
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)
    railmon_logger.setLevel(numeric_level)
    railmon_logger.propagate = False

    # Remove existing railmon handlers to ensure idempotency
    for h in list(railmon_logger.handlers):
        railmon_logger.removeHandler(h)
        h.close()

    target_path = os.path.abspath(log_file) if log_file else resolve_default_log_path()
    try:
        if _ensure_secure_log_path(target_path):
            formatter = logging.Formatter(LOG_FORMAT)
            file_handler = logging.handlers.RotatingFileHandler(
                target_path,
                maxBytes=MAX_LOG_BYTES,
                backupCount=BACKUP_COUNT,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            file_handler.setLevel(numeric_level)
            railmon_logger.addHandler(file_handler)

            # POSIX permission hardening on newly opened file
            if sys.platform != "win32" and os.path.exists(target_path):
                try:
                    os.chmod(target_path, 0o600)
                except OSError:
                    pass
    except OSError:
        pass

    # Ensure at least a NullHandler is attached if file logging could not be established
    if not railmon_logger.handlers:
        railmon_logger.addHandler(logging.NullHandler())

    # Console logging: strictly forbidden when TUI is active
    if enable_console and not is_tui:
        console_handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter(LOG_FORMAT)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(numeric_level)
        railmon_logger.addHandler(console_handler)

    return railmon_logger


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
ALLOWED_BASE_SCHEMES: FrozenSet[str] = frozenset({"https"})
ALLOWED_BASE_HOSTS: FrozenSet[str] = frozenset({"railway.gov.gr"})
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://railway.gov.gr/train-tracker",
}


def validate_base_url(url: str) -> str:
    """Validate and sanitize base URL against SSRF and insecure transport rules.

    Enforces:
    - Non-empty string
    - Scheme must be HTTPS (no unencrypted plaintext HTTP or local protocols)
    - Hostname must be in ALLOWED_BASE_HOSTS (strictly 'railway.gov.gr')
    - Port must be omitted or 443

    Raises:
        ValueError: If base_url violates security policies.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("base_url must be a non-empty string")

    parsed = urllib.parse.urlsplit(url.strip())
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()

    if scheme not in ALLOWED_BASE_SCHEMES:
        raise ValueError(
            f"Untrusted URL scheme '{scheme}'. Only secure HTTPS is permitted ({sorted(ALLOWED_BASE_SCHEMES)})."
        )
    if host not in ALLOWED_BASE_HOSTS:
        raise ValueError(
            f"Untrusted host '{host}'. Only verified domain endpoints are permitted ({sorted(ALLOWED_BASE_HOSTS)})."
        )
    if parsed.port not in (None, 443):
        raise ValueError(f"Untrusted port '{parsed.port}'. Only standard HTTPS (443) is permitted.")

    return url.strip().rstrip("/")


class RailwayAPIError(IOError):
    """Base exception raised when a RailwayGovClient API operation fails."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


def parse_retry_after(
    header_val: Optional[str],
    default_delay: float = 30.0,
    min_delay: float = 30.0,
    max_delay: float = 300.0,
) -> float:
    """Parse HTTP Retry-After header adhering to RFC 7231 / RFC 9110.

    Supports both delta-seconds and IMF-fixdate / RFC 2822 HTTP-date formats.
    Enforces repository minimum backoff rules and caps maximum delay.

    Args:
        header_val: Raw Retry-After header string, or None.
        default_delay: Fallback delay in seconds if header is missing or unparseable.
        min_delay: Minimum allowable backoff (mandated >= 30.0s for rate limiting).
        max_delay: Maximum allowable backoff cap (default: 300.0s / 5 minutes).

    Returns:
        Backoff duration in seconds, clamped between min_delay and max_delay.
    """
    if not header_val or not str(header_val).strip():
        return max(default_delay, min_delay)

    clean_val = str(header_val).strip()

    # 1. Attempt delta-seconds integer or float
    try:
        seconds = float(clean_val)
        if math.isnan(seconds) or math.isinf(seconds) or seconds < 0:
            return max(default_delay, min_delay)
        return min(max(seconds, min_delay), max_delay)
    except (ValueError, TypeError):
        pass

    # 2. Attempt HTTP-date timestamp (IMF-fixdate / RFC 2822 / RFC 850)
    try:
        target_dt = email.utils.parsedate_to_datetime(clean_val)
        if target_dt is not None:
            now_dt = datetime.now(timezone.utc)
            delta_sec = (target_dt - now_dt).total_seconds()
            if delta_sec <= 0:
                return max(default_delay, min_delay)
            return min(max(delta_sec, min_delay), max_delay)
    except Exception as exc:
        logger.debug("Failed to parse HTTP-date in Retry-After header %r: %s", clean_val, exc)

    return max(default_delay, min_delay)


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Safely coerce any value to a finite float, returning default on failure."""
    if val is None:
        return default
    try:
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return default
        return num
    except (ValueError, TypeError):
        return default


def _safe_optional_float(val: Any) -> Optional[float]:
    """Safely coerce a value to an optional finite float, returning None on failure."""
    if val is None:
        return None
    try:
        num = float(val)
        if math.isnan(num) or math.isinf(num):
            return None
        return num
    except (ValueError, TypeError):
        return None


def _safe_int(val: Any, default: int = 0) -> int:
    """Safely coerce a value to an int (including float strings like '5.0'), returning default on failure."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            num = float(val)
            if math.isnan(num) or math.isinf(num):
                return default
            return int(num)
        except (ValueError, TypeError):
            return default


def _safe_optional_int(val: Any) -> Optional[int]:
    """Safely coerce a value to an optional int, returning None on failure."""
    if val is None:
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        try:
            num = float(val)
            if math.isnan(num) or math.isinf(num):
                return None
            return int(num)
        except (ValueError, TypeError):
            return None


def _safe_optional_str(val: Any) -> Optional[str]:
    """Safely coerce a value to an optional trimmed string, returning None if empty."""
    if val is None:
        return None
    s = str(val).strip()
    return s if s else None


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
        if not isinstance(data, dict):
            raise TypeError(f"TrainPosition payload must be a dict, got {type(data).__name__}")

        # Coordinate Bounds Validation
        lat_raw = data.get("lat")
        lng_raw = data.get("lng")
        if lat_raw is None or lng_raw is None:
            raise ValueError("TrainPosition payload missing required coordinates ('lat' and 'lng' must not be None)")

        try:
            lat = float(lat_raw)
            lng = float(lng_raw)
        except (ValueError, TypeError) as exc:
            raise ValueError(f"Non-numeric coordinates: lat={lat_raw!r}, lng={lng_raw!r}") from exc

        if math.isnan(lat) or math.isinf(lat) or not (-90.0 <= lat <= 90.0):
            raise ValueError(f"Latitude out of bounds [-90.0, 90.0]: {lat}")
        if math.isnan(lng) or math.isinf(lng) or not (-180.0 <= lng <= 180.0):
            raise ValueError(f"Longitude out of bounds [-180.0, 180.0]: {lng}")

        # Defensive Numeric Coercion
        speed = max(0.0, _safe_float(data.get("speed"), default=0.0))
        status = "Κανονική" if speed > 5.0 else "Στάση"
        raw_course = data.get("course") if data.get("course") is not None else data.get("bearing")
        course = _safe_float(raw_course, default=0.0) % 360.0
        track_heading = _safe_float(data.get("trackHeading"), default=0.0) % 360.0
        delay = _safe_int(data.get("delay"), default=0)

        # Defensive String & Identifier Coercion
        train_id = str(data.get("id") or data.get("_id") or "").strip()
        train_num = str(data.get("trainNumber") or data.get("name") or "Unknown").strip() or "Unknown"

        return cls(
            id=train_id,
            train_number=train_num,
            locomotive_number=_safe_optional_str(data.get("locomotiveNumber")),
            name=_safe_optional_str(data.get("name")),
            train_type=_safe_optional_str(data.get("trainType")),
            service_type=_safe_optional_str(data.get("serviceType")),
            status=status,
            origin=_safe_optional_str(data.get("origin")),
            destination=_safe_optional_str(data.get("destination")),
            next_station=_safe_optional_str(data.get("nextStation")),
            speed=speed,
            course=course,
            track_heading=track_heading,
            lat=lat,
            lng=lng,
            altitude=_safe_optional_float(data.get("altitude")),
            delay=delay,
            progress=_safe_optional_float(data.get("progress")),
            chainage_m=_safe_optional_int(data.get("chainage_m")),
            corridor=_safe_optional_str(data.get("corridor")),
            distance_to_destination_m=_safe_optional_int(data.get("distanceToDestination_m")),
            timestamp=_safe_optional_str(data.get("timestamp")),
            schedule_id=_safe_optional_str(data.get("scheduleId")),
        )


class RailwayGovClient:
    """Client for the Hellenic Railways train-tracker APIs."""

    def __init__(self, base_url: str = BASE_URL) -> None:
        self.base_url = validate_base_url(base_url)
        self.ssl_ctx = ssl.create_default_context()

    def _get(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        max_retries: int = 3,
        base_backoff_sec: float = 1.0,
        max_backoff_sec: float = 30.0,
    ) -> Any:
        url = f"{self.base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"

        for attempt in range(max_retries + 1):
            req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
            try:
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=15) as resp:
                    content = resp.read().decode("utf-8")
                    return json.loads(content)
            except urllib.error.HTTPError as err:
                if err.code == 429:
                    retry_header = err.headers.get("Retry-After") if err.headers else None
                    retry_after = parse_retry_after(retry_header, default_delay=30.0, min_delay=30.0)
                    if attempt < max_retries:
                        logger.warning(
                            "HTTP 429 Rate Limited on %s. Retrying after %.1fs (attempt %d/%d)...",
                            url,
                            retry_after,
                            attempt + 1,
                            max_retries,
                        )
                        time.sleep(retry_after)
                        continue
                    raise RailwayAPIError(
                        f"HTTP 429 Rate Limited on {path}: upstream limit exceeded (Retry-After: {retry_after:.1f}s)",
                        status_code=429,
                        retry_after=retry_after,
                    ) from err
                elif 500 <= err.code < 600:
                    if attempt < max_retries:
                        jitter = random.uniform(0.8, 1.2)
                        sleep_time = min(base_backoff_sec * (2 ** attempt) * jitter, max_backoff_sec)
                        logger.warning(
                            "HTTP %d Server Error on %s. Retrying in %.1fs (attempt %d/%d)...",
                            err.code,
                            url,
                            sleep_time,
                            attempt + 1,
                            max_retries,
                        )
                        time.sleep(sleep_time)
                        continue
                    raise RailwayAPIError(
                        f"HTTP {err.code} Server Error on {path}: {err.reason}",
                        status_code=err.code,
                    ) from err
                else:
                    raise RailwayAPIError(
                        f"HTTP {err.code} Client Error on {path}: {err.reason}",
                        status_code=err.code,
                    ) from err
            except (urllib.error.URLError, TimeoutError, OSError) as err:
                if attempt < max_retries:
                    jitter = random.uniform(0.8, 1.2)
                    sleep_time = min(base_backoff_sec * (2 ** attempt) * jitter, max_backoff_sec)
                    logger.warning(
                        "Network error connecting to %s (%s). Retrying in %.1fs (attempt %d/%d)...",
                        url,
                        err,
                        sleep_time,
                        attempt + 1,
                        max_retries,
                    )
                    time.sleep(sleep_time)
                    continue
                raise RailwayAPIError(
                    f"Network error connecting to {path}: {err}",
                ) from err

    def get_upcoming_schedules(self) -> List[Dict[str, Any]]:
        """Fetch today's upcoming scheduled train trips."""
        res = self._get("/api/public/schedules/upcoming")
        if isinstance(res, dict):
            schedules = res.get("schedules", [])
            return schedules if isinstance(schedules, list) else []
        return []

    def get_schedule_by_id(self, schedule_id: str) -> Dict[str, Any]:
        """Fetch full itinerary for a schedule."""
        res = self._get(f"/api/public/schedules/{schedule_id}")
        return res if isinstance(res, dict) else {}

    def get_schedule_by_train_number(self, train_number: str) -> Dict[str, Any]:
        """Fetch schedule by commercial train number (e.g. '1597')."""
        res = self._get(f"/api/public/train-schedules/by-train-number/{train_number}")
        return res if isinstance(res, dict) else {}

    def get_station(self, station_id: str) -> Dict[str, Any]:
        """Fetch station metadata."""
        res = self._get(f"/api/public/stations/{station_id}")
        return res if isinstance(res, dict) else {}

    def get_station_departures(self, station_id: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetch departure board for a station."""
        res = self._get(f"/api/public/stations/{station_id}/departures", {"limit": limit})
        if isinstance(res, dict):
            deps = res.get("departures", [])
            return deps if isinstance(deps, list) else []
        return []

    def get_station_arrivals(self, station_id: str, limit: int = 15) -> List[Dict[str, Any]]:
        """Fetch arrival board for a station."""
        res = self._get(f"/api/public/stations/{station_id}/arrivals", {"limit": limit})
        if isinstance(res, dict):
            arrs = res.get("arrivals", [])
            return arrs if isinstance(arrs, list) else []
        return []

    def get_rail_infrastructure(self, types: str = "station,track", limit: int = 1000) -> Dict[str, Any]:
        """Fetch GeoJSON rail network features (stations, tracks, signals)."""
        res = self._get("/api/msd/geo", {"mode": "public", "types": types, "limit": limit})
        return res if isinstance(res, dict) else {}

    @staticmethod
    def _decode_sse_positions(payload_str: str) -> Optional[List[TrainPosition]]:
        """Parse and validate a JSON SSE event payload into a list of TrainPosition items."""
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping malformed SSE JSON payload: %s", exc)
            return None

        if not isinstance(payload, dict):
            logger.debug("Skipping non-dict SSE JSON payload: %r", payload)
            return None

        raw_positions = payload.get("positions", [])
        if not isinstance(raw_positions, list):
            logger.debug(
                "Expected 'positions' list in payload, got %s",
                type(raw_positions).__name__,
            )
            return None

        trains: List[TrainPosition] = []
        for p in raw_positions:
            if not isinstance(p, dict):
                logger.debug("Skipping non-dict position item: %r", p)
                continue
            try:
                trains.append(TrainPosition.from_dict(p))
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping invalid train position record: %s", exc)
        return trains

    @staticmethod
    def _compute_sse_reconnect_wait(
        err: Exception,
        reconnect_delay: float,
        attempt: int,
        consecutive_failures: int,
    ) -> Tuple[float, int]:
        """Compute reconnect wait time and updated attempt counter for an SSE stream interruption."""
        log_fn = logger.warning if consecutive_failures == 0 else logger.debug
        prefix = (
            "SSE connection"
            if consecutive_failures == 0
            else f"SSE reconnect attempt {consecutive_failures + 1}"
        )

        if isinstance(err, urllib.error.HTTPError) and err.code == 429:
            retry_header = err.headers.get("Retry-After") if err.headers else None
            wait_time = parse_retry_after(retry_header, default_delay=30.0, min_delay=30.0)
            log_fn(
                "%s rate limited (HTTP 429): %s. Backing off for %.1fs (Retry-After)...",
                prefix,
                err,
                wait_time,
            )
            return wait_time, 0

        jitter = random.uniform(0.8, 1.2)
        wait_time = min(reconnect_delay * (2 ** attempt) * jitter, 60.0)
        if isinstance(err, urllib.error.HTTPError) and 500 <= err.code < 600:
            log_fn(
                "%s server error (HTTP %d %s): %s. Reconnecting in %.1fs...",
                prefix,
                err.code,
                err.reason,
                err,
                wait_time,
            )
        else:
            log_fn(
                "%s interrupted: %s. Reconnecting in %.1fs...",
                prefix,
                err,
                wait_time,
            )
        return wait_time, attempt + 1

    def _iter_sse_payloads(
        self,
        resp: Any,
        event_filter: str,
        stop_event: Optional[threading.Event],
    ) -> Generator[List[TrainPosition], None, None]:
        """Iterate over raw SSE HTTP response lines and yield decoded TrainPosition batches."""
        current_event = "message"
        data_buffer: List[str] = []

        for raw_line in resp:
            if stop_event is not None and stop_event.is_set():
                return

            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line:
                if data_buffer and current_event == event_filter:
                    trains = self._decode_sse_positions("".join(data_buffer))
                    if trains is not None:
                        yield trains
                data_buffer = []
                current_event = "message"
            elif line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                data_buffer.append(line.split(":", 1)[1].strip())

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
        attempt = 0
        disconnect_time: Optional[float] = None
        consecutive_failures: int = 0

        while stop_event is None or not stop_event.is_set():
            try:
                req = urllib.request.Request(stream_url, headers=headers)
                with urllib.request.urlopen(req, context=self.ssl_ctx, timeout=timeout) as resp:
                    for trains in self._iter_sse_payloads(resp, event_filter, stop_event):
                        if consecutive_failures > 0 and disconnect_time is not None:
                            logger.info(
                                "Upstream SSE connection re-established after %.1fs outage.",
                                time.monotonic() - disconnect_time,
                            )
                            consecutive_failures = 0
                            disconnect_time = None
                        yield trains
                        attempt = 0

                    if stop_event is not None and stop_event.is_set():
                        return
                    if reconnect_delay <= 0:
                        return

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if stop_event is not None and stop_event.is_set():
                    return
                if reconnect_delay <= 0:
                    raise

                if consecutive_failures == 0:
                    disconnect_time = time.monotonic()

                wait_time, attempt = self._compute_sse_reconnect_wait(
                    e, reconnect_delay, attempt, consecutive_failures
                )
                consecutive_failures += 1

                if stop_event is not None:
                    if stop_event.wait(timeout=wait_time):
                        return
                else:
                    time.sleep(wait_time)


def _positive_int(val: str) -> int:
    """Argparse validator ensuring a positive integer (> 0)."""
    try:
        parsed = int(val)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid int value: '{val}'") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError(f"value must be a positive integer (> 0), got {parsed}")
    return parsed


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
  railmon departures Athens           # View live departures for Athens Central
  railmon departures SKA --arrivals   # View live arrivals at SKA Hub
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
        help="Launch full-screen interactive curses terminal map dashboard (auto-falls back to plain stream if non-TTY or unsupported).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Force headless plain-text streaming mode (suppresses interactive curses TUI even if --tui is set).",
    )
    parser.add_argument(
        "--daemon",
        action="store_true",
        help="Run in headless background daemon mode (alias for --headless).",
    )
    parser.add_argument(
        "--ascii",
        action="store_true",
        default=False,
        help="Force pure 7-bit ASCII rendering fallback (disable Unicode Braille and UTF-8 glyphs).",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run offline using synthetic telemetry simulator (no live network connection required).",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=15.0,
        help="Target frame rate for TUI rendering loop (default: 15.0, range: 1.0-15.0).",
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
        "--replay",
        type=str,
        default=None,
        metavar="DB_PATH",
        help="Replay recorded historical telemetry from a SQLite database file in the TUI DVR.",
    )
    parser.add_argument(
        "--replay-speed",
        type=float,
        default=1.0,
        help="Initial playback speed multiplier for --replay mode (1, 5, 15, or 60; default: 1.0).",
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
        "--retention-days",
        type=_positive_int,
        default=None,
        help="Rolling retention window in days; archives older telemetry rows to an archive SQLite DB.",
    )
    parser.add_argument(
        "--archive-db",
        type=str,
        default=None,
        help="Path to archive SQLite database for aged telemetry (default: <db_stem>_archive.db).",
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default=None,
        help="Custom path for rotating log file (default: platform state dir).",
    )
    parser.add_argument(
        "--log-level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging severity threshold (default: INFO).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging shortcut (equivalent to --log-level DEBUG).",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
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

    # 3. schedule (F-6.2)
    p_sched = subparsers.add_parser(
        "schedule",
        help="Inspect timetable and intermediate calling stops for a train.",
        description="Fetch and display intermediate calling points, platform tracks, scheduled and actual times for any commercial train.",
    )
    p_sched.add_argument(
        "train_number",
        type=str,
        help="Commercial train number (e.g. '1597', 'IC52', 'IC 52', '2232').",
    )
    p_sched.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="Run in offline mock mode with synthetic train timetables.",
    )
    p_sched.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured JSON to stdout instead of tabular text.",
    )
    p_sched.add_argument(
        "--ascii",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force pure 7-bit ASCII rendering fallback.",
    )

    # 4. departures
    p_dep = subparsers.add_parser(
        "departures",
        help="Query live station departure and arrival boards.",
        description="Fetch and display upcoming departures or arrivals for any Greek railway station.",
    )
    p_dep.add_argument(
        "station",
        type=str,
        help="Station name, Latin alias, numerical code, or 3-letter ID (e.g. 'Athens', 'Αθήνα', 'SKA', '1001').",
    )
    p_dep.add_argument(
        "-a",
        "--arrivals",
        action="store_true",
        help="Display upcoming station arrivals instead of departures.",
    )
    p_dep.add_argument(
        "-n",
        "--limit",
        type=int,
        default=15,
        help="Maximum number of train movements to display (default: 15, range: 1-50).",
    )
    p_dep.add_argument(
        "-t",
        "--type",
        type=str,
        default="all",
        choices=["all", "intercity", "suburban", "regional", "freight"],
        help="Filter departures by service type (default: 'all').",
    )
    p_dep.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON to stdout instead of tabular text.",
    )
    p_dep.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="Run in offline mock mode with synthetic station timetables.",
    )
    p_dep.add_argument(
        "--ascii",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force pure 7-bit ASCII rendering fallback.",
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
    p_rec.add_argument(
        "--retention-days",
        type=_positive_int,
        default=argparse.SUPPRESS,
        help="Rolling retention window in days; archives older telemetry rows to an archive SQLite DB.",
    )
    p_rec.add_argument(
        "--archive-db",
        type=str,
        default=argparse.SUPPRESS,
        help="Path to archive SQLite database for aged telemetry (default: <db_stem>_archive.db).",
    )

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

    # history archive
    p_arch = hist_subs.add_parser("archive", parents=[db_parent], help="Archive aged telemetry rows to an archive SQLite database.")
    p_arch.add_argument("--before", type=str, help="ISO-8601 UTC cutoff timestamp (e.g. 2026-11-01T00:00:00Z).")
    p_arch.add_argument("--retention-days", type=_positive_int, help="Archive telemetry rows older than N days.")
    p_arch.add_argument("--archive-db", type=str, help="Target archive SQLite database file path (default: <db_stem>_archive.db).")

    return parser


def is_tty_supported() -> bool:
    """Validate whether stdout supports interactive full-screen curses rendering.

    Returns:
        bool: True if sys.stdout is an interactive terminal with an active TTY;
              False if redirected to a pipe, regular file, or null device.
    """
    return bool(hasattr(sys.stdout, "isatty") and sys.stdout.isatty())


def _normalize_mode_enums(
    source: Any,
    recording: Any = RecordingMode.DISABLED,
    deadband: Any = DeadbandFilterMode.ENABLED,
    glyph_mode: Any = GlyphMode.AUTO,
    verbosity: Any = OutputVerbosity.NORMAL,
    *,
    demo: Optional[bool] = None,
    record: Optional[bool] = None,
    enable_deadband: Optional[bool] = None,
    ascii_mode: Optional[bool] = None,
    quiet: Optional[bool] = None,
) -> Tuple[TelemetrySource, RecordingMode, DeadbandFilterMode, GlyphMode, OutputVerbosity]:
    """Normalize enum arguments and legacy boolean compatibility flags."""
    if demo is not None or isinstance(source, bool):
        flag = demo if demo is not None else bool(source)
        norm_source = TelemetrySource.DEMO if flag else TelemetrySource.LIVE
    else:
        norm_source = source

    if record is not None or isinstance(recording, bool):
        flag = record if record is not None else bool(recording)
        norm_rec = RecordingMode.ENABLED if flag else RecordingMode.DISABLED
    else:
        norm_rec = recording

    if enable_deadband is not None or isinstance(deadband, bool):
        flag = enable_deadband if enable_deadband is not None else bool(deadband)
        norm_db = DeadbandFilterMode.ENABLED if flag else DeadbandFilterMode.DISABLED
    else:
        norm_db = deadband

    if ascii_mode is not None or isinstance(glyph_mode, bool):
        flag = ascii_mode if ascii_mode is not None else bool(glyph_mode)
        norm_glyph = GlyphMode.ASCII if flag else GlyphMode.UTF8
    else:
        norm_glyph = glyph_mode

    if quiet is not None or isinstance(verbosity, bool):
        flag = quiet if quiet is not None else bool(verbosity)
        norm_verb = OutputVerbosity.QUIET if flag else OutputVerbosity.NORMAL
    else:
        norm_verb = verbosity

    return norm_source, norm_rec, norm_db, norm_glyph, norm_verb


def _start_optional_recorder(
    recording: RecordingMode,
    db_path: str,
    source: TelemetrySource,
    deadband: DeadbandFilterMode,
    retention_days: Optional[int],
    archive_path: Optional[str],
) -> Tuple[Optional[Any], Optional[Any]]:
    """Initialize and start SQLite Database and TelemetryRecorder when recording is enabled."""
    if recording != RecordingMode.ENABLED:
        return None, None

    from storage.db import Database
    from storage.recorder import RecorderConfig, TelemetryRecorder

    db = Database(db_path)
    db.init_schema()
    recorder = TelemetryRecorder(
        db,
        RecorderConfig(
            mode="demo" if source == TelemetrySource.DEMO else "live",
            enable_deadband=(deadband == DeadbandFilterMode.ENABLED),
            retention_days=retention_days,
            archive_path=archive_path,
        ),
    )
    recorder.start()
    return db, recorder


def _format_plain_train_line(t: TrainPosition) -> str:
    """Format a single TrainPosition row for plain-text CLI stream output."""
    route = (
        f"{t.origin or '—'} -> {t.destination or '—'}"
        if (t.origin or t.destination)
        else (t.corridor or "Shunting/Yard")
    )
    delay_str = (
        f"+{t.delay}m"
        if t.delay > 0
        else (f"{t.delay}m" if t.delay < 0 else "On-time")
    )
    return (
        f"{t.train_number:<8} "
        f"{(t.service_type or t.train_type or 'N/A')[:11]:<12} "
        f"{route[:30]:<32} "
        f"{t.speed:>5.1f} km/h "
        f"{delay_str:<8} "
        f"({t.lat:.4f}, {t.lng:.4f})"
    )


def _build_tui_worker(
    source: TelemetrySource,
    store: Any,
    db_path: str,
    reconnect_delay: float,
    recorder: Optional[Any],
    replay_speed: float = 1.0,
) -> Any:
    """Instantiate the appropriate background ingestion or DVR replay worker for run_tui_mode."""
    from tui.mock_engine import MockTelemetryEngine
    from tui.stream_worker import ReplayWorker, StreamWorker

    if source == TelemetrySource.REPLAY:
        return ReplayWorker(
            store=store,
            db_path=db_path,
            speed_multiplier=replay_speed,
        )
    if source == TelemetrySource.DEMO:
        return StreamWorker(store=store, mock_engine=MockTelemetryEngine(), recorder=recorder)
    return StreamWorker(
        store=store,
        client=RailwayGovClient(),
        base_backoff_sec=reconnect_delay,
        recorder=recorder,
    )


def run_tui_mode(
    source: TelemetrySource = TelemetrySource.LIVE,
    fps: float = 15.0,
    reconnect_delay: float = 3.0,
    recording: RecordingMode = RecordingMode.DISABLED,
    db_path: str = "railway_history.db",
    deadband: DeadbandFilterMode = DeadbandFilterMode.ENABLED,
    retention_days: Optional[int] = None,
    archive_path: Optional[str] = None,
    glyph_mode: GlyphMode = GlyphMode.AUTO,
    replay_speed: float = 1.0,
    # Backward compatibility shims:
    demo: Optional[bool] = None,
    record: Optional[bool] = None,
    enable_deadband: Optional[bool] = None,
    ascii_mode: Optional[bool] = None,
) -> None:
    """Launch the interactive curses terminal map application.

    If standard output is not a TTY or curses initialization fails (e.g. missing
    terminfo database, container environment, or missing windows-curses), automatically
    falls back to plain-text streaming mode.
    """
    source, recording, deadband, glyph_mode, _ = _normalize_mode_enums(
        source,
        recording,
        deadband,
        glyph_mode,
        demo=demo,
        record=record,
        enable_deadband=enable_deadband,
        ascii_mode=ascii_mode,
    )
    if source == TelemetrySource.REPLAY:
        recording = RecordingMode.DISABLED

    if not is_tty_supported():
        logger.warning(
            "Terminal environment does not support curses (sys.stdout is not a TTY). Falling back to headless mode."
        )
        print(
            "[WARN] Standard output is not a TTY. Falling back to plain-text stream mode.",
            file=sys.stderr,
        )
        run_plain_stream_mode(
            source=source,
            reconnect_delay=reconnect_delay,
            recording=recording,
            db_path=db_path,
            deadband=deadband,
            retention_days=retention_days,
            archive_path=archive_path,
        )
        return

    try:
        import curses
        from tui.app import run_tui
        from tui.geo import is_utf8_supported
        from tui.mock_engine import load_stations, load_tracks
        from tui.state import TrainStateStore
    except (ImportError, ModuleNotFoundError) as err:
        if sys.platform == "win32" and "curses" in str(err).lower():
            logger.warning(
                "windows-curses is not installed on Windows (%s). Falling back to headless mode.",
                err,
            )
            print(
                "\n[WARN] The interactive terminal dashboard (--tui) requires 'windows-curses' on Windows.\n"
                "Falling back to plain-text stream mode. To enable TUI, run: pip install windows-curses\n",
                file=sys.stderr,
            )
            run_plain_stream_mode(
                source=source,
                reconnect_delay=reconnect_delay,
                recording=recording,
                db_path=db_path,
                deadband=deadband,
                retention_days=retention_days,
                archive_path=archive_path,
            )
            return
        raise

    db, recorder = _start_optional_recorder(
        recording, db_path, source, deadband, retention_days, archive_path
    )

    # 1. Initialize thread-safe state store
    store = TrainStateStore()
    if recording == RecordingMode.ENABLED:
        store.set_recording_status(True, db_path)

    # 2. Load static rail infrastructure
    stations = list(load_stations().values())
    tracks = list(load_tracks().values())

    # 3. Configure ingestion or DVR replay worker
    worker = _build_tui_worker(
        source=source,
        store=store,
        db_path=db_path,
        reconnect_delay=reconnect_delay,
        recorder=recorder,
        replay_speed=replay_speed,
    )

    # 4. Start background ingestion thread
    worker.start()

    # 5. Resolve ASCII fallback mode
    use_ascii = (
        True
        if glyph_mode == GlyphMode.ASCII
        else (False if glyph_mode == GlyphMode.UTF8 else not is_utf8_supported(sys.stdout))
    )
    if use_ascii:
        logger.info("Non-UTF-8 environment or --ascii specified; enabling 7-bit ASCII fallback matrix.")

    # 6. Execute curses event loop with terminal protection and fallback
    fallback_needed = False
    curses_error_cls = getattr(curses, "error", Exception)
    with install_signal_handlers() as stop_event:
        try:
            run_tui(
                store=store,
                worker=worker,
                stations=stations,
                tracks=tracks,
                target_fps=fps,
                stop_event=stop_event,
                ascii_mode=use_ascii,
            )
        except curses_error_cls as exc:
            logger.warning(
                "Terminal environment does not support curses (%s). Falling back to headless mode.",
                exc,
            )
            print(
                f"\n[WARN] Terminal environment does not support curses ({exc}). Falling back to plain-text stream mode.\n",
                file=sys.stderr,
            )
            fallback_needed = True
        finally:
            stop_event.set()
            worker.stop()
            if recorder:
                recorder.stop()
            if db:
                db.close()

    if fallback_needed:
        run_plain_stream_mode(
            source=source,
            reconnect_delay=reconnect_delay,
            recording=recording,
            db_path=db_path,
            deadband=deadband,
            retention_days=retention_days,
            archive_path=archive_path,
        )


def _iter_replay_plain_batches(db_path: str) -> Generator[List[TrainPosition], None, None]:
    """Yield chronological batches of recorded TrainPositions from SQLite for headless replay."""
    from storage.db import Database
    from storage.repository import HistoricalRepository

    if not os.path.exists(db_path):
        return
    db = Database(db_path, auto_migrate=False)
    try:
        repo = HistoricalRepository(db)
        bounds = repo.get_replay_bounds()
        if bounds is None:
            return
        observations = repo.get_replay_observations(
            bounds.min_recorded_at,
            bounds.max_recorded_at,
            include_start=True,
        )
        by_ts: Dict[str, List[TrainPosition]] = {}
        for obs in observations:
            by_ts.setdefault(obs.recorded_at, []).append(obs.position)
        for batch in by_ts.values():
            yield batch
    finally:
        db.close()


def _resolve_plain_stream_source(
    source: TelemetrySource,
    db_path: str,
    reconnect_delay: float,
    stop_event: threading.Event,
) -> Tuple[Generator[List[TrainPosition], None, None], str]:
    """Resolve the generator and display label for run_plain_stream_mode."""
    if source == TelemetrySource.REPLAY:
        return _iter_replay_plain_batches(db_path), f"SQLite Historical Replay ({db_path})"
    if source == TelemetrySource.DEMO:
        from tui.mock_engine import MockTelemetryEngine

        stream = MockTelemetryEngine().stream_positions(interval_sec=1.0, stop_event=stop_event)
        return stream, "Offline Mock Simulation"
    stream = RailwayGovClient().stream_train_positions(
        reconnect_delay=reconnect_delay,
        stop_event=stop_event,
    )
    return stream, "Live SSE Stream (/api/train-stream)"


def run_plain_stream_mode(
    source: TelemetrySource = TelemetrySource.LIVE,
    reconnect_delay: float = 3.0,
    recording: RecordingMode = RecordingMode.DISABLED,
    db_path: str = "railway_history.db",
    deadband: DeadbandFilterMode = DeadbandFilterMode.ENABLED,
    retention_days: Optional[int] = None,
    archive_path: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
    # Backward compatibility shims:
    demo: Optional[bool] = None,
    record: Optional[bool] = None,
    enable_deadband: Optional[bool] = None,
) -> None:
    """Stream train positions to stdout in formatted tabular text."""
    source, recording, deadband, _, _ = _normalize_mode_enums(
        source,
        recording,
        deadband,
        demo=demo,
        record=record,
        enable_deadband=enable_deadband,
    )
    if source == TelemetrySource.REPLAY:
        recording = RecordingMode.DISABLED

    with install_signal_handlers(stop_event) as stop_event:
        db, recorder = _start_optional_recorder(
            recording, db_path, source, deadband, retention_days, archive_path
        )
        stream, source_label = _resolve_plain_stream_source(
            source, db_path, reconnect_delay, stop_event
        )

        rec_label = f" | SQLite Recording: {db_path}" if recording == RecordingMode.ENABLED else ""
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
                if stop_event.is_set():
                    break

                print(f"\n[{time.strftime('%H:%M:%S')}] Live Trains on Network: {len(batch)}")
                print(
                    f"{'Train':<8} {'Type':<12} {'Route':<32} {'Speed':<10} {'Delay':<8} {'Position (Lat, Lng)':<22}"
                )
                print("-" * 95)

                for t in batch:
                    print(_format_plain_train_line(t))
                sys.stdout.flush()
                if stop_event.wait(timeout=1.0):
                    break
            print("\nMonitoring stopped.")
        except KeyboardInterrupt:
            print("\nMonitoring stopped.")
        except BrokenPipeError:
            pass
        finally:
            if recorder:
                recorder.stop()
            if db:
                db.close()


def _safe_file_size(path: str) -> int:
    """Return file size in bytes or 0 on OSError."""
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def run_record_mode(
    db_path: str = "railway_history.db",
    source: TelemetrySource = TelemetrySource.LIVE,
    deadband: DeadbandFilterMode = DeadbandFilterMode.ENABLED,
    batch_size: int = 50,
    flush_interval_sec: float = 2.0,
    verbosity: OutputVerbosity = OutputVerbosity.NORMAL,
    reconnect_delay: float = 3.0,
    retention_days: Optional[int] = None,
    archive_path: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
    # Backward compatibility shims:
    demo: Optional[bool] = None,
    enable_deadband: Optional[bool] = None,
    quiet: Optional[bool] = None,
) -> int:
    """Execute dedicated headless recording daemon with terminal metrics display."""
    source, _, deadband, _, verbosity = _normalize_mode_enums(
        source,
        deadband=deadband,
        verbosity=verbosity,
        demo=demo,
        enable_deadband=enable_deadband,
        quiet=quiet,
    )

    from storage.cli_formatters import format_file_size
    from storage.db import Database
    from storage.recorder import RecorderConfig, TelemetryRecorder

    with install_signal_handlers(stop_event) as stop_event:
        db = Database(db_path)
        db.init_schema()

        config = RecorderConfig(
            batch_size=batch_size,
            flush_interval_sec=flush_interval_sec,
            enable_deadband=(deadband == DeadbandFilterMode.ENABLED),
            mode="demo" if source == TelemetrySource.DEMO else "live",
            retention_days=retention_days,
            archive_path=archive_path,
        )
        recorder = TelemetryRecorder(db, config)
        recorder.start()

        source_label = "Offline Mock Simulation" if source == TelemetrySource.DEMO else "Live SSE Stream (/api/train-stream)"
        deadband_label = "ENABLED (distance >= 15m, delay delta >= 1m)" if deadband == DeadbandFilterMode.ENABLED else "DISABLED (recording all raw coordinates)"

        print("=" * 70)
        print("  railmon - Hellenic Railways Telemetry Ingestion Daemon")
        print(f"  Source: {source_label}")
        print(f"  Database: {db_path} (WAL mode)")
        print(f"  Deadband Compression: {deadband_label}")
        print("=" * 70)
        now_str = time.strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now_str}] Session #{recorder.session_id} initialized. Recording started. Press Ctrl+C to stop.\n")

        if verbosity == OutputVerbosity.NORMAL:
            print(f"  {'Timestamp':<11} | {'Trains':>6} | {'Batch Rows':>10} | {'Total Stored':>12} | {'Compressed':>10} | {'DB Size':>9}")
            print("-" * 75)

        start_time = time.time()
        last_stat_time = time.time()

        try:
            if source == TelemetrySource.DEMO:
                from tui.mock_engine import MockTelemetryEngine

                stream = MockTelemetryEngine().stream_positions(interval_sec=1.0, stop_event=stop_event)
            else:
                stream = RailwayGovClient().stream_train_positions(
                    reconnect_delay=reconnect_delay,
                    stop_event=stop_event,
                )

            for batch in stream:
                if stop_event.is_set() or recorder.disk_full_tripped:
                    stop_event.set()
                    break
                recorder.enqueue_batch(batch)
                if stop_event.is_set() or recorder.disk_full_tripped:
                    stop_event.set()
                    break

                now = time.time()
                if verbosity == OutputVerbosity.NORMAL and (now - last_stat_time) >= 5.0:
                    stats = recorder.get_stats()
                    filter_stats = stats.get("filter") or {}
                    ratio = filter_stats.get("suppression_ratio_pct", 0.0)
                    db_size = _safe_file_size(db_path)
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
            db_size = _safe_file_size(db_path)

            print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Session #{recorder.session_id} finalized.")
            print(f"  - Duration: {duration:.1f}s")
            print(f"  - Total Telemetry Rows Stored: {stats.get('total_records_written', 0):,d}")
            if deadband == DeadbandFilterMode.ENABLED:
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
    from storage.repository import HistoricalRepository, validate_export_path

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

    try:
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
                    try:
                        clean_out = validate_export_path(args.out)
                    except (ValueError, PermissionError) as err:
                        print(f"[Error] Invalid export path: {err}", file=sys.stderr)
                        return 1
                    with open(clean_out, "w", encoding="utf-8") as f:
                        f.write(out_str)
                    count = len(data.get("features", []))
                    print(f"Exported GeoJSON ({count} features) to {clean_out}")
                else:
                    print(out_str)
            elif fmt == "csv":
                if args.out:
                    try:
                        clean_out = validate_export_path(args.out)
                    except (ValueError, PermissionError) as err:
                        print(f"[Error] Invalid export path: {err}", file=sys.stderr)
                        return 1
                    count = repo.export_csv(
                        query_type="telemetry",
                        target=clean_out,
                        params={
                            "train_number": args.train_number,
                            "start_time": args.start,
                            "end_time": args.end,
                        },
                    )
                    print(f"Exported {count} CSV records to {clean_out}")
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

        elif action == "archive":
            from datetime import datetime, timedelta, timezone

            before_iso = getattr(args, "before", None)
            ret_days = getattr(args, "retention_days", None)
            if not before_iso and ret_days is None:
                print(
                    "[Error] Specify either '--before <ISO-TIMESTAMP>' or '--retention-days <DAYS>' for 'railmon history archive'.",
                    file=sys.stderr,
                )
                return 1
            if not before_iso and ret_days is not None:
                cutoff_dt = datetime.now(timezone.utc) - timedelta(days=ret_days)
                before_iso = cutoff_dt.isoformat().replace("+00:00", "Z")

            try:
                arch_path = repo.resolve_archive_path(getattr(args, "archive_db", None))
            except (ValueError, PermissionError) as err:
                print(f"[Error] Invalid archive database path: {err}", file=sys.stderr)
                return 1
            archived_count = repo.archive_before(str(before_iso), archive_path=arch_path)
            print(f"Archived {archived_count:,d} telemetry records older than {before_iso} to {arch_path}")
    finally:
        db.close()
    return 0


def run_schedules_mode() -> int:
    """Fetch and display today's upcoming train timetables.

    Returns:
        Exit code (0 on success, 1 on failure).
    """
    client = RailwayGovClient()
    print("Fetching today's upcoming train schedules from railway.gov.gr...\n")
    try:
        schedules = client.get_upcoming_schedules()
    except (RailwayAPIError, urllib.error.URLError, TimeoutError, OSError) as err:
        print(f"[Error] Failed to fetch train timetables: {err}", file=sys.stderr)
        return 1

    if not schedules:
        print("No upcoming train timetables returned for today.")
        return 0

    print(f"Total upcoming schedules: {len(schedules)}")
    for s in schedules[:15]:
        orig = s.get("origin")
        dest = s.get("destination")
        orig_name = orig.get("nameGreek") if isinstance(orig, dict) else orig or "N/A"
        dest_name = dest.get("nameGreek") if isinstance(dest, dict) else dest or "N/A"
        print(
            f"• Train {str(s.get('trainNumber')):>5} | {orig_name} -> {dest_name} | Dep: {s.get('scheduledDeparture', 'N/A')}"
        )
    return 0


def run_departures_mode(args: argparse.Namespace) -> int:
    """Fetch and display station departure or arrival board.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 on success, 1 on failure).
    """
    from storage.cli_formatters import format_station_board_table
    from tui.mock_engine import (
        generate_mock_station_board,
        load_stations,
        resolve_station_for_cli,
    )

    query = getattr(args, "station", "")
    stations = load_stations()
    station_node, suggestions = resolve_station_for_cli(query, stations)

    if station_node is None:
        print(f"[Error] Station not found: '{query}'", file=sys.stderr)
        if suggestions:
            print("Did you mean:", file=sys.stderr)
            for sugg in suggestions:
                print(f"  • {sugg}", file=sys.stderr)
        return 1

    is_arrivals = bool(getattr(args, "arrivals", False))
    limit = max(1, min(50, int(getattr(args, "limit", 15))))
    service_type = str(getattr(args, "type", "all")).strip().lower()
    is_demo = bool(getattr(args, "demo", False))
    is_json = bool(getattr(args, "json", False))
    is_ascii = bool(getattr(args, "ascii", False))

    if is_demo:
        records = generate_mock_station_board(station_node, is_arrivals=is_arrivals, limit=limit)
        data_source = "synthetic simulation (--demo)"
    else:
        client = RailwayGovClient()
        try:
            if is_arrivals:
                records = client.get_station_arrivals(station_node.id, limit=limit)
            else:
                records = client.get_station_departures(station_node.id, limit=limit)
        except (RailwayAPIError, urllib.error.URLError, TimeoutError, OSError) as err:
            print(f"[Error] Failed to fetch station timetable: {err}", file=sys.stderr)
            return 1
        data_source = "railway.gov.gr"

    if service_type != "all":
        records = [
            r
            for r in records
            if service_type in str(r.get("serviceType") or r.get("service_type") or "").strip().lower()
        ]

    if is_json:
        payload = {
            "station": {
                "id": station_node.id,
                "code": station_node.code,
                "name_el": station_node.name_el,
                "name_en": station_node.name_en,
                "corridor": station_node.corridor,
                "lat": station_node.point.lat,
                "lng": station_node.point.lng,
            },
            "mode": "arrivals" if is_arrivals else "departures",
            "query": query,
            "count": len(records),
            "records": records,
        }
        print(json.dumps(payload, indent=2, ensure_ascii=is_ascii))
        return 0

    now_str = datetime.now().strftime("%H:%M:%S")
    table_str = format_station_board_table(
        station=station_node,
        records=records,
        is_arrivals=is_arrivals,
        is_ascii=is_ascii,
        data_source=data_source,
        timestamp_str=now_str,
    )
    print(table_str)
    return 0


def run_schedule_mode(args: argparse.Namespace) -> int:
    """Fetch and display train timetable and intermediate calling stops.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code (0 on success, 1 on failure).
    """
    import difflib
    from storage.cli_formatters import format_train_schedule_table
    from tui.mock_engine import (
        generate_mock_train_schedule,
        get_active_mock_train_numbers,
    )

    query = getattr(args, "train_number", "").strip()
    if not query:
        print("[Error] Missing required argument: train_number", file=sys.stderr)
        return 1

    is_demo = bool(getattr(args, "demo", False))
    is_json = bool(getattr(args, "json", False))
    is_ascii = bool(getattr(args, "ascii", False))

    timetable: Optional[Dict[str, Any]] = None
    data_source = "railway.gov.gr"

    if is_demo:
        timetable = generate_mock_train_schedule(query)
        data_source = "synthetic simulation (--demo)"
    else:
        client = RailwayGovClient()
        try:
            timetable = client.get_schedule_by_train_number(query)
        except (RailwayAPIError, urllib.error.URLError, TimeoutError, OSError) as err:
            candidates = get_active_mock_train_numbers()
            matches = difflib.get_close_matches(query.upper(), [c.upper() for c in candidates], n=4, cutoff=0.3)
            print(f"[Error] Failed to fetch schedule for train '{query}': {err}", file=sys.stderr)
            if matches:
                print("Did you mean:", file=sys.stderr)
                for m in matches:
                    print(f"  • {m}", file=sys.stderr)
            return 1

    if not timetable or not timetable.get("stops"):
        candidates = get_active_mock_train_numbers()
        matches = difflib.get_close_matches(query.upper(), [c.upper() for c in candidates], n=4, cutoff=0.3)
        print(f"[Error] Timetable not found for train '{query}'.", file=sys.stderr)
        if matches:
            print("Did you mean:", file=sys.stderr)
            for m in matches:
                print(f"  • {m}", file=sys.stderr)
        return 1

    if is_json:
        print(json.dumps(timetable, indent=2, ensure_ascii=is_ascii))
        return 0

    table_str = format_train_schedule_table(
        timetable,
        is_ascii=is_ascii,
        data_source=data_source,
    )
    print(table_str)
    return 0


def _configure_win32_utf8_console() -> None:
    """Configure Windows console codepage and standard streams for UTF-8 output."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.kernel32.SetConsoleCP(65001)
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
    except (ImportError, AttributeError, OSError):
        pass
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError, ValueError):
        pass


def _attach_optional_retention_kwargs(args: argparse.Namespace, target_kwargs: Dict[str, Any]) -> None:
    """Attach optional --retention-days and --archive-db arguments if present."""
    if getattr(args, "retention_days", None) is not None:
        target_kwargs["retention_days"] = args.retention_days
    if getattr(args, "archive_db", None) is not None:
        target_kwargs["archive_path"] = args.archive_db


def _dispatch_replay_mode(args: argparse.Namespace, is_tui: bool, replay_db: str) -> int:
    """Validate SQLite replay database path and launch TUI DVR or headless plain stream."""
    if not os.path.exists(replay_db):
        print(
            f"[Error] SQLite replay database file '{replay_db}' not found.\n"
            f"Record telemetry first with:\n"
            f"    railmon record --db {replay_db}\n"
            f"    railmon --tui --record --db {replay_db}",
            file=sys.stderr,
        )
        return 1

    glyph_mode = GlyphMode.ASCII if getattr(args, "ascii", False) else GlyphMode.AUTO
    replay_speed = float(getattr(args, "replay_speed", 1.0) or 1.0)
    if is_tui:
        run_tui_mode(
            source=TelemetrySource.REPLAY,
            fps=args.fps,
            reconnect_delay=args.reconnect_delay,
            recording=RecordingMode.DISABLED,
            db_path=replay_db,
            glyph_mode=glyph_mode,
            replay_speed=replay_speed,
        )
        return 0

    run_plain_stream_mode(
        source=TelemetrySource.REPLAY,
        reconnect_delay=args.reconnect_delay,
        recording=RecordingMode.DISABLED,
        db_path=replay_db,
    )
    return 0


def _dispatch_cli_mode(args: argparse.Namespace, is_tui: bool) -> int:
    """Route parsed CLI arguments to the appropriate execution mode."""
    if args.mode == "departures":
        return run_departures_mode(args)

    if args.mode == "schedule":
        return run_schedule_mode(args)

    if args.mode == "schedules":
        ret = run_schedules_mode()
        return ret if isinstance(ret, int) else 0

    if args.mode == "history":
        return run_history_mode(args)

    replay_db = getattr(args, "replay", None)
    if replay_db is not None and str(replay_db).strip():
        return _dispatch_replay_mode(args, is_tui, str(replay_db).strip())

    enable_deadband = not getattr(args, "no_filter_deadband", False)
    deadband = DeadbandFilterMode.ENABLED if enable_deadband else DeadbandFilterMode.DISABLED
    source = TelemetrySource.DEMO if getattr(args, "demo", False) else TelemetrySource.LIVE
    db_path = getattr(args, "db", "railway_history.db")

    if args.mode == "record":
        verbosity = OutputVerbosity.QUIET if getattr(args, "quiet", False) else OutputVerbosity.NORMAL
        rec_kwargs: Dict[str, Any] = {
            "db_path": db_path,
            "source": source,
            "deadband": deadband,
            "batch_size": getattr(args, "batch_size", 50),
            "flush_interval_sec": getattr(args, "flush_interval", 2.0),
            "verbosity": verbosity,
            "reconnect_delay": getattr(args, "reconnect_delay", 3.0),
        }
        _attach_optional_retention_kwargs(args, rec_kwargs)
        return run_record_mode(**rec_kwargs)

    is_rec = bool(getattr(args, "record", False))
    if is_tui:
        glyph_mode = GlyphMode.ASCII if getattr(args, "ascii", False) else GlyphMode.AUTO
        if is_rec:
            tui_kwargs: Dict[str, Any] = {
                "source": source,
                "fps": args.fps,
                "reconnect_delay": args.reconnect_delay,
                "recording": RecordingMode.ENABLED,
                "db_path": db_path,
                "deadband": deadband,
                "glyph_mode": glyph_mode,
            }
            _attach_optional_retention_kwargs(args, tui_kwargs)
            run_tui_mode(**tui_kwargs)
        else:
            run_tui_mode(
                source=source,
                fps=args.fps,
                reconnect_delay=args.reconnect_delay,
                glyph_mode=glyph_mode,
            )
        return 0

    if is_rec:
        stream_kwargs: Dict[str, Any] = {
            "source": source,
            "reconnect_delay": args.reconnect_delay,
            "recording": RecordingMode.ENABLED,
            "db_path": db_path,
            "deadband": deadband,
        }
        _attach_optional_retention_kwargs(args, stream_kwargs)
        run_plain_stream_mode(**stream_kwargs)
    else:
        run_plain_stream_mode(
            source=source,
            reconnect_delay=args.reconnect_delay,
        )
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    """Main CLI entry point.

    Args:
        argv: Optional list of command-line arguments (defaults to sys.argv[1:]).

    Returns:
        Exit code (0 for clean execution).
    """
    _configure_win32_utf8_console()

    parser = build_parser()
    args = parser.parse_args(argv)

    log_level = "DEBUG" if getattr(args, "debug", False) else getattr(args, "log_level", "INFO")
    has_replay = bool(getattr(args, "replay", None) and str(getattr(args, "replay", "")).strip())
    is_tui = bool(
        (getattr(args, "tui", False) or has_replay)
        and not (getattr(args, "headless", False) or getattr(args, "daemon", False))
    )
    setup_logging(
        log_file=getattr(args, "log_file", None),
        log_level=log_level,
        is_tui=is_tui,
        enable_console=False,
    )

    try:
        return _dispatch_cli_mode(args, is_tui)
    except KeyboardInterrupt:
        print("\nProcess interrupted by user.")
        return 0
    except BrokenPipeError:
        return 0


def demo_cli() -> None:
    """Legacy entry point preserving backwards compatibility."""
    main()


if __name__ == "__main__":
    sys.exit(main())
