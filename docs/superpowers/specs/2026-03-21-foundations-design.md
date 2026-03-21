# Bird Observatory Foundations — Design Spec

**Date:** 2026-03-21
**Status:** Draft
**Goal:** Establish architectural foundations that make the remaining 17 backlog items easier to implement, address operational gaps, and create a reproducible testing environment.

---

## Guiding Principles

This is a **prototype and first-of-its-kind system**. Two things matter most:

1. **Rich, useful data** — Gathering data that tells a meaningful story about the yard's ecosystem. Not raw frame counts, but visits, patterns, behaviors. Data should be easy to query, understand, and act on.
2. **Clarity of presentation** — The dashboard should make the data immediately understandable. If you need to think about what a number means, the UI isn't doing its job.

Testing and reproducibility are critical for iterating toward these goals.

---

## Problem Statement

The bird observatory has grown organically over weeks of development. While the core pipeline works well (23 bugs fixed in quality audit, SQLite migration complete, motion gate deployed), several structural issues make iteration slow:

1. **Frame-per-row data model** — 124K+ total rows (30K classified) for what's probably ~5K-10K actual bird visits. Dashboard counts are inflated, the review queue is bloated, storage grows faster than necessary.
2. **Duplicated inference code** — classify.py and live_detector.py share ~170 lines of functionally identical code (YOLO preprocessing, NMS, species classification, label parsing) that has already diverged in places (confidence thresholds, bug fixes applied to one but not the other).
3. **Reviews still in JSONL** — `review/pending` loads all ~30K classified entries from SQLite into Python dicts to cross-reference against ~950 reviews in JSONL. The memory spike comes from the classified entries, not the reviews themselves. Moving reviews to SQLite turns this into a JOIN.
4. **No reproducible test environment** — Can't benchmark changes, test at night, or compare pipeline versions without live birds. Need mock RTSP feeds from recorded video.
5. **Operational gaps** — No log rotation (~93MB/week growth), no automated health checks, stale config files.

## Approach: Foundations First

Four strategic changes (including test infrastructure), plus quick operational wins. The backlog items (17 remaining) become much simpler after these foundations are in place.

---

## Foundation 0: Mock RTSP Test Feeds

### What
Set up a way to loop recorded video files as mock RTSP streams, providing reproducible input for benchmarking and testing — even at nighttime when there are no live birds.

### Why
- Currently impossible to test pipeline changes without live birds
- Can't compare "before vs after" on the same input
- Nighttime development sessions have no data to work with
- Efficiency improvements can't be measured without controlled input
- Regression testing requires deterministic input

### Design
Use `go2rtc` or `ffmpeg` to serve pre-recorded video clips as RTSP streams.

**Option A: ffmpeg looping server (simplest)**
```bash
# Loop a recorded clip as an RTSP stream on a test port
ffmpeg -re -stream_loop -1 -i test_clips/feeder_sample.mp4 \
  -c copy -f rtsp rtsp://localhost:8554/test-feeder
```

**Option B: go2rtc config with file source**
```yaml
# go2rtc.yaml addition
test-feeder:
  - file:///Users/vives/bird-classifier/test_clips/feeder_sample.mp4#loop
test-ground:
  - file:///Users/vives/bird-classifier/test_clips/ground_sample.mp4#loop
```

**Test clips to capture:**
- 5-minute feeder clip with multiple species (daytime, good lighting)
- 5-minute ground cam clip with activity
- 1-minute clip with multi-bird scenario
- 1-minute clip with known difficult species (e.g., sparrow confusion)
- Optional: nighttime clip for wildlife pipeline testing

### How to use
```bash
# Point capture or live_detector at test stream
CAMERAS="test-feeder:test" python capture_snapshots.py
# Or
python live_detector.py --rtsp-url rtsp://localhost:8554/test-feeder
```

### Enables
- Benchmark any pipeline change on identical input
- Run full integration tests at any time of day
- Compare threshold tuning (e.g., confidence 0.3 vs 0.35) on same footage
- Test visit logic with known ground-truth data

---

## Foundation 1: Shared Inference Library (`bird_inference.py`)

### What
Extract duplicated YOLO detection + AIY species classification code from `classify.py` and `live_detector.py` into a shared module.

### Why
- ~170 lines of functionally duplicated code across both files (~410 lines total, as the implementations have diverged)
- Detection confidence has already diverged: 0.3 in batch (classify.py:67) vs 0.35 in live (live_detector.py:83) — unclear if intentional
- `parse_label` has a **bug in classify.py** (uses `split("(")[0]` which breaks on nested parens) that's already fixed in live_detector.py (uses `rindex("(")`)
- Motion gate exists in classify.py but NOT in live_detector.py — extracting shared code makes adding it trivial
- `SPECIES_ALIASES` is duplicated in 4 places: classify.py, live_detector.py, classifications_db.py, api.py

