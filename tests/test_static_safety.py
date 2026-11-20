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


if __name__ == "__main__":
    unittest.main()
