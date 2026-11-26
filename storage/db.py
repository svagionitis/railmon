"""
Database connection factory and transaction manager.
"""

import contextlib
import logging
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Dict, Generator, List, Optional, Set, Tuple
import weakref

from storage.models import parse_iso_utc_datetime
from storage.schema import SchemaManager

logger = logging.getLogger("railmon." + __name__)

_WAL_CHECKPOINT_SQL: Dict[str, str] = {
    "PASSIVE": "PRAGMA wal_checkpoint(PASSIVE);",
    "FULL": "PRAGMA wal_checkpoint(FULL);",
    "RESTART": "PRAGMA wal_checkpoint(RESTART);",
    "TRUNCATE": "PRAGMA wal_checkpoint(TRUNCATE);",
}


def _build_readonly_sqlite_uri(db_path: str) -> str:
    """Build a percent-encoded RFC 3986 SQLite read-only URI for local filesystem paths."""
    return f"{Path(os.path.abspath(db_path)).as_uri()}?mode=ro"


def _convert_timestamp(val: bytes) -> Any:
    """Safely convert SQLite TIMESTAMP bytes to UTC datetime or raw string."""
    s = val.decode("utf-8")
    parsed = parse_iso_utc_datetime(s)
    if parsed is not None:
        return parsed
    return s


sqlite3.register_converter("TIMESTAMP", _convert_timestamp)
sqlite3.register_converter("timestamp", _convert_timestamp)


