"""db_pool — Shared SQLite connection pool for the bird observatory.

Provides thread-local connection management with consistent PRAGMA setup,
proper lifecycle handling, and table initialization safety.

All database modules (classifications_db, visits_db, reviews_db, birdnet_db)
should use this instead of managing their own connections.

Fixes addressed:
- _table_ensured race condition (thread-safe with lock)
- Connections never closed (atexit cleanup)
- Stale readonly data (isolation_level=None for autocommit reads)
- Health check leak (old connection closed on failure)
- Consistent PRAGMA setup across all modules
"""

import atexit
import logging
import sqlite3
import threading
from pathlib import Path

log = logging.getLogger(__name__)

# Default database paths
CLASSIFICATIONS_DB = Path("/Users/vives/bird-snapshots/logs/classifications.db")
BIRDNET_DB = Path("/Users/vives/bird-snapshots/birdnet-audio/birdnet_local.db")

# Thread-local storage for connections
_local = threading.local()

# Track all connections for cleanup
_all_connections = []
_all_connections_lock = threading.Lock()

# Table initialization locks (one per DB path)
_init_locks = {}
_init_locks_lock = threading.Lock()
_initialized_tables = set()  # (db_path, table_name) pairs


def _get_init_lock(db_path):
    """Get or create a lock for table initialization on a specific DB."""
    with _init_locks_lock:
        key = str(db_path)
        if key not in _init_locks:
            _init_locks[key] = threading.Lock()
        return _init_locks[key]


def get_conn(db_path=CLASSIFICATIONS_DB, readonly=False):
    """Get a thread-local SQLite connection.

    Args:
        db_path: Path to the SQLite database file
        readonly: If True, opens in read-only mode with autocommit
                  (each SELECT sees latest data, no stale snapshots)

    Returns:
        sqlite3.Connection with row_factory=sqlite3.Row
    """
    # Build a unique attribute name per DB + mode
    safe_name = str(db_path).replace("/", "_").replace(".", "_")
    attr = f"_conn_{safe_name}_{'ro' if readonly else 'rw'}"

    conn = getattr(_local, attr, None)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            # Connection is dead — close it properly and create a new one
            try:
                conn.close()
            except Exception:
                pass
            conn = None

    # Create new connection
    uri = f"file:{db_path}"
    if readonly:
        uri += "?mode=ro"

    conn = sqlite3.connect(
        uri, uri=True, timeout=10,
        # Autocommit for readonly connections prevents stale snapshots
        isolation_level=None if readonly else "",
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    if readonly:
        conn.execute("PRAGMA query_only=ON")

    setattr(_local, attr, conn)

    # Track for cleanup
    with _all_connections_lock:
        _all_connections.append(conn)

    return conn


def ensure_table(db_path, create_sql, index_sqls=None):
    """Thread-safe table initialization. Idempotent — safe to call multiple times.

    Args:
        db_path: Path to the database
        create_sql: CREATE TABLE IF NOT EXISTS statement
        index_sqls: Optional list of CREATE INDEX IF NOT EXISTS statements
    """
    # Extract table name for tracking
    key = (str(db_path), create_sql[:100])
    if key in _initialized_tables:
        return

    lock = _get_init_lock(db_path)
    with lock:
        if key in _initialized_tables:
            return  # double-check after acquiring lock

        conn = get_conn(db_path, readonly=False)
        conn.execute(create_sql)
        if index_sqls:
            for sql in index_sqls:
                conn.execute(sql)
        conn.commit()
        _initialized_tables.add(key)


def add_columns(db_path, column_sqls):
    """Idempotent column addition. Ignores errors for existing columns.

    Args:
        db_path: Path to the database
        column_sqls: List of ALTER TABLE ADD COLUMN statements
    """
    conn = get_conn(db_path, readonly=False)
    for sql in column_sqls:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    conn.commit()


def close_all():
    """Close all tracked connections. Call on shutdown."""
    with _all_connections_lock:
        for conn in _all_connections:
            try:
                conn.close()
            except Exception:
                pass
        _all_connections.clear()
    log.info("All database connections closed")


# Register cleanup on process exit
atexit.register(close_all)
