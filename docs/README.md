# Railmon Documentation Directory Index

This directory contains the complete technical documentation, architectural specifications, implementation plans, and incident post-mortems for `railmon` (Hellenic Railways real-time train tracker & geospatial monitor).

---

## Directory Organization

```
docs/
├── architecture/      # High-level system architecture, reverse engineering, safety rules & reviews
├── plans/
│   ├── core/          # Phases 1–6: Geospatial projection, simulator, SSE pipeline, TUI renderer
│   ├── storage/       # Phases 1–5: SQLite historical persistence, deadband filtering & analytics
│   ├── topology/      # Track graph calibration, subpixel aspect ratio & coordinate snapping
│   ├── features/      # F-Series: Interactive features (sorting, breadcrumbs, sparklines, DVR)
│   └── fixes/         # P-Series: Priority-graded production fixes
│       ├── p0/        # Critical: Crash prevention, data loss, signals & deadlock mitigation
│       ├── p1/        # High: WAL checkpoints, schema versions, HTTP 429 & terminal fallbacks
│       └── p2/        # Medium: Cleanups, deprecation warnings, AST length & FPS throttling
└── investigations/    # Technical investigations, incident analyses & bug resolutions
```

---

## 1. Architecture & Specifications (`architecture/`)

Core architectural blueprints, reverse-engineering analyses of `railway.gov.gr`, safety guidelines, and production readiness reviews.

| Document | Description |
|---|---|
| [`architecture.md`](./architecture/architecture.md) | Hellenic Railways SSE telemetry stream reverse engineering, data models & system architecture |
| [`railway-gov-gr-api-reference.md`](./architecture/railway-gov-gr-api-reference.md) | Comprehensive `railway.gov.gr` REST, MSD GeoJSON, SSE streaming & auth endpoint specification |
| [`historical-storage-sqlite.md`](./architecture/historical-storage-sqlite.md) | Embedded SQLite persistence manual, WAL concurrent operation, deadband filtering & CLI guide |
| [`misra-autosar-python-safety-guidelines.md`](./architecture/misra-autosar-python-safety-guidelines.md) | High-assurance safety guidelines (MISRA C++, AUTOSAR C++14, NASA JPL) applied to Python |
| [`production-readiness-review.md`](./architecture/production-readiness-review.md) | Comprehensive production readiness audit, resilience findings & prioritized fix backlog |
| [`codebase-architecture-review.md`](./architecture/codebase-architecture-review.md) | Full senior engineering & architecture review (post-F-6–F-10), dependency graphs, P0–P3 findings & remediation roadmap |
| [`tui-feature-enhancements.md`](./architecture/tui-feature-enhancements.md) | Master specification and requirements for interactive Curses TUI feature enhancements |

---

## 2. Implementation Plans (`plans/`)

Detailed engineering plans broken down by subsystem and lifecycle phase.

### Core TUI & Engine Phases (`plans/core/`)

| Document | Focus |
|---|---|
| [`tui-map-implementation-plan.md`](./plans/core/tui-map-implementation-plan.md) | Master implementation roadmap for the curses terminal user interface |
| [`phase-1-geospatial-projection-braille-canvas.md`](./plans/core/phase-1-geospatial-projection-braille-canvas.md) | EPSG:4326 geospatial projection and Unicode Braille 2x4 dot matrix canvas |
| [`phase-2-rail-network-topology-mock-simulator.md`](./plans/core/phase-2-rail-network-topology-mock-simulator.md) | Track geometry graph, station topologies, and offline kinematic simulator |
| [`phase-3-thread-safe-state-store-sse-worker.md`](./plans/core/phase-3-thread-safe-state-store-sse-worker.md) | Thread-safe state synchronizer, background SSE worker, and reconnect backoff |
| [`phase-4-curses-tui-layout-multi-pane-rendering.md`](./plans/core/phase-4-curses-tui-layout-multi-pane-rendering.md) | Multi-pane curses layout, focus switching, train roster, and inspector panes |
| [`phase-5-interactive-controls-following-filtering.md`](./plans/core/phase-5-interactive-controls-following-filtering.md) | Viewport panning, zoom levels, camera follow mode, and corridor filters |
| [`phase-6-verification-automated-tests-cli-integration.md`](./plans/core/phase-6-verification-automated-tests-cli-integration.md) | Automated testing, headless validation, and unified CLI entrypoints |

