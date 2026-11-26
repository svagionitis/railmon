"""Comprehensive unit and integration tests for F-10.1 Multiplexed SSE Ingestion.

Covers trainHistory instant trail hydration, scheduleCards ghost run isolation &
live supersession, consist-aware delayUpdates, StreamWorker dispatch, offline
MockTelemetryEngine parity, TUI controls/rendering (UTF-8 and ASCII), GTFS-RT v2.0
VehiclePositions vs. TripUpdates partitioning, and Prometheus metrics.
"""

import io
import json
import threading
import time
import unittest
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import MagicMock, patch

from exporter.collector import StreamMetricsCollector, TrainTelemetryCollector
from gtfs_rt.mapper import GtfsRtMapper
from gtfs_rt.models import FeedKind, FeedMessage, GtfsRtConfig
from train_tracker import (
    DelayUpdate,
    GhostTrain,
    MAX_SSE_FRAME_BYTES,
    RailwayGovClient,
    SSEEventType,
    SSEFrame,
    TrainHistoryPoint,
    TrainPosition,
)
from tui.app import TUIApp
from tui.geo import GREECE_PRESET, GeoProjector, Viewport
from tui.mock_engine import MockTelemetryEngine, load_stations, load_tracks
from tui.renderer import (
    PaneGeometry,
    TUIRenderer,
    compute_station_board_entries,
)
from tui.state import (
    GhostVisibilityMode,
    InputMode,
    InspectorMode,
    MAX_PENDING_HISTORY_TRAINS,
    PENDING_HISTORY_TTL_SEC,
    TrainStateStore,
)
from tui.stream_worker import StreamWorker


class _MockCursesWin:
    """Minimal headless mock curses window capturing drawn text for verification."""

    def __init__(self, height: int = 24, width: int = 80) -> None:
        self.height = height
        self.width = width
        self.strings: List[Tuple[int, int, str, int]] = []
        self.erased = False
        self.boxed = False

    def getmaxyx(self) -> Tuple[int, int]:
        return self.height, self.width

    def erase(self) -> None:
        self.erased = True
        self.strings.clear()

    def clear(self) -> None:
        self.erase()

    def box(self) -> None:
        self.boxed = True

    def border(self, *args: Any) -> None:
        self.boxed = True

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        self.strings.append((y, x, str(text), attr))

    def noutrefresh(self) -> None:
        pass

    def refresh(self) -> None:
        pass

    def keypad(self, flag: bool) -> None:
        pass

    def nodelay(self, flag: bool) -> None:
        pass

    def timeout(self, ms: int) -> None:
        pass

    def getch(self) -> int:
        return -1

    def subwin(self, h: int, w: int, y: int, x: int) -> "_MockCursesWin":
        return _MockCursesWin(h, w)

    def all_text(self) -> str:
        return "\n".join(s[2] for s in self.strings)


def _make_live_pos(
    tid: str = "live-1",
    train_number: str = "IC56",
    lat: float = 39.6385,
    lng: float = 22.4192,
    speed: float = 120.0,
    delay: int = 2,
    corridor: str = "E85",
    schedule_id: Optional[str] = "6a94745bd16c475ca144e488",
    loco: Optional[str] = "220005",
) -> TrainPosition:
    """Construct a valid live GPS TrainPosition for testing."""
    return TrainPosition(
        id=tid,
        train_number=train_number,
        locomotive_number=loco,
        name=f"Train {train_number}",
        train_type="intercity",
        service_type="InterCity",
        status="Κανονική" if speed > 5.0 else "Στάση",
        origin="Αθήνα",
        destination="Θεσσαλονίκη",
        next_station="Λάρισα",
        speed=speed,
        course=350.0,
        track_heading=350.0,
        lat=lat,
        lng=lng,
        altitude=85.0,
        delay=delay,
        progress=0.55,
        chainage_m=340000,
        corridor=corridor,
        distance_to_destination_m=150000,
        timestamp="2026-11-23T20:40:33.000Z",
        schedule_id=schedule_id,
    )


def _make_ghost(
    gid: str = "schedule:6a94743bd16c475ca1449e4a",
    train_number: str = "1237",
    schedule_id: str = "6a94743bd16c475ca1449e4a",
    corridor: str = "PIRAIR",
    origin: str = "Αεροδρόμιο",
    destination: str = "Πειραιάς",
    delay: int = 3,
    progress: float = 0.59,
) -> GhostTrain:
    """Construct a valid scheduleCards GhostTrain for testing."""
    return GhostTrain(
        id=gid,
        train_number=train_number,
        schedule_id=schedule_id,
        schedule_number=f"{train_number}-20261123-2232",
        name=train_number,
        service_type="Suburban",
        category="Προαστιακός",
        origin=origin,
        destination=destination,
        corridor=corridor,
        scheduled_departure="2026-11-23T20:32:00.000Z",
        scheduled_arrival="2026-11-23T21:37:00.000Z",
        delay=delay,
        progress=progress,
        timestamp="2026-11-23T21:10:53.755Z",
    )


