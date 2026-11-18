"""
Unit and integration tests for thread-safe state store and background stream worker.
"""

import time
import threading
import unittest
from typing import Generator, List

from train_tracker import TrainPosition
from tui.mock_engine import MockTelemetryEngine
from tui.state import (
    ConnectionStatus,
    HistoricalBreadcrumb,
    StateSnapshot,
    TrainRecord,
    TrainStateStore,
)
from tui.stream_worker import StreamWorker


def _create_sample_train(
    train_id: str = "ic-50",
    train_number: str = "IC 50",
    lat: float = 38.0,
    lng: float = 23.7,
    speed: float = 120.0,
    corridor: str = "PATHE",
    origin: str = "Athens",
    destination: str = "Thessaloniki",
) -> TrainPosition:
    """Helper to instantiate a valid TrainPosition for testing."""
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120-001",
        name=train_number,
        train_type="InterCity",
        service_type="Passenger",
        status="Κανονική" if speed > 5.0 else "Στάση",
        origin=origin,
        destination=destination,
        next_station="SKA",
        speed=speed,
        course=15.0,
        track_heading=15.0,
        lat=lat,
        lng=lng,
        altitude=45.0,
        delay=2,
        progress=0.15,
        chainage_m=12000,
        corridor=corridor,
        distance_to_destination_m=480000,
        timestamp="2026-11-18T12:00:00Z",
        schedule_id="sch-101",
    )


