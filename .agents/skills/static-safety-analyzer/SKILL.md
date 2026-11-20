---
name: static-safety-analyzer
description: >-
  Audits the Python codebase against MISRA C++, AUTOSAR C++14, SEI CERT, and
  NASA JPL Power-of-10 safety rules using a zero-dependency AST verifier and
  strict static analysis linters (ruff, mypy, flake8, bandit). Use this skill
  when auditing code safety, enforcing coding standards, reviewing pull
  requests, or validating high-assurance compliance.
---

# Static Safety Analyzer (MISRA / AUTOSAR / NASA JPL Gate)

Enforces deterministic, safety-critical coding standards across [`train_tracker.py`](../../../train_tracker.py), [`storage/`](../../../storage/), [`tui/`](../../../tui/), and [`tests/`](../../../tests/), verifying compliance with [`.agents/rules/misra-autosar-python.md`](../../rules/misra-autosar-python.md).

## When to Apply
- After authoring or refactoring any production module (`train_tracker.py`, `storage/*.py`, `tui/*.py`).
- Before committing changes, cutting a release, or signing off on a safety/quality review.
- When diagnosing resource leaks, unhandled exceptions, float precision bugs, or high cyclomatic complexity.

---

## Step-by-Step Verification Procedure

### 1. Run the Zero-Dependency MISRA / AUTOSAR Python AST Gate
Execute the built-in AST safety verifier script [`safety_ast_audit.py`](./scripts/safety_ast_audit.py), which requires only the Python 3.10+ standard library:

```bash
# Standard audit (fails on any ERROR violation):
python3 .agents/skills/static-safety-analyzer/scripts/safety_ast_audit.py

# Strict audit (fails on any ERROR or WARNING violation):
python3 .agents/skills/static-safety-analyzer/scripts/safety_ast_audit.py --strict
```

#### Rules Checked by `safety_ast_audit.py`:
1. **`PY-MISRA-01` (No Dynamic Execution / Deserialization):** Flags `eval()`, `exec()`, `compile()`, `pickle.load(s)`, `marshal.load(s)`, and `subprocess(..., shell=True)`.
2. **`PY-MISRA-02` (Bounded Queues):** Verifies that every `queue.Queue()` / `LifoQueue()` / `PriorityQueue()` specifies a positive `maxsize`.
3. **`PY-MISRA-03` (Exception Hygiene):** Flags bare `except:` blocks and silent `except Exception: pass` handlers outside cleanup destructors (`close()`, `__exit__`, `__del__`).
4. **`PY-MISRA-04` (Consistent Return Paths):** Flags functions that mix bare `return` with value `return <expr>`.
5. **`PY-MISRA-05` (No Mutable Default Arguments):** Flags `def f(x=[])`, `def f(x={})`, `def f(x=set())`.
6. **`PY-MISRA-06` (100% Type Annotation Coverage):** Verifies that every parameter and return value in production modules carries an explicit type annotation.
7. **`PY-MISRA-07` (Floating-Point Equality Safety):** Flags direct `==` or `!=` comparisons against float literals (e.g., `x == 0.0`, `lat != 0.0`).
8. **`PY-MISRA-08` (Complexity & Nesting Limits):** Computes McCabe cyclomatic complexity and control-flow nesting depth for every function.
9. **`PY-MISRA-09` (Mandatory I/O Timeouts):** Verifies that every `urlopen()` and `sqlite3.connect()` call specifies an explicit `timeout=...`.
10. **`PY-MISRA-10` (No Recursion, No Wildcard Imports, No Production `assert`):** Flags direct recursion, `from module import *`, and runtime `assert` statements in production modules.

**Pass Criteria:** Exit code `0` (`0` errors).

---

### 2. Run Strict Static Type Checking (MyPy)
When `mypy` is installed in the development environment, run strict type checking using the configuration in [`pyproject.toml`](../../../pyproject.toml):

```bash
mypy train_tracker.py storage/ tui/ tests/
```

**Pass Criteria:** Zero type errors across all modules.

---

### 3. Run Extended Static Analysis Linters (Ruff / Flake8 / Bandit)
When optional dev tools (`ruff`, `flake8`, `bandit`) are installed in the active environment:

```bash
# Ruff (checks flake8-bugbear, bandit, blind-except, return consistency, mccabe, pylint rules):
ruff check train_tracker.py storage/ tui/ tests/

# Or Flake8 + Bandit:
flake8 train_tracker.py storage/ tui/ tests/
bandit -r train_tracker.py storage/ tui/ -ll
```

---

## Remediating Violations

- **Float Equality (`PY-MISRA-07`):** Replace `val != 0.0` or `val == 0.0` with `abs(val) > 1e-9`, `abs(val) < 1e-9`, or `math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)`.
- **Silent Exception (`PY-MISRA-03`):** Narrow `except Exception:` to specific exception tuples (e.g. `except (ValueError, TypeError, OSError):`) and log at `logger.debug()` or `logger.warning()`. If a broad catch in a best-effort fallback is genuinely required, document it with `# safety: ignore[PY-MISRA-03]`.
- **High Complexity / Nesting (`PY-MISRA-08`):** Extract inner loop bodies, retry calculations, or subcommand handlers into private helper functions (`_helper_name(...)`) with full type annotations.
