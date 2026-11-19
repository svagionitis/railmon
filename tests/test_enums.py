"""Unit tests for strongly typed Enums and backward-compatible parameter coercion."""

import io
import unittest
from unittest.mock import MagicMock, patch

from train_tracker import (
    DeadbandFilterMode,
    GlyphMode,
    OutputVerbosity,
    RecordingMode,
    TelemetrySource,
    run_plain_stream_mode,
    run_record_mode,
    run_tui_mode,
)
from tui.app import CycleDirection, InputMode, TUIApp
from tui.geo import GeoPoint, GeoProjector, Viewport
from tui.renderer import HelpOverlay, PaneGeometry, PlaybackState, TUIRenderer
from tui.state import ConnectionStatus, StateSnapshot, TrainStateStore


class TestEnumDefinitions(unittest.TestCase):
    """Test suite verifying Enum members and values."""

    def test_telemetry_source_members(self):
        self.assertEqual(TelemetrySource.LIVE.value, "live")
        self.assertEqual(TelemetrySource.DEMO.value, "demo")

    def test_recording_mode_members(self):
        self.assertEqual(RecordingMode.DISABLED.value, "disabled")
        self.assertEqual(RecordingMode.ENABLED.value, "enabled")

    def test_deadband_filter_mode_members(self):
        self.assertEqual(DeadbandFilterMode.ENABLED.value, "enabled")
        self.assertEqual(DeadbandFilterMode.DISABLED.value, "disabled")

    def test_output_verbosity_members(self):
        self.assertEqual(OutputVerbosity.NORMAL.value, "normal")
        self.assertEqual(OutputVerbosity.QUIET.value, "quiet")

    def test_glyph_mode_members(self):
        self.assertEqual(GlyphMode.AUTO.value, "auto")
        self.assertEqual(GlyphMode.UTF8.value, "utf8")
        self.assertEqual(GlyphMode.ASCII.value, "ascii")

    def test_cycle_direction_members(self):
        self.assertIsNotNone(CycleDirection.FORWARD)
        self.assertIsNotNone(CycleDirection.BACKWARD)
        self.assertNotEqual(CycleDirection.FORWARD, CycleDirection.BACKWARD)

    def test_help_overlay_members(self):
        self.assertIsNotNone(HelpOverlay.HIDDEN)
        self.assertIsNotNone(HelpOverlay.VISIBLE)
        self.assertNotEqual(HelpOverlay.HIDDEN, HelpOverlay.VISIBLE)

    def test_playback_state_members(self):
        self.assertIsNotNone(PlaybackState.RUNNING)
        self.assertIsNotNone(PlaybackState.PAUSED)
        self.assertNotEqual(PlaybackState.RUNNING, PlaybackState.PAUSED)

    def test_input_mode_members(self):
        self.assertIsNotNone(InputMode.NORMAL)
        self.assertIsNotNone(InputMode.SEARCH)
        self.assertIsNotNone(InputMode.HELP)


class TestCycleSelectionCoercion(unittest.TestCase):
    """Test suite verifying TUIApp.cycle_selection Enum usage and boolean fallback."""

    def setUp(self):
        self.store = TrainStateStore()
        self.store.select_next = MagicMock()
        self.store.select_prev = MagicMock()

        self.app = TUIApp.__new__(TUIApp)
        self.app.store = self.store

    def test_cycle_selection_typed_forward(self):
        self.app.cycle_selection(CycleDirection.FORWARD)
        self.store.select_next.assert_called_once()
        self.store.select_prev.assert_not_called()

    def test_cycle_selection_typed_backward(self):
        self.app.cycle_selection(CycleDirection.BACKWARD)
        self.store.select_prev.assert_called_once()
        self.store.select_next.assert_not_called()

    def test_cycle_selection_legacy_forward_kwarg(self):
        self.app.cycle_selection(forward=True)
        self.store.select_next.assert_called_once()
        self.store.select_prev.assert_not_called()

    def test_cycle_selection_legacy_backward_kwarg(self):
        self.app.cycle_selection(forward=False)
        self.store.select_prev.assert_called_once()
        self.store.select_next.assert_not_called()

    def test_cycle_selection_boolean_positional(self):
        self.app.cycle_selection(True)
        self.store.select_next.assert_called_once()
        self.app.cycle_selection(False)
        self.store.select_prev.assert_called_once()


