#!/usr/bin/env python3
"""
railmon - Hellenic Railways (railway.gov.gr) Live Train Tracker & Terminal Map.
Python Client, Real-time SSE Monitor, and Curses TUI.
"""

import argparse
import contextlib
from datetime import datetime, timezone
import email.utils
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
from typing import Any, Dict, FrozenSet, Generator, List, Optional

__version__ = "1.0.0"
__author__ = "Hellenic Train Tracking Contributors"

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
    ):
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

    def __init__(self, base_url: str = BASE_URL):
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
                                    except json.JSONDecodeError as exc:
                                        logger.debug("Skipping malformed SSE JSON payload: %s", exc)
                                        current_event = "message"
                                        continue

                                    if not isinstance(payload, dict):
                                        logger.debug("Skipping non-dict SSE JSON payload: %r", payload)
                                        current_event = "message"
                                        continue

                                    raw_positions = payload.get("positions", [])
                                    if not isinstance(raw_positions, list):
                                        logger.debug(
                                            "Expected 'positions' list in payload, got %s",
                                            type(raw_positions).__name__,
                                        )
                                        current_event = "message"
                                        continue

                                    trains: List[TrainPosition] = []
                                    for p in raw_positions:
                                        if not isinstance(p, dict):
                                            logger.debug("Skipping non-dict position item: %r", p)
                                            continue
                                        try:
                                            trains.append(TrainPosition.from_dict(p))
                                        except (ValueError, TypeError) as exc:
                                            logger.debug("Skipping invalid train position record: %s", exc)

                                    if consecutive_failures > 0 and disconnect_time is not None:
                                        outage_dur = time.monotonic() - disconnect_time
                                        logger.info(
                                            "Upstream SSE connection re-established after %.1fs outage.",
                                            outage_dur,
                                        )
                                        consecutive_failures = 0
                                        disconnect_time = None

                                    yield trains
                                    attempt = 0
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

                if consecutive_failures == 0:
                    disconnect_time = time.monotonic()

                if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                    retry_header = e.headers.get("Retry-After") if e.headers else None
                    wait_time = parse_retry_after(retry_header, default_delay=30.0, min_delay=30.0)
                    if consecutive_failures == 0:
                        logger.warning(
                            "SSE connection rate limited (HTTP 429): %s. Backing off for %.1fs (Retry-After)...",
                            e,
                            wait_time,
                        )
                    else:
                        logger.debug(
                            "SSE reconnect attempt %d rate limited (HTTP 429): %s. Backing off for %.1fs (Retry-After)...",
                            consecutive_failures + 1,
                            e,
                            wait_time,
                        )
                    attempt = 0
                elif isinstance(e, urllib.error.HTTPError) and 500 <= e.code < 600:
                    jitter = random.uniform(0.8, 1.2)
                    wait_time = min(reconnect_delay * (2 ** attempt) * jitter, 60.0)
                    if consecutive_failures == 0:
                        logger.warning(
                            "SSE connection server error (HTTP %d %s): %s. Reconnecting in %.1fs...",
                            e.code,
                            e.reason,
                            e,
                            wait_time,
                        )
                    else:
                        logger.debug(
                            "SSE reconnect attempt %d server error (HTTP %d %s): %s. Reconnecting in %.1fs...",
                            consecutive_failures + 1,
                            e.code,
                            e.reason,
                            e,
                            wait_time,
                        )
                    attempt += 1
                else:
                    jitter = random.uniform(0.8, 1.2)
                    wait_time = min(reconnect_delay * (2 ** attempt) * jitter, 60.0)
                    if consecutive_failures == 0:
                        logger.warning(
                            "SSE connection interrupted: %s. Reconnecting in %.1fs...",
                            e,
                            wait_time,
                        )
                    else:
                        logger.debug(
                            "SSE reconnect attempt %d interrupted: %s. Reconnecting in %.1fs...",
                            consecutive_failures + 1,
                            e,
                            wait_time,
                        )
                    attempt += 1

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