def _mock_sse_response(lines: List[bytes]) -> MagicMock:
    """Create a context-manager mock HTTP response yielding SSE byte lines."""
    resp = MagicMock()
    resp.__enter__.return_value = iter(lines)
    resp.__exit__.return_value = False
    return resp


class TestMultiplexedSSEClient(unittest.TestCase):
    """Phase 1 tests: RailwayGovClient multiplexed SSE parsing and resilience."""

    def setUp(self) -> None:
        self.client = RailwayGovClient("https://railway.gov.gr")

    @patch("urllib.request.urlopen")
    def test_tc_f101_01_stream_events_parses_all_channels_and_skips_comments(
        self, mock_urlopen: MagicMock
    ) -> None:
        """TC-F101-01: Decode trainHistory, scheduleCards, trainPositionsUx, delayUpdates, error."""
        history_payload = {
            "history": {
                "t1": [
                    {"ts": "2026-11-23T21:09:51.000Z", "lat": 38.01, "lng": 23.71, "speed": 80.0},
                    {"ts": "2026-11-23T21:09:53.000Z", "lat": 38.02, "lng": 23.72, "speed": 95.0},
                ]
            }
        }
        ghost_payload = {
            "ghosts": [
                {
                    "id": "schedule:abc123",
                    "trainNumber": "1237",
                    "scheduleId": "abc123",
                    "origin": "Αεροδρόμιο",
                    "destination": "Πειραιάς",
                    "corridor": "PIRAIR",
                    "serviceType": "Suburban",
                    "delay": 2,
                    "progress": 0.45,
                    "ghost": True,
                }
            ]
        }
        pos_payload = {
            "positions": [
                {"id": "t1", "trainNumber": "IC56", "lat": 38.03, "lng": 23.73, "speed": 110.0}
            ]
        }
        delay_payload = {"delays": [{"trainNumber": "IC56", "delay": 7}]}
        err_payload = {"error": "Upstream broker transient lag"}

        lines = [
            b": keep-alive heartbeat\n",
            b"\n",
            b"event: unknownFutureChannel\n",
            b'data: {"foo": "bar"}\n',
            b"\n",
            b"event: trainHistory\n",
            f"data: {json.dumps(history_payload)}\n".encode("utf-8"),
            b"\n",
            b"event: scheduleCards\n",
            f"data: {json.dumps(ghost_payload)}\n".encode("utf-8"),
            b"\n",
            b"event: trainPositionsUx\n",
            f"data: {json.dumps(pos_payload)}\n".encode("utf-8"),
            b"\n",
            b"event: delayUpdates\n",
            f"data: {json.dumps(delay_payload)}\n".encode("utf-8"),
            b"\n",
            b"event: error\n",
            f"data: {json.dumps(err_payload)}\n".encode("utf-8"),
            b"\n",
        ]
        mock_urlopen.return_value = _mock_sse_response(lines)

        frames = list(self.client.stream_events(reconnect_delay=0.0))
        self.assertEqual(
            [f.event_type for f in frames],
            [
                SSEEventType.TRAIN_HISTORY,
                SSEEventType.SCHEDULE_CARDS,
                SSEEventType.TRAIN_POSITIONS_UX,
                SSEEventType.DELAY_UPDATES,
                SSEEventType.ERROR,
            ],
        )
        self.assertEqual(len(frames[0].history), 1)
        self.assertEqual(frames[0].history[0][0], "t1")
        self.assertEqual(len(frames[0].history[0][1]), 2)
        self.assertEqual(frames[1].ghosts[0].train_number, "1237")
        self.assertEqual(frames[2].positions[0].train_number, "IC56")
        self.assertEqual(frames[3].delays[0], DelayUpdate(train_number="IC56", delay=7))
        self.assertIn("transient lag", frames[4].error_message or "")

    @patch("urllib.request.urlopen")
    def test_tc_f101_02_raw_train_positions_gated_by_flag(
        self, mock_urlopen: MagicMock
    ) -> None:
        """TC-F101-02: Raw trainPositions channel emitted only when include_raw_positions=True."""
        lines = [
            b"event: trainPositions\n",
            b'data: {"positions": [{"id": "r1", "trainNumber": "50", "lat": 38.5, "lng": 23.1}]}\n',
            b"\n",
        ]
        mock_urlopen.return_value = _mock_sse_response(lines)
        self.assertEqual(list(self.client.stream_events(reconnect_delay=0.0)), [])

        mock_urlopen.return_value = _mock_sse_response(lines)
        frames = list(
            self.client.stream_events(include_raw_positions=True, reconnect_delay=0.0)
        )
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].event_type, SSEEventType.TRAIN_POSITIONS)
        self.assertEqual(frames[0].positions[0].id, "r1")

    @patch("urllib.request.urlopen")
    def test_tc_f101_03_oversized_frame_dropped_cleanly(
        self, mock_urlopen: MagicMock
    ) -> None:
        """TC-F101-03: Frames exceeding MAX_SSE_FRAME_BYTES are dropped without crashing."""
        huge_chunk = b"data: " + (b"x" * (MAX_SSE_FRAME_BYTES + 128)) + b"\n"
        lines = [
            b"event: trainHistory\n",
            huge_chunk,
            b"\n",
            b"event: delayUpdates\n",
            b'data: {"delays": [{"trainNumber": "1597", "delay": 5}]}\n',
            b"\n",
        ]
        mock_urlopen.return_value = _mock_sse_response(lines)
        frames = list(self.client.stream_events(reconnect_delay=0.0))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].event_type, SSEEventType.DELAY_UPDATES)
        self.assertEqual(frames[0].delays[0].delay, 5)

    def test_tc_f101_04_defensive_validation_and_bounds(self) -> None:
        """TC-F101-04: Reject Null Island/NaN history points and clamp extreme delays/progress."""
        with self.assertRaises(ValueError):
            TrainHistoryPoint.from_dict({"lat": 0.0, "lng": 0.0}, fallback_epoch=1000.0)
        with self.assertRaises(ValueError):
            TrainHistoryPoint.from_dict({"lat": float("nan"), "lng": 23.0}, fallback_epoch=1000.0)
        with self.assertRaises(ValueError):
            TrainHistoryPoint.from_dict({"lat": 95.0, "lng": 23.0}, fallback_epoch=1000.0)

        g = GhostTrain.from_dict(
            {"scheduleId": "s99", "trainNumber": "99", "delay": 999999, "progress": 1.8}
        )
        self.assertEqual(g.id, "schedule:s99")
        self.assertEqual(g.delay, 1440)
        self.assertAlmostEqual(g.progress or 0.0, 1.0)

        upd_low = DelayUpdate.from_dict({"trainNumber": "99", "delay": -500})
        self.assertEqual(upd_low.delay, -120)

    @patch("urllib.request.urlopen")
    def test_tc_f101_05_empty_schedule_cards_emits_clear_frame(
        self, mock_urlopen: MagicMock
    ) -> None:
        """TC-F101-05: Empty scheduleCards payload yields an empty SCHEDULE_CARDS frame."""
        lines = [
            b"event: scheduleCards\n",
            b'data: {"ghosts": []}\n',
            b"\n",
        ]
        mock_urlopen.return_value = _mock_sse_response(lines)
        frames = list(self.client.stream_events(reconnect_delay=0.0))
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].event_type, SSEEventType.SCHEDULE_CARDS)
        self.assertEqual(frames[0].ghosts, ())


