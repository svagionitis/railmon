"""Unit and integration test suite for F-7.3 Custom Terminal Color Theme Profiles."""

from dataclasses import dataclass
from typing import Any
import unittest
from unittest.mock import MagicMock, patch
import curses

from tui.theme import (
    PAIR_BORDER,
    PAIR_CONNECTED,
    PAIR_DEFAULT,
    PAIR_HEADER,
    PAIR_ROSTER_SELECT,
    PAIR_STATION,
    PAIR_STATION_DIM,
    PAIR_STATUS_BAR,
    PAIR_TRACK,
    PAIR_TRAIL,
    PAIR_TRAIN_ALERT,
    PAIR_TRAIN_FREIGHT,
    PAIR_TRAIN_HALTED,
    PAIR_TRAIN_ON_TIME,
    PAIR_TRAIN_PROAST,
    PAIR_TRAIN_WARN,
    THEME_ORDER,
    THEME_REGISTRY,
    ColorPairDef,
    ThemeProfile,
    ThemeSpec,
    get_next_theme,
    get_theme_spec,
    parse_theme_name,
)
from tui.state import InputMode, StateSnapshot, TrainStateStore
from tui.renderer import TUIRenderer, safe_color_pair


class TestThemeSubsystem(unittest.TestCase):
    """Test the theme domain models, registries, and parsing functions."""

    def test_theme_profile_enum_members(self) -> None:
        """Verify all 5 theme profiles exist with expected string values."""
        self.assertEqual(ThemeProfile.AZURE.value, "azure")
        self.assertEqual(ThemeProfile.AMBER.value, "amber")
        self.assertEqual(ThemeProfile.MATRIX.value, "matrix")
        self.assertEqual(ThemeProfile.SOLARIZED.value, "solarized")
        self.assertEqual(ThemeProfile.MONOCHROME.value, "monochrome")
        self.assertEqual(len(ThemeProfile), 5)

    def test_theme_registry_completeness(self) -> None:
        """Verify all enum profiles are registered and cover all 16 PAIR_* indices."""
        all_expected_pairs = {
            PAIR_DEFAULT,
            PAIR_HEADER,
            PAIR_BORDER,
            PAIR_TRACK,
            PAIR_STATION,
            PAIR_STATION_DIM,
            PAIR_TRAIN_ON_TIME,
            PAIR_TRAIN_WARN,
            PAIR_TRAIN_ALERT,
            PAIR_TRAIN_PROAST,
            PAIR_TRAIN_FREIGHT,
            PAIR_TRAIN_HALTED,
            PAIR_ROSTER_SELECT,
            PAIR_TRAIL,
            PAIR_STATUS_BAR,
            PAIR_CONNECTED,
        }
        self.assertEqual(len(all_expected_pairs), 16)

        for profile in ThemeProfile:
            self.assertIn(profile, THEME_REGISTRY)
            spec = get_theme_spec(profile)
            self.assertIsInstance(spec, ThemeSpec)
            self.assertEqual(spec.profile, profile)
            self.assertTrue(len(spec.display_name) > 0)
            self.assertTrue(len(spec.description) > 0)

            # Check all 16 pairs exist
            defined_pairs = set(spec.pairs.keys())
            self.assertEqual(
                defined_pairs,
                all_expected_pairs,
                f"Theme {profile.name} is missing color pairs: {all_expected_pairs - defined_pairs}",
            )
            for pair_id, p_def in spec.pairs.items():
                self.assertIsInstance(p_def, ColorPairDef)
                self.assertEqual(p_def.pair_id, pair_id)

    def test_get_theme_spec_type_validation(self) -> None:
        """Verify get_theme_spec rejects non-ThemeProfile types."""
        with self.assertRaises(TypeError):
            get_theme_spec("azure")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            get_theme_spec(123)  # type: ignore[arg-type]

    def test_get_next_theme_cycle_sequence(self) -> None:
        """Verify sequential cycling traverses all themes in designated order."""
        expected_sequence = (
            (ThemeProfile.AZURE, ThemeProfile.AMBER),
            (ThemeProfile.AMBER, ThemeProfile.MATRIX),
            (ThemeProfile.MATRIX, ThemeProfile.SOLARIZED),
            (ThemeProfile.SOLARIZED, ThemeProfile.MONOCHROME),
            (ThemeProfile.MONOCHROME, ThemeProfile.AZURE),
        )
        for current, expected_next in expected_sequence:
            self.assertEqual(get_next_theme(current), expected_next)

    def test_get_next_theme_type_validation(self) -> None:
        """Verify get_next_theme rejects invalid inputs with TypeError."""
        with self.assertRaises(TypeError):
            get_next_theme("amber")  # type: ignore[arg-type]

    def test_parse_theme_name(self) -> None:
        """Verify string parsing resolves exact names, case variants, and aliases."""
        # Exact values
        self.assertEqual(parse_theme_name("azure"), ThemeProfile.AZURE)
        self.assertEqual(parse_theme_name("amber"), ThemeProfile.AMBER)
        self.assertEqual(parse_theme_name("matrix"), ThemeProfile.MATRIX)
        self.assertEqual(parse_theme_name("solarized"), ThemeProfile.SOLARIZED)
        self.assertEqual(parse_theme_name("monochrome"), ThemeProfile.MONOCHROME)

        # Case-insensitivity & whitespace
        self.assertEqual(parse_theme_name("  AZURE  "), ThemeProfile.AZURE)
        self.assertEqual(parse_theme_name("Amber"), ThemeProfile.AMBER)
        self.assertEqual(parse_theme_name("Matrix"), ThemeProfile.MATRIX)

        # Aliases
        self.assertEqual(parse_theme_name("crt"), ThemeProfile.AMBER)
        self.assertEqual(parse_theme_name("vt220"), ThemeProfile.AMBER)
        self.assertEqual(parse_theme_name("green"), ThemeProfile.MATRIX)
        self.assertEqual(parse_theme_name("solarized-dark"), ThemeProfile.SOLARIZED)
        self.assertEqual(parse_theme_name("solarized_dark"), ThemeProfile.SOLARIZED)
        self.assertEqual(parse_theme_name("mono"), ThemeProfile.MONOCHROME)
        self.assertEqual(parse_theme_name("bw"), ThemeProfile.MONOCHROME)
        self.assertEqual(parse_theme_name("hellenic"), ThemeProfile.AZURE)
        self.assertEqual(parse_theme_name("default"), ThemeProfile.AZURE)

        # None and invalid returns None safely
        self.assertIsNone(parse_theme_name(None))
        self.assertIsNone(parse_theme_name(""))
        self.assertIsNone(parse_theme_name("unknown-palette"))


