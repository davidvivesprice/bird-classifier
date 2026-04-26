> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Phase 2: Reviews → SQLite

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate reviews from JSONL to SQLite, replacing the ~30K in-memory cross-reference with SQL JOINs. Same proven pattern as the classifications migration.

**Architecture:** Add `reviews` table to `classifications.db`. Migration script loads existing 1,015 JSONL entries. API dual-writes to both SQLite and JSONL during transition. All review endpoints rewritten to use SQL queries. `load_reviews()` and incremental JSONL caching removed.

**Tech Stack:** Python 3.9, SQLite (WAL mode), FastAPI, pytest

**Spec:** `docs/superpowers/specs/2026-03-21-foundations-design.md` — Foundation 2

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `reviews_db.py` | SQLite interface for reviews table (create, insert, query) |
| `migrate_reviews_to_sqlite.py` | One-time migration script (JSONL → SQLite) |
| `tests/test_reviews_db.py` | Unit tests for reviews_db module |

### Modified Files
| File | What Changes |
|------|-------------|
| `dashboard/api.py` | Replace load_reviews() + all review endpoints with SQL queries via reviews_db |
| `classifications_db.py` | Add reviews table creation to ensure_tables() |

---

## Task 1: Create reviews_db.py with Schema and Basic Operations

**Files:**
- Create: `reviews_db.py`
- Create: `tests/test_reviews_db.py`
- Modify: `classifications_db.py` (add reviews table to schema)

- [ ] **Step 1: Write tests for reviews_db**

Tests should cover:
- `ensure_reviews_table()` creates the table
- `insert_review()` stores a review and can be retrieved
- `insert_review()` with UPSERT — same file replaces previous review
- `get_review(filename)` returns the review dict or None
- `get_all_reviews()` returns dict keyed by filename
- `count_reviews()` returns total count
- `get_reviews_by_verdict(verdict)` filters correctly

Use an in-memory SQLite database for testing (`:memory:`).

- [ ] **Step 2: Run tests — FAIL**

- [ ] **Step 3: Implement reviews_db.py**

Schema (from spec):
```sql
CREATE TABLE IF NOT EXISTS reviews (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file             TEXT    UNIQUE NOT NULL,
    verdict          TEXT    NOT NULL,
    correct_species  TEXT    DEFAULT '',
    bird_index       INTEGER DEFAULT 0,
    missed_birds     INTEGER DEFAULT 0,
    timestamp        TEXT    NOT NULL,
    reviewer         TEXT    DEFAULT 'dashboard'
);
CREATE INDEX IF NOT EXISTS idx_reviews_file ON reviews(file);
CREATE INDEX IF NOT EXISTS idx_reviews_verdict ON reviews(verdict);
CREATE INDEX IF NOT EXISTS idx_reviews_species ON reviews(correct_species);
```

Functions needed:
- `ensure_reviews_table(conn)` — create table + indexes
- `insert_review(conn, review_dict)` — INSERT OR REPLACE (upsert by file)
- `get_review(conn, filename)` → dict or None
- `get_all_reviews(conn)` → dict keyed by filename
- `count_reviews(conn)` → int
- `get_reviews_by_verdict(conn, verdict)` → list of dicts
- `get_pending_files(conn, species=None, multibird=False, offset=0, limit=50)` → uses LEFT JOIN with classifications
- `get_review_goals(conn, regional_species, threshold=20)` → goals data
- `get_reviewed_entries(conn, species=None, verdict=None, offset=0, limit=50)` → reviewed items with classification data

Use the same DB path as classifications: `~/bird-snapshots/logs/classifications.db`
Use WAL mode, thread-local connections (same pattern as classifications_db.py).

- [ ] **Step 4: Run tests — PASS**
- [ ] **Step 5: Commit**

---

## Task 2: Migration Script

**Files:**
- Create: `migrate_reviews_to_sqlite.py`

- [ ] **Step 1: Write migration script**

Read `dashboard/reviews.jsonl` line by line, parse JSON, insert into reviews table. Handle:
- Early entries missing `missed_birds` and `bird_index` fields (default to 0/false)
- Duplicate filenames (later entry wins — same file reviewed twice = keep latest)
- Empty lines, malformed JSON (skip with warning)
- Idempotent: safe to re-run (INSERT OR REPLACE)

Print summary: total read, inserted, duplicates, errors.

- [ ] **Step 2: Run migration on actual data**

```bash
cd ~/bird-classifier/.worktrees/reviews-sqlite
venv-coral/bin/python migrate_reviews_to_sqlite.py
```

Expected: ~1,015 entries migrated, some may be deduplicated.

- [ ] **Step 3: Verify migration**

