"""Shared zero-dependency bounded HTTP server and request handler primitives for railmon.

Provides a strictly acyclic, pure-stdlib foundation shared by all three embedded
HTTP daemons (WebMapServer, MetricsServer, GtfsRtServer) to defend against:
  - Unbounded OS thread and file-descriptor exhaustion (BoundedSemaphore cap
    with RFC 9112 lingering-close HTTP 503 overload rejection and fast fallback).
  - Silent 0-byte and 1-byte-trickle Slowloris attacks (monotonic end-to-end
    header read deadline via _DeadlineSocketReader + total header byte cap).
  - Infinite HTTP/1.1 keep-alive thread pinning (idle timeout + max requests).
  - Stalled zero-window SSE consumer write hangs (prepare_sse_stream helper).
  - Unhandled socket-drop and timeout tracebacks on stderr.
"""

from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import logging
import math
import socket
import sys
import threading
import time
from typing import Any, Optional, Tuple

logger = logging.getLogger("railmon.http")

DEFAULT_MAX_CONNECTIONS: int = 64
DEFAULT_MAX_REJECT_WORKERS: int = 8
DEFAULT_REQUEST_TIMEOUT_SEC: float = 10.0
DEFAULT_HEADER_TIMEOUT_SEC: float = 10.0
DEFAULT_KEEPALIVE_TIMEOUT_SEC: float = 5.0
DEFAULT_MAX_KEEPALIVE_REQUESTS: int = 100
DEFAULT_SSE_WRITE_TIMEOUT_SEC: float = 10.0
DEFAULT_LISTEN_BACKLOG: int = 64
DEFAULT_OVERLOAD_RETRY_AFTER_SEC: int = 5
DEFAULT_REJECT_RECV_TIMEOUT_SEC: float = 0.25
DEFAULT_REJECT_LINGER_TIMEOUT_SEC: float = 0.10
MAX_HTTP_HEADER_BYTES: int = 65536
MIN_HTTP_HEADER_BYTES: int = 1024

_EXPECTED_SOCKET_ERRORS = (
    BrokenPipeError,
    ConnectionResetError,
    ConnectionAbortedError,
    TimeoutError,
    socket.timeout,
)


class HeaderLimitExceededError(Exception):
    """Raised when incoming HTTP request-line and headers exceed max_header_bytes."""


def validate_positive_int(name: str, value: Any, *, minimum: int = 1) -> int:
    """Validate that value is a non-boolean integer >= minimum."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}, got {value!r}")
    return value


def validate_positive_finite_float(name: str, value: Any) -> float:
    """Validate that value is a finite, strictly positive non-boolean float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number, got {value!r}")
    val = float(value)
    if not math.isfinite(val) or val <= 0.0:
        raise ValueError(f"{name} must be finite and strictly positive, got {value!r}")
    return val