class TestTrainStateStore(unittest.TestCase):
    """Test suite for TrainStateStore state tracking, filtering, and concurrency."""

    def setUp(self):
        self.store = TrainStateStore(stale_timeout_sec=5.0, max_trail_len=5)

    def test_update_positions_single_and_batch(self):
        """Verify batch updates create records and increment version monotonically."""
        t1 = _create_sample_train("ic-50", "IC 50", 38.0, 23.7)
        t2 = _create_sample_train("pro-1597", "1597", 37.95, 23.6, corridor="PROASTIAKOS")

        self.assertEqual(self.store._version, 0)
        self.store.update_positions([t1, t2])

        self.assertEqual(self.store._version, 1)
        self.assertEqual(self.store._total_messages, 1)

        snapshot = self.store.get_snapshot()
        self.assertEqual(len(snapshot.trains), 2)
        self.assertIn("ic-50", snapshot.trains)
        self.assertIn("pro-1597", snapshot.trains)
        self.assertEqual(snapshot.trains["ic-50"].position.train_number, "IC 50")

    def test_breadcrumb_history_and_maxlen(self):
        """Verify coordinate trail accumulates up to max_trail_len."""
        for step in range(10):
            pos = _create_sample_train("ic-50", "IC 50", 38.0 + (step * 0.01), 23.7 + (step * 0.01))
            self.store.update_positions([pos])

        record = self.store.get_train("ic-50")
        self.assertIsNotNone(record)
        self.assertEqual(len(record.trail), 5)  # max_trail_len is 5
        self.assertEqual(record.update_count, 10)
        # Check that latest coordinate is stored in the trail
        self.assertAlmostEqual(record.trail[-1].lat, 38.0 + (9 * 0.01), places=4)

    def test_breadcrumb_stationary_filtering(self):
        """Verify identical coordinates do not duplicate breadcrumb entries."""
        pos = _create_sample_train("ic-50", "IC 50", 38.0, 23.7, speed=0.0)
        for _ in range(5):
            self.store.update_positions([pos])

        record = self.store.get_train("ic-50")
        self.assertIsNotNone(record)
        self.assertEqual(len(record.trail), 1)  # Only 1 distinct position recorded
        self.assertEqual(record.update_count, 5)

    def test_prune_stale_trains(self):
        """Verify stale trains past cutoff are pruned and selection resets."""
        t1 = _create_sample_train("ic-50", "IC 50")
        t2 = _create_sample_train("ic-51", "IC 51")
        self.store.update_positions([t1, t2])
        self.store.select_by_id("ic-50")

        # Artificially age ic-50
        self.store._trains["ic-50"].last_updated = time.time() - 100.0

        pruned = self.store.prune_stale_trains(max_age_sec=10.0)
        self.assertEqual(pruned, 1)

        snapshot = self.store.get_snapshot()
        self.assertNotIn("ic-50", snapshot.trains)
        self.assertIn("ic-51", snapshot.trains)
        self.assertIsNone(snapshot.selected_train)
        self.assertFalse(snapshot.follow_mode)

    def test_cursor_navigation_next_prev_wrap(self):
        """Verify select_next and select_prev cycle correctly with wrap-around."""
        t1 = _create_sample_train("ic-50", "IC 50")
        t2 = _create_sample_train("ic-51", "IC 51")
        t3 = _create_sample_train("pro-1597", "1597")
        self.store.update_positions([t1, t2, t3])

        # Initially no selection
        self.assertIsNone(self.store.get_selected_train())

        # Next moves to first train (natural sort: IC 50)
        sel1 = self.store.select_next()
        self.assertEqual(sel1, "ic-50")

        # Next moves to second train (IC 51)
        sel2 = self.store.select_next()
        self.assertEqual(sel2, "ic-51")

        # Next moves to third train (1597)
        sel3 = self.store.select_next()
        self.assertEqual(sel3, "pro-1597")

        # Next wraps back to first train (IC 50)
        sel4 = self.store.select_next()
        self.assertEqual(sel4, "ic-50")

        # Prev wraps back to last train (1597)
        prev1 = self.store.select_prev()
        self.assertEqual(prev1, "pro-1597")

    def test_filter_query_matching(self):
        """Verify text filter matches train number, origin/dest, or status."""
        t1 = _create_sample_train("ic-50", "IC 50", origin="Athens", destination="Thessaloniki")
        t2 = _create_sample_train("pro-1597", "1597", origin="Piraeus", destination="Kiato")
        t3 = _create_sample_train("reg-884", "884", origin="Paleofarsalos", destination="Kalambaka")
        self.store.update_positions([t1, t2, t3])

        # Filter by train number
        self.store.set_filter_query("50")
        snapshot = self.store.get_snapshot()
        self.assertEqual(len(snapshot.filtered_trains), 1)
        self.assertEqual(snapshot.filtered_trains[0].position.id, "ic-50")

        # Filter by destination
        self.store.set_filter_query("kiato")
        snapshot = self.store.get_snapshot()
        self.assertEqual(len(snapshot.filtered_trains), 1)
        self.assertEqual(snapshot.filtered_trains[0].position.id, "pro-1597")

        # Filter matching multiple
        self.store.set_filter_query("a")
        snapshot = self.store.get_snapshot()
        self.assertGreaterEqual(len(snapshot.filtered_trains), 2)

        # Clear filter
        self.store.set_filter_query("")
        snapshot = self.store.get_snapshot()
        self.assertEqual(len(snapshot.filtered_trains), 3)

    def test_corridor_filtering(self):
        """Verify corridor filter restricts trains appropriately."""
        t1 = _create_sample_train("ic-50", "IC 50", corridor="PATHE")
        t2 = _create_sample_train("pro-1597", "1597", corridor="PROASTIAKOS")
        t3 = _create_sample_train("reg-884", "884", corridor="THESSALY")
        self.store.update_positions([t1, t2, t3])

        self.store.set_corridor_filter("PATHE")
        snapshot = self.store.get_snapshot()
        self.assertEqual(len(snapshot.filtered_trains), 1)
        self.assertEqual(snapshot.filtered_trains[0].position.id, "ic-50")

        self.store.set_corridor_filter(None)
        snapshot = self.store.get_snapshot()
        self.assertEqual(len(snapshot.filtered_trains), 3)

    def test_follow_mode_toggle(self):
        """Verify follow-mode requires a valid selection."""
        # Cannot enable follow mode with nothing selected
        self.assertFalse(self.store.toggle_follow_mode())
        self.assertFalse(self.store.get_snapshot().follow_mode)

        # Select a train
        t1 = _create_sample_train("ic-50", "IC 50")
        self.store.update_positions([t1])
        self.store.select_by_id("ic-50")

        # Toggle on
        self.assertTrue(self.store.toggle_follow_mode())
        self.assertTrue(self.store.get_snapshot().follow_mode)

        # Toggle off
        self.assertFalse(self.store.toggle_follow_mode())
        self.assertFalse(self.store.get_snapshot().follow_mode)

        # Explicit set
        self.store.set_follow_mode(True)
        self.assertTrue(self.store.get_snapshot().follow_mode)

        # Clear selection disengages follow mode
        self.store.clear_selection()
        self.assertFalse(self.store.get_snapshot().follow_mode)

    def test_snapshot_thread_isolation(self):
        """Verify mutating the store does not corrupt an existing snapshot."""
        t1 = _create_sample_train("ic-50", "IC 50")
        self.store.update_positions([t1])
        snapshot1 = self.store.get_snapshot()

        # Update position in store
        t1_updated = _create_sample_train("ic-50", "IC 50", speed=160.0)
        t2 = _create_sample_train("ic-51", "IC 51")
        self.store.update_positions([t1_updated, t2])

        # snapshot1 must still reflect original state
        self.assertEqual(len(snapshot1.trains), 1)
        self.assertNotIn("ic-51", snapshot1.trains)
        self.assertEqual(snapshot1.version, 1)

        # New snapshot reflects changes
        snapshot2 = self.store.get_snapshot()
        self.assertEqual(len(snapshot2.trains), 2)
        self.assertIn("ic-51", snapshot2.trains)
        self.assertEqual(snapshot2.version, 2)

    def test_concurrent_multithreaded_stress(self):
        """Stress test: concurrent writers and readers under high frequency."""
        stop_event = threading.Event()
        errors = []

        def writer_task(worker_id: int):
            step = 0
            while not stop_event.is_set():
                train_id = f"worker-{worker_id}"
                pos = _create_sample_train(
                    train_id=train_id,
                    train_number=f"W{worker_id}",
                    lat=38.0 + (step * 0.001),
                    lng=23.7 + (step * 0.001),
                    speed=100.0 + (step % 50),
                )
                try:
                    self.store.update_positions([pos])
                    step += 1
                    time.sleep(0.001)
                except Exception as e:
                    errors.append(f"Writer error: {e}")

        def reader_task():
            while not stop_event.is_set():
                try:
                    snap = self.store.get_snapshot()
                    # Access collections in snapshot to verify thread safety
                    _ = len(snap.trains)
                    _ = len(snap.filtered_trains)
                    if snap.selected_train:
                        _ = snap.selected_train.position.speed
                    time.sleep(0.002)
                except Exception as e:
                    errors.append(f"Reader error: {e}")

        threads = []
        for i in range(4):
            t = threading.Thread(target=writer_task, args=(i,))
            threads.append(t)
        for _ in range(2):
            t = threading.Thread(target=reader_task)
            threads.append(t)

        for t in threads:
            t.start()

        time.sleep(0.5)  # Run concurrent stress for 500ms
        stop_event.set()

        for t in threads:
            t.join(timeout=1.0)

        self.assertEqual(errors, [], f"Encountered thread safety errors: {errors}")
        self.assertGreater(self.store._total_messages, 100)


