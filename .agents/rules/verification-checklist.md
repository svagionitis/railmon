# Verification Checklist (Run Before Submitting Code)

Before presenting any Python code snippet or submitting a change in `railmon`, verify it against this checklist:

1. **Connection Lifecycle & Resource Safety:**
   - Are all network requests and sockets wrapped in context managers (`with`) or safely closed?
   - Is an explicit `timeout` passed to all HTTP requests (`urllib.request.urlopen`)?
   - Is reconnect backoff enforced for stream listeners (minimum 3 seconds with jitter)?
   - Are threads coordinated with a shared `threading.Event` shutdown flag?

2. **Concurrency & SQLite Integrity:**
   - Are SQLite connections kept isolated within a single thread or managed by a dedicated writer queue?
   - Are `PRAGMA busy_timeout = 5000` and `PRAGMA journal_mode = WAL` configured?
   - Are writes grouped in micro-batches rather than autocommitting in tight loops?
   - Are database schema changes tracked with `PRAGMA user_version`?

3. **Security & Data Protection:**
   - Are all SQL queries 100% parameterized with `?` placeholders (no string interpolation)?
   - Is SSL certificate verification strictly enabled on all outbound HTTPS calls?
   - Are file creation permissions restricted (`0600` on POSIX for sensitive `.db` files)?
   - Are CLI export paths sanitized against directory traversal?

4. **Resource Bounds & Performance:**
   - Are all inter-thread queues bounded with an explicit `maxsize`?
   - Is the TUI event loop throttled to avoid spinning the CPU (10–15 FPS cap)?
   - Are in-memory caches (active trains, trail history) bounded with eviction limits?

5. **Terminal Compatibility & Curses:**
   - Is curses teardown guaranteed via `curses.wrapper` or wrapped in `try...finally` with `curses.endwin()`?
   - Does the renderer handle `KEY_RESIZE` and enforce an 80×24 minimum dimension banner?
   - Does rendering fall back to ASCII if UTF-8 encoding or color palettes are unavailable?
   - Is raw `print()` avoided in core modules in favor of standard `logging`?

6. **Defensive Parsing & Type Safety:**
   - Are all function arguments and returns annotated with standard type hints?
   - Does `TrainPosition.from_dict` handle `None`, missing fields, and type conversions safely?
   - Are coordinate bounds checked (`-90 <= lat <= 90`, `-180 <= lng <= 180`)?

7. **Relative Paths in Documentation:**
   - Do all documentation links in `docs/` and `README.md` use document-relative paths (`./`, `../`) with no machine-specific absolute path URIs?

8. **Testing & Regressions:**
   - Does the change break existing stream, TUI, or storage consumers?
   - Are unit and integration tests passing (`python -m unittest discover tests`)?
