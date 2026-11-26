"""Domain models and metric primitives for railmon Prometheus exporter."""

import math
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set, Tuple, Union

RE_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
RE_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
RE_UNIT = re.compile(r"^[a-zA-Z0-9_]+$")

RESERVED_GAUGE_SUFFIXES: Tuple[str, ...] = (
    "_total",
    "_created",
    "_info",
    "_bucket",
    "_sum",
    "_count",
    "_gsum",
    "_gcount",
)


class MetricType(str, Enum):
    """Prometheus & OpenMetrics metric representation types."""

    GAUGE = "gauge"
    COUNTER = "counter"
    INFO = "info"
    SUMMARY = "summary"
    UNTYPED = "untyped"
    UNKNOWN = "unknown"


def canonical_family_base_name(name: str, metric_type: MetricType) -> str:
    """Strip OpenMetrics type-generated suffixes (_total, _info) to obtain canonical family name."""
    if metric_type == MetricType.COUNTER and name.endswith("_total") and len(name) > 6:
        return name[:-6]
    if metric_type == MetricType.INFO and name.endswith("_info") and len(name) > 5:
        return name[:-5]
    return name


@dataclass(frozen=True)
class Sample:
    """Represents a single metric measurement with labels and timestamp."""

    name: str
    labels: Tuple[Tuple[str, str], ...] = ()
    value: float = 0.0
    timestamp: Optional[float] = None

    def __post_init__(self) -> None:
        """Validate sample name, label syntax, and label key uniqueness."""
        if not RE_METRIC_NAME.match(self.name):
            raise ValueError(
                f"Invalid sample name '{self.name}'. Must match ^[a-zA-Z_:][a-zA-Z0-9_:]*$"
            )
        seen_keys: Set[str] = set()
        for k, _ in self.labels:
            if not RE_LABEL_NAME.match(k):
                raise ValueError(
                    f"Invalid label name '{k}'. Must match ^[a-zA-Z_][a-zA-Z0-9_]*$"
                )
            if k.startswith("__"):
                raise ValueError(
                    f"Reserved label name '{k}' starting with '__' is prohibited"
                )
            if k in seen_keys:
                raise ValueError(f"Duplicate label name '{k}' in sample '{self.name}'")
            seen_keys.add(k)


def _validate_metric_family_unit(
    name: str,
    metric_type: MetricType,
    unit: str,
) -> None:
    """Validate OpenMetrics 1.0.0 unit charset, type restrictions, and suffix alignment."""
    if metric_type in (MetricType.INFO, MetricType.UNTYPED, MetricType.UNKNOWN):
        raise ValueError(
            f"OpenMetrics unit '{unit}' is prohibited on metric type '{metric_type.value}'"
        )
    if not RE_UNIT.match(unit):
        raise ValueError(
            f"Invalid OpenMetrics unit '{unit}'. Must match ^[a-zA-Z0-9_]+$"
        )
    base_name = canonical_family_base_name(name, metric_type)
    if not base_name.endswith(f"_{unit}"):
        raise ValueError(
            f"OpenMetrics unit '{unit}' must match '_{unit}' suffix of metric family '{base_name}'"
        )


@dataclass
class MetricFamily:
    """Collection of samples sharing a metric name, documentation, and type."""

    name: str
    documentation: str
    metric_type: MetricType
    unit: Optional[str] = None
    samples: List[Sample] = field(default_factory=list)

    def __post_init__(self) -> None:
        """Validate metric family name syntax, type suffix rules, and unit conformance."""
        if not RE_METRIC_NAME.match(self.name):
            raise ValueError(
                f"Invalid metric family name '{self.name}'. Must match ^[a-zA-Z_:][a-zA-Z0-9_:]*$"
            )
        if self.metric_type == MetricType.GAUGE:
            for suffix in RESERVED_GAUGE_SUFFIXES:
                if self.name.endswith(suffix):
                    raise ValueError(
                        f"Gauge metric family '{self.name}' must not end with reserved suffix '{suffix}'"
                    )
        if self.unit is not None:
            if self.unit == "":
                self.unit = None
            else:
                _validate_metric_family_unit(self.name, self.metric_type, self.unit)

    def add_sample(
        self,
        name: Optional[str] = None,
        labels: Optional[Dict[str, Any]] = None,
        value: Union[int, float] = 0.0,
        timestamp: Optional[float] = None,
    ) -> None:
        """Append a validated, sorted sample to the metric family."""
        sample_name = name or self.name
        sorted_labels: Tuple[Tuple[str, str], ...] = ()
        if labels:
            sorted_labels = tuple(
                sorted((str(k), str(v)) for k, v in labels.items())
            )
        self.samples.append(
            Sample(
                name=sample_name,
                labels=sorted_labels,
                value=float(value),
                timestamp=timestamp,
            )
        )


@dataclass(frozen=True)
class ExporterConfig:
    """Immutable runtime configuration for the Prometheus exporter."""

    host: str = "127.0.0.1"
    port: int = 9100
    db_path: Optional[str] = None
    demo_mode: bool = False
    collect_db_stats: bool = True
    scrape_timeout_seconds: float = 10.0
    gzip_compression: bool = True

    def __post_init__(self) -> None:
        """Validate configuration settings."""
        if not (1 <= self.port <= 65535):
            raise ValueError(f"Port must be between 1 and 65535, got {self.port}")
        if not math.isfinite(self.scrape_timeout_seconds) or self.scrape_timeout_seconds <= 0.0:
            raise ValueError("Scrape timeout must be finite and strictly positive")
