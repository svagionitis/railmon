"""Terminal User Interface (TUI) package for Hellenic Railways live tracker.

Exports all public TUI and headless telemetry symbols lazily via PEP 562
(__getattr__ / __dir__) so headless daemons (server, exporter, gtfs_rt,
railmon_watchdog, storage) can import tui.state, tui.geo, tui.mock_engine,
tui.stream_worker, and tui.theme without triggering an eager curses import.
"""

import importlib
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Tuple

if TYPE_CHECKING:
    from tui.app import TUIApp, run_tui
    from tui.geo import (
        BoundingBox,
        BrailleCanvas,
        DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC,
        DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC,
        DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH,
        DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC,
        GeoPoint,
        GeoProjector,
        InfrastructureFeature,
        InfrastructureType,
        OverspeedSeverity,
        PortalNode,
        SCALE_BAR_INTERVALS_KM,
        ScaleBarSpec,
        SensorHealthAssessment,
        SensorHealthState,
        SignalAspect,
        SlowOrder,
        SpeedEnforcementResult,
        SpeedLimitZoneType,
        Viewport,
        detect_train_tunnel,
        evaluate_sensor_health,
        evaluate_train_speed,
        find_nearest_station_aabb,
        haversine_distance_km,
        is_point_in_tunnel,
        parse_infrastructure_geojson,
        parse_speed_limits_json,
        project_point_to_segment,
    )
    from tui.mock_engine import (
        MockTelemetryEngine,
        SimulatedTrain,
        StationNode,
        TrackPolyline,
        calculate_bearing,
        haversine_distance,
        load_infrastructure,
        load_slow_orders,
        load_speed_limits,
        load_stations,
        load_tracks,
    )
    from tui.renderer import (
        PaneGeometry,
        StationBoardEntry,
        TUIRenderer,
        calculate_scroll_offset,
        compute_station_board_entries,
        extract_unit_suffix,
        format_corridor_display_name,
        format_disambiguated_train_label,
        format_next_station_eta,
        format_progress_bar,
        format_scale_bar,
        format_wgs84_coords,
        get_compass_bearing,
        get_delay_color_pair,
        get_heading_glyph,
        get_track_corridor_color_pair,
        normalize_station_key,
    )
    from tui.state import (
        BreadcrumbMode,
        ConnectionStatus,
        HistoricalBreadcrumb,
        InfrastructureLayerMode,
        InspectorMode,
        REPLAY_SPEED_MULTIPLIERS,
        RosterSortMode,
        StateSnapshot,
        StationLabelMode,
        TrackColorMode,
        TrainRecord,
        TrainStateStore,
    )
    from tui.stream_worker import ReplayWorker, StreamWorker
    from tui.theme import (
        ThemeProfile,
        ThemeSpec,
        get_next_theme,
        get_theme_spec,
        parse_theme_name,
    )

_LAZY_SUBMODULES: FrozenSet[str] = frozenset(
    {
        "app",
        "data",
        "geo",
        "mock_engine",
        "renderer",
        "state",
        "stream_worker",
        "theme",
    }
)