### Historical Storage Engine Phases (`plans/storage/`)

| Document | Focus |
|---|---|
| [`sqlite-historical-data-plan.md`](./plans/storage/sqlite-historical-data-plan.md) | High-level roadmap for embedded historical persistence subsystem |
| [`storage-phase-1-infrastructure-and-schema.md`](./plans/storage/storage-phase-1-infrastructure-and-schema.md) | Schema design, index optimization, and WAL connection manager (`db.py`) |
| [`storage-phase-2-asynchronous-recorder-and-deadband-filter.md`](./plans/storage/storage-phase-2-asynchronous-recorder-and-deadband-filter.md) | Non-blocking telemetry ingestion daemon and spatial deadband filtering |
| [`storage-phase-3-historical-repository-and-query-engine.md`](./plans/storage/storage-phase-3-historical-repository-and-query-engine.md) | Query repository, punctuality reports, dwell detection, and GeoJSON export |
| [`storage-phase-4-cli-and-system-integration.md`](./plans/storage/storage-phase-4-cli-and-system-integration.md) | CLI command hierarchy (`railmon record`, `railmon history`) & TUI integration |
| [`storage-phase-5-verification-benchmarking-and-documentation.md`](./plans/storage/storage-phase-5-verification-benchmarking-and-documentation.md) | Synthetic workload benchmarks, concurrency validation, and user manual |

### Topology & Cartography Calibration (`plans/topology/`)

| Document | Focus |
|---|---|
| [`tui-step-1-enrich-tracks-and-stations-plan.md`](./plans/topology/tui-step-1-enrich-tracks-and-stations-plan.md) | High-density track vector coordinates and station geographic enrichment |
| [`tui-step-2-conformal-subpixel-aspect-ratio-plan.md`](./plans/topology/tui-step-2-conformal-subpixel-aspect-ratio-plan.md) | Conformal terminal aspect ratio calibration (`2.15` factor) and zoom matrix |
| [`tui-step-3-orthogonal-map-snapping-plan.md`](./plans/topology/tui-step-3-orthogonal-map-snapping-plan.md) | Orthogonal snap engine mapping coordinates to track segments |
| [`tui-step-4-renderer-display-snapping-plan.md`](./plans/topology/tui-step-4-renderer-display-snapping-plan.md) | Display-level snapping and subpixel dot-matrix rendering alignment |
| [`tui-train-track-misalignment-zoom-fix-plan.md`](./plans/topology/tui-train-track-misalignment-zoom-fix-plan.md) | Resolution plan for train icon and track geometry drift under zoom |

### Interactive Feature Plans (`plans/features/`)

| Document | Feature |
|---|---|
| [`f-1-1-roster-sorting-plan.md`](./plans/features/f-1-1-roster-sorting-plan.md) | Dynamic train roster sorting by ID, Speed, and Delay (`s` / `S`) |
| [`f-1-2-breadcrumb-trail-toggle-plan.md`](./plans/features/f-1-2-breadcrumb-trail-toggle-plan.md) | Train historical breadcrumb trails toggle (`b` / `B`) |
| [`f-2-1-speed-sparkline-telemetry-age-plan.md`](./plans/features/f-2-1-speed-sparkline-telemetry-age-plan.md) | Real-time Unicode speed sparkline (` ▂▃▅▆▇`) & telemetry age counters |
| [`f-2-2-coordinates-distance-eta-plan.md`](./plans/features/f-2-2-coordinates-distance-eta-plan.md) | Precise WGS84 coordinates readout, next-station distance & dynamic ETA |
| [`f-3-1-terminal-mouse-interaction-plan.md`](./plans/features/f-3-1-terminal-mouse-interaction-plan.md) | Terminal mouse interaction (click-to-select, wheel zoom & click-to-pan) |
| [`f-3-2-station-departure-board-proximity-inspector-plan.md`](./plans/features/f-3-2-station-departure-board-proximity-inspector-plan.md) | Station departure board & proximity inspector mode (`m` / `M`) |
| [`f-3-3-map-layer-toggles-plan.md`](./plans/features/f-3-3-map-layer-toggles-plan.md) | Map layer visibility toggles (`v` station labels, `t` corridor colors) |
| [`f-3-4-dynamic-conformal-map-scale-bar-plan.md`](./plans/features/f-3-4-dynamic-conformal-map-scale-bar-plan.md) | Live cartographic scale bar adapting to viewport zoom and center latitude |
| [`f-4-1-instant-in-tui-trajectory-export-plan.md`](./plans/features/f-4-1-instant-in-tui-trajectory-export-plan.md) | Instant in-TUI GeoJSON and CSV snapshot export hotkey (`e`) |
| [`f-4-2-sqlite-historical-dvr-playback-plan.md`](./plans/features/f-4-2-sqlite-historical-dvr-playback-plan.md) | Historical DVR playback and timeline scrubbing (`--replay <db>`) |

