"""Phase 2 (P1-14) regression tests: concurrency, CoW snapshots, RFC 9110, WHATWG SSE & GTFS-RT."""

import io
import http.client
import os
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from types import MappingProxyType
from unittest.mock import MagicMock, patch

from gtfs_rt.exporter import MAX_INSPECT_BYTES, inspect_pb_file, write_atomic_bytes
from gtfs_rt.mapper import FeedCache, GtfsRtMapper, _AthensFallbackTZ
from gtfs_rt.models import (
    FeedEntity,
    FeedHeader,
    FeedKind,
    FeedMessage,
    GtfsRtConfig,
    WireDecodeError,
)
from railmon_http import accepts_gzip, format_representation_etag
from server.sse import SSEBroadcaster
from storage.db import Database
from storage.recorder import RecorderConfig, TelemetryRecorder
from train_tracker import RailwayGovClient, TrainPosition
from tui.renderer import safe_addstr
from tui.state import ConnectionStatus, TrainStateStore


def _pos(tid: str = "t1", tnum: str = "IC52", lat: float = 38.5, lng: float = 22.9) -> TrainPosition:
    return TrainPosition(
        id=tid,
        train_number=tnum,
        locomotive_number="120-001",
        name=f"IC {tnum}",
        train_type="IC",
        service_type="InterCity",
        status="Running",
        origin="Athens",
        destination="Thessaloniki",
        next_station="Larissa",
        speed=100.0,
        course=0.0,
        track_heading=0.0,
        lat=lat,
        lng=lng,
        altitude=None,
        delay=2,
        progress=0.5,
        chainage_m=100000,
        corridor="pathe-mainline",
        distance_to_destination_m=100000,
        timestamp="2026-07-15T20:30:00Z",
        schedule_id=None,
    )


