"""Embedded zero-dependency web map server package for railmon."""

from typing import List

import train_tracker

from server.assets import (
    GeoAssetCache,
    get_geojson_asset,
    get_spa_html,
)
from server.daemon import (
    WebMapServer,
    install_signal_handlers,
)
from server.handler import (
    WebMapRequestHandler,
)
from server.models import (
    ClientConnection,
    ServerConfig,
    ServerHealthMetrics,
    SSEMessage,
    TileProvider,
)
from server.sse import MAX_CLIENT_QUEUE_DEPTH, SSEBroadcaster

__version__: str = train_tracker.__version__
__author__: str = train_tracker.__author__

__all__: List[str] = [
    "__version__",
    "__author__",
    "MAX_CLIENT_QUEUE_DEPTH",
    "ClientConnection",
    "GeoAssetCache",
    "ServerConfig",
    "ServerHealthMetrics",
    "SSEBroadcaster",
    "SSEMessage",
    "TileProvider",
    "WebMapRequestHandler",
    "WebMapServer",
    "get_geojson_asset",
    "get_spa_html",
    "install_signal_handlers",
]
