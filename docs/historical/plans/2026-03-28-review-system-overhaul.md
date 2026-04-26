> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Review System Overhaul + Dashboard Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 17 review system bugs with a single `apply_verdict()` function, plus 6 dashboard fixes (audio health, species combobox, food rate, NAS removal, heatmap label).

**Architecture:** Extract all file-movement + DB-update logic into one `apply_verdict()` function called by every review endpoint. Fix dashboard UI issues inline. Remove dead NAS code.

**Tech Stack:** Python/FastAPI, SQLite, vanilla JS, Chart.js

**Spec:** `docs/superpowers/specs/2026-03-28-review-system-overhaul-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `dashboard/api.py` | Modify | `apply_verdict()`, fix 5 review endpoints, audio health, food rate, NAS removal |
| `dashboard/index.html` | Modify | Species combobox, random species, heatmap label, NAS dot removal, verdict labels |
| `classifications_db.py` | Modify | Add `update_common_name()`, fix `count_classified()` |
| `classify.py` | Modify | Startup orphan cleanup |
| `live_detector.py` | Modify | Remove NAS comments |
| `GUIDE.md` | Modify | Note NAS decommissioned |
| `tests/test_apply_verdict.py` | Create | Tests for apply_verdict logic |

---

### Task 1: `apply_verdict()` Core Function + Tests

**Files:**
- Modify: `dashboard/api.py`
- Modify: `classifications_db.py`
- Create: `tests/test_apply_verdict.py`

- [ ] **Step 1: Add `update_common_name()` to classifications_db.py**

Add after `get_entry_by_file()` (line ~279):

```python
def update_common_name(filename, new_species):
    """Update the common_name for a classification after a review correction."""
    conn = get_conn(readonly=False)
    conn.execute(
        "UPDATE classifications SET common_name = ? WHERE file = ?",
        (new_species, filename),
    )
    conn.commit()
```

- [ ] **Step 2: Fix `count_classified()` to exclude trashed/not_a_bird**

Replace `count_classified()` (line 260-262):

```python
def count_classified():
    conn = get_conn(readonly=True)
    return conn.execute(
        "SELECT COUNT(*) FROM classifications c "
        "WHERE c.action='classified' "
        "AND NOT EXISTS ("
        "  SELECT 1 FROM reviews r WHERE r.file = c.file "
        "  AND (r.verdict = 'trash' OR (r.verdict = 'wrong' AND r.correct_species = 'not_a_bird'))"
        ")"
    ).fetchone()[0]
```

Note: This requires the reviews table to be accessible. Add import if needed:

```python
# At top of file, check if reviews table exists in same DB or separate
# Reviews are in the SAME SQLite file (classifications.db has both tables)
```

- [ ] **Step 3: Write tests for apply_verdict logic**

Create `tests/test_apply_verdict.py`:

```python
"""Tests for apply_verdict — file movement + DB update logic."""

import os
import shutil
import sqlite3
import tempfile
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


