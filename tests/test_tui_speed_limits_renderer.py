"""Unit tests for railway corridor speed limits and slow order TUI rendering (F-5.3)."""

import curses
import unittest
from typing import Optional
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition
from tui.geo import (
    GeoPoint,
    OverspeedSeverity,
    SlowOrder,
    SpeedEnforcementResult,
    evaluate_train_speed,
)
from tui.mock_engine import load_infrastructure, load_slow_orders, load_speed_limits, load_stations, load_tracks
from tui.renderer import TUIRenderer
from tui.state import ConnectionStatus, StateSnapshot, TrainRecord, TrainStateStore


class MockCursesWindow:
    """Mock curses window recording text grid blits and operations."""

    def __init__(self, height: int, width: int):
        self.height = height
        self.width = width
        self.strings = []
        self.erased = False
        self.boxed = False

    def getmaxyx(self):
        return self.height, self.width

    def erase(self):
        self.erased = True

    def box(self):
        self.boxed = True

    def border(self, *args):
        self.boxed = True

    def addstr(self, y, x, s, attr=0):
        self.strings.append((y, x, s, attr))

    def noutrefresh(self):
        pass


def _make_train_position(
    train_id: str = "train-1",
    train_number: str = "IC52",
    lat: float = 39.5050,
    lng: float = 22.3680,
    speed: float = 140.0,
    corridor: str = "pathe-mainline",
) -> TrainPosition:
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120010",
        name=train_number,
        train_type="Intercity",
        service_type="Passenger",
        status="In Service",
        origin="Athens",
        destination="Thessaloniki",
        next_station="Larissa",
        speed=speed,
        delay=4.0,
        course=0.0,
        track_heading=0.0,
        lat=lat,
        lng=lng,
        altitude=100.0,
        progress=0.5,
        chainage_m=200000.0,
        corridor=corridor,
        distance_to_destination_m=100000.0,
        timestamp=1000.0,
        schedule_id="SCHED1",
    )


