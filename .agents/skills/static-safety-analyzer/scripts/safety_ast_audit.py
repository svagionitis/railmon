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

DEFAULT_PRODUCTION_TARGETS: Tuple[str, ...] = (
    "train_tracker.py",
    "railmon_watchdog.py",
    "railmon_http.py",
    "storage",
    "tui",
    "server",
    "exporter",
    "gtfs_rt",
    "scripts",
)
DEFAULT_TEST_TARGETS: Tuple[str, ...] = ("tests",)

FORBIDDEN_CALLS: Dict[str, str] = {
    "eval": "Dynamic code execution via eval() is prohibited",
    "builtins.eval": "Dynamic code execution via eval() is prohibited",
    "exec": "Dynamic code execution via exec() is prohibited",
    "builtins.exec": "Dynamic code execution via exec() is prohibited",
    "compile": "Dynamic code compilation via compile() is prohibited",
    "builtins.compile": "Dynamic code compilation via compile() is prohibited",
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


def _is_wildcard_case(case: ast.match_case) -> bool:
    """Return True if a match_case is an unguarded wildcard catch-all (`case _:`)."""
    return (
        case.guard is None
        and isinstance(case.pattern, ast.MatchAs)
        and case.pattern.pattern is None
        and case.pattern.name is None
    )


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
        elif isinstance(child, ast.Match):
            complexity += sum(1 for case in child.cases if not _is_wildcard_case(case))
    return complexity


def _compute_max_nesting_depth(node: ast.AST, current_depth: int = 0) -> int:
    """Compute maximum control-flow nesting depth iteratively (flat elif chains share one level)."""
    max_d = current_depth
    branch_types = (
        ast.For,
        ast.AsyncFor,
        ast.While,
        ast.Try,
        ast.With,
        ast.AsyncWith,
        ast.Match,
    )
    stack: List[Tuple[ast.AST, int]] = [(node, current_depth)]

    while stack:
        curr, depth = stack.pop()
        if depth > max_d:
            max_d = depth
        for child in ast.iter_child_nodes(curr):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.If):
                next_depth = depth + 1
                if next_depth > max_d:
                    max_d = next_depth
                for stmt in child.body:
                    stack.append((stmt, next_depth))
                for stmt in child.orelse:
                    branch_depth = depth if isinstance(stmt, ast.If) else next_depth
                    stack.append((stmt, branch_depth))
                continue
            next_depth = depth + (1 if isinstance(child, branch_types) else 0)
            stack.append((child, next_depth))

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


def _extract_broad_exception_name(exc_type: ast.expr) -> Optional[str]:
    """Return 'Exception' or 'BaseException' if exc_type catches a broad base class (directly or in a tuple)."""
    if isinstance(exc_type, ast.Name) and exc_type.id in ("Exception", "BaseException"):
        return exc_type.id
    if isinstance(exc_type, ast.Tuple):
        for elt in exc_type.elts:
            if isinstance(elt, ast.Name) and elt.id in ("Exception", "BaseException"):
                return elt.id
    return None


def _is_none_constant(node: ast.AST) -> bool:
    """Return True if an AST expression is the literal None constant."""
    return isinstance(node, ast.Constant) and node.value is None


