"""Unit and integration test suite for exporter domain collectors and registry."""

import concurrent.futures
import math
import os
import stat
import tempfile
import time
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from exporter.collector import (
    BaseCollector,
    MetricRegistry,
    ProcessMetricsCollector,
    StorageMetricsCollector,
    StreamMetricsCollector,
    TrainTelemetryCollector,
    WatchdogMetricsCollector,
)
from exporter.formatter import MetricsFormatter
from exporter.models import MetricFamily, MetricType
from storage.db import Database
from tests.test_exporter_formatter import validate_openmetrics_100
from tui.state import (
    ConflictAlert,
    ConflictSeverity,
    ConflictWatchdogConfig,
    ConnectionStatus,
    HistoricalBreadcrumb,
    OverspeedSeverity,
    SensorHealthAssessment,
    SensorHealthState,
    SlowOrder,
    SpeedEnforcementResult,
    TrainPosition,
    TrainRecord,
    TrainStateStore,
)


def _make_train_position(
    train_id: str,
    train_number: str = "IC52",
    speed: float = 85.0,
    delay: float = 0.0,
    corridor: str = "PATHE",
    lat: float = 38.0,
    lng: float = 23.7,
) -> TrainPosition:
    """Helper creating a valid TrainPosition instance for testing."""
    return TrainPosition(
        id=train_id,
        train_number=train_number,
        locomotive_number="120-001",
        name=train_number,
        train_type="InterCity",
        service_type="Passenger",
        status="RUNNING",
        origin="Athens",
        destination="Thessaloniki",
        next_station="SKA",
        speed=speed,
        course=15.0,
        track_heading=15.0,
        lat=lat,
        lng=lng,
        altitude=45.0,
        delay=delay,
        progress=0.25,
        chainage_m=15000,
        corridor=corridor,
        distance_to_destination_m=450000,
        timestamp="2026-11-23T15:00:00Z",
        schedule_id="sch-101",
    )


class DummyCollector(BaseCollector):
    """Dummy test collector returning a static MetricFamily."""

    def __init__(self, name: str = "dummy_metric", value: float = 42.0) -> None:
        self.name = name
        self.value = value

    def collect(self) -> List[MetricFamily]:
        fam = MetricFamily(self.name, "A dummy metric for testing", MetricType.GAUGE)
        fam.add_sample(value=self.value)
        return [fam]


class FailingCollector(BaseCollector):
    """Collector that intentionally raises an exception to test fault isolation."""

    def collect(self) -> List[MetricFamily]:
        raise RuntimeError("Simulated collector hardware/network failure")


class TestMetricRegistry(unittest.TestCase):
    """Tests for MetricRegistry registration, lifecycle, and fault isolation."""

    def test_base_collector_not_implemented(self) -> None:
        collector = BaseCollector()
        with self.assertRaises(NotImplementedError):
            collector.collect()

    def test_registration_and_deregistration(self) -> None:
        reg = MetricRegistry()
        c1 = DummyCollector("c1", 10.0)
        c2 = DummyCollector("c2", 20.0)

        reg.register(c1)
        reg.register(c2)
        # Duplicate registration should be idempotent
        reg.register(c1)

        fams = reg.collect_all()
        names = [f.name for f in fams]
        self.assertEqual(names, ["c1", "c2"])

        # Unregister c1
        reg.unregister(c1)
        fams_after = reg.collect_all()
        self.assertEqual([f.name for f in fams_after], ["c2"])

        # Unregister unknown collector should be no-op
        reg.unregister(c1)

    def test_fault_isolation(self) -> None:
        reg = MetricRegistry()
        healthy1 = DummyCollector("healthy_before", 1.0)
        faulty = FailingCollector()
        healthy2 = DummyCollector("healthy_after", 2.0)

        reg.register(healthy1)
        reg.register(faulty)
        reg.register(healthy2)

        # Scrape must not raise and should return metrics from healthy collectors
        fams = reg.collect_all()
        names = [f.name for f in fams]
        self.assertEqual(names, ["healthy_before", "healthy_after"])