@dataclass(frozen=True)
class HttpLimitsConfig:
    """Immutable connection, timeout, and Slowloris hardening limits for HTTP daemons."""

    max_connections: int = DEFAULT_MAX_CONNECTIONS
    request_timeout_sec: float = DEFAULT_REQUEST_TIMEOUT_SEC
    header_timeout_sec: float = DEFAULT_HEADER_TIMEOUT_SEC
    keepalive_timeout_sec: float = DEFAULT_KEEPALIVE_TIMEOUT_SEC
    max_keepalive_requests: int = DEFAULT_MAX_KEEPALIVE_REQUESTS
    sse_write_timeout_sec: float = DEFAULT_SSE_WRITE_TIMEOUT_SEC
    max_reject_workers: int = DEFAULT_MAX_REJECT_WORKERS
    listen_backlog: int = DEFAULT_LISTEN_BACKLOG
    overload_retry_after_sec: int = DEFAULT_OVERLOAD_RETRY_AFTER_SEC
    max_header_bytes: int = MAX_HTTP_HEADER_BYTES

    def __post_init__(self) -> None:
        """Validate all connection, timeout, and header bounds strictly."""
        validate_positive_int("max_connections", self.max_connections, minimum=1)
        validate_positive_finite_float("request_timeout_sec", self.request_timeout_sec)
        validate_positive_finite_float("header_timeout_sec", self.header_timeout_sec)
        validate_positive_finite_float("keepalive_timeout_sec", self.keepalive_timeout_sec)
        validate_positive_int("max_keepalive_requests", self.max_keepalive_requests, minimum=1)
        validate_positive_finite_float("sse_write_timeout_sec", self.sse_write_timeout_sec)
        validate_positive_int("max_reject_workers", self.max_reject_workers, minimum=1)
        validate_positive_int("listen_backlog", self.listen_backlog, minimum=1)
        validate_positive_int(
            "overload_retry_after_sec", self.overload_retry_after_sec, minimum=1
        )
        validate_positive_int(
            "max_header_bytes", self.max_header_bytes, minimum=MIN_HTTP_HEADER_BYTES
        )

    @classmethod
    def for_request_timeout(
        cls,
        request_timeout_sec: float,
        *,
        max_connections: int = DEFAULT_MAX_CONNECTIONS,
        sse_write_timeout_sec: Optional[float] = None,
    ) -> "HttpLimitsConfig":
        """Build limits clamping header and keep-alive timeouts to request_timeout_sec."""
        req_timeout = validate_positive_finite_float("request_timeout_sec", request_timeout_sec)
        sse_timeout = (
            req_timeout
            if sse_write_timeout_sec is None
            else validate_positive_finite_float("sse_write_timeout_sec", sse_write_timeout_sec)
        )
        return cls(
            max_connections=max_connections,
            request_timeout_sec=req_timeout,
            header_timeout_sec=min(DEFAULT_HEADER_TIMEOUT_SEC, req_timeout),
            keepalive_timeout_sec=min(DEFAULT_KEEPALIVE_TIMEOUT_SEC, req_timeout),
            sse_write_timeout_sec=sse_timeout,
        )


def _build_overload_503_response(retry_after_sec: int) -> bytes:
    """Build static HTTP/1.1 503 Service Unavailable overload response bytes."""
    body = json.dumps(
        {
            "error": "Service Unavailable: maximum concurrent connections reached",
            "status": 503,
        }
    ).encode("utf-8")
    headers = (
        "HTTP/1.1 503 Service Unavailable\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"Retry-After: {retry_after_sec}\r\n"
        "Connection: close\r\n"
        "Cache-Control: no-store\r\n"
        "X-Content-Type-Options: nosniff\r\n"
        "X-Frame-Options: DENY\r\n"
        "Referrer-Policy: no-referrer\r\n"
        "\r\n"
    ).encode("ascii")
    return headers + body