class TestMultiplexedStateStore(unittest.TestCase):
    """Phase 2 tests: TrainStateStore staging, ghost isolation, supersession & delay patching."""

    def setUp(self) -> None:
        self.store = TrainStateStore(stale_timeout_sec=90.0, max_trail_len=25)

    def test_tc_f101_07_out_of_order_history_hydrates_first_live_frame(self) -> None:
        """TC-F101-07: trainHistory arriving BEFORE trainPositionsUx hydrates trails immediately."""
        now = 1_763_930_000.0
        pts = tuple(
            TrainHistoryPoint(
                lat=39.50 + i * 0.002,
                lng=22.40 + i * 0.001,
                timestamp_epoch=now - (20 - i),
                speed=60.0 + i * 3.0,
            )
            for i in range(20)
        )
        hydrated = self.store.hydrate_train_history([("live-1", pts)], now=now)
        self.assertEqual(hydrated, 1)
        self.assertEqual(len(self.store.get_snapshot().trains), 0)

        pos = _make_live_pos(tid="live-1", lat=39.545, lng=22.425, speed=125.0)
        self.store.update_positions([pos], virtual_now=now + 1.0)

        snap = self.store.get_snapshot(now=now + 1.0)
        rec = snap.trains["live-1"]
        self.assertEqual(len(rec.trail), 21)
        self.assertEqual(snap.history_hydrated_trains, 1)
        spd_hist = rec.get_speed_history(max_samples=15)
        self.assertEqual(len(spd_hist), 15)
        self.assertAlmostEqual(spd_hist[-1], 125.0)

    def test_tc_f101_08_late_history_merges_without_overwriting_newer_live_tip(self) -> None:
        """TC-F101-08: trainHistory arriving after live updates merges chronologically."""
        t0 = 1_763_930_100.0
        pos = _make_live_pos(tid="live-1", lat=39.60, lng=22.42, speed=130.0)
        self.store.update_positions([pos], virtual_now=t0)
        orig_last_updated = self.store.get_snapshot(now=t0).trains["live-1"].last_updated

        older_pts = (
            TrainHistoryPoint(lat=39.55, lng=22.40, timestamp_epoch=t0 - 10.0, speed=110.0),
            TrainHistoryPoint(lat=39.58, lng=22.41, timestamp_epoch=t0 - 5.0, speed=120.0),
        )
        self.store.hydrate_train_history([("live-1", older_pts)], now=t0 + 2.0)

        rec = self.store.get_snapshot(now=t0 + 2.0).trains["live-1"]
        self.assertEqual(len(rec.trail), 3)
        self.assertAlmostEqual(rec.trail[0].lat, 39.55)
        self.assertAlmostEqual(rec.trail[-1].lat, 39.60)
        self.assertAlmostEqual(rec.last_updated, orig_last_updated)

    def test_tc_f101_09_pending_history_bounded_and_ttl_evicted(self) -> None:
        """TC-F101-09: _pending_history enforces LRU cap and expires stale entries on prune."""
        t0 = 1_763_930_000.0
        pt = (TrainHistoryPoint(lat=38.0, lng=23.7, timestamp_epoch=t0, speed=80.0),)
        entries = [(f"tid-{i}", pt) for i in range(MAX_PENDING_HISTORY_TRAINS + 25)]
        self.store.hydrate_train_history(entries, now=t0)
        self.assertEqual(len(self.store._pending_history), MAX_PENDING_HISTORY_TRAINS)
        self.assertNotIn("tid-0", self.store._pending_history)

        self.store.prune_stale_trains(now=t0 + PENDING_HISTORY_TTL_SEC + 10.0)
        self.assertEqual(len(self.store._pending_history), 0)

    def test_tc_f101_10_ghost_isolation_from_gps_watchdogs(self) -> None:
        """TC-F101-10: Ghosts stay out of _trains and never trigger F-5.1/F-5.2/F-5.3 alerts."""
        t0 = 1_763_930_000.0
        g1 = _make_ghost(gid="schedule:g1", train_number="1237", schedule_id="g1")
        g2 = _make_ghost(gid="schedule:g2", train_number="1239", schedule_id="g2")
        self.store.update_ghost_trains([g1, g2], now=t0)

        snap = self.store.get_snapshot(now=t0 + 180.0)
        self.assertEqual(len(snap.trains), 0)
        self.assertEqual(snap.ghost_count, 2)
        self.assertEqual(len(snap.filtered_ghosts), 2)
        self.assertEqual(snap.overspeed_count, 0)
        self.assertEqual(snap.frozen_count, 0)
        self.assertEqual(snap.dropped_count, 0)
        self.assertEqual(len(snap.active_conflicts), 0)

    def test_tc_f101_11_live_supersession_and_selection_promotion(self) -> None:
        """TC-F101-11: Live GPS train suppresses duplicate ghost and promotes active selection."""
        t0 = 1_763_930_000.0
        ghost = _make_ghost(
            gid="schedule:sched-1237",
            train_number="1237",
            schedule_id="sched-1237",
        )
        self.store.update_ghost_trains([ghost], now=t0)
        self.assertTrue(self.store.select_by_id("schedule:sched-1237"))
        snap1 = self.store.get_snapshot(now=t0)
        self.assertIsNone(snap1.selected_train)
        self.assertIsNotNone(snap1.selected_ghost)
        self.assertEqual(snap1.selected_ghost.ghost.train_number, "1237")  # type: ignore[union-attr]

        # Live GPS transponder for the same schedule comes online
        live = _make_live_pos(
            tid="live-1237",
            train_number="1237",
            schedule_id="sched-1237",
            corridor="PIRAIR",
        )
        self.store.update_positions([live], virtual_now=t0 + 2.0)

        snap2 = self.store.get_snapshot(now=t0 + 2.0)
        self.assertEqual(snap2.ghost_count, 0)
        self.assertEqual(len(snap2.filtered_ghosts), 0)
        self.assertIsNone(snap2.selected_ghost)
        self.assertIsNotNone(snap2.selected_train)
        self.assertEqual(snap2.selected_train.position.id, "live-1237")  # type: ignore[union-attr]

    def test_tc_f101_12_delay_updates_patch_coupled_consists_without_refreshing_gps_age(
        self,
    ) -> None:
        """TC-F101-12: delayUpdates patches all coupled consists & ghosts without touching last_updated."""
        t0 = 1_763_930_000.0
        unit_a = _make_live_pos(tid="emu-a", train_number="1597", delay=1, loco="460103")
        unit_b = _make_live_pos(tid="emu-b", train_number="1597", delay=1, loco="460108")
        ghost = _make_ghost(gid="schedule:g1237", train_number="1237", delay=0)
        self.store.update_positions([unit_a, unit_b], virtual_now=t0)
        self.store.update_ghost_trains([ghost], now=t0)

        modified = self.store.apply_delay_updates(
            [
                DelayUpdate(train_number="1597", delay=9),
                DelayUpdate(train_number="1237", delay=6),
            ],
            now=t0 + 60.0,
        )
        self.assertEqual(modified, 3)

        snap = self.store.get_snapshot(now=t0 + 60.0)
        self.assertEqual(snap.trains["emu-a"].position.delay, 9)
        self.assertEqual(snap.trains["emu-b"].position.delay, 9)
        self.assertEqual(snap.ghost_trains["schedule:g1237"].ghost.delay, 6)
        # Crucial F-5.2 invariant: GPS last_updated MUST remain t0, NOT t0 + 60
        self.assertAlmostEqual(snap.trains["emu-a"].last_updated, t0)
        self.assertAlmostEqual(snap.trains["emu-b"].last_updated, t0)
        self.assertEqual(snap.delay_updates_applied, 3)

    def test_tc_f101_13_ghost_visibility_toggle_filtering_and_navigation(self) -> None:
        """TC-F101-13: Toggle GhostVisibilityMode, filter ghosts, navigate unified roster, block follow."""
        t0 = 1_763_930_000.0
        live = _make_live_pos(tid="live-1", train_number="IC56", corridor="E85")
        g_pro = _make_ghost(gid="schedule:gp", train_number="1237", corridor="PIRAIR")
        g_ths = _make_ghost(
            gid="schedule:gt",
            train_number="1520",
            corridor="THESSALY",
            origin="Λάρισα",
            destination="Βόλος",
        )
        self.store.update_positions([live], virtual_now=t0)
        self.store.update_ghost_trains([g_pro, g_ths], now=t0)

        # Unified navigation: live-1 -> schedule:gp -> schedule:gt -> live-1
        self.assertEqual(self.store.select_next(), "live-1")
        self.assertTrue(self.store.toggle_follow_mode())
        self.assertEqual(self.store.select_next(), "schedule:gp")
        # Selecting a ghost automatically disengages follow mode
        self.assertFalse(self.store.get_snapshot(now=t0).follow_mode)
        self.assertFalse(self.store.toggle_follow_mode())

        # Corridor filter isolates PIRAIR ghost
        self.store.set_corridor_filter("PROASTIAKOS")
        snap_pro = self.store.get_snapshot(now=t0)
        self.assertEqual([g.ghost.train_number for g in snap_pro.filtered_ghosts], ["1237"])
        self.store.set_corridor_filter(None)

        # Hiding ghosts clears active ghost selection
        self.assertEqual(self.store.toggle_ghost_visibility(), GhostVisibilityMode.HIDE)
        snap_hidden = self.store.get_snapshot(now=t0)
        self.assertEqual(len(snap_hidden.filtered_ghosts), 0)
        self.assertIsNone(snap_hidden.selected_ghost)

        with self.assertRaises(TypeError):
            self.store.set_ghost_visibility_mode("HIDE")  # type: ignore[arg-type]

    def test_tc_f101_14_stale_ghost_pruning(self) -> None:
        """TC-F101-14: Stale ghosts beyond ghost_stale_timeout_sec are pruned cleanly."""
        store = TrainStateStore(stale_timeout_sec=90.0, ghost_stale_timeout_sec=30.0)
        t0 = 1_763_930_000.0
        store.update_ghost_trains([_make_ghost(gid="schedule:old")], now=t0)
        store.select_by_id("schedule:old")

        store.prune_stale_trains(now=t0 + 45.0)
        snap = store.get_snapshot(now=t0 + 45.0)
        self.assertEqual(snap.ghost_count, 0)
        self.assertIsNone(snap.selected_ghost)


