# TUI Feature Enhancements & Expansion Specification

Detailed architectural and technical specification for upcoming interactive, analytical, and geospatial enhancements to the `railmon` Curses Terminal User Interface (`tui/`).

---

## 1. Executive Summary

While the current `railmon` TUI provides a real-time Braille GIS terminal map, live train roster, telemetry inspector, corridor presets (`0`–`3`), camera follow mode (`f`), and interactive filtering (`/`), several high-value features—including two controls already documented in [`README.md`](../README.md#L168-L180)—can be added to complete the operator experience while preserving zero-dependency standard-library purity.

### Feature Summary Matrix

| Tier | Feature ID | Feature Name | Keybinding | Primary Modules Affected |
| :--- | :--- | :--- | :--- | :--- |
| **Tier 1 (Parity)** | **F-1.1** | Dynamic Train Roster Sorting (`ID`, `Speed`, `Delay`) | `s` / `S` | [`tui/state.py`](../tui/state.py), [`tui/app.py`](../tui/app.py), [`tui/renderer.py`](../tui/renderer.py) |
| **Tier 1 (Parity)** | **F-1.2** | Historical Breadcrumb Trail Mode Toggle | `b` / `B` | [`tui/state.py`](../tui/state.py), [`tui/app.py`](../tui/app.py), [`tui/renderer.py`](../tui/renderer.py) |
| **Tier 2 (Inspector)** | **F-2.1** | Live Speed Sparkline & Telemetry Age Indicator | *(Auto)* | [`tui/renderer.py`](../tui/renderer.py), [`tui/state.py`](../tui/state.py) |
| **Tier 2 (Inspector)** | **F-2.2** | WGS84 Coordinate Readout, Next-Station Distance & ETA | *(Auto)* | [`tui/renderer.py`](../tui/renderer.py), [`tui/geo.py`](../tui/geo.py) |
| **Tier 3 (Map & UX)** | **F-3.1** | Terminal Mouse Interaction (Click-to-Select & Scroll Zoom) | Mouse | [`tui/app.py`](../tui/app.py), [`tui/renderer.py`](../tui/renderer.py) |
| **Tier 3 (Map & UX)** | **F-3.2** | Station Departure Board & Proximity Inspector Mode | `m` / Click | [`tui/state.py`](../tui/state.py), [`tui/app.py`](../tui/app.py), [`tui/renderer.py`](../tui/renderer.py) |
| **Tier 3 (Map & UX)** | **F-3.3** | Map Layer Toggles (Station Labels & Corridor Colors) | `v` / `t` | [`tui/app.py`](../tui/app.py), [`tui/renderer.py`](../tui/renderer.py) |
| **Tier 3 (Map & UX)** | **F-3.4** | Dynamic Conformal Map Scale Bar Overlay | *(Auto)* | [`tui/renderer.py`](../tui/renderer.py), [`tui/geo.py`](../tui/geo.py) |
| **Tier 4 (Storage)** | **F-4.1** | Instant In-TUI GeoJSON / CSV Trajectory Snapshot Export | `e` | [`tui/app.py`](../tui/app.py), [`storage/repository.py`](../storage/repository.py) |
| **Tier 4 (Storage)** | **F-4.2** | SQLite Historical DVR Playback & Timeline Scrubbing | `--replay` | [`tui/app.py`](../tui/app.py), [`storage/repository.py`](../storage/repository.py), [`train_tracker.py`](../train_tracker.py) |

---

## 2. Updated Dashboard Layout & Visual Mockup

### 2.1 Enhanced Multi-Pane Console Mockup (ASCII)

```text
+---------------------------------------------------------------------------------------------------+
| RAILMON | HELLENIC RAILWAYS (railway.gov.gr) | View: PATHE Mainline | Active: 14 | Delayed: 3 [●] |
+--------------------------------------------------------------------+------------------------------+
| [Map Viewport: 39.80°N, 22.50°E | Zoom: 1.5x | Trails: ALL]        | [ACTIVE TRAINS (14) | Sort:▼]|
|                                                                    |  #   TRAIN  TYPE  SPD▼  DLY  |
|   Thessaloniki                                                     | > 1  1599   IC    142   +9m  |
|     ◉===========================                                   |   2  1597   IC    118   +4m  |
|       \                        \                                   |   3  2232   PR    114    0m  |
|        \ Platy                  \                                  |   4  1314   PR     92   +1m  |
|         ◉===                     ◉ Kilkis                          |   5  2236   PR     86    0m  |
|          \  \                                                      |   6  1598   IC      0    0m  |
|           \  \ Katerini                                            +------------------------------+
|            \  ◉                                                    | [SELECTED TRAIN TELEMETRY]   |
|             \  \  ·                                                | Train No:     1599 (IC)      |
|              \  \  ·                                               | Locomotive:   120-024        |
|               \  ◉ Larissa                                         | Route:        ΑΘΗΝ -> ΘΕΣΣ   |
|                \  \  ·                                             | Next Station: Λάρισα (14.2km)|
|                 \  \  ▲ ◄ [1599] (142km/h, +9m)                    | Est. Arrival: ~6 min         |
|                  \  \                                              | Speed:        142.0 km/h     |
|                   \  ◉ Domokos                                     | Speed Trend:  ▂▃▅▆▇██▇▇█     |
|                    \  \                                            | Delay:        +9 min (Minor) |
|                     \  ◉ Lianokladi                                | Coords:       39.4821N,22.34E|
|                                                                    | Telemetry:    Updated 2s ago |
|  |---- 25 km ----|                                                 | Progress:     [======>--] 68%|
+--------------------------------------------------------------------+------------------------------+
| [q]Quit [h/j/k/l]Pan [+/-]Zoom [s]Sort [b]Trails [e]Export [m]Stations [f]Follow [/]Filter [?]Help|
+---------------------------------------------------------------------------------------------------+
```

### 2.2 Architectural Data & Event Flow (Mermaid)

```mermaid
flowchart TD
    subgraph Ingestion ["Background Ingestion / Playback"]
        SSE["StreamWorker (Live SSE / Mock)"]
        DVR["ReplayWorker (SQLite Historical DVR)"]
    end

    subgraph State ["State Layer (tui/state.py)"]
        STORE["TrainStateStore (RLock)"]
        SNAP["StateSnapshot (Immutable DTO)"]
    end

    subgraph Controller ["Controller Layer (tui/app.py)"]
        INPUT["Input & Mouse Dispatcher"]
        CAM["GeoProjector & Viewport Kinematics"]
        EXP["Snapshot Exporter (GeoJSON/CSV)"]
    end

    subgraph Presentation ["Presentation Layer (tui/renderer.py)"]
        MAP["Map Pane (Braille + Trails + Scale Bar)"]
        ROSTER["Roster Pane (Sorted Table)"]
        INSP["Inspector Pane (Train Sparkline / Station Board)"]
        STATUS["Status Bar (Prompts & Toast Alerts)"]
    end

    SSE -->|update_positions| STORE
    DVR -->|update_positions| STORE
    STORE -->|get_snapshot| SNAP
    INPUT -->|Sort / Filter / Breadcrumb / Mode| STORE
    INPUT -->|Pan / Zoom / Click| CAM
    INPUT -->|Key 'e'| EXP
    SNAP --> MAP
    SNAP --> ROSTER
    SNAP --> INSP
    SNAP --> STATUS
    CAM --> MAP
```

---

## 3. Tier 1: Documentation Parity & Core Roster Controls

These two features are listed in the Controls Reference table of [`README.md`](../README.md#L168-L180) (`s` and `b`) and should be prioritized first to ensure full parity between documentation and runtime behavior.

---

### F-1.1: Dynamic Train Roster Sorting (`s` / `S`)

#### Problem Statement
[`README.md`](../README.md#L177) documents `s` to *"Cycle train roster sorting (Train ID, Speed, Delay)"*, whereas [`TrainStateStore._compute_filtered_list`](../tui/state.py#L465-L492) currently sorts exclusively by `_natural_sort_key` (Train ID).

#### Technical Design
1. **`RosterSortMode` Enum in [`tui/state.py`](../tui/state.py):**
   ```python
   class RosterSortMode(Enum):
       """Ordering criteria for the active train roster table."""
       TRAIN_ID = auto()   # Ascending natural alphanumeric sort (IC 50 before 1597)
       SPEED = auto()      # Descending speed (fastest first), tie-broken by natural ID
       DELAY = auto()      # Descending delay (most delayed first), tie-broken by natural ID
   ```
2. **State & Snapshot Extension in [`tui/state.py`](../tui/state.py):**
   - Add `self._sort_mode: RosterSortMode = RosterSortMode.TRAIN_ID` to [`TrainStateStore.__init__`](../tui/state.py#L219).
   - Add `sort_mode: RosterSortMode = RosterSortMode.TRAIN_ID` to [`StateSnapshot`](../tui/state.py#L95).
   - Add `cycle_sort_mode() -> RosterSortMode` method on `TrainStateStore` that cycles `TRAIN_ID -> SPEED -> DELAY -> TRAIN_ID` under `self._lock`, preserving the currently selected train ID (`self._selected_id`) and incrementing `self._version`.
   - Update `_compute_filtered_list`:
     - `RosterSortMode.TRAIN_ID`: `key=_natural_sort_key`
     - `RosterSortMode.SPEED`: `key=lambda r: (-(r.position.speed or 0.0), _natural_sort_key(r))`
     - `RosterSortMode.DELAY`: `key=lambda r: (-(r.position.delay or 0.0), _natural_sort_key(r))`
3. **Renderer Header Indicator in [`tui/renderer.py`](../tui/renderer.py#L850-L854):**
   - Highlight the active sort column in the roster table header (`TRAIN▲`, `SPD▼`, or `DLY▼`; in `--ascii` mode: `TRAIN^`, `SPDv`, `DLYv`).
4. **Keybinding in [`tui/app.py`](../tui/app.py#L384):**
   - Bind `ord("s")` and `ord("S")` in `_handle_normal_input` to `self.store.cycle_sort_mode()`.

---

### F-1.2: Historical Breadcrumb Trail Mode Toggle (`b` / `B`)

#### Problem Statement
[`README.md`](../README.md#L176) documents `b` to *"Toggle historical breadcrumb trails"*, whereas [`TUIRenderer.render_map`](../tui/renderer.py#L721-L735) unconditionally renders breadcrumb trails for all trains. On crowded suburban corridors, users often want to view trails only for the selected train or hide them completely.

#### Technical Design
1. **`BreadcrumbMode` Enum in [`tui/state.py`](../tui/state.py):**
   ```python
   class BreadcrumbMode(Enum):
       """Visibility policy for historical breadcrumb trails on the map canvas."""
       ALL = auto()       # Draw breadcrumb dots for all active trains
       SELECTED = auto()  # Draw breadcrumb dots only for snapshot.selected_train
       OFF = auto()       # Hide all historical breadcrumb trails
   ```
2. **State Machine Transition:**

```mermaid
stateDiagram-v2
    [*] --> ALL
    ALL --> SELECTED : Press 'b'
    SELECTED --> OFF : Press 'b'
    OFF --> ALL : Press 'b'
```

```text
+--------------------+   Press 'b'   +--------------------+   Press 'b'   +--------------------+
| BreadcrumbMode.ALL | ------------> |   SELECTED ONLY    | ------------> | BreadcrumbMode.OFF |
+--------------------+               +--------------------+               +--------------------+
          ^                                                                         |
          +------------------------------- Press 'b' -------------------------------+
```

3. **Renderer Integration in [`tui/renderer.py`](../tui/renderer.py#L721-L735):**
   - Check `snapshot.breadcrumb_mode`:
     - If `OFF`: skip step 2 (`Overlay historical breadcrumbs`).
     - If `SELECTED`: render `record.trail` only when `snapshot.selected_train` is not `None` and `record.position.id == snapshot.selected_train.position.id`.
     - If `ALL`: render trails for all visible trains in `snapshot.filtered_trains` (fixing a subtle bug where trails of filtered-out trains were still drawn because the loop iterated over `snapshot.trains.values()` instead of `snapshot.filtered_trains`).
   - Display `[Trails: ALL | SEL | OFF]` in the map viewport title bar when space permits.

---

## 4. Tier 2: Telemetry Inspector & Analytical Enhancements

These enhancements enrich the **Selected Train Telemetry** pane ([`TUIRenderer.render_inspector`](../tui/renderer.py#L903-L992)) using data already present in [`TrainRecord`](../tui/state.py#L51-L92) and [`tui/data/stations.json`](../tui/data/stations.json).

---

### F-2.1: Live Speed Sparkline & Telemetry Age Indicator

#### Overview
Each [`TrainRecord`](../tui/state.py#L51) maintains a `trail: Deque[HistoricalBreadcrumb]` (up to 15 samples) where every [`HistoricalBreadcrumb`](../tui/state.py#L41) stores `(lat, lng, timestamp, speed)`, as well as `record.last_updated` and `record.update_count`. Surfacing these in the Inspector gives instant visibility into whether a train is accelerating, cruising, braking, or experiencing a telemetry drop.

#### Technical Design
1. **Unicode / ASCII Sparkline Generator (`format_speed_sparkline` in [`tui/renderer.py`](../tui/renderer.py)):**
   - **UTF-8 Glyphs:** `("▁", "▂", "▃", "▄", "▅", "▆", "▇", "█")` (8 levels normalized against $V_{\max} = \max(160.0, \max(\text{speeds}))$).
   - **ASCII Fallback (`--ascii`):** `("_", ".", "-", "=", "+", "*", "#", "@")`.
   - Append instantaneous trend arrow: `▲` (accelerating $> +3\text{ km/h}$), `▼` (braking $< -3\text{ km/h}$), or `─` (steady).
2. **Telemetry Age & Stale Warning Row:**
   - Compute `age_sec = max(0, int(time.time() - record.last_updated))`.
   - Format as `"Updated 2s ago (#48)"` (where `#48` is `record.update_count`).
   - Color-code with `PAIR_TRAIN_WARN` if `age_sec > 30` and `PAIR_TRAIN_ALERT` if `age_sec > 120` to alert the user of a dead GPS transponder.

---

### F-2.2: WGS84 Coordinate Readout, Next-Station Distance & ETA

#### Overview
While the map viewport title displays the camera center coordinates, the Inspector does not currently show the selected train's exact latitude/longitude or how far it is from `next_station`.

#### Technical Design
1. **Haversine Distance Helper in [`tui/geo.py`](../tui/geo.py):**
   - Implement `haversine_distance_km(a: GeoPoint, b: GeoPoint) -> float` using standard spherical Earth radius $R = 6371.0\text{ km}$.
2. **Station Lookup Index in [`TUIRenderer.__init__`](../tui/renderer.py#L505):**
   - Build a normalized lookup dictionary mapping Greek (`name_el`), English (`name_en`), and station `id` keys from `self.stations` to their `StationNode`.
3. **Next-Station Distance & Kinematic ETA:**
   - When `pos.next_station` matches a known `StationNode`:
     - Compute straight-line/track distance $d_{\text{km}}$ between `(pos.lat, pos.lng)` and `station.point`.
     - If `pos.speed >= 5.0`, compute $\text{ETA}_{\min} = \text{round}\left(\frac{d_{\text{km}}}{\text{pos.speed}} \times 60\right)$.
     - Render in Inspector: `Next Station: Λάρισα (14.2 km, ~6m)`.
4. **Coordinate Readout Row:**
   - Render `Coordinates: 39.6384°N, 22.4192°E` (or `39.6384d N, 22.4192d E` in `--ascii` mode).

---

## 5. Tier 3: Map Canvas & Interactive UX Features

---

### F-3.1: Terminal Mouse Interaction (Click-to-Select & Scroll Zoom)

#### Overview
Modern terminal emulators (Windows Terminal, iTerm2, Kitty, Alacritty, GNOME Terminal, xterm) report mouse clicks and wheel events through `curses.getmouse()`. Adding mouse support makes spatial exploration and train selection instantaneous while keeping keyboard navigation 100% functional.

#### Technical Design
1. **Mouse Initialization in [`TUIApp.setup_terminal`](../tui/app.py#L199):**
   ```python
   try:
       curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
   except curses.error:
       pass  # Gracefully degrade on terminals without mouse support
   ```
2. **Event Dispatch (`key == curses.KEY_MOUSE`) in [`TUIApp.process_input`](../tui/app.py#L498):**
   - Call `_, mx, my, _, bstate = curses.getmouse()`.
   - **Region 1: Roster Pane (`geometry.roster_x <= mx < roster_x + roster_w` and `roster_y <= my < roster_y + roster_h`):**
     - **Left Click (`BUTTON1_CLICKED | BUTTON1_PRESSED`):** Map clicked row `r_idx = my - (geometry.roster_y + 2)` + active `scroll_offset` to `snapshot.filtered_trains[item_idx]` and call `self.store.select_by_id(...)`.
     - **Double Click (`BUTTON1_DOUBLE_CLICKED`):** Select train and engage follow camera (`self.store.set_follow_mode(True)`).
     - **Scroll Wheel Up / Down (`BUTTON4_PRESSED` / `BUTTON5_PRESSED`):** Cycle train selection backward / forward.
   - **Region 2: Map Pane (`geometry.map_x < mx < map_x + map_w - 1` and `map_y < my < map_y + map_h - 1`):**
     - **Left Click:** Hit-test `(col = mx - map_x - 1, row = my - map_y - 1)` against projected train cells (Manhattan radius $\le 1$ cell). If a train is hit, select it; otherwise, if double-clicked on empty canvas, recenter the viewport on the clicked geographic coordinate via `projector.subpixel_to_geo(col * 2 + 1, row * 4 + 2)`.
     - **Scroll Wheel Up / Down:** Zoom in (`self.zoom(1.25)`) / Zoom out (`self.zoom(0.8)`).

---

### F-3.2: Station Departure Board & Proximity Inspector Mode (`m`)

#### Overview
Allow users to switch the bottom-right Inspector pane between **Train Telemetry Mode** (default) and **Station Proximity Board Mode** (`m` key or clicking a station node `◉` on the map).

#### Visual Layout of Station Inspector Mode

```text
+------------------------------+
| [STATION BOARD: Larissa]     |
| Corridor: PATHE Mainline     |
| Coords:   39.6310°N, 22.423E |
|                              |
| APPROACHING / AT STATION:    |
| TRAIN  DIR  DIST   ETA   DLY |
| 1597   NNW  4.2km  ~2m   +4m |
| 2591   SSE  11.8km ~7m    0m |
| 1598   HLT  0.1km  AT    0m  |
+------------------------------+
```

#### Technical Design
- Add `selected_station_id: Optional[str]` to [`TrainStateStore`](../tui/state.py).
- Pressing `m` cycles focus through major hub stations (or nearest station to current viewport center).
- Compute Haversine distance from the selected `StationNode` to all active trains, filter for trains within $80\text{ km}$ or whose `next_station` / `origin` / `destination` matches the station, and sort by ascending distance.

---

### F-3.3: Map Layer Toggles (`v` Station Labels / `t` Corridor Track Colors)

#### Overview
At low zoom levels (`0.5x`–`1.0x`), station labels can crowd narrow corridors, and monochromatic white Braille tracks make it harder to distinguish suburban lines from the mainline trunk.

#### Technical Design
1. **Station Label Visibility Mode (`v` key):**
   - Cycles through:
     1. `HUBS_AND_SECONDARY` (default: all stations with collision avoidance)
     2. `HUBS_ONLY` (only `stn.is_hub == True` labels shown)
     3. `MARKERS_ONLY` (draw `◉` glyphs without text labels for maximum track visibility)
2. **Corridor-Colored Braille Tracks (`t` key):**
   - Toggle between uniform dim-white tracks (`PAIR_TRACK`) and corridor-colored track layers:
     - `PATHE`: `PAIR_TRACK` (White)
     - `PROASTIAKOS`: `PAIR_TRAIN_PROAST` (Cyan)
     - `NORTHERN` / `THESSALY`: `PAIR_STATION_DIM` (Yellow)

---

### F-3.4: Dynamic Conformal Map Scale Bar Overlay

#### Overview
Because the zoom factor ranges from `0.2x` to `25.0x`, users benefit from a live geographic scale bar in the bottom-left corner of the map viewport showing real-world distance.

#### Technical Design
1. **Scale Calculation in [`GeoProjector`](../tui/geo.py):**
   - Compute kilometers per terminal column cell at current viewport center latitude $\phi_0$:
     $$\text{km\_per\_cell} = \frac{2.0}{S_x \cdot \cos(\phi_0)} \times 111.32$$
   - Snap a target bar width of $12\text{--}18$ cells to the nearest standard cartographic interval in $\{1, 2, 5, 10, 20, 25, 50, 100, 200\}\text{ km}$.
2. **Rendering in [`TUIRenderer.render_map`](../tui/renderer.py):**
   - Draw `|--- 25 km ---|` (or `├──── 25 km ────┤` in UTF-8 mode) at row `max_y - 2`, column `2` inside the map window before overlaying trains.

---

## 6. Tier 4: Historical Storage & Replay Integration

The repository already includes a rich SQLite historical subsystem in [`storage/`](../storage/) ([`storage/repository.py`](../storage/repository.py), [`storage/recorder.py`](../storage/recorder.py)). Integrating it directly with the interactive TUI bridges live monitoring and post-trip analysis.

---

### F-4.1: Instant In-TUI GeoJSON / CSV Trajectory Snapshot Export (`e`)

#### Overview
When observing an interesting delay event or anomaly in the TUI, pressing `e` immediately exports the selected train's trajectory to a timestamped `.geojson` file in the working directory without leaving the dashboard.

#### Technical Design
1. **Dual-Source Trajectory Resolution:**
   - **If `--record` is active** (`snapshot.is_recording and snapshot.recording_db`): Query full recorded trip points for `selected_train.position.train_number` via [`HistoricalRepository.export_geojson`](../storage/repository.py).
   - **If running without `--record` (Live or `--demo`):** Build an RFC 7946 GeoJSON `FeatureCollection` directly from the in-memory `selected_train.trail` (`Deque[HistoricalBreadcrumb]`) and current [`TrainPosition`](../train_tracker.py).
2. **Filename & Security Validation:**
   - Reuse path sanitization rules from [`storage/repository.py`](../storage/repository.py) to write `railmon_train_<train_no>_<YYYYMMDD_HHMMSS>.geojson`.
3. **Transient Status Bar Toast Notification:**
   - Add `toast_message: Optional[str]` and `toast_expiry: float` to [`TUIApp`](../tui/app.py).
   - Display `[✓ Exported railmon_train_1597_20261119.geojson]` in the status bar for 4 seconds.

---

### F-4.2: SQLite Historical DVR Playback & Timeline Scrubbing (`--replay <db>`)

#### Overview
Allow `railmon --tui --replay railway_history.db` to replay recorded historical telemetry through the exact same Curses TUI dashboard, complete with pause, fast-forward (`1x`, `5x`, `15x`, `60x`), and timeline scrubbing (`[` / `]` to jump $\pm 5\text{ minutes}$).

#### Sequence Diagram

```mermaid
sequenceDiagram
    autonumber
    participant User as Operator (Keyboard)
    participant App as TUIApp (tui/app.py)
    participant Replay as ReplayWorker (tui/stream_worker.py)
    participant Repo as HistoricalRepository (storage/repository.py)
    participant Store as TrainStateStore (tui/state.py)

    User->>App: Launch `railmon --tui --replay railmon.db`
    App->>Replay: start(db_path, speed_multiplier=10.0)
    loop Every virtual tick (dt * speed_multiplier)
        Replay->>Repo: Fetch telemetry frame at virtual_timestamp
        Repo-->>Replay: List[TrainPosition]
        Replay->>Store: update_positions(batch)
    end
    User->>App: Press ']' (Scrub +5 minutes)
    App->>Replay: seek_relative(+300s)
    Replay->>Store: clear() & load snapshot at new virtual_timestamp
```

#### ASCII Architecture Diagram

```text
+----------------------------------+       seek / speed        +----------------------------------+
|            TUIApp                | ------------------------> |          ReplayWorker            |
|  Keys: [Space] Pause             |                           |  Virtual Clock: t_cur            |
|        [ / ]   Scrub -/+ 5m      |                           |  Multiplier: 1x / 5x / 15x / 60x |
|        < / >   Speed Multiplier  |                           +----------------------------------+
+----------------------------------+                                            |
                 ^                                                              | SQL Window Query
                 | get_snapshot()                                               v
+----------------------------------+                           +----------------------------------+
|         TrainStateStore          | <------------------------ |       HistoricalRepository       |
|   (In-Memory Active Positions)   |     update_positions()    |     (SQLite WAL Read-Only)       |
+----------------------------------+                           +----------------------------------+
```

---

## 7. Consolidated Controls Reference (Proposed)

| Key | Mode | Action |Status |
| :--- | :--- | :--- | :--- |
| `h` / `j` / `k` / `l` or Arrows | Normal | Pan map viewport West / South / North / East | Existing |
| `+` / `-` | Normal | Zoom In / Zoom Out (`0.2x` to `25.0x`) | Existing |
| `0` / `1` / `2` / `3` | Normal | Apply corridor viewport & filter presets | Existing |
| `Tab` / `n` / `p` | Normal | Cycle active train selection forward / backward | Existing |
| `f` / `F` | Normal | Toggle camera auto-follow on selected train | Existing |
| `c` / `C` | Normal | Clear search filter, corridor filter, and selection | Existing |
| `/` | Normal | Open interactive real-time search filter prompt | Existing |
| `Space` | Normal | Pause / resume telemetry stream or replay clock | Existing |
| `?` | Normal | Toggle interactive Keyboard Controls modal | Existing |
| `q` / `Esc` | Normal | Clean exit from `railmon` TUI | Existing |
| **`s` / `S`** | **Normal** | **Cycle roster sorting (`Train ID` → `Speed` → `Delay`)** | **Proposed (Tier 1)** |
| **`b` / `B`** | **Normal** | **Cycle breadcrumb trails (`All` → `Selected` → `Off`)** | **Proposed (Tier 1)** |
| **`v`** | **Normal** | **Cycle station labels (`All` → `Hubs Only` → `Markers Only`)** | **Proposed (Tier 3)** |
| **`t`** | **Normal** | **Toggle corridor-colored Braille track rendering** | **Proposed (Tier 3)** |
| **`m`** | **Normal** | **Toggle Inspector between Train Telemetry & Station Board** | **Proposed (Tier 3)** |
| **`e`** | **Normal** | **Export selected train trajectory to GeoJSON file** | **Proposed (Tier 4)** |
| **`[` / `]`** | **Replay** | **Scrub historical replay timeline `-5 min` / `+5 min`** | **Proposed (Tier 4)** |
| **`<` / `>`** | **Replay** | **Decrease / Increase historical replay speed (`1x`..`60x`)** | **Proposed (Tier 4)** |

---

## 8. Phased Implementation & Verification Plan

### Phase 1: Tier 1 Parity (`s` Sorting & `b` Breadcrumb Modes)
1. Add `RosterSortMode` and `BreadcrumbMode` enums to [`tui/state.py`](../tui/state.py).
2. Wire keybindings `s`/`S` and `b`/`B` in [`tui/app.py`](../tui/app.py) and update the help modal in [`tui/renderer.py`](../tui/renderer.py#L1117-L1143).
3. Add unit tests in [`tests/test_state.py`](../tests/test_state.py), [`tests/test_tui_app.py`](../tests/test_tui_app.py), and [`tests/test_tui_renderer.py`](../tests/test_tui_renderer.py) verifying sort stability, selection preservation across sort changes, and breadcrumb visibility filtering.

### Phase 2: Tier 2 Inspector Analytics (Sparklines, Telemetry Age, Distance & ETA)
1. Add `haversine_distance_km` to [`tui/geo.py`](../tui/geo.py).
2. Add `format_speed_sparkline` and station distance/ETA lookup to [`tui/renderer.py`](../tui/renderer.py).
3. Verify both UTF-8 and `--ascii` fallback rendering via [`tests/test_tui_renderer.py`](../tests/test_tui_renderer.py).

### Phase 3: Tier 3 Interactive Map & Mouse Support
1. Enable `curses.mousemask` and coordinate hit-testing in [`tui/app.py`](../tui/app.py).
2. Add dynamic scale bar calculation, station label mode `v`, corridor track colors `t`, and station proximity board `m` in [`tui/renderer.py`](../tui/renderer.py).

### Phase 4: Tier 4 Storage Export & Historical Replay
1. Wire `e` in [`tui/app.py`](../tui/app.py) to export GeoJSON snapshots with status bar toast notifications.
2. Implement `ReplayWorker` and `--replay <db>` CLI option in [`train_tracker.py`](../train_tracker.py) and [`tui/stream_worker.py`](../tui/stream_worker.py).
