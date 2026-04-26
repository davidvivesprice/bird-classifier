> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Unified Classification Query System Implementation Plan (v2 — post-review)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace 6+ duplicate query paths with one `get_classifications()` function that handles all filtering (effective species, trash exclusion, pending vs reviewed) in one place.

**Architecture:** New `get_classifications()`, `count_classifications()`, and `list_classification_species()` in reviews_db.py. Migrate ALL API endpoints in a single task to avoid half-migrated state. Frontend updated atomically with API. Playwright screenshot verification after each task.

**Tech Stack:** Python, SQLite, FastAPI, Playwright (verification)

**Spec:** `docs/superpowers/specs/2026-03-30-unified-classification-query-design.md`

**Review findings incorporated:**
- C1: Frontend + API changes must be atomic (same commit)
- C3: multibird filter moved to SQL, not post-filter
- C4: Tests need `import reviews_db` + `_reset_connections()` helper
- H1: All endpoints migrated in one task to avoid half-migrated state
- H2: Pending uses `c.timestamp DESC`, reviewed uses `r.timestamp DESC`
- H4: Species dropdown includes both effective and original species (UNION preserved)
- M6: Frontend verdict label handles uncorrected "wrong" case

**CRITICAL VERIFICATION GATE:** After every task, the implementer MUST:
1. Run `pytest tests/ -q` — all pass
2. Restart dashboard, wait 25s for startup
3. Take Playwright screenshots of ALL review subtabs
4. Read each screenshot with the Read tool and verify visually
5. Test species filter with a corrected species AND an original species
6. Only then commit

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `reviews_db.py` | Modify | Add unified query functions + `_reset_connections()` helper |
| `dashboard/api.py` | Modify | Migrate ALL review endpoints to use new functions |
| `dashboard/index.html` | Modify | Update field names, fix verdict labels — ATOMIC with api.py |
| `tests/test_unified_query.py` | Create | Tests for all query paths, filter combinations, edge cases |

---

### Task 1: Build Unified Query Functions + Tests

**Files:**
- Modify: `reviews_db.py`
- Create: `tests/test_unified_query.py`

- [ ] **Step 1: Add `_reset_connections()` helper to reviews_db.py**

Add after `_reset_table_flag()`:

```python
def _reset_connections():
    """Reset all thread-local connections and table flag. For testing only."""
    global _table_ensured
    _table_ensured = False
    for attr in ("_reviews_ro_conn", "_reviews_rw_conn"):
        conn = getattr(_local, attr, None)
        if conn:
            try:
                conn.close()
            except Exception:
                pass
            delattr(_local, attr)
```

- [ ] **Step 2: Add unified query functions to reviews_db.py**

Add after the `_reset_connections` function. Key design decisions from reviews:

1. `_EFFECTIVE_SPECIES_SQL` — CASE WHEN expression used everywhere
2. `_build_classification_query` — shared WHERE builder for get + count
3. `multibird` is a SQL filter (`json_array_length`), NOT a post-filter
4. ORDER BY differs: pending uses `c.timestamp DESC`, reviewed uses `r.timestamp DESC`
5. `list_classification_species` uses UNION to include both effective AND original species