class TestMultiplexedWorkerAndMock(unittest.TestCase):
    """Phase 2 tests: StreamWorker SSEFrame dispatch and MockTelemetryEngine.stream_events."""

    def test_tc_f101_15_stream_worker_dispatches_all_frames_and_guards_recorder(self) -> None:
        """TC-F101-15: StreamWorker routes all SSEFrames and sends ONLY GPS positions to recorder."""
        store = TrainStateStore()
        mock_recorder = MagicMock()
        mock_recorder.is_running = True
        now = time.time()

        live_pos = _make_live_pos(tid="t1", train_number="IC56", delay=1)
        hist_pts = (
            TrainHistoryPoint(lat=39.61, lng=22.40, timestamp_epoch=now - 5.0, speed=100.0),
            TrainHistoryPoint(lat=39.62, lng=22.41, timestamp_epoch=now - 2.0, speed=110.0),
        )
        ghost = _make_ghost(gid="schedule:g1", train_number="1237", delay=2)

        frames = [
            SSEFrame(event_type=SSEEventType.TRAIN_HISTORY, history=(("t1", hist_pts),)),
            SSEFrame(event_type=SSEEventType.SCHEDULE_CARDS, ghosts=(ghost,)),
            SSEFrame(event_type=SSEEventType.TRAIN_POSITIONS_UX, positions=(live_pos,)),
            SSEFrame(
                event_type=SSEEventType.DELAY_UPDATES,
                delays=(DelayUpdate(train_number="IC56", delay=8),),
            ),
            SSEFrame(event_type=SSEEventType.ERROR, error_message="minor warning"),
        ]

        done_evt = threading.Event()

        def finite_sse() -> Any:
            for fr in frames:
                yield fr
            done_evt.set()
            while not worker._stop_event.is_set():
                time.sleep(0.01)

        worker = StreamWorker(
            store=store,
            custom_stream=finite_sse,
            recorder=mock_recorder,
        )
        worker.start()
        try:
            self.assertTrue(done_evt.wait(timeout=2.0))
            time.sleep(0.05)
            snap = store.get_snapshot()
            self.assertIn("t1", snap.trains)
            self.assertEqual(len(snap.trains["t1"].trail), 3)
            self.assertEqual(snap.trains["t1"].position.delay, 8)
            self.assertEqual(snap.ghost_count, 1)
            event_map = dict(snap.stream_event_counts)
            self.assertEqual(event_map.get("trainHistory"), 1)
            self.assertEqual(event_map.get("scheduleCards"), 1)
            self.assertEqual(event_map.get("trainPositionsUx"), 1)
            self.assertEqual(event_map.get("delayUpdates"), 1)
            self.assertEqual(event_map.get("error"), 1)

            # Recorder received ONLY the GPS position batch, never ghost runs
            mock_recorder.enqueue_batch.assert_called_once()
            recorded_batch = mock_recorder.enqueue_batch.call_args[0][0]
            self.assertEqual(len(recorded_batch), 1)
            self.assertEqual(recorded_batch[0].id, "t1")
        finally:
            worker.stop()

    def test_tc_f101_16_mock_engine_stream_events_emits_history_ghosts_positions(self) -> None:
        """TC-F101-16: MockTelemetryEngine.stream_events() emits startup history, ghosts & positions."""
        engine = MockTelemetryEngine()
        stop_evt = threading.Event()
        gen = engine.stream_events(interval_sec=0.001, stop_event=stop_evt)

        first = next(gen)
        second = next(gen)
        third = next(gen)
        stop_evt.set()

        self.assertEqual(first.event_type, SSEEventType.TRAIN_HISTORY)
        self.assertGreater(len(first.history), 0)
        self.assertEqual(second.event_type, SSEEventType.SCHEDULE_CARDS)
        self.assertGreaterEqual(len(second.ghosts), 2)
        self.assertEqual(third.event_type, SSEEventType.TRAIN_POSITIONS_UX)
        self.assertGreater(len(third.positions), 0)