def _collect_direct_returns(func_node: ast.AST) -> List[ast.Return]:
    """Collect all Return statements belonging directly to func_node iteratively (excluding nested functions)."""
    returns: List[ast.Return] = []
    stack: List[ast.AST] = list(ast.iter_child_nodes(func_node))

    while stack:
        curr = stack.pop()
        if isinstance(curr, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(curr, ast.Return):
            returns.append(curr)
        stack.extend(ast.iter_child_nodes(curr))

    return returns


def _is_direct_recursive(
    func_node: ast.FunctionDef | ast.AsyncFunctionDef, in_class: bool
) -> bool:
    """Check if a sync or async function calls itself directly."""
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
    ) -> None:
        self.filepath = filepath
        self.source_lines = source_lines
        self.is_test_file = is_test_file
        self.max_complexity = max_complexity
        self.warn_complexity = warn_complexity
        self.max_nesting = max_nesting
        self.violations: List[SafetyViolation] = []
        self._class_stack: List[str] = []
        self._func_stack: List[str] = []
        self._import_aliases: Dict[str, str] = {}

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

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            bound = alias.asname if alias.asname else alias.name
            self._import_aliases[bound] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        # PY-MISRA-10: No wildcard imports + track import aliases
        mod = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                self._add(
                    "PY-MISRA-10",
                    "ERROR",
                    node,
                    f"from {mod} import *",
                    "Wildcard imports ('import *') are prohibited; import explicit symbols.",
                )
            elif mod:
                bound = alias.asname if alias.asname else alias.name
                self._import_aliases[bound] = f"{mod}.{alias.name}"
        self.generic_visit(node)

    def _resolve_call_name(self, call_name: str) -> str:
        """Resolve an AST call name through tracked import aliases."""
        if not call_name:
            return ""
        if call_name in self._import_aliases:
            return self._import_aliases[call_name]
        head, sep, tail = call_name.partition(".")
        if sep and head in self._import_aliases:
            return f"{self._import_aliases[head]}.{tail}"
        return call_name

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

    def _check_forbidden_and_shell(self, node: ast.Call, resolved_name: str) -> None:
        # PY-MISRA-01: Forbidden dynamic execution / deserialization
        if resolved_name in FORBIDDEN_CALLS:
            self._add(
                "PY-MISRA-01",
                "ERROR",
                node,
                resolved_name,
                FORBIDDEN_CALLS[resolved_name],
            )

        # PY-MISRA-01: subprocess with shell=True
        is_subproc = resolved_name.startswith("subprocess.") or resolved_name in (
            "Popen",
            "run",
            "call",
            "check_output",
            "check_call",
        )
        if is_subproc:
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
                        f"{resolved_name}(shell=True)",
                        "Subprocess execution with shell=True is prohibited.",
                    )

    def _check_queue_bound(self, node: ast.Call, resolved_name: str) -> None:
        # PY-MISRA-02: queue.Queue must specify maxsize > 0
        if self.is_test_file or resolved_name not in QUEUE_CONSTRUCTORS:
            return
        has_maxsize = False
        if node.args:
            has_maxsize = True
            arg0 = node.args[0]
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, int) and arg0.value <= 0:
                has_maxsize = False
        for kw in node.keywords:
            if kw.arg == "maxsize":
                has_maxsize = True
                if (
                    isinstance(kw.value, ast.Constant)
                    and isinstance(kw.value.value, int)
                    and kw.value.value <= 0
                ):
                    has_maxsize = False
        if not has_maxsize:
            self._add(
                "PY-MISRA-02",
                "ERROR",
                node,
                resolved_name,
                "Inter-thread Queue must specify an explicit positive 'maxsize' bound.",
            )

    def _check_io_timeout(self, node: ast.Call, resolved_name: str) -> None:
        # PY-MISRA-09: urlopen() and sqlite3.connect() must pass explicit non-None timeout=...
        if self.is_test_file or resolved_name not in TIMEOUT_REQUIRED_CALLS:
            return
        has_valid_timeout = False
        for kw in node.keywords:
            if kw.arg == "timeout":
                has_valid_timeout = not _is_none_constant(kw.value)
                break
        if not has_valid_timeout and "urlopen" in resolved_name and len(node.args) >= 3:
            has_valid_timeout = not _is_none_constant(node.args[2])
        if not has_valid_timeout and resolved_name == "sqlite3.connect" and len(node.args) >= 2:
            has_valid_timeout = not _is_none_constant(node.args[1])
        if not has_valid_timeout:
            self._add(
                "PY-MISRA-09",
                "ERROR",
                node,
                resolved_name,
                f"Blocking I/O call '{resolved_name}()' must specify an explicit non-None 'timeout=...' argument.",
            )

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _get_call_name(node)
        resolved_name = self._resolve_call_name(call_name)
        self._check_forbidden_and_shell(node, resolved_name)
        self._check_queue_bound(node, resolved_name)
        self._check_io_timeout(node, resolved_name)
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
                # PY-MISRA-03: Silent broad except Exception / (Exception, ...): pass
                broad_name = _extract_broad_exception_name(handler.type)
                if broad_name is not None and _is_silent_pass_body(handler.body):
                    self._add(
                        "PY-MISRA-03",
                        "WARNING",
                        handler,
                        f"except {broad_name}: pass",
                        f"Silent 'except {broad_name}: pass' swallows errors without diagnostic logging or specific exception narrowing.",
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

        # PY-MISRA-10: No direct recursion (sync or async)
        if _is_direct_recursive(node, in_class):
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


def _classify_test_file(filepath: Path, base_dir: Optional[Path] = None) -> bool:
    """Classify whether filepath is a test file using repo/base-dir relative path segments."""
    if filepath.name.startswith("test_"):
        return True
    rel_path = filepath
    anchor = base_dir if base_dir is not None else (Path.cwd() if filepath.is_absolute() else None)
    if anchor is not None:
        try:
            rel_path = filepath.resolve().relative_to(anchor.resolve())
        except ValueError:
            return False
    return bool(rel_path.parts) and rel_path.parts[0] == "tests"


def audit_file(
    filepath: Path,
    max_complexity: int = 25,
    warn_complexity: int = 15,
    max_nesting: int = 5,
    base_dir: Optional[Path] = None,
    is_test_file: Optional[bool] = None,
) -> List[SafetyViolation]:
    """Parse and audit a single Python file against PY-MISRA rules."""
    rel_str = str(filepath)
    is_test = is_test_file if is_test_file is not None else _classify_test_file(filepath, base_dir)
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
    default_desc = ", ".join(DEFAULT_PRODUCTION_TARGETS + DEFAULT_TEST_TARGETS)
    parser = argparse.ArgumentParser(
        description="MISRA C++ / AUTOSAR C++14 / NASA JPL Static Safety AST Auditor for Python."
    )
    parser.add_argument(
        "targets",
        nargs="*",
        help=f"Files or directories to audit (defaults to {default_desc}).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Treat warnings as errors (exit code 1 on any warning or error).",
    )
    parser.add_argument(
        "--advisory",
        action="store_true",
        help="Enable advisory McCabe complexity warnings at CC > 15 (equivalent to --warn-complexity 15).",
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
        help="Warning threshold for McCabe cyclomatic complexity (default: 25; use --advisory or 15 for advisory report).",
    )
    parser.add_argument(
        "--max-nesting",
        type=int,
        default=5,
        help="Maximum control-flow nesting depth threshold (default: 5).",
    )
    args = parser.parse_args(argv)

    effective_warn = min(args.warn_complexity, 15) if args.advisory else args.warn_complexity
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
                warn_complexity=effective_warn,
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
