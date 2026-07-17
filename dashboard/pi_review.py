"""Pi-native review — verdict tracking with an airtight audit trail.

Standalone from the iMac-side review2 system per the post-split
guidance: each side runs its own review surface. We share nothing
with review2 — different DB file, different endpoints, different
table schema, no apply_verdict file-move side effects.

v2 (2026-07-07, the review port): the iMac's proven patterns arrive
without its baggage —
  - richer verdicts: yes / no (+optional correct_species) /
    not_a_bird / trash / skip
  - append-only pi_review_history + current-state pi_reviews cache,
    written in ONE transaction (the "airtight" pattern)
  - client_id idempotency (retry-safe writes; duplicate = no-op)
  - undo as an APPEND ('undone' row restoring the prior state),
    not a delete — provenance survives
  - GET /queue: keyset-cursor unreviewed queue enriched with
    also_heard (BirdNET corroboration ±30s from the Pi's own audio DB)

Deliberately NOT ported: apply_verdict file moves (classifications
rows + JPGs stay untouched — pure metadata), offset pagination, and
bulk writes that bypass history.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, HTTPException

# Pi paths. classifications.db is the existing pipeline-side DB; we
# read model_source from it but never write.
DB_PATH = Path.home() / "bird-snapshots" / "logs" / "pi_reviews.db"
CLASSIFICATIONS_DB_PATH = (
    Path.home() / "bird-snapshots" / "logs" / "classifications.db"
)
DEMO_CLASSIFICATIONS_DB_PATH = (
    Path.home() / "bird-snapshots" / "logs" / "classifications_demo.db"
)
BIRDNET_DB_PATH = (
    Path.home() / "bird-snapshots" / "birdnet-audio" / "birdnet_local.db"
)

VALID_VERDICTS = ("yes", "no", "not_a_bird", "trash", "skip")

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize_mode(mode: str | None) -> str:
    return "demo" if mode == "demo" else "live"


def _classifications_db_path(mode: str | None) -> Path:
    return DEMO_CLASSIFICATIONS_DB_PATH if _normalize_mode(mode) == "demo" else CLASSIFICATIONS_DB_PATH


def _lookup_model_source(filename: str, mode: str | None = "live") -> str | None:
    """Pull the classifier name (extra_json.model_source) from the
    pipeline's classifications.db. Returns None if the file isn't
    found or the lookup fails (e.g. DB locked) — caller stores NULL."""
    db_path = _classifications_db_path(mode)
    try:
        with sqlite3.connect(str(db_path), timeout=2.0) as c:
            c.row_factory = sqlite3.Row
            row = c.execute(
                "SELECT json_extract(extra_json, '$.model_source') AS m "
                "FROM classifications "
                "WHERE file = ? AND action = 'classified' LIMIT 1",
                (filename,),
            ).fetchone()
            return row["m"] if row else None
    except sqlite3.Error:
        return None


_REVIEWS_SCHEMA_V2 = """
    CREATE TABLE IF NOT EXISTS pi_reviews (
        file            TEXT PRIMARY KEY,
        verdict         TEXT NOT NULL CHECK (verdict IN
                            ('yes','no','not_a_bird','trash','skip')),
        correct_species TEXT NOT NULL DEFAULT '',
        reviewed_at     TEXT NOT NULL,
        source_mode     TEXT NOT NULL DEFAULT 'live',
        model_source    TEXT
    );
