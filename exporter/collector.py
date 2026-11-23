"""Domain-specific metric collectors for train telemetry, safety, stream, and storage."""

import logging
import os
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from exporter.models import MetricFamily, MetricType
from tui.state import ConnectionStatus, SensorHealthState, TrainStateStore

logger = logging.getLogger("railmon.exporter.collector")


class BaseCollector:
    """Abstract base class for all domain telemetry metric collectors."""

    def collect(self) -> List[MetricFamily]:
        """Gather and return a list of MetricFamily instances.

        Returns:
            List of MetricFamily instances populated with current measurements.
        """
        raise NotImplementedError


class MetricRegistry:
    """Thread-safe registry managing domain collectors and scrape dispatch."""

    def __init__(self) -> None:
        self._collectors: List[BaseCollector] = []
        self._lock = threading.Lock()

    def register(self, collector: BaseCollector) -> None:
        """Register a collector instance if not already present."""
        with self._lock:
            if collector not in self._collectors:
                self._collectors.append(collector)

    def unregister(self, collector: BaseCollector) -> None:
        """Remove a registered collector instance."""
        with self._lock:
            if collector in self._collectors:
                self._collectors.remove(collector)

    def collect_all(self) -> List[MetricFamily]:
        """Orchestrate collection across all registered collectors with fault isolation.

        If any collector raises an unhandled exception, the error is logged as a
        warning and remaining collectors continue executing normally without failing the scrape.
        """
        with self._lock:
            collectors = list(self._collectors)

        families: List[MetricFamily] = []
        for coll in collectors:
            try:
                families.extend(coll.collect())
            except Exception as exc:
                logger.warning(
                    "Collector '%s' raised an unexpected exception during scrape: %s",
                    coll.__class__.__name__,
                    exc,
                    exc_info=True,
                )
        return families


class TrainTelemetryCollector(BaseCollector):
    """Collects real-time active train telemetry, speeds, delays, and punctuality."""

    def __init__(self, store: TrainStateStore) -> None:
        self.store = store

    def collect(self) -> List[MetricFamily]:
        snap = self.store.get_snapshot()

        fam_active = MetricFamily(
            "railmon_active_trains",
            "Total number of currently active trains operating on the network",
            MetricType.GAUGE,
            unit="trains",
        )
        fam_delay = MetricFamily(
            "railmon_train_delay_minutes",
            "Current delay in minutes for an individual train",
            MetricType.GAUGE,
            unit="minutes",
        )
        fam_speed = MetricFamily(
            "railmon_train_speed_kmh",
            "Instantaneous train speed in km/h",
            MetricType.GAUGE,
            unit="km/h",
        )
        fam_max_speed = MetricFamily(
            "railmon_train_max_speed_kmh",
            "Maximum recorded speed for this train during its operating window",
            MetricType.GAUGE,
            unit="km/h",
        )
        fam_delayed_tier = MetricFamily(
            "railmon_trains_delayed_total",
            "Count of active trains categorized by delay severity",
            MetricType.GAUGE,
            unit="trains",
        )
        fam_punctuality = MetricFamily(
            "railmon_corridor_punctuality_ratio",
            "Ratio of active trains running on-time (delay <= 5 min) over total trains",
            MetricType.GAUGE,
            unit="ratio",
        )
        fam_avg_delay = MetricFamily(
            "railmon_corridor_average_delay_minutes",
            "Arithmetic mean delay across all active trains on the corridor",
            MetricType.GAUGE,
            unit="minutes",
        )

        corridor_counts: Dict[str, int] = {}
        corridor_delays: Dict[str, List[float]] = {}
        corridor_tiers: Dict[str, Dict[str, int]] = {}

        for tr in snap.trains.values():
            pos = tr.position
            tn = str(pos.train_number or pos.id)
            ttype = str(pos.train_type or "COMMERCIAL")
            corr = str(getattr(pos, "corridor", None) or "UNKNOWN")
            dly = float(pos.delay) if pos.delay is not None else 0.0
            spd = max(0.0, float(pos.speed)) if pos.speed is not None else 0.0

            # Calculate peak speed from trail breadcrumbs and current speed
            peak_spd = spd
            if tr.trail:
                for b in tr.trail:
                    if b.speed is not None and float(b.speed) > peak_spd:
                        peak_spd = float(b.speed)

            # Sample individual train metrics
            labels_train = {"train_number": tn, "type": ttype, "corridor": corr}
            fam_delay.add_sample(labels=labels_train, value=dly)
            fam_speed.add_sample(labels=labels_train, value=spd)
            fam_max_speed.add_sample(
                labels={"train_number": tn, "corridor": corr}, value=peak_spd
            )

            # Aggregate per corridor
            corridor_counts[corr] = corridor_counts.get(corr, 0) + 1
            if corr not in corridor_delays:
                corridor_delays[corr] = []
                corridor_tiers[corr] = {
                    "on_time": 0,
                    "minor": 0,
                    "moderate": 0,
                    "severe": 0,
                }
            corridor_delays[corr].append(dly)

            if dly <= 5.0:
                corridor_tiers[corr]["on_time"] += 1
            elif dly <= 15.0:
                corridor_tiers[corr]["minor"] += 1
            elif dly <= 60.0:
                corridor_tiers[corr]["moderate"] += 1
            else:
                corridor_tiers[corr]["severe"] += 1

        for corr, count in sorted(corridor_counts.items()):
            fam_active.add_sample(labels={"corridor": corr}, value=count)
            delays = corridor_delays[corr]
            on_time_count = corridor_tiers[corr]["on_time"]
            ratio = (on_time_count / count) if count > 0 else 1.0
            avg_d = (sum(delays) / count) if count > 0 else 0.0

            fam_punctuality.add_sample(labels={"corridor": corr}, value=ratio)
            fam_avg_delay.add_sample(labels={"corridor": corr}, value=avg_d)

            for tier, tier_count in sorted(corridor_tiers[corr].items()):
                fam_delayed_tier.add_sample(
                    labels={"corridor": corr, "severity": tier}, value=tier_count
                )

        return [
            fam_active,
            fam_delay,
            fam_speed,
            fam_max_speed,
            fam_delayed_tier,
            fam_punctuality,
            fam_avg_delay,
        ]


