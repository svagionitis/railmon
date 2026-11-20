#!/usr/bin/env python3
"""
Zero-Dependency MISRA C++ / AUTOSAR C++14 / NASA JPL Static Safety AST Auditor for Python.

Audits Python source files against the PY-MISRA-01 through PY-MISRA-10 safety rules:
  - PY-MISRA-01: Forbidden dynamic execution/deserialization (eval, exec, compile, pickle, shell=True)
  - PY-MISRA-02: Unbounded queues (queue.Queue without maxsize > 0)
  - PY-MISRA-03: Exception hygiene (no bare except:, no silent except Exception: pass)
  - PY-MISRA-04: Consistent explicit return paths (no mixed implicit None / value returns)
  - PY-MISRA-05: No mutable default arguments (list, dict, set literals/calls)
  - PY-MISRA-06: 100% parameter and return type annotation coverage in production modules
  - PY-MISRA-07: No exact float literal equality (== / != against float literal)
  - PY-MISRA-08: Cyclomatic complexity (McCabe) and control-flow nesting depth bounds
  - PY-MISRA-09: Mandatory explicit timeout=... on urlopen() and sqlite3.connect()
  - PY-MISRA-10: No recursion, no wildcard imports (import *), no runtime assert in production code
"""

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path
import re
import sys
from typing import Dict, List, Optional, Sequence, Set, Tuple

DEFAULT_PRODUCTION_TARGETS: Tuple[str, ...] = ("train_tracker.py", "storage", "tui")
DEFAULT_TEST_TARGETS: Tuple[str, ...] = ("tests",)

FORBIDDEN_CALLS: Dict[str, str] = {
    "eval": "Dynamic code execution via eval() is prohibited",
    "exec": "Dynamic code execution via exec() is prohibited",
    "compile": "Dynamic code compilation via compile() is prohibited",
    "pickle.load": "Untrusted deserialization via pickle.load() is prohibited",
    "pickle.loads": "Untrusted deserialization via pickle.loads() is prohibited",
    "marshal.load": "Untrusted deserialization via marshal.load() is prohibited",
    "marshal.loads": "Untrusted deserialization via marshal.loads() is prohibited",
}

QUEUE_CONSTRUCTORS: Set[str] = {
    "Queue",
    "LifoQueue",
    "PriorityQueue",
    "queue.Queue",
    "queue.LifoQueue",
    "queue.PriorityQueue",
}

TIMEOUT_REQUIRED_CALLS: Set[str] = {
    "urlopen",
    "request.urlopen",
    "urllib.request.urlopen",
    "sqlite3.connect",
}

INLINE_IGNORE_RE = re.compile(r"#\s*safety:\s*ignore(?:\[([A-Za-z0-9_,\s-]+)\])?")


@dataclass(frozen=True)
class SafetyViolation:
    """Represents a single static analysis rule violation."""

    rule_id: str
    severity: str  # "ERROR" or "WARNING"
    filepath: str
    line: int
    col: int
    symbol: str
    message: str

    def format(self) -> str:
        """Format violation as a standard compiler/linter diagnostic line."""
        return (
            f"{self.filepath}:{self.line}:{self.col}: "
            f"[{self.severity}] {self.rule_id} ({self.symbol}): {self.message}"
        )


