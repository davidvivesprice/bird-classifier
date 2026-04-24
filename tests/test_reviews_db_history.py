"""Tests for reviews_db's airtight additions: review_history table,
client_id idempotency, prev_row_id chaining, undo.

Existing insert_review() and get_review() behaviors MUST continue to
work — those are covered by tests/test_reviews_db.py. This file only
tests the new invariants.

Plan: docs/superpowers/specs/2026-04-23-airtight-review-system.md
"""
import sqlite3
import threading
import pytest

import reviews_db


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    """Isolated in-memory DB with both reviews AND review_history tables."""
    uri = "file:test_reviews_history?mode=memory&cache=shared"
    setup_conn = sqlite3.connect(uri, uri=True)
    # classifications isn't used directly here but apply_verdict touches it
    setup_conn.execute("""
        CREATE TABLE IF NOT EXISTS classifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file TEXT UNIQUE NOT NULL,
            action TEXT NOT NULL DEFAULT 'classified',
            common_name TEXT,
            timestamp TEXT NOT NULL DEFAULT '2026-01-01T00:00:00'
        )
    """)
    setup_conn.commit()

    reviews_db._reset_table_flag()

    local = threading.local()

    def fake_get_conn(readonly=False):
        attr = "_test_ro" if readonly else "_test_rw"
        conn = getattr(local, attr, None)
        if conn is not None:
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                conn = None

        conn = sqlite3.connect(uri, uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
        setattr(local, attr, conn)

        # Create both tables + both sets of indexes
        conn.execute(reviews_db.CREATE_TABLE)
        for idx in reviews_db.INDEXES:
            conn.execute(idx)
        conn.execute(reviews_db.CREATE_HISTORY_TABLE)
        for idx in reviews_db.HISTORY_INDEXES:
            conn.execute(idx)
        conn.commit()
        return conn

    monkeypatch.setattr(reviews_db, "get_conn", fake_get_conn)
    yield setup_conn

    setup_conn.execute("DROP TABLE IF EXISTS reviews")
    setup_conn.execute("DROP TABLE IF EXISTS review_history")
    setup_conn.execute("DROP TABLE IF EXISTS classifications")
    setup_conn.commit()
    setup_conn.close()


# ── Schema ─────────────────────────────────────────────────────────────


def test_history_table_has_required_columns(in_memory_db):
    conn = reviews_db.get_conn(readonly=False)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(review_history)")}
    required = {"id", "file", "verdict", "correct_species", "bird_index",
                "missed_birds", "reviewer", "timestamp",
                "prev_row_id", "client_id"}
    assert required.issubset(cols), f"missing: {required - cols}"


def test_history_has_unique_index_on_client_id(in_memory_db):
    """client_id unique where not null — enables idempotency."""
    conn = reviews_db.get_conn(readonly=False)
    idx_rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='review_history'"
    ).fetchall()
    names = {r[0] for r in idx_rows}
    assert "idx_rh_client" in names


# ── insert_review: existing behaviors preserved ────────────────────────


def test_existing_insert_review_still_works_without_client_id(in_memory_db):
    """Backward compat — callers that don't know about client_id keep working."""
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00",
    })
    rev = reviews_db.get_review("a.jpg")
    assert rev is not None
    assert rev["verdict"] == "correct"


def test_insert_review_also_appends_history_row(in_memory_db):
    """Even without a client_id, every insert APPENDS to history.
    This is the audit trail — never silently lost."""
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00",
    })
    conn = reviews_db.get_conn(readonly=True)
    count = conn.execute(
        "SELECT COUNT(*) FROM review_history WHERE file='a.jpg'"
    ).fetchone()[0]
    assert count == 1


def test_second_insert_without_client_id_appends_another_history_row(in_memory_db):
    """No client_id = no idempotency. Two separate legitimate reviews
    should produce two history rows, reviews reflects the latest."""
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00",
    })
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "wrong", "correct_species": "Downy Woodpecker",
        "timestamp": "2026-04-24T10:05:00",
    })
    conn = reviews_db.get_conn(readonly=True)
    hist_rows = conn.execute(
        "SELECT verdict FROM review_history WHERE file='a.jpg' ORDER BY id"
    ).fetchall()
    assert [r[0] for r in hist_rows] == ["correct", "wrong"]
    current = reviews_db.get_review("a.jpg")
    assert current["verdict"] == "wrong"
    assert current["correct_species"] == "Downy Woodpecker"