class TestApplyVerdictRules:
    """Test file movement rules for each verdict type."""

    def setup_method(self):
        """Create temp directory structure mimicking bird-snapshots."""
        self.tmpdir = Path(tempfile.mkdtemp())
        self.classified = self.tmpdir / "classified"
        self.annotated = self.tmpdir / "annotated"
        self.trash = self.tmpdir / "trash"
        self.skipped = self.tmpdir / "skipped"
        for d in [self.classified, self.annotated, self.trash, self.skipped]:
            d.mkdir()

        # Create a test image in classified/Song_Sparrow/
        species_dir = self.classified / "Song_Sparrow"
        species_dir.mkdir()
        self.test_file = "test_bird.jpg"
        (species_dir / self.test_file).write_text("fake image")
        (self.annotated / self.test_file).write_text("fake annotated")

    def teardown_method(self):
        shutil.rmtree(self.tmpdir)

    def test_correct_verdict_no_move(self):
        """Correct verdict: file stays in original species folder."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            self.test_file, "correct", "",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert (self.classified / "Song_Sparrow" / self.test_file).exists()
        assert result["moved"] is False

    def test_wrong_with_correction_moves_file(self):
        """Wrong + correction: file moves to corrected species folder."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            self.test_file, "wrong", "House Finch",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert not (self.classified / "Song_Sparrow" / self.test_file).exists()
        assert (self.classified / "House_Finch" / self.test_file).exists()
        assert result["moved"] is True

    def test_wrong_not_a_bird_moves_to_trash(self):
        """Wrong + not_a_bird: classified goes to trash, annotated deleted."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            self.test_file, "wrong", "not_a_bird",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert not (self.classified / "Song_Sparrow" / self.test_file).exists()
        assert (self.trash / self.test_file).exists()
        assert not (self.annotated / self.test_file).exists()  # deleted
        assert result["moved"] is True

    def test_trash_verdict_moves_both(self):
        """Trash: both classified and annotated go to trash."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            self.test_file, "trash", "",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert not (self.classified / "Song_Sparrow" / self.test_file).exists()
        assert (self.trash / self.test_file).exists()
        assert result["moved"] is True

    def test_skip_verdict_moves_to_skipped(self):
        """Skip: classified file goes to skipped/."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            self.test_file, "skip", "",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert not (self.classified / "Song_Sparrow" / self.test_file).exists()
        assert (self.skipped / self.test_file).exists()
        # Annotated stays
        assert (self.annotated / self.test_file).exists()
        assert result["moved"] is True

    def test_reclassify_no_move(self):
        """Reclassify (missed): file stays put."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            self.test_file, "reclassify", "",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert (self.classified / "Song_Sparrow" / self.test_file).exists()
        assert result["moved"] is False

    def test_missing_file_returns_error(self):
        """Missing file returns error, doesn't crash."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            "nonexistent.jpg", "trash", "",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert result["error"] is not None

    def test_wrong_correction_creates_target_dir(self):
        """Target species directory is created if it doesn't exist."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            self.test_file, "wrong", "Pileated Woodpecker",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert (self.classified / "Pileated_Woodpecker" / self.test_file).exists()

    def test_sanitize_species_name(self):
        """Species names with apostrophes/spaces are sanitized for directory names."""
        from dashboard.api import _apply_verdict_files
        result = _apply_verdict_files(
            self.test_file, "wrong", "Lincoln's Sparrow",
            self.classified, self.annotated, self.trash, self.skipped
        )
        assert (self.classified / "Lincolns_Sparrow" / self.test_file).exists()
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_apply_verdict.py -v`
Expected: FAIL — `_apply_verdict_files` doesn't exist yet.

- [ ] **Step 5: Implement `_apply_verdict_files()` and `apply_verdict()` in api.py**

Add after the `_create_review_entry` function (around line 175) in `dashboard/api.py`:

```python
def _apply_verdict_files(filename, verdict, correct_species,
                         classified_dir=None, annotated_dir=None,
                         trash_dir=None, skipped_dir=None):
    """Move files to match verdict. Pure file logic, no DB writes.

    Returns {"moved": bool, "from_dir": str|None, "to_dir": str|None, "error": str|None}
    """
    classified_dir = classified_dir or CLASSIFIED_DIR
    annotated_dir = annotated_dir or ANNOTATED_DIR
    trash_dir = trash_dir or TRASH_DIR
    skipped_dir = skipped_dir or Path(BASE_DIR / "skipped")

    def _find(name):
        for d in classified_dir.iterdir():
            if d.is_dir():
                candidate = d / name
                if candidate.exists():
                    return candidate
        return None

    def _sanitize(species):
        return species.replace(" ", "_").replace("'", "").replace("/", "-")

    result = {"moved": False, "from_dir": None, "to_dir": None, "error": None}

    if verdict in ("correct", "reclassify"):
        # No file movement needed
        return result

    src = _find(filename)

    if verdict == "trash" or (verdict == "wrong" and correct_species == "not_a_bird"):
        # Move both classified and annotated to trash
        trash_dir.mkdir(parents=True, exist_ok=True)
        if src:
            result["from_dir"] = src.parent.name
            shutil.move(str(src), str(trash_dir / filename))
            result["moved"] = True
            result["to_dir"] = "trash"
        else:
            result["error"] = f"File not found in classified/: {filename}"
        # Also trash annotated copy (same filename, no prefix)
        ann = annotated_dir / filename
        if ann.exists():
            ann.unlink()

    elif verdict == "wrong" and correct_species:
        # Move to corrected species directory
        safe_name = _sanitize(correct_species)
        dst_dir = classified_dir / safe_name
        dst_dir.mkdir(parents=True, exist_ok=True)
        if src:
            result["from_dir"] = src.parent.name
            shutil.move(str(src), str(dst_dir / filename))
            result["moved"] = True
            result["to_dir"] = safe_name
        else:
            result["error"] = f"File not found in classified/: {filename}"

    elif verdict == "skip" or (verdict == "wrong" and not correct_species):
        # Move to skipped
        skipped_dir.mkdir(parents=True, exist_ok=True)
        if src:
            result["from_dir"] = src.parent.name
            shutil.move(str(src), str(skipped_dir / filename))
            result["moved"] = True
            result["to_dir"] = "skipped"
        else:
            result["error"] = f"File not found in classified/: {filename}"

    return result


