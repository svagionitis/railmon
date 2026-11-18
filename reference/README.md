# Upstream Reference Assets

This directory contains upstream Next.js JavaScript bundles, CSS stylesheets, and webpack chunks fetched from the official Hellenic Railways train tracking portal (`railway.gov.gr/train-tracker`).

These files serve as the ground-truth reference for:
1. **API Endpoints:** Identifying REST routes (`/api/public/*`, `/api/msd/geo`) and the Server-Sent Events stream (`/api/train-stream`).
2. **Telemetry Schema & Parsing:** Inspecting the data models, event types (`trainPositionsUx`, `trainPositions`, `delayUpdates`, `trainHistory`, `scheduleCards`), and normalization functions (`NR` in `79801.a0cbc95dac2cd2a3.js`).
3. **Frontend Implementation:** Tracking Mapbox GL integration, coordinate handling, speed threshold classification (`speed > 5 km/h`), and service categories (`Επιβατικά`, `Προαστιακός`, `Εμπορικά`, `Συντήρησης`, `Εκτός δρομολογίου`).

## Directory Layout

* `page.js`: Main client-side router component for `/train-tracker`.
* `*.css`: Compiled production stylesheets including Tailwind CSS styling and Mapbox styles.
* `chunks/`: Next.js webpack production chunks containing runtime application logic:
  * `51017.498891c087c24464.js`: Desktop map view, station media, stream video hooks.
  * `50508.7bb72d5062a6c808.js`: Mobile tracker view and layout handlers.
  * `47794.19386b2538c55881.js`: SSE stream connection manager and `/api/msd/geo` GeoJSON loader.
  * `79801.a0cbc95dac2cd2a3.js`: Domain data models, schedule lookup, and train categorization.

See [`docs/architecture.md`](../docs/architecture.md) for detailed architecture and reverse engineering analysis.
