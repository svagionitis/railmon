"""Domain-specific metric collectors for train telemetry, safety, stream, and storage."""

import logging
import math
import os
import sqlite3
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from exporter.models import MetricFamily, MetricType
from tui.state import ConnectionStatus, SensorHealthState, TrainStateStore

logger = logging.getLogger("railmon.exporter.collector")


def _clean_label_token(raw: Optional[Any]) -> str:
    """Return stripped string token or empty string for missing values."""
    if raw is None:
        return ""
    return str(raw).strip()


def _safe_finite_float(
    raw: Any,
    default: float = 0.0,
    *,
    min_value: Optional[float] = None,
) -> float:
    """Convert raw numeric telemetry to a finite float with optional lower bound."""
    if raw is None or isinstance(raw, bool):
        return default
    try:
        fval = float(raw)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(fval):
        return default
    if min_value is not None and fval < min_value:
        return min_value
    return fval


def _compute_peak_speed(current_speed: float, trail: Any) -> float:
    """Return maximum finite speed observed across current position and historical trail."""
    peak = current_speed
    if not trail:
        return peak
    for breadcrumb in trail:
        b_speed = _safe_finite_float(getattr(breadcrumb, "speed", None), default=-1.0)
        if b_speed > peak:
            peak = b_speed
    return peak


def _select_collision_candidate(
    base_tn: str,
    loco: str,
    pos_id: str,
    loco_count: int,
) -> str:
    """Select deterministic disambiguation label for a colliding train_number group."""
    if loco and loco_count == 1:
        return f"{base_tn}/{loco}"
    if loco:
        return f"{base_tn}/{loco}#{pos_id}"
    return f"{base_tn}/{pos_id}"