def apply_verdict(filename, verdict, correct_species=""):
    """Move file + update DB to match verdict. Single source of truth.

    Called by all review endpoints. Handles file movement AND DB updates.
    """
    result = _apply_verdict_files(filename, verdict, correct_species)

    # Update classifications.common_name if species was corrected
    if (verdict == "wrong" and correct_species
            and correct_species != "not_a_bird" and result["moved"]):
        try:
            cdb.update_common_name(filename, normalize_species(correct_species))
        except Exception as e:
            logging.warning("Failed to update common_name for %s: %s", filename, e)

    if result["moved"]:
        logging.info("apply_verdict: %s → %s (%s → %s)",
                     filename, verdict, result["from_dir"], result["to_dir"])
    elif result["error"]:
        logging.warning("apply_verdict: %s — %s", filename, result["error"])

    return result
```

- [ ] **Step 6: Run tests**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_apply_verdict.py -v`
Expected: All tests PASS.

- [ ] **Step 7: Commit**

```bash
git add dashboard/api.py classifications_db.py tests/test_apply_verdict.py
git commit -m "feat: apply_verdict() — single source of truth for review file management"
```

---

### Task 2: Wire All Review Endpoints to `apply_verdict()`

**Files:**
- Modify: `dashboard/api.py:932-990` (batch endpoints)
- Modify: `dashboard/api.py:648-690` (bulk reclassify)
- Modify: `dashboard/api.py:1297-1326` (submit_review)
- Modify: `dashboard/api.py:1404-1410` (update_review)

- [ ] **Step 1: Fix `submit_review` — replace inline file logic with `apply_verdict()`**

Replace the entire file movement block in `submit_review` (everything between `rdb.insert_review(review)` and `return {"status": "ok", "review": review}`) with:

```python
    rdb.insert_review(review)
    invalidate_cache("stats:", "species:", "goals:", "highlights:", "profile:", "weekly_snapshot")

    # Move files + update DB to match verdict
    apply_verdict(review["file"], verdict, review.get("correct_species", ""))

    return {"status": "ok", "review": review}
```

- [ ] **Step 2: Fix `batch_confirm` — add `apply_verdict()` call per file**

In `batch_confirm` (line 932), after each successful INSERT, add the apply_verdict call. The verdict is "correct" so it's a no-op for files, but keeps the pattern consistent:

```python
    for f in files:
        try:
            rw_conn.execute(
                "INSERT OR IGNORE INTO reviews (file, verdict, timestamp, reviewer) "
                "VALUES (?, 'correct', ?, 'batch-review')",
                (f, now_ts),
            )
            count += 1
        except Exception:
            pass
    rw_conn.commit()
```

No `apply_verdict` needed for batch_confirm — "correct" verdict doesn't move files. Leave as-is.

- [ ] **Step 3: Fix `batch_reject` — add `apply_verdict()` call per file**

