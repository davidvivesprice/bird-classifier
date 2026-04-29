# Phase 1 Shadow Deployment — Handoff Gates & Coordination (2026-04-29)

**Status:** Ready for Phase 0 → Phase 1 transition

## Overview

Phase 1 is the **visual-cutover shadow deployment** (flagship YOLO detector on Pi, AIY fallback on iMac, parallel validation ≥7 days). This document specifies the exact handoff gates between Phase 0 (current state) and Phase 1, and the coordination points for the transition.

---

## Phase 0 → Phase 1 Handoff Checklist

### Pre-Handoff Verification (Phase 0 Complete)

- [x] bird-integrity-audit.service deployed on Pi
- [x] bird-integrity-audit.timer systemd units in place
- [x] refresh-rtsp.service deployed on Pi
- [x] refresh-rtsp.timer systemd units in place
- [x] Both services tested manually (run with `--wait`)
- [x] Tracker health endpoint exposed via `/api/pipeline/health`
- [x] Tier 2 readiness checkpoint completed (baseline captured, Phase 1 cleanlab ready)
- [x] Training data verification gate script (`tools/verify_training_data.py`) ready
- [x] Cleanlab Phase 1 script (`tools/tier2_phase1_cleanlab.py`) ready

### Phase 1 Kickoff (Visual Cutover)

**When:** David signals "ready for Phase 1" and Tier 2 Phase 1 (cleanlab) is underway or complete

**What happens:**
1. Pi takes over visual/YOLO detection (feeder camera)
2. iMac retains audio (BirdNET) and ground-camera detection (if enabled)
3. Both systems validate in parallel for ≥7 days
4. Metrics collected: per-species ROC curves, per-track agreement ratio, regressions on rare species

**Exact handoff steps:**

1. **Stop iMac visual pipeline** (preserve audio pipeline)
   ```bash
   # On iMac
   launchctl stop com.vivessato.bird-pipeline-v3
   # Leaves go2rtc, audio_analyzer, enhanced_audio, dashboard running
   ```

2. **Disable iMac's refresh-rtsc.timer** (Pi will take over RTSP management for feeder)
   ```bash
   # On iMac (if refresh-rtsp is running there)
   systemctl --user mask refresh-rtsp.timer || true
   ```

3. **Enable Pi systemd services**
   ```bash
   ssh vives@pi5.local "systemctl --user enable bird-integrity-audit.timer refresh-rtsp.timer && \
     systemctl --user start bird-integrity-audit.timer refresh-rtsp.timer"
   ```

4. **Set DB_MODE=mirror on Pi for integrity audit** (reads-only, no writes to classifications.db)
   ```bash
   # In /home/vives/.config/systemd/user/bird-integrity-audit.service, set:
   Environment="DB_MODE=mirror"
   # This prevents the audit from attempting writes while consuming read-only Litestream mirror
   ```

5. **Verify Pi pipeline starts**
   ```bash
   ssh vives@pi5.local "systemctl --user status bird-pipeline" 
   # Should show: Active: active (running)
   ```

6. **Enable shadow validation harness on iMac** (collects comparison metrics)
   ```bash
   # Shadow harness runs daily at 00:00 UTC
   # Compares Pi detections vs AIY detections on a shared feed
   # Output: shadow_validation_report.json (updated daily)
   ```

### Phase 1 → Phase 2 Handoff (Audio Cutover, ~7 days later)

**When:** Shadow validation report shows ≥0.90 intra-frame agreement, no regressions on rare species

**What happens:**
1. Pi takes over audio (BirdNET moves from iMac to Pi)
2. iMac's BirdNET shuts down
3. Pi's refresh-rtsp.timer is now responsible for RTSP health (already enabled)
4. All classification flows converge on Pi

**Exact handoff steps:**

1. **Unmask refresh-rtsp.timer on Pi** (was masked during Phase 1 visual-only)
   ```bash
   ssh vives@pi5.local "systemctl --user unmask refresh-rtsp.timer && \
     systemctl --user enable refresh-rtsp.timer && \
     systemctl --user start refresh-rtsp.timer"
   ```

2. **Stop iMac audio pipeline**
   ```bash
   # On iMac
   launchctl stop com.vivessato.audio-analyzer
   launchctl stop com.vivessato.enhanced-audio
   ```

3. **Start Pi audio pipeline** (if not already running)
   ```bash
   ssh vives@pi5.local "systemctl --user start audio-analyzer enhanced-audio"
   ```

4. **Verify all 4 Pi services are healthy**
   ```bash
   ssh vives@pi5.local "systemctl --user status bird-pipeline bird-dashboard \
     bird-integrity-audit audio-analyzer | grep Active"
   # All should show: Active: active (running)
   ```

5. **Run final data integrity audit** (both iMac mirror + Pi native)
   ```bash
   ssh vives@pi5.local "python3 tools/audit_data_integrity.py --check"
   ```