_LAZY_EXPORTS: Dict[str, Tuple[str, str]] = {
    # tui.app (curses-dependent; loaded strictly on demand)
    "TUIApp": ("tui.app", "TUIApp"),
    "run_tui": ("tui.app", "run_tui"),
    # tui.geo (headless-safe)
    "BoundingBox": ("tui.geo", "BoundingBox"),
    "BrailleCanvas": ("tui.geo", "BrailleCanvas"),
    "DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC": ("tui.geo", "DEFAULT_OPEN_LINE_DROP_THRESHOLD_SEC"),
    "DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC": ("tui.geo", "DEFAULT_SENSOR_FREEZE_MAX_PACKET_AGE_SEC"),
    "DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH": ("tui.geo", "DEFAULT_SENSOR_FREEZE_MIN_SPEED_KMH"),
    "DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC": ("tui.geo", "DEFAULT_SENSOR_FREEZE_THRESHOLD_SEC"),
    "GeoPoint": ("tui.geo", "GeoPoint"),
    "GeoProjector": ("tui.geo", "GeoProjector"),
    "InfrastructureFeature": ("tui.geo", "InfrastructureFeature"),
    "InfrastructureType": ("tui.geo", "InfrastructureType"),
    "OverspeedSeverity": ("tui.geo", "OverspeedSeverity"),
    "PortalNode": ("tui.geo", "PortalNode"),
    "SCALE_BAR_INTERVALS_KM": ("tui.geo", "SCALE_BAR_INTERVALS_KM"),
    "ScaleBarSpec": ("tui.geo", "ScaleBarSpec"),
    "SensorHealthAssessment": ("tui.geo", "SensorHealthAssessment"),
    "SensorHealthState": ("tui.geo", "SensorHealthState"),
    "SignalAspect": ("tui.geo", "SignalAspect"),
    "SlowOrder": ("tui.geo", "SlowOrder"),
    "SpeedEnforcementResult": ("tui.geo", "SpeedEnforcementResult"),
    "SpeedLimitZoneType": ("tui.geo", "SpeedLimitZoneType"),
    "Viewport": ("tui.geo", "Viewport"),
    "detect_train_tunnel": ("tui.geo", "detect_train_tunnel"),
    "evaluate_sensor_health": ("tui.geo", "evaluate_sensor_health"),
    "evaluate_train_speed": ("tui.geo", "evaluate_train_speed"),
    "find_nearest_station_aabb": ("tui.geo", "find_nearest_station_aabb"),
    "haversine_distance_km": ("tui.geo", "haversine_distance_km"),
    "is_point_in_tunnel": ("tui.geo", "is_point_in_tunnel"),
    "parse_infrastructure_geojson": ("tui.geo", "parse_infrastructure_geojson"),
    "parse_speed_limits_json": ("tui.geo", "parse_speed_limits_json"),
    "project_point_to_segment": ("tui.geo", "project_point_to_segment"),
    # tui.mock_engine (headless-safe)
    "MockTelemetryEngine": ("tui.mock_engine", "MockTelemetryEngine"),
    "SimulatedTrain": ("tui.mock_engine", "SimulatedTrain"),
    "StationNode": ("tui.mock_engine", "StationNode"),
    "TrackPolyline": ("tui.mock_engine", "TrackPolyline"),
    "calculate_bearing": ("tui.mock_engine", "calculate_bearing"),
    "haversine_distance": ("tui.mock_engine", "haversine_distance"),
    "load_infrastructure": ("tui.mock_engine", "load_infrastructure"),
    "load_slow_orders": ("tui.mock_engine", "load_slow_orders"),
    "load_speed_limits": ("tui.mock_engine", "load_speed_limits"),
    "load_stations": ("tui.mock_engine", "load_stations"),
    "load_tracks": ("tui.mock_engine", "load_tracks"),
    # tui.renderer (curses-dependent; loaded strictly on demand)
    "PaneGeometry": ("tui.renderer", "PaneGeometry"),
    "StationBoardEntry": ("tui.renderer", "StationBoardEntry"),
    "TUIRenderer": ("tui.renderer", "TUIRenderer"),
    "calculate_scroll_offset": ("tui.renderer", "calculate_scroll_offset"),
    "compute_station_board_entries": ("tui.renderer", "compute_station_board_entries"),
    "extract_unit_suffix": ("tui.renderer", "extract_unit_suffix"),
    "format_corridor_display_name": ("tui.renderer", "format_corridor_display_name"),
    "format_disambiguated_train_label": ("tui.renderer", "format_disambiguated_train_label"),
    "format_next_station_eta": ("tui.renderer", "format_next_station_eta"),
    "format_progress_bar": ("tui.renderer", "format_progress_bar"),
    "format_scale_bar": ("tui.renderer", "format_scale_bar"),
    "format_wgs84_coords": ("tui.renderer", "format_wgs84_coords"),
    "get_compass_bearing": ("tui.renderer", "get_compass_bearing"),
    "get_delay_color_pair": ("tui.renderer", "get_delay_color_pair"),
    "get_heading_glyph": ("tui.renderer", "get_heading_glyph"),
    "get_track_corridor_color_pair": ("tui.renderer", "get_track_corridor_color_pair"),
    "normalize_station_key": ("tui.renderer", "normalize_station_key"),
    # tui.state (headless-safe)
    "BreadcrumbMode": ("tui.state", "BreadcrumbMode"),
    "ConnectionStatus": ("tui.state", "ConnectionStatus"),
    "HistoricalBreadcrumb": ("tui.state", "HistoricalBreadcrumb"),
    "InfrastructureLayerMode": ("tui.state", "InfrastructureLayerMode"),
    "InspectorMode": ("tui.state", "InspectorMode"),
    "REPLAY_SPEED_MULTIPLIERS": ("tui.state", "REPLAY_SPEED_MULTIPLIERS"),
    "RosterSortMode": ("tui.state", "RosterSortMode"),
    "StateSnapshot": ("tui.state", "StateSnapshot"),
    "StationLabelMode": ("tui.state", "StationLabelMode"),
    "TrackColorMode": ("tui.state", "TrackColorMode"),
    "TrainRecord": ("tui.state", "TrainRecord"),
    "TrainStateStore": ("tui.state", "TrainStateStore"),
    # tui.stream_worker (headless-safe)
    "ReplayWorker": ("tui.stream_worker", "ReplayWorker"),
    "StreamWorker": ("tui.stream_worker", "StreamWorker"),
    # tui.theme (headless-safe)
    "ThemeProfile": ("tui.theme", "ThemeProfile"),
    "ThemeSpec": ("tui.theme", "ThemeSpec"),
    "get_next_theme": ("tui.theme", "get_next_theme"),
    "get_theme_spec": ("tui.theme", "get_theme_spec"),
    "parse_theme_name": ("tui.theme", "parse_theme_name"),
}

__all__: List[str] = list(_LAZY_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    """Lazily resolve public TUI symbols and submodules on first attribute access (PEP 562)."""
    if name in _LAZY_EXPORTS:
        module_name, attr_name = _LAZY_EXPORTS[name]
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    if name in _LAZY_SUBMODULES:
        module = importlib.import_module(f"tui.{name}")
        globals()[name] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> List[str]:
    """Return sorted module attributes including lazily exported public symbols."""
    return sorted(set(globals().keys()) | set(__all__) | set(_LAZY_SUBMODULES))
