"""Unit tests for exporter domain models, metric primitives, and validation."""

import unittest
from exporter.models import ExporterConfig, MetricFamily, MetricType, Sample


class TestExporterModels(unittest.TestCase):
    """Test suite for exporter metric primitives and configuration dataclasses."""

    def test_tc_mod_01_metric_type_enum(self) -> None:
        """TC-MOD-01: MetricType enum values match Prometheus & OpenMetrics string specifications."""
        self.assertEqual(MetricType.GAUGE.value, "gauge")
        self.assertEqual(MetricType.COUNTER.value, "counter")
        self.assertEqual(MetricType.INFO.value, "info")
        self.assertEqual(MetricType.SUMMARY.value, "summary")
        self.assertEqual(MetricType.UNTYPED.value, "untyped")

    def test_tc_mod_02_sample_immutability(self) -> None:
        """TC-MOD-02: Sample is a frozen dataclass with expected defaults."""
        sample = Sample(name="railmon_train_count", value=12.0)
        self.assertEqual(sample.name, "railmon_train_count")
        self.assertEqual(sample.value, 12.0)
        self.assertEqual(sample.labels, ())
        self.assertIsNone(sample.timestamp)

        # Frozen immutability check
        with self.assertRaises(AttributeError):
            sample.value = 15.0  # type: ignore[misc]

    def test_tc_mod_03_sample_valid_naming(self) -> None:
        """TC-MOD-03: Sample accepts valid metric names and label identifiers."""
        sample = Sample(
            name="railmon_train_speed_kmh:total",
            labels=(("corridor", "PATHE"), ("train_number", "IC52")),
            value=142.5,
        )
        self.assertEqual(sample.name, "railmon_train_speed_kmh:total")
        self.assertEqual(len(sample.labels), 2)

    def test_tc_mod_04_sample_rejects_invalid_names(self) -> None:
        """TC-MOD-04: Sample rejects invalid metric names violating syntax regex."""
        invalid_names = [
            "123_invalid_start_with_digit",
            "invalid-hyphen-name",
            "invalid name with spaces",
            "invalid$character",
            "",
        ]
        for name in invalid_names:
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    Sample(name=name, value=1.0)

    def test_tc_mod_05_sample_rejects_invalid_labels(self) -> None:
        """TC-MOD-05: Sample rejects invalid label names violating syntax regex."""
        invalid_labels = [
            "123_start_with_digit",
            "has-hyphens",
            "has spaces",
            "has:colons",  # Colons permitted in metric names, but NOT in label names
            "foo$bar",
            "",
        ]
        for lbl in invalid_labels:
            with self.subTest(lbl=lbl):
                with self.assertRaises(ValueError):
                    Sample(name="valid_metric", labels=((lbl, "val"),), value=1.0)

    def test_tc_mod_06_sample_rejects_reserved_labels(self) -> None:
        """TC-MOD-06: Sample rejects reserved internal labels starting with '__'."""
        reserved_labels = ["__name__", "__scheme__", "__address__", "__"]
        for lbl in reserved_labels:
            with self.subTest(lbl=lbl):
                with self.assertRaises(ValueError) as ctx:
                    Sample(name="valid_metric", labels=((lbl, "val"),), value=1.0)
                self.assertIn("Reserved label name", str(ctx.exception))

    def test_tc_mod_07_metric_family_validation(self) -> None:
        """TC-MOD-07: MetricFamily validates name syntax and preserves metadata."""
        fam = MetricFamily(
            name="railmon_active_trains",
            documentation="Total active trains on network",
            metric_type=MetricType.GAUGE,
            unit="trains",
        )
        self.assertEqual(fam.name, "railmon_active_trains")
        self.assertEqual(fam.metric_type, MetricType.GAUGE)
        self.assertEqual(fam.unit, "trains")
        self.assertEqual(fam.samples, [])

        with self.assertRaises(ValueError):
            MetricFamily(
                name="invalid-family-name",
                documentation="Bad name",
                metric_type=MetricType.GAUGE,
            )

    def test_tc_mod_08_metric_family_add_sample_sorting(self) -> None:
        """TC-MOD-08: MetricFamily.add_sample sorts labels alphabetically for determinism."""
        fam = MetricFamily(
            name="railmon_delay_minutes",
            documentation="Delay minutes per train",
            metric_type=MetricType.GAUGE,
        )
        # Pass labels in arbitrary unsorted order
        fam.add_sample(
            labels={"z_label": "3", "a_label": "1", "m_label": "2"},
            value=8.5,
        )
        self.assertEqual(len(fam.samples), 1)
        sample = fam.samples[0]
        self.assertEqual(sample.name, "railmon_delay_minutes")
        self.assertEqual(sample.value, 8.5)
        # Assert alphabetical label sorting
        self.assertEqual(
            sample.labels,
            (("a_label", "1"), ("m_label", "2"), ("z_label", "3")),
        )

    def test_tc_mod_09_exporter_config_defaults(self) -> None:
        """TC-MOD-09: ExporterConfig provides sensible defaults and converts cleanly."""
        cfg = ExporterConfig()
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 9100)
        self.assertIsNone(cfg.db_path)
        self.assertFalse(cfg.demo_mode)
        self.assertTrue(cfg.collect_db_stats)
        self.assertEqual(cfg.scrape_timeout_seconds, 10.0)
        self.assertTrue(cfg.gzip_compression)

    def test_tc_mod_10_exporter_config_validation(self) -> None:
        """TC-MOD-10: ExporterConfig rejects invalid ports and non-positive timeouts."""
        invalid_ports = [0, -1, 65536, 100000]
        for p in invalid_ports:
            with self.subTest(port=p):
                with self.assertRaises(ValueError):
                    ExporterConfig(port=p)

        invalid_timeouts = [0.0, -0.5, -10.0]
        for t in invalid_timeouts:
            with self.subTest(timeout=t):
                with self.assertRaises(ValueError):
                    ExporterConfig(scrape_timeout_seconds=t)


if __name__ == "__main__":
    unittest.main()
