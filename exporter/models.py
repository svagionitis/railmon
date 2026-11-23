"""Domain models and metric primitives for railmon Prometheus exporter."""

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Union

RE_METRIC_NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
RE_LABEL_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


class MetricType(str, Enum):
    """Prometheus & OpenMetrics metric representation types."""

    GAUGE = "gauge"
    COUNTER = "counter"
    INFO = "info"
    SUMMARY = "summary"
    UNTYPED = "untyped"


@dataclass(frozen=True)
class Sample:
    """Represents a single metric measurement with labels and timestamp."""

    name: str
    labels: Tuple[Tuple[str, str], ...] = ()
    value: float = 0.0
    timestamp: Optional[float] = None

    def __post_init__(self) -> None:
        """Validate sample name and label syntax."""
        if not RE_METRIC_NAME.match(self.name):
            raise ValueError(
                f"Invalid sample name '{self.name}'. Must match ^[a-zA-Z_:][a-zA-Z0-9_:]*$"
            )
        for k, _ in self.labels:
            if not RE_LABEL_NAME.match(k):
                raise ValueError(
                    f"Invalid label name '{k}'. Must match ^[a-zA-Z_][a-zA-Z0-9_]*$"
                )
            if k.startswith("__"):
                raise ValueError(
                    f"Reserved label name '{k}' starting with '__' is prohibited"
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
        """Validate metric family name syntax."""
        if not RE_METRIC_NAME.match(self.name):
            raise ValueError(
                f"Invalid metric family name '{self.name}'. Must match ^[a-zA-Z_:][a-zA-Z0-9_:]*$"
            )

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
        if self.scrape_timeout_seconds <= 0.0:
            raise ValueError("Scrape timeout must be strictly positive")
