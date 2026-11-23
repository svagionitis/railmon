"""Unit tests for F-5.2 Sensor Health rendering across roster table, inspector pane, and status bar."""

import time
import unittest
from typing import List, Tuple

from train_tracker import TrainPosition
from tui.geo import (
    GeoPoint,
    SensorHealthAssessment,
    SensorHealthState,
)
from tui.mock_engine import StationNode
from tui.renderer import TUIRenderer
from tui.state import ConnectionStatus, StateSnapshot, TrainRecord, TrainStateStore


class MockCursesWindow:
    """Mock curses window recording strings and attributes."""

    def __init__(self, max_y: int = 24, max_x: int = 80) -> None:
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

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        self.strings.append((y, x, text, attr))

    def addch(self, *args: int) -> None:
        pass


def _make_pos(
    train_id: str = "train-1",
    train_number: str = "IC 52",
    speed: float = 120.0,
    lat: float = 39.5,
    lng: float = 22.3,
    delay: float = 0.0,
    corridor: str = "PATHE",
) -> TrainPosition:
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120-001",
        name=train_number,
        train_type="InterCity",
        service_type="Passenger",
        status="Κανονική" if speed > 5.0 else "Στάση",
        origin="Athens",
        destination="Thessaloniki",
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


class TestSensorHealthRenderer(unittest.TestCase):
    """Test suite for TUI rendering of Sensor Health mnemonics, inspector rows, and alert badges."""

    def setUp(self) -> None:
        self.renderer = TUIRenderer(ascii_mode=False)
        self.ascii_renderer = TUIRenderer(ascii_mode=True)

    def test_render_roster_renders_sensor_health_status_codes(self) -> None:
        """Verify Active Trains Roster renders RUN, DWL, HLT, FRZ, DRP in ST column."""
        store = TrainStateStore()
        stn_larissa = StationNode(
            id="LAR",
            code=1100,
            name_el="Λάρισα",
            name_en="Larissa",
            point=GeoPoint(lat=39.6385, lng=22.4241),
            corridor="PATHE",
            is_hub=True,
        )
        store.set_stations([stn_larissa])

        now = time.time()
        pos_run = _make_pos("t1", "1597", speed=120.0, lat=38.5, lng=23.0)
        pos_dwl = _make_pos("t2", "1599", speed=0.0, lat=39.6385, lng=22.4241)
        pos_frz = _make_pos("t3", "52", speed=140.0, lat=38.7, lng=22.4)
        pos_drp = _make_pos("t4", "50", speed=110.0, lat=39.1, lng=22.3)

        store.update_positions([pos_run, pos_dwl, pos_frz, pos_drp])

        # Manually configure timings for FRZ and DRP
        with store._lock:
            # t3 is frozen: speed > 1, static for 65s
            store._trains["t3"].last_moved_time = now - 65.0
            store._trains["t3"].last_updated = now - 1.0

            # t4 is dropped: last updated 190s ago (exceeds 180s threshold)
            store._trains["t4"].last_updated = now - 190.0

        snapshot = store.get_snapshot()
        win = MockCursesWindow(20, 50)
        self.renderer.render_roster(win, snapshot)

        rendered = " ".join(s[2] for s in win.strings)
        self.assertIn("RUN", rendered)
        self.assertIn("DWL", rendered)
        self.assertIn("FRZ", rendered)
        self.assertIn("DRP", rendered)

    def test_render_inspector_displays_sensor_health_row_and_tags(self) -> None:
        """Verify Telemetry Inspector pane renders Sensor Health row and frozen/dropped tags."""
        store = TrainStateStore()
        now = time.time()
        pos = _make_pos("t-frz", "1599", speed=142.0, lat=38.7412, lng=22.4810)
        store.update_positions([pos])

        with store._lock:
            store._trains["t-frz"].last_moved_time = now - 65.0
            store._trains["t-frz"].last_updated = now - 2.0
            store._selected_id = "t-frz"

        snapshot = store.get_snapshot()

        win = MockCursesWindow(22, 45)
        self.renderer.render_inspector(win, snapshot)
        rendered = " ".join(s[2] for s in win.strings)

        self.assertIn("Telemetry:", rendered)
        self.assertIn("[FROZEN]", rendered)
        self.assertIn("Sensor:", rendered)
        self.assertIn("FROZEN", rendered)
        self.assertIn("Status:", rendered)
        self.assertIn("SENSOR FREEZE", rendered)

    def test_status_bar_displays_sensor_anomaly_badges(self) -> None:
        """Verify bottom status bar displays warning badges when frozen or dropped trains exist."""
        store = TrainStateStore()
        now = time.time()
        pos_frz = _make_pos("t-frz", "1599", speed=140.0)
        pos_drp1 = _make_pos("t-drp1", "50", speed=120.0)
        pos_drp2 = _make_pos("t-drp2", "52", speed=110.0)
        store.update_positions([pos_frz, pos_drp1, pos_drp2])
        store.set_connection_status(ConnectionStatus.CONNECTED)

        with store._lock:
            store._trains["t-frz"].last_moved_time = now - 65.0
            store._trains["t-frz"].last_updated = now - 1.0

            store._trains["t-drp1"].last_updated = now - 190.0
            store._trains["t-drp2"].last_updated = now - 190.0

        snapshot = store.get_snapshot()
        win = MockCursesWindow(1, 100)
        self.renderer.render_status_bar(win, snapshot, width=100, msg_rate=4.0)

        rendered = " ".join(s[2] for s in win.strings)
        self.assertIn("1 FROZEN", rendered)
        self.assertIn("2 DROPPED", rendered)
        self.assertIn("LIVE", rendered)

    def test_status_bar_ascii_mode_anomaly_badge(self) -> None:
        """Verify status bar anomaly badge renders properly in ASCII fallback mode."""
        store = TrainStateStore()
        now = time.time()
        pos_frz = _make_pos("t-frz", "1599", speed=140.0, lat=39.5)
        pos_drp = _make_pos("t-drp", "50", speed=120.0, lat=40.5)
        store.update_positions([pos_frz, pos_drp])
        store.set_connection_status(ConnectionStatus.CONNECTED)

        with store._lock:
            store._trains["t-frz"].last_moved_time = now - 65.0
            store._trains["t-frz"].last_updated = now - 1.0

            store._trains["t-drp"].last_updated = now - 190.0

        snapshot = store.get_snapshot()
        win = MockCursesWindow(1, 100)
        self.ascii_renderer.render_status_bar(win, snapshot, width=100, msg_rate=4.0)

        rendered = " ".join(s[2] for s in win.strings)
        self.assertIn("! 1 FROZEN, 1 DROPPED", rendered)


if __name__ == "__main__":
    unittest.main()
