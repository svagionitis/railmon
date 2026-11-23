"""Unit tests for Prometheus 0.0.4 and OpenMetrics 1.0.0 serializers and formatters."""

import math
import unittest
from exporter.formatter import (
    MetricsFormatter,
    escape_help_string,
    escape_label_value,
    format_float,
    negotiate_metrics_format,
)
from exporter.models import MetricFamily, MetricType


class TestExporterFormatter(unittest.TestCase):
    """Test suite for metric string escaping, floating-point formatting, and serialization."""

    def test_tc_fmt_01_escape_help_string(self) -> None:
        """TC-FMT-01: escape_help_string escapes backslashes and newlines."""
        self.assertEqual(escape_help_string("Simple docstring"), "Simple docstring")
        self.assertEqual(
            escape_help_string("First line\nSecond line with \\slash"),
            "First line\\nSecond line with \\\\slash",
        )

    def test_tc_fmt_02_escape_label_value(self) -> None:
        """TC-FMT-02: escape_label_value escapes backslashes, double quotes, and newlines."""
        self.assertEqual(escape_label_value("simple_value"), "simple_value")
        self.assertEqual(escape_label_value('IC "52"'), 'IC \\"52\\"')
        self.assertEqual(escape_label_value("line\\sub"), "line\\\\sub")
        self.assertEqual(escape_label_value("line1\nline2"), "line1\\nline2")
        self.assertEqual(escape_label_value('quote " and \\ and \n'), 'quote \\" and \\\\ and \\n')

    def test_tc_fmt_03_format_float_numbers(self) -> None:
        """TC-FMT-03: format_float formats integers cleanly and preserves decimals."""
        self.assertEqual(format_float(0.0), "0")
        self.assertEqual(format_float(142.0), "142")
        self.assertEqual(format_float(-10.0), "-10")
        self.assertEqual(format_float(8.5), "8.5")
        self.assertEqual(format_float(123.456), "123.456")

    def test_tc_fmt_04_format_float_ieee_specials(self) -> None:
        """TC-FMT-04: format_float emits canonical Prometheus tokens for IEEE-754 specials."""
        self.assertEqual(format_float(float("nan")), "NaN")
        self.assertEqual(format_float(math.nan), "NaN")
        self.assertEqual(format_float(float("inf")), "+Inf")
        self.assertEqual(format_float(float("-inf")), "-Inf")

    def test_tc_fmt_05_negotiate_metrics_format_openmetrics(self) -> None:
        """TC-FMT-05: negotiate_metrics_format selects OpenMetrics when requested."""
        content_type, mode = negotiate_metrics_format(
            "application/openmetrics-text; version=1.0.0"
        )
        self.assertEqual(mode, "openmetrics")
        self.assertIn("application/openmetrics-text", content_type)

        content_type, mode = negotiate_metrics_format(
            "application/openmetrics-text; version=1.0.0, text/plain; q=0.5"
        )
        self.assertEqual(mode, "openmetrics")

    def test_tc_fmt_06_negotiate_metrics_format_prometheus_default(self) -> None:
        """TC-FMT-06: negotiate_metrics_format defaults to Prometheus 0.0.4 for text/plain or wildcards."""
        headers = [None, "", "*/*", "text/plain", "text/html, */*"]
        for hdr in headers:
            with self.subTest(header=hdr):
                content_type, mode = negotiate_metrics_format(hdr)
                self.assertEqual(mode, "prometheus")
                self.assertIn("text/plain", content_type)

    def test_tc_fmt_07_format_prometheus_004_payload(self) -> None:
        """TC-FMT-07: format_prometheus_004 outputs valid Prometheus 0.0.4 payload."""
        fam = MetricFamily(
            name="railmon_train_speed_kmh",
            documentation="Instantaneous speed\nin km/h",
            metric_type=MetricType.GAUGE,
        )
        fam.add_sample(labels={"train_number": "IC52", "type": "IC"}, value=142.0)
        fam.add_sample(labels={"train_number": "1597", "type": "PR"}, value=88.5)

        payload_bytes = MetricsFormatter.format_prometheus_004([fam])
        text = payload_bytes.decode("utf-8")

        self.assertIn("# HELP railmon_train_speed_kmh Instantaneous speed\\nin km/h\n", text)
        self.assertIn("# TYPE railmon_train_speed_kmh gauge\n", text)
        self.assertIn('railmon_train_speed_kmh{train_number="1597",type="PR"} 88.5\n', text)
        self.assertIn('railmon_train_speed_kmh{train_number="IC52",type="IC"} 142\n', text)
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("# EOF", text)

    def test_tc_fmt_08_format_prometheus_004_maps_info_to_gauge(self) -> None:
        """TC-FMT-08: format_prometheus_004 maps MetricType.INFO to gauge."""
        fam = MetricFamily(
            name="railmon_build_info",
            documentation="Build information",
            metric_type=MetricType.INFO,
        )
        fam.add_sample(labels={"version": "1.0.0"}, value=1.0)

        payload = MetricsFormatter.format_prometheus_004([fam]).decode("utf-8")
        self.assertIn("# TYPE railmon_build_info gauge\n", payload)
        self.assertIn('railmon_build_info{version="1.0.0"} 1\n', payload)

    def test_tc_fmt_09_format_openmetrics_100_emits_unit(self) -> None:
        """TC-FMT-09: format_openmetrics_100 emits # UNIT when unit is present."""
        fam = MetricFamily(
            name="railmon_train_delay_minutes",
            documentation="Delay minutes",
            metric_type=MetricType.GAUGE,
            unit="minutes",
        )
        fam.add_sample(labels={"train_number": "IC52"}, value=5.0)

        payload = MetricsFormatter.format_openmetrics_100([fam]).decode("utf-8")
        self.assertIn("# UNIT railmon_train_delay_minutes minutes\n", payload)

    def test_tc_fmt_10_format_openmetrics_100_eof_trailer(self) -> None:
        """TC-FMT-10: format_openmetrics_100 ends with mandatory # EOF trailer."""
        fam = MetricFamily(
            name="railmon_active_trains",
            documentation="Active trains count",
            metric_type=MetricType.GAUGE,
        )
        fam.add_sample(value=10.0)

        payload = MetricsFormatter.format_openmetrics_100([fam]).decode("utf-8")
        self.assertTrue(payload.endswith("# EOF\n"))

    def test_tc_fmt_11_format_openmetrics_100_counter_total_suffix(self) -> None:
        """TC-FMT-11: format_openmetrics_100 enforces _total suffix on counter sample names."""
        fam = MetricFamily(
            name="railmon_messages",
            documentation="Stream messages counter",
            metric_type=MetricType.COUNTER,
        )
        # Sample name does not end with _total
        fam.add_sample(value=42.0)

        payload = MetricsFormatter.format_openmetrics_100([fam]).decode("utf-8")
        self.assertIn("# TYPE railmon_messages counter\n", payload)
        self.assertIn("railmon_messages_total 42\n", payload)

        # If sample already ends with _total, do not duplicate
        fam2 = MetricFamily(
            name="railmon_reconnects_total",
            documentation="Stream reconnects",
            metric_type=MetricType.COUNTER,
        )
        fam2.add_sample(value=3.0)
        payload2 = MetricsFormatter.format_openmetrics_100([fam2]).decode("utf-8")
        self.assertIn("railmon_reconnects_total 3\n", payload2)
        self.assertNotIn("railmon_reconnects_total_total", payload2)

    def test_tc_fmt_12_formatters_skip_empty_families(self) -> None:
        """TC-FMT-12: Both formatters cleanly skip families with zero samples."""
        fam_empty = MetricFamily(
            name="railmon_empty_metric",
            documentation="No samples",
            metric_type=MetricType.GAUGE,
        )
        fam_active = MetricFamily(
            name="railmon_active_trains",
            documentation="Active trains",
            metric_type=MetricType.GAUGE,
        )
        fam_active.add_sample(value=5.0)

        prom = MetricsFormatter.format_prometheus_004([fam_empty, fam_active]).decode("utf-8")
        self.assertNotIn("railmon_empty_metric", prom)
        self.assertIn("railmon_active_trains", prom)

        openm = MetricsFormatter.format_openmetrics_100([fam_empty, fam_active]).decode("utf-8")
        self.assertNotIn("railmon_empty_metric", openm)
        self.assertIn("railmon_active_trains", openm)

    def test_tc_edge_01_malformed_accept_headers_fallback(self) -> None:
        """TC-EXP-EDGE-01: Malformed and unusual Accept headers safely fallback to Prometheus 0.0.4."""
        malformed_headers = [
            "invalid/mime; q=bad, text/*",
            ";;;",
            ",,,,",
            "   ",
            "application/json",
            "text/html; q=0.9, */*; q=0.1",
        ]
        for hdr in malformed_headers:
            with self.subTest(header=hdr):
                content_type, mode = negotiate_metrics_format(hdr)
                self.assertEqual(mode, "prometheus")
                self.assertIn("text/plain", content_type)

        # Quality weights: when openmetrics is preferred over text/plain
        content_type, mode = negotiate_metrics_format(
            "text/plain; q=0.5, application/openmetrics-text; version=1.0.0; q=0.9"
        )
        self.assertEqual(mode, "openmetrics")

    def test_tc_edge_02_greek_unicode_polytonic_labels(self) -> None:
        """TC-EXP-EDGE-02: Greek Unicode polytonic text in labels and help strings format cleanly in UTF-8."""
        fam = MetricFamily(
            name="railmon_station_dwell_minutes",
            documentation="Χρόνος αναμονής σε σταθμούς (Λάρισα, Θεσσαλονίκη)",
            metric_type=MetricType.GAUGE,
            unit="minutes",
        )
        fam.add_sample(
            labels={
                "origin": "Αθήνα",
                "destination": "Θεσσαλονίκη",
                "station": "Παλαιοφάρσαλος",
                "corridor": "ΠΑΘΕ",
            },
            value=4.5,
        )

        prom_bytes = MetricsFormatter.format_prometheus_004([fam])
        prom_text = prom_bytes.decode("utf-8")
        self.assertIn("Χρόνος αναμονής σε σταθμούς (Λάρισα, Θεσσαλονίκη)", prom_text)
        self.assertIn('station="Παλαιοφάρσαλος"', prom_text)
        self.assertIn('destination="Θεσσαλονίκη"', prom_text)

        open_bytes = MetricsFormatter.format_openmetrics_100([fam])
        open_text = open_bytes.decode("utf-8")
        self.assertIn('origin="Αθήνα"', open_text)
        self.assertTrue(open_text.endswith("# EOF\n"))

    def test_tc_edge_03_injection_attack_sanitization_in_samples(self) -> None:
        """TC-EXP-EDGE-03: Label injection attack characters are escaped and cannot inject metric lines."""
        fam = MetricFamily(
            name="railmon_train_status",
            documentation="Status of trains",
            metric_type=MetricType.GAUGE,
        )
        # Attempt to inject unauthorized metric via label value
        malicious_label = 'IC52"}\nrailmon_injected_metric{hacked="true"} 999\n#'
        fam.add_sample(labels={"train_number": malicious_label}, value=1.0)

        prom_bytes = MetricsFormatter.format_prometheus_004([fam])
        prom_text = prom_bytes.decode("utf-8")

        # The raw unescaped injection MUST NOT appear as a new line
        self.assertNotIn('\nrailmon_injected_metric{hacked="true"} 999\n', prom_text)
        # Quotes and newlines must be safely escaped
        self.assertIn('train_number="IC52\\"}\\nrailmon_injected_metric{hacked=\\"true\\"} 999\\n#"', prom_text)

    def test_tc_edge_04_floating_point_extremes_in_metrics(self) -> None:
        """TC-EXP-EDGE-04: Metric families containing NaN, +Inf, -Inf, and negative floats format safely."""
        fam = MetricFamily(
            name="railmon_kinematic_extremes",
            documentation="Extreme kinematic telemetry values",
            metric_type=MetricType.GAUGE,
        )
        fam.add_sample(labels={"kind": "nan"}, value=float("nan"))
        fam.add_sample(labels={"kind": "pos_inf"}, value=float("inf"))
        fam.add_sample(labels={"kind": "neg_inf"}, value=float("-inf"))
        fam.add_sample(labels={"kind": "neg_delay"}, value=-12.5)

        prom_text = MetricsFormatter.format_prometheus_004([fam]).decode("utf-8")
        self.assertIn('railmon_kinematic_extremes{kind="nan"} NaN\n', prom_text)
        self.assertIn('railmon_kinematic_extremes{kind="pos_inf"} +Inf\n', prom_text)
        self.assertIn('railmon_kinematic_extremes{kind="neg_inf"} -Inf\n', prom_text)
        self.assertIn('railmon_kinematic_extremes{kind="neg_delay"} -12.5\n', prom_text)


if __name__ == "__main__":
    unittest.main()
