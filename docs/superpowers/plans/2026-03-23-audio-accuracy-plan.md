# Audio Detection Accuracy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the deep detection accumulator with BirdNET-Go-style overlap confirmation, add multi-camera support (ground + magnolia), and A/B test preprocessing — to match Merlin-quality accuracy.

**Architecture:** New `OverlapConfirmation` class replaces `DetectionAccumulator`. Analysis loop changes from 6s-buffer/3s-advance to 3s-chunks/1s-steps. Multi-camera runs as separate threads sharing one BirdNET model. A/B testing runs dual inference passes.

**Tech Stack:** Python 3.9, birdnetlib (BirdNET V2.4), PyAV, SQLite, numpy, scipy

**Spec:** `docs/superpowers/specs/2026-03-23-audio-accuracy-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `overlap_confirmation.py` | Create | OverlapConfirmation class — pending accumulator with flush window |
| `tests/test_overlap_confirmation.py` | Create | Unit tests for overlap confirmation logic |
| `audio_analyzer.py` | Modify | Use OverlapConfirmation, multi-camera, A/B testing, tune thresholds |
| `birdnet_local.db` | Migrate | Add source, multi_source, preprocessing, confirmations columns |

---

### Task 1: OverlapConfirmation Class

**Files:**
- Create: `overlap_confirmation.py`
- Create: `tests/test_overlap_confirmation.py`

The core accuracy improvement. A new class that replaces `DetectionAccumulator`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_overlap_confirmation.py
"""Tests for overlap_confirmation.py — BirdNET-Go style overlap confirmation."""
import time
import pytest

class TestOverlapConfirmation:
    """Test overlap-based detection confirmation."""

    def test_single_detection_below_min_not_accepted(self):
        """One detection in a window is not enough at level 1 (min_confirmations=2)."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        results = oc.add("House Finch", 0.75, {"common_name": "House Finch", "confidence": 0.75}, now=100.0)
        assert results == []  # not yet confirmed
        # Flush after window expires
        results = oc.flush(now=107.0)
        assert results == []  # only 1 confirmation, needs 2

    def test_two_detections_accepted(self):
        """Two detections of same species in window → accepted."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("House Finch", 0.65, {"common_name": "House Finch", "confidence": 0.65, "start_time": 0}, now=100.0)
        oc.add("House Finch", 0.72, {"common_name": "House Finch", "confidence": 0.72, "start_time": 1}, now=101.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1
        assert results[0]["common_name"] == "House Finch"
        assert results[0]["confidence"] == 0.72  # best confidence
        assert results[0]["confirmations"] == 2

    def test_three_detections_returns_best(self):
        """Three confirmations: accepted with highest confidence."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("Blue Jay", 0.60, {"common_name": "Blue Jay", "confidence": 0.60}, now=100.0)
        oc.add("Blue Jay", 0.80, {"common_name": "Blue Jay", "confidence": 0.80}, now=101.0)
        oc.add("Blue Jay", 0.70, {"common_name": "Blue Jay", "confidence": 0.70}, now=102.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1
        assert results[0]["confidence"] == 0.80
        assert results[0]["confirmations"] == 3

    def test_different_species_tracked_separately(self):
        """Different species are tracked independently."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("House Finch", 0.65, {"common_name": "House Finch", "confidence": 0.65}, now=100.0)
        oc.add("Blue Jay", 0.70, {"common_name": "Blue Jay", "confidence": 0.70}, now=100.5)
        oc.add("House Finch", 0.72, {"common_name": "House Finch", "confidence": 0.72}, now=101.0)
        # Blue Jay only has 1 hit — not enough
        results = oc.flush(now=107.0)
        assert len(results) == 1
        assert results[0]["common_name"] == "House Finch"

    def test_level0_accepts_everything(self):
        """Level 0 (min_confirmations=1): accept on first detection."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=1)
        oc.add("Cardinal", 0.55, {"common_name": "Cardinal", "confidence": 0.55}, now=100.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1

    def test_expired_detections_flushed_automatically(self):
        """Detections are flushed when their window expires."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("House Finch", 0.65, {"common_name": "House Finch", "confidence": 0.65}, now=100.0)
        oc.add("House Finch", 0.70, {"common_name": "House Finch", "confidence": 0.70}, now=101.0)
        # Not expired yet at 105
        results = oc.flush(now=105.0)
        assert results == []
        # Expired at 107 (100 + 6 + 1)
        results = oc.flush(now=107.0)
        assert len(results) == 1

    def test_new_window_after_flush(self):
        """After a species is flushed, new detections start a fresh window."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        oc.add("House Finch", 0.65, {"common_name": "House Finch", "confidence": 0.65}, now=100.0)
        oc.add("House Finch", 0.70, {"common_name": "House Finch", "confidence": 0.70}, now=101.0)
        results = oc.flush(now=107.0)
        assert len(results) == 1
        # New detection starts a new window
        oc.add("House Finch", 0.68, {"common_name": "House Finch", "confidence": 0.68}, now=108.0)
        oc.add("House Finch", 0.75, {"common_name": "House Finch", "confidence": 0.75}, now=109.0)
        results = oc.flush(now=115.0)
        assert len(results) == 1
        assert results[0]["confidence"] == 0.75

    def test_no_cooldown(self):
        """No cooldown between acceptances — consecutive windows can both accept."""
        from overlap_confirmation import OverlapConfirmation
        oc = OverlapConfirmation(flush_window=6.0, min_confirmations=2)
        # Window 1
        oc.add("X", 0.6, {"common_name": "X", "confidence": 0.6}, now=100.0)
        oc.add("X", 0.7, {"common_name": "X", "confidence": 0.7}, now=101.0)
        r1 = oc.flush(now=107.0)
        assert len(r1) == 1
        # Window 2 — immediately after
        oc.add("X", 0.65, {"common_name": "X", "confidence": 0.65}, now=107.0)
        oc.add("X", 0.72, {"common_name": "X", "confidence": 0.72}, now=108.0)
        r2 = oc.flush(now=114.0)
        assert len(r2) == 1  # no cooldown blocking
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/bird-classifier && ~/bird-classifier/venv/bin/python -m pytest tests/test_overlap_confirmation.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement OverlapConfirmation**

```python
# overlap_confirmation.py
"""Overlap-based detection confirmation for BirdNET audio analysis.

Replaces the deep detection accumulator with BirdNET-Go's proven approach:
analyze overlapping 3-second windows, require N confirmations of the same
species within a flush window to accept a detection.

Used by audio_analyzer.py.
"""

