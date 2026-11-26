"""Unit tests for Prometheus 0.0.4 and OpenMetrics 1.0.0 serializers and formatters."""

import math
import re
from typing import Dict, List, Optional, Set, Tuple
import unittest
from exporter.formatter import (
    MetricsFormatter,
    escape_help_string,
    escape_label_value,
    format_float,
    negotiate_metrics_format,
)
from exporter.models import (
    RE_LABEL_NAME,
    RE_METRIC_NAME,
    RE_UNIT,
    RESERVED_GAUGE_SUFFIXES,
    MetricFamily,
    MetricType,
)

_VALID_OPENMETRICS_TYPES = frozenset(
    {"counter", "gauge", "histogram", "gaugehistogram", "stateset", "info", "summary", "unknown"}
)


def _validate_escaped_string(text: str) -> Optional[str]:
    """Validate OpenMetrics 1.0.0 ESCAPED_STRING syntax (only \\\\, \\n, \\\" escapes)."""
    idx = 0
    length = len(text)
    while idx < length:
        ch = text[idx]
        if ch == '"':
            return "unescaped double quote"
        if ch == "\\":
            if idx + 1 >= length:
                return "trailing backslash"
            nxt = text[idx + 1]
            if nxt not in ("\\", "n", '"'):
                return f"invalid escape sequence '\\{nxt}'"
            idx += 2
            continue
        idx += 1
    return None


def _parse_openmetrics_labels(raw_labels: str) -> Tuple[Optional[Tuple[Tuple[str, str], ...]], Optional[str]]:
    """Parse OpenMetrics 1.0.0 label set inside braces {k=\"v\",...}."""
    if not raw_labels:
        return (), None
    labels: List[Tuple[str, str]] = []
    seen_keys: Set[str] = set()
    idx = 0
    length = len(raw_labels)
    while idx < length:
        eq_idx = raw_labels.find("=", idx)
        if eq_idx < 0:
            return None, f"malformed label pair near '{raw_labels[idx:]}'"
        key = raw_labels[idx:eq_idx]
        if not RE_LABEL_NAME.match(key) or key.startswith("__"):
            return None, f"invalid or reserved label name '{key}'"
        if key in seen_keys:
            return None, f"duplicate label key '{key}' within sample"
        seen_keys.add(key)

        val_start = eq_idx + 1
        if val_start >= length or raw_labels[val_start] != '"':
            return None, f"label '{key}' value must start with double quote"
        pos = val_start + 1
        val_chars: List[str] = []
        closed = False
        while pos < length:
            ch = raw_labels[pos]
            if ch == "\\":
                if pos + 1 >= length or raw_labels[pos + 1] not in ("\\", "n", '"'):
                    return None, f"invalid escape in label '{key}'"
                val_chars.append(raw_labels[pos : pos + 2])
                pos += 2
                continue
            if ch == '"':
                closed = True
                pos += 1
                break
            val_chars.append(ch)
            pos += 1
        if not closed:
            return None, f"unterminated label value for '{key}'"
        labels.append((key, "".join(val_chars)))
        if pos < length:
            if raw_labels[pos] != ",":
                return None, f"expected ',' after label '{key}', got '{raw_labels[pos]}'"
            idx = pos + 1
            if idx == length:
                return None, "trailing comma in label set"
        else:
            idx = pos
    return tuple(sorted(labels)), None


