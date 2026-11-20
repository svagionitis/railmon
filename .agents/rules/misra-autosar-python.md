# MISRA C++ / AUTOSAR C++14 / NASA JPL Safety Standards for Python

This document defines mandatory high-assurance coding and static analysis standards for `railmon`, adapted from **MISRA C++**, **AUTOSAR C++14**, **SEI CERT**, and **NASA JPL's "Power of 10" Rules**. Because `railmon` runs continuously as a live railway telemetry daemon and curses terminal map dashboard with zero external runtime dependencies, all code in [`train_tracker.py`](../../train_tracker.py), [`storage/`](../../storage), [`tui/`](../../tui), and [`tests/`](../../tests) must comply with these rules.

---

## 1. Rule Summary Matrix

| Rule ID | Standard Origin | Title | Enforcement |
| :--- | :--- | :--- | :--- |
| **PY-MISRA-01** | *JPL Rule 1 / MISRA Dir 4.12* | No Dynamic Code Execution, Reflection, or Unsafe Deserialization | Mandatory (AST Gate) |
| **PY-MISRA-02** | *JPL Rule 2 / AUTOSAR A6-5-1* | Bounded Loops, Retries, and Inter-Thread Queues | Mandatory (AST Gate) |
| **PY-MISRA-03** | *AUTOSAR A15-3-4 / MISRA 15-3-4* | Explicit Exception Hygiene (No Bare or Silent `except`) | Mandatory (AST Gate) |
| **PY-MISRA-04** | *MISRA Rule 17.7 / AUTOSAR A0-1-2* | Explicit Return Paths and Narrowed Optional Handling | Mandatory (AST + MyPy) |
| **PY-MISRA-05** | *AUTOSAR A7-1-1 / MISRA 7-1-1* | Immutability by Default & No Mutable Default Arguments | Mandatory (AST Gate) |
| **PY-MISRA-06** | *MISRA Rule 10.1 / AUTOSAR A8-4-1* | Complete Type Annotations & Strict Type Soundness | Mandatory (AST + MyPy) |
| **PY-MISRA-07** | *MISRA Dir 4.6 / AUTOSAR A4-7-1* | Floating-Point & Geospatial Coordinate Safety (`NaN`/`Inf` Guards) | Mandatory (AST Gate) |
| **PY-MISRA-08** | *JPL Rule 6 / McCabe Limit* | Structural Complexity, Parameter Count, and Function Length Bounds | Mandatory (AST Gate) |
| **PY-MISRA-09** | *AUTOSAR A18-5-2 / RAII* | Deterministic Resource Lifecycle (`with` Contexts & Explicit Timeouts) | Mandatory (AST Gate) |
| **PY-MISRA-10** | *JPL Rule 3 & 4 / MISRA 18-0-4* | No Recursion, No Wildcard Imports, No `assert` for Runtime Validation | Mandatory (AST Gate) |

---

## 2. Detailed Safety Rules

