# Phase 4 Implementation Plan: Curses TUI Layout & Multi-Pane Rendering

Detailed engineering specification, terminal layout geometry algorithms, curses window hierarchy, color palette mappings, double-buffered rendering engine, station label collision avoidance, and automated test specifications for **Phase 4** of the Hellenic Railways (`railway.gov.gr`) Console TUI Map.

---

## 1. Overview & Objectives

Phase 4 constructs the core visual presentation layer of the terminal map dashboard. It translates atomic state snapshots from the thread-safe state store ([`TrainStateStore`](../tui/state.py)) and rasterized geospatial features from the projection engine ([`GeoProjector`](../tui/geo.py) & [`BrailleCanvas`](../tui/geo.py)) into a cohesive, responsive, multi-pane terminal interface using Python's standard `curses` library.

```text
+---------------------------------------------------------------------------------------------------+
|                        Phase 4 Multi-Pane Terminal Layout Architecture                            |
+---------------------------------------------------------------------------------------------------+

+---------------------------------------------------------------------------------------------------+
| HEADER BAR (1 row): Title, Active Preset, Train Totals (Active/Delayed), Mode, System Clock       |
+--------------------------------------------------------------------+------------------------------+
| MAP VIEWPORT (Left Pane, ~65% Width)                               | ACTIVE TRAINS ROSTER (Right- |
| - Dynamic Bounding Box & HUD Coordinates                           | Top Pane, ~52% Height)       |
| - Braille Raster Canvas (Track vectors via Bresenham)              | - Table Header (No/Spd/Dly)  |
| - Station Glyphs (◉) & Greek Names with Collision Clipping         | - Windowed Scroll View       |
| - Train Heading Glyphs (▲/◥/►/◢/▼/◣/◄/◤) & ID Badges               | - Cursor Pointer (>)         |
| - Breadcrumb Trail Points (·) with Decaying Intensity              | - Delay Badges (Warn/Alert)  |
| - Selected Train Halo Overlay                                      +------------------------------+
|                                                                    | SELECTED TRAIN TELEMETRY     |
|                                                                    | (Right-Bottom Pane)          |
|                                                                    | - Locomotive ID & Status     |
|                                                                    | - Route: Origin -> Dest      |
|                                                                    | - Next Station & Distance    |
|                                                                    | - Speed, Heading, Chainage   |
|                                                                    | - Progress Bar [=====>---]   |
+--------------------------------------------------------------------+------------------------------+
| STATUS BAR & LEGEND (1-2 rows): Keybindings Cheat Sheet, Glyphs Legend, Connection Status Badge    |
+---------------------------------------------------------------------------------------------------+
```

### 1.1 Primary Deliverables

1. **`tui/renderer.py`**:
   - `PaneGeometry`: Screen geometry partitioner calculating coordinate bounds for Header, Map, Roster, Inspector, and Status Bar across arbitrary terminal dimensions.
   - `ColorPalette`: Curses color pair manager with 8-color and 256-color support, providing semantic styles for train categories, delay severity levels, track lines, and highlighted selections.
   - `TUIRenderer`: High-performance drawing engine orchestrating multi-pane rendering, station collision avoidance, directional train glyph overlays, scroll windowing, and route progress meters.
2. **`tui/app.py`**:
   - `TUIApp`: Application coordinator managing curses lifecycle, double-buffered frame ticks (15–20 FPS), non-blocking input sampling, terminal resize handling (`SIGWINCH` / `KEY_RESIZE`), and graceful terminal restoration.
3. **`tests/test_tui_renderer.py`**:
   - Automated unit test suite validating window partitioning math, color pair mapping, station label clipping, heading glyph lookup, roster scroll windowing, and progress bar formatting without requiring an interactive TTY.

### 1.2 Upstream & Downstream Dependencies

