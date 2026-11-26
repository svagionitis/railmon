"""High-performance zero-dependency Prometheus and OpenMetrics text serializers."""

import logging
import math
from typing import Iterable, List, Optional, Set, Tuple

from exporter.models import (
    RE_UNIT,
    MetricFamily,
    MetricType,
    canonical_family_base_name,
)

logger = logging.getLogger("railmon.exporter.formatter")

_PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
_OPENMETRICS_CONTENT_TYPE = "application/openmetrics-text; version=1.0.0; charset=utf-8"
_MAX_ACCEPT_RANGES = 64


def escape_help_string(doc: str, *, escape_quotes: bool = False) -> str:
    """Escape backslashes, newlines, and optionally double quotes in # HELP strings."""
    normalized = doc.replace("\r\n", "\n").replace("\r", "\n")
    escaped = normalized.replace("\\", "\\\\").replace("\n", "\\n")
    if escape_quotes:
        return escaped.replace('"', '\\"')
    return escaped


def escape_label_value(val: str) -> str:
    """Escape backslashes, double quotes, and newlines in label values."""
    normalized = val.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_float(val: float) -> str:
    """Format float values according to Prometheus/OpenMetrics lexical rules."""
    if math.isnan(val):
        return "NaN"
    if math.isinf(val):
        return "+Inf" if val > 0.0 else "-Inf"
    if val.is_integer():
        return f"{int(val)}"
    return f"{val:.6g}"


def _parse_media_q_value(params: List[str]) -> Optional[float]:
    """Extract and validate an RFC 9110 quality weight (q=) from media-range parameters."""
    for param in params:
        key, sep, raw_val = param.partition("=")
        if not sep:
            continue
        if key.strip().lower() != "q":
            continue
        try:
            q_val = float(raw_val.strip())
        except ValueError:
            return None
        if not math.isfinite(q_val) or q_val < 0.0 or q_val > 1.0:
            return None
        return q_val
    return 1.0


def negotiate_metrics_format(accept_header: Optional[str]) -> Tuple[str, str]:
    """Negotiate Content-Type and format mode from client Accept header using RFC 9110 q-weights.

    Args:
        accept_header: Value of incoming HTTP 'Accept' header.

    Returns:
        Tuple of (content_type_header_value, format_mode) where format_mode
        is either 'openmetrics' or 'prometheus'.
    """
    if not accept_header or not accept_header.strip():
        return (_PROMETHEUS_CONTENT_TYPE, "prometheus")

    best_openmetrics = -1.0
    best_prometheus = -1.0

    for raw_range in accept_header.split(",")[:_MAX_ACCEPT_RANGES]:
        parts = raw_range.split(";")
        media_type = parts[0].strip().lower()
        if not media_type:
            continue
        q_val = _parse_media_q_value(parts[1:])
        if q_val is None:
            continue
        if media_type == "application/openmetrics-text":
            if q_val > best_openmetrics:
                best_openmetrics = q_val
        elif media_type == "text/plain":
            if q_val > best_prometheus:
                best_prometheus = q_val

    if best_openmetrics > 0.0 and best_openmetrics >= best_prometheus:
        return (_OPENMETRICS_CONTENT_TYPE, "openmetrics")

    return (_PROMETHEUS_CONTENT_TYPE, "prometheus")


def _resolve_openmetrics_type(metric_type: MetricType) -> str:
    """Map internal MetricType to strict OpenMetrics 1.0.0 # TYPE token."""
    if metric_type in (MetricType.UNTYPED, MetricType.UNKNOWN):
        return "unknown"
    return metric_type.value


def _resolve_prometheus_type(metric_type: MetricType) -> str:
    """Map internal MetricType to strict Prometheus 0.0.4 # TYPE token."""
    if metric_type == MetricType.INFO:
        return "gauge"
    if metric_type in (MetricType.UNTYPED, MetricType.UNKNOWN):
        return "untyped"
    return metric_type.value


def _resolve_prometheus_family_name(base_name: str, metric_type: MetricType) -> str:
    """Resolve # HELP/# TYPE identifier for Prometheus 0.0.4 so metadata matches sample names."""
    if metric_type == MetricType.COUNTER:
        return f"{base_name}_total"
    if metric_type == MetricType.INFO:
        return f"{base_name}_info"
    return base_name


def _resolve_sample_name(
    sample_name: str,
    fam_name: str,
    base_name: str,
    metric_type: MetricType,
) -> str:
    """Resolve canonical wire sample name consistently across OpenMetrics and Prometheus 0.0.4."""
    if metric_type == MetricType.COUNTER:
        if sample_name.endswith("_created"):
            return f"{base_name}_created"
        if sample_name == fam_name or sample_name == base_name:
            return f"{base_name}_total"
        if sample_name.endswith("_total"):
            return sample_name
        return f"{sample_name}_total"
    if metric_type == MetricType.INFO:
        return f"{base_name}_info"
    return sample_name


