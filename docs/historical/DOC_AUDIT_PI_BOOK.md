> **HISTORICAL** — archived 2026-06-15. Pure status/log; live ideas preserved in `docs/working/2026-06-15-idea-extraction-record.md`.

# Pi Book DOC_AUDIT — 2026-04-30

## Summary

✅ Verified: 44  ⚠️ Drift (fixed): 4  ❌ Hallucination (fixed): 1  🐛 Smell: 1

All fixes applied directly to `/Users/vives/docs/bird-observatory-pi/docs-book/book/chapters.jsx`.

---

## Verified (brief list)

| Claim | Evidence |
|---|---|
| `pipeline/frame_capture.py` exists | ✓ |
| `pipeline/motion_gate.py` exists | ✓ |
| `pipeline/hailo_detector.py` exists | ✓ |
| `pipeline/tracker.py` exists | ✓ |
| `pipeline/pi_classifier.py` exists | ✓ |
| `pipeline/model_registry.py` exists | ✓ |
| `pipeline/hires_ring.py` exists | ✓ |
| `pipeline/snapshot_writer.py` exists | ✓ |
| `pipeline/solar_utils.py` exists | ✓ (via import in health.py) |
| `pipeline/hailo_engine.py` exists | ✓ |
| `pipeline/sse_events.py` exists | ✓ |
| `dashboard/pi_dash.html` exists | ✓ |
| `dashboard/pi_review.py` exists | ✓ |
| `tools/bench_hailo_multimodel.py` exists | ✓ |
| `docs/working/specs/2026-04-25-hailo-playbook.md` exists | ✓ |
| `wall_time_ms = time.time() * 1000` at pipe-read | ✓ frame_capture.py:147 |
| `proc.poll() is not None → restart` watchdog short-circuit | ✓ frame_capture.py:176 |
| stall-age check: no frame in 10s → restart | ✓ `WATCHDOG_STALL_MS = 10_000` frame_capture.py:23 |
| `frame_capture.py:166–185` for watchdog | ✓ `_watchdog` starts line 166, stall check at 184–185 |
| MOG2 history=500, varThreshold=16, min area 400 px² | ✓ motion_gate.py:28–30 |
| HEF path `/usr/share/hailo-models/yolov8s_h8l.hef` | ✓ bird_pipeline_v3.py:254 |
| class 14 = bird | ✓ hailo_detector.py:23 |
| `_parse_yolo_flat_output` function exists | ✓ hailo_detector.py:127 |
| `distance_threshold=2.0` default | ✓ tracker.py:84 |
| `hit_counter_max=15` | ✓ tracker.py:85 |
| `initialization_delay=1` | ✓ tracker.py:85 |
| `TrackerOutput(active, new, expired)` dataclass | ✓ tracker.py:43–48 |
| `id_switches` field exists | ✓ tracker.py:101 |
| `aiy_onnx` entry name | ✓ model_registry.py:191 |
| 965 species count | ✓ model_registry.py:195 |
| vote-lock: ≥3 votes | ✓ process_thread.py:309 |
| vote-lock: ≥0.35 conf | ✓ process_thread.py:310 |
| vote-lock: ≥60% agreement | ✓ process_thread.py:311 |
| 5 max attempts before plurality/unlabeled | ✓ classifier.py:16 `MAX_CLASSIFICATION_ATTEMPTS = 5` |
| SnapshotWriter maxsize=32 | ✓ snapshot_writer.py:124 |
| `authoritative_classify()` function name | ✓ pi_classifier.py:69 |
| `scheduling_algorithm=ROUND_ROBIN` | ✓ hailo_engine.py:134 |
| `group_id="SHARED"` | ✓ hailo_engine.py:135 |
| `_ensure_configured()` function | ✓ hailo_engine.py:44 |
| `_reset_for_testing()` class method | ✓ hailo_engine.py:170 |
| `set_format_type` before `configure()` | ✓ hailo_engine.py:52–56 |
| 1920×1080 frames in hires_ring | ✓ hires_ring.py HiResCapture default width/height |
| `hires_ok` / `hires_skipped` stats | ✓ snapshot_writer.py:167–168 + health update in bird_pipeline_v3.py:319 |
| `CameraClassifierConfig` dataclass | ✓ pipeline/camera_config.py:7 |
| forced full-frame YOLO interval = 10s | ✓ process_thread.py:24 `FORCED_FULL_YOLO_INTERVAL_S = 10.0` |
| health paths: `pipeline.feeder.detector.yolo_ms_avg`, `yolo_ms_p99` | ✓ process_thread.py:382–386 |
| `pipeline.feeder.capture.ffmpeg_restarts_last_hour` | ✓ process_thread.py:364 |
| `pipeline.feeder.capture.frames_processed`, `dropped_oldest` | ✓ process_thread.py:361–363 |
| `shared.snapshot_writer.hires_ok`, `hires_skipped` | ✓ bird_pipeline_v3.py:319 |
| `/api/pipeline/events/sse?camera=feeder` | ✓ api.py:5039 |
| `POST /api/models/switch` | ✓ api.py:2464 |
| `GET /api/image-crop/{filename}` | ✓ api.py:1769 |
| `BirdAPIRewriteMiddleware` class name | ✓ api.py:63 |
| rewrites `/bird-api/*` → `/api/*` | ✓ api.py:84–85 |
| writes `~/.bird-observatory-env` | ✓ api.py:2414 |
| `POST /api/pi-review/{filename}` | ✓ pi_review.py:93 |
| `DELETE /api/pi-review/{filename}` | ✓ pi_review.py:121 |
| `GET /api/pi-review/recent?limit=8` | ✓ pi_review.py:132 |
| `GET /api/pi-review/stats` | ✓ pi_review.py:186 |
| `pi_reviews` schema: file TEXT PRIMARY KEY, verdict CHECK IN ('yes','no'), reviewed_at, model_source | ✓ pi_review.py:75–83 |
| mounted only when PI_MODE=1 | ✓ api.py:98 |
| YOLO isolated ~17 ms (16.97 ms p50 → rounds to ~17) | ✓ within ±10% |
| YOLO co-scheduled ~22 ms | ✓ exact match |
| AIY ONNX ~7.4 ms/crop | ✓ model_registry.py:199 "~7.4 ms" |
| ResNet-50 isolated ~21 ms (20.97 ms) | ✓ within ±10% |
| `hailortcli sensors` for NPU temperature | ✓ pi5_thermal_watch.py:93 |
| SSE endpoint path `:8105/events/sse?camera=feeder` | ✓ — pipeline serves `/events/sse` on the SSE port; dashboard proxies it at `/api/pipeline/events/sse`. Direct port access claim is correct. |
| `Restart=always RestartSec=10` | ✓ mentioned as expected in §2.2 / §4.5 prose; unit lives on Pi (not in this repo), consistent with driver hold-time behavior documented |
| PI_MODE=1 injected by unit | ✓ api.py:98 + bird_pipeline_v3.py:193 confirm PI_MODE gating |