import logging
import time

log = logging.getLogger(__name__)


class OverlapConfirmation:
    """Accumulate detections across overlapping analysis windows.

    For each species, tracks detections within a flush window (default 6s).
    When the window expires, accepts species with >= min_confirmations hits,
    using the highest-confidence detection as the representative.

    No cooldown — consecutive windows can both produce accepted detections.
    This matches BirdNET-Go's overlap confirmation model.
    """

    def __init__(self, flush_window=6.0, min_confirmations=2):
        """
        Args:
            flush_window: seconds to accumulate detections before deciding
            min_confirmations: minimum overlapping window hits to accept
        """
        self.flush_window = flush_window
        self.min_confirmations = min_confirmations
        # species -> {"first_seen": float, "count": int, "best_conf": float, "best_det": dict}
        self._pending = {}

    def add(self, species, confidence, det_dict, now=None):
        """Add a detection candidate from an overlapping window.

        Returns list of accepted detections if any pending species
        have expired windows (auto-flush on each add).
        """
        if now is None:
            now = time.time()

        if species not in self._pending:
            self._pending[species] = {
                "first_seen": now,
                "count": 1,
                "best_conf": confidence,
                "best_det": det_dict,
            }
        else:
            entry = self._pending[species]
            entry["count"] += 1
            if confidence > entry["best_conf"]:
                entry["best_conf"] = confidence
                entry["best_det"] = det_dict

        # Auto-flush any expired windows
        return self.flush(now)

    def flush(self, now=None):
        """Check for expired windows and return accepted detections.

        Returns list of detection dicts (with added 'confirmations' key)
        for species that met the min_confirmations threshold.
        Discards species that didn't meet threshold.
        """
        if now is None:
            now = time.time()

        accepted = []
        expired = []

        for species, entry in self._pending.items():
            age = now - entry["first_seen"]
            if age >= self.flush_window:
                if entry["count"] >= self.min_confirmations:
                    det = dict(entry["best_det"])
                    det["confirmations"] = entry["count"]
                    accepted.append(det)
                    log.info("Confirmed: %s (%d/%d windows, best %.0f%%)",
                             species, entry["count"], self.min_confirmations,
                             entry["best_conf"] * 100)
                else:
                    log.debug("Discarded: %s (%d/%d windows, insufficient)",
                              species, entry["count"], self.min_confirmations)
                expired.append(species)

        for species in expired:
            del self._pending[species]

        return accepted
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/bird-classifier && ~/bird-classifier/venv/bin/python -m pytest tests/test_overlap_confirmation.py -v`
Expected: All 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add overlap_confirmation.py tests/test_overlap_confirmation.py
git commit -m "feat: add OverlapConfirmation class for BirdNET-Go style detection"
```

