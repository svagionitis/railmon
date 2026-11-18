# Resilience, Circuit Breakers & Headless Fallbacks

This document outlines fault tolerance and resilience requirements for `railmon` when encountering external network instability, unsupported execution environments, or storage constraints.

## 1. Terminal Environment Detection & Headless Fallback
`railmon` must detect when curses cannot initialize and gracefully fall back to a headless or plain-text mode rather than crashing with an unhandled exception:
- **TTY Check:** Validate `sys.stdout.isatty()` before launching the curses event loop.
- **Terminfo Absence Handling:** Catch `curses.error` during `curses.initscr()` (such as missing `TERM` variable in minimal container or CI environments) and output an explanatory message:
  ```python
  try:
      curses.initscr()
  except curses.error as e:
      logger.warning("Terminal environment does not support curses (%s). Falling back to headless mode.", e)
      run_headless()
  ```
- **Headless Mode:** Support running telemetry logging and storage recording without any interactive UI (via `--headless` or `--daemon` flag).

## 2. Network Resilience & Upstream Circuit Breakers
- **Exponential Reconnection Backoff:** When `/api/train-stream` drops or refuses connection, delay retries using exponential backoff with jitter:
  $$\text{delay} = \min(\text{base\_delay} \times 2^{\text{attempt}}, \text{max\_delay}) \pm \text{jitter}$$
  - `base_delay`: 3.0 seconds
  - `max_delay`: 60.0 seconds
- **HTTP 429 (Rate Limited) Handling:** If upstream responds with HTTP 429, parse the `Retry-After` header if present. If absent, back off for a minimum of 30 seconds before retrying.
- **Zero-Crash Payload Tolerance:** If the upstream schema adds, renames, or omits fields (`speed`, `delay_min`, `serviceType`), log a debug warning and continue processing with default values (`None` or `0`). Never allow unexpected fields to terminate the listener.

## 3. Storage Pressure & Database Safeguards
- **Disk Full Prevention:** Telemetry recorders running for weeks or months accumulate large WAL and database files.
- **Configurable Retention:** Provide a rolling data retention policy (e.g., `--retention-days 30`).
- **Disk Free Probing:** Before batch commits, check remaining storage capacity where supported (`shutil.disk_usage`). If free disk space falls below 100 MB, halt recording with a `CRITICAL` log and do not corrupt the database file.
