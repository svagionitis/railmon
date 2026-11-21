"""Unit tests for railway infrastructure GIS TUI rendering and mode controls (F-7.2)."""

import curses
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import TrainPosition
from tui.app import TUIApp
from tui.geo import (
    GREECE_PRESET,
    GeoPoint,
    GeoProjector,
    InfrastructureFeature,
    InfrastructureType,
    PortalNode,
    SignalAspect,
    Viewport,
)
from tui.mock_engine import load_infrastructure, load_stations, load_tracks
from tui.renderer import TUIRenderer
from tui.state import (
    ConnectionStatus,
    InfrastructureLayerMode,
    InspectorMode,
    StateSnapshot,
    TrainRecord,
    TrainStateStore,
)


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


class TestInfrastructureRenderer(unittest.TestCase):
    """Test suite for TUIRenderer infrastructure GIS layer overlays."""

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
        self.projector = GeoProjector(
            Viewport(center=GREECE_PRESET.center, zoom=1.5, width_cells=80, height_cells=24)
        )

    def _make_snapshot(self, infra_mode: InfrastructureLayerMode, selected_train=None) -> StateSnapshot:
        return StateSnapshot(
            trains={},
            filtered_trains=(),
            selected_train=selected_train,
            selected_index=0,
            connection_status=ConnectionStatus.CONNECTED,
            status_message="OK",
            total_messages_received=100,
            last_stream_update=1000.0,
            follow_mode=False,
            filter_query="",
            corridor_filter=None,
            version=1,
            infrastructure_mode=infra_mode,
        )

    def test_renderer_initialization_with_infrastructure(self) -> None:
        self.assertGreater(len(self.renderer_utf8.infrastructure), 10)
        self.assertGreater(len(self.renderer_utf8.tunnels), 3)

    def test_render_map_gis_header_title(self) -> None:
        win = MockCursesWindow(24, 80)

        # Mode ALL
        snap_all = self._make_snapshot(InfrastructureLayerMode.ALL)
        self.renderer_utf8.render_map(win, snap_all, self.projector)
        title_all = [s for _, _, s, _ in win.strings if "Map Viewport:" in s][0]
        self.assertIn("GIS: ALL", title_all)

        # Mode TUNNELS_BRIDGES
        win.strings.clear()
        snap_tun = self._make_snapshot(InfrastructureLayerMode.TUNNELS_BRIDGES)
        self.renderer_utf8.render_map(win, snap_tun, self.projector)
        title_tun = [s for _, _, s, _ in win.strings if "Map Viewport:" in s][0]
        self.assertIn("GIS: TUNNELS", title_tun)

        # Mode OFF
        win.strings.clear()
        snap_off = self._make_snapshot(InfrastructureLayerMode.OFF)
        self.renderer_utf8.render_map(win, snap_off, self.projector)
        title_off = [s for _, _, s, _ in win.strings if "Map Viewport:" in s][0]
        self.assertNotIn("GIS:", title_off)

    def test_render_map_infrastructure_glyphs_utf8(self) -> None:
        win = MockCursesWindow(30, 100)
        snap = self._make_snapshot(InfrastructureLayerMode.ALL)
        self.renderer_utf8.render_map(win, snap, self.projector)

        rendered_chars = [s for _, _, s, _ in win.strings]
        all_text = "".join(rendered_chars)

        # In UTF-8 mode, portals '∩', bridges '═', signals '◆' should be blitted
        self.assertTrue(any("∩" in s for s in rendered_chars) or "∩" in all_text)
        self.assertTrue(any("═" in s for s in rendered_chars) or "═" in all_text)
        self.assertTrue(any("◆" in s for s in rendered_chars) or "◆" in all_text)

    def test_render_map_infrastructure_glyphs_ascii_fallback(self) -> None:
        win = MockCursesWindow(30, 100)
        snap = self._make_snapshot(InfrastructureLayerMode.ALL)
        self.renderer_ascii.render_map(win, snap, self.projector)

        rendered_chars = [s for _, _, s, _ in win.strings]
        all_text = "".join(rendered_chars)

        # In ASCII mode, fallback glyphs 'n', '=', 'S'
        self.assertFalse(any("∩" in s for s in rendered_chars))
        self.assertFalse(any("═" in s for s in rendered_chars))

    def test_render_map_suppresses_signals_in_tunnels_bridges_mode(self) -> None:
        win = MockCursesWindow(30, 100)
        snap = self._make_snapshot(InfrastructureLayerMode.TUNNELS_BRIDGES)
        self.renderer_utf8.render_map(win, snap, self.projector)

        rendered_chars = [s for _, _, s, _ in win.strings]
        # Signals '◆' must be suppressed in TUNNELS_BRIDGES mode
        self.assertFalse(any("◆" in s for s in rendered_chars))

    def _make_train_position(
        self,
        train_id: str = "T1",
        train_number: str = "1597",
        lat: float = 38.7620,
        lng: float = 22.5320,
        speed: float = 138.0,
        status: str = "In Service",
    ) -> TrainPosition:
        return TrainPosition(
            id=train_id,
            train_number=train_number,
            locomotive_number="120010",
            name=train_number,
            train_type="Intercity",
            service_type="Passenger",
            status=status,
            origin="Athens",
            destination="Thessaloniki",
            next_station="Lianokladi",
            speed=speed,
            delay=4.0,
            course=0.0,
            track_heading=0.0,
            lat=lat,
            lng=lng,
            altitude=100.0,
            progress=0.5,
            chainage_m=200000.0,
            corridor="pathe-mainline",
            distance_to_destination_m=100000.0,
            timestamp=1000.0,
            schedule_id="SCHED1",
        )

    def test_render_inspector_displays_tunnel_badge_when_in_tunnel(self) -> None:
        # Create a train positioned inside Kallidromo Tunnel
        pos_in_tunnel = self._make_train_position(
            train_id="test-train-1",
            train_number="1597",
            lat=38.7620,
            lng=22.5320,
            speed=138.0,
            status="In Service",
        )
        record = TrainRecord(position=pos_in_tunnel, trail=(), first_seen=100.0, last_updated=100.0)
        snap = self._make_snapshot(InfrastructureLayerMode.ALL, selected_train=record)

        win = MockCursesWindow(20, 45)
        self.renderer_utf8.render_inspector(win, snap)

        rendered_texts = [s for _, _, s, _ in win.strings]
        all_inspector_text = " ".join(rendered_texts)
        self.assertIn("TUNNEL", all_inspector_text)
        self.assertIn("Kallidromo", all_inspector_text)

    def test_render_inspector_standard_status_when_outside_tunnel(self) -> None:
        # Train positioned outside any tunnel (e.g. Athens station area)
        pos_outside = self._make_train_position(
            train_id="test-train-2",
            train_number="1599",
            lat=37.9926,
            lng=23.7203,
            speed=0.0,
            status="Boarding",
        )
        record = TrainRecord(position=pos_outside, trail=(), first_seen=100.0, last_updated=100.0)
        snap = self._make_snapshot(InfrastructureLayerMode.ALL, selected_train=record)

        win = MockCursesWindow(20, 45)
        self.renderer_utf8.render_inspector(win, snap)

        rendered_texts = [s for _, _, s, _ in win.strings]
        all_inspector_text = " ".join(rendered_texts)
        self.assertNotIn("TUNNEL", all_inspector_text)
        self.assertIn("Boarding", all_inspector_text)


