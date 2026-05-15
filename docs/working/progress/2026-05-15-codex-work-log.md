# 2026-05-15 Codex Work Log

Purpose: concise operational log for the Pi 5 bird observatory work I took over on May 15. This complements the longer running log at `docs/working/progress/2026-05-12-codex-live-log.md`.

## Current State

- Repo: `/Users/vives/bird-classifier-pi`
- Live Pi runtime: `vives@pi5.local:/home/vives/bird-classifier`
- Branch: `pi-main`
- Public remote: `origin https://github.com/davidvivesprice/bird-classifier.git`
- Latest functional commit covered by this log: `47ef783 fix(classifier): normalize aiy raw score confidence`
- Live services after the latest restart:
  - `bird-pipeline`: active
  - `bird-dashboard`: active
  - `go2rtc`: active
  - `cloudflared`: active

## Operating Goal

The live dashboard needs labels and boxes to appear on the displayed bird, stay synchronized with the video, and avoid high-confidence wrong labels. Bounding boxes are currently diagnostic scaffolding; final UX can hide boxes once label tracking is trustworthy.

## Work Completed

### Demo/live classification split

Commit: `0fd3615 fix(dashboard): split demo and live classifications`

Problem:

- Demo-loop classifications were showing in the live Recent Classifications strip after switching back to live.
- The dashboard and pipeline had no clean storage boundary between demo and live rows.

Fix:

- Live snapshot rows now write to `classifications.db`.
- Demo rows now write to `classifications_demo.db` when `PIPELINE_TEST_RTSP_URL` is set.
- Dashboard Recent Classifications and stats request `mode=live|demo`.
- Review writes carry source mode.
- Source switches clear stale overlay state.

Data migration on Pi:

- Backups created:
  - `~/bird-snapshots/logs/backups/classifications.db.20260515-live-demo-split.bak`
  - `~/bird-snapshots/logs/backups/pi_reviews.db.20260515-live-demo-split.bak`
- Known demo-period rows moved from live DB to demo DB.
- Final state left in live mode with demo loop inactive.

Verification:

- `./venv/bin/python -m pytest tests/test_demo_mode_classifications_routing.py tests/test_dashboard_live_video_proxy.py tests/pipeline/test_snapshot_writer_rc3.py tests/test_classifications_db.py -q`
- Result on Pi: `32 passed, 4 warnings`
- Browser smoke: switching live to demo and back updated the Recent Classifications strip without a page refresh.

### AIY raw-score confidence fix

Commit: `47ef783 fix(classifier): normalize aiy raw score confidence`

Problem:

- Live labels sometimes showed unlikely species such as Northern Flicker, Chipping Sparrow, American Tree Sparrow, or Red Crossbill at confidence `1.0`.
- Saved 1080p authoritative reclassification of the same rows usually disagreed and often preferred House Finch or House Sparrow at lower confidence.

Root cause:

- `pipeline/pi_classifier.py` normalized raw scores with `raw / 255 if raw > 1 else raw`.
- AIY/Hailo registry `raw_score` values are integer 0-255.
- Integer `raw_score == 1` was therefore treated as confidence `1.0`, not `1/255`.
- That made the weakest nonzero AIY ties eligible for live vote-locks as perfect-confidence species labels.

Fix:

- Added `_normalize_raw_score()` in `pipeline/pi_classifier.py`.
- Integer raw scores, including `1`, always divide by `255`.
- Already-normalized float scores in `[0, 1]` still pass through.

Verification:

- Added `tests/pipeline/test_pi_classifier.py`.
- Red test before fix showed `raw_score=1` producing `ClassificationResult(species='Northern Flicker', confidence=1.0, ...)`.
- Green focused tests on Pi:
  - `./venv/bin/python -m pytest tests/pipeline/test_pi_classifier.py -q`
  - Result: `3 passed`
- Green nearby regression set on Pi:
  - `./venv/bin/python -m pytest tests/pipeline/test_pi_classifier.py tests/pipeline/test_process_thread.py tests/pipeline/test_snapshot_writer_rc3.py -q`
  - Result: `20 passed`