def run_tui_mode(
    demo: bool = False,
    fps: float = 20.0,
    reconnect_delay: float = 3.0,
    record: bool = False,
    db_path: str = "railway_history.db",
    enable_deadband: bool = True,
    retention_days: Optional[int] = None,
    archive_path: Optional[str] = None,
    ascii_mode: Optional[bool] = None,
) -> None:
    """Launch the interactive curses terminal map application.

    If standard output is not a TTY or curses initialization fails (e.g. missing
    terminfo database, container environment, or missing windows-curses), automatically
    falls back to plain-text streaming mode.

    Args:
        demo: If True, run offline mock simulation; otherwise connect to live SSE stream.
        fps: Target frames per second for curses refresh loop.
        reconnect_delay: Initial reconnect delay in seconds for live stream interruptions.
        record: If True, persist incoming telemetry to SQLite in background.
        db_path: Destination SQLite database file path when recording is active.
        enable_deadband: If True, apply deadband filter to eliminate stationary coordinate noise.
        retention_days: Optional telemetry retention window in days.
        archive_path: Optional target SQLite database for archived telemetry.
        ascii_mode: Optional boolean to force 7-bit ASCII fallback or UTF-8 mode (auto-detected if None).
    """
    if not is_tty_supported():
        logger.warning(
            "Terminal environment does not support curses (sys.stdout is not a TTY). Falling back to headless mode."
        )
        print(
            "[WARN] Standard output is not a TTY. Falling back to plain-text stream mode.",
            file=sys.stderr,
        )
        run_plain_stream_mode(
            demo=demo,
            reconnect_delay=reconnect_delay,
            record=record,
            db_path=db_path,
            enable_deadband=enable_deadband,
            retention_days=retention_days,
            archive_path=archive_path,
        )
        return

    try:
        import curses
        from tui.app import run_tui
        from tui.geo import is_utf8_supported
        from tui.mock_engine import MockTelemetryEngine, load_stations, load_tracks
        from tui.state import TrainStateStore
        from tui.stream_worker import StreamWorker
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
                demo=demo,
                reconnect_delay=reconnect_delay,
                record=record,
                db_path=db_path,
                enable_deadband=enable_deadband,
                retention_days=retention_days,
                archive_path=archive_path,
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
                retention_days=retention_days,
                archive_path=archive_path,
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

    # 5. Resolve ASCII fallback mode
    if ascii_mode is None:
        use_ascii = not is_utf8_supported(sys.stdout)
    else:
        use_ascii = bool(ascii_mode)

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
            demo=demo,
            reconnect_delay=reconnect_delay,
            record=record,
            db_path=db_path,
            enable_deadband=enable_deadband,
            retention_days=retention_days,
            archive_path=archive_path,
        )


def run_plain_stream_mode(
    demo: bool = False,
    reconnect_delay: float = 3.0,
    record: bool = False,
    db_path: str = "railway_history.db",
    enable_deadband: bool = True,
    retention_days: Optional[int] = None,
    archive_path: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
) -> None:
    """Stream train positions to stdout in formatted tabular text.

    Args:
        demo: If True, stream from MockTelemetryEngine; otherwise stream from live SSE feed.
        reconnect_delay: Reconnection interval for live SSE feed.
        record: If True, persist incoming telemetry to SQLite in background.
        db_path: Destination SQLite database file path when recording is active.
        enable_deadband: If True, apply deadband filter to eliminate stationary coordinate noise.
        retention_days: Optional telemetry retention window in days.
        archive_path: Optional target SQLite database for archived telemetry.
        stop_event: Optional threading.Event to signal shutdown.
    """
    with install_signal_handlers(stop_event) as stop_event:
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
                    retention_days=retention_days,
                    archive_path=archive_path,
                ),
            )
            recorder.start()

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
                if stop_event.is_set():
                    break

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
                    delay_str = (
                        f"+{t.delay}m"
                        if t.delay > 0
                        else (f"{t.delay}m" if t.delay < 0 else "On-time")
                    )
                    print(
                        f"{t.train_number:<8} "
                        f"{(t.service_type or t.train_type or 'N/A')[:11]:<12} "
                        f"{route[:30]:<32} "
                        f"{t.speed:>5.1f} km/h "
                        f"{delay_str:<8} "
                        f"({t.lat:.4f}, {t.lng:.4f})"
                    )
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