class WatchdogMetricsCollector(BaseCollector):
    """Collects safety conflict alerts, sensor freeze, and telemetry drop metrics."""

    def __init__(self, store: TrainStateStore) -> None:
        self.store = store
        self._cumulative_conflicts: Dict[Tuple[str, str, str], int] = {}
        self._seen_conflict_ids: Set[str] = set()

    def collect(self) -> List[MetricFamily]:
        snap = self.store.get_snapshot()

        fam_conflicts_active = MetricFamily(
            "railmon_watchdog_conflicts_active",
            "Number of currently active safety conflicts detected by watchdog",
            MetricType.GAUGE,
            unit="alerts",
        )
        fam_conflicts_total = MetricFamily(
            "railmon_watchdog_conflicts_total",
            "Cumulative count of safety conflicts triggered since exporter startup",
            MetricType.COUNTER,
            unit="alerts",
        )
        fam_freezes = MetricFamily(
            "railmon_sensor_freezes_active",
            "Number of trains flagged with frozen telemetry coordinates",
            MetricType.GAUGE,
            unit="sensors",
        )
        fam_drops = MetricFamily(
            "railmon_telemetry_drops_active",
            "Number of trains exhibiting dropped telemetry feeds (> 60s)",
            MetricType.GAUGE,
            unit="drops",
        )
        fam_overspeed = MetricFamily(
            "railmon_speed_exceedances_active",
            "Number of trains currently exceeding corridor line speed limits",
            MetricType.GAUGE,
            unit="exceedances",
        )

        active_counts: Dict[Tuple[str, str, str], int] = {}
        for conflict in snap.active_conflicts:
            sev = conflict.severity.name.lower()
            ctype = getattr(conflict, "conflict_type", "collision")
            if hasattr(ctype, "name"):
                ctype = ctype.name.lower()
            corr = (
                getattr(conflict, "corridor_name", None)
                or getattr(conflict, "corridor_id", None)
                or getattr(conflict, "corridor", None)
                or "UNKNOWN"
            )

            key = (sev, str(ctype), str(corr))
            active_counts[key] = active_counts.get(key, 0) + 1

            cid = getattr(
                conflict,
                "alert_id",
                getattr(
                    conflict,
                    "conflict_id",
                    f"{sev}_{ctype}_{corr}_{getattr(conflict, 'train_id_1', getattr(conflict, 'train1_id', ''))}",
                ),
            )
            if cid not in self._seen_conflict_ids:
                self._seen_conflict_ids.add(cid)
                self._cumulative_conflicts[key] = self._cumulative_conflicts.get(key, 0) + 1

        for (sev, ctype, corr), count in sorted(active_counts.items()):
            fam_conflicts_active.add_sample(
                labels={"severity": sev, "conflict_type": ctype, "corridor": corr},
                value=count,
            )

        for (sev, ctype, corr), cum_count in sorted(self._cumulative_conflicts.items()):
            fam_conflicts_total.add_sample(
                labels={"severity": sev, "conflict_type": ctype, "corridor": corr},
                value=cum_count,
            )

        freezes_by_corr: Dict[str, int] = {}
        drops_by_corr: Dict[str, int] = {}
        overspeed_by_corr: Dict[str, int] = {}

        for tr in snap.trains.values():
            corr = str(getattr(tr.position, "corridor", None) or "UNKNOWN")
            if tr.sensor_health:
                sh_state = getattr(tr.sensor_health, "state", None)
                if sh_state == SensorHealthState.FROZEN or (
                    hasattr(sh_state, "name") and "FROZEN" in sh_state.name
                ):
                    freezes_by_corr[corr] = freezes_by_corr.get(corr, 0) + 1
                elif sh_state == SensorHealthState.DROPPED or (
                    hasattr(sh_state, "name") and "DROPPED" in sh_state.name
                ):
                    drops_by_corr[corr] = drops_by_corr.get(corr, 0) + 1

            if tr.speed_result and getattr(tr.speed_result, "severity", None) is not None:
                sev_name = getattr(tr.speed_result.severity, "name", "")
                if sev_name in ("OVERSPEED", "TSR_VIOLATION"):
                    overspeed_by_corr[corr] = overspeed_by_corr.get(corr, 0) + 1

        for corr, count in sorted(freezes_by_corr.items()):
            fam_freezes.add_sample(labels={"corridor": corr}, value=count)
        for corr, count in sorted(drops_by_corr.items()):
            fam_drops.add_sample(labels={"corridor": corr}, value=count)
        for corr, count in sorted(overspeed_by_corr.items()):
            fam_overspeed.add_sample(labels={"corridor": corr}, value=count)

        return [
            fam_conflicts_active,
            fam_conflicts_total,
            fam_freezes,
            fam_drops,
            fam_overspeed,
        ]