### Design
```python
# bird_inference.py
SPECIES_ALIASES = {
    "Slate-colored Junco": "Dark-eyed Junco",
    "Myrtle Warbler": "Yellow-rumped Warbler",
    "Feral Pigeon": "Rock Pigeon",
    "Yellow-shafted Flicker": "Northern Flicker",
}

def normalize_species(name: str) -> str:
    """Apply species aliases."""
    return SPECIES_ALIASES.get(name, name)

class YOLODetector:
    def __init__(self, model_path, confidence=0.3, nms_iou=0.45, providers=None): ...
    def detect(self, image) -> list[dict]: ...

class SpeciesClassifier:
    def __init__(self, model_path, labels_path, regional_species=None, providers=None): ...
    def classify(self, crop) -> tuple[list, list]: ...
    # Returns (filtered_predictions, raw_predictions) — both callers need both

def get_providers() -> list: ...  # CoreML + CPU fallback
def crop_bird(image, box, pad_ratio=0.15) -> np.ndarray: ...
def parse_label(label_str) -> tuple[str, str]: ...  # Uses rindex("(") — the correct version
```

Both `classify.py` and `live_detector.py` import from this module. Configuration (thresholds, providers) passed at construction time — no globals.

Note: `live_detector.py` currently has a combined `load_models()` function. It will need refactoring to construct `YOLODetector` and `SpeciesClassifier` separately.

### Also extract: `solar_utils.py`
The `_solar_times()` function is duplicated across classify.py (L95-124), live_detector.py (L111-140), and audio_analyzer.py (L103-145). Extract to a shared module. This is folded into Foundation 1 rather than being a separate quick win, since we're touching the same files.

### Testing
- Unit tests for `YOLODetector.detect()` with a known test image
- Unit tests for `SpeciesClassifier.classify()` with a known bird crop
- Unit tests for `parse_label()` including edge cases (nested parens)
- Unit tests for `normalize_species()` with aliases
- Integration test: process a test image through both classify.py and live_detector.py, verify identical results
- **Mock RTSP test**: Run live_detector with test feed, compare output to classify.py on same frames

### Enables
- Adding motion gate to live_detector.py (~5 lines instead of reimplementing)
- Centralized threshold config and bug fixes
- Future: wildlife classifier can reuse detection infrastructure

---

## Foundation 2: Reviews → SQLite

### What
Migrate `reviews.jsonl` (currently at `dashboard/reviews.jsonl`, ~950 entries) to a `reviews` table in `classifications.db`.

### Why
- `review/pending` calls `get_classified_for_pending()` which loads all ~30K classified entries from SQLite into Python dicts, then iterates to cross-reference against reviews — the memory spike comes from this, not from the reviews file itself
- With reviews in SQLite, the cross-reference becomes a LEFT JOIN — `get_classified_for_pending()` can be replaced entirely
- Same lesson as the JSONL→SQLite migration: unlock data with SQL
- Enables richer queries: "how many reviews per day?", "which species has most wrong verdicts?"

### Design
```sql
CREATE TABLE reviews (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file             TEXT    UNIQUE NOT NULL,
    verdict          TEXT    NOT NULL,  -- correct|wrong|skip|trash|reclassify|requeued
    correct_species  TEXT,              -- corrected species (for wrong/reclassify verdicts)
    bird_index       INTEGER DEFAULT 0, -- which bird in multi-bird frame
    missed_birds     INTEGER DEFAULT 0, -- flag for missed detections
    timestamp        TEXT    NOT NULL,  -- when review was submitted
    reviewer         TEXT    DEFAULT 'dashboard'
);

CREATE INDEX idx_reviews_file ON reviews(file);
CREATE INDEX idx_reviews_verdict ON reviews(verdict);
CREATE INDEX idx_reviews_species ON reviews(correct_species);
```

Note: Verdict values match the current code exactly: `correct`, `wrong`, `skip`, `trash`, `reclassify`, `requeued` (see api.py VALID_VERDICTS + programmatic requeue).

Dual-write to both SQLite and JSONL (backup) during transition. The incremental JSONL caching logic (api.py L106-135, `f.seek(_reviews_size)`) will be replaced with SQLite reads.