class TestMultiplexedTUI(unittest.TestCase):
    """Phase 3 tests: TUIApp keybindings/guards and TUIRenderer UTF-8 + ASCII rendering."""

    def setUp(self) -> None:
        self.stations = list(load_stations().values())
        self.tracks = list(load_tracks().values())
        self.store = TrainStateStore()
        self.live = _make_live_pos(tid="live-1", train_number="IC56", corridor="E85")
        self.ghost = _make_ghost(
            gid="schedule:g1237",
            train_number="1237",
            origin="Αεροδρόμιο",
            destination="Πειραιάς",
            delay=4,
        )
        self.store.update_positions([self.live])
        self.store.update_ghost_trains([self.ghost])

    def _make_app(self) -> TUIApp:
        stdscr = _MockCursesWin(30, 120)
        worker = MagicMock(spec=StreamWorker)
        worker.mock_engine = MockTelemetryEngine()
        worker.client = None
        return TUIApp(
            stdscr=stdscr,
            store=self.store,
            worker=worker,
            stations=self.stations,
            tracks=self.tracks,
        )

    def test_tc_f101_17_key_u_toggles_ghosts_in_normal_and_types_in_search(self) -> None:
        """TC-F101-17: 'u'/'U' toggles GhostVisibilityMode in NORMAL and appends in SEARCH."""
        app = self._make_app()
        self.assertEqual(self.store.get_snapshot().ghost_visibility_mode, GhostVisibilityMode.SHOW)

        app._handle_normal_input(ord("u"))
        self.assertEqual(self.store.get_snapshot().ghost_visibility_mode, GhostVisibilityMode.HIDE)
        self.assertIn("HIDDEN", app.toast_message or "")

        app._handle_normal_input(ord("U"))
        self.assertEqual(self.store.get_snapshot().ghost_visibility_mode, GhostVisibilityMode.SHOW)
        self.assertIn("SHOW", app.toast_message or "")

        app.input_mode = InputMode.SEARCH
        app.search_buffer = ""
        app._handle_search_input(ord("u"))
        self.assertEqual(app.search_buffer, "u")
        self.assertEqual(self.store.get_snapshot().ghost_visibility_mode, GhostVisibilityMode.SHOW)

    def test_tc_f101_18_ghost_selected_guards_and_timetable_modal(self) -> None:
        """TC-F101-18: f/e/w guard selected ghosts with toasts; 'i' opens ghost timetable modal."""
        app = self._make_app()
        self.assertTrue(self.store.select_by_id("schedule:g1237"))

        app.toggle_follow()
        self.assertFalse(self.store.get_snapshot().follow_mode)
        self.assertIn("no live GPS", app.toast_message or "")

        self.assertIsNone(app.export_selected_train_snapshot("geojson"))
        self.assertIn("ghost run", (app.toast_message or "").lower())

        app.launch_cab_stream_for_selected_train()
        self.assertIn("ghost run", (app.toast_message or "").lower())

        app.toggle_timetable_modal()
        self.assertEqual(app.input_mode, InputMode.TIMETABLE)
        self.assertTrue(app.show_timetable)

    def test_tc_f101_19_renderer_roster_ghost_inspector_and_station_board_utf8_and_ascii(
        self,
    ) -> None:
        """TC-F101-19: Render ghost roster rows, Ghost Inspector & Station Board in 80x24 UTF-8/ASCII."""
        self.store.select_by_id("schedule:g1237")
        snap = self.store.get_snapshot()

        for ascii_mode in (False, True):
            renderer = TUIRenderer(
                stations=self.stations,
                tracks=self.tracks,
                ascii_mode=ascii_mode,
            )
            roster_win = _MockCursesWin(12, 34)
            renderer.render_roster(roster_win, snap)
            roster_txt = roster_win.all_text()
            self.assertIn("|+1G", roster_txt)
            self.assertIn("1237", roster_txt)
            self.assertIn("GHT", roster_txt)

            wide_roster_win = _MockCursesWin(12, 42)
            renderer.render_roster(wide_roster_win, snap)
            self.assertIn("+1 GHOST", wide_roster_win.all_text())

            insp_win = _MockCursesWin(11, 34)
            renderer.render_inspector(insp_win, snap)
            insp_txt = insp_win.all_text()
            self.assertIn("SELECTED GHOST RUN", insp_txt)
            self.assertIn("1237 [GHOST]", insp_txt)
            self.assertIn("22:32", insp_txt)  # Europe/Athens local conversion from 20:32Z
            self.assertIn("NONE", insp_txt)

            # Station board for Athens Airport (Αεροδρόμιο) includes ghost 1237
            airport_stn = renderer.resolve_station("Αεροδρόμιο")
            self.assertIsNotNone(airport_stn)
            entries = compute_station_board_entries(
                station=airport_stn,  # type: ignore[arg-type]
                trains=snap.filtered_trains,
                resolve_station_fn=renderer.resolve_station,
                selected_train_id="schedule:g1237",
                ghosts=snap.filtered_ghosts,
            )
            ghost_entries = [e for e in entries if e.train_id == "schedule:g1237"]
            self.assertEqual(len(ghost_entries), 1)
            self.assertEqual(ghost_entries[0].direction, "GHT")
            self.assertEqual(ghost_entries[0].dist_str, "N/A")
            self.assertEqual(ghost_entries[0].eta_str, "22:32")


