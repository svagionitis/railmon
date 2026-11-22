"""Unit tests for F-5.1 Corridor Headway & Opposing Train Conflict Watchdog rendering."""

import time
import unittest
from typing import List, Tuple

from train_tracker import TrainPosition
from tui.geo import (
    ConflictAlert,
    ConflictSeverity,
    ConflictType,
    GeoPoint,
)
from tui.renderer import TUIRenderer
from tui.state import ConnectionStatus, StateSnapshot, TrainRecord, TrainStateStore


class MockCursesWindow:
    """Mock curses window recording strings and attributes."""

    def __init__(self, max_y: int = 24, max_x: int = 100) -> None:
        self.max_y = max_y
        self.max_x = max_x
        self.strings: List[Tuple[int, int, str, int]] = []
        self.boxed = False

    def getmaxyx(self) -> Tuple[int, int]:
        return self.max_y, self.max_x

    def erase(self) -> None:
        self.strings.clear()

    def box(self) -> None:
        self.boxed = True

    def border(self, *args: int) -> None:
        self.boxed = True

    def addstr(self, y: int, x: int, string: str, attr: int = 0) -> None:
        self.strings.append((y, x, string, attr))

    def addnstr(self, y: int, x: int, string: str, n: int, attr: int = 0) -> None:
        self.strings.append((y, x, string[:n], attr))

    def get_all_text(self) -> str:
        return "\n".join(s[2] for s in self.strings)


def _make_pos(
    train_id: str = "T1597",
    train_number: str = "1597",
    speed: float = 92.0,
    course: float = 30.0,
    corridor: str = "proastiakos-chalkida",
) -> TrainPosition:
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120-001",
        name=train_number,
        train_type="Intercity",
        service_type="Passenger",
        status="In Transit",
        origin="Athens",
        destination="Thessaloniki",
        next_station="Avlis",
        speed=speed,
        course=course,
        track_heading=course,
        lat=38.4000,
        lng=23.6000,
        altitude=20.0,
        delay=2.0,
        progress=0.5,
        chainage_m=10000,
        corridor=corridor,
        distance_to_destination_m=20000,
        timestamp="2026-11-22T12:00:00Z",
        schedule_id="sch-1",
    )


