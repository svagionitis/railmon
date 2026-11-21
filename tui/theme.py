"""Terminal color theme profiles and palette management for railmon TUI.

Provides strongly-typed color pairs, 256-color / 16-color / 8-color degradation
tables, and theme specifications for custom visual profiles (F-7.3).
"""

from dataclasses import dataclass
from enum import Enum, unique
from typing import Dict, Optional, Tuple
import curses

# Curses color pair indices
PAIR_DEFAULT = 1
PAIR_HEADER = 2
PAIR_BORDER = 3
PAIR_TRACK = 4
PAIR_STATION = 5
PAIR_STATION_DIM = 6
PAIR_TRAIN_ON_TIME = 7
PAIR_TRAIN_WARN = 8
PAIR_TRAIN_ALERT = 9
PAIR_TRAIN_PROAST = 10
PAIR_TRAIN_FREIGHT = 11
PAIR_TRAIN_HALTED = 12
PAIR_ROSTER_SELECT = 13
PAIR_TRAIL = 14
PAIR_STATUS_BAR = 15
PAIR_CONNECTED = 16


@unique
class ThemeProfile(Enum):
    """Available visual color theme profiles."""

    AZURE = "azure"              # Hellenic Azure (Default Greek rail livery)
    AMBER = "amber"              # Retro Amber CRT Phosphor (VT220 style)
    MATRIX = "matrix"            # Cyberpunk phosphor green (P31 cathode ray)
    SOLARIZED = "solarized"      # Solarized Dark precision low-fatigue palette
    MONOCHROME = "monochrome"    # High-contrast black & white (WCAG AAA / e-ink)


@dataclass(frozen=True)
class ColorPairDef:
    """Specification for a single curses color pair."""

    pair_id: int
    fg_256: int
    bg_256: int
    fg_16: int
    bg_16: int
    attr_override: Optional[int] = None


@dataclass(frozen=True)
class ThemeSpec:
    """Complete specification of a terminal theme profile."""

    profile: ThemeProfile
    display_name: str
    description: str
    pairs: Dict[int, ColorPairDef]
    has_custom_bg: bool = False
    bg_color_256: int = -1
    bg_color_16: int = -1


def build_azure_theme() -> ThemeSpec:
    """Construct Hellenic Azure default theme."""
    bg_def = -1
    pairs = {
        PAIR_DEFAULT: ColorPairDef(PAIR_DEFAULT, 255, bg_def, curses.COLOR_WHITE, bg_def),
        PAIR_HEADER: ColorPairDef(PAIR_HEADER, 39, bg_def, curses.COLOR_CYAN, bg_def),
        PAIR_BORDER: ColorPairDef(PAIR_BORDER, 32, bg_def, curses.COLOR_BLUE, bg_def),
        PAIR_TRACK: ColorPairDef(PAIR_TRACK, 45, bg_def, curses.COLOR_WHITE, bg_def),
        PAIR_STATION: ColorPairDef(PAIR_STATION, 220, bg_def, curses.COLOR_YELLOW, bg_def),
        PAIR_STATION_DIM: ColorPairDef(PAIR_STATION_DIM, 178, bg_def, curses.COLOR_YELLOW, bg_def),
        PAIR_TRAIN_ON_TIME: ColorPairDef(PAIR_TRAIN_ON_TIME, 42, bg_def, curses.COLOR_GREEN, bg_def),
        PAIR_TRAIN_WARN: ColorPairDef(PAIR_TRAIN_WARN, 214, bg_def, curses.COLOR_YELLOW, bg_def),
        PAIR_TRAIN_ALERT: ColorPairDef(PAIR_TRAIN_ALERT, 196, bg_def, curses.COLOR_RED, bg_def),
        PAIR_TRAIN_PROAST: ColorPairDef(PAIR_TRAIN_PROAST, 51, bg_def, curses.COLOR_CYAN, bg_def),
        PAIR_TRAIN_FREIGHT: ColorPairDef(PAIR_TRAIN_FREIGHT, 177, bg_def, curses.COLOR_MAGENTA, bg_def),
        PAIR_TRAIN_HALTED: ColorPairDef(PAIR_TRAIN_HALTED, 245, bg_def, curses.COLOR_WHITE, bg_def),
        PAIR_ROSTER_SELECT: ColorPairDef(PAIR_ROSTER_SELECT, 16, 39, curses.COLOR_BLACK, curses.COLOR_CYAN),
        PAIR_TRAIL: ColorPairDef(PAIR_TRAIL, 38, bg_def, curses.COLOR_GREEN, bg_def),
        PAIR_STATUS_BAR: ColorPairDef(PAIR_STATUS_BAR, 234, 252, curses.COLOR_BLACK, curses.COLOR_WHITE),
        PAIR_CONNECTED: ColorPairDef(PAIR_CONNECTED, 42, bg_def, curses.COLOR_GREEN, bg_def),
    }
    return ThemeSpec(
        profile=ThemeProfile.AZURE,
        display_name="Hellenic Azure",
        description="Deep blue branding, bright cyan tracks, golden train glyphs",
        pairs=pairs,
        has_custom_bg=False,
    )