class TestRendererThemeApplication(unittest.TestCase):
    """Test renderer palette binding, degradation modes, and attribute overrides."""

    @patch("curses.has_colors", return_value=True)
    @patch("curses.start_color")
    @patch("curses.use_default_colors")
    @patch("curses.init_pair")
    def test_apply_theme_256_colors(
        self,
        mock_init_pair: MagicMock,
        mock_use_default: MagicMock,
        mock_start_color: MagicMock,
        mock_has_colors: MagicMock,
    ) -> None:
        """Verify apply_theme binds 256-color palette when curses.COLORS >= 256."""
        renderer = TUIRenderer()
        with patch.object(curses, "COLORS", 256, create=True), patch.object(
            curses, "COLOR_PAIRS", 64, create=True
        ):
            ok = renderer.apply_theme(ThemeProfile.AMBER)
            self.assertTrue(ok)
            self.assertEqual(renderer.active_theme, ThemeProfile.AMBER)
            # init_pair should be called 16 times
            self.assertEqual(mock_init_pair.call_count, 16)

            # Check that PAIR_DEFAULT was initialized with fg=214, bg=232
            spec = get_theme_spec(ThemeProfile.AMBER)
            default_def = spec.pairs[PAIR_DEFAULT]
            mock_init_pair.assert_any_call(PAIR_DEFAULT, default_def.fg_256, default_def.bg_256)

    @patch("curses.has_colors", return_value=True)
    @patch("curses.start_color")
    @patch("curses.use_default_colors")
    @patch("curses.init_pair")
    def test_apply_theme_16_color_fallback(
        self,
        mock_init_pair: MagicMock,
        mock_use_default: MagicMock,
        mock_start_color: MagicMock,
        mock_has_colors: MagicMock,
    ) -> None:
        """Verify apply_theme falls back to 16-color ANSI codes when curses.COLORS < 256."""
        renderer = TUIRenderer()
        with patch.object(curses, "COLORS", 8, create=True), patch.object(
            curses, "COLOR_PAIRS", 64, create=True
        ):
            ok = renderer.apply_theme(ThemeProfile.AMBER)
            self.assertTrue(ok)
            spec = get_theme_spec(ThemeProfile.AMBER)
            default_def = spec.pairs[PAIR_DEFAULT]
            # Should have used fg_16 and bg_16
            mock_init_pair.assert_any_call(PAIR_DEFAULT, default_def.fg_16, default_def.bg_16)

    @patch("curses.has_colors", return_value=False)
    def test_apply_theme_no_colors_graceful_degradation(
        self, mock_has_colors: MagicMock
    ) -> None:
        """Verify apply_theme returns False gracefully without error when curses has no color."""
        renderer = TUIRenderer()
        ok = renderer.apply_theme(ThemeProfile.MONOCHROME)
        self.assertFalse(ok)
        self.assertEqual(renderer.active_theme, ThemeProfile.MONOCHROME)

    def test_apply_theme_type_validation(self) -> None:
        """Verify apply_theme rejects non-ThemeProfile types."""
        renderer = TUIRenderer()
        with self.assertRaises(TypeError):
            renderer.apply_theme("azure")  # type: ignore[arg-type]

    @patch("curses.has_colors", return_value=True)
    @patch("curses.start_color")
    @patch("curses.use_default_colors")
    @patch("curses.init_pair")
    def test_monochrome_attribute_overrides_in_safe_color_pair(
        self,
        mock_init_pair: MagicMock,
        mock_use_default: MagicMock,
        mock_start_color: MagicMock,
        mock_has_colors: MagicMock,
    ) -> None:
        """Verify safe_color_pair blends attribute overrides in Monochrome mode."""
        renderer = TUIRenderer()
        with patch.object(curses, "COLORS", 256, create=True), patch.object(
            curses, "COLOR_PAIRS", 64, create=True
        ):
            renderer.apply_theme(ThemeProfile.MONOCHROME)

        # In monochrome mode, PAIR_TRAIN_ALERT has attribute override A_REVERSE | A_BOLD
        with patch("curses.color_pair", return_value=0):
            attr = safe_color_pair(PAIR_TRAIN_ALERT, curses.A_NORMAL)
            self.assertTrue(attr & curses.A_REVERSE)
            self.assertTrue(attr & curses.A_BOLD)

            # PAIR_HEADER has attribute override A_BOLD
            header_attr = safe_color_pair(PAIR_HEADER, curses.A_NORMAL)
            self.assertTrue(header_attr & curses.A_BOLD)


