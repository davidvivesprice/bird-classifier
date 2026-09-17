"""incremental_vacuum must drain its result cursor: SQLite emits one row per
freed page and sqlite3.execute() steps only once, so an undrained call frees
a single 4 KB page (pipeline.db sat at 82 % freelist for two months)."""
import sqlite3

from pipeline.event_store import EventStore


def _freelist(path):
    c = sqlite3.connect(path)
    n = c.execute("PRAGMA freelist_count").fetchone()[0]
    c.close()
    return n


def test_incremental_vacuum_frees_many_pages(tmp_path):
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    with store._conn_lock:
        # auto_vacuum=INCREMENTAL (set in __init__) only takes effect once the
        # file has been VACUUMed — production got that on 2026-07-18.
        store.conn.execute("VACUUM")
        store.conn.execute("CREATE TABLE junk (blob BLOB)")
        store.conn.executemany("INSERT INTO junk VALUES (?)",
                               [(b"x" * 4000,) for _ in range(3000)])
        store.conn.commit()
        store.conn.execute("DELETE FROM junk")
        store.conn.commit()
    store.daily_checkpoint()
    before = _freelist(str(db))
    assert before > 1000, f"test setup: expected a large freelist, got {before}"

    store.incremental_vacuum(pages=5000)
    store.daily_checkpoint()
    after = _freelist(str(db))
    assert after <= before - 1000, f"freed only {before - after} pages ({before}->{after})"
    store.shutdown()