6. **Archive iMac services** (no longer needed, ready for retirement)
   ```bash
   # On iMac, move services to disabled/
   mkdir -p ~/disabled
   mv /Library/LaunchAgents/com.vivessato.bird-*.plist ~/disabled/
   ```

---

## Database Replication During Phase 1 (Read-Only Mirror)

**Context:** Pi's classification.db is read-only during Phase 1 shadow (consumes iMac data via Litestream mirror)

**Setup (Phase 0, pre-handoff):**

```bash
# On Pi, configure Litestream as a read-only subscriber to iMac's databases
# Litestream pulls snapshots from iMac:/home/vives/bird-snapshots/logs/
#   → Pi:/home/vives/bird-snapshots/logs/ (read-only mount or replica)

# Litestream config on Pi (at ~/.config/litestream/litestream.yml):
# dbs:
#   - path: /home/vives/bird-snapshots/logs/classifications.db
#     replicas:
#       - url: s3://bucket/...  (or http://iMac:8099/db/classifications.db)

# Or simpler: rsync from iMac every N minutes
# cron: */5 * * * * rsync -av vives@imac.local:/home/vives/bird-snapshots/logs/ ~/bird-snapshots/logs/
```

**Read-only enforcement during Phase 1:**

```bash
# In bird-integrity-audit.service, ENV var gates:
Environment="DB_MODE=mirror"

# In integrity_audit.py:
if os.environ.get("DB_MODE") == "mirror":
    conn.execute("PRAGMA query_only=ON")  # SQLite read-only mode
    # Prevents accidental writes while the mirror is being consumed
```

**Return to write mode at Phase 1 → Phase 2 transition:**

```bash
# Remove DB_MODE=mirror from bird-integrity-audit.service
# systemctl --user --user-mode edit bird-integrity-audit.service
# Delete: Environment="DB_MODE=mirror"
```

---

## Tier 2 Training Pipeline During Phases 0–1

While Phase 1 shadow runs (7+ days), Tier 2 training proceeds in parallel:

- **Phase 0 (now):** Cleanlab runs on 34K weak AIY labels (1–2 hours offline)
- **Phase 0-1 boundary:** Data verification gate, Phase 2 backbone training starts
- **Phase 1 (7 days):** Phase 2–5 training proceeds (phases 2–6 ≈ 10–15 days)
- **Phase 1 end (day 7+):** Phase 6 (QAT) + Phase 7 (shadow) validation start
- **Phase 2 (day 14+):** Phase 8 (live cutover) — flagship deployed, Phase 1 shadow validation review

---

## Validation Metrics (Phase 1 Shadow)

**Collected daily by shadow_validation_harness.py:**

```json
{
  "per_species_roc_curves": {
    "Northern_Cardinal": { "auc": 0.95, "precision": 0.92, "recall": 0.88 },
    "...": "..."
  },
  "per_track_agreement_ratio": 0.92,
  "intra_frame_agreement": 0.95,
  "regressions_on_rare_species": [],
  "pi_vs_aiy_confusion_matrix": { "...": "..." },
  "timestamp": "2026-05-06T00:00:00Z"
}
```

**Success gate:**
- `per_track_agreement_ratio >= 0.90`
- `no regressions on rare species (< 20 confirmed images)`
- `intra_frame_agreement >= 0.90`

---

## Cross-System Communication During Handoff

Coordination points recorded in `docs/working/progress/cross-claude-comms.md`:

1. **Phase 0 → Phase 1:** Tick off manual handoff steps above; note in comms
2. **Phase 1 progress:** Daily validation report summary
3. **Phase 1 → Phase 2:** Validation gate review, decision to proceed
4. **Phase 2 start:** Tier 2 flagship hot-swap ready (model compiled, Hailo manifest updated)

---

## Rollback Plan (If Phase 1 Validation Fails)

If shadow validation shows `per_track_agreement_ratio < 0.85` or new regressions:

1. **Revert visual-cutover** (iMac resumes YOLO, Pi stops detector)
   ```bash
   launchctl start com.vivessato.bird-pipeline-v3  # On iMac
   ssh vives@pi5.local "systemctl --user stop bird-pipeline"
   ```

2. **Diagnose:** Compare confusion matrices, per-species ROC curves, track agreement by species
3. **Iterate:** Adjust tracker threshold, re-tune vote-lock, or retrain on Pi-native data
4. **Retry Phase 1** after addressing root cause

---

## Reference

- `tier2-readiness-checkpoint.md` — Tier 2 pipeline phases 1–8
- `systemd-services-phase0.md` — Service definitions + timers
- `docs/04-hailo-engine.md` — Multi-model NPU details (Phase 2 onwards)
- `docs/09-the-unified-brain.md` — Long-term vision (Phase 8 is the cutover described here)

