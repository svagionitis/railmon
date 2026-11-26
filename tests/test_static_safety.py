"""Unit tests for the MISRA / AUTOSAR / NASA JPL Static Safety AST Auditor."""

from pathlib import Path
import tempfile
import textwrap
import unittest

import importlib.util

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDITOR_PATH = (
    REPO_ROOT
    / ".agents"
    / "skills"
    / "static-safety-analyzer"
    / "scripts"
    / "safety_ast_audit.py"
)


def _load_auditor_module():
    spec = importlib.util.spec_from_file_location("safety_ast_audit", AUDITOR_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load auditor script from {AUDITOR_PATH}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestStaticSafetyAnalyzer(unittest.TestCase):
    """Test suite verifying PY-MISRA-01 through PY-MISRA-10 static AST enforcement."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.auditor = _load_auditor_module()

    def _audit_snippet(self, code: str, filename: str = "sample_prod.py"):
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = Path(tmpdir) / filename
            fpath.write_text(textwrap.dedent(code), encoding="utf-8")
            return self.auditor.audit_file(fpath)

    def test_codebase_passes_strict_safety_gate(self) -> None:
        """TC-SAFE-01: Full railmon production and test codebase passes strict MISRA/AUTOSAR AST gate."""
        exit_code = self.auditor.main(["--strict"])
        self.assertEqual(exit_code, 0, "Codebase failed strict MISRA/AUTOSAR AST safety audit")

    def test_detects_forbidden_dynamic_execution_and_shell(self) -> None:
        """TC-SAFE-02: PY-MISRA-01 flags eval(), exec(), pickle.loads(), and subprocess(shell=True)."""
        violations = self._audit_snippet(
            """
            import pickle
            import subprocess

            def bad_exec(data: bytes) -> None:
                eval("1 + 1")
                pickle.loads(data)
                subprocess.run("ls", shell=True)
            """
        )
        rules = [v.rule_id for v in violations]
        self.assertEqual(rules.count("PY-MISRA-01"), 3)

    def test_detects_unbounded_queue(self) -> None:
        """TC-SAFE-03: PY-MISRA-02 flags queue.Queue() without positive maxsize."""
        violations = self._audit_snippet(
            """
            import queue

            def make_queues() -> None:
                q_bad = queue.Queue()
                q_zero = queue.Queue(maxsize=0)
                q_good = queue.Queue(maxsize=100)
            """
        )
        rules = [v.rule_id for v in violations]
        self.assertEqual(rules.count("PY-MISRA-02"), 2)

    def test_detects_bare_and_silent_exceptions(self) -> None:
        """TC-SAFE-04: PY-MISRA-03 flags bare except: and silent except Exception: pass."""
        violations = self._audit_snippet(
            """
            def risky() -> None:
                try:
                    x = 1
                except:
                    pass
                try:
                    y = 2
                except Exception:
                    pass
            """
        )
        rule_severities = [(v.rule_id, v.severity) for v in violations]
        self.assertIn(("PY-MISRA-03", "ERROR"), rule_severities)
        self.assertIn(("PY-MISRA-03", "WARNING"), rule_severities)

    def test_detects_inconsistent_returns_and_mutable_defaults(self) -> None:
        """TC-SAFE-05: PY-MISRA-04 and PY-MISRA-05 flag mixed return paths and mutable defaults."""
        violations = self._audit_snippet(
            """
            from typing import List, Optional

            def bad_fn(flag: bool, items: List[int] = []) -> Optional[int]:
                if flag:
                    return
                return 42
            """
        )
        rules = {v.rule_id for v in violations}
        self.assertIn("PY-MISRA-04", rules)
        self.assertIn("PY-MISRA-05", rules)

    def test_detects_missing_types_and_float_equality(self) -> None:
        """TC-SAFE-06: PY-MISRA-06 and PY-MISRA-07 flag missing type annotations and float equality."""
        violations = self._audit_snippet(
            """
            def untyped(lat):
                return lat == 0.0
            """
        )
        rules = {v.rule_id for v in violations}
        self.assertIn("PY-MISRA-06", rules)
        self.assertIn("PY-MISRA-07", rules)

    def test_detects_missing_io_timeouts_and_recursion(self) -> None:
        """TC-SAFE-07: PY-MISRA-09 and PY-MISRA-10 flag missing I/O timeouts, assert, and recursion."""
        violations = self._audit_snippet(
            """
            import sqlite3
            import urllib.request

            def rec(n: int) -> int:
                assert n >= 0
                urllib.request.urlopen("https://railway.gov.gr")
                sqlite3.connect("test.db")
                return rec(n - 1)
            """
        )
        rules = [v.rule_id for v in violations]
        self.assertEqual(rules.count("PY-MISRA-09"), 2)
        self.assertEqual(rules.count("PY-MISRA-10"), 2)

    def test_default_production_targets_cover_entire_repo(self) -> None:
        """TC-SAFE-08: DEFAULT_PRODUCTION_TARGETS covers all production packages, modules, and scripts."""
        expected = {
            "train_tracker.py",
            "railmon_watchdog.py",
            "railmon_http.py",
            "storage",
            "tui",
            "server",
            "exporter",
            "gtfs_rt",
            "scripts",
        }
        self.assertTrue(expected.issubset(set(self.auditor.DEFAULT_PRODUCTION_TARGETS)))
        prod_files = self.auditor.collect_python_files(
            [str(REPO_ROOT / target) for target in self.auditor.DEFAULT_PRODUCTION_TARGETS]
        )
        self.assertGreaterEqual(len(prod_files), 38)

    def test_detects_import_alias_evasion(self) -> None:
        """TC-SAFE-09: Import aliases cannot bypass PY-MISRA-01, PY-MISRA-02, or PY-MISRA-09."""
        violations = self._audit_snippet(
            """
            import pickle as p
            from subprocess import run as r
            from queue import Queue as Q
            from urllib.request import urlopen as u
            from sqlite3 import connect as c

            def evasive(raw: bytes) -> None:
                p.loads(raw)
                r("ls", shell=True)
                q = Q()
                u("https://railway.gov.gr")
                c(":memory:")
            """
        )
        rules = [v.rule_id for v in violations]
        self.assertEqual(rules.count("PY-MISRA-01"), 2)
        self.assertEqual(rules.count("PY-MISRA-02"), 1)
        self.assertEqual(rules.count("PY-MISRA-09"), 2)

    def test_rejects_explicit_timeout_none(self) -> None:
        """TC-SAFE-10: PY-MISRA-09 rejects timeout=None (keyword or positional)."""
        violations = self._audit_snippet(
            """
            import sqlite3
            import urllib.request

            def bad_timeouts() -> None:
                urllib.request.urlopen("https://railway.gov.gr", timeout=None)
                urllib.request.urlopen("https://railway.gov.gr", None, None)
                sqlite3.connect(":memory:", timeout=None)
                sqlite3.connect(":memory:", None)
            """
        )
        rules = [v.rule_id for v in violations]
        self.assertEqual(rules.count("PY-MISRA-09"), 4)

    def test_detects_tuple_broad_silent_exception(self) -> None:
        """TC-SAFE-11: PY-MISRA-03 flags silent except (Exception, ...) and (BaseException,): pass."""
        violations = self._audit_snippet(
            """
            def swallow_tuples() -> None:
                try:
                    x = 1
                except (Exception, ValueError):
                    pass
                try:
                    y = 2
                except (BaseException,):
                    pass
            """
        )
        rule_severities = [(v.rule_id, v.severity) for v in violations]
        self.assertEqual(rule_severities.count(("PY-MISRA-03", "WARNING")), 2)

    def test_counts_match_statement_complexity_and_nesting(self) -> None:
        """TC-SAFE-12: PY-MISRA-08 counts ast.Match cases in complexity and nesting depth."""
        violations = self._audit_snippet(
            """
            def deep_match(val: int, flag: bool) -> int:
                for _ in range(1):
                    while flag:
                        with open("x", "r", encoding="utf-8"):
                            try:
                                for __ in range(1):
                                    match val:
                                        case 1:
                                            return 1
                                        case 2:
                                            return 2
                                        case _:
                                            return 0
                            except OSError:
                                return -1
                return 0
            """
        )
        nesting_msgs = [v.message for v in violations if v.rule_id == "PY-MISRA-08"]
        self.assertTrue(any("nesting depth" in m for m in nesting_msgs))

        import ast as _ast

        tree = _ast.parse(
            textwrap.dedent(
                """
                def match_cc(val: int) -> int:
                    match val:
                        case 1:
                            return 1
                        case 2:
                            return 2
                        case 3:
                            return 3
                        case _:
                            return 0
                """
            )
        )
        self.assertEqual(self.auditor._compute_mccabe_complexity(tree.body[0]), 4)
        self.assertEqual(self.auditor._compute_max_nesting_depth(tree.body[0]), 1)

    def test_detects_async_direct_recursion(self) -> None:
        """TC-SAFE-13: PY-MISRA-10 flags direct recursion inside AsyncFunctionDef."""
        violations = self._audit_snippet(
            """
            async def async_rec(n: int) -> int:
                if n <= 0:
                    return 0
                return await async_rec(n - 1)
            """
        )
        rules = [v.rule_id for v in violations]
        self.assertIn("PY-MISRA-10", rules)

    def test_ancestor_tests_directory_does_not_disable_production_rules(self) -> None:
        """TC-SAFE-14: Cloning under a path containing 'tests' must not suppress production rules."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "tests" / "railmon_clone"
            repo_dir.mkdir(parents=True)
            prod_file = repo_dir / "prod_mod.py"
            prod_file.write_text(
                textwrap.dedent(
                    """
                    def bad_prod(x):
                        assert x > 0
                        return x == 0.0
                    """
                ),
                encoding="utf-8",
            )
            violations = self.auditor.audit_file(prod_file, base_dir=repo_dir)
        rules = {v.rule_id for v in violations}
        self.assertIn("PY-MISRA-06", rules)
        self.assertIn("PY-MISRA-07", rules)
        self.assertIn("PY-MISRA-10", rules)

    def test_auditor_script_passes_strict_self_audit(self) -> None:
        """TC-SAFE-15: safety_ast_audit.py itself passes strict AST audit with zero violations."""
        violations = self.auditor.audit_file(
            AUDITOR_PATH,
            max_complexity=25,
            warn_complexity=25,
            max_nesting=5,
            base_dir=REPO_ROOT,
        )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