class TestRendererEnumCoercion(unittest.TestCase):
    """Test suite verifying TUIRenderer Enum arguments and legacy boolean compatibility."""

    def setUp(self):
        self.renderer = TUIRenderer()
        self.win = MagicMock()
        self.win.getmaxyx.return_value = (24, 80)
        self.snapshot = TrainStateStore().get_snapshot()
        self.geom = PaneGeometry(
            header_y=0, header_x=0, header_h=3, header_w=80,
            map_y=3, map_x=0, map_h=15, map_w=50,
            roster_y=3, roster_x=50, roster_h=15, roster_w=30,
            inspector_y=18, inspector_x=0, inspector_h=5, inspector_w=80,
            status_y=23, status_x=0, status_h=1, status_w=80,
            term_height=24, term_width=80, is_valid=True,
        )
        self.projector = GeoProjector(
            Viewport(center=GeoPoint(lat=38.0, lng=23.7), width_cells=50, height_cells=15)
        )

    def _make_mock_windows(self):
        def _win():
            w = MagicMock()
            w.getmaxyx.return_value = (24, 80)
            return w

        return {
            "root": _win(),
            "header": _win(),
            "map": _win(),
            "roster": _win(),
            "inspector": _win(),
            "status": _win(),
        }

    def test_render_status_bar_typed_enums(self):
        # Should execute cleanly without error
        self.renderer.render_status_bar(
            self.win,
            self.snapshot,
            width=80,
            msg_rate=1.5,
            input_mode=InputMode.SEARCH,
            search_buffer="IC52",
            playback_state=PlaybackState.PAUSED,
        )
        self.assertTrue(self.win.erase.called)

    def test_render_status_bar_legacy_booleans(self):
        self.renderer.render_status_bar(
            self.win,
            self.snapshot,
            width=80,
            msg_rate=1.5,
            search_mode=True,
            search_buffer="IC52",
            paused=True,
        )
        self.assertTrue(self.win.erase.called)

    def test_render_all_typed_enums(self):
        windows = self._make_mock_windows()
        self.renderer.render_all(
            windows=windows,
            geometry=self.geom,
            snapshot=self.snapshot,
            projector=self.projector,
            msg_rate=2.0,
            input_mode=InputMode.NORMAL,
            help_overlay=HelpOverlay.HIDDEN,
            playback_state=PlaybackState.RUNNING,
        )
        self.assertTrue(windows["header"].erase.called or windows["header"].box.called)

    def test_render_all_legacy_booleans(self):
        windows = self._make_mock_windows()
        self.renderer.render_all(
            windows=windows,
            geometry=self.geom,
            snapshot=self.snapshot,
            projector=self.projector,
            msg_rate=2.0,
            search_mode=False,
            show_help=False,
            paused=False,
        )
        self.assertTrue(windows["header"].erase.called or windows["header"].box.called)


class TestCLIFunctionCoercion(unittest.TestCase):
    """Test suite verifying CLI runner function parameter normalization."""

    @patch("train_tracker.run_plain_stream_mode")
    @patch("train_tracker.is_tty_supported", return_value=False)
    def test_run_tui_mode_legacy_bool_normalization(self, mock_tty, mock_plain):
        with patch("sys.stderr", new_callable=io.StringIO):
            run_tui_mode(demo=True, record=True, enable_deadband=False, ascii_mode=True)
        mock_plain.assert_called_once_with(
            source=TelemetrySource.DEMO,
            reconnect_delay=3.0,
            recording=RecordingMode.ENABLED,
            db_path="railway_history.db",
            deadband=DeadbandFilterMode.DISABLED,
            retention_days=None,
            archive_path=None,
        )

    @patch("storage.recorder.TelemetryRecorder")
    @patch("storage.db.Database")
    @patch("train_tracker.RailwayGovClient")
    def test_run_plain_stream_mode_legacy_bool_normalization(self, mock_client, mock_db, mock_rec):
        mock_stream = MagicMock()
        mock_stream.__iter__.return_value = iter([])
        mock_client.return_value.stream_train_positions.return_value = mock_stream

        with patch("sys.stdout", new_callable=io.StringIO):
            run_plain_stream_mode(demo=False, record=False, enable_deadband=True)


if __name__ == "__main__":
    unittest.main()
