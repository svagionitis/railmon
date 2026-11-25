"""Tests for package metadata, PEP 621 pyproject.toml, and single-source versioning."""

import ast
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Set

import train_tracker
from storage.recorder import RecorderConfig

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).resolve().parent.parent
_EXCLUDED_TOP_DIRS: Set[str] = {
    ".git",
    "__pycache__",
    ".agents",
    "tests",
    "scripts",
    "reference",
    "build",
    "dist",
}
_HAS_WHEEL_BUILD_TOOLS: bool = importlib.util.find_spec("setuptools") is not None


def _discover_production_py_files() -> List[Path]:
    """Return sorted repository-relative paths of all core production .py modules."""
    results: List[Path] = []
    for py_path in sorted(REPO_ROOT.rglob("*.py")):
        rel = py_path.relative_to(REPO_ROOT)
        if any(part in _EXCLUDED_TOP_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        results.append(rel)
    return results


def _parse_pyproject() -> Dict[str, Any]:
    """Parse pyproject.toml using tomllib (Python 3.11+) with regex fallback for 3.10."""
    content = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(content)

    def _extract_list(section: str, key: str) -> List[str]:
        sec_re = re.search(rf"\[{re.escape(section)}\](.*?)(?=\n\[|\Z)", content, re.DOTALL)
        if not sec_re:
            return []
        key_re = re.search(rf"{re.escape(key)}\s*=\s*\[(.*?)\]", sec_re.group(1), re.DOTALL)
        if not key_re:
            return []
        return re.findall(r'"([^"]+)"', key_re.group(1))

    return {
        "tool": {
            "setuptools": {
                "py-modules": _extract_list("tool.setuptools", "py-modules"),
                "packages": _extract_list("tool.setuptools", "packages"),
            },
            "ruff": {"src": _extract_list("tool.ruff", "src")},
            "coverage": {"run": {"source": _extract_list("tool.coverage.run", "source")}},
        }
    }


class TestPackagingAndVersioning(unittest.TestCase):
    """Test suite validating single-source versioning and pyproject.toml packaging."""

    def test_single_source_version_format(self):
        """TC-PKG-01: train_tracker.__version__ exists and conforms to SemVer 2.0.0."""
        self.assertTrue(hasattr(train_tracker, "__version__"))
        version = train_tracker.__version__
        self.assertIsInstance(version, str)
        semver_pattern = r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$"
        self.assertRegex(version, semver_pattern, f"Version {version!r} is not valid SemVer")

    def test_author_metadata_exists(self):
        """TC-PKG-02: train_tracker.__author__ is defined."""
        self.assertTrue(hasattr(train_tracker, "__author__"))
        self.assertIsInstance(train_tracker.__author__, str)
        self.assertTrue(len(train_tracker.__author__) > 0)

    def test_recorder_config_inherits_version(self):
        """TC-PKG-03: RecorderConfig.client_version dynamically matches train_tracker.__version__."""
        config = RecorderConfig()
        self.assertEqual(config.client_version, train_tracker.__version__)

    def test_pyproject_toml_exists_and_configured(self):
        """TC-PKG-04: pyproject.toml exists and specifies correct build backend and entrypoint."""
        pyproject_path = REPO_ROOT / "pyproject.toml"
        self.assertTrue(pyproject_path.exists(), "pyproject.toml not found at repo root")
        content = pyproject_path.read_text(encoding="utf-8")

        # Verify build-system
        self.assertIn('build-backend = "setuptools.build_meta"', content)
        self.assertIn("setuptools>=61.0.0", content)

        # Verify dynamic versioning
        self.assertIn('dynamic = ["version"]', content)
        self.assertIn('version = { attr = "train_tracker.__version__" }', content)

        # Verify entry points
        self.assertIn('railmon = "train_tracker:main"', content)

        # Verify package data
        self.assertIn('"tui.data" = ["*.json"]', content)

    def test_runtime_dependencies_purity(self):
        """TC-PKG-05: Runtime dependencies enforce zero PyPI dependencies on Linux/macOS."""
        pyproject_path = REPO_ROOT / "pyproject.toml"
        content = pyproject_path.read_text(encoding="utf-8")

        # Extract dependencies section
        dep_match = re.search(r"dependencies\s*=\s*\[(.*?)\]", content, re.DOTALL)
        self.assertIsNotNone(dep_match, "dependencies table not found in pyproject.toml")
        deps_block = dep_match.group(1).strip()
        lines = [line.strip().strip(",").strip("\"'") for line in deps_block.splitlines() if line.strip()]

        for dep in lines:
            if not dep:
                continue
            # The only permitted runtime dependency is windows-curses on win32
            self.assertTrue(
                "windows-curses" in dep and "win32" in dep,
                f"Unexpected runtime dependency in pyproject.toml: {dep}"
            )

    def test_package_data_files_exist(self):
        """TC-PKG-06: All GIS package data files referenced in packaging exist on filesystem."""
        expected_files = (
            "stations.json",
            "tracks.json",
            "infrastructure.json",
            "speed_limits.json",
        )
        for filename in expected_files:
            asset_file = REPO_ROOT / "tui" / "data" / filename
            self.assertTrue(asset_file.exists(), f"tui/data/{filename} missing")

    def test_server_package_configured_and_importable(self):
        """TC-PKG-07: server package is registered in pyproject.toml and importable."""
        pyproject_path = REPO_ROOT / "pyproject.toml"
        content = pyproject_path.read_text(encoding="utf-8")
        self.assertIn('"server"', content)

        import server
        self.assertEqual(server.__version__, train_tracker.__version__)
        self.assertEqual(server.__author__, train_tracker.__author__)
        self.assertTrue(hasattr(server, "SSEBroadcaster"))
        self.assertTrue(hasattr(server, "ServerConfig"))

    def test_server_runtime_zero_pypi_dependencies(self):
        """TC-PKG-08: server package imports exclusively from Python stdlib and internal packages."""
        import ast

        server_dir = REPO_ROOT / "server"
        allowed_stdlib = {
            "dataclasses",
            "enum",
            "errno",
            "gzip",
            "http",
            "json",
            "logging",
            "math",
            "os",
            "pathlib",
            "queue",
            "secrets",
            "signal",
            "socket",
            "socketserver",
            "sys",
            "threading",
            "time",
            "types",
            "typing",
            "urllib",
            "uuid",
            "webbrowser",
        }
        allowed_internal = {"server", "storage", "train_tracker", "tui", "exporter", "gtfs_rt"}
        allowed = allowed_stdlib | allowed_internal

        for py_file in server_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root_mod = alias.name.split(".")[0]
                        self.assertIn(
                            root_mod,
                            allowed,
                            f"Prohibited third-party PyPI import '{root_mod}' in {py_file.name}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        root_mod = node.module.split(".")[0]
                        self.assertIn(
                            root_mod,
                            allowed,
                            f"Prohibited third-party PyPI import '{root_mod}' in {py_file.name}",
                        )

    def test_exporter_package_configured_and_importable(self):
        """TC-PKG-09: exporter package is registered in pyproject.toml and importable."""
        pyproject_path = REPO_ROOT / "pyproject.toml"
        content = pyproject_path.read_text(encoding="utf-8")
        self.assertIn('"exporter"', content)

        import exporter
        self.assertEqual(exporter.__version__, train_tracker.__version__)
        self.assertEqual(exporter.__author__, train_tracker.__author__)
        self.assertTrue(hasattr(exporter, "MetricFamily"))
        self.assertTrue(hasattr(exporter, "MetricType"))
        self.assertTrue(hasattr(exporter, "Sample"))
        self.assertTrue(hasattr(exporter, "ExporterConfig"))
        self.assertTrue(hasattr(exporter, "MetricsFormatter"))

    def test_exporter_runtime_zero_pypi_dependencies(self):
        """TC-PKG-10: exporter package imports exclusively from Python stdlib and internal packages."""
        import ast

        exporter_dir = REPO_ROOT / "exporter"
        allowed_stdlib = {
            "dataclasses",
            "enum",
            "errno",
            "gzip",
            "http",
            "json",
            "logging",
            "math",
            "os",
            "pathlib",
            "queue",
            "re",
            "signal",
            "socket",
            "socketserver",
            "sys",
            "threading",
            "time",
            "types",
            "typing",
            "urllib",
        }
        allowed_internal = {"exporter", "server", "storage", "train_tracker", "tui"}
        allowed = allowed_stdlib | allowed_internal

        for py_file in exporter_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root_mod = alias.name.split(".")[0]
                        self.assertIn(
                            root_mod,
                            allowed,
                            f"Prohibited third-party PyPI import '{root_mod}' in {py_file.name}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        root_mod = node.module.split(".")[0]
                        self.assertIn(
                            root_mod,
                            allowed,
                            f"Prohibited third-party PyPI import '{root_mod}' in {py_file.name}",
                        )

    def test_gtfs_rt_package_configured_and_importable(self):
        """TC-PKG-11: gtfs_rt package is registered in pyproject.toml and importable."""
        pyproject_path = REPO_ROOT / "pyproject.toml"
        content = pyproject_path.read_text(encoding="utf-8")
        self.assertIn('"gtfs_rt"', content)

        import gtfs_rt

        self.assertEqual(gtfs_rt.__version__, train_tracker.__version__)
        self.assertEqual(gtfs_rt.__author__, train_tracker.__author__)
        self.assertTrue(hasattr(gtfs_rt, "FeedMessage"))
        self.assertTrue(hasattr(gtfs_rt, "GtfsRtMapper"))
        self.assertTrue(hasattr(gtfs_rt, "GtfsRtServer"))
        self.assertTrue(hasattr(gtfs_rt, "GtfsRtConfig"))

    def test_gtfs_rt_runtime_zero_pypi_dependencies(self):
        """TC-PKG-12: gtfs_rt package imports exclusively from Python stdlib and internal packages."""
        import ast

        gtfs_dir = REPO_ROOT / "gtfs_rt"
        allowed_stdlib = {
            "collections",
            "dataclasses",
            "datetime",
            "enum",
            "errno",
            "gzip",
            "http",
            "json",
            "logging",
            "math",
            "os",
            "pathlib",
            "re",
            "signal",
            "socket",
            "socketserver",
            "struct",
            "sys",
            "threading",
            "time",
            "types",
            "typing",
            "unicodedata",
            "urllib",
            "zoneinfo",
        }
        allowed_internal = {"gtfs_rt", "exporter", "server", "storage", "train_tracker", "tui"}
        allowed = allowed_stdlib | allowed_internal

        for py_file in gtfs_dir.glob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root_mod = alias.name.split(".")[0]
                        self.assertIn(
                            root_mod,
                            allowed,
                            f"Prohibited third-party PyPI import '{root_mod}' in {py_file.name}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        root_mod = node.module.split(".")[0]
                        self.assertIn(
                            root_mod,
                            allowed,
                            f"Prohibited third-party PyPI import '{root_mod}' in {py_file.name}",
                        )

    def test_watchdog_relocated_and_no_pypi_collision(self):
        """TC-PKG-13: railmon_watchdog is registered in pyproject.toml with no root watchdog.py."""
        parsed = _parse_pyproject()
        setuptools_cfg = parsed["tool"]["setuptools"]
        self.assertIn("railmon_watchdog", setuptools_cfg["py-modules"])
        self.assertNotIn("watchdog", setuptools_cfg["py-modules"])
        self.assertIn("railmon_watchdog.py", parsed["tool"]["ruff"]["src"])
        self.assertIn("railmon_watchdog", parsed["tool"]["coverage"]["run"]["source"])

        self.assertFalse(
            (REPO_ROOT / "watchdog.py").exists(),
            "Root watchdog.py must not exist (collides with PyPI watchdog package)",
        )
        self.assertTrue((REPO_ROOT / "railmon_watchdog.py").exists())

        import railmon_watchdog

        self.assertTrue(hasattr(railmon_watchdog, "WatchConfig"))
        self.assertTrue(hasattr(railmon_watchdog, "WatchdogEvaluator"))
        self.assertTrue(hasattr(railmon_watchdog, "dispatch_all_channels"))

        for rel_path in _discover_production_py_files():
            py_file = REPO_ROOT / rel_path
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        self.assertNotEqual(
                            alias.name.split(".")[0],
                            "watchdog",
                            f"Colliding 'import watchdog' found in {rel_path}",
                        )
                elif isinstance(node, ast.ImportFrom) and node.module:
                    self.assertNotEqual(
                        node.module.split(".")[0],
                        "watchdog",
                        f"Colliding 'from watchdog import' found in {rel_path}",
                    )

    def test_watchdog_runtime_zero_pypi_dependencies(self):
        """TC-PKG-14: railmon_watchdog.py imports exclusively from Python stdlib and internal packages."""
        watchdog_file = REPO_ROOT / "railmon_watchdog.py"
        allowed_stdlib = {
            "__future__",
            "dataclasses",
            "enum",
            "json",
            "logging",
            "math",
            "os",
            "re",
            "subprocess",
            "sys",
            "threading",
            "time",
            "typing",
            "urllib",
            "winsound",
        }
        allowed_internal = {"tui"}
        allowed = allowed_stdlib | allowed_internal

        tree = ast.parse(watchdog_file.read_text(encoding="utf-8"), filename=str(watchdog_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root_mod = alias.name.split(".")[0]
                    self.assertIn(
                        root_mod,
                        allowed,
                        f"Prohibited third-party PyPI import '{root_mod}' in railmon_watchdog.py",
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                root_mod = node.module.split(".")[0]
                self.assertIn(
                    root_mod,
                    allowed,
                    f"Prohibited third-party PyPI import '{root_mod}' in railmon_watchdog.py",
                )

    def test_pyproject_covers_all_production_modules(self):
        """TC-PKG-15a: Every production .py module is declared in pyproject.toml py-modules or packages."""
        parsed = _parse_pyproject()
        setuptools_cfg = parsed["tool"]["setuptools"]
        py_modules = set(setuptools_cfg["py-modules"])
        packages = set(setuptools_cfg["packages"])

        for rel_path in _discover_production_py_files():
            if len(rel_path.parts) == 1:
                self.assertIn(
                    rel_path.stem,
                    py_modules,
                    f"Top-level production module '{rel_path}' omitted from pyproject.toml py-modules",
                )
            else:
                pkg_name = ".".join(rel_path.parts[:-1])
                self.assertIn(
                    pkg_name,
                    packages,
                    f"Package '{pkg_name}' for '{rel_path}' omitted from pyproject.toml packages",
                )

    @unittest.skipUnless(_HAS_WHEEL_BUILD_TOOLS, "setuptools and wheel required for wheel build test")
    def test_built_wheel_contains_all_modules_and_assets(self):
        """TC-PKG-15b: Built wheel includes all production modules and GIS JSON assets, without watchdog.py."""
        prod_files = _discover_production_py_files()
        expected_wheel_entries = {rel.as_posix() for rel in prod_files} | {
            "tui/data/stations.json",
            "tui/data/tracks.json",
            "tui/data/infrastructure.json",
            "tui/data/speed_limits.json",
        }

        with tempfile.TemporaryDirectory() as tmp_root:
            tmp_path = Path(tmp_root)
            stage_dir = tmp_path / "src"
            wheel_dir = tmp_path / "dist"
            stage_dir.mkdir()
            wheel_dir.mkdir()

            for root_file in ("pyproject.toml", "README.md", "train_tracker.py", "railmon_watchdog.py"):
                shutil.copy2(REPO_ROOT / root_file, stage_dir / root_file)
            for pkg_dir in ("storage", "tui", "server", "exporter", "gtfs_rt"):
                shutil.copytree(
                    REPO_ROOT / pkg_dir,
                    stage_dir / pkg_dir,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
                )

            build_script = (
                "import sys; from setuptools import build_meta; "
                "print(build_meta.build_wheel(sys.argv[1]))"
            )
            proc = subprocess.run(
                [sys.executable, "-c", build_script, str(wheel_dir)],
                cwd=str(stage_dir),
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Wheel build failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
            )

            wheels = list(wheel_dir.glob("railmon-*.whl"))
            self.assertEqual(len(wheels), 1, f"Expected 1 built wheel, found {wheels}")

            with zipfile.ZipFile(wheels[0], "r") as zf:
                archive_names = set(zf.namelist())

            missing = sorted(expected_wheel_entries - archive_names)
            self.assertEqual(missing, [], f"Missing files in built wheel: {missing}")
            self.assertNotIn("watchdog.py", archive_names)

    def test_headless_imports_succeed_without_curses(self):
        """TC-PKG-16: All headless modules and lazy tui headless exports import cleanly without curses."""
        headless_targets = (
            "train_tracker",
            "railmon_watchdog",
            "storage",
            "storage.cli_formatters",
            "server",
            "exporter",
            "gtfs_rt",
            "tui",
            "tui.geo",
            "tui.mock_engine",
            "tui.state",
            "tui.stream_worker",
            "tui.theme",
        )
        for mod_name in headless_targets:
            snippet = (
                "import sys; "
                "sys.modules['curses'] = None; "
                "sys.modules['_curses'] = None; "
                f"__import__({mod_name!r})"
            )
            proc = subprocess.run(
                [sys.executable, "-c", snippet],
                cwd=str(REPO_ROOT),
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(
                proc.returncode,
                0,
                f"Headless import of '{mod_name}' failed without curses:\n{proc.stderr}",
            )

        lazy_headless_snippet = "\n".join(
            [
                "import sys",
                "sys.modules['curses'] = None",
                "sys.modules['_curses'] = None",
                "from tui import TrainStateStore, GeoPoint, MockTelemetryEngine, StreamWorker, ThemeProfile",
                "store = TrainStateStore()",
                "assert store.get_snapshot().theme_profile == ThemeProfile.AZURE",
                "assert store.cycle_theme_profile() == ThemeProfile.AMBER",
                "import tui",
                "try:",
                "    _ = tui.TUIApp",
                "    raise AssertionError('Expected ModuleNotFoundError for TUIApp without curses')",
                "except ModuleNotFoundError:",
                "    pass",
                "try:",
                "    _ = tui.TUIRenderer",
                "    raise AssertionError('Expected ModuleNotFoundError for TUIRenderer without curses')",
                "except ModuleNotFoundError:",
                "    pass",
            ]
        )
        proc = subprocess.run(
            [sys.executable, "-c", lazy_headless_snippet],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"Lazy tui headless verification failed:\n{proc.stderr}",
        )

        import tui

        self.assertEqual(len(tui.__all__), 80)
        self.assertIn("compute_tunnel_grace_sec", tui.__all__)
        dir_names = set(dir(tui))
        for symbol in tui.__all__:
            self.assertIn(symbol, dir_names)
            self.assertIsNotNone(getattr(tui, symbol))
        with self.assertRaises(AttributeError):
            _ = getattr(tui, "_nonexistent_tui_symbol_")

    def test_coexistence_with_third_party_pypi_watchdog(self):
        """TC-PKG-17: railmon_watchdog and train_tracker coexist cleanly with PyPI's watchdog package."""
        snippet = (
            "import sys, types; "
            "fake_pkg = types.ModuleType('watchdog'); "
            "fake_pkg.__path__ = []; "
            "fake_obs = types.ModuleType('watchdog.observers'); "
            "fake_obs.Observer = object; "
            "fake_pkg.observers = fake_obs; "
            "sys.modules['watchdog'] = fake_pkg; "
            "sys.modules['watchdog.observers'] = fake_obs; "
            "import railmon_watchdog, train_tracker; "
            "cfg = railmon_watchdog.WatchConfig(train_number='IC 52'); "
            "ev = railmon_watchdog.WatchdogEvaluator(cfg); "
            "assert ev.config.train_number == 'IC 52'; "
            "assert sys.modules['watchdog'] is fake_pkg; "
            "from watchdog.observers import Observer; "
            "assert Observer is object"
        )
        proc = subprocess.run(
            [sys.executable, "-c", snippet],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"PyPI watchdog coexistence check failed:\n{proc.stderr}",
        )

    def test_gitattributes_and_launcher_line_endings(self):
        """TC-PKG-18: .gitattributes enforces LF on POSIX launcher railmon and CRLF on .bat scripts."""
        gitattributes_path = REPO_ROOT / ".gitattributes"
        self.assertTrue(gitattributes_path.exists(), ".gitattributes missing at repo root")
        content = gitattributes_path.read_text(encoding="utf-8")
        self.assertIn("railmon text eol=lf", content)
        self.assertIn("*.bat text eol=crlf", content)

        posix_launcher = REPO_ROOT / "railmon"
        self.assertTrue(posix_launcher.exists(), "POSIX launcher './railmon' missing")
        raw_bytes = posix_launcher.read_bytes()
        self.assertNotIn(
            b"\r\n",
            raw_bytes,
            "POSIX launcher './railmon' contains CRLF line endings (breaks Linux/WSL shebang)",
        )


if __name__ == "__main__":
    unittest.main()