In `batch_reject` (line 962), after the commit, call `apply_verdict` for each file:

```python
    rw_conn.commit()
    # Move files to match rejection
    for f in files:
        apply_verdict(f, "wrong", correct)
    if count:
        invalidate_cache("stats:", "species:", "goals:", "highlights:", "profile:", "weekly_snapshot")
    return {"rejected": count, "correct_species": correct}
```

- [ ] **Step 4: Fix `bulk_reclassify` — add `apply_verdict()` call per file**

In `bulk_reclassify` (line 648), after the commit, call `apply_verdict` for each file:

```python
    rw_conn.commit()
    # Move files to match reclassification
    for f in files:
        apply_verdict(f[0], "wrong", to_species)
    invalidate_cache("pending", "stats", "species", "highlights", "profile", "weekly_snapshot")
```

- [ ] **Step 5: Fix `update_review` — add `apply_verdict()` call**

In `update_review` (line 1404), add apply_verdict after inserting the review:

```python
def update_review(filename: str, verdict: str, correct_species: str = "", missed_birds: str = "false", bird_index: str = "0"):
    """Update an existing review verdict."""
    review = _create_review_entry(filename, verdict, correct_species, missed_birds, bird_index)
    rdb.insert_review(review)
    apply_verdict(filename, verdict, normalize_species(correct_species) if correct_species else "")
    invalidate_cache("stats:", "species:", "goals:", "highlights:", "profile:", "weekly_snapshot")
    return {"status": "ok", "review": review}
```

- [ ] **Step 6: Run full test suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 7: Commit**

```bash
git add dashboard/api.py
git commit -m "fix: all review endpoints now call apply_verdict() for file management"
```

---

### Task 3: Startup Orphan Cleanup

**Files:**
- Modify: `classify.py:main()` around line 1005

- [ ] **Step 1: Add orphan cleanup function to classify.py**

Add before `main()`:

```python
def _cleanup_orphan_records():
    """Delete classification DB records where the image file no longer exists on disk."""
    from classifications_db import get_conn
    conn = get_conn(readonly=True)
    rows = conn.execute(
        "SELECT file, common_name FROM classifications WHERE action='classified' AND common_name IS NOT NULL"
    ).fetchall()

    orphans = []
    for row in rows:
        fname, species = row[0], row[1]
        if not species:
            continue
        safe_dir = species.replace(" ", "_").replace("'", "")
        classified_path = CLASSIFIED_DIR / safe_dir / fname
        annotated_path = ANNOTATED_DIR / fname
        if not classified_path.exists() and not annotated_path.exists():
            orphans.append(fname)

    if orphans:
        rw_conn = get_conn(readonly=False)
        for fname in orphans:
            rw_conn.execute("DELETE FROM classifications WHERE file = ?", (fname,))
        rw_conn.commit()
        # Also clean orphan reviews
        import reviews_db as rdb
        rw_reviews = rdb.get_conn(readonly=False)
        for fname in orphans:
            rw_reviews.execute("DELETE FROM reviews WHERE file = ?", (fname,))
        rw_reviews.commit()
        logging.info("Startup cleanup: removed %d orphan records (no file on disk)", len(orphans))
    else:
        logging.info("Startup cleanup: no orphan records found")
```

- [ ] **Step 2: Call it in main() before watch mode**

Add after the `vdb.end_stale_visits()` block (around line 1025), before `if args.reprocess`:

```python
    # Clean up orphan DB records (files that no longer exist on disk)
    try:
        _cleanup_orphan_records()
    except Exception as e:
        logging.warning("Orphan cleanup failed: %s", e)
```

- [ ] **Step 3: Run tests**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 4: Commit**

```bash
git add classify.py
git commit -m "feat: startup orphan cleanup — delete DB records for missing files"
```

---

### Task 4: Remove All NAS Code

**Files:**
- Modify: `dashboard/api.py:61,288-302`
- Modify: `dashboard/index.html:2058,2162,5999,6041-6042,6094`
- Modify: `live_detector.py:10,110,291`
- Modify: `GUIDE.md`