class TestMultiplexedGtfsAndMetrics(unittest.TestCase):
    """Phase 4 tests: GTFS-RT v2.0 ghost TripUpdates/Alerts and Prometheus metrics."""

    def test_tc_f101_20_gtfs_rt_ghost_partitioning_and_proto_roundtrip(self) -> None:
        """TC-F101-20: Ghosts excluded from VehiclePositions, emitted in TripUpdates & Alerts."""
        store = TrainStateStore()
        live = _make_live_pos(
            tid="live-56",
            train_number="IC56",
            schedule_id="sched-56",
            delay=2,
        )
        ghost_delayed = _make_ghost(
            gid="schedule:sched-1237",
            train_number="1237",
            schedule_id="sched-1237",
            delay=20,
        )
        ghost_dup = _make_ghost(
            gid="schedule:sched-56",
            train_number="IC56",
            schedule_id="sched-56",
            delay=20,
        )
        store.update_positions([live])
        store.update_ghost_trains([ghost_delayed, ghost_dup])
        snap = store.get_snapshot()

        mapper = GtfsRtMapper(GtfsRtConfig(delay_alert_min=15, include_ghosts=True))

        # 1. VehiclePositions MUST contain ONLY live GPS train, never ghosts
        vp_msg = mapper.build_feed(snap, feed_kind=FeedKind.VEHICLES)
        vp_ids = {e.id for e in vp_msg.entity}
        self.assertEqual(vp_ids, {"vp:live-56"})

        # 2. TripUpdates MUST contain live IC56 + non-superseded ghost 1237 (no duplicate sched-56)
        tu_msg = mapper.build_feed(snap, feed_kind=FeedKind.TRIPS)
        tu_ids = {e.id for e in tu_msg.entity}
        self.assertEqual(tu_ids, {"tu:sched-56", "tu:sched-1237"})
        ghost_tu = next(e.trip_update for e in tu_msg.entity if e.id == "tu:sched-1237")
        self.assertIsNotNone(ghost_tu)
        self.assertEqual(ghost_tu.delay, 1200)  # type: ignore[union-attr]
        self.assertIsNone(ghost_tu.vehicle)  # type: ignore[union-attr]

        # 3. Alerts MUST include bilingual delay alert for delayed ghost 1237
        al_msg = mapper.build_feed(snap, feed_kind=FeedKind.ALERTS)
        al_ids = {e.id for e in al_msg.entity}
        self.assertIn("al:delay:sched-1237", al_ids)

        # 4. Binary Protobuf roundtrip of COMBINED feed
        combined = mapper.build_feed(snap, feed_kind=FeedKind.COMBINED)
        decoded = FeedMessage.from_proto(combined.to_proto())
        self.assertEqual({e.id for e in decoded.entity}, {e.id for e in combined.entity})

        # 5. Respect include_ghosts=False
        mapper_no_ghosts = GtfsRtMapper(GtfsRtConfig(include_ghosts=False))
        tu_only_live = mapper_no_ghosts.build_feed(snap, feed_kind=FeedKind.TRIPS)
        self.assertEqual({e.id for e in tu_only_live.entity}, {"tu:sched-56"})

    def test_tc_f101_21_prometheus_ghost_gauge_and_multiplexed_event_counter(self) -> None:
        """TC-F101-21: Exporter reports railmon_ghost_trains and railmon_stream_events_total."""
        from exporter.formatter import MetricsFormatter

        store = TrainStateStore()
        store.update_positions([_make_live_pos(tid="live-1", corridor="PATHE")])
        store.update_ghost_trains([_make_ghost(corridor="PIRAIR")])
        store.record_stream_event("trainPositionsUx")
        store.record_stream_event("trainHistory")
        store.record_stream_event("scheduleCards")
        store.record_stream_event("delayUpdates")

        t_fams = {f.name: f for f in TrainTelemetryCollector(store).collect()}
        active_samples = {dict(s.labels)["corridor"]: s.value for s in t_fams["railmon_active_trains"].samples}
        ghost_samples = {dict(s.labels)["corridor"]: s.value for s in t_fams["railmon_ghost_trains"].samples}
        self.assertEqual(active_samples, {"PATHE": 1.0})
        self.assertEqual(ghost_samples, {"PIRAIR": 1.0})

        stream_fams_list = StreamMetricsCollector(store).collect()
        s_fams = {f.name: f for f in stream_fams_list}
        evt_samples = {
            dict(s.labels)["event_type"]: s.value
            for s in s_fams["railmon_stream_events"].samples
        }
        self.assertEqual(evt_samples["trainHistory"], 1.0)
        self.assertEqual(evt_samples["scheduleCards"], 1.0)
        self.assertEqual(evt_samples["delayUpdates"], 1.0)
        self.assertEqual(evt_samples["trainPositionsUx"], 1.0)

        # Verify wire outputs in both OpenMetrics 1.0.0 and Prometheus 0.0.4 emit _total sample names
        om_text = MetricsFormatter.format_openmetrics_100(stream_fams_list).decode("utf-8")
        prom_text = MetricsFormatter.format_prometheus_004(stream_fams_list).decode("utf-8")
        self.assertIn("# TYPE railmon_stream_events counter\n", om_text)
        self.assertIn('railmon_stream_events_total{event_type="trainHistory"} 1\n', om_text)
        self.assertIn("# TYPE railmon_stream_events_total counter\n", prom_text)
        self.assertIn('railmon_stream_events_total{event_type="trainHistory"} 1\n', prom_text)


if __name__ == "__main__":
    unittest.main()
