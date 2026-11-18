# Architecture & Layer Boundaries

## 1. System Architecture & Layer Hierarchy

`train-tracker-railway-gov-gr` is a Python client and toolkit for tracking real-time train positions, schedules, and railway network telemetry from the official Hellenic Railways (`railway.gov.gr`) portal. Code must adhere strictly to the following layered hierarchy:

```text
+-------------------------------------------------------------+
|                     Presentation Layer                      |
|          (CLI Monitor, Terminal UI, REST API Exporters)     |
+-------------------------------------------------------------+
                               | Consumes structured domain entities
                               v
+-------------------------------------------------------------+
|                  Application & Client Layer                 |
|                                                             |
|   +-----------------------------------------------------+   |
|   |         RailwayGovClient (High-Level Client)        |   |
|   +-----------------------------------------------------+   |
|            |                                |               |
|            v                                v               |
|   +-------------------+          +----------------------+   |
|   | SSE Stream Engine |          | REST API Integrator  |   |
|   | (/api/train-stream)          | (Schedules, Stations)|   |
|   +-------------------+          +----------------------+   |
+-------------------------------------------------------------+
                               | Serializes / Deserializes
                               v
+-------------------------------------------------------------+
|                    Domain & Model Layer                     |
|                                                             |
|   +-------------------+  +-------------------+              |
|   |   TrainPosition   |  |   TrainSchedule   |              |
|   +-------------------+  +-------------------+              |
|   +-------------------+  +-------------------+              |
|   |   StationInfo     |  |   GeoJSONFeature  |              |
|   +-------------------+  +-------------------+              |
+-------------------------------------------------------------+
                               | Network Protocol
                               v
+-------------------------------------------------------------+
|         Remote Upstream: railway.gov.gr Services            |
|       (GNSS/HEPOS Telemetry, SSE Broker, REST Endpoints)    |
+-------------------------------------------------------------+
```

## 2. Layer Boundaries & Invariants

1. **Presentation Layer (`cli.py`, `app.py`, etc.):**
   - Must only interact with domain dataclasses (`TrainPosition`, `Schedule`) and high-level client interfaces.
   - Must not contain raw HTTP request logic, regex parsing, or SSE framing protocols.
   - Handles formatting, terminal rendering, user argument parsing, and output formatting (JSON/table).

2. **Client Layer (`RailwayGovClient`, streaming engines):**
   - Encapsulates all network communication (`urllib`, `requests`, `aiohttp`).
   - Responsible for setting appropriate headers (User-Agent, Referer, Accept).
   - Manages connection lifecycle: timeout enforcement, SSE connection persistence, exponential backoff, and reconnect logic.
   - Converts raw JSON/SSE payloads into validated domain models.

3. **Domain & Model Layer (`TrainPosition`, `Schedule`, etc.):**
   - Independent of network libraries and presentation code.
   - Defined using Python `dataclasses` with explicit type annotations.
   - Implements factory methods (`from_dict`) that validate required fields and handle missing or nullable fields gracefully.

4. **Remote Telemetry Contract:**
   - Always treat coordinates as WGS84 (`lat`, `lng`).
   - Maintain speed in km/h, distances in meters (`chainage_m`, `distanceToDestination_m`), and delays in minutes.