- [ ] **Step 1: Fix api.py NAS references**

Line 61 — remove or update comment mentioning NAS nginx proxy.

Lines 288-302 — in `_check_go2rtc()`, change error message from "NAS unreachable" to "go2rtc unreachable":

```python
    except Exception as e:
        err = str(e)
        detail = f"go2rtc unreachable ({err})"
        if "Connection refused" in err:
            detail += ". Docker container may need restart: docker restart go2rtc"
```

- [ ] **Step 2: Fix index.html NAS references**

Line 2058 — remove the NAS status dot:
```html
<!-- DELETE this line: -->
<span class="sdot" id="sdot-nas" title="NAS">●</span><span class="sdot-label">NAS</span>
```

Line 2162 — update camera hint:
```html
<div id="live-offline-hint" style="display:none;font-size:0.75rem;margin-top:6px;opacity:0.6;">Camera feeds require local network access</div>
```

Line 5999 — remove 'nas' from status dots array:
```javascript
['api','cams','audio'].forEach(function(id) {
```

Lines 6041-6042 — delete the NAS dot setter:
```javascript
// DELETE: _setDot('nas', svc.nas);
```

Line 6094 — remove NAS from services list:
```javascript
// DELETE: {key: 'nas', name: 'NAS Proxy', icon: '🌐'},
```

- [ ] **Step 3: Fix live_detector.py NAS comments**

Line 10: Change "proxied through nginx on the NAS" to "served by local go2rtc"
Line 110: Change "Auth cookie for NAS proxy" to "Auth cookie for API access"
Line 291: Change "proxied through nginx + Traefik on the NAS" to "served by local go2rtc"

- [ ] **Step 4: Update GUIDE.md**

Add a note at the top of the NAS section: "**NOTE: NAS (VivesSyn) was decommissioned March 2026. All services now run on the iMac.**"

- [ ] **Step 5: Commit**

```bash
git add dashboard/api.py dashboard/index.html live_detector.py GUIDE.md
git commit -m "chore: remove all NAS references — NAS decommissioned March 2026"
```

---

### Task 5: Audio Health + Heatmap Label + Food Rate Fix

**Files:**
- Modify: `dashboard/api.py:261-284` (audio health)
- Modify: `dashboard/api.py:2668-2675` (food rate)
- Modify: `dashboard/index.html:2478` (heatmap label)

- [ ] **Step 1: Fix audio health to always show detection count**

Replace `_check_audio_analyzer_health()` (lines 261-284):

```python
def _check_audio_analyzer_health():
    """Check audio_analyzer via metrics endpoint + DB for detection counts."""
    metrics = _fetch_service("http://localhost:8098/metrics", "Audio Analyzer")

    # Always augment with DB detection counts (even when analyzer is paused)
    try:
        conn = _get_birdnet_conn()
        if conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*), MAX(date || ' ' || time) FROM notes WHERE date = ?",
                        (datetime.now().strftime("%Y-%m-%d"),))
            row = cur.fetchone()
            metrics["detections_today"] = row[0] or 0
            metrics["last_detection"] = row[1] or "none"
    except Exception:
        pass

    # Always set detail string (not just when status is "ok")
    today = metrics.get("detections_today", 0)
    last = metrics.get("last_detection", "none")
    if metrics.get("status") == "ok":
        metrics["detail"] = f"Running, {today} detections today, last: {last}"
    else:
        metrics["detail"] = f"Paused (nighttime), {today} detections today, last: {last}"

    return metrics
```

- [ ] **Step 2: Fix food rate — exclude "unknown"**

Replace the food_prefs calculation (lines 2668-2675):

```python
    food_prefs = {}
    for food, count in by_food.items():
        if food == "unknown":
            continue  # Can't calculate rate without food log data
        hours = food_hours.get(food, 1)
        food_prefs[food] = {
            "detections": count,
            "hours_available": round(hours, 1),
            "rate_per_hour": round(count / max(hours, 0.1), 2),
        }
```

- [ ] **Step 3: Handle empty food state in the species activity JS**