class TestTrainTelemetryCollector(unittest.TestCase):
    """Tests for TrainTelemetryCollector fleet, speed, delay, and punctuality metrics."""

    def setUp(self) -> None:
        self.store = TrainStateStore()
        self.collector = TrainTelemetryCollector(self.store)

    def test_empty_store_metrics(self) -> None:
        fams = self.collector.collect()
        self.assertEqual(len(fams), 8)
        self.assertIn("railmon_ghost_trains", {f.name for f in fams})
        # All families present but zero samples
        for fam in fams:
            self.assertEqual(len(fam.samples), 0)

    def test_populated_store_metrics(self) -> None:
        # Train 1: PATHE on-time (delay 2.0 min)
        t1 = _make_train_position("1", "IC52", speed=140.0, delay=2.0, corridor="PATHE")
        # Train 2: PATHE moderate delay (delay 25.0 min)
        t2 = _make_train_position("2", "IC54", speed=110.0, delay=25.0, corridor="PATHE")
        # Train 3: PROASTIAKOS minor delay (delay 8.0 min)
        t3 = _make_train_position("3", "1597", speed=90.0, delay=8.0, corridor="PROASTIAKOS")
        # Train 4: PROASTIAKOS severe delay (delay 75.0 min)
        t4 = _make_train_position("4", "2232", speed=60.0, delay=75.0, corridor="PROASTIAKOS")

        self.store.update_positions([t1, t2, t3, t4])

        # Add historical breadcrumbs with peak speed to train 1
        with self.store._lock:
            rec = self.store._trains["1"]
            rec.trail.append(
                HistoricalBreadcrumb(lat=38.1, lng=23.8, timestamp=time.time() - 30, speed=160.0)
            )

        fams = self.collector.collect()
        fam_map = {f.name: f for f in fams}

        # 1. Active trains
        fam_active = fam_map["railmon_active_trains"]
        active_by_corr = {dict(s.labels)["corridor"]: s.value for s in fam_active.samples}
        self.assertEqual(active_by_corr, {"PATHE": 2.0, "PROASTIAKOS": 2.0})

        # 2. Train delays
        fam_delay = fam_map["railmon_train_delay_minutes"]
        delays = {dict(s.labels)["train_number"]: s.value for s in fam_delay.samples}
        self.assertEqual(delays["IC52"], 2.0)
        self.assertEqual(delays["IC54"], 25.0)
        self.assertEqual(delays["1597"], 8.0)
        self.assertEqual(delays["2232"], 75.0)

        # 3. Train speeds & peak speeds
        fam_speed = fam_map["railmon_train_speed_kmh"]
        speeds = {dict(s.labels)["train_number"]: s.value for s in fam_speed.samples}
        self.assertEqual(speeds["IC52"], 140.0)

        fam_max_speed = fam_map["railmon_train_max_speed_kmh"]
        max_speeds = {dict(s.labels)["train_number"]: s.value for s in fam_max_speed.samples}
        self.assertEqual(max_speeds["IC52"], 160.0)  # Peak from trail

        # 4. Delayed tiers
        fam_tiers = fam_map["railmon_trains_delayed"]
        pathe_tiers = {
            dict(s.labels)["severity"]: s.value
            for s in fam_tiers.samples
            if dict(s.labels)["corridor"] == "PATHE"
        }
        self.assertEqual(pathe_tiers["on_time"], 1.0)
        self.assertEqual(pathe_tiers["moderate"], 1.0)
        self.assertEqual(pathe_tiers["minor"], 0.0)
        self.assertEqual(pathe_tiers["severe"], 0.0)

        # 5. Punctuality ratio
        fam_punct = fam_map["railmon_corridor_punctuality_ratio"]
        punct = {dict(s.labels)["corridor"]: s.value for s in fam_punct.samples}
        # 1 on-time out of 2 = 0.5
        self.assertAlmostEqual(punct["PATHE"], 0.5)
        # 0 on-time out of 2 = 0.0
        self.assertAlmostEqual(punct["PROASTIAKOS"], 0.0)

        # 6. Average delay
        fam_avg = fam_map["railmon_corridor_average_delay_minutes"]
        avg = {dict(s.labels)["corridor"]: s.value for s in fam_avg.samples}
        self.assertAlmostEqual(avg["PATHE"], 13.5)  # (2.0 + 25.0) / 2
        self.assertAlmostEqual(avg["PROASTIAKOS"], 41.5)  # (8.0 + 75.0) / 2

    def test_train_reaping_avoids_timeseries_leak(self) -> None:
        t1 = _make_train_position("1", "IC52", speed=100.0, delay=0.0, corridor="PATHE")
        self.store.update_positions([t1])

        fams1 = self.collector.collect()
        delays1 = {dict(s.labels)["train_number"] for s in fams1[1].samples}
        self.assertIn("IC52", delays1)

        # Remove train from store (simulating trip completion or prune)
        with self.store._lock:
            del self.store._trains["1"]

        fams2 = self.collector.collect()
        delays2 = {dict(s.labels).get("train_number") for s in fams2[1].samples}
        self.assertNotIn("IC52", delays2)


