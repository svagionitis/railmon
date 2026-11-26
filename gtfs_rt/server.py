"""Embedded multi-threaded HTTP daemon serving GTFS-Realtime v2.0 feeds."""

import errno
import gzip
import json
import logging
import threading
import time
from typing import Any, Dict, Optional, Tuple
import urllib.parse

from gtfs_rt.mapper import FeedCache, GtfsRtMapper
from gtfs_rt.models import (
    GTFS_RT_VERSION,
    FeedKind,
    GtfsRtConfig,
    OutputFormat,
)
from railmon_http import (
    BoundedHTTPRequestHandler,
    BoundedHTTPServer,
    HttpLimitsConfig,
)
from tui.state import TrainStateStore

logger = logging.getLogger("railmon.gtfs_rt.server")

_PB_ROUTES: Dict[str, FeedKind] = {
    "/gtfs-rt/feed.pb": FeedKind.COMBINED,
    "/gtfs-rt/vehicle-positions.pb": FeedKind.VEHICLES,
    "/gtfs-rt/trip-updates.pb": FeedKind.TRIPS,
    "/gtfs-rt/alerts.pb": FeedKind.ALERTS,
}

_JSON_ROUTES: Dict[str, FeedKind] = {
    "/gtfs-rt/feed.json": FeedKind.COMBINED,
    "/gtfs-rt/vehicle-positions.json": FeedKind.VEHICLES,
    "/gtfs-rt/trip-updates.json": FeedKind.TRIPS,
    "/gtfs-rt/alerts.json": FeedKind.ALERTS,
}


def resolve_gtfs_route(
    raw_path: str,
    query_str: str = "",
    accept_hdr: str = "",
) -> Optional[Tuple[FeedKind, OutputFormat]]:
    """Map an HTTP request path, query string, and Accept header to (FeedKind, OutputFormat)."""
    clean_path = raw_path.rstrip("/") or "/"
    if clean_path in _JSON_ROUTES:
        return _JSON_ROUTES[clean_path], OutputFormat.JSON

    if clean_path in _PB_ROUTES:
        kind = _PB_ROUTES[clean_path]
        qs = urllib.parse.parse_qs(query_str)
        fmt_vals = qs.get("format", [])
        if fmt_vals and fmt_vals[0].strip().lower() == "json":
            return kind, OutputFormat.JSON
        accept_low = (accept_hdr or "").lower()
        if "application/json" in accept_low and "application/x-protobuf" not in accept_low:
            return kind, OutputFormat.JSON
        return kind, OutputFormat.PROTOBUF

    return None


def etag_matches(if_none_match: Optional[str], current_etag: str) -> bool:
    """Return True if client If-None-Match header matches current_etag."""
    if not if_none_match:
        return False
    for token in if_none_match.split(","):
        cleaned = token.strip()
        if cleaned == "*" or cleaned == current_etag or cleaned == f"W/{current_etag}":
            return True
    return False


