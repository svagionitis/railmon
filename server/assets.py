"""Embedded web application assets, templates, and GeoJSON cache for railmon serve."""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.models import ServerConfig, TileProvider

logger = logging.getLogger("railmon.server.assets")

# Base directory for built-in railway topology GeoJSON assets
DATA_DIR: Path = Path(__file__).resolve().parent.parent / "tui" / "data"


class GeoAssetCache:
    """Thread-safe in-memory cache for railway GIS topology layers."""

    def __init__(self, data_dir: Path = DATA_DIR) -> None:
        """Initialize asset cache with target data directory."""
        self._data_dir = data_dir
        self._lock = threading.Lock()
        self._cache: Dict[str, str] = {}

    def get_asset_raw(self, asset_filename: str) -> str:
        """Retrieve raw cached JSON string for a given dataset file."""
        with self._lock:
            if asset_filename in self._cache:
                return self._cache[asset_filename]

        target_file = self._data_dir / asset_filename
        if not target_file.exists():
            logger.error("Required GIS dataset file missing: %s", target_file)
            return json.dumps({"type": "FeatureCollection", "features": []})

        try:
            raw_text = target_file.read_text(encoding="utf-8")
            # Validate JSON parseability
            json.loads(raw_text)
            with self._lock:
                self._cache[asset_filename] = raw_text
            return raw_text
        except Exception as exc:
            logger.error("Failed to load GIS dataset %s: %s", asset_filename, exc)
            return json.dumps({"type": "FeatureCollection", "features": []})

    def get_asset_json_string(self, asset_filename: str) -> str:
        """Alias for get_asset_raw for backwards compatibility."""
        return self.get_asset_raw(asset_filename)

    def get_tracks_geojson(self) -> str:
        """Convert tracks.json corridors and segments into standard RFC 7946 GeoJSON."""
        cache_key = "__tracks_geojson__"
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        raw = self.get_asset_raw("tracks.json")
        try:
            data = json.loads(raw)
            features: List[Dict[str, Any]] = []
            for corridor in data.get("corridors", []):
                # Convert segments: tracks.json has [[lat, lng], ...], GeoJSON requires [[lng, lat], ...]
                multi_line: List[List[List[float]]] = []
                for segment in corridor.get("segments", []):
                    line_coords: List[List[float]] = []
                    for pt in segment:
                        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                            # Swap from (lat, lng) to (lng, lat) for GeoJSON
                            line_coords.append([float(pt[1]), float(pt[0])])
                    if line_coords:
                        multi_line.append(line_coords)

                if multi_line:
                    features.append({
                        "type": "Feature",
                        "id": corridor.get("id", ""),
                        "geometry": {
                            "type": "MultiLineString",
                            "coordinates": multi_line,
                        },
                        "properties": {
                            "id": corridor.get("id", ""),
                            "name": corridor.get("name", ""),
                            "type": corridor.get("type", "mainline"),
                            "electrified": corridor.get("electrified", False),
                            "double_track": corridor.get("double_track", False),
                        },
                    })

            geojson = json.dumps({
                "type": "FeatureCollection",
                "version": data.get("version", "1.0"),
                "features": features,
            })
            with self._lock:
                self._cache[cache_key] = geojson
            return geojson
        except Exception as exc:
            logger.error("Failed to convert tracks to GeoJSON: %s", exc)
            return json.dumps({"type": "FeatureCollection", "features": []})

    def get_stations_geojson(self) -> str:
        """Convert stations.json list into standard RFC 7946 GeoJSON Point features."""
        cache_key = "__stations_geojson__"
        with self._lock:
            if cache_key in self._cache:
                return self._cache[cache_key]

        raw = self.get_asset_raw("stations.json")
        try:
            data = json.loads(raw)
            features: List[Dict[str, Any]] = []
            for station in data.get("stations", []):
                lat = station.get("lat")
                lng = station.get("lng")
                if lat is not None and lng is not None:
                    features.append({
                        "type": "Feature",
                        "id": station.get("id", ""),
                        "geometry": {
                            "type": "Point",
                            "coordinates": [float(lng), float(lat)],
                        },
                        "properties": {
                            "id": station.get("id", ""),
                            "code": station.get("code", ""),
                            "name_el": station.get("name_el", ""),
                            "name_en": station.get("name_en", ""),
                            "corridor": station.get("corridor", ""),
                            "is_hub": station.get("is_hub", False),
                            "altitude_m": station.get("altitude_m", 0.0),
                        },
                    })

            geojson = json.dumps({
                "type": "FeatureCollection",
                "version": data.get("version", "1.0"),
                "features": features,
            })
            with self._lock:
                self._cache[cache_key] = geojson
            return geojson
        except Exception as exc:
            logger.error("Failed to convert stations to GeoJSON: %s", exc)
            return json.dumps({"type": "FeatureCollection", "features": []})

    def get_infrastructure_geojson(self) -> str:
        """Return infrastructure.json FeatureCollection."""
        return self.get_asset_raw("infrastructure.json")

    def get_speed_limits_json(self) -> str:
        """Return speed_limits.json."""
        return self.get_asset_raw("speed_limits.json")


# Global asset cache singleton
_ASSET_CACHE = GeoAssetCache()


def get_geojson_asset(asset_name: str) -> str:
    """Convenience accessor for cached GeoJSON string assets."""
    cleaned = asset_name.strip().lower()
    if cleaned in ("/api/tracks", "tracks"):
        return _ASSET_CACHE.get_tracks_geojson()
    if cleaned in ("/api/stations", "stations"):
        return _ASSET_CACHE.get_stations_geojson()
    if cleaned in ("/api/infrastructure", "infrastructure"):
        return _ASSET_CACHE.get_infrastructure_geojson()
    if cleaned in ("/api/speed-limits", "speed_limits", "speed-limits"):
        return _ASSET_CACHE.get_speed_limits_json()
    return _ASSET_CACHE.get_asset_raw(asset_name)