class _DeadlineSocketReader(io.RawIOBase):
    """Raw socket reader enforcing monotonic header deadlines and byte caps.

    Defeats 1-byte-trickle Slowloris attacks by recalculating the remaining
    monotonic time budget before every underlying socket recv_into() call.
    Does not close the underlying socket on close() (owned by TCPServer).
    """

    def __init__(self, sock: socket.socket, op_timeout: float) -> None:
        super().__init__()
        self._sock = sock
        self._op_timeout = op_timeout
        self._deadline: Optional[float] = None
        self._first_byte_timeout: Optional[float] = None
        self._header_timeout_sec: float = op_timeout
        self._got_first_byte: bool = False
        self._timed_out: bool = False
        self._header_bytes_read: int = 0
        self._max_header_bytes: Optional[int] = None

    @property
    def got_first_byte(self) -> bool:
        """Return True if at least one byte was received for the active request."""
        return self._got_first_byte

    @property
    def timed_out(self) -> bool:
        """Return True if the active request experienced a read timeout."""
        return self._timed_out

    def readable(self) -> bool:
        """Return True indicating this raw stream supports reading."""
        return True

    def begin_request_headers(
        self,
        *,
        first_byte_timeout: float,
        header_timeout: float,
        max_header_bytes: int,
        start_deadline_immediately: bool,
    ) -> None:
        """Arm deadline and byte-budget guards for an incoming HTTP request header."""
        self._got_first_byte = False
        self._timed_out = False
        self._first_byte_timeout = first_byte_timeout
        self._header_timeout_sec = header_timeout
        self._deadline = (
            time.monotonic() + header_timeout if start_deadline_immediately else None
        )
        self._header_bytes_read = 0
        self._max_header_bytes = max_header_bytes

    def end_request_headers(self) -> None:
        """Disarm header deadline/byte-budget guards and restore op socket timeout."""
        self._deadline = None
        self._first_byte_timeout = None
        self._max_header_bytes = None
        try:
            self._sock.settimeout(self._op_timeout)
        except OSError as exc:
            logger.debug("Skipping socket timeout reset on closed handle: %s", exc)

    def _compute_next_timeout(self) -> float:
        """Compute bounded socket timeout for the next recv_into() call."""
        if (
            not self._got_first_byte
            and self._deadline is None
            and self._first_byte_timeout is not None
        ):
            return min(self._op_timeout, self._first_byte_timeout)
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0.0:
                self._timed_out = True
                raise TimeoutError("HTTP request header read deadline exceeded")
            return min(self._op_timeout, remaining)
        return self._op_timeout

    def _record_header_bytes(self, count: int) -> None:
        """Track header bytes received and raise on exceeding max_header_bytes."""
        if count <= 0:
            return
        if not self._got_first_byte:
            self._got_first_byte = True
            if self._deadline is None and self._first_byte_timeout is not None:
                self._deadline = time.monotonic() + self._header_timeout_sec
        if self._max_header_bytes is not None:
            self._header_bytes_read += count
            if self._header_bytes_read > self._max_header_bytes:
                raise HeaderLimitExceededError(
                    f"HTTP request headers exceeded {self._max_header_bytes} bytes"
                )

    def readinto(self, b: Any) -> int:
        """Read bytes into buffer b while enforcing monotonic header deadlines."""
        if self.closed:
            return 0
        timeout = self._compute_next_timeout()
        self._sock.settimeout(timeout)
        try:
            n = self._sock.recv_into(b)
        except TimeoutError:
            self._timed_out = True
            raise
        self._record_header_bytes(n)
        return n