"""


def init_db() -> None:
    """Idempotent. Creates/migrates the v2 schema. Called at dashboard
    startup when PI_MODE=1.

    Migration: v1's CHECK only allowed ('yes','no'); SQLite can't alter a
    CHECK, so when the old constraint is detected the table is rebuilt
    (rename → create v2 → copy → drop). Existing rows all satisfy v2."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        old_sql = c.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='pi_reviews'"
        ).fetchone()
        v1_leftover = c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pi_reviews_v1'"
        ).fetchone()
        _COPY_V1 = (
            "INSERT OR IGNORE INTO pi_reviews "
            "    (file, verdict, correct_species, reviewed_at, "
            "     source_mode, model_source) "
            "SELECT file, verdict, '', reviewed_at, "
            "       COALESCE(source_mode, 'live'), model_source "
            "FROM pi_reviews_v1"
        )
        if old_sql and "'not_a_bird'" not in (old_sql["sql"] or ""):
            # Rebuild in ONE explicit transaction. executescript autocommits
            # between statements, so a crash after the RENAME but before the
            # copy would strand every verdict in pi_reviews_v1 and the next
            # startup would create an EMPTY cache. SQLite DDL is
            # transactional, so BEGIN IMMEDIATE ... COMMIT makes the whole
            # rebuild atomic.
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute("ALTER TABLE pi_reviews RENAME TO pi_reviews_v1")
                c.execute(_REVIEWS_SCHEMA_V2)
                c.execute(_COPY_V1)
                c.execute("DROP TABLE pi_reviews_v1")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        elif v1_leftover:
            # A crash under the old non-atomic migration left rows stranded
            # in pi_reviews_v1 — resume the copy now, atomically.
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(_REVIEWS_SCHEMA_V2)
                c.execute(_COPY_V1)
                c.execute("DROP TABLE pi_reviews_v1")
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise
        else:
            c.executescript(_REVIEWS_SCHEMA_V2)
        c.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_pi_reviews_at
                ON pi_reviews(reviewed_at);
            CREATE INDEX IF NOT EXISTS idx_pi_reviews_model
                ON pi_reviews(model_source);
            CREATE INDEX IF NOT EXISTS idx_pi_reviews_source_mode
                ON pi_reviews(source_mode);

            -- append-only audit trail (the airtight pattern)
            CREATE TABLE IF NOT EXISTS pi_review_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                file            TEXT NOT NULL,
                verdict         TEXT NOT NULL,
                correct_species TEXT NOT NULL DEFAULT '',
                source_mode     TEXT NOT NULL DEFAULT 'live',
                model_source    TEXT,
                client_id       TEXT,
                prev_row_id     INTEGER,
                created_at      TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_prh_file
                ON pi_review_history(file, id);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_prh_client
                ON pi_review_history(client_id)
                WHERE client_id IS NOT NULL;
            """
        )
        c.commit()


router = APIRouter(prefix="/api/pi-review", tags=["pi-review"])


def _upsert_cache(c, filename, verdict, correct_species, source_mode, model_source):
    c.execute(
        "INSERT INTO pi_reviews (file, verdict, correct_species, reviewed_at, "
        "                        source_mode, model_source) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(file) DO UPDATE SET "
        "    verdict = excluded.verdict, "
        "    correct_species = excluded.correct_species, "
        "    reviewed_at = excluded.reviewed_at, "
        "    source_mode = excluded.source_mode, "
        "    model_source = excluded.model_source",
        (filename, verdict, correct_species, _now_iso(), source_mode, model_source),
    )


@router.post("/undo/{history_id}")
def undo_review(history_id: int):
    """Undo by APPEND: write an 'undone' history row and restore the file's
    prior state (previous non-undone verdict, or unreviewed). Provenance is
    never deleted."""
    with _lock, _conn() as c:
        row = c.execute(
            "SELECT * FROM pi_review_history WHERE id = ?", (history_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="history row not found")
        if row["verdict"] in ("undone", "cleared"):
            raise HTTPException(status_code=400, detail="cannot undo an undo/clear")
        # Clobber guard: undo must target the file's LATEST standing verdict.
        # Undoing an older row would silently overwrite whatever verdict came
        # after it (e.g. keyboard-yes then button-no, then Z on the yes row).
        # A newer verdict that was itself already undone doesn't count.
        newer = c.execute(
            "SELECT h.id FROM pi_review_history h "
            "WHERE h.file = ? AND h.id > ? "
            "  AND h.verdict NOT IN ('undone', 'cleared') "
            "  AND NOT EXISTS (SELECT 1 FROM pi_review_history u "
            "                  WHERE u.verdict = 'undone' AND u.prev_row_id = h.id) "
            "ORDER BY h.id DESC LIMIT 1",
            (row["file"], history_id),
        ).fetchone()
        if newer is not None:
            raise HTTPException(
                status_code=409,
                detail=f"a newer verdict (history id {newer['id']}) exists for "
                       "this file — undo that one instead",
            )
        prior = c.execute(
            "SELECT * FROM pi_review_history "
            "WHERE file = ? AND id < ? AND verdict != 'undone' "
            "ORDER BY id DESC LIMIT 1",
            (row["file"], history_id),
        ).fetchone()
        c.execute(
            "INSERT INTO pi_review_history "
            "(file, verdict, correct_species, source_mode, model_source, "
            " client_id, prev_row_id, created_at) "
            "VALUES (?, 'undone', '', ?, ?, NULL, ?, ?)",
            (row["file"], row["source_mode"], row["model_source"],
             history_id, _now_iso()),
        )
        if prior is not None and prior["verdict"] != "cleared":
            _upsert_cache(c, row["file"], prior["verdict"],
                          prior["correct_species"], prior["source_mode"],
                          prior["model_source"])
            restored = prior["verdict"]
        else:
            c.execute("DELETE FROM pi_reviews WHERE file = ?", (row["file"],))
            restored = None
        c.commit()
    return {"ok": True, "file": row["file"], "restored_verdict": restored}


@router.get("/queue")
def review_queue(limit: int = 20, cursor: int | None = None, mode: str = "live"):
    """Unreviewed classifications, newest first, keyset-paginated
    (?cursor=<last id from the previous page>). Each item is enriched with
    also_heard: a same-species BirdNET detection within ±30s from the Pi's
    own audio DB — the strongest cheap trust signal a reviewer can get."""
    limit = max(1, min(limit, 100))
    source_mode = _normalize_mode(mode)
    cls_path = _classifications_db_path(source_mode)
    if not cls_path.exists():
        return {"items": [], "next_cursor": None, "mode": source_mode}
    with _lock, _conn() as c:
        reviewed = {r["file"] for r in c.execute(
            "SELECT file FROM pi_reviews WHERE source_mode = ?", (source_mode,))}
    items = []
    last_id = None
    scanned = 0
    try:
        with sqlite3.connect(str(cls_path), timeout=2.0) as cc:
            cc.row_factory = sqlite3.Row
            q = ("SELECT id, file, source_timestamp, common_name AS species, "
                 "confidence, json_extract(extra_json, '$.model_source') AS model_source "
                 "FROM classifications WHERE action = 'classified' ")
            args: list = []
            if cursor is not None:
                q += "AND id < ? "
                args.append(cursor)
            q += "ORDER BY id DESC LIMIT ?"
            args.append(limit * 3)          # oversample; reviewed rows filtered out
            for r in cc.execute(q, args):
                scanned += 1
                last_id = r["id"]
                if r["file"] in reviewed:
                    continue
                items.append(dict(r))
                if len(items) >= limit:
                    break
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"classifications.db: {e}")
    _enrich_also_heard(items)
    # Keep paging whenever the scan window was exhausted, even if every
    # scanned row was already reviewed (items empty) — otherwise a fully
    # reviewed window dead-ends the queue while older unreviewed rows exist.
    # None only when the SQL itself ran out of rows.
    next_cursor = (items[-1]["id"] if len(items) >= limit
                   else last_id if scanned >= limit * 3 else None)
    return {"items": items, "next_cursor": next_cursor, "mode": source_mode}


def _enrich_also_heard(items: list) -> None:
    """Set item['also_heard'] when the audio DB heard the same species within
    ±30s of the classification. Tolerant: silently no-ops if the audio DB is
    missing or the timestamps don't parse."""
    if not items or not BIRDNET_DB_PATH.exists():
        for it in items:
            it["also_heard"] = False
        return
    try:
        with sqlite3.connect(str(BIRDNET_DB_PATH), timeout=1.5) as bc:
            bc.row_factory = sqlite3.Row
            for it in items:
                it["also_heard"] = False
                ts = it.get("source_timestamp") or ""
                sp = (it.get("species") or "").lower()
                # source_timestamp like 'YYYY-MM-DD HH:MM:SS' (or ISO 'T')
                ts = ts.replace("T", " ")
                if len(ts) < 19 or not sp:
                    continue
                d, t = ts[:10], ts[11:19]
                row = bc.execute(
                    "SELECT 1 FROM notes WHERE date = ? AND lower(common_name) = ? "
                    "AND abs(strftime('%s', ?) - strftime('%s', time)) <= 30 LIMIT 1",
                    (d, sp, t),
                ).fetchone()
                it["also_heard"] = row is not None
    except sqlite3.Error:
        for it in items:
            it.setdefault("also_heard", False)


