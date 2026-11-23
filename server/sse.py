"""Thread-safe Server-Sent Events broadcaster and subscriber manager."""

import logging
import queue
import threading
import time
from typing import Dict, List, Optional

from server.models import ClientConnection, ServerHealthMetrics

logger = logging.getLogger("railmon.server.sse")

# Maximum queued events per subscriber before dropping oldest frames
MAX_CLIENT_QUEUE_DEPTH: int = 100


class SSEBroadcaster:
    """Manages active browser SSE client queues with non-blocking bounded fan-out."""

    def __init__(self, max_queue_depth: int = MAX_CLIENT_QUEUE_DEPTH) -> None:
        """Initialize broadcaster with thread lock and tracking counters."""
        self._max_queue_depth = max_queue_depth
        self._lock = threading.Lock()
        self._clients: Dict[str, ClientConnection] = {}
        self._started_at: float = time.time()
        self._total_clients_served: int = 0
        self._total_broadcasts: int = 0
        self._total_frames_sent: int = 0
        self._total_frames_dropped: int = 0

    def register_client(self, remote_addr: str = "127.0.0.1") -> ClientConnection:
        """Register a new SSE client subscriber and return its connection descriptor."""
        conn = ClientConnection(
            remote_addr=remote_addr,
            queue=queue.Queue(maxsize=self._max_queue_depth),
        )
        with self._lock:
            self._clients[conn.client_id] = conn
            self._total_clients_served += 1
            client_count = len(self._clients)

        logger.info(
            "Registered SSE subscriber [id=%s, ip=%s, active_clients=%d]",
            conn.client_id,
            remote_addr,
            client_count,
        )
        return conn

    def unregister_client(self, client_id: str) -> Optional[ClientConnection]:
        """Unregister an SSE client and purge its remaining queue."""
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
            # Drain remaining items in queue to release memory
            while not conn.queue.empty():
                try:
                    conn.queue.get_nowait()
                except queue.Empty:
                    break
        return conn

    def broadcast(self, event_name: str, payload_json: str) -> int:
        """Broadcast an event frame to all registered subscriber queues.

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
                conn.touch()
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
                    conn.touch()
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
            )