```python
# ── Unified classification query system ──

_EFFECTIVE_SPECIES_SQL = """
CASE WHEN r.verdict = 'wrong' AND r.correct_species IS NOT NULL
     AND r.correct_species != '' AND r.correct_species != 'not_a_bird'
THEN r.correct_species
ELSE c.common_name
END
"""


def _build_classification_query(status, species=None, verdict=None,
                                 camera=None, date=None, multibird=False):
    """Build WHERE clause + params for unified classification queries."""
    where = ["c.action = 'classified'", "c.common_name IS NOT NULL"]
    params = []

    if status == "pending":
        where.append("(r.file IS NULL OR r.verdict = 'requeued')")
    elif status == "reviewed":
        where.append("r.file IS NOT NULL")
        where.append("r.verdict != 'requeued'")
        where.append("r.verdict NOT IN ('trash')")
        where.append("NOT (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird')")

    if species:
        where.append(f"({_EFFECTIVE_SPECIES_SQL}) = ?")
        params.append(species)

    if verdict and status != "pending":
        where.append("r.verdict = ?")
        params.append(verdict)

    if camera:
        where.append("c.camera = ?")
        params.append(camera)

    if date:
        where.append("c.source_date = ?")
        params.append(date)

    if multibird:
        where.append("json_array_length(c.birds_json) > 1")

    return " AND ".join(where), params


def get_classifications(status="pending", species=None, verdict=None,
                        camera=None, date=None, multibird=False,
                        offset=0, limit=50):
    """Unified query for all classification views.

    Args:
        status: "pending" (no review), "reviewed" (has review, not trash), "all"
        species: Filter by effective species (corrected if corrected, original otherwise)
        verdict: Filter by specific verdict (only for reviewed)
        camera: Filter by camera name
        date: Filter by source_date
        multibird: Only show multi-bird frames
        offset/limit: Pagination
    """
    where_clause, params = _build_classification_query(
        status, species, verdict, camera, date, multibird
    )

    # Order: pending by classification time, reviewed by review time
    order = "c.timestamp DESC" if status == "pending" else "COALESCE(r.timestamp, c.timestamp) DESC"

    sql = (
        f"SELECT c.file, c.common_name AS original_species, "
        f"({_EFFECTIVE_SPECIES_SQL}) AS species, "
        f"c.scientific_name, c.confidence, c.source_timestamp, c.source_date, "
        f"c.best_detection_json, c.top3_json, c.raw_top3_json, c.birds_json, "
        f"c.extra_json, c.camera, c.raw_score, "
        f"r.verdict, r.correct_species, r.missed_birds, r.bird_index, "
        f"r.timestamp AS review_timestamp, r.reviewer "
        f"FROM classifications c "
        f"LEFT JOIN reviews r ON c.file = r.file "
        f"WHERE {where_clause} "
        f"ORDER BY {order} "
        f"LIMIT ? OFFSET ?"
    )
    params.extend([limit, offset])

    conn = get_conn(readonly=True)
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def count_classifications(status="pending", species=None, verdict=None,
                          camera=None, date=None, multibird=False):
    """Count classifications — same filters as get_classifications."""
    where_clause, params = _build_classification_query(
        status, species, verdict, camera, date, multibird
    )

    sql = (
        f"SELECT COUNT(*) "
        f"FROM classifications c "
        f"LEFT JOIN reviews r ON c.file = r.file "
        f"WHERE {where_clause}"
    )

    conn = get_conn(readonly=True)
    return conn.execute(sql, params).fetchone()[0]


def list_classification_species(status="reviewed"):
    """List distinct species for filter dropdowns.

    Returns both effective species (for corrected items) AND original species
    (for non-corrected items), so the dropdown includes all species the user
    might want to filter by.
    """
    where_clause, params = _build_classification_query(status)

    sql = (
        f"SELECT DISTINCT name FROM ("
        f"  SELECT ({_EFFECTIVE_SPECIES_SQL}) AS name "
        f"  FROM classifications c "
        f"  LEFT JOIN reviews r ON c.file = r.file "
        f"  WHERE {where_clause} "
        f"  UNION "
        f"  SELECT c.common_name AS name "
        f"  FROM classifications c "
        f"  LEFT JOIN reviews r ON c.file = r.file "
        f"  WHERE {where_clause} "
        f") ORDER BY name"
    )

    conn = get_conn(readonly=True)
    rows = conn.execute(sql, params + params).fetchall()
    return [r[0] for r in rows if r[0]]
```

- [ ] **Step 3: Write comprehensive tests**

Create `tests/test_unified_query.py`. Key fixes from reviews:
- `import reviews_db` at module level (C4 fix)
- Use `_reset_connections()` not just `_reset_table_flag()` (C4 fix)
- Test empty DB, status="all", multibird filter
- Test that pending ORDER BY uses c.timestamp
- Test uncorrected "wrong" verdicts (M6)