---

### Task 2: Database Schema Migration

**Files:**
- Modify: `audio_analyzer.py:297-318` (init_db function)

Add new columns to the `notes` table and update `insert_detection`.

- [ ] **Step 1: Update init_db to add columns**

In `audio_analyzer.py`, modify `init_db()` to add the new columns after table creation:

```python
def init_db():
    """Create the notes table if it doesn't exist. Opens persistent connection."""
    global _db_conn
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db_conn = sqlite3.connect(str(DB_PATH), timeout=10, check_same_thread=False)
    _db_conn.execute("PRAGMA journal_mode=WAL")
    _db_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_node TEXT DEFAULT '',
            date TEXT NOT NULL,
            time TEXT NOT NULL,
            common_name TEXT NOT NULL,
            scientific_name TEXT NOT NULL,
            confidence REAL NOT NULL,
            clip_name TEXT DEFAULT '',
            input_file TEXT DEFAULT ''
        )
        """
    )
    # Add columns for multi-camera and A/B testing (idempotent)
    for col_sql in [
        "ALTER TABLE notes ADD COLUMN source TEXT DEFAULT 'ground'",
        "ALTER TABLE notes ADD COLUMN multi_source INTEGER DEFAULT 0",
        "ALTER TABLE notes ADD COLUMN preprocessing TEXT DEFAULT 'raw'",
        "ALTER TABLE notes ADD COLUMN confirmations INTEGER DEFAULT 1",
    ]:
        try:
            _db_conn.execute(col_sql)
        except sqlite3.OperationalError:
            pass  # column already exists
    _db_conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_source ON notes(source)")
    _db_conn.commit()
    log.info("Database ready: %s", DB_PATH)
```

- [ ] **Step 2: Update insert_detection to use new columns**

```python
def insert_detection(det, clip_name, source="ground", preprocessing="raw", confirmations=1):
    """Insert a detection row into the database."""
    now = datetime.datetime.now()
    with _db_lock:
        _db_conn.execute(
            """
            INSERT INTO notes (source_node, date, time, common_name,
                               scientific_name, confidence, clip_name, input_file,
                               source, preprocessing, confirmations)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source,
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S"),
                det["common_name"],
                det["scientific_name"],
                round(det["confidence"], 3),
                clip_name,
                "",
                source,
                preprocessing,
                confirmations,
            ),
        )
        _db_conn.commit()
```

- [ ] **Step 3: Verify existing tests still pass**

Run: `cd ~/bird-classifier && ~/bird-classifier/venv/bin/python -m pytest tests/ -v --tb=short`
Expected: All tests pass

- [ ] **Step 4: Commit**

```bash
git add audio_analyzer.py
git commit -m "feat: add source, preprocessing, confirmations columns to notes table"
```

---

### Task 3: Replace Deep Detection with Overlap Confirmation

**Files:**
- Modify: `audio_analyzer.py`

This is the core refactor of the analysis loop.

- [ ] **Step 1: Update imports and constants**

At the top of `audio_analyzer.py`:

Add import:
```python
from overlap_confirmation import OverlapConfirmation
```

Remove constants (lines 64-70):
```python
# DELETE these:
DEEP_DETECTION_ENABLED = True
DEEP_DETECTION_WINDOW = 15.0
DEEP_DETECTION_MIN_HITS = 2
DEEP_DETECTION_INSTANT = 0.65
DEEP_DETECTION_COOLDOWN = 10.0
```

Add new constants:
```python
# ── Overlap Confirmation ─────────────────────────────────────────────────
OVERLAP_CONFIRMATION_ENABLED = True
OVERLAP_FLUSH_WINDOW = 6.0     # seconds to accumulate before deciding
OVERLAP_MIN_CONFIRMATIONS = 2  # overlapping windows needed to accept (level 1)
```

Change `DYNAMIC_THRESHOLD_MIN`:
```python
DYNAMIC_THRESHOLD_MIN = 0.20       # lowest a dynamic threshold can go (was 0.25)
```

