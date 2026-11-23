"""
Background worker thread managing telemetry stream ingestion and reconnection.
Supports live SSE network streams and offline mock generator sources.
"""

from collections.abc import Iterable
import logging
import random
import threading
import time
from typing import Any, Generator, Iterable, List, Optional, Union
import urllib.error

from storage.db import Database
from storage.models import ReplayBounds
from storage.repository import HistoricalRepository, _epoch_to_iso_utc
from train_tracker import RailwayGovClient, TrainPosition, parse_retry_after
from tui.mock_engine import MockTelemetryEngine
from tui.state import ConnectionStatus, REPLAY_SPEED_MULTIPLIERS, TrainStateStore

logger = logging.getLogger("railmon." + __name__)


class StreamWorker:
    """
    Background worker thread managing telemetry stream ingestion and reconnection.
    Decouples network/simulation I/O from the synchronous UI rendering loop.
    """

    def __init__(
        self,
        store: TrainStateStore,
        client: Optional[RailwayGovClient] = None,
        mock_engine: Optional[MockTelemetryEngine] = None,
        custom_stream: Optional[
            Union[Generator[List[TrainPosition], None, None], Iterable[List[TrainPosition]]]
        ] = None,
        recorder: Optional[Any] = None,
        base_backoff_sec: float = 3.0,
        max_backoff_sec: float = 60.0,
        prune_interval_sec: float = 5.0,
    ) -> None:
        self.store = store
        self.client = client
        self.mock_engine = mock_engine
        self.custom_stream = custom_stream
        self.recorder = recorder

        self.base_backoff_sec = base_backoff_sec
        self.max_backoff_sec = max_backoff_sec
        self.prune_interval_sec = prune_interval_sec

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Unpaused by default
        self._disconnect_time: Optional[float] = None
        self._consecutive_failures: int = 0
        self._last_prune_time: float = 0.0

    @property
    def is_running(self) -> bool:
        """Check if worker thread is currently active."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        """Check if stream ingestion is currently paused."""
        return not self._pause_event.is_set()

    def start(self) -> None:
        """Start background daemon worker thread."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._pause_event.set()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="StreamWorkerThread",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_sec: float = 2.0) -> None:
        """Signal thread to stop and wait for clean termination."""
        self._stop_event.set()
        self._pause_event.set()  # Unblock if paused
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_sec)
        self.store.set_connection_status(ConnectionStatus.DISCONNECTED, "Stream stopped")

    def pause(self) -> None:
        """Temporarily pause stream ingestion."""
        self._pause_event.clear()
        self.store.set_connection_status(ConnectionStatus.PAUSED, "Paused by user")

    def resume(self) -> None:
        """Resume stream ingestion from paused state."""
        self._pause_event.set()
        self.store.set_connection_status(ConnectionStatus.CONNECTED, "Live stream active")

    def _check_prune(self) -> int:
        """Periodically prune stale trains from the state store."""
        now = time.monotonic()
        if (now - self._last_prune_time) >= self.prune_interval_sec:
            self._last_prune_time = now
            pruned = self.store.prune_stale_trains()
            if pruned > 0:
                logger.debug(
                    "StreamWorker pruned %d stale train(s) from TrainStateStore.",
                    pruned,
                )
            return pruned
        return 0

    def _run_loop(self) -> None:
        """Main worker execution loop with exponential backoff and error recovery."""
        backoff = self.base_backoff_sec

        while not self._stop_event.is_set():
            # Honor pause signal
            self._pause_event.wait()
            if self._stop_event.is_set():
                break

            self.store.set_connection_status(ConnectionStatus.CONNECTING, "Connecting to stream...")

            try:
                stream = self._get_stream_generator()

                for batch in stream:
                    if self._stop_event.is_set():
                        break

                    # Wait if paused mid-stream
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        break

                    if self.store.get_snapshot().connection_status != ConnectionStatus.CONNECTED:
                        self.store.set_connection_status(ConnectionStatus.CONNECTED, "Live stream active")
                    backoff = self.base_backoff_sec  # Reset backoff upon successful data frame

                    if self._consecutive_failures > 0 and self._disconnect_time is not None:
                        outage_dur = time.monotonic() - self._disconnect_time
                        logger.info("StreamWorker connection re-established after %.1fs outage.", outage_dur)
                        self._consecutive_failures = 0
                        self._disconnect_time = None

                    if batch:
                        self.store.update_positions(batch)
                        if self.recorder is not None and getattr(self.recorder, "is_running", False):
                            self.recorder.enqueue_batch(batch)

                    self._check_prune()

                # If generator completed normally without error
                self._check_prune()
                if self._stop_event.is_set():
                    break

            except Exception as exc:
                self._check_prune()
                if self._stop_event.is_set():
                    break

                # If paused while an in-flight socket read failed, preserve PAUSED state until resumed
                if not self._pause_event.is_set():
                    self._pause_event.wait()
                    if self._stop_event.is_set():
                        break

                # Apply rate limit vs exponential backoff with jitter
                if isinstance(exc, urllib.error.HTTPError) and exc.code == 429:
                    retry_header = exc.headers.get("Retry-After") if exc.headers else None
                    sleep_time = parse_retry_after(retry_header, default_delay=30.0, min_delay=30.0)
                    msg = f"HTTP 429 Rate Limited. Backing off for {sleep_time:.1f}s (Retry-After)..."
                elif isinstance(exc, urllib.error.HTTPError) and 500 <= exc.code < 600:
                    jitter = random.uniform(0.8, 1.2)
                    sleep_time = min(backoff * jitter, self.max_backoff_sec)
                    msg = f"HTTP {exc.code} Server Error ({exc.reason}). Reconnecting in {sleep_time:.1f}s..."
                    backoff = min(backoff * 2.0, self.max_backoff_sec)
                else:
                    jitter = random.uniform(0.8, 1.2)
                    sleep_time = min(backoff * jitter, self.max_backoff_sec)
                    msg = f"Connection error: {exc}. Reconnecting in {sleep_time:.1f}s..."
                    backoff = min(backoff * 2.0, self.max_backoff_sec)

                if self._consecutive_failures == 0:
                    self._disconnect_time = time.monotonic()
                    logger.warning(msg)
                else:
                    logger.debug("StreamWorker reconnect attempt %d: %s", self._consecutive_failures + 1, msg)
                self._consecutive_failures += 1

                self.store.set_connection_status(ConnectionStatus.RECONNECTING, msg)

                # Interruptible sleep
                if self._stop_event.wait(timeout=sleep_time):
                    break

        self.store.set_connection_status(ConnectionStatus.DISCONNECTED, "Stream disconnected")

    def _get_stream_generator(self) -> Iterable[List[TrainPosition]]:
        """Instantiate appropriate telemetry stream source."""
        if callable(self.custom_stream):
            return self.custom_stream()
        elif self.custom_stream is not None:
            return self.custom_stream
        elif self.mock_engine is not None:
            return self.mock_engine.stream_positions(
                interval_sec=1.0,
                stop_event=self._stop_event,
            )
        elif self.client is not None:
            return self.client.stream_train_positions(
                reconnect_delay=0.0,
                stop_event=self._stop_event,
            )
        else:
            raise ValueError("No valid telemetry source provided to StreamWorker")


