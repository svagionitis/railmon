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
import re
import shutil
import signal
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from typing import Any, Dict, FrozenSet, Generator, List, Optional, Sequence, Tuple

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


@dataclass(frozen=True)
class CabStreamInfo:
    """Metadata describing a live locomotive forward cab video stream."""

    train_number: str
    has_stream: bool
    status: str  # "ONLINE", "OFFLINE", "UNAVAILABLE", "ERROR"
    stream_type: str = "hls"
    playlist_url: Optional[str] = None
    locomotive_id: Optional[str] = None
    camera_position: str = "FORWARD_CAB"
    resolution: Optional[str] = None
    fps: Optional[int] = None
    bitrate_kbps: Optional[int] = None
    latency_sec: Optional[float] = None
    error_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert CabStreamInfo to a serializable dictionary."""
        return {
            "trainNumber": self.train_number,
            "locomotiveId": self.locomotive_id,
            "hasStream": self.has_stream,
            "status": self.status,
            "cameraPosition": self.camera_position,
            "streamType": self.stream_type,
            "playlistUrl": self.playlist_url,
            "resolution": self.resolution,
            "fps": self.fps,
            "bitrateKbps": self.bitrate_kbps,
            "latencySec": self.latency_sec,
            "errorMessage": self.error_message,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], default_train_number: str = "") -> "CabStreamInfo":
        """Construct CabStreamInfo defensively from an API payload dictionary."""
        raw_has_stream = data.get("hasStream", False)
        raw_url = data.get("playlistUrl") or data.get("url")
        has_stream = bool(raw_has_stream and raw_url)
        status = str(data.get("status", "ONLINE" if has_stream else "UNAVAILABLE")).upper()
        return cls(
            train_number=str(data.get("trainNumber") or default_train_number),
            has_stream=has_stream,
            status=status,
            stream_type=str(data.get("streamType", "hls")).lower(),
            playlist_url=str(raw_url) if raw_url else None,
            locomotive_id=str(data["locomotiveId"]) if data.get("locomotiveId") else None,
            camera_position=str(data.get("cameraPosition", "FORWARD_CAB")),
            resolution=str(data["resolution"]) if data.get("resolution") else None,
            fps=_safe_optional_int(data.get("fps")),
            bitrate_kbps=_safe_optional_int(data.get("bitrateKbps")),
            latency_sec=_safe_optional_float(data.get("latencySec")),
            error_message=str(data["message"]) if data.get("message") else None,
        )

    @classmethod
    def unavailable(cls, train_number: str, message: str = "No video stream available.") -> "CabStreamInfo":
        """Factory for unavailable stream response."""
        return cls(
            train_number=train_number,
            has_stream=False,
            status="UNAVAILABLE",
            error_message=message,
        )


MAX_SSE_FRAME_BYTES: int = 4 * 1024 * 1024  # 4 MiB hard cap per SSE frame (PY-MISRA-02)
MAX_HISTORY_TRAINS_PER_FRAME: int = 500
MAX_HISTORY_POINTS_PER_TRAIN: int = 50
MAX_GHOSTS_PER_FRAME: int = 500
MAX_DELAY_UPDATES_PER_FRAME: int = 500
MIN_VALID_DELAY_MIN: int = -120
MAX_VALID_DELAY_MIN: int = 1440


def _parse_iso_utc_epoch(raw_ts: Any) -> Optional[float]:
    """Parse an ISO-8601 timestamp string into finite UTC POSIX epoch seconds."""
    if not raw_ts or not isinstance(raw_ts, str):
        return None
    cleaned = raw_ts.strip()
    if not cleaned:
        return None
    try:
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        epoch = dt.timestamp()
        return epoch if math.isfinite(epoch) and epoch > 0.0 else None
    except (ValueError, TypeError, OverflowError):
        return None


class SSEEventType(Enum):
    """Supported multiplexed SSE event channels from /api/train-stream (F-10.1)."""

    TRAIN_POSITIONS_UX = "trainPositionsUx"
    TRAIN_POSITIONS = "trainPositions"
    TRAIN_HISTORY = "trainHistory"
    SCHEDULE_CARDS = "scheduleCards"
    DELAY_UPDATES = "delayUpdates"
    ERROR = "error"


@dataclass(frozen=True)
class TrainHistoryPoint:
    """Single trailing breadcrumb point delivered by SSE event: trainHistory."""

    lat: float
    lng: float
    timestamp_epoch: float
    speed: float = 0.0
    course: Optional[float] = None
    track_heading: Optional[float] = None
    iso_timestamp: Optional[str] = None

    @classmethod
    def from_dict(
        cls,
        data: Dict[str, Any],
        fallback_epoch: float,
    ) -> "TrainHistoryPoint":
        """Defensively parse and validate a trainHistory point dictionary."""
        if not isinstance(data, dict):
            raise TypeError(f"TrainHistoryPoint payload must be a dict, got {type(data).__name__}")

        lat = _safe_optional_float(data.get("lat"))
        lng = _safe_optional_float(data.get("lng"))
        if lat is None or lng is None:
            raise ValueError("TrainHistoryPoint missing finite lat/lng")
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lng <= 180.0):
            raise ValueError(f"TrainHistoryPoint out of WGS84 bounds: ({lat}, {lng})")
        if abs(lat) < 1e-9 and abs(lng) < 1e-9:
            raise ValueError("TrainHistoryPoint at Null Island (0.0, 0.0) rejected")

        iso_ts = _safe_optional_str(data.get("ts") or data.get("timestamp") or data.get("receivedAt"))
        parsed_epoch = _parse_iso_utc_epoch(iso_ts)
        if parsed_epoch is None:
            parsed_epoch = _parse_iso_utc_epoch(data.get("receivedAt"))
        epoch = parsed_epoch if parsed_epoch is not None else float(fallback_epoch)
        if not math.isfinite(epoch):
            epoch = float(fallback_epoch)

        speed = max(0.0, _safe_float(data.get("speed"), default=0.0))
        raw_course = _safe_optional_float(data.get("course"))
        course = (raw_course % 360.0) if raw_course is not None else None
        raw_heading = _safe_optional_float(data.get("trackHeading"))
        track_heading = (raw_heading % 360.0) if raw_heading is not None else None

        return cls(
            lat=lat,
            lng=lng,
            timestamp_epoch=epoch,
            speed=speed,
            course=course,
            track_heading=track_heading,
            iso_timestamp=iso_ts,
        )


@dataclass(frozen=True)
class GhostTrain:
    """In-progress scheduled run lacking live GPS telemetry (SSE event: scheduleCards)."""

    id: str
    train_number: str
    schedule_id: Optional[str]
    schedule_number: Optional[str]
    name: Optional[str]
    service_type: Optional[str]
    category: Optional[str]
    origin: Optional[str]
    destination: Optional[str]
    corridor: Optional[str]
    scheduled_departure: Optional[str]
    scheduled_arrival: Optional[str]
    delay: int
    progress: Optional[float]
    schedule_status: str = "in_progress"
    gps_status: str = "none"
    signal_status: str = "none"
    timestamp: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GhostTrain":
        """Defensively parse a scheduleCards ghost object."""
        if not isinstance(data, dict):
            raise TypeError(f"GhostTrain payload must be a dict, got {type(data).__name__}")

        schedule_id = _safe_optional_str(data.get("scheduleId") or data.get("_id"))
        train_num = _safe_optional_str(data.get("trainNumber") or data.get("name"))
        raw_id = _safe_optional_str(data.get("id"))
        if raw_id is None:
            if schedule_id is not None:
                raw_id = f"schedule:{schedule_id}"
            elif train_num is not None:
                raw_id = f"ghost:{train_num}"
        if not raw_id:
            raise ValueError("GhostTrain missing id, scheduleId, and trainNumber")

        resolved_num = train_num or raw_id
        raw_delay = _safe_int(data.get("delay"), default=0)
        delay = max(MIN_VALID_DELAY_MIN, min(MAX_VALID_DELAY_MIN, raw_delay))

        raw_prog = _safe_optional_float(data.get("progress"))
        progress = max(0.0, min(1.0, raw_prog)) if raw_prog is not None else None

        return cls(
            id=raw_id,
            train_number=resolved_num,
            schedule_id=schedule_id,
            schedule_number=_safe_optional_str(data.get("scheduleNumber")),
            name=_safe_optional_str(data.get("name")),
            service_type=_safe_optional_str(data.get("serviceType")),
            category=_safe_optional_str(data.get("category")),
            origin=_safe_optional_str(data.get("origin")),
            destination=_safe_optional_str(data.get("destination")),
            corridor=_safe_optional_str(data.get("corridor")),
            scheduled_departure=_safe_optional_str(data.get("scheduledDeparture")),
            scheduled_arrival=_safe_optional_str(data.get("scheduledArrival")),
            delay=delay,
            progress=progress,
            schedule_status=_safe_optional_str(data.get("scheduleStatus")) or "in_progress",
            gps_status=_safe_optional_str(data.get("gpsStatus")) or "none",
            signal_status=_safe_optional_str(data.get("signalStatus")) or "none",
            timestamp=_safe_optional_str(data.get("timestamp")),
        )


@dataclass(frozen=True)
class DelayUpdate:
    """Incremental train delay delta delivered by SSE event: delayUpdates."""

    train_number: str
    delay: int
    train_id: Optional[str] = None
    schedule_id: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DelayUpdate":
        """Defensively parse a delayUpdates item with finite delay bounds."""
        if not isinstance(data, dict):
            raise TypeError(f"DelayUpdate payload must be a dict, got {type(data).__name__}")

        train_num = _safe_optional_str(data.get("trainNumber"))
        train_id = _safe_optional_str(data.get("trainId") or data.get("id"))
        schedule_id = _safe_optional_str(data.get("scheduleId"))
        if not (train_num or train_id or schedule_id):
            raise ValueError("DelayUpdate missing trainNumber, trainId, and scheduleId")

        raw_delay = _safe_optional_int(data.get("delay"))
        if raw_delay is None:
            raise ValueError("DelayUpdate missing finite integer delay")
        clamped_delay = max(MIN_VALID_DELAY_MIN, min(MAX_VALID_DELAY_MIN, raw_delay))

        return cls(
            train_number=train_num or "",
            delay=clamped_delay,
            train_id=train_id,
            schedule_id=schedule_id,
        )


@dataclass(frozen=True)
class SSEFrame:
    """Immutable multiplexed frame decoded from /api/train-stream (F-10.1)."""

    event_type: SSEEventType
    positions: Tuple[TrainPosition, ...] = ()
    history: Tuple[Tuple[str, Tuple[TrainHistoryPoint, ...]], ...] = ()
    ghosts: Tuple[GhostTrain, ...] = ()
    delays: Tuple[DelayUpdate, ...] = ()
    error_message: Optional[str] = None
    received_at: float = 0.0


def validate_stream_url(url: str) -> str:
    """Validate that stream URL strictly uses http or https scheme.

    Args:
        url: URL string to validate.

    Returns:
        Validated URL string.

    Raises:
        ValueError: If scheme is not http or https, or URL is malformed.
    """
    clean_url = (url or "").strip()
    if not clean_url:
        raise ValueError("Stream URL must not be empty.")
    parts = urllib.parse.urlsplit(clean_url)
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(
            f"Insecure or invalid stream URL scheme: '{parts.scheme}'. Only http and https are allowed."
        )
    if not parts.netloc:
        raise ValueError(f"Invalid stream URL missing hostname: '{clean_url}'")
    return clean_url


def sanitize_train_identifier(identifier: str) -> str:
    """Sanitize train number or identifier against command injection.

    Args:
        identifier: Train number or identifier string.

    Returns:
        Sanitized train number string.

    Raises:
        ValueError: If train number contains illegal characters or is empty.
    """
    clean_id = (identifier or "").strip()
    if not clean_id or not re.match(r"^[A-Za-z0-9\- ]{1,32}$", clean_id):
        raise ValueError(
            f"Invalid train identifier: '{identifier}'. Must be 1-32 alphanumeric characters, dashes, or spaces."
        )
    return clean_id


def detect_video_player(preferred_player: Optional[str] = None) -> Tuple[str, Optional[str]]:
    """Detect available media player executable in prioritized order.

    Priority:
        1. Preferred override (preferred_player or RAILMON_PLAYER env var)
        2. mpv
        3. ffplay
        4. vlc
        5. browser fallback

    Returns:
        Tuple of (player_name, binary_path). If player is browser, binary_path is None.
    """
    override = preferred_player or os.environ.get("RAILMON_PLAYER")
    if override:
        clean_override = override.strip()
        if clean_override.lower() == "browser":
            return ("browser", None)
        path = shutil.which(clean_override)
        if path:
            return (clean_override.lower(), path)
        if os.path.isfile(clean_override) and os.access(clean_override, os.X_OK):
            return (os.path.basename(clean_override).lower(), clean_override)

    for player in ("mpv", "ffplay", "vlc"):
        path = shutil.which(player)
        if path:
            return (player, path)

    return ("browser", None)


def build_player_command(
    player_name: str,
    binary_path: Optional[str],
    playlist_url: str,
    train_number: str,
) -> List[str]:
    """Construct argument list for external media player execution.

    Args:
        player_name: Name of player ('mpv', 'ffplay', 'vlc', or custom).
        binary_path: Resolved binary path or command name.
        playlist_url: Validated HLS playlist URL.
        train_number: Clean train number string.

    Returns:
        List of command arguments.
    """
    exe = binary_path or player_name
    title = f"railmon: {train_number} Forward Cab View"
    p_lower = player_name.lower()

    if "mpv" in p_lower:
        return [
            exe,
            f"--title={title}",
            "--geometry=960x540-20+40",
            "--no-terminal",
            "--force-window=yes",
            "--ontop",
            playlist_url,
        ]
    elif "ffplay" in p_lower:
        return [
            exe,
            "-window_title",
            title,
            "-autoexit",
            "-loglevel",
            "error",
            "-x",
            "960",
            "-y",
            "540",
            playlist_url,
        ]
    elif "vlc" in p_lower:
        return [
            exe,
            f"--meta-title={title}",
            "--play-and-exit",
            "--video-on-top",
            playlist_url,
        ]
    else:
        return [exe, playlist_url]


def launch_detached_player(
    cmd_args: List[str],
    is_windows: Optional[bool] = None,
) -> subprocess.Popen[Any]:
    """Launch a media player subprocess fully detached from the terminal session.

    Args:
        cmd_args: List of command arguments.
        is_windows: Optional platform override (defaults to sys.platform == 'win32').

    Returns:
        Running subprocess.Popen instance.
    """
    if is_windows is None:
        is_windows = (sys.platform == "win32")

    if is_windows:
        detached_flags = (
            getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
        return subprocess.Popen(
            cmd_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=detached_flags,
            close_fds=True,
        )
    else:
        return subprocess.Popen(
            cmd_args,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )


def launch_cab_stream(
    stream_info: CabStreamInfo,
    preferred_player: Optional[str] = None,
) -> Tuple[bool, str, str]:
    """Orchestrate player detection, validation, and detached playback launch.

    Args:
        stream_info: Populated CabStreamInfo with valid playlist URL.
        preferred_player: Optional player executable name or 'browser'.

    Returns:
        Tuple of (success: bool, player_name: str, message: str).
    """
    if not stream_info.has_stream or not stream_info.playlist_url:
        return (False, "none", stream_info.error_message or "No stream available.")

    try:
        url = validate_stream_url(stream_info.playlist_url)
        train_num = sanitize_train_identifier(stream_info.train_number)
    except ValueError as exc:
        return (False, "invalid", str(exc))

    player_name, binary_path = detect_video_player(preferred_player)

    if player_name == "browser":
        try:
            opened = webbrowser.open(url)
            if opened:
                return (True, "browser", f"Opened stream in default web browser for train {train_num}.")
            return (False, "browser", "Failed to launch default web browser.")
        except Exception as exc:
            return (False, "browser", f"Web browser launch error: {str(exc)}")

    try:
        cmd = build_player_command(player_name, binary_path, url, train_num)
        launch_detached_player(cmd)
        return (True, player_name, f"Launched {player_name} for train {train_num}.")
    except Exception as exc:
        try:
            webbrowser.open(url)
            return (True, "browser", f"Player '{player_name}' failed ({exc}); fell back to web browser.")
        except Exception:
            return (False, player_name, f"Failed to execute '{player_name}': {str(exc)}")


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

    def get_cab_stream_url(self, train_id_or_number: str) -> CabStreamInfo:
        """Fetch live locomotive forward cab stream metadata for a train.

        Args:
            train_id_or_number: Train ID or commercial train number (e.g. '1597', 'IC52').

        Returns:
            CabStreamInfo populated with playlist URL and status.
        """
        clean_id = urllib.parse.quote(str(train_id_or_number).strip())
        try:
            payload = self._get(f"/api/public/trains/{clean_id}/stream")
            if isinstance(payload, dict):
                return CabStreamInfo.from_dict(payload, default_train_number=train_id_or_number)
            return CabStreamInfo.unavailable(train_id_or_number, "Invalid stream response format.")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return CabStreamInfo.unavailable(
                    train_id_or_number,
                    f"No forward cab camera installed or stream active for train {train_id_or_number}.",
                )
            return CabStreamInfo(
                train_number=str(train_id_or_number),
                has_stream=False,
                status="ERROR",
                error_message=f"HTTP {exc.code}: {exc.reason}",
            )
        except Exception as exc:
            return CabStreamInfo(
                train_number=str(train_id_or_number),
                has_stream=False,
                status="ERROR",
                error_message=f"Network error: {str(exc)}",
            )

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
    def _decode_sse_history(
        payload_str: str,
        now: float,
    ) -> Optional[Tuple[Tuple[str, Tuple[TrainHistoryPoint, ...]], ...]]:
        """Parse SSE event: trainHistory JSON into immutable per-train point tuples."""
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping malformed trainHistory JSON payload: %s", exc)
            return None

        if not isinstance(payload, dict):
            return None

        raw_history = payload.get("history")
        if not isinstance(raw_history, dict):
            return None

        parsed_entries: List[Tuple[str, Tuple[TrainHistoryPoint, ...]]] = []
        for idx, (raw_tid, raw_points) in enumerate(raw_history.items()):
            if idx >= MAX_HISTORY_TRAINS_PER_FRAME:
                break
            tid = str(raw_tid).strip()
            if not tid or not isinstance(raw_points, list):
                continue
            trimmed = raw_points[-MAX_HISTORY_POINTS_PER_TRAIN:]
            pts: List[TrainHistoryPoint] = []
            total_pts = len(trimmed)
            for p_idx, item in enumerate(trimmed):
                if not isinstance(item, dict):
                    continue
                fallback_ts = now - float(total_pts - p_idx)
                try:
                    pts.append(TrainHistoryPoint.from_dict(item, fallback_epoch=fallback_ts))
                except (ValueError, TypeError) as exc:
                    logger.debug("Skipping invalid trainHistory point for %s: %s", tid, exc)
            if pts:
                pts.sort(key=lambda pt: pt.timestamp_epoch)
                parsed_entries.append((tid, tuple(pts)))
        return tuple(parsed_entries)

    @staticmethod
    def _decode_sse_schedule_cards(payload_str: str) -> Optional[Tuple[GhostTrain, ...]]:
        """Parse SSE event: scheduleCards JSON into a tuple of GhostTrain records."""
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping malformed scheduleCards JSON payload: %s", exc)
            return None

        if not isinstance(payload, dict):
            return None

        raw_ghosts = payload.get("ghosts")
        if not isinstance(raw_ghosts, list):
            return None

        ghosts: List[GhostTrain] = []
        for item in raw_ghosts[:MAX_GHOSTS_PER_FRAME]:
            if not isinstance(item, dict):
                continue
            try:
                ghosts.append(GhostTrain.from_dict(item))
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping invalid scheduleCards ghost item: %s", exc)
        return tuple(ghosts)

    @staticmethod
    def _decode_sse_delay_updates(payload_str: str) -> Optional[Tuple[DelayUpdate, ...]]:
        """Parse SSE event: delayUpdates JSON into a tuple of DelayUpdate items."""
        try:
            payload = json.loads(payload_str)
        except json.JSONDecodeError as exc:
            logger.debug("Skipping malformed delayUpdates JSON payload: %s", exc)
            return None

        if not isinstance(payload, dict):
            return None

        raw_delays = payload.get("delays")
        if not isinstance(raw_delays, list):
            return None

        updates: List[DelayUpdate] = []
        for item in raw_delays[:MAX_DELAY_UPDATES_PER_FRAME]:
            if not isinstance(item, dict):
                continue
            try:
                updates.append(DelayUpdate.from_dict(item))
            except (ValueError, TypeError) as exc:
                logger.debug("Skipping invalid delayUpdates item: %s", exc)
        return tuple(updates)

    @staticmethod
    def _decode_sse_error(payload_str: str) -> str:
        """Extract a concise diagnostic string from an SSE event: error payload."""
        try:
            payload = json.loads(payload_str)
            if isinstance(payload, dict):
                msg = payload.get("error") or payload.get("message") or payload_str
                return str(msg)[:256]
            return str(payload)[:256]
        except json.JSONDecodeError:
            return payload_str[:256]

    @classmethod
    def _decode_sse_frame(
        cls,
        event_name: str,
        payload_str: str,
        include_raw_positions: bool,
        now: float,
    ) -> Optional[SSEFrame]:
        """Decode a single completed SSE event into a typed SSEFrame, or None if ignored."""
        if event_name == SSEEventType.TRAIN_POSITIONS_UX.value:
            trains = cls._decode_sse_positions(payload_str)
            if trains is None:
                return None
            return SSEFrame(
                event_type=SSEEventType.TRAIN_POSITIONS_UX,
                positions=tuple(trains),
                received_at=now,
            )

        if event_name == SSEEventType.TRAIN_POSITIONS.value:
            if not include_raw_positions:
                return None
            trains = cls._decode_sse_positions(payload_str)
            if trains is None:
                return None
            return SSEFrame(
                event_type=SSEEventType.TRAIN_POSITIONS,
                positions=tuple(trains),
                received_at=now,
            )

        if event_name == SSEEventType.TRAIN_HISTORY.value:
            history = cls._decode_sse_history(payload_str, now=now)
            if history is None:
                return None
            return SSEFrame(
                event_type=SSEEventType.TRAIN_HISTORY,
                history=history,
                received_at=now,
            )

        if event_name == SSEEventType.SCHEDULE_CARDS.value:
            ghosts = cls._decode_sse_schedule_cards(payload_str)
            if ghosts is None:
                return None
            return SSEFrame(
                event_type=SSEEventType.SCHEDULE_CARDS,
                ghosts=ghosts,
                received_at=now,
            )

        if event_name == SSEEventType.DELAY_UPDATES.value:
            delays = cls._decode_sse_delay_updates(payload_str)
            if delays is None:
                return None
            return SSEFrame(
                event_type=SSEEventType.DELAY_UPDATES,
                delays=delays,
                received_at=now,
            )

        if event_name == SSEEventType.ERROR.value:
            return SSEFrame(
                event_type=SSEEventType.ERROR,
                error_message=cls._decode_sse_error(payload_str),
                received_at=now,
            )

        logger.debug("Ignoring unrecognized SSE event channel: %r", event_name)
        return None

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

    def _iter_sse_frames(
        self,
        resp: Any,
        stop_event: Optional[threading.Event],
        include_raw_positions: bool = False,
    ) -> Generator[SSEFrame, None, None]:
        """Iterate over raw SSE HTTP response lines and yield bounded multiplexed SSEFrames."""
        current_event = "message"
        data_buffer: List[str] = []
        buffered_bytes: int = 0
        oversized_frame: bool = False

        for raw_line in resp:
            if stop_event is not None and stop_event.is_set():
                return

            line = raw_line.decode("utf-8", errors="ignore").strip()
            if not line:
                if data_buffer and not oversized_frame:
                    frame = self._decode_sse_frame(
                        event_name=current_event,
                        payload_str="".join(data_buffer),
                        include_raw_positions=include_raw_positions,
                        now=time.time(),
                    )
                    if frame is not None:
                        yield frame
                data_buffer = []
                buffered_bytes = 0
                oversized_frame = False
                current_event = "message"
            elif line.startswith(":"):
                continue  # W3C SSE comment / keep-alive heartbeat
            elif line.startswith("event:"):
                current_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                chunk = line.split(":", 1)[1].strip()
                buffered_bytes += len(chunk.encode("utf-8", errors="ignore"))
                if buffered_bytes > MAX_SSE_FRAME_BYTES:
                    if not oversized_frame:
                        logger.warning(
                            "Dropping oversized SSE '%s' frame exceeding %d bytes.",
                            current_event,
                            MAX_SSE_FRAME_BYTES,
                        )
                    oversized_frame = True
                    data_buffer = []
                elif not oversized_frame:
                    data_buffer.append(chunk)

    def _iter_sse_payloads(
        self,
        resp: Any,
        event_filter: str,
        stop_event: Optional[threading.Event],
    ) -> Generator[List[TrainPosition], None, None]:
        """Iterate over raw SSE HTTP response lines and yield decoded TrainPosition batches."""
        include_raw = event_filter == SSEEventType.TRAIN_POSITIONS.value
        for frame in self._iter_sse_frames(
            resp=resp,
            stop_event=stop_event,
            include_raw_positions=include_raw,
        ):
            if frame.event_type.value == event_filter:
                yield list(frame.positions)

    def stream_events(
        self,
        include_raw_positions: bool = False,
        reconnect_delay: float = 3.0,
        stop_event: Optional[threading.Event] = None,
        timeout: float = 10.0,
    ) -> Generator[SSEFrame, None, None]:
        """Stream multiplexed SSE frames (positions, history, ghost cards, delays, errors).

        Args:
            include_raw_positions: Whether to also emit raw 'trainPositions' frames alongside
                smoothed 'trainPositionsUx' frames (default: False).
            reconnect_delay: Base seconds to wait before reconnecting on error. If <= 0,
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
                    for frame in self._iter_sse_frames(
                        resp=resp,
                        stop_event=stop_event,
                        include_raw_positions=include_raw_positions,
                    ):
                        if consecutive_failures > 0 and disconnect_time is not None:
                            logger.info(
                                "Upstream SSE connection re-established after %.1fs outage.",
                                time.monotonic() - disconnect_time,
                            )
                            consecutive_failures = 0
                            disconnect_time = None
                        yield frame
                        attempt = 0

                    if stop_event is not None and stop_event.is_set():
                        return
                    if reconnect_delay <= 0.0:
                        return

            except (urllib.error.URLError, TimeoutError, OSError) as e:
                if stop_event is not None and stop_event.is_set():
                    return
                if reconnect_delay <= 0.0:
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
        include_raw = event_filter == SSEEventType.TRAIN_POSITIONS.value
        for frame in self.stream_events(
            include_raw_positions=include_raw,
            reconnect_delay=reconnect_delay,
            stop_event=stop_event,
            timeout=timeout,
        ):
            if frame.event_type.value == event_filter:
                yield list(frame.positions)


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
        "--theme",
        type=str,
        default=None,
        choices=["azure", "amber", "matrix", "solarized", "monochrome"],
        help="Initial terminal color theme profile for TUI dashboard (default: azure or $RAILMON_THEME).",
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

    # 5. cab (F-7.1)
    p_cab = subparsers.add_parser(
        "cab",
        aliases=["stream-cab"],
        help="Launch or inspect live locomotive forward cab video stream.",
        description="Resolve and launch real-time forward cab camera video feeds for active trains.",
    )
    p_cab.add_argument(
        "train_number",
        type=str,
        help="Commercial train number or ID (e.g. '1597', 'IC52', '1599').",
    )
    p_cab.add_argument(
        "--player",
        type=str,
        default=None,
        help="Force player executable (e.g. 'mpv', 'ffplay', 'vlc', 'browser').",
    )
    p_cab.add_argument(
        "--url-only",
        action="store_true",
        default=False,
        help="Print raw HLS playlist URL (.m3u8) to stdout and exit.",
    )
    p_cab.add_argument(
        "--browser",
        action="store_true",
        default=False,
        help="Launch video stream directly in default web browser.",
    )
    p_cab.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured stream metadata JSON to stdout.",
    )
    p_cab.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="Run in offline mock mode with simulated camera feeds.",
    )
    p_cab.add_argument(
        "--ascii",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force pure 7-bit ASCII rendering fallback.",
    )

    # 6. infrastructure / geo (F-7.2)
    p_infra = subparsers.add_parser(
        "infrastructure",
        aliases=["infra", "geo"],
        help="Query and inspect railway GIS infrastructure assets (tunnels, bridges, signals).",
        description="Query and display railway civil and operational infrastructure features.",
    )
    p_infra.add_argument(
        "--type",
        "-t",
        dest="feature_type",
        choices=["all", "tunnel", "bridge", "signal"],
        default="all",
        help="Filter features by asset type (default: all).",
    )
    p_infra.add_argument(
        "--corridor",
        "-c",
        type=str,
        default=None,
        help="Filter features by railway corridor (e.g. 'pathe-mainline', 'proastiakos').",
    )
    p_infra.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured JSON summary to stdout.",
    )
    p_infra.add_argument(
        "--geojson",
        action="store_true",
        default=False,
        help="Output standard GeoJSON FeatureCollection to stdout.",
    )
    p_infra.add_argument(
        "--ascii",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force pure 7-bit ASCII rendering fallback.",
    )

    # 6b. speed-limits & slow-orders (F-5.3)
    p_spd = subparsers.add_parser(
        "speed-limits",
        aliases=["speeds", "speed_limits"],
        help="Query and inspect railway corridor speed limits and restrictions (F-5.3).",
        description="Query and display railway corridor line speeds, segment limits, and slow orders.",
    )
    p_spd.add_argument(
        "--corridor",
        "-c",
        type=str,
        default=None,
        help="Filter speed limits by corridor (e.g. 'pathe-mainline', 'proastiakos').",
    )
    p_spd.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured JSON summary to stdout.",
    )
    p_spd.add_argument(
        "--ascii",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force pure 7-bit ASCII rendering fallback.",
    )

    p_tsr = subparsers.add_parser(
        "slow-orders",
        aliases=["tsr", "slow_orders"],
        help="Query active Temporary Speed Restrictions (TSR / Slow Orders) (F-5.3).",
        description="Query and display active rail slow orders (civil maintenance speed restrictions).",
    )
    p_tsr.add_argument(
        "--corridor",
        "-c",
        type=str,
        default=None,
        help="Filter slow orders by corridor.",
    )
    p_tsr.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured JSON list of slow orders.",
    )
    p_tsr.add_argument(
        "--ascii",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force pure 7-bit ASCII rendering fallback.",
    )

    # 6c. sensor-health & telemetry watchdog (F-5.2)
    p_health = subparsers.add_parser(
        "sensor-health",
        aliases=["health", "sensor_health"],
        help="Audit fleet telemetry freshness and sensor freeze watchdog states (F-5.2).",
        description="Query and display fleet-wide telemetry sensor health, drops, freezes, and station dwells.",
    )
    p_health.add_argument(
        "--alerts-only",
        action="store_true",
        default=False,
        help="Only display trains with telemetry drops, sensor freezes, or stale packets.",
    )
    p_health.add_argument(
        "--corridor",
        "-c",
        type=str,
        default=None,
        help="Filter trains by corridor name or substring.",
    )
    p_health.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="Use simulated mock fleet telemetry for diagnostic verification.",
    )
    p_health.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured JSON fleet health report to stdout.",
    )
    p_health.add_argument(
        "--ascii",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force pure 7-bit ASCII rendering fallback.",
    )

    # 6d. conflicts & headway watchdog (F-5.1)
    p_conflict = subparsers.add_parser(
        "conflicts",
        aliases=["conflict", "watchdog_conflicts"],
        help="Audit active corridor headway compression and opposing train collision risks (F-5.1).",
        description="Evaluate pairwise track conflicts, closing velocities, and passing siding meets across Greek corridors.",
    )
    p_conflict.add_argument(
        "--corridor",
        "-c",
        type=str,
        default=None,
        help="Filter conflicts by corridor name or identifier (e.g. 'pathe-mainline', 'proastiakos-chalkida').",
    )
    p_conflict.add_argument(
        "--critical-only",
        action="store_true",
        default=False,
        help="Only display CRITICAL severity collision and emergency headway alerts.",
    )
    p_conflict.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output structured JSON conflict report to stdout.",
    )
    p_conflict.add_argument(
        "--ascii",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Force pure 7-bit ASCII rendering fallback.",
    )

    # 7. watch (F-6.3)
    p_watch = subparsers.add_parser(
        "watch",
        help="Monitor a train trip and issue proximity and delay push alerts.",
        description="Background terminal watchdog monitoring train progress, delay spikes, and station proximity.",
    )
    p_watch.add_argument(
        "train_number",
        type=str,
        help="Commercial train identifier to monitor (e.g. 'IC52', '1597').",
    )
    p_watch.add_argument(
        "--dest",
        "-d",
        type=str,
        default=None,
        help="Destination station name, code, or ID.",
    )
    p_watch.add_argument(
        "--notify",
        "-n",
        type=str,
        default="15m",
        help="Proximity notification ETA threshold (e.g. '15m', '10m', '30m', default: 15m).",
    )
    p_watch.add_argument(
        "--distance",
        type=float,
        default=15.0,
        help="Proximity distance threshold in km (default: 15.0 km).",
    )
    p_watch.add_argument(
        "--delay-delta",
        type=float,
        default=3.0,
        help="Delay increase threshold in minutes triggering an alert (default: 3.0m).",
    )
    p_watch.add_argument(
        "--webhook",
        type=str,
        default=None,
        help="Outbound webhook URL for alert delivery (Telegram, Discord, Slack, or Generic HTTP POST).",
    )
    p_watch.add_argument(
        "--webhook-format",
        choices=["auto", "generic", "telegram", "discord", "slack"],
        default="auto",
        help="Webhook payload format (default: auto).",
    )
    p_watch.add_argument(
        "--webhook-chat-id",
        type=str,
        default=None,
        help="Telegram target chat_id (when using Telegram webhook).",
    )
    p_watch.add_argument(
        "--webhook-token",
        type=str,
        default=None,
        help="Bearer authorization token for webhook delivery.",
    )
    p_watch.add_argument(
        "--no-bell",
        action="store_true",
        default=False,
        help="Disable terminal audio bell.",
    )
    p_watch.add_argument(
        "--no-desktop",
        action="store_true",
        default=False,
        help="Disable OS native desktop notifications.",
    )
    p_watch.add_argument(
        "--once",
        action="store_true",
        default=False,
        help="Exit automatically when train reaches destination station.",
    )
    p_watch.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Output stream of JSON alert event records to stdout.",
    )
    p_watch.add_argument(
        "--headless",
        "--daemon",
        action="store_true",
        default=False,
        help="Run in quiet background daemon mode without interactive terminal UI.",
    )
    p_watch.add_argument(
        "--demo",
        action="store_true",
        default=False,
        help="Run against simulated synthetic train movements.",
    )
    p_watch.add_argument(
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

    # history bottlenecks (F-9.1)
    p_bottlenecks = hist_subs.add_parser(
        "bottlenecks",
        parents=[db_parent],
        help="Analyze corridor bottlenecks, station dwell delays, and infrastructure heatmaps.",
    )
    p_bottlenecks.add_argument("--corridor", type=str, help="Filter by corridor name (e.g. PATHE, Proastiakos).")
    p_bottlenecks.add_argument("--days", type=int, help="Analyze the last N days of recorded telemetry.")
    p_bottlenecks.add_argument("--start-date", type=str, help="Start date filter (YYYY-MM-DD).")
    p_bottlenecks.add_argument("--end-date", type=str, help="End date filter (YYYY-MM-DD).")
    p_bottlenecks.add_argument(
        "--type",
        choices=["all", "intercity", "suburban", "regional"],
        default="all",
        help="Filter by train service type.",
    )
    p_bottlenecks.add_argument("--min-trips", type=int, default=3, help="Minimum evaluated trips per node (default: 3).")
    p_bottlenecks.add_argument("--limit", type=int, default=15, help="Maximum bottleneck nodes to display (default: 15).")
    p_bottlenecks.add_argument("--ascii", action="store_true", help="Force 7-bit ASCII characters without Unicode blocks.")
    p_bottlenecks.add_argument("--json", action="store_true", help="Output machine-readable JSON format.")

    # history segments (F-9.2)
    p_segments = hist_subs.add_parser(
        "segments",
        parents=[db_parent],
        help="Analyze empirical inter-station segment transit durations and percentiles.",
    )
    p_segments.add_argument("--corridor", type=str, help="Filter by corridor name (e.g. PATHE, Proastiakos).")
    p_segments.add_argument("--from", dest="from_station", type=str, help="Filter by origin station name.")
    p_segments.add_argument("--to", dest="to_station", type=str, help="Filter by destination station name.")
    p_segments.add_argument("--days", type=int, help="Analyze the last N days of recorded telemetry.")
    p_segments.add_argument("--start-date", type=str, help="Start date filter (YYYY-MM-DD).")
    p_segments.add_argument("--end-date", type=str, help="End date filter (YYYY-MM-DD).")
    p_segments.add_argument(
        "--type",
        choices=["all", "intercity", "suburban", "regional"],
        default="all",
        help="Filter by train service type.",
    )
    p_segments.add_argument("--min-trips", type=int, default=3, help="Minimum evaluated trips per segment (default: 3).")
    p_segments.add_argument("--limit", type=int, default=25, help="Maximum segment entries to display (default: 25).")
    p_segments.add_argument("--ascii", action="store_true", help="Force 7-bit ASCII characters.")
    p_segments.add_argument("--json", action="store_true", help="Output machine-readable JSON format.")

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

    # 5. serve (F-8.1: Embedded Zero-Dependency Web Map Daemon)
    p_serve = subparsers.add_parser(
        "serve",
        aliases=["server", "web"],
        help="Launch embedded zero-dependency web map daemon for browser visualization.",
        description=(
            "Start a multi-threaded HTTP server daemon serving an interactive "
            "Leaflet GIS map dashboard of the Greek railway network with real-time "
            "Server-Sent Events (SSE) telemetry, conflict warnings, and station inspectors."
        ),
    )
    p_serve.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Network bind interface IP address (default: 127.0.0.1; use 0.0.0.0 for LAN access).",
    )
    p_serve.add_argument(
        "--port",
        type=int,
        default=8080,
        help="TCP port to bind web map HTTP server (default: 8080).",
    )
    p_serve.add_argument(
        "--demo",
        "--mock",
        action="store_true",
        default=False,
        help="Run in offline demonstration mode with synthetic train telemetry simulation.",
    )
    p_serve.add_argument(
        "--record",
        action="store_true",
        default=False,
        help="Record incoming live telemetry concurrently to SQLite WAL database.",
    )
    p_serve.add_argument(
        "--db",
        type=str,
        default="railway_history.db",
        help="Path to SQLite database for recording or query inspection (default: railway_history.db).",
    )
    p_serve.add_argument(
        "--replay",
        type=str,
        default=None,
        help="Replay historical telemetry from a recorded SQLite WAL database.",
    )
    p_serve.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Playback speed multiplier for historical replay mode (default: 1.0; e.g. 2.0, 5.0, 10.0).",
    )
    p_serve.add_argument(
        "--open-browser",
        "-b",
        action="store_true",
        default=False,
        help="Automatically launch default web browser pointing to the web map dashboard on startup.",
    )
    p_serve.add_argument(
        "--cors",
        action="store_true",
        default=False,
        help="Enable permissive Cross-Origin Resource Sharing (Access-Control-Allow-Origin: *).",
    )
    p_serve.add_argument(
        "--tile-provider",
        type=str,
        choices=["carto-dark", "carto-light", "osm"],
        default="carto-dark",
        help="Cartographic base map tile layer provider (default: carto-dark).",
    )
    p_serve.add_argument(
        "--reconnect-delay",
        type=float,
        default=3.0,
        help="Base reconnection delay in seconds for live SSE upstream connection (default: 3.0s).",
    )
    p_serve.add_argument(
        "--no-filter-deadband",
        action="store_true",
        default=False,
        help="Disable stationary coordinate noise suppression deadband filter during recording.",
    )
    p_serve.add_argument(
        "--retention-days",
        type=_positive_int,
        default=None,
        help="Automatically prune recorded observations older than N days on startup.",
    )
    p_serve.add_argument(
        "--archive-path",
        type=str,
        default=None,
        help="Export pruned observations to gzipped JSONL archive before deletion.",
    )
    p_serve.add_argument(
        "--max-connections",
        type=int,
        default=64,
        help="Maximum concurrent HTTP connections before returning HTTP 503 (default: 64).",
    )
    p_serve.add_argument(
        "--max-sse-clients",
        type=int,
        default=32,
        help="Maximum concurrent /api/stream SSE browser subscribers (default: 32).",
    )
    p_serve.add_argument(
        "--request-timeout",
        type=float,
        default=10.0,
        help="HTTP socket and header read/write timeout in seconds (default: 10.0s).",
    )
    p_serve.add_argument(
        "--allowed-hosts",
        type=str,
        default=None,
        help="Comma-separated Host header allowlist (e.g. 'railmon.local,192.168.1.50') for DNS-rebinding/reverse-proxy defense.",
    )

    # 6. metrics (F-8.2: Prometheus & OpenMetrics Operational Exporter)
    p_metrics = subparsers.add_parser(
        "metrics",
        aliases=["exporter", "prometheus"],
        help="Launch Prometheus & OpenMetrics operational telemetry exporter daemon.",
        description=(
            "Start a lightweight multi-threaded HTTP server daemon exposing real-time Greek rail "
            "operational metrics conforming to Prometheus 0.0.4 and OpenMetrics 1.0.0 "
            "standards on port 9100 for scraping by Prometheus, VictoriaMetrics, or Grafana."
        ),
    )
    p_metrics.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Network bind interface IP address (default: 127.0.0.1; use 0.0.0.0 for LAN access).",
    )
    p_metrics.add_argument(
        "--port",
        type=int,
        default=9100,
        help="TCP port to bind Prometheus exporter HTTP server (default: 9100).",
    )
    p_metrics.add_argument(
        "--demo",
        "--mock",
        action="store_true",
        default=False,
        help="Run exporter using offline synthetic train telemetry simulation feed.",
    )
    p_metrics.add_argument(
        "--db",
        type=str,
        default="railway_history.db",
        help="Path to SQLite WAL database for historical and storage metric reporting (default: railway_history.db).",
    )
    p_metrics.add_argument(
        "--no-db",
        action="store_true",
        default=False,
        help="Disable SQLite storage metrics collection even if database file exists.",
    )
    p_metrics.add_argument(
        "--reconnect-delay",
        type=float,
        default=3.0,
        help="Base reconnection delay in seconds for live SSE upstream connection (default: 3.0s).",
    )
    p_metrics.add_argument(
        "--no-filter-deadband",
        action="store_true",
        default=False,
        help="Disable deadband filtering for upstream coordinate ingestion.",
    )
    p_metrics.add_argument(
        "--no-gzip",
        action="store_true",
        default=False,
        help="Disable transparent Gzip compression on /metrics scrape responses.",
    )
    p_metrics.add_argument(
        "--scrape-timeout",
        type=float,
        default=10.0,
        help="Scrape timeout deadline in seconds (default: 10.0s).",
    )
    p_metrics.add_argument(
        "--cors",
        action="store_true",
        default=False,
        help="Enable permissive Cross-Origin Resource Sharing (Access-Control-Allow-Origin: *).",
    )
    p_metrics.add_argument(
        "--max-connections",
        type=int,
        default=64,
        help="Maximum concurrent HTTP scraper connections before returning HTTP 503 (default: 64).",
    )
    p_metrics.add_argument(
        "--allowed-hosts",
        type=str,
        default=None,
        help="Comma-separated Host header allowlist (e.g. 'railmon.local,192.168.1.50') for DNS-rebinding/reverse-proxy defense.",
    )

    # 7. export / gtfs-rt (F-8.3: GTFS-Realtime v2.0 Protocol Buffer Feed Exporter)
    _add_export_subparser(subparsers)

    return parser