def _get_call_name(node: ast.Call) -> str:
    """Resolve dotted name of an AST Call node if statically determinable."""
    parts: List[str] = []
    curr: ast.AST = node.func
    while isinstance(curr, ast.Attribute):
        parts.append(curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        parts.append(curr.id)
        return ".".join(reversed(parts))
    return ""


def _compute_mccabe_complexity(func_node: ast.AST) -> int:
    """Compute standard McCabe cyclomatic complexity (compatible with flake8-mccabe / ruff C901)."""
    complexity = 1
    for child in ast.walk(func_node):
        if isinstance(
            child,
            (
                ast.If,
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.IfExp,
                ast.ExceptHandler,
            ),
        ):
            complexity += 1
        elif isinstance(child, ast.comprehension):
            complexity += 1 + len(child.ifs)
    return complexity


def _compute_max_nesting_depth(node: ast.AST, current_depth: int = 0) -> int:
    """Compute maximum control-flow nesting depth inside a function body (flat elif chains count at the same level)."""
    max_d = current_depth
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(child, ast.If):
            # Body of if increments depth; elif in orelse stays at current_depth + 1
            next_depth = current_depth + 1
            for stmt in child.body:
                d = _compute_max_nesting_depth(stmt, next_depth)
                if d > max_d:
                    max_d = d
            for stmt in child.orelse:
                if isinstance(stmt, ast.If):
                    d = _compute_max_nesting_depth(stmt, current_depth)
                else:
                    d = _compute_max_nesting_depth(stmt, next_depth)
                if d > max_d:
                    max_d = d
            continue
        is_branch = isinstance(
            child,
            (
                ast.For,
                ast.AsyncFor,
                ast.While,
                ast.Try,
                ast.With,
                ast.AsyncWith,
            ),
        )
        next_depth = current_depth + (1 if is_branch else 0)
        child_max = _compute_max_nesting_depth(child, next_depth)
        if child_max > max_d:
            max_d = child_max
    return max_d


def _is_silent_pass_body(body: Sequence[ast.stmt]) -> bool:
    """Return True if an exception handler body contains only pass or ellipsis."""
    effective = [
        stmt
        for stmt in body
        if not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        )
    ]
    if not effective:
        return True
    if len(effective) == 1:
        stmt = effective[0]
        if isinstance(stmt, ast.Pass):
            return True
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and stmt.value.value is Ellipsis
        ):
            return True
    return False


def _collect_direct_returns(func_node: ast.AST) -> List[ast.Return]:
    """Collect all Return statements belonging directly to func_node (excluding nested functions)."""
    returns: List[ast.Return] = []

    def _visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Return):
                returns.append(child)
            _visit(child)

    _visit(func_node)
    return returns


def _is_direct_recursive(func_node: ast.FunctionDef, in_class: bool) -> bool:
    """Check if a function calls itself directly."""
    func_name = func_node.name
    for child in ast.walk(func_node):
        if child is func_node:
            continue
        if isinstance(child, ast.Call):
            if not in_class and isinstance(child.func, ast.Name) and child.func.id == func_name:
                return True
            if (
                in_class
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == func_name
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id in ("self", "cls")
            ):
                return True
    return False


