---
name: package-distribution-validator
description: >-
  Validates packaging, executable entrypoints, zero-dependency runtime purity on Linux/macOS,
  and cross-platform installation readiness (railmon CLI and Windows railmon.bat).
  Use this skill before releasing a new version or changing dependency manifests.
---

# Package & Distribution Validator

Validates that `railmon` adheres to its core design tenet: **zero external runtime dependencies on Linux and macOS**, while maintaining cross-platform installation and executable wrapper integrity.

## When to Apply
- Modifying `requirements.txt`, `requirements-dev.txt`, or packaging metadata (`pyproject.toml`, `setup.py`).
- Modifying executable launchers: `./railmon` (POSIX shell / python wrapper) or `./railmon.bat` (Windows launcher).
- Tagging a new version or preparing a release artifact.

## Validation Procedure

### 1. Zero-Dependency Runtime Manifest Audit
Inspect `requirements.txt` to ensure no external PyPI runtime packages have been introduced for Linux or macOS:
```bash
grep -v "^#" requirements.txt | grep -v "^$"
```
**Pass Criteria:**
- The only permitted runtime entry is `windows-curses>=2.4.0; sys_platform == "win32"`.
- All development/lint/test dependencies must strictly reside in `requirements-dev.txt`.

### 2. Executable Launcher Verification
Verify that both the POSIX and Windows entrypoints execute cleanly without import or syntax errors:

#### A. POSIX Launcher (`./railmon`)
```bash
python3 railmon --help
```
**Pass Criteria:**
- Returns exit code 0.
- Displays full CLI argument options (`--live`, `--record`, `--mock`, `--headless`, etc.).

#### B. Windows Batch Launcher (`railmon.bat`)
Verify batch file syntax and Python detection:
```cmd
railmon.bat --help
```
**Pass Criteria:**
- Successfully finds `python` on PATH and prints CLI help with exit code 0.

### 3. Clean Environment Installation Test
Simulate installing in a fresh, isolated virtual environment:
```bash
python -m venv .test_venv
# On POSIX:
.test_venv/bin/pip install -r requirements.txt
.test_venv/bin/python train_tracker.py --help

# On Windows:
.test_venv\Scripts\pip install -r requirements.txt
.test_venv\Scripts\python train_tracker.py --help
```
**Pass Criteria:**
- Installation completes without compiling C-extensions or fetching unexpected dependencies.
- Command outputs `--help` with returncode 0.

### 4. Cleanup
Remove temporary virtual environment:
```bash
rm -rf .test_venv  # POSIX
# or Remove-Item -Recurse -Force .test_venv  # PowerShell
```
