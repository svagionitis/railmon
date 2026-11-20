# railmon

> **Real-Time Terminal Train Tracker & Geospatial Monitor for Hellenic Railways (`railway.gov.gr`)**

`railmon` is a high-performance terminal utility and Python client for tracking passenger, suburban, and freight trains across the Greek national rail network in real time. It connects directly to the official Hellenic Railways Organisation (ΟΣΕ / Hellenic Train) Server-Sent Events (SSE) telemetry stream and renders a live, interactive Braille GIS terminal map alongside real-time speeds, delays, and locomotive statistics.

---

## Features

- **Braille Terminal Map**: Sub-character GIS projection rendering the Greek railway network, corridors, stations, and active train vectors with direction-aware glyphs (`▲`, `▼`, `◄`, `►`, `▲/►`, etc.).
- **Live SSE Telemetry**: Consumes live updates from `railway.gov.gr/api/train-stream` with automatic exponential reconnection and thread-safe state synchronization.
- **Interactive TUI Dashboard**:
  - **Live Roster**: Searchable train list with real-time delay indicators, speed, and origin/destination info.
  - **Telemetry Inspector**: Deep inspection of locomotive ID, HEPOS spatial coordinates, speed, track heading, chainage, and corridor progress.
  - **Follow Camera Mode (`f`)**: Locks viewport center on a selected train as it traverses tracks across Greece.
  - **Corridor Filtering (`c`)**: Filter by Athens–Thessaloniki Mainline, Proastiakos Suburban, Peloponnese, Northern Greece, or View All.
  - **Historical Breadcrumb Trails (`b`)**: Visualize train journey paths over time.
  - **Dynamic Conformal Map Scale Bar**: Live bottom-left cartographic scale bar (`├──── 25 km ────┤` / `|---- 25 km ----|`) that automatically adapts to viewport zoom (`0.2x`–`25.0x`) and center latitude.
- **Historical Telemetry Recording & Analytics**:
  - Embedded zero-dependency SQLite persistence with Write-Ahead Logging (WAL) for non-blocking concurrent reading and writing.
  - Spatial deadband filtering reduces database footprint by 40–60% by suppressing uninformative stationary coordinates while preserving motion fidelity.
  - Headless ingestion daemon mode (`railmon record`) for server or background recording.
  - Rich CLI analytics (`railmon history`) for punctuality reports, dwell profiling, trip summaries, and RFC 7946 GeoJSON / CSV trajectory export.
- **Offline Simulation Mode (`--demo`)**: Full physics-based kinematic mock engine simulating trains traversing real track geometry without requiring internet access.
- **Lightweight & Dependency-Free**: Pure standard library implementation on Linux/macOS (Python 3.10+ `curses`, `urllib`, `dataclasses`, `json`). On Windows, `windows-curses` provides native console support.

---

## Installation & Quick Start

Ensure you have Python 3.10 or higher installed:

### Linux / macOS

```bash
# Clone the repository
git clone https://github.com/svagionitis/railmon.git
cd railmon

# Make railmon executable
chmod +x railmon

# Pure standard library - no external packages required!
# (On minimal/stripped distributions like Debian minimal or Alpine, ensure python3-curses is installed:
#  Debian/Ubuntu: sudo apt install python3-curses python3-sqlite3)
```

### Windows (PowerShell / Command Prompt)

```powershell
# Clone the repository
git clone https://github.com/svagionitis/railmon.git
cd railmon

# Install windows-curses for interactive TUI support
pip install -r requirements.txt
```