class BoundedHTTPRequestHandler(BaseHTTPRequestHandler):
    """Hardened HTTP/1.1 request handler enforcing deadlines, header caps, and keep-alive limits."""

    protocol_version: str = "HTTP/1.1"
    timeout: Optional[float] = DEFAULT_REQUEST_TIMEOUT_SEC
    rbufsize: int = 8192
    wbufsize: int = 0

    def _resolve_http_limits(self) -> HttpLimitsConfig:
        """Return HttpLimitsConfig configured on parent server or safe defaults."""
        limits = getattr(self.server, "http_limits", None)
        if isinstance(limits, HttpLimitsConfig):
            return limits
        return HttpLimitsConfig()

    def setup(self) -> None:
        """Initialize socket timeouts and monotonic deadline-enforcing rfile."""
        limits = self._resolve_http_limits()
        self._http_limits: HttpLimitsConfig = limits
        self._requests_handled: int = 0
        self._force_close_header: bool = False
        self._connection_header_written: bool = False
        self._timeout_recorded_for_req: bool = False
        self.timeout = limits.request_timeout_sec

        super().setup()

        # Replace default rfile with monotonic deadline-guarded BufferedReader while
        # preserving stdlib wfile (_SocketWriter -> sendall) semantics.
        try:
            self.rfile.close()
        except OSError as exc:
            logger.debug("Error closing default rfile wrapper during setup: %s", exc)

        self._deadline_reader = _DeadlineSocketReader(
            self.connection,
            op_timeout=limits.request_timeout_sec,
        )
        raw_bufsize = self.rbufsize if self.rbufsize > 0 else io.DEFAULT_BUFFER_SIZE
        # Cap buffer size at max_header_bytes so a single header read never
        # pulls trailing request body bytes over the header-size threshold.
        bufsize = min(raw_bufsize, limits.max_header_bytes)
        self.rfile = io.BufferedReader(self._deadline_reader, buffer_size=bufsize)

    def log_message(self, log_format: str, *args: Any) -> None:
        """Route HTTP access and diagnostic logs to Python logging instead of stderr."""
        logger.debug(
            "%s - - [%s] %s",
            self.client_address[0] if self.client_address else "127.0.0.1",
            self.log_date_time_string(),
            log_format % args,
        )

    def send_header(self, keyword: str, value: str) -> None:
        """Track explicit Connection headers so forced close headers are never duplicated."""
        if keyword.lower() == "connection":
            self._connection_header_written = True
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        """Inject Connection: close when keep-alive request budget is exhausted."""
        if self._force_close_header and not self._connection_header_written:
            super().send_header("Connection", "close")
            self._connection_header_written = True
        super().end_headers()

    def parse_request(self) -> bool:
        """Parse HTTP request line/headers and disarm header deadline afterward."""
        try:
            parsed = super().parse_request()
        finally:
            self._deadline_reader.end_request_headers()

        if parsed and (self._requests_handled + 1) >= self._http_limits.max_keepalive_requests:
            self.close_connection = True
            self._force_close_header = True
        return parsed

    def _record_server_timeout(self) -> None:
        """Record an active request timeout on the parent server at most once per request."""
        if self._timeout_recorded_for_req:
            return
        self._timeout_recorded_for_req = True
        recorder = getattr(self.server, "record_timeout", None)
        if callable(recorder):
            recorder()

    def _send_header_limit_431(self) -> None:
        """Emit HTTP 431 Request Header Fields Too Large and close the connection."""
        self.close_connection = True
        self._force_close_header = True
        if not hasattr(self, "requestline"):
            self.requestline = ""
        if not getattr(self, "request_version", None):
            self.request_version = self.protocol_version
        body = json.dumps(
            {
                "error": "Request Header Fields Too Large",
                "status": 431,
            }
        ).encode("utf-8")
        try:
            self.send_response(431)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
        except _EXPECTED_SOCKET_ERRORS as exc:
            logger.debug("Failed writing 431 response to client: %s", exc)

    def handle_one_request(self) -> None:
        """Handle a single HTTP request with Slowloris deadline, header cap, and keep-alive guards."""
        is_first_request = self._requests_handled == 0
        self._force_close_header = False
        self._connection_header_written = False
        self._timeout_recorded_for_req = False
        self.command = ""

        first_byte_timeout = (
            self._http_limits.header_timeout_sec
            if is_first_request
            else self._http_limits.keepalive_timeout_sec
        )
        self._deadline_reader.begin_request_headers(
            first_byte_timeout=first_byte_timeout,
            header_timeout=self._http_limits.header_timeout_sec,
            max_header_bytes=self._http_limits.max_header_bytes,
            start_deadline_immediately=is_first_request,
        )

        try:
            super().handle_one_request()
        except HeaderLimitExceededError as exc:
            logger.warning(
                "HTTP header size limit exceeded from %s: %s",
                self.client_address,
                exc,
            )
            self._deadline_reader.end_request_headers()
            self._send_header_limit_431()
            return

        if self._deadline_reader.timed_out:
            keepalive_idle = (not is_first_request) and (not self._deadline_reader.got_first_byte)
            if not keepalive_idle:
                self._record_server_timeout()
            self.close_connection = True
            return

        if self.command:
            self._requests_handled += 1

    def prepare_sse_stream(self) -> None:
        """Configure connection for long-lived SSE streaming with bounded write timeout.

        Forces Connection: close semantics after stream exit (preventing HTTP/1.1
        keep-alive re-entry hangs) and applies sse_write_timeout_sec so zero-window
        stalled consumers raise TimeoutError instead of pinning threads forever.
        """
        self.close_connection = True
        self._force_close_header = True
        self._deadline_reader.end_request_headers()
        try:
            self.connection.settimeout(self._http_limits.sse_write_timeout_sec)
        except OSError as exc:
            logger.debug("Could not apply SSE write timeout on closed socket: %s", exc)