def _build_disambiguated_train_labels(trains: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """Return deterministically sorted (effective_train_number, TrainRecord) pairs.

    Single-unit trains with a unique base train_number preserve their exact commercial
    train_number label. Colliding groups (coupled consists, multiple 'Unknown'/'0'
    switchers, or modem handovers) are deterministically disambiguated so identical
    (metric_name, labels) tuples are never emitted within the same scrape.
    """
    entries: List[Tuple[str, str, str, str, Any]] = []
    base_counts: Dict[str, int] = {}
    loco_counts: Dict[Tuple[str, str], int] = {}

    for store_key, record in trains.items():
        pos = record.position
        raw_tn = _clean_label_token(getattr(pos, "train_number", None))
        pos_id = (
            _clean_label_token(getattr(pos, "id", None))
            or _clean_label_token(store_key)
            or "UNKNOWN"
        )
        base_tn = raw_tn if raw_tn else pos_id
        loco = _clean_label_token(getattr(pos, "locomotive_number", None))
        entries.append((base_tn, loco, pos_id, str(store_key), record))
        base_counts[base_tn] = base_counts.get(base_tn, 0) + 1
        if loco:
            loco_key = (base_tn, loco)
            loco_counts[loco_key] = loco_counts.get(loco_key, 0) + 1

    entries.sort(key=lambda item: (item[0], item[1], item[2], item[3]))
    used_labels: Set[str] = {
        base_tn for base_tn, count in base_counts.items() if count == 1
    }

    resolved: List[Tuple[str, Any]] = []
    max_ordinal = len(entries) + 3
    for base_tn, loco, pos_id, _, record in entries:
        if base_counts[base_tn] == 1:
            resolved.append((base_tn, record))
            continue

        candidate = _select_collision_candidate(
            base_tn, loco, pos_id, loco_counts.get((base_tn, loco), 0)
        )
        if candidate in used_labels:
            for ordinal in range(2, max_ordinal):
                fallback = f"{candidate}#{ordinal}"
                if fallback not in used_labels:
                    candidate = fallback
                    break
        used_labels.add(candidate)
        resolved.append((candidate, record))

    return resolved


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
            unit="kmh",
        )
        fam_max_speed = MetricFamily(
            "railmon_train_max_speed_kmh",
            "Maximum recorded speed for this train during its operating window",
            MetricType.GAUGE,
            unit="kmh",
        )
        fam_delayed_tier = MetricFamily(
            "railmon_trains_delayed",
            "Count of active trains categorized by delay severity",
            MetricType.GAUGE,
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

        for effective_tn, tr in _build_disambiguated_train_labels(snap.trains):
            pos = tr.position
            ttype = str(pos.train_type or "COMMERCIAL")
            corr = str(getattr(pos, "corridor", None) or "UNKNOWN")
            dly = _safe_finite_float(pos.delay, default=0.0)
            spd = _safe_finite_float(pos.speed, default=0.0, min_value=0.0)
            peak_spd = _compute_peak_speed(spd, tr.trail)

            # Sample individual train metrics
            labels_train = {"train_number": effective_tn, "type": ttype, "corridor": corr}
            fam_delay.add_sample(labels=labels_train, value=dly)
            fam_speed.add_sample(labels=labels_train, value=spd)
            fam_max_speed.add_sample(
                labels={"train_number": effective_tn, "corridor": corr}, value=peak_spd
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

        fam_ghosts = MetricFamily(
            "railmon_ghost_trains",
            "Count of active scheduled ghost runs lacking live GPS telemetry",
            MetricType.GAUGE,
        )
        ghost_corridors: Dict[str, int] = {}
        for grec in (getattr(snap, "ghost_trains", {}) or {}).values():
            g_corr = str(getattr(grec.ghost, "corridor", None) or "UNKNOWN")
            ghost_corridors[g_corr] = ghost_corridors.get(g_corr, 0) + 1
        for g_corr, g_count in sorted(ghost_corridors.items()):
            fam_ghosts.add_sample(labels={"corridor": g_corr}, value=g_count)

        return [
            fam_active,
            fam_delay,
            fam_speed,
            fam_max_speed,
            fam_delayed_tier,
            fam_punctuality,
            fam_avg_delay,
            fam_ghosts,
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
        )
        fam_conflicts_total = MetricFamily(
            "railmon_watchdog_conflicts",
            "Cumulative count of safety conflicts triggered since exporter startup",
            MetricType.COUNTER,
        )
        fam_freezes = MetricFamily(
            "railmon_sensor_freezes_active",
            "Number of trains flagged with frozen telemetry coordinates",
            MetricType.GAUGE,
        )
        fam_drops = MetricFamily(
            "railmon_telemetry_drops_active",
            "Number of trains exhibiting dropped telemetry feeds (> 60s)",
            MetricType.GAUGE,
        )
        fam_overspeed = MetricFamily(
            "railmon_speed_exceedances_active",
            "Number of trains currently exceeding corridor line speed limits",
            MetricType.GAUGE,
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
        )
        fam_msgs = MetricFamily(
            "railmon_stream_messages",
            "Total telemetry frames received from upstream",
            MetricType.COUNTER,
        )
        fam_reconn = MetricFamily(
            "railmon_stream_reconnects",
            "Total count of network reconnection cycles and stream reconnects",
            MetricType.COUNTER,
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

        fam_events = MetricFamily(
            "railmon_stream_events",
            "Cumulative multiplexed SSE events received per channel",
            MetricType.COUNTER,
        )
        for evt_name, evt_count in getattr(snap, "stream_event_counts", ()):
            fam_events.add_sample(
                labels={"event_type": str(evt_name)},
                value=float(evt_count),
            )

        now = time.time()
        lat = max(0.0, now - snap.last_stream_update) if snap.last_stream_update > 0 else 0.0
        fam_latency.add_sample(labels={"endpoint": self.endpoint}, value=lat)

        return [fam_conn, fam_msgs, fam_reconn, fam_latency, fam_events]


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
        self.stats_cache_ttl_seconds = max(0.0, float(stats_cache_ttl_seconds))
        self._error_retry_cooldown_seconds = min(5.0, self.stats_cache_ttl_seconds)
        self._cached_db_stats: Optional[Dict[str, int]] = None
        self._last_stats_fetch: Optional[float] = None
        self._lock = threading.Lock()

    def _collect_recorder_metrics(
        self,
        fam_queue: MetricFamily,
        fam_records: MetricFamily,
        fam_deadband: MetricFamily,
    ) -> bool:
        """Populate in-memory recorder metrics if recorder is running."""
        recorder_active = bool(self.recorder and getattr(self.recorder, "is_running", False))
        if not recorder_active:
            return False
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
        except (AttributeError, KeyError, TypeError, ValueError, RuntimeError) as exc:
            logger.debug("Failed extracting TelemetryRecorder metrics: %s", exc)
        return True

    def _get_or_refresh_db_stats(self) -> Optional[Dict[str, int]]:
        """Return cached historical DB stats, refreshing safely without poisoning on errors."""
        now = time.monotonic()
        with self._lock:
            is_fresh = (
                self._last_stats_fetch is not None
                and (now - self._last_stats_fetch) < self.stats_cache_ttl_seconds
            )
            if is_fresh:
                return dict(self._cached_db_stats) if self._cached_db_stats is not None else None

            fetched = self._fetch_db_stats()
            if fetched is not None:
                self._cached_db_stats = fetched
                self._last_stats_fetch = now
                return dict(fetched)

            # Preserve last known good stats and schedule a bounded retry cooldown
            self._last_stats_fetch = (
                now - self.stats_cache_ttl_seconds + self._error_retry_cooldown_seconds
            )
            return dict(self._cached_db_stats) if self._cached_db_stats is not None else None

    def collect(self) -> List[MetricFamily]:
        families: List[MetricFamily] = []

        fam_records = MetricFamily(
            "railmon_db_records",
            "Cumulative telemetry observations stored in the SQLite WAL database",
            MetricType.COUNTER,
        )
        fam_file_size = MetricFamily(
            "railmon_db_file_size_bytes",
            "Current file size on disk for main database and write-ahead log",
            MetricType.GAUGE,
            unit="bytes",
        )
        fam_sessions = MetricFamily(
            "railmon_db_sessions",
            "Total number of recorded tracking sessions initialized in database",
            MetricType.COUNTER,
        )
        fam_queue = MetricFamily(
            "railmon_db_write_queue_depth",
            "Number of telemetry rows currently queued in-memory awaiting disk commit",
            MetricType.GAUGE,
        )
        fam_deadband = MetricFamily(
            "railmon_db_deadband_suppressed",
            "Number of raw telemetry updates filtered out by deadband spatial/temporal dampening",
            MetricType.COUNTER,
        )

        # 1. Active recorder stats (in-memory, lock-free)
        recorder_active = self._collect_recorder_metrics(fam_queue, fam_records, fam_deadband)

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

            # 3. Database repository stats (cached, read-only SQLite URI)
            db_stats = self._get_or_refresh_db_stats()
            if db_stats is not None:
                if not recorder_active:
                    fam_records.add_sample(
                        labels={"table": "telemetry_history"},
                        value=float(db_stats.get("total_telemetry_rows", 0)),
                    )
                fam_sessions.add_sample(
                    labels={"mode": "sqlite"},
                    value=float(db_stats.get("total_sessions", 0)),
                )

        families.extend([fam_records, fam_file_size, fam_sessions, fam_queue, fam_deadband])
        return families

    def _fetch_db_stats(self) -> Optional[Dict[str, int]]:
        """Safely fetch historical database metrics over a strictly read-only SQLite handle."""
        if not self.db_path or not os.path.exists(self.db_path):
            return None
        try:
            with open(self.db_path, "rb") as fh:
                header = fh.read(16)
            if not header.startswith(b"SQLite format 3\x00"):
                return None
        except OSError as exc:
            logger.debug("Could not inspect SQLite header for '%s': %s", self.db_path, exc)
            return None

        db: Optional[Any] = None
        try:
            from storage.db import Database
            from storage.repository import HistoricalRepository

            db = Database(self.db_path, auto_migrate=False, read_only=True)
            repo = HistoricalRepository(db)
            stats = repo.get_database_stats()
            return {
                "total_telemetry_rows": int(stats.total_telemetry_rows),
                "total_sessions": int(stats.total_sessions),
            }
        except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
            logger.debug("Could not query HistoricalRepository stats: %s", exc)
            return None
        finally:
            if db is not None:
                try:
                    db.close()
                except (sqlite3.Error, OSError) as close_exc:
                    logger.debug("Error closing read-only stats DB handle: %s", close_exc)


class ProcessMetricsCollector(BaseCollector):
    """Collects exporter daemon uptime, scrape latency, and build metadata."""

    def __init__(self, version: str) -> None:
        self.version = version
        self.python_version = (
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        )
        self._start_mono = time.perf_counter()
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
            self._last_scrape_duration = _safe_finite_float(duration, default=0.0, min_value=0.0)

    def collect(self) -> List[MetricFamily]:
        fam_info = MetricFamily(
            "railmon_exporter_build",
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
        fam_uptime.add_sample(value=max(0.0, time.perf_counter() - self._start_mono))

        fam_scrapes = MetricFamily(
            "railmon_exporter_scrapes",
            "Total incoming Prometheus scrape requests received",
            MetricType.COUNTER,
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
