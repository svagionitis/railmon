"""HTTP request handler for web map SPA, JSON REST APIs, and SSE streams."""

import gzip
import json
import logging
import math
import queue
import secrets
import socket
import time
from typing import TYPE_CHECKING, Any, Dict, List, Optional
import urllib.parse

from exporter.formatter import MetricsFormatter, negotiate_metrics_format
from gtfs_rt.models import GTFS_RT_VERSION, OutputFormat
from gtfs_rt.server import etag_matches, resolve_gtfs_route
from railmon_http import BoundedHTTPRequestHandler
from server.assets import get_geojson_asset, get_spa_html
from server.sse import SSECapacityError

if TYPE_CHECKING:
    from server.daemon import WebMapServer

logger = logging.getLogger("railmon.server.handler")

_API_CSP_POLICY: str = "default-src 'none'; frame-ancestors 'none'"


def build_spa_csp_header(csp_nonce: str) -> str:
    """Build a strict Content-Security-Policy header scoped to a per-request nonce."""
    return (
        "default-src 'none'; "
        f"script-src 'nonce-{csp_nonce}' https://unpkg.com/leaflet@1.9.4/dist/leaflet.js; "
        "style-src 'self' 'unsafe-inline' https://unpkg.com/leaflet@1.9.4/dist/leaflet.css; "
        "img-src 'self' data: https://*.basemaps.cartocdn.com "
        "https://*.tile.openstreetmap.org https://unpkg.com/leaflet@1.9.4/dist/images/; "
        "connect-src 'self'; "
        "font-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'none'; "
        "frame-ancestors 'none'"
    )


def _sanitize_float(val: Any, default: float = 0.0) -> float:
    """Sanitize float values, guarding against None, NaN, and Infinity."""
    if val is None:
        return default
    try:
        f = float(val)
        if math.isnan(f) or math.isinf(f):
            return default
        return f
    except (ValueError, TypeError):
        return default