### Key Queries Unlocked
```sql
-- Pending review: replaces get_classified_for_pending() entirely
SELECT c.* FROM classifications c
LEFT JOIN reviews r ON c.file = r.file
WHERE c.action = 'classified'
  AND (r.file IS NULL OR r.verdict = 'requeued')
ORDER BY c.timestamp DESC LIMIT 50;

-- Review goals (confirmed count per species)
SELECT c.common_name, COUNT(*) as confirmed
FROM reviews r JOIN classifications c ON r.file = c.file
WHERE r.verdict = 'correct'
GROUP BY c.common_name;

-- Review activity (data richness: understand review patterns)
SELECT date(timestamp) as day, verdict, COUNT(*) as cnt
FROM reviews GROUP BY day, verdict ORDER BY day DESC;
```

### Testing
- Migration script: verify all 950 reviews.jsonl entries land in SQLite correctly
- Round-trip test: write a review via API, verify it appears in both SQLite and JSONL
- Endpoint tests: `review/pending`, `review/classified`, `review/goals` all return correct results
- Performance test: `review/pending` completes in <50ms (vs current ~200ms+)
- Crash recovery test: kill API mid-review, verify no data loss

### Rollback
reviews.jsonl continues to be written. If SQLite reviews have issues, revert api.py to read from JSONL. Migration script is idempotent (safe to re-run).

---

## Foundation 3: Visit-Based Event Model

### What
Group consecutive same-species detections into "visits" — one row per bird visit instead of one row per frame.