```python
"""Tests for unified classification query system."""
import sqlite3
import pytest
from pathlib import Path
from unittest.mock import patch

import reviews_db  # module-level import for _reset_connections


@pytest.fixture
def test_db(tmp_path):
    """Create a test DB with known classifications + reviews."""
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS classifications (
        file TEXT PRIMARY KEY, action TEXT, common_name TEXT,
        scientific_name TEXT, confidence REAL, source_timestamp TEXT,
        source_date TEXT, best_detection_json TEXT, top3_json TEXT,
        raw_top3_json TEXT, birds_json TEXT, extra_json TEXT,
        camera TEXT, raw_score REAL, timestamp TEXT
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS reviews (
        file TEXT PRIMARY KEY, verdict TEXT, correct_species TEXT,
        missed_birds INTEGER DEFAULT 0, bird_index INTEGER DEFAULT 0,
        timestamp TEXT, reviewer TEXT
    )""")

    data = [
        # (file, species, conf, camera, timestamp, verdict, correct_species, review_ts)
        ("bird1.jpg", "Song Sparrow", 0.9, "feeder", "2026-03-30 10:00:00", "correct", "", "2026-03-30 11:00:00"),
        ("bird2.jpg", "Dark-eyed Junco", 0.85, "ground", "2026-03-30 10:01:00", "wrong", "Black-capped Chickadee", "2026-03-30 11:01:00"),
        ("bird3.jpg", "Hairy Woodpecker", 0.7, "feeder", "2026-03-30 10:02:00", "wrong", "Downy Woodpecker", "2026-03-30 11:02:00"),
        ("bird4.jpg", "Rock Pigeon", 0.6, "ground", "2026-03-30 10:03:00", "trash", "", "2026-03-30 11:03:00"),
        ("bird5.jpg", "House Finch", 0.95, "feeder", "2026-03-30 10:04:00", None, None, None),  # pending
        ("bird6.jpg", "Blue Jay", 0.8, "feeder", "2026-03-30 10:05:00", "wrong", "not_a_bird", "2026-03-30 11:05:00"),
        ("bird7.jpg", "European Starling", 0.5, "ground", "2026-03-30 10:06:00", "wrong", "", "2026-03-30 11:06:00"),  # wrong, no correction
        ("multi1.jpg", "Song Sparrow", 0.9, "feeder", "2026-03-30 10:07:00", None, None, None),  # pending, multi-bird
    ]

    for f, sp, conf, cam, ts, verdict, correct, rts in data:
        birds_json = '[{"species":"Song Sparrow"},{"species":"House Finch"}]' if f == "multi1.jpg" else '[]'
        conn.execute(
            "INSERT INTO classifications (file, action, common_name, confidence, camera, source_timestamp, timestamp, birds_json) "
            "VALUES (?, 'classified', ?, ?, ?, ?, ?, ?)",
            (f, sp, conf, cam, ts, ts, birds_json)
        )
        if verdict:
            conn.execute(
                "INSERT INTO reviews (file, verdict, correct_species, timestamp, reviewer) "
                "VALUES (?, ?, ?, ?, 'test')",
                (f, verdict, correct or "", rts)
            )

    conn.commit()
    conn.close()
    return db_path


@pytest.fixture(autouse=True)
def reset_db_state():
    """Reset reviews_db state before each test."""
    yield
    reviews_db._reset_connections()


class TestGetClassifications:

    def test_pending_returns_only_unreviewed(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="pending")
            files = [r["file"] for r in results]
            assert "bird5.jpg" in files
            assert "multi1.jpg" in files
            assert "bird1.jpg" not in files  # reviewed
            assert "bird4.jpg" not in files  # trashed
            assert len(files) == 2

    def test_reviewed_excludes_trash_and_not_a_bird(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="reviewed")
            files = [r["file"] for r in results]
            assert "bird4.jpg" not in files  # trashed
            assert "bird6.jpg" not in files  # not_a_bird
            assert "bird1.jpg" in files      # correct
            assert "bird2.jpg" in files      # corrected
            assert "bird3.jpg" in files      # corrected
            assert "bird7.jpg" in files      # wrong, no correction (still reviewed)

    def test_effective_species(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="reviewed")
            by_file = {r["file"]: r for r in results}
            assert by_file["bird2.jpg"]["species"] == "Black-capped Chickadee"
            assert by_file["bird2.jpg"]["original_species"] == "Dark-eyed Junco"
            assert by_file["bird1.jpg"]["species"] == "Song Sparrow"
            assert by_file["bird1.jpg"]["original_species"] == "Song Sparrow"
            # Uncorrected wrong: species == original
            assert by_file["bird7.jpg"]["species"] == "European Starling"
            assert by_file["bird7.jpg"]["original_species"] == "European Starling"

    def test_species_filter_matches_effective(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="reviewed", species="Black-capped Chickadee")
            assert len(results) == 1
            assert results[0]["file"] == "bird2.jpg"

    def test_species_filter_excludes_corrected_away(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="reviewed", species="Dark-eyed Junco")
            files = [r["file"] for r in results]
            assert "bird2.jpg" not in files

    def test_multibird_filter_in_sql(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="pending", multibird=True)
            assert len(results) == 1
            assert results[0]["file"] == "multi1.jpg"

    def test_multibird_count_matches_get(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            items = reviews_db.get_classifications(status="pending", multibird=True)
            count = reviews_db.count_classifications(status="pending", multibird=True)
            assert count == len(items)

    def test_status_all_includes_everything(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="all")
            assert len(results) == 8  # all records

    def test_pending_has_null_verdict(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="pending")
            for r in results:
                assert r["verdict"] is None

    def test_response_has_required_fields(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            results = reviews_db.get_classifications(status="reviewed")
            for r in results:
                for field in ["file", "species", "original_species", "verdict",
                              "correct_species", "confidence", "source_timestamp", "camera"]:
                    assert field in r

    def test_empty_db(self, tmp_path):
        db_path = str(tmp_path / "empty.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE classifications (
            file TEXT PRIMARY KEY, action TEXT, common_name TEXT,
            scientific_name TEXT, confidence REAL, source_timestamp TEXT,
            source_date TEXT, best_detection_json TEXT, top3_json TEXT,
            raw_top3_json TEXT, birds_json TEXT, extra_json TEXT,
            camera TEXT, raw_score REAL, timestamp TEXT
        )""")
        conn.execute("""CREATE TABLE reviews (
            file TEXT PRIMARY KEY, verdict TEXT, correct_species TEXT,
            missed_birds INTEGER DEFAULT 0, bird_index INTEGER DEFAULT 0,
            timestamp TEXT, reviewer TEXT
        )""")
        conn.commit()
        conn.close()
        with patch("reviews_db.DB_PATH", db_path):
            reviews_db._reset_connections()
            assert reviews_db.get_classifications(status="pending") == []
            assert reviews_db.count_classifications(status="pending") == 0
            assert reviews_db.list_classification_species(status="reviewed") == []


class TestCountClassifications:

    def test_count_matches_get_reviewed(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            items = reviews_db.get_classifications(status="reviewed")
            count = reviews_db.count_classifications(status="reviewed")
            assert count == len(items)

    def test_count_pending(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            count = reviews_db.count_classifications(status="pending")
            assert count == 2  # bird5 + multi1

    def test_count_with_species(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            count = reviews_db.count_classifications(status="reviewed", species="Black-capped Chickadee")
            assert count == 1


class TestListClassificationSpecies:

    def test_includes_effective_species(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            species = reviews_db.list_classification_species(status="reviewed")
            assert "Black-capped Chickadee" in species  # corrected TO
            assert "Downy Woodpecker" in species         # corrected TO
            assert "Song Sparrow" in species              # confirmed

    def test_excludes_trashed_species(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            species = reviews_db.list_classification_species(status="reviewed")
            assert "Rock Pigeon" not in species  # trashed

    def test_includes_original_species_via_union(self, test_db):
        with patch("reviews_db.DB_PATH", test_db):
            reviews_db._reset_connections()
            species = reviews_db.list_classification_species(status="reviewed")
            # Original species of corrected items should ALSO be in the list
            # (via UNION) so users can filter by "what the AI called it"
            assert "Dark-eyed Junco" in species   # original of bird2
            assert "Hairy Woodpecker" in species   # original of bird3
```