class StreamMetricsCollector(BaseCollector):
    """Collects upstream SSE stream connectivity, message count, and latency."""

    def __init__(self, store: TrainStateStore, endpoint: str = "railway.gov.gr") -> None:
        self.store = store
        self.endpoint = endpoint
        self._prev_status: Optional[str] = None
        self._reconnects_total: int = 0

    def collect(self) -> List[MetricFamily]:
        snap = self.store.get_snapshot()

        fam_conn = MetricFamily(
            "railmon_stream_connected",
            "Upstream SSE stream connection status (1 = connected, 0 = disconnected)",
            MetricType.GAUGE,
            unit="boolean",
        )
        fam_msgs = MetricFamily(
            "railmon_stream_messages_total",
            "Total telemetry frames received from upstream",
            MetricType.COUNTER,
            unit="messages",
        )
        fam_reconn = MetricFamily(
            "railmon_stream_reconnects_total",
            "Total count of network reconnection cycles and stream reconnects",
            MetricType.COUNTER,
            unit="events",
        )
        fam_latency = MetricFamily(
            "railmon_stream_latency_seconds",
            "Elapsed seconds since last received upstream telemetry frame",
            MetricType.GAUGE,
            unit="seconds",
        )

        is_conn = 1.0 if getattr(snap.connection_status, "name", "") == "CONNECTED" else 0.0
        fam_conn.add_sample(labels={"endpoint": self.endpoint}, value=is_conn)

        curr_status = getattr(snap.connection_status, "name", str(snap.connection_status))
        if self._prev_status is not None and self._prev_status != curr_status:
            if curr_status in ("CONNECTING", "RECONNECTING"):
                self._reconnects_total += 1
        self._prev_status = curr_status

        fam_reconn.add_sample(labels={"endpoint": self.endpoint}, value=self._reconnects_total)
        fam_msgs.add_sample(
            labels={"event_type": "trainPositionsUx"},
            value=float(snap.total_messages_received),
        )

        now = time.time()
        lat = max(0.0, now - snap.last_stream_update) if snap.last_stream_update > 0 else 0.0
        fam_latency.add_sample(labels={"endpoint": self.endpoint}, value=lat)

        return [fam_conn, fam_msgs, fam_reconn, fam_latency]


