# Phase 1 Shadow Deployment — Daily Validation Checklist (2026-04-29)

**Context:** Phase 1 runs for ≥7 days parallel. This checklist ensures validation metrics are collected systematically and the success gate (≥0.90 intra-frame agreement) is objectively measurable.

---

## Daily Validation Routine (Morning, ~UTC 00:00)

Run this sequence every morning during Phase 1 shadow (7 consecutive days minimum).

### 1. Health Check (5 min)

```bash
# Pi-side
ssh vives@pi5.local "systemctl --user status bird-pipeline bird-dashboard"
# Expected: Active: active (running) for both

# iMac-side  
launchctl list | grep bird-pipeline
# Expected: system status = 0 (running)

# Verify RTSP connectivity
ssh vives@pi5.local "curl -s http://localhost:1984/api/streams | jq '.feeder-sub | .consumers'"
# Expected: >0 consumers (Pi video capture running)

curl -s http://localhost:1984/api/streams | jq '.feeder-sub | .consumers'
# Expected: >0 consumers (iMac reference stream running)
```

### 2. Database Integrity (5 min)

```bash
# Pi-side: run audit in mirror mode
ssh vives@pi5.local "DB_MODE=mirror python3 tools/audit_data_integrity.py --json" > /tmp/pi_audit.json

# iMac-side: run audit in write mode
python3 tools/audit_data_integrity.py --json > /tmp/imac_audit.json

# Check results
jq '.orphan_rows | length' /tmp/pi_audit.json
jq '.orphan_rows | length' /tmp/imac_audit.json
# Expected: 0 for both (no data corruption during shadow period)
```

### 3. Collect Tracker Health Metrics (5 min)

```bash
# Pi-side
ssh vives@pi5.local "curl -s http://localhost:8100/health | jq '.shared.tracker'"

# Example output:
# {
#   "feeder": {
#     "id_switches": 23,
#     "active_tracks": 0
#   }
# }

# iMac-side (AIY fallback metrics)
curl -s http://localhost:8100/health | jq '.shared.tracker'
```

Record in `phase1_metrics.csv`:
```
date,time,pi_id_switches,pi_active_tracks,imac_id_switches,imac_active_tracks
2026-05-01,00:00,23,0,15,0
```

### 4. Shadow Validation Report (10 min)

```bash
# On iMac, run shadow_validation_harness.py
# This compares Pi YOLO detections vs AIY detections on the same frame stream
python3 tools/shadow_validation_harness.py \
  --pi-url http://pi5.local:8105 \
  --imac-url http://localhost:8105 \
  --output-dir /tmp/shadow_validation_$(date +%Y%m%d)

# Output files:
# - shadow_validation_report.json (detailed metrics)
# - per_species_roc.csv (ROC curves)
# - confusion_matrix.json (Pi vs AIY predictions)
# - per_track_agreement.csv (track-by-track agreement ratio)

# Check key metrics
jq '.per_track_agreement_ratio' /tmp/shadow_validation_*/shadow_validation_report.json
# Expected: >= 0.85 (ramping toward 0.90 success gate)

jq '.intra_frame_agreement' /tmp/shadow_validation_*/shadow_validation_report.json
# Expected: >= 0.85

jq '.regressions_on_rare_species' /tmp/shadow_validation_*/shadow_validation_report.json
# Expected: [] (empty array — no new failure cases)
```

### 5. Logs Review (10 min)

```bash
# Pi systemd logs (last 24 hours)
ssh vives@pi5.local "journalctl --user -u bird-pipeline -u bird-dashboard -n 100 --since '24 hours ago' | grep -E 'ERROR|WARN|exception|Traceback'"
# Expected: no ERROR or exception messages

# iMac logs (last 24 hours, system.log)
log stream --level debug --predicate 'process == "bird-pipeline"' --since '24 hours ago' 2>/dev/null | grep -E 'ERROR|WARN|exception'
# Expected: no ERROR or exception messages
```

### 6. Metric Aggregation (5 min)

Append daily summary to `phase1_validation_log.txt`:

```
=== 2026-05-01 ===
Health: ✓ (both running)
Data integrity: ✓ (0 orphans)
Pi tracker: id_switches=23, active_tracks=0
iMac AIY: id_switches=15, active_tracks=0
Shadow report: agreement=0.88, intra_frame=0.89
Regressions: none
Logs: clean (no errors)
Status: GREEN (proceed day 2)
```

---

## Phase 1 Success Gate

**Proceed to Phase 1 → Phase 2 cutover when ALL of the following hold for ≥7 consecutive days:**

1. **Per-track agreement ratio ≥ 0.90**
   - Measure: `shadow_validation_report.json['per_track_agreement_ratio']`
   - Why: Validates that Pi YOLO + tracker agree with iMac AIY on same bird tracks

2. **Intra-frame agreement ≥ 0.90**
   - Measure: `shadow_validation_report.json['intra_frame_agreement']`
   - Why: Within a single frame, Pi and iMac should label the same detections the same

