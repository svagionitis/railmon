# Logging, Observability & Error Reporting

This document defines standard practices for logging, diagnostics, and operational observability in `railmon`. Production daemons and interactive TUIs must never pollute standard console outputs or obscure runtime failures.

## 1. No Bare `print()` Statements in Core Modules
- **Prohibited in Ingestion & Library Code:** Core modules (`train_tracker.py`, `storage/`, `tui/`) must never use raw `print()` statements for diagnostic logging or error reporting.
- **CLI Subcommand Exception:** Plain terminal utility subcommands (e.g., `--help`, `--version`, `--list-stations`, or JSON dump commands) may print directly to `sys.stdout`.
- **TUI Safety:** Under no circumstances should background threads print to `sys.stdout` or `sys.stderr` while `curses` is actively controlling the terminal, as it will corrupt the screen buffer.

## 2. Standard Logging Hierarchy
Use Python's built-in `logging` module with per-module loggers:
```python
import logging
logger = logging.getLogger("railmon." + __name__)
```

### Log Level Discipline:
| Level | Appropriate Usage |
|---|---|
| `DEBUG` | Fine-grained telemetry parsing details, raw SSE event payload dumps, internal queue sizes, and lock acquisition timers. |
| `INFO` | High-level lifecycle milestones: stream connected, clean shutdown initiated, database migration applied, hourly summary stats. |
| `WARNING` | Non-fatal anomalies: malformed train coordinate skipped, transient SSE network disconnect (retrying with backoff), missing non-critical station metadata. |
| `ERROR` | Unrecoverable component errors: persistent database I/O error, failed database migration, repeated upstream HTTP 5xx responses exhausting retry limits. |
| `CRITICAL` | Fatal conditions requiring immediate daemon shutdown: disk full, unhandled fatal exceptions. |

## 3. Log Rotation in Daemon / Headless Mode
When running as a background service or with the `--record` daemon flag:
- Log messages must be directed to a file using `logging.handlers.RotatingFileHandler`.
- Default log file location:
  - Linux/macOS: `~/.local/state/railmon/railmon.log` (or `$XDG_STATE_HOME/railmon/`)
  - Windows: `%LOCALAPPDATA%\railmon\logs\railmon.log`
- Configuration parameters:
  - Maximum file size: `10 MB`
  - Backup count: `3` retained rotations
  - Standard format: `%(asctime)s [%(levelname)s] [%(threadName)s] %(name)s: %(message)s`

## 4. Upstream Disconnection Reporting
Do not spam logs on standard upstream connection drops:
- First disconnect: Log at `WARNING` with backoff interval.
- Subsequent retry attempts: Log at `DEBUG` or `INFO`.
- Re-establishment: Log at `INFO` with duration of outage.
