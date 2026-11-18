# Hellenic Railways (railway.gov.gr) Live Train Tracker Architecture

Detailed architectural analysis and reverse engineering specification of the Hellenic Railways (`railway.gov.gr/train-tracker`) digital monitoring platform, real-time Server-Sent Events (SSE) telemetry feed, HEPOS spatial positioning pipeline, and Python client integration.

---

## 1. Executive Summary & System Overview

The `railway.gov.gr/train-tracker` platform is the official live train tracking and railway network surveillance system operated by the Hellenic Railways Organisation (ΟΣΕ) and the Greek Ministry of Infrastructure and Transport. The platform provides real-time train positioning, travel progress, scheduled versus actual timetables, train delays, and infrastructure GIS visualization across the Greek national rail network.

```text
+-----------------------------------------------------------------------------------+
|                           System Overview Topology                                |
+-----------------------------------------------------------------------------------+

 [Locomotives on Tracks]
   |
   |-- Dual Multi-band GNSS (Galileo + GPS)
   |-- HEPOS Ground Network Differential Corrections (RTK/DGNSS via NTRIP)
   |-- On-board Telemetry Ingestion Unit (OTU)
   v
 [Satellite / Cellular Backhaul]
   |
   |-- Primary: Starlink Low-Latency Satellite Uplink
   |-- Secondary: 4G/5G Cellular Failover
   v
 [National Control Hub - Acharnes Railway Center (SKA)]
   |
   |-- Telemetry Ingestion & Parsing Engine
   |-- GIS Map Matcher & Track Snapper (HEPOS -> OSE Spatial Graph)
   |-- Delay Calculation & Timetable Synchronizer
   v
 [Public Cloud & Edge Distribution (railway.gov.gr)]
   |
   +---> SSE Broker: /api/train-stream (Real-Time Positions & Delays)
   +---> REST Gateway: /api/public/* (Schedules, Stations, GIS Layers)
           |
           +-----------------------------------+
           |                                   |
           v                                   v
   [Official Web Frontend]             [External Python Client]
   (Next.js + Mapbox GL JS)            (../train_tracker.py)
```

---

## 2. HEPOS Positioning & Telemetry Pipeline

### 2.1 The Role of HEPOS
The platform officially integrates the **Hellenic Positioning System (HEPOS)**, operated by the Hellenic Cadastre (*Ελληνικό Κτηματολόγιο*), comprising a network of 98 permanent GNSS reference stations distributed throughout the Greek territory.

* **Centimeter-Level Track Discrimination:** Conventional single-frequency GPS suffers from 3–5 meter horizontal error, which fails to distinguish which specific track a train is traversing along double-track corridors (e.g. Athens–Thessaloniki) or multi-track rail yards.
* **Galileo & RTK Integration:** Dual multi-frequency GNSS receivers mounted on locomotives receive European Galileo (E1/E5) and GPS signals combined with HEPOS Network RTK / DGNSS differential corrections. This yields sub-decimeter positioning accuracy.
* **Track Snapping:** The backend telemetry pipeline maps the corrected coordinates against OSE's spatial vector network, yielding exact chainage distance (`chainage_m`), track segment ID (`railwayId`), and along-track linear distance (`alongTrackDistance`).

### 2.2 Telemetry Ingestion Flow