def _is_valid_openmetrics_unit(
    base_name: str,
    unit: Optional[str],
    metric_type: MetricType,
) -> bool:
    """Defensive OpenMetrics 1.0.0 guard confirming # UNIT syntax, type, and suffix alignment."""
    if not unit:
        return False
    if metric_type in (MetricType.INFO, MetricType.UNTYPED, MetricType.UNKNOWN):
        return False
    if not RE_UNIT.match(unit):
        return False
    return base_name.endswith(f"_{unit}")


def _sanitize_sample_value(
    value: float,
    metric_type: MetricType,
    sample_name: str,
) -> Optional[float]:
    """Validate and normalize sample values according to OpenMetrics 1.0.0 type invariants."""
    if metric_type == MetricType.COUNTER:
        if math.isnan(value) or value < 0.0:
            logger.warning(
                "Skipping invalid counter sample '%s' with non-monotonic/NaN value: %r",
                sample_name,
                value,
            )
            return None
    elif metric_type == MetricType.INFO:
        if not math.isclose(value, 1.0, rel_tol=0.0, abs_tol=1e-9):
            logger.warning(
                "Normalizing INFO sample '%s' value %r to canonical 1.0",
                sample_name,
                value,
            )
            return 1.0
    return value


def _format_sample_line(
    sample_name: str,
    labels: Tuple[Tuple[str, str], ...],
    value: float,
) -> str:
    """Format a single Prometheus/OpenMetrics sample line."""
    if labels:
        lbls = ",".join(f'{k}="{escape_label_value(v)}"' for k, v in labels)
        return f"{sample_name}{{{lbls}}} {format_float(value)}"
    return f"{sample_name} {format_float(value)}"


def _collect_family_sample_lines(
    fam: MetricFamily,
    base_name: str,
    seen_series: Set[Tuple[str, Tuple[Tuple[str, str], ...]]],
) -> List[str]:
    """Resolve, sanitize, and deduplicate sample lines for a MetricFamily."""
    sample_lines: List[str] = []
    for sample in fam.samples:
        resolved_name = _resolve_sample_name(
            sample.name, fam.name, base_name, fam.metric_type
        )
        series_key = (resolved_name, sample.labels)
        if series_key in seen_series:
            logger.warning(
                "Skipping duplicate metric series '%s' with labels %s in scrape",
                resolved_name,
                sample.labels,
            )
            continue
        sanitized_val = _sanitize_sample_value(
            sample.value, fam.metric_type, resolved_name
        )
        if sanitized_val is None:
            continue
        seen_series.add(series_key)
        sample_lines.append(_format_sample_line(resolved_name, sample.labels, sanitized_val))
    return sample_lines


class MetricsFormatter:
    """Serializes MetricFamily collections into Prometheus or OpenMetrics formats."""

    @staticmethod
    def format_prometheus_004(families: Iterable[MetricFamily]) -> bytes:
        """Serialize families into Prometheus 0.0.4 text format."""
        lines: List[str] = []
        seen_families: Set[str] = set()
        seen_series: Set[Tuple[str, Tuple[Tuple[str, str], ...]]] = set()

        for fam in families:
            if not fam.samples:
                continue
            base_name = canonical_family_base_name(fam.name, fam.metric_type)
            prom_name = _resolve_prometheus_family_name(base_name, fam.metric_type)
            if prom_name in seen_families:
                logger.warning(
                    "Skipping duplicate MetricFamily '%s' in Prometheus 0.0.4 scrape",
                    prom_name,
                )
                continue

            sample_lines = _collect_family_sample_lines(fam, base_name, seen_series)
            if not sample_lines:
                continue

            seen_families.add(prom_name)
            lines.append(f"# HELP {prom_name} {escape_help_string(fam.documentation)}")
            lines.append(f"# TYPE {prom_name} {_resolve_prometheus_type(fam.metric_type)}")
            lines.extend(sample_lines)

        lines.append("")  # Trailing newline required
        return "\n".join(lines).encode("utf-8")

    @staticmethod
    def format_openmetrics_100(families: Iterable[MetricFamily]) -> bytes:
        """Serialize families into strict OpenMetrics 1.0.0 text format."""
        lines: List[str] = []
        seen_families: Set[str] = set()
        seen_series: Set[Tuple[str, Tuple[Tuple[str, str], ...]]] = set()

        for fam in families:
            if not fam.samples:
                continue
            base_name = canonical_family_base_name(fam.name, fam.metric_type)
            if base_name in seen_families:
                logger.warning(
                    "Skipping duplicate MetricFamily '%s' in OpenMetrics 1.0.0 scrape",
                    base_name,
                )
                continue

            sample_lines = _collect_family_sample_lines(fam, base_name, seen_series)
            if not sample_lines:
                continue

            seen_families.add(base_name)
            escaped_doc = escape_help_string(fam.documentation, escape_quotes=True)
            lines.append(f"# HELP {base_name} {escaped_doc}")
            lines.append(f"# TYPE {base_name} {_resolve_openmetrics_type(fam.metric_type)}")
            if _is_valid_openmetrics_unit(base_name, fam.unit, fam.metric_type):
                lines.append(f"# UNIT {base_name} {fam.unit}")
            lines.extend(sample_lines)

        lines.append("# EOF\n")
        return "\n".join(lines).encode("utf-8")