@router.post("/{filename}")
def post_verdict(filename: str, body: dict = Body(...), mode: str = "live"):
    """Record a verdict. Body: {verdict, correct_species?, client_id?}.
    verdict ∈ yes|no|not_a_bird|trash|skip. correct_species only meaningful
    with 'no'. client_id makes the write idempotent (safe retries)."""
    source_mode = _normalize_mode(mode)
    verdict = body.get("verdict")
    if verdict not in VALID_VERDICTS:
        raise HTTPException(
            status_code=400,
            detail=f"verdict must be one of {', '.join(VALID_VERDICTS)}",
        )
    correct_species = (body.get("correct_species") or "").strip()
    client_id = body.get("client_id") or None
    model_source = _lookup_model_source(filename, source_mode)
    with _lock, _conn() as c:
        if client_id is not None:
            dup = c.execute(
                "SELECT id FROM pi_review_history WHERE client_id = ?",
                (client_id,),
            ).fetchone()
            if dup is not None:
                return {"ok": True, "file": filename, "verdict": verdict,
                        "history_id": dup["id"], "duplicate": True,
                        "source_mode": source_mode, "model_source": model_source}
        prev = c.execute(
            "SELECT id FROM pi_review_history WHERE file = ? "
            "ORDER BY id DESC LIMIT 1", (filename,),
        ).fetchone()
        cur = c.execute(
            "INSERT INTO pi_review_history "
            "(file, verdict, correct_species, source_mode, model_source, "
            " client_id, prev_row_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (filename, verdict, correct_species, source_mode, model_source,
             client_id, prev["id"] if prev else None, _now_iso()),
        )
        _upsert_cache(c, filename, verdict, correct_species, source_mode, model_source)
        c.commit()
        history_id = cur.lastrowid
    return {
        "ok": True,
        "file": filename,
        "verdict": verdict,
        "correct_species": correct_species,
        "history_id": history_id,
        "duplicate": False,
        "source_mode": source_mode,
        "model_source": model_source,
    }


