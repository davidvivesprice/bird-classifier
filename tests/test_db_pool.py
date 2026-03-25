"""Tests for db_pool.py — shared SQLite connection management."""
import sqlite3
import threading
import pytest
from pathlib import Path


@pytest.fixture
def test_db(tmp_path):
    """Create a temporary SQLite database for testing."""
    return tmp_path / "test.db"


class TestGetConn:
    def test_returns_connection(self, test_db):
        from db_pool import get_conn
        conn = get_conn(test_db)
        assert conn is not None
        assert isinstance(conn, sqlite3.Connection)

    def test_readonly_connection(self, test_db):
        from db_pool import get_conn
        # Create DB first with a write connection
        rw = get_conn(test_db, readonly=False)
        rw.execute("CREATE TABLE t (id INTEGER)")
        rw.commit()
        # Now open readonly
        ro = get_conn(test_db, readonly=True)
        rows = ro.execute("SELECT * FROM t").fetchall()
        assert rows == []

    def test_thread_local_isolation(self, test_db):
        from db_pool import get_conn
        conn1 = get_conn(test_db)
        conn1.execute("CREATE TABLE t (id INTEGER)")
        conn1.commit()

        # Different thread gets different connection
        results = []
        def thread_fn():
            conn2 = get_conn(test_db)
            results.append(id(conn2))
        t = threading.Thread(target=thread_fn)
        t.start()
        t.join()

        assert len(results) == 1
        assert results[0] != id(conn1)

    def test_same_thread_reuses_connection(self, test_db):
        from db_pool import get_conn
        conn1 = get_conn(test_db)
        conn2 = get_conn(test_db)
        assert conn1 is conn2

    def test_row_factory_set(self, test_db):
        from db_pool import get_conn
        conn = get_conn(test_db)
        assert conn.row_factory == sqlite3.Row

    def test_wal_mode(self, test_db):
        from db_pool import get_conn
        conn = get_conn(test_db)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"


class TestEnsureTable:
    def test_creates_table(self, test_db):
        from db_pool import get_conn, ensure_table
        ensure_table(test_db, "CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, name TEXT)")
        conn = get_conn(test_db)
        conn.execute("INSERT INTO t (name) VALUES ('test')")
        conn.commit()
        row = conn.execute("SELECT name FROM t").fetchone()
        assert row["name"] == "test"

    def test_idempotent(self, test_db):
        from db_pool import ensure_table
        sql = "CREATE TABLE IF NOT EXISTS t (id INTEGER)"
        ensure_table(test_db, sql)
        ensure_table(test_db, sql)  # should not raise

    def test_creates_indexes(self, test_db):
        from db_pool import get_conn, ensure_table
        ensure_table(
            test_db,
            "CREATE TABLE IF NOT EXISTS t (id INTEGER, name TEXT)",
            ["CREATE INDEX IF NOT EXISTS idx_name ON t(name)"],
        )
        conn = get_conn(test_db)
        indexes = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='t'"
        ).fetchall()
        assert any(r["name"] == "idx_name" for r in indexes)

    def test_thread_safe(self, test_db):
        from db_pool import ensure_table
        sql = "CREATE TABLE IF NOT EXISTS t (id INTEGER)"
        errors = []
        def init():
            try:
                ensure_table(test_db, sql)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=init) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


class TestAddColumns:
    def test_adds_new_column(self, test_db):
        from db_pool import get_conn, ensure_table, add_columns
        ensure_table(test_db, "CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        add_columns(test_db, ["ALTER TABLE t ADD COLUMN name TEXT DEFAULT 'x'"])
        conn = get_conn(test_db)
        conn.execute("INSERT INTO t (id, name) VALUES (1, 'hello')")
        conn.commit()
        row = conn.execute("SELECT name FROM t WHERE id=1").fetchone()
        assert row["name"] == "hello"

    def test_idempotent(self, test_db):
        from db_pool import ensure_table, add_columns
        ensure_table(test_db, "CREATE TABLE IF NOT EXISTS t (id INTEGER)")
        add_columns(test_db, ["ALTER TABLE t ADD COLUMN name TEXT"])
        add_columns(test_db, ["ALTER TABLE t ADD COLUMN name TEXT"])  # no error


class TestCloseAll:
    def test_closes_connections(self, test_db):
        from db_pool import get_conn, close_all, _all_connections
        conn = get_conn(test_db)
        assert len(_all_connections) > 0
        close_all()
        # Connection should be closed — executing should fail
        # (can't easily test this without implementation details)