def _parse_openmetrics_sample(
    line: str,
) -> Tuple[Optional[Tuple[str, Tuple[Tuple[str, str], ...], float]], Optional[str]]:
    """Parse a single OpenMetrics 1.0.0 sample line into (name, sorted_labels, float_value)."""
    brace_idx = line.find("{")
    space_idx = line.find(" ")
    if space_idx < 0:
        return None, "missing space separating metric and value"

    if 0 <= brace_idx < space_idx:
        sample_name = line[:brace_idx]
        # Locate closing brace outside quotes
        pos = brace_idx + 1
        in_quote = False
        close_brace = -1
        while pos < len(line):
            ch = line[pos]
            if in_quote:
                if ch == "\\":
                    pos += 2
                    continue
                if ch == '"':
                    in_quote = False
            else:
                if ch == '"':
                    in_quote = True
                elif ch == "}":
                    close_brace = pos
                    break
            pos += 1
        if close_brace < 0:
            return None, "unterminated '{' label block"
        raw_labels = line[brace_idx + 1 : close_brace]
        labels, lbl_err = _parse_openmetrics_labels(raw_labels)
        if lbl_err is not None or labels is None:
            return None, lbl_err or "invalid labels"
        rest = line[close_brace + 1 :]
        if not rest.startswith(" "):
            return None, "missing space after '}'"
        val_tokens = rest[1:].split(" ")
    else:
        sample_name = line[:space_idx]
        labels = ()
        val_tokens = line[space_idx + 1 :].split(" ")

    if not RE_METRIC_NAME.match(sample_name):
        return None, f"invalid sample metric name '{sample_name}'"
    if not val_tokens or len(val_tokens) > 2:
        return None, f"invalid value/timestamp token count in '{line}'"
    raw_val = val_tokens[0]
    if raw_val == "NaN":
        fval = float("nan")
    elif raw_val in ("+Inf", "Inf"):
        fval = float("inf")
    elif raw_val == "-Inf":
        fval = float("-inf")
    else:
        try:
            fval = float(raw_val)
        except ValueError:
            return None, f"invalid numeric value '{raw_val}'"
    return (sample_name, labels, fval), None