@router.delete("/{filename}")
def clear_verdict(filename: str, mode: str = "live"):
    """Toggle a verdict off. Appends a 'cleared' history row (the audit
    trail stays append-only — provenance survives) and drops the cache row
    so the file reads as unreviewed again."""
    source_mode = _normalize_mode(mode)
    with _lock, _conn() as c:
        cur = c.execute(
            "DELETE FROM pi_reviews WHERE file = ? AND source_mode = ?",
            (filename, source_mode),
        )
        deleted = cur.rowcount
        if deleted:
            prev = c.execute(
                "SELECT id FROM pi_review_history WHERE file = ? "
                "ORDER BY id DESC LIMIT 1", (filename,),
            ).fetchone()
            c.execute(
                "INSERT INTO pi_review_history "
                "(file, verdict, correct_species, source_mode, model_source, "
                " client_id, prev_row_id, created_at) "
                "VALUES (?, 'cleared', '', ?, NULL, NULL, ?, ?)",
                (filename, source_mode, prev["id"] if prev else None, _now_iso()),
            )
        c.commit()
    return {"ok": True, "file": filename, "source_mode": source_mode, "deleted": deleted}


@router.get("/recent")
def recent_classifications(limit: int = 8, mode: str = "live"):
    """Last N rows from classifications.db, joined with their
    pi_reviews verdict (None if unreviewed). Drives the Recent
    Classifications strip on the Pi dashboard.

    The dashboard's "Load more" affordance bumps `limit` in 8-card
    increments — bumped the cap to 400 so a focused review session
    can burn through a whole afternoon's classifications without
    paging the API."""
    if limit < 1:
        limit = 1
    if limit > 400:
        limit = 400
    source_mode = _normalize_mode(mode)
    classifications_db_path = _classifications_db_path(source_mode)
    if not classifications_db_path.exists():
        return {"items": [], "mode": source_mode}
    rows = []
    try:
        with sqlite3.connect(str(classifications_db_path), timeout=2.0) as cls_c:
            cls_c.row_factory = sqlite3.Row
            for r in cls_c.execute(
                "SELECT file, source_timestamp, common_name AS species, "
                "       confidence, "
                "       json_extract(extra_json, '$.model_source') AS model_source "
                "FROM classifications "
                "WHERE action = 'classified' "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ):
                rows.append(dict(r))
    except sqlite3.Error as e:
        raise HTTPException(status_code=500, detail=f"classifications.db: {e}")

    if rows:
        files = [r["file"] for r in rows]
        with _lock, _conn() as c:
            placeholders = ",".join("?" * len(files))
            verdicts = {
                row["file"]: dict(row)
                for row in c.execute(
                    "SELECT file, verdict, reviewed_at "
                    f"FROM pi_reviews WHERE source_mode = ? AND file IN ({placeholders})",
                    [source_mode, *files],
                )
            }
    else:
        verdicts = {}

    for r in rows:
        v = verdicts.get(r["file"])
        r["verdict"] = v["verdict"] if v else None
        r["reviewed_at"] = v["reviewed_at"] if v else None

    return {"items": rows, "mode": source_mode}