class TestStateStoreAndAppControls(unittest.TestCase):
    """Test suite for TrainStateStore mode cycling and TUIApp key handling."""

    def test_state_store_cycle_infrastructure_mode(self) -> None:
        store = TrainStateStore()
        snap1 = store.get_snapshot()
        self.assertEqual(snap1.infrastructure_mode, InfrastructureLayerMode.ALL)

        # ALL -> TUNNELS_BRIDGES
        m2 = store.cycle_infrastructure_mode()
        self.assertEqual(m2, InfrastructureLayerMode.TUNNELS_BRIDGES)
        self.assertEqual(store.get_snapshot().infrastructure_mode, InfrastructureLayerMode.TUNNELS_BRIDGES)

        # TUNNELS_BRIDGES -> OFF
        m3 = store.cycle_infrastructure_mode()
        self.assertEqual(m3, InfrastructureLayerMode.OFF)
        self.assertEqual(store.get_snapshot().infrastructure_mode, InfrastructureLayerMode.OFF)

        # OFF -> ALL
        m4 = store.cycle_infrastructure_mode()
        self.assertEqual(m4, InfrastructureLayerMode.ALL)
        self.assertEqual(store.get_snapshot().infrastructure_mode, InfrastructureLayerMode.ALL)

    def test_app_g_key_handler(self) -> None:
        store = TrainStateStore()
        mock_worker = MagicMock()
        mock_stdscr = MagicMock()
        mock_stdscr.getmaxyx.return_value = (30, 100)

        app = TUIApp(stdscr=mock_stdscr, store=store, worker=mock_worker)

        # Initial mode is ALL
        self.assertEqual(store.get_snapshot().infrastructure_mode, InfrastructureLayerMode.ALL)

        # Press 'g' -> TUNNELS_BRIDGES
        app._handle_normal_input(ord("g"))
        self.assertEqual(store.get_snapshot().infrastructure_mode, InfrastructureLayerMode.TUNNELS_BRIDGES)
        self.assertIsNotNone(app.toast_message)
        self.assertIn("Tunnels & Bridges", app.toast_message or "")

        # Press 'G' -> OFF
        app._handle_normal_input(ord("G"))
        self.assertEqual(store.get_snapshot().infrastructure_mode, InfrastructureLayerMode.OFF)
        self.assertIn("Off", app.toast_message or "")

        # Press 'g' -> ALL
        app._handle_normal_input(ord("g"))
        self.assertEqual(store.get_snapshot().infrastructure_mode, InfrastructureLayerMode.ALL)
        self.assertIn("All", app.toast_message or "")


if __name__ == "__main__":
    unittest.main()
