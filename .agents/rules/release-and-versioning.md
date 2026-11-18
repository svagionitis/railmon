# Release Management, Semantic Versioning & Upstream Compatibility

This document defines standard conventions for releasing updates, incrementing versions, and maintaining backward compatibility in `railmon`.

## 1. Semantic Versioning (SemVer 2.0.0)
Releases must follow the `MAJOR.MINOR.PATCH` format:
- **`MAJOR` (e.g., 2.0.0):** Incompatible API changes, major breaking CLI interface rewrites, or database schema migrations requiring manual intervention without forward compatibility.
- **`MINOR` (e.g., 1.2.0):** New backward-compatible functionality (e.g., adding a new corridor filter, new storage query endpoint, new TUI display widget).
- **`PATCH` (e.g., 1.1.3):** Backward-compatible bug fixes, performance optimizations, or upstream endpoint parsing resilience fixes.

## 2. Single-Source Versioning
- The canonical application version string must reside in a single location:
  `train_tracker.py` as `__version__ = "X.Y.Z"`.
- Packaging files (`pyproject.toml`, `setup.py`) and CLI flags (`railmon --version`) must dynamically read or reference this single source of truth to avoid version drift.

## 3. Backward-Compatible Upstream Parsing
- The Hellenic Railways API (`railway.gov.gr`) is an external government service that may alter or extend its JSON payload schemas without prior notice.
- **Forward & Backward Tolerance:** New fields added upstream must never cause existing clients to crash. Deprecated fields removed upstream must fail gracefully with sensible fallbacks (`None`, `0`, or `""`).

## 4. Database Schema Migration Integrity
- Never drop existing SQLite telemetry tables or columns without an automated migration step in `storage/schema.py`.
- Schema changes must:
  1. Increment `PRAGMA user_version`.
  2. Provide a migration script that executes inside an explicit transaction (`BEGIN IMMEDIATE`).
  3. Validate database integrity with `PRAGMA integrity_check` before completing startup.
