---
name: memory-leak-profiler
description: >-
  Profiles Python heap allocations, object counts, and queue depth over long-duration
  telemetry ingestion cycles using tracemalloc. Detects memory leaks and uncollected objects
  before deploying background daemons.
---

# Memory Leak & Daemon Profiler

Profiles heap memory, queue growth, and object lifecycles during simulated high-frequency telemetry ingestion to guarantee zero unbounded memory expansion in 24/7 background recorder processes.

## When to Apply
- Before tagging releases intended for continuous background daemon usage.
- After modifying internal queueing logic in `storage/recorder.py` or cache retention in `tui/state.py`.
- When diagnosing suspected memory leaks or sluggish performance over extended runtimes.

## Profiling Procedure

### 1. Execute Long-Duration Ingestion Simulation
Run a simulated high-throughput ingestion test using `tracemalloc`:
```bash
python -c "
import tracemalloc, time, tempfile, os
from storage.db import Database
from storage.recorder import TelemetryRecorder, RecorderConfig
from scripts.benchmark_storage import _make_bench_position

tracemalloc.start()
with tempfile.TemporaryDirectory() as tmp:
    db_path = os.path.join(tmp, 'profiler.db')
    db = Database(db_path)
    db.init_schema()
    recorder = TelemetryRecorder(db, RecorderConfig(batch_size=100, flush_interval_sec=0.2))
    recorder.start()

    snap1 = tracemalloc.take_snapshot()

    # Ingest 50,000 simulated positions
    for i in range(50000):
        recorder.enqueue(_make_bench_position(train_number=f'T{i%20}', lat=38.0 + (i%100)*0.001))
        if i % 1000 == 0:
            time.sleep(0.01)

    recorder.stop()
    snap2 = tracemalloc.take_snapshot()

    top_stats = snap2.compare_to(snap1, 'lineno')
    print('[Memory Leak Profiler] Top 5 memory increases:')
    for stat in top_stats[:5]:
        print(stat)
"
```

### 2. Verify Evaluation Criteria
- **Heap Growth Slope:** After the initial cache warm-up (first 5,000 frames), total heap memory growth must plateau ($< 5\text{ MB}$ additional retention over 50,000 events).
- **Queue Drain:** The internal `queue.Queue` size must drain to 0 upon calling `recorder.stop()`.
- **Dangling References:** Verify that unreferenced `TrainPosition` instances are reclaimed by garbage collection and not retained in global or module-level lists.
