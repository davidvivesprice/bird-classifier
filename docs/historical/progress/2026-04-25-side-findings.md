> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Side findings — David's observation backlog

**Purpose:** things David notices in passing that he wants to surface
later as task-starters. **This file is David's queue, not Claude's
work-trace.** Reference this when starting a new task to see if
anything related is queued. Claude only adds entries here when
explicitly told, OR when noticing something that's clearly a "you
should know about this for later" signal that David wouldn't
otherwise see.

Format per entry:
```
## YYYY-MM-DD — short title
**Noticed while:** what was happening
**Observation:** the thing
**Why surface later:** what task this would unblock or inform
**Status:** open | in-flight | resolved | folded into <doc/PR>
```

---

## 2026-04-25 — `review_history` legacy backfill missing on iMac

**Noticed while:** restarting iMac dashboard for the review UI bug fix.
**Observation:** The 1,827 historical reviews in iMac `reviews` table are NOT backfilled into the new `review_history` table. New reviews going forward write history correctly; old ones don't have audit trail.
**Why surface later:** When training the flagship, if we want full provenance (every label change recorded), we need a one-shot migration. Probably 30 min of SQL.
**Status:** open.

## 2026-04-25 — debug test row in iMac `review_history`

**Noticed while:** end-to-end verifying review2 endpoint after dashboard restart.
**Observation:** `id=1`, `file=feeder_2026-04-25_10-55-32_5565.jpg`, `verdict=correct`, `client_id=debug-restart-test-002`. It's a real verdict on a real file, just labeled with a debug client_id and not authored by a human reviewer.
**Why surface later:** When you want a clean review history (e.g., before exporting reviewer-confidence stats), this row is debug noise. Could keep, could delete, could rewrite client_id.
**Status:** open.

## 2026-04-25 — 4 pre-existing test_pipeline_classifier.py failures

**Discovered while:** running RC3 Task 3 full pipeline test sweep.
**Observation:** `tests/pipeline/test_pipeline_classifier.py` has 4 failing tests, all pre-existing (verified by `git checkout e553b09 -- pipeline tests` baseline run): `test_yard_useless_aiy_rescues`, `test_yard_uncertain_both_agree`, `test_disagreement_falls_through_to_unlabeled`, `test_no_confident_answer_returns_unlabeled`. These exercise the SmartClassifier's per-camera yard-first decision tree (yard threshold, AIY fallback, both-agree path).
**Why it matters:** RC3 didn't touch SmartClassifier — these failures predate. But they ARE testing the live classifier's lock-time vote logic, which is the system that produced the ~30% noise rows. If the decision-tree tests are failing, behavior of the production classifier may have drifted from spec since these tests were written. Likely related to the `confident_threshold=0.25` / `uncertain_low=0.10` defaults that were tuned April 18 for the new yard softmax — tests may still assert old thresholds. Worth investigating before/during RC2.
**Status:** open. Roll into RC2 or a "classifier test refresh" task.

## 2026-04-25 — iMac YOLO is 2× slower than docs claim

**Noticed while:** pulling SnapshotWriter health stats during the detection+snapshot audit.
**Observation:** `yolo_ms_avg: 212`, `yolo_ms_p99: 542` on iMac. The `~/docs/bird-observatory/08-classify-pipeline.md` doc says expected ~98ms with CoreML acceleration. We're 2.2× the expected average, 5.5× at p99.
**Why surface later:** Either CoreML isn't actually being used (silent fallback to CPU), iMac CPU is heavily loaded, or the doc figure was for a different model variant. If on CPU instead of CoreML, every detection costs 3-4× more — affects throughput on bursty multi-bird scenes and could be a contributing factor to tracker coasting (no fresh detection in time).
**Status:** open. Quick check: `grep -i 'coreml\|provider' ~/bird-snapshots/logs/bird-pipeline-stdout.log | head` to see what onnxruntime actually used at startup.

---
