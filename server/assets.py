"""Embedded web application assets, templates, and GeoJSON cache for railmon serve."""

import json
import logging
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from server.models import ServerConfig

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


def get_spa_html(config: Optional[ServerConfig] = None) -> str:
    """Generate complete standalone HTML5 single-page application.

    Injects cartographic tile configuration, Leaflet map bootstrap, SVG locomotive
    marker glyphs, real-time SSE listener, HUD roster table, and conflict banner.
    """
    cfg = config or ServerConfig()
    tile_url = cfg.tile_provider.tile_url
    attribution = cfg.tile_provider.attribution

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>RAILMON | Hellenic Railways Real-Time Map</title>
  <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" integrity="sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=" crossorigin=""/>
  <style>
    :root {{
      --bg-dark: #0a0e17;
      --card-bg: rgba(16, 24, 39, 0.92);
      --border-color: #1f293d;
      --text-main: #e2e8f0;
      --text-muted: #94a3b8;
      --azure-blue: #0070f3;
      --emerald-green: #10b981;
      --amber-warn: #f59e0b;
      --crimson-alert: #ef4444;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg-dark);
      color: var(--text-main);
      overflow: hidden;
      height: 100vh;
      display: flex;
      flex-direction: column;
    }}
    header {{
      height: 48px;
      background: var(--card-bg);
      border-bottom: 1px solid var(--border-color);
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 0 16px;
      z-index: 1000;
    }}
    .brand {{ font-weight: 700; letter-spacing: 1px; color: var(--azure-blue); display: flex; align-items: center; gap: 8px; }}
    .status-pill {{
      display: inline-flex;
      align-items: center;
      gap: 6px;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
      background: rgba(16, 185, 129, 0.15);
      color: var(--emerald-green);
    }}
    .status-pill.reconnecting {{
      background: rgba(245, 158, 11, 0.15);
      color: var(--amber-warn);
    }}
    .main-container {{ flex: 1; display: flex; position: relative; overflow: hidden; }}
    #map {{ flex: 1; height: 100%; width: 100%; background: #070a0f; }}
    #sidebar {{
      width: 380px;
      background: var(--card-bg);
      border-left: 1px solid var(--border-color);
      display: flex;
      flex-direction: column;
      z-index: 1000;
      backdrop-filter: blur(8px);
    }}
    .roster-header {{ padding: 12px 16px; border-bottom: 1px solid var(--border-color); }}
    .search-box {{
      width: 100%;
      background: #1e293b;
      border: 1px solid #334155;
      padding: 8px 12px;
      border-radius: 6px;
      color: white;
      font-size: 13px;
    }}
    .roster-list {{ flex: 1; overflow-y: auto; list-style: none; }}
    .roster-item {{
      padding: 10px 16px;
      border-bottom: 1px solid var(--border-color);
      display: flex;
      justify-content: space-between;
      cursor: pointer;
      transition: background 0.15s;
    }}
    .roster-item:hover, .roster-item.selected {{ background: #1e293b; }}
    .conflict-banner {{
      position: absolute;
      top: 12px;
      left: 50%;
      transform: translateX(-50%);
      background: var(--crimson-alert);
      color: white;
      padding: 8px 18px;
      border-radius: 8px;
      font-weight: bold;
      z-index: 2000;
      box-shadow: 0 4px 16px rgba(239, 68, 68, 0.4);
      display: none;
    }}
  </style>
</head>
<body>
  <header>
    <div class="brand">RAILMON <span>| Greek Railways Live Telemetry</span></div>
    <div style="display: flex; gap: 12px; align-items: center;">
      <span id="activeCount" style="font-size: 13px;">Active: 0</span>
      <div id="connPill" class="status-pill">● LIVE SSE</div>
    </div>
  </header>
  <div class="main-container">
    <div id="conflictBanner" class="conflict-banner">⚠️ CONFLICT ALERT: Headway Violation</div>
    <div id="map"></div>
    <div id="sidebar">
      <div class="roster-header">
        <input type="text" id="searchInput" class="search-box" placeholder="Filter train ID or station...">
      </div>
      <ul id="rosterList" class="roster-list"></ul>
    </div>
  </div>

  <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js" integrity="sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=" crossorigin=""></script>
  <script>
    const map = L.map('map').setView([39.0742, 22.8243], 7);
    L.tileLayer('{tile_url}', {{
      maxZoom: 18,
      attribution: '{attribution}'
    }}).addTo(map);

    const trainMarkers = new Map();

    // Fetch static tracks & stations
    fetch('/api/tracks').then(r => r.json()).then(data => {{
      L.geoJSON(data, {{ style: {{ color: '#00b4d8', weight: 3, opacity: 0.8 }} }}).addTo(map);
    }}).catch(e => console.warn('Could not load tracks:', e));

    fetch('/api/stations').then(r => r.json()).then(data => {{
      L.geoJSON(data, {{
        pointToLayer: (feature, latlng) => L.circleMarker(latlng, {{ radius: 4, color: '#94a3b8', fillOpacity: 0.8 }})
      }}).addTo(map);
    }}).catch(e => console.warn('Could not load stations:', e));

    // Real-Time SSE Stream Setup
    const sse = new EventSource('/api/stream');
    sse.addEventListener('init', e => {{
      const data = JSON.parse(e.data);
      updateRoster(data.trains || []);
    }});
    sse.addEventListener('trainPositionsUx', e => {{
      const updates = JSON.parse(e.data);
      updatePositions(updates);
    }});
    sse.onerror = () => {{
      document.getElementById('connPill').className = 'status-pill reconnecting';
      document.getElementById('connPill').innerText = '○ RECONNECTING';
    }};
    sse.onopen = () => {{
      document.getElementById('connPill').className = 'status-pill';
      document.getElementById('connPill').innerText = '● LIVE SSE';
    }};

    function updatePositions(trains) {{
      trains.forEach(t => {{
        if (!t.lat || !t.lng) return;
        if (trainMarkers.has(t.id)) {{
          trainMarkers.get(t.id).setLatLng([t.lat, t.lng]);
        }} else {{
          const m = L.circleMarker([t.lat, t.lng], {{ radius: 6, color: '#f59e0b', fillOpacity: 1.0 }}).bindPopup(`Train ${{t.train_number || t.id}}<br>Speed: ${{t.speed}} km/h`);
          m.addTo(map);
          trainMarkers.set(t.id, m);
        }}
      }});
      document.getElementById('activeCount').innerText = `Active: ${{trainMarkers.size}}`;
    }}

    function updateRoster(trains) {{
      const roster = document.getElementById('rosterList');
      roster.innerHTML = '';
      trains.forEach(t => {{
        const li = document.createElement('li');
        li.className = 'roster-item';
        li.innerHTML = `<strong>${{t.train_number || t.id}}</strong> <span>${{t.speed || 0}} km/h</span>`;
        roster.appendChild(li);
      }});
      updatePositions(trains);
    }}
  </script>
</body>
</html>"""