def _add_export_subparser(subparsers: Any) -> None:
    """Register the `export` / `gtfs-rt` CLI subcommand parser (F-8.3)."""
    p_export = subparsers.add_parser(
        "export",
        aliases=["gtfs-rt", "gtfsrt"],
        help="Export live or historical telemetry to GTFS-Realtime (GTFS-RT v2.0) Protocol Buffers.",
        description=(
            "Translate Hellenic Railways live SSE telemetry, synthetic demo runs, or SQLite "
            "recordings into standard GTFS-Realtime v2.0 Protocol Buffers (.pb) or ProtoJSON "
            "feeds (VehiclePositions, TripUpdates, Service Alerts) with zero third-party deps."
        ),
    )
    p_export.add_argument(
        "--gtfs-rt",
        action="store_true",
        default=False,
        help="Enable GTFS-Realtime v2.0 Protocol Buffer feed export.",
    )
    p_export.add_argument(
        "--feed",
        choices=["combined", "vehicles", "trips", "alerts"],
        default="combined",
        help="GTFS-RT entity scope to emit (default: combined).",
    )
    p_export.add_argument(
        "--format",
        dest="output_format",
        choices=["pb", "json"],
        default="pb",
        help="Output format: binary protobuf ('pb') or ProtoJSON ('json', default: pb).",
    )
    p_export.add_argument(
        "--out",
        "-o",
        type=str,
        default=None,
        help="Destination file path (use '-' to emit raw binary or JSON to stdout).",
    )
    p_export.add_argument(
        "--out-dir",
        type=str,
        default=None,
        help="Directory for writing split feeds (feed, vehicle_positions, trip_updates, alerts).",
    )
    p_export.add_argument(
        "--watch",
        action="store_true",
        default=False,
        help="Continuously refresh --out or --out-dir files every --interval seconds.",
    )
    p_export.add_argument(
        "--interval",
        type=float,
        default=10.0,
        help="Refresh cadence in seconds for --watch mode (default: 10.0s).",
    )
    p_export.add_argument(
        "--serve",
        action="store_true",
        default=False,
        help="Launch embedded multi-threaded HTTP server exposing /gtfs-rt/*.pb endpoints.",
    )
    p_export.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Network bind interface IP for --serve (default: 127.0.0.1).",
    )
    p_export.add_argument(
        "--port",
        type=int,
        default=8085,
        help="TCP port for --serve GTFS-RT HTTP daemon (default: 8085).",
    )
    p_export.add_argument(
        "--from-db",
        type=str,
        default=None,
        help="Reconstruct GTFS-RT snapshot from a recorded SQLite WAL database.",
    )
    p_export.add_argument(
        "--at",
        type=str,
        default=None,
        help="Optional ISO-8601 UTC timestamp for --from-db point-in-time snapshot.",
    )
    p_export.add_argument(
        "--window-sec",
        type=int,
        default=120,
        help="Lookback window in seconds for --from-db snapshots (default: 120s).",
    )
    p_export.add_argument(
        "--inspect",
        type=str,
        default=None,
        help="Decode an existing binary GTFS-RT .pb file and print ProtoJSON summary.",
    )
    p_export.add_argument(
        "--demo",
        "--mock",
        action="store_true",
        default=False,
        help="Run using offline synthetic Greek rail telemetry simulation.",
    )
    p_export.add_argument(
        "--corridor",
        "-c",
        type=str,
        default=None,
        help="Filter exported entities to a corridor (e.g. PATHE, PROASTIAKOS).",
    )
    p_export.add_argument(
        "--enrich-schedules",
        action="store_true",
        default=False,
        help="Query railway.gov.gr timetable REST API (TTL-cached) for full stop predictions.",
    )
    p_export.add_argument(
        "--include-freight",
        action="store_true",
        default=False,
        help="Include freight/maintenance trains in exported GTFS-RT feed.",
    )
    p_export.add_argument(
        "--include-unassigned",
        action="store_true",
        default=False,
        help="Include unassigned depot/shunting locomotives in exported GTFS-RT feed.",
    )
    p_export.add_argument(
        "--no-alerts",
        action="store_true",
        default=False,
        help="Disable GTFS-RT Service Alert entity synthesis.",
    )
    p_export.add_argument(
        "--delay-alert-min",
        type=int,
        default=15,
        help="Delay threshold in minutes triggering SIGNIFICANT_DELAYS alerts (default: 15).",
    )
    p_export.add_argument(
        "--stop-id-field",
        choices=["id", "code"],
        default="id",
        help="Station attribute emitted as GTFS stop_id ('id' or 'code', default: id).",
    )
    p_export.add_argument(
        "--agency-id",
        type=str,
        default="OSE",
        help="GTFS agency_id value for service alert selectors (default: OSE).",
    )
    p_export.add_argument(
        "--feed-version",
        type=str,
        default=None,
        help="Optional FeedHeader.feed_version string aligned with static GTFS.",
    )
    p_export.add_argument(
        "--cors",
        action="store_true",
        default=False,
        help="Enable permissive CORS headers on --serve endpoints.",
    )
    p_export.add_argument(
        "--no-gzip",
        action="store_true",
        default=False,
        help="Disable transparent Gzip compression in --serve mode.",
    )
    p_export.add_argument(
        "--reconnect-delay",
        type=float,
        default=3.0,
        help="Base reconnection delay in seconds for live SSE upstream stream (default: 3.0s).",
    )
    p_export.add_argument(
        "--max-connections",
        type=int,
        default=64,
        help="Maximum concurrent HTTP connections in --serve mode before returning HTTP 503 (default: 64).",
    )
    p_export.add_argument(
        "--request-timeout",
        type=float,
        default=10.0,
        help="HTTP socket and header timeout in seconds for --serve mode (default: 10.0s).",
    )
    p_export.add_argument(
        "--allowed-hosts",
        type=str,
        default=None,
        help="Comma-separated Host header allowlist for --serve mode (DNS-rebinding/reverse-proxy defense).",
    )


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
    theme: Optional[str] = None,
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
        err_name = getattr(err, "name", None)
        is_curses_missing = err_name in ("curses", "_curses") or "curses" in str(err).lower()
        if is_curses_missing:
            if sys.platform == "win32":
                dep_label = "'windows-curses' on Windows"
                install_hint = "run: pip install windows-curses"
            else:
                dep_label = "Python 'curses' (_curses)"
                install_hint = "install your system python3-curses / ncurses package"
            logger.warning(
                "Curses runtime is unavailable (%s). Falling back to headless mode.",
                err,
            )
            print(
                f"\n[WARN] The interactive terminal dashboard (--tui) requires {dep_label}.\n"
                f"Falling back to plain-text stream mode. To enable TUI, {install_hint}\n",
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

    # 5. Resolve ASCII fallback mode and initial color theme
    initial_theme = None
    theme_name = theme if theme is not None else os.environ.get("RAILMON_THEME")
    if theme_name:
        from tui.theme import parse_theme_name
        initial_theme = parse_theme_name(theme_name)

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
                initial_theme=initial_theme,
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


def _handle_history_segments_action(repo: Any, args: argparse.Namespace) -> None:
    """Helper to dispatch 'railmon history segments' command."""
    from datetime import datetime, timedelta, timezone
    from storage.cli_formatters import format_segment_transit_table

    start_date = getattr(args, "start_date", None)
    days = getattr(args, "days", None)
    if days and not start_date:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        start_date = cutoff.strftime("%Y-%m-%d")

    train_type_arg = getattr(args, "type", "all")
    type_filter = train_type_arg if train_type_arg != "all" else None

    report = repo.get_segment_transit_times(
        corridor=getattr(args, "corridor", None),
        from_station=getattr(args, "from_station", None),
        to_station=getattr(args, "to_station", None),
        train_type=type_filter,
        start_date=start_date,
        end_date=getattr(args, "end_date", None),
        min_trips=getattr(args, "min_trips", 3),
        limit=getattr(args, "limit", 25),
    )

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        use_ascii = getattr(args, "ascii", False)
        print(format_segment_transit_table(report, use_ascii=use_ascii))


def _handle_history_bottlenecks_action(repo: Any, args: argparse.Namespace) -> None:
    """Helper to dispatch 'railmon history bottlenecks' command."""
    from datetime import datetime, timedelta, timezone
    from storage.cli_formatters import format_bottlenecks_table

    start_date = getattr(args, "start_date", None)
    days = getattr(args, "days", None)
    if days and not start_date:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        start_date = cutoff.strftime("%Y-%m-%d")

    train_type_arg = getattr(args, "type", "all")
    type_filter = train_type_arg if train_type_arg != "all" else None

    report = repo.get_corridor_bottlenecks(
        corridor=getattr(args, "corridor", None),
        start_date=start_date,
        end_date=getattr(args, "end_date", None),
        train_type=type_filter,
        min_trips=getattr(args, "min_trips", 3),
        limit=getattr(args, "limit", 15),
    )

    if getattr(args, "json", False):
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        use_ascii = getattr(args, "ascii", False)
        print(format_bottlenecks_table(report, use_ascii=use_ascii))


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
            "Please specify a history action: trips, train, punctuality, dwell, "
            "bottlenecks, segments, stats, sessions, export, archive.\n"
            "Use 'railmon history --help' for details."
        )
        return 0

    db = Database(db_path)
    repo = HistoricalRepository(db)

    try:
        if action == "trips":
            trips = repo.get_active_trips(
                service_date=args.date, corridor=args.corridor, limit=args.limit
            )
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

        elif action == "bottlenecks":
            _handle_history_bottlenecks_action(repo, args)

        elif action == "segments":
            _handle_history_segments_action(repo, args)

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
                from storage.models import format_utc_iso

                cutoff_dt = datetime.now(timezone.utc) - timedelta(days=ret_days)
                before_iso = format_utc_iso(cutoff_dt)

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