def _snap_replay_speed(multiplier: float) -> float:
    """Snap an arbitrary speed multiplier to the nearest supported REPLAY_SPEED_MULTIPLIERS entry."""
    val = max(1.0, float(multiplier))
    return min(REPLAY_SPEED_MULTIPLIERS, key=lambda m: abs(m - val))


class ReplayWorker:
    """
    Background SQLite historical DVR playback worker (F-4.2).

    Streams recorded telemetry from SQLite through TrainStateStore using a controllable
    virtual clock supporting pause/resume, variable speed multipliers (1x, 5x, 15x, 60x),
    and timeline scrubbing (-/+ 5 minutes via '[' and ']').
    """

    def __init__(
        self,
        store: TrainStateStore,
        db_path: str,
        speed_multiplier: float = 1.0,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
        lookback_sec: float = 900.0,
        tick_interval_sec: float = 0.1,
    ) -> None:
        self.store = store
        self.db_path = db_path
        self.start_time_filter = start_time
        self.end_time_filter = end_time
        self.lookback_sec = max(60.0, float(lookback_sec))
        self.tick_interval_sec = max(0.01, float(tick_interval_sec))

        self._clock_lock = threading.RLock()
        self._speed_multiplier: float = _snap_replay_speed(speed_multiplier)
        self._bounds: Optional[ReplayBounds] = None
        self._virtual_time: float = 0.0
        self._at_eof: bool = False

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()

    @property
    def is_running(self) -> bool:
        """Check if background replay thread is currently active."""
        return self._thread is not None and self._thread.is_alive()

    @property
    def is_paused(self) -> bool:
        """Check if DVR playback is currently paused."""
        return not self._pause_event.is_set()

    @property
    def speed_multiplier(self) -> float:
        """Current playback speed multiplier (1.0, 5.0, 15.0, or 60.0)."""
        with self._clock_lock:
            return self._speed_multiplier

    @property
    def virtual_time(self) -> float:
        """Current virtual replay clock epoch timestamp in seconds."""
        with self._clock_lock:
            return self._virtual_time

    @property
    def bounds(self) -> Optional[ReplayBounds]:
        """Resolved temporal boundaries of the SQLite database."""
        with self._clock_lock:
            return self._bounds

    @property
    def at_eof(self) -> bool:
        """Return True if the virtual clock has reached the end of the recorded timeline."""
        with self._clock_lock:
            return self._at_eof

    def initialize_timeline(self) -> Optional[ReplayBounds]:
        """Load database bounds and reconstruct the initial frame at t_min synchronously."""
        db = Database(self.db_path, auto_migrate=False)
        try:
            repo = HistoricalRepository(db)
            bounds = repo.get_replay_bounds(
                start_time=self.start_time_filter,
                end_time=self.end_time_filter,
            )
            with self._clock_lock:
                self._bounds = bounds
                if bounds is None:
                    self._virtual_time = 0.0
                    self._at_eof = True
                    self.store.set_replay_status(
                        is_replay=True,
                        db_path=self.db_path,
                        virtual_time=None,
                        start_time=None,
                        end_time=None,
                        speed=self._speed_multiplier,
                    )
                    self.store.set_connection_status(
                        ConnectionStatus.ERROR,
                        "No recorded telemetry in database",
                    )
                    return None

                self._virtual_time = bounds.min_epoch
                self._at_eof = False
                self.store.set_replay_status(
                    is_replay=True,
                    db_path=self.db_path,
                    virtual_time=bounds.min_epoch,
                    start_time=bounds.min_epoch,
                    end_time=bounds.max_epoch,
                    speed=self._speed_multiplier,
                )
                initial_obs = repo.get_replay_snapshot_at(
                    bounds.min_epoch,
                    lookback_sec=self.lookback_sec,
                )
                pairs = [(o.position, o.recorded_epoch) for o in initial_obs]
                self.store.replace_replay_state(pairs, virtual_now=bounds.min_epoch)
                self.store.set_connection_status(
                    ConnectionStatus.CONNECTED,
                    f"DVR Replay ({int(self._speed_multiplier)}x)",
                )
                return bounds
        finally:
            db.close()

    def start(self) -> None:
        """Initialize replay bounds and launch background virtual clock worker thread."""
        if self.is_running:
            return
        self._stop_event.clear()
        self._pause_event.set()
        if self._bounds is None:
            self.initialize_timeline()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="ReplayWorkerThread",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout_sec: float = 2.0) -> None:
        """Signal replay worker thread to terminate and wait for clean exit."""
        self._stop_event.set()
        self._pause_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=timeout_sec)
        self.store.set_connection_status(ConnectionStatus.DISCONNECTED, "Replay stopped")

    def pause(self) -> None:
        """Pause virtual clock progression."""
        self._pause_event.clear()
        self.store.set_connection_status(ConnectionStatus.PAUSED, "Replay paused")

    def resume(self) -> None:
        """Resume virtual clock progression (or restart from t_min if paused at EOF)."""
        with self._clock_lock:
            if self._bounds is not None and self._at_eof:
                self._at_eof = False
                self.seek_to(self._bounds.min_epoch)
            spd = int(self._speed_multiplier)
        self._pause_event.set()
        self.store.set_connection_status(ConnectionStatus.CONNECTED, f"DVR Replay ({spd}x)")

    def set_speed(self, multiplier: float) -> float:
        """Explicitly set the replay speed multiplier (snapped to 1x, 5x, 15x, or 60x)."""
        snapped = _snap_replay_speed(multiplier)
        with self._clock_lock:
            self._speed_multiplier = snapped
            self.store.set_replay_status(
                is_replay=True,
                db_path=self.db_path,
                virtual_time=self._virtual_time if self._bounds is not None else None,
                start_time=self._bounds.min_epoch if self._bounds is not None else None,
                end_time=self._bounds.max_epoch if self._bounds is not None else None,
                speed=snapped,
            )
        return snapped

    def cycle_speed(self, faster: bool = True) -> float:
        """Step playback speed multiplier up ('>') or down ('<') within (1x, 5x, 15x, 60x)."""
        with self._clock_lock:
            curr = _snap_replay_speed(self._speed_multiplier)
            idx = REPLAY_SPEED_MULTIPLIERS.index(curr)
            if faster:
                new_idx = min(len(REPLAY_SPEED_MULTIPLIERS) - 1, idx + 1)
            else:
                new_idx = max(0, idx - 1)
            return self.set_speed(REPLAY_SPEED_MULTIPLIERS[new_idx])

    def seek_to(self, target_epoch: float) -> float:
        """Scrub virtual clock to an absolute epoch timestamp and reconstruct active train state."""
        with self._clock_lock:
            if self._bounds is None:
                self.initialize_timeline()
            if self._bounds is None:
                return 0.0

            clamped = max(self._bounds.min_epoch, min(self._bounds.max_epoch, float(target_epoch)))
            db = Database(self.db_path, auto_migrate=False)
            try:
                repo = HistoricalRepository(db)
                observations = repo.get_replay_snapshot_at(
                    clamped,
                    lookback_sec=self.lookback_sec,
                )
            finally:
                db.close()

            pairs = [(o.position, o.recorded_epoch) for o in observations]
            self._virtual_time = clamped
            self._at_eof = (self._bounds.max_epoch - clamped) < 1e-6
            self.store.replace_replay_state(pairs, virtual_now=clamped)
            self.store.set_replay_status(
                is_replay=True,
                db_path=self.db_path,
                virtual_time=clamped,
                start_time=self._bounds.min_epoch,
                end_time=self._bounds.max_epoch,
                speed=self._speed_multiplier,
            )
            return clamped

    def seek_relative(self, delta_sec: float) -> float:
        """Scrub virtual clock relative to current virtual_time by delta_sec (e.g. -300s or +300s)."""
        with self._clock_lock:
            target = self._virtual_time + float(delta_sec)
            return self.seek_to(target)

    def _step_virtual_clock(self, repo: HistoricalRepository, dt_real: float) -> bool:
        """Advance virtual clock by one tick and ingest any observations in (t_prev, t_next]."""
        with self._clock_lock:
            if self._bounds is None:
                return False
            t_prev = self._virtual_time
            t_max = self._bounds.max_epoch
            if (t_max - t_prev) <= 1e-6:
                self._at_eof = True
                return False

            dt_virtual = max(0.0, dt_real) * self._speed_multiplier
            t_next = min(t_max, t_prev + dt_virtual)
            start_iso = _epoch_to_iso_utc(t_prev)
            end_iso = _epoch_to_iso_utc(t_next)

            observations = repo.get_replay_observations(
                start_time=start_iso,
                end_time=end_iso,
                include_start=False,
            )
            pairs = [(o.position, o.recorded_epoch) for o in observations]
            self._virtual_time = t_next
            self.store.ingest_replay_observations(pairs, virtual_now=t_next)
            self.store.prune_stale_trains(max_age_sec=self.lookback_sec, now=t_next)

            if (t_max - t_next) <= 1e-6:
                self._at_eof = True
                return False
            return True

    def _run_loop(self) -> None:
        """Main background DVR virtual clock progression loop."""
        if self._bounds is None:
            return

        db = Database(self.db_path, auto_migrate=False)
        repo = HistoricalRepository(db)
        last_mono = time.monotonic()

        try:
            while not self._stop_event.is_set():
                if not self._pause_event.is_set():
                    self._pause_event.wait(timeout=0.1)
                    last_mono = time.monotonic()
                    continue

                if self._stop_event.wait(timeout=self.tick_interval_sec):
                    break

                if not self._pause_event.is_set():
                    last_mono = time.monotonic()
                    continue

                now_mono = time.monotonic()
                dt_real = min(1.0, max(0.0, now_mono - last_mono))
                last_mono = now_mono

                keep_playing = self._step_virtual_clock(repo, dt_real)
                if not keep_playing:
                    self._pause_event.clear()
                    self.store.set_connection_status(
                        ConnectionStatus.PAUSED,
                        "End of replay timeline",
                    )
        finally:
            db.close()
