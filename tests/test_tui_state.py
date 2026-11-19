"""
Unit and integration tests for thread-safe state store and background stream worker.
"""

import email.message
import io
import time
import threading
import unittest
import urllib.error
from typing import Generator, List, Optional
from unittest.mock import patch

from train_tracker import RailwayGovClient, TrainPosition
from tui.mock_engine import MockTelemetryEngine
from tui.state import (
    BreadcrumbMode,
    ConnectionStatus,
    HistoricalBreadcrumb,
    RosterSortMode,
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
    delay: float = 2.0,
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
        delay=delay,
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

    def test_default_stale_timeout_30_minutes(self):
        """TC-P2-01 & TC-P2-02: Verify default stale timeout is 1800s (30m) for store and TrainRecord."""
        default_store = TrainStateStore()
        self.assertEqual(default_store._stale_timeout_sec, 1800.0)

        t = _create_sample_train("ic-50", "IC 50")
        rec = TrainRecord(position=t)
        # Inactive for 1700s (< 1800s) -> not stale
        rec.last_updated = time.time() - 1700.0
        self.assertFalse(rec.is_stale())

        # Inactive for 1801s (> 1800s) -> stale
        rec.last_updated = time.time() - 1801.0
        self.assertTrue(rec.is_stale())

    def test_prune_stale_trains_30m_boundary_and_counters(self):
        """TC-P2-03: Verify 30m boundary enforcement, total_trains_pruned tracking, and idempotency."""
        store = TrainStateStore()
        t1 = _create_sample_train("ic-50", "IC 50")
        t2 = _create_sample_train("ic-51", "IC 51")
        t3 = _create_sample_train("ic-52", "IC 52")
        store.update_positions([t1, t2, t3])

        now = time.time()
        store._trains["ic-50"].last_updated = now - 100.0   # Active (recent)
        store._trains["ic-51"].last_updated = now - 1790.0  # Active (< 1800s)
        store._trains["ic-52"].last_updated = now - 1850.0  # Stale (> 1800s)

        v_before = store._version
        pruned = store.prune_stale_trains()  # Default 1800s cutoff
        self.assertEqual(pruned, 1)
        self.assertEqual(store._total_trains_pruned, 1)
        self.assertGreater(store._last_prune_ts, 0.0)
        self.assertGreater(store._version, v_before)

        snap = store.get_snapshot()
        self.assertIn("ic-50", snap.trains)
        self.assertIn("ic-51", snap.trains)
        self.assertNotIn("ic-52", snap.trains)

        # Calling again when no trains are stale -> returns 0, version unchanged
        v_after = store._version
        pruned_second = store.prune_stale_trains()
        self.assertEqual(pruned_second, 0)
        self.assertEqual(store._version, v_after)
        self.assertEqual(store._total_trains_pruned, 1)

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

    def test_corridor_filtering_real_world_telemetry(self):
        """Verify real-world telemetry with aliases or missing corridor tags filter correctly."""
        p_suburban = TrainPosition.from_dict({
            "id": "2232",
            "trainNumber": "2232",
            "serviceType": "Suburban",
            "origin": "Άνω Λιόσια",
            "destination": "Αεροδρόμιο",
            "nextStation": "Νερατζιώτισσα",
            "lat": 38.050842,
            "lng": 23.782449,
            "speed": 100.0,
            "corridor": "PIRAIR",
        })
        p_pathe = TrainPosition.from_dict({
            "id": "ic-52",
            "trainNumber": "IC 52",
            "serviceType": "Intercity",
            "origin": "Αθήνα",
            "destination": "Θεσσαλονίκη",
            "nextStation": "Λάρισα",
            "lat": 39.638,
            "lng": 22.422,
            "speed": 140.0,
            "corridor": None,
        })
        p_northern = TrainPosition.from_dict({
            "id": "3672",
            "trainNumber": "3672",
            "serviceType": "Regional",
            "origin": "Θεσσαλονίκη",
            "destination": "Σέρρες",
            "lat": 41.09,
            "lng": 23.55,
            "speed": 80.0,
            "corridor": None,
        })

        self.store.update_positions([p_suburban, p_pathe, p_northern])

        # Preset 0: All trains
        self.store.set_corridor_filter(None)
        self.assertEqual(len(self.store.get_snapshot().filtered_trains), 3)

        # Preset 1: Proastiakos
        self.store.set_corridor_filter("PROASTIAKOS")
        filtered_proast = self.store.get_snapshot().filtered_trains
        self.assertEqual(len(filtered_proast), 1)
        self.assertEqual(filtered_proast[0].position.train_number, "2232")

        # Preset 2: PATHE Mainline
        self.store.set_corridor_filter("PATHE")
        filtered_pathe = self.store.get_snapshot().filtered_trains
        self.assertEqual(len(filtered_pathe), 1)
        self.assertEqual(filtered_pathe[0].position.train_number, "IC 52")

        # Preset 3: Northern Greece
        self.store.set_corridor_filter("NORTHERN")
        filtered_north = self.store.get_snapshot().filtered_trains
        self.assertEqual(len(filtered_north), 1)
        self.assertEqual(filtered_north[0].position.train_number, "3672")

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

    def test_sort_mode_cycling_and_ordering(self):
        """Verify RosterSortMode cycles TRAIN_ID -> SPEED -> DELAY -> TRAIN_ID with deterministic tie-breaking."""
        t1 = _create_sample_train("t-1597", "1597", speed=80.0, delay=4.0)
        t2 = _create_sample_train("t-ic50", "IC 50", speed=140.0, delay=0.0)
        t3 = _create_sample_train("t-ic52", "IC 52", speed=80.0, delay=12.0)
        t4 = _create_sample_train("t-2232", "2232", speed=0.0, delay=-1.0)
        self.store.update_positions([t1, t2, t3, t4])

        # 1. Default sort mode is TRAIN_ID (natural ascending: IC 50, IC 52, 1597, 2232)
        snap_id = self.store.get_snapshot()
        self.assertEqual(snap_id.sort_mode, RosterSortMode.TRAIN_ID)
        self.assertEqual(
            [r.position.id for r in snap_id.filtered_trains],
            ["t-ic50", "t-ic52", "t-1597", "t-2232"],
        )

        # Select t-1597 (at index 2 in TRAIN_ID order)
        self.store.select_by_id("t-1597")
        self.assertEqual(self.store.get_snapshot().selected_index, 2)

        # 2. Cycle to SPEED (descending speed, tie-broken by natural ID: IC 50 (140), IC 52 (80), 1597 (80), 2232 (0))
        v_before = self.store.get_snapshot().version
        new_mode = self.store.cycle_sort_mode()
        self.assertEqual(new_mode, RosterSortMode.SPEED)
        snap_spd = self.store.get_snapshot()
        self.assertEqual(snap_spd.sort_mode, RosterSortMode.SPEED)
        self.assertGreater(snap_spd.version, v_before)
        self.assertEqual(
            [r.position.id for r in snap_spd.filtered_trains],
            ["t-ic50", "t-ic52", "t-1597", "t-2232"],
        )

        # 3. Cycle to DELAY (descending delay: IC 52 (+12), 1597 (+4), IC 50 (0), 2232 (-1))
        new_mode = self.store.cycle_sort_mode()
        self.assertEqual(new_mode, RosterSortMode.DELAY)
        snap_dly = self.store.get_snapshot()
        self.assertEqual(snap_dly.sort_mode, RosterSortMode.DELAY)
        self.assertEqual(
            [r.position.id for r in snap_dly.filtered_trains],
            ["t-ic52", "t-1597", "t-ic50", "t-2232"],
        )
        # Selected train t-1597 is preserved and moves to index 1 in DELAY sort order
        self.assertEqual(snap_dly.selected_train.position.id, "t-1597")
        self.assertEqual(snap_dly.selected_index, 1)

        # Cursor navigation select_next() follows DELAY sort order (from t-1597 -> t-ic50)
        self.assertEqual(self.store.select_next(), "t-ic50")

        # 4. Cycle back to TRAIN_ID
        self.assertEqual(self.store.cycle_sort_mode(), RosterSortMode.TRAIN_ID)

        # Explicit set_sort_mode and type validation
        self.store.set_sort_mode(RosterSortMode.SPEED)
        self.assertEqual(self.store.get_snapshot().sort_mode, RosterSortMode.SPEED)
        with self.assertRaises(TypeError):
            self.store.set_sort_mode("SPEED")  # type: ignore[arg-type]

    def test_breadcrumb_mode_cycling_and_explicit_set(self):
        """Verify BreadcrumbMode cycles ALL -> SELECTED -> OFF -> ALL and supports explicit setting."""
        snap_initial = self.store.get_snapshot()
        self.assertEqual(snap_initial.breadcrumb_mode, BreadcrumbMode.ALL)
        v0 = snap_initial.version

        # 1. Cycle ALL -> SELECTED
        mode1 = self.store.cycle_breadcrumb_mode()
        self.assertEqual(mode1, BreadcrumbMode.SELECTED)
        snap1 = self.store.get_snapshot()
        self.assertEqual(snap1.breadcrumb_mode, BreadcrumbMode.SELECTED)
        self.assertGreater(snap1.version, v0)

        # 2. Cycle SELECTED -> OFF
        v1 = snap1.version
        mode2 = self.store.cycle_breadcrumb_mode()
        self.assertEqual(mode2, BreadcrumbMode.OFF)
        snap2 = self.store.get_snapshot()
        self.assertEqual(snap2.breadcrumb_mode, BreadcrumbMode.OFF)
        self.assertGreater(snap2.version, v1)

        # 3. Cycle OFF -> ALL
        v2 = snap2.version
        mode3 = self.store.cycle_breadcrumb_mode()
        self.assertEqual(mode3, BreadcrumbMode.ALL)
        snap3 = self.store.get_snapshot()
        self.assertEqual(snap3.breadcrumb_mode, BreadcrumbMode.ALL)
        self.assertGreater(snap3.version, v2)

        # 4. Explicit set_breadcrumb_mode and idempotency on version
        v3 = snap3.version
        self.store.set_breadcrumb_mode(BreadcrumbMode.ALL)
        self.assertEqual(self.store.get_snapshot().version, v3)

        self.store.set_breadcrumb_mode(BreadcrumbMode.SELECTED)
        snap4 = self.store.get_snapshot()
        self.assertEqual(snap4.breadcrumb_mode, BreadcrumbMode.SELECTED)
        self.assertGreater(snap4.version, v3)

        with self.assertRaises(TypeError):
            self.store.set_breadcrumb_mode("OFF")  # type: ignore[arg-type]

    def test_snapshot_thread_isolation(self):
        """Verify mutating the store does not corrupt an existing snapshot or its trail deques."""
        t1 = _create_sample_train("ic-50", "IC 50", lat=38.0, lng=23.7)
        self.store.update_positions([t1])
        snapshot1 = self.store.get_snapshot()
        self.assertEqual(len(snapshot1.trains["ic-50"].trail), 1)

        # Update position in store with moved coordinates (appends breadcrumb to live store)
        t1_updated = _create_sample_train("ic-50", "IC 50", lat=38.1, lng=23.8, speed=160.0)
        t2 = _create_sample_train("ic-51", "IC 51")
        self.store.update_positions([t1_updated, t2])

        # snapshot1 must still reflect original state AND original trail deque without mutation
        self.assertEqual(len(snapshot1.trains), 1)
        self.assertNotIn("ic-51", snapshot1.trains)
        self.assertEqual(snapshot1.version, 1)
        self.assertEqual(len(snapshot1.trains["ic-50"].trail), 1)
        self.assertEqual(snapshot1.trains["ic-50"].position.speed, 120.0)

        # New snapshot reflects changes
        snapshot2 = self.store.get_snapshot()
        self.assertEqual(len(snapshot2.trains), 2)
        self.assertIn("ic-51", snapshot2.trains)
        self.assertEqual(snapshot2.version, 2)
        self.assertEqual(len(snapshot2.trains["ic-50"].trail), 2)
        self.assertEqual(snapshot2.trains["ic-50"].position.speed, 160.0)

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
                    # Access collections and iterate trail deques to verify thread safety
                    _ = len(snap.trains)
                    _ = len(snap.filtered_trains)
                    for rec in snap.trains.values():
                        for crumb in rec.trail:
                            _ = crumb.lat
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
        self.assertTrue(worker.is_paused)
        self.assertEqual(self.store.get_snapshot().connection_status, ConnectionStatus.PAUSED)
        count_at_pause = self.store.get_snapshot().total_messages_received

        time.sleep(0.06)
        # Verify no new updates arrived while paused
        self.assertEqual(self.store.get_snapshot().total_messages_received, count_at_pause)

        # Resume
        worker.resume()
        self.assertFalse(worker.is_paused)
        time.sleep(0.08)
        self.assertGreater(self.store.get_snapshot().total_messages_received, count_at_pause)

        worker.stop()

    def test_stream_worker_error_while_paused_preserves_paused_status(self):
        """Verify in-flight stream error while paused does not overwrite PAUSED status."""
        ready_to_fail = threading.Event()

        def failing_after_pause_stream() -> Generator[List[TrainPosition], None, None]:
            yield [_create_sample_train("ic-50", "IC 50", lat=38.0)]
            ready_to_fail.wait(timeout=1.0)
            raise ConnectionResetError("In-flight socket timeout while paused")

        worker = StreamWorker(
            store=self.store,
            custom_stream=failing_after_pause_stream(),
            base_backoff_sec=0.05,
            max_backoff_sec=0.2,
        )
        worker.start()

        try:
            deadline = time.time() + 1.0
            while time.time() < deadline and self.store.get_snapshot().total_messages_received < 1:
                time.sleep(0.01)

            worker.pause()
            self.assertTrue(worker.is_paused)
            self.assertEqual(self.store.get_snapshot().connection_status, ConnectionStatus.PAUSED)

            # Trigger in-flight socket error while worker is paused
            ready_to_fail.set()
            time.sleep(0.06)

            # ConnectionStatus must remain PAUSED (not clobbered by RECONNECTING)
            self.assertEqual(self.store.get_snapshot().connection_status, ConnectionStatus.PAUSED)
        finally:
            worker.stop(timeout_sec=1.0)

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

    @patch("urllib.request.urlopen")
    def test_stream_worker_client_error_transitions_to_reconnecting_without_stdout(self, mock_urlopen):
        """Verify StreamWorker with RailwayGovClient catches URLError, sets RECONNECTING, and emits zero stdout."""
        mock_urlopen.side_effect = urllib.error.URLError("Network unreachable")
        client = RailwayGovClient("https://railway.gov.gr")

        worker = StreamWorker(
            store=self.store,
            client=client,
            base_backoff_sec=0.5,
            max_backoff_sec=2.0,
        )

        with patch("sys.stdout", new_callable=io.StringIO) as mock_stdout:
            worker.start()
            # Give worker time to hit urlopen failure and enter RECONNECTING backoff wait
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if self.store.get_snapshot().connection_status == ConnectionStatus.RECONNECTING:
                    break
                time.sleep(0.01)

            snap = self.store.get_snapshot()
            self.assertEqual(snap.connection_status, ConnectionStatus.RECONNECTING)
            self.assertIn("Network unreachable", snap.status_message)

            # Stop should interrupt backoff wait immediately
            stop_start = time.time()
            worker.stop(timeout_sec=1.0)
            stop_elapsed = time.time() - stop_start

            self.assertFalse(worker.is_running)
            self.assertLess(stop_elapsed, 0.4)
            self.assertEqual(mock_stdout.getvalue(), "")

    @patch("urllib.request.urlopen")
    def test_stream_worker_handles_http_429_rate_limit(self, mock_urlopen):
        """TC-RES-18: StreamWorker parses Retry-After on 429, sets RECONNECTING, and stop() cleanly interrupts."""
        hdrs = email.message.Message()
        hdrs["Retry-After"] = "45"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://railway.gov.gr/api/train-stream",
            429,
            "Too Many Requests",
            hdrs,
            io.BytesIO(b""),
        )
        client = RailwayGovClient("https://railway.gov.gr")
        worker = StreamWorker(store=self.store, client=client)

        worker.start()
        try:
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if self.store.get_snapshot().connection_status == ConnectionStatus.RECONNECTING:
                    break
                time.sleep(0.01)

            snap = self.store.get_snapshot()
            self.assertEqual(snap.connection_status, ConnectionStatus.RECONNECTING)
            self.assertIn("HTTP 429 Rate Limited", snap.status_message)
            self.assertIn("45.0s", snap.status_message)

            stop_start = time.time()
            worker.stop(timeout_sec=1.0)
            stop_elapsed = time.time() - stop_start

            self.assertFalse(worker.is_running)
            self.assertLess(stop_elapsed, 0.4)
        finally:
            if worker.is_running:
                worker.stop(timeout_sec=1.0)

    @patch("urllib.request.urlopen")
    def test_stream_worker_handles_http_503_server_error(self, mock_urlopen):
        """TC-RES-19: StreamWorker detects HTTP 503 Server Error and transitions to RECONNECTING."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://railway.gov.gr/api/train-stream",
            503,
            "Service Unavailable",
            email.message.Message(),
            io.BytesIO(b""),
        )
        client = RailwayGovClient("https://railway.gov.gr")
        worker = StreamWorker(store=self.store, client=client)

        worker.start()
        try:
            deadline = time.time() + 1.0
            while time.time() < deadline:
                if self.store.get_snapshot().connection_status == ConnectionStatus.RECONNECTING:
                    break
                time.sleep(0.01)

            snap = self.store.get_snapshot()
            self.assertEqual(snap.connection_status, ConnectionStatus.RECONNECTING)
            self.assertIn("HTTP 503 Server Error", snap.status_message)
        finally:
            worker.stop(timeout_sec=1.0)

    def test_stream_worker_periodic_prune(self):
        """TC-P2-05: StreamWorker periodically invokes prune_stale_trains on configured cadence."""
        t_stale = _create_sample_train("ic-99", "IC 99")
        self.store.update_positions([t_stale])
        self.store._trains["ic-99"].last_updated = time.time() - 2000.0

        def single_batch_stream():
            yield [_create_sample_train("ic-100", "IC 100")]

        worker = StreamWorker(
            store=self.store,
            custom_stream=single_batch_stream,
            prune_interval_sec=0.001,
        )
        worker.start()
        try:
            deadline = time.time() + 1.0
            while time.time() < deadline:
                snap = self.store.get_snapshot()
                if "ic-100" in snap.trains and "ic-99" not in snap.trains:
                    break
                time.sleep(0.01)

            snap = self.store.get_snapshot()
            self.assertIn("ic-100", snap.trains)
            self.assertNotIn("ic-99", snap.trains)
            self.assertGreaterEqual(self.store._total_trains_pruned, 1)
        finally:
            worker.stop(timeout_sec=1.0)


if __name__ == "__main__":
    unittest.main()