def validate_openmetrics_100(payload: bytes) -> List[str]:
    """Independent zero-dependency OpenMetrics 1.0.0 ABNF & semantic validator for tests.

    Returns a list of violation descriptions; an empty list indicates 100% conformance.
    """
    errors: List[str] = []
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        return [f"invalid UTF-8 payload: {exc}"]

    if "\r" in text:
        errors.append("CR ('\\r') character is forbidden in OpenMetrics 1.0.0")
    if not text.endswith("# EOF\n"):
        errors.append("OpenMetrics 1.0.0 payload must terminate with '# EOF\\n'")
        return errors

    lines = text.split("\n")[:-1]  # strip final empty element after trailing LF
    if not lines or lines[-1] != "# EOF":
        errors.append("final line must be exact '# EOF'")
        return errors

    closed_families: Set[str] = set()
    current_family: Optional[str] = None
    current_type: Optional[str] = None
    current_unit: Optional[str] = None
    seen_meta_for_current: Set[str] = set()
    seen_sample_in_current = False
    seen_series: Set[Tuple[str, Tuple[Tuple[str, str], ...]]] = set()

    for lineno, line in enumerate(lines[:-1], start=1):
        if not line:
            errors.append(f"line {lineno}: blank line forbidden in OpenMetrics 1.0.0")
            continue

        if line.startswith("# "):
            parts = line.split(" ", 3)
            if len(parts) < 3 or parts[1] not in ("TYPE", "HELP", "UNIT"):
                errors.append(f"line {lineno}: invalid metadata/comment line '{line}'")
                continue
            kind, fam_name = parts[1], parts[2]
            if not RE_METRIC_NAME.match(fam_name):
                errors.append(f"line {lineno}: invalid family name '{fam_name}'")
                continue

            if fam_name != current_family:
                if current_family is not None:
                    closed_families.add(current_family)
                if fam_name in closed_families:
                    errors.append(
                        f"line {lineno}: duplicate or interleaved MetricFamily '{fam_name}'"
                    )
                current_family = fam_name
                current_type = None
                current_unit = None
                seen_meta_for_current = set()
                seen_sample_in_current = False
            elif seen_sample_in_current:
                errors.append(
                    f"line {lineno}: metadata '# {kind}' appeared after samples for '{fam_name}'"
                )

            if kind in seen_meta_for_current:
                errors.append(f"line {lineno}: duplicate '# {kind}' for '{fam_name}'")
            seen_meta_for_current.add(kind)

            if kind == "TYPE":
                if len(parts) != 4 or parts[3] not in _VALID_OPENMETRICS_TYPES:
                    errors.append(f"line {lineno}: invalid OpenMetrics TYPE '{line}'")
                    continue
                current_type = parts[3]
                if current_type == "counter" and fam_name.endswith("_total"):
                    errors.append(
                        f"line {lineno}: counter family '{fam_name}' must not end with '_total'"
                    )
                elif current_type == "info" and fam_name.endswith("_info"):
                    errors.append(
                        f"line {lineno}: info family '{fam_name}' must not end with '_info'"
                    )
                elif current_type == "gauge":
                    for suf in RESERVED_GAUGE_SUFFIXES:
                        if fam_name.endswith(suf):
                            errors.append(
                                f"line {lineno}: gauge family '{fam_name}' must not end with '{suf}'"
                            )
                if current_unit is not None and current_type in ("info", "stateset", "unknown"):
                    errors.append(
                        f"line {lineno}: unit '{current_unit}' forbidden on type '{current_type}'"
                    )
            elif kind == "HELP":
                help_doc = parts[3] if len(parts) == 4 else ""
                esc_err = _validate_escaped_string(help_doc)
                if esc_err is not None:
                    errors.append(f"line {lineno}: invalid # HELP string ({esc_err})")
            elif kind == "UNIT":
                unit_val = parts[3] if len(parts) == 4 else ""
                if not unit_val or not RE_UNIT.match(unit_val):
                    errors.append(f"line {lineno}: invalid # UNIT token '{unit_val}'")
                    continue
                current_unit = unit_val
                if not fam_name.endswith(f"_{unit_val}"):
                    errors.append(
                        f"line {lineno}: unit '{unit_val}' is not a suffix of '{fam_name}'"
                    )
                if current_type in ("info", "stateset", "unknown"):
                    errors.append(
                        f"line {lineno}: unit '{unit_val}' forbidden on type '{current_type}'"
                    )
            continue

        if line.startswith("#"):
            errors.append(f"line {lineno}: invalid comment line '{line}'")
            continue

        parsed, sample_err = _parse_openmetrics_sample(line)
        if sample_err is not None or parsed is None:
            errors.append(f"line {lineno}: {sample_err}")
            continue
        sample_name, labels, fval = parsed
        seen_sample_in_current = True

        series_key = (sample_name, labels)
        if series_key in seen_series:
            errors.append(
                f"line {lineno}: duplicate sample series '{sample_name}' labels={labels}"
            )
        seen_series.add(series_key)

        if current_family is None:
            errors.append(f"line {lineno}: sample '{sample_name}' without MetricFamily metadata")
            continue

        effective_type = current_type or "unknown"
        if effective_type == "counter":
            if sample_name not in (f"{current_family}_total", f"{current_family}_created"):
                errors.append(
                    f"line {lineno}: counter sample '{sample_name}' must be '{current_family}_total'"
                )
            if math.isnan(fval) or fval < 0.0:
                errors.append(
                    f"line {lineno}: counter sample '{sample_name}' has invalid value {fval}"
                )
        elif effective_type == "info":
            if sample_name != f"{current_family}_info":
                errors.append(
                    f"line {lineno}: info sample '{sample_name}' must be '{current_family}_info'"
                )
            if not math.isclose(fval, 1.0, rel_tol=0.0, abs_tol=1e-9):
                errors.append(
                    f"line {lineno}: info sample '{sample_name}' value must be 1, got {fval}"
                )
        elif effective_type in ("gauge", "unknown"):
            if sample_name != current_family:
                errors.append(
                    f"line {lineno}: {effective_type} sample '{sample_name}' != family '{current_family}'"
                )

    return errors


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

        # If sample already ends with _total, strip from # TYPE/# HELP and do not duplicate on sample
        fam2 = MetricFamily(
            name="railmon_reconnects_total",
            documentation="Stream reconnects",
            metric_type=MetricType.COUNTER,
        )
        fam2.add_sample(value=3.0)
        payload2 = MetricsFormatter.format_openmetrics_100([fam2]).decode("utf-8")
        self.assertIn("# TYPE railmon_reconnects counter\n", payload2)
        self.assertIn("# HELP railmon_reconnects Stream reconnects\n", payload2)
        self.assertIn("railmon_reconnects_total 3\n", payload2)
        self.assertNotIn("railmon_reconnects_total_total", payload2)
        self.assertEqual(validate_openmetrics_100(payload2.encode("utf-8")), [])

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

    def test_tc_omx_03_counter_and_info_openmetrics_vs_prometheus_parity(self) -> None:
        """TC-OMX-03: Counter and Info emit canonical base metadata in OpenMetrics and matching suffixed metadata in Prometheus 0.0.4."""
        fam_counter = MetricFamily(
            name="railmon_stream_messages",
            documentation="Total telemetry frames received",
            metric_type=MetricType.COUNTER,
        )
        fam_counter.add_sample(labels={"event_type": "trainPositionsUx"}, value=42.0)

        fam_uptime = MetricFamily(
            name="railmon_exporter_uptime_seconds",
            documentation="Exporter uptime",
            metric_type=MetricType.COUNTER,
            unit="seconds",
        )
        fam_uptime.add_sample(value=12.5)

        fam_info = MetricFamily(
            name="railmon_exporter_build",
            documentation="Build metadata",
            metric_type=MetricType.INFO,
        )
        fam_info.add_sample(labels={"version": "1.0.0"}, value=1.0)

        om_bytes = MetricsFormatter.format_openmetrics_100([fam_counter, fam_uptime, fam_info])
        self.assertEqual(validate_openmetrics_100(om_bytes), [])
        om_text = om_bytes.decode("utf-8")
        self.assertIn("# TYPE railmon_stream_messages counter\n", om_text)
        self.assertIn('railmon_stream_messages_total{event_type="trainPositionsUx"} 42\n', om_text)
        self.assertIn("# TYPE railmon_exporter_uptime_seconds counter\n", om_text)
        self.assertIn("# UNIT railmon_exporter_uptime_seconds seconds\n", om_text)
        self.assertIn("railmon_exporter_uptime_seconds_total 12.5\n", om_text)
        self.assertIn("# TYPE railmon_exporter_build info\n", om_text)
        self.assertIn('railmon_exporter_build_info{version="1.0.0"} 1\n', om_text)

        prom_text = MetricsFormatter.format_prometheus_004([fam_counter, fam_uptime, fam_info]).decode("utf-8")
        self.assertIn("# TYPE railmon_stream_messages_total counter\n", prom_text)
        self.assertIn('railmon_stream_messages_total{event_type="trainPositionsUx"} 42\n', prom_text)
        self.assertIn("# TYPE railmon_exporter_uptime_seconds_total counter\n", prom_text)
        self.assertIn("railmon_exporter_uptime_seconds_total 12.5\n", prom_text)
        self.assertIn("# TYPE railmon_exporter_build_info gauge\n", prom_text)
        self.assertIn('railmon_exporter_build_info{version="1.0.0"} 1\n', prom_text)
        self.assertNotIn("# UNIT", prom_text)

    def test_tc_omx_04_untyped_to_unknown_help_quote_escaping_and_crlf(self) -> None:
        """TC-OMX-04: UNTYPED maps to unknown in OpenMetrics, # HELP escapes quotes, and CR is stripped."""
        fam = MetricFamily(
            name="railmon_legacy_signal",
            documentation='Raw "quoted" signal\r\nsecond line',
            metric_type=MetricType.UNTYPED,
        )
        fam.add_sample(value=7.0)

        om_bytes = MetricsFormatter.format_openmetrics_100([fam])
        self.assertEqual(validate_openmetrics_100(om_bytes), [])
        om_text = om_bytes.decode("utf-8")
        self.assertNotIn("\r", om_text)
        self.assertIn('# HELP railmon_legacy_signal Raw \\"quoted\\" signal\\nsecond line\n', om_text)
        self.assertIn("# TYPE railmon_legacy_signal unknown\n", om_text)
        self.assertIn("railmon_legacy_signal 7\n", om_text)

        prom_text = MetricsFormatter.format_prometheus_004([fam]).decode("utf-8")
        self.assertIn("# TYPE railmon_legacy_signal untyped\n", prom_text)

    def test_tc_omx_05_rfc9110_accept_header_q_value_negotiation(self) -> None:
        """TC-OMX-05: negotiate_metrics_format handles real Prometheus q-values, case, and q=0 rejection."""
        # Real Prometheus v2.25+ / v3.x scrape header
        prom_hdr = (
            "application/openmetrics-text;version=1.0.0;escaping=allow-utf-8;q=0.6,"
            "application/openmetrics-text;version=0.0.1;q=0.4,"
            "text/plain;version=0.0.4;escaping=allow-utf-8;q=0.3,"
            "*/*;q=0.1"
        )
        ct, mode = negotiate_metrics_format(prom_hdr)
        self.assertEqual(mode, "openmetrics")
        self.assertIn("application/openmetrics-text", ct)

        # Explicit q=0 rejects OpenMetrics
        ct0, mode0 = negotiate_metrics_format(
            "application/openmetrics-text; version=1.0.0; q=0, text/plain; q=1.0"
        )
        self.assertEqual(mode0, "prometheus")
        self.assertIn("text/plain", ct0)

        # Lower q than text/plain prefers Prometheus 0.0.4
        _, mode_low = negotiate_metrics_format(
            "text/plain; q=0.9, application/openmetrics-text; version=1.0.0; q=0.2"
        )
        self.assertEqual(mode_low, "prometheus")

        # Case-insensitive OpenMetrics header
        _, mode_upper = negotiate_metrics_format("APPLICATION/OPENMETRICS-TEXT; VERSION=1.0.0")
        self.assertEqual(mode_upper, "openmetrics")

    def test_tc_omx_06_scrape_wide_dedup_and_invalid_counter_info_guards(self) -> None:
        """TC-OMX-06: Formatter deduplicates families/series, skips negative/NaN counters, and normalizes INFO to 1."""
        fam1 = MetricFamily("railmon_speed_kmh", "Speed", MetricType.GAUGE, unit="kmh")
        fam1.add_sample(labels={"train_number": "IC52"}, value=120.0)
        fam1.add_sample(labels={"train_number": "IC52"}, value=125.0)  # duplicate series!

        fam1_dup = MetricFamily("railmon_speed_kmh", "Duplicate family", MetricType.GAUGE, unit="kmh")
        fam1_dup.add_sample(labels={"train_number": "IC54"}, value=110.0)

        fam_ctr = MetricFamily("railmon_events", "Events", MetricType.COUNTER)
        fam_ctr.add_sample(labels={"kind": "neg"}, value=-5.0)
        fam_ctr.add_sample(labels={"kind": "nan"}, value=float("nan"))
        fam_ctr.add_sample(labels={"kind": "ok"}, value=3.0)

        fam_info = MetricFamily("railmon_app", "App info", MetricType.INFO)
        fam_info.add_sample(labels={"version": "2.0"}, value=2.0)

        om_bytes = MetricsFormatter.format_openmetrics_100([fam1, fam1_dup, fam_ctr, fam_info])
        self.assertEqual(validate_openmetrics_100(om_bytes), [])
        om_text = om_bytes.decode("utf-8")
        self.assertEqual(om_text.count('railmon_speed_kmh{train_number="IC52"}'), 1)
        self.assertNotIn('kind="neg"', om_text)
        self.assertNotIn('kind="nan"', om_text)
        self.assertIn('railmon_events_total{kind="ok"} 3\n', om_text)
        self.assertIn('railmon_app_info{version="2.0"} 1\n', om_text)

    def test_tc_omx_07_validator_rejects_all_p06_violations(self) -> None:
        """TC-OMX-07: Strict OpenMetrics 1.0.0 validator explicitly rejects every historical P0-6 violation."""
        bad_payloads = [
            b"# HELP a doc\n# TYPE a gauge\n# UNIT a km/h\na 1\n# EOF\n",
            b"# HELP a_active doc\n# TYPE a_active gauge\n# UNIT a_active alerts\na_active 1\n# EOF\n",
            b"# HELP c_total doc\n# TYPE c_total counter\nc_total 1\n# EOF\n",
            b"# HELP b_info doc\n# TYPE b_info info\nb_info 1\n# EOF\n",
            b"# HELP g_total doc\n# TYPE g_total gauge\ng_total 1\n# EOF\n",
            b"# HELP u doc\n# TYPE u untyped\nu 1\n# EOF\n",
            b'# HELP q doc with "unescaped" quote\n# TYPE q gauge\nq 1\n# EOF\n',
            b'# HELP d doc\n# TYPE d gauge\nd{k="1"} 1\nd{k="1"} 2\n# EOF\n',
            b"# HELP m doc\n# TYPE m gauge\nm 1\n",
        ]
        for idx, payload in enumerate(bad_payloads):
            with self.subTest(case=idx):
                errs = validate_openmetrics_100(payload)
                self.assertGreater(len(errs), 0, f"Validator failed to reject case {idx}")


if __name__ == "__main__":
    unittest.main()
