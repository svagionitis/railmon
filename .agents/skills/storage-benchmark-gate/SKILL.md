---
name: storage-benchmark-gate
description: >-
  Executes the SQLite telemetry storage performance and data integrity benchmarks,
  evaluating write throughput, deadband suppression efficiency, analytical query latencies,
  and WAL checkpoint behavior. Use this skill when modifying storage schemas, indexing,
  or ingestion transaction logic.
---

# Storage Benchmark Gate

Evaluates the persistence layer of `railmon` to prevent performance regressions in high-volume telemetry ingestion, deadband filtering, and time-series query latency.

## When to Apply
- After modifying database schema or indexing in `storage/schema.py`.
- After altering transaction batching or deadband filtering in `storage/recorder.py` or `storage/filter.py`.
- Prior to major version releases to establish empirical performance baselines.

## Execution Procedure

### 1. Run the Storage Benchmark Suite
Execute the benchmarking harness against an isolated temporary database:
```bash
python scripts/benchmark_storage.py --records 5000 --json storage_bench_results.json
```

### 2. Verify Performance Thresholds

Check the generated results against the production thresholds:

1. **Write Throughput (Batch Size >= 100):**
   - **Target:** $\ge 5,000$ rows / second.
   - **Critical Floor:** $\ge 2,500$ rows / second.
2. **Deadband Filter Suppression:**
   - **Stationary Suppression:** $\ge 85\%$ of redundant stationary frames suppressed.
   - **Overall Storage Footprint Reduction:** $\ge 40\%$ reduction.
3. **Analytical Read Query Latencies (on 5,000+ rows):**
   - `get_active_positions`: mean $\le 5.0\text{ ms}$, p95 $\le 10.0\text{ ms}$.
   - `get_train_trajectory`: mean $\le 10.0\text{ ms}$, p95 $\le 20.0\text{ ms}$.
   - `get_corridor_statistics`: mean $\le 15.0\text{ ms}$, p95 $\le 30.0\text{ ms}$.

### 3. Run Benchmark Unit Tests
Execute the automated regression assertions in the test suite:
```bash
pytest tests/test_storage_benchmarks.py -v
```

### 4. Database Integrity Verification
Verify that WAL mode and database integrity remain pristine after high-load operations:
```bash
python -c "import sqlite3; conn = sqlite3.connect('storage/railmon.db'); print(conn.execute('PRAGMA integrity_check').fetchall()); print(conn.execute('PRAGMA journal_mode').fetchall())"
```
**Pass Criteria:**
- `[('ok',)]` returned for `integrity_check`.
- `wal` returned for `journal_mode`.