In the JS that renders food preferences (search for "food_preferences" or "food-prefs" in `loadSpeciesActivity()`), add a check: if `food_preferences` is empty or undefined, show "No food data logged" instead of the food table.

- [ ] **Step 4: Add heatmap label**

In index.html line 2478, update the heatmap heading:

```html
<h3 style="margin:0 0 12px">Activity Heatmap <span style="font-size:12px; opacity:0.6;">(camera + audio, last 7 days)</span></h3>
```

- [ ] **Step 5: Run tests**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -v`
Expected: All tests pass.

- [ ] **Step 6: Commit**

```bash
git add dashboard/api.py dashboard/index.html
git commit -m "fix: audio health always shows counts, exclude unknown food, label heatmap"
```

---

### Task 6: Species Activity Combobox + Random Species on Load

**Files:**
- Modify: `dashboard/index.html:2448-2451` (HTML)
- Modify: `dashboard/index.html:6551-6562` (JS loadSpeciesAutocomplete)
- Modify: `dashboard/index.html:6566+` (JS loadSpeciesActivity)

- [ ] **Step 1: Replace HTML input+datalist with combobox structure**

Replace lines 2448-2451:

```html
      <div class="combo-select" id="activity-species-combo" style="flex:1; position:relative;">
        <input type="text" id="activity-species-input" placeholder="Select a species..." autocomplete="off"
               style="width:100%; padding:8px 32px 8px 8px; border:1px solid #444; background:#1a1a2e; color:#e0e0e0; border-radius:6px; font-size:14px; cursor:pointer;"
               onclick="toggleSpeciesDropdown()" oninput="filterSpeciesDropdown(this.value)">
        <span style="position:absolute; right:10px; top:50%; transform:translateY(-50%); pointer-events:none; opacity:0.5;">▼</span>
        <div id="species-dropdown" class="combo-dropdown" style="display:none; position:absolute; top:100%; left:0; right:0; max-height:300px; overflow-y:auto; background:#1a1a2e; border:1px solid #444; border-top:none; border-radius:0 0 6px 6px; z-index:100;"></div>
      </div>
```

Remove the datalist line and the Search button (the combobox auto-loads on selection).

- [ ] **Step 2: Add combobox JavaScript**

Replace `loadSpeciesAutocomplete()` and add new functions:

```javascript
var _speciesList = [];  // cached species list

async function loadSpeciesAutocomplete() {
    try {
        var resp = await fetch('/bird-api/activity/species-list');
        var data = await resp.json();
        // Sort by total count descending
        _speciesList = (data.species || []).sort(function(a, b) {
            return (b.total || 0) - (a.total || 0);
        });
        renderSpeciesDropdown(_speciesList);

        // Auto-load a random species from top 10
        if (_speciesList.length > 0) {
            var top = _speciesList.slice(0, Math.min(10, _speciesList.length));
            var pick = top[Math.floor(Math.random() * top.length)];
            document.getElementById('activity-species-input').value = pick.name;
            loadSpeciesActivity();
        }
    } catch (e) {
        console.warn('Species list load failed:', e);
    }
}

function renderSpeciesDropdown(list) {
    var dd = document.getElementById('species-dropdown');
    dd.innerHTML = list.map(function(sp) {
        return '<div class="combo-option" data-value="' + escAttr(sp.name) + '" ' +
               'onclick="selectSpecies(\'' + escAttr(sp.name) + '\')" ' +
               'style="padding:8px 12px; cursor:pointer; border-bottom:1px solid #333; font-size:13px;"' +
               ' onmouseover="this.style.background=\'#2a2a4e\'" onmouseout="this.style.background=\'none\'">' +
               escHtml(sp.name) + ' <span style="opacity:0.5; font-size:11px;">(' + (sp.total || 0) + ')</span></div>';
    }).join('');
}

function toggleSpeciesDropdown() {
    var dd = document.getElementById('species-dropdown');
    if (dd.style.display === 'none') {
        renderSpeciesDropdown(_speciesList);
        dd.style.display = 'block';
    } else {
        dd.style.display = 'none';
    }
}

