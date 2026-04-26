"""Pi-native review — simple yes/no verdict tracking.

Standalone from the iMac-side review2 system per the post-split
guidance: each side runs its own review surface. We share nothing
with review2 — different DB file, different endpoints, different
table schema, no apply_verdict file-move side effects.

Mission: David clicks ✓ or ✗ on Recent Classifications cards. We
record the verdict + which classifier produced the row at click
time, so per-model accuracy stays stable over the system's lifetime.

Schema:
    pi_reviews(file PRIMARY KEY, verdict CHECK in ('yes','no'),
               reviewed_at, model_source)

One verdict per file — UPSERT semantics (re-clicking ✗ after ✓
overwrites). The classifications.db row stays untouched; the JPG
stays in place. Pure metadata.
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

_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(str(DB_PATH), timeout=5.0)
    c.row_factory = sqlite3.Row
    return c


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _lookup_model_source(filename: str) -> str | None:
    """Pull the classifier name (extra_json.model_source) from the
    pipeline's classifications.db. Returns None if the file isn't
    found or the lookup fails (e.g. DB locked) — caller stores NULL."""
    try:
        with sqlite3.connect(str(CLASSIFICATIONS_DB_PATH), timeout=2.0) as c:
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


def init_db() -> None:
    """Idempotent. Creates the pi_reviews table + indexes if missing.
    Called at dashboard startup when PI_MODE=1."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock, _conn() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS pi_reviews (
                file         TEXT PRIMARY KEY,
                verdict      TEXT NOT NULL CHECK (verdict IN ('yes','no')),
                reviewed_at  TEXT NOT NULL,
                model_source TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_pi_reviews_at
                ON pi_reviews(reviewed_at);
            CREATE INDEX IF NOT EXISTS idx_pi_reviews_model
                ON pi_reviews(model_source);
            """
        )
        c.commit()


router = APIRouter(prefix="/api/pi-review", tags=["pi-review"])


@router.post("/{filename}")
def post_verdict(filename: str, body: dict = Body(...)):
    verdict = body.get("verdict")
    if verdict not in ("yes", "no"):
        raise HTTPException(
            status_code=400,
            detail="verdict must be 'yes' or 'no'",
        )
    model_source = _lookup_model_source(filename)
    with _lock, _conn() as c:
        c.execute(
            "INSERT INTO pi_reviews (file, verdict, reviewed_at, model_source) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(file) DO UPDATE SET "
            "    verdict = excluded.verdict, "
            "    reviewed_at = excluded.reviewed_at, "
            "    model_source = excluded.model_source",
            (filename, verdict, _now_iso(), model_source),
        )
        c.commit()
    return {
        "ok": True,
        "file": filename,
        "verdict": verdict,
        "model_source": model_source,
    }


@router.delete("/{filename}")
def clear_verdict(filename: str):
    """Undo — drop the verdict row entirely. The next review-state
    fetch will treat the file as unreviewed again."""
    with _lock, _conn() as c:
        cur = c.execute("DELETE FROM pi_reviews WHERE file = ?", (filename,))
        c.commit()
        deleted = cur.rowcount
    return {"ok": True, "file": filename, "deleted": deleted}


@router.get("/recent")
def recent_classifications(limit: int = 8):
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
    rows = []
    try:
        with sqlite3.connect(str(CLASSIFICATIONS_DB_PATH), timeout=2.0) as cls_c:
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
                    f"FROM pi_reviews WHERE file IN ({placeholders})",
                    files,
                )
            }
    else:
        verdicts = {}

    for r in rows:
        v = verdicts.get(r["file"])
        r["verdict"] = v["verdict"] if v else None
        r["reviewed_at"] = v["reviewed_at"] if v else None

    return {"items": rows}


@router.get("/stats")
def review_stats():
    """Accuracy summary by classifier model_source. The Pi dashboard
    surfaces this above the Recent Classifications strip so the user
    can see at-a-glance how AIY (or whichever classifier is active)
    is doing."""
    with _lock, _conn() as c:
        rows = list(
            c.execute(
                "SELECT COALESCE(model_source, 'unknown') AS model_source, "
                "       SUM(CASE WHEN verdict = 'yes' THEN 1 ELSE 0 END) AS yes_n, "
                "       SUM(CASE WHEN verdict = 'no'  THEN 1 ELSE 0 END) AS no_n, "
                "       COUNT(*) AS total "
                "FROM pi_reviews "
                "GROUP BY model_source "
                "ORDER BY total DESC"
            )
        )
    by_model = []
    grand_total = 0
    grand_yes = 0
    for r in rows:
        n = int(r["total"] or 0)
        y = int(r["yes_n"] or 0)
        no = int(r["no_n"] or 0)
        grand_total += n
        grand_yes += y
        by_model.append(
            {
                "model_source": r["model_source"],
                "yes": y,
                "no": no,
                "total": n,
                "accuracy": (y / n) if n > 0 else 0.0,
            }
        )
    overall_acc = (grand_yes / grand_total) if grand_total > 0 else 0.0
    return {
        "total_reviewed": grand_total,
        "overall_accuracy": overall_acc,
        "by_model": by_model,
    }
