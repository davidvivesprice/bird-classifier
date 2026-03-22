# Phase 3: Visit-Based Event Model

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group consecutive same-species detections into "visits" — one row per bird visit instead of one row per frame. Mourning Dove's 1,066 daily detections become ~20-30 visits.

**Architecture:** New `visits` table in classifications.db. Retroactive script populates from historical data. classify.py extends visits in real-time during watch mode. Dashboard API gains visit-based endpoints alongside existing detection endpoints.

**Tech Stack:** Python 3.9, SQLite (WAL mode), pytest

**Spec:** `docs/superpowers/specs/2026-03-21-foundations-design.md` — Foundation 3

**Key data insight:** 93.7% of Mourning Dove detections occur within 60 seconds of the previous one. A 60-second gap threshold will collapse ~1,000 detections into ~20-30 visits.

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `visits_db.py` | SQLite interface for visits table (create, extend, end, query) |
| `populate_visits.py` | Retroactive script: build visits from existing classifications |
| `tests/test_visits_db.py` | Unit tests for visits_db |
| `tests/test_visits_integration.py` | Integration tests against real data |

### Modified Files
| File | What Changes |
|------|-------------|
| `classify.py` | After inserting classification, create/extend visit |
| `dashboard/api.py` | Add visit-based endpoints alongside existing ones |

---

## Task 1: Create visits_db.py — Schema and Core Operations

**Files:**
- Create: `visits_db.py`
- Create: `tests/test_visits_db.py`

- [ ] **Step 1: Write tests**

Test with in-memory SQLite:
- Table creation
- `start_visit()` creates a new visit, returns visit_id
- `extend_visit(visit_id, ...)` updates frame_count, best_confidence, end_time
- `end_visit(visit_id)` sets status='ended'
- `get_active_visit(camera, species)` finds active visit within gap threshold
- `get_active_visit()` returns None if gap exceeded
- `end_stale_visits()` ends all active visits (for crash recovery)
- `get_visits(date, camera, species)` with filters
- `count_visits(date)` vs count of classifications — should be much lower
- Visit duration calculation

- [ ] **Step 2: Implement visits_db.py**

Schema:
```sql
CREATE TABLE IF NOT EXISTS visits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    camera          TEXT    NOT NULL,
    species         TEXT    NOT NULL,
    scientific_name TEXT,
    start_time      TEXT    NOT NULL,
    end_time        TEXT,
    status          TEXT    DEFAULT 'active',
    frame_count     INTEGER DEFAULT 1,
    best_confidence REAL,
    best_score      REAL,
    best_snapshot   TEXT,
    avg_confidence  REAL,
    bird_count      INTEGER DEFAULT 1,
    source_date     TEXT    NOT NULL
);
```

Core functions:
```python
def start_visit(camera, species, scientific_name, timestamp, source_date,
                confidence, score, snapshot, bird_count=1) -> int:
    """Start a new visit. Returns visit_id."""

def extend_visit(visit_id, timestamp, confidence, score, snapshot):
    """Add a frame to an existing visit. Updates best if higher confidence."""

def end_visit(visit_id):
    """Mark visit as ended."""

def get_active_visit(camera, species, current_time, gap_seconds=60) -> dict or None:
    """Find an active visit for this camera+species within the gap threshold."""
    # SELECT ... WHERE camera=? AND species=? AND status='active'
    #   AND (julianday(?) - julianday(end_time)) * 86400 <= gap_seconds

def end_stale_visits():
    """End all active visits (crash recovery on startup)."""

def get_visits(date=None, camera=None, species=None, limit=50, offset=0) -> list:
    """Query visits with optional filters."""

def count_visits(date=None, camera=None, species=None) -> int:
    """Count visits with optional filters."""

def get_visit_summary(date) -> list:
    """Species visit counts for a date — replacement for detection counts."""
    # SELECT species, COUNT(*) as visits, SUM(frame_count) as frames,
    #        MAX(best_confidence) as peak_confidence
    # FROM visits WHERE source_date=? GROUP BY species

def get_visit_stats(date) -> dict:
    """Aggregate visit statistics for a date."""
```

- [ ] **Step 3: Run tests — PASS**
- [ ] **Step 4: Commit**

---

## Task 2: Retroactive Visit Population Script

**Files:**
- Create: `populate_visits.py`

- [ ] **Step 1: Write populate_visits.py**

Logic:
1. Query all classified detections ordered by camera, common_name, source_timestamp
2. For each detection, check if it extends the current visit (same camera + species, within 60s gap)
3. If yes: extend (increment frame_count, update best confidence, update end_time)
4. If no: end previous visit, start new one

```python
#!/usr/bin/env python3
"""Populate visits table from existing classifications.

Groups consecutive same-species detections into visits using a 60-second
gap threshold. Safe to re-run (clears visits table first).
"""
```

Key query to get ordered detections:
```sql
SELECT file, camera, common_name, scientific_name, source_timestamp, source_date,
       confidence, raw_score
FROM classifications
WHERE action = 'classified' AND common_name IS NOT NULL
ORDER BY camera, common_name, source_timestamp
```

Process in Python: iterate, track active visits per (camera, species), start/extend/end based on time gaps.

