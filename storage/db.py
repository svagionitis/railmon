"""
Database connection factory and transaction manager.
"""

import contextlib
from datetime import datetime
import os
import sqlite3
from typing import Any, Generator, Optional

from storage.schema import SchemaManager


def _convert_timestamp(val: bytes) -> Any:
    """Safely convert SQLite TIMESTAMP bytes to datetime or string."""
    s = val.decode("utf-8")
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        pass
    try:
        return datetime.fromisoformat(s.replace(" ", "T"))
    except Exception:
        return s


sqlite3.register_converter("TIMESTAMP", _convert_timestamp)
sqlite3.register_converter("timestamp", _convert_timestamp)


class Database:
    """
    High-performance SQLite database connection coordinator.
    Configures Write-Ahead Logging (WAL) and memory pragmas for high-throughput ingestion.
    """

    def __init__(self, db_path: str = "railway_history.db", auto_migrate: bool = True):
        self.db_path = db_path
        self._is_memory = (db_path == ":memory:") or db_path.startswith("file::memory:")

        # For in-memory databases, maintain persistent connection to prevent schema teardown
        self._shared_memory_conn: Optional[sqlite3.Connection] = None
        if self._is_memory:
            self._shared_memory_conn = self._create_connection()

        if auto_migrate:
            with self.connection() as conn:
                SchemaManager.initialize_schema(conn)

    def init_schema(self) -> None:
        """Explicitly initialize all required database tables and indices."""
        with self.connection() as conn:
            SchemaManager.initialize_schema(conn)

    def _create_connection(self) -> sqlite3.Connection:
        """Create a new SQLite connection configured with optimal ingestion pragmas."""
        if not self._is_memory:
            parent = os.path.dirname(os.path.abspath(self.db_path))
            if parent and not os.path.exists(parent):
                os.makedirs(parent, exist_ok=True)

        conn = sqlite3.connect(
            self.db_path,
            timeout=30.0,
            detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
        )
        conn.row_factory = sqlite3.Row

        cursor = conn.cursor()
        try:
            if not self._is_memory:
                cursor.execute("PRAGMA journal_mode = WAL;")
                cursor.execute("PRAGMA synchronous = NORMAL;")
            cursor.execute("PRAGMA foreign_keys = ON;")
            cursor.execute("PRAGMA temp_store = MEMORY;")
            cursor.execute("PRAGMA cache_size = -64000;")  # 64MB cache
        finally:
            cursor.close()

        return conn

    @contextlib.contextmanager
    def connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Context manager yielding an active SQLite connection."""
        if self._is_memory and self._shared_memory_conn is not None:
            yield self._shared_memory_conn
        else:
            conn = self._create_connection()
            try:
                yield conn
            finally:
                conn.close()

    @contextlib.contextmanager
    def transaction(self) -> Generator[sqlite3.Cursor, None, None]:
        """Context manager for atomic transactions (commits on success, rolls back on error)."""
        with self.connection() as conn:
            cursor = conn.cursor()
            try:
                with conn:  # Context manager on conn manages BEGIN / COMMIT / ROLLBACK
                    yield cursor
            finally:
                cursor.close()

    def checkpoint_wal(self, mode: str = "PASSIVE") -> None:
        """Trigger WAL checkpoint to write changes to base file (PASSIVE, FULL, RESTART, TRUNCATE)."""
        if self._is_memory:
            return
        with self.connection() as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(f"PRAGMA wal_checkpoint({mode});")
            finally:
                cursor.close()

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
        """Close shared memory connection if open."""
        if self._shared_memory_conn is not None:
            self._shared_memory_conn.close()
            self._shared_memory_conn = None
