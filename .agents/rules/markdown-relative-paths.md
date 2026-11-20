# Markdown Relative Paths Policy

All links and repository file references inside checked-in Markdown documentation (`docs/**/*.md`, `README.md`, `.agents/**/*.md`) must use portable, document-relative paths rather than machine-specific absolute paths or `file://` URIs.

## 1. Forbidden Path Formats
- **No `file://` URIs**: Never embed `file:///...` links in repository Markdown files.
- **No Machine-Local Absolute Paths**: Never embed workstation-specific prefixes (`/home/<user>/...`, `/Users/<user>/...`, `C:\...`) in links, tables, diagrams, or prose referring to repository files.

## 2. Document-Relative Link Resolution
Compute every Markdown link `[label](./target)` relative to the directory containing the Markdown file itself:

| Source Document Location | Target Location | Required Relative Link |
|---|---|---|
| `README.md` (repo root) | `train_tracker.py` | `./train_tracker.py` |
| `README.md` (repo root) | `docs/architecture.md` | `./docs/architecture.md` |
| `docs/<doc>.md` | `docs/<other>.md` | `./<other>.md` |
| `docs/<doc>.md` | `train_tracker.py` | `../train_tracker.py` |
| `docs/<doc>.md` | `storage/db.py` | `../storage/db.py` |
| `docs/<doc>.md` | `tests/<test>.py` | `../tests/<test>.py` |
| `docs/<subdir>/<doc>.md` | `docs/<other>.md` | `../<other>.md` |
| `docs/<subdir>/<doc>.md` | `train_tracker.py` | `../../train_tracker.py` |

Line anchors must be preserved when linking:
```markdown
[`RailwayGovClient.stream_train_positions`](../train_tracker.py#L90-L150)
```

## 3. Plain Code Spans & Diagrams
- **Paths in Plain Text / Code Spans / Diagram Labels**: When referencing repository files in diagram boxes, tables, or text, use document-relative paths (`../storage/db.py`) or repo-relative paths (`storage/db.py`), never local machine paths.
- **Remote API Endpoints & URLs**: URLs such as `https://railway.gov.gr/api/train-stream` are documentation subjects and are permitted as bare URLs or Markdown links.

## 4. Visual Diagrams Integration
All documentation must pair text with **ASCII diagrams** (for terminal/plain-text readability) and **Mermaid diagrams** (for rich Markdown rendering), as specified in [`.agents/rules/diagrams-and-visual-documentation.md`](./diagrams-and-visual-documentation.md).

> **Note:** This rule applies strictly to repository files. Direct chat responses to the user may still use workspace clickable links as required by chat formatting guidelines.
