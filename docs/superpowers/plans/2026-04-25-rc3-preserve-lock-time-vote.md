# RC3: Preserve Lock-Time Vote Info in Saved Snapshots

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop silently overwriting lock-time classification with write-time AIY noise. Preserve both in `extra_json` so we can tell sound locks from noise locks at review time + retrospectively.

**Architecture:** `SnapshotWriter._write_one` currently overwrites `p["species"]` / `p["species_confidence"]` / `p["model_source"]` with the `authoritative_classify` (write-time AIY) result. Change: STOP overwriting. Keep lock-time values as canonical. Add the authoritative result + a `disagreement` flag as METADATA in extra fields on the entry dict — `classifications_db.insert_classification` automatically packs unknown fields into the `extra_json` column (verified at `classifications_db.py:149`).

Backward-compatible because `extra_json` is a free-form JSON blob; existing rows just won't have the new keys (dashboard JSON-extract returns NULL gracefully).

**Tech Stack:** Python 3.9 (`venv-coral`), pytest, sqlite3 for verification, FastAPI for the live dashboard.

**Why this change unblocks the data audit:** without it, every saved row reflects only write-time AIY (often hallucinating raw_score=1 on stale frames). With it, every NEW row carries the lock-time vote result + the AIY second opinion + a disagreement marker. Future cleanup work can filter on `disagreement=true AND authoritative_confidence < threshold` to identify suspect rows — this is the foundation for RC2 (confidence floor) and the cleanlab work after.

---

## Task 1: Capture lock-time values BEFORE the auth call (no behavior change yet)

**Files:**
- Modify: `pipeline/snapshot_writer.py:401-417` (the block right before the auth call)
- Test: `tests/pipeline/test_snapshot_writer_rc3.py` (new file)

This task adds the test scaffolding + captures the lock-time values to local variables BEFORE the auth call mutates `p`. No behavioral change yet — just sets up the next task.

- [ ] **Step 1.1: Inspect `_KNOWN_FIELDS` to confirm name choices won't collide**

```bash
grep -n '_KNOWN_FIELDS' /Users/vives/bird-classifier/classifications_db.py
```

Expected: a set/list of column names. New keys we'll add to entry dict — `authoritative`, `lock_time`, `disagreement`, `authoritative_relabeled` — must NOT be in that set. If any collide, choose alternate names with an `rc3_` prefix.

- [ ] **Step 1.2: Write failing test for "lock-time values captured before auth runs"**

Create `/Users/vives/bird-classifier/tests/pipeline/test_snapshot_writer_rc3.py`:

```python
"""RC3: SnapshotWriter must preserve lock-time vote info, not silently
overwrite it with the write-time authoritative_classify result.

See docs/superpowers/specs/2026-04-25-detection-snapshot-audit-findings.md
for the failure mode this guards against.
"""
from __future__ import annotations
import sys
from pathlib import Path
from unittest.mock import MagicMock

import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from pipeline.snapshot_writer import SnapshotWriter


def _make_payload(species: str = "Northern Cardinal",
                  species_conf: float = 0.5,
                  model_source: str = "yard"):
    """Build a SnapshotWriter payload mimicking what process_thread submits."""
    return {
        "camera": "feeder",
        "frame": np.zeros((360, 640, 3), dtype=np.uint8),
        "wall_time_ms": 1000000.0,
        "track_id": 42,
        "species": species,
        "species_confidence": species_conf,
        "model_source": model_source,
        "confidence": 0.85,
        "bbox": [100, 100, 300, 300],
        "frame_count": 5,
        "vote_history": [(species, species_conf)] * 3,
    }


def test_lock_time_values_captured_before_auth_overwrite(monkeypatch):
    """The original p['species']/['species_confidence']/['model_source']
    from process_thread must be readable AFTER the auth call, even if
    auth tries to mutate them.
    """
    # Stub out the file/DB writes — we only care about in-memory state.
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    def fake_insert(entry):
        captured_entry.update(entry)
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification", fake_insert)

    # Stub authoritative_classify to return a DIFFERENT species (the noise case)
    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": "American Goldfinch",
                  "confidence": 0.01,
                  "model_source": "aiy"})())

    writer = SnapshotWriter(classifier=fake_classifier)
    payload = _make_payload(species="Northern Cardinal",
                            species_conf=0.5,
                            model_source="yard")
    writer._write_one(payload)

    # The captured entry must record the LOCK-TIME species, not the auth one.
    assert captured_entry["lock_time"]["species"] == "Northern Cardinal", (
        f"lock_time.species was {captured_entry['lock_time']['species']!r}, "
        f"expected 'Northern Cardinal' (the lock-time vote winner)"
    )
    assert captured_entry["lock_time"]["confidence"] == 0.5
    assert captured_entry["lock_time"]["source"] == "yard"
```