@router.get("/stats")
def review_stats(mode: str = "live"):
    """Accuracy summary by classifier model_source. The Pi dashboard
    surfaces this above the Recent Classifications strip so the user
    can see at-a-glance how AIY (or whichever classifier is active)
    is doing."""
    source_mode = _normalize_mode(mode)
    with _lock, _conn() as c:
        rows = list(
            c.execute(
                "SELECT COALESCE(model_source, 'unknown') AS model_source, "
                "       SUM(CASE WHEN verdict = 'yes' THEN 1 ELSE 0 END) AS yes_n, "
                "       SUM(CASE WHEN verdict IN ('no','not_a_bird') THEN 1 ELSE 0 END) AS no_n, "
                "       COUNT(*) AS total "
                "FROM pi_reviews "
                "WHERE source_mode = ? "
                "GROUP BY model_source "
                "ORDER BY total DESC",
                (source_mode,),
            )
        )
    by_model = []
    grand_total = 0
    grand_yes = 0
    grand_judged = 0
    for r in rows:
        n = int(r["total"] or 0)
        y = int(r["yes_n"] or 0)
        no = int(r["no_n"] or 0)
        judged = y + no          # skip/trash carry no accuracy signal
        grand_total += n
        grand_yes += y
        grand_judged += judged
        by_model.append(
            {
                "model_source": r["model_source"],
                "yes": y,
                "no": no,
                "total": n,
                "accuracy": (y / judged) if judged > 0 else 0.0,
            }
        )
    overall_acc = (grand_yes / grand_judged) if grand_judged > 0 else 0.0
    return {
        "mode": source_mode,
        "total_reviewed": grand_total,
        "overall_accuracy": overall_acc,
        "by_model": by_model,
    }
