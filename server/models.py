"""Domain models, configurations, and metric types for the railmon web server."""

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from queue import Queue
from typing import Any, Dict, Optional, Tuple

from railmon_http import (
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_REQUEST_TIMEOUT_SEC,
    DEFAULT_SSE_WRITE_TIMEOUT_SEC,
    validate_allowed_hosts,
    validate_positive_finite_float,
    validate_positive_int,
)


class TileProvider(str, Enum):
    """Cartographic base tile layer provider."""

    CARTO_DARK = "carto-dark"
    CARTO_LIGHT = "carto-light"
    OSM = "osm"

    @classmethod
    def from_string(cls, val: str) -> "TileProvider":
        """Parse string to TileProvider with defensive default."""
        cleaned = str(val or "").strip().lower()
        for provider in cls:
            if provider.value == cleaned:
                return provider
        return cls.CARTO_DARK

    @property
    def tile_url(self) -> str:
        """Return standard WMS / tile template URL."""
        if self == TileProvider.CARTO_LIGHT:
            return "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"
        if self == TileProvider.OSM:
            return "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
        return "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"

    @property
    def attribution(self) -> str:
        """Return legal attribution string for tiles."""
        if self == TileProvider.OSM:
            return '&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap</a> contributors'
        return '&copy; <a href="https://carto.com/attributions" target="_blank" rel="noopener noreferrer">CARTO</a>'


@dataclass(frozen=True)
class ServerConfig:
    """Immutable runtime configuration for WebMapServer."""

    host: str = "127.0.0.1"
    port: int = 8080
    cors_enabled: bool = False
    tile_provider: TileProvider = TileProvider.CARTO_DARK
    demo_mode: bool = False
    record_enabled: bool = False
    db_path: Optional[str] = None
    replay_db: Optional[str] = None
    replay_speed: float = 1.0
    open_browser: bool = False
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    max_sse_clients: int = 32
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC
    sse_write_timeout_sec: float = DEFAULT_SSE_WRITE_TIMEOUT_SEC
    allowed_hosts: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate configuration parameters."""
        if isinstance(self.port, bool) or not isinstance(self.port, int) or not (1 <= self.port <= 65535):
            raise ValueError(f"Port must be between 1 and 65535, got {self.port}")
        if not isinstance(self.cors_enabled, bool):
            raise ValueError(f"cors_enabled must be a bool, got {self.cors_enabled!r}")
        validate_positive_finite_float("replay_speed", self.replay_speed)
        validate_positive_int("max_connections", self.max_connections, minimum=1)
        validate_positive_int("max_sse_clients", self.max_sse_clients, minimum=1)
        validate_positive_finite_float("request_timeout_sec", self.request_timeout_sec)
        validate_positive_finite_float("sse_write_timeout_sec", self.sse_write_timeout_sec)
        object.__setattr__(self, "allowed_hosts", validate_allowed_hosts(self.allowed_hosts))

    def to_dict(self) -> Dict[str, Any]:
        """Convert configuration to JSON-serializable dictionary."""
        return {
            "host": self.host,
            "port": self.port,
            "cors_enabled": self.cors_enabled,
            "tile_provider": self.tile_provider.value,
            "demo_mode": self.demo_mode,
            "record_enabled": self.record_enabled,
            "db_path": self.db_path,
            "replay_db": self.replay_db,
            "replay_speed": self.replay_speed,
            "open_browser": self.open_browser,
            "max_connections": self.max_connections,
            "max_sse_clients": self.max_sse_clients,
            "request_timeout_sec": self.request_timeout_sec,
            "sse_write_timeout_sec": self.sse_write_timeout_sec,
            "allowed_hosts": list(self.allowed_hosts),
        }


@dataclass
class ClientConnection:
    """Represents a connected browser SSE client channel."""

    client_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    remote_addr: str = "127.0.0.1"
    connected_at: float = field(default_factory=time.time)
    last_event_at: float = field(default_factory=time.time)
    queue: Queue[Tuple[str, str]] = field(default_factory=lambda: Queue(maxsize=100))
    frames_sent: int = 0
    frames_dropped: int = 0

    def touch(self) -> None:
        """Update last active timestamp."""
        self.last_event_at = time.time()


@dataclass(frozen=True)
class SSEMessage:
    """Standard-compliant Server-Sent Events framing envelope."""

    event: str
    data: str
    msg_id: Optional[str] = None
    retry_ms: Optional[int] = None

    def to_wire_format(self) -> str:
        """Serialize event to RFC compliant text/event-stream wire format."""
        lines = []
        if self.msg_id is not None:
            lines.append(f"id: {self.msg_id}")
        if self.event:
            lines.append(f"event: {self.event}")
        if self.retry_ms is not None:
            lines.append(f"retry: {self.retry_ms}")
        for data_line in self.data.split("\n"):
            lines.append(f"data: {data_line}")
        lines.append("\n")
        return "\n".join(lines)


@dataclass(frozen=True)
class ServerHealthMetrics:
    """Operational health telemetry snapshot for /api/health."""

    uptime_seconds: float
    active_clients: int
    total_clients_served: int
    total_broadcasts: int
    total_frames_sent: int
    total_frames_dropped: int
    max_clients: int = 0
    total_clients_rejected: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert metrics to dictionary."""
        return {
            "uptime_seconds": round(self.uptime_seconds, 2),
            "active_clients": self.active_clients,
            "total_clients_served": self.total_clients_served,
            "total_broadcasts": self.total_broadcasts,
            "total_frames_sent": self.total_frames_sent,
            "total_frames_dropped": self.total_frames_dropped,
            "max_clients": self.max_clients,
            "total_clients_rejected": self.total_clients_rejected,
        }