class StorageMetricsCollector(BaseCollector):
    """Collects SQLite database file size, WAL size, row counts, and deadband metrics."""

    def __init__(
        self,
        db_path: Optional[str] = None,
        recorder: Optional[Any] = None,
        stats_cache_ttl_seconds: float = 60.0,
    ) -> None:
        self.db_path = db_path
        self.recorder = recorder
        self.stats_cache_ttl_seconds = stats_cache_ttl_seconds
        self._cached_db_stats: Optional[Dict[str, Any]] = None
        self._last_stats_fetch: float = 0.0

    def collect(self) -> List[MetricFamily]:
        families: List[MetricFamily] = []

        fam_records = MetricFamily(
            "railmon_db_records_total",
            "Cumulative telemetry observations stored in the SQLite WAL database",
            MetricType.COUNTER,
            unit="rows",
        )
        fam_file_size = MetricFamily(
            "railmon_db_file_size_bytes",
            "Current file size on disk for main database and write-ahead log",
            MetricType.GAUGE,
            unit="bytes",
        )
        fam_sessions = MetricFamily(
            "railmon_db_sessions_total",
            "Total number of recorded tracking sessions initialized in database",
            MetricType.COUNTER,
            unit="sessions",
        )
        fam_queue = MetricFamily(
            "railmon_db_write_queue_depth",
            "Number of telemetry rows currently queued in-memory awaiting disk commit",
            MetricType.GAUGE,
            unit="rows",
        )
        fam_deadband = MetricFamily(
            "railmon_db_deadband_suppressed_total",
            "Number of raw telemetry updates filtered out by deadband spatial/temporal dampening",
            MetricType.COUNTER,
            unit="rows",
        )

        # 1. Active recorder stats (in-memory, lock-free)
        recorder_active = self.recorder and getattr(self.recorder, "is_running", False)
        if recorder_active:
            try:
                rec_stats = self.recorder.get_stats()
                fam_queue.add_sample(
                    labels={"database": "telemetry"},
                    value=float(rec_stats.get("queue_size", 0)),
                )
                fam_records.add_sample(
                    labels={"table": "telemetry_history"},
                    value=float(rec_stats.get("total_records_written", 0)),
                )
                flt_stats = rec_stats.get("filter", {})
                if flt_stats:
                    fam_deadband.add_sample(
                        labels={"filter": "spatial_temporal"},
                        value=float(flt_stats.get("total_suppressed", 0)),
                    )
            except Exception as exc:
                logger.debug("Failed extracting TelemetryRecorder metrics: %s", exc)

        # 2. Database disk file measurements
        if self.db_path and os.path.exists(self.db_path):
            try:
                main_size = os.path.getsize(self.db_path)
                fam_file_size.add_sample(labels={"file_type": "main"}, value=float(main_size))
                wal_path = f"{self.db_path}-wal"
                wal_size = os.path.getsize(wal_path) if os.path.exists(wal_path) else 0
                fam_file_size.add_sample(labels={"file_type": "wal"}, value=float(wal_size))
            except OSError as err:
                logger.debug("Failed reading database file sizes: %s", err)

        # 3. Database repository stats (cached)
        now = time.monotonic()
        if self.db_path and os.path.exists(self.db_path):
            if (
                now - self._last_stats_fetch >= self.stats_cache_ttl_seconds
                or self._cached_db_stats is None
            ):
                self._cached_db_stats = self._fetch_db_stats()
                self._last_stats_fetch = now

            if self._cached_db_stats:
                if not recorder_active:
                    fam_records.add_sample(
                        labels={"table": "telemetry_history"},
                        value=float(self._cached_db_stats.get("total_telemetry_rows", 0)),
                    )
                fam_sessions.add_sample(
                    labels={"mode": "sqlite"},
                    value=float(self._cached_db_stats.get("total_sessions", 0)),
                )

        families.extend([fam_records, fam_file_size, fam_sessions, fam_queue, fam_deadband])
        return families

    def _fetch_db_stats(self) -> Dict[str, Any]:
        """Safely fetch historical database metrics with error resilience."""
        if not self.db_path or not os.path.exists(self.db_path):
            return {}
        try:
            with open(self.db_path, "rb") as f:
                header = f.read(16)
            if not header.startswith(b"SQLite format 3\x00"):
                return {}
        except Exception:
            return {}

        db = None
        try:
            from storage.db import Database
            from storage.repository import HistoricalRepository

            db = Database(self.db_path, auto_migrate=False)
            repo = HistoricalRepository(db)
            st = repo.get_stats()
            return {
                "total_telemetry_rows": st.total_telemetry_rows,
                "total_sessions": st.total_sessions,
            }
        except Exception as exc:
            logger.debug("Could not query HistoricalRepository stats: %s", exc)
            return {}
        finally:
            if db is not None:
                try:
                    db.close()
                except Exception:
                    pass