def build_amber_theme() -> ThemeSpec:
    """Construct Retro Amber CRT phosphor theme."""
    bg_256 = 232  # Obsidian Black
    bg_16 = curses.COLOR_BLACK
    pairs = {
        PAIR_DEFAULT: ColorPairDef(PAIR_DEFAULT, 214, bg_256, curses.COLOR_YELLOW, bg_16),
        PAIR_HEADER: ColorPairDef(PAIR_HEADER, 220, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_BOLD),
        PAIR_BORDER: ColorPairDef(PAIR_BORDER, 130, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_DIM),
        PAIR_TRACK: ColorPairDef(PAIR_TRACK, 178, bg_256, curses.COLOR_YELLOW, bg_16),
        PAIR_STATION: ColorPairDef(PAIR_STATION, 220, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_BOLD),
        PAIR_STATION_DIM: ColorPairDef(
            PAIR_STATION_DIM, 94, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_TRAIN_ON_TIME: ColorPairDef(PAIR_TRAIN_ON_TIME, 214, bg_256, curses.COLOR_YELLOW, bg_16),
        PAIR_TRAIN_WARN: ColorPairDef(
            PAIR_TRAIN_WARN, 208, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_BOLD
        ),
        PAIR_TRAIN_ALERT: ColorPairDef(
            PAIR_TRAIN_ALERT, 196, bg_256, curses.COLOR_RED, bg_16, attr_override=curses.A_BOLD
        ),
        PAIR_TRAIN_PROAST: ColorPairDef(PAIR_TRAIN_PROAST, 215, bg_256, curses.COLOR_YELLOW, bg_16),
        PAIR_TRAIN_FREIGHT: ColorPairDef(PAIR_TRAIN_FREIGHT, 172, bg_256, curses.COLOR_YELLOW, bg_16),
        PAIR_TRAIN_HALTED: ColorPairDef(
            PAIR_TRAIN_HALTED, 130, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_ROSTER_SELECT: ColorPairDef(PAIR_ROSTER_SELECT, 232, 214, curses.COLOR_BLACK, curses.COLOR_YELLOW),
        PAIR_TRAIL: ColorPairDef(PAIR_TRAIL, 94, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_DIM),
        PAIR_STATUS_BAR: ColorPairDef(PAIR_STATUS_BAR, 232, 214, curses.COLOR_BLACK, curses.COLOR_YELLOW),
        PAIR_CONNECTED: ColorPairDef(
            PAIR_CONNECTED, 220, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_BOLD
        ),
    }
    return ThemeSpec(
        profile=ThemeProfile.AMBER,
        display_name="Retro Amber CRT",
        description="Warm monochrome amber phosphor on black (VT220 styling)",
        pairs=pairs,
        has_custom_bg=True,
        bg_color_256=bg_256,
        bg_color_16=bg_16,
    )


def build_matrix_theme() -> ThemeSpec:
    """Construct Matrix phosphor green theme."""
    bg_256 = 232  # Obsidian Black
    bg_16 = curses.COLOR_BLACK
    pairs = {
        PAIR_DEFAULT: ColorPairDef(PAIR_DEFAULT, 46, bg_256, curses.COLOR_GREEN, bg_16),
        PAIR_HEADER: ColorPairDef(PAIR_HEADER, 82, bg_256, curses.COLOR_GREEN, bg_16, attr_override=curses.A_BOLD),
        PAIR_BORDER: ColorPairDef(PAIR_BORDER, 22, bg_256, curses.COLOR_GREEN, bg_16, attr_override=curses.A_DIM),
        PAIR_TRACK: ColorPairDef(PAIR_TRACK, 34, bg_256, curses.COLOR_GREEN, bg_16),
        PAIR_STATION: ColorPairDef(PAIR_STATION, 46, bg_256, curses.COLOR_GREEN, bg_16, attr_override=curses.A_BOLD),
        PAIR_STATION_DIM: ColorPairDef(
            PAIR_STATION_DIM, 28, bg_256, curses.COLOR_GREEN, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_TRAIN_ON_TIME: ColorPairDef(PAIR_TRAIN_ON_TIME, 46, bg_256, curses.COLOR_GREEN, bg_16),
        PAIR_TRAIN_WARN: ColorPairDef(
            PAIR_TRAIN_WARN, 118, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_BOLD
        ),
        PAIR_TRAIN_ALERT: ColorPairDef(
            PAIR_TRAIN_ALERT, 232, 46, curses.COLOR_BLACK, curses.COLOR_GREEN, attr_override=curses.A_BOLD
        ),
        PAIR_TRAIN_PROAST: ColorPairDef(PAIR_TRAIN_PROAST, 48, bg_256, curses.COLOR_GREEN, bg_16),
        PAIR_TRAIN_FREIGHT: ColorPairDef(PAIR_TRAIN_FREIGHT, 65, bg_256, curses.COLOR_GREEN, bg_16),
        PAIR_TRAIN_HALTED: ColorPairDef(
            PAIR_TRAIN_HALTED, 22, bg_256, curses.COLOR_GREEN, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_ROSTER_SELECT: ColorPairDef(PAIR_ROSTER_SELECT, 232, 46, curses.COLOR_BLACK, curses.COLOR_GREEN),
        PAIR_TRAIL: ColorPairDef(PAIR_TRAIL, 28, bg_256, curses.COLOR_GREEN, bg_16, attr_override=curses.A_DIM),
        PAIR_STATUS_BAR: ColorPairDef(PAIR_STATUS_BAR, 232, 46, curses.COLOR_BLACK, curses.COLOR_GREEN),
        PAIR_CONNECTED: ColorPairDef(
            PAIR_CONNECTED, 82, bg_256, curses.COLOR_GREEN, bg_16, attr_override=curses.A_BOLD
        ),
    }
    return ThemeSpec(
        profile=ThemeProfile.MATRIX,
        display_name="Matrix Green",
        description="High-contrast phosphor green on black for night monitoring",
        pairs=pairs,
        has_custom_bg=True,
        bg_color_256=bg_256,
        bg_color_16=bg_16,
    )


def build_solarized_theme() -> ThemeSpec:
    """Construct Solarized Dark low-fatigue theme."""
    bg_256 = 234  # Base03 (#002b36)
    bg_16 = curses.COLOR_BLACK
    pairs = {
        PAIR_DEFAULT: ColorPairDef(PAIR_DEFAULT, 244, bg_256, curses.COLOR_WHITE, bg_16),
        PAIR_HEADER: ColorPairDef(PAIR_HEADER, 37, bg_256, curses.COLOR_CYAN, bg_16),
        PAIR_BORDER: ColorPairDef(PAIR_BORDER, 240, bg_256, curses.COLOR_BLUE, bg_16),
        PAIR_TRACK: ColorPairDef(PAIR_TRACK, 33, bg_256, curses.COLOR_CYAN, bg_16),
        PAIR_STATION: ColorPairDef(PAIR_STATION, 136, bg_256, curses.COLOR_YELLOW, bg_16),
        PAIR_STATION_DIM: ColorPairDef(
            PAIR_STATION_DIM, 240, bg_256, curses.COLOR_YELLOW, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_TRAIN_ON_TIME: ColorPairDef(PAIR_TRAIN_ON_TIME, 64, bg_256, curses.COLOR_GREEN, bg_16),
        PAIR_TRAIN_WARN: ColorPairDef(PAIR_TRAIN_WARN, 166, bg_256, curses.COLOR_YELLOW, bg_16),
        PAIR_TRAIN_ALERT: ColorPairDef(PAIR_TRAIN_ALERT, 160, bg_256, curses.COLOR_RED, bg_16),
        PAIR_TRAIN_PROAST: ColorPairDef(PAIR_TRAIN_PROAST, 61, bg_256, curses.COLOR_MAGENTA, bg_16),
        PAIR_TRAIN_FREIGHT: ColorPairDef(PAIR_TRAIN_FREIGHT, 125, bg_256, curses.COLOR_MAGENTA, bg_16),
        PAIR_TRAIN_HALTED: ColorPairDef(
            PAIR_TRAIN_HALTED, 240, bg_256, curses.COLOR_WHITE, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_ROSTER_SELECT: ColorPairDef(PAIR_ROSTER_SELECT, 234, 244, curses.COLOR_BLACK, curses.COLOR_WHITE),
        PAIR_TRAIL: ColorPairDef(PAIR_TRAIL, 240, bg_256, curses.COLOR_BLUE, bg_16, attr_override=curses.A_DIM),
        PAIR_STATUS_BAR: ColorPairDef(PAIR_STATUS_BAR, 234, 245, curses.COLOR_BLACK, curses.COLOR_WHITE),
        PAIR_CONNECTED: ColorPairDef(PAIR_CONNECTED, 64, bg_256, curses.COLOR_GREEN, bg_16),
    }
    return ThemeSpec(
        profile=ThemeProfile.SOLARIZED,
        display_name="Solarized Dark",
        description="Standard low-fatigue precision palette for terminal operators",
        pairs=pairs,
        has_custom_bg=True,
        bg_color_256=bg_256,
        bg_color_16=bg_16,
    )


def build_monochrome_theme() -> ThemeSpec:
    """Construct High-Contrast Monochrome theme (WCAG AAA / e-ink)."""
    bg_256 = 16  # Black
    bg_16 = curses.COLOR_BLACK
    fg_white = curses.COLOR_WHITE
    fg_black = curses.COLOR_BLACK
    pairs = {
        PAIR_DEFAULT: ColorPairDef(
            PAIR_DEFAULT, 255, bg_256, fg_white, bg_16, attr_override=curses.A_NORMAL
        ),
        PAIR_HEADER: ColorPairDef(
            PAIR_HEADER, 255, bg_256, fg_white, bg_16, attr_override=curses.A_BOLD
        ),
        PAIR_BORDER: ColorPairDef(
            PAIR_BORDER, 255, bg_256, fg_white, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_TRACK: ColorPairDef(
            PAIR_TRACK, 255, bg_256, fg_white, bg_16, attr_override=curses.A_NORMAL
        ),
        PAIR_STATION: ColorPairDef(
            PAIR_STATION, 255, bg_256, fg_white, bg_16, attr_override=curses.A_BOLD
        ),
        PAIR_STATION_DIM: ColorPairDef(
            PAIR_STATION_DIM, 255, bg_256, fg_white, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_TRAIN_ON_TIME: ColorPairDef(
            PAIR_TRAIN_ON_TIME, 255, bg_256, fg_white, bg_16, attr_override=curses.A_NORMAL
        ),
        PAIR_TRAIN_WARN: ColorPairDef(
            PAIR_TRAIN_WARN, 255, bg_256, fg_white, bg_16, attr_override=curses.A_UNDERLINE | curses.A_BOLD
        ),
        PAIR_TRAIN_ALERT: ColorPairDef(
            PAIR_TRAIN_ALERT, 16, 255, fg_black, fg_white, attr_override=curses.A_REVERSE | curses.A_BOLD
        ),
        PAIR_TRAIN_PROAST: ColorPairDef(
            PAIR_TRAIN_PROAST, 255, bg_256, fg_white, bg_16, attr_override=curses.A_NORMAL
        ),
        PAIR_TRAIN_FREIGHT: ColorPairDef(
            PAIR_TRAIN_FREIGHT, 255, bg_256, fg_white, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_TRAIN_HALTED: ColorPairDef(
            PAIR_TRAIN_HALTED, 255, bg_256, fg_white, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_ROSTER_SELECT: ColorPairDef(
            PAIR_ROSTER_SELECT, 16, 255, fg_black, fg_white, attr_override=curses.A_REVERSE | curses.A_BOLD
        ),
        PAIR_TRAIL: ColorPairDef(
            PAIR_TRAIL, 255, bg_256, fg_white, bg_16, attr_override=curses.A_DIM
        ),
        PAIR_STATUS_BAR: ColorPairDef(
            PAIR_STATUS_BAR, 16, 255, fg_black, fg_white, attr_override=curses.A_REVERSE
        ),
        PAIR_CONNECTED: ColorPairDef(
            PAIR_CONNECTED, 255, bg_256, fg_white, bg_16, attr_override=curses.A_BOLD
        ),
    }
    return ThemeSpec(
        profile=ThemeProfile.MONOCHROME,
        display_name="High-Contrast Monochrome",
        description="Pure black-and-white for minimal/e-ink consoles and WCAG AAA accessibility",
        pairs=pairs,
        has_custom_bg=True,
        bg_color_256=bg_256,
        bg_color_16=bg_16,
    )


THEME_ORDER: Tuple[ThemeProfile, ...] = (
    ThemeProfile.AZURE,
    ThemeProfile.AMBER,
    ThemeProfile.MATRIX,
    ThemeProfile.SOLARIZED,
    ThemeProfile.MONOCHROME,
)

THEME_REGISTRY: Dict[ThemeProfile, ThemeSpec] = {
    ThemeProfile.AZURE: build_azure_theme(),
    ThemeProfile.AMBER: build_amber_theme(),
    ThemeProfile.MATRIX: build_matrix_theme(),
    ThemeProfile.SOLARIZED: build_solarized_theme(),
    ThemeProfile.MONOCHROME: build_monochrome_theme(),
}


def get_theme_spec(profile: ThemeProfile) -> ThemeSpec:
    """Retrieve theme specification by profile enum."""
    if not isinstance(profile, ThemeProfile):
        raise TypeError(f"Expected ThemeProfile enum, got {type(profile).__name__}: {profile}")
    return THEME_REGISTRY[profile]


def get_next_theme(current: ThemeProfile) -> ThemeProfile:
    """Return next sequential theme profile in cycle order."""
    if not isinstance(current, ThemeProfile):
        raise TypeError(f"Expected ThemeProfile enum, got {type(current).__name__}: {current}")
    idx = THEME_ORDER.index(current)
    return THEME_ORDER[(idx + 1) % len(THEME_ORDER)]


def parse_theme_name(name: Optional[str]) -> Optional[ThemeProfile]:
    """Parse a string theme identifier (case-insensitive) into a ThemeProfile."""
    if not name or not name.strip():
        return None
    cleaned = name.strip().lower().replace("-", "_")
    for profile in ThemeProfile:
        if profile.value == cleaned:
            return profile
    aliases = {
        "hellenic": ThemeProfile.AZURE,
        "default": ThemeProfile.AZURE,
        "blue": ThemeProfile.AZURE,
        "crt": ThemeProfile.AMBER,
        "vt220": ThemeProfile.AMBER,
        "green": ThemeProfile.MATRIX,
        "solarized_dark": ThemeProfile.SOLARIZED,
        "mono": ThemeProfile.MONOCHROME,
        "bw": ThemeProfile.MONOCHROME,
    }
    return aliases.get(cleaned)
