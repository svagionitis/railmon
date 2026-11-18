"""Terminal User Interface (TUI) package for Hellenic Railways live tracker."""

from tui.geo import BoundingBox, BrailleCanvas, GeoPoint, GeoProjector, Viewport
from tui.mock_engine import (
    MockTelemetryEngine,
    SimulatedTrain,
    StationNode,
    TrackPolyline,
    calculate_bearing,
    haversine_distance,
    load_stations,
    load_tracks,
)
from tui.state import (
    ConnectionStatus,
    HistoricalBreadcrumb,
    StateSnapshot,
    TrainRecord,
    TrainStateStore,
)
from tui.stream_worker import StreamWorker

__all__ = [
    "GeoPoint",
    "BoundingBox",
    "Viewport",
    "GeoProjector",
    "BrailleCanvas",
    "StationNode",
    "TrackPolyline",
    "SimulatedTrain",
    "MockTelemetryEngine",
    "load_stations",
    "load_tracks",
    "haversine_distance",
    "calculate_bearing",
    "ConnectionStatus",
    "HistoricalBreadcrumb",
    "TrainRecord",
    "StateSnapshot",
    "TrainStateStore",
    "StreamWorker",
]
