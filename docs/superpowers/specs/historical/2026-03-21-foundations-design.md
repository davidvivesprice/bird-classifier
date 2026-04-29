> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Bird Observatory — Complete System Spec & Roadmap

**Date:** 2026-03-21
**Status:** Phases 0–6 COMPLETE (March 22, 2026). Phase 7 deferred. Phase 8 next.
**Scope:** Everything — foundations, backlog, audit findings, operational gaps, future work. Single source of truth.

---

## Guiding Principles

This is a **prototype and first-of-its-kind system**. Three things matter most:

1. **Rich, useful data** — Gathering data that tells a meaningful story about the yard's ecosystem. Not raw frame counts, but visits, patterns, behaviors. Data should be easy to query, understand, and act on. Collect richly, but only when it's useful.
2. **Clarity of presentation** — The dashboard should make the data immediately understandable. If you need to think about what a number means, the UI isn't doing its job.
3. **Testability** — Every change should be verifiable. Reproducible inputs, deterministic outputs, evidence before assertions.

---

## Current System State (March 21, 2026)

### What Works Well
- Two-stage detection pipeline (YOLOv8n + AIY Birds V1) — 103ms per frame, hardware accelerated
- SQLite primary database — 124K rows, ~95MB, WAL mode, fast queries
- Motion gate — 70-80% YOLO call elimination on static frames (batch classifier only)
- Audio analyzer — BirdNET V2.4, 1,073 detections/day on March 20
- Live detection overlay — 3 FPS SSE bounding boxes
- Dashboard — functional single-page app with chart, review, species info
- Quality audit — 23 bugs fixed across two phases, all deployed
- Dynamic RTSP URL loading — services read from rtsp_urls.json

### What's Broken or Missing
Organized by priority tier, with every item from the backlog audit, code audit, and operational audit.

---

## Tier 0: Test Infrastructure

Without this, we can't verify anything else.

### F0. Mock RTSP Test Feeds — COMPLETE (March 22, 2026)

**Problem:** Can't test pipeline changes without live birds. Can't benchmark at night. Can't compare before/after on same input.

**Design:** Loop recorded video as RTSP streams via ffmpeg or go2rtc file source.

```bash
# Option A: ffmpeg (simplest)
ffmpeg -re -stream_loop -1 -i test_clips/feeder_sample.mp4 \
  -c copy -f rtsp rtsp://localhost:8554/test-feeder

# Option B: go2rtc config
test-feeder:
  - file:///Users/vives/bird-classifier/test_clips/feeder_sample.mp4#loop
```

**Test clips needed:**
- 5-min feeder clip with multiple species (daytime, good lighting)
- 5-min ground cam clip with activity
- 1-min multi-bird scenario
- 1-min difficult species (sparrow confusion)
- Nighttime clip (for wildlife pipeline)

**Enables:** Benchmarking, regression testing, threshold comparison, visit logic verification.

---

## Tier 1: Architectural Foundations

These are structural changes that make everything else easier.

### F1. Shared Inference Library (`bird_inference.py`) — COMPLETE (March 22, 2026)

**Problem:** classify.py and live_detector.py share ~170 lines of functionally identical code (~410 lines total across diverged implementations). The code has already diverged in harmful ways:
- Detection confidence: 0.3 (classify.py:67) vs 0.35 (live_detector.py:83)
- `parse_label` bug: classify.py uses `split("(")[0]` (breaks on nested parens), live_detector.py correctly uses `rindex("(")`
- Motion gate in classify.py only — can't add to live_detector without touching duplicated code
- `SPECIES_ALIASES` duplicated in 4 files: classify.py, live_detector.py, classifications_db.py, api.py

**Design:**
```python
# bird_inference.py
SPECIES_ALIASES = { "Slate-colored Junco": "Dark-eyed Junco", ... }
def normalize_species(name): ...

class YOLODetector:
    def __init__(self, model_path, confidence=0.3, nms_iou=0.45, providers=None): ...
    def detect(self, image) -> list[dict]: ...

class SpeciesClassifier:
    def __init__(self, model_path, labels_path, regional_species=None, providers=None): ...
    def classify(self, crop) -> tuple[list, list]: ...

def get_providers() -> list: ...
def crop_bird(image, box, pad_ratio=0.15) -> np.ndarray: ...
def parse_label(label_str) -> tuple[str, str]: ...
```

