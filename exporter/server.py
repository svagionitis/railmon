"""Embedded multi-threaded HTTP daemon for serving Prometheus & OpenMetrics."""

import errno
import gzip
import json
import logging
import signal
import sys
import threading
import time
from types import FrameType
from typing import Any, Optional, Tuple

from exporter.collector import MetricRegistry, ProcessMetricsCollector
from exporter.formatter import MetricsFormatter, negotiate_metrics_format
from exporter.models import ExporterConfig
from railmon_http import (
    BoundedHTTPRequestHandler,
    BoundedHTTPServer,
    HttpLimitsConfig,
)

logger = logging.getLogger("railmon.exporter.server")


class MetricsRequestHandler(BoundedHTTPRequestHandler):
    """Dispatches /metrics, /health, / and OPTIONS preflight requests."""

    server: "MetricsServer"
    protocol_version: str = "HTTP/1.1"

    def log_message(self, log_format: str, *args: Any) -> None:
        """Route standard HTTP request logs to Python logging framework."""
        logger.debug(
            "%s - - [%s] %s",
            self.client_address[0] if self.client_address else "127.0.0.1",
            self.log_date_time_string(),
            log_format % args,
        )

    def do_HEAD(self) -> None:
        """Handle HTTP HEAD requests (same headers as GET with zero body bytes)."""
        self._dispatch(is_head=True)

    def do_GET(self) -> None:
        """Handle HTTP GET requests."""
        self._dispatch(is_head=False)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        self.send_response(204)
        self._add_cors_headers()
        self.send_header("Allow", "GET, HEAD, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self) -> None:
        """Reject POST requests with 405 Method Not Allowed."""
        self._send_error(405, "Method Not Allowed: Exporter daemon is read-only")

    def do_PUT(self) -> None:
        """Reject PUT requests with 405 Method Not Allowed."""
        self._send_error(405, "Method Not Allowed: Exporter daemon is read-only")

    def do_DELETE(self) -> None:
        """Reject DELETE requests with 405 Method Not Allowed."""
        self._send_error(405, "Method Not Allowed: Exporter daemon is read-only")

    def _add_cors_headers(self) -> None:
        """Inject permissive CORS headers only when explicitly enabled in ExporterConfig."""
        cfg = getattr(self.server, "config", None)
        if cfg is not None and getattr(cfg, "cors_enabled", False):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Accept, Accept-Encoding, Content-Type",
            )

    def _dispatch(self, is_head: bool = False) -> None:
        """Route request by URL path."""
        try:
            raw_path = self.path.split("?")[0].rstrip("/") or "/"

            if raw_path == "/metrics":
                self._handle_metrics(is_head=is_head)
            elif raw_path == "/health":
                self._handle_health(is_head=is_head)
            elif raw_path in ("/", "/index.html"):
                self._handle_index(is_head=is_head)
            else:
                self._send_error(404, f"Endpoint '{raw_path}' not found")
        except TimeoutError:
            self._record_server_timeout()
            self.close_connection = True
            logger.debug("Client timed out during metrics request processing")
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.debug("Client disconnected abruptly during request processing")

    def _handle_metrics(self, is_head: bool = False) -> None:
        """Collect, negotiate format, serialize, and emit metrics payload."""
        t_start = time.perf_counter()
        status_code = 200

        try:
            families = self.server.registry.collect_all()
            if (time.perf_counter() - t_start) > self.server.config.scrape_timeout_seconds:
                status_code = 504
                self.close_connection = True
                self._record_server_timeout()
                self._send_error(
                    504,
                    "Gateway Timeout: metrics scrape exceeded configured deadline",
                )
                return

            accept_header = self.headers.get("Accept")
            content_type, format_mode = negotiate_metrics_format(accept_header)

            if format_mode == "openmetrics":
                body = MetricsFormatter.format_openmetrics_100(families)
            else:
                body = MetricsFormatter.format_prometheus_004(families)

            from railmon_http import accepts_gzip

            content_encoding: Optional[str] = None
            if self.server.config.gzip_compression and len(body) > 256:
                if accepts_gzip(self.headers.get("Accept-Encoding")):
                    body = gzip.compress(body, compresslevel=6)
                    content_encoding = "gzip"

            self.send_response(status_code)
            self._add_cors_headers()
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Vary", "Accept, Accept-Encoding")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            if content_encoding:
                self.send_header("Content-Encoding", content_encoding)
            self.end_headers()

            if not is_head:
                self.wfile.write(body)

        except TimeoutError:
            self._record_server_timeout()
            self.close_connection = True
            logger.debug("Scraper client timed out during metrics response write")
            status_code = 499
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            logger.debug("Scraper client disconnected abruptly during metrics write")
            status_code = 499
        except Exception as exc:
            logger.error("Scrape generation failed: %s", exc, exc_info=True)
            status_code = 500
            try:
                self._send_error(500, f"Internal Scrape Error: {exc}")
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
                logger.debug("Failed sending 500 error to disconnected scraper client")
        finally:
            duration = time.perf_counter() - t_start
            self._record_scrape_telemetry(duration, "/metrics", status_code)

    def _record_scrape_telemetry(self, duration: float, endpoint: str, status: int) -> None:
        """Find registered ProcessMetricsCollector and record scrape telemetry."""
        for coll in getattr(self.server.registry, "_collectors", []):
            if isinstance(coll, ProcessMetricsCollector):
                coll.record_scrape(duration=duration, endpoint=endpoint, status=status)
                break

    def _handle_health(self, is_head: bool = False) -> None:
        """Emit operational health status as application/json."""
        uptime = time.time() - self.server._started_at
        collectors_count = len(getattr(self.server.registry, "_collectors", []))
        payload = {
            "status": "ok",
            "uptime_seconds": round(uptime, 2),
            "collectors_registered": collectors_count,
            "timestamp": time.time(),
            "version": getattr(self.server.config, "version", "1.0.0"),
            "active_http_connections": self.server.active_connections,
            "max_http_connections": self.server.max_connections,
            "rejected_http_connections": self.server.rejected_connections,
        }
        body = json.dumps(payload, indent=2).encode("utf-8")

        self.send_response(200)
        self._add_cors_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()

        if not is_head:
            self.wfile.write(body)

    def _handle_index(self, is_head: bool = False) -> None:
        """Serve semantic HTML landing page with endpoint index and scraper config."""
        html = (
            "<!DOCTYPE html>\n"
            "<html lang=\"en\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\">\n"
            "  <title>Railmon Operational Exporter</title>\n"
            "  <style>\n"
            "    body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; "
            "margin: 2rem; background: #0f172a; color: #f8fafc; line-height: 1.5; }\n"
            "    h1 { color: #38bdf8; font-size: 1.75rem; margin-bottom: 0.5rem; }\n"
            "    a { color: #38bdf8; text-decoration: none; }\n"
            "    a:hover { text-decoration: underline; }\n"
            "    pre { background: #1e293b; padding: 1rem; border-radius: 6px; overflow-x: auto; color: #a5f3fc; }\n"
            "    ul { list-style: square; padding-left: 1.5rem; }\n"
            "    li { margin: 0.5rem 0; }\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            "  <h1>Railmon Operational Metrics Exporter</h1>\n"
            "  <p>Real-time Greek railway operational telemetry exporter conforming to "
            "Prometheus 0.0.4 and OpenMetrics 1.0.0.</p>\n"
            "  <h2>Available Endpoints</h2>\n"
            "  <ul>\n"
            "    <li><a href=\"/metrics\">/metrics</a> &mdash; Real-time Prometheus/OpenMetrics scrape endpoint</li>\n"
            "    <li><a href=\"/health\">/health</a> &mdash; Operational health check (JSON)</li>\n"
            "  </ul>\n"
            "  <h2>Prometheus Scrape Configuration</h2>\n"
            "  <pre>scrape_configs:\n"
            "  - job_name: 'railmon'\n"
            "    scrape_interval: 15s\n"
            "    static_configs:\n"
            f"      - targets: ['{self.server.server_address[0]}:{self.server.server_port}']</pre>\n"
            "</body>\n"
            "</html>\n"
        )
        body = html.encode("utf-8")

        self.send_response(200)
        self._add_cors_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()

        if not is_head:
            self.wfile.write(body)

    def _send_error(self, status_code: int, message: str) -> None:
        """Emit structured JSON error response (RFC 9110 Allow/close on 405, bodyless HEAD)."""
        try:
            payload = {"error": message, "status": status_code}
            body = json.dumps(payload).encode("utf-8")
            if status_code == 405:
                self.close_connection = True

            self.send_response(status_code)
            self._add_cors_headers()
            if status_code == 405:
                self.send_header("Allow", "GET, HEAD, OPTIONS")
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            if self.close_connection:
                self.send_header("Connection", "close")
            self.end_headers()
            if getattr(self, "command", "GET") != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError):
            logger.debug("Failed sending error response to disconnected client")


