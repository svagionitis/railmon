"""Unit tests for P0-13: Duplicate Train Numbers Disambiguation and Multi-Unit Consist Detection."""

import time
import unittest
from typing import List, Optional, Tuple

from train_tracker import TrainPosition
from tui.geo import (
    ConflictSeverity,
    ConflictType,
    ConflictWatchdogConfig,
    GeoPoint,
    evaluate_train_conflicts,
)
from tui.renderer import (
    TUIRenderer,
    extract_unit_suffix,
    format_disambiguated_train_label,
)
from tui.state import (
    RosterSortMode,
    TrainRecord,
    TrainStateStore,
    _delay_sort_key,
    _natural_sort_key,
    _speed_sort_key,
)


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
    train_id: str,
    train_number: str,
    locomotive_number: Optional[str] = None,
    speed: float = 115.0,
    delay: float = 3.0,
    lat: float = 38.0,
    lng: float = 23.7,
    course: float = 15.0,
    corridor: str = "pathe-mainline",
) -> TrainPosition:
    """Construct a test TrainPosition instance."""
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number=locomotive_number,
        name=f"Service {train_number}",
        train_type="Proastiakos",
        service_type="Suburban",
        status="RUN",
        origin="Athens Airport",
        destination="Kiato",
        next_station="SKA",
        speed=speed,
        course=course,
        track_heading=course,
        lat=lat,
        lng=lng,
        altitude=15.0,
        delay=int(delay),
        progress=0.45,
        chainage_m=12500,
        corridor=corridor,
        distance_to_destination_m=35000,
        timestamp=str(time.time()),
        schedule_id="SCH-1",
    )