**Also extract:** `solar_utils.py` — `_solar_times()` is duplicated across classify.py (L95-124), live_detector.py (L111-140), audio_analyzer.py (L103-145).

**Note:** live_detector.py's combined `load_models()` function will need refactoring to construct separate detector/classifier instances.

**Tests:**
- Unit: YOLODetector.detect() with known image, SpeciesClassifier.classify() with known crop
- Unit: parse_label() with nested parens, normalize_species() with aliases
- Integration: same image through classify.py and live_detector.py → identical results
- Mock RTSP: live_detector on test feed, compare output to classify.py on same frames

**Enables:** Motion gate in live_detector, centralized threshold config, wildlife pipeline reuse.

---

### F2. Reviews → SQLite — COMPLETE (March 22, 2026)

**Problem:** `review/pending` calls `get_classified_for_pending()` which loads all ~30K classified entries from SQLite into Python dicts, then iterates to cross-reference against ~950 reviews in JSONL. The memory spike (~50-100MB) comes from loading the classified entries, not the reviews themselves.

**Design:**
```sql
CREATE TABLE reviews (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    file             TEXT    UNIQUE NOT NULL,
    verdict          TEXT    NOT NULL,  -- correct|wrong|skip|trash|reclassify|requeued
    correct_species  TEXT,
    bird_index       INTEGER DEFAULT 0,
    missed_birds     INTEGER DEFAULT 0,
    timestamp        TEXT    NOT NULL,
    reviewer         TEXT    DEFAULT 'dashboard'
);
CREATE INDEX idx_reviews_file ON reviews(file);
CREATE INDEX idx_reviews_verdict ON reviews(verdict);
CREATE INDEX idx_reviews_species ON reviews(correct_species);
```

Verdict values match current code: `correct`, `wrong`, `skip`, `trash`, `reclassify`, `requeued`.

**Key queries unlocked:**
```sql
-- Pending review: replaces get_classified_for_pending() entirely
SELECT c.* FROM classifications c
LEFT JOIN reviews r ON c.file = r.file
WHERE c.action = 'classified' AND (r.file IS NULL OR r.verdict = 'requeued')
ORDER BY c.timestamp DESC LIMIT 50;

-- Review goals
SELECT c.common_name, COUNT(*) as confirmed
FROM reviews r JOIN classifications c ON r.file = c.file
WHERE r.verdict = 'correct' GROUP BY c.common_name;

-- Review patterns (data richness)
SELECT date(timestamp) as day, verdict, COUNT(*) FROM reviews
GROUP BY day, verdict ORDER BY day DESC;
```

Dual-write to SQLite + JSONL during transition. Incremental JSONL caching (api.py L106-135) replaced with SQLite reads. Rollback: revert to JSONL reads. Migration idempotent.

**Tests:**
- Migration: all 950 reviews.jsonl entries land correctly
- Round-trip: write review via API → appears in both SQLite and JSONL
- Endpoints: review/pending, review/classified, review/goals return correct results
- Performance: review/pending < 50ms (vs current ~200ms+)
- Crash recovery: kill API mid-review → no data loss

---

### F3. Visit-Based Event Model

**Problem:** 30K classified rows for ~5K-10K actual bird visits. Mourning Dove peak: 1,115 detections/day (March 19) — most are the same bird sitting still. Auto-culling is a band-aid. Dashboard counts are meaningless. Review queue is bloated.