class MetricsServer(BoundedHTTPServer):
    """Multi-threaded bounded HTTP server daemon managing Prometheus scrapers."""

    def __init__(
        self,
        server_address: Tuple[str, int],
        registry: MetricRegistry,
        config: Optional[ExporterConfig] = None,
    ) -> None:
        """Initialize metrics server daemon with address, registry, and configuration.

        Args:
            server_address: (host, port) tuple for TCP socket binding.
            registry: MetricRegistry instance orchestrating domain metric collectors.
            config: Optional ExporterConfig holding runtime settings.

        Raises:
            OSError: When socket binding fails (e.g. port already in use).
        """
        self.registry = registry
        self.config = config or ExporterConfig(
            host=server_address[0],
            port=server_address[1] if server_address[1] != 0 else 9100,
        )
        self._shutdown_event = threading.Event()
        self._server_thread: Optional[threading.Thread] = None
        self._started_at: float = time.time()

        http_limits = HttpLimitsConfig.for_request_timeout(
            self.config.scrape_timeout_seconds,
            max_connections=self.config.max_connections,
        )

        try:
            super().__init__(
                server_address,
                MetricsRequestHandler,
                http_limits=http_limits,
                allowed_hosts=self.config.allowed_hosts,
            )
        except OSError as exc:
            if exc.errno == errno.EADDRINUSE:
                logger.error(
                    "Port %d is already in use on interface '%s'. "
                    "Specify an alternate port using '--port <number>'.",
                    server_address[1],
                    server_address[0],
                )
            raise

        logger.info(
            "Initialized MetricsServer on %s:%d (CORS=%s, gzip=%s, max_conn=%d, timeout=%.1fs)",
            self.server_address[0],
            self.server_port,
            self.config.cors_enabled,
            self.config.gzip_compression,
            self.http_limits.max_connections,
            self.http_limits.request_timeout_sec,
        )

    @property
    def is_running(self) -> bool:
        """Return True if the server daemon is actively servicing requests."""
        return not self._shutdown_event.is_set()

    @property
    def server_url(self) -> str:
        """Return canonical HTTP URL for the root landing page."""
        host = self.server_address[0]
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.server_port}/"

    def start(self, background: bool = False) -> None:
        """Start servicing incoming HTTP requests.

        Args:
            background: If True, executes serve_forever in a dedicated daemon thread.
                        If False, blocks the calling thread until stop() is invoked.
        """
        self._shutdown_event.clear()
        if background:
            self._server_thread = threading.Thread(
                target=self.serve_forever,
                name="MetricsServerThread",
                daemon=True,
            )
            self._server_thread.start()
            logger.info("MetricsServer running in background thread at %s", self.server_url)
        else:
            logger.info("Serving Prometheus metrics at %s (Press Ctrl+C to stop)", self.server_url)
            try:
                self.serve_forever()
            except KeyboardInterrupt:
                logger.info("Received KeyboardInterrupt; stopping MetricsServer...")
            finally:
                self.stop()

    def stop(self) -> None:
        """Gracefully terminate server and release socket resources."""
        if self._shutdown_event.is_set():
            return
        self._shutdown_event.set()
        logger.info("Shutting down MetricsServer daemon...")
        try:
            self.safe_shutdown()
            self.server_close()
        except Exception as exc:
            logger.warning("Error during MetricsServer shutdown: %s", exc)

        if self._server_thread is not None and self._server_thread.is_alive():
            if threading.current_thread() is not self._server_thread:
                self._server_thread.join(timeout=2.0)
        logger.info("MetricsServer stopped successfully.")

    def setup_signal_handlers(self) -> None:
        """Attach SIGINT and SIGTERM handlers for graceful process shutdown."""
        def _handler(signum: int, frame: Optional[FrameType]) -> None:
            sig_name = signal.Signals(signum).name
            logger.info("Received signal %s; shutting down MetricsServer daemon...", sig_name)
            self.stop()
            sys.exit(0)

        try:
            signal.signal(signal.SIGINT, _handler)
            signal.signal(signal.SIGTERM, _handler)
        except (ValueError, AttributeError):
            # Non-main thread or unsupported OS platform
            pass