class TestDuplicateTrainsDisambiguation(unittest.TestCase):
    """Test suite covering unit suffix extraction, label formatting, state store, and conflict suppression."""

    def test_extract_unit_suffix_prioritizes_locomotive_number(self) -> None:
        """Verify extract_unit_suffix extracts last 3 digits of locomotive number."""
        self.assertEqual(extract_unit_suffix("460103", "6afc08"), "103")
        self.assertEqual(extract_unit_suffix("460108", "8b3d09"), "108")
        self.assertEqual(extract_unit_suffix("120005", "test-id"), "005")
        self.assertEqual(extract_unit_suffix("42", "fallback-id"), "42")

    def test_extract_unit_suffix_falls_back_to_train_id(self) -> None:
        """Verify extract_unit_suffix falls back to trailing characters of train ID."""
        self.assertEqual(extract_unit_suffix(None, "6afc0821"), "821")
        self.assertEqual(extract_unit_suffix("", "session-c08"), "c08")
        self.assertEqual(extract_unit_suffix(None, None), "001")

    def test_format_disambiguated_train_label_unique(self) -> None:
        """Verify unique train numbers return base train number unchanged."""
        label_utf8 = format_disambiguated_train_label(
            train_number="IC54",
            locomotive_number="120002",
            train_id="id-1",
            is_duplicate=False,
            ascii_mode=False,
            max_len=8,
        )
        self.assertEqual(label_utf8, "IC54")

        label_ascii = format_disambiguated_train_label(
            train_number="1597",
            locomotive_number="460112",
            train_id="id-2",
            is_duplicate=False,
            ascii_mode=True,
            max_len=8,
        )
        self.assertEqual(label_ascii, "1597")

    def test_format_disambiguated_train_label_duplicate_utf8_and_ascii(self) -> None:
        """Verify duplicate train numbers append unit suffix with middle-dot (UTF-8) or slash (ASCII)."""
        # UTF-8 mode
        label_utf8_1 = format_disambiguated_train_label(
            train_number="2232",
            locomotive_number="460103",
            train_id="id-1",
            is_duplicate=True,
            ascii_mode=False,
            max_len=8,
        )
        self.assertEqual(label_utf8_1, "2232·103")

        label_utf8_2 = format_disambiguated_train_label(
            train_number="2232",
            locomotive_number="460108",
            train_id="id-2",
            is_duplicate=True,
            ascii_mode=False,
            max_len=8,
        )
        self.assertEqual(label_utf8_2, "2232·108")

        # ASCII mode
        label_ascii = format_disambiguated_train_label(
            train_number="2232",
            locomotive_number="460103",
            train_id="id-1",
            is_duplicate=True,
            ascii_mode=True,
            max_len=8,
        )
        self.assertEqual(label_ascii, "2232/103")

    def test_format_disambiguated_train_label_shunting_switchers(self) -> None:
        """Verify shunting switchers broadcasting '0' or 'Unknown' format as SHN or UNK."""
        label_shn = format_disambiguated_train_label(
            train_number="0",
            locomotive_number="A205",
            train_id="sw-1",
            is_duplicate=True,
            ascii_mode=False,
            max_len=8,
        )
        self.assertEqual(label_shn, "SHN·205")

        label_unk = format_disambiguated_train_label(
            train_number="Unknown",
            locomotive_number=None,
            train_id="sess-889",
            is_duplicate=True,
            ascii_mode=True,
            max_len=8,
        )
        self.assertEqual(label_unk, "UNK/889")

    def test_state_store_detects_duplicate_train_numbers_set(self) -> None:
        """Verify TrainStateStore computes duplicate_train_numbers set in StateSnapshot."""
        store = TrainStateStore()
        p1 = _make_pos("t-1", "2232", "460103")
        p2 = _make_pos("t-2", "2232", "460108")
        p3 = _make_pos("t-3", "IC54", "120005")
        p4 = _make_pos("t-4", "0", "A101")
        p5 = _make_pos("t-5", "0", "A102")

        store.update_positions([p1, p2, p3, p4, p5])
        snap = store.get_snapshot()

        self.assertIn("2232", snap.duplicate_train_numbers)
        self.assertIn("0", snap.duplicate_train_numbers)
        self.assertNotIn("IC54", snap.duplicate_train_numbers)

    def test_deterministic_sort_stability_with_duplicate_numbers(self) -> None:
        """Verify _natural_sort_key produces deterministic ordering breaking ties by locomotive and ID."""
        rec1 = TrainRecord(position=_make_pos("t-b", "2232", "460108"))
        rec2 = TrainRecord(position=_make_pos("t-a", "2232", "460103"))

        key1 = _natural_sort_key(rec1)
        key2 = _natural_sort_key(rec2)

        self.assertEqual(key1[0], key2[0])
        self.assertEqual(key1[1], key2[1])
        # Tie broken by locomotive number
        self.assertLess(key2[2], key1[2])

        # Sorting list of records
        trains = [rec1, rec2]
        trains.sort(key=_natural_sort_key)
        self.assertEqual(trains[0].position.locomotive_number, "460103")
        self.assertEqual(trains[1].position.locomotive_number, "460108")

        # Speed sort key tie-breaker
        trains.sort(key=_speed_sort_key)
        self.assertEqual(trains[0].position.locomotive_number, "460103")
        self.assertEqual(trains[1].position.locomotive_number, "460108")

        # Delay sort key tie-breaker
        trains.sort(key=_delay_sort_key)
        self.assertEqual(trains[0].position.locomotive_number, "460103")
        self.assertEqual(trains[1].position.locomotive_number, "460108")

    def test_search_and_filter_by_locomotive_number_and_id(self) -> None:
        """Verify searching filter query matches locomotive number or transponder ID."""
        store = TrainStateStore()
        p1 = _make_pos("session-alpha", "2232", "460103")
        p2 = _make_pos("session-beta", "2232", "460108")
        store.update_positions([p1, p2])

        # Filter by locomotive number "103"
        store.set_filter_query("103")
        snap = store.get_snapshot()
        self.assertEqual(len(snap.filtered_trains), 1)
        self.assertEqual(snap.filtered_trains[0].position.locomotive_number, "460103")

        # Filter by transponder ID "beta"
        store.set_filter_query("beta")
        snap = store.get_snapshot()
        self.assertEqual(len(snap.filtered_trains), 1)
        self.assertEqual(snap.filtered_trains[0].position.id, "session-beta")

    def test_inter_consist_coupled_headway_conflict_suppression(self) -> None:
        """Verify coupled units of same train traveling co-directionally are demoted to ADVISORY."""
        # Two units 50 meters apart on PATHE mainline, same heading, same speed
        p1 = _make_pos("t-1", "2232", "460103", speed=115.0, lat=38.0000, lng=23.7000, course=10.0)
        p2 = _make_pos("t-2", "2232", "460108", speed=115.0, lat=38.0004, lng=23.7001, course=10.0)

        cfg = ConflictWatchdogConfig(
            headway_critical_distance_km=1.0,
            coupled_consist_max_distance_km=0.45,
        )

        alerts = evaluate_train_conflicts(
            active_trains=[p1, p2],
            config=cfg,
            now=time.time(),
        )

        self.assertEqual(len(alerts), 1)
        alert = alerts[0]
        # Must be ADVISORY severity, NOT CRITICAL HEADWAY BREACH
        self.assertEqual(alert.severity, ConflictSeverity.ADVISORY)
        self.assertIn("Coupled multiple-unit consist", alert.description)
        self.assertIn("2232", alert.description)

    def test_roster_rendering_disambiguation_and_layout_width(self) -> None:
        """Verify render_roster displays disambiguated numbers and respects 30-cell inner width."""
        store = TrainStateStore()
        p1 = _make_pos("t-1", "2232", "460103", speed=117.0, delay=3.0)
        p2 = _make_pos("t-2", "2232", "460108", speed=116.0, delay=3.0)
        p3 = _make_pos("t-3", "IC54", "120002", speed=95.0, delay=0.0)
        store.update_positions([p1, p2, p3])

        renderer_utf8 = TUIRenderer(ascii_mode=False)
        renderer_ascii = TUIRenderer(ascii_mode=True)

        snap = store.get_snapshot()

        # Terminal sidebar width 32 -> inner width 30
        win_utf8 = MockCursesWindow(max_y=10, max_x=32)
        renderer_utf8.render_roster(win_utf8, snap)

        all_text_utf8 = win_utf8.get_all_text()
        self.assertIn("2232·103", all_text_utf8)
        self.assertIn("2232·108", all_text_utf8)
        self.assertIn("IC54", all_text_utf8)

        # Check that no rendered text line exceeds max_w (30 cells)
        for _, _, text, _ in win_utf8.strings:
            self.assertLessEqual(len(text), 30, f"Roster string exceeded 30 cells: '{text}'")

        # ASCII mode
        win_ascii = MockCursesWindow(max_y=10, max_x=32)
        renderer_ascii.render_roster(win_ascii, snap)
        all_text_ascii = win_ascii.get_all_text()
        self.assertIn("2232/103", all_text_ascii)
        self.assertIn("2232/108", all_text_ascii)

    def test_inspector_pane_renders_consist_row_for_duplicates(self) -> None:
        """Verify inspector renders 'Consist:' row when a duplicate train is selected."""
        store = TrainStateStore()
        p1 = _make_pos("t-1", "2232", "460103")
        p2 = _make_pos("t-2", "2232", "460108")
        store.update_positions([p1, p2])

        store.select_by_id("t-1")
        snap = store.get_snapshot()

        renderer = TUIRenderer()
        win = MockCursesWindow(max_y=24, max_x=40)
        renderer.render_inspector(win, snap)

        text = win.get_all_text()
        self.assertIn("Consist:", text)
        self.assertIn("Coupled Unit (460103)", text)

    def test_map_view_renders_disambiguated_callout_for_selected_duplicate(self) -> None:
        """Verify map view callout label uses disambiguated badge for selected duplicate train."""
        store = TrainStateStore()
        p1 = _make_pos("t-1", "2232", "460103", speed=117.0, delay=3.0)
        p2 = _make_pos("t-2", "2232", "460108", speed=116.0, delay=3.0)
        store.update_positions([p1, p2])

        store.select_by_id("t-1")
        snap = store.get_snapshot()

        renderer = TUIRenderer(tracks=[])
        from tui.geo import GeoProjector, Viewport
        projector = GeoProjector(
            Viewport(center=GeoPoint(lat=38.0, lng=23.7), zoom=5.0, width_cells=80, height_cells=24)
        )
        win = MockCursesWindow(max_y=26, max_x=82)
        renderer.render_map(win, snap, projector)

        text = win.get_all_text()
        self.assertIn("[2232·103]", text)


if __name__ == "__main__":
    unittest.main()