class TestSpeedLimitsRenderer(unittest.TestCase):
    """Test suite for TUIRenderer speed limits exceedance and slow order indicators."""

    def setUp(self) -> None:
        self.stations = list(load_stations().values())
        self.tracks = list(load_tracks().values())
        self.infrastructure = list(load_infrastructure().values())
        self.renderer_utf8 = TUIRenderer(
            stations=self.stations,
            tracks=self.tracks,
            infrastructure=self.infrastructure,
            ascii_mode=False,
        )
        self.renderer_ascii = TUIRenderer(
            stations=self.stations,
            tracks=self.tracks,
            infrastructure=self.infrastructure,
            ascii_mode=True,
        )

    def test_render_inspector_normal_speed(self) -> None:
        pos = _make_train_position(train_id="train-1", train_number="IC52", speed=140.0)
        rec = TrainRecord(position=pos)
        rec.speed_result = SpeedEnforcementResult(
            train_id="IC52",
            current_speed=140.0,
            governing_limit=160.0,
            margin_kmh=-20.0,
            severity=OverspeedSeverity.NORMAL,
            governing_name="PATHE Mainline",
            is_tsr=False,
            status_label="",
            color_pair=1,
        )
        snapshot = StateSnapshot(
            trains={"train-1": rec},
            filtered_trains=(rec,),
            selected_train=rec,
            selected_index=0,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=1,
            last_stream_update=100.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            selected_speed_result=rec.speed_result,
        )
        win = MockCursesWindow(25, 60)
        self.renderer_utf8.render_inspector(win, snapshot)

        content = " ".join(s[2] for s in win.strings)
        self.assertIn("Speed:", content)
        self.assertIn("140.0 km/h", content)
        self.assertIn("Line Limit:", content)
        self.assertIn("160 km/h (PATHE Mainline)", content)
        self.assertNotIn("OVERSPEED", content)

    def test_render_inspector_overspeed_alert(self) -> None:
        pos = _make_train_position(train_id="train-2", train_number="IC52", speed=174.0)
        rec = TrainRecord(position=pos)
        rec.speed_result = SpeedEnforcementResult(
            train_id="IC52",
            current_speed=174.0,
            governing_limit=160.0,
            margin_kmh=14.0,
            severity=OverspeedSeverity.OVERSPEED,
            governing_name="PATHE Mainline",
            is_tsr=False,
            status_label="[OVERSPEED +14]",
            color_pair=9,
        )
        snapshot = StateSnapshot(
            trains={"train-2": rec},
            filtered_trains=(rec,),
            selected_train=rec,
            selected_index=0,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=1,
            last_stream_update=100.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            selected_speed_result=rec.speed_result,
            overspeed_count=1,
            overspeed_train_ids=("train-2",),
        )
        win = MockCursesWindow(25, 60)
        self.renderer_utf8.render_inspector(win, snapshot)

        content = " ".join(s[2] for s in win.strings)
        self.assertIn("174.0 km/h [OVERSPEED +14]", content)
        self.assertIn("Line Limit:", content)
        self.assertIn("160 km/h (PATHE Mainline)", content)

    def test_render_inspector_tsr_active_and_violation(self) -> None:
        # Compliant in TSR
        pos = _make_train_position(
            train_id="train-3",
            train_number="1599",
            lat=38.8680,
            lng=22.4250,
            speed=38.0,
        )
        rec = TrainRecord(position=pos)
        rec.speed_result = SpeedEnforcementResult(
            train_id="1599",
            current_speed=38.0,
            governing_limit=40.0,
            margin_kmh=-2.0,
            severity=OverspeedSeverity.NORMAL,
            governing_name="TSR: Spercheios Viaduct Maintenance",
            is_tsr=True,
            status_label="[TSR: 40 km/h]",
            color_pair=8,
        )
        snapshot = StateSnapshot(
            trains={"train-3": rec},
            filtered_trains=(rec,),
            selected_train=rec,
            selected_index=0,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=1,
            last_stream_update=100.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            selected_speed_result=rec.speed_result,
        )
        win = MockCursesWindow(25, 60)
        self.renderer_utf8.render_inspector(win, snapshot)
        content = " ".join(s[2] for s in win.strings)
        self.assertIn("[TSR: 40 km/h]", content)
        self.assertIn("Line Limit:", content)

        # Violation in TSR
        pos_viol = _make_train_position(
            train_id="train-3",
            train_number="1599",
            lat=38.8680,
            lng=22.4250,
            speed=54.0,
        )
        rec_viol = TrainRecord(position=pos_viol)
        rec_viol.speed_result = SpeedEnforcementResult(
            train_id="1599",
            current_speed=54.0,
            governing_limit=40.0,
            margin_kmh=14.0,
            severity=OverspeedSeverity.TSR_VIOLATION,
            governing_name="TSR: Spercheios Viaduct Maintenance",
            is_tsr=True,
            status_label="[TSR OVERSPEED +14]",
            color_pair=9,
        )
        snap_viol = StateSnapshot(
            trains={"train-3": rec_viol},
            filtered_trains=(rec_viol,),
            selected_train=rec_viol,
            selected_index=0,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=1,
            last_stream_update=100.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            selected_speed_result=rec_viol.speed_result,
            overspeed_count=1,
            overspeed_train_ids=("train-3",),
        )
        win_viol = MockCursesWindow(25, 60)
        self.renderer_utf8.render_inspector(win_viol, snap_viol)
        content_viol = " ".join(s[2] for s in win_viol.strings)
        self.assertIn("[TSR OVERSPEED]", content_viol)

    def test_render_roster_overspeed_indicator(self) -> None:
        pos1 = _make_train_position(train_id="t1", train_number="IC52", speed=174.0)
        pos2 = _make_train_position(train_id="t2", train_number="1597", speed=90.0)
        rec1 = TrainRecord(position=pos1)
        rec2 = TrainRecord(position=pos2)
        snapshot = StateSnapshot(
            trains={"t1": rec1, "t2": rec2},
            filtered_trains=(rec1, rec2),
            selected_train=rec2,
            selected_index=1,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=1,
            last_stream_update=100.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            overspeed_count=1,
            overspeed_train_ids=("t1",),
        )
        win = MockCursesWindow(10, 32)
        self.renderer_utf8.render_roster(win, snapshot)
        content = " ".join(s[2] for s in win.strings)
        self.assertIn("!174", content)
        self.assertIn("  90", content)

    def test_render_status_bar_overspeed_badge(self) -> None:
        rec = TrainRecord(position=_make_train_position(train_id="t1", speed=174.0))
        snapshot = StateSnapshot(
            trains={"t1": rec},
            filtered_trains=(rec,),
            selected_train=rec,
            selected_index=0,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=1,
            last_stream_update=100.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            overspeed_count=2,
            overspeed_train_ids=("t1", "t2"),
        )
        win = MockCursesWindow(1, 80)
        self.renderer_utf8.render_status_bar(win, snapshot, width=80)
        content = " ".join(s[2] for s in win.strings)
        self.assertIn("2 OVERSPEED", content)

        # ASCII mode
        win_ascii = MockCursesWindow(1, 80)
        self.renderer_ascii.render_status_bar(win_ascii, snapshot, width=80)
        content_ascii = " ".join(s[2] for s in win_ascii.strings)
        self.assertIn("! 2 OVERSPEED", content_ascii)


class TestStateStoreSpeedIntegration(unittest.TestCase):
    """Test suite for TrainStateStore automatic speed evaluation on ingestion."""

    def test_state_store_evaluates_overspeed(self) -> None:
        store = TrainStateStore()
        pos = _make_train_position(
            train_id="t1",
            train_number="IC52",
            lat=39.5050,
            lng=22.3680,
            speed=175.0,
            corridor="pathe-mainline",
        )
        store.update_positions([pos])
        snap = store.get_snapshot()

        self.assertEqual(snap.overspeed_count, 1)
        self.assertEqual(snap.overspeed_train_ids, ("t1",))
        rec = snap.trains["t1"]
        self.assertIsNotNone(rec.speed_result)
        assert rec.speed_result is not None
        self.assertEqual(rec.speed_result.severity, OverspeedSeverity.OVERSPEED)
        self.assertEqual(rec.speed_result.governing_limit, 160.0)


if __name__ == "__main__":
    unittest.main()