class TestConflictWatchdogRenderer(unittest.TestCase):
    """Test suite for roster, inspector, and status bar rendering of conflict alerts."""

    def setUp(self) -> None:
        self.renderer_utf8 = TUIRenderer(ascii_mode=False)
        self.renderer_ascii = TUIRenderer(ascii_mode=True)
        self.mock_win = MockCursesWindow()

    def test_render_roster_conflict_status_codes(self) -> None:
        """Verify roster displays !OP, !HW, ?OP, and MET status mnemonics."""
        pos1 = _make_pos(train_id="T1", train_number="1597")
        pos2 = _make_pos(train_id="T2", train_number="1598")
        pos3 = _make_pos(train_id="T3", train_number="IC52")
        pos4 = _make_pos(train_id="T4", train_number="2236")

        rec1 = TrainRecord(position=pos1)
        rec1.active_conflict = ConflictAlert(
            alert_id="T1_T2_OPPOSING",
            train_id_1="T1",
            train_number_1="1597",
            train_id_2="T2",
            train_number_2="1598",
            corridor_id="proastiakos-chalkida",
            corridor_name="Chalkida Branch",
            conflict_type=ConflictType.OPPOSING_COLLISION,
            severity=ConflictSeverity.CRITICAL,
            distance_km=2.1,
            closing_speed_kmh=180.0,
            time_to_collision_sec=42.0,
            is_single_track=True,
            station_proximity_name=None,
            description="Critical Opposing",
            timestamp=time.time(),
        )

        rec2 = TrainRecord(position=pos2)
        rec2.active_conflict = ConflictAlert(
            alert_id="T2_T1_HEADWAY",
            train_id_1="T2",
            train_number_1="1598",
            train_id_2="T1",
            train_number_2="1597",
            corridor_id="proastiakos-chalkida",
            corridor_name="Chalkida Branch",
            conflict_type=ConflictType.HEADWAY_VIOLATION,
            severity=ConflictSeverity.CRITICAL,
            distance_km=0.8,
            closing_speed_kmh=25.0,
            time_to_collision_sec=115.0,
            is_single_track=True,
            station_proximity_name=None,
            description="Critical Headway",
            timestamp=time.time(),
        )

        rec3 = TrainRecord(position=pos3)
        rec3.active_conflict = ConflictAlert(
            alert_id="T3_T4_WARN",
            train_id_1="T3",
            train_number_1="IC52",
            train_id_2="T4",
            train_number_2="2236",
            corridor_id="pathe-mainline",
            corridor_name="PATHE Mainline",
            conflict_type=ConflictType.HEADWAY_VIOLATION,
            severity=ConflictSeverity.WARNING,
            distance_km=1.8,
            closing_speed_kmh=20.0,
            time_to_collision_sec=None,
            is_single_track=False,
            station_proximity_name=None,
            description="Headway Warning",
            timestamp=time.time(),
        )

        rec4 = TrainRecord(position=pos4)
        rec4.active_conflict = ConflictAlert(
            alert_id="T4_T3_MEET",
            train_id_1="T4",
            train_number_1="2236",
            train_id_2="T3",
            train_number_2="IC52",
            corridor_id="proastiakos-chalkida",
            corridor_name="Chalkida Branch",
            conflict_type=ConflictType.STATION_MEET,
            severity=ConflictSeverity.ADVISORY,
            distance_km=1.2,
            closing_speed_kmh=10.0,
            time_to_collision_sec=None,
            is_single_track=True,
            station_proximity_name="Avlis",
            description="Station Meet",
            timestamp=time.time(),
        )

        st1, _ = self.renderer_utf8._resolve_roster_status_and_pair(rec1, False)
        st2, _ = self.renderer_utf8._resolve_roster_status_and_pair(rec2, False)
        st3, _ = self.renderer_utf8._resolve_roster_status_and_pair(rec3, False)
        st4, _ = self.renderer_utf8._resolve_roster_status_and_pair(rec4, False)

        self.assertEqual(st1, "!OP")
        self.assertEqual(st2, "!HW")
        self.assertEqual(st3, "?HW")
        self.assertEqual(st4, "MET")

    def test_render_inspector_conflict_card(self) -> None:
        """Verify inspector renders conflict rows when active conflict is present."""
        pos = _make_pos(train_id="T1", train_number="1597")
        rec = TrainRecord(position=pos)
        rec.active_conflict = ConflictAlert(
            alert_id="T1_T2_OPPOSING",
            train_id_1="T1",
            train_number_1="1597",
            train_id_2="T2",
            train_number_2="1598",
            corridor_id="proastiakos-chalkida",
            corridor_name="Chalkida Branch",
            conflict_type=ConflictType.OPPOSING_COLLISION,
            severity=ConflictSeverity.CRITICAL,
            distance_km=2.1,
            closing_speed_kmh=180.0,
            time_to_collision_sec=42.0,
            is_single_track=True,
            station_proximity_name=None,
            description="Critical Opposing",
            timestamp=time.time(),
        )

        snap = StateSnapshot(
            trains={"T1": rec},
            filtered_trains=(rec,),
            selected_train=rec,
            selected_index=0,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=1,
            last_stream_update=time.time(),
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            active_conflicts=(rec.active_conflict,),
            critical_conflict_count=1,
            selected_conflict=rec.active_conflict,
        )

        self.renderer_utf8.render_inspector(self.mock_win, snap)
        rendered_text = self.mock_win.get_all_text()

        self.assertIn("Conflict:", rendered_text)
        self.assertIn("OPPOSING_COLLISION", rendered_text)
        self.assertIn("1598", rendered_text)
        self.assertIn("TTC / Rel:", rendered_text)
        self.assertIn("42s", rendered_text)

    def test_render_status_bar_critical_conflict_marquee(self) -> None:
        """Verify status bar displays critical conflict marquee banner."""
        alert = ConflictAlert(
            alert_id="T1_T2_OPPOSING",
            train_id_1="T1",
            train_number_1="1597",
            train_id_2="T2",
            train_number_2="1598",
            corridor_id="proastiakos-chalkida",
            corridor_name="Chalkida Branch",
            conflict_type=ConflictType.OPPOSING_COLLISION,
            severity=ConflictSeverity.CRITICAL,
            distance_km=2.1,
            closing_speed_kmh=180.0,
            time_to_collision_sec=42.0,
            is_single_track=True,
            station_proximity_name=None,
            description="Critical Opposing",
            timestamp=time.time(),
        )

        snap = StateSnapshot(
            trains={},
            filtered_trains=(),
            selected_train=None,
            selected_index=-1,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=1,
            last_stream_update=time.time(),
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            active_conflicts=(alert,),
            critical_conflict_count=1,
        )

        # UTF-8 Mode
        self.mock_win.erase()
        self.renderer_utf8.render_status_bar(self.mock_win, snap, width=140)
        text_utf8 = self.mock_win.get_all_text()
        self.assertIn("CRITICAL CONFLICT", text_utf8)
        self.assertIn("1597", text_utf8)
        self.assertIn("1598", text_utf8)
        self.assertIn("2.1km", text_utf8)
        self.assertIn("TTC 42s", text_utf8)

        # ASCII Mode
        self.mock_win.erase()
        self.renderer_ascii.render_status_bar(self.mock_win, snap, width=140)
        text_ascii = self.mock_win.get_all_text()
        self.assertIn("[! CRITICAL CONFLICT", text_ascii)
        self.assertIn("1597", text_ascii)
        self.assertIn("1598", text_ascii)


if __name__ == "__main__":
    unittest.main()