class GtfsRtHandler(BoundedHTTPRequestHandler):
    """HTTP request handler serving binary protobuf and ProtoJSON GTFS-RT feeds."""

    server: "GtfsRtServer"
    protocol_version: str = "HTTP/1.1"

    def log_message(self, log_format: str, *args: Any) -> None:
        """Route HTTP access logs into railmon logger instead of stderr."""
        logger.debug(
            "%s - - [%s] %s",
            self.client_address[0] if self.client_address else "127.0.0.1",
            self.log_date_time_string(),
            log_format % args,
        )

    def do_HEAD(self) -> None:
        """Handle HTTP HEAD requests (headers only, zero body)."""
        self._dispatch(is_head=True)

    def do_GET(self) -> None:
        """Handle HTTP GET requests."""
        self._dispatch(is_head=False)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight OPTIONS requests."""
        self.send_response(204)
        self._add_cors()
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        """Reject POST requests with 405 Method Not Allowed."""
        self._send_err(405, "Method Not Allowed: GTFS-RT feed server is read-only")

    def do_PUT(self) -> None:
        """Reject PUT requests with 405 Method Not Allowed."""
        self._send_err(405, "Method Not Allowed: GTFS-RT feed server is read-only")

    def do_DELETE(self) -> None:
        """Reject DELETE requests with 405 Method Not Allowed."""
        self._send_err(405, "Method Not Allowed: GTFS-RT feed server is read-only")

    def _add_cors(self) -> None:
        """Emit permissive CORS headers only when explicitly enabled in server config."""
        if getattr(self.server.config, "cors_enabled", False):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Accept, Accept-Encoding, If-None-Match, Content-Type",
            )

    def _dispatch(self, is_head: bool = False) -> None:
        """Dispatch incoming GET/HEAD path to feed, health, or index handlers."""
        try:
            parts = urllib.parse.urlsplit(self.path)
            raw_path = parts.path.rstrip("/") or "/"

            route = resolve_gtfs_route(
                raw_path,
                query_str=parts.query,
                accept_hdr=self.headers.get("Accept", ""),
            )
            if route is not None:
                self._handle_feed(route[0], route[1], is_head=is_head)
            elif raw_path == "/health":
                self._handle_health(is_head=is_head)
            elif raw_path in ("/", "/index.html"):
                self._handle_index(is_head=is_head)
            else:
                self._send_err(404, f"Endpoint '{raw_path}' not found")
        except TimeoutError:
            self._record_server_timeout()
            self.close_connection = True
            logger.debug("Client timed out during GTFS-RT request")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.debug("Client disconnected abruptly during GTFS-RT request")

    def _handle_feed(
        self,
        feed_kind: FeedKind,
        fmt: OutputFormat,
        is_head: bool = False,
    ) -> None:
        """Build or reuse cached GTFS-RT payload and emit with ETag/gzip support."""
        snapshot = self.server.store.get_snapshot()
        cached = self.server.feed_cache.get_or_build(snapshot, feed_kind)

        if fmt == OutputFormat.JSON:
            body = cached.json_bytes
            etag = cached.etag_json
            content_type = "application/json; charset=utf-8"
        else:
            body = cached.pb_bytes
            etag = cached.etag_pb
            content_type = "application/x-protobuf"

        if etag_matches(self.headers.get("If-None-Match"), etag):
            self.send_response(304)
            self._add_cors()
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=5")
            self.send_header("X-GTFS-RT-Version", GTFS_RT_VERSION)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        encoding: Optional[str] = None
        if self.server.config.gzip_compression and len(body) > 256:
            if "gzip" in self.headers.get("Accept-Encoding", ""):
                body = gzip.compress(body, compresslevel=6)
                encoding = "gzip"

        self.send_response(200)
        self._add_cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "public, max-age=5")
        self.send_header("X-GTFS-RT-Version", GTFS_RT_VERSION)
        self.send_header("X-Entity-Count", str(cached.entity_count))
        if encoding is not None:
            self.send_header("Content-Encoding", encoding)
        self.end_headers()

        if not is_head:
            self.wfile.write(body)

    def _handle_health(self, is_head: bool = False) -> None:
        """Emit JSON operational health diagnostics for GTFS-RT feed server."""
        snapshot = self.server.store.get_snapshot()
        payload = {
            "status": "ok",
            "gtfs_rt_version": GTFS_RT_VERSION,
            "uptime_seconds": round(time.time() - self.server._started_at, 2),
            "active_trains": len(snapshot.trains),
            "snapshot_version": snapshot.version,
            "connection_status": snapshot.connection_status.name,
            "timestamp": time.time(),
            "active_http_connections": self.server.active_connections,
            "max_http_connections": self.server.max_connections,
            "rejected_http_connections": self.server.rejected_connections,
        }
        body = json.dumps(payload, indent=2).encode("utf-8")
        self.send_response(200)
        self._add_cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        if not is_head:
            self.wfile.write(body)

    def _handle_index(self, is_head: bool = False) -> None:
        """Serve minimal HTML index listing GTFS-RT endpoints and OTP snippet."""
        base = self.server.server_url.rstrip("/")
        html = (
            "<!DOCTYPE html>\n"
            '<html lang="en">\n'
            "<head>\n"
            '  <meta charset="utf-8">\n'
            "  <title>Railmon GTFS-Realtime v2.0 Feed Server</title>\n"
            "  <style>\n"
            "    body { font-family: system-ui, sans-serif; margin: 2rem; "
            "background: #0f172a; color: #f8fafc; line-height: 1.5; }\n"
            "    h1 { color: #38bdf8; }\n"
            "    a { color: #38bdf8; text-decoration: none; }\n"
            "    pre { background: #1e293b; padding: 1rem; border-radius: 6px; color: #a5f3fc; }\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            "  <h1>Railmon GTFS-Realtime v2.0 Feed Server</h1>\n"
            "  <p>Hellenic Railways (railway.gov.gr) Protocol Buffers feed exporter.</p>\n"
            "  <h2>Binary Protobuf Feeds (application/x-protobuf)</h2>\n"
            "  <ul>\n"
            '    <li><a href="/gtfs-rt/feed.pb">/gtfs-rt/feed.pb</a> &mdash; Combined Feed</li>\n'
            '    <li><a href="/gtfs-rt/vehicle-positions.pb">/gtfs-rt/vehicle-positions.pb</a> &mdash; VehiclePositions</li>\n'
            '    <li><a href="/gtfs-rt/trip-updates.pb">/gtfs-rt/trip-updates.pb</a> &mdash; TripUpdates</li>\n'
            '    <li><a href="/gtfs-rt/alerts.pb">/gtfs-rt/alerts.pb</a> &mdash; Service Alerts</li>\n'
            "  </ul>\n"
            "  <h2>ProtoJSON Debug Feeds &amp; Health</h2>\n"
            "  <ul>\n"
            '    <li><a href="/gtfs-rt/feed.json">/gtfs-rt/feed.json</a></li>\n'
            '    <li><a href="/gtfs-rt/vehicle-positions.json">/gtfs-rt/vehicle-positions.json</a></li>\n'
            '    <li><a href="/gtfs-rt/trip-updates.json">/gtfs-rt/trip-updates.json</a></li>\n'
            '    <li><a href="/gtfs-rt/alerts.json">/gtfs-rt/alerts.json</a></li>\n'
            '    <li><a href="/health">/health</a></li>\n'
            "  </ul>\n"
            "  <h2>OpenTripPlanner Sample Updater</h2>\n"
            "  <pre>{\n"
            '  "type": "vehicle-positions",\n'
            f'  "url": "{base}/gtfs-rt/vehicle-positions.pb",\n'
            '  "feedId": "OSE",\n'
            '  "frequencySec": 10\n'
            "}</pre>\n"
            "</body>\n"
            "</html>\n"
        )
        body = html.encode("utf-8")
        self.send_response(200)
        self._add_cors()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not is_head:
            self.wfile.write(body)

    def _send_err(self, status_code: int, message: str) -> None:
        """Emit structured JSON HTTP error payload."""
        try:
            body = json.dumps({"error": message, "status": status_code}).encode("utf-8")
            self.send_response(status_code)
            self._add_cors()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            logger.debug("Failed sending error response to disconnected GTFS-RT client")


class GtfsRtServer(BoundedHTTPServer):
    """Multi-threaded bounded HTTP server daemon serving GTFS-RT v2.0 feeds."""

    def __init__(
        self,
        server_address: Tuple[str, int],
        store: TrainStateStore,
        mapper: Optional[GtfsRtMapper] = None,
        config: Optional[GtfsRtConfig] = None,
    ) -> None:
        self.store = store
        self.config = config or GtfsRtConfig(
            host=server_address[0],
            port=server_address[1] if server_address[1] != 0 else 8085,
        )
        self.mapper = mapper or GtfsRtMapper(config=self.config)
        self.feed_cache = FeedCache(self.mapper)
        self._shutdown_event = threading.Event()
        self._server_thread: Optional[threading.Thread] = None
        self._started_at: float = time.time()

        http_limits = HttpLimitsConfig.for_request_timeout(
            self.config.request_timeout_sec,
            max_connections=self.config.max_connections,
        )

        try:
            super().__init__(
                server_address,
                GtfsRtHandler,
                http_limits=http_limits,
                allowed_hosts=self.config.allowed_hosts,
            )
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                logger.error(
                    "Port %d is already in use on '%s'. Choose another port via --port.",
                    server_address[1],
                    server_address[0],
                )
            raise

        logger.info(
            "Initialized GtfsRtServer on %s:%d (CORS=%s, max_conn=%d, timeout=%.1fs)",
            self.server_address[0],
            self.server_port,
            self.config.cors_enabled,
            self.http_limits.max_connections,
            self.http_limits.request_timeout_sec,
        )

    @property
    def is_running(self) -> bool:
        """Return True while server daemon is active."""
        return not self._shutdown_event.is_set()

    @property
    def server_url(self) -> str:
        """Return canonical base URL for the GTFS-RT HTTP server."""
        host = self.server_address[0]
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.server_port}/"

    def start(self, background: bool = False) -> None:
        """Start serving GTFS-RT HTTP endpoints."""
        self._shutdown_event.clear()
        if background:
            self._server_thread = threading.Thread(
                target=self.serve_forever,
                name="GtfsRtServerThread",
                daemon=True,
            )
            self._server_thread.start()
            logger.info("GtfsRtServer running in background at %s", self.server_url)
        else:
            logger.info("Serving GTFS-RT feeds at %s (Press Ctrl+C to stop)", self.server_url)
            try:
                self.serve_forever()
            except KeyboardInterrupt:
                logger.info("Received KeyboardInterrupt; stopping GtfsRtServer...")
            finally:
                self.stop()

    def stop(self) -> None:
        """Gracefully terminate GTFS-RT server and close sockets."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        try:
            self.shutdown()
            self.server_close()
        except Exception as exc:
            logger.warning("Error during GtfsRtServer shutdown: %s", exc)
        if self._server_thread is not None and self._server_thread.is_alive():
            if threading.current_thread() is not self._server_thread:
                self._server_thread.join(timeout=2.0)