Print summary: total detections, total visits, compression ratio, top species by visit count.

- [ ] **Step 2: Run on real data**

```bash
venv-coral/bin/python populate_visits.py
```

Expected: ~30K detections → ~2K-5K visits (6-15x compression).

- [ ] **Step 3: Verify**

```bash
venv-coral/bin/python -c "
import sqlite3
conn = sqlite3.connect('/Users/vives/bird-snapshots/logs/classifications.db')
det = conn.execute(\"SELECT COUNT(*) FROM classifications WHERE action='classified'\").fetchone()[0]
vis = conn.execute('SELECT COUNT(*) FROM visits').fetchone()[0]
print(f'Detections: {det}, Visits: {vis}, Ratio: {det/vis:.1f}x')
# Top species
rows = conn.execute('SELECT species, COUNT(*), SUM(frame_count) FROM visits GROUP BY species ORDER BY 2 DESC LIMIT 10').fetchall()
for sp, v, f in rows:
    print(f'  {sp}: {v} visits ({f} frames, {f/v:.1f} frames/visit)')
"
```

- [ ] **Step 4: Commit**

---

## Task 3: Wire classify.py to Create/Extend Visits in Real-Time

**Files:**
- Modify: `classify.py`

- [ ] **Step 1: Add visit tracking to classify.py**

In `process_file()`, after the successful classification result is built and `insert_classification()` is called, add visit logic:

```python
import visits_db as vdb

# After insert_classification(result):
if result["action"] == "classified" and result.get("top_prediction"):
    species = result["top_prediction"]["common_name"]
    camera = result.get("camera", "feeder")
    timestamp = result.get("source_timestamp") or result["timestamp"]
    confidence = result.get("best_detection", {}).get("confidence", 0)
    score = result.get("top_prediction", {}).get("raw_score", 0)
    source_date = timestamp[:10] if timestamp else None

    active = vdb.get_active_visit(camera, species, timestamp)
    if active:
        vdb.extend_visit(active["id"], timestamp, confidence, score, result["file"])
    else:
        vdb.start_visit(
            camera=camera, species=species,
            scientific_name=result.get("top_prediction", {}).get("scientific_name", ""),
            timestamp=timestamp, source_date=source_date,
            confidence=confidence, score=score, snapshot=result["file"],
            bird_count=result.get("detections", 1),
        )
```

Also add crash recovery in `main()` before starting watch_mode:
```python
vdb.end_stale_visits()
```

- [ ] **Step 2: Handle multi-bird frames**

If `result["birds"]` has multiple entries, create/extend a visit for EACH species detected:
```python
if len(result.get("birds", [])) > 1:
    for bird in result["birds"]:
        species = bird.get("species") or bird.get("common_name")
        # ... create/extend visit for each
```

- [ ] **Step 3: Verify classify.py imports cleanly**

```bash
venv-coral/bin/python -c "import classify; print('OK')"
```

- [ ] **Step 4: Commit**

---

## Task 4: Visit API Endpoints

**Files:**
- Modify: `dashboard/api.py`

- [ ] **Step 1: Add visit endpoints**

```python
import visits_db as vdb

@app.get("/api/visits")
def get_visits(date: str = "today", camera: str = "all", species: str = "",
               limit: int = 50, offset: int = 0):
    """Get visits with optional filters."""

@app.get("/api/visit-summary")
def visit_summary(date: str = "today", camera: str = "all"):
    """Species visit counts for dashboard chart — replaces detection counts."""

@app.get("/api/visit-stats")
def visit_stats(date: str = "today"):
    """Aggregate visit statistics."""

@app.get("/api/visits/{species}")
def species_visits(species: str, date: str = "today", camera: str = "all",
                   limit: int = 50, offset: int = 0):
    """Get visits for a specific species with frame details."""
```

These are NEW endpoints alongside existing ones. Don't replace the detection-based endpoints yet — the dashboard can switch to visits when ready.

- [ ] **Step 2: Add visit data to existing species endpoint**

In the existing `/api/species` response, add visit count alongside detection count:
```python
# Alongside existing "count" field:
"visit_count": vdb.count_visits(date=date, species=name),
```

- [ ] **Step 3: Commit**

---

## Task 5: Integration Tests and Verification

**Files:**
- Create: `tests/test_visits_integration.py`

- [ ] **Step 1: Write integration tests**

Against real database:
- visits table exists and has data
- visit count is significantly lower than detection count (at least 5x)
- Mourning Dove visits << Mourning Dove detections
- All visits have valid species names
- Active visits have no end_time stale by more than 1 hour (crash recovery works)
- Visit summary matches detection data (same species set)

- [ ] **Step 2: Run full test suite**

```bash
venv-coral/bin/python -m pytest tests/ -v --tb=short
```

- [ ] **Step 3: Tag**

```bash
git tag -a v0.7-visit-model -m "Phase 3: visit-based event model

- visits_db.py: SQLite interface for visits table
- populate_visits.py: retroactive population from classifications
- classify.py: real-time visit creation/extension
- API: /api/visits, /api/visit-summary, /api/visit-stats endpoints
- Integration tests against real data"
```

- [ ] **Step 4: Commit**