class TestWatchdogMetricsCollector(unittest.TestCase):
    """Tests for WatchdogMetricsCollector safety conflicts, freezes, and overspeeds."""

    def setUp(self) -> None:
        self.store = TrainStateStore()
        self.collector = WatchdogMetricsCollector(self.store)

    def test_safety_conflicts_and_cumulative_counters(self) -> None:
        alert1 = ConflictAlert(
            alert_id="ALERT-001",
            train_id_1="1",
            train_number_1="IC52",
            train_id_2="2",
            train_number_2="IC54",
            corridor_id="pathe",
            corridor_name="PATHE",
            conflict_type=MagicMock(name="opposing_collision"),
            severity=ConflictSeverity.CRITICAL,
            distance_km=2.5,
            closing_speed_kmh=180.0,
            time_to_collision_sec=50.0,
            is_single_track=True,
            station_proximity_name=None,
            description="Opposing vectors on single track",
            timestamp=time.time(),
        )

        mock_snap1 = MagicMock()
        mock_snap1.active_conflicts = (alert1,)
        mock_snap1.trains = {}

        self.store.get_snapshot = MagicMock(return_value=mock_snap1)

        fams = self.collector.collect()
        fam_active = [f for f in fams if f.name == "railmon_watchdog_conflicts_active"][0]
        fam_total = [f for f in fams if f.name == "railmon_watchdog_conflicts"][0]

        self.assertEqual(len(fam_active.samples), 1)
        self.assertEqual(fam_active.samples[0].value, 1.0)
        self.assertEqual(dict(fam_active.samples[0].labels)["severity"], "critical")
        self.assertEqual(fam_total.samples[0].value, 1.0)

        # Second scrape with same alert still active should keep cumulative counter at 1
        fams_second = self.collector.collect()
        fam_total_second = [f for f in fams_second if f.name == "railmon_watchdog_conflicts"][0]
        self.assertEqual(fam_total_second.samples[0].value, 1.0)

        # Third scrape with a new alert should increment cumulative counter to 2
        alert2 = ConflictAlert(
            alert_id="ALERT-002",
            train_id_1="3",
            train_number_1="1597",
            train_id_2="4",
            train_number_2="2232",
            corridor_id="proastiakos",
            corridor_name="PROASTIAKOS",
            conflict_type=MagicMock(name="headway_violation"),
            severity=ConflictSeverity.WARNING,
            distance_km=1.2,
            closing_speed_kmh=30.0,
            time_to_collision_sec=None,
            is_single_track=False,
            station_proximity_name=None,
            description="Headway spacing collapse",
            timestamp=time.time(),
        )
        mock_snap2 = MagicMock()
        mock_snap2.active_conflicts = (alert1, alert2)
        mock_snap2.trains = {}
        self.store.get_snapshot = MagicMock(return_value=mock_snap2)

        fams_third = self.collector.collect()
        fam_total_third = [f for f in fams_third if f.name == "railmon_watchdog_conflicts"][0]
        total_sum = sum(s.value for s in fam_total_third.samples)
        self.assertEqual(total_sum, 2.0)

    def test_sensor_health_and_overspeed(self) -> None:
        t1 = _make_train_position("1", "IC52", speed=150.0, corridor="PATHE")
        t2 = _make_train_position("2", "1597", speed=0.0, corridor="PROASTIAKOS")
        t3 = _make_train_position("3", "2232", speed=175.0, corridor="PATHE")

        rec1 = TrainRecord(position=t1)
        rec1.sensor_health = SensorHealthAssessment(
            state=SensorHealthState.FROZEN,
            elapsed_sec=120.0,
            static_duration_sec=120.0,
            displacement_m=0.0,
            current_speed_kmh=0.0,
        )

        rec2 = TrainRecord(position=t2)
        rec2.sensor_health = SensorHealthAssessment(
            state=SensorHealthState.DROPPED,
            elapsed_sec=180.0,
            static_duration_sec=180.0,
            displacement_m=0.0,
            current_speed_kmh=0.0,
        )

        rec3 = TrainRecord(position=t3)
        rec3.speed_result = SpeedEnforcementResult(
            train_id="3",
            current_speed=175.0,
            governing_limit=160.0,
            margin_kmh=15.0,
            severity=OverspeedSeverity.OVERSPEED,
            governing_name="PATHE Vmax",
            is_tsr=False,
        )

        mock_snap = MagicMock()
        mock_snap.active_conflicts = ()
        mock_snap.trains = {"1": rec1, "2": rec2, "3": rec3}
        self.store.get_snapshot = MagicMock(return_value=mock_snap)

        fams = self.collector.collect()
        fam_map = {f.name: f for f in fams}

        # Freezes
        fam_freezes = fam_map["railmon_sensor_freezes_active"]
        freezes = {dict(s.labels)["corridor"]: s.value for s in fam_freezes.samples}
        self.assertEqual(freezes.get("PATHE"), 1.0)

        # Drops
        fam_drops = fam_map["railmon_telemetry_drops_active"]
        drops = {dict(s.labels)["corridor"]: s.value for s in fam_drops.samples}
        self.assertEqual(drops.get("PROASTIAKOS"), 1.0)

        # Overspeed
        fam_overspeed = fam_map["railmon_speed_exceedances_active"]
        overspeed = {dict(s.labels)["corridor"]: s.value for s in fam_overspeed.samples}
        self.assertEqual(overspeed.get("PATHE"), 1.0)


class TestStreamMetricsCollector(unittest.TestCase):
    """Tests for StreamMetricsCollector connection, message, and latency metrics."""

    def setUp(self) -> None:
        self.store = TrainStateStore()
        self.collector = StreamMetricsCollector(self.store, endpoint="test-api.railway.gov.gr")

    def test_connection_states_and_reconnect_counting(self) -> None:
        # Initial: DISCONNECTED
        fams0 = self.collector.collect()
        fam_conn0 = [f for f in fams0 if f.name == "railmon_stream_connected"][0]
        self.assertEqual(fam_conn0.samples[0].value, 0.0)

        # Transition to CONNECTING -> should record 1 reconnect event
        self.store.set_connection_status(ConnectionStatus.CONNECTING, "Connecting...")
        fams1 = self.collector.collect()
        fam_reconn1 = [f for f in fams1 if f.name == "railmon_stream_reconnects"][0]
        self.assertEqual(fam_reconn1.samples[0].value, 1.0)

        # Transition to CONNECTED -> connected gauge becomes 1.0
        self.store.set_connection_status(ConnectionStatus.CONNECTED, "Stream connected")
        fams2 = self.collector.collect()
        fam_conn2 = [f for f in fams2 if f.name == "railmon_stream_connected"][0]
        self.assertEqual(fam_conn2.samples[0].value, 1.0)

    def test_message_counter_and_latency(self) -> None:
        t_now = time.time()
        with self.store._lock:
            self.store._total_messages = 1337
            self.store._last_update_ts = t_now - 2.5

        fams = self.collector.collect()
        fam_map = {f.name: f for f in fams}

        fam_msgs = fam_map["railmon_stream_messages"]
        self.assertEqual(fam_msgs.samples[0].value, 1337.0)

        fam_lat = fam_map["railmon_stream_latency_seconds"]
        self.assertGreaterEqual(fam_lat.samples[0].value, 2.0)