# ── client_id idempotency ─────────────────────────────────────────────


def test_same_client_id_twice_does_not_duplicate_history(in_memory_db):
    """The property David specifically asked for: a double-submit with
    the same client_id is a no-op. No lost corrections, no doubled entries."""
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00",
        "client_id": "session-1-card-7",
    })
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "trash",  # different verdict!
        "timestamp": "2026-04-24T10:00:01",
        "client_id": "session-1-card-7",      # same client_id
    })
    conn = reviews_db.get_conn(readonly=True)
    hist_count = conn.execute(
        "SELECT COUNT(*) FROM review_history WHERE file='a.jpg'"
    ).fetchone()[0]
    assert hist_count == 1, "duplicate client_id must not append twice"
    # Reviews reflects the FIRST write (not the ignored second)
    current = reviews_db.get_review("a.jpg")
    assert current["verdict"] == "correct"


def test_different_client_ids_on_same_file_both_recorded(in_memory_db):
    """User reviews bird, later genuinely changes mind. Both go in history."""
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00", "client_id": "c1",
    })
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "wrong", "correct_species": "House Finch",
        "timestamp": "2026-04-24T10:05:00", "client_id": "c2",
    })
    conn = reviews_db.get_conn(readonly=True)
    hist_count = conn.execute(
        "SELECT COUNT(*) FROM review_history WHERE file='a.jpg'"
    ).fetchone()[0]
    assert hist_count == 2


# ── prev_row_id chaining ──────────────────────────────────────────────


def test_first_history_row_has_null_prev(in_memory_db):
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00",
    })
    conn = reviews_db.get_conn(readonly=True)
    prev = conn.execute(
        "SELECT prev_row_id FROM review_history WHERE file='a.jpg'"
    ).fetchone()[0]
    assert prev is None


def test_second_history_row_points_at_first(in_memory_db):
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00",
    })
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "trash",
        "timestamp": "2026-04-24T10:05:00",
    })
    conn = reviews_db.get_conn(readonly=True)
    rows = conn.execute(
        "SELECT id, prev_row_id FROM review_history WHERE file='a.jpg' ORDER BY id"
    ).fetchall()
    first_id = rows[0][0]
    second_prev = rows[1][1]
    assert second_prev == first_id


# ── get_history ─────────────────────────────────────────────────────────


def test_get_history_returns_chronological(in_memory_db):
    for verdict in ["correct", "wrong", "correct"]:
        reviews_db.insert_review({
            "file": "a.jpg", "verdict": verdict,
            "correct_species": "House Finch" if verdict == "wrong" else "",
            "timestamp": "2026-04-24T10:00:00",
        })
    history = reviews_db.get_history("a.jpg")
    assert len(history) == 3
    assert [h["verdict"] for h in history] == ["correct", "wrong", "correct"]


def test_get_history_empty_for_unreviewed_file(in_memory_db):
    history = reviews_db.get_history("never-seen.jpg")
    assert history == []


# ── undo ────────────────────────────────────────────────────────────────


def test_undo_appends_undone_entry_and_reverts_current_state(in_memory_db):
    reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00", "client_id": "c1",
    })
    r2 = reviews_db.insert_review({
        "file": "a.jpg", "verdict": "trash",
        "timestamp": "2026-04-24T10:05:00", "client_id": "c2",
    })
    result = reviews_db.undo(r2["history_id"], client_id="undo-1")
    assert result["ok"]

    conn = reviews_db.get_conn(readonly=True)
    verdicts = [r[0] for r in conn.execute(
        "SELECT verdict FROM review_history WHERE file='a.jpg' ORDER BY id"
    ).fetchall()]
    assert verdicts == ["correct", "trash", "undone"]

    # Reviews now reflects the state BEFORE the trash — i.e. correct
    current = reviews_db.get_review("a.jpg")
    assert current["verdict"] == "correct"


def test_undo_of_first_review_clears_current(in_memory_db):
    r1 = reviews_db.insert_review({
        "file": "a.jpg", "verdict": "correct",
        "timestamp": "2026-04-24T10:00:00", "client_id": "c1",
    })
    reviews_db.undo(r1["history_id"], client_id="undo-1")
    # With only an undone entry, the file effectively has no current review.
    current = reviews_db.get_review("a.jpg")
    assert current is None or current.get("verdict") == "undone"
    # The caller decides what "undone" means for their UX; the important
    # thing is history shows correct→undone and pagination can act on it.