3. **No regressions on rare species** (< 20 confirmed training images)
   - Measure: `shadow_validation_report.json['regressions_on_rare_species']` is `[]`
   - Why: Ensures flagship doesn't catastrophically fail on uncommon visitors

4. **Zero data integrity issues**
   - Measure: `audit_data_integrity.py --json | .orphan_rows | length == 0`
   - Why: Litestream mirror shouldn't corrupt even under sustained reads

5. **No ERROR logs in systemd journals**
   - Measure: `journalctl | grep ERROR | wc -l == 0`
   - Why: Both systems should run stably for 7 days

**Gate failure = rollback to Phase 0** (iMac resumes YOLO, Pi goes read-only)

---

## Phase 1 → Phase 2 Cutover (Day 7+)

Once all gates pass:

1. **Final validation report** (run once more to capture final day)
   ```bash
   python3 tools/shadow_validation_harness.py --final-report
   # Generates: PHASE1_FINAL_VALIDATION_REPORT.json
   ```

2. **Archive Phase 1 data**
   ```bash
   mkdir -p /tmp/phase1_shadow_archive/$(date +%Y%m%d)
   cp /tmp/shadow_validation_*/*.json /tmp/phase1_shadow_archive/$(date +%Y%m%d)/
   cp phase1_metrics.csv /tmp/phase1_shadow_archive/$(date +%Y%m%d)/
   ```

3. **Execute Phase 1 → Phase 2 handoff**
   - See `phase1-shadow-handoff-gates.md` for detailed steps
   - Pi takes audio, iMac audio pipelines stop, Phase 8 cutover plan kicks in

---

## Troubleshooting During Phase 1

### Symptom: per_track_agreement < 0.85 (stuck at day 3)

**Likely cause:** Tracker threshold (2.0) too loose on Pi, ID-switches fusing two birds

**Action:**
1. Check Pi tracker health: `shared.tracker.feeder.id_switches` — high count indicates threshold issue
2. Review confusion matrix: which species pairs are confusing?
3. Option A: Lower threshold from 2.0 → 1.5 temporarily
4. Option B: Check if AIY fallback is triggering correctly (use disagreement detector)
5. Re-run shadow validation after adjustment

### Symptom: intra_frame_agreement < 0.85 (inconsistent frame-by-frame)

**Likely cause:** Motion gate parameters or YOLO confidence threshold differs between Pi and iMac

**Action:**
1. Check YOLO confidence thresholds: Pi `bird_pipeline_v3.py:confidence=0.3` vs iMac `yolo_model.py:confidence=...`
2. Check motion gate parameters: MOG2 history, threshold — should be identical per ch 23 spec
3. Verify AIY vote-lock thresholds: both sides should use ≥3 votes, ≥0.35 conf, ≥60% agreement
4. Sync config, restart, re-run validation

### Symptom: Regressions on rare species (day 4)

**Likely cause:** Rare species not seen in training data, Pi model generalizes differently

**Action:**
1. Identify which species regressed: check `per_species_roc_curves` in shadow report
2. Sample images for that species from hold-out set
3. Verify AIY fallback is triggering for uncertain cases (use disagreement detector stats)
4. If systematic: may need to include rare species in Tier 2 Phase 2 training
5. Document and decide: proceed with known limitation, or re-train with rarer data

### Symptom: Data integrity failure (orphan rows appear)

**Likely cause:** Litestream mirror corruption, filesystem race condition

**Action:**
1. Stop Pi pipeline: `systemctl --user stop bird-pipeline`
2. Re-sync mirror: `rsync -av --delete imac.local:/bird-snapshots/logs/ ~/bird-snapshots/logs/`
3. Re-run audit: `python3 tools/audit_data_integrity.py --check`
4. If still failing: **HALT Phase 1** — data integrity is a blocker
5. Investigate root cause (DB corruption, Litestream state, filesystem issue)

---

## Metrics CSV Format

`phase1_metrics.csv` (append daily):

```
date,time,pi_id_switches,pi_active_tracks,pi_error_count,imac_id_switches,imac_active_tracks,imac_error_count,shadow_agreement,intra_frame_agreement,per_track_agreement,regressions,notes
2026-05-01,00:00,23,0,0,15,0,0,0.88,0.89,0.87,[],clean
2026-05-02,00:00,21,0,0,12,0,0,0.89,0.90,0.88,[],excellent_day
```

---

## Success Indicators

During Phase 1, watch for:

- ✅ **Stable agreement ratio:** trending toward 0.90 (not declining)
- ✅ **Zero orphan rows:** Litestream mirror is reliable
- ✅ **Zero errors in logs:** both systems rock-solid
- ✅ **No per-species regressions:** Tier 2 hasn't broken edge cases (yet)
- ✅ **ID-switch rate stable:** tracker threshold (2.0) is appropriate for Pi motion profile

---

## Reference

- `phase1-shadow-handoff-gates.md` — Handoff steps and coordination
- `tier2-readiness-checkpoint.md` — Overall Tier 2 phases 0–8
- `docs/04-hailo-engine.md` — Pi multi-model details
- `tools/shadow_validation_harness.py` — Metric collection harness