> **Note for Windows Users:**
> It is strongly recommended to run `railmon` inside **[Windows Terminal](https://aka.ms/terminal)**.
> If running in a standalone PowerShell / Command Prompt window (`conhost`), ensure your console font is set to **Cascadia Mono**, **Cascadia Code**, or a Unicode font with Braille glyph coverage (e.g., right-click window title bar -> *Properties* -> *Font* -> select *Cascadia Mono*). Otherwise, Braille track patterns (`U+2800`–`U+28FF`) and directional glyphs may appear as empty boxes (`□`).

### Package Installation & Build (`pyproject.toml`)

`railmon` conforms to modern Python packaging standards (PEP 517 / PEP 518 / PEP 621) via [`pyproject.toml`](./pyproject.toml). You can install it directly into your environment or build reusable wheel and source distributions.

#### Install from Local Source
```bash
# Install railmon as a CLI command into your active Python environment
pip install .

# Or install in an isolated application environment with pipx
pipx install .

# Install in editable mode for development (with testing and linting tools)
pip install -e ".[dev]"
```

After installation, the `railmon` binary is available directly on your `$PATH`:
```bash
railmon --version
railmon --tui
```

#### Build Wheel and Source Distribution
To assemble standalone distribution artifacts (`.whl` and `.tar.gz`) for release or offline deployment:

```bash
# 1. Install standard build frontend (if not already installed)
pip install build

# 2. Build the wheel and sdist archives
python -m build

# 3. Inspect the generated archives in dist/
ls -lh dist/
# Output:
#   dist/railmon-1.0.0-py3-none-any.whl
#   dist/railmon-1.0.0.tar.gz

# 4. Install the built wheel artifact
pip install dist/railmon-1.0.0-py3-none-any.whl
```

---

### Launch Interactive Terminal Dashboard (TUI)

```bash
# Linux / macOS
./railmon --tui
./railmon --tui --demo

# Windows (Command Prompt or PowerShell)
railmon --tui
railmon --tui --demo
# or
python railmon --tui
```

### Console Stream Mode

```bash
# Plain text streaming output
./railmon              # Linux / macOS
railmon                # Windows

# Offline mock stream
./railmon --demo       # Linux / macOS
railmon --demo         # Windows

# View today's scheduled trips
./railmon schedules    # Linux / macOS
railmon schedules      # Windows
```

### Historical Recording & Terminal Analytics

```bash
# Record live telemetry as a headless daemon
./railmon record

# Record to custom database with deadband compression
./railmon record --db railway_2026.db --deadband

# Run interactive TUI with persistent background recording ([● REC] badge)
./railmon --tui --record

# Inspect recorded database statistics
./railmon history stats

# View corridor punctuality and average delay reports
./railmon history punctuality

# Inspect chronologically ordered telemetry for a specific train
./railmon history train IC52 --limit 50

# Analyze station dwell times and halts
./railmon history dwell IC50 --min-dwell 30

# Export train journey as RFC 7946 GeoJSON (ready for QGIS, Kepler.gl)
./railmon history export IC52 --format geojson --out ic52.geojson
```

---

## Controls Reference (TUI)

| Key | Action |
|-----|--------|
| `Tab` / `Shift-Tab` | Switch active focus between Panes (Map / Train Roster / Details) |
| `h` / `j` / `k` / `l` or Arrows | Pan map viewport West / South / North / East |
| `+` / `-` | Zoom In / Zoom Out (0.2x to 25.0x) |
| `f` | Toggle Camera Follow mode on selected train |
| `c` | Cycle railway corridor filter (Mainline, Suburban, etc.) |
| `b` | Toggle historical breadcrumb trails |
| `s` | Cycle train roster sorting (Train ID, Speed, Delay) |
| `m` / `M` | Cycle Station Departure Board & Proximity Inspector / Return to train view |
| `/` | Filter trains by number, name, or station query |
| `?` | Show interactive Keyboard Controls modal |
| `q` | Quit `railmon` |

---

## Repository Architecture

- [`railmon`](./railmon): Executable wrapper script.
- [`pyproject.toml`](./pyproject.toml): PEP 517 / PEP 518 / PEP 621 packaging specification, dynamic versioning, and unified tool configuration.
- [`train_tracker.py`](./train_tracker.py): Core HTTP/SSE API client, data models, and CLI argument parsing.
- [`tui/`](./tui): Modular curses terminal user interface:
  - [`tui/geo.py`](./tui/geo.py): EPSG:4326 to Braille canvas (2x4 dot matrix) projection engine.
  - [`tui/state.py`](./tui/state.py): Thread-safe telemetry state store and snapshot generation.
  - [`tui/renderer.py`](./tui/renderer.py): Curses multi-pane layout, Braille drawing, and UTF-8 UI renderer.
  - [`tui/mock_engine.py`](./tui/mock_engine.py): Offline physics simulation and track graph kinematics.
  - [`tui/stream_worker.py`](./tui/stream_worker.py): Background SSE ingestion thread.
  - [`tui/app.py`](./tui/app.py): Application coordinator and event loop.
- [`storage/`](./storage): Embedded SQLite historical persistence subsystem:
  - [`storage/db.py`](./storage/db.py): High-performance WAL connection coordinator.
  - [`storage/recorder.py`](./storage/recorder.py): Asynchronous background batch persistence daemon.
  - [`storage/filter.py`](./storage/filter.py): Deadband filtering and coordinate compression engine.
  - [`storage/repository.py`](./storage/repository.py): Analytical query engine, DTOs, and GeoJSON/CSV exporters.
  - [`storage/cli_formatters.py`](./storage/cli_formatters.py): Zero-dependency tabular terminal reporting formatters.
- [`scripts/`](./scripts): Development and profiling utilities:
  - [`scripts/benchmark_storage.py`](./scripts/benchmark_storage.py): Ingestion throughput, read latency, and deadband benchmarking harness.
- [`docs/historical-storage-sqlite.md`](./docs/architecture/historical-storage-sqlite.md): Complete schema reference, CLI guide, and SQL analytical recipe cookbook.
- [`docs/architecture.md`](./docs/architecture/architecture.md): In-depth architectural analysis and reverse engineering report of the `railway.gov.gr` platform.
- [`docs/tui-feature-enhancements.md`](./docs/architecture/tui-feature-enhancements.md): Interactive, analytical, and geospatial Curses TUI feature enhancement specification.
- [`tests/`](./tests): Comprehensive automated test suite (380+ tests).

---

## Running Automated Tests

```bash
python3 -m unittest discover tests
```

---

## License

MIT License. Built for transit enthusiasts, travelers, and terminal developers.
