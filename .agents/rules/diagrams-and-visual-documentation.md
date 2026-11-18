# Diagrams and Visual Documentation Standards

This rule governs the creation of visual architecture, workflows, state machines, and data structures across all repository documentation (`docs/**/*.md`, `README.md`, `.agents/**/*.md`) and technical design comments.

---

## 1. Dual Diagramming Strategy: ASCII & Mermaid

Every technical document, architectural specification, and complex module documentation must utilize visual diagrams. Use both **ASCII diagrams** and **Mermaid diagrams** depending on the medium and viewer:

```text
+-----------------------------------------------------------------------------+
|                          DUAL DIAGRAMMING MATRIX                            |
+----------------------+-----------------------------+------------------------+
| Format               | Primary Purpose             | Permitted Contexts     |
+----------------------+-----------------------------+------------------------+
| Mermaid (`mermaid`)  | High-fidelity visualization | Markdown docs (docs/,  |
|                      | Flowcharts, sequences,      | README.md, pull        |
|                      | state machines, ERDs        | request descriptions)  |
+----------------------+-----------------------------+------------------------+
| ASCII (`text`)       | Terminal, console, TUI,     | Code docstrings, CLI   |
|                      | memory layout, frames, and  | epilogs, plain text    |
|                      | text-only fallback          | files, Markdown docs   |
+----------------------+-----------------------------+------------------------+
```

---

## 2. Mermaid Diagram Specifications

Use fenced ```` ```mermaid ```` code blocks. Follow these syntax and rendering rules:

### A. Supported Diagram Types
- **Flowcharts / Architecture Graphs:** `flowchart TD` (top-down) or `flowchart LR` (left-to-right).
- **Sequence Diagrams:** `sequenceDiagram` for inter-process, network, or multi-threaded protocol message passing.
- **State Diagrams:** `stateDiagram-v2` for connection states, train operational states, and UI modes.
- **Entity-Relationship Models:** `erDiagram` for SQLite schema tables, foreign keys, and cardinalities.
- **Class Diagrams:** `classDiagram` for domain model hierarchies.

### B. Syntax & Quotation Rules
- **Quote Node Labels:** Always double-quote node labels containing punctuation, parentheses, brackets, or spaces:
  ```mermaid
  flowchart TD
      A["StreamWorker (Thread)"] -->|Batch Updates| B["TrainStateStore (RLock)"]
      B --> C["StateSnapshot (Immutable)"]
  ```
- **No HTML Tags:** Avoid raw HTML tags (`<br>`, `<font>`) inside labels; use standard newline syntax or separate nodes.
- **Subgraphs for Layer Boundaries:** Group related components using `subgraph Name ["Display Label"]` to visually convey architectural layering.

---

## 3. ASCII Diagram Specifications

Use fenced ```` ```text ```` or ```` ```ascii ```` code blocks. Follow these layout conventions:

### A. Clean Box-Drawing & Connectors
- Use standard ASCII glyphs (`+`, `-`, `|`, `^`, `v`, `>`, `<`) or clean Unicode box characters (`┌`, `─`, `┐`, `│`, `└`, `┘`, `▲`, `▼`, `◄`, `►`).
- Keep horizontal and vertical borders aligned.
- Label all interconnecting flows with clear directional arrows (`-->`, `|`, `v`).

### B. Standard ASCII Flow Template
```text
+------------------------+        HTTP / SSE        +------------------------+
|   railway.gov.gr API   | =======================> |   RailwayGovClient     |
+------------------------+                          +------------------------+
                                                                |
                                                                v
+------------------------+      Batch Commit        +------------------------+
|  SQLite WAL Database   | <----------------------- |   TelemetryRecorder    |
+------------------------+     (executemany)        +------------------------+
```

---

## 4. Relative Paths for All File References in Documents

All file references, hyperlinked code spans, and paths embedded in Markdown documentation **must use document-relative paths**.

### A. Strict Rules
- **Zero `file://` URIs:** Never include `file:///...` links in repository Markdown files.
- **Zero Workstation Absolute Paths:** Never include machine-specific paths (`C:\...`, `/home/<user>/...`, `/Users/<user>/...`).
- **Relative to Document Directory:** Calculate every path relative to the directory containing the Markdown file:
  - From `docs/architecture.md` referencing `train_tracker.py`: `../train_tracker.py`
  - From `docs/architecture.md` referencing `storage/db.py`: `../storage/db.py`
  - From `docs/sub/topic.md` referencing `docs/architecture.md`: `../architecture.md`
  - From `README.md` referencing `train_tracker.py`: `./train_tracker.py`

### B. Link Format
```markdown
[train_tracker.py](../train_tracker.py#L90-L150)
[storage/repository.py](../storage/repository.py)
[docs/architecture.md](./architecture.md)
```
