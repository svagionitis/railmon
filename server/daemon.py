"""Embedded zero-dependency HTTP server daemon for railmon web map."""

import errno
import logging
import signal
import sys
import threading
import time
import webbrowser
from types import FrameType
from typing import Callable, Optional, Tuple

from railmon_http import BoundedHTTPServer, HttpLimitsConfig
from server.handler import WebMapRequestHandler
from server.models import ServerConfig
from server.sse import SSEBroadcaster
from tui.state import TrainStateStore

logger = logging.getLogger("railmon.server.daemon")


class WebMapServer(BoundedHTTPServer):
    """Multi-threaded bounded HTTP server daemon managing state, SSE bus, and client lifecycle."""

    def __init__(
        self,
        server_address: Tuple[str, int],
        store: TrainStateStore,
        broadcaster: SSEBroadcaster,
        config: Optional[ServerConfig] = None,
    ) -> None:
        """Initialize web map server with domain state, SSE bus, and runtime config.

        Args:
            server_address: (host, port) tuple for TCP socket binding.
            store: TrainStateStore maintaining active train positions and watchdog state.
            broadcaster: SSEBroadcaster managing active browser subscriber queues.
            config: Immutable ServerConfig instance with runtime flags.

        Raises:
            OSError: When socket binding fails (e.g. port already in use).
        """
        self.store = store
        self.broadcaster = broadcaster
        self.config = config or ServerConfig(
            host=server_address[0],
            port=server_address[1] if server_address[1] != 0 else 8080,
        )
        self._shutdown_event = threading.Event()
        self._server_thread: Optional[threading.Thread] = None
        self._started_at: float = time.time()

        # Initialize embedded Prometheus & OpenMetrics Operational Registry (F-8.2)
        from exporter.collector import (
            MetricRegistry,
            ProcessMetricsCollector,
            StorageMetricsCollector,
            StreamMetricsCollector,
            TrainTelemetryCollector,
            WatchdogMetricsCollector,
        )
        import train_tracker

        self.metric_registry = MetricRegistry()
        self.metric_registry.register(TrainTelemetryCollector(store=self.store))
        self.metric_registry.register(WatchdogMetricsCollector(store=self.store))
        self.metric_registry.register(StreamMetricsCollector(store=self.store))
        if self.config.db_path and self.config.record_enabled:
            self.metric_registry.register(
                StorageMetricsCollector(db_path=self.config.db_path)
            )
        self.process_collector = ProcessMetricsCollector(
            version=train_tracker.__version__
        )
        self.metric_registry.register(self.process_collector)

        # Initialize embedded GTFS-Realtime v2.0 Feed Exporter (F-8.3)
        from gtfs_rt.mapper import FeedCache, GtfsRtMapper

        self.gtfs_mapper = GtfsRtMapper()
        self.gtfs_cache = FeedCache(self.gtfs_mapper)

        http_limits = HttpLimitsConfig.for_request_timeout(
            self.config.request_timeout_sec,
            max_connections=self.config.max_connections,
            sse_write_timeout_sec=self.config.sse_write_timeout_sec,
        )

        try:
            super().__init__(
                server_address,
                WebMapRequestHandler,
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
            "Initialized WebMapServer on %s:%d (CORS=%s, TileProvider=%s, max_conn=%d, timeout=%.1fs)",
            self.server_address[0],
            self.server_port,
            self.config.cors_enabled,
            self.config.tile_provider.value,
            self.http_limits.max_connections,
            self.http_limits.request_timeout_sec,
        )

    @property
    def is_running(self) -> bool:
        """Check whether server is actively servicing requests."""
        return not self._shutdown_event.is_set()

    @property
    def server_url(self) -> str:
        """Return formatted canonical URL for the root dashboard."""
        host = self.server_address[0]
        if host in ("0.0.0.0", ""):
            host = "127.0.0.1"
        return f"http://{host}:{self.server_port}/"

    def service_actions(self) -> None:
        """Periodic maintenance tasks executed within the server loop.

        Automatically called by BaseServer during each select/poll tick.
        Purges stale SSE clients that have not received frames within 60s.
        """
        super().service_actions()
        purged = self.broadcaster.cleanup_stale_clients(max_idle_sec=60.0)
        if purged > 0:
            logger.debug("Reaped %d idle SSE subscriber connections", purged)

    def start(self, background: bool = False) -> None:
        """Start servicing incoming HTTP and SSE connections.

        Args:
            background: If True, executes serve_forever in a daemon thread and returns.
                        If False, blocks the calling thread until stop() is invoked.
        """
        self._shutdown_event.clear()
        if self.config.open_browser:
            self._launch_browser_async()

        if background:
            self._server_thread = threading.Thread(
                target=self.serve_forever,
                name="WebMapServerThread",
                daemon=True,
            )
            self._server_thread.start()
            logger.info("WebMapServer running in background thread at %s", self.server_url)
        else:
            logger.info("Serving railmon web map at %s (Press Ctrl+C to stop)", self.server_url)
            self.serve_forever()

    def stop(self) -> None:
        """Signal graceful server shutdown and close listening sockets."""
        if self._shutdown_event.is_set():
            return
        logger.info("Shutting down WebMapServer...")
        self._shutdown_event.set()
        self.shutdown()
        self.server_close()
        if self._server_thread is not None and self._server_thread.is_alive():
            self._server_thread.join(timeout=3.0)
        logger.info("WebMapServer stopped cleanly.")

    def _launch_browser_async(self, delay_sec: float = 0.5) -> None:
        """Launch default browser after a brief startup delay."""
        url = self.server_url

        def _open() -> None:
            time.sleep(delay_sec)
            try:
                logger.info("Launching default web browser: %s", url)
                webbrowser.open(url)
            except Exception as exc:
                logger.warning("Failed to auto-launch browser: %s", exc)

        launcher_thread = threading.Thread(
            target=_open, name="BrowserLauncherThread", daemon=True
        )
        launcher_thread.start()


def install_signal_handlers(
    server: WebMapServer,
    on_exit_callback: Optional[Callable[[], None]] = None,
) -> None:
    """Register SIGINT and SIGTERM handlers to cleanly terminate server daemon."""

    def _handler(signum: int, _frame: Optional[FrameType]) -> None:
        sig_name = signal.Signals(signum).name
        logger.info("Received termination signal %s; shutting down daemon...", sig_name)
        if on_exit_callback is not None:
            try:
                on_exit_callback()
            except Exception as exc:
                logger.error("Error during on_exit_callback: %s", exc)
        server.stop()
        sys.exit(0)

    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except (ValueError, AttributeError):
        # signal registration fails if not on main thread (e.g. unit tests)
        logger.debug("Signal handlers skipped (not running on main thread)")
