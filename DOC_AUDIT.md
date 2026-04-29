# Documentation Audit

**Date:** 2026-04-26
**Repo:** /Users/vives/bird-classifier
**Docs audited:** CLAUDE.md, GUIDE.md, docs/superpowers/specs/ (all 8 active), docs/superpowers/plans/ (active plan), docs/superpowers/progress/ (active logs)
**Code roots scanned:** bird_pipeline_v3.py, pipeline/, dashboard/api.py, dashboard/index.html, dashboard/live.html, models/, ~/Library/LaunchAgents/com.vives.*, audio_analyzer.py, reviews_db.py, tools/

## What was reorganised

Beyond claim-level fixes, the full docs/ tree was restructured:

- **GUIDE.md** — complete rewrite (see Hallucination section below)
- **CLAUDE.md** — two drift fixes (service count, fps description)
- **55 historical docs** moved to `historical/` subfolders under specs/, plans/, progress/, and reviews/ — all pre-dated designs, implemented plans, and session handoffs that described superseded system states. Each received a `> HISTORICAL` banner.
- **4 README.md index files** created: `docs/superpowers/README.md`, `specs/README.md`, `plans/README.md`, `progress/README.md`
- **Active docs that received content fixes:** `specs/2026-04-23-airtight-review-system.md` (endpoint path, shipped vs. not-built status), `specs/2026-04-25-review-ui-helpers.md` (line numbers), `specs/2026-04-23-tier2-training-plan-v1.md` (RC2 calibration caveat)