### Production Resilience & Safety Fixes (`plans/fixes/`)

#### P0 — Critical Plans (`plans/fixes/p0/`)

| Document | Focus |
|---|---|
| [`p0-1-station-dwell-multi-trip-fix-plan.md`](./plans/fixes/p0/p0-1-station-dwell-multi-trip-fix-plan.md) | Multi-trip station dwell collapse bugfix across calendar days |
| [`p0-2-sse-exception-curses-shutdown-fix-plan.md`](./plans/fixes/p0/p0-2-sse-exception-curses-shutdown-fix-plan.md) | Graceful curses shutdown on SSE exception swallowing and buffer corruption |
| [`p0-3-sigterm-sighup-wal-teardown-fix-plan.md`](./plans/fixes/p0/p0-3-sigterm-sighup-wal-teardown-fix-plan.md) | `SIGTERM` / `SIGHUP` handlers and clean WAL checkpoint teardown |
| [`p0-4-tui-pause-stream-worker-fix-plan.md`](./plans/fixes/p0/p0-4-tui-pause-stream-worker-fix-plan.md) | Proper thread pausing in `StreamWorker` during TUI pause mode |
| [`p0-5-sqlite-connection-churn-memory-sharing-fix-plan.md`](./plans/fixes/p0/p0-5-sqlite-connection-churn-memory-sharing-fix-plan.md) | Connection pool churn mitigation and safe cross-thread `:memory:` sharing |
| [`p0-6-unbounded-storage-disk-circuit-breaker-fix-plan.md`](./plans/fixes/p0/p0-6-unbounded-storage-disk-circuit-breaker-fix-plan.md) | Disk-full circuit breaker and zero-data-loss telemetry archival strategy |
| [`p0-7-train-itinerary-schema-and-time-display-fix-plan.md`](./plans/fixes/p0/p0-7-train-itinerary-schema-and-time-display-fix-plan.md) | Timetable schema alignment, station name resolution & Athens timezone |
| [`p0-8-tui-timetable-modal-scrolling-fix-plan.md`](./plans/fixes/p0/p0-8-tui-timetable-modal-scrolling-fix-plan.md) | Timetable modal window scrolling boundaries & page-down overflow |
| [`p0-9-stale-train-pruning-and-screen-warning-clearance-fix-plan.md`](./plans/fixes/p0/p0-9-stale-train-pruning-and-screen-warning-clearance-fix-plan.md) | Stale train eviction (90s cutoff) & screen warning / conflict clearance |
| [`p0-10-telemetry-drop-watchdog-resilience-fix-plan.md`](./plans/fixes/p0/p0-10-telemetry-drop-watchdog-resilience-fix-plan.md) | Telemetry drop watchdog resilience, tunnel deadbands & station layover preemption |
| [`p0-11-sensor-freeze-watchdog-resilience-fix-plan.md`](./plans/fixes/p0/p0-11-sensor-freeze-watchdog-resilience-fix-plan.md) | Sensor freeze watchdog resilience, packet freshness qualification & station crawl filtering |
| [`p0-12-track-conflict-detection-resilience-fix-plan.md`](./plans/fixes/p0/p0-12-track-conflict-detection-resilience-fix-plan.md) | Track conflict watchdog resilience, dynamic corridor snapping & station siding headway suppression |
| [`p0-13-duplicate-train-numbers-disambiguation-fix-plan.md`](./plans/fixes/p0/p0-13-duplicate-train-numbers-disambiguation-fix-plan.md) | Duplicate train numbers disambiguation, multi-unit consist detection & stable roster ordering |
| [`p0-14-shipping-cli-crashes-and-gtfs-from-db-out-dir-fix-plan.md`](./plans/fixes/p0/p0-14-shipping-cli-crashes-and-gtfs-from-db-out-dir-fix-plan.md) | Four shipping CLI bugs (`history bottlenecks`/`segments`/`trips` crashes & silent GTFS-RT `--from-db --out-dir` data loss) |
| [`p0-15-desktop-notification-rce-and-web-spa-xss-fix-plan.md`](./plans/fixes/p0/p0-15-desktop-notification-rce-and-web-spa-xss-fix-plan.md) | Arbitrary command/code execution (RCE) in desktop notifications (`watchdog.py`) & DOM-based XSS + strict CSP in Web SPA (`server/`) |
| [`p0-16-packaging-watchdog-namespace-and-headless-curses-fix-plan.md`](./plans/fixes/p0/p0-16-packaging-watchdog-namespace-and-headless-curses-fix-plan.md) | Broken packaging (`watchdog.py` -> `railmon_watchdog.py` relocation, `pyproject.toml`, `.gitattributes`) & two-layer headless `curses` import crash (`tui/__init__.py`, `tui/theme.py`, `run_tui_mode`) |
| [`p0-17-safety-watchdog-failures-fix-plan.md`](./plans/fixes/p0/p0-17-safety-watchdog-failures-fix-plan.md) | Three systemic safety-watchdog failures: state-aware stale eviction TTL (restoring drop & tunnel alerts), specific-first corridor normalization + family geometric snapping, and live `TrainRecord.last_updated` stale-ghost conflict preemption |
| [`p0-18-storage-durability-data-loss-and-analytics-fix-plan.md`](./plans/fixes/p0/p0-18-storage-durability-data-loss-and-analytics-fix-plan.md) | Five storage durability, data-loss & analytics failures: canonical fixed-width UTC timestamps + index-backed legacy query filter, contiguous run-island F-9.2 segment transit SQL, chunked TOCTOU/PK-safe `archive_before()`, backoff-guarded non-blocking retention archival, and two-phase post-commit `DeadbandFilter` + SQLite lock retry |
| [`p0-19-openmetrics-compliance-label-collisions-and-storage-collector-fix-plan.md`](./plans/fixes/p0/p0-19-openmetrics-compliance-label-collisions-and-storage-collector-fix-plan.md) | OpenMetrics 1.0.0 hard scrape failures (27-family canonical naming/units, `UNTYPED -> unknown`, `# HELP` escaping, RFC 9110 `q=` negotiation), deterministic duplicate train label disambiguation + scrape-wide deduplication, and broken `StorageMetricsCollector` (`repo.get_database_stats()` over read-only SQLite + final `PY-MISRA-03` cleanup) |