### Why
- 30K classified rows for what's probably ~5K-10K actual visits
- Mourning Dove peak: ~1,115 detections/day (March 19) — most are the same bird sitting still
- Auto-culling is a band-aid for the underlying data model problem
- Dashboard counts become meaningful ("12 cardinal visits" vs "1,115 detections")
- Review becomes manageable (review one visit's best frame, not hundreds)
- Best frame per visit = automatic species photo candidates
- **Data richness**: Visit duration, visit frequency, time-of-day patterns become queryable

### Design
```sql
CREATE TABLE visits (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    camera          TEXT    NOT NULL,
    species         TEXT    NOT NULL,
    scientific_name TEXT,
    start_time      TEXT    NOT NULL,
    end_time        TEXT,
    status          TEXT    DEFAULT 'active',  -- active|ended
    frame_count     INTEGER DEFAULT 1,
    best_confidence REAL,
    best_score      REAL,
    best_snapshot   TEXT,   -- filename of highest-confidence frame
    avg_confidence  REAL,
    bird_count      INTEGER DEFAULT 1,
    source_date     TEXT    NOT NULL
);

CREATE INDEX idx_visits_date ON visits(source_date);
CREATE INDEX idx_visits_species ON visits(species);
CREATE INDEX idx_visits_date_species ON visits(source_date, species);
CREATE INDEX idx_visits_status ON visits(status);
```

### Visit Logic
- A visit starts when a species is first detected
- A visit ends when N seconds pass with no detection of that species (configurable, default 60s)
- During a visit, track: frame count, best confidence frame, running average
- The `classifications` table remains unchanged (raw data) — `visits` is a derived view
- `status = 'active'` for ongoing visits, `'ended'` when gap timeout expires

### Crash Recovery
- On startup, `classify.py` checks for any `status = 'active'` visits and ends them (sets `end_time` to last known frame timestamp)
- Visit state is in SQLite, not in-memory — survives restarts

### Implementation Strategy
1. **Retroactive**: Script to populate `visits` from existing `classifications` data (use timestamp gaps to infer visit boundaries)
2. **Live**: `classify.py` creates/extends visits as it processes frames in watch mode
3. **Dashboard**: Show visits instead of raw detections (with option to drill into raw frames)
4. **Verify with mock RTSP**: Use test feeds to confirm visit logic with known ground truth

### Data Richness Queries Unlocked
```sql
-- Average visit duration by species
SELECT species, AVG(julianday(end_time) - julianday(start_time)) * 86400 as avg_seconds
FROM visits WHERE status = 'ended'
GROUP BY species ORDER BY avg_seconds DESC;

-- Peak visit hours
SELECT CAST(SUBSTR(start_time, 12, 2) AS INTEGER) as hour, COUNT(*) as visits
FROM visits GROUP BY hour ORDER BY visits DESC;

-- Species that visit together (overlapping visits)
SELECT v1.species, v2.species, COUNT(*) as co_visits
FROM visits v1 JOIN visits v2
  ON v1.camera = v2.camera
  AND v1.id < v2.id
  AND v1.start_time <= v2.end_time
  AND v2.start_time <= v1.end_time
GROUP BY v1.species, v2.species
ORDER BY co_visits DESC;
```

### Testing
- Retroactive script: verify visit count is 10-20x lower than raw detection count
- Gap logic: unit test that two detections 30s apart = one visit, 90s apart = two visits
- Multi-bird: verify two species in same frame create two separate visits
- Mock RTSP test: run classifier on test feed, verify visit boundaries match visual inspection
- Dashboard: verify visit counts display correctly

---

## Quick Wins (Parallel Track)

These are independent of the four foundations and can be done immediately:

| # | Fix | Files | Effort |
|---|-----|-------|--------|
| Q1 | Add log rotation via newsyslog.conf | /etc/newsyslog.d/ | 15 min |
| Q2 | Schedule check_health.sh as LaunchAgent | ~/Library/LaunchAgents/ | 15 min |
| Q3 | Add composite index (action, common_name) | classifications_db.py | 1 line |
| Q4 | Add composite index (source_date, action, common_name) | classifications_db.py | 1 line |
| Q5 | Delete stale config/go2rtc.yaml (RTSP tokens out of sync with root copy) | config/ | Delete file |
| Q6 | Centralize pipeline config to config/pipeline.json | classify.py, live_detector.py, audio_analyzer.py | 30 min |

Note: Solar extraction (previously Q6) is folded into Foundation 1 since it touches the same files.

---

## Audio Detection: Current Status & Investigation Notes

The audio system is performing better than initial reports suggested. As of March 20:
- **1,073 audio detections/day** (up from 359 on March 19 — likely threshold tuning took effect)
- `DEEP_DETECTION_INSTANT` was lowered from 0.90 → 0.65 (major loosening)
- `DYNAMIC_THRESHOLD_MIN` is 0.25 (aggressive floor)
- Confidence distribution is healthy: most detections in 0.5-0.9 range

**Audio vs Visual comparison (March 20):**
- Audio catches species cameras miss: Song Sparrow (160 audio vs 42 visual), Blue Jay (81 audio vs few visual)
- Cameras catch species audio misses: Mourning Dove (1,066 visual vs 6 audio — doves are quiet)
- The two systems complement each other well

**Remaining concerns:**
- The dramatic jump from 359→1,073 detections between March 19-20 needs investigation — was this a threshold change, a better day for birdsong, or a bug?
- No dual-instance comparison yet (raw vs enhanced audio)
- The `birdnetlib` sensitivity parameter is documented as a no-op (see gotchas doc) — all tuning is via thresholds

**Action items (deferred to after foundations):**
- Use mock RTSP audio feeds to benchmark detection rates with different thresholds
- Investigate March 19→20 detection jump
- Consider auto-trigger audio classification on capture events (currently independent service)

---

## Implementation Order

```
Phase 0: Quick Wins (Q1-Q6)           ← parallel, low risk, immediate
Phase 0.5: Mock RTSP Test Feeds       ← enables testing for all subsequent phases
Phase 1: Shared Inference Library      ← unblocks motion gate in live_detector
Phase 2: Reviews → SQLite             ← same migration pattern, proven
Phase 3: Visit-Based Event Model      ← highest complexity, highest payoff
```

Each phase gets:
- Git health checkpoint before starting
- Tests written alongside implementation
- Verification on mock RTSP feeds where applicable
- Git commit on completion

---

## What This Enables (Backlog Items)

After foundations are in place, these backlog items become straightforward:

| Backlog Item | Foundation Required | Simplified How |
|-------------|-------------------|----------------|
| Auto-culling | Visits | Keep best frame per visit, not every frame |
| Multi-bird review rework | Reviews SQLite + Visits | Review one visit, JOIN for review state |
| Species photo updates | Visits | Best frame per visit = photo candidates |
| Detection speed | Shared Inference + Mock RTSP | Motion gate in live_detector, benchmark on test feed |
| Species detection clickable | Visits | Visit-based counts, natural navigation |
| Goals auto-collapse | Reviews SQLite | SQL query for completion state |
| Audio detection investigation | Quick Wins (indexes) + Mock RTSP | Controlled comparison with known input |
| Dual BirdNET comparison | Mock RTSP | Same audio input to both instances |

---

## Success Criteria

1. **Mock RTSP**: Can loop a test video as RTSP, run the full pipeline against it, get deterministic results.
2. **Shared Inference**: classify.py and live_detector.py both import from bird_inference.py. No duplicated YOLO/AIY code. SPECIES_ALIASES defined once. Motion gate works in both. parse_label bug fixed.
3. **Reviews SQLite**: `review/pending` endpoint uses SQL JOIN, `get_classified_for_pending()` eliminated. No transient memory spike. reviews.jsonl still written as backup.
4. **Visits**: Dashboard shows visit-based counts. Retroactive script populates visits from historical data. Daily visit count is 10-20x lower than raw detection count. Visit duration and co-occurrence queries work.
5. **Quick Wins**: Logs rotate. Health checks run automatically. Indexes speed up common queries.
6. **Tests**: Each foundation has unit tests and integration tests. Mock RTSP feeds used for end-to-end verification.
