"""Automated tests for in-TUI train watch alerting integration (F-6.3)."""

from __future__ import annotations

import curses
from unittest import TestCase
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition
from tui.app import TUIApp
from tui.renderer import TUIRenderer
from tui.state import StateSnapshot, TrainRecord, TrainStateStore


def make_dummy_pos(
    train_id: str = "t-52",
    train_number: str = "IC 52",
    speed: float = 70.0,
    delay: float = 2.0,
    lat: float = 38.0,
    lng: float = 23.7,
    origin: str = "Athens",
    destination: str = "Thessaloniki",
) -> TrainPosition:
    """Helper creating a valid TrainPosition."""
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120-001",
        name=f"Train {train_number}",
        train_type="IC",
        service_type="Intercity",
        status="Running",
        origin=origin,
        destination=destination,
        next_station="Larissa",
        speed=speed,
        course=0.0,
        track_heading=0.0,
        lat=lat,
        lng=lng,
        altitude=100.0,
        delay=int(delay),
        progress=0.5,
        chainage_m=100000,
        corridor="PATHE",
        distance_to_destination_m=200000,
        timestamp="2026-11-22T14:00:00Z",
        schedule_id="sch-1",
    )


class TestTUIWatchState(TestCase):
    """Test TrainStateStore and StateSnapshot watch tracking."""

    def test_state_store_toggle_watch(self) -> None:
        store = TrainStateStore()
        self.assertFalse(store.is_train_watched("t-52"))

        # Toggle on
        is_watched = store.toggle_watch_train("t-52")
        self.assertTrue(is_watched)
        self.assertTrue(store.is_train_watched("t-52"))
        self.assertIn("t-52", store.get_watched_train_ids())

        # Snapshot reflects watched train
        snap = store.get_snapshot()
        self.assertIn("t-52", snap.watched_train_ids)

        # Toggle off
        is_watched_again = store.toggle_watch_train("t-52")
        self.assertFalse(is_watched_again)
        self.assertFalse(store.is_train_watched("t-52"))

        snap2 = store.get_snapshot()
        self.assertNotIn("t-52", snap2.watched_train_ids)


class TestTUIAppWatchIntegration(TestCase):
    """Test TUIApp interactive watch alert toggling and evaluation."""

    def setUp(self) -> None:
        self.store = TrainStateStore()
        self.mock_stdscr = MagicMock()
        self.mock_stdscr.getmaxyx.return_value = (40, 120)
        self.mock_worker = MagicMock()
        self.app = TUIApp(
            stdscr=self.mock_stdscr,
            store=self.store,
            worker=self.mock_worker,
            stations=[],
            tracks=[],
            infrastructure=[],
        )

    def test_toggle_watch_no_selection(self) -> None:
        res = self.app.toggle_watch_selected_train()
        self.assertIsNone(res)
        msg, is_err = self.app.get_active_toast()
        self.assertTrue(is_err)
        self.assertIn("Select a train first", str(msg))

    def test_toggle_watch_with_selected_train(self) -> None:
        pos = make_dummy_pos("t-52", "IC 52")
        self.store.update_positions([pos])
        self.store.select_next()

        # Toggle on
        res = self.app.toggle_watch_selected_train()
        self.assertTrue(res)
        msg, is_err = self.app.get_active_toast()
        self.assertFalse(is_err)
        self.assertIn("Alert Watch ON: IC 52", str(msg))

        # Toggle off
        res2 = self.app.toggle_watch_selected_train()
        self.assertFalse(res2)
        msg2, is_err2 = self.app.get_active_toast()
        self.assertFalse(is_err2)
        self.assertIn("Watch Alert OFF: IC 52", str(msg2))

    @patch("watchdog.dispatch_desktop_notification")
    @patch("watchdog.dispatch_terminal_bell")
    def test_in_tui_watch_alert_evaluation(
        self,
        mock_bell: MagicMock,
        mock_desktop: MagicMock,
    ) -> None:
        pos1 = make_dummy_pos("t-52", "IC 52", speed=0.0, delay=0.0)
        self.store.update_positions([pos1])
        self.store.toggle_watch_train("t-52")

        snap1 = self.store.get_snapshot()
        self.app._evaluate_in_tui_watch_alerts(snap1)

        # Update position with departure (speed 65 km/h) and delay jump to +7m
        pos2 = make_dummy_pos("t-52", "IC 52", speed=65.0, delay=7.0, lat=38.2, lng=23.7)
        self.store.update_positions([pos2])
        snap2 = self.store.get_snapshot()

        self.app._evaluate_in_tui_watch_alerts(snap2)

        toast, is_err = self.app.get_active_toast()
        self.assertIsNotNone(toast)
        self.assertTrue(is_err)
        self.assertIn("IC 52", str(toast))
        mock_bell.assert_called()


class TestTUIRendererWatchIndicator(TestCase):
    """Test TUIRenderer rendering watch indicator in active train roster."""

    def test_render_roster_watch_marker(self) -> None:
        renderer = TUIRenderer(ascii_mode=False)
        pos = make_dummy_pos("t-52", "IC 52")
        record = TrainRecord(position=pos)

        mock_win = MagicMock()
        mock_win.getmaxyx.return_value = (20, 40)

        snapshot = StateSnapshot(
            trains={"t-52": record},
            filtered_trains=(record,),
            selected_train=record,
            selected_index=0,
            connection_status=MagicMock(),
            status_message="OK",
            total_messages_received=10,
            last_stream_update=1000.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            watched_train_ids=frozenset({"T-52"}),
        )

        with patch("tui.renderer.safe_addstr") as mock_addstr:
            renderer.render_roster(mock_win, snapshot)
            # Find the line rendering the train row
            calls = [call[0] for call in mock_addstr.call_args_list]
            row_texts = [c[3] for c in calls if len(c) > 3 and "IC 52" in str(c[3])]
            self.assertTrue(len(row_texts) > 0)
            # Check for watch badge '●'
            self.assertIn("●", row_texts[0])

    def test_render_roster_ascii_watch_marker(self) -> None:
        renderer = TUIRenderer(ascii_mode=True)
        pos = make_dummy_pos("t-52", "IC 52")
        record = TrainRecord(position=pos)

        mock_win = MagicMock()
        mock_win.getmaxyx.return_value = (20, 40)

        snapshot = StateSnapshot(
            trains={"t-52": record},
            filtered_trains=(record,),
            selected_train=record,
            selected_index=0,
            connection_status=MagicMock(),
            status_message="OK",
            total_messages_received=10,
            last_stream_update=1000.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            watched_train_ids=frozenset({"T-52"}),
        )

        with patch("tui.renderer.safe_addstr") as mock_addstr:
            renderer.render_roster(mock_win, snapshot)
            calls = [call[0] for call in mock_addstr.call_args_list]
            row_texts = [c[3] for c in calls if len(c) > 3 and "IC 52" in str(c[3])]
            self.assertTrue(len(row_texts) > 0)
            # Check for ascii watch badge '*'
            self.assertIn("*", row_texts[0])
