---
name: database-backup-and-migration-validator
description: >-
  Validates online SQLite backups (sqlite3.backup API) while telemetry is actively writing
  in WAL mode, and tests idempotent schema migrations across database versions.
  Use this skill when modifying database schema, backup utilities, or migration scripts.
---

# Database Backup & Migration Validator

Validates that `railmon`'s SQLite persistence layer supports hot online backups without service interruption and executes zero-downtime database schema upgrades.

## When to Apply
- When altering schemas, constraints, or table structures in `storage/schema.py`.
- When adding or modifying backup routines or CLI export utilities.
- Prior to deploying new software versions to environments with existing historical database files.

## Validation Procedure

### 1. Online Hot Backup Under Active Write Load
Validate that SQLite's online backup API operates safely concurrently with high-speed WAL writes:
```bash
python -c "
import sqlite3, tempfile, os, time, threading
from storage.db import Database
from storage.schema import init_db

# Create active WAL database
with tempfile.TemporaryDirectory() as tmp:
    db_path = os.path.join(tmp, 'live.db')
    backup_path = os.path.join(tmp, 'backup.db')
    
    db = Database(db_path)
    db.init_schema()
    
    stop_writing = threading.Event()
    
    def writer():
        conn = sqlite3.connect(db_path, timeout=5.0)
        conn.execute('PRAGMA journal_mode = WAL;')
        i = 0
        while not stop_writing.is_set():
            with conn:
                conn.execute(
                    'INSERT OR REPLACE INTO trains (train_number, first_observed_at, last_observed_at) VALUES (?, ?, ?)',
                    (f'TR{i}', '2026-11-18T10:00:00Z', '2026-11-18T10:00:00Z')
                )
            i += 1
            time.sleep(0.005)
        conn.close()

    t = threading.Thread(target=writer)
    t.start()
    
    time.sleep(0.1)  # Let writer start
    
    # Perform hot online backup
    src_conn = sqlite3.connect(db_path, timeout=5.0)
    dst_conn = sqlite3.connect(backup_path)
    with dst_conn:
        src_conn.backup(dst_conn, pages=100)
    dst_conn.close()
    src_conn.close()
    
    stop_writing.set()
    t.join()
    
    # Check integrity of backup database
    check_conn = sqlite3.connect(backup_path)
    integrity = check_conn.execute('PRAGMA integrity_check').fetchone()[0]
    count = check_conn.execute('SELECT COUNT(*) FROM trains').fetchone()[0]
    check_conn.close()
    
    assert integrity == 'ok', f'Backup integrity check failed: {integrity}'
    assert count > 0, 'Backup database contains no rows'
    print(f'[SUCCESS] Hot online backup verified cleanly ({count} records captured, integrity=ok)')
"
```
**Pass Criteria:**
- Script completes with zero exceptions or database lock errors.
- Output reports `[SUCCESS] Hot online backup verified cleanly`.

### 2. Idempotent Schema Migration Probing
1. Open an existing older database version or test fixture.
2. Run database initialization:
   ```bash
   python -c "from storage.db import Database; db = Database('storage/railmon.db'); db.init_schema()"
   ```
3. Run initialization a second time against the same file.
4. **Pass Criteria:**
   - Must complete idempotently without raising `OperationalError: table ... already exists`.
   - `PRAGMA user_version` reflects the target schema version.
   - `PRAGMA integrity_check` reports `ok`.