class SafetyASTVisitor(ast.NodeVisitor):
    """AST Visitor enforcing PY-MISRA-01 through PY-MISRA-10 rules."""

    def __init__(
        self,
        filepath: str,
        source_lines: List[str],
        is_test_file: bool = False,
        max_complexity: int = 25,
        warn_complexity: int = 15,
        max_nesting: int = 5,
    ):
        self.filepath = filepath
        self.source_lines = source_lines
        self.is_test_file = is_test_file
        self.max_complexity = max_complexity
        self.warn_complexity = warn_complexity
        self.max_nesting = max_nesting
        self.violations: List[SafetyViolation] = []
        self._class_stack: List[str] = []
        self._func_stack: List[str] = []

    def _is_ignored(self, line: int, rule_id: str) -> bool:
        """Check if source line has a `# safety: ignore` pragma covering rule_id."""
        if 1 <= line <= len(self.source_lines):
            src = self.source_lines[line - 1]
            match = INLINE_IGNORE_RE.search(src)
            if match:
                rules_raw = match.group(1)
                if not rules_raw:
                    return True
                allowed = {r.strip().upper() for r in rules_raw.split(",")}
                return rule_id.upper() in allowed
        return False

    def _add(
        self,
        rule_id: str,
        severity: str,
        node: ast.AST,
        symbol: str,
        message: str,
    ) -> None:
        line = getattr(node, "lineno", 1)
        col = getattr(node, "col_offset", 0) + 1
        if self._is_ignored(line, rule_id):
            return
        self.violations.append(
            SafetyViolation(
                rule_id=rule_id,
                severity=severity,
                filepath=self.filepath,
                line=line,
                col=col,
                symbol=symbol,
                message=message,
            )
        )

    def _current_scope(self, name: str) -> str:
        prefix = ".".join(self._class_stack + self._func_stack)
        return f"{prefix}.{name}" if prefix else name

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # PY-MISRA-10: No wildcard imports
        for alias in node.names:
            if alias.name == "*":
                mod = node.module or ""
                self._add(
                    "PY-MISRA-10",
                    "ERROR",
                    node,
                    f"from {mod} import *",
                    "Wildcard imports ('import *') are prohibited; import explicit symbols.",
                )
        self.generic_visit(node)

    def visit_Assert(self, node: ast.Assert) -> None:
        # PY-MISRA-10: No runtime assert in production code
        if not self.is_test_file:
            self._add(
                "PY-MISRA-10",
                "ERROR",
                node,
                "assert",
                "Runtime 'assert' statements are prohibited in production code (stripped under python -O). Raise ValueError/TypeError instead.",
            )
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        # PY-MISRA-07: No exact float literal equality (== or !=)
        if not self.is_test_file:
            operands = [node.left] + list(node.comparators)
            for idx, op in enumerate(node.ops):
                if isinstance(op, (ast.Eq, ast.NotEq)):
                    left_op = operands[idx]
                    right_op = operands[idx + 1]
                    if self._is_float_literal(left_op) or self._is_float_literal(right_op):
                        self._add(
                            "PY-MISRA-07",
                            "ERROR",
                            node,
                            "float-equality",
                            "Direct equality/inequality comparison (== / !=) against a float literal is prohibited; use math.isclose(), abs(x) < eps, or range comparisons.",
                        )
        self.generic_visit(node)

    @staticmethod
    def _is_float_literal(node: ast.AST) -> bool:
        if isinstance(node, ast.Constant) and isinstance(node.value, float):
            return True
        if (
            isinstance(node, ast.UnaryOp)
            and isinstance(node.op, (ast.UAdd, ast.USub))
            and isinstance(node.operand, ast.Constant)
            and isinstance(node.operand.value, float)
        ):
            return True
        return False

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _get_call_name(node)

        # PY-MISRA-01: Forbidden dynamic execution / deserialization
        if call_name in FORBIDDEN_CALLS:
            self._add(
                "PY-MISRA-01",
                "ERROR",
                node,
                call_name,
                FORBIDDEN_CALLS[call_name],
            )

        # PY-MISRA-01: subprocess with shell=True
        if call_name.startswith("subprocess.") or call_name in ("Popen", "run", "call", "check_output", "check_call"):
            for kw in node.keywords:
                if (
                    kw.arg == "shell"
                    and isinstance(kw.value, ast.Constant)
                    and kw.value.value is True
                ):
                    self._add(
                        "PY-MISRA-01",
                        "ERROR",
                        node,
                        f"{call_name}(shell=True)",
                        "Subprocess execution with shell=True is prohibited.",
                    )

        # PY-MISRA-02: queue.Queue must specify maxsize > 0
        if not self.is_test_file and call_name in QUEUE_CONSTRUCTORS:
            has_maxsize = False
            if node.args:
                has_maxsize = True
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, int) and arg0.value <= 0:
                    has_maxsize = False
            for kw in node.keywords:
                if kw.arg == "maxsize":
                    has_maxsize = True
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int) and kw.value.value <= 0:
                        has_maxsize = False
            if not has_maxsize:
                self._add(
                    "PY-MISRA-02",
                    "ERROR",
                    node,
                    call_name,
                    "Inter-thread Queue must specify an explicit positive 'maxsize' bound.",
                )

        # PY-MISRA-09: urlopen() and sqlite3.connect() must pass explicit timeout=...
        if not self.is_test_file and call_name in TIMEOUT_REQUIRED_CALLS:
            has_timeout = any(kw.arg == "timeout" for kw in node.keywords)
            if not has_timeout and len(node.args) >= 3 and "urlopen" in call_name:
                has_timeout = True
            if not has_timeout and len(node.args) >= 2 and call_name == "sqlite3.connect":
                has_timeout = True
            if not has_timeout:
                self._add(
                    "PY-MISRA-09",
                    "ERROR",
                    node,
                    call_name,
                    f"Blocking I/O call '{call_name}()' must specify an explicit 'timeout=...' argument.",
                )

        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        current_func = self._func_stack[-1] if self._func_stack else ""
        is_cleanup_method = current_func in ("__del__", "close", "__exit__")

        for handler in node.handlers:
            # PY-MISRA-03: Bare except:
            if handler.type is None:
                self._add(
                    "PY-MISRA-03",
                    "ERROR",
                    handler,
                    "bare-except",
                    "Bare 'except:' clause is prohibited; catch specific exception types.",
                )
            elif not self.is_test_file and not is_cleanup_method:
                # PY-MISRA-03: Silent broad except Exception: pass
                is_broad = isinstance(handler.type, ast.Name) and handler.type.id in (
                    "Exception",
                    "BaseException",
                )
                if is_broad and _is_silent_pass_body(handler.body):
                    self._add(
                        "PY-MISRA-03",
                        "WARNING",
                        handler,
                        f"except {handler.type.id}: pass",
                        f"Silent 'except {handler.type.id}: pass' swallows errors without diagnostic logging or specific exception narrowing.",
                    )

        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._check_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._check_function(node)

    def _check_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        scope_name = self._current_scope(node.name)
        in_class = len(self._class_stack) > 0

        # PY-MISRA-05: No mutable default arguments
        all_defaults = list(node.args.defaults) + [
            d for d in node.args.kw_defaults if d is not None
        ]
        for default_node in all_defaults:
            if isinstance(default_node, (ast.List, ast.Dict, ast.Set)):
                self._add(
                    "PY-MISRA-05",
                    "ERROR",
                    default_node,
                    scope_name,
                    "Mutable default argument (list/dict/set literal) is prohibited; use None or an immutable tuple/frozenset.",
                )
            elif isinstance(default_node, ast.Call) and _get_call_name(default_node) in (
                "list",
                "dict",
                "set",
            ):
                self._add(
                    "PY-MISRA-05",
                    "ERROR",
                    default_node,
                    scope_name,
                    "Mutable default argument (list()/dict()/set()) is prohibited; use None or an immutable tuple/frozenset.",
                )

        # PY-MISRA-06: Type annotation coverage in production code
        if not self.is_test_file:
            if node.returns is None:
                self._add(
                    "PY-MISRA-06",
                    "ERROR",
                    node,
                    scope_name,
                    f"Function '{scope_name}' is missing an explicit return type annotation.",
                )

            all_args = (
                list(node.args.posonlyargs)
                + list(node.args.args)
                + list(node.args.kwonlyargs)
            )
            if node.args.vararg:
                all_args.append(node.args.vararg)
            if node.args.kwarg:
                all_args.append(node.args.kwarg)

            for idx, arg in enumerate(all_args):
                if idx == 0 and in_class and arg.arg in ("self", "cls"):
                    continue
                if arg.annotation is None:
                    self._add(
                        "PY-MISRA-06",
                        "ERROR",
                        arg,
                        f"{scope_name}({arg.arg})",
                        f"Parameter '{arg.arg}' in '{scope_name}' is missing a type annotation.",
                    )

        # PY-MISRA-04: Consistent return statements (no mixed bare `return` and `return <val>`)
        if not self.is_test_file:
            returns = _collect_direct_returns(node)
            has_value_return = any(r.value is not None for r in returns)
            has_bare_return = any(r.value is None for r in returns)
            if has_value_return and has_bare_return:
                self._add(
                    "PY-MISRA-04",
                    "ERROR",
                    node,
                    scope_name,
                    f"Function '{scope_name}' mixes bare 'return' and value 'return <expr>'; use explicit 'return None'.",
                )

        # PY-MISRA-08: Cyclomatic complexity & nesting depth
        if not self.is_test_file:
            mccabe = _compute_mccabe_complexity(node)
            if mccabe > self.max_complexity:
                self._add(
                    "PY-MISRA-08",
                    "ERROR",
                    node,
                    scope_name,
                    f"McCabe cyclomatic complexity is {mccabe} (exceeds hard limit of {self.max_complexity}).",
                )
            elif mccabe > self.warn_complexity:
                self._add(
                    "PY-MISRA-08",
                    "WARNING",
                    node,
                    scope_name,
                    f"McCabe cyclomatic complexity is {mccabe} (advisory threshold is {self.warn_complexity}).",
                )

            nesting = _compute_max_nesting_depth(node, 0)
            if nesting > self.max_nesting:
                self._add(
                    "PY-MISRA-08",
                    "WARNING",
                    node,
                    scope_name,
                    f"Control-flow nesting depth is {nesting} (exceeds threshold of {self.max_nesting}).",
                )

        # PY-MISRA-10: No direct recursion
        if isinstance(node, ast.FunctionDef) and _is_direct_recursive(node, in_class):
            self._add(
                "PY-MISRA-10",
                "ERROR",
                node,
                scope_name,
                f"Recursive call detected in '{scope_name}'; recursion is prohibited by NASA JPL / MISRA rules.",
            )

        self._func_stack.append(node.name)
        self.generic_visit(node)
        self._func_stack.pop()


