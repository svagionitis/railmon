"""Tests for package metadata, PEP 621 pyproject.toml, and single-source versioning."""

import re
import unittest
from pathlib import Path

import train_tracker
from storage.recorder import RecorderConfig

REPO_ROOT = Path(__file__).resolve().parent.parent


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
        """TC-PKG-06: Package data files referenced in packaging exist on filesystem."""
        stations_file = REPO_ROOT / "tui" / "data" / "stations.json"
        tracks_file = REPO_ROOT / "tui" / "data" / "tracks.json"
        self.assertTrue(stations_file.exists(), "tui/data/stations.json missing")
        self.assertTrue(tracks_file.exists(), "tui/data/tracks.json missing")

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


if __name__ == "__main__":
    unittest.main()