def run_cab_stream_mode(args: argparse.Namespace) -> int:
    """Handle the 'cab' CLI subcommand execution.

    Args:
        args: Parsed command-line arguments.

    Returns:
        Exit code: 0 on success, 1 on error or stream unavailable.
    """
    train_num = getattr(args, "train_number", "").strip()
    if not train_num:
        sys.stderr.write("Error: Missing required argument: train_number\n")
        return 1

    demo = bool(getattr(args, "demo", False))
    preferred_player = getattr(args, "player", None)
    url_only = bool(getattr(args, "url_only", False))
    use_browser = bool(getattr(args, "browser", False))
    output_json = bool(getattr(args, "json", False))
    is_ascii = bool(getattr(args, "ascii", False))

    if use_browser:
        preferred_player = "browser"

    if demo:
        from tui.mock_engine import generate_mock_cab_stream
        stream_info = generate_mock_cab_stream(train_num)
    else:
        client = RailwayGovClient()
        stream_info = client.get_cab_stream_url(train_num)

    if output_json:
        sys.stdout.write(json.dumps(stream_info.to_dict(), indent=2, ensure_ascii=is_ascii) + "\n")
        return 0 if stream_info.has_stream else 1

    if url_only:
        if stream_info.has_stream and stream_info.playlist_url:
            sys.stdout.write(f"{stream_info.playlist_url}\n")
            return 0
        else:
            sys.stderr.write(f"No active stream for train {train_num}: {stream_info.error_message or 'Unavailable'}\n")
            return 1

    # Standard console launch
    sys.stdout.write(f"Resolving forward cab video stream for Train {train_num}...\n")
    if not stream_info.has_stream or not stream_info.playlist_url:
        sys.stderr.write(f"Error: {stream_info.error_message or 'No stream available.'}\n")
        return 1

    ok_glyph = "[OK]" if is_ascii else "✓"
    sys.stdout.write(
        f"{ok_glyph} Stream Online: {stream_info.stream_type.upper()} "
        f"({stream_info.resolution or 'HD'} @ {stream_info.fps or 30}fps"
        f"{f', {stream_info.bitrate_kbps} kbps' if stream_info.bitrate_kbps else ''})\n"
    )

    success, player_name, msg = launch_cab_stream(stream_info, preferred_player=preferred_player)
    if success:
        sys.stdout.write(f"{ok_glyph} {msg}\n")
        return 0
    else:
        sys.stderr.write(f"Error: {msg}\n")
        return 1


