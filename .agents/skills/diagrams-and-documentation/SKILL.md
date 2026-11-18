---
name: diagrams-and-documentation
description: >-
  Standardizes the creation of ASCII diagrams, Mermaid diagrams, and enforces
  document-relative paths for all filenames and links across repository documentation
  (docs/, README.md, .agents/). Use this skill when authoring, reviewing, or updating
  architectural documentation, technical specifications, and implementation plans.
---

# Diagrams and Documentation Standards

Guides the construction of dual-format visual diagrams (ASCII and Mermaid) and validates that all repository file paths and links in documentation are strictly document-relative.

## When to Apply
- Authoring or updating design documents, architecture specs, or review reports in `docs/`.
- Documenting system workflows, state machines, protocol message sequences, or database schemas.
- Adding architectural or protocol overview comments in code modules.
- Auditing repository Markdown files for compliance with relative path standards and visual clarity.

---

## 1. Visual Diagramming: Dual-Format Strategy

Every architectural, workflow, or lifecycle document must provide visual representations:
1. **Mermaid Diagrams (`mermaid`):** Rendered natively by GitHub, GitLab, and Markdown previewers for rich, interactive graphical views.
2. **ASCII Diagrams (`text`):** Readable directly inside terminal editors (`vim`, `nano`), command-line tools, diffs, and plain text code comments.

---

### A. Mermaid Diagram Guidelines

Use fenced ```` ```mermaid ```` code blocks.

#### Supported Types & Syntax
- **Architecture & Topologies (`flowchart TD` / `flowchart LR`):**
  Always quote node labels that contain spaces, parentheses, brackets, or punctuation.
  ```mermaid
  flowchart TD
      A["StreamWorker (Thread)"] -->|Enqueues Batch| B["TelemetryRecorder (Thread)"]
      B --> C[("SQLite WAL Database")]
  ```
- **Inter-Process / Protocol Sequences (`sequenceDiagram`):**
  ```mermaid
  sequenceDiagram
      autonumber
      participant C as Client
      participant S as SSE Stream
      participant Q as Queue
      participant D as SQLite WAL
      C->>S: GET /api/train-stream
      S-->>C: event: trainPositionsUx
      C->>Q: put_nowait(batch)
      Q->>D: executemany (batch commit)
  ```
- **State Transitions (`stateDiagram-v2`):**
  ```mermaid
  stateDiagram-v2
      [*] --> DISCONNECTED
      DISCONNECTED --> CONNECTING : start()
      CONNECTING --> CONNECTED : handshake ok
      CONNECTED --> RECONNECTING : socket drop
      RECONNECTING --> CONNECTING : backoff elapsed
      CONNECTED --> PAUSED : toggle_pause()
      PAUSED --> CONNECTED : toggle_pause()
      CONNECTED --> [*] : stop()
  ```
- **Database Schema Models (`erDiagram`):**
  ```mermaid
  erDiagram
      trains ||--o{ scheduled_trips : operates
      scheduled_trips ||--o{ telemetry_history : records
      scheduled_trips ||--o{ schedule_stops : contains
  ```

---

### B. ASCII Diagram Guidelines

Use fenced ```` ```text ```` code blocks.
- Construct clear, symmetric boxes using `+`, `-`, `|` or Unicode box glyphs (`┌`, `─`, `┐`, `│`, `└`, `┘`).
- Align column boundaries and arrow heads (`-->`, `<--`, `v`, `^`).
- Example:

```text
+-----------------------+                    +------------------------+
|   TrainStateStore     |  get_snapshot()    |   TUIApp (Event Loop)  |
|  (Thread-Safe RLock)  | -----------------> |  (Target FPS: 15-20)   |
+-----------------------+                    +------------------------+
            ^                                            |
            | update_positions()                         v
+-----------------------+                    +------------------------+
|     StreamWorker      |                    |      TUIRenderer       |
|   (Ingestion Thread)  |                    |    (Braille Canvas)    |
+-----------------------+                    +------------------------+
```

---

## 2. Document-Relative Paths for All File References

All links and file references within documentation (`docs/**/*.md`, `README.md`, `.agents/**/*.md`) **must be document-relative**:

### A. Relative Resolution Matrix
| Document Location | Referenced File | Required Relative Link |
|---|---|---|
| `README.md` (root) | `train_tracker.py` | `./train_tracker.py` |
| `README.md` (root) | `docs/architecture.md` | `./docs/architecture.md` |
| `docs/<doc>.md` | `train_tracker.py` | `../train_tracker.py` |
| `docs/<doc>.md` | `storage/db.py` | `../storage/db.py` |
| `docs/<doc>.md` | `docs/other.md` | `./other.md` |
| `docs/sub/<doc>.md` | `train_tracker.py` | `../../train_tracker.py` |

### B. Anti-Patterns to Eliminate
- ❌ **Absolute workstation paths:** `C:\Users\...\storage\db.py` or `/home/user/project/storage/db.py`
- ❌ **`file://` URIs:** `[storage](file:///C:/Users/.../storage/db.py)`
- ❌ **Root-slashed paths:** `/storage/db.py` (resolves to filesystem root in standard Markdown renderers)

---

## 3. Verification & Quality Check

Run this verification check across all modified Markdown documents before finalizing:

```bash
# 1. Verify zero absolute paths or file:// URIs remain:
grep -En "file://|/home/|/Users/|[A-Za-z]:\\\\" docs/*.md README.md

# 2. Verify all ```mermaid blocks have valid diagram declarations:
grep -En "```mermaid" docs/*.md README.md
```
Pass criteria:
- Command 1 outputs zero matches (exit code 1).
- Command 2 confirms presence of visual diagrams in major architectural documents.