function filterSpeciesDropdown(query) {
    var dd = document.getElementById('species-dropdown');
    dd.style.display = 'block';
    if (!query) {
        renderSpeciesDropdown(_speciesList);
        return;
    }
    var q = query.toLowerCase();
    var filtered = _speciesList.filter(function(sp) {
        return sp.name.toLowerCase().indexOf(q) !== -1;
    });
    renderSpeciesDropdown(filtered);
}

function selectSpecies(name) {
    document.getElementById('activity-species-input').value = name;
    document.getElementById('species-dropdown').style.display = 'none';
    loadSpeciesActivity();
}

// Close dropdown on outside click
document.addEventListener('click', function(e) {
    var combo = document.getElementById('activity-species-combo');
    if (combo && !combo.contains(e.target)) {
        document.getElementById('species-dropdown').style.display = 'none';
    }
});
```

- [ ] **Step 3: Update `loadSpeciesActivity()` to work without Search button**

Find `loadSpeciesActivity()` and ensure it reads from the input field (no changes needed if it already does — just verify the input ID matches `activity-species-input`).

Also add Enter key support:

```javascript
// In the input element, add: onkeydown="if(event.key==='Enter'){selectSpecies(this.value);}"
```

- [ ] **Step 4: Clear previous data on Activity tab load**

In `loadActivityTab()`, add at the top:

```javascript
// Clear previous species data when switching to Activity tab
document.getElementById('activity-species-result').innerHTML = '';
if (typeof _activityHourChart !== 'undefined' && _activityHourChart) {
    _activityHourChart.destroy();
    _activityHourChart = null;
}
```

- [ ] **Step 5: Test manually**

Open dashboard → Activity tab. Verify:
- Random species loads automatically
- Clicking input shows full dropdown
- Typing filters the list
- Clicking an option loads that species
- Enter key works
- Clicking outside closes dropdown

- [ ] **Step 6: Commit**

```bash
git add dashboard/index.html
git commit -m "feat: species activity combobox — searchable dropdown + random species on load"
```

---

### Task 7: Fix Verdict Labels in Classified Tab

**Files:**
- Modify: `dashboard/index.html:5403`

- [ ] **Step 1: Verify and fix verdict label mapping**

Line 5403 should already be partially fixed from earlier in this session. Verify it reads:

```javascript
var verdictLabel = item.verdict === 'correct' ? 'Correct' : item.verdict === 'wrong' ? 'Wrong' : item.verdict === 'trash' ? 'Trashed' : item.verdict === 'skip' ? 'Skipped' : item.verdict === 'reclassify' ? 'Missed Birds' : item.verdict;
```

If not, fix it.

- [ ] **Step 2: Commit (if changed)**

```bash
git add dashboard/index.html
git commit -m "fix: verdict labels in Classified tab show actual verdict names"
```

---

### Task 8: Verify and Smoke Test

**Files:** None — manual verification

- [ ] **Step 1: Run full test suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -v`
Expected: All tests pass including new apply_verdict tests.

- [ ] **Step 2: Restart dashboard**

```bash
launchctl stop com.vives.bird-dashboard; sleep 2; launchctl start com.vives.bird-dashboard
```

- [ ] **Step 3: Test review file movement**

In the dashboard Review tab:
1. Find an unreviewed image
2. Click "Wrong" and correct to a different species
3. Verify: file moved to correct folder, `classifications.common_name` updated in DB
4. Verify: image appears under correct species in Classified tab filter

- [ ] **Step 4: Verify NAS removal**

Check dashboard header — NAS dot should be gone. Check service status section — no NAS entry.

- [ ] **Step 5: Verify Activity tab**

Open Activity tab:
- Random species should auto-load
- Click the input → dropdown appears
- Type to filter
- Heatmap says "(camera + audio, last 7 days)"

- [ ] **Step 6: Verify audio health**

Check `/api/health` response — audio_analyzer should show detection count even if paused.

- [ ] **Step 7: Commit any smoke test fixes**

```bash
git add -u
git commit -m "fix: address issues from smoke testing"
```