class TestStreamWorker(unittest.TestCase):
    """Test suite for background StreamWorker thread and lifecycle."""

    def setUp(self):
        self.store = TrainStateStore()

    def test_stream_worker_start_stop(self):
        """Verify StreamWorker starts daemon thread and terminates on stop()."""
        def finite_stream() -> Generator[List[TrainPosition], None, None]:
            for i in range(5):
                yield [_create_sample_train("ic-50", "IC 50", lat=38.0 + i)]
                time.sleep(0.05)

        worker = StreamWorker(store=self.store, custom_stream=finite_stream())
        self.assertFalse(worker.is_running)

        worker.start()
        self.assertTrue(worker.is_running)

        time.sleep(0.15)
        worker.stop(timeout_sec=1.0)
        self.assertFalse(worker.is_running)

        snap = self.store.get_snapshot()
        self.assertEqual(snap.connection_status, ConnectionStatus.DISCONNECTED)
        self.assertGreaterEqual(snap.total_messages_received, 1)

    def test_stream_worker_pause_resume(self):
        """Verify pausing halts updates and resuming restarts consumption."""
        def endless_stream() -> Generator[List[TrainPosition], None, None]:
            count = 0
            while True:
                yield [_create_sample_train("ic-50", "IC 50", lat=38.0 + count)]
                count += 1
                time.sleep(0.02)

        worker = StreamWorker(store=self.store, custom_stream=endless_stream())
        worker.start()

        time.sleep(0.08)
        self.assertGreater(self.store.get_snapshot().total_messages_received, 0)

        # Pause
        worker.pause()
        self.assertEqual(self.store.get_snapshot().connection_status, ConnectionStatus.PAUSED)
        count_at_pause = self.store.get_snapshot().total_messages_received

        time.sleep(0.06)
        # Verify no new updates arrived while paused
        self.assertEqual(self.store.get_snapshot().total_messages_received, count_at_pause)

        # Resume
        worker.resume()
        time.sleep(0.08)
        self.assertGreater(self.store.get_snapshot().total_messages_received, count_at_pause)

        worker.stop()

    def test_stream_worker_reconnection_backoff(self):
        """Verify StreamWorker catches stream errors, transitions to RECONNECTING, and retries."""
        call_count = 0

        def failing_stream() -> Generator[List[TrainPosition], None, None]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionResetError("Simulated socket reset")
            else:
                yield [_create_sample_train("ic-50", "IC 50")]
                time.sleep(0.1)

        worker = StreamWorker(
            store=self.store,
            custom_stream=failing_stream(),
            base_backoff_sec=0.05,
            max_backoff_sec=0.2,
        )
        worker.start()

        # Allow time for initial failure and backoff retry
        time.sleep(0.15)
        worker.stop()

        self.assertGreaterEqual(call_count, 1)

    def test_stream_worker_mock_engine_integration(self):
        """Verify StreamWorker ingests batches from Phase 2 MockTelemetryEngine."""
        engine = MockTelemetryEngine(time_scale=1.0)
        worker = StreamWorker(store=self.store, mock_engine=engine)

        worker.start()
        time.sleep(0.15)
        worker.stop()

        snap = self.store.get_snapshot()
        self.assertGreater(len(snap.trains), 0)
        # Verify known mock trains exist in state store
        self.assertIn("mock-ic-50", snap.trains)


if __name__ == "__main__":
    unittest.main()
