"""High-performance zero-dependency Prometheus and OpenMetrics text serializers."""

import math
from typing import Iterable, List, Optional, Tuple

from exporter.models import MetricFamily, MetricType


def escape_help_string(doc: str) -> str:
    """Escape backslashes and newlines in # HELP strings."""
    return doc.replace("\\", "\\\\").replace("\n", "\\n")


def escape_label_value(val: str) -> str:
    """Escape backslashes, double quotes, and newlines in label values."""
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def format_float(val: float) -> str:
    """Format float values according to Prometheus/OpenMetrics lexical rules."""
    if math.isnan(val):
        return "NaN"
    if math.isinf(val):
        return "+Inf" if val > 0 else "-Inf"
    if val.is_integer():
        return f"{int(val)}"
    return f"{val:.6g}"


def negotiate_metrics_format(accept_header: Optional[str]) -> Tuple[str, str]:
    """Negotiate Content-Type and format mode from client Accept header.

    Args:
        accept_header: Value of incoming HTTP 'Accept' header.

    Returns:
        Tuple of (content_type_header_value, format_mode) where format_mode
        is either 'openmetrics' or 'prometheus'.
    """
    if not accept_header:
        return (
            "text/plain; version=0.0.4; charset=utf-8",
            "prometheus",
        )

    # Check for explicit OpenMetrics preference
    if "application/openmetrics-text" in accept_header:
        return (
            "application/openmetrics-text; version=1.0.0; charset=utf-8",
            "openmetrics",
        )

    return (
        "text/plain; version=0.0.4; charset=utf-8",
        "prometheus",
    )


class MetricsFormatter:
    """Serializes MetricFamily collections into Prometheus or OpenMetrics formats."""

    @staticmethod
    def format_prometheus_004(families: Iterable[MetricFamily]) -> bytes:
        """Serialize families into Prometheus 0.0.4 text format."""
        lines: List[str] = []
        for fam in families:
            if not fam.samples:
                continue
            lines.append(f"# HELP {fam.name} {escape_help_string(fam.documentation)}")
            # Prometheus 0.0.4 maps INFO to gauge
            mtype = "gauge" if fam.metric_type == MetricType.INFO else fam.metric_type.value
            lines.append(f"# TYPE {fam.name} {mtype}")
            for s in fam.samples:
                if s.labels:
                    lbls = ",".join(f'{k}="{escape_label_value(v)}"' for k, v in s.labels)
                    lines.append(f"{s.name}{{{lbls}}} {format_float(s.value)}")
                else:
                    lines.append(f"{s.name} {format_float(s.value)}")
        lines.append("")  # Trailing newline required
        return "\n".join(lines).encode("utf-8")

    @staticmethod
    def format_openmetrics_100(families: Iterable[MetricFamily]) -> bytes:
        """Serialize families into OpenMetrics 1.0.0 text format."""
        lines: List[str] = []
        for fam in families:
            if not fam.samples:
                continue
            lines.append(f"# HELP {fam.name} {escape_help_string(fam.documentation)}")
            lines.append(f"# TYPE {fam.name} {fam.metric_type.value}")
            if fam.unit:
                lines.append(f"# UNIT {fam.name} {fam.unit}")
            for s in fam.samples:
                sample_name = s.name
                # OpenMetrics counters enforce _total suffix
                if fam.metric_type == MetricType.COUNTER and not sample_name.endswith("_total"):
                    sample_name = f"{sample_name}_total"

                if s.labels:
                    lbls = ",".join(f'{k}="{escape_label_value(v)}"' for k, v in s.labels)
                    lines.append(f"{sample_name}{{{lbls}}} {format_float(s.value)}")
                else:
                    lines.append(f"{sample_name} {format_float(s.value)}")
        lines.append("# EOF\n")
        return "\n".join(lines).encode("utf-8")