def run_infrastructure_mode(args: argparse.Namespace) -> int:
    """Execute the headless infrastructure GIS inspection CLI command.

    Args:
        args: Parsed CLI namespace arguments.

    Returns:
        Exit code: 0 on clean output, 1 on error.
    """
    from tui.mock_engine import load_infrastructure
    from storage.cli_formatters import format_infrastructure_table

    is_ascii = bool(getattr(args, "ascii", False))
    feature_type = str(getattr(args, "feature_type", "all")).lower().strip()
    corridor_filter = getattr(args, "corridor", None)
    output_json = bool(getattr(args, "json", False))
    output_geojson = bool(getattr(args, "geojson", False))

    features_dict = load_infrastructure()
    features = list(features_dict.values())

    if feature_type != "all":
        features = [f for f in features if f.feature_type.value == feature_type]

    if corridor_filter:
        c_target = corridor_filter.lower().strip()
        features = [f for f in features if c_target in f.corridor_id.lower()]

    if output_geojson:
        geojson_features = []
        for f in features:
            if f.feature_type.value == "signal":
                geom = {"type": "Point", "coordinates": [f.point.lng, f.point.lat]}
            else:
                geom = {"type": "LineString", "coordinates": [[p.lng, p.lat] for p in f.points]}
            props: Dict[str, Any] = {
                "id": f.id,
                "name": f.name,
                "type": f.feature_type.value,
                "corridor": f.corridor_id,
                "description": f.description,
            }
            if f.length_m is not None:
                props["length_m"] = f.length_m
            if f.signal_aspect is not None:
                props["aspect"] = f.signal_aspect.value
            if f.portals:
                props["portals"] = [
                    {"name": p.name, "coordinates": [p.point.lng, p.point.lat]} for p in f.portals
                ]
            geojson_features.append({
                "type": "Feature",
                "id": f.id,
                "geometry": geom,
                "properties": props,
            })
        collection = {
            "type": "FeatureCollection",
            "features": geojson_features,
        }
        sys.stdout.write(json.dumps(collection, indent=2, ensure_ascii=is_ascii) + "\n")
        return 0

    if output_json:
        summary_list = []
        for f in features:
            item: Dict[str, Any] = {
                "id": f.id,
                "name": f.name,
                "type": f.feature_type.value,
                "corridor": f.corridor_id,
                "description": f.description,
                "point": {"lat": f.point.lat, "lng": f.point.lng},
            }
            if f.length_m is not None:
                item["length_m"] = f.length_m
            if f.signal_aspect is not None:
                item["aspect"] = f.signal_aspect.value
            summary_list.append(item)
        sys.stdout.write(json.dumps(summary_list, indent=2, ensure_ascii=is_ascii) + "\n")
        return 0

    table_str = format_infrastructure_table(features, ascii_mode=is_ascii)
    sys.stdout.write(table_str + "\n")
    return 0