**Active doc set after audit:**
- Root: CLAUDE.md, GUIDE.md, DOC_AUDIT.md
- specs/: 8 files (4 lit reviews, 2 as-built/helpers, 1 review system, 1 training plan)
- plans/: 1 active plan (RC2 — pending David's v1/v2/v3 choice)
- progress/: 2 files (side-findings log, 2026-04-26 evening handoff)

---

## Summary

| Bucket | Count |
|---|---|
| ✅ Verified | 14 |
| ⚠️ Drift | 3 (all auto-fixed) |
| ❌ Hallucination | 9 (all auto-fixed) |
| 🐛 Smell | 1 (flagged for review) |
| ⏭ Skipped | 2 |

---

## ✅ Verified

14 claims matched the code. No action needed.

<details>
<summary>Show verified claims</summary>

- `CLAUDE.md:40` — go2rtc at port 1984 → `com.vives.go2rtc.plist`: `/usr/local/bin/go2rtc`
- `CLAUDE.md:40` — bird_pipeline_v3 health:8100, SSE:8105 → `com.vives.bird-pipeline.plist`: `PIPELINE_HEALTH_PORT=8100`, `PIPELINE_SSE_PORT=8105`
- `CLAUDE.md:41` — dashboard uvicorn at port 8099 → `com.vives.bird-dashboard.plist`
- `CLAUDE.md:42` — audio_analyzer at port 8098 → `audio_analyzer.py` docstring
- `CLAUDE.md:43` — enhanced_audio at port 8096 → `com.vives.bird-enhanced-audio.plist`
- `CLAUDE.md:44` — cloudflared tunnel → `com.vives.bird-tunnel.plist`
- `CLAUDE.md:45` — rtsp-sync cron at 3:10 AM → `com.vives.bird-rtsp-sync.plist` StartCalendarInterval
- `GUIDE.md` (old):129 — AIY Birds V1 has 965 classes → `models/inat_bird_labels.txt`: 965 lines
- `GUIDE.md` (old):126 — `models/yolov8n_bird.onnx` exists → confirmed via `ls models/`
- `GUIDE.md` (old):128 — BirdNET V2.4 via birdnetlib → `audio_analyzer.py:2` docstring confirms V2.4
- `GUIDE.md` (old):130 — venv/ Python 3.12, venv-coral/ Python 3.9 → confirmed `python3 --version`
- `GUIDE.md` (old):132 — onnxruntime==1.23.2 → `pip show onnxruntime` returns 1.23.2
- `GUIDE.md` (old):133 — Coral USB used for species classification → `pipeline/classifier.py` YardClassifier + Coral lock
- `GUIDE.md` (old):116 — Audio location 41.35N −70.73W → `audio_analyzer.py` (Chilmark, MA)

</details>

---

## ⚠️ Drift (auto-fixed)

### 1. Pipeline SSE port was missing from GUIDE.md port table

- **Doc:** `GUIDE.md` (old):89
- **Original claim:** Port table listed 8100 as the pipeline port with no distinction between health and SSE
- **Code reality:** Port 8100 is the health endpoint; SSE events stream on port 8105 (`PIPELINE_SSE_PORT=8105` in `com.vives.bird-pipeline.plist`)
- **Fix applied:** GUIDE.md port table now lists both 8100 (health) and 8105 (SSE) as separate rows

### 2. CLAUDE.md services count wrong and bird-integrity-audit missing from table

- **Doc:** `CLAUDE.md:35`
- **Original claim:** "Services (6 active + 1 cron)"
- **Code reality:** 7 active + 1 cron. `com.vives.bird-integrity-audit` runs as a LaunchAgent (StartInterval) and was absent from the table.
- **Fix applied:** Updated heading to "7 active + 1 cron"; added `bird-integrity-audit` row to services table

### 3. CLAUDE.md substream fps description misleading

- **Doc:** `CLAUDE.md:49` (old)
- **Original claim:** "FrameCapture (native substream, 640x360 at 5fps)"
- **Code reality:** `pipeline/frame_capture.py:86–94` deliberately omits `-vf fps=N`. The sub-stream ffmpeg outputs at native camera fps (~30fps); Python reads at YOLO throughput (~5–7 fps on iMac). The `fps=5` constructor param is stored but not used as a throttle on the sub-stream (it is used for the hi-res ring buffer path only).
- **Fix applied:** Updated to "native substream, 640×360, reads at YOLO rate ~5–7 fps"

---

## ❌ Hallucination (auto-fixed)

All nine were in GUIDE.md, which described a system architecture that hasn't been active since early April 2026. The file has been fully rewritten to reflect the current v3 pipeline.

### 1. "Ten macOS LaunchAgents + one Docker container"

- **Doc:** `GUIDE.md` (old):61
- **Claim:** System runs ten LaunchAgents plus one Docker container (go2rtc)
- **Verification attempts:**
  - Direct: `ls ~/Library/LaunchAgents/com.vives.*` → 8 plists found (7 bird + 1 go2rtc), not 10
  - Docker: `docker ps | grep go2rtc` → no output; go2rtc is a native binary at `/usr/local/bin/go2rtc` via `com.vives.go2rtc.plist`
- **Fix applied:** GUIDE.md services section rewritten; Docker reference removed; correct count and table

### 2. `com.vives.bird-capture` listed as active

- **Doc:** `GUIDE.md` (old):66
- **Claim:** `capture_snapshots.py` runs as an active LaunchAgent
- **Verification:** `ls ~/Library/LaunchAgents/com.vives.bird-capture*` → not found
- **Fix applied:** Removed from active services; noted as deactivated in Historical Note

### 3. `com.vives.bird-classifier` listed as active

- **Doc:** `GUIDE.md` (old):67
- **Claim:** `classify.py --watch` runs as an active service
- **Verification:** `ls ~/Library/LaunchAgents/com.vives.bird-classifier*` → not found
- **Fix applied:** Removed from active services; explained in Historical Note (Coral USB single-session conflict)

### 4. `com.vives.bird-livedetect` listed as active

- **Doc:** `GUIDE.md` (old):72
- **Claim:** `live_detector.py` runs as active service on port 8097
- **Verification:** `ls live_detector.py` → "No such file or directory". File was deleted. No `bird-livedetect` plist exists.
- **Fix applied:** Removed entirely; Historical Note explains the migration to v3

### 5. `com.vives.bird-health-monitor` listed as active

- **Doc:** `GUIDE.md` (old):70
- **Claim:** `health_monitor.py` runs every 5 min checking and restarting services
- **Verification:** `ls ~/Library/LaunchAgents/com.vives.bird-health-monitor*` → not found
- **Fix applied:** Removed from services table

### 6. `bird_pipeline.py` referenced as the pipeline script

- **Doc:** `GUIDE.md` (old):51, 72, 159, 162 — multiple references
- **Claim:** The live pipeline is `bird_pipeline.py`
- **Verification:** `ls bird_pipeline.py` → "No such file or directory". The LaunchAgent plist runs `bird_pipeline_v3.py`.
- **Fix applied:** All references updated to `bird_pipeline_v3.py`

### 7. go2rtc described as a Docker container

- **Doc:** `GUIDE.md` (old):75, 190 — "— (Docker) | go2rtc" and `docker restart go2rtc`
- **Claim:** go2rtc runs as a Docker container
- **Verification:** `docker ps | grep go2rtc` → no output. `com.vives.go2rtc.plist` executes `/usr/local/bin/go2rtc` directly (native binary).
- **Fix applied:** Services table shows go2rtc as native binary; troubleshooting uses `launchctl kickstart` instead of `docker restart`

### 8. "Batch visual" pipeline described as active

- **Doc:** `GUIDE.md` (old):47
- **Claim:** `capture_snapshots.py` + `classify.py --watch` form an active batch pipeline
- **Verification:** Neither LaunchAgent plist exists; v3 pipeline handles all classification continuously via RTSP
- **Fix applied:** Batch pipeline description removed from Data Pipelines; moved to Historical Note

### 9. Port 8097 / live_detector described as a running service

- **Doc:** `GUIDE.md` (old):49, 87, 105–107
- **Claim:** `live_detector.py` runs at port 8097 with SpeciesVoter temporal voting
- **Verification:** `live_detector.py` does not exist. No port 8097 listener. No `bird-livedetect` plist.
- **Fix applied:** Entire section removed; Historical Note explains the migration

---

## 🐛 Smells (flagged for human review)

### 1. `_coral_lock` held during ONNX inference in `authoritative_classify()` — confidence: high

- **Code:** `pipeline/classifier.py:143–160`
- **What:** `authoritative_classify()` acquires `_coral_lock` and holds it for the duration of AIY inference. The comment at line 143 says "since AIY also runs on Coral" — but `SmartClassifier` never passes `tpu_model_path` to `SpeciesClassifier`, so AIY always uses the ONNX+CoreML backend. The lock guards against racing with yard's actual Coral inference, but the comment and the hold are misleading — the lock delays snapshot writes (up to 5s) even though AIY doesn't touch the TPU hardware.
- **Why suspicious:** If AIY doesn't use Coral, the lock hold during `authoritative_classify()` extends the snapshot latency unnecessarily every time yard is actively classifying. The real intent (serialize all classifier calls) would be cleaner expressed with a dedicated classifier lock rather than reusing the Coral hardware lock.
- **Triggered by doc claim:** `imac-live-classify-as-built.md:202` — "AIY runs on Coral too via the same `_coral_lock`" — corrected in the doc; code comment at `classifier.py:143` still reads "since AIY also runs on Coral" and should be updated.

### 2. auth.confidence > 1.0 in 504 post-watershed rows — confidence: high

- **Code:** `pipeline/snapshot_writer.py` → `_authoritative_species()`, `pipeline/classifier.py`
- **What:** 504 rows in `classifications.db` (post watershed id 756294) have `json_extract(extra_json, '$.authoritative.confidence') > 1.0`, max observed at 2.5. AIY Birds V1 softmax cannot produce values > 1.0 by construction.
- **Why suspicious:** The leak is most likely (a) `_authoritative_species` returning a raw logit/score on some code path instead of the softmax confidence, or (b) a numpy scalar serialization artifact. This directly affects RC2's bucket B threshold (`auth.confidence >= 0.1`): if confidence can be >1, rows that should be bucket A may land in bucket B, corrupting the calibration split.
- **Triggered by doc claim:** `docs/superpowers/progress/2026-04-25-side-findings.md` — "504 post-watershed rows have auth.confidence > 1 (max 2.5)" — already logged; repeated here as a blocker note before RC2 ships

---

## ⏭ Skipped

- `GUIDE.md` (old): "YOLO bird detection confidence: >= 0.3" (batch classify.py threshold) — skipped because `classify.py` is deactivated. The v3 threshold lives in `bird_pipeline_v3.py:~262` and was not independently audited in this run.
- `GUIDE.md` (old): "SpeciesVoter: 2 agreeing frames out of 5, 5s cooldown" — skipped; `live_detector.py` is deleted. SpeciesVoter does not exist in the v3 pipeline.