def collect_python_files(paths: Sequence[str]) -> List[Path]:
    """Resolve file and directory arguments into a sorted list of .py files."""
    files: List[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_file() and p.suffix == ".py":
            files.append(p)
        elif p.is_dir():
            files.extend(sorted(p.rglob("*.py")))
    return sorted(set(files))


def audit_file(
    filepath: Path,
    max_complexity: int = 25,
    warn_complexity: int = 15,
    max_nesting: int = 5,
) -> List[SafetyViolation]:
    """Parse and audit a single Python file against PY-MISRA rules."""
    rel_str = str(filepath)
    is_test = "tests" in filepath.parts or filepath.name.startswith("test_")
    source = filepath.read_text(encoding="utf-8")
    source_lines = source.splitlines()

    try:
        tree = ast.parse(source, filename=rel_str)
    except SyntaxError as exc:
        return [
            SafetyViolation(
                rule_id="PY-SYNTAX",
                severity="ERROR",
                filepath=rel_str,
                line=exc.lineno or 1,
                col=exc.offset or 1,
                symbol="SyntaxError",
                message=str(exc.msg),
            )
        ]

    visitor = SafetyASTVisitor(
        filepath=rel_str,
        source_lines=source_lines,
        is_test_file=is_test,
        max_complexity=max_complexity,
        warn_complexity=warn_complexity,
        max_nesting=max_nesting,
    )
    visitor.visit(tree)
    return visitor.violations


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point for the MISRA/AUTOSAR Python AST Safety Auditor."""
    parser = argparse.ArgumentParser(
        description="MISRA C++ / AUTOSAR C++14 / NASA JPL Static Safety AST Auditor for Python."
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help="Files or directories to audit (defaults to train_tracker.py, storage, tui, tests).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (exit code 1 on any warning or error).",
    )
    parser.add_argument(
        "--max-complexity",
        type=int,
        default=25,
        help="Hard error ceiling for McCabe cyclomatic complexity (default: 25).",
    )
    parser.add_argument(
        "--warn-complexity",
        type=int,
        default=25,
        help="Warning threshold for McCabe cyclomatic complexity (default: 25; set to 15 for advisory report).",
    )
    parser.add_argument(
        "--max-nesting",
        type=int,
        default=5,
        help="Maximum control-flow nesting depth threshold (default: 5).",
    )
    args = parser.parse_args(argv)

    targets = (
        list(args.targets)
        if args.targets
        else list(DEFAULT_PRODUCTION_TARGETS) + list(DEFAULT_TEST_TARGETS)
    )
    py_files = collect_python_files(targets)

    all_violations: List[SafetyViolation] = []
    for fpath in py_files:
        all_violations.extend(
            audit_file(
                fpath,
                max_complexity=args.max_complexity,
                warn_complexity=args.warn_complexity,
                max_nesting=args.max_nesting,
            )
        )

    errors = [v for v in all_violations if v.severity == "ERROR"]
    warnings = [v for v in all_violations if v.severity == "WARNING"]

    print("=" * 78)
    print("  RAILMON STATIC SAFETY ANALYZER (MISRA / AUTOSAR / NASA JPL AST GATE)")
    print("=" * 78)
    print(f"  Files Audited : {len(py_files)}")
    print(f"  Errors        : {len(errors)}")
    print(f"  Warnings      : {len(warnings)}")
    print("-" * 78)

    if all_violations:
        for v in all_violations:
            print(v.format())
        print("-" * 78)
    else:
        print("  [PASS] Zero safety violations or warnings detected across all files.")
        print("-" * 78)

    if errors or (args.strict and warnings):
        print("  RESULT: FAILED")
        return 1

    print("  RESULT: PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