class TestPhase2ConcurrencyAndProtocols(unittest.TestCase):
    """Regression coverage for P1-6..P1-10, P1-12..P1-14, P1-16, and P1-19."""

    def test_tc_con_01_cow_snapshot_immutable_and_maintenance_tick(self) -> None:
        """TC-CON-01: CoW get_snapshot() returns cached immutable MappingProxyType; maintenance tick bumps _version."""
        store = TrainStateStore()
        store.update_positions([_pos("1", "IC52")])
        snap1 = store.get_snapshot()
        snap2 = store.get_snapshot()
        self.assertIs(snap1, snap2)
        self.assertIsInstance(snap1.trains, MappingProxyType)
        with self.assertRaises(TypeError):
            snap1.trains["bad"] = None  # type: ignore[index]

        v_before = snap1.version
        # Simulate open-line telemetry drop after 300s via run_maintenance_tick
        changed = store.run_maintenance_tick(now=snap1.last_stream_update + 300.0)
        self.assertTrue(changed)
        snap3 = store.get_snapshot()
        self.assertGreater(snap3.version, v_before)

    def test_tc_con_03_monotonic_feed_cache_boot_nonce_and_lazy_json(self) -> None:
        """TC-CON-03: FeedCache is strictly monotonic, uses per-instance boot nonce, and serializes JSON lazily."""
        store = TrainStateStore()
        store.update_positions([_pos("1", "IC52")])
        snap_v1 = store.get_snapshot()
        store.update_positions([_pos("1", "IC52", lat=38.55)])
        snap_v2 = store.get_snapshot()

        mapper = GtfsRtMapper(GtfsRtConfig())
        cache_a = FeedCache(mapper)
        cache_b = FeedCache(mapper)

        c_v2 = cache_a.get_or_build(snap_v2, FeedKind.COMBINED)
        # Lazy JSON not materialized until accessed
        self.assertIsNone(c_v2._json_box[0])
        # Out-of-order older snapshot must not roll _active_version backwards
        _ = cache_a.get_or_build(snap_v1, FeedKind.COMBINED)
        self.assertEqual(cache_a._active_version, snap_v2.version)

        # Accessing json_bytes materializes JSON on demand
        self.assertIn(b"IC52", c_v2.json_bytes)
        self.assertIsNotNone(c_v2._json_box[0])

        # Separate instances (restarts) produce distinct ETags for same version
        c_b = cache_b.get_or_build(snap_v2, FeedKind.COMBINED)
        self.assertNotEqual(c_v2.etag_pb, c_b.etag_pb)

    def test_tc_con_04_recorder_enqueue_batch_item_bound(self) -> None:
        """TC-CON-04: TelemetryRecorder.enqueue_batch() bounds total queued items by queue_maxsize."""
        from unittest.mock import PropertyMock

        db = Database(":memory:")
        try:
            cfg = RecorderConfig(queue_maxsize=5, batch_size=100, flush_interval_sec=60.0)
            rec = TelemetryRecorder(db, config=cfg)
            with patch.object(TelemetryRecorder, "is_running", new_callable=PropertyMock, return_value=True):
                accepted = rec.enqueue_batch([_pos(f"t{i}", f"IC{i}") for i in range(20)])
                self.assertEqual(accepted, 5)
                self.assertFalse(rec.enqueue(_pos("overflow", "IC99")))
        finally:
            db.close()

    def test_tc_con_05_sse_broadcaster_staleness_and_shutdown_sentinel(self) -> None:
        """TC-CON-05: broadcast() does not touch() stalled clients; shutdown() pushes wake sentinel."""
        bc = SSEBroadcaster(max_queue_depth=4, max_clients=4)
        conn = bc.register_client("127.0.0.1")
        conn.last_event_at = 100.0
        bc.broadcast("trainPositionsUx", "{}")
        # broadcast() must NOT advance last_event_at for an unconsumed queue
        self.assertEqual(conn.last_event_at, 100.0)
        # shutdown() unregisters client and pushes None sentinel
        bc.shutdown()
        self.assertEqual(bc.client_count, 0)
        self.assertIsNone(conn.queue.get_nowait())

    def test_tc_proto_01_rfc9110_accepts_gzip_and_etag_scoping(self) -> None:
        """TC-PROTO-01: RFC 9110 Accept-Encoding q= parsing and encoding-scoped ETags."""
        self.assertTrue(accepts_gzip("gzip, deflate"))
        self.assertTrue(accepts_gzip("GZIP;q=0.8"))
        self.assertFalse(accepts_gzip("gzip;q=0"))
        self.assertFalse(accepts_gzip("br, deflate"))
        self.assertFalse(accepts_gzip(None))
        self.assertEqual(format_representation_etag('"abc"', False), '"abc"')
        self.assertEqual(format_representation_etag('"abc"', True), '"abc-gz"')
        self.assertEqual(format_representation_etag('W/"abc"', True), 'W/"abc-gz"')

    def test_tc_proto_02_inspect_pb_bounded_and_proto_entity_cap(self) -> None:
        """TC-PROTO-02: FeedMessage.from_proto() enforces max_entities cap; inspect_pb_file enforces MAX_INSPECT_BYTES."""
        msg = FeedMessage(
            header=FeedHeader(gtfs_realtime_version="2.0", timestamp=1700000000),
            entity=tuple(FeedEntity(id=f"e{i}") for i in range(5)),
        )
        raw = msg.to_proto()
        with self.assertRaises(WireDecodeError):
            FeedMessage.from_proto(raw, max_entities=3)

        with tempfile.TemporaryDirectory() as tmpdir:
            big_path = os.path.join(tmpdir, "big.pb")
            with open(big_path, "wb") as fh:
                fh.write(b"x" * (MAX_INSPECT_BYTES + 16))
            with self.assertRaises(ValueError):
                inspect_pb_file(big_path)

    def test_tc_proto_03_whatwg_sse_framing_bom_multiline_and_attempt_cap(self) -> None:
        """TC-PROTO-03: WHATWG SSE BOM stripping, multi-line data newline join, Last-Event-ID, and attempt cap."""
        client = RailwayGovClient()
        # Cap exponent at attempt=2000 (no OverflowError)
        wait_sec, next_attempt = client._compute_sse_reconnect_wait(
            http.client.IncompleteRead(b""), reconnect_delay=3.0, attempt=2000, consecutive_failures=1
        )
        self.assertLessEqual(wait_sec, 60.0)
        self.assertEqual(next_attempt, 2001)

        # WHATWG BOM + id + multi-line data joined with '\n'
        payload = (
            "\ufeffid: evt-42\n"
            "event: error\n"
            "data: line1\n"
            "data: line2\n\n"
        ).encode("utf-8")
        last_id_box: list = [None]
        frames = list(
            client._iter_sse_frames(
                resp=io.BytesIO(payload),
                stop_event=threading.Event(),
                last_event_id_box=last_id_box,
            )
        )
        self.assertEqual(last_id_box[0], "evt-42")
        self.assertEqual(len(frames), 1)
        self.assertEqual(frames[0].error_message, "line1\nline2")

    def test_tc_tui_17_safe_addstr_ascii_fallback(self) -> None:
        """TC-TUI-17: safe_addstr catches UnicodeEncodeError on ASCII locales and degrades cleanly."""
        win = MagicMock()
        win.getmaxyx.return_value = (24, 80)
        calls = []

        def _addstr(y: int, x: int, text: str, attr: int) -> None:
            calls.append(text)
            if len(calls) == 1:
                raise UnicodeEncodeError("ascii", text, 0, 1, "ordinal not in range(128)")

        win.addstr.side_effect = _addstr
        safe_addstr(win, 0, 0, "Αθήνα → Θεσσαλονίκη")
        self.assertEqual(len(calls), 2)
        calls[1].encode("ascii")  # Second attempt is strictly ASCII

    def test_tc_gtfs_20_athens_fallback_dst_and_midnight_trip_date(self) -> None:
        """TC-GTFS-20: _AthensFallbackTZ computes exact EU EET/EEST offsets; overnight trip keeps departure start_date."""
        tz = _AthensFallbackTZ()
        winter = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz)
        summer = datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc).astimezone(tz)
        self.assertEqual(winter.utcoffset().total_seconds(), 2 * 3600)  # type: ignore[union-attr]
        self.assertEqual(summer.utcoffset().total_seconds(), 3 * 3600)  # type: ignore[union-attr]
        self.assertEqual(winter.tzname(), "EET")
        self.assertEqual(summer.tzname(), "EEST")

        # Overnight train departing 2026-07-15T23:30+03:00 checked at 2026-07-16T01:15+03:00
        mapper = GtfsRtMapper(GtfsRtConfig())
        ref_after_midnight = int(datetime(2026, 7, 15, 22, 15, tzinfo=timezone.utc).timestamp())
        desc = mapper._build_trip_desc(
            _pos("t600", "IC600"),
            ref_epoch=ref_after_midnight,
            sched={"scheduledDeparture": "2026-07-15T20:30:00Z"},
        )
        self.assertEqual(desc.start_date, "20260715")

    def test_tc_gtfs_21_write_atomic_bytes_mkstemp_and_win_retry(self) -> None:
        """TC-GTFS-21: write_atomic_bytes succeeds via mkstemp and retries transient Windows PermissionError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            target = os.path.join(tmpdir, "feed.pb")
            orig_replace = os.replace
            attempts = [0]

            def _flaky_replace(src: str, dst: str) -> None:
                attempts[0] += 1
                if attempts[0] == 1:
                    raise PermissionError("locked by reader")
                orig_replace(src, dst)

            with patch("sys.platform", "win32"), patch("os.replace", side_effect=_flaky_replace):
                out = write_atomic_bytes(target, b"gtfs-rt-bytes")
            self.assertEqual(attempts[0], 2)
            with open(out, "rb") as fh:
                self.assertEqual(fh.read(), b"gtfs-rt-bytes")


if __name__ == "__main__":
    unittest.main()
