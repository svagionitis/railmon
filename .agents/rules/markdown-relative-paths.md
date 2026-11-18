# Markdown Relative Paths Policy

All links and repository file references inside checked-in Markdown documentation (`docs/**/*.md`, `README.md`, `.agents/**/*.md`) must use portable, document-relative paths rather than machine-specific absolute paths or `file://` URIs.

## 1. Forbidden Path Formats
- **No `file://` URIs**: Never embed `file:///...` links in repository Markdown files.
- **No Machine-Local Absolute Paths**: Never embed workstation-specific prefixes (`/home/<user>/...`, `/Users/<user>/...`, `C:\...`) in links, tables, or prose referring to repository files.

## 2. Document-Relative Link Resolution
Compute every Markdown link `[label](target)` relative to the directory containing the Markdown file itself:

| Source Document Location | Target Location | Required Relative Link |
|---|---|---|
| `README.md` (repo root) | `train_tracker.py` | `./train_tracker.py` |
| `README.md` (repo root) | `docs/architecture.md` | `./docs/architecture.md` |
| `docs/<doc>.md` | `docs/<other>.md` | `./<other>.md` |
| `docs/<doc>.md` | `train_tracker.py` | `../train_tracker.py` |
| `docs/<doc>.md` | `tests/<test>.py` | `../tests/<test>.py` |
| `docs/<subdir>/<doc>.md` | `docs/<other>.md` | `../<other>.md` |
| `docs/<subdir>/<doc>.md` | `train_tracker.py` | `../../train_tracker.py` |

## 3. Plain Code Spans vs. Remote Endpoints
- **Repository Paths in Plain Text / Backticks**: Use repo-root-relative paths (`train_tracker.py`, `docs/architecture.md`) or basenames (`train_tracker.py`).
- **Remote API Endpoints & URLs**: URLs such as `https://railway.gov.gr/api/train-stream` are documentation subjects and are permitted as bare URLs or Markdown links.

> **Note:** This rule applies strictly to repository files. Direct chat responses to the user may still use workspace clickable links as required by chat formatting guidelines.