```text
+-------------------------------------------------------------------------------+
|                      HEPOS & Track Snapping Signal Flow                       |
+-------------------------------------------------------------------------------+

  +-----------------------+           +-----------------------+
  |  Galileo/GPS Satellites|          | HEPOS GNSS Reference  |
  |  (E1/E5, L1/L2 Signals)|          | Network (98 Stations) |
  +-----------------------+           +-----------------------+
              \                                   /
               \                                 / RTK / DGNSS Corrections
                v                               v  (NTRIP / Sat Data)
             +-------------------------------------+
             |  Locomotive Dual GNSS Transceiver   |
             |  Centimeter-Level Position Solution |
             +-------------------------------------+
                                |
                                | High-frequency telemetry packet
                                | (Speed, Azimuth, Altitude, Raw WGS84)
                                v
             +-------------------------------------+
             | Telemetry Backhaul: Starlink Uplink |
             | (Backup: Terrestrial 4G/5G LTE)     |
             +-------------------------------------+
                                |
                                v
             +-------------------------------------+
             | SKA Central Telemetry Server (ΟΣΕ)  |
             +-------------------------------------+
                                |
                                v
             +-------------------------------------+
             |   OSE GIS Track-Snapping Engine     |
             |                                     |
             | - Maps WGS84 point to track vector  |
             | - Computes chainage_m & railwayId   |
             | - Computes alongTrackDistance (m)   |
             | - Determines trackHeading & course  |
             +-------------------------------------+
                                |
                                v
             +-------------------------------------+
             | railway.gov.gr Distribution Broker  |
             | SSE Push: /api/train-stream         |
             +-------------------------------------+
```

---

## 3. Network & Communication Architecture

The backend exposes two primary communication interfaces: Server-Sent Events (SSE) for continuous live train state, and REST HTTP endpoints for schedules, stations, and GIS infrastructure.

### 3.1 Real-Time Streaming: Server-Sent Events (SSE)

* **URL:** `GET https://railway.gov.gr/api/train-stream`
* **Transport:** HTTP/1.1 or HTTP/2 persistent streaming connection.
* **Headers:**
  ```http
  GET /api/train-stream HTTP/1.1
  Host: railway.gov.gr
  Accept: text/event-stream
  Referer: https://railway.gov.gr/train-tracker
  User-Agent: Mozilla/5.0 ...
  ```

#### Multiplexed SSE Event Types

```text
+-------------------------------------------------------------------------------+
|                     SSE Event Multiplexing Pipeline                           |
+-------------------------------------------------------------------------------+

 [HTTP SSE Connection: /api/train-stream]
   |
   |-- event: trainPositionsUx  ===> Smoothed, track-snapped real-time train batch
   |-- event: trainPositions    ===> Raw sensor telemetry batch
   |-- event: delayUpdates      ===> Live delay change notifications
   |-- event: trainHistory      ===> Breadcrumb path history for active trains
   |-- event: scheduleCards     ===> Scheduled train cards & timetable sync
   |-- event: error             ===> Stream state warnings
```

1. **`trainPositionsUx`**: The primary event used by the map and user interface. Contains an array of trains with smoothed positions, track heading, route assignment, and UX delay buffers (typically 10-second smoothing).
2. **`trainPositions`**: Direct telemetry feed containing raw sensor positions prior to UX interpolation.
3. **`delayUpdates`**: Delta updates reporting active train delays in minutes:
   ```json
   {"delays": [{"trainNumber": "1597", "delay": 4}]}
   ```
4. **`trainHistory`**: Trajectory history for playback and tracking breadcrumbs.
5. **`scheduleCards`**: Timetable representations ("ghosts") for trains scheduled to depart shortly.

---

### 3.2 REST API Endpoints

```text
+-------------------------------------------------------------------------------+
|                        REST Service Architecture                              |
+-------------------------------------------------------------------------------+

 railway.gov.gr
   |
   +-- /api/public/schedules/
   |     |-- upcoming                      -> List of today's scheduled runs
   |     +-- {scheduleId}                  -> Full itinerary & stop timings
   |
   +-- /api/public/train-schedules/
   |     +-- by-train-number/{trainNumber} -> Timetable by train number (e.g. 1597)
   |
   +-- /api/public/stations/
   |     +-- {stationId}                   -> Station details & amenities
   |     |-- {stationId}/departures        -> Live departure board (?limit=15)
   |     +-- {stationId}/arrivals          -> Live arrival board (?limit=15)
   |
   +-- /api/public/station-media
   |     +-- ?pathname={path}              -> Station imagery & diagrams
   |
   +-- /api/msd/geo
   |     +-- ?mode=public&types={types}    -> GeoJSON infrastructure features
   |
   +-- /api/public/trains/{trainId}/stream -> Live cab video playlist (HLS)
```

