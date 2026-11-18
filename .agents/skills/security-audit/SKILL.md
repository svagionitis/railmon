---
name: security-audit
description: >-
  Audits python codebase for common security issues: SQL injection, unverified SSL contexts,
  directory traversal in export paths, unsafe subprocess executions, and hardcoded secrets.
  Use this skill before releases, security reviews, or when adding external API integrations.
---

# Security Audit

Scans the `railmon` codebase for security vulnerabilities, compliance with secure coding standards, and safe data handling practices.

## When to Apply
- Prior to public releases or deployment in shared multi-user environments.
- After adding or modifying SQL queries in `storage/`.
- After updating network request logic, HTTP headers, or export file path handling.

## Audit Procedure

### 1. Automated AST Vulnerability Scan with Bandit
Run bandit across all source directories (excluding test fixtures):
```bash
bandit -r train_tracker.py storage/ tui/ -ll -v
```
*(If bandit is not installed, install via `pip install bandit` or perform the manual AST pattern checks below).*

**Pass Criteria:** Zero High or Medium severity findings reported.

### 2. Manual Pattern Inspections

Run static checks for critical security anti-patterns:

#### A. SQL Injection Probe
Verify that zero SQL queries concatenate or interpolate strings:
```bash
# Look for formatted strings inside SQL executes:
grep -En "execute\([f\"'].*\{" storage/*.py
```
**Pass Criteria:** Zero matches. All queries must use `?` parameterization.

#### B. SSL / TLS Verification Check
Confirm that certificate validation is never disabled:
```bash
grep -En "ssl\._create_unverified_context|verify\s*=\s*False|CERT_NONE" train_tracker.py storage/ tui/
```
**Pass Criteria:** Zero matches.

#### C. Unsafe Subprocess & Execution Check
Verify that `shell=True`, `eval()`, `exec()`, or `pickle` are never used:
```bash
grep -En "shell\s*=\s*True|eval\(|exec\(|pickle\." train_tracker.py storage/ tui/
```
**Pass Criteria:** Zero matches.

#### D. Path Traversal & Unsanitized File Writes
Inspect all functions writing files from user inputs (such as `--export-csv` in `storage/cli_formatters.py`):
- Ensure paths are validated against `..` escapes.
- Ensure directories are checked for existence and safe permissions.