```bash
venv-coral/bin/python -c "
import sqlite3
conn = sqlite3.connect('/Users/vives/bird-snapshots/logs/classifications.db')
count = conn.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]
verdicts = conn.execute('SELECT verdict, COUNT(*) FROM reviews GROUP BY verdict').fetchall()
print(f'Total reviews: {count}')
for v, c in verdicts: print(f'  {v}: {c}')
"
```

- [ ] **Step 4: Commit**

---

## Task 3: Update API Endpoints — review/pending

**Files:**
- Modify: `dashboard/api.py` (review_pending endpoint, lines 458-506)

This is the highest-impact change. Currently loads ~30K entries into memory.

- [ ] **Step 1: Write test for pending endpoint**

Add to `tests/test_reviews_db.py`:
- Test that `get_pending_files()` returns classified files NOT in reviews
- Test that files with verdict='requeued' appear as pending
- Test species filter works
- Test pagination (offset/limit)

- [ ] **Step 2: Replace review_pending() in api.py**

The new implementation uses a SQL LEFT JOIN:
```sql
SELECT c.file, c.common_name, c.confidence, c.source_timestamp,
       c.best_detection_json, c.top3_json, c.birds_json, c.camera
FROM classifications c
LEFT JOIN reviews r ON c.file = r.file
WHERE c.action = 'classified'
  AND (r.file IS NULL OR r.verdict = 'requeued')
  [AND c.common_name = :species]
ORDER BY c.timestamp DESC
LIMIT :limit OFFSET :offset
```

Also need a COUNT query for pagination metadata.

- [ ] **Step 3: Verify endpoint works**

```bash
curl -s http://localhost:8099/api/review/pending?limit=5 | python3 -m json.tool | head -20
```

(Note: API runs from main checkout, not worktree. Test via direct Python call or wait for merge.)

- [ ] **Step 4: Commit**

---

## Task 4: Update API Endpoints — submit_review and review/goals

**Files:**
- Modify: `dashboard/api.py`

- [ ] **Step 1: Update submit_review()**

After creating review entry, DUAL-WRITE:
1. Insert into SQLite via `reviews_db.insert_review()`
2. Append to JSONL via `_append_jsonl()` (backup, keep during transition)

- [ ] **Step 2: Update review_goals()**

Replace Python iteration with SQL:
```sql
-- Count confirmed per species (correct verdict)
SELECT c.common_name, COUNT(*) as confirmed
FROM reviews r JOIN classifications c ON r.file = c.file
WHERE r.verdict = 'correct'
GROUP BY c.common_name

-- Count wrong-but-corrected per species
SELECT r.correct_species, COUNT(*) as confirmed
FROM reviews r
WHERE r.verdict = 'wrong' AND r.correct_species != ''
GROUP BY r.correct_species
```

Merge both result sets for total confirmed per species.

- [ ] **Step 3: Update review_classified()**

Replace batch file lookup with SQL JOIN:
```sql
SELECT r.*, c.common_name, c.confidence, c.source_timestamp, c.camera
FROM reviews r
JOIN classifications c ON r.file = c.file
WHERE r.verdict IN ('correct', 'wrong', 'reclassify')
  [AND c.common_name = :species]
  [AND r.verdict = :verdict]
ORDER BY r.timestamp DESC
LIMIT :limit OFFSET :offset
```

- [ ] **Step 4: Commit**

---

## Task 5: Remove Old JSONL Cache Code

**Files:**
- Modify: `dashboard/api.py`

- [ ] **Step 1: Remove load_reviews() function and cache variables**

Remove:
- `_reviews_cache` (line 93)
- `_reviews_size` (line 94)
- `load_reviews()` function (lines 97-126)
- All calls to `load_reviews()` throughout the file

All review reads now go through reviews_db.py SQL queries.

Keep `_append_jsonl()` — still used for JSONL backup writes.

- [ ] **Step 2: Run all tests**

```bash
venv-coral/bin/python -m pytest tests/ -v
```

- [ ] **Step 3: Commit**

---

## Task 6: Integration Tests and Final Verification

**Files:**
- Create: `tests/test_reviews_integration.py`

- [ ] **Step 1: Write integration tests**

- Round-trip: insert review via reviews_db → query via pending/goals/classified functions → verify correct
- Verify dual-write: insert via submit_review logic → check both SQLite and JSONL have the entry
- Verify pending count decreases after review
- Verify goals update after correct review

- [ ] **Step 2: Run full test suite**
- [ ] **Step 3: Commit and tag**

```bash
git tag -a v0.6-reviews-sqlite -m "Phase 2: reviews migrated to SQLite"
```
