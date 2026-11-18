# Git Commit Instructions

When writing a commit message, follow these repository rules:

## 1. Subject Line Format
- **Conventional Commits (Preferred):** `<type>(<Scope>): <verb in imperative> <summary>`
  - Types: `feat`, `fix`, `perf`, `refactor`, `docs`, `test`, `build`, `ci`, `chore`.
- **Scope Naming:** Use lowercase component names identifying the affected subsystem:
  - `stream`: SSE connection handling, buffering, event multiplexing.
  - `client`: `RailwayGovClient` HTTP calls, headers, session management.
  - `models`: Dataclass models (`TrainPosition`, `Schedule`, `Station`).
  - `cli`: Terminal monitor and command-line interface.
  - `geo`: GeoJSON infrastructure parsing or coordinate transformations.
  - `docs`: Documentation in `docs/` or `README.md`.
  - `tests`: Test suites and test fixtures.
- **Verb:** Use lowercase imperative mood (`fix`, `add`, `implement`, `refactor`, `optimize`, `update`).
- **Punctuation:** Do not end the subject line with a period.
- **Length:** Target <= 50 characters when possible; 72 characters is the absolute hard limit.

## 2. Body Structure
- **Blank Line:** Separate subject and body with exactly one blank line.
- **Wrapping:** Manually hard-wrap all body lines at 72 characters.
- **Summary Paragraph:** Start with a brief overview explaining the problem, context, and rationale (*what* and *why*).

## 3. Example Commit Messages
```text
feat(stream): implement exponential backoff on SSE disconnects

Add configurable reconnect delay and maximum retry threshold to
stream_train_positions() to prevent rapid reconnect loops during
upstream network jitter.

docs(architecture): add ASCII diagrams for HEPOS telemetry pipeline

Include detailed signal flow and coordinate transformation diagrams
documenting GNSS RTK corrections and track snapping logic.
```
