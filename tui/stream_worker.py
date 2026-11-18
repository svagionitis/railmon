"""
Background worker thread managing telemetry stream ingestion and reconnection.
Supports live SSE network streams and offline mock generator sources.
"""

from collections.abc import Iterable
import logging
import random
import threading
from typing import Any, Generator, Iterable, List, Optional, Union

from train_tracker import RailwayGovClient, TrainPosition
from tui.mock_engine import MockTelemetryEngine
from tui.state import ConnectionStatus, TrainStateStore

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
        custom_stream: Optional[Union[Generator[List[TrainPosition], None, None], Iterable[List[TrainPosition]]]] = None,
        recorder: Optional[Any] = None,
        base_backoff_sec: float = 1.0,
        max_backoff_sec: float = 30.0,
    ):
        self.store = store
        self.client = client
        self.mock_engine = mock_engine
        self.custom_stream = custom_stream
        self.recorder = recorder

        self.base_backoff_sec = base_backoff_sec
        self.max_backoff_sec = max_backoff_sec

        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()  # Unpaused by default

    @property
    def is_running(self) -> bool:
        """Check if worker thread is currently active."""
        return self._thread is not None and self._thread.is_alive()

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
        self.store.set_connection_status(ConnectionStatus.PAUSED, "Stream paused")

    def resume(self) -> None:
        """Resume stream ingestion from paused state."""
        self._pause_event.set()
        self.store.set_connection_status(ConnectionStatus.CONNECTED, "Stream active")

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

                    if batch:
                        self.store.update_positions(batch)
                        if self.recorder is not None and getattr(self.recorder, "is_running", False):
                            self.recorder.enqueue_batch(batch)

                # If generator completed normally without error
                if self._stop_event.is_set():
                    break

            except Exception as exc:
                if self._stop_event.is_set():
                    break

                # Apply exponential backoff with jitter
                jitter = random.uniform(0.8, 1.2)
                sleep_time = min(backoff * jitter, self.max_backoff_sec)
                msg = f"Connection error: {exc}. Reconnecting in {sleep_time:.1f}s..."
                logger.warning(msg)
                self.store.set_connection_status(ConnectionStatus.RECONNECTING, msg)

                # Interruptible sleep
                if self._stop_event.wait(timeout=sleep_time):
                    break

                backoff = min(backoff * 2.0, self.max_backoff_sec)

        self.store.set_connection_status(ConnectionStatus.DISCONNECTED, "Stream disconnected")

    def _get_stream_generator(self) -> Iterable[List[TrainPosition]]:
        """Instantiate appropriate telemetry stream source."""
        if self.custom_stream is not None:
            return self.custom_stream
        elif self.mock_engine is not None:
            return self.mock_engine.stream_positions(interval_sec=1.0)
        elif self.client is not None:
            return self.client.stream_train_positions(
                reconnect_delay=0.0,
                stop_event=self._stop_event,
            )
        else:
            raise ValueError("No valid telemetry source provided to StreamWorker")