#### P1 — High-Priority Plans (`plans/fixes/p1/`)

| Document | Focus |
|---|---|
| [`p1-1-pragma-wal-checkpoint-allowlist-fix-plan.md`](./plans/fixes/p1/p1-1-pragma-wal-checkpoint-allowlist-fix-plan.md) | SQL injection mitigation via mode allowlist in `PRAGMA wal_checkpoint` |
| [`p1-2-pragma-user-version-startup-integrity-fix-plan.md`](./plans/fixes/p1/p1-2-pragma-user-version-startup-integrity-fix-plan.md) | `PRAGMA user_version` validation and startup schema integrity checks |
| [`p1-3-db-permissions-export-path-sanitization-fix-plan.md`](./plans/fixes/p1/p1-3-db-permissions-export-path-sanitization-fix-plan.md) | POSIX `0600` database file permissions and path traversal sanitization |
| [`p1-4-coordinate-bounds-defensive-parsing-fix-plan.md`](./plans/fixes/p1/p1-4-coordinate-bounds-defensive-parsing-fix-plan.md) | Defensive coordinate bounds checking (`19.0°–29.0°E`, `34.0°–42.0°N`) |
| [`p1-5-http-429-retry-after-5xx-resilience-fix-plan.md`](./plans/fixes/p1/p1-5-http-429-retry-after-5xx-resilience-fix-plan.md) | Exponential backoff and RFC 7231 `Retry-After` header parsing |
| [`p1-6-tty-detection-headless-curses-fallback-fix-plan.md`](./plans/fixes/p1/p1-6-tty-detection-headless-curses-fallback-fix-plan.md) | Safe headless environment detection and non-interactive curses fallback |
| [`p1-7-ascii-fallback-safe-init-pair-fix-plan.md`](./plans/fixes/p1/p1-7-ascii-fallback-safe-init-pair-fix-plan.md) | Automatic ASCII fallback for non-UTF-8 terminals and safe `init_pair` cap |
| [`p1-8-logging-hierarchy-rotating-file-handler-fix-plan.md`](./plans/fixes/p1/p1-8-logging-hierarchy-rotating-file-handler-fix-plan.md) | Hierarchical `railmon` logging with size-bounded `RotatingFileHandler` |
| [`p1-9-pyproject-toml-single-source-version-fix-plan.md`](./plans/fixes/p1/p1-9-pyproject-toml-single-source-version-fix-plan.md) | PEP 517/621 `pyproject.toml` packaging and single-source `__version__` |

