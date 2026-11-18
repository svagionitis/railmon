---
name: markdown-relative-paths
description: >-
  Enforces and converts file links and path references in repository Markdown
  documentation (docs/, README.md, .agents/) to portable relative paths. Use
  this skill when creating, editing, or auditing Markdown documentation in the
  repository.
---

# Markdown Relative Paths

Ensures all Markdown links, diagram labels, and repository file references in checked-in documentation remain portable across developer workstations, CI runners, and Git viewers by using document-relative paths instead of `file://` URIs or local filesystem paths.

## When to Apply
- Authoring new design docs, implementation plans, or analysis reports in `docs/`.
- Editing `README.md`, `docs/**/*.md`, or `.agents/**/*.md`.
- Adding ASCII or Mermaid diagrams that reference project files.
- Auditing existing Markdown files for accidental `file://` or `/home/` links.

## Step-by-Step Procedure

### 1. Compute Relative Link Targets from the Document's Directory
Determine the directory depth $d$ of the Markdown file relative to the repository root:
- **`d = 0` (`README.md`)**: Prefix repo paths with `./` (e.g., `./train_tracker.py`, `./docs/architecture.md`).
- **`d = 1` (`docs/<doc>.md`)**:
  - Sibling doc in `docs/`: `./<other>.md`
  - Repo directories & root files (`train_tracker.py`, `storage/`, `tests/`): `../<file>`, `../<dir>/<file>`
- **`d = 2` (`docs/<subdir>/<doc>.md`)**:
  - Sibling doc in same subdir: `./<other>.md`
  - Parent `docs/` file: `../<other>.md`
  - Repo directories: `../../<dir>/<file>`

Preserve line-number anchors (`#L10-L25`) when converting links:
```markdown
[`RailwayGovClient.stream_train_positions`](../train_tracker.py#L90-L150)
[`Database`](../storage/db.py#L31-L80)
```

### 2. Relative Paths in Diagrams & Tables
When labeling boxes in ASCII or Mermaid diagrams, use document-relative or repo-root relative paths:
- In ASCII diagrams: `../storage/db.py` or `storage/db.py`
- In Mermaid labels: `A["storage/recorder.py"]`

### 3. Convert Existing Absolute / `file://` Links
Order replacements from most specific (deepest shared directory) to least specific (repo root):
1. Strip `file://<repo-root>/<doc-subdir>/` -> `./` (for same-directory links)
2. Strip `file://<repo-root>/` -> `../` (repeated `d` times for repo-root targets)
3. Replace bare `<repo-root>/` references in Markdown links or prose with relative or repo-root-relative paths.

### 4. Preserve Target OS Runtime Paths
Do **not** rewrite remote API URLs, embedded Linux runtime paths, or external services that are documentation subjects:
- Remote URLs: `https://railway.gov.gr/api/train-stream`
- System paths: `/etc/hosts`, `/dev/null`

### 5. Validate Before Finishing
Run a verification scan on the modified Markdown file(s) to confirm zero `file://` URIs or local home directory prefixes remain:
```bash
grep -En "file://|/home/|/Users/|[A-Za-z]:\\\\" <path-to-doc.md>
```
Expected result: **no matches** (exit code `1`).
