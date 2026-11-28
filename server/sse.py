"""Thread-safe Server-Sent Events broadcaster and subscriber manager."""

import logging
import queue
import threading
import time
from typing import Dict, List, Optional

from railmon_http import validate_positive_int
from server.models import ClientConnection, ServerHealthMetrics

logger = logging.getLogger("railmon.server.sse")

# Maximum queued events per subscriber before dropping oldest frames
MAX_CLIENT_QUEUE_DEPTH: int = 100

# Default cap on simultaneous persistent SSE subscribers (strictly below HTTP max_connections)
MAX_SSE_CLIENTS: int = 32


class SSECapacityError(RuntimeError):
    """Raised when SSEBroadcaster rejects a new subscriber at max_clients capacity."""


class SSEBroadcaster:
    """Manages active browser SSE client queues with non-blocking bounded fan-out."""

    def __init__(
        self,
        max_queue_depth: int = MAX_CLIENT_QUEUE_DEPTH,
        max_clients: int = MAX_SSE_CLIENTS,
    ) -> None:
        """Initialize broadcaster with thread lock, client capacity, and tracking counters."""
        self._max_queue_depth: int = validate_positive_int(
            "max_queue_depth", max_queue_depth, minimum=1
        )
        self._max_clients: int = validate_positive_int(
            "max_clients", max_clients, minimum=1
        )
        self._lock = threading.Lock()
        self._clients: Dict[str, ClientConnection] = {}
        self._started_at: float = time.time()
        self._total_clients_served: int = 0
        self._total_clients_rejected: int = 0
        self._total_broadcasts: int = 0
        self._total_frames_sent: int = 0
        self._total_frames_dropped: int = 0

    @property
    def max_clients(self) -> int:
        """Return configured maximum number of concurrent SSE subscribers."""
        return self._max_clients

    @property
    def total_clients_rejected(self) -> int:
        """Return cumulative count of SSE connections rejected due to capacity cap."""
        with self._lock:
            return self._total_clients_rejected

    def register_client(self, remote_addr: str = "127.0.0.1") -> ClientConnection:
        """Register a new SSE client subscriber and return its connection descriptor.

        Raises:
            SSECapacityError: When active subscribers have reached max_clients.
        """
        conn = ClientConnection(
            remote_addr=remote_addr,
            queue=queue.Queue(maxsize=self._max_queue_depth),
        )
        with self._lock:
            if len(self._clients) >= self._max_clients:
                self._total_clients_rejected += 1
                active = len(self._clients)
                rejected_total = self._total_clients_rejected
                logger.warning(
                    "SSE client capacity reached (%d/%d); rejecting subscriber [ip=%s, total_rejected=%d]",
                    active,
                    self._max_clients,
                    remote_addr,
                    rejected_total,
                )
                raise SSECapacityError(
                    f"Maximum SSE client capacity reached ({self._max_clients})"
                )
            self._clients[conn.client_id] = conn
            self._total_clients_served += 1
            client_count = len(self._clients)

        logger.info(
            "Registered SSE subscriber [id=%s, ip=%s, active_clients=%d/%d]",
            conn.client_id,
            remote_addr,
            client_count,
            self._max_clients,
        )
        return conn

    def unregister_client(
        self,
        client_id: str,
        *,
        notify_close: bool = False,
    ) -> Optional[ClientConnection]:
        """Unregister an SSE client, purge its queue, and optionally push a wake-up sentinel."""
        with self._lock:
            conn = self._clients.pop(client_id, None)
            client_count = len(self._clients)

        if conn is not None:
            logger.info(
                "Unregistered SSE subscriber [id=%s, frames_sent=%d, active_clients=%d]",
                client_id,
                conn.frames_sent,
                client_count,
            )
            while not conn.queue.empty():
                try:
                    conn.queue.get_nowait()
                except queue.Empty:
                    break
            if notify_close:
                try:
                    conn.queue.put_nowait(None)  # type: ignore[arg-type]
                except queue.Full:
                    pass
        return conn

    def shutdown(self) -> int:
        """Unregister all active SSE clients and wake their blocked handler threads."""
        with self._lock:
            client_ids = list(self._clients.keys())
        for cid in client_ids:
            self.unregister_client(cid, notify_close=True)
        return len(client_ids)

    def broadcast(self, event_name: str, payload_json: str) -> int:
        """Broadcast an event frame to all registered subscriber queues.

        Does NOT call conn.touch() on enqueue so dead/stalled consumers are still evicted
        when their handler threads fail to flush.

        Returns:
            Number of clients the event was successfully enqueued for.
        """
        with self._lock:
            subscribers = list(self._clients.values())
            self._total_broadcasts += 1

        enqueued_count = 0
        for conn in subscribers:
            try:
                conn.queue.put_nowait((event_name, payload_json))
                conn.frames_sent += 1
                enqueued_count += 1
                with self._lock:
                    self._total_frames_sent += 1
            except queue.Full:
                # Bounded queue enforcement: drop oldest frame to prevent thread lockup
                try:
                    conn.queue.get_nowait()
                except queue.Empty:
                    pass

                try:
                    conn.queue.put_nowait((event_name, payload_json))
                    conn.frames_sent += 1
                    conn.frames_dropped += 1
                    enqueued_count += 1
                    with self._lock:
                        self._total_frames_sent += 1
                        self._total_frames_dropped += 1
                except queue.Full:
                    conn.frames_dropped += 1
                    with self._lock:
                        self._total_frames_dropped += 1
                    logger.warning(
                        "Subscriber queue saturated; dropped frame for [id=%s]",
                        conn.client_id,
                    )
        return enqueued_count

    def broadcast_keepalive(self) -> int:
        """Broadcast a standard SSE comment line (:ping) to keep sockets open."""
        return self.broadcast("", ":ping")

    def cleanup_stale_clients(self, max_idle_sec: float = 60.0) -> int:
        """Evict subscribers that have not acknowledged or read frames for max_idle_sec."""
        cutoff = time.time() - max_idle_sec
        stale_ids: List[str] = []

        with self._lock:
            for client_id, conn in self._clients.items():
                if conn.last_event_at < cutoff:
                    stale_ids.append(client_id)

        for client_id in stale_ids:
            logger.warning("Evicting stale SSE subscriber [id=%s]", client_id)
            self.unregister_client(client_id)
        return len(stale_ids)

    @property
    def client_count(self) -> int:
        """Return the number of currently connected clients."""
        with self._lock:
            return len(self._clients)

    def get_health_metrics(self) -> ServerHealthMetrics:
        """Compute point-in-time operational metrics snapshot."""
        now = time.time()
        with self._lock:
            return ServerHealthMetrics(
                uptime_seconds=now - self._started_at,
                active_clients=len(self._clients),
                total_clients_served=self._total_clients_served,
                total_broadcasts=self._total_broadcasts,
                total_frames_sent=self._total_frames_sent,
                total_frames_dropped=self._total_frames_dropped,
                max_clients=self._max_clients,
                total_clients_rejected=self._total_clients_rejected,
            )