class WebMapRequestHandler(BoundedHTTPRequestHandler):
    """Dispatches REST routes, embedded static SPA, and real-time SSE streams."""

    server: "WebMapServer"
    protocol_version: str = "HTTP/1.1"

    def log_message(self, log_format: str, *args: Any) -> None:
        """Route request logs to Python logging instead of stderr."""
        logger.debug(
            "%s - - [%s] %s",
            self.client_address[0] if self.client_address else "127.0.0.1",
            self.log_date_time_string(),
            log_format % args,
        )

    def do_HEAD(self) -> None:
        """Handle incoming HEAD request (same headers as GET, zero body)."""
        self._dispatch(is_head=True)

    def do_GET(self) -> None:
        """Handle incoming GET request."""
        self._dispatch(is_head=False)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight OPTIONS request."""
        self.send_response(204)
        self._add_cors_headers()
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        """Reject POST requests with 405 Method Not Allowed."""
        self._send_json_error(405, "Method Not Allowed: Web daemon is read-only")

    def do_PUT(self) -> None:
        """Reject PUT requests with 405 Method Not Allowed."""
        self._send_json_error(405, "Method Not Allowed: Web daemon is read-only")

    def do_DELETE(self) -> None:
        """Reject DELETE requests with 405 Method Not Allowed."""
        self._send_json_error(405, "Method Not Allowed: Web daemon is read-only")

    def _dispatch(self, is_head: bool = False) -> None:
        """Route path to appropriate handler function."""
        raw_path = self.path.split("?")[0].rstrip("/") or "/"

        if raw_path in ("/", "/index.html"):
            self._handle_root(is_head=is_head)
        elif raw_path == "/api/stream":
            if is_head:
                self._handle_sse_stream_head()
            else:
                self._handle_sse_stream()
        elif raw_path == "/api/trains":
            self._handle_trains(is_head=is_head)
        elif raw_path in (
            "/api/tracks",
            "/api/stations",
            "/api/infrastructure",
            "/api/speed-limits",
        ):
            self._handle_static_geo(raw_path, is_head=is_head)
        elif raw_path == "/api/conflicts":
            self._handle_conflicts(is_head=is_head)
        elif raw_path == "/api/health":
            self._handle_health(is_head=is_head)
        elif raw_path in ("/metrics", "/api/metrics"):
            self._handle_metrics(is_head=is_head)
        elif raw_path.startswith("/gtfs-rt/"):
            self._handle_gtfs_rt(is_head=is_head)
        elif raw_path.startswith("/api/history/train/"):
            train_id = raw_path.split("/")[-1]
            self._handle_train_history(train_id, is_head=is_head)
        else:
            self._send_json_error(404, f"Endpoint '{raw_path}' not found")

    def _handle_root(self, is_head: bool = False) -> None:
        """Serve embedded single-page application dashboard HTML with strict per-request CSP."""
        cfg = getattr(self.server, "config", None)
        csp_nonce = secrets.token_urlsafe(16)
        html_content = get_spa_html(cfg, csp_nonce=csp_nonce).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html_content)))
        self.send_header("Cache-Control", "no-store")
        self._add_security_headers(csp_policy=build_spa_csp_header(csp_nonce))
        self._add_cors_headers()
        self.end_headers()
        if not is_head:
            self.wfile.write(html_content)

    def _handle_trains(self, is_head: bool = False) -> None:
        """Return JSON snapshot of all currently active trains."""
        snapshot = self.server.store.get_snapshot()
        train_list: List[Dict[str, Any]] = []

        for record in snapshot.trains.values():
            p = record.position
            train_id_val = str(getattr(p, "train_id", getattr(p, "id", "")))
            train_num_val = str(getattr(p, "train_number", train_id_val))
            speed_val = round(_sanitize_float(p.speed, 0.0), 1)
            delay_val = round(_sanitize_float(p.delay, 0.0), 1)
            heading_raw = getattr(
                p, "heading", getattr(p, "track_heading", getattr(p, "course", 0.0))
            )
            heading_val = round(_sanitize_float(heading_raw, 0.0), 1)

            train_dict: Dict[str, Any] = {
                "id": train_id_val,
                "train_number": train_num_val,
                "lat": _sanitize_float(p.lat, 0.0),
                "lng": _sanitize_float(p.lng, 0.0),
                "speed": speed_val,
                "delay": delay_val,
                "heading": heading_val,
                "corridor": p.corridor or "",
                "service_type": p.service_type or "",
                "train_type": p.train_type or "",
                "origin": p.origin or "",
                "destination": p.destination or "",
                "next_station": p.next_station or "",
                "last_update": record.last_updated,
                "trail_count": len(record.trail),
            }
            train_list.append(train_dict)

        payload_bytes = json.dumps(train_list, ensure_ascii=False).encode("utf-8")
        self._send_bytes_response(
            200, payload_bytes, "application/json; charset=utf-8", is_head=is_head
        )

    def _handle_static_geo(self, path: str, is_head: bool = False) -> None:
        """Serve cached GeoJSON layer for tracks, stations, infrastructure, or speed limits."""
        content_type = "application/geo+json; charset=utf-8"
        if path == "/api/speed-limits":
            content_type = "application/json; charset=utf-8"

        raw_json_str = get_geojson_asset(path)
        payload_bytes = raw_json_str.encode("utf-8")
        self._send_bytes_response(200, payload_bytes, content_type, is_head=is_head)

    def _handle_conflicts(self, is_head: bool = False) -> None:
        """Return active headway and opposing track conflict alerts."""
        snapshot = self.server.store.get_snapshot()
        conflicts_dict: Dict[str, Dict[str, Any]] = {}

        # Collect from snapshot.active_conflicts and individual train records
        all_alerts = list(getattr(snapshot, "active_conflicts", ()))
        for record in snapshot.trains.values():
            if record.active_conflict is not None:
                all_alerts.append(record.active_conflict)

        for c in all_alerts:
            train_a = str(getattr(c, "train_id_1", getattr(c, "train_a_id", "")))
            train_b = str(getattr(c, "train_id_2", getattr(c, "train_b_id", "")))
            cid = str(getattr(c, "alert_id", f"CONF-{train_a}-{train_b}"))
            if cid in conflicts_dict:
                continue

            corridor = str(
                getattr(
                    c,
                    "corridor_name",
                    getattr(c, "corridor_id", getattr(c, "corridor", "")),
                )
            )
            dist_km = getattr(c, "distance_km", None)
            if dist_km is None and hasattr(c, "distance_m"):
                dist_km = round(c.distance_m / 1000.0, 2)
            elif dist_km is not None:
                dist_km = round(float(dist_km), 2)
            else:
                dist_km = 0.0

            conflicts_dict[cid] = {
                "conflict_id": cid,
                "severity": (
                    c.severity.name if hasattr(c.severity, "name") else str(c.severity)
                ),
                "type": (
                    c.conflict_type.name
                    if hasattr(c.conflict_type, "name")
                    else str(c.conflict_type)
                ),
                "train_a": train_a,
                "train_b": train_b,
                "distance_km": dist_km,
                "closing_speed_kmh": round(
                    float(getattr(c, "closing_speed_kmh", 0.0)), 1
                ),
                "corridor": corridor,
                "message": str(getattr(c, "description", getattr(c, "message", ""))),
                "timestamp": getattr(
                    c, "timestamp", getattr(c, "detected_at", time.time())
                ),
            }

        conflicts = list(conflicts_dict.values())
        payload_bytes = json.dumps(conflicts, ensure_ascii=False).encode("utf-8")
        self._send_bytes_response(
            200, payload_bytes, "application/json; charset=utf-8", is_head=is_head
        )

    def _handle_health(self, is_head: bool = False) -> None:
        """Return operational health metrics, client count, and ingestion status."""
        health = self.server.broadcaster.get_health_metrics().to_dict()
        snapshot = self.server.store.get_snapshot()
        conn_status = (
            snapshot.connection_status.name
            if hasattr(snapshot.connection_status, "name")
            else str(snapshot.connection_status)
        )
        cfg = getattr(self.server, "config", None)
        tile_provider = cfg.tile_provider.value if cfg else "carto-dark"

        health.update(
            {
                "status": "healthy",
                "active_trains_count": len(snapshot.trains),
                "connection_status": conn_status,
                "total_messages_received": snapshot.total_messages_received,
                "tile_provider": tile_provider,
                "active_http_connections": self.server.active_connections,
                "max_http_connections": self.server.max_connections,
                "rejected_http_connections": self.server.rejected_connections,
            }
        )
        payload_bytes = json.dumps(health, ensure_ascii=False).encode("utf-8")
        self._send_bytes_response(
            200, payload_bytes, "application/json; charset=utf-8", is_head=is_head
        )

    def _handle_metrics(self, is_head: bool = False) -> None:
        """Serve Prometheus & OpenMetrics operational telemetry with content negotiation."""
        t_start = time.perf_counter()
        registry = getattr(self.server, "metric_registry", None)
        if registry is None:
            self._send_json_error(503, "MetricRegistry not initialized on web daemon")
            return

        families = registry.collect_all()
        accept = self.headers.get("Accept", "")
        content_type, fmt = negotiate_metrics_format(accept)

        if fmt == "openmetrics":
            body = MetricsFormatter.format_openmetrics_100(families)
        else:
            body = MetricsFormatter.format_prometheus_004(families)

        is_compressed = False
        accept_encoding = self.headers.get("Accept-Encoding", "")
        if "gzip" in accept_encoding:
            body = gzip.compress(body, compresslevel=6)
            is_compressed = True

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if is_compressed:
            self.send_header("Content-Encoding", "gzip")
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self._add_security_headers(csp_policy=_API_CSP_POLICY)
        self._add_cors_headers()
        self.end_headers()

        duration = time.perf_counter() - t_start
        proc = getattr(self.server, "process_collector", None)
        if proc is not None:
            proc.record_scrape(duration, endpoint="/metrics", status=200)

        if not is_head:
            self.wfile.write(body)

    def _handle_gtfs_rt(self, is_head: bool = False) -> None:
        """Serve GTFS-Realtime v2.0 protobuf or ProtoJSON feeds on /gtfs-rt/*."""
        gtfs_cache = getattr(self.server, "gtfs_cache", None)
        if gtfs_cache is None:
            self._send_json_error(503, "GTFS-RT exporter not initialized on web daemon")
            return

        parts = urllib.parse.urlsplit(self.path)
        route = resolve_gtfs_route(
            parts.path,
            query_str=parts.query,
            accept_hdr=self.headers.get("Accept", ""),
        )
        if route is None:
            self._send_json_error(404, f"Endpoint '{parts.path}' not found")
            return

        feed_kind, fmt = route
        snapshot = self.server.store.get_snapshot()
        cached = gtfs_cache.get_or_build(snapshot, feed_kind)

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
            self._add_security_headers(csp_policy=_API_CSP_POLICY)
            self._add_cors_headers()
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=5")
            self.send_header("X-GTFS-RT-Version", GTFS_RT_VERSION)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        is_compressed = False
        if len(body) > 256 and "gzip" in self.headers.get("Accept-Encoding", ""):
            body = gzip.compress(body, compresslevel=6)
            is_compressed = True

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", "public, max-age=5")
        self.send_header("X-GTFS-RT-Version", GTFS_RT_VERSION)
        self.send_header("X-Entity-Count", str(cached.entity_count))
        if is_compressed:
            self.send_header("Content-Encoding", "gzip")
        self._add_security_headers(csp_policy=_API_CSP_POLICY)
        self._add_cors_headers()
        self.end_headers()
        if not is_head:
            self.wfile.write(body)

    def _handle_train_history(self, train_id: str, is_head: bool = False) -> None:
        """Return GeoJSON LineString feature of recent breadcrumb coordinates for train."""
        snapshot = self.server.store.get_snapshot()
        record = snapshot.trains.get(train_id)

        if record is None:
            self._send_json_error(404, f"Train '{train_id}' not found in active state")
            return

        coords: List[List[float]] = []
        for crumb in record.trail:
            coords.append([_sanitize_float(crumb.lng, 0.0), _sanitize_float(crumb.lat, 0.0)])

        # Append current position as final point
        coords.append([
            _sanitize_float(record.position.lng, 0.0),
            _sanitize_float(record.position.lat, 0.0),
        ])

        geojson_feature = {
            "type": "Feature",
            "id": f"history-{train_id}",
            "geometry": {
                "type": "LineString",
                "coordinates": coords,
            },
            "properties": {
                "train_id": train_id,
                "breadcrumb_count": len(coords),
                "speed": _sanitize_float(record.position.speed, 0.0),
                "delay": _sanitize_float(record.position.delay, 0.0),
            },
        }

        payload_bytes = json.dumps(geojson_feature, ensure_ascii=False).encode("utf-8")
        self._send_bytes_response(
            200, payload_bytes, "application/geo+json; charset=utf-8", is_head=is_head
        )

    def _handle_sse_stream_head(self) -> None:
        """Handle HEAD request for /api/stream."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self._add_security_headers(csp_policy=_API_CSP_POLICY)
        self._add_cors_headers()
        self.end_headers()

    def _handle_sse_stream(self) -> None:
        """Maintain persistent HTTP/1.1 Server-Sent Events stream with bounded capacity and write timeout."""
        remote_ip = self.client_address[0] if self.client_address else "127.0.0.1"
        try:
            client_conn = self.server.broadcaster.register_client(remote_addr=remote_ip)
        except SSECapacityError:
            self._send_json_error(
                503,
                "Service Unavailable: maximum SSE subscribers reached",
                retry_after=self._http_limits.overload_retry_after_sec,
            )
            return

        client_id = client_conn.client_id
        self.prepare_sse_stream()
        logger.info("SSE client handshake complete [id=%s, ip=%s]", client_id, remote_ip)

        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "close")
            self._add_security_headers(csp_policy=_API_CSP_POLICY)
            self._add_cors_headers()
            self.end_headers()

            # 1. Send initial network snapshot
            init_payload = self._build_snapshot_json()
            self._send_sse_frame("init", init_payload)

            # 2. Main event streaming loop
            last_ping_time = time.time()
            while self.server.is_running:
                try:
                    item = client_conn.queue.get(timeout=1.0)
                    if item is None:
                        break
                    event_type, event_data = item
                    self._send_sse_frame(event_type, event_data)
                    client_conn.frames_sent += 1
                    client_conn.touch()
                except queue.Empty:
                    pass

                # Keepalive heartbeat every 15 seconds of silence (touch on successful flush)
                now = time.time()
                if now - last_ping_time >= 15.0:
                    self._send_sse_raw(":ping\n\n")
                    client_conn.touch()
                    last_ping_time = now

        except (TimeoutError, socket.timeout):
            self._record_server_timeout()
            logger.info("SSE client write timed out; evicting stalled subscriber [id=%s]", client_id)
        except (
            BrokenPipeError,
            ConnectionResetError,
            ConnectionAbortedError,
        ):
            logger.info("SSE client disconnected [id=%s]", client_id)
        except Exception as exc:
            logger.warning("SSE stream loop exception [id=%s]: %s", client_id, exc)
        finally:
            self.close_connection = True
            self.server.broadcaster.unregister_client(client_id)
            logger.debug("Cleaned up SSE client channel [id=%s]", client_id)

    def _build_snapshot_json(self) -> str:
        """Build initial state snapshot payload for the 'init' SSE event."""
        snapshot = self.server.store.get_snapshot()
        train_list: List[Dict[str, Any]] = []
        for record in snapshot.trains.values():
            p = record.position
            train_id_val = str(getattr(p, "train_id", getattr(p, "id", "")))
            heading_raw = getattr(
                p, "heading", getattr(p, "track_heading", getattr(p, "course", 0.0))
            )
            heading_val = float(heading_raw) if heading_raw is not None else 0.0
            train_list.append(
                {
                    "id": train_id_val,
                    "lat": float(p.lat),
                    "lng": float(p.lng),
                    "speed": float(p.speed or 0.0),
                    "delay": float(p.delay or 0.0),
                    "heading": heading_val,
                    "corridor": p.corridor or "",
                    "next_station": p.next_station or "",
                }
            )
        return json.dumps(
            {
                "server_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "active_trains_count": len(train_list),
                "trains": train_list,
            },
            ensure_ascii=False,
        )

    def _send_sse_frame(self, event_name: str, payload_json: str) -> None:
        """Write framed SSE event to output socket."""
        lines = [f"event: {event_name}"]
        for line in payload_json.split("\n"):
            lines.append(f"data: {line}")
        lines.append("\n")
        raw_msg = "\n".join(lines).encode("utf-8")
        self.wfile.write(raw_msg)
        self.wfile.flush()

    def _send_sse_raw(self, raw_str: str) -> None:
        """Write raw comment or heartbeat string to output socket."""
        self.wfile.write(raw_str.encode("utf-8"))
        self.wfile.flush()

    def _send_bytes_response(
        self,
        code: int,
        body: bytes,
        content_type: str,
        is_head: bool = False,
    ) -> None:
        """Send complete HTTP response headers and optional payload body."""
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self._add_security_headers(csp_policy=_API_CSP_POLICY)
        self._add_cors_headers()
        self.end_headers()
        if not is_head:
            self.wfile.write(body)

    def _send_json_error(
        self,
        code: int,
        message: str,
        *,
        retry_after: Optional[int] = None,
    ) -> None:
        """Emit clean JSON error payload with corresponding HTTP error status."""
        self.close_connection = True
        error_body = json.dumps(
            {
                "error": code,
                "message": message,
                "path": self.path,
            }
        ).encode("utf-8")
        self.send_response(code)
        if code == 405:
            self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(error_body)))
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.send_header("Connection", "close")
        self._add_security_headers(csp_policy=_API_CSP_POLICY)
        self._add_cors_headers()
        self.end_headers()
        if getattr(self, "command", "GET") != "HEAD":
            self.wfile.write(error_body)

    def _add_security_headers(self, csp_policy: Optional[str] = None) -> None:
        """Attach baseline HTTP security headers to responses."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if csp_policy:
            self.send_header("Content-Security-Policy", csp_policy)

    def _add_cors_headers(self) -> None:
        """Attach permissive CORS headers if enabled in server configuration."""
        if getattr(self.server, "config", None) and self.server.config.cors_enabled:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Accept, Authorization, If-None-Match",
            )