#### P2 — Medium-Priority Plans (`plans/fixes/p2/`)

| Document | Focus |
|---|---|
| [`p2-1-dead-stale-train-pruning-deadband-cache-fix-plan.md`](./plans/fixes/p2/p2-1-dead-stale-train-pruning-deadband-cache-fix-plan.md) | Eviction of stale trains and bounded deadband state cache |
| [`p2-2-tui-frame-rate-cap-idle-redraw-fix-plan.md`](./plans/fixes/p2/p2-2-tui-frame-rate-cap-idle-redraw-fix-plan.md) | Frame rate throttling and suppression of redundant idle redraw cycles |
| [`p2-3-utcnow-deprecation-warnings-fix-plan.md`](./plans/fixes/p2/p2-3-utcnow-deprecation-warnings-fix-plan.md) | Python 3.12+ `datetime.now(timezone.utc)` migration |
| [`p2-4-boolean-parameters-to-enums-fix-plan.md`](./plans/fixes/p2/p2-4-boolean-parameters-to-enums-fix-plan.md) | Refactoring boolean flags to explicit self-documenting enums |
| [`p2-5-function-name-length-violations-fix-plan.md`](./plans/fixes/p2/p2-5-function-name-length-violations-fix-plan.md) | Renaming functions exceeding 30 characters for NASA JPL rule compliance |

---

## 3. Incident Investigations & Post-Mortems (`investigations/`)

Root cause investigations, reproduction steps, and technical resolutions for observed bugs and regressions.

| Document | Subject |
|---|---|
| [`corridor-presets-filtering-issue.md`](./investigations/corridor-presets-filtering-issue.md) | Corridor preset filter edge cases and train assignment resolution |
| [`telemetry-anomalies-and-duplicate-train-numbers-investigation.md`](./investigations/telemetry-anomalies-and-duplicate-train-numbers-investigation.md) | Telemetry drops, sensor freeze, track conflicts & duplicate train numbers |
| [`train-itinerary-schema-and-time-display-issue.md`](./investigations/train-itinerary-schema-and-time-display-issue.md) | Train timetable hex station IDs and 2026 timestamp display resolution |
| [`tui-breadcrumb-corrupted-characters-investigation.md`](./investigations/tui-breadcrumb-corrupted-characters-investigation.md) | Corrupted breadcrumb glyphs (`M-7`, `7`) in non-UTF-8 console environments |
| [`tui-timetable-modal-scrolling-issue.md`](./investigations/tui-timetable-modal-scrolling-issue.md) | Timetable inspector modal scrolling boundaries and line-wrap handling |
| [`tui-train-track-misalignment-zoom-issue.md`](./investigations/tui-train-track-misalignment-zoom-issue.md) | Visual misalignment between train vectors and track paths under viewport zoom |
| [`tui-windows-zoom-and-deque-mutation-issue.md`](./investigations/tui-windows-zoom-and-deque-mutation-issue.md) | Windows Terminal zoom hotkeys and multithreaded `deque` iteration crash |