#### Endpoint Specifications

| Endpoint | Method | Key Parameters | Description |
|---|---|---|---|
| `/api/public/schedules/upcoming` | `GET` | None | Returns all passenger train schedules operating today. |
| `/api/public/schedules/{id}` | `GET` | `scheduleId` | Itinerary with scheduled/actual stop times and platform data. |
| `/api/public/train-schedules/by-train-number/{num}` | `GET` | `trainNumber` | Schedule lookup by commercial number (e.g., `1314`, `1597`). |
| `/api/public/stations/{id}` | `GET` | `stationId` | Station name (EL/EN), address, operator, facilities, photos. |
| `/api/public/stations/{id}/departures` | `GET` | `limit` (int) | Upcoming departures with delay status and target stations. |
| `/api/public/stations/{id}/arrivals` | `GET` | `limit` (int) | Upcoming arrivals with origin stations and delays. |
| `/api/msd/geo` | `GET` | `types`, `limit` | GeoJSON features: `station`, `track`, `signal`, `tunnel`, `bridge`. |
| `/api/public/trains/{id}/stream` | `GET` | `trainId` | Returns `{ streamType: "hls", hls: { playlistUrl: "..." } }`. |

---

## 4. Telemetry Data Model

### 4.1 Telemetry Packet Schema

```text
+-------------------------------------------------------------------------------+
|                        TrainPosition Data Structure                           |
+-------------------------------------------------------------------------------+

 TrainPosition
  |-- Identifiers
  |    |-- id                     : String (e.g. "6afc0821f55acaffdcf89c86")
  |    |-- trainNumber            : String (e.g. "2232" or "1597")
  |    |-- locomotiveNumber       : String (e.g. "460103" or "120010")
  |    +-- name                   : String
  |
  |-- Geographic & Motion Telemetry (HEPOS Derived)
  |    |-- lat                    : Float  (e.g. 38.050842, WGS84)
  |    |-- lng                    : Float  (e.g. 23.782449, WGS84)
  |    |-- speed                  : Float  (km/h, e.g. 116.99)
  |    |-- course                 : Float  (degrees, compass azimuth)
  |    |-- trackHeading           : Float  (degrees, track-aligned)
  |    |-- altitude               : Float  (meters above sea level)
  |    +-- headingSource          : String ("track" | "sensor")
  |
  |-- Track Topology & Linear Referencing
  |    |-- corridor               : String (e.g. "PIRAIR", "KIATO", "E85")
  |    |-- chainage_m             : Integer (milestone distance in meters)
  |    |-- railwayId              : String (OSM/OSE railway edge ID)
  |    +-- alongTrackDistance     : Float  (offset along current track segment)
  |
  |-- Operational State & Timetable
  |    |-- serviceType            : String ("Suburban" | "Intercity" | null)
  |    |-- trainType              : String ("passenger" | "freight" | "unassigned")
  |    |-- origin                 : String (e.g. "Άνω Λιόσια")
  |    |-- destination            : String (e.g. "Αεροδρόμιο")
  |    |-- nextStation            : String (e.g. "Νερατζιώτισσα")
  |    |-- delay                  : Integer (minutes)
  |    |-- progress               : Float  (0.0 to 1.0)
  |    |-- distanceToDestination_m: Integer (meters)
  |    +-- timestamp              : String (ISO 8601 UTC)
```

### 4.2 Categorization & Status Mapping Rules

Based on frontend logic in [`reference/chunks/79801.a0cbc95dac2cd2a3.js`](../reference/chunks/79801.a0cbc95dac2cd2a3.js):

```text
+-------------------------------------------------------------------------------+
|                      Train Categorization Decision Tree                       |
+-------------------------------------------------------------------------------+

                     Is scheduleId / scheduleNumber present?
                                  /          \
                                No            Yes
                               /                \
           [Category: "Εκτός δρομολογίου"]       Does corridor == "PIRAIR"
                   (Out of Service)              or serviceType == "Suburban"?
                                                         /          \
                                                       Yes           No
                                                       /               \
                                          [Category: "Προαστιακός"]    Is trainType == "freight"?
                                                  (Suburban)                   /          \
                                                                             Yes           No
                                                                             /               \
                                                                [Category: "Εμπορικά"]   [Category: "Επιβατικά"]
                                                                        (Freight)         (Intercity/Passenger)
```

