"""Unit tests for Server-Sent Events broadcaster and memory management."""

import queue
import threading
import time
import unittest

from server.sse import SSEBroadcaster


class TestServerSSE(unittest.TestCase):
    """Test suite validating SSEBroadcaster multi-client fan-out and queue bounding."""

    def test_register_and_unregister_client(self) -> None:
        """TC-SSE-01: Broadcaster registers subscribers, tracks count, and frees queues."""
        broadcaster = SSEBroadcaster(max_queue_depth=10)
        self.assertEqual(broadcaster.client_count, 0)

        conn1 = broadcaster.register_client("127.0.0.1")
        self.assertEqual(broadcaster.client_count, 1)

        conn2 = broadcaster.register_client("192.168.1.100")
        self.assertEqual(broadcaster.client_count, 2)

        # Put an item in conn1 queue
        conn1.queue.put_nowait(("evt", "data"))
        self.assertFalse(conn1.queue.empty())

        # Unregister conn1
        unregistered = broadcaster.unregister_client(conn1.client_id)
        self.assertIsNotNone(unregistered)
        self.assertEqual(unregistered.client_id, conn1.client_id)
        self.assertEqual(broadcaster.client_count, 1)

        # Queue should have been drained to prevent memory leaks
        self.assertTrue(conn1.queue.empty())

        # Unregister non-existent returns None
        self.assertIsNone(broadcaster.unregister_client("non-existent-id"))
        self.assertEqual(broadcaster.client_count, 1)

        broadcaster.unregister_client(conn2.client_id)
        self.assertEqual(broadcaster.client_count, 0)

    def test_broadcast_delivers_to_all_clients(self) -> None:
        """TC-SSE-02: Broadcast enqueues message frames to all registered subscribers."""
        broadcaster = SSEBroadcaster(max_queue_depth=10)
        conn1 = broadcaster.register_client()
        conn2 = broadcaster.register_client()
        conn3 = broadcaster.register_client()

        delivered = broadcaster.broadcast("trainPositionsUx", '[{"id":"1599"}]')
        self.assertEqual(delivered, 3)

        for conn in (conn1, conn2, conn3):
            self.assertFalse(conn.queue.empty())
            event, data = conn.queue.get_nowait()
            self.assertEqual(event, "trainPositionsUx")
            self.assertEqual(data, '[{"id":"1599"}]')
            self.assertEqual(conn.frames_sent, 1)

        metrics = broadcaster.get_health_metrics()
        self.assertEqual(metrics.total_broadcasts, 1)
        self.assertEqual(metrics.total_frames_sent, 3)
        self.assertEqual(metrics.total_frames_dropped, 0)

    def test_bounded_queue_frame_drop_policy(self) -> None:
        """TC-SSE-03: Bounded client queues drop oldest frames when saturated."""
        max_depth = 3
        broadcaster = SSEBroadcaster(max_queue_depth=max_depth)
        conn = broadcaster.register_client()

        # Enqueue up to capacity
        for i in range(1, 4):
            broadcaster.broadcast("tick", f"msg-{i}")

        self.assertEqual(conn.queue.qsize(), 3)
        self.assertEqual(conn.frames_dropped, 0)

        # Enqueue 4th message: oldest ("msg-1") should be dropped to accommodate "msg-4"
        broadcaster.broadcast("tick", "msg-4")

        self.assertEqual(conn.queue.qsize(), 3)
        self.assertEqual(conn.frames_dropped, 1)

        # Read queue: should contain msg-2, msg-3, msg-4
        m2 = conn.queue.get_nowait()
        m3 = conn.queue.get_nowait()
        m4 = conn.queue.get_nowait()
        self.assertEqual(m2[1], "msg-2")
        self.assertEqual(m3[1], "msg-3")
        self.assertEqual(m4[1], "msg-4")

        metrics = broadcaster.get_health_metrics()
        self.assertEqual(metrics.total_frames_dropped, 1)

    def test_broadcast_keepalive(self) -> None:
        """TC-SSE-04: Broadcast keepalive sends :ping comment frame."""
        broadcaster = SSEBroadcaster()
        conn = broadcaster.register_client()

        broadcaster.broadcast_keepalive()
        self.assertFalse(conn.queue.empty())
        event, data = conn.queue.get_nowait()
        self.assertEqual(event, "")
        self.assertEqual(data, ":ping")

    def test_cleanup_stale_clients(self) -> None:
        """TC-SSE-05: Evicts subscribers that have exceeded idle timeout."""
        broadcaster = SSEBroadcaster()
        conn1 = broadcaster.register_client()
        conn2 = broadcaster.register_client()

        # Simulate conn1 being inactive 100 seconds ago
        conn1.last_event_at = time.time() - 100.0

        evicted = broadcaster.cleanup_stale_clients(max_idle_sec=30.0)
        self.assertEqual(evicted, 1)
        self.assertEqual(broadcaster.client_count, 1)
        self.assertIn(conn2.client_id, broadcaster._clients)
        self.assertNotIn(conn1.client_id, broadcaster._clients)

    def test_concurrent_broadcast_thread_safety(self) -> None:
        """TC-SSE-06: Validates thread safety under concurrent registrations and broadcasts."""
        broadcaster = SSEBroadcaster(max_queue_depth=50)
        stop_event = threading.Event()
        clients = []

        def worker_subscriber() -> None:
            conn = broadcaster.register_client()
            clients.append(conn)
            while not stop_event.is_set():
                try:
                    conn.queue.get(timeout=0.01)
                except queue.Empty:
                    pass

        def worker_broadcaster() -> None:
            for i in range(100):
                broadcaster.broadcast("ping", f'{{"seq":{i}}}')
                time.sleep(0.001)

        sub_threads = [threading.Thread(target=worker_subscriber) for _ in range(5)]
        pub_threads = [threading.Thread(target=worker_broadcaster) for _ in range(2)]

        for t in sub_threads + pub_threads:
            t.start()

        for t in pub_threads:
            t.join()

        stop_event.set()
        for t in sub_threads:
            t.join()

        self.assertEqual(broadcaster.client_count, 5)
        metrics = broadcaster.get_health_metrics()
        self.assertEqual(metrics.total_broadcasts, 200)


if __name__ == "__main__":
    unittest.main()
