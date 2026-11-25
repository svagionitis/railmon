"""
Asynchronous background telemetry recorder and batch persistence daemon.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
import os
import queue
import shutil
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple, Union

from storage.db import Database
from storage.filter import DeadbandBatchEvaluation, DeadbandConfig, DeadbandFilter
from storage.models import (
    TelemetryHistoryRow,
    canonicalize_iso_utc,
    format_utc_iso,
    iso_utc_now,
)
from train_tracker import TrainPosition, __version__

logger = logging.getLogger("railmon." + __name__)

MIN_FREE_DISK_BYTES: int = 100 * 1024 * 1024  # 100 MB disk-full circuit breaker threshold


def _is_transient_sqlite_error(exc: BaseException) -> bool:
    """Return True if exc is a transient SQLITE_BUSY / SQLITE_LOCKED operational error."""
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


@dataclass(frozen=True)
class RecorderConfig:
    """Configuration parameters for asynchronous telemetry recorder."""

    batch_size: int = 50                     # Buffer threshold to trigger batch commit
    flush_interval_sec: float = 2.0          # Max latency (seconds) before flushing buffer
    queue_maxsize: int = 10000               # Bounded buffer to avoid memory bloat
    enable_deadband: bool = True             # Enable coordinate deadband filtering
    deadband_config: Optional[DeadbandConfig] = None
    client_version: str = __version__
    mode: str = "live"                       # 'live' or 'demo'
    retention_days: Optional[int] = None     # Rolling retention window in days (archives older telemetry)
    archive_path: Optional[str] = None       # Target SQLite archive file path (defaults to <db_stem>_archive.db)
    min_free_disk_bytes: int = MIN_FREE_DISK_BYTES
    retention_archive_interval_sec: float = 3600.0
    archive_chunk_size: int = 5000           # Bounded telemetry rows archived per SQLite chunk
    archive_retry_base_sec: float = 60.0     # Initial backoff delay (seconds) after archival error
    archive_retry_max_sec: float = 3600.0    # Maximum capped backoff delay (seconds) on repeated errors
    archive_background: bool = True          # Run periodic archival on non-overlapping background thread
    write_retry_max_attempts: int = 3        # In-flush bounded retry attempts on transient SQLITE_BUSY
    max_pending_buffer_size: int = 5000      # Upper bound for retained buffer items across transient lock outages


class TelemetryRecorder:
    """
    Background daemon thread managing batched, non-blocking persistence
    of Hellenic Railways telemetry snapshots into SQLite.
    """

    def __init__(
        self,
        db: Database,
        config: Optional[RecorderConfig] = None,
    ) -> None:
        self.db = db
        self.config = config or RecorderConfig()
        if self.config.retention_days is not None and self.config.retention_days <= 0:
            raise ValueError("retention_days must be a positive integer")
        if self.config.archive_chunk_size <= 0:
            raise ValueError("archive_chunk_size must be a positive integer")
        if self.config.archive_retry_base_sec <= 0.0:
            raise ValueError("archive_retry_base_sec must be positive")
        if self.config.archive_retry_max_sec < self.config.archive_retry_base_sec:
            raise ValueError("archive_retry_max_sec must be >= archive_retry_base_sec")
        if self.config.write_retry_max_attempts < 1:
            raise ValueError("write_retry_max_attempts must be >= 1")
        if self.config.max_pending_buffer_size < 1:
            raise ValueError("max_pending_buffer_size must be >= 1")

        self.filter = (
            DeadbandFilter(self.config.deadband_config)
            if self.config.enable_deadband
            else None
        )

        self._queue: queue.Queue[Optional[Union[TrainPosition, List[TrainPosition]]]] = (
            queue.Queue(maxsize=self.config.queue_maxsize)
        )
        self._thread: Optional[threading.Thread] = None
        self._archive_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()

        self._session_id: Optional[int] = None
        self._session_started_at: str = ""
        self._total_records_written: int = 0
        self._total_batches_flushed: int = 0
        self._total_records_archived: int = 0
        self._total_flush_failures: int = 0
        self._total_archive_failures: int = 0
        self._archive_consecutive_failures: int = 0
        self._archive_in_progress: bool = False
        self._last_flush_time: float = 0.0
        self._last_flush_mono: float = 0.0
        self._last_archive_time: float = 0.0
        self._next_archive_mono: float = 0.0
        self._last_filter_prune_time: float = 0.0
        self._last_filter_prune_mono: float = 0.0
        self._disk_full_tripped: bool = False

        self._lock = threading.Lock()
        self._archive_lock = threading.Lock()

    @property
    def is_running(self) -> bool:
        """Check if background worker thread is active."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def session_id(self) -> Optional[int]:
        """Return the active audit recording session ID."""
        return self._session_id

    @property
    def disk_full_tripped(self) -> bool:
        """Return True if the disk-full circuit breaker has tripped (< 100 MB free)."""
        return self._disk_full_tripped

    def _has_sufficient_disk_space(self) -> bool:
        """Probe available disk capacity via shutil.disk_usage; trip circuit breaker if < min_free_disk_bytes."""
        if self._disk_full_tripped:
            return False
        if self.db._is_memory or self.config.min_free_disk_bytes <= 0:
            return True

        dirs_to_check = [os.path.dirname(os.path.abspath(self.db.db_path)) or "."]
        if self.config.archive_path and not self.config.archive_path.startswith("file::memory:") and self.config.archive_path != ":memory:":
            arch_dir = os.path.dirname(os.path.abspath(self.config.archive_path)) or "."
            if arch_dir not in dirs_to_check:
                dirs_to_check.append(arch_dir)

        for target_dir in dirs_to_check:
            check_dir = target_dir if os.path.exists(target_dir) else "."
            try:
                usage = shutil.disk_usage(check_dir)
            except OSError as exc:
                logger.warning("Unable to query disk usage for '%s': %s", check_dir, exc)
                continue

            if usage.free < self.config.min_free_disk_bytes:
                self._disk_full_tripped = True
                self._stop_event.set()
                logger.critical(
                    "Disk space critically low on '%s' (%d bytes free < %d bytes required). "
                    "Halting TelemetryRecorder to prevent database corruption.",
                    check_dir,
                    usage.free,
                    self.config.min_free_disk_bytes,
                )
                while not self._queue.empty():
                    try:
                        self._queue.get_nowait()
                        self._queue.task_done()
                    except queue.Empty:
                        break
                return False

        return True

    def _run_retention_archive(self) -> int:
        """Archive historical telemetry older than retention_days with capped exponential backoff."""
        if self.config.retention_days is None or self._disk_full_tripped:
            with self._archive_lock:
                self._archive_in_progress = False
            return 0
        if not self._has_sufficient_disk_space():
            with self._archive_lock:
                self._archive_in_progress = False
            return 0

        from storage.repository import HistoricalRepository

        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=self.config.retention_days)
        cutoff_iso = format_utc_iso(cutoff_dt)
        next_delay: float = self.config.archive_retry_base_sec

        try:
            repo = HistoricalRepository(self.db)
            target_archive = repo.resolve_archive_path(self.config.archive_path)
            archived = repo.archive_before(
                cutoff_iso,
                archive_path=target_archive,
                chunk_size=self.config.archive_chunk_size,
                stop_event=self._stop_event,
            )
            with self._archive_lock:
                self._total_records_archived += archived
                self._archive_consecutive_failures = 0
                self._last_archive_time = time.time()
            next_delay = self.config.retention_archive_interval_sec
            if archived > 0:
                logger.info(
                    "Retention policy (--retention-days=%d) archived %d telemetry rows older than %s to '%s'.",
                    self.config.retention_days,
                    archived,
                    cutoff_iso,
                    target_archive,
                )
            return archived
        except (sqlite3.Error, OSError, ValueError, PermissionError) as exc:
            with self._archive_lock:
                self._archive_consecutive_failures += 1
                self._total_archive_failures += 1
                fail_count = self._archive_consecutive_failures
            exp = min(fail_count - 1, 10)
            next_delay = min(
                self.config.archive_retry_max_sec,
                self.config.archive_retry_base_sec * float(2 ** exp),
            )
            logger.error(
                "Retention archival failed before %s (consecutive failures: %d; retrying in %.1fs): %s",
                cutoff_iso,
                fail_count,
                next_delay,
                exc,
            )
            return 0
        finally:
            with self._archive_lock:
                self._next_archive_mono = time.monotonic() + max(0.0, float(next_delay))
                self._archive_in_progress = False

    def _trigger_background_archive(self) -> bool:
        """Spawn a single non-overlapping background archival worker if none is running."""
        with self._archive_lock:
            if self._archive_in_progress:
                return False
            if self._archive_thread is not None and self._archive_thread.is_alive():
                return False
            self._archive_in_progress = True
            worker = threading.Thread(
                target=self._run_retention_archive,
                name="TelemetryArchiveThread",
                daemon=True,
            )
            self._archive_thread = worker
        worker.start()
        return True

    def start(self) -> None:
        """Start the background recorder daemon and initialize audit session."""
        with self._lock:
            if self.is_running:
                return

            now_wall = time.time()
            now_mono = time.monotonic()
            self._stop_event.clear()
            self._disk_full_tripped = False
            self._session_started_at = iso_utc_now()
            self._total_records_written = 0
            self._total_batches_flushed = 0
            self._total_records_archived = 0
            self._total_flush_failures = 0
            self._total_archive_failures = 0
            self._archive_consecutive_failures = 0
            self._archive_in_progress = False
            self._last_flush_time = now_wall
            self._last_flush_mono = now_mono
            self._last_archive_time = now_wall
            self._next_archive_mono = now_mono + self.config.retention_archive_interval_sec
            self._last_filter_prune_time = now_wall
            self._last_filter_prune_mono = now_mono

            if not self._has_sufficient_disk_space():
                return

            if self.config.retention_days is not None:
                self._run_retention_archive()

            # Create session in recording_sessions table
            with self.db.transaction() as cur:
                cur.execute(
                    """
                    INSERT INTO recording_sessions (started_at, mode, total_records_written, client_version)
                    VALUES (?, ?, 0, ?)
                    """,
                    (self._session_started_at, self.config.mode, self.config.client_version),
                )
                self._session_id = cur.lastrowid

            self._thread = threading.Thread(
                target=self._worker_loop,
                name="TelemetryRecorderThread",
                daemon=True,
            )
            self._thread.start()
            logger.info("TelemetryRecorder started. Session ID: %s", self._session_id)

    def stop(self, timeout_sec: float = 5.0) -> None:
        """
        Signal recorder to stop, drain queued items, join background archival,
        flush buffers, and finalize the recording session.
        """
        with self._lock:
            if not self.is_running and not self._disk_full_tripped:
                return

            self._stop_event.set()
            # Push sentinel to awaken queue worker if waiting
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass

        if self._thread:
            self._thread.join(timeout=timeout_sec)

        with self._archive_lock:
            arch_thread = self._archive_thread
        if arch_thread is not None and arch_thread.is_alive():
            arch_thread.join(timeout=timeout_sec)

        # Finalize session audit and checkpoint WAL (skip disk writes if disk-full circuit tripped)
        with self._lock:
            if not self._disk_full_tripped:
                ended_at = iso_utc_now()
                if self._session_id is not None:
                    try:
                        with self.db.transaction() as cur:
                            cur.execute(
                                """
                                UPDATE recording_sessions
                                SET ended_at = ?, total_records_written = ?
                                WHERE session_id = ?
                                """,
                                (ended_at, self._total_records_written, self._session_id),
                            )
                    except (sqlite3.Error, OSError, ValueError) as exc:
                        logger.error(
                            "Failed to finalize recording session %s: %s",
                            self._session_id,
                            exc,
                        )

                try:
                    self.db.checkpoint_wal("TRUNCATE")
                except (sqlite3.Error, OSError, ValueError) as exc:
                    logger.warning("WAL checkpoint on recorder stop encountered an error: %s", exc)

            logger.info(
                "TelemetryRecorder stopped. Session %s ended. Total rows written: %d",
                self._session_id,
                self._total_records_written,
            )

    def enqueue(self, position: TrainPosition) -> bool:
        """
        Enqueue a single train position. Non-blocking; returns False if queue is saturated or halted.
        """
        if not self.is_running or self._disk_full_tripped or self._stop_event.is_set():
            return False
        try:
            self._queue.put_nowait(position)
            return True
        except queue.Full:
            logger.warning(
                "TelemetryRecorder queue saturated. Dropping position for %s",
                position.train_number,
            )
            return False

    def enqueue_batch(self, positions: List[TrainPosition]) -> int:
        """
        Enqueue a batch of positions. Non-blocking; returns count of successfully enqueued items.
        """
        if not self.is_running or self._disk_full_tripped or self._stop_event.is_set() or not positions:
            return 0
        try:
            self._queue.put_nowait(positions)
            return len(positions)
        except queue.Full:
            logger.warning(
                "TelemetryRecorder queue saturated. Dropping batch of %d positions",
                len(positions),
            )
            return 0

    def _worker_loop(self) -> None:
        """Main worker execution loop maintaining buffer batching, retry retention, and timeout flush."""
        buffer: List[TrainPosition] = []
        self._last_flush_time = time.time()
        self._last_flush_mono = time.monotonic()

        while not self._stop_event.is_set():
            try:
                item = self._queue.get(timeout=0.1)
                if item is None:
                    # Sentinel received
                    break

                if isinstance(item, list):
                    buffer.extend(item)
                else:
                    buffer.append(item)

                self._queue.task_done()
            except queue.Empty:
                pass

            now_mono = time.monotonic()
            reached_size = len(buffer) >= self.config.batch_size
            reached_time = (now_mono - self._last_flush_mono) >= self.config.flush_interval_sec

            if buffer and (reached_size or reached_time):
                _, persisted, retryable = self._flush_buffer_with_status(buffer)
                if persisted or not retryable:
                    buffer.clear()
                elif len(buffer) > self.config.max_pending_buffer_size:
                    dropped = len(buffer) - self.config.max_pending_buffer_size
                    del buffer[:dropped]
                    logger.warning(
                        "Pending telemetry retry buffer exceeded %d items; dropped %d oldest items.",
                        self.config.max_pending_buffer_size,
                        dropped,
                    )
                self._last_flush_time = time.time()
                self._last_flush_mono = time.monotonic()

            with self._archive_lock:
                next_arch_due = now_mono >= self._next_archive_mono
            if (
                not self._disk_full_tripped
                and self.config.retention_days is not None
                and next_arch_due
            ):
                if self.config.archive_background:
                    self._trigger_background_archive()
                else:
                    self._run_retention_archive()

            if self.filter is not None:
                prune_interval = self.filter.config.stale_prune_interval_sec
                if (now_mono - self._last_filter_prune_mono) >= prune_interval:
                    evicted = self.filter.prune_stale_states()
                    if evicted > 0:
                        logger.debug(
                            "TelemetryRecorder evicted %d stale train states from DeadbandFilter.",
                            evicted,
                        )
                    self._last_filter_prune_time = time.time()
                    self._last_filter_prune_mono = now_mono

        # Draining phase on shutdown (skip if disk-full circuit breaker tripped)
        if not self._disk_full_tripped:
            while not self._queue.empty():
                try:
                    item = self._queue.get_nowait()
                    if item is not None:
                        if isinstance(item, list):
                            buffer.extend(item)
                        else:
                            buffer.append(item)
                    self._queue.task_done()
                except queue.Empty:
                    break

            if buffer:
                self._flush_buffer_with_status(buffer)
                buffer.clear()
        else:
            buffer.clear()

    @staticmethod
    def _build_flush_rows(
        admitted: List[TrainPosition],
    ) -> Tuple[List[Tuple[Any, ...]], List[Tuple[Any, ...]], List[Tuple[Any, ...]]]:
        """Build canonicalized parameter tuples for trains, scheduled_trips, and telemetry_history."""
        distinct_trains: Dict[str, TrainPosition] = {}
        distinct_trips: Dict[str, TrainPosition] = {}
        for p in admitted:
            distinct_trains[p.train_number] = p
            if p.schedule_id:
                distinct_trips[p.schedule_id] = p

        now_iso = iso_utc_now()
        train_rows: List[Tuple[Any, ...]] = []
        for p in distinct_trains.values():
            obs_ts = canonicalize_iso_utc(p.timestamp, fallback=now_iso) if p.timestamp else now_iso
            train_rows.append(
                (
                    p.train_number,
                    p.locomotive_number,
                    p.name,
                    p.train_type,
                    p.service_type,
                    obs_ts,
                    obs_ts,
                )
            )

        trip_rows: List[Tuple[Any, ...]] = []
        for p in distinct_trips.values():
            ts_canon = canonicalize_iso_utc(p.timestamp, fallback=now_iso) if p.timestamp else now_iso
            trip_rows.append(
                (
                    p.schedule_id,
                    p.train_number,
                    p.origin,
                    p.destination,
                    p.corridor,
                    ts_canon[:10],
                )
            )

        history_rows = [
            TelemetryHistoryRow.from_train_position(p).to_db_row()
            for p in admitted
        ]
        return train_rows, trip_rows, history_rows

    def _commit_flush_transaction(
        self,
        train_rows: List[Tuple[Any, ...]],
        trip_rows: List[Tuple[Any, ...]],
        history_rows: List[Tuple[Any, ...]],
    ) -> None:
        """Persist trains, scheduled_trips, telemetry_history, and session count atomically."""
        with self.db.transaction() as cur:
            # 1. Upsert trains table
            cur.executemany(
                """
                INSERT INTO trains (
                    train_number, locomotive_number, default_name, train_type, service_type,
                    first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(train_number) DO UPDATE SET
                    locomotive_number = COALESCE(excluded.locomotive_number, trains.locomotive_number),
                    default_name = COALESCE(excluded.default_name, trains.default_name),
                    train_type = COALESCE(excluded.train_type, trains.train_type),
                    service_type = COALESCE(excluded.service_type, trains.service_type),
                    last_observed_at = excluded.last_observed_at
                """,
                train_rows,
            )

            # 2. Upsert referenced scheduled_trips table
            if trip_rows:
                cur.executemany(
                    """
                    INSERT INTO scheduled_trips (
                        schedule_id, train_number, origin, destination, corridor, service_date
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(schedule_id) DO UPDATE SET
                        train_number = excluded.train_number,
                        origin = COALESCE(scheduled_trips.origin, excluded.origin),
                        destination = COALESCE(scheduled_trips.destination, excluded.destination),
                        corridor = COALESCE(scheduled_trips.corridor, excluded.corridor)
                    """,
                    trip_rows,
                )

            # 3. Bulk insert telemetry_history
            cur.executemany(
                """
                INSERT INTO telemetry_history (
                    train_number, schedule_id, recorded_at, lat, lng, speed, course,
                    track_heading, altitude, delay_min, status, progress, chainage_m,
                    dist_to_dest_m, next_station, corridor
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                history_rows,
            )

            # 4. Update session records written
            if self._session_id is not None:
                cur.execute(
                    """
                    UPDATE recording_sessions
                    SET total_records_written = total_records_written + ?
                    WHERE session_id = ?
                    """,
                    (len(history_rows), self._session_id),
                )

    def _flush_buffer_with_status(
        self,
        items: List[TrainPosition],
    ) -> Tuple[int, bool, bool]:
        """
        Purely evaluate deadband, persist batch with transient lock retry, and promote
        deadband state strictly after SQLite COMMIT succeeds.

        Returns:
            Tuple of (rows_written, persisted, retryable).
        """
        if not items or self._disk_full_tripped:
            return 0, False, False

        evaluation: Optional[DeadbandBatchEvaluation] = None
        if self.filter is not None:
            evaluation = self.filter.evaluate_batch(items)
            admitted = list(evaluation.admitted)
            if not admitted:
                self.filter.commit_batch(evaluation)
                return 0, True, False
        else:
            admitted = items

        if not self._has_sufficient_disk_space():
            return 0, False, False

        train_rows, trip_rows, history_rows = self._build_flush_rows(admitted)
        max_attempts = self.config.write_retry_max_attempts

        for attempt in range(1, max_attempts + 1):
            try:
                self._commit_flush_transaction(train_rows, trip_rows, history_rows)
                if evaluation is not None and self.filter is not None:
                    self.filter.commit_batch(evaluation)
                self._total_records_written += len(history_rows)
                self._total_batches_flushed += 1
                return len(history_rows), True, False
            except sqlite3.OperationalError as exc:
                if _is_transient_sqlite_error(exc):
                    if attempt < max_attempts:
                        backoff = min(0.5, 0.05 * float(2 ** (attempt - 1)))
                        logger.warning(
                            "Transient SQLite lock flushing %d rows (attempt %d/%d; retrying in %.2fs): %s",
                            len(history_rows),
                            attempt,
                            max_attempts,
                            backoff,
                            exc,
                        )
                        if not self._stop_event.is_set():
                            self._stop_event.wait(timeout=backoff)
                        else:
                            time.sleep(min(0.05, backoff))
                        continue
                    self._total_flush_failures += 1
                    logger.error(
                        "Failed to flush telemetry batch after %d transient lock attempts: %s",
                        max_attempts,
                        exc,
                    )
                    return 0, False, True
                self._total_flush_failures += 1
                logger.error("Non-transient SQLite OperationalError flushing batch: %s", exc, exc_info=True)
                return 0, False, False
            except (sqlite3.Error, ValueError, TypeError, OSError) as exc:
                self._total_flush_failures += 1
                logger.error("Failed to flush telemetry batch to SQLite: %s", exc, exc_info=True)
                return 0, False, False

        return 0, False, True

    def _flush_buffer(self, items: List[TrainPosition]) -> int:
        """Filter items, write to SQLite atomically, and promote deadband state post-commit."""
        written, _, _ = self._flush_buffer_with_status(items)
        return written

    def get_stats(self) -> Dict[str, Any]:
        """Return real-time metrics for queue depth, records written, failures, and filter ratios."""
        filter_stats = self.filter.stats if self.filter else {}
        with self._archive_lock:
            archived_total = self._total_records_archived
            archive_failures = self._total_archive_failures
            archive_consec_failures = self._archive_consecutive_failures
        return {
            "is_running": self.is_running,
            "session_id": self._session_id,
            "queue_size": self._queue.qsize(),
            "total_records_written": self._total_records_written,
            "total_batches_flushed": self._total_batches_flushed,
            "total_records_archived": archived_total,
            "total_flush_failures": self._total_flush_failures,
            "total_archive_failures": archive_failures,
            "archive_consecutive_failures": archive_consec_failures,
            "disk_full_tripped": self._disk_full_tripped,
            "filter": filter_stats,
        }

    def __enter__(self) -> "TelemetryRecorder":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.stop()