- [ ] **Step 1.3: Run test to verify it fails**

```bash
cd /Users/vives/bird-classifier
./venv-coral/bin/python -m pytest tests/pipeline/test_snapshot_writer_rc3.py::test_lock_time_values_captured_before_auth_overwrite -xvs
```

Expected: FAIL with `KeyError: 'lock_time'` (the entry dict doesn't have that key yet).

- [ ] **Step 1.4: Modify `_write_one` to capture lock-time vars + add to entry**

Edit `/Users/vives/bird-classifier/pipeline/snapshot_writer.py`. Find the block at line 404-417 (the `# Re-classify with AIY...` comment + the auth call + apply). Replace with:

```python
        # Capture lock-time classification values BEFORE auth call. RC3:
        # the live pipeline's vote-lock decision (yard / AIY / both_agree at
        # lock moment) is the canonical "what the system thought" record.
        # The authoritative AIY second opinion below is metadata, not a
        # replacement. See docs/superpowers/plans/2026-04-25-rc3-*.md
        lock_time_species = p["species"]
        lock_time_confidence = p["species_confidence"]
        lock_time_source = p["model_source"]

        # Re-classify with AIY on the (now hi-res, ideally) crop. Result is
        # stored as METADATA (not as a replacement for lock-time). This lets
        # us see in review whether AIY at write time agrees with what the
        # live pipeline decided. Disagreement + low auth confidence = noise
        # marker for retrospective filtering.
        auth = self._authoritative_species(p["frame"], p["bbox"])
        if auth is not None:
            self.stats["aiy_relabel"] += 1
        else:
            self.stats["aiy_none"] += 1
```

Note we DELETED the three `p["..."] = auth["..."]` lines. p["species"] etc. now retain their lock-time values.

Then find the `entry = {` block at line 470. Just before the closing `}` (line 491-492), add the new fields:

```python
            "model_source": str(p.get("model_source") or ""),
            # RC3: lock-time + authoritative + disagreement, all stored in
            # extra_json (classifications_db packs unknown fields automatically
            # — see classifications_db.py:149).
            "lock_time": {
                "species": lock_time_species,
                "confidence": lock_time_confidence,
                "source": lock_time_source,
            },
            "authoritative": {
                "species": auth["species"] if auth else None,
                "confidence": auth["confidence"] if auth else None,
                "source": auth["model_source"] if auth else None,
            } if auth else None,
            "disagreement": bool(auth and auth["species"] != lock_time_species),
        }
```

- [ ] **Step 1.5: Run test to verify it passes**

```bash
cd /Users/vives/bird-classifier
./venv-coral/bin/python -m pytest tests/pipeline/test_snapshot_writer_rc3.py::test_lock_time_values_captured_before_auth_overwrite -xvs
```

Expected: PASS.

- [ ] **Step 1.6: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/snapshot_writer.py tests/pipeline/test_snapshot_writer_rc3.py
git commit -m "RC3 step 1: preserve lock-time vote info as canonical, store auth as metadata"
```

---

## Task 2: Test that disagreement flag is set correctly

**Files:**
- Test: `tests/pipeline/test_snapshot_writer_rc3.py` (append)

- [ ] **Step 2.1: Write the test for disagreement flag**

Append to `tests/pipeline/test_snapshot_writer_rc3.py`:

```python
def test_disagreement_flag_true_when_species_differ(monkeypatch):
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": "American Goldfinch", "confidence": 0.01,
                  "model_source": "aiy"})())

    writer = SnapshotWriter(classifier=fake_classifier)
    writer._write_one(_make_payload(species="Northern Cardinal"))

    assert captured_entry["disagreement"] is True
    assert captured_entry["authoritative"]["species"] == "American Goldfinch"
    assert captured_entry["authoritative"]["confidence"] == 0.01


def test_disagreement_flag_false_when_species_match(monkeypatch):
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=type(
        "R", (), {"species": "Northern Cardinal", "confidence": 0.85,
                  "model_source": "aiy"})())

    writer = SnapshotWriter(classifier=fake_classifier)
    writer._write_one(_make_payload(species="Northern Cardinal"))

    assert captured_entry["disagreement"] is False
    assert captured_entry["authoritative"]["confidence"] == 0.85


