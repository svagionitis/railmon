# Performance, Resource Bounds & CPU Throttling

`railmon` is designed to run continuously for days or months as a low-overhead monitoring service or interactive dashboard. To prevent runaway memory consumption, CPU spin loops, and thread starvation, all components must enforce explicit resource bounds.

## 1. Bounded Queues & Backpressure Management
- **Never Use Unbounded Queues:** All inter-thread communication queues must specify an explicit capacity:
  ```python
  import queue
  telemetry_queue = queue.Queue(maxsize=10000)
  ```
- **Drop / Backpressure Policy:** When high-volume burst traffic or disk I/O stalls cause the queue to reach `maxsize`:
  - Do not block the network reader thread indefinitely with a blocking `queue.put()`.
  - Use `queue.put_nowait()` or bounded timeouts.
  - If the queue is saturated, log a rate-limited `WARNING` and drop redundant/older frames (or drop stationary train updates first) rather than causing an Out-Of-Memory (OOM) crash.

## 2. CPU Throttling & Frame Rate Caps
- **No Tight Event Loops:** Background ingestion and UI render threads must never run in an unthrottled `while True` loop without sleep or blocking I/O.
- **TUI Frame Rate Cap:** The interactive dashboard must limit refresh rates to a maximum of **10 to 15 frames per second (FPS)**:
  - Configure `curses.halfdelay(1)` or use non-blocking `getch()` paired with an explicit sleep interval:
    ```python
    time.sleep(0.05)  # 20 FPS maximum frame budget
    ```
- **Idle State CPU Usage:** When no telemetry frames are arriving and no user input is received, idle CPU consumption must remain below **1% of a single CPU core**.

## 3. Memory Bounds & Object Retention
- **In-Memory Cache Expiry:** In-memory tracking structures (e.g., active train dictionaries, trail history caches) must implement an eviction or expiration strategy:
  - Stale trains with no telemetry received for over 30 minutes must be marked inactive and purged from the hot rendering map.
  - Trail coordinates per train must be capped (e.g., maximum 50 historical trail points per locomotive).
- **Avoid Circular References:** Data models (`TrainPosition`) should avoid storing references back to parent engines or views that prevent Python's generational garbage collector from reclaiming memory.