- Restarted `bird-pipeline.service`.
- Post-restart service health: `overall ok`; frames advanced; no drops or restarts reported.

User validation:

- David reported after the fix: "it looks good."

## Important Findings

- On the Pi, `HailoDetector.detect()` ignores the motion gate and runs full-frame whenever called. The earlier suspicion that MOG2 motion gating was starving Hailo YOLO was wrong for the live Pi path.
- The live high-confidence wrong-label bug was in classifier confidence normalization, not the dashboard renderer and not the saved snapshot writer.
- The regional species filter is enabled and includes House Finch, Black-capped Chickadee, Northern Flicker, Common Grackle, Blue Jay, American Goldfinch, and House Sparrow. The bad labels were region-plausible-but-context-wrong, not impossible tropical leakage.
- Saved authoritative crops from the 1080p HLS extraction path often produce better species guesses than live 640x360 crops. This supports keeping high-res snapshots as the review/training path.

## Verification Caveats

The full `tests/pipeline -q` suite is not clean on the Pi. Current unrelated failures:

- `tests/pipeline/test_frame_capture.py` expects old private methods such as `_input_args`, `_spawn_ffmpeg`, and `_restart`.
- `tests/pipeline/test_pipeline_classifier.py` expects older `SmartClassifier` decision-tree behavior.

Focused tests covering this work passed. Do not claim the full pipeline test suite is green until those stale tests are reconciled.

## User-Eye Checks

Needed when birds are visibly present:

- Labels should no longer jump to 100%-confidence unlikely species.
- If boxes/labels vanish while birds are visible, check backend counters first:
  - `detections_total`
  - `active_tracks`
  - `events_emitted`
  - dashboard sync diag track count
- If backend tracks are present but browser labels are absent, investigate overlay rendering/sync.
- If backend tracks are absent while birds are visible, investigate Hailo detector confidence, NMS output, and tracker association.

## Next Work Queue

1. Watch live rows over a bird visit and compare:
   - live lock-time species/confidence
   - authoritative saved-frame species/confidence
   - human visual species
2. Add a lightweight live-label confidence audit view or script that flags lock-time vs authoritative disagreements in the last N rows.
3. If boxes disappear on visible birds, instrument detector/tracker boundaries:
   - raw Hailo detections per frame
   - rejected detections below confidence threshold
   - active Norfair tracked objects
   - expired track reasons
4. Reconcile stale pipeline tests so broader regression runs can be trusted again.

## Useful Commands

Service health:

```bash
ssh vives@pi5.local "systemctl --user is-active bird-pipeline bird-dashboard go2rtc cloudflared"
```

Pipeline health:

```bash
ssh vives@pi5.local "python3 - <<'PY'
import json, urllib.request
h=json.load(urllib.request.urlopen('http://127.0.0.1:8100/api/pipeline/health', timeout=2))
print(json.dumps(h, indent=2)[:12000])
PY"
```

Focused regression tests:

```bash
ssh vives@pi5.local "cd /home/vives/bird-classifier && ./venv/bin/python -m pytest tests/pipeline/test_pi_classifier.py tests/pipeline/test_process_thread.py tests/pipeline/test_snapshot_writer_rc3.py -q"
```

Recent live classification disagreement sample:

```bash
ssh vives@pi5.local "cd /home/vives/bird-classifier && ./venv/bin/python - <<'PY'
import sqlite3, json
from pathlib import Path
db = Path.home() / 'bird-snapshots' / 'logs' / 'classifications.db'
with sqlite3.connect(db) as c:
    c.row_factory = sqlite3.Row
    rows = list(c.execute('select id,file,common_name,confidence,extra_json from classifications order by id desc limit 12'))
for row in rows:
    extra = json.loads(row['extra_json'] or '{}')
    print(row['id'], row['common_name'], row['confidence'], 'lock=', extra.get('lock_time'), 'auth=', extra.get('authoritative'), 'dis=', extra.get('disagreement'))
PY"
```