def run_speed_limits_mode(args: argparse.Namespace) -> int:
    """Execute the headless corridor speed limits inspection CLI command (F-5.3).

    Args:
        args: Parsed CLI namespace arguments.

    Returns:
        Exit code: 0 on clean execution.
    """
    from tui.mock_engine import load_speed_limits
    from storage.cli_formatters import format_speed_limits_table

    is_ascii = bool(getattr(args, "ascii", False))
    corridor_filter = getattr(args, "corridor", None)
    output_json = bool(getattr(args, "json", False))

    rules, tsrs = load_speed_limits()

    if output_json:
        data = {
            "corridors": [
                {
                    "id": cid,
                    "name": rule.name,
                    "default_speed_kmh": rule.default_speed_kmh,
                    "description": rule.description,
                    "segments": [
                        {
                            "id": seg.id,
                            "name": seg.name,
                            "max_speed_kmh": seg.max_speed_kmh,
                            "buffer_m": seg.buffer_m,
                        }
                        for seg in rule.segments
                    ],
                }
                for cid, rule in sorted(rules.items(), key=lambda x: x[0])
                if not corridor_filter or (
                    corridor_filter.lower() in cid.lower()
                    or corridor_filter.lower() in rule.name.lower()
                )
            ],
            "slow_orders": [
                {
                    "id": tsr.id,
                    "corridor_id": tsr.corridor_id,
                    "name": tsr.name,
                    "max_speed_kmh": tsr.max_speed_kmh,
                    "reason": tsr.reason,
                    "active": tsr.active,
                }
                for tsr in tsrs
                if not corridor_filter or (
                    corridor_filter.lower() in tsr.corridor_id.lower()
                    or corridor_filter.lower() in tsr.name.lower()
                )
            ],
        }
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=is_ascii) + "\n")
        return 0

    table = format_speed_limits_table(
        rules=rules,
        tsrs=tsrs,
        corridor_filter=corridor_filter,
        ascii_mode=is_ascii,
    )
    sys.stdout.write(table + "\n")
    return 0


def run_slow_orders_mode(args: argparse.Namespace) -> int:
    """Execute the headless slow orders (TSR) inspection CLI command (F-5.3).

    Args:
        args: Parsed CLI namespace arguments.

    Returns:
        Exit code: 0 on clean execution.
    """
    from tui.mock_engine import load_slow_orders
    from storage.cli_formatters import format_slow_orders_table

    is_ascii = bool(getattr(args, "ascii", False))
    corridor_filter = getattr(args, "corridor", None)
    output_json = bool(getattr(args, "json", False))

    tsrs = load_slow_orders()

    if output_json:
        norm_filter = corridor_filter.strip().lower() if corridor_filter else None
        filtered = [
            {
                "id": tsr.id,
                "corridor_id": tsr.corridor_id,
                "name": tsr.name,
                "max_speed_kmh": tsr.max_speed_kmh,
                "reason": tsr.reason,
                "active": tsr.active,
                "points": [[p.lat, p.lng] for p in tsr.points],
            }
            for tsr in tsrs
            if not norm_filter or (
                norm_filter in tsr.corridor_id.lower()
                or norm_filter in tsr.name.lower()
            )
        ]
        sys.stdout.write(json.dumps(filtered, indent=2, ensure_ascii=is_ascii) + "\n")
        return 0

    table = format_slow_orders_table(
        tsrs=tsrs,
        corridor_filter=corridor_filter,
        ascii_mode=is_ascii,
    )
    sys.stdout.write(table + "\n")
    return 0


def run_sensor_health_mode(args: argparse.Namespace) -> int:
    """Execute the headless fleet sensor health inspection CLI command (F-5.2).

    Args:
        args: Parsed CLI namespace arguments.

    Returns:
        Exit code: 0 on clean execution.
    """
    from tui.geo import GeoPoint, evaluate_sensor_health
    from tui.mock_engine import MockTelemetryEngine, load_stations
    from storage.cli_formatters import format_sensor_health_table

    is_ascii = bool(getattr(args, "ascii", False))
    corridor_filter = getattr(args, "corridor", None)
    alerts_only = bool(getattr(args, "alerts_only", False))
    output_json = bool(getattr(args, "json", False))

    stations = list(load_stations().values())
    now_ts = time.time()

    engine = MockTelemetryEngine()
    train_positions = engine.step(1.0)

    evaluations: List[Tuple[Any, Any]] = []
    for pos in train_positions:
        pt = GeoPoint(lat=pos.lat, lng=pos.lng)
        health, _, _, _ = evaluate_sensor_health(
            point=pt,
            current_speed=pos.speed,
            last_updated=now_ts,
            last_moved_pt=pt,
            last_moved_time=now_ts,
            station_dwell_start=None,
            stations=stations,
            now=now_ts,
        )
        evaluations.append((pos, health))

    if output_json:
        data = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now_ts)),
            "trains": [
                {
                    "train_number": getattr(p, "train_number", None) or getattr(p, "id", ""),
                    "train_type": getattr(p, "train_type", "IC"),
                    "corridor": getattr(p, "corridor", ""),
                    "speed_kmh": getattr(p, "speed", 0.0),
                    "status_code": h.short_code,
                    "health_state": h.state.value,
                    "elapsed_sec": h.elapsed_sec,
                    "static_duration_sec": h.static_duration_sec,
                    "is_alert": h.is_alert,
                    "description": h.description,
                    "nearest_station": h.nearest_station,
                    "dwell_duration_sec": h.dwell_duration_sec,
                }
                for p, h in evaluations
                if (not corridor_filter or (
                    corridor_filter.lower() in getattr(p, "corridor", "").lower()
                    or corridor_filter.lower() in str(getattr(p, "train_number", "")).lower()
                ))
                and (not alerts_only or h.is_alert or h.state.value in ("FROZEN", "DROPPED", "STALE"))
            ],
        }
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=is_ascii) + "\n")
        return 0

    table = format_sensor_health_table(
        assessments=evaluations,
        alerts_only=alerts_only,
        corridor_filter=corridor_filter,
        ascii_mode=is_ascii,
    )
    sys.stdout.write(table + "\n")
    return 0


def run_conflicts_mode(args: argparse.Namespace) -> int:
    """Execute the headless network conflict audit CLI command (F-5.1).

    Args:
        args: Parsed CLI namespace arguments.

    Returns:
        Exit code: 0 on clean execution.
    """
    from tui.geo import evaluate_train_conflicts, ConflictSeverity
    from tui.mock_engine import MockTelemetryEngine, load_stations, load_tracks
    from storage.cli_formatters import format_conflict_table, format_conflict_json

    is_ascii = bool(getattr(args, "ascii", False))
    corridor_filter = getattr(args, "corridor", None)
    crit_only = bool(getattr(args, "critical_only", False))
    output_json = bool(getattr(args, "json", False))

    stations = list(load_stations().values())
    tracks = load_tracks()
    engine = MockTelemetryEngine()
    train_positions = engine.step(1.0)

    alerts = evaluate_train_conflicts(
        active_trains=train_positions,
        tracks_data=tracks,
        stations=stations,
        now=time.time(),
    )

    if corridor_filter:
        c_filt = corridor_filter.strip().lower()
        alerts = [
            a
            for a in alerts
            if c_filt in a.corridor_id.lower() or c_filt in a.corridor_name.lower()
        ]

    if crit_only:
        alerts = [a for a in alerts if a.severity == ConflictSeverity.CRITICAL]

    if output_json:
        sys.stdout.write(format_conflict_json(alerts) + "\n")
        return 0

    table = format_conflict_table(
        conflicts=alerts,
        ascii_mode=is_ascii,
    )
    sys.stdout.write(table + "\n")
    return 0


def _emit_watch_events(
    evaluator: Any,
    events: Sequence[Any],
    output_json: bool,
    is_ascii: bool,
) -> bool:
    """Output and deliver a collection of WatchAlertEvents.

    Returns:
        True if an arrival event fired and exit_on_arrival is requested, False otherwise.
    """
    from railmon_watchdog import (
        WatchTriggerType,
        dispatch_all_channels,
        format_watch_event_line,
    )

    for event in events:
        if output_json:
            sys.stdout.write(json.dumps(event.to_dict()) + "\n")
            sys.stdout.flush()
        else:
            sys.stdout.write(format_watch_event_line(event, ascii_mode=is_ascii) + "\n")
            sys.stdout.flush()

        dispatch_all_channels(evaluator.config, event)

        if event.trigger_type == WatchTriggerType.ARRIVAL and evaluator.config.exit_on_arrival:
            return True
    return False