**Design:**
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
    best_snapshot   TEXT,
    avg_confidence  REAL,
    bird_count      INTEGER DEFAULT 1,
    source_date     TEXT    NOT NULL
);
CREATE INDEX idx_visits_date ON visits(source_date);
CREATE INDEX idx_visits_species ON visits(species);
CREATE INDEX idx_visits_date_species ON visits(source_date, species);
CREATE INDEX idx_visits_status ON visits(status);
```

**Visit logic:** Start on first detection. End after N seconds gap (default 60s, configurable). Track frame count, best confidence frame, running average. `classifications` table unchanged — `visits` is derived.

**Crash recovery:** On startup, end any `status='active'` visits. Visit state in SQLite, not memory.

**Implementation:**
1. Retroactive script: populate from existing classifications (timestamp gaps = visit boundaries)
2. Live: classify.py creates/extends visits in watch mode
3. Dashboard: show visits with option to drill into raw frames

**Data richness queries:**
```sql
-- Average visit duration by species
SELECT species, AVG(julianday(end_time) - julianday(start_time)) * 86400 as avg_seconds
FROM visits WHERE status = 'ended' GROUP BY species;

-- Species co-occurrence (overlapping visits)
SELECT v1.species, v2.species, COUNT(*) as co_visits
FROM visits v1 JOIN visits v2 ON v1.camera = v2.camera
  AND v1.id < v2.id AND v1.start_time <= v2.end_time AND v2.start_time <= v1.end_time
GROUP BY v1.species, v2.species ORDER BY co_visits DESC;