class TestStorageMetricsCollector(unittest.TestCase):
    """Tests for StorageMetricsCollector file size, WAL size, queue depth, and deadband metrics."""

    def test_no_db_and_no_recorder(self) -> None:
        collector = StorageMetricsCollector(db_path=None, recorder=None)
        fams = collector.collect()
        self.assertEqual(len(fams), 5)
        for fam in fams:
            self.assertEqual(len(fam.samples), 0)

    def test_disk_file_measurements(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            db_file = os.path.join(tmpdir, "test.db")
            wal_file = f"{db_file}-wal"

            # Write dummy bytes
            with open(db_file, "wb") as f:
                f.write(b"0" * 4096)
            with open(wal_file, "wb") as f:
                f.write(b"0" * 2048)

            collector = StorageMetricsCollector(db_path=db_file)
            fams = collector.collect()
            fam_file_size = [f for f in fams if f.name == "railmon_db_file_size_bytes"][0]

            sizes = {dict(s.labels)["file_type"]: s.value for s in fam_file_size.samples}
            self.assertEqual(sizes["main"], 4096.0)
            self.assertEqual(sizes["wal"], 2048.0)

    def test_recorder_metrics(self) -> None:
        mock_recorder = MagicMock()
        mock_recorder.is_running = True
        mock_recorder.get_stats.return_value = {
            "queue_size": 42,
            "total_records_written": 10500,
            "filter": {
                "total_suppressed": 3500,
            },
        }

        collector = StorageMetricsCollector(recorder=mock_recorder)
        fams = collector.collect()
        fam_map = {f.name: f for f in fams}

        # Queue depth
        fam_queue = fam_map["railmon_db_write_queue_depth"]
        self.assertEqual(fam_queue.samples[0].value, 42.0)

        # Records written
        fam_rec = fam_map["railmon_db_records"]
        self.assertEqual(fam_rec.samples[0].value, 10500.0)

        # Deadband suppressed
        fam_dead = fam_map["railmon_db_deadband_suppressed"]
        self.assertEqual(fam_dead.samples[0].value, 3500.0)


class TestProcessMetricsCollector(unittest.TestCase):
    """Tests for ProcessMetricsCollector build info, uptime, and scrape tracking."""

    def test_process_metrics(self) -> None:
        collector = ProcessMetricsCollector(version="1.2.3")
        collector.record_scrape(duration=0.0035, endpoint="/metrics", status=200)
        collector.record_scrape(duration=0.0040, endpoint="/metrics", status=200)
        collector.record_scrape(duration=0.0010, endpoint="/health", status=404)

        fams = collector.collect()
        fam_map = {f.name: f for f in fams}

        # Build info
        fam_info = fam_map["railmon_exporter_build"]
        self.assertEqual(fam_info.samples[0].value, 1.0)
        labels = dict(fam_info.samples[0].labels)
        self.assertEqual(labels["version"], "1.2.3")
        self.assertIn("python_version", labels)

        # Uptime
        fam_uptime = fam_map["railmon_exporter_uptime_seconds"]
        self.assertGreater(fam_uptime.samples[0].value, 0.0)

        # Scrapes total
        fam_scrapes = fam_map["railmon_exporter_scrapes"]
        scrapes = {
            (dict(s.labels)["endpoint"], dict(s.labels)["status"]): s.value
            for s in fam_scrapes.samples
        }
        self.assertEqual(scrapes.get(("/metrics", "200")), 2.0)
        self.assertEqual(scrapes.get(("/health", "404")), 1.0)

        # Duration
        fam_dur = fam_map["railmon_exporter_scrape_duration_seconds"]
        self.assertEqual(fam_dur.samples[0].value, 0.0010)


class TestCollectorEndToEndIntegration(unittest.TestCase):
    """Integration test combining registry, all collectors, and Phase 1 formatter."""

    def test_end_to_end_pipeline(self) -> None:
        store = TrainStateStore()
        store.set_connection_status(ConnectionStatus.CONNECTED, "Live")
        pos = _make_train_position("1", "IC52", speed=135.0, delay=3.5, corridor="PATHE")
        store.update_positions([pos])

        registry = MetricRegistry()
        registry.register(TrainTelemetryCollector(store))
        registry.register(WatchdogMetricsCollector(store))
        registry.register(StreamMetricsCollector(store))
        registry.register(StorageMetricsCollector())
        registry.register(ProcessMetricsCollector(version="2.0.0"))

        families = registry.collect_all()
        self.assertGreater(len(families), 15)

        # Serialize into Prometheus 0.0.4 text
        prom_bytes = MetricsFormatter.format_prometheus_004(families)
        prom_text = prom_bytes.decode("utf-8")
        self.assertIn("# HELP railmon_active_trains", prom_text)
        self.assertIn('railmon_active_trains{corridor="PATHE"} 1', prom_text)
        self.assertIn('railmon_train_delay_minutes{corridor="PATHE",train_number="IC52",type="InterCity"} 3.5', prom_text)
        self.assertTrue(prom_text.endswith("\n"))

        # Serialize into OpenMetrics 1.0.0 text
        open_bytes = MetricsFormatter.format_openmetrics_100(families)
        self.assertEqual(validate_openmetrics_100(open_bytes), [])
        open_text = open_bytes.decode("utf-8")
        self.assertIn("# UNIT railmon_train_delay_minutes minutes\n", open_text)
        self.assertIn("# UNIT railmon_train_speed_kmh kmh\n", open_text)
        self.assertIn("# UNIT railmon_exporter_uptime_seconds seconds\n", open_text)
        self.assertNotIn("# UNIT railmon_active_trains", open_text)
        self.assertTrue(open_text.endswith("# EOF\n"))

    def test_tc_edge_04_extreme_kinematics_and_none_attributes(self) -> None:
        """TC-EXP-EDGE-04: Extreme kinematics (NaN speed, None delay, missing corridor) collected safely."""
        store = TrainStateStore()
        # Train with None corridor, None delay, NaN speed
        pos = TrainPosition(
            id="TRAIN-EDGE",
            train_number="",
            locomotive_number=None,
            name=None,
            train_type=None,
            service_type=None,
            status=None,
            origin=None,
            destination=None,
            next_station=None,
            speed=float("nan"),
            course=None,
            track_heading=None,
            lat=38.0,
            lng=23.5,
            altitude=None,
            delay=None,
            progress=None,
            chainage_m=None,
            corridor=None,
            distance_to_destination_m=None,
            timestamp=None,
            schedule_id=None,
        )
        store.update_positions([pos])

        collector = TrainTelemetryCollector(store)
        fams = collector.collect()
        fam_map = {f.name: f for f in fams}

        # Active trains must assign UNKNOWN corridor
        active_fam = fam_map["railmon_active_trains"]
        corridors = [dict(s.labels).get("corridor") for s in active_fam.samples]
        self.assertIn("UNKNOWN", corridors)

        # Delays should default to 0.0 without throwing
        delay_fam = fam_map["railmon_train_delay_minutes"]
        self.assertEqual(len(delay_fam.samples), 1)
        self.assertEqual(delay_fam.samples[0].value, 0.0)

    def test_tc_edge_10_corrupt_or_truncated_sqlite_db(self) -> None:
        """TC-EXP-EDGE-10: Corrupt or non-SQLite files handle gracefully without raising."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 1. 0-byte file
            empty_file = os.path.join(tmpdir, "empty.db")
            with open(empty_file, "wb") as f:
                pass

            collector_empty = StorageMetricsCollector(db_path=empty_file)
            fams_empty = collector_empty.collect()
            self.assertEqual(len(fams_empty), 5)
            # Size should be 0
            file_size_sample = [f for f in fams_empty if f.name == "railmon_db_file_size_bytes"][0].samples[0]
            self.assertEqual(file_size_sample.value, 0.0)

            # 2. Corrupt garbage file
            garbage_file = os.path.join(tmpdir, "garbage.db")
            with open(garbage_file, "wb") as f:
                f.write(b"NOT_A_SQLITE_DATABASE_HEADER_DATA_PADDING_BYTES")

            collector_garbage = StorageMetricsCollector(db_path=garbage_file)
            fams_garbage = collector_garbage.collect()
            self.assertEqual(len(fams_garbage), 5)

    def test_tc_omx_08_coupled_consist_label_disambiguation(self) -> None:
        """TC-OMX-08: Coupled multiple-unit consist sharing train_number emits distinct valid series."""
        store = TrainStateStore()
        u1 = TrainPosition(
            id="id-a", train_number="2232", locomotive_number="460103", name="2232",
            train_type="DES", service_type="Passenger", status="RUNNING",
            origin="ATH", destination="KIA", next_station="SKA",
            speed=116.0, course=270.0, track_heading=270.0, lat=38.05, lng=23.72,
            altitude=50.0, delay=3, progress=0.4, chainage_m=10000, corridor="PROASTIAKOS",
            distance_to_destination_m=90000, timestamp="2026-11-26T05:00:00Z", schedule_id="s1",
        )
        u2 = TrainPosition(
            id="id-b", train_number="2232", locomotive_number="460108", name="2232",
            train_type="DES", service_type="Passenger", status="RUNNING",
            origin="ATH", destination="KIA", next_station="SKA",
            speed=115.0, course=270.0, track_heading=270.0, lat=38.0504, lng=23.7201,
            altitude=50.0, delay=3, progress=0.4, chainage_m=10050, corridor="PROASTIAKOS",
            distance_to_destination_m=89950, timestamp="2026-11-26T05:00:00Z", schedule_id="s1",
        )
        store.update_positions([u1, u2])

        fams = TrainTelemetryCollector(store).collect()
        fam_map = {f.name: f for f in fams}

        speed_labels = {dict(s.labels)["train_number"]: s.value for s in fam_map["railmon_train_speed_kmh"].samples}
        max_labels = {dict(s.labels)["train_number"]: s.value for s in fam_map["railmon_train_max_speed_kmh"].samples}
        self.assertEqual(speed_labels, {"2232/460103": 116.0, "2232/460108": 115.0})
        self.assertEqual(max_labels, {"2232/460103": 116.0, "2232/460108": 115.0})

        om_bytes = MetricsFormatter.format_openmetrics_100(fams)
        self.assertEqual(validate_openmetrics_100(om_bytes), [])

    def test_tc_omx_09_switchers_and_modem_handover_disambiguation(self) -> None:
        """TC-OMX-09: Unassigned switchers, same-loco modem handovers, and unique trains disambiguate cleanly."""
        store = TrainStateStore()
        sw1 = TrainPosition(
            id="id-c", train_number="Unknown", locomotive_number=None, name="Unknown",
            train_type="SHUNT", service_type="Freight", status="IDLE",
            origin="", destination="", next_station="", speed=0.0, course=0.0,
            track_heading=0.0, lat=37.98, lng=23.69, altitude=10.0, delay=0,
            progress=0.0, chainage_m=0, corridor="PATHE", distance_to_destination_m=0,
            timestamp="2026-11-26T05:00:00Z", schedule_id=None,
        )
        sw2 = TrainPosition(
            id="id-d", train_number="Unknown", locomotive_number=None, name="Unknown",
            train_type="SHUNT", service_type="Freight", status="IDLE",
            origin="", destination="", next_station="", speed=5.0, course=0.0,
            track_heading=0.0, lat=37.981, lng=23.691, altitude=10.0, delay=0,
            progress=0.0, chainage_m=0, corridor="PATHE", distance_to_destination_m=0,
            timestamp="2026-11-26T05:00:00Z", schedule_id=None,
        )
        ho_old = _make_train_position("sess-old", "IC52", speed=120.0, delay=2.0, corridor="PATHE")
        ho_new = _make_train_position("sess-new", "IC52", speed=122.0, delay=2.0, corridor="PATHE")
        unique_ic54 = _make_train_position("u-54", "IC54", speed=140.0, delay=1.0, corridor="PATHE")
        store.update_positions([sw1, sw2, ho_old, ho_new, unique_ic54])

        fams = TrainTelemetryCollector(store).collect()
        fam_speed = [f for f in fams if f.name == "railmon_train_speed_kmh"][0]
        tns = {dict(s.labels)["train_number"] for s in fam_speed.samples}
        self.assertEqual(
            tns,
            {"Unknown/id-c", "Unknown/id-d", "IC52/120-001#sess-old", "IC52/120-001#sess-new", "IC54"},
        )
        self.assertEqual(validate_openmetrics_100(MetricsFormatter.format_openmetrics_100(fams)), [])

    def test_tc_omx_10_max_speed_cross_type_collision_and_non_finite_isolation(self) -> None:
        """TC-OMX-10: Cross-type same train_number avoids fam_max_speed collision and non-finite delay/speed stays finite."""
        store = TrainStateStore()
        p1 = TrainPosition(
            id="t-1", train_number="50", locomotive_number="120-001", name="50",
            train_type="IC", service_type="Passenger", status="RUNNING",
            origin="ATH", destination="SKG", next_station="LAR", speed=150.0,
            course=0.0, track_heading=0.0, lat=38.5, lng=22.8, altitude=50.0,
            delay=4.0, progress=0.5, chainage_m=200000, corridor="PATHE",
            distance_to_destination_m=200000, timestamp="2026-11-26T05:00:00Z", schedule_id="s1",
        )
        p2 = TrainPosition(
            id="t-2", train_number="50", locomotive_number="460-002", name="50",
            train_type="PR", service_type="Passenger", status="RUNNING",
            origin="ATH", destination="SKG", next_station="LAR", speed=95.0,
            course=0.0, track_heading=0.0, lat=38.6, lng=22.8, altitude=50.0,
            delay=6.0, progress=0.5, chainage_m=210000, corridor="PATHE",
            distance_to_destination_m=190000, timestamp="2026-11-26T05:00:00Z", schedule_id="s2",
        )
        p_nan = TrainPosition(
            id="t-nan", train_number="99", locomotive_number=None, name="99",
            train_type="IC", service_type="Passenger", status="RUNNING",
            origin="ATH", destination="SKG", next_station="LAR", speed=float("nan"),
            course=0.0, track_heading=0.0, lat=38.7, lng=22.8, altitude=50.0,
            delay=float("nan"), progress=0.5, chainage_m=220000, corridor="PATHE",
            distance_to_destination_m=180000, timestamp="2026-11-26T05:00:00Z", schedule_id="s3",
        )
        p_inf = TrainPosition(
            id="t-inf", train_number="100", locomotive_number=None, name="100",
            train_type="IC", service_type="Passenger", status="RUNNING",
            origin="ATH", destination="SKG", next_station="LAR", speed=float("inf"),
            course=0.0, track_heading=0.0, lat=38.8, lng=22.8, altitude=50.0,
            delay=float("-inf"), progress=0.5, chainage_m=225000, corridor="PATHE",
            distance_to_destination_m=175000, timestamp="2026-11-26T05:00:00Z", schedule_id="s4",
        )
        store.update_positions([p1, p2, p_nan])
        with store._lock:
            store._trains["t-inf"] = TrainRecord(position=p_inf)

        fams = TrainTelemetryCollector(store).collect()
        fam_map = {f.name: f for f in fams}
        max_keys = [s.labels for s in fam_map["railmon_train_max_speed_kmh"].samples]
        self.assertEqual(len(max_keys), len(set(max_keys)))
        avg_delay = fam_map["railmon_corridor_average_delay_minutes"].samples[0].value
        self.assertTrue(math.isfinite(avg_delay))
        self.assertAlmostEqual(avg_delay, (4.0 + 6.0 + 0.0 + 0.0) / 4.0)
        self.assertEqual(validate_openmetrics_100(MetricsFormatter.format_openmetrics_100(fams)), [])

    def test_tc_omx_11_real_sqlite_storage_metrics_collector(self) -> None:
        """TC-OMX-11: StorageMetricsCollector queries real SQLite DB via repo.get_database_stats()."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "real_metrics.db")
            db = Database(db_path)
            try:
                with db.transaction() as cur:
                    cur.execute(
                        "INSERT INTO recording_sessions (started_at, mode) VALUES ('2026-11-26T05:00:00.000000Z', 'live')"
                    )
                    cur.execute(
                        "INSERT INTO recording_sessions (started_at, mode) VALUES ('2026-11-26T06:00:00.000000Z', 'live')"
                    )
                    cur.execute(
                        "INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at) "
                        "VALUES ('IC52', 'IC', '2026-11-26T05:00:00.000000Z', '2026-11-26T05:00:00.000000Z')"
                    )
                    for idx in range(5):
                        cur.execute(
                            "INSERT INTO telemetry_history (train_number, recorded_at, lat, lng, speed, course, delay_min, corridor) "
                            "VALUES ('IC52', ?, 38.0, 23.7, 120.0, 0.0, 2, 'PATHE')",
                            (f"2026-11-26T05:00:0{idx}.000000Z",),
                        )
            finally:
                db.close()

            collector = StorageMetricsCollector(db_path=db_path)
            fams = collector.collect()
            fam_map = {f.name: f for f in fams}

            self.assertEqual(fam_map["railmon_db_records"].samples[0].value, 5.0)
            self.assertEqual(fam_map["railmon_db_sessions"].samples[0].value, 2.0)
            file_sizes = {dict(s.labels)["file_type"]: s.value for s in fam_map["railmon_db_file_size_bytes"].samples}
            self.assertGreater(file_sizes["main"], 0.0)

            om_bytes = MetricsFormatter.format_openmetrics_100(fams)
            self.assertEqual(validate_openmetrics_100(om_bytes), [])
            om_text = om_bytes.decode("utf-8")
            self.assertIn('railmon_db_records_total{table="telemetry_history"} 5\n', om_text)
            self.assertIn('railmon_db_sessions_total{mode="sqlite"} 2\n', om_text)

    def test_tc_omx_12_storage_collector_active_wal_and_readonly_file(self) -> None:
        """TC-OMX-12: StorageMetricsCollector reads under active WAL writer and chmod read-only file with special path chars."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sub = os.path.join(tmpdir, "railmon metrics #1 100%")
            os.makedirs(sub)
            db_path = os.path.join(sub, "live #wal.db")
            writer = Database(db_path)
            try:
                with writer.transaction() as cur:
                    cur.execute(
                        "INSERT INTO recording_sessions (started_at, mode) VALUES ('2026-11-26T05:00:00.000000Z', 'live')"
                    )
                    cur.execute(
                        "INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at) "
                        "VALUES ('IC52', 'IC', '2026-11-26T05:00:00.000000Z', '2026-11-26T05:00:00.000000Z')"
                    )
                    cur.execute(
                        "INSERT INTO telemetry_history (train_number, recorded_at, lat, lng, speed, course, delay_min, corridor) "
                        "VALUES ('IC52', '2026-11-26T05:00:00.000000Z', 38.0, 23.7, 130.0, 0.0, 1, 'PATHE')"
                    )

                # Scrape while writer connection is still open with active WAL
                collector_live = StorageMetricsCollector(db_path=db_path)
                fams_live = {f.name: f for f in collector_live.collect()}
                self.assertEqual(fams_live["railmon_db_records"].samples[0].value, 1.0)
                self.assertEqual(fams_live["railmon_db_sessions"].samples[0].value, 1.0)
            finally:
                writer.checkpoint_wal("TRUNCATE")
                writer.close()

            # Mark database read-only and verify scrape succeeds without mutation
            os.chmod(db_path, stat.S_IREAD)
            try:
                collector_ro = StorageMetricsCollector(db_path=db_path)
                fams_ro = {f.name: f for f in collector_ro.collect()}
                self.assertEqual(fams_ro["railmon_db_records"].samples[0].value, 1.0)
                self.assertEqual(fams_ro["railmon_db_sessions"].samples[0].value, 1.0)
            finally:
                os.chmod(db_path, stat.S_IREAD | stat.S_IWRITE)

    def test_tc_omx_13_storage_collector_non_poisoning_cache_and_thread_safety(self) -> None:
        """TC-OMX-13: Transient fetch failures preserve last-known-good cached stats and concurrent scrapes are safe."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "cache_test.db")
            db = Database(db_path)
            try:
                with db.transaction() as cur:
                    cur.execute(
                        "INSERT INTO recording_sessions (started_at, mode) VALUES ('2026-11-26T05:00:00.000000Z', 'live')"
                    )
                    cur.execute(
                        "INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at) "
                        "VALUES ('IC52', 'IC', '2026-11-26T05:00:00.000000Z', '2026-11-26T05:00:00.000000Z')"
                    )
                    for idx in range(3):
                        cur.execute(
                            "INSERT INTO telemetry_history (train_number, recorded_at, lat, lng, speed, course, delay_min, corridor) "
                            "VALUES ('IC52', ?, 38.0, 23.7, 120.0, 0.0, 0, 'PATHE')",
                            (f"2026-11-26T05:00:0{idx}.000000Z",),
                        )
            finally:
                db.close()

            collector = StorageMetricsCollector(db_path=db_path, stats_cache_ttl_seconds=0.0)
            fams_ok = {f.name: f for f in collector.collect()}
            self.assertEqual(fams_ok["railmon_db_records"].samples[0].value, 3.0)

            # Simulate transient error on next TTL refresh -> prior cached stats MUST be preserved
            with patch.object(collector, "_fetch_db_stats", return_value=None):
                fams_err = {f.name: f for f in collector.collect()}
                self.assertEqual(fams_err["railmon_db_records"].samples[0].value, 3.0)
                self.assertEqual(fams_err["railmon_db_sessions"].samples[0].value, 1.0)

            # Concurrent scrapes across 8 threads
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
                futures = [pool.submit(collector.collect) for _ in range(16)]
                results = [fut.result(timeout=5.0) for fut in futures]
            self.assertEqual(len(results), 16)
            for fam_list in results:
                fm = {f.name: f for f in fam_list}
                self.assertEqual(fm["railmon_db_records"].samples[0].value, 3.0)

    def test_tc_omx_15_full_five_collector_kitchen_sink_openmetrics_and_prometheus_parity(self) -> None:
        """TC-OMX-15: Full 5-collector scrape passes strict OpenMetrics 1.0.0 validation and matches Prometheus sample names."""
        from train_tracker import GhostTrain

        store = TrainStateStore()
        store.set_connection_status(ConnectionStatus.CONNECTED, "Live")
        u1 = TrainPosition(
            id="id-a", train_number="2232", locomotive_number="460103", name="2232",
            train_type="DES", service_type="Passenger", status="RUNNING",
            origin="ATH", destination="KIA", next_station="SKA",
            speed=180.0, course=270.0, track_heading=270.0, lat=38.05, lng=23.72,
            altitude=50.0, delay=3, progress=0.4, chainage_m=10000, corridor="PROASTIAKOS",
            distance_to_destination_m=90000, timestamp="2026-11-26T05:00:00Z", schedule_id="s1",
        )
        u2 = TrainPosition(
            id="id-b", train_number="2232", locomotive_number="460108", name="2232",
            train_type="DES", service_type="Passenger", status="RUNNING",
            origin="ATH", destination="KIA", next_station="SKA",
            speed=115.0, course=270.0, track_heading=270.0, lat=38.0504, lng=23.7201,
            altitude=50.0, delay=20, progress=0.4, chainage_m=10050, corridor="PROASTIAKOS",
            distance_to_destination_m=89950, timestamp="2026-11-26T05:00:00Z", schedule_id="s1",
        )
        sw1 = TrainPosition(
            id="id-c", train_number="Unknown", locomotive_number=None, name="Unknown",
            train_type="SHUNT", service_type="Freight", status="IDLE",
            origin="", destination="", next_station="", speed=0.0, course=0.0,
            track_heading=0.0, lat=37.98, lng=23.69, altitude=10.0, delay=0,
            progress=0.0, chainage_m=0, corridor="PATHE", distance_to_destination_m=0,
            timestamp="2026-11-26T05:00:00Z", schedule_id=None,
        )
        sw2 = TrainPosition(
            id="id-d", train_number="Unknown", locomotive_number=None, name="Unknown",
            train_type="SHUNT", service_type="Freight", status="IDLE",
            origin="", destination="", next_station="", speed=5.0, course=0.0,
            track_heading=0.0, lat=37.981, lng=23.691, altitude=10.0, delay=0,
            progress=0.0, chainage_m=0, corridor="PATHE", distance_to_destination_m=0,
            timestamp="2026-11-26T05:00:00Z", schedule_id=None,
        )
        store.update_positions([u1, u2, sw1, sw2])
        store.update_ghost_trains([
            GhostTrain(
                id="schedule:g-1", train_number="IC56", schedule_id="g-1",
                schedule_number="IC56-20261126", name="IC56",
                service_type="InterCity", category="InterCity",
                origin="Athens", destination="Thessaloniki", corridor="PATHE",
                scheduled_departure="2026-11-26T05:00:00.000Z",
                scheduled_arrival="2026-11-26T09:15:00.000Z",
                delay=0, progress=0.1, timestamp="2026-11-26T05:10:00.000Z",
            )
        ])
        store.record_stream_event("trainPositionsUx")
        store.record_stream_event("scheduleCards")

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "kitchen_sink.db")
            db = Database(db_path)
            try:
                with db.transaction() as cur:
                    cur.execute(
                        "INSERT INTO recording_sessions (started_at, mode) VALUES ('2026-11-26T05:00:00.000000Z', 'live')"
                    )
                    cur.execute(
                        "INSERT INTO trains (train_number, train_type, first_observed_at, last_observed_at) "
                        "VALUES ('IC52', 'IC', '2026-11-26T05:00:00.000000Z', '2026-11-26T05:00:00.000000Z')"
                    )
                    cur.execute(
                        "INSERT INTO telemetry_history (train_number, recorded_at, lat, lng, speed, course, delay_min, corridor) "
                        "VALUES ('IC52', '2026-11-26T05:00:00.000000Z', 38.0, 23.7, 120.0, 0.0, 2, 'PATHE')"
                    )
            finally:
                db.close()

            reg = MetricRegistry()
            reg.register(TrainTelemetryCollector(store))
            reg.register(WatchdogMetricsCollector(store))
            reg.register(StreamMetricsCollector(store))
            reg.register(StorageMetricsCollector(db_path=db_path))
            proc = ProcessMetricsCollector(version="2.0.0")
            proc.record_scrape(0.004, "/metrics", 200)
            reg.register(proc)

            # Verify uptime remains non-negative even if wall clock jumps backward
            with patch("exporter.collector.time.time", return_value=0.0):
                families = reg.collect_all()

            om_bytes = MetricsFormatter.format_openmetrics_100(families)
            prom_bytes = MetricsFormatter.format_prometheus_004(families)
            self.assertEqual(validate_openmetrics_100(om_bytes), [])

            # Extract sample lines (ignoring comments/EOF) and verify exact sample-name parity
            om_sample_names = {
                line.split("{", 1)[0].split(" ", 1)[0]
                for line in om_bytes.decode("utf-8").splitlines()
                if line and not line.startswith("#")
            }
            prom_sample_names = {
                line.split("{", 1)[0].split(" ", 1)[0]
                for line in prom_bytes.decode("utf-8").splitlines()
                if line and not line.startswith("#")
            }
            self.assertEqual(om_sample_names, prom_sample_names)
            self.assertIn("railmon_db_records_total", om_sample_names)
            self.assertIn("railmon_db_sessions_total", om_sample_names)
            self.assertIn("railmon_trains_delayed", om_sample_names)
            self.assertNotIn("railmon_trains_delayed_total", om_sample_names)

    def test_tc_omx_18_empty_initialized_sqlite_db_reports_zero_counts(self) -> None:
        """TC-OMX-18: Valid empty initialized SQLite DB exports 0 rows and 0 sessions cleanly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "empty_schema.db")
            db = Database(db_path)
            db.close()

            collector = StorageMetricsCollector(db_path=db_path)
            fams = collector.collect()
            fam_map = {f.name: f for f in fams}
            self.assertEqual(fam_map["railmon_db_records"].samples[0].value, 0.0)
            self.assertEqual(fam_map["railmon_db_sessions"].samples[0].value, 0.0)
            self.assertEqual(validate_openmetrics_100(MetricsFormatter.format_openmetrics_100(fams)), [])


if __name__ == "__main__":
    unittest.main()
