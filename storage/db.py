"""
Database connection factory and transaction manager.
"""

import contextlib
from datetime import datetime
import logging
import os
import sqlite3
import threading
from typing import Any, Dict, Generator, List, Optional, Set, Tuple

from storage.schema import SchemaManager

logger = logging.getLogger("railmon." + __name__)

_WAL_CHECKPOINT_SQL: Dict[str, str] = {
    "PASSIVE": "PRAGMA wal_checkpoint(PASSIVE);",
    "FULL": "PRAGMA wal_checkpoint(FULL);",
    "RESTART": "PRAGMA wal_checkpoint(RESTART);",
    "TRUNCATE": "PRAGMA wal_checkpoint(TRUNCATE);",
}


def _convert_timestamp(val: bytes) -> Any:
    """Safely convert SQLite TIMESTAMP bytes to datetime or string."""
    s = val.decode("utf-8")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        pass
    try:
        return datetime.fromisoformat(s.replace(" ", "T"))
    except (ValueError, TypeError):
        return s


sqlite3.register_converter("TIMESTAMP", _convert_timestamp)
sqlite3.register_converter("timestamp", _convert_timestamp)


class Database:
    """
    High-performance SQLite database connection coordinator.
    Configures Write-Ahead Logging (WAL) and memory pragmas for high-throughput ingestion.
    """

    def __init__(self, db_path: str = "railway_history.db", auto_migrate: bool = True) -> None:
        self.db_path = db_path
        self._is_memory = (db_path == ":memory:") or db_path.startswith("file::memory:")

        self._local = threading.local()
        self._conn_lock = threading.Lock()
        self._open_connections: Set[sqlite3.Connection] = set()
        self._memory_lock = threading.RLock()

        # For in-memory databases, maintain persistent connection to prevent schema teardown
        self._shared_memory_conn: Optional[sqlite3.Connection] = None
        if self._is_memory:
            self._shared_memory_conn = self._create_connection()

        if auto_migrate:
            try:
                with self.connection() as conn:
                    SchemaManager.initialize_schema(conn)
            except Exception:
                self.close()
                raise

        self._ensure_secure_permissions()

    def _ensure_secure_permissions(self) -> None:
        """Enforce owner-only permissions (0600) on POSIX database and WAL companion files."""
        if self._is_memory or os.name == "nt":
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
        with self.connection() as conn:
            return SchemaManager.initialize_schema(conn)

    def verify_integrity(self) -> Tuple[bool, List[str]]:
        """Run PRAGMA integrity_check and foreign_key_check on the database."""
        with self.connection() as conn:
            return SchemaManager.verify_integrity(conn)

    def optimize(self) -> None:
        """Run PRAGMA optimize on the active connection."""
        with self.connection() as conn:
            SchemaManager.optimize(conn)

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection configured with optimal ingestion pragmas."""
        if not self._is_memory and os.name != "nt":
            old_umask = os.umask(0o077)
            try:
                parent = os.path.dirname(os.path.abspath(self.db_path))
                if parent and not os.path.exists(parent):
                    os.makedirs(parent, exist_ok=True)

                conn = sqlite3.connect(
                    self.db_path,
                    timeout=5.0,
                    detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
                    check_same_thread=False,
                )
            finally:
                os.umask(old_umask)
        else:
            if not self._is_memory:
                parent = os.path.dirname(os.path.abspath(self.db_path))
                if parent and not os.path.exists(parent):
                    os.makedirs(parent, exist_ok=True)

            conn = sqlite3.connect(
                self.db_path,
                timeout=5.0,
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

    def _get_thread_connection(self) -> sqlite3.Connection:
        """Return the current thread's persistent SQLite connection, creating it if needed."""
        conn: Optional[sqlite3.Connection] = getattr(self._local, "conn", None)
        with self._conn_lock:
            if conn is not None and conn in self._open_connections:
                return conn

        conn = self._create_connection()
        with self._conn_lock:
            self._open_connections.add(conn)
        self._local.conn = conn
        return conn

    @contextlib.contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager yielding an active thread-scoped SQLite connection."""
        if self._is_memory:
            with self._memory_lock:
                if self._shared_memory_conn is None:
                    self._shared_memory_conn = self._create_connection()
                yield self._shared_memory_conn
        else:
            conn = self._get_thread_connection()
            yield conn

    @contextlib.contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for atomic write transactions using BEGIN IMMEDIATE."""
        with self.connection() as conn:
            cursor = conn.cursor()
            try:
                if conn.in_transaction:
                    conn.commit()
                cursor.execute("BEGIN IMMEDIATE;")
                try:
                    yield cursor
                except Exception:
                    if conn.in_transaction:
                        conn.rollback()
                    raise
                else:
                    if conn.in_transaction:
                        conn.commit()
            finally:
                cursor.close()

    def checkpoint_wal(self, mode: str = "PASSIVE") -> None:
        """Trigger WAL checkpoint to write changes to base file (PASSIVE, FULL, RESTART, TRUNCATE)."""
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
            return
        with self.connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql)
            finally:
                cursor.close()
        self._ensure_secure_permissions()

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
            if self._shared_memory_conn is not None:
                try:
                    self._shared_memory_conn.close()
                except Exception:
                    pass
                self._shared_memory_conn = None

        with self._conn_lock:
            conns = list(self._open_connections)
            self._open_connections.clear()

        for conn in conns:
            try:
                conn.close()
            except Exception:
                pass

        if hasattr(self._local, "conn"):
            self._local.conn = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