def test_authoritative_none_when_classifier_returns_none(monkeypatch):
    monkeypatch.setattr("cv2.imencode", lambda *a, **kw: (True, np.zeros(10, dtype=np.uint8)))
    monkeypatch.setattr("pathlib.Path.mkdir", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.write_bytes", lambda *a, **kw: None)
    monkeypatch.setattr("pathlib.Path.unlink", lambda *a, **kw: None)

    captured_entry = {}
    import classifications_db as cdb
    monkeypatch.setattr(cdb, "insert_classification",
                        lambda e: captured_entry.update(e))

    fake_classifier = MagicMock()
    fake_classifier.authoritative_classify = MagicMock(return_value=None)

    writer = SnapshotWriter(classifier=fake_classifier)
    writer._write_one(_make_payload(species="Northern Cardinal"))

    # authoritative is None → disagreement is False (no second opinion to disagree with)
    assert captured_entry["authoritative"] is None
    assert captured_entry["disagreement"] is False
    # Lock-time values still preserved
    assert captured_entry["lock_time"]["species"] == "Northern Cardinal"
```

- [ ] **Step 2.2: Run all three new tests**

```bash
cd /Users/vives/bird-classifier
./venv-coral/bin/python -m pytest tests/pipeline/test_snapshot_writer_rc3.py -xvs
```

Expected: 4 tests pass (1 from Task 1 + 3 here).

- [ ] **Step 2.3: Commit**

```bash
cd /Users/vives/bird-classifier
git add tests/pipeline/test_snapshot_writer_rc3.py
git commit -m "RC3 step 2: tests for disagreement flag + auth=None handling"
```

---

## Task 3: Run the full test suite to confirm no regressions

**Files:** none modified.

- [ ] **Step 3.1: Run all snapshot_writer-relevant tests + a broader sweep**

```bash
cd /Users/vives/bird-classifier
./venv-coral/bin/python -m pytest tests/pipeline/ -x -q 2>&1 | tail -25
```

Expected: green. If any test fails because it was implicitly relying on `p["species"]` getting overwritten by the auth result, fix the test (the new behavior is correct; the old test was asserting wrong-by-design).

- [ ] **Step 3.2: Run the dashboard's review-API tests too (touch DB-shaped concerns)**

```bash
cd /Users/vives/bird-classifier
./venv-coral/bin/python -m pytest tests/test_api_review2.py tests/test_api_review2_queue.py tests/test_api_review2_batch.py tests/test_reviews_db_history.py tests/test_api_endpoints.py -q 2>&1 | tail -15
```

Expected: 56 pass (or whatever the count is — should be unchanged from before this RC).

- [ ] **Step 3.3: Commit if any test fixes were needed**

If Step 3.1 or 3.2 required test fixes, commit them. If everything green without fixes, skip this commit step.

```bash
cd /Users/vives/bird-classifier
git status  # confirm clean or stage-then-commit any fixes
git commit -m "RC3 step 3: test fixes for tests asserting now-removed overwrite behavior" || true
```

---

## Task 4: Live verification on iMac (David's call to restart)

**Files:** none modified. This is verification, not implementation.

- [ ] **Step 4.1: Confirm the dashboard service knows about the new field shape**

The dashboard reads `extra_json` via `json_extract` — additions are backward-compatible (NULL on absent keys). No restart needed for the dashboard.

The PIPELINE service is what writes the new fields. It needs a restart to load the new snapshot_writer code.

```bash
# Check pipeline last started:
ps -p $(pgrep -f bird_pipeline_v3 | head -1) -o etime= 2>/dev/null | tr -d ' '
```

- [ ] **Step 4.2: Restart bird-pipeline (David's go-ahead required)**

ASK DAVID before this step. The bird-pipeline restart drops the in-flight detection state for ~10s.

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-pipeline"
```

Wait 10 seconds for warmup.

- [ ] **Step 4.3: Verify next snapshots have the new extra_json fields**

Wait for at least 5 new classifications to appear (a few minutes of bird activity), then:

```bash
sqlite3 -column -header /Users/vives/bird-snapshots/logs/classifications.db "
SELECT id,
       common_name,
       json_extract(extra_json, '\$.lock_time.species') AS lock_sp,
       json_extract(extra_json, '\$.lock_time.confidence') AS lock_conf,
       json_extract(extra_json, '\$.lock_time.source') AS lock_src,
       json_extract(extra_json, '\$.authoritative.species') AS auth_sp,
       json_extract(extra_json, '\$.authoritative.confidence') AS auth_conf,
       json_extract(extra_json, '\$.disagreement') AS disagree
FROM classifications
WHERE action='classified'
  AND extra_json IS NOT NULL
  AND json_extract(extra_json, '\$.lock_time') IS NOT NULL
ORDER BY id DESC LIMIT 10;"
```

Expected: 10 rows, all with non-NULL lock_sp/lock_conf/lock_src/auth_sp/auth_conf/disagree. At least 1-2 should show `disagree=1` (the bug case we're now able to see).

- [ ] **Step 4.4: Document the watershed in the audit findings doc**

Append to `docs/superpowers/progress/2026-04-25-detection-snapshot-audit-findings.md` a section:

```markdown
## RC3 watershed — <ISO timestamp> (commit hash)

Pre-watershed rows: opaque noise. Saved species/confidence reflect
write-time AIY only (often hallucinating on stale frames).

Post-watershed rows: every row carries `extra_json.lock_time` (vote-
locked vote winner) + `extra_json.authoritative` (write-time AIY second
opinion) + `extra_json.disagreement`. SQL filters can identify suspect
rows: `disagreement=true AND authoritative.confidence < 0.1` is the
clearest "this is noise" signal. Sample disagreement rate from first
N post-watershed rows: <fill in>%.

Use the watershed commit hash as the cutoff for any cleanup or cleanlab
work — pre-watershed rows have no provenance.
```

Then commit the doc update:

```bash
cd /Users/vives/bird-classifier
git add docs/superpowers/progress/2026-04-25-detection-snapshot-audit-findings.md
git commit -m "RC3 step 4: document watershed for retrospective filtering"
```

---

## Task 5: Spot-check that the multi-bird case now provides better signal

**Files:** none modified.

- [ ] **Step 5.1: Find a recent multi-bird moment**

```bash
sqlite3 -column -header /Users/vives/bird-snapshots/logs/classifications.db "
SELECT strftime('%Y-%m-%d %H:%M:%S', source_timestamp) AS ts,
       COUNT(*) AS rows_in_second,
       GROUP_CONCAT(common_name, ' | ') AS species,
       GROUP_CONCAT(json_extract(extra_json, '\$.disagreement'), ' | ') AS disagreements
FROM classifications
WHERE action='classified' AND extra_json IS NOT NULL
  AND json_extract(extra_json, '\$.lock_time') IS NOT NULL
GROUP BY strftime('%Y-%m-%d %H:%M:%S', source_timestamp)
HAVING rows_in_second > 1
ORDER BY ts DESC LIMIT 5;"
```

Expected: rows where multi-bird scenes have varying disagreement flags. This is the data we need to drive RC2 (confidence floor) and RC4 (review UX).

- [ ] **Step 5.2: Sanity-check by viewing one of the disagreement JPGs**

Pick a disagreement=1 row from above, find its file path, look at the JPG. Eyeball: is it a real bird or noise? Does the lock-time species match what's in the picture better than the authoritative species (or vice versa)?

This step tells us whether RC3's flag is well-calibrated against ground truth.

---

## Self-Review (writing-plans skill checklist)

**1. Spec coverage:** The audit findings doc names RC3 as "preserve both: save the lock-time vote winner AND the authoritative result; flag disagreement; let reviewer see both." Tasks 1-2 cover save + flag. Task 4 verifies live. Task 5 closes the loop with eyeball verification. ✓

**2. Placeholder scan:** I see `<fill in>%` in Task 4 — that's intentional (David fills in the actual measurement after running). Otherwise no TBDs/TODOs. ✓

**3. Type consistency:** `lock_time` / `authoritative` / `disagreement` keys consistent across Task 1, Task 2 tests, Task 4 SQL queries. `lock_time_species` (snake_case local var) vs `lock_time.species` (nested in extra_json) — naming is intentional, the local var is captured before being used to populate the nested dict. ✓

**4. Out of scope (explicitly NOT in this plan):**
- RC2 (confidence floor at write boundary — separate plan)
- RC4 (review UI surfacing — separate plan)
- Showing disagreement in the live dashboard UI (handled by RC4)
- Retroactive cleanup of pre-watershed noisy rows (handled by RC2 + cleanlab)
- iMac YOLO 2× slow finding (in side-findings.md)

---