### PY-MISRA-01: No Dynamic Code Execution, Reflection, or Unsafe Deserialization
- **Prohibited Constructs:** Never call `eval()`, `exec()`, `compile()`, `pickle.loads()`, `pickle.load()`, `marshal.loads()`, or `subprocess` with `shell=True`.
- **Untrusted Payloads:** External JSON from `railway.gov.gr` or local files must be parsed strictly with `json.loads()` / `json.load()` and validated via typed factory methods (such as [`TrainPosition.from_dict`](../../train_tracker.py#L427-L484)) before use.

### PY-MISRA-02: Bounded Loops, Retries, and Inter-Thread Queues
- **Bounded Queues:** Every `queue.Queue`, `queue.LifoQueue`, or `queue.PriorityQueue` instantiation must pass an explicit positive `maxsize` keyword argument (e.g., `queue.Queue(maxsize=10000)`). Unbounded queues are forbidden.
- **Controlled Loops:** Every `while` loop in daemon workers ([`RailwayGovClient.stream_train_positions`](../../train_tracker.py#L617-L787), [`TelemetryRecorder._worker_loop`](../../storage/recorder.py#L293-L360), [`StreamWorker._run_loop`](../../tui/stream_worker.py#L113-L200), [`TUIApp.run_loop`](../../tui/app.py#L953-L1076)) must check a `threading.Event` shutdown flag (`stop_event.is_set()`) or have an explicit iteration/timeout bound.

### PY-MISRA-03: Explicit Exception Hygiene
- **No Bare `except:`:** Bare `except:` clauses are strictly prohibited because they trap `SystemExit`, `KeyboardInterrupt`, and `MemoryError`. Always catch specific exception classes or tuples (e.g., `except (urllib.error.URLError, TimeoutError, OSError):`).
- **No Unjustified Silent Catch:** Catching broad `except Exception:` followed solely by `pass` without logging or an explicit `# safety: ignore[PY-MISRA-03]` justification comment (permitted only in best-effort destructor/cleanup teardown such as `__del__` or `close()`) is prohibited.

### PY-MISRA-04: Explicit Return Paths and Narrowed Optional Handling
- **Consistent Return Statements:** If a function contains a `return <expr>` with a value on any branch, every exit branch must explicitly `return <expr>` or `return None` (no falling off the end of a value-returning function or bare `return`).
- **Explicit `None` Narrowing:** Callers of functions returning `Optional[T]` must explicitly check `is not None` before dereferencing attributes.

### PY-MISRA-05: Immutability by Default & No Mutable Default Arguments
- **No Mutable Default Arguments:** Function and method parameters must never use mutable literals (`[]`, `{}`, `set()`) or mutable calls (`list()`, `dict()`, `set()`) as default values. Use `None` (initializing inside the body) or immutable `tuple` / `frozenset`.
- **Frozen Value Objects:** Configuration objects, geospatial primitives, and database DTOs ([`RecorderConfig`](../../storage/recorder.py#L25-L40), [`DeadbandConfig`](../../storage/filter.py#L14-L27), [`GeoPoint`](../../tui/geo.py#L103-L113), [`StateSnapshot`](../../tui/state.py#L159-L183)) must use `@dataclass(frozen=True)`.

### PY-MISRA-06: Complete Type Annotations & Strict Type Soundness
- **100% Signature Annotation Coverage:** Every function and method in production modules (`train_tracker.py`, `storage/`, `tui/`) must declare explicit type annotations for all parameters (excluding `self` and `cls`) and an explicit return type annotation (`-> ReturnType` or `-> None`).
- **Enum Type Safety:** Prefer strongly-typed `Enum` classes ([`TelemetrySource`](../../train_tracker.py#L33-L38), [`RecordingMode`](../../train_tracker.py#L40-L45), [`ConnectionStatus`](../../tui/state.py#L21-L30)) over raw magic strings or boolean flag proliferation.

### PY-MISRA-07: Floating-Point & Geospatial Coordinate Safety
- **No Exact Float Equality:** Direct equality or inequality comparisons (`==`, `!=`) against floating-point literals (e.g., `x == 0.0`, `lat != 0.0`) are prohibited due to IEEE 754 representation drift. Use `math.isclose()`, epsilon thresholds (`abs(x) < 1e-9`), or range comparisons (`<=`, `>=`).
- **Finite & Range Validation:** All ingested floating-point telemetry values (`lat`, `lng`, `speed`, `course`, `altitude`, `progress`) and parsed HTTP headers (`Retry-After`) must be checked for `NaN` and `Inf` (`math.isnan()`, `math.isinf()`, or `math.isfinite()`) and clamped to physical domain bounds (`-90.0 <= lat <= 90.0`, `-180.0 <= lng <= 180.0`, `speed >= 0.0`).

### PY-MISRA-08: Structural Complexity & Function Bounds
- **Cyclomatic Complexity (McCabe):** Functions and methods should maintain a McCabe cyclomatic complexity $\le 15$ (hard ceiling $\le 25$ for top-level CLI dispatchers or state machines).
- ** Nesting Depth & Parameters:** Maximum control-flow nesting depth (`if`/`for`/`while`/`try`/`with`) must not exceed $5$ levels. Decompose deeply nested loops or parsers into focused helper functions.

### PY-MISRA-09: Deterministic Resource Lifecycle (RAII & Timeouts)
- **Mandatory Timeouts on Blocking I/O:** Every `urllib.request.urlopen()` and `sqlite3.connect()` call must specify an explicit `timeout=...` keyword argument.
- **Scoped Acquisition (`with` / `try...finally`):** File descriptors (`open()`), network streams (`urlopen()`), SQLite transactions (`db.transaction()`), and thread locks (`with self._lock:`) must use context managers or `try...finally` blocks to guarantee deterministic release on exception paths.

### PY-MISRA-10: No Recursion, No Wildcard Imports, No Runtime `assert` Guards
- **No Recursion:** Direct or mutual function recursion is forbidden across all modules to guarantee bounded call-stack depth.
- **No Wildcard Imports:** `from module import *` is strictly prohibited.
- **No `assert` in Production Logic:** Never use `assert` statements in production modules (`train_tracker.py`, `storage/`, `tui/`) to validate runtime inputs or state, because `python -O` strips all `assert` statements. Raise explicit `ValueError`, `TypeError`, or domain exceptions instead.

---

## 3. Compliance Verification

Run the zero-dependency static safety analyzer before committing any changes:

```bash
python3 .agents/skills/static-safety-analyzer/scripts/safety_ast_audit.py --strict
```
