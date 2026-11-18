# Security, Hardening & Data Protection

This document sets security baselines for `railmon`. Because `railmon` processes public transport infrastructure data, runs as an automated background daemon, and persists historical geographical positions, it must follow strict application security and data protection standards.

## 1. Network & TLS Security
- **Strict TLS Certificate Validation:** All HTTPS requests to `railway.gov.gr` or external APIs must validate remote SSL/TLS certificates. Never disable certificate verification or use unverified SSL contexts (`ssl._create_unverified_context` is strictly prohibited).
- **Safe Request Timeouts:** Every HTTP request must specify an explicit, bounded timeout (e.g., `timeout=10.0`). Indefinite blocking on network I/O is a denial-of-service vulnerability.
- **SSRF & URL Whitelisting:** The client must only connect to trusted, hardcoded domain endpoints (`railway.gov.gr`). Do not accept arbitrary external URLs from CLI arguments or environment variables without hostname whitelisting.

## 2. Database & SQL Injection Prevention
- **100% Parameterized Queries:** All SQL statements in `storage/` must use parameterized placeholders (`?`). String interpolation, format strings (`f"SELECT ... WHERE id = '{param}'"`), or manual concatenation are strictly prohibited for database queries.
- **Table & Column Identifier Safeguards:** Where table names or sort orders are dynamic, validate them against an explicit set of allowed enum values before execution.

## 3. Filesystem Permissions & Multi-User Privacy
- **Restrictive SQLite File Modes:** Historical tracking data contains continuous telemetry that could be abused in multi-user POSIX environments. When creating new SQLite database files or state directories, ensure restrictive permissions:
  ```python
  import os
  # Restrict newly created database file to owner read/write (POSIX 0600)
  if os.name != "nt":
      old_umask = os.umask(0o077)
      try:
          # create/open database file
          pass
      finally:
          os.umask(old_umask)
  ```
- **Path Traversal Protection on Exports:** For CLI export subcommands (such as `--export-csv <path>` or `--export-json <path>`), validate and sanitize paths against directory traversal (`../`). Ensure output directories are canonicalized with `os.path.abspath` and verify that the target directory exists and is writable.

## 4. Subprocess Execution & Shell Safety
- **No `shell=True`:** If external system commands are ever invoked, never pass `shell=True` to `subprocess.Popen` or `subprocess.run`. Pass commands as discrete argument lists (`["command", "arg1", "arg2"]`).
- **No Dynamic Code Evaluation:** The use of `eval()`, `exec()`, or untrusted `pickle.loads()` is strictly prohibited. Use standard library `json` for all serialization.