def run_record_mode(
    db_path: str = "railway_history.db",
    demo: bool = False,
    enable_deadband: bool = True,
    batch_size: int = 50,
    flush_interval_sec: float = 2.0,
    quiet: bool = False,
    reconnect_delay: float = 3.0,
    retention_days: Optional[int] = None,
    archive_path: Optional[str] = None,
    stop_event: Optional[threading.Event] = None,
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
        retention_days: Optional telemetry retention window in days.
        archive_path: Optional target SQLite database for archived telemetry.
        stop_event: Optional threading.Event to signal shutdown.

    Returns:
        Exit code (0 on success).
    """
    from storage.cli_formatters import format_file_size
    from storage.db import Database
    from storage.recorder import RecorderConfig, TelemetryRecorder

    with install_signal_handlers(stop_event) as stop_event:
        db = Database(db_path)
        db.init_schema()

        config = RecorderConfig(
            batch_size=batch_size,
            flush_interval_sec=flush_interval_sec,
            enable_deadband=enable_deadband,
            mode="demo" if demo else "live",
            retention_days=retention_days,
            archive_path=archive_path,
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
                if stop_event.is_set() or recorder.disk_full_tripped:
                    stop_event.set()
                    break
                recorder.enqueue_batch(batch)
                if stop_event.is_set() or recorder.disk_full_tripped:
                    stop_event.set()
                    break

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

    log_level = "DEBUG" if getattr(args, "debug", False) else getattr(args, "log_level", "INFO")
    is_tui = bool(getattr(args, "tui", False) and not (getattr(args, "headless", False) or getattr(args, "daemon", False)))
    setup_logging(
        log_file=getattr(args, "log_file", None),
        log_level=log_level,
        is_tui=is_tui,
        enable_console=False,
    )

    try:
        if args.mode == "schedules":
            ret = run_schedules_mode()
            return ret if isinstance(ret, int) else 0
        elif args.mode == "record":
            enable_deadband = not getattr(args, "no_filter_deadband", False)
            rec_kwargs: Dict[str, Any] = {
                "db_path": getattr(args, "db", "railway_history.db"),
                "demo": getattr(args, "demo", False),
                "enable_deadband": enable_deadband,
                "batch_size": getattr(args, "batch_size", 50),
                "flush_interval_sec": getattr(args, "flush_interval", 2.0),
                "quiet": getattr(args, "quiet", False),
                "reconnect_delay": getattr(args, "reconnect_delay", 3.0),
            }
            if getattr(args, "retention_days", None) is not None:
                rec_kwargs["retention_days"] = args.retention_days
            if getattr(args, "archive_db", None) is not None:
                rec_kwargs["archive_path"] = args.archive_db
            return run_record_mode(**rec_kwargs)
        elif args.mode == "history":
            return run_history_mode(args)
        elif args.tui and not (getattr(args, "headless", False) or getattr(args, "daemon", False)):
            is_rec = getattr(args, "record", False)
            if is_rec:
                enable_deadband = not getattr(args, "no_filter_deadband", False)
                tui_kwargs: Dict[str, Any] = {
                    "demo": args.demo,
                    "fps": args.fps,
                    "reconnect_delay": args.reconnect_delay,
                    "record": True,
                    "db_path": getattr(args, "db", "railway_history.db"),
                    "enable_deadband": enable_deadband,
                }
                if getattr(args, "retention_days", None) is not None:
                    tui_kwargs["retention_days"] = args.retention_days
                if getattr(args, "archive_db", None) is not None:
                    tui_kwargs["archive_path"] = args.archive_db
                if getattr(args, "ascii", False):
                    tui_kwargs["ascii_mode"] = True
                run_tui_mode(**tui_kwargs)
            else:
                extra_tui_kwargs: Dict[str, Any] = {}
                if getattr(args, "ascii", False):
                    extra_tui_kwargs["ascii_mode"] = True
                run_tui_mode(
                    demo=args.demo,
                    fps=args.fps,
                    reconnect_delay=args.reconnect_delay,
                    **extra_tui_kwargs,
                )
        else:
            is_rec = getattr(args, "record", False)
            if is_rec:
                enable_deadband = not getattr(args, "no_filter_deadband", False)
                stream_kwargs: Dict[str, Any] = {
                    "demo": args.demo,
                    "reconnect_delay": args.reconnect_delay,
                    "record": True,
                    "db_path": getattr(args, "db", "railway_history.db"),
                    "enable_deadband": enable_deadband,
                }
                if getattr(args, "retention_days", None) is not None:
                    stream_kwargs["retention_days"] = args.retention_days
                if getattr(args, "archive_db", None) is not None:
                    stream_kwargs["archive_path"] = args.archive_db
                run_plain_stream_mode(**stream_kwargs)
            else:
                run_plain_stream_mode(
                    demo=args.demo,
                    reconnect_delay=args.reconnect_delay,
                )
        return 0
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