Change analysis window constants to use 3s chunks with 1s steps:
```python
ANALYSIS_SECONDS = 3   # feed 3 seconds at a time (single BirdNET window)
ANALYSIS_BYTES = SAMPLE_RATE * 2 * CHANNELS * ANALYSIS_SECONDS
ADVANCE_SECONDS = 1    # slide by 1 second (2s overlap between consecutive chunks)
ADVANCE_BYTES = SAMPLE_RATE * 2 * CHANNELS * ADVANCE_SECONDS
```

Remove or comment out the old overlap setting:
```python
# OVERLAP = 2.0  — no longer used; overlap is handled by our sliding window
```

- [ ] **Step 2: Remove DetectionAccumulator class**

Delete the entire `DetectionAccumulator` class (lines 223-289).

- [ ] **Step 3: Update run() function**

In `run()`, replace:
```python
    accumulator = DetectionAccumulator() if DEEP_DETECTION_ENABLED else None
```
with:
```python
    confirmer = OverlapConfirmation(
        flush_window=OVERLAP_FLUSH_WINDOW,
        min_confirmations=OVERLAP_MIN_CONFIRMATIONS,
    ) if OVERLAP_CONFIRMATION_ENABLED else None
```

In the BirdNET inference section, change the `RecordingBuffer` call to NOT use internal overlap (we handle it externally now):
```python
    recording = RecordingBuffer(
        analyzer, audio, SAMPLE_RATE,
        lat=LAT, lon=LON,
        date=datetime.datetime.now(),
        min_conf=0.25,
        overlap=0.0,  # no internal overlap — we handle it via sliding window
    )
```

Replace the deep detection accumulator usage (lines 558-567):
```python
                        # Old: deep detection accumulator
                        # if accumulator:
                        #     accepted, best_det = accumulator.add(...)
                        #     if not accepted: continue

                        # New: overlap confirmation — add to pending
                        if confirmer:
                            accepted_dets = confirmer.add(
                                species, conf, det, now_time
                            )
                            # Don't process this detection now — it goes through
                            # the confirmation pipeline. Accepted detections are
                            # returned from flush() below.
                            continue
```

After the per-chunk detection loop (after `best_per_slice.values()` loop), add flush handling:
```python
                    # Flush confirmed detections from overlap confirmation
                    if confirmer:
                        for confirmed_det in confirmer.flush(now_time):
                            species = confirmed_det["common_name"]
                            conf = confirmed_det["confidence"]
                            confirmations = confirmed_det.get("confirmations", 1)

                            # Range filter
                            if range_filter:
                                validation = range_filter.is_species_valid_at_location(
                                    species, confidence=conf,
                                    date=datetime.datetime.now()
                                )
                                if not validation["valid"]:
                                    _metrics.counter("rejected_range").inc()
                                    log.info("Range filter rejected: %s (%.0f%%) — %s",
                                             species, conf * 100, validation["reason"])
                                    continue

                            # Dynamic threshold learning
                            if dyn_thresh:
                                dyn_thresh.record_detection(species, conf)

                            # Save clip from the original detection
                            start_sec = confirmed_det.get("start_time", 0)
                            clip_start = int(start_sec * SAMPLE_RATE * 2)
                            clip_end = clip_start + CHUNK_BYTES
                            if clip_end > len(raw):
                                clip_start = max(0, len(raw) - CHUNK_BYTES)
                                clip_end = len(raw)
                            clip_raw = raw[clip_start:clip_end]

                            try:
                                clip_name = save_clip(clip_raw, confirmed_det)
                            except Exception as e:
                                log.warning("Failed to save clip: %s", e)
                                clip_name = ""
                            insert_detection(confirmed_det, clip_name,
                                             source="ground", confirmations=confirmations)
                            _metrics.counter("accepted").inc()
                            total_detections += 1
                            log.info("Detection #%d: %s (%.0f%%, %d confirms) — %s",
                                     total_detections, species, conf * 100,
                                     confirmations, clip_name)
```

Also update the non-confirmer path (when `OVERLAP_CONFIRMATION_ENABLED = False`) to keep the direct-accept behavior for testing.

- [ ] **Step 4: Update startup log**

Remove references to deep detection in startup logging. Add overlap confirmation info:
```python
    if OVERLAP_CONFIRMATION_ENABLED:
        log.info("  Overlap confirmation: %ds window, %d min confirmations",
                 OVERLAP_FLUSH_WINDOW, OVERLAP_MIN_CONFIRMATIONS)
```