def run_watch_mode(args: argparse.Namespace) -> int:
    """Execute the headless or interactive train watchdog alerting daemon (railmon watch).

    Args:
        args: Parsed CLI namespace arguments.

    Returns:
        Exit code: 0 on clean exit, 1 on error.
    """
    from railmon_watchdog import (
        WatchConfig,
        WatchTriggerType,
        WatchdogEvaluator,
        WebhookFormat,
        format_watch_ticker,
        parse_notify_minutes,
    )

    train_num = getattr(args, "train_number", "").strip()
    if not train_num:
        sys.stderr.write("Error: Missing required argument: train_number\n")
        return 1

    dest_arg = getattr(args, "dest", None)
    notify_str = getattr(args, "notify", "15m")
    notify_eta = parse_notify_minutes(notify_str)
    notify_dist = float(getattr(args, "distance", 15.0) or 15.0)
    delay_delta = float(getattr(args, "delay_delta", 3.0) or 3.0)
    webhook_url = getattr(args, "webhook", None)
    webhook_format_str = str(getattr(args, "webhook_format", "auto")).lower()
    try:
        wb_fmt = WebhookFormat(webhook_format_str)
    except ValueError:
        wb_fmt = WebhookFormat.AUTO
    webhook_chat_id = getattr(args, "webhook_chat_id", None)
    webhook_token = getattr(args, "webhook_token", None)
    enable_bell = not bool(getattr(args, "no_bell", False))
    enable_desktop = not bool(getattr(args, "no_desktop", False))
    exit_on_arrival = bool(getattr(args, "once", False))
    output_json = bool(getattr(args, "json", False))
    headless = bool(getattr(args, "headless", False) or getattr(args, "daemon", False))
    demo = bool(getattr(args, "demo", False))
    is_ascii = bool(getattr(args, "ascii", False))

    config = WatchConfig(
        train_number=train_num,
        destination_station=dest_arg,
        notify_eta_minutes=notify_eta,
        notify_distance_km=notify_dist,
        delay_delta_threshold_min=delay_delta,
        enable_bell=enable_bell,
        enable_desktop=enable_desktop,
        webhook_url=webhook_url,
        webhook_format=wb_fmt,
        webhook_chat_id=webhook_chat_id,
        webhook_token=webhook_token,
        exit_on_arrival=exit_on_arrival,
    )

    evaluator = WatchdogEvaluator(config)

    source = TelemetrySource.DEMO if demo else TelemetrySource.LIVE
    db_path = getattr(args, "db", "railway_history.db")
    reconnect_delay = float(getattr(args, "reconnect_delay", 3.0))

    stop_event = threading.Event()
    with install_signal_handlers(stop_event) as stop_event:
        stream, source_label = _resolve_plain_stream_source(
            source, db_path, reconnect_delay, stop_event
        )

        if not output_json and not headless:
            sys.stdout.write(f"Starting railmon watch daemon for Train {train_num}...\n")
            dest_desc = evaluator.config.destination_name or evaluator.config.destination_station or "None"
            sys.stdout.write(
                f"Source: {source_label} | Destination: {dest_desc} | Notify ETA: {int(notify_eta)}m\n"
            )
            sys.stdout.write("Press Ctrl+C to stop.\n\n")
            sys.stdout.flush()

        last_ticker_time = 0.0
        matching_pos: Optional[TrainPosition] = None

        try:
            for batch in stream:
                if stop_event.is_set():
                    break

                now_mono = time.time()
                for pos in batch:
                    events = evaluator.evaluate_position(pos, now=now_mono)
                    if not events:
                        continue
                    matching_pos = pos
                    should_exit = _emit_watch_events(evaluator, events, output_json, is_ascii)
                    if should_exit:
                        if not output_json and not headless:
                            sys.stdout.write(
                                f"\nDestination arrived: Train {train_num}. Watchdog exiting (--once).\n"
                            )
                        return 0

                # Check for train telemetry timeout
                timeout_event = evaluator.evaluate_timeout(now=now_mono)
                if timeout_event:
                    _emit_watch_events(evaluator, [timeout_event], output_json, is_ascii)

                # Periodic ticker display in interactive console
                if not output_json and not headless:
                    if (now_mono - last_ticker_time) >= 5.0 and matching_pos is not None:
                        last_ticker_time = now_mono
                        ticker_box = format_watch_ticker(
                            evaluator.config,
                            evaluator.state,
                            matching_pos,
                            ascii_mode=is_ascii,
                        )
                        sys.stdout.write(f"\n{ticker_box}\n")
                        sys.stdout.flush()

                if stop_event.wait(timeout=1.0):
                    break

        except KeyboardInterrupt:
            if not output_json and not headless:
                sys.stdout.write("\nWatchdog stopped by user.\n")
            return 0
        except BrokenPipeError:
            return 0

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
    theme = getattr(args, "theme", None)
    if is_tui:
        replay_kwargs: Dict[str, Any] = {
            "source": TelemetrySource.REPLAY,
            "fps": args.fps,
            "reconnect_delay": args.reconnect_delay,
            "recording": RecordingMode.DISABLED,
            "db_path": replay_db,
            "glyph_mode": glyph_mode,
            "replay_speed": replay_speed,
        }
        if theme is not None:
            replay_kwargs["theme"] = theme
        run_tui_mode(**replay_kwargs)
        return 0

    run_plain_stream_mode(
        source=TelemetrySource.REPLAY,
        reconnect_delay=args.reconnect_delay,
        recording=RecordingMode.DISABLED,
        db_path=replay_db,
    )
    return 0


def _build_serve_worker(
    source: TelemetrySource,
    store: Any,
    broadcaster: Any,
    db_path: str,
    reconnect_delay: float,
    recorder: Optional[Any],
    replay_speed: float = 1.0,
) -> Any:
    """Build telemetry worker configured with SSE broadcast callback hooks."""
    from tui.mock_engine import MockTelemetryEngine
    from tui.stream_worker import ReplayWorker, StreamWorker

    def _broadcast_positions(positions: Sequence[TrainPosition]) -> None:
        if not positions or broadcaster.client_count == 0:
            return
        items = []
        for p in positions:
            heading_raw = getattr(
                p, "heading", getattr(p, "track_heading", getattr(p, "course", 0.0))
            )
            heading_val = float(heading_raw) if heading_raw is not None else 0.0
            items.append(
                {
                    "id": str(getattr(p, "train_id", getattr(p, "id", ""))),
                    "lat": float(p.lat),
                    "lng": float(p.lng),
                    "speed": float(p.speed or 0.0),
                    "delay": float(p.delay or 0.0),
                    "heading": heading_val,
                    "timestamp": getattr(p, "timestamp", ""),
                }
            )
        payload = json.dumps(items, ensure_ascii=False)
        broadcaster.broadcast("trainPositionsUx", payload)

    orig_update_positions = store.update_positions

    def _wrapped_update_positions(
        positions: List[TrainPosition], virtual_now: Optional[float] = None
    ) -> None:
        orig_update_positions(positions, virtual_now=virtual_now)
        _broadcast_positions(positions)

    store.update_positions = _wrapped_update_positions

    orig_ingest_replay = store.ingest_replay_observations

    def _wrapped_ingest_replay(
        observations: Sequence[Tuple[TrainPosition, float]], virtual_now: float
    ) -> None:
        orig_ingest_replay(observations, virtual_now)
        _broadcast_positions([pos for pos, _ in observations])

    store.ingest_replay_observations = _wrapped_ingest_replay

    orig_replace_replay = store.replace_replay_state

    def _wrapped_replace_replay(
        observations: Sequence[Tuple[TrainPosition, float]], virtual_now: float
    ) -> None:
        orig_replace_replay(observations, virtual_now)
        _broadcast_positions([pos for pos, _ in observations])

    store.replace_replay_state = _wrapped_replace_replay

    if source == TelemetrySource.REPLAY:
        return ReplayWorker(
            store=store,
            db_path=db_path,
            speed_multiplier=replay_speed,
        )
    if source == TelemetrySource.DEMO:
        return StreamWorker(
            store=store,
            mock_engine=MockTelemetryEngine(),
            recorder=recorder,
        )
    return StreamWorker(
        store=store,
        client=RailwayGovClient(),
        base_backoff_sec=reconnect_delay,
        recorder=recorder,
    )


def _print_serve_banner(
    server: Any,
    source: TelemetrySource,
    recording: bool,
    db_path: str,
) -> None:
    """Print informative, formatted startup banner to stdout."""
    url = getattr(
        server,
        "server_url",
        f"http://{server.server_address[0]}:{server.server_port}/",
    )
    print("\n" + "=" * 78)
    print(f"  RAILMON EMBEDDED WEB MAP SERVER v{__version__} (Hellenic Railways)")
    print("=" * 78)
    print(f"  [INFO] Telemetry Mode     : {source.value.upper()}")
    print(f"  [INFO] Bound Interface    : {url}")
    tile_prov = getattr(
        server.config.tile_provider, "value", str(server.config.tile_provider)
    )
    print(f"  [INFO] Tile Provider      : {tile_prov}")
    print(f"  [INFO] CORS Enabled       : {server.config.cors_enabled}")
    if recording:
        print(f"  [INFO] SQLite Recording   : ENABLED (database: '{db_path}')")
    else:
        print("  [INFO] SQLite Recording   : DISABLED")
    print(
        f"  [INFO] Browser Auto-Launch: {'ENABLED' if server.config.open_browser else 'DISABLED'}"
    )
    print("\n  Endpoints Available:")
    print(f"    -> Dashboard UI : {url}")
    print(f"    -> Train States : {url}api/trains")
    print(f"    -> Live Stream  : {url}api/stream")
    print(f"    -> Metrics      : {url}metrics")
    print(f"    -> GTFS-RT Feed : {url}gtfs-rt/feed.pb")
    print(f"    -> System Health: {url}api/health")
    print("\n  Press Ctrl+C to stop.")
    print("=" * 78 + "\n", flush=True)


def _parse_validated_http_limits(
    args: argparse.Namespace,
    *,
    timeout_attr: str,
    include_sse: bool = False,
) -> Optional[Tuple[int, int, float]]:
    """Validate bounded HTTP daemon CLI limits; print error and return None on failure."""
    from railmon_http import (
        DEFAULT_MAX_CONNECTIONS,
        DEFAULT_REQUEST_TIMEOUT_SEC,
        validate_positive_finite_float,
        validate_positive_int,
    )

    try:
        raw_conn = getattr(args, "max_connections", DEFAULT_MAX_CONNECTIONS)
        max_conn = validate_positive_int("--max-connections", int(raw_conn), minimum=1)
        raw_timeout = getattr(args, timeout_attr, DEFAULT_REQUEST_TIMEOUT_SEC)
        timeout_sec = validate_positive_finite_float(
            f"--{timeout_attr.replace('_', '-')}", float(raw_timeout)
        )
        max_sse = 32
        if include_sse:
            raw_sse = getattr(args, "max_sse_clients", 32)
            max_sse = validate_positive_int("--max-sse-clients", int(raw_sse), minimum=1)
    except (ValueError, TypeError) as exc:
        print(f"[ERROR] Invalid HTTP daemon limit: {exc}", file=sys.stderr)
        return None
    return max_conn, max_sse, timeout_sec


def _parse_allowed_hosts_arg(raw_value: Optional[str]) -> Optional[Tuple[str, ...]]:
    """Parse and validate comma-separated --allowed-hosts CLI argument."""
    if raw_value is None:
        return ()
    text = str(raw_value).strip()
    if not text:
        return ()
    from railmon_http import validate_allowed_hosts

    tokens = [part.strip() for part in text.split(",")]
    if any(not tok for tok in tokens):
        print(
            f"[ERROR] Invalid --allowed-hosts value {raw_value!r}: empty entry detected.",
            file=sys.stderr,
        )
        return None
    try:
        return validate_allowed_hosts(tokens)
    except ValueError as exc:
        print(f"[ERROR] Invalid --allowed-hosts: {exc}", file=sys.stderr)
        return None