class BoundedHTTPServer(ThreadingHTTPServer):
    """Bounded multi-threaded HTTP server enforcing concurrency caps and fast 503 rejection."""

    daemon_threads: bool = True
    allow_reuse_address: bool = True
    block_on_close: bool = False
    request_queue_size: int = DEFAULT_LISTEN_BACKLOG

    def __init__(
        self,
        server_address: Tuple[str, int],
        RequestHandlerClass: Any,
        *,
        http_limits: Optional[HttpLimitsConfig] = None,
        bind_and_activate: bool = True,
    ) -> None:
        """Initialize bounded server with concurrency semaphores and listen backlog."""
        self.http_limits: HttpLimitsConfig = http_limits or HttpLimitsConfig()
        self.request_queue_size = self.http_limits.listen_backlog
        self._conn_semaphore = threading.BoundedSemaphore(self.http_limits.max_connections)
        self._reject_semaphore = threading.BoundedSemaphore(self.http_limits.max_reject_workers)
        self._stats_lock = threading.Lock()
        self._active_connections: int = 0
        self._peak_connections: int = 0
        self._rejected_connections: int = 0
        self._timed_out_connections: int = 0
        self._overload_503_payload: bytes = _build_overload_503_response(
            self.http_limits.overload_retry_after_sec
        )
        super().__init__(
            server_address,
            RequestHandlerClass,
            bind_and_activate=bind_and_activate,
        )

    @property
    def max_connections(self) -> int:
        """Return configured hard cap on concurrent client connections."""
        return self.http_limits.max_connections

    @property
    def active_connections(self) -> int:
        """Return count of currently active client worker threads."""
        with self._stats_lock:
            return self._active_connections

    @property
    def peak_connections(self) -> int:
        """Return high-water mark of concurrent client worker threads."""
        with self._stats_lock:
            return self._peak_connections

    @property
    def rejected_connections(self) -> int:
        """Return total count of connections rejected with HTTP 503 due to saturation."""
        with self._stats_lock:
            return self._rejected_connections

    @property
    def timed_out_connections(self) -> int:
        """Return total count of active/Slowloris requests terminated by timeout."""
        with self._stats_lock:
            return self._timed_out_connections

    def record_timeout(self) -> None:
        """Increment counter of active/Slowloris request timeouts."""
        with self._stats_lock:
            self._timed_out_connections += 1

    def _reject_socket_fast_nonblocking(self, request: Any) -> None:
        """Perform zero-blocking best-effort 503 send and immediate teardown."""
        try:
            request.setblocking(False)
            try:
                request.send(self._overload_503_payload)
            except (BlockingIOError, OSError) as exc:
                logger.debug("Non-blocking 503 send skipped under flood: %s", exc)
        except OSError as exc:
            logger.debug("Socket unavailable during fast overload rejection: %s", exc)
        finally:
            self.shutdown_request(request)

    def _reject_socket_worker(self, request: Any) -> None:
        """Deliver clean HTTP 503 using RFC 9112 lingering close to avoid TCP RST."""
        try:
            request.settimeout(DEFAULT_REJECT_RECV_TIMEOUT_SEC)
            try:
                request.recv(4096)
            except (TimeoutError, OSError):
                logger.debug("No in-flight request bytes drained prior to 503 rejection")

            try:
                request.sendall(self._overload_503_payload)
                request.shutdown(socket.SHUT_WR)
            except (TimeoutError, OSError) as exc:
                logger.debug("Client closed before receiving 503 rejection: %s", exc)
                return

            request.settimeout(DEFAULT_REJECT_LINGER_TIMEOUT_SEC)
            try:
                request.recv(1024)
            except (TimeoutError, OSError):
                logger.debug("Client linger wait elapsed after 503 SHUT_WR")
        finally:
            try:
                self.close_request(request)
            finally:
                self._reject_semaphore.release()

    def _reject_overloaded_socket(self, request: Any, client_address: Any) -> None:
        """Reject an overflow TCP connection with HTTP 503 without stalling accept()."""
        if not self._reject_semaphore.acquire(blocking=False):
            self._reject_socket_fast_nonblocking(request)
            return

        reject_thread = threading.Thread(
            target=self._reject_socket_worker,
            args=(request,),
            name=f"{type(self).__name__}Reject",
            daemon=True,
        )
        try:
            reject_thread.start()
        except (RuntimeError, OSError) as exc:
            logger.error(
                "Could not start 503 rejector thread for %s: %s",
                client_address,
                exc,
            )
            self._reject_semaphore.release()
            self._reject_socket_fast_nonblocking(request)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Admit a connection up to max_connections or reject immediately with HTTP 503."""
        if not self._conn_semaphore.acquire(blocking=False):
            with self._stats_lock:
                self._rejected_connections += 1
                active = self._active_connections
            logger.warning(
                "%s connection cap reached (%d/%d); rejecting %s with HTTP 503",
                type(self).__name__,
                active,
                self.http_limits.max_connections,
                client_address,
            )
            self._reject_overloaded_socket(request, client_address)
            return

        with self._stats_lock:
            self._active_connections += 1
            if self._active_connections > self._peak_connections:
                self._peak_connections = self._active_connections

        worker = threading.Thread(
            target=self.process_request_thread,
            args=(request, client_address),
            name=f"{type(self).__name__}Worker",
            daemon=self.daemon_threads,
        )
        try:
            worker.start()
        except (RuntimeError, OSError) as exc:
            logger.error(
                "%s failed to spawn worker thread for %s: %s",
                type(self).__name__,
                client_address,
                exc,
            )
            with self._stats_lock:
                self._active_connections = max(0, self._active_connections - 1)
            self._conn_semaphore.release()
            self.shutdown_request(request)

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        """Execute request handler and deterministically release the connection semaphore."""
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._stats_lock:
                self._active_connections = max(0, self._active_connections - 1)
            self._conn_semaphore.release()

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Suppress expected client socket drops and timeouts; log unexpected errors cleanly."""
        exc_type, exc_val, _ = sys.exc_info()
        if exc_type is not None and issubclass(exc_type, _EXPECTED_SOCKET_ERRORS):
            logger.debug(
                "%s socket closed/timed out for %s: %s",
                type(self).__name__,
                client_address,
                exc_val,
            )
            return
        logger.warning(
            "%s unhandled request error from %s: %s",
            type(self).__name__,
            client_address,
            exc_val,
            exc_info=True,
        )


__all__ = [
    "DEFAULT_MAX_CONNECTIONS",
    "DEFAULT_MAX_REJECT_WORKERS",
    "DEFAULT_REQUEST_TIMEOUT_SEC",
    "DEFAULT_HEADER_TIMEOUT_SEC",
    "DEFAULT_KEEPALIVE_TIMEOUT_SEC",
    "DEFAULT_MAX_KEEPALIVE_REQUESTS",
    "DEFAULT_SSE_WRITE_TIMEOUT_SEC",
    "DEFAULT_LISTEN_BACKLOG",
    "DEFAULT_OVERLOAD_RETRY_AFTER_SEC",
    "MAX_HTTP_HEADER_BYTES",
    "HeaderLimitExceededError",
    "HttpLimitsConfig",
    "BoundedHTTPRequestHandler",
    "BoundedHTTPServer",
    "validate_positive_int",
    "validate_positive_finite_float",
]