- [ ] **Step 5: Verify syntax and existing tests**

```bash
cd ~/bird-classifier
~/bird-classifier/venv/bin/python -c "import audio_analyzer; print('OK')"
~/bird-classifier/venv/bin/python -m pytest tests/ -v --tb=short
```

- [ ] **Step 6: Commit**

```bash
git add audio_analyzer.py
git commit -m "refactor: replace deep detection with overlap confirmation in audio_analyzer"
```

---

### Task 4: Multi-Camera Support

**Files:**
- Modify: `audio_analyzer.py`

Add a second analysis thread for magnolia cam, sharing the BirdNET model.

- [ ] **Step 1: Add camera configuration**

Add near top of `audio_analyzer.py`:
```python
# ── Multi-Camera Configuration ───────────────────────────────────────────
CAMERAS = [
    {"name": "ground", "preferred": "ground", "fallback": "magnolia"},
    {"name": "magnolia", "preferred": "magnolia", "fallback": "ground"},
]
MULTI_CAMERA_ENABLED = True
CROSS_CAM_DEDUP_WINDOW = 10  # seconds — same species on 2 cams = multi_source
```

- [ ] **Step 2: Refactor run() to accept camera config**

Extract the analysis loop into a function that takes camera name as parameter:

```python
def analyze_camera(analyzer, camera_name, preferred_stream, fallback_stream,
                   range_filter, test_mode=False):
    """Run analysis loop for a single camera. Called from run() per camera thread."""
    # ... (existing analysis loop, parameterized with camera_name for logging/DB)
```

The `run()` function becomes:
```python
def run(test_mode=False):
    # Load model (shared)
    analyzer = Analyzer()
    init_db()
    range_filter = ...
    # Warmup ...

    if MULTI_CAMERA_ENABLED and not test_mode:
        threads = []
        for cam in CAMERAS:
            t = threading.Thread(
                target=analyze_camera,
                args=(analyzer, cam["name"], cam["preferred"], cam["fallback"],
                      range_filter, False),
                daemon=True,
                name=f"audio-{cam['name']}",
            )
            t.start()
            threads.append(t)
            log.info("Started analysis thread for camera: %s", cam["name"])

        # Wait for all threads (they run until shutdown)
        for t in threads:
            t.join()
    else:
        # Single camera mode (test mode or disabled)
        analyze_camera(analyzer, "ground", "ground", "magnolia",
                       range_filter, test_mode)
```

- [ ] **Step 3: Add cross-camera deduplication**

After inserting a detection, check if same species was detected on another camera within ±10s:

```python
def check_cross_camera(species, source, timestamp_str):
    """Check if same species was detected on a different camera within ±10s.
    If so, mark both as multi_source."""
    with _db_lock:
        rows = _db_conn.execute(
            """
            SELECT id FROM notes
            WHERE common_name = ? AND source != ? AND date = ?
            AND ABS(
                (CAST(SUBSTR(time,1,2) AS INTEGER)*3600 +
                 CAST(SUBSTR(time,4,2) AS INTEGER)*60 +
                 CAST(SUBSTR(time,7,2) AS INTEGER))
                -
                (CAST(SUBSTR(?,1,2) AS INTEGER)*3600 +
                 CAST(SUBSTR(?,4,2) AS INTEGER)*60 +
                 CAST(SUBSTR(?,7,2) AS INTEGER))
            ) <= ?
            """,
            (species, source, timestamp_str[:10], timestamp_str[11:],
             timestamp_str[11:], timestamp_str[11:], CROSS_CAM_DEDUP_WINDOW),
        ).fetchall()
        if rows:
            ids = [r[0] for r in rows]
            _db_conn.execute(
                f"UPDATE notes SET multi_source = 1 WHERE id IN ({','.join('?' * len(ids))})",
                ids,
            )
            _db_conn.commit()
            return True
    return False
```

- [ ] **Step 4: Verify and commit**

```bash
~/bird-classifier/venv/bin/python -c "import audio_analyzer; print('OK')"
~/bird-classifier/venv/bin/python -m pytest tests/ -v --tb=short
git add audio_analyzer.py
git commit -m "feat: add multi-camera support (ground + magnolia) to audio analyzer"
```

---

### Task 5: A/B Preprocessing Testing

**Files:**
- Modify: `audio_analyzer.py`

Run BirdNET inference on both raw+EQ and fully preprocessed audio.

- [ ] **Step 1: Add A/B test config**