class Database:
    """
    High-performance SQLite database connection coordinator.
    Configures Write-Ahead Logging (WAL) and memory pragmas for high-throughput ingestion.
    """

    def __init__(
        self,
        db_path: str = "railway_history.db",
        auto_migrate: bool = True,
        read_only: bool = False,
        verify_integrity: Optional[bool] = None,
    ) -> None:
        self.db_path = db_path
        self.read_only = bool(read_only)
        self._is_memory = (db_path == ":memory:") or db_path.startswith("file::memory:")

        if self.read_only and self._is_memory:
            raise ValueError("Cannot open an in-memory SQLite database in read-only mode")
        if self.read_only and auto_migrate:
            raise ValueError("auto_migrate=True is incompatible with read_only=True")

        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._closed: bool = False
        self._open_connections: Set[sqlite3.Connection] = set()
        self._thread_conns: Dict[
            int, Tuple[weakref.ref[threading.Thread], sqlite3.Connection]
        ] = {}
        self._memory_lock = threading.RLock()
        self._memory_tx_depth: int = 0

        # For in-memory databases, maintain persistent connection to prevent schema teardown
        self._shared_memory_conn: Optional[sqlite3.Connection] = None
        if self._is_memory:
            self._shared_memory_conn = self._create_connection()

        if auto_migrate:
            try:
                with self.connection() as conn:
                    SchemaManager.initialize_schema(conn, verify_integrity=verify_integrity)
            except Exception:
                self.close()
                raise

        if not self.read_only:
            self._ensure_secure_permissions()

    def _assert_writable(self, operation: str) -> None:
        """Raise sqlite3.OperationalError when mutating operations target a read-only handle."""
        if self.read_only:
            raise sqlite3.OperationalError(
                f"Cannot perform '{operation}': database handle is opened in read-only mode"
            )

    def _ensure_secure_permissions(self) -> None:
        """Enforce owner-only permissions (0600) on POSIX database and WAL companion files."""
        if self._is_memory or self.read_only or os.name == "nt":
            return

        abs_path = os.path.abspath(self.db_path)
        for suffix in ("", "-wal", "-shm"):
            target = f"{abs_path}{suffix}"
            if os.path.exists(target):
                try:
                    cur_mode = os.stat(target).st_mode & 0o777
                    if cur_mode != 0o600:
                        os.chmod(target, 0o600)
                except OSError:
                    pass

    def init_schema(self) -> int:
        """Explicitly initialize all required database tables, indices, and verify integrity."""
        self._assert_writable("init_schema")
        with self.connection() as conn:
            return SchemaManager.initialize_schema(conn)

    def verify_integrity(self) -> Tuple[bool, List[str]]:
        """Run PRAGMA integrity_check and foreign_key_check on the database."""
        with self.connection() as conn:
            return SchemaManager.verify_integrity(conn)

    def optimize(self) -> None:
        """Run PRAGMA optimize on the active connection."""
        self._assert_writable("optimize")
        with self.connection() as conn:
            SchemaManager.optimize(conn)

    def _create_readonly_connection(self) -> sqlite3.Connection:
        """Create a lightweight, strictly read-only SQLite URI connection."""
        conn = sqlite3.connect(
            _build_readonly_sqlite_uri(self.db_path),
            uri=True,
            timeout=2.0,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            check_same_thread=False,
        )
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        try:
            cursor.execute("PRAGMA query_only = ON;")
            cursor.execute("PRAGMA busy_timeout = 2000;")
            cursor.execute("PRAGMA temp_store = MEMORY;")
            cursor.execute("PRAGMA cache_size = -4000;")  # Lightweight 4MB read cache
        except Exception:
            cursor.close()
            conn.close()
            raise
        else:
            cursor.close()
        return conn

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection configured with optimal ingestion pragmas."""
        if self.read_only:
            return self._create_readonly_connection()

        use_uri = self.db_path.startswith("file:")
        if not self._is_memory and not use_uri:
            abs_path = os.path.abspath(self.db_path)
            parent = os.path.dirname(abs_path)
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)
            if os.name != "nt" and not os.path.exists(abs_path):
                # Atomically pre-create with owner-only mode 0600 without mutating process-wide umask
                fd = os.open(abs_path, os.O_CREAT | os.O_RDWR, 0o600)
                os.close(fd)

        conn = sqlite3.connect(
            self.db_path,
            uri=use_uri,
            timeout=5.0,
            isolation_level=None,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
            check_same_thread=False,
        )

        conn.row_factory = sqlite3.Row
        self._ensure_secure_permissions()

        cursor = conn.cursor()
        try:
            if not self._is_memory:
                cursor.execute("PRAGMA journal_mode = WAL;")
                cursor.execute("PRAGMA synchronous = NORMAL;")
            cursor.execute("PRAGMA busy_timeout = 5000;")
            cursor.execute("PRAGMA foreign_keys = ON;")
            cursor.execute("PRAGMA temp_store = MEMORY;")
            cursor.execute("PRAGMA cache_size = -64000;")  # 64MB cache
        except Exception:
            cursor.close()
            conn.close()
            raise
        else:
            cursor.close()

        self._ensure_secure_permissions()
        return conn

    def _reap_dead_thread_connections_locked(self) -> List[sqlite3.Connection]:
        """Collect and unlink connections owned by terminated threads (caller holds _conn_lock)."""
        stale: List[sqlite3.Connection] = []
        dead_tids: List[int] = []
        for tid, (thread_ref, tracked_conn) in self._thread_conns.items():
            thread_obj = thread_ref()
            if thread_obj is None or not thread_obj.is_alive():
                dead_tids.append(tid)
                stale.append(tracked_conn)
        for tid in dead_tids:
            self._thread_conns.pop(tid, None)
        for stale_conn in stale:
            self._open_connections.discard(stale_conn)
        return stale

    def _get_thread_connection(self) -> sqlite3.Connection:
        """Return the current thread's persistent SQLite connection, creating it if needed."""
        tid = threading.get_ident()
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        stale_conns: List[sqlite3.Connection] = []
        reused: Optional[sqlite3.Connection] = None

        with self._conn_lock:
            if self._closed:
                raise sqlite3.ProgrammingError("Cannot operate on a closed Database instance.")
            stale_conns = self._reap_dead_thread_connections_locked()
            if conn is not None and conn in self._open_connections:
                reused = conn

        for stale_conn in stale_conns:
            try:
                stale_conn.close()
            except (sqlite3.Error, OSError):
                pass

        if reused is not None:
            return reused

        new_conn = self._create_connection()
        with self._conn_lock:
            if self._closed:
                new_conn.close()
                raise sqlite3.ProgrammingError("Cannot operate on a closed Database instance.")
            self._open_connections.add(new_conn)
            self._thread_conns[tid] = (weakref.ref(threading.current_thread()), new_conn)
        self._local.conn = new_conn
        return new_conn

    @contextlib.contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager yielding an active thread-scoped SQLite connection."""
        if self._is_memory:
            with self._memory_lock:
                if self._closed:
                    raise sqlite3.ProgrammingError(
                        "Cannot operate on a closed Database instance."
                    )
                if self._shared_memory_conn is None:
                    self._shared_memory_conn = self._create_connection()
                yield self._shared_memory_conn
        else:
            conn = self._get_thread_connection()
            yield conn

    def _get_tx_depth(self) -> int:
        if self._is_memory:
            return self._memory_tx_depth
        return int(getattr(self._local, "tx_depth", 0))

    def _set_tx_depth(self, depth: int) -> None:
        if self._is_memory:
            self._memory_tx_depth = depth
        else:
            self._local.tx_depth = depth

    @contextlib.contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for atomic write transactions with true SAVEPOINT nesting."""
        self._assert_writable("transaction")
        with self.connection() as conn:
            cursor = conn.cursor()
            depth = self._get_tx_depth()
            # Treat externally-started transactions as outer depth >= 1 to avoid silent commit
            if depth == 0 and conn.in_transaction:
                depth = 1
            savepoint_name = f"railmon_sp_{depth}" if depth > 0 else ""
            try:
                if depth == 0:
                    cursor.execute("BEGIN IMMEDIATE;")
                else:
                    cursor.execute(f"SAVEPOINT {savepoint_name};")
                self._set_tx_depth(depth + 1)
                try:
                    yield cursor
                except Exception:
                    if depth == 0:
                        if conn.in_transaction:
                            cursor.execute("ROLLBACK;")
                    else:
                        cursor.execute(f"ROLLBACK TO SAVEPOINT {savepoint_name};")
                        cursor.execute(f"RELEASE SAVEPOINT {savepoint_name};")
                    raise
                else:
                    if depth == 0:
                        if conn.in_transaction:
                            cursor.execute("COMMIT;")
                    else:
                        cursor.execute(f"RELEASE SAVEPOINT {savepoint_name};")
            finally:
                self._set_tx_depth(max(0, self._get_tx_depth() - 1))
                cursor.close()

    def checkpoint_wal(self, mode: str = "PASSIVE") -> Tuple[int, int, int]:
        """Trigger WAL checkpoint to write changes to base file (PASSIVE, FULL, RESTART, TRUNCATE).

        Returns:
            Tuple of (busy, log_frames, checkpointed_frames).
        """
        self._assert_writable("checkpoint_wal")
        if not isinstance(mode, str):
            raise ValueError(f"Invalid WAL checkpoint mode: {mode!r}")

        normalized_mode = mode.strip().upper()
        sql = _WAL_CHECKPOINT_SQL.get(normalized_mode)
        if sql is None:
            allowed = ", ".join(sorted(_WAL_CHECKPOINT_SQL.keys()))
            raise ValueError(
                f"Invalid WAL checkpoint mode {mode!r}. Allowed modes: {allowed}"
            )

        if self._is_memory:
            return (0, 0, 0)
        busy, log_frames, checkpointed = 0, 0, 0
        with self.connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
                row = cursor.fetchone()
                if row is not None and len(row) >= 3:
                    busy = int(row[0])
                    log_frames = int(row[1])
                    checkpointed = int(row[2])
            finally:
                cursor.close()
        if busy != 0:
            logger.warning(
                "WAL checkpoint(%s) blocked by active readers (busy=%d, log=%d, checkpointed=%d)",
                normalized_mode,
                busy,
                log_frames,
                checkpointed,
            )
        self._ensure_secure_permissions()
        return (busy, log_frames, checkpointed)

    def get_size_bytes(self) -> int:
        """Return total disk size of the main database and WAL/SHM companion files."""
        if self._is_memory:
            return 0
        total = 0
        for suffix in ["", "-wal", "-shm"]:
            filepath = f"{self.db_path}{suffix}"
            if os.path.exists(filepath):
                total += os.path.getsize(filepath)
        return total

    def close(self) -> None:
        """Close all open thread-local connections and any shared memory connection."""
        with self._memory_lock:
            self._closed = True
            if self._shared_memory_conn is not None:
                try:
                    self._shared_memory_conn.close()
                except (sqlite3.Error, OSError):
                    pass
                self._shared_memory_conn = None
            self._memory_tx_depth = 0

        with self._conn_lock:
            self._closed = True
            conns = list(self._open_connections)
            self._open_connections.clear()
            self._thread_conns.clear()

        for conn in conns:
            try:
                conn.close()
            except (sqlite3.Error, OSError):
                pass

        if hasattr(self._local, "conn"):
            self._local.conn = None
        if hasattr(self._local, "tx_depth"):
            self._local.tx_depth = 0

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