class TestStateStoreThemeManagement(unittest.TestCase):
    """Test state store theme storage, cycling, and snapshot propagation."""

    def test_default_theme_is_azure(self) -> None:
        """Verify initial default theme is ThemeProfile.AZURE."""
        store = TrainStateStore()
        snap = store.get_snapshot()
        self.assertEqual(snap.theme_profile, ThemeProfile.AZURE)

    def test_initial_theme_injection(self) -> None:
        """Verify TrainStateStore respects injected initial_theme."""
        store = TrainStateStore(initial_theme=ThemeProfile.MATRIX)
        snap = store.get_snapshot()
        self.assertEqual(snap.theme_profile, ThemeProfile.MATRIX)

    def test_set_theme_profile_increments_version(self) -> None:
        """Verify set_theme_profile modifies state and increments version."""
        store = TrainStateStore()
        v0 = store.get_snapshot().version
        store.set_theme_profile(ThemeProfile.SOLARIZED)
        snap = store.get_snapshot()
        self.assertEqual(snap.theme_profile, ThemeProfile.SOLARIZED)
        self.assertGreater(snap.version, v0)

        # Setting same theme again should not increment version
        v1 = snap.version
        store.set_theme_profile(ThemeProfile.SOLARIZED)
        self.assertEqual(store.get_snapshot().version, v1)

    def test_set_theme_profile_type_validation(self) -> None:
        """Verify set_theme_profile raises TypeError on invalid argument."""
        store = TrainStateStore()
        with self.assertRaises(TypeError):
            store.set_theme_profile("amber")  # type: ignore[arg-type]

    def test_cycle_theme_profile_progression(self) -> None:
        """Verify cycle_theme_profile advances sequentially through THEME_ORDER."""
        store = TrainStateStore()
        self.assertEqual(store.get_snapshot().theme_profile, ThemeProfile.AZURE)

        t1 = store.cycle_theme_profile()
        self.assertEqual(t1, ThemeProfile.AMBER)
        self.assertEqual(store.get_snapshot().theme_profile, ThemeProfile.AMBER)

        t2 = store.cycle_theme_profile()
        self.assertEqual(t2, ThemeProfile.MATRIX)

        t3 = store.cycle_theme_profile()
        self.assertEqual(t3, ThemeProfile.SOLARIZED)

        t4 = store.cycle_theme_profile()
        self.assertEqual(t4, ThemeProfile.MONOCHROME)

        t5 = store.cycle_theme_profile()
        self.assertEqual(t5, ThemeProfile.AZURE)