* **Motion Status:**
  $$\text{status} = \begin{cases} \text{"Κανονική"} & \text{if } \text{speed} > 5.0\text{ km/h} \\ \text{"Στάση"} & \text{otherwise} \end{cases}$$
* **Badge Styling (Tailwind):**
  * `Επιβατικά` (Intercity): `bg-green-100 text-green-700`
  * `Προαστιακός` (Suburban): `bg-cyan-100 text-cyan-700`
  * `Εμπορικά` (Freight): `bg-blue-100 text-blue-700`
  * `Συντήρησης` (Maintenance): `bg-orange-100 text-orange-700`
  * `Εκτός δρομολογίου` (Out of service): `bg-slate-100 text-slate-700`

---

## 5. Python Application Architecture

The Python client implementation is housed in [`train_tracker.py`](../train_tracker.py).

```text
+-------------------------------------------------------------------------------+
|                       Python Client Ingestion Pipeline                        |
+-------------------------------------------------------------------------------+

 [RailwayGovClient]
   |
   |-- 1. Stream Listener (stream_train_positions)
   |       |
   |       +---> Open HTTPS connection to /api/train-stream
   |       +---> Maintain line buffer across chunk boundaries
   |       +---> Detect event: trainPositionsUx blocks
   |       +---> Parse JSON payload into List[TrainPosition]
   |       +---> Yield batch to generator consumer
   |       +---> On disconnect: enforce exponential backoff & reconnect
   |
   |-- 2. REST Query Interface
           |
           +---> get_upcoming_schedules()   -> List[Schedule]
           +---> get_schedule_by_id(id)     -> Detailed itinerary
           +---> get_station_departures(id) -> Live departure board
           +---> get_rail_infrastructure()  -> GeoJSON network
```

### 5.1 Real-Time Streaming Sequence Diagram

```text
+-------------------------------------------------------------------------------+
|                     Real-Time Streaming Sequence Diagram                      |
+-------------------------------------------------------------------------------+

 Client Application           RailwayGovClient               railway.gov.gr (SSE)
        |                            |                                |
        | stream_train_positions()   |                                |
        |--------------------------->| GET /api/train-stream          |
        |                            | (Accept: text/event-stream)    |
        |                            |------------------------------->|
        |                            |                                | HTTP 200 OK
        |                            |<-------------------------------| Content-Type: text/event-stream
        |                            |                                |
        |                            | event: trainPositionsUx        |
        |                            | data: {"positions": [...]}     |
        |                            |<-------------------------------|
        |                            |                                |
        |                            | [Parse & validate TrainPosition]
        | yield List[TrainPosition]  |                                |
        |<---------------------------|                                |
        |                            |                                |
        | [Process / Display]        | event: trainPositionsUx        |
        |                            | data: {"positions": [...]}     |
        |                            |<-------------------------------|
        | yield List[TrainPosition]  |                                |
        |<---------------------------|                                |
        |                            |                                |
        |                            | [Network drop / Reset]         |
        |                            |x------------------------------x|
        |                            |                                |
        |                            | [Sleep reconnect_delay]        |
        |                            |                                |
        |                            | GET /api/train-stream          |
        |                            |------------------------------->|
```

---

## 6. Implementation References

* **Python Source Code:** [`train_tracker.py`](../train_tracker.py)
* **Agent Architecture Guidelines:** [`.agents/rules/architecture.md`](../.agents/rules/architecture.md)
* **Python Language Rules:** [`.agents/rules/python-idioms.md`](../.agents/rules/python-idioms.md)
* **Path Resolution Policy:** [`.agents/rules/markdown-relative-paths.md`](../.agents/rules/markdown-relative-paths.md)
