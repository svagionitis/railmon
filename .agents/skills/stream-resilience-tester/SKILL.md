---
name: stream-resilience-tester
description: >-
  Validates SSE (Server-Sent Events) network resilience, reconnection backoffs,
  malformed JSON payload tolerance, chunk fragmentation, and socket drop recovery.
  Use this skill when modifying network streaming, client timeouts, or upstream reconnection logic.
---

# Stream Resilience Tester

Validates the fault tolerance of `railmon`'s live telemetry ingestion engine against hostile or degraded network conditions without hitting upstream production servers.

## When to Apply
- Modifying `RailwayGovClient.stream_train_positions` or upstream HTTP connection logic in `train_tracker.py`.
- Altering retry intervals, backoff factors, or timeout handlers.
- Testing client behavior under upstream HTTP errors (429 Rate Limited, 502 Bad Gateway, 503 Service Unavailable).

## Test Scenarios & Execution

### 1. Execute Network Boundary Tests
Run the mock stream unit tests:
```bash
pytest tests/test_tracker.py -k "test_stream" -v
```

### 2. Verify Reconnection & Backoff Scenarios

Use mock fixtures to simulate failure modes:

#### Scenario A: Transient Socket Drops (TCP RST)
- **Simulation:** Stream yields 3 valid SSE events, then raises `urllib.error.URLError(ConnectionResetError)`.
- **Expected Behavior:**
  1. The client catches the error.
  2. Sleeps for at least `base_delay` (3.0 seconds).
  3. Re-establishes the connection without terminating the worker thread.
  4. Yields new frames cleanly upon reconnect.

#### Scenario B: Upstream HTTP 429 / 503
- **Simulation:** Mock server responds with HTTP 429 or HTTP 503.
- **Expected Behavior:**
  1. The client logs a warning with the status code.
  2. Respects `Retry-After` if provided, or applies exponential backoff ($3\text{s} \to 6\text{s} \to 12\text{s} \to \dots$).
  3. Avoids rapid spin loops that would hammer the upstream API.

#### Scenario C: Fragmented SSE Chunks & Malformed Payloads
- **Simulation:** SSE chunks split across multiple socket buffers (e.g., `data: {"positi` followed by `ons": [...]}\n\n`), or corrupted JSON `data: {invalid_json}`.
- **Expected Behavior:**
  1. Line buffer reconstructs fragmented lines properly.
  2. Corrupted JSON lines are logged at `DEBUG` and safely skipped without crashing the generator.

#### Scenario D: Clean Shutdown During Blocked Read
- **Simulation:** Ingestion thread is blocked waiting for SSE data on a slow stream when `shutdown_event.set()` is called.
- **Expected Behavior:**
  1. Socket timeout triggers periodically (e.g., 5-10s).
  2. The thread notices the shutdown flag and exits gracefully within 2 seconds.
