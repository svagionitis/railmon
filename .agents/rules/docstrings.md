# Python Documentation & Docstring Requirements

Every public module, class, method, and free function must be documented using PEP 257 / Google-style docstrings.

## 1. Function / Method Template (Google Style)

```python
def get_station_departures(self, station_id: str, limit: int = 15) -> List[Dict[str, Any]]:
    """Fetch the live departure board for a specific railway station.

    Args:
        station_id: Unique station identifier (e.g., '6928c3c150ab5bc72b39ce7b').
        limit: Maximum number of upcoming departures to return. Defaults to 15.

    Returns:
        List of departure dictionaries containing trainNumber, destination, scheduledTime,
        status, and delay in minutes.

    Raises:
        urllib.error.HTTPError: If the upstream API returns an HTTP error code (e.g. 404).
        urllib.error.URLError: If network connectivity fails.
    """
```

## 2. Class Template

```python
class RailwayGovClient:
    """Client for the Hellenic Railways (railway.gov.gr) real-time tracking APIs.

    Provides high-level methods to fetch schedules, station timetables,
    rail infrastructure GeoJSON layers, and consume Server-Sent Events (SSE)
    position streams.

    Attributes:
        base_url: Root API endpoint (defaults to 'https://railway.gov.gr').
        ssl_ctx: SSL context configured for secure HTTPS requests.
    """
```

## 3. Telemetry Units & Conventions
Always document physical units explicitly in docstrings and dataclass field annotations:
- **Speeds:** Always in kilometers per hour (`km/h`).
- **Headings & Bearings:** Compass azimuth in degrees (`0.0` to `360.0`).
- **Distances & Offsets:** Track distances in meters (`m`).
- **Delays:** Elapsed schedule offset in minutes.
- **Coordinates:** WGS84 decimal degrees (`lat`, `lng`).