def run_serve_mode(args: argparse.Namespace) -> int:
    """Execute embedded zero-dependency web map server daemon.

    Args:
        args: Parsed command-line arguments namespace.

    Returns:
        int: Exit status code (0 for clean shutdown, 1 for fatal server error, 2 for invalid args).
    """
    import errno
    from server.daemon import WebMapServer
    from server.models import ServerConfig, TileProvider
    from server.sse import SSEBroadcaster
    from tui.state import TrainStateStore

    # 1. Parameter Validation
    host = str(args.host or "127.0.0.1").strip()
    port = int(args.port)
    if not (1 <= port <= 65535):
        print(
            f"[ERROR] Invalid port number: {port}. Port must be between 1 and 65535.",
            file=sys.stderr,
        )
        return 2

    http_limit_vals = _parse_validated_http_limits(
        args, timeout_attr="request_timeout", include_sse=True
    )
    if http_limit_vals is None:
        return 2
    max_connections, max_sse_clients, request_timeout = http_limit_vals

    allowed_hosts = _parse_allowed_hosts_arg(getattr(args, "allowed_hosts", None))
    if allowed_hosts is None:
        return 2

    replay_db = getattr(args, "replay", None)
    if replay_db is not None:
        replay_db_str = str(replay_db).strip()
        if replay_db_str and not os.path.exists(replay_db_str):
            print(
                f"[ERROR] Historical replay database not found: '{replay_db_str}'",
                file=sys.stderr,
            )
            return 2
    else:
        replay_db_str = ""

    raw_speed = getattr(args, "speed", None)
    replay_speed = float(raw_speed) if raw_speed is not None else 1.0
    if not math.isfinite(replay_speed) or replay_speed <= 0.0:
        print(
            f"[ERROR] Invalid replay speed: {replay_speed}. Replay speed must be > 0.0.",
            file=sys.stderr,
        )
        return 2

    tile_provider_enum = TileProvider.from_string(
        getattr(args, "tile_provider", "carto-dark")
    )

    # 2. Security Audit Notice for Public Network Binding
    if host in ("0.0.0.0", "::"):
        print(
            "\n[SECURITY NOTICE] Server is binding to all network interfaces (0.0.0.0).\n"
            "The railmon web map dashboard and REST API will be accessible across your local network.\n",
            file=sys.stderr,
        )

    # 3. Resolve Telemetry Source & Persistence Flags
    if replay_db_str:
        source = TelemetrySource.REPLAY
        is_rec = False
    elif getattr(args, "demo", False):
        source = TelemetrySource.DEMO
        is_rec = bool(getattr(args, "record", False))
    else:
        source = TelemetrySource.LIVE
        is_rec = bool(getattr(args, "record", False))

    db_path = getattr(args, "db", "railway_history.db")
    enable_deadband = not getattr(args, "no_filter_deadband", False)
    deadband = (
        DeadbandFilterMode.ENABLED if enable_deadband else DeadbandFilterMode.DISABLED
    )
    recording_mode = RecordingMode.ENABLED if is_rec else RecordingMode.DISABLED

    # 4. Initialize Domain State Store & Event Broadcaster
    store = TrainStateStore()
    effective_max_sse = min(max_sse_clients, max_connections)
    broadcaster = SSEBroadcaster(max_clients=effective_max_sse)

    # 5. Start Optional Telemetry Recorder Thread
    db, recorder = _start_optional_recorder(
        recording=recording_mode,
        db_path=db_path,
        source=source,
        deadband=deadband,
        retention_days=getattr(args, "retention_days", None),
        archive_path=getattr(args, "archive_path", None),
    )
    if is_rec:
        store.set_recording_status(True, db_path)

    # 6. Configure & Attach Telemetry Ingestion Worker
    worker = _build_serve_worker(
        source=source,
        store=store,
        broadcaster=broadcaster,
        db_path=replay_db_str or db_path,
        reconnect_delay=getattr(args, "reconnect_delay", 3.0),
        recorder=recorder,
        replay_speed=replay_speed,
    )
    worker.start()

    # 7. Construct Server Configuration & Instantiate Daemon
    config = ServerConfig(
        host=host,
        port=port,
        cors_enabled=bool(getattr(args, "cors", False)),
        tile_provider=tile_provider_enum,
        demo_mode=(source == TelemetrySource.DEMO),
        record_enabled=is_rec,
        db_path=db_path if is_rec else None,
        replay_db=replay_db_str if source == TelemetrySource.REPLAY else None,
        replay_speed=replay_speed,
        open_browser=bool(getattr(args, "open_browser", False)),
        max_connections=max_connections,
        max_sse_clients=effective_max_sse,
        request_timeout_sec=request_timeout,
        sse_write_timeout_sec=request_timeout,
        allowed_hosts=allowed_hosts,
    )

    try:
        server = WebMapServer(
            server_address=(host, port),
            store=store,
            broadcaster=broadcaster,
            config=config,
        )
    except OSError as exc:
        worker.stop()
        if recorder:
            recorder.stop()
        if db:
            db.close()
        if exc.errno == errno.EADDRINUSE:
            print(
                f"\n[ERROR] Port {port} is already in use on interface '{host}'.\n"
                f"Please specify a different port using '--port <number>'.\n",
                file=sys.stderr,
            )
            return 1
        print(f"\n[ERROR] Failed to start web map server: {exc}\n", file=sys.stderr)
        return 1

    # 8. Print Operator Banner
    _print_serve_banner(server, source, is_rec, db_path)

    # 9. Main Server Execution Loop with Signal Handling
    with install_signal_handlers() as stop_event:
        server.start(background=True)
        try:
            while not stop_event.is_set():
                stop_event.wait(timeout=0.5)
        except KeyboardInterrupt:
            stop_event.set()
        finally:
            server.stop()
            worker.stop()
            if recorder:
                recorder.stop()
            if db:
                db.close()

    print("\n[INFO] Web map daemon shut down cleanly.")
    return 0


def _print_metrics_banner(
    server: Any,
    source: TelemetrySource,
    db_path: Optional[str],
) -> None:
    """Print informative, formatted startup banner for Prometheus exporter to stdout."""
    host = server.server_address[0]
    if host in ("0.0.0.0", ""):
        display_host = "127.0.0.1"
    else:
        display_host = host
    port = server.server_port
    base_url = f"http://{display_host}:{port}/"

    print("\n" + "=" * 78)
    print(f"  RAILMON PROMETHEUS & OPENMETRICS OPERATIONAL EXPORTER v{__version__}")
    print("=" * 78)
    print(f"  [INFO] Bound Interface    : {base_url}")
    print(f"  [INFO] Scrape Endpoint    : {base_url}metrics")
    print(f"  [INFO] Health Probe       : {base_url}health")
    print(f"  [INFO] Telemetry Mode     : {source.value.upper()}")
    print("  [INFO] Standards          : Prometheus 0.0.4 / OpenMetrics 1.0.0 (Negotiated)")
    gzip_str = "ENABLED" if getattr(server.config, "gzip_compression", True) else "DISABLED"
    print(f"  [INFO] Gzip Compression   : {gzip_str}")
    print(f"  [INFO] CORS Enabled       : {getattr(server.config, 'cors_enabled', False)}")
    if db_path:
        print(f"  [INFO] Storage Telemetry  : ENABLED (database: '{db_path}')")
    else:
        print("  [INFO] Storage Telemetry  : DISABLED")
    print("-" * 78)
    print("  Sample Prometheus Scrape Configuration (prometheus.yml):")
    print("    scrape_configs:")
    print("      - job_name: \"railmon\"")
    print("        scrape_interval: 15s")
    print("        static_configs:")
    print(f"          - targets: [\"{display_host}:{port}\"]")
    print("=" * 78)
    print("[INFO] Exporter daemon running. Press Ctrl+C to terminate cleanly.\n", flush=True)


def run_metrics_mode(args: argparse.Namespace) -> int:
    """Execute standalone Prometheus and OpenMetrics operational exporter daemon.

    Args:
        args: Parsed command-line arguments namespace.

    Returns:
        int: Exit status code (0 for clean shutdown, 1 for fatal server error, 2 for invalid args).
    """
    import errno
    from exporter.collector import (
        MetricRegistry,
        ProcessMetricsCollector,
        StorageMetricsCollector,
        StreamMetricsCollector,
        TrainTelemetryCollector,
        WatchdogMetricsCollector,
    )
    from exporter.models import ExporterConfig
    from exporter.server import MetricsServer
    from tui.mock_engine import MockTelemetryEngine
    from tui.state import TrainStateStore
    from tui.stream_worker import StreamWorker

    host = getattr(args, "host", "127.0.0.1")
    port = getattr(args, "port", 9100)

    # 1. Parameter Validation
    if not (1 <= port <= 65535):
        print(
            f"[ERROR] Invalid port: {port}. Port must be between 1 and 65535.",
            file=sys.stderr,
        )
        return 2

    http_limit_vals = _parse_validated_http_limits(args, timeout_attr="scrape_timeout")
    if http_limit_vals is None:
        return 2
    max_connections, _, scrape_timeout = http_limit_vals

    allowed_hosts = _parse_allowed_hosts_arg(getattr(args, "allowed_hosts", None))
    if allowed_hosts is None:
        return 2

    # 2. Security Audit Notice for Public Network Binding
    if host in ("0.0.0.0", "::"):
        print(
            "\n[SECURITY NOTICE] Server is binding to all network interfaces (0.0.0.0).\n"
            "The Prometheus exporter endpoint /metrics and health API will be accessible across your local network.\n",
            file=sys.stderr,
        )

    # 3. Resolve Telemetry Source & Persistence Flags
    demo = bool(getattr(args, "demo", False) or getattr(args, "mock", False))
    source = TelemetrySource.DEMO if demo else TelemetrySource.LIVE
    no_db = bool(getattr(args, "no_db", False))
    db_path = getattr(args, "db", "railway_history.db") if not no_db else None

    # 4. Initialize Domain State Store
    store = TrainStateStore()

    # 5. Configure & Attach Telemetry Ingestion Worker
    if demo:
        mock_engine = MockTelemetryEngine()
        worker = StreamWorker(store=store, mock_engine=mock_engine)
    else:
        client = RailwayGovClient()
        worker = StreamWorker(
            store=store,
            client=client,
            base_backoff_sec=getattr(args, "reconnect_delay", 3.0),
        )
    worker.start()

    # 6. Assemble Metric Registry & Domain Collectors
    registry = MetricRegistry()
    registry.register(TrainTelemetryCollector(store=store))
    registry.register(WatchdogMetricsCollector(store=store))
    registry.register(StreamMetricsCollector(store=store))
    if db_path:
        registry.register(StorageMetricsCollector(db_path=db_path))
    process_collector = ProcessMetricsCollector(version=__version__)
    registry.register(process_collector)

    # 7. Construct Server Configuration & Instantiate Daemon
    config = ExporterConfig(
        host=host,
        port=port,
        db_path=db_path,
        demo_mode=demo,
        gzip_compression=not bool(getattr(args, "no_gzip", False)),
        cors_enabled=bool(getattr(args, "cors", False)),
        scrape_timeout_seconds=scrape_timeout,
        max_connections=max_connections,
        allowed_hosts=allowed_hosts,
    )

    try:
        server = MetricsServer(
            server_address=(host, port),
            registry=registry,
            config=config,
        )
    except OSError as exc:
        worker.stop()
        if exc.errno == errno.EADDRINUSE:
            print(
                f"\n[ERROR] Port {port} is already in use on interface '{host}'.\n"
                f"Please specify a different port using '--port <number>'.\n",
                file=sys.stderr,
            )
            return 1
        print(f"\n[ERROR] Failed to start Prometheus exporter: {exc}\n", file=sys.stderr)
        return 1

    # 8. Print Operator Banner
    _print_metrics_banner(server, source, db_path)

    # 9. Main Server Execution Loop with Signal Handling
    with install_signal_handlers() as stop_event:
        server.start(background=True)
        try:
            while not stop_event.is_set():
                stop_event.wait(timeout=0.5)
        except KeyboardInterrupt:
            stop_event.set()
        finally:
            server.stop()
            worker.stop()

    print("\n[INFO] Prometheus exporter daemon shut down cleanly.")
    return 0


def _build_gtfs_config(args: argparse.Namespace) -> Any:
    """Construct validated GtfsRtConfig from parsed CLI arguments."""
    from gtfs_rt.models import GtfsRtConfig, StopIdField
    from railmon_http import validate_allowed_hosts

    raw_allowed = getattr(args, "allowed_hosts", None)
    allowed_hosts: Tuple[str, ...] = ()
    if raw_allowed is not None and str(raw_allowed).strip():
        tokens = [part.strip() for part in str(raw_allowed).strip().split(",")]
        if any(not tok for tok in tokens):
            raise ValueError(f"Invalid --allowed-hosts value {raw_allowed!r}: empty entry detected.")
        allowed_hosts = validate_allowed_hosts(tokens)

    stop_field = StopIdField(str(getattr(args, "stop_id_field", "id")).lower())
    return GtfsRtConfig(
        agency_id=str(getattr(args, "agency_id", "OSE") or "OSE").strip(),
        feed_version=getattr(args, "feed_version", None),
        include_unassigned=bool(getattr(args, "include_unassigned", False)),
        include_freight=bool(getattr(args, "include_freight", False)),
        include_alerts=not bool(getattr(args, "no_alerts", False)),
        delay_alert_min=int(getattr(args, "delay_alert_min", 15)),
        stop_id_field=stop_field,
        enrich_schedules=bool(getattr(args, "enrich_schedules", False)),
        corridor_filter=getattr(args, "corridor", None),
        host=str(getattr(args, "host", "127.0.0.1") or "127.0.0.1").strip(),
        port=int(getattr(args, "port", 8085)),
        refresh_interval_sec=float(getattr(args, "interval", 10.0)),
        gzip_compression=not bool(getattr(args, "no_gzip", False)),
        cors_enabled=bool(getattr(args, "cors", False)),
        demo_mode=bool(getattr(args, "demo", False) or getattr(args, "mock", False)),
        max_connections=int(getattr(args, "max_connections", 64)),
        request_timeout_sec=float(getattr(args, "request_timeout", 10.0)),
        allowed_hosts=allowed_hosts,
    )


def _make_sched_fetcher(enrich: bool, demo: bool) -> Any:
    """Create optional schedule fetcher callback for GTFS-RT TripUpdate enrichment."""
    if not enrich:
        return None
    if demo:
        from tui.mock_engine import generate_mock_train_schedule

        def _fetch_mock(_sid: Optional[str], train_num: str) -> Optional[Dict[str, Any]]:
            return generate_mock_train_schedule(train_num)

        return _fetch_mock

    client = RailwayGovClient()

    def _fetch_live(sid: Optional[str], train_num: str) -> Optional[Dict[str, Any]]:
        if sid:
            res = client.get_schedule_by_id(sid)
            if res:
                return res
        res_num = client.get_schedule_by_train_number(train_num)
        return res_num if res_num else None

    return _fetch_live


