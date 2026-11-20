---
name: ci-quality-gate
description: >-
  Runs the full production quality gate: pytest test suite with coverage,
  mypy strict type checking, flake8 linting, and black code formatting verification.
  Use this skill before committing code changes, creating releases, or validating pull requests.
---

# CI Quality Gate

Automates execution of all code quality, type safety, test coverage, and style checks required for production readiness.

## When to Apply
- Prior to submitting any significant feature or bug fix.
- When preparing a pull request or release tag.
- When verifying that a refactoring did not introduce type or lint regressions.

## Quality Checks Procedure

### 0. MISRA / AUTOSAR / NASA JPL Static Safety AST Gate
Run the zero-dependency static safety AST auditor first:
```bash
python3 .agents/skills/static-safety-analyzer/scripts/safety_ast_audit.py
```
**Pass Criteria:**
- Zero safety rule errors (`PY-MISRA-01` through `PY-MISRA-10`).

### 1. Test Suite & Coverage Verification
Run the complete unit and integration test suite:
```bash
# Using pytest with coverage (when dependencies are installed):
pytest tests/ -v --cov=. --cov-report=term-missing
# Or using standard library unittest runner (zero-dependency environment):
python -m unittest discover tests -v
```
**Pass Criteria:**
- 100% test pass rate (0 failures, 0 errors).
- No regression in branch or line coverage.

### 2. Static Type Checking with MyPy
Ensure type safety across the entire codebase:
```bash
mypy --config-file .editorconfig train_tracker.py storage/ tui/ tests/
```
*(If no specific mypy configuration exists, run `mypy train_tracker.py`)*

**Pass Criteria:**
- Zero type errors reported.
- All public function arguments and return types must be fully typed.

### 3. Style & PEP 8 Linting with Flake8
Run flake8 to detect formatting issues, unused imports, or potential bugs:
```bash
flake8 train_tracker.py storage/ tui/ tests/ --max-line-length=100 --extend-ignore=E203,W503
```
**Pass Criteria:**
- Zero flake8 warnings or errors.

### 4. Code Formatting Check with Black
Verify that code conforms to the standard black code style:
```bash
black --check --diff train_tracker.py storage/ tui/ tests/
```
**Pass Criteria:**
- Clean diff (no reformatting required). If formatting errors occur, run `black train_tracker.py storage/ tui/ tests/` to fix.

## Reporting Results
Summarize the pass/fail status of each gate:
- Tests & Coverage: Passed / Failed
- MyPy: Passed / Failed
- Flake8: Passed / Failed
- Black: Clean / Unformatted