- [ ] **Step 4: Run tests — verify they fail (function not found)**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_unified_query.py -v`
Expected: ImportError or AttributeError — functions don't exist yet.

- [ ] **Step 5: Implement the functions (from Step 2 above)**

- [ ] **Step 6: Run tests — all pass**

- [ ] **Step 7: Run full suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -q`

- [ ] **Step 8: Commit**

```bash
git add reviews_db.py tests/test_unified_query.py
git commit -m "feat: unified get_classifications() — one function, one set of rules"
```

---

### Task 2: Migrate ALL API Endpoints + Frontend (ATOMIC)

**Files:**
- Modify: `dashboard/api.py` — ALL review endpoints
- Modify: `dashboard/index.html` — renderClassifiedGrid, verdict labels

**This is one task, one commit.** All endpoints + frontend updated together to avoid half-migrated state (H1 fix).

- [ ] **Step 1: Migrate `/api/review/pending`**

Replace in `review_pending`:
```python
rows = rdb.get_classifications(status="pending", species=sp, multibird=mb, offset=offset, limit=limit)
remaining = rdb.count_classifications(status="pending", species=sp, multibird=mb)
```

Update response building: `r["common_name"]` → `r["original_species"]` for the species field AND in the BirdNET audio corroboration block (lines ~1273, ~1287).

- [ ] **Step 2: Migrate `/api/review/classified`**

Replace entire endpoint body:
```python
sp = species or None
v = verdict or None
rows = rdb.get_classifications(status="reviewed", species=sp, verdict=v, offset=offset, limit=limit)
total = rdb.count_classifications(status="reviewed", species=sp, verdict=v)
species_list = rdb.list_classification_species(status="reviewed")

items = []
for r in rows:
    best_det = json.loads(r["best_detection_json"]) if r.get("best_detection_json") else {}
    is_corrected = (r["verdict"] == "wrong" and r.get("correct_species")
                    and r["species"] != r["original_species"])
    items.append({
        "file": r["file"],
        "species": r["species"],
        "original_species": r["original_species"],
        "confidence": best_det.get("confidence", 0) if best_det else r.get("confidence", 0),
        "verdict": r["verdict"],
        "correct_species": r.get("correct_species", ""),
        "is_corrected": is_corrected,
        "missed_birds": bool(r.get("missed_birds", False)),
        "review_timestamp": r.get("review_timestamp", ""),
        "source_timestamp": r.get("source_timestamp", ""),
    })

return {"items": items, "total": total, "species_list": species_list}
```