- **Upstream Dependencies:**
  - Consumes geospatial projection, coordinate math, and Braille canvas rasterization from [`tui/geo.py`](../tui/geo.py).
  - Consumes station coordinates and rail vector geometries from [`tui/data/stations.json`](../tui/data/stations.json) and [`tui/data/tracks.json`](../tui/data/tracks.json).
  - Consumes atomic, lock-free [`StateSnapshot`](../tui/state.py#L70-L86) instances from [`TrainStateStore`](../tui/state.py#L97-L367).
- **Downstream Dependencies:**
  - Provides the visual rendering foundation and display windows for Phase 5 interactive keystrokes (panning, zooming, train selection, search filtering).
  - Provides the entrypoint `TUIApp.run()` to be exposed by the main CLI in Phase 6 (`--tui` and `--demo` flags in [`train_tracker.py`](../train_tracker.py)).

---

## 2. Screen Geometry & Dynamic Layout Partitioning

### 2.1 Minimum Terminal Dimensions Guard

A full operational dashboard requires sufficient space to display the map, telemetry roster, inspector panel, and headers simultaneously:
- **Minimum Columns ($W_{\min}$):** $80$
- **Minimum Rows ($H_{\min}$):** $24$

When the terminal dimensions fall below $80 \times 24$, the rendering engine suppresses normal pane drawing and renders a centered warning screen:

```text
+-------------------------------------------------------------------+
|                                                                   |
|              ⚠️  TERMINAL WINDOW TOO SMALL                        |
|                                                                   |
|              Current Size:  68 columns x 18 rows                  |
|              Required Size: 80 columns x 24 rows                  |
|                                                                   |
|              Please resize your terminal window...                |
|                                                                   |
+-------------------------------------------------------------------+
```

### 2.2 Mathematical Partitioning Formulas

Given current terminal dimensions $(H, W) = \text{stdscr.getmaxyx()}$:

1. **Header Window (`w_header`):**
   - Row: $0$, Column: $0$
   - Height: $1$, Width: $W$

2. **Status / Footer Window (`w_status`):**
   - Row: $H - 1$, Column: $0$
   - Height: $1$, Width: $W$

3. **Usable Canvas Height:**
   $$H_{\text{usable}} = H - 2$$

4. **Horizontal Split (Map Viewport vs Sidebar):**
   - The map occupies approximately $65\%$ of the terminal width, clamped to ensure the sidebar retains at least $32$ columns and the map retains at least $48$ columns:
   $$W_{\text{map}} = \max\left(48, \min\left(W - 32, \lfloor W \cdot 0.65 \rfloor\right)\right)$$
   $$W_{\text{side}} = W - W_{\text{map}}$$

5. **Vertical Split of Sidebar (Roster vs Inspector):**
   - The Active Trains Roster occupies approximately $52\%$ of the usable sidebar height, ensuring at least $8$ rows for the roster and $7$ rows for the telemetry inspector:
   $$H_{\text{roster}} = \max\left(8, \min\left(H_{\text{usable}} - 7, \lfloor H_{\text{usable}} \cdot 0.52 \rfloor\right)\right)$$
   $$H_{\text{inspector}} = H_{\text{usable}} - H_{\text{roster}}$$

### 2.3 Curses Subwindow Hierarchy

```text
stdscr (Root Screen Buffer: H x W)
│
├── w_header    [subwin(1, W, 0, 0)]
│
├── w_map       [subwin(H_usable, W_map, 1, 0)]
│   └── Canvas Inner: (H_usable - 2) rows x (W_map - 2) cols
│
├── w_roster    [subwin(H_roster, W_side, 1, W_map)]
│   └── Table Inner: (H_roster - 2) rows x (W_side - 2) cols
│
├── w_inspector [subwin(H_inspector, W_side, 1 + H_roster, W_map)]
│   └── Detail Inner: (H_inspector - 2) rows x (W_side - 2) cols
│
└── w_status    [subwin(1, W, H - 1, 0)]
```

All subwindows are derived from `stdscr`. In curses, subwindows share the underlying character cell buffer with `stdscr`, minimizing memory allocations and preventing buffer tearing.

---

## 3. Color Management & Visual Palette

### 3.1 Terminal Capability Detection & Fallback

The styling engine detects color capabilities during initialization:
- Calls `curses.has_colors()`: If `False`, falls back to monochrome attributes (`A_BOLD`, `A_REVERSE`, `A_DIM`).
- Calls `curses.use_default_colors()`: Maps color ID `-1` to the terminal emulator's native transparent background.
- Detects `curses.COLORS >= 256`: Uses refined pastel and gray shades if available; falls back to standard 8 ANSI colors otherwise.

### 3.2 Curses Color Pairs Specification

| Pair ID | Constant Name | Foreground | Background | Attributes | Semantic Usage |
|---|---|---|---|---|---|
| `1` | `PAIR_DEFAULT` | `COLOR_WHITE` | Default (`-1`) | `A_NORMAL` | Standard borders, default body text |
| `2` | `PAIR_HEADER` | `COLOR_CYAN` | Default (`-1`) | `A_BOLD` | Main application title, column headers |
| `3` | `PAIR_BORDER` | `COLOR_BLUE` | Default (`-1`) | `A_DIM` | Box frame outlines and panel dividers |
| `4` | `PAIR_TRACK` | `COLOR_WHITE` | Default (`-1`) | `A_DIM` | Rasterized Braille railway track vectors |
| `5` | `PAIR_STATION` | `COLOR_YELLOW` | Default (`-1`) | `A_BOLD` | Station node glyphs (`◉`) and hub labels |
| `6` | `PAIR_STATION_DIM`| `COLOR_YELLOW` | Default (`-1`) | `A_DIM` | Non-hub station names |
| `7` | `PAIR_TRAIN_ON_TIME`| `COLOR_GREEN` | Default (`-1`) | `A_BOLD` | Train on-time ($\le 3$ min delay) / Intercity |
| `8` | `PAIR_TRAIN_WARN` | `COLOR_YELLOW` | Default (`-1`) | `A_BOLD` | Minor delay ($4 - 10$ min) warning |
| `9` | `PAIR_TRAIN_ALERT`| `COLOR_RED` | Default (`-1`) | `A_BOLD` | Major delay ($> 10$ min) critical alert |
| `10`| `PAIR_TRAIN_PROAST`| `COLOR_CYAN` | Default (`-1`) | `A_NORMAL` | Suburban / Proastiakos train category |
| `11`| `PAIR_TRAIN_FREIGHT`|`COLOR_MAGENTA`| Default (`-1`) | `A_NORMAL` | Freight / Commercial train category |
| `12`| `PAIR_TRAIN_HALTED`| `COLOR_WHITE` | Default (`-1`) | `A_DIM` | Stationary train ($\text{spd} = 0\text{ km/h}$) |
| `13`| `PAIR_ROSTER_SELECT`|`COLOR_BLACK` | `COLOR_CYAN` | `A_BOLD` | Selected roster row cursor (`>`) |
| `14`| `PAIR_TRAIL` | `COLOR_GREEN` | Default (`-1`) | `A_DIM` | Decaying historical GPS breadcrumb dots |
| `15`| `PAIR_STATUS_BAR` | `COLOR_BLACK` | `COLOR_WHITE`| `A_NORMAL` | Bottom status bar inverted strip |
| `16`| `PAIR_CONNECTED` | `COLOR_GREEN` | Default (`-1`) | `A_BOLD` | Live stream connected status indicator |

---

## 4. Multi-Pane Component Specifications (`tui/renderer.py`)

### 4.1 Header Bar Renderer (`render_header`)

Renders a full-width title strip summarizing national network operations:

```text
 HELLENIC RAILWAYS (railway.gov.gr) LIVE MAP | View: PATHE Corridor | Active: 14 | Delayed: 3 (21%) | 15:42:10 
```

**Field Construction:**
- **System Branding:** `"HELLENIC RAILWAYS (railway.gov.gr) LIVE MAP"` in `PAIR_HEADER`.
- **Active Corridor:** Display active preset name (`"All Greece"`, `"PATHE Corridor"`, `"Proastiakos"`, or `"Northern Greece"`).
- **Active Trains Count:** Total active train count $N_{\text{active}}$ from `snapshot.filtered_trains`.
- **Delay Metrics:** Count and percentage of active trains delayed $> 3$ minutes. If delays $> 0$, render in `PAIR_TRAIN_WARN` or `PAIR_TRAIN_ALERT`.
- **System Clock:** Local wall-clock time formatted as `HH:MM:SS`.

### 4.2 Map Viewport Renderer (`render_map`)

The map viewport is the primary visual component. It projects geographic tracks, stations, and live trains onto a curses subwindow.

```text
+-- [Map Viewport: 39.80°N, 22.50°E | Zoom: 1.2x | Follow: #1597] ----------------------------------+
|                                                                                                   |
|   Thessaloniki (Θεσσαλονίκη)                                                                      |
|     ◉===========================                                                                 |
|       \                        \ [IDOMENI]                                                        |
|        \ Platy (Πλατύ)          \                                                                 |
|         ◉===                                                                                      |
|          \  \                                                                                     |
|           \  \ Katerini (Κατερίνη)                                                                |
|            \  ◉                                                                                   |
|             \  \                                                                                  |
|              \  \                                                                                 |
|               \  ◉ Larissa (Λάρισα)                                                               |
|                \  \                                                                               |
|                 \  \  ◄ [1597] (118 km/h, +4m)                                                    |
|                  \  \                                                                             |
|                   \  ◉ Domokos                                                                    |
|                    \  \                                                                           |
|                     \  ◉ Lianokladi (Λιανοκλάδι)                                                  |
|                      \  \                                                                         |
|                       \  ◉ Thiva (Θήβα)                                                           |
|                        \  \                                                                       |
|                         \  ◉ SKA (Acharnes Hub)                                                   |
|                          \  / \                                                                   |
|                           ◉====◉ Athens Central / Airport                                         |
+---------------------------------------------------------------------------------------------------+
```

#### Step-by-Step Drawing Pipeline:

1. **Window Box & Viewport Title:**
   - Draw enclosing box: `win.box()`.
   - Format viewport header: `[Map Viewport: {lat:.2f}°N, {lng:.2f}°E | Zoom: {zoom:.1f}x{follow_tag}]`.

2. **Rasterize Railway Tracks onto Braille Canvas:**
   - Inner dimensions: `inner_h = win_h - 2`, `inner_w = win_w - 2`.
   - Instantiate or clear `BrailleCanvas(width_cells=inner_w, height_cells=inner_h)`.
   - Update `GeoProjector` with current viewport center, zoom, and inner dimensions.
   - Iterate over corridor segments in `tracks.json`:
     - Transform sequence of $(lat, lng)$ coordinates into sub-pixel points $(px, py)$ via `projector.geo_to_subpixel()`.
     - Rasterize track lines using `canvas.draw_polyline(sub_points)`.
   - Blit canvas row strings directly to curses window rows $1 \dots \text{inner\_h}$ with `PAIR_TRACK`.

3. **Overlay Historical Breadcrumbs:**
   - For each train in `snapshot.trains.values()`:
     - For point in `train.trail`:
       - Convert $(lat, lng) \to (col, row)$ via `projector.geo_to_cell()`.
       - If within inner bounds: `win.addch(1 + row, 1 + col, ord('·'), curses.color_pair(PAIR_TRAIL))`.

4. **Overlay Stations & Greek Label Collision Avoidance:**
   - To prevent overlapping text when zoomed out, implement a **Greedy Spatial Bounding Box Collision Engine**:
     - Maintain an occupancy grid `occupied_cells: Set[Tuple[int, int]]`.
     - Prioritize **Hub Stations** (`is_hub=True`) over secondary stations.
     - For each station:
       - Project coordinates to $(col, row) = \text{projector.geo_to_cell}(lat, lng)$.
       - If $(col, row)$ outside inner canvas, skip.
       - Plot station node glyph: `◉` (or `o` in ASCII fallback) in `PAIR_STATION`. Mark $(col, row)$ as occupied.
       - Compute label display name (e.g. `"Larissa (Λάρισα)"`).
       - Compute Greek Unicode terminal display width: Greek characters (`α-ω`, `Α-Ω`) occupy 1 cell width, whereas wide CJK glyphs occupy 2. Use `len(label)` for Greek/Latin text.
       - Candidate label position: $(col + 2, row)$.
       - Check if any cell in range $(col + 2) \dots (col + 2 + \text{width})$ is already occupied.
       - If clear: write label in `PAIR_STATION_DIM` and register cells into `occupied_cells`.
       - If blocked and station is not a hub: suppress label to maintain map readability.

5. **Overlay Live Trains & Directional Heading Glyphs:**
   - For each active train record in `snapshot.filtered_trains`:
     - Project coordinate $(lat, lng) \to (col, row)$.
     - If outside inner bounds, skip.
     - Determine heading glyph based on `track_heading` or `course` ($0^\circ - 360^\circ$):

       ```python
       HEADING_GLYPHS = ("▲", "◥", "►", "◢", "▼", "◣", "◄", "◤")
       # Each octant covers 45 degrees, centered on 0, 45, 90, etc.
       octant = int((heading + 22.5) % 360 // 45)
       glyph = HEADING_GLYPHS[octant]
       ```

     - Determine color pair based on delay and speed:
       - If speed $== 0$: `PAIR_TRAIN_HALTED`
       - Else if delay $> 10$: `PAIR_TRAIN_ALERT`
       - Else if delay $> 3$: `PAIR_TRAIN_WARN`
       - Else if service is Proastiakos: `PAIR_TRAIN_PROAST`
       - Else: `PAIR_TRAIN_ON_TIME`
     - Draw heading glyph at $(col, row)$.
     - If train is the currently selected train:
       - Draw callout tag: `win.addstr(row, col + 2, f"[{train_num}]", curses.A_REVERSE | color)`
       - Or draw following marker `◄ [1597] (118 km/h, +4m)`.

### 4.3 Train Roster Renderer (`render_roster`)

Displays a tabular, scrollable list of active trains with status indicators and delay badges.

```text
+-- ACTIVE TRAINS ROSTER (14) ------------------+
| #   TRAIN  TYPE  SPD   DLY  ST                |
| > 1  1597  IC   118   +4m  RUN                |
|   2  2232  PR   114   +0m  RUN                |
|   3  1598  IC     0   +0m  HLT                |
|   4  1314  PR    92   +1m  RUN                |
|   5  4601  SH     0   +0m  HLT                |
|   6  1599  IC   124   +9m  RUN                |
|   7  2236  PR    86   +0m  RUN                |
+-----------------------------------------------+
```

#### Windowed Scrolling Algorithm:

Given:
- $N$ = total number of filtered trains (`len(snapshot.filtered_trains)`).
- $H_{\text{vis}}$ = visible row capacity = `inner_h - 1` (excluding header row).
- $idx$ = currently selected train index (`snapshot.selected_index`).

The start scroll offset $S$ is calculated to keep the cursor centered:

$$S = \max\left(0, \min\left(idx - \lfloor H_{\text{vis}} / 2 \rfloor, N - H_{\text{vis}}\right)\right)$$

Rows displayed: $i \in [S, \min(N, S + H_{\text{vis}}))$.

#### Column Layout & Alignment:

| Column | Width | Alignment | Description | Example |
|---|---|---|---|---|
| Cursor | 2 cols | Left | `>` for selected, ` ` otherwise | `> ` |
| `#` | 3 cols | Right | Sequence number ($1 \dots N$) | ` 1 ` |
| `TRAIN` | 6 cols | Left | Train number (e.g. `1597`) | `1597  ` |
| `TYPE` | 5 cols | Left | `IC`, `PR`, `SH`, `FR` | `IC   ` |
| `SPD` | 5 cols | Right | Speed in km/h | ` 118 ` |
| `DLY` | 5 cols | Right | Delay in minutes (`+4m`, `+0m`) | ` +4m ` |
| `ST` | 4 cols | Left | Operational status (`RUN`, `HLT`, `ARR`) | `RUN ` |

### 4.4 Selected Train Telemetry Inspector (`render_inspector`)

Displays comprehensive telemetry for the train currently under cursor inspection:

```text
+-- SELECTED TRAIN TELEMETRY -------------------+
| Train No:      1597 (InterCity)               |
| Locomotive:    120010 (Siemens HellasSprinter)|
| Status:        Κανονική (In Service)          |
| Route:         ΑΘΗΝ (07:18) -> ΘΕΣΣ (11:35)   |
| Next Station:  Κατερίνη (ETA: 10:48)          |
| Speed:         118.2 km/h                     |
| Delay:         +4 min (Minor Warning)         |
| Track Heading: 342.5° (NNW)                   |
| Chainage:      248.5 km (Dist Left: 82.3 km)  |
| Progress:      [===========>--------] 62%     |
| Last Seen:     15:42:08 (2s ago)              |
+-----------------------------------------------+
```

#### Route Progress Bar Algorithm:

Given progress percentage $P \in [0.0, 1.0]$:
- Usable progress bar width $W_{\text{bar}} = W_{\text{inner}} - 18$ (allocating space for `"Progress: "` and `" 100%"`).
- Completed fill count: $F = \lfloor P \cdot W_{\text{bar}} \rfloor$.
- Arrow tip: If $0 < F < W_{\text{bar}}$, render `>` at the leading edge.
- Remaining track: Render `-` for $W_{\text{bar}} - F - 1$ cells.
- Progress string: `f"[{'=' * (F - 1)}>{'-' * (W_bar - F)}] {int(P * 100):3d}%"`.

#### Empty State Handling:

When no train is selected ($idx = -1$ or roster is empty):
```text
+-- SELECTED TRAIN TELEMETRY -------------------+
|                                               |
|            No train selected.                 |
|                                               |
|   Use [Tab] or [↑/↓] to select a train        |
|   from the roster above.                      |
|                                               |
+-----------------------------------------------+
```

### 4.5 Status Bar & Legend Renderer (`render_status_bar`)

The bottom status line provides actionable keybinding hints and real-time connection telemetry:

```text
 [q] Quit  [h/j/k/l] Pan  [+/-] Zoom  [Tab] Select  [f] Follow  [/] Filter | ● LIVE (2.4 msg/s) 
```

**Connection Status Indicators:**
- `ConnectionStatus.CONNECTED`: `● LIVE ({rate:.1f} msg/s)` in `PAIR_CONNECTED`.
- `ConnectionStatus.CONNECTING`: `○ CONNECTING...` in `PAIR_DEFAULT`.
- `ConnectionStatus.RECONNECTING`: `⟳ RECONNECTING...` in `PAIR_TRAIN_WARN`.
- `ConnectionStatus.DISCONNECTED` (Demo mode): `● DEMO MODE` in `PAIR_HEADER`.
- `ConnectionStatus.ERROR`: `✕ CONNECTION ERROR` in `PAIR_TRAIN_ALERT`.

---

## 5. Application Lifecycle & Double-Buffered App Loop (`tui/app.py`)

### 5.1 Curses Lifecycle & Terminal Safety

To guarantee terminal restoration (preventing garbled terminals, hidden cursors, or disabled echo on exit), `TUIApp` executes through `curses.wrapper()`:

```python
def main_curses_entrypoint(stdscr: "curses.window", store: TrainStateStore, worker: StreamWorker) -> None:
    """Safely initialize curses environment and run application loop."""
    # 1. Terminal setup
    curses.curs_set(0)            # Hide hardware text cursor
    curses.cbreak()               # Disable line buffering; pass keys immediately
    curses.noecho()               # Do not echo typed characters
    stdscr.keypad(True)           # Enable keypad & function keys (KEY_UP, etc.)
    stdscr.nodelay(True)          # Non-blocking getch()

    # 2. Color initialization
    if curses.has_colors():
        curses.start_color()
        try:
            curses.use_default_colors()
        except curses.error:
            pass  # Fall back to standard color indices if unsupported

    # 3. Instantiate Renderer & Run Event Loop
    app = TUIApp(stdscr, store, worker)
    try:
        app.run_loop()
    finally:
        # Cleanup is guaranteed by curses.wrapper, but explicit worker stop ensures daemon thread terminates
        worker.stop()
```

### 5.2 Double-Buffering & Frame Rate Synchronization

To achieve 15–20 FPS animation without visual tearing:
1. Individual subwindows (`w_header`, `w_map`, `w_roster`, `w_inspector`, `w_status`) are drawn and staged using `win.noutrefresh()`.
2. Staged subwindows do not write directly to the physical terminal display.
3. At the conclusion of the frame, `curses.doupdate()` executes a single optimized terminal write, blitting only dirty cells to stdout.

```text
Frame Tick (50ms / 20 FPS)
│
├── 1. Read Atomic Snapshot: snapshot = store.get_snapshot()
│
├── 2. Sample Keyboard Input: key = stdscr.getch()
│      - Handle 'q' / 'Q' / ESC -> Trigger graceful exit
│      - Handle KEY_RESIZE -> Invalidate layout geometry
│
├── 3. Render Subwindows:
│      - render_header()   -> w_header.noutrefresh()
│      - render_map()      -> w_map.noutrefresh()
│      - render_roster()   -> w_roster.noutrefresh()
│      - render_inspector()-> w_inspector.noutrefresh()
│      - render_status()   -> w_status.noutrefresh()
│
├── 4. Atomic Double-Buffer Flush: curses.doupdate()
│
└── 5. Precise Sleep: Delta time sleep targeting 50ms interval
```

### 5.3 Dynamic Terminal Resizing Protocol (`SIGWINCH` / `KEY_RESIZE`)

When the user resizes their terminal emulator:
1. Curses intercepts `SIGWINCH` and returns `curses.KEY_RESIZE` from `stdscr.getch()`.
2. `TUIApp` executes the resize handler:
   ```python
   def handle_resize(self) -> None:
       """Rebuild window hierarchy and projection buffers following terminal resize."""
       curses.endwin()
       self.stdscr.refresh()
       max_y, max_x = self.stdscr.getmaxyx()

       # Destroy and recreate subwindows to fit new geometry
       self.rebuild_subwindows(max_y, max_x)

       # Update GeoProjector viewport cell dimensions
       self.projector.viewport.width_cells = self.map_w - 2
       self.projector.viewport.height_cells = self.map_h - 2

       # Force complete screen repaint
       self.stdscr.clear()
   ```

---

## 6. Class Design & API Contracts

### 6.1 `PaneGeometry` (`tui/renderer.py`)

```python
@dataclass(frozen=True)
class PaneGeometry:
    """Coordinate boundaries and dimensions for all dashboard panes."""

    term_height: int
    term_width: int
    is_valid: bool  # True if term_width >= 80 and term_height >= 24

    # Header window
    header_y: int
    header_x: int
    header_h: int
    header_w: int

    # Map viewport
    map_y: int
    map_x: int
    map_h: int
    map_w: int

    # Roster pane
    roster_y: int
    roster_x: int
    roster_h: int
    roster_w: int

    # Inspector pane
    inspector_y: int
    inspector_x: int
    inspector_h: int
    inspector_w: int

    # Status bar
    status_y: int
    status_x: int
    status_h: int
    status_w: int

    @classmethod
    def calculate(cls, height: int, width: int) -> "PaneGeometry":
        """Compute responsive pane partitions from terminal dimensions."""
        ...
```

### 6.2 `TUIRenderer` (`tui/renderer.py`)

```python
class TUIRenderer:
    """Multi-pane Curses drawing engine for live train map dashboard."""

    def __init__(self, stations_data: dict, tracks_data: dict):
        """Initialize renderer with static railway network vector data.

        Args:
            stations_data: Parsed stations dictionary from stations.json.
            tracks_data: Parsed track network dictionary from tracks.json.
        """
        ...

    def init_colors(self) -> None:
        """Initialize standard curses color pairs and semantic visual attributes."""
        ...

    def render_all(
        self,
        windows: Dict[str, "curses.window"],
        geometry: PaneGeometry,
        snapshot: StateSnapshot,
        projector: GeoProjector,
        msg_rate: float = 0.0,
    ) -> None:
        """Render all dashboard panes to their respective subwindow buffers.

        Args:
            windows: Dictionary of active curses subwindows ('header', 'map', etc.).
            geometry: Current calculated screen partition bounds.
            snapshot: Atomic point-in-time state snapshot from TrainStateStore.
            projector: GeoProjector configured for the map canvas viewport.
            msg_rate: Ingestion frequency in messages per second.
        """
        ...
```

### 6.3 `TUIApp` (`tui/app.py`)

```python
class TUIApp:
    """Main application loop coordinating state, renderer, and user events."""

    def __init__(
        self,
        stdscr: "curses.window",
        store: TrainStateStore,
        worker: StreamWorker,
        stations_path: Optional[str] = None,
        tracks_path: Optional[str] = None,
        target_fps: float = 20.0,
    ):
        """Initialize application controller.

        Args:
            stdscr: Root curses window from curses.wrapper.
            store: Active thread-safe TrainStateStore.
            worker: Running StreamWorker ingestion daemon.
            stations_path: Path to stations.json.
            tracks_path: Path to tracks.json.
            target_fps: Rendering target frame rate (default: 20 FPS).
        """
        ...

    def run_loop(self) -> None:
        """Run double-buffered frame ticks and handle non-blocking keyboard input."""
        ...
```

---

## 7. Detailed Test Plan (`tests/test_tui_renderer.py`)

Testing curses interfaces in automated CI environments requires testing layout math, formatting utilities, and mock-based window drawing without requiring a live interactive terminal.

| Test Category | Test Method | Verification Target |
|---|---|---|
| **Geometry Partitioning** | `test_pane_geometry_standard_80x24` | Verify exact coordinate boundaries on standard $80 \times 24$ terminal: Map gets 52 cols, Roster gets 28 cols, Status gets row 23. |
| **Geometry Minimum Guard** | `test_pane_geometry_undersized` | Terminal dimensions $< 80 \times 24$ (e.g. $70 \times 20$) sets `is_valid=False`, triggering resize alert screen. |
| **Wide Screen Scaling** | `test_pane_geometry_large_terminal` | On $160 \times 50$ terminal, map width scales to $104$ cols ($65\%$), sidebar gets $56$ cols. |
| **Heading Glyphs Octants** | `test_heading_glyph_octants` | Verify all 8 octants: $0^\circ \to \text{"▲"}$, $45^\circ \to \text{"◥"}$, $90^\circ \to \text{"►"}$, $135^\circ \to \text{"◢"}$, $180^\circ \to \text{"▼"}$, $225^\circ \to \text{"◣"}$, $270^\circ \to \text{"◄"}$, $315^\circ \to \text{"◤"}$. |
| **Delay Severity Classification**| `test_delay_color_mapping` | Verify delay $\le 3\text{m} \to \text{ON\_TIME}$, $4 \dots 10\text{m} \to \text{WARN}$, $> 10\text{m} \to \text{ALERT}$. |
| **Roster Scroll Windowing** | `test_roster_scroll_offset_bounds` | Verify scroll window calculation keeps cursor visible: index 0 starts at offset 0; index 25 in 10-row window offsets to $\approx 20$. |
| **Station Collision Logic** | `test_station_label_collision_suppression`| Two stations with identical coordinates: Hub station label is drawn; secondary station label is suppressed to prevent text overlap. |
| **Progress Bar Construction** | `test_route_progress_bar_string` | Verify progress formatting: $0\% \to \text{"[>-------]   0%"}$, $50\% \to \text{"[====>---]  50%"}$, $100\% \to \text{"[========] 100%"}$. |
| **Greek Text String Width** | `test_greek_unicode_display_width` | Verify Greek station strings (e.g. `"Αθήνα"`) calculate proper character column count without throwing encoding exceptions. |
| **Mock Window Buffer Drawing**| `test_mock_curses_render_frame` | Mock curses subwindows with call interceptors; verify `addstr` and `box` calls execute without errors across all panes. |

---

## 8. Implementation Step-by-Step Checklist

- [x] **Step 1: Layout Geometry & Window Partitioning (`tui/renderer.py`)**
  - Implement `PaneGeometry` with minimum dimension checks ($80 \times 24$) and responsive coordinate partition formulas.
  - Implement undersized terminal fallback banner renderer.
- [x] **Step 2: Curses Color System & Visual Hierarchy (`tui/renderer.py`)**
  - Implement `init_colors()` configuring 16 color pairs with default background support (`use_default_colors()`).
  - Add fallback mappings for terminals lacking 256-color support.
- [x] **Step 3: Multi-Pane Component Renderers (`tui/renderer.py`)**
  - Implement `render_header()` with network metrics, active corridor, and clock.
  - Implement `render_map()` integrating `GeoProjector`, `BrailleCanvas`, station collision avoidance, breadcrumb trails, and heading glyphs.
  - Implement `render_roster()` with table formatting, delay badges, and windowed scrolling.
  - Implement `render_inspector()` with key-value telemetry and route progress bar.
  - Implement `render_status_bar()` with keyboard shortcut cheat sheet and live stream health indicator.
- [x] **Step 4: Double-Buffered Curses Application Loop (`tui/app.py`)**
  - Implement `TUIApp` wrapping execution with `curses.wrapper()`.
  - Implement 20 FPS tick loop with non-blocking `getch()`, `noutrefresh()`, and `curses.doupdate()`.
  - Implement dynamic resize handling (`KEY_RESIZE` / `SIGWINCH`) reconstructing subwindows and updating projector viewport.
  - Implement basic exit keystrokes (`q`, `Q`, `ESC`, `Ctrl+C`).
- [x] **Step 5: Unit & Headless Curses Test Suite (`tests/test_tui_renderer.py`)**
  - Write test cases for `PaneGeometry`, heading glyphs, delay classification, scroll offset, progress bars, and Greek text width.
  - Write mock-based rendering test verifying subwindow draw calls execute without runtime errors.
- [x] **Step 6: Documentation & Link Verification**
  - Run all tests via `python3 -m unittest discover tests`.
  - Verify relative links per `markdown-relative-paths.md`.
  - Update `docs/tui-map-implementation-plan.md` to link directly to this Phase 4 plan.
