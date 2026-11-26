"""Embedded zero-dependency Prometheus & OpenMetrics operational exporter package."""

from typing import List

import train_tracker

from exporter.collector import (
    BaseCollector,
    MetricRegistry,
    ProcessMetricsCollector,
    StorageMetricsCollector,
    StreamMetricsCollector,
    TrainTelemetryCollector,
    WatchdogMetricsCollector,
)
from exporter.formatter import (
    MetricsFormatter,
    escape_help_string,
    escape_label_value,
    format_float,
    negotiate_metrics_format,
)
from exporter.models import (
    RE_UNIT,
    RESERVED_GAUGE_SUFFIXES,
    ExporterConfig,
    MetricFamily,
    MetricType,
    Sample,
    canonical_family_base_name,
)
from exporter.server import (
    MetricsRequestHandler,
    MetricsServer,
)

__version__: str = train_tracker.__version__
__author__: str = train_tracker.__author__

__all__: List[str] = [
    "__version__",
    "__author__",
    "RE_UNIT",
    "RESERVED_GAUGE_SUFFIXES",
    "canonical_family_base_name",
    "MetricType",
    "Sample",
    "MetricFamily",
    "ExporterConfig",
    "MetricsFormatter",
    "escape_help_string",
    "escape_label_value",
    "format_float",
    "negotiate_metrics_format",
    "BaseCollector",
    "MetricRegistry",
    "TrainTelemetryCollector",
    "WatchdogMetricsCollector",
    "StreamMetricsCollector",
    "StorageMetricsCollector",
    "ProcessMetricsCollector",
    "MetricsServer",
    "MetricsRequestHandler",
]