-- Peak visit hours
SELECT CAST(SUBSTR(start_time, 12, 2) AS INTEGER) as hour, COUNT(*) FROM visits
GROUP BY hour ORDER BY 2 DESC;
```

**Tests:**
- Retroactive: visit count 10-20x lower than detection count
- Gap logic: 30s apart = 1 visit, 90s apart = 2 visits
- Multi-bird: two species in same frame = two visits
- Mock RTSP: verify boundaries against visual inspection

---

## Tier 2: UI / Display Fixes (Backlog)

Status from audit. Items marked [DONE] are verified complete.

### B1. Unified time & date display — DONE (March 22)
Both Classified tab and Species tab show date/time consistently. Standardized to "Mar 20, 3:42 PM" with relative time as secondary. Consistent across all tabs.

### B2. Species tab sort order — [DONE]
Backend returns `ORDER BY timestamp DESC` (newest first).

### B3. Recent sightings timestamp — [DONE]
`timeAgo()` provides near-real-time relative display.

### B4. Species section heading — DONE (March 22)
"Species — Today" heading added with tab navigation to all-time/historical view. Date selector integrated into species grid header.

### B5. Top bar audio display — DONE (March 22)
Desktop shows "4 in yard — Song Sparrow, Blue Jay, ..." with name list (truncated at 3 species). Mobile shows count only.

### B6. Clickable camera icon on species detection count — DONE (March 22)
Camera icon added next to count in chart Y-axis labels. Click handler navigates to species grid filtered to that species.

### B7. Classification goals auto-collapse — DONE (March 22)
Goals panel auto-collapses after confirming a bird. After `submitReview('correct')`, checks if species meets goal threshold, then collapses panel and clears filter if complete.

### B8. Species list at Chillmark feeder — DONE (March 22)
Species filter dropdown now populated from `/api/regional-species`. Shows the full regional list (what the classifier looks for), not just detected species.

### B9. Merge Slate-colored Junco → Dark-eyed Junco — PARTIAL
Aliased in api.py/classifications_db.py/live_detector.py, but still listed separately in `chilmark_feeder_species.txt` (line 53).

**Fix:** Remove "Slate-colored Junco" from chilmark_feeder_species.txt. The alias handles normalization; the species file shouldn't list both. (Foundation F1 consolidates SPECIES_ALIASES to one location.)

### B10. Fox Sparrow — [DONE]
Present at line 21 of chilmark_feeder_species.txt.

### B11. Auto-culling / confidence threshold — [DONE] (species-cap based)
Species-cap based auto-trash implemented (`trashed:overcap`). No confidence-based auto-trash, but visit model (F3) will make this largely unnecessary.

---

## Tier 3: Multi-Bird Review Rework (Backlog)

Full rework needed. Current state from audit:

### B12. "Multiple birds" in dropdown — DONE (March 22)
"Multiple birds" added as first option in species filter dropdown. Filters review queue to frames with `birds_json` containing 2+ entries.

### B13. Per-bird correction buttons — DONE (March 22)
Correction buttons (Correct/Wrong/No Bird) shown directly beneath each bird's bounding box preview. Clicking a bird card selects it and shows its action buttons.

### B14. Update overlay & bird list on correction — DONE (March 22)
When a bird is corrected, the label updates immediately in the numbered bird list (client-side). Overlay re-annotation deferred as designed.

### B15. Visual selection state — [DONE]
Green highlight on selected bird card.

### B16. "Confirm — I'm done" dialog — DONE (March 22)
After reviewing all birds in a multi-bird frame, shows "All birds reviewed — move to next?" with Confirm/Back buttons. Single-bird frames still auto-advance.

---

## Tier 4: Audio Classification (Backlog + Investigation)

### Current Status (March 20 data)
Audio is performing better than reported:
- **1,073 detections/day** (up from 359 on March 19)
- `DEEP_DETECTION_INSTANT` lowered from 0.90 → 0.65 (significant loosening)
- `DYNAMIC_THRESHOLD_MIN` at 0.25
- Confidence distribution healthy: most detections 0.5-0.9

**Audio vs Visual comparison (March 20):**
| Species | Visual | Audio | Notes |
|---------|--------|-------|-------|
| Mourning Dove | 1,066 | 6 | Quiet bird — visual dominates |
| House Finch | 616 | 460 | Both strong |
| Song Sparrow | 42 | 160 | Audio catches more — vocal bird |
| Blue Jay | few | 81 | Audio catches more |
| Black-capped Chickadee | 149 | 135 | Roughly equal |

The two systems complement each other. Audio isn't "broken" — it was too restrictive, and recent threshold changes helped significantly.

### B17. Investigate audio detection counts — RESOLVED (March 22, 2026)
The 359→1,073 jump between March 19-20 was caused by **RTSP token expiry**, not threshold changes.

**Root cause:** `sync_rtsp_urls.sh` had a SCP port flag bug (`-p` vs `-P`) causing nightly token sync to fail silently since March 20. March 19 had detections only 12:36-18:41 (service down all morning due to stale token). March 20 had a full day 06:49-18:28 with still-valid tokens. Daily counts across Mar 11-20 range from 387-1,073, so March 20 was high but not anomalous.

**Fix:** SCP bug fixed, plus comprehensive RTSP resilience added (see `rtsp-resilience-design.md`).

### B18. Dual-instance BirdNET comparison — NOT DONE
Run BirdNET on both raw and enhanced audio. `enhanced_audio_stream.py` currently only serves filtered audio for playback — not analyzed.

**Fix:** Add a second BirdNET analysis pass on the enhanced (bandpass-filtered) audio stream. Log to a separate table or with a `source` column. Compare detection rates. Four data points: raw+BirdNET, enhanced+BirdNET, raw+custom, enhanced+custom.

**Dependency:** Mock RTSP feeds (F0) enable controlled comparison.

### B19. Audio-visual temporal correlation — INVESTIGATED (March 23, 2026)
Audio analyzer runs as independent continuous service (correct architecture — birds sing without camera triggers).

**Findings (March 20 data, ±60s correlation window):**
- 6 species had same-species audio+visual matches within 60s
- House Finch: 42.9% of visual detections corroborated by audio (very vocal)
- Song Sparrow: 31% corroborated — audio catches 160 vs 42 visual (vocal but less visible)
- Mourning Dove: 2.2% — quiet bird, camera dominates
- Eastern Bluebird: 48 audio-only detections, 0 visual (audio finds species camera misses)
- Downy Woodpecker: 70 visual-only, 0 audio (visual finds species audio misses)
- The two systems are highly complementary — audio-visual fusion would significantly improve coverage

**Next step:** Add audio corroboration as an enrichment field on visual detections or visit summaries. Build a cross-reference API endpoint.

### `birdnetlib` sensitivity — KNOWN NO-OP
The `sensitivity` parameter is hardcoded in birdnetlib's analyzer.py. All tuning is via confidence thresholds and deep detection. Documented in gotchas.

---

## Tier 5: Performance / Architecture (Backlog + Audit)

### B20. Reduce screenshot I/O overhead — PARTIAL
Motion pre-filter works well (single write per motion event). Every frame is still fetched from UniFi API before motion check.

**Future:** Dual-stream capture (Frigate Stage 3) — pull low-res for detection, full-res only on confirmed bird. Medium-high complexity. Deferred.

### B21. Send cropped photos to classifier — [DONE]
`crop_bird()` with 15% padding sends only cropped regions to AIY Stage 2.

### B22. Detection speed — PARTIAL
- Batch: 5s snapshot poll, 10s classify poll. Could reduce classify poll to 2-3s or switch to file-watching.
- Live: 3 FPS (configurable). Reasonable.
- Metrics infrastructure exists (live_detector.py `/metrics` endpoint).

**Quick win:** Reduce `WATCH_INTERVAL` from 10s to 2s (or use `watchdog` library for event-driven file watching).

### B23. Species photo updates — PARTIAL
All 11 species have 4 images each (updated March 11). Quality/relevance unknown.

**After visits (F3):** Best frame per visit = automatic photo candidate pipeline. Query for highest-confidence visits per species, offer as cover photo candidates.

---

## Tier 6: SQL & Query Optimizations (Audit Findings)

### Q-SQL1. N+1 query in `get_food_activity()` — DONE (March 22)
food_log now JOINed with classifications on timestamp range in SQL. N+1 loop eliminated.

### Q-SQL2. Python loops doing SQL's job — DONE (March 22)
- `get_activity_heatmap()`: hour arrays now built with `GROUP BY CAST(SUBSTR(time,1,2) AS INTEGER)`
- `get_species_activity()`: hour/dow extraction moved to SQL
- `cull_trash_species()`: now uses `ORDER BY raw_score` in SQL

### Q-SQL3. Missing composite indexes
- `(action, common_name)` — speeds up species queries
- `(source_date, action, common_name)` — speeds up date-filtered species queries

**Fix:** Add to INDEXES list in classifications_db.py. 2 lines.

### Q-SQL4. Connection pooling for food_log/birdnet queries — DONE (March 22)
api.py food_log and birdnet_db operations now use thread-local pooling consistent with classifications_db.py.

### Q-SQL5. Cache invalidation gaps
- BirdNET summary cache (30s) not invalidated on new detections
- Classification cache not invalidated on direct DB inserts (only on reviews)
- `review/pending` species list stale after new classifications arrive

---

## Tier 7: Frontend Architecture (Audit Findings)

### FE1. Split 5,600-line index.html
Natural split points identified:
1. `live-feed.js` — MSE/HLS/MP4 connections (~800 lines)
2. `species-panel.js` — popup, gallery, info (~500 lines)
3. `dashboard.js` — chart, grid, stats (~600 lines)
4. `review.js` — classification UI, cheat sheet (~900 lines)
5. `detections.js` — SSE handlers, overlay (~300 lines)
6. `utils.js` — escape, debounce, managed intervals (~200 lines)

**Note:** No build tools. Could use simple `<script>` tags or a single concatenation step. Keep it simple — this is a prototype.

### FE2. 85+ inline event handlers (onclick=)
Replace with event delegation via data attributes. Prevents listener accumulation, simplifies cleanup.

### FE3. Unbounded SSE array growth
`sseRealtimeDetections` capped at 500 via `.slice(-500)` (O(n)). `recentDetections` capped at 50 via `.slice(0, 50)`. Use ring buffers or fixed-length arrays.

### FE4. Memory leak risks
- Event listeners added without cleanup on re-render (tab clicks, chart re-render)
- Phase 8 fixed canvas listener stacking, but similar patterns remain in tab switchers

### FE5. Magic numbers scattered
Timing intervals, buffer sizes, cache limits hardcoded throughout. Extract to config object at top of script.

### FE6. Inline styles (59+ instances)
Display toggling via `.style.display = 'none'/'block'` instead of CSS class toggling. Use `.classList.toggle()`.

---

## Tier 8: Operational Gaps (Audit Findings)

### OPS1. Log rotation — MISSING
Total logs: ~279MB, growing ~93MB/week. Duplicate log files: `classifier.log` and `classifier-stdout.log` are the same content (98MB each).

**Fix:** Add newsyslog.conf or launchd-based rotation. Deduplicate classifier logs (single output path in plist).

### OPS2. Automated health checks — MISSING
`check_health.sh` exists (94 lines, checks PIDs, API, logs, NAS) but is never scheduled. No alerting.

**Fix:** Add LaunchAgent to run check_health.sh every 15 minutes. Log results. Alert on consecutive failures.

### OPS3. Stale config/go2rtc.yaml
`config/go2rtc.yaml` has old RTSP tokens and wrong WebRTC candidate IP. Root `go2rtc.yaml` is the active one.

**Fix:** Delete `config/go2rtc.yaml`.

### OPS4. Python environment inconsistency
Three different Python executables across 8 services:
- `/usr/bin/python3` (system) — bird-audio, bird-enhanced-audio, bird-livedetect (with PYTHONPATH to venv-coral)
- `/usr/local/bin/python3` — bird-capture
- `venv-coral/bin/python3` — bird-classifier
- `venv/bin/uvicorn` — bird-dashboard

System Python could be updated by macOS, breaking services. The PYTHONPATH hack works but is fragile.

**Note:** This is documented and understood (gotchas: unsigned binaries, pycoral requires 3.9). Low priority to change — just be aware.

### OPS5. RTSP token sync recovery gap
`bird-rtsp-sync` runs daily at 3:10 AM only. If NAS is unreachable at that moment, audio services won't get fresh tokens until tomorrow.

**Fix:** Add retry logic or run more frequently (every 6 hours).

### OPS6. No off-site backup
All data on single iMac. No automated backup of classifications.db or classified images.

**Note:** Low priority for a prototype, but worth a simple rsync to NAS as minimum.

### OPS7. Duplicate log files
`classifier.log` + `classifier-stdout.log` (98MB each) are the same content. `live_detector.log` + `live_detector_stdout.log` similar.

**Fix:** Point LaunchAgent stdout and Python file logging to the same path, or disable one.

### OPS8. HANDOFF.md has wrong reviews.jsonl path
Says `~/bird-snapshots/logs/reviews.jsonl` but actual location is `~/bird-classifier/dashboard/reviews.jsonl`.

**Fix:** Update HANDOFF.md.

---

## Tier 9: Future Work (From Strategic Docs)

These are documented in existing planning docs but not yet started.

### FW1. Wildlife Pipeline (SpeciesNet)
Detailed plan in `wildlife-pipeline-plan.md`. Google SpeciesNet (MegaDetector v5 + EfficientNet V2 M) for nocturnal mammal detection. Skunks, rabbits, deer, raccoons. Complements bird pipeline — mammals are active at night when bird classifier is idle.

**Dependency:** Foundation F1 (shared inference) provides reusable detection infrastructure.

### FW2. Frigate Stage 3: Dual-Stream Capture
Pull low-res sub-stream for detection, full-res only on confirmed bird. 4x faster YOLO on 480p. Requires changes to capture_snapshots.py and sync_snapshots.sh.

### FW3. Frigate Stage 4: Object Tracking (Norfair)
Persistent object IDs across frames. Detect once, track thereafter. Re-classify only if confidence is low. Most complex change — requires new `live_classifier.py`.

**Dependency:** Foundation F3 (visits) is the simpler version of this. Object tracking is the "proper" implementation.

### FW4. Frigate Stage 5: Confidence Accumulation
Accumulate classifications across first N frames of a visit. Voting/averaging to pick best species call. Extends existing voter pattern from live_detector.py to batch classifier.

### FW5. Audio-Visual Fusion
When BirdNET detects a species at the exact moment the camera sees a bird, fuse signals. Neither system alone may be confident, but together they could be very confident.

### FW6. Temporal Priors
Use time-of-day as Bayesian prior for classification. Species X feeds at dawn, species Y comes midday. A 55/45 split becomes clearer if one species is 10x more common at that hour.

---

## Implementation Order

```
Phase 0:   Quick Wins (OPS1-3, Q-SQL3, OPS7-8, B9)     ← DONE (March 22, 2026)
Phase 0.5: Mock RTSP Test Feeds (F0)                     ← DONE (March 22, 2026)
Phase 1:   Shared Inference Library (F1)                  ← DONE (March 22, 2026)
Phase 2:   Reviews → SQLite (F2)                         ← DONE (March 22, 2026)
Phase 3:   Visit-Based Event Model (F3)                  ← DONE (March 22, 2026)
Phase 4:   UI/Display Fixes (B1, B4-B8)                  ← DONE (March 22, 2026)
Phase 5:   Multi-Bird Review Rework (B12-B14, B16)       ← DONE (March 22, 2026)
Phase 6:   SQL Optimizations (Q-SQL1-2, Q-SQL4-5)        ← DONE (March 22, 2026)
Phase 7:   Frontend Architecture (FE1-6)                  ← DEFERRED (prototype not worth splitting yet)
Phase 8:   Audio Deep Dive (B17-B19)                      ← B17 RESOLVED, B19 INVESTIGATED, B18 next
Phase 9:   Future Work (FW1-6)                            ← when foundations are solid
```

Each phase gets:
- Git health checkpoint before starting
- Tests written alongside implementation
- Verification on mock RTSP feeds where applicable
- Git commit on completion

---

## Success Criteria

1. **Mock RTSP**: Can loop test video as RTSP, run full pipeline, get deterministic results. — **DONE**: test_clips/ with serve_test_feed.sh and README.
2. **Shared Inference**: classify.py and live_detector.py import from bird_inference.py. No duplicated YOLO/AIY code. SPECIES_ALIASES defined once. Motion gate works in both. parse_label bug fixed. — **DONE**: bird_inference.py (389 lines), solar_utils.py (97 lines). classify.py 1085→807 lines, live_detector.py ~842→657 lines. Motion gate added to live_detector.
3. **Reviews SQLite**: `review/pending` uses SQL JOIN, `get_classified_for_pending()` eliminated. No memory spike. JSONL backup continues (then retired). — **DONE**: reviews_db.py, 568 rows migrated from 1,015 JSONL lines. JSONL dual-write subsequently retired.
4. **Visits**: Dashboard shows visit-based counts. Daily visit count lower than detection count. Duration/co-occurrence queries work. — **DONE**: 31K detections → 10.9K visits (2.9x compression). visits_db.py, populate_visits.py, API endpoints /api/visits, /api/visit-summary, /api/visit-stats.
5. **Quick Wins**: Logs rotate. Health checks automated. Indexes added. Stale configs removed. — **DONE**: composite indexes added, config/go2rtc.yaml deleted, Slate-colored Junco removed, newsyslog rotation configured, bird-healthcheck LaunchAgent added.
6. **UI Fixes**: Unified timestamps, species heading, bird names in yard, clickable chart, goals auto-collapse, full species dropdown. — **DONE (March 22)**: All 6 items (B1, B4-B8) complete.
7. **Multi-Bird Review**: "Multiple birds" filter, per-bird buttons, confirm dialog. — **DONE (March 22)**: All 4 items (B12-B14, B16) complete.
8. **SQL Optimizations**: N+1 eliminated, Python loops pushed to SQL, connection pooling. — **DONE (March 22)**: Q-SQL1, Q-SQL2, Q-SQL4 complete.
9. **Wrong-species file move bug**: Images now move to the correct species directory when a correction is submitted (was moving to original-species directory). — **FIXED (March 22)**.
10. **Tests**: Each foundation has unit + integration tests. Mock RTSP for end-to-end verification. — **DONE**: 100 tests passing, 4 skipped. test_bird_inference.py (28), test_solar_utils.py (8), test_integration.py (4), test_reviews_db.py (23), test_reviews_integration.py (6), test_visits_db.py (23), test_visits_integration.py (8).