class TestAppThemeControls(unittest.TestCase):
    """Test TUIApp key dispatching, toast notifications, and search mode isolation."""

    def _create_mock_app(self) -> Any:
        from tui.app import TUIApp

        stdscr = MagicMock()
        stdscr.getmaxyx.return_value = (30, 100)
        store = TrainStateStore()
        worker = MagicMock()
        app = TUIApp(
            stdscr=stdscr,
            store=store,
            worker=worker,
            stations=[],
            tracks=[],
            ascii_mode=False,
        )
        return app

    @patch("curses.has_colors", return_value=True)
    @patch("curses.start_color")
    @patch("curses.use_default_colors")
    @patch("curses.init_pair")
    def test_key_k_cycles_theme_in_normal_mode(
        self,
        mock_init_pair: MagicMock,
        mock_use_default: MagicMock,
        mock_start_color: MagicMock,
        mock_has_colors: MagicMock,
    ) -> None:
        """Verify uppercase 'K' (ord('K')) cycles theme in NORMAL mode."""
        app = self._create_mock_app()
        self.assertEqual(app.input_mode, InputMode.NORMAL)
        self.assertEqual(app.store.get_snapshot().theme_profile, ThemeProfile.AZURE)

        # Dispatch 'K'
        handled = app._handle_normal_input(ord("K"))
        self.assertTrue(handled)
        self.assertEqual(app.store.get_snapshot().theme_profile, ThemeProfile.AMBER)
        self.assertEqual(app.renderer.active_theme, ThemeProfile.AMBER)

        # Toast notification should be active
        toast_msg, is_err = app.get_active_toast()
        self.assertIsNotNone(toast_msg)
        self.assertIn("Retro Amber", str(toast_msg))
        self.assertFalse(is_err)

    @patch("curses.has_colors", return_value=True)
    @patch("curses.start_color")
    @patch("curses.use_default_colors")
    @patch("curses.init_pair")
    def test_key_f10_cycles_theme_in_normal_mode(
        self,
        mock_init_pair: MagicMock,
        mock_use_default: MagicMock,
        mock_start_color: MagicMock,
        mock_has_colors: MagicMock,
    ) -> None:
        """Verify KEY_F10 cycles theme in NORMAL mode."""
        app = self._create_mock_app()
        f10_key = getattr(curses, "KEY_F10", 274)
        handled = app._handle_normal_input(f10_key)
        self.assertTrue(handled)
        self.assertEqual(app.store.get_snapshot().theme_profile, ThemeProfile.AMBER)

    def test_lowercase_k_does_not_cycle_theme(self) -> None:
        """Verify lowercase 'k' executes pan up and does NOT cycle theme."""
        app = self._create_mock_app()
        self.assertEqual(app.store.get_snapshot().theme_profile, ThemeProfile.AZURE)
        initial_lat = app.projector.viewport.center.lat

        # Dispatch 'k'
        app._handle_normal_input(ord("k"))
        # Theme should remain AZURE
        self.assertEqual(app.store.get_snapshot().theme_profile, ThemeProfile.AZURE)
        # Viewport should have panned northward
        self.assertGreater(app.projector.viewport.center.lat, initial_lat)

    def test_key_k_in_search_mode_appends_character_without_cycling(self) -> None:
        """Verify typing 'K' while searching appends to buffer and does not change theme."""
        app = self._create_mock_app()
        app.input_mode = InputMode.SEARCH
        app.search_buffer = "IC"

        # Dispatch 'K' in search mode
        app._handle_search_input(ord("K"))
        self.assertEqual(app.search_buffer, "ICK")
        self.assertEqual(app.store.get_snapshot().theme_profile, ThemeProfile.AZURE)

    @patch("train_tracker.run_tui_mode")
    def test_cli_theme_argument_passed_to_run_tui_mode(self, mock_run_tui: MagicMock) -> None:
        """Verify passing --theme passes the theme to run_tui_mode."""
        import train_tracker

        with patch("sys.argv", ["railmon", "--tui", "--theme", "amber"]):
            code = train_tracker.main()
            self.assertEqual(code, 0)
            mock_run_tui.assert_called_once()
            self.assertEqual(mock_run_tui.call_args.kwargs.get("theme"), "amber")


if __name__ == "__main__":
    unittest.main()
