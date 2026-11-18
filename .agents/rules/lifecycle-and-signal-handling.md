# Lifecycle, Clean Shutdown & Signal Handling

`railmon` runs both as an interactive terminal dashboard (TUI) and as a background telemetry recorder. This rule governs process startup, signal handling, and clean shutdown procedures to prevent database corruption, shell state disruption, and orphaned threads.

## 1. Unified Cancellation Tokens
- **Shared Shutdown Event:** All asynchronous loops, HTTP listeners, and worker threads must coordinate shutdown through a shared `threading.Event` (e.g., `stop_event` or `shutdown_event`).
- **Responsive Loop Polling:** Thread loops must poll `shutdown_event.is_set()` with non-blocking timeouts (e.g., `queue.get(timeout=0.5)` or `stream.read(timeout=1.0)`) rather than blocking indefinitely without interrupt capability.

## 2. Signal Trapping & OS Compatibility
- **Registered Signals:** The application must handle `SIGINT` (Ctrl+C) and `SIGTERM` (service termination) on POSIX and Windows:
  ```python
  import signal
  def signal_handler(signum, frame):
      shutdown_event.set()
  signal.signal(signal.SIGINT, signal_handler)
  signal.signal(signal.SIGTERM, signal_handler)
  ```
- **SIGHUP on POSIX:** Where supported (`sys_platform != "win32"`), intercept `signal.SIGHUP` to either trigger a graceful shutdown or log a clean disconnect when the controlling terminal closes.

## 3. Terminal & Curses State Preservation
- **Always Wrap with `curses.wrapper` or `try...finally`:** Never leave the host console without resetting curses mode. If curses is initialized manually, teardown must be guaranteed in a `finally` block:
  ```python
  try:
      # curses setup
      curses.noecho()
      curses.cbreak()
      stdscr.keypad(True)
      ...
  finally:
      try:
          stdscr.keypad(False)
          curses.nocbreak()
          curses.echo()
          curses.endwin()
      except Exception:
          pass
  ```
- **Restore Cursor Visibility:** Always ensure `curses.curs_set(1)` is restored so user shells do not lose their cursor on exit.

## 4. Graceful Ingestion Teardown
On shutdown signal trigger:
1. **Signal Stream Disconnect:** Abort the active HTTP/SSE connection to release the network socket.
2. **Drain Writer Queue:** Give the database storage writer thread a bounded grace period (e.g., 2.0 seconds) to flush pending telemetry frames in memory to SQLite.
3. **Commit & Checkpoint:** Close database connections cleanly, triggering a WAL checkpoint.
4. **Join Worker Threads:** Join all spawned worker threads with a timeout before terminating the main process.