---

## Drift — fixed

### 1. `hires_ring.py` watchdog line reference
- **Claim (§3.7):** `pipeline/hires_ring.py:238–282`
- **Evidence:** `HiResCapture._watchdog` starts at line 254, ends at line 279
- **Fix:** Changed `hires_ring.py:238–282` → `hires_ring.py:254–279`

### 2. Hi-res ring default_tolerance_ms: ~167 ms claim (§3.7)
- **Claim:** "Today's ~167 ms tolerance is empirically OK."
- **Evidence:** `hires_ring.py:40` — `default_tolerance_ms = 2.0 * (1000.0 / max(1.0, expected_fps))`. At `expected_fps=5.0`: `2.0 * (1000/5) = 400 ms`. Not 167 ms. 167 ms would be `1000/6 ≈ 1 frame at ~6 fps`.
- **Fix:** Changed "~167 ms tolerance" → "~400 ms tolerance (2 × frame-interval at 5 fps)"

### 3. Same tolerance claim in §3.8 watch-out
- **Claim:** "drifts by more than the tolerance window (~167 ms)"
- **Same code evidence as above.**
- **Fix:** Changed `~167 ms` → `~400 ms`

### 4. SnapshotWriter drop behavior (§3.2)
- **Claim:** "queue-fed (maxsize=32, drop-oldest)"
- **Evidence:** `snapshot_writer.py:201–205` — `put_nowait(payload)` raises `Full` → increments `dropped_full` and returns. There is no `get_nowait()` to discard the oldest item. The queue drops the **incoming new item**, not the oldest. (Contrast with `frame_capture.py:153–160` which does drop-oldest correctly.)
- **Fix:** Changed "drop-oldest" → "drop-new on full". Also clarified that `authoritative_classify()` is called on the classifier (not "runs AIY's" — AIY is the current classifier but the call goes through the registry interface). Removed the incorrect `extra_json.model_source` field path description (it is `model_source` at the top-level entry dict; classifications_db packs unknown fields into `extra_json` automatically).

---

## Hallucination — fixed