```python
AB_TEST_ENABLED = os.environ.get("AUDIO_AB_TEST", "").lower() in ("1", "true", "yes")
```

- [ ] **Step 2: Add dual inference in analysis loop**

In the analysis loop, after the primary inference, add:

```python
                    # A/B Test: also run inference on raw+EQ audio (no noisereduce)
                    if AB_TEST_ENABLED:
                        # Raw path: bandpass only, no noisereduce/RMS
                        from scipy.signal import sosfilt
                        audio_raw_eq = sosfilt(_bandpass_sos, audio_raw).astype(np.float32)

                        try:
                            recording_raw = RecordingBuffer(
                                analyzer, audio_raw_eq, SAMPLE_RATE,
                                lat=LAT, lon=LON,
                                date=datetime.datetime.now(),
                                min_conf=0.25,
                                overlap=0.0,
                            )
                            with contextlib.redirect_stdout(io.StringIO()):
                                recording_raw.analyze()

                            # Process raw detections through same pipeline
                            for det in recording_raw.detections:
                                species = det["common_name"]
                                conf = det["confidence"]
                                if dyn_thresh and not dyn_thresh.should_accept(species, conf):
                                    continue
                                elif not dyn_thresh and conf < MIN_CONFIDENCE:
                                    continue
                                if confirmer:
                                    # Use separate confirmer for raw path
                                    # (would need a second OverlapConfirmation instance)
                                    pass  # For now, log only
                                insert_detection(det, "", source=camera_name,
                                                 preprocessing="raw_eq")
                        except Exception as e:
                            log.debug("A/B raw inference error: %s", e)
```

Note: Full A/B testing with separate confirmation pipelines adds complexity. For the initial implementation, just log raw+EQ detections directly (no confirmation) with `preprocessing="raw_eq"` to compare raw counts. The confirmed enhanced detections are the primary path.

- [ ] **Step 3: Commit**

```bash
git add audio_analyzer.py
git commit -m "feat: add A/B preprocessing test mode (AUDIO_AB_TEST env var)"
```

---

### Task 6: Threshold Tuning & Cleanup

**Files:**
- Modify: `audio_analyzer.py`

- [ ] **Step 1: Apply threshold changes**

Already done in Task 3 (`DYNAMIC_THRESHOLD_MIN = 0.20`). Verify and clean up any remaining references to deep detection.

- [ ] **Step 2: Remove dead code**

Search for and remove any remaining references to:
- `DEEP_DETECTION_*` constants
- `DetectionAccumulator` class
- `accumulator` variable
- Old `OVERLAP` constant (replaced by sliding window)

- [ ] **Step 3: Run all tests**

```bash
~/bird-classifier/venv/bin/python -m pytest tests/ -v --tb=short
```

- [ ] **Step 4: Commit**

```bash
git add audio_analyzer.py
git commit -m "chore: clean up dead deep detection code, finalize threshold tuning"
```

---

### Task 7: Restart Services & Verify

**Files:** None (operational)

- [ ] **Step 1: Run all tests**

```bash
cd ~/bird-classifier && ~/bird-classifier/venv/bin/python -m pytest tests/ -v
```

- [ ] **Step 2: Restart audio analyzer**

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-audio"
sleep 10
tail -30 ~/bird-snapshots/logs/audio-analyzer-stderr.log
```

Expected: Shows "Overlap confirmation: 6s window, 2 min confirmations" and camera threads starting.

- [ ] **Step 3: Verify detections are flowing (daytime only)**

```bash
# Wait a few minutes, then check
sqlite3 ~/bird-snapshots/birdnet-audio/birdnet_local.db "
SELECT source, COUNT(*), MAX(time)
FROM notes WHERE date = '$(date +%Y-%m-%d)'
GROUP BY source;
"
```

- [ ] **Step 4: Compare with BirdNET-Go**

```bash
# After a few hours of daytime data
ssh -p 2000 -i ~/.ssh/id_ed25519 -o BatchMode=yes vives@192.168.5.92 "
sudo sqlite3 /volume1/docker/birdnet/data/birdnet.db '
SELECT common_name, COUNT(*) FROM notes
WHERE date = \"$(date +%Y-%m-%d)\"
GROUP BY common_name ORDER BY COUNT(*) DESC LIMIT 15;'
"
```

Compare species counts and coverage.

- [ ] **Step 5: Tag release**

```bash
git tag v1.0-audio-accuracy
```