def _emit_feed_output(
    msg: Any,
    mapper: Any,
    args: argparse.Namespace,
    snapshot: Optional[Any] = None,
    positions: Optional[Sequence[TrainPosition]] = None,
    schedules: Optional[Dict[str, Dict[str, Any]]] = None,
) -> int:
    """Write GTFS-RT feed to --out-dir, --out, or stdout with TTY binary protection."""
    from gtfs_rt.exporter import export_feed_dir, export_feed_file, serialize_feed
    from gtfs_rt.models import OutputFormat

    fmt_raw = str(getattr(args, "output_format", "pb")).lower()
    out_path = getattr(args, "out", None)
    out_dir = getattr(args, "out_dir", None)
    if fmt_raw == "pb" and out_path and out_path.lower().endswith(".json"):
        fmt = OutputFormat.JSON
    else:
        fmt = OutputFormat(fmt_raw)

    if out_dir:
        written = export_feed_dir(
            mapper=mapper,
            out_dir=out_dir,
            fmt=fmt,
            snapshot=snapshot,
            positions=positions,
            schedules=schedules,
            now=float(msg.header.timestamp) if msg.header.timestamp else None,
        )
        for path in written:
            print(f"[OK] Exported GTFS-RT feed: {path}", file=sys.stderr)
        return 0

    if out_path and out_path != "-":
        saved = export_feed_file(msg, out_path, fmt=fmt)
        print(
            f"[OK] Exported {len(msg.entity)} GTFS-RT entities ({fmt.value}) -> {saved}",
            file=sys.stderr,
        )
        return 0

    payload = serialize_feed(msg, fmt)
    if fmt == OutputFormat.PROTOBUF and out_path != "-" and is_tty_supported():
        print(
            "[ERROR] Refusing to write raw binary protobuf to an interactive terminal.\n"
            "Specify '--out feed.pb', '--out -' (to force binary stdout), or '--format json'.",
            file=sys.stderr,
        )
        return 2

    if fmt == OutputFormat.JSON:
        sys.stdout.write(payload.decode("utf-8") + "\n")
        sys.stdout.flush()
    else:
        sys.stdout.buffer.write(payload)
        sys.stdout.buffer.flush()
    return 0


def _print_gtfs_banner(server: Any, source: TelemetrySource) -> None:
    """Print startup banner for standalone GTFS-Realtime HTTP server daemon."""
    url = server.server_url
    print("\n" + "=" * 78)
    print(f"  RAILMON GTFS-REALTIME v2.0 FEED SERVER v{__version__} (Hellenic Railways)")
    print("=" * 78)
    print(f"  [INFO] Telemetry Mode  : {source.value.upper()}")
    print(f"  [INFO] Bound Interface : {url}")
    print(f"  [INFO] Agency ID       : {server.config.agency_id}")
    print("  [INFO] Endpoints       :")
    print(f"    -> Combined Feed     : {url}gtfs-rt/feed.pb")
    print(f"    -> VehiclePositions  : {url}gtfs-rt/vehicle-positions.pb")
    print(f"    -> TripUpdates       : {url}gtfs-rt/trip-updates.pb")
    print(f"    -> Service Alerts    : {url}gtfs-rt/alerts.pb")
    print(f"    -> ProtoJSON Debug   : {url}gtfs-rt/feed.json")
    print(f"    -> Health Probe      : {url}health")
    print("\n  Press Ctrl+C to stop.")
    print("=" * 78 + "\n", flush=True)


def _export_inspect_mode(inspect_path: str) -> int:
    """Decode an existing .pb file and write ProtoJSON summary to stdout."""
    from gtfs_rt.exporter import inspect_pb_file
    from gtfs_rt.wire import WireDecodeError

    try:
        report = inspect_pb_file(inspect_path)
    except (FileNotFoundError, ValueError, WireDecodeError, OSError) as exc:
        print(f"[ERROR] Failed to inspect GTFS-RT file '{inspect_path}': {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    sys.stdout.flush()
    return 0


def _export_db_mode(args: argparse.Namespace, mapper: Any, feed_kind: Any) -> int:
    """Export a historical GTFS-RT feed snapshot from SQLite WAL storage."""
    from gtfs_rt.exporter import load_db_snapshot

    db_path = str(getattr(args, "from_db", "") or "").strip()
    at_time = getattr(args, "at", None)
    window_sec = int(getattr(args, "window_sec", 120))
    try:
        positions, schedules, ref_ts = load_db_snapshot(
            db_path=db_path,
            at_time=at_time,
            window_sec=window_sec,
        )
        msg = mapper.build_from_positions(
            positions=positions,
            feed_kind=feed_kind,
            now=ref_ts,
            schedules=schedules,
        )
        return _emit_feed_output(
            msg,
            mapper,
            args,
            positions=positions,
            schedules=schedules,
        )
    except (FileNotFoundError, ValueError, PermissionError, OSError) as exc:
        print(f"[ERROR] Historical GTFS-RT export failed: {exc}", file=sys.stderr)
        return 1


def _export_serve_mode(args: argparse.Namespace, config: Any, mapper: Any) -> int:
    """Run standalone multi-threaded GTFS-RT HTTP feed server daemon."""
    import errno
    from gtfs_rt.server import GtfsRtServer
    from tui.mock_engine import MockTelemetryEngine
    from tui.state import TrainStateStore
    from tui.stream_worker import StreamWorker

    if config.host in ("0.0.0.0", "::"):
        print(
            "\n[SECURITY NOTICE] GTFS-RT HTTP server is binding to all interfaces (0.0.0.0).\n",
            file=sys.stderr,
        )

    source = TelemetrySource.DEMO if config.demo_mode else TelemetrySource.LIVE
    store = TrainStateStore()
    if config.demo_mode:
        worker = StreamWorker(store=store, mock_engine=MockTelemetryEngine())
    else:
        worker = StreamWorker(
            store=store,
            client=RailwayGovClient(),
            base_backoff_sec=float(getattr(args, "reconnect_delay", 3.0)),
        )
    worker.start()

    try:
        server = GtfsRtServer(
            server_address=(config.host, config.port),
            store=store,
            mapper=mapper,
            config=config,
        )
    except OSError as exc:
        worker.stop()
        if exc.errno == errno.EADDRINUSE:
            print(
                f"\n[ERROR] Port {config.port} is already in use on '{config.host}'. "
                f"Specify another port via '--port <number>'.\n",
                file=sys.stderr,
            )
            return 1
        print(f"\n[ERROR] Failed to start GTFS-RT server: {exc}\n", file=sys.stderr)
        return 1

    _print_gtfs_banner(server, source)
    with install_signal_handlers() as stop_event:
        server.start(background=True)
        try:
            while not stop_event.is_set():
                stop_event.wait(timeout=0.5)
        except KeyboardInterrupt:
            stop_event.set()
        finally:
            server.stop()
            worker.stop()

    print("\n[INFO] GTFS-RT HTTP server shut down cleanly.")
    return 0


def _export_watch_mode(
    args: argparse.Namespace,
    config: Any,
    mapper: Any,
    feed_kind: Any,
) -> int:
    """Continuously write atomic GTFS-RT files on a fixed interval."""
    from tui.mock_engine import MockTelemetryEngine
    from tui.state import TrainStateStore
    from tui.stream_worker import StreamWorker

    if not getattr(args, "out", None) and not getattr(args, "out_dir", None):
        print("[ERROR] --watch mode requires either --out <file> or --out-dir <dir>.", file=sys.stderr)
        return 2

    store = TrainStateStore()
    if config.demo_mode:
        worker = StreamWorker(store=store, mock_engine=MockTelemetryEngine())
    else:
        worker = StreamWorker(
            store=store,
            client=RailwayGovClient(),
            base_backoff_sec=float(getattr(args, "reconnect_delay", 3.0)),
        )
    worker.start()

    with install_signal_handlers() as stop_event:
        try:
            stop_event.wait(timeout=min(1.0, config.refresh_interval_sec))
            while not stop_event.is_set():
                snapshot = store.get_snapshot()
                msg = mapper.build_feed(snapshot, feed_kind=feed_kind)
                rc = _emit_feed_output(msg, mapper, args, snapshot=snapshot)
                if rc != 0:
                    return rc
                stop_event.wait(timeout=config.refresh_interval_sec)
        except KeyboardInterrupt:
            stop_event.set()
        except (ValueError, PermissionError, OSError) as exc:
            print(f"[ERROR] GTFS-RT watch export failed: {exc}", file=sys.stderr)
            return 1
        finally:
            worker.stop()
    return 0


def _export_snapshot_mode(
    args: argparse.Namespace,
    config: Any,
    mapper: Any,
    feed_kind: Any,
) -> int:
    """Capture a single live or demo telemetry batch and export GTFS-RT feed."""
    if config.demo_mode:
        from tui.mock_engine import MockTelemetryEngine

        positions = MockTelemetryEngine().step(1.0)
        msg = mapper.build_from_positions(positions, feed_kind=feed_kind)
        try:
            return _emit_feed_output(msg, mapper, args, positions=positions)
        except (ValueError, PermissionError, OSError) as exc:
            print(f"[ERROR] GTFS-RT snapshot export failed: {exc}", file=sys.stderr)
            return 1

    stop_event = threading.Event()
    client = RailwayGovClient()
    positions_live: List[TrainPosition] = []
    try:
        for batch in client.stream_train_positions(
            reconnect_delay=0.0,
            stop_event=stop_event,
            timeout=15.0,
        ):
            positions_live = batch
            break
    except (RailwayAPIError, urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"[ERROR] Failed to capture live SSE telemetry batch: {exc}", file=sys.stderr)
        return 1

    msg = mapper.build_from_positions(positions_live, feed_kind=feed_kind)
    try:
        return _emit_feed_output(msg, mapper, args, positions=positions_live)
    except (ValueError, PermissionError, OSError) as exc:
        print(f"[ERROR] GTFS-RT snapshot export failed: {exc}", file=sys.stderr)
        return 1


def run_export_mode(args: argparse.Namespace) -> int:
    """Execute GTFS-Realtime v2.0 Protocol Buffer feed exporter (F-8.3)."""
    from gtfs_rt.mapper import GtfsRtMapper, ScheduleCache
    from gtfs_rt.models import FeedKind

    mode_name = str(getattr(args, "mode", "export") or "export").lower()
    gtfs_flag = bool(getattr(args, "gtfs_rt", False)) or mode_name in ("gtfs-rt", "gtfsrt")
    if not gtfs_flag:
        print(
            "[ERROR] Please specify an export format flag, e.g.:\n"
            "  railmon export --gtfs-rt --demo --out feed.pb\n"
            "  railmon export --gtfs-rt --serve --port 8085",
            file=sys.stderr,
        )
        return 2

    inspect_path = getattr(args, "inspect", None)
    if inspect_path:
        return _export_inspect_mode(str(inspect_path))

    try:
        config = _build_gtfs_config(args)
    except ValueError as exc:
        print(f"[ERROR] Invalid GTFS-RT configuration: {exc}", file=sys.stderr)
        return 2

    feed_kind = FeedKind(str(getattr(args, "feed", "combined")).lower())
    fetcher = _make_sched_fetcher(config.enrich_schedules, config.demo_mode)
    mapper = GtfsRtMapper(
        config=config,
        schedule_cache=ScheduleCache(fetcher=fetcher),
    )

    if getattr(args, "from_db", None):
        return _export_db_mode(args, mapper, feed_kind)
    if getattr(args, "serve", False):
        return _export_serve_mode(args, config, mapper)
    if getattr(args, "watch", False):
        return _export_watch_mode(args, config, mapper, feed_kind)
    return _export_snapshot_mode(args, config, mapper, feed_kind)


def _dispatch_subcommand_mode(args: argparse.Namespace) -> Optional[int]:
    """Dispatch dedicated CLI subcommands if matched, or return None."""
    mode = getattr(args, "mode", None)
    if mode in ("serve", "server", "web"):
        return run_serve_mode(args)

    if mode in ("metrics", "exporter", "prometheus"):
        return run_metrics_mode(args)

    if mode in ("export", "gtfs-rt", "gtfsrt"):
        return run_export_mode(args)

    if mode in ("conflicts", "conflict", "watchdog_conflicts"):
        return run_conflicts_mode(args)

    if mode in ("sensor-health", "health", "sensor_health"):
        return run_sensor_health_mode(args)

    if mode in ("speed-limits", "speeds", "speed_limits"):
        return run_speed_limits_mode(args)

    if mode in ("slow-orders", "tsr", "slow_orders"):
        return run_slow_orders_mode(args)

    if mode in ("infrastructure", "infra", "geo"):
        return run_infrastructure_mode(args)

    if mode in ("cab", "stream-cab"):
        return run_cab_stream_mode(args)

    if mode == "departures":
        return run_departures_mode(args)

    if mode == "watch":
        return run_watch_mode(args)

    if mode == "schedule":
        return run_schedule_mode(args)

    if mode == "schedules":
        ret = run_schedules_mode()
        return ret if isinstance(ret, int) else 0

    if mode == "history":
        return run_history_mode(args)

    return None


def _dispatch_cli_mode(args: argparse.Namespace, is_tui: bool) -> int:
    """Route parsed CLI arguments to the appropriate execution mode."""
    sub_res = _dispatch_subcommand_mode(args)
    if sub_res is not None:
        return sub_res

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
        theme = getattr(args, "theme", None)
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
            if theme is not None:
                tui_kwargs["theme"] = theme
            _attach_optional_retention_kwargs(args, tui_kwargs)
            run_tui_mode(**tui_kwargs)
        else:
            tui_call_kwargs: Dict[str, Any] = {
                "source": source,
                "fps": args.fps,
                "reconnect_delay": args.reconnect_delay,
                "glyph_mode": glyph_mode,
            }
            if theme is not None:
                tui_call_kwargs["theme"] = theme
            run_tui_mode(**tui_call_kwargs)
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
    is_serve = getattr(args, "mode", None) in ("serve", "server", "web")
    is_tui = bool(
        not is_serve
        and (getattr(args, "tui", False) or has_replay)
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