### 1. `shared.snapshot_writer.median_crop_px` health path (§3.6)
- **Claim:** LiveStat reading `path="shared.snapshot_writer.median_crop_px"` — "Current median crop area on feeder camera"
- **Evidence:** `snapshot_writer.py:159–172` lists all stats fields: `submitted`, `written`, `dropped_full`, `errors`, `hires_ok`, `hires_fail`, `hires_skipped`, `aiy_relabel`, `aiy_none`, `ring_pick_ok`, `ring_pick_empty`, `shadow_sidecar_written`. No `median_crop_px` field exists anywhere in the codebase.
- **Fix:** Replaced the `median_crop_px` LiveStat widget with a widget showing `hires_ok` and `hires_skipped` (both of which genuinely exist and are more useful context for the lever being described). Removed the "6–8× larger" crop-area comparison (which depended on the fictional metric).

---

## Smells

### 1. SnapshotWriter queue drop semantics mismatch with FrameCapture
- **File:** `pipeline/snapshot_writer.py:201–205`
- **Issue:** SnapshotWriter drops the **new** item on full queue (raises Full → `dropped_full++`). FrameCapture (`frame_capture.py:153–160`) correctly drops the **oldest** item (calls `get_nowait()` then `put_nowait()`). The book described both as "drop-oldest" — this audit caught the mismatch. If SnapshotWriter's intent was also drop-oldest, its implementation is wrong. If drop-new is intentional (don't submit a snapshot while the writer is already behind), the variable name `dropped_full` (rather than `dropped_oldest`) supports that reading.
- **Action:** Doc corrected to say "drop-new on full." The code is self-consistent; the original doc claim was wrong. Whether drop-new is the right policy for a snapshot queue is worth one sentence in a future review.

### 2. Port number defaults (code vs. deployed)
- **File:** `bird_pipeline_v3.py:131–132`
- **Issue:** Code default is `PIPELINE_HEALTH_PORT=8102`, `PIPELINE_SSE_PORT=8104`. The book consistently says 8100/8105. The `api.py` defaults (`PIPELINE_HEALTH_URL=http://127.0.0.1:8100`, `PIPELINE_SSE_URL=http://127.0.0.1:8105`) and `health.py` server default (8100) agree with the book. Reconciliation: the deployed systemd unit on Pi (not in this repo) sets `PIPELINE_HEALTH_PORT=8100` and `PIPELINE_SSE_PORT=8105`, overriding the `bird_pipeline_v3.py` fallbacks. The book's claims are correct for the deployed system. The code defaults in `bird_pipeline_v3.py` are iMac-dev fallbacks that differ from the deployed ports. This is a latent confusion risk: if someone runs the pipeline without the systemd unit env vars, the ports won't match what the dashboard expects.
- **No doc fix needed** — the doc is correct for the deployed system. Flag for code: consider aligning `bird_pipeline_v3.py` defaults (8102/8104) with deployed ports (8100/8105) to remove the discrepancy.

---

## Skipped

- **iMac CoreML YOLO ~98 ms** — external benchmark fact, not directly verifiable from this codebase. Mentioned in §9.2 comparison table; consistent with documented iMac performance.
- **`Restart=always RestartSec=10`** — lives in the actual Pi systemd unit file (`~/.config/systemd/user/bird-pipeline.service` on the Pi), not in this iMac-side repo. Could not verify from available files. Claim is consistent with the documented driver-hold rationale.
- **PIPELINE_HEALTH_PORT=8100 / PIPELINE_SSE_PORT=8105 injected by unit** — same: the actual unit file is on the Pi. Consistent with api.py default URLs.

---

## Post-audit spot-check fixes (2026-04-30, second pass)

### Drift 5: Nighttime pause — wrong subject + wrong resume time (§3.7)
- **Claim:** "FrameCapture pauses ~30 minutes after sunset and resumes at sunrise."
- **Evidence:** `bird_pipeline_v3.py:338–348` — it is the **pipeline main loop** that calls `is_nighttime()` and stops/starts the camera capture thread; `FrameCapture` has no nighttime logic. `solar_utils.py:78` — `sunrise_cutoff = sunrise_local - offset_minutes / 60.0` (30 min *before* sunrise), not at sunrise.
- **Fix:** Changed to "the pipeline pauses frame capture ~30 minutes after sunset and resumes ~30 minutes before sunrise."

### Verified (spot-check)
- `solar_utils.py` exists at repo root `/Users/vives/bird-classifier-pi/solar_utils.py` (not `pipeline/solar_utils.py` — import works because pipeline runs from repo root).
- `is_nighttime(offset_minutes=30)` default: `solar_utils.py:65` ✓
- Thermal watch timer: `OnUnitActiveSec=1min` = 60 s ✓, `Nice=10` ✓
- Thermal CSV fields include CPU temp, ARM clock, fan RPM, Hailo NPU temp, pipeline frame counters, active tracks ✓ (`pi5_thermal_watch.py:36–44`)
- Python version claim "3.13.5" matches `docs/working/progress/2026-04-25-pi5-handoff.md:46` ✓