Remove the ad-hoc count query and species list query that were inline.

- [ ] **Step 3: Migrate `/api/review/smart-queue`**

Replace pending query to use `rdb.get_classifications(status="pending")` and `rdb.count_classifications(status="pending")`.

- [ ] **Step 4: Migrate `/api/review/batch`**

Replace species query to use `rdb.get_classifications(status="pending", species=sp)`.

- [ ] **Step 5: Migrate `/api/skipped`**

Replace `rdb.get_reviewed_entries(verdict="skip")` with `rdb.get_classifications(status="reviewed", verdict="skip")`. Update field references: `r["common_name"]` → `r["original_species"]`.

- [ ] **Step 6: Update frontend `renderClassifiedGrid` (ATOMIC with API)**

Update the rendering to use server-computed fields:

```javascript
var speciesDisplay = item.is_corrected
    ? item.species + ' (was: ' + item.original_species + ')'
    : item.species;
var verdictClass = item.verdict === 'correct' ? 'color:#22c55e'
    : item.is_corrected ? 'color:#f59e0b'
    : item.verdict === 'wrong' ? 'color:#ef4444'
    : 'color:#f59e0b';
var verdictLabel = item.verdict === 'correct' ? 'Correct'
    : item.is_corrected ? 'Corrected'
    : item.verdict === 'wrong' ? 'Wrong'
    : item.verdict === 'skip' ? 'Skipped'
    : item.verdict === 'reclassify' ? 'Missed Birds'
    : item.verdict;
```

Note: handles uncorrected "wrong" verdicts (M6 fix) — shows "Wrong" in red.

Also update `openClassifiedLightbox` to use `item.species` (effective) for the title.

- [ ] **Step 7: Deprecate old functions in reviews_db.py**

Add deprecation comments:
```python
# DEPRECATED — use get_classifications(status="pending")
def get_pending_classifications(...):

# DEPRECATED — use count_classifications(status="pending")
def count_pending(...):

# DEPRECATED — use get_classifications(status="reviewed")
def get_reviewed_entries(...):
```

- [ ] **Step 8: Run full test suite**

- [ ] **Step 9: VERIFICATION GATE — Playwright screenshots**

Restart dashboard. Take screenshots of:
1. Classify tab (pending items)
2. Classified tab — All Species, All Verdicts
3. Classified tab — filter by "Black-capped Chickadee" (has corrections TO it)
4. Classified tab — filter by "Dark-eyed Junco" (has corrections FROM it — should show originals via UNION)
5. Batch tab
6. Skipped tab
7. Mobile view of Classify tab

Read each screenshot. Verify:
- No dead/broken images
- No trashed items
- Corrections show "species (was: original)"
- Uncorrected "wrong" shows "Wrong" in red
- Species filter works correctly
- Counts match visible items
- No overflow on mobile

- [ ] **Step 10: Commit**

```bash
git add dashboard/api.py dashboard/index.html reviews_db.py
git commit -m "feat: migrate all review endpoints to unified query

All 6 review endpoints now use get_classifications() with shared
filtering logic. Frontend updated atomically. Effective species,
trash exclusion, multibird filter, and count consistency all
handled in one place."
```

---

### Task 3: Final Verification Through Cloudflare Tunnel

- [ ] **Step 1: Restart dashboard cleanly**

```bash
pkill -9 -f "uvicorn"; sleep 3
launchctl unload ~/Library/LaunchAgents/com.vives.bird-dashboard.plist
launchctl load ~/Library/LaunchAgents/com.vives.bird-dashboard.plist
sleep 25
```

- [ ] **Step 2: Test through tunnel**

```bash
curl -s "https://birds.vivessato.com/bird-api/review/classified?species=Black-capped+Chickadee&limit=3"
curl -s "https://birds.vivessato.com/bird-api/review/classified?species=Dark-eyed+Junco&limit=3"
curl -s "https://birds.vivessato.com/bird-api/review/pending?limit=3"
```

Verify: effective species is correct, no trash, counts match.

- [ ] **Step 3: Playwright screenshot through tunnel**

Take screenshot of birds.vivessato.com Classified tab with species filter.

- [ ] **Step 4: Fix anything found, commit**