# Master HTML5/CSS3/ES6 Single-Page Application Template
SPA_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>RAILMON | Hellenic Railways Real-Time Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
  <style>
    :root {
      --bg-dark: #0a0e17;
      --bg-card: rgba(16, 24, 39, 0.94);
      --bg-card-hover: #1e293b;
      --bg-input: #1e293b;
      --border-color: #1f293d;
      --border-highlight: #334155;
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
      --text-dim: #64748b;

      /* Service category & delay color tokens */
      --azure-ic: #0ea5e9;
      --emerald-pro: #10b981;
      --amber-reg: #f59e0b;
      --crimson-delay: #ef4444;
      --purple-freight: #a855f7;

      /* Railway corridor palette */
      --line-pathe: #38bdf8;
      --line-airport: #34d399;
      --line-chalkida: #fbbf24;
      --line-transbalkan: #c084fc;
      --line-branch: #a3e635;
    }

    * { box-sizing: border-box; margin: 0; padding: 0; }
    html, body {
      height: 100%;
      width: 100%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg-dark);
      color: var(--text-main);
      overflow: hidden;
      display: flex;
      flex-direction: column;
    }

    /* Top Navigation Header */
    header {
      height: 50px;
      min-height: 50px;
      background: var(--bg-card);
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 16px;
      z-index: 1000;
      backdrop-filter: blur(8px);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      font-weight: 700;
      font-size: 15px;
      letter-spacing: 0.5px;
      color: var(--azure-ic);
      white-space: nowrap;
    }
    .brand-title { color: var(--text-main); font-weight: 600; font-size: 13px; }
    .brand-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--azure-ic); }

    .header-controls {
      display: flex;
      align-items: center;
      gap: 14px;
      font-size: 12px;
    }
    .tile-select, .layer-toggle-btn {
      background: var(--bg-input);
      color: var(--text-main);
      border: 1px solid var(--border-color);
      border-radius: 6px;
      padding: 4px 8px;
      font-size: 12px;
      cursor: pointer;
      outline: none;
    }
    .tile-select:focus, .layer-toggle-btn:focus { border-color: var(--azure-ic); }
    .layer-toggles { display: flex; align-items: center; gap: 8px; }
    .layer-toggle-label {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      color: var(--text-muted);
      cursor: pointer;
      user-select: none;
    }
    .layer-toggle-label input { cursor: pointer; accent-color: var(--azure-ic); }

    .header-metrics {
      display: flex;
      align-items: center;
      gap: 12px;
      font-size: 12px;
    }
    .metric-badge {
      padding: 4px 8px;
      border-radius: 6px;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid var(--border-color);
      white-space: nowrap;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.3px;
      background: rgba(16, 185, 129, 0.15);
      color: var(--emerald-pro);
      border: 1px solid rgba(16, 185, 129, 0.3);
      white-space: nowrap;
    }
    .status-pill.reconnecting {
      background: rgba(245, 158, 11, 0.15);
      color: var(--amber-reg);
      border-color: rgba(245, 158, 11, 0.3);
    }
    .status-pill.offline {
      background: rgba(239, 68, 68, 0.15);
      color: var(--crimson-delay);
      border-color: rgba(239, 68, 68, 0.3);
    }

    /* Main Container (Map + Sidebar) */
    .main-container {
      flex: 1;
      display: flex;
      position: relative;
      overflow: hidden;
      width: 100%;
      height: calc(100vh - 74px);
    }

    /* Leaflet Map */
    #map {
      flex: 1;
      height: 100%;
      width: 100%;
      background: #070a0f;
      z-index: 1;
    }
    #map.offline-grid {
      background: radial-gradient(#1e293b 1px, #070a0f 1px);
      background-size: 24px 24px;
    }

    /* Conflict Warning Banner */
    .conflict-banner {
      position: absolute;
      top: 12px;
      left: 50%;
      transform: translateX(-50%);
      background: var(--crimson-delay);
      color: white;
      padding: 9px 18px;
      border-radius: 8px;
      font-weight: 600;
      font-size: 13px;
      z-index: 2000;
      box-shadow: 0 4px 20px rgba(239, 68, 68, 0.5);
      display: none;
      align-items: center;
      gap: 12px;
      animation: alertPulse 1.8s infinite;
      max-width: 90%;
    }
    @keyframes alertPulse {
      0% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0.7); }
      70% { box-shadow: 0 0 0 12px rgba(239, 68, 68, 0); }
      100% { box-shadow: 0 0 0 0 rgba(239, 68, 68, 0); }
    }
    .conflict-banner .alert-actions { display: flex; align-items: center; gap: 8px; }
    .conflict-banner button {
      background: rgba(255, 255, 255, 0.2);
      border: 1px solid rgba(255, 255, 255, 0.4);
      color: white;
      border-radius: 4px;
      padding: 3px 8px;
      font-size: 11px;
      cursor: pointer;
      font-weight: bold;
    }
    .conflict-banner button:hover { background: rgba(255, 255, 255, 0.35); }

    /* Operator HUD Sidebar */
    #sidebar {
      width: 380px;
      min-width: 340px;
      background: var(--bg-card);
      border-left: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      z-index: 1000;
      backdrop-filter: blur(12px);
      transition: transform 0.3s ease;
    }
    .sidebar-toggle-btn {
      display: none;
      position: absolute;
      bottom: 20px;
      right: 20px;
      z-index: 1500;
      background: var(--azure-ic);
      color: white;
      border: none;
      border-radius: 50%;
      width: 44px;
      height: 44px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.4);
      font-size: 18px;
      cursor: pointer;
    }

    /* Roster Toolbar */
    .roster-toolbar {
      padding: 12px 14px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      gap: 10px;
    }
    .search-box {
      width: 100%;
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      padding: 8px 12px;
      border-radius: 6px;
      color: var(--text-main);
      font-size: 13px;
      outline: none;
      transition: border-color 0.2s;
    }
    .search-box:focus { border-color: var(--azure-ic); }
    .sort-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      font-size: 12px;
      color: var(--text-muted);
    }
    .sort-btn-group { display: flex; gap: 4px; }
    .sort-btn {
      background: var(--bg-input);
      border: 1px solid var(--border-color);
      color: var(--text-muted);
      border-radius: 4px;
      padding: 3px 8px;
      font-size: 11px;
      cursor: pointer;
    }
    .sort-btn.active {
      background: var(--azure-ic);
      color: white;
      border-color: var(--azure-ic);
      font-weight: 600;
    }

    /* Scrollable Train Roster List */
    .roster-list {
      flex: 1;
      overflow-y: auto;
      list-style: none;
    }
    .roster-list::-webkit-scrollbar { width: 6px; }
    .roster-list::-webkit-scrollbar-thumb { background: #334155; border-radius: 3px; }
    .roster-item {
      padding: 11px 14px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
      cursor: pointer;
      transition: background 0.15s;
    }
    .roster-item:hover { background: var(--bg-card-hover); }
    .roster-item.selected {
      background: rgba(14, 165, 233, 0.12);
      border-left: 3px solid var(--azure-ic);
    }
    .roster-col-main { display: flex; flex-direction: column; gap: 3px; }
    .roster-title-row { display: flex; align-items: center; gap: 8px; }
    .train-num { font-weight: 700; font-size: 14px; color: var(--text-main); }
    .service-badge {
      font-size: 10px;
      font-weight: 700;
      padding: 2px 5px;
      border-radius: 4px;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .badge-ic { background: rgba(14, 165, 233, 0.2); color: var(--azure-ic); }
    .badge-pro { background: rgba(16, 185, 129, 0.2); color: var(--emerald-pro); }
    .badge-reg { background: rgba(245, 158, 11, 0.2); color: var(--amber-reg); }
    .badge-other { background: rgba(148, 163, 184, 0.2); color: var(--text-muted); }

    .route-subtext {
      font-size: 11px;
      color: var(--text-dim);
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
      max-width: 210px;
    }
    .roster-col-stats {
      display: flex;
      flex-direction: column;
      align-items: flex-end;
      gap: 3px;
    }
    .speed-val { font-size: 13px; font-weight: 700; color: var(--text-main); }
    .speed-val small { font-size: 10px; font-weight: normal; color: var(--text-dim); }
    .delay-val { font-size: 11px; font-weight: 600; padding: 1px 5px; border-radius: 4px; }
    .delay-ontime { color: var(--emerald-pro); background: rgba(16, 185, 129, 0.1); }
    .delay-minor { color: var(--amber-reg); background: rgba(245, 158, 11, 0.1); }
    .delay-severe { color: var(--crimson-delay); background: rgba(239, 68, 68, 0.15); font-weight: 700; }

    /* Selected Train Inspector Card */
    #inspectorCard {
      background: rgba(15, 23, 42, 0.98);
      border-top: 1px solid var(--border-color);
      padding: 14px;
      display: flex;
      flex-direction: column;
      gap: 10px;
      font-size: 12px;
    }
    .inspector-empty {
      color: var(--text-dim);
      text-align: center;
      padding: 12px;
      font-style: italic;
    }
    .inspector-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .inspector-header h3 {
      font-size: 15px;
      font-weight: 700;
      color: var(--azure-ic);
      display: flex;
      align-items: center;
      gap: 8px;
    }
    .inspector-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px 12px;
    }
    .inspector-field label {
      display: block;
      font-size: 10px;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.5px;
      margin-bottom: 2px;
    }
    .inspector-field span {
      font-size: 12px;
      font-weight: 600;
      color: var(--text-main);
    }
    .sparkline-box {
      margin-top: 4px;
      display: flex;
      flex-direction: column;
      gap: 3px;
    }
    .sparkline-box label {
      font-size: 10px;
      color: var(--text-dim);
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .sparkline-svg {
      width: 100%;
      height: 38px;
      background: rgba(30, 41, 59, 0.5);
      border-radius: 4px;
      overflow: hidden;
    }
    .inspector-actions {
      display: flex;
      gap: 8px;
      margin-top: 4px;
    }
    .btn-action {
      flex: 1;
      padding: 6px 10px;
      border-radius: 5px;
      font-size: 11px;
      font-weight: 600;
      cursor: pointer;
      text-align: center;
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 5px;
      border: 1px solid var(--border-color);
      background: var(--bg-input);
      color: var(--text-main);
      transition: background 0.15s, border-color 0.15s;
    }
    .btn-action:hover {
      background: var(--bg-card-hover);
      border-color: var(--azure-ic);
    }
    .btn-action.btn-cab {
      background: rgba(14, 165, 233, 0.2);
      border-color: rgba(14, 165, 233, 0.4);
      color: var(--azure-ic);
    }
    .btn-action.btn-cab:hover {
      background: rgba(14, 165, 233, 0.35);
    }

    /* Bottom Status Bar */
    footer {
      height: 24px;
      min-height: 24px;
      background: #06090e;
      border-top: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 14px;
      font-size: 11px;
      color: var(--text-dim);
      z-index: 1000;
    }
    .status-left, .status-right { display: flex; align-items: center; gap: 14px; }

    /* Custom Leaflet SVG Train Markers */
    .custom-train-marker {
      background: none !important;
      border: none !important;
    }
    .train-marker-body {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      cursor: pointer;
    }
    .loco-glyph-wrap {
      width: 28px;
      height: 28px;
      display: flex;
      align-items: center;
      justify-content: center;
      transition: transform 0.25s cubic-bezier(0.4, 0, 0.2, 1);
    }
    .loco-svg {
      width: 26px;
      height: 26px;
      filter: drop-shadow(0 2px 4px rgba(0,0,0,0.8));
    }
    .marker-label {
      font-size: 10px;
      font-weight: 800;
      color: #ffffff;
      background: rgba(10, 14, 23, 0.88);
      border: 1px solid var(--border-highlight);
      padding: 1px 4px;
      border-radius: 4px;
      margin-top: -2px;
      white-space: nowrap;
      pointer-events: none;
    }
    .marker-speed {
      font-size: 9px;
      font-weight: 600;
      color: #38bdf8;
      background: rgba(15, 23, 42, 0.9);
      padding: 0 3px;
      border-radius: 3px;
      margin-top: 1px;
      pointer-events: none;
    }

    /* Category marker colors */
    .cat-ic .loco-body { fill: #0ea5e9; stroke: #38bdf8; stroke-width: 1.5; }
    .cat-pro .loco-body { fill: #10b981; stroke: #34d399; stroke-width: 1.5; }
    .cat-reg .loco-body { fill: #f59e0b; stroke: #fbbf24; stroke-width: 1.5; }
    .cat-other .loco-body { fill: #94a3b8; stroke: #cbd5e1; stroke-width: 1.5; }

    .marker-selected .loco-glyph-wrap {
      filter: drop-shadow(0 0 8px #38bdf8) !important;
    }
    .marker-selected .marker-label {
      border-color: #38bdf8;
      color: #38bdf8;
    }

    .marker-delayed .loco-body { fill: #ef4444 !important; stroke: #fca5a5 !important; }
    .marker-delayed .loco-glyph-wrap {
      animation: locoDelayPulse 1.5s infinite;
    }
    @keyframes locoDelayPulse {
      0% { filter: drop-shadow(0 0 2px rgba(239, 68, 68, 0.8)); }
      50% { filter: drop-shadow(0 0 10px rgba(239, 68, 68, 1)); }
      100% { filter: drop-shadow(0 0 2px rgba(239, 68, 68, 0.8)); }
    }

    /* Station Popup Styling */
    .leaflet-popup-content-wrapper {
      background: rgba(15, 23, 42, 0.96) !important;
      color: var(--text-main) !important;
      border: 1px solid var(--border-color);
      border-radius: 8px !important;
      box-shadow: 0 8px 24px rgba(0,0,0,0.6) !important;
    }
    .leaflet-popup-tip { background: rgba(15, 23, 42, 0.96) !important; }
    .station-popup { font-size: 12px; }
    .station-popup h4 { font-size: 14px; font-weight: 700; color: var(--azure-ic); margin-bottom: 4px; }
    .station-popup .meta { color: var(--text-muted); font-size: 11px; margin-bottom: 8px; }
    .station-popup a {
      color: #38bdf8;
      text-decoration: none;
      font-weight: 600;
      display: inline-block;
      margin-top: 4px;
    }
    .station-popup a:hover { text-decoration: underline; }

    /* Responsive Breakpoints */
    @media (max-width: 1024px) {
      #sidebar {
        position: absolute;
        bottom: 0;
        left: 0;
        right: 0;
        width: 100%;
        height: 50vh;
        border-left: none;
        border-top: 1px solid var(--border-color);
        transform: translateY(100%);
      }
      #sidebar.open { transform: translateY(0); }
      .sidebar-toggle-btn { display: flex; align-items: center; justify-content: center; }
      .header-controls .layer-toggles { display: none; }
    }
  </style>
</head>
<body>
  <!-- Top Navigation Header -->
  <header>
    <div class="brand">
      <div class="brand-dot"></div>
      RAILMON
      <span class="brand-title">| Hellenic Railways Real-Time Map</span>
    </div>

    <div class="header-controls">
      <select id="tileSelect" class="tile-select" onchange="switchTileProvider(this.value)">
        <option value="carto-dark">CartoDB Dark Matter</option>
        <option value="carto-light">CartoDB Positron</option>
        <option value="osm">OpenStreetMap</option>
      </select>

      <div class="layer-toggles">
        <label class="layer-toggle-label">
          <input type="checkbox" id="layerTracks" checked onchange="toggleLayer('tracks', this.checked)"> Tracks
        </label>
        <label class="layer-toggle-label">
          <input type="checkbox" id="layerStations" checked onchange="toggleLayer('stations', this.checked)"> Stations
        </label>
        <label class="layer-toggle-label">
          <input type="checkbox" id="layerInfra" checked onchange="toggleLayer('infra', this.checked)"> Infra
        </label>
        <label class="layer-toggle-label">
          <input type="checkbox" id="layerTrails" onchange="toggleLayer('trails', this.checked)"> Trails
        </label>
      </div>
    </div>

    <div class="header-metrics">
      <div class="metric-badge">Active: <strong id="activeCount" style="color:var(--azure-ic);">0</strong></div>
      <div class="metric-badge">Delayed: <strong id="delayCount" style="color:var(--crimson-delay);">0</strong></div>
      <div id="connPill" class="status-pill">● LIVE SSE</div>
    </div>
  </header>

  <!-- Main View Area (Map + Floating Alerts + Sidebar) -->
  <div class="main-container">
    <div id="conflictBanner" class="conflict-banner">
      <span>⚠️ <strong>WATCHDOG CONFLICT ALERT:</strong> <span id="conflictText">Headway Violation</span></span>
      <div class="alert-actions">
        <button onclick="dismissConflictBanner()">Dismiss</button>
      </div>
    </div>

    <div id="map"></div>

    <button id="sidebarToggle" class="sidebar-toggle-btn" onclick="toggleSidebar()">☰</button>

    <aside id="sidebar">
      <div class="roster-toolbar">
        <input type="text" id="searchInput" class="search-box" placeholder="Filter train ID, line, station..." oninput="handleSearch(this.value)">
        <div class="sort-row">
          <span>Sort By:</span>
          <div class="sort-btn-group">
            <button id="sortBtnId" class="sort-btn active" onclick="setSort('id')">ID</button>
            <button id="sortBtnSpeed" class="sort-btn" onclick="setSort('speed')">Speed</button>
            <button id="sortBtnDelay" class="sort-btn" onclick="setSort('delay')">Delay</button>
          </div>
        </div>
      </div>

      <ul id="rosterList" class="roster-list"></ul>

      <div id="inspectorCard">
        <div class="inspector-empty">Select a train on the map or roster to inspect telemetry</div>
      </div>
    </aside>
  </div>

  <!-- Bottom Operational Status Bar -->
  <footer>
    <div class="status-left">
      <span>Ingestion Latency: <strong id="metricLatency" style="color:#e2e8f0;">--</strong></span>
      <span>SSE Clients: <strong id="metricClients" style="color:#e2e8f0;">--</strong></span>
    </div>
    <div class="status-right">
      <span id="mapCenterCoords">39.0742°N, 22.8243°E (Zoom 7)</span>
      <span>railmon v0.1.0</span>
    </div>
  </footer>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    // Injected Server Runtime Parameters
    const INITIAL_TILE_URL = '__TILE_URL__';
    const INITIAL_ATTRIBUTION = '__ATTRIBUTION__';
    const INITIAL_TILE_PROVIDER = '__TILE_PROVIDER__';

    // Application Reactive State
    const trainsMap = new Map();
    const trainMarkers = new Map();
    const trainTrails = new Map();
    const speedHistories = new Map();
    let selectedTrainId = null;
    let searchFilter = '';
    let sortField = 'id';
    let sortAsc = true;
    let sseSource = null;
    let reconnectDelay = 1000;
    let currentTileLayer = null;
    let offlineFallbackActive = false;

    // Layer Group References
    let tracksLayer = null;
    let stationsLayer = null;
    let infraLayer = null;
    let trailsLayerGroup = L.layerGroup();

    // 1. Initialize Leaflet Map
    const map = L.map('map', {
      center: [39.0742, 22.8243],
      zoom: 7,
      minZoom: 6,
      maxZoom: 18,
      zoomControl: false
    });
    L.control.zoom({ position: 'bottomleft' }).addTo(map);
    trailsLayerGroup.addTo(map);

    // Setup Initial Tile Layer
    function setTileLayer(tileUrl, attribution) {
      if (currentTileLayer) {
        map.removeLayer(currentTileLayer);
      }
      currentTileLayer = L.tileLayer(tileUrl, {
        maxZoom: 18,
        attribution: attribution
      });
      currentTileLayer.on('tileerror', function() {
        if (!offlineFallbackActive) {
          offlineFallbackActive = true;
          document.getElementById('map').classList.add('offline-grid');
          console.warn('[RAILMON] CDN tile error: fallback vector grid activated.');
        }
      });
      currentTileLayer.addTo(map);
    }
    setTileLayer(INITIAL_TILE_URL, INITIAL_ATTRIBUTION);
    document.getElementById('tileSelect').value = INITIAL_TILE_PROVIDER;

    function switchTileProvider(provider) {
      if (provider === 'carto-light') {
        setTileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', '&copy; CARTO');
      } else if (provider === 'osm') {
        setTileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', '&copy; OpenStreetMap');
      } else {
        setTileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', '&copy; CARTO');
      }
    }

    map.on('moveend', function() {
      const center = map.getCenter();
      const zoom = map.getZoom();
      document.getElementById('mapCenterCoords').innerText =
        `${center.lat.toFixed(4)}°N, ${center.lng.toFixed(4)}°E (Zoom ${zoom})`;
    });

    // 2. Fetch and Render Railway Vector Topologies
    function loadGeoLayers() {
      // Tracks Corridors
      fetch('/api/tracks')
        .then(r => r.json())
        .then(data => {
          tracksLayer = L.geoJSON(data, {
            style: function(f) {
              const id = ((f.properties && f.properties.id) || '').toLowerCase();
              let color = '#38bdf8'; // PATHE default
              if (id.includes('airport') || id.includes('kiato')) color = '#34d399';
              else if (id.includes('chalkida')) color = '#fbbf24';
              else if (id.includes('balkan') || id.includes('idomeni')) color = '#c084fc';
              else if (id.includes('volos') || id.includes('kalambaka') || id.includes('macedonia')) color = '#a3e635';
              return { color: color, weight: 3, opacity: 0.85, lineCap: 'round', lineJoin: 'round' };
            }
          }).addTo(map);
        }).catch(err => console.warn('[RAILMON] Could not load tracks:', err));

      // Stations
      fetch('/api/stations')
        .then(r => r.json())
        .then(data => {
          stationsLayer = L.geoJSON(data, {
            pointToLayer: function(f, latlng) {
              const isHub = f.properties && f.properties.is_hub;
              return L.circleMarker(latlng, {
                radius: isHub ? 6 : 4,
                fillColor: isHub ? '#38bdf8' : '#94a3b8',
                color: '#0a0e17',
                weight: 1.5,
                opacity: 1,
                fillOpacity: 0.9
              });
            },
            onEachFeature: function(f, layer) {
              const p = f.properties || {};
              const popup = `
                <div class="station-popup">
                  <h4>${p.name_el || 'Station'} ${p.name_en ? '(' + p.name_en + ')' : ''}</h4>
                  <div class="meta">Code: <strong>${p.code || '--'}</strong> | Corridor: ${p.corridor || 'Mainline'}</div>
                  ${p.altitude_m ? '<div class="meta">Altitude: ' + p.altitude_m + 'm</div>' : ''}
                  <a href="https://railway.gov.gr/departures/${p.code || ''}" target="_blank">View Live Departures &rarr;</a>
                </div>`;
              layer.bindPopup(popup);
            }
          }).addTo(map);
        }).catch(err => console.warn('[RAILMON] Could not load stations:', err));

      // Infrastructure (Tunnels, Bridges, Signals)
      fetch('/api/infrastructure')
        .then(r => r.json())
        .then(data => {
          infraLayer = L.geoJSON(data, {
            filter: f => (f.properties && f.properties.type === 'tunnel'),
            style: { color: '#64748b', weight: 4, dashArray: '6, 6', opacity: 0.9 }
          }).addTo(map);
        }).catch(err => console.warn('[RAILMON] Could not load infrastructure:', err));
    }
    loadGeoLayers();

    function toggleLayer(name, visible) {
      if (name === 'tracks' && tracksLayer) visible ? map.addLayer(tracksLayer) : map.removeLayer(tracksLayer);
      if (name === 'stations' && stationsLayer) visible ? map.addLayer(stationsLayer) : map.removeLayer(stationsLayer);
      if (name === 'infra' && infraLayer) visible ? map.addLayer(infraLayer) : map.removeLayer(infraLayer);
      if (name === 'trails') visible ? map.addLayer(trailsLayerGroup) : map.removeLayer(trailsLayerGroup);
    }

    // 3. SVG Locomotive Marker Construction
    function createTrainIcon(t) {
      const isSelected = selectedTrainId === t.id;
      const isDelayed = (t.delay || 0) > 15;
      const service = (t.service_type || t.train_type || 'IC').toUpperCase();
      let catClass = 'cat-ic';
      if (service.includes('PRO') || service.includes('SUB')) catClass = 'cat-pro';
      else if (service.includes('REG')) catClass = 'cat-reg';
      else if (service.includes('FREIGHT')) catClass = 'cat-other';

      const heading = Math.round(t.heading || 0);
      const trainNum = t.train_number || t.id;
      const speed = Math.round(t.speed || 0);

      const html = `
        <div class="train-marker-body ${catClass} ${isSelected ? 'marker-selected' : ''} ${isDelayed ? 'marker-delayed' : ''}">
          <div class="loco-glyph-wrap" style="transform: rotate(${heading}deg);">
            <svg class="loco-svg" viewBox="0 0 24 24">
              <!-- Directional Locomotive Body (Arrow heading forward) -->
              <path class="loco-body" d="M12 2L4 19L12 15L20 19L12 2Z" />
              <circle cx="12" cy="11" r="2.5" fill="#0a0e17" />
            </svg>
          </div>
          <div class="marker-label">${trainNum}</div>
          ${speed > 0 ? `<div class="marker-speed">${speed}k</div>` : ''}
        </div>`;

      return L.divIcon({
        html: html,
        className: 'custom-train-marker',
        iconSize: [40, 48],
        iconAnchor: [20, 24],
        popupAnchor: [0, -24]
      });
    }

    // 4. Marker & Trajectory Update Management
    function upsertTrainMarker(t) {
      if (!t.lat || !t.lng) return;
      const pos = [t.lat, t.lng];

      // Update speed observation history
      if (!speedHistories.has(t.id)) speedHistories.set(t.id, []);
      const history = speedHistories.get(t.id);
      history.push(t.speed || 0);
      if (history.length > 20) history.shift();

      // Update breadcrumb trail
      if (!trainTrails.has(t.id)) {
        const polyline = L.polyline([pos], { color: '#0ea5e9', weight: 2, opacity: 0.6 });
        trainTrails.set(t.id, polyline);
        if (document.getElementById('layerTrails').checked || selectedTrainId === t.id) {
          polyline.addTo(trailsLayerGroup);
        }
      } else {
        const polyline = trainTrails.get(t.id);
        const pts = polyline.getLatLngs();
        pts.push(pos);
        if (pts.length > 30) pts.shift();
        polyline.setLatLngs(pts);
      }

      if (trainMarkers.has(t.id)) {
        const marker = trainMarkers.get(t.id);
        marker.setLatLng(pos);
        marker.setIcon(createTrainIcon(t));
      } else {
        const marker = L.marker(pos, { icon: createTrainIcon(t) });
        marker.on('click', () => selectTrain(t.id));
        marker.addTo(map);
        trainMarkers.set(t.id, marker);
      }
    }

    function removeStaleMarkers(activeIds) {
      const activeSet = new Set(activeIds);
      trainMarkers.forEach((marker, id) => {
        if (!activeSet.has(id)) {
          map.removeLayer(marker);
          trainMarkers.delete(id);
          if (trainTrails.has(id)) {
            trailsLayerGroup.removeLayer(trainTrails.get(id));
            trainTrails.delete(id);
          }
          speedHistories.delete(id);
          trainsMap.delete(id);
        }
      });
    }

    // 5. Sidebar Roster Rendering
    function renderRoster() {
      const roster = document.getElementById('rosterList');
      const trains = Array.from(trainsMap.values());

      let delayedCount = 0;
      trains.forEach(t => { if ((t.delay || 0) > 15) delayedCount++; });
      document.getElementById('activeCount').innerText = trains.length;
      document.getElementById('delayCount').innerText = delayedCount;

      const filtered = trains.filter(t => {
        if (!searchFilter) return true;
        const needle = searchFilter.toLowerCase();
        const idMatch = (t.id || '').toLowerCase().includes(needle);
        const numMatch = (t.train_number || '').toLowerCase().includes(needle);
        const corrMatch = (t.corridor || '').toLowerCase().includes(needle);
        const stnMatch = (t.next_station || '').toLowerCase().includes(needle);
        return idMatch || numMatch || corrMatch || stnMatch;
      });

      filtered.sort((a, b) => {
        let valA = a[sortField];
        let valB = b[sortField];
        if (typeof valA === 'string') {
          return sortAsc ? valA.localeCompare(valB) : valB.localeCompare(valA);
        }
        valA = Number(valA || 0);
        valB = Number(valB || 0);
        return sortAsc ? (valA - valB) : (valB - valA);
      });

      roster.innerHTML = filtered.map(t => {
        const service = (t.service_type || t.train_type || 'IC').toUpperCase();
        let badgeClass = 'badge-ic';
        if (service.includes('PRO') || service.includes('SUB')) badgeClass = 'badge-pro';
        else if (service.includes('REG')) badgeClass = 'badge-reg';
        else if (service.includes('FREIGHT')) badgeClass = 'badge-other';

        const delay = Number(t.delay || 0);
        let delayClass = 'delay-ontime';
        let delayText = 'On Time';
        if (delay > 15) {
          delayClass = 'delay-severe';
          delayText = `+${Math.round(delay)}m`;
        } else if (delay > 3) {
          delayClass = 'delay-minor';
          delayText = `+${Math.round(delay)}m`;
        } else if (delay > 0) {
          delayText = `+${Math.round(delay)}m`;
        }

        const isSel = selectedTrainId === t.id;
        return `
          <li class="roster-item ${isSel ? 'selected' : ''}" onclick="selectTrain('${t.id}')">
            <div class="roster-col-main">
              <div class="roster-title-row">
                <span class="train-num">${t.train_number || t.id}</span>
                <span class="service-badge ${badgeClass}">${service}</span>
              </div>
              <div class="route-subtext">${t.corridor || 'PATHE'} &rarr; ${t.next_station || 'In Transit'}</div>
            </div>
            <div class="roster-col-stats">
              <span class="speed-val">${Math.round(t.speed || 0)} <small>km/h</small></span>
              <span class="delay-val ${delayClass}">${delayText}</span>
            </div>
          </li>`;
      }).join('');
    }

    function handleSearch(val) {
      searchFilter = val.trim();
      renderRoster();
    }

    function setSort(field) {
      if (sortField === field) {
        sortAsc = !sortAsc;
      } else {
        sortField = field;
        sortAsc = (field === 'id');
      }
      document.querySelectorAll('.sort-btn').forEach(btn => btn.classList.remove('active'));
      const activeBtnId = 'sortBtn' + field.charAt(0).toUpperCase() + field.slice(1);
      const activeBtn = document.getElementById(activeBtnId);
      if (activeBtn) activeBtn.classList.add('active');
      renderRoster();
    }

    // 6. Selected Train Inspector & SVG Sparkline
    function selectTrain(id) {
      selectedTrainId = id;
      const train = trainsMap.get(id);

      // Pan smoothly to train on map
      if (train && train.lat && train.lng) {
        map.panTo([train.lat, train.lng]);
      }

      // Update markers appearance
      trainMarkers.forEach((marker, markerId) => {
        const t = trainsMap.get(markerId);
        if (t) marker.setIcon(createTrainIcon(t));
      });

      renderInspector(train);
      renderRoster();
    }

    function renderInspector(t) {
      const card = document.getElementById('inspectorCard');
      if (!t) {
        card.innerHTML = '<div class="inspector-empty">Select a train on the map or roster to inspect telemetry</div>';
        return;
      }

      const history = speedHistories.get(t.id) || [t.speed || 0];
      const sparklineSvg = generateSparkline(history, 320, 38);
      const delay = Number(t.delay || 0);

      card.innerHTML = `
        <div class="inspector-header">
          <h3>Train ${t.train_number || t.id}</h3>
          <span class="metric-badge" style="color:var(--azure-ic);">${t.service_type || 'InterCity'}</span>
        </div>
        <div class="inspector-grid">
          <div class="inspector-field">
            <label>Current Speed</label>
            <span>${(t.speed || 0).toFixed(1)} km/h</span>
          </div>
          <div class="inspector-field">
            <label>Delay</label>
            <span style="color:${delay > 15 ? 'var(--crimson-delay)' : (delay > 3 ? 'var(--amber-reg)' : 'var(--emerald-pro)')};">
              ${delay > 0 ? '+' + Math.round(delay) + ' min' : 'On Time'}
            </span>
          </div>
          <div class="inspector-field">
            <label>Heading</label>
            <span>${Math.round(t.heading || 0)}&deg;</span>
          </div>
          <div class="inspector-field">
            <label>Next Station</label>
            <span>${t.next_station || 'In Transit'}</span>
          </div>
          <div class="inspector-field">
            <label>Corridor</label>
            <span>${t.corridor || 'PATHE Mainline'}</span>
          </div>
          <div class="inspector-field">
            <label>Coordinates</label>
            <span>${Number(t.lat).toFixed(4)}, ${Number(t.lng).toFixed(4)}</span>
          </div>
        </div>
        <div class="sparkline-box">
          <label>Speed Trend (Last 20 Samples)</label>
          ${sparklineSvg}
        </div>
        <div class="inspector-actions">
          <button class="btn-action" onclick="centerOnTrain('${t.id}')">Center</button>
          <button class="btn-action" onclick="toggleSingleTrail('${t.id}')">Trail</button>
          <a class="btn-action btn-cab" href="/cab?train=${t.id}" target="_blank">Cab Stream &rarr;</a>
        </div>`;
    }

    function generateSparkline(points, width, height) {
      if (!points || points.length < 2) {
        return `<svg class="sparkline-svg" viewBox="0 0 ${width} ${height}"></svg>`;
      }
      const max = Math.max(160, ...points);
      const min = 0;
      const step = width / (points.length - 1);
      const coords = points.map((p, i) => {
        const x = i * step;
        const y = height - ((p - min) / (max - min || 1)) * (height - 6) - 3;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      });
      return `
        <svg class="sparkline-svg" viewBox="0 0 ${width} ${height}">
          <polyline fill="none" stroke="#0ea5e9" stroke-width="2" points="${coords.join(' ')}" />
        </svg>`;
    }

    function centerOnTrain(id) {
      const t = trainsMap.get(id);
      if (t && t.lat && t.lng) map.setView([t.lat, t.lng], 12);
    }

    function toggleSingleTrail(id) {
      const trail = trainTrails.get(id);
      if (!trail) return;
      if (trailsLayerGroup.hasLayer(trail)) {
        trailsLayerGroup.removeLayer(trail);
      } else {
        trail.addTo(trailsLayerGroup);
      }
    }

    // 7. Watchdog Conflict Alert System
    function showConflictBanner(alert) {
      const banner = document.getElementById('conflictBanner');
      const text = document.getElementById('conflictText');
      text.innerText = `${alert.type || 'CONFLICT'}: Train ${alert.train_a} & ${alert.train_b} (${alert.distance_km ? alert.distance_km.toFixed(1) + 'km' : ''} on ${alert.corridor || 'Mainline'})`;
      banner.style.display = 'flex';
    }

    function dismissConflictBanner() {
      document.getElementById('conflictBanner').style.display = 'none';
    }

    // 8. Resilient Real-Time SSE Stream Connection Loop
    function connectSSE() {
      const pill = document.getElementById('connPill');
      pill.className = 'status-pill reconnecting';
      pill.innerText = '○ CONNECTING';

      sseSource = new EventSource('/api/stream');

      sseSource.onopen = function() {
        pill.className = 'status-pill';
        pill.innerText = '● LIVE SSE';
        reconnectDelay = 1000;
        console.log('[RAILMON] Live telemetry SSE stream connected.');
      };

      sseSource.addEventListener('init', function(e) {
        try {
          const snapshot = JSON.parse(e.data);
          const activeIds = [];
          if (snapshot.trains && Array.isArray(snapshot.trains)) {
            snapshot.trains.forEach(t => {
              trainsMap.set(t.id, t);
              upsertTrainMarker(t);
              activeIds.push(t.id);
            });
          }
          removeStaleMarkers(activeIds);
          renderRoster();
          if (selectedTrainId && trainsMap.has(selectedTrainId)) {
            renderInspector(trainsMap.get(selectedTrainId));
          }
        } catch (err) {
          console.error('[RAILMON] Error processing init snapshot:', err);
        }
      });

      sseSource.addEventListener('trainPositionsUx', function(e) {
        try {
          const updates = JSON.parse(e.data);
          if (Array.isArray(updates)) {
            updates.forEach(upd => {
              const existing = trainsMap.get(upd.id) || {};
              const merged = Object.assign({}, existing, upd);
              trainsMap.set(upd.id, merged);
              upsertTrainMarker(merged);
            });
            renderRoster();
            if (selectedTrainId && trainsMap.has(selectedTrainId)) {
              renderInspector(trainsMap.get(selectedTrainId));
            }
          }
        } catch (err) {
          console.error('[RAILMON] Error processing position delta:', err);
        }
      });

      sseSource.addEventListener('conflictAlert', function(e) {
        try {
          const alert = JSON.parse(e.data);
          showConflictBanner(alert);
        } catch (err) {
          console.error('[RAILMON] Error processing conflict alert:', err);
        }
      });

      sseSource.onerror = function() {
        pill.className = 'status-pill reconnecting';
        pill.innerText = '○ RECONNECTING';
        if (sseSource) {
          sseSource.close();
          sseSource = null;
        }
        console.warn(`[RAILMON] SSE connection dropped. Reconnecting in ${reconnectDelay}ms...`);
        setTimeout(connectSSE, reconnectDelay);
        reconnectDelay = Math.min(reconnectDelay * 1.5, 30000);
      };
    }
    connectSSE();

    // 9. Periodic Server Health Metrics Polling
    function pollHealthMetrics() {
      fetch('/api/health')
        .then(r => r.json())
        .then(data => {
          if (data.metrics) {
            document.getElementById('metricLatency').innerText = (data.metrics.ingestion_latency_ms || 0).toFixed(1) + 'ms';
            document.getElementById('metricClients').innerText = data.metrics.connected_clients || 1;
          }
        }).catch(() => {});
    }
    setInterval(pollHealthMetrics, 5000);
    pollHealthMetrics();

    function toggleSidebar() {
      const sidebar = document.getElementById('sidebar');
      sidebar.classList.toggle('open');
    }
  </script>
</body>
</html>"""


def get_spa_html(config: Optional[ServerConfig] = None) -> str:
    """Generate complete standalone HTML5 single-page application.

    Injects cartographic tile configuration, Leaflet map bootstrap, SVG locomotive
    marker glyphs, real-time SSE listener, HUD roster table, and conflict banner.
    """
    cfg = config or ServerConfig()
    tile_url = cfg.tile_provider.tile_url
    attribution = cfg.tile_provider.attribution
    tile_provider_str = cfg.tile_provider.value

    # Injected replacement tokens
    html = SPA_HTML_TEMPLATE.replace("__TILE_URL__", tile_url)
    html = html.replace("__ATTRIBUTION__", attribution)
    html = html.replace("__TILE_PROVIDER__", tile_provider_str)

    return html
