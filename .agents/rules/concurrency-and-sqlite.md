# Concurrency, Thread Safety & SQLite Data Integrity

This document establishes operational requirements for multi-threaded concurrency and SQLite persistence in `railmon`. Because `railmon` operates as a 24/7 background telemetry daemon and interactive TUI, thread safety and database resilience under continuous write load are critical.

## 1. Thread Isolation for Database Connections
- **Zero Cross-Thread Connection Sharing:** Never share an open `sqlite3.Connection` or `sqlite3.Cursor` across threads. SQLite connections must either be scoped locally within a thread (`threading.local`) or managed by a dedicated database writer thread.
- **Dedicated Writer Queue Architecture:** Telemetry ingestion threads must not execute ad-hoc direct writes to the main database file simultaneously. Telemetry events must be enqueued onto a thread-safe `queue.Queue` and consumed by a single designated storage writer thread.

## 2. SQLite Configuration & WAL Pragmas
Every connection opened by `railmon` (readers and writers) must execute the following setup pragmas:
```python
conn.execute("PRAGMA journal_mode = WAL;")
conn.execute("PRAGMA synchronous = NORMAL;")
conn.execute("PRAGMA busy_timeout = 5000;")
conn.execute("PRAGMA foreign_keys = ON;")
```
- **WAL Mode:** Write-Ahead Logging allows concurrent readers alongside the single writer without blocking.
- **Busy Timeout:** Setting `busy_timeout = 5000` prevents immediate `OperationalError: database is locked` exceptions under momentary lock contention.

## 3. Micro-Batching & Explicit Transactions
- **No Autocommit Loops:** Never write high-frequency stream events inside tight autocommit loops.
- **Micro-Batching:** Accumulate incoming frames in memory and commit in micro-batches (e.g., commit every 500ms or when the queue reaches 100 items):
  ```python
  with conn:  # Enforces BEGIN ... COMMIT / ROLLBACK automatically
      conn.executemany("INSERT INTO train_positions (...) VALUES (...)", batch)
  ```
- **Use `BEGIN IMMEDIATE` for Exclusive Writes:** For critical multi-statement state updates, acquire write locks upfront with `conn.execute("BEGIN IMMEDIATE")` to avoid deadlock elevation mid-transaction.

## 4. Schema Versioning & Migrations
- **User Version Pragma:** Database schema revisions must be tracked using `PRAGMA user_version`.
- **Idempotent Upgrades:** Migrations must check `PRAGMA user_version` on startup and apply incremental schema patches idempotently before opening writer loops.
- **Integrity Probing:** Storage maintenance routines must support running `PRAGMA integrity_check` and `PRAGMA optimize` periodically or during quiet intervals.
