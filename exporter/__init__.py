"""Embedded zero-dependency Prometheus & OpenMetrics operational exporter package."""

from typing import List

import train_tracker

from exporter.formatter import (
    MetricsFormatter,
    escape_help_string,
    escape_label_value,
    format_float,
    negotiate_metrics_format,
)
from exporter.models import (
    ExporterConfig,
    MetricFamily,
    MetricType,
    Sample,
)

__version__: str = train_tracker.__version__
__author__: str = train_tracker.__author__

__all__: List[str] = [
    "__version__",
    "__author__",
    "MetricType",
    "Sample",
    "MetricFamily",
    "ExporterConfig",
    "MetricsFormatter",
    "escape_help_string",
    "escape_label_value",
    "format_float",
    "negotiate_metrics_format",
]
