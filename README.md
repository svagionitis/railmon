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
| `/` | Filter trains by number, name, or station query |
| `?` | Show interactive Keyboard Controls modal |
| `q` | Quit `railmon` |

---

## Repository Architecture

- [`railmon`](./railmon): Executable wrapper script.
- [`train_tracker.py`](./train_tracker.py): Core HTTP/SSE API client, data models, and CLI argument parsing.
- [`tui/`](./tui/): Modular curses terminal user interface:
  - [`tui/geo.py`](./tui/geo.py): EPSG:4326 to Braille canvas (2x4 dot matrix) projection engine.
  - [`tui/state.py`](./tui/state.py): Thread-safe telemetry state store and snapshot generation.
  - [`tui/renderer.py`](./tui/renderer.py): Curses multi-pane layout, Braille drawing, and UTF-8 UI renderer.
  - [`tui/mock_engine.py`](./tui/mock_engine.py): Offline physics simulation and track graph kinematics.
  - [`tui/stream_worker.py`](./tui/stream_worker.py): Background SSE ingestion thread.
  - [`tui/app.py`](./tui/app.py): Application coordinator and event loop.
- [`docs/architecture.md`](./docs/architecture.md): In-depth architectural analysis and reverse engineering report of the `railway.gov.gr` platform.
- [`tests/`](./tests/): Comprehensive automated test suite (100+ tests).

---

## Running Automated Tests

```bash
python3 -m unittest discover tests
```

---

## License

MIT License. Built for transit enthusiasts, travelers, and terminal developers.