class ProcessMetricsCollector(BaseCollector):
    """Collects exporter daemon uptime, scrape latency, and build metadata."""

    def __init__(self, version: str) -> None:
        self.version = version
        self.python_version = (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
        self._start_time = time.time()
        self._scrapes_total: Dict[Tuple[str, str], int] = {}
        self._last_scrape_duration: float = 0.0
        self._lock = threading.Lock()

    def record_scrape(
        self, duration: float, endpoint: str = "/metrics", status: int = 200
    ) -> None:
        """Record the completion of a scrape request."""
        with self._lock:
            key = (endpoint, str(status))
            self._scrapes_total[key] = self._scrapes_total.get(key, 0) + 1
            self._last_scrape_duration = max(0.0, float(duration))

    def collect(self) -> List[MetricFamily]:
        fam_info = MetricFamily(
            "railmon_exporter_build_info",
            "Railmon Prometheus operational exporter build information",
            MetricType.INFO,
        )
        fam_info.add_sample(
            labels={"version": self.version, "python_version": self.python_version},
            value=1.0,
        )

        fam_uptime = MetricFamily(
            "railmon_exporter_uptime_seconds",
            "Elapsed seconds since the exporter daemon was initialized",
            MetricType.COUNTER,
            unit="seconds",
        )
        fam_uptime.add_sample(value=time.time() - self._start_time)

        fam_scrapes = MetricFamily(
            "railmon_exporter_scrapes_total",
            "Total incoming Prometheus scrape requests received",
            MetricType.COUNTER,
            unit="scrapes",
        )
        fam_duration = MetricFamily(
            "railmon_exporter_scrape_duration_seconds",
            "Latency taken to collect, format, and serialize the metrics payload on last scrape",
            MetricType.GAUGE,
            unit="seconds",
        )

        with self._lock:
            for (ep, st), count in sorted(self._scrapes_total.items()):
                fam_scrapes.add_sample(labels={"endpoint": ep, "status": st}, value=count)
            fam_duration.add_sample(
                labels={"endpoint": "/metrics"}, value=self._last_scrape_duration
            )

        return [fam_info, fam_uptime, fam_scrapes, fam_duration]
