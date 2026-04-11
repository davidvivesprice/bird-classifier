# Live Detection Pipeline v2 — Whole-Implementation Review

**Date:** 2026-04-10 (first pass), amended 2026-04-11
**Reviewer:** Claude (Opus 4.6, with specialist subagents)
**Spec:** [`docs/superpowers/specs/2026-04-10-live-detection-v2-design.md`](../specs/2026-04-10-live-detection-v2-design.md)
**Plan:** [`docs/superpowers/plans/2026-04-10-live-detection-v2.md`](../plans/2026-04-10-live-detection-v2.md)
**Implementation status:** Merged to main, currently running on both cameras

> This is the "final whole-implementation review" step from `superpowers:subagent-driven-development` that was skipped at v2 cutover. The pipeline shipped, then the team moved straight into dashboard fire-fighting and architectural debate, and the codebase was never reviewed end-to-end against the spec. Five fix commits landed on top of the merge without any review at all. This document corrects that.

> **Amendment, 2026-04-11:** The original review (sections 1–8 below) framed the 2017 Intel iMac as the root cause of performance problems. **That framing was wrong.** David correctly pointed out that Frigate runs 7 cameras with smooth tracking on a cheap Synology NAS, which is proof that the hardware class is adequate. The real root cause is that our pipeline is doing **unnecessary work that Frigate never asks its hardware to do**. Section 9 ("Architectural Audit — What We Do That Frigate Wouldn't") has been added to correct this and supersedes the hardware-bottleneck framing in sections 1, 6, and 7. The bugs and spec-compliance findings in sections 3–5 remain valid; the recommendations in section 7 should be read *after* section 9.

---

## Executive Summary

The pipeline v2 implementation **mostly matches the spec on paper** and **mostly works in practice** — 50/50 unit tests pass, the orchestrator runs continuously, both cameras produce data, and the SQLite event store contains 23,194 events / 1,063 tracks across 14+ species in 3.25 hours of runtime. The post-merge fixes (full-frame YOLO, MJPEG dashboard, AIY-on-CPU) made meaningful improvements that the spec was never updated to reflect.

But there are **four critical bugs** and **eight important bugs** that explain almost every runtime anomaly we observed, and **the headline finding sits underneath all of it**: the host hardware is a 2017 Intel iMac running at the absolute limit of its RAM and 2.5–3× its CPU core count. **No model swap or detector tweak will save us — the binding constraint is the machine, not the inference path.** The 1760 ms YOLO p99 tail is page faults and swap thrashing, not ONNX Runtime quirks.

The right next moves, in priority order:

1. **Fix the four critical correctness bugs** (audio DB path, audio SQL schema, watchdog `_restart()` reset, and dashboard MJPEG freshness indicator) — all are small, all are explanatory, all are non-controversial.
2. **Reduce work** on the hardware. The biggest single lever is **cutting capture resolution from 1920×1080 to 720×480 or 640×360** at the ffmpeg stage. This drops decode cost, memory copies, motion-gate work, and YOLO preprocessing all at once.
3. **Restore the two-stream architecture** (smooth HD via existing go2rtc → dashboard, label overlay via SSE) so the visible video rate stops being chained to the YOLO rate.
4. **Decide whether to keep the audio cross-check at all.** It is structurally dead today. Either re-wire it to the real schema or remove it from the spec.
5. Save the model-swap conversation for **after** the hardware-constrained reality is reflected in the code. Once we're not swapping, we can re-measure and decide whether YOLO11n / native CoreML is worth pursuing.

---

## 1. The Hardware Reality (Read This First)

| Metric | Value | Implication |
|---|---|---|
| Model | iMac18,2 (2017 21.5") | Pre-Apple-Silicon — no ANE |
| CPU | Intel i5-7400 @ 3.0 GHz, 4 cores | Kaby Lake, no hyperthreading |
| RAM | 8 GB total | Tight |
| GPU | Intel HD 630 / Iris Plus 640 (integrated) | CoreML routes here, not ANE |
| **Load avg (1m / 5m / 15m)** | **10.33 / 11.67 / 12.33** | **2.5–3× oversubscribed sustained** |
| **Free RAM** | **46 MB** of 8144 MB | **99.4% RAM consumed** |
| Compressor | 1165 MB | Heavy macOS memory compression |
| Lifetime swap ops | 2,375,198,224 in / 2,487,649,358 out | **2.4 billion** swap operations |
| Lifetime swap written | 17 TB | The disk has been hammered |

**Implications for everything else in this review:**
- The 1760 ms YOLO p99 spike is most plausibly **page-fault stalls**, not an ONNX Runtime bug.
- The "yolo_avg=86 ms" feeder number is achievable on a healthy 4-core Intel for a 4 BFLOP model running through ONNX → CoreML → Intel GPU. It's not great but it's not unreasonable. The tail is the problem.
- Any "make the model faster" pursuit is fighting the wrong battle until we reduce **work**, **memory pressure**, and **CPU contention**. A 4× faster YOLO that still operates on 1080p frames in a swap-thrashing process will not feel any faster.
- Apple's ANE-based CoreML acceleration recommendations from generic benchmarks **do not apply to this machine**. CoreML will use Metal-on-Intel-GPU, which is a much smaller win than ANE-on-M-series.

---

## 2. Runtime State Snapshot (3.25 hours uptime)

### Pipeline health (`/health`)

```
overall: degraded
uptime_s: 11732
```

| Camera | Frames | Detections | yolo_avg | yolo_p99 | Active tracks | Stationary | Notes |
|---|---:|---:|---:|---:|---:|---:|---|
| feeder | 38,155 | 7,864 | **86 ms** | **1,760 ms** | 1 | 0 ever | Test loop input |
| ground | 50,642 | 1,989 | **7 ms** | 425 ms | 0 ever | 0 ever | **In watchdog restart loop since 17:05** |

### Classifier stats (global, single SmartClassifier instance)

```
yard: 658
aiy: 0
both_agree: 0
audio_confirmed: 0
unlabeled: 2
lock_timeouts: 0
retries: 0
```

### Event store (`pipeline.db`)

- `pipeline_events`: **23,194 rows**
- `pipeline_tracks`: **1,063 rows**
- Species histogram (top of 14+): Downy Woodpecker (328), **None (323)**, Black-capped Chickadee (175), House Finch (68), White-breasted Nuthatch (58), Tufted Titmouse (47), Dark-eyed Junco (21), Hairy Woodpecker (13), Song Sparrow (13), American Goldfinch (7), Mourning Dove (4), Northern Flicker (2), Brown-headed Cowbird (1), European Starling (1), Lincoln's Sparrow (1), Northern Cardinal (1)
- Every event row has `model_source='yard'` — **AIY has never won classification**, ever
- 30% of all tracks have `species=None` — but `classifier.stats['unlabeled']` says only 2 (discrepancy: stats counts classifier *calls*, tracks have a separate species-assignment lifecycle)

### Stream topology

- `feeder-main` is the **test video loop** (`exec:ffmpeg ... playlist.txt` in go2rtc)
- `ground-main` is the **real UniFi camera** (`rtsp://192.168.4.9:7447/RTSnv0lLeUd8cJDw`)
- HLS recorders run as separate ffmpeg subprocesses for both cameras (4 ffmpegs total + 1 capture per camera = 6 ffmpeg processes against the same 8GB RAM pool)

### Tests

- **50/50 pipeline unit tests passing** (108 s) under `venv-coral`
- Two `pytest.mark.slow` markers warn (unregistered) — cosmetic

---

## 3. Spec Compliance — File by File

| File | Status | Notes |
|---|---|---|
| `pipeline/frame.py` | ✅ compliant | Trivially matches |
| `pipeline/frame_capture.py` | ⚠️ deviation | Watchdog `_restart()` doesn't reset `last_frame_ms` (Critical Bug 3); pipe drainer can spin on dead pipe |
| `pipeline/motion_gate.py` | ✅ compliant | MOG2 + morphology + contour → bbox per spec |
| `pipeline/detector.py` | ⚠️ silent deviation | Region-based YOLO removed in fix `c34c29b`, replaced with full-frame. Justified (ONNX squashes to 640² anyway) but spec was never updated. Three dead methods + dead constructor arg remain |
| `pipeline/tracker.py` | ✅ compliant | Frigate-inspired distance fn matches spec; `Track.trust_level` is dead |
| `pipeline/classifier.py` | ❌ multiple deviations | (1) AIY moved off Coral to ONNX → `_coral_lock` no longer serializes anything contended; (2) AIY confidence is `uint8/100` not a probability (Critical Bug 5); (3) `_audio_lookup` SQL hits a non-existent table (Critical Bug 2); (4) `stats["retries"]` never incremented |
| `pipeline/event_store.py` | ✅ mostly compliant | Schema matches; `daily_checkpoint()` is defined but never called from anywhere; `INSERT OR REPLACE` may silently overwrite same-frame same-track events |
| `pipeline/annotator.py` | ✅ compliant | Label pill + downscale + JPEG encode work; minor stylistic deviation (plain rectangle instead of rounded pill, ASCII "vv" badge) |
| `pipeline/debug_stream.py` | ⚠️ deviation | `is_slow()` per-client backpressure check from spec is missing; failed clients are flagged but never removed (memory leak in long-running pipeline) |
| `pipeline/hls_recorder.py` | ✅ mostly compliant | Missing `second_level_segment_index` HLS flag; `chunks_written` / `last_chunk_ms` stats are initialized but never updated |
| `pipeline/health.py` | ❌ half-compliant | `_compute_status` only checks 4 of the 8 spec'd degraded/broken rules; `shared` section never populated |
| `pipeline/process_thread.py` | ⚠️ deviation | `num_frames` bug (Bug 6); p99 calc is wrong (Bug 8); classifier stats reported per-camera but are actually shared (Bug 7); `forced_full` interval logic is dead since detector ignores the distinction |
| `bird_pipeline_v2.py` | ❌ deviation | `BIRDNET_DB` path is wrong (Critical Bug 1) |
| `dashboard/api.py` (v2 parts) | ⚠️ leak risk | `/api/pipeline/events` instantiates a new `EventStore` per request, spawning a flusher thread each time (Bug 12) |
| `dashboard/index.html` (v2 parts) | ❌ major deviation | (1) Discarded WebSocket+canvas + 4 spec dashboard requirements (cellular mode, scrubbing, best-visit card, tab-backgrounded close); (2) MJPEG freshness indicator is broken (Critical Bug 4); (3) `connectPipelineSSE` is ~90 lines of dead code; (4) `visibilitychange` handler references `_v2Ws` which is never assigned |
| `tests/pipeline/test_detector.py` | ✅ updated post-merge | Tests now match the full-frame contract |

---

## 4. Bugs Found

### Critical (correctness, observable in production)

**C1. Wrong audio DB path** — `bird_pipeline_v2.py:20`
```python
BIRDNET_DB = Path.home() / "bird-snapshots" / "logs" / "birdnet_local.db"
```
The actual file lives at `~/bird-snapshots/birdnet-audio/birdnet_local.db` (cross-checked in `health_monitor.py:42`, `dashboard/api.py:1889`, `yard_prior.py:25`, `db_pool.py:27`, `audio_analyzer.py:68`). Path 4 audio cross-check has been silently dead since merge.
**Fix:** `BIRDNET_DB = Path.home() / "bird-snapshots" / "birdnet-audio" / "birdnet_local.db"`

**C2. Audio cross-check SQL hits a non-existent table** — `pipeline/classifier.py:146-151`
```python
row = conn.execute(
    """SELECT common_name FROM detections
       WHERE camera = ? AND timestamp_ms BETWEEN ? AND ?
       ORDER BY confidence DESC LIMIT 1""",
    (camera, start_ms, end_ms),
).fetchone()
```
The real `birdnet_local.db` schema is `notes(date, time, common_name, source, ...)` — no `detections` table, no `timestamp_ms` column, no `camera` column. The query raises every single call, the exception is swallowed by the broad `except`, and `_audio_lookup` returns `None` forever. Even if Critical Bug 1 is fixed, audio_confirmed will still be 0 because of this.
**Fix:** Either rewrite the query against the real `notes` schema, or rip Path 4 out entirely and update the spec. Decision required (see Recommendation R7).

**C3. `_restart()` doesn't reset `last_frame_ms`** — `pipeline/frame_capture.py:164-178`
```python
def _restart(self):
    proc = self.proc
    if proc is not None:
        try: proc.kill(); proc.wait(timeout=5)
        except Exception: pass
    try:
        self._spawn_ffmpeg()
        self.stats["ffmpeg_restarts"] += 1
    except Exception as e: ...
```
The watchdog at line 154 computes `age_ms = now - self.stats["last_frame_ms"]`. After kill+respawn, `last_frame_ms` still holds the timestamp of the *previous* ffmpeg's last frame. On the next watchdog tick (2 s later), age is still > 10,000 ms, so it restarts again. The growing stall numbers in the log (10220 → 17789 → 20040 → 22337 → ...) are exactly `wall_clock_now - the_old_timestamp`. **This is what's happening to the ground camera right now.**
**Fix:** Add one line at end of `_restart()`:
```python
self.stats["last_frame_ms"] = time.time() * 1000
```

**C4. Dashboard MJPEG freshness indicator is broken** — `dashboard/index.html` (v2 client)
The `<img>` element's `load` event fires **once** for the first part of a `multipart/x-mixed-replace` response in every browser engine — subsequent parts do **not** refire `load`. So `_v2LastFrameMs` is updated exactly once per *reconnect*, not per frame. The "pulsing" / "stale" / "Reconnecting…" toast logic in the 500 ms interval is therefore reading a connection-age timestamp, not a freshness timestamp, and will spuriously degrade on a perfectly healthy stream.
**Fix:** Drop the `img.load`-based freshness logic. Poll `/api/pipeline/health.pipeline.<camera>.capture.last_frame_age_ms` at the existing 500 ms interval — once Bug I5 is fixed and that field is actually populated.

### Important (correctness, lower visibility OR larger blast radius)

**I1. AIY confidence is uint8/100, not a probability** — `pipeline/classifier.py:131`
```python
"confidence": float(top.get("raw_score", 0)) / 100.0,
```
`raw_score` is the AIY model's quantized uint8 output (0–255). Dividing by 100 means a "passing" score of 0.60 corresponds to raw_score ≥ 60/255 ≈ 23.5%. The threshold isn't semantically "60% confidence" — it's "23.5% of max quantization value." Combined with the yard-first decision tree, this means Path 2 (yard fails → AIY) isn't actually testing what it looks like it's testing.
**Fix:** Either divide by 255 and tune thresholds against probabilities, or commit to a raw_score threshold and rename the field. Document the chosen scale at `CONFIDENT` and `UNCERTAIN_LOW`.

**I2. `num_frames` in track summary is the global counter** — `pipeline/process_thread.py:110`
```python
self.event_store.write_track_summary(
    camera=self.name, track=track,
    num_frames=self._stats["frames_processed"],  # ← global, not per-track
)
```
Track 1063 ends up with `num_frames=53098`, more than the total frames the camera has ever processed (50,642). Every track summary in the database is wrong. **(Confirmed by direct DB inspection.)**
**Fix:** Add `frame_count: int = 0` to `Track`, increment in `tracker.update()` for each track that received a hit, pass `track.frame_count` here.

**I3. Per-camera classifier stats are shared** — `pipeline/process_thread.py:192` + `pipeline/classifier.py`
The single `SmartClassifier` instance has a single `self.stats` dict. The process thread reports it under `pipeline.<camera>.classifier`, so both cameras display identical numbers. Operationally, you can't tell which camera is classifying what.
**Fix:** Have `SmartClassifier.stats` become `dict[camera_name, dict]`, indexed at write time using the `camera` arg already passed to `classify()`. Process thread pulls only its slice.

**I4. p99 calculation is mathematically wrong** — `pipeline/process_thread.py:170`
```python
yolo_p99 = sorted(samples)[-max(1, len(samples) // 100)]
```
For any `n < 200`, `n // 100` is 0 or 1, `max(1, ...)` is 1, and `sorted[-1]` is the **maximum** (p100), not the 99th percentile. The reported `1760 ms p99` for feeder is really "the single worst frame in the last 100 samples" — useful as a worst-case probe but mislabeled.
**Fix:** `yolo_p99 = sorted(samples)[int(len(samples) * 0.99)]` or use `numpy.percentile(samples, 99)`.

**I5. `FrameCapture.stats` are never published through health** — `pipeline/process_thread.py:175-178`
The `capture` health dict only contains `last_frame_age_ms` and `frames_processed`, which the process thread computes itself. `FrameCapture.stats` already tracks `dropped_oldest`, `ffmpeg_restarts`, and `last_frame_ms` — none of which propagate to the dashboard. So you can't see the ground camera's restart loop on the health endpoint, only by tailing the log.
**Fix:** Pass the `FrameCapture` instance into `CameraProcessThread` (or its stats dict) and merge those fields into the `capture` health update.

**I6. Half the spec'd `_compute_status` rules are missing** — `pipeline/health.py`
Spec § Health Monitoring lists 8 degraded/broken triggers; implementation checks 4. Missing: `classifier.retry exhaustion > 20% of tracks`, `coral_unavailable > 5min`, `recorder restarts > 3/h`, `camera unreachable > 60s in daytime`. The currently-degraded ground camera in restart loop **does not trigger any rule** — the "degraded" label is firing on feeder's bad p99, not on ground's actual failure.
**Fix:** Implement the missing rules.

**I7. `yolo_ms_samples` includes zero-cost skip frames** — `pipeline/process_thread.py:80`
When the motion gate returns no regions, `BirdDetector.detect()` returns `[]` immediately at near-zero cost, but the process thread still appends that 0–5 ms timing into the rolling histogram. Ground sees mostly empty frames, so the average drops to 7 ms — which looks great but is meaningless because YOLO barely ran. Feeder's 86 ms is also pulled down by skip frames.
**Fix:** Only record the timing into `yolo_ms_samples` when `len(detections) > 0` OR `forced_full`, OR keep a separate "frames where YOLO actually ran" counter.

**I8. `dashboard/api.py:/api/pipeline/events` thread leak** — `dashboard/api.py:3463`
Every request constructs a new `EventStore`, which spawns a background flusher thread, then `shutdown()` joins it with a 2 s timeout. Per-request thread spawn + 2 s tail latency is bad on both axes.
**Fix:** Cache one `EventStore` at module level, or open a direct read-only `sqlite3.connect(db_path, uri=True)` for query endpoints.

### Possible bug — needs verification (mine, not subagent's)

**P1. Events table `confidence` may store the YOLO bbox score, not the classifier score** — `pipeline/process_thread.py:99`
Direct DB observation: track 67's events show `confidence` jumping from 0.471 → 0.471 → 0.334 across consecutive frames, all with `model_source='yard'`. Since classifier.py:56 requires `confidence >= 0.60` to assign `model_source='yard'`, the value being stored cannot be the classifier's verdict. My hypothesis is that `track.confidence` is mutated every frame by the tracker (with the latest YOLO bbox confidence), and the event-write path uses the current `track.confidence` rather than the classifier-time confidence. **Needs verification by reading `pipeline/tracker.py`'s update path to confirm whether `track.confidence` is overwritten.** If confirmed, fix is to add a separate `track.species_confidence` field that the classifier sets and the event-write path reads.

### Minor / dead code

**M1.** `pipeline/classifier.py:43` — `stats["retries"]` initialized, never incremented.
**M2.** `pipeline/tracker.py:23` — `Track.trust_level = "normal"` initialized, never assigned.
**M3.** `pipeline/detector.py:50, 84-113` — `_detect_region`, `_is_stationary_only`, `stationary_track_regions_fn` constructor arg, `self.get_stationary` attribute. All dead after the full-frame switch. ~30 lines.
**M4.** `pipeline/hls_recorder.py:24` — `stats["chunks_written"]`, `stats["last_chunk_ms"]` initialized, never updated.
**M5.** `pipeline/debug_stream.py:121-128` — `mark_failed()` flag set but no code removes failed clients; long-running pipeline leaks dead client entries.
**M6.** `pipeline/event_store.py:213` — `daily_checkpoint()` defined, never called from any code path.
**M7.** `pipeline/process_thread.py` — `FORCED_FULL_YOLO_INTERVAL_S` interval logic runs every 10 s but the detector ignores the distinction (always full-frame when motion).
**M8.** `dashboard/index.html:5904-5993` — `connectPipelineSSE` (~90 lines) is uncalled. Dead since v2 cutover.
**M9.** `dashboard/index.html:7581-7588` — `visibilitychange` handler references `_v2Ws` which is never assigned (MJPEG path uses `<img>`, not WebSocket). Both branches are no-ops.
**M10.** `pipeline/hls_recorder.py:38` — missing `second_level_segment_index` HLS flag from spec; possible filename collision on sub-second restart.
**M11.** Norfair scalar-distance startup warning — intentional per spec (Frigate-inspired scalar fn). Vectorizing would silence it but is low priority.
**M12.** Old `bird_pipeline.py` is still in tree. Expected during the spec'd 1-week rollback window; worth removing on the cleanup calendar.

---

## 5. Runtime Anomalies → Code Path Mapping

| Observed | Root cause | Bug ID |
|---|---|---|
| `audio_confirmed=0` always | Wrong DB path **AND** wrong SQL schema (two independent reasons) | C1 + C2 |
| `aiy=0` always | Path 1 yard ≥ 0.60 short-circuits + AIY threshold semantics broken (uint8/100) | I1 + design |
| `both_agree=0` always | Same as above — yard always wins, AIY never gets compared | I1 + design |
| `unlabeled=2` (tiny) | Yard model is reliable on the test video at the current threshold | (working as designed for this video) |
| 30% of tracks have `species=None` despite `unlabeled=2` | Stats counts classifier *calls*; tracks that are never classified (small bbox, expired before classification) account for most of the difference | not a bug |
| Identical classifier stats on both cameras | Single shared `SmartClassifier` instance, single global stats dict | I3 |
| Ground `yolo_avg=7 ms` | Skip-frame timings included in histogram + ground sees mostly-empty frames | I7 |
| Feeder `yolo_p99=1760 ms` | (1) p99 calc is actually p100/max + (2) hardware swap thrashing produces real spikes | I4 + hardware |
| Ground watchdog restart loop (every 2-3 s since 17:05) | `_restart()` doesn't reset `last_frame_ms` | C3 |
| Overall = "degraded" | Triggered on feeder's broken p99 number, not on ground's actual failure | I4 + I6 |
| Ground stationary tracks = 0 ever | Restart loop never lets a track survive `hit_counter_max=15` | downstream of C3 |
| Norfair scalar-distance warning every startup | Intentional design choice per spec | (working as designed) |

---

## 6. Detection-Stage Alternatives Analysis

The spec asked us to weigh alternative initial-detection stages. The dispatched research subagent produced a thorough comparison of YOLOv8n at lower input resolution, YOLOv5n/v6, YOLO11n, NanoDet-Plus, MobileNet-SSD v2/v3, EfficientDet-Lite0, Apple Vision framework, motion-only gating, and detector decimation.

**The original top recommendation was wrong for this hardware** — it recommended native CoreML export for ~4× speedup via Apple's ANE. **There is no ANE on a 2017 Intel iMac.** CoreML on this machine routes through Metal-on-Intel-GPU (HD 630 / Iris Plus 640), where the speedup is closer to 1.3–1.6× and is largely about reducing Python+ONNX wrapper overhead, not raw inference compute. With this correction, the realistic ranking changes:

### Corrected ranking for Intel iMac 18,2

| Rank | Option | Cost | Expected speedup | Accuracy cost |
|---|---|---|---|---|
| **1** | **Cut capture resolution at ffmpeg from 1920×1080 → 720×480 or 640×360** | Low (config-only, ~2 h) | ~3–5× memory, ~2–3× decode CPU, helps EVERYTHING downstream | Small for feeder, possibly real for ground (small distant birds) |
| **2** | **Re-export YOLOv8n at imgsz=416** | Low (~2 h) | ~2× compute (less because of runtime overhead) | Small (~5% mAP on small birds) |
| **3** | **Detector decimation: run YOLO every 3rd frame, tracker interpolates** | Medium (~6 h) | ~3× detection load on motion-heavy feeder | Risk of missing fast flyby birds |
| **4** | **Fix ONNX Runtime config: force FP32, single-thread, disable buggy ops** | Low (~2 h, mostly debugging) | Possibly collapses the p99 tail | None |
| **5** | YOLO11n via ONNX | Low-medium (~4 h, retrain required) | ~1.5× | None / slightly better |
| **6** | Native CoreML `.mlpackage` | Medium (~4-6 h, coremltools dep) | ~1.3–1.6× on Intel GPU (NOT 4× — that's ANE only) | None |
| ❌ | NanoDet, MobileNet-SSD, EfficientDet-Lite, YOLOv5n | High effort, marginal/no win, worse small-object recall |
| ❌ | Apple `VNRecognizeAnimalsRequest` | **Cats and dogs only — no birds.** Per [Apple docs](https://developer.apple.com/documentation/vision/vnanimalidentifier), revision 1 is the only revision and it returns `["Dog", "Cat"]` from `knownAnimalIdentifiers`. This was a false-premise instruction in my brief to the subagent — well caught |
| ❌ | Motion-only gating (no detector) | "Free" but catastrophic for precision — the species classifier would confidently misclassify squirrels, branches, and shadows as birds, directly violating the mission's "If it says Cardinal, there better be a Cardinal" principle |

### The big architectural insight

**The biggest lever is not the model, it's the resolution and the work-per-frame.** A 2017 Intel iMac running 6 ffmpeg processes against 1920×1080 RTSP, decoding to BGR raw frames, copying them through Python, running motion gate on the full frame, running YOLO preprocessing on the full frame, and then encoding annotated JPEGs back out — at 5 FPS × 2 cameras — is doing **work that is fundamentally too expensive for the hardware**, regardless of which model sits in the middle.

Cutting capture to 720×480 (518K pixels vs 2.07M pixels — 4× reduction) should:
- Reduce ffmpeg decode CPU by ~4× per camera
- Reduce per-frame memory copies by ~4×
- Reduce motion gate (OpenCV BGR ops) work by ~4×
- Reduce YOLO preprocessing (resize to 640²) work proportionally
- Free RAM under pressure → fewer page faults → less p99 tail

Combined with the existing motion gate (which already skips most ground frames), this should make the pipeline feel substantially smoother without a single line of model code changing.

The trade-off is **small-bird recall**. For feeder camera (close-up birds) it's almost certainly fine. For ground camera (distant birds across a yard) it might cost detections at the periphery. Worth measuring.

### Detection-stage recommendation

Don't switch models yet. Do this in order:
1. Fix Critical Bugs C1–C4 (correctness, ~30 min total).
2. Reduce capture resolution and re-measure for 24 h. Track yolo_avg, yolo_p99, total detections, missed birds via spot-check.
3. If p99 is still bad after reducing resolution, then fix ONNX Runtime config (force FP32, single thread, see [onnxruntime#17033](https://github.com/microsoft/onnxruntime/issues/17033)).
4. Only after both above are done — and only if the result is still not smooth enough — invest in YOLO11n / native CoreML.

---

## 7. Recommendations Ranked

### Tier 1 — Fix immediately (small, explanatory, no design questions)
- **R1.** Fix `_restart()` `last_frame_ms` reset (Critical C3) — **one line**, stops ground watchdog loop today.
- **R2.** Fix p99 calculation (Important I4) — one line, makes the health endpoint useful.
- **R3.** Fix `BIRDNET_DB` path (Critical C1) — one line.
- **R4.** Fix `num_frames` in track summary (Important I2) — small.

### Tier 2 — Hardware-aware optimization (the actual perf win)
- **R5.** Cut capture resolution from 1920×1080 → 720×480 or 640×360. Single config change in `bird_pipeline_v2.py:124-125`. Measure for 24 h.
- **R6.** After R5, re-evaluate whether the YOLO p99 tail still needs work. If yes, fix ONNX Runtime config (FP32, single thread).

### Tier 3 — Architecture (the smoothness fix David has been asking for)
- **R7.** Decide the audio cross-check fate: re-wire to real `notes` schema, or remove from spec entirely. Both Critical Bugs C1 and C2 collapse into this one decision.
- **R8.** Restore the two-stream architecture: visible video plays from go2rtc directly via the existing `<video>` element; SSE delivers timestamped track events; dashboard syncs labels-to-video using a fixed delay buffer (up to 60 s — David already approved this). The MJPEG path stays as a debug toggle. **This is the change that ends the "video rate = YOLO rate" coupling.**

### Tier 4 — Health and visibility
- **R9.** Per-camera classifier stats (Important I3).
- **R10.** Publish `FrameCapture.stats` through health (Important I5).
- **R11.** Implement the missing `_compute_status` degraded/broken rules (Important I6).
- **R12.** Filter skip frames out of `yolo_ms_samples` (Important I7).
- **R13.** Fix the dashboard MJPEG freshness indicator (Critical C4) — depends on R10 first.
- **R14.** Fix `dashboard/api.py:/api/pipeline/events` thread leak (Important I8).

### Tier 5 — Hygiene
- **R15.** Decide AIY confidence semantics (Important I1) — divide by 255 and tune thresholds, OR document and rename to a raw-score threshold.
- **R16.** Verify possible bug P1 (event confidence storing YOLO score). If confirmed, add `track.species_confidence`.
- **R17.** Update spec doc to reflect the three post-merge deviations: full-frame YOLO, HTTP MJPEG dashboard, AIY-on-CPU. Also document deferred dashboard features (cellular mode, scrubbing, best-visit card).
- **R18.** Delete dead code (M1–M9): about 150 lines total across `pipeline/detector.py`, `pipeline/classifier.py`, `pipeline/tracker.py`, `pipeline/hls_recorder.py`, `pipeline/debug_stream.py`, `pipeline/event_store.py`, `dashboard/index.html`.
- **R19.** Decide whether to keep the Coral lock in `SmartClassifier` (it no longer serializes anything contended now that AIY is on CPU).
- **R20.** Vectorize the Norfair distance function to silence the startup warning (low priority).

### Tier 6 — Long term
- **R21.** Once Tier 1–4 are done and the system has stabilized, *measure*. Then revisit YOLO11n / native CoreML / decimation as a model-swap conversation.
- **R22.** Remove old `bird_pipeline.py` after the spec'd 1-week rollback window.

---

## 8. Followups Tracked

| Task ID | Title |
|---:|---|
| 113 | Followup: events confidence stores YOLO not classifier score (P1, needs verification) |
| 114 | Followup: num_frames in track summary uses global counter (I2, confirmed by reviewer) |

The remaining bugs in this review have not been individually tasked. If David wants to execute the recommendations, the natural next step is to write a fix-plan that bundles Tier 1 + Tier 2 + Tier 3 into discrete tasks.

---

## 9. Architectural Audit — What We Do That Frigate Wouldn't

> **This section is the amendment that corrects the hardware framing in sections 1, 6, and 7.** The original review treated perf as a hardware limit. Frigate proves otherwise: it runs 7 cameras with smooth live tracking on a low-end Synology, meaning the problem has to be architectural. This section catalogs the architectural mistakes, ordered by impact.

### 9.0 The reframe

Three things need to happen *before* we change any model or tune any threshold:

1. **Stop decoding HD for detection.** Detection runs on a substream (~480p), display plays HD, they are independent.
2. **Stop drawing labels server-side on pixel buffers.** Labels are coordinates. The browser draws them on a transparent overlay layer on top of an unmodified HD `<video>` element.
3. **Stop decoding HD in Python at all for display.** The browser connects directly to go2rtc for MSE/WebRTC, decodes in the browser.

Once those three changes land, the pipeline's job becomes: "decode a small substream, run motion + YOLO + tracker + classifier, emit timestamped JSON events." That's dramatically less work. The hardware is fine.

### 9.1 The seven wasted-work items (all visible in our own code)

**A1. Software H.264 decode on 1080p streams** — `pipeline/frame_capture.py:82-92`
The ffmpeg spawn command has no `-hwaccel` flag. This iMac's ffmpeg build has `--enable-videotoolbox` (confirmed via `ffmpeg -hwaccels`). VideoToolbox is Apple's hardware video decode/encode framework and is the macOS equivalent of VAAPI. **We are doing software H.264 decode of 1920×1080 frames on a 4-core Intel CPU while a perfectly good hardware decoder sits idle.** Fix: add `-hwaccel videotoolbox -hwaccel_output_format nv12` to the input args. This alone will free ~30–50% of the capture ffmpeg's CPU per camera. (Note: output format changes from bgr24 → nv12, so downstream color conversion needs updating. The alternative is to let go2rtc handle the decode and have the pipeline consume a pre-decoded substream — see A7.)

**A2. 6.2 MB per-frame numpy `.copy()`** — `pipeline/frame_capture.py:121-123`
```python
arr = np.frombuffer(data, dtype=np.uint8).reshape(
    (self.height, self.width, 3)
).copy()  # copy so buffer can be reused
```
At 1920×1080×3 bytes = 6.22 MB per frame × 5 fps × 2 cameras = **62 MB/s of raw memcpy, forever**, running in the GIL. The "so buffer can be reused" comment is a safety justification, not a requirement — a fixed ring of preallocated buffers with a generation counter would give the same safety at zero allocation cost. This is also the most likely direct cause of the YOLO p100 spike: a big periodic allocation in the same Python heap that ONNX Runtime is using fragments the allocator and occasionally produces a multi-100-ms stall.

**A3. Server-side annotation and JPEG re-encode — the single biggest architectural mistake** — `pipeline/annotator.py:76-107`
On every frame we:
1. `cv2.resize(bgr, (960, 540))` — big memory copy
2. Draw label pills per track — `cv2.rectangle` + `addWeighted` blend + `cv2.putText`
3. `cv2.imencode(".jpg", ..., quality=75)` — software JPEG encode
4. Push bytes over WebSocket → proxy as MJPEG → browser renders `<img>`

**Frigate does none of this, because Frigate doesn't annotate live video at all.** This is more radical than I initially described. Frigate's live player (`web/src/components/player/LivePlayer.tsx`) is an unmodified HTML `<video>` element fed by go2rtc via `MSEPlayer` / `WebRTCPlayer`. Bounding boxes appear in exactly three places in Frigate, none of them on the live `<video>`:

1. A **debug image endpoint** (`/api/<camera>/latest.webp?bbox=1`) — Python-side draws boxes on the most recent shared-memory frame and returns a **single polled image**. It only runs when a user is looking at the debug page, and it's on-demand, not streamed. See `frigate/api/media.py`, GitHub issue #822, discussions #12570 / #15085 / #21798.
2. **Event snapshots** — one image per detection event, generated at event close time.
3. **Recorded playback** — a canvas overlay on top of `<video>` driven by `<video>.currentTime` and the tracker's per-frame lifecycle log, using `use_wallclock_as_timestamps 1` for time alignment.

**Our current MJPEG annotator is effectively "Frigate's debug view, but always on, for every viewer, at full frame rate, for both cameras."** That is the smoking gun you've been pointing at. Delete the entire path:
- `pipeline/annotator.py` (136 lines) — delete
- `pipeline/debug_stream.py` MJPEG broadcast — delete
- `dashboard/api.py` `/api/debug-stream-mjpeg/` proxy — delete
- `dashboard/index.html` `<img id="v2-mjpeg-img">` + `connectDebugStreamV2` — delete

Replace with an SSE endpoint emitting `{wall_time_ms, camera, tracks: [{track_id, bbox, species, confidence, model_source}]}` and let the browser decide how to display (section 9.3). Estimated CPU saved: 15–25% of `bird_pipeline_v2.py`'s CPU budget per camera, plus the entire uvicorn MJPEG proxy overhead.

**A4. Motion gate on full 1080p** — `pipeline/motion_gate.py`
MOG2 background subtraction is O(pixels). Running it on 1920×1080 is ~16× more work than running it on 640×360, per frame, forever. Frigate runs motion on the detect substream. Fix: same as A1/A7 — run the whole pipeline on a substream.

**A5. YOLO on full 1080p (well-known — spec says "ONNX resizes anyway")** — `pipeline/detector.py:70-82`
The current code comment defends full-frame YOLO on the grounds that "ONNX Runtime resizes everything to 640×640 anyway, so multiple small regions = multiple full-cost YOLO calls." **That is correct for region-based detection but misses the point for substream detection.** If the *input frame* is already 640×360, the resize is a no-op and the preprocessing cost drops proportionally. More importantly, the memory pressure from 1080p BGR allocations (A2) disappears. Fix: point the pipeline at a substream at ffmpeg ingest time.

**A6. Display pixels proxied through Python** — `dashboard/api.py` MJPEG endpoint + `pipeline/debug_stream.py`
The WebSocket-to-MJPEG proxy in `dashboard/api.py` exists only to re-encode the annotator's output for browser consumption. When A3 lands, the entire proxy disappears. The dashboard's `<img src="/api/debug-stream-mjpeg/feeder">` becomes `<video>` pointing at go2rtc's direct WebSocket MSE endpoint (`ws://127.0.0.1:1984/api/ws?src=feeder-main`), same as every Frigate deployment.

**A7. go2rtc exists, we aren't using its substream capability** — `go2rtc.yaml`
Current config has only `feeder-main` and `ground-main`. go2rtc can produce a downscaled substream via an `exec:` source or by chaining a transcode (`ffmpeg:feeder-main#video=h264#width=640#height=360#bitrate=512k`). For the test video loop it's a one-line change; for the real UniFi camera on ground, most UniFi Protect cameras expose a "low" substream directly. We are also not using go2rtc as the pipeline's frame source — we're spawning our own ffmpeg inside the Python process. A go2rtc substream consumed directly by the pipeline replaces `pipeline/frame_capture.py`'s ffmpeg spawn entirely.

Note: I initially said "two ffmpeg per camera is wasted decode." That's wrong. Our HLS recorder uses `-c copy` (remux, no decode), so it's nearly free. The extra RTSP session to go2rtc costs something but not much. Strike this from the worry list.

**A8. BGR24 vs YUV420p — 2× memory bandwidth for no benefit** — `pipeline/frame_capture.py:89`
We pass `-pix_fmt bgr24` to ffmpeg. That's 3 bytes/pixel. **Frigate uses `-pix_fmt yuv420p` throughout — 1.5 bytes/pixel.** For a 1920×1080 frame: 6.22 MB (BGR24) vs 3.11 MB (YUV420p). At 5 fps × 2 cameras that's 31 MB/s we're burning for nothing, because YOLO, motion gating, and the species classifier can all consume YUV or do cheaper color conversion on small crops rather than full frames. Fix: switch to `-pix_fmt yuv420p` and lazy-convert only crops that need BGR.

**A9. Capture and tracker share one Python process, GIL-contended** — `bird_pipeline_v2.py` + `pipeline/process_thread.py`
Our pipeline is one Python process running: ffmpeg readers, motion gate, detector, tracker, classifier, annotator, event store writer, SSE/debug broadcaster, HTTP health server. All threads, all fighting for the same GIL. On a 4-core CPU with ONNX Runtime holding the GIL during inference, the other threads stall. Frigate puts **capture in a separate OS process from detect/track** (not a thread), connected via `multiprocessing.Queue` carrying POSIX shared memory slot names (not frame data). The capture process writes frames directly into shm; the tracker process `get()`s a numpy view into the same shm, zero copy. Fix: lift the frame capture into a `multiprocessing.Process`, use `posix_ipc`/`multiprocessing.shared_memory` for a small ring of preallocated slots, pass slot indices via a queue. This is the real version of A2 — the ring-buffer solution is half-measures; the full fix is the process split.

**A10. Stationary-track suppression exists in spec and partially in code, but is dead** — `pipeline/detector.py:50-52, 54-68`
The `BirdDetector` constructor accepts `stationary_track_regions_fn` and stores it as `self.get_stationary`. The detector's `_is_stationary_only()` method exists. But the current `detect()` method (after the full-frame switch) never consults either — it just runs full-frame YOLO on any motion. **Frigate skips detection on stationary tracks until motion resumes nearby, which is the single biggest win for bird feeder use cases** (a perching bird produces zero YOLO cost). Our runtime data confirms this is dead: `stationary_tracks=0 ever` on both cameras. Fix: reinstate the fast-path `if all motion regions are explained by stationary tracks: return []` check before running YOLO. The wiring already exists; it just needs to be called.

**A11. Classifier runs on every track every frame** — `pipeline/process_thread.py:121-164`
We classify a track exactly once (`needs_classification` flips to False after the first successful call). That's better than every frame, but worse than Frigate, which does **vote-based debounce**: a sub-label is only assigned after `confidence ≥ 0.8` AND `≥ 3 classification attempts` AND `≥ 60% agreement on the label`, then locked. Voting kills first-frame false positives (a blurry crop misidentified on frame 1 becomes corrected by frames 2–4). We don't vote. We lock in the first confident answer.

Fix: maintain a small ring of `(species, confidence)` tuples per track. Classify on the first 3–5 frames of the track's existence. If ≥ 60% of votes agree AND max confidence ≥ 0.8, lock the winner. Otherwise keep trying up to some attempt cap. This is an accuracy fix more than a performance fix, but it's a direct response to your "tracking post classification" question and matches how Frigate actually does it.

**Summary table — estimated impact if all eleven are fixed:**

| Item | Estimated savings / gain |
|---|---|
| A1 VideoToolbox hw decode | 30–50% of each capture ffmpeg's CPU |
| A2 Preallocated frame ring (half-measure of A9) | ~10% of pipeline Python CPU, likely eliminates p100 spike |
| A3 **Delete server-side annotation** | **15–25% of pipeline Python CPU per camera, plus proxy overhead** |
| A4 Motion on substream | ~90% reduction in motion gate cost |
| A5 YOLO on substream | ~50% reduction in YOLO preprocess cost |
| A6 Delete display proxy | Eliminates uvicorn proxy CPU + 100–300ms latency |
| A7 go2rtc substream | Enables A4/A5, one-line config change |
| A8 YUV420p instead of BGR24 | ~50% decode-pipe bandwidth reduction |
| A9 **Multiprocess split + shm** | Eliminates GIL contention on YOLO inference; is the real version of A2 |
| A10 Stationary suppression | ~70–90% fewer YOLO calls on a quiet feeder scene |
| A11 Classification voting | +accuracy, not perf |

The unifying claim: **Frigate decodes 1.73 MB/s per camera. We decode 186 MB/s per camera (at 30 fps) or ~31 MB/s (at our 5 fps).** That's the architectural gap. It's 17× at our current capture rate, 108× at a full-rate comparison. No model swap touches that.

None of these require changing the species classifier. None require a new detector model. All of them are *things our code is already doing that it shouldn't be doing at all.*

### 9.2 The two-stream architecture (the thing that was in the spec before I dropped it)

```
                                  ┌────────────────────────┐
                                  │  Camera (UniFi/RTSP)   │
                                  └───────────┬────────────┘
                                              │
                                  ┌───────────▼───────────┐
                                  │        go2rtc         │
                                  │ main → publish (MSE)  │
                                  │ sub  → publish (RTSP) │
                                  └──┬────────────────┬───┘
                                     │                │
                              main   │                │   sub (640×360 @ 5fps)
                                     │                │
                 ┌───────────────────▼──┐      ┌──────▼──────────────────┐
                 │     Browser MSE      │      │  bird_pipeline_v2       │
                 │  <video> plays HD    │      │  motion → yolo → track  │
                 │     at native fps    │      │  → classify → SSE       │
                 └──────────┬───────────┘      └──────┬──────────────────┘
                            │                         │
                            │          labels         │
                            │      (JSON events)      │
                            └─────────────┬───────────┘
                                          │
                                  ┌───────▼────────┐
                                  │  Dashboard     │
                                  │  canvas draws  │
                                  │  bboxes on top │
                                  │  of <video>    │
                                  └────────────────┘
```

**Why this works:**
- The browser-to-go2rtc path is native. MSE is already how Frigate's live view works. go2rtc is specifically designed for this.
- The pipeline only decodes the small substream, which it already has compute budget for.
- Labels are just JSON events. They're cheap to transport, cheap to store, cheap to replay.
- The dashboard gets a smooth 30 fps HD video no matter how slow YOLO is, because the label overlay is *independent* of the video playback.

**Latency characteristics:**
- MSE live stream latency: ~1–3 s (baseline for an MSE buffer)
- Detection latency: ~200–500 ms (substream decode + YOLO + classify)
- Relative latency of labels vs video: ~detection latency − MSE buffer lag = *approximately zero*, or slightly video-ahead

The "labels leading video" case is what the 60 s buffer tolerance you mentioned is for — see section 9.3.

### 9.3 Sync — and Frigate's simpler answer

I originally sketched a 60-second-buffered live-sync mechanism (tag every detection with `wall_time_ms`, buffer SSE events, hold video back by a fixed `LABEL_DELAY_MS`, draw labels on a canvas at `currentTime`-derived wall time). That works, and it's still a viable option. But **the research confirmed that Frigate doesn't do anything nearly this complex — because Frigate doesn't sync labels to live video at all**. Their answer is simpler and worth considering first.

**Frigate's live-view philosophy:**
- The live `<video>` element plays the unmodified camera stream via MSE/WebRTC. No overlay. Ever.
- The "is there a bird right now?" signal comes from a separate **event/object panel** — a list of currently-active tracks with species and confidence, drawn next to the video, not on top of it. Data source is a WebSocket event stream (`/ws`).
- When the user wants boxes, they open the **debug page**, which polls `latest.webp?bbox=1` — a single server-rendered still image. Not streamed. Not live.
- When the user wants to inspect an event after it happened, they click the event and get **recorded playback** with a canvas overlay driven by the tracker's lifecycle log and `<video>.currentTime`. This is the real sync mode, and it's easy because everything is after-the-fact.

**So we have three design options:**

**Option 1 — Frigate's exact approach (simplest, fastest, no sync at all).**
- Live: `<video>` plays go2rtc stream. No overlay. Side panel shows current tracks as a text list ("Downy Woodpecker on feeder · 0.87"). Updates via SSE.
- Debug: a separate "Show boxes" toggle that polls `/api/pipeline/latest.jpg?camera=feeder&bbox=1`, which the pipeline serves from the most recent annotator frame (~1 fps, not streamed).
- Review: click an event in the event list → open a replay view with canvas overlay over the HLS recording. This is the only place sync is needed, and it's trivial because timestamps are wall-clock.
- **Pros:** zero live-sync complexity. Fastest. Matches what Frigate has proven on a potato.
- **Cons:** the casual viewer doesn't see boxes on the live video. Is that okay for the bird observatory's mission? Maybe: the mission cares about "there's a Cardinal NOW," which the text list answers perfectly. But a casual observer might expect to see a box around the bird.

**Option 2 — Buffered-replay sync (the original sketch).**
- Live: `<video>` plays HLS with a deliberate `LABEL_DELAY_MS` offset (e.g., 2000 ms). Canvas overlay is driven by SSE events buffered by `wall_time_ms`. Labels land on the right frames because both video and labels are time-aligned to wall clock.
- **Pros:** user sees boxes on live video. Matches user expectation.
- **Cons:** adds complexity. HLS live playback at a fixed offset is doable but finicky (chunk boundaries, seek, catch-up on tab refocus). Needs careful wall-clock synchronization between pipeline and dashboard.

**Option 3 — Hybrid: Frigate-style live by default, buffered replay on request.**
- Default view: Option 1 (unannotated video + text panel).
- A "Playback with overlays" toggle switches to Option 2 (HLS with 2-second delay and canvas overlay).
- Frigate has this separation too (live vs event playback), just at a coarser granularity.
- **Pros:** best of both. Default is fast and simple; power users get the annotated replay.
- **Cons:** two code paths to maintain.

My recommendation: **Option 3, built in stages.** Start by shipping Option 1 (it's a 1-day change and it unblocks the CPU). Once the system is healthy, add the Option 2 replay toggle in a second pass.

**Concretely for Option 1 (the staged first step):**
```
1. Pipeline: add SSE endpoint GET /pipeline/events/live → emits
     event: tracks
     data: {"camera": "feeder", "wall_time_ms": 1775855942046,
            "tracks": [{"track_id": 67, "species": "House Finch",
                        "confidence": 0.87, "bbox": [1720,344,1918,450],
                        "model_source": "yard"}]}
2. Dashboard: restore <video> element, src = go2rtc MSE endpoint
3. Dashboard: side panel reads the SSE, shows a text list of current tracks
4. Delete annotator/MJPEG/debug-stream entirely
```

If you later want Option 2, the SSE events already have the timestamps you need — it's a dashboard-only addition.

### 9.4 Post-classification tracking — what should happen vs what we do

**What should happen** (Frigate pattern):
- A track exists for the lifetime of an object across its entire on-screen presence, including temporary occlusion
- Classification runs **once** per track, on the highest-quality crop available in the first N frames
- Once a species is assigned, it sticks for the rest of the track's lifetime
- If the track expires and a new object appears in a similar spot, it is a **new track** with a new ID — no re-identification across expiration
- Frigate's "best" snapshot concept: the track keeps a rolling "best crop so far" (by area + sharpness score) and the classifier only needs to run on the best crop, not every frame
- Stationary objects (a bird sitting on a perch for 10 seconds) should not repeatedly burn YOLO cycles — motion gate + stationary-track suppression handles this

**What Frigate actually does** (specific, from the research subagent):
- **Vote-based sub-label assignment**: a track is only given a species when `max_confidence ≥ 0.8` AND `≥ 3 classification attempts` AND `≥ 60% of votes agree on the same class label`. Debounce ring of votes per track. This kills single-frame false positives without losing real IDs.
- Once assigned, the sub-label is **locked for the remainder of the track's lifetime** — occlusion, temporary re-detection gaps, Kalman coasting, all fine.
- **Stationary tracks stop being re-detected** — once the bbox stops moving, Frigate marks the track stationary and **skips YOLO for that region** until motion resumes nearby. This is the single biggest per-frame CPU saver for a feeder scene.
- **Top-score tracking**: the track carries a `top_score` which is the highest **median score across a rolling window**, not a single-frame max. Noise-resistant. Used for "best snapshot."
- **No cross-expiration re-identification**: if the bird flies away and comes back, it's a new track with a new ID. Frigate doesn't try to solve the hard re-ID problem. Neither should we.

**What we do** (`pipeline/process_thread.py:121-164`, `pipeline/classifier.py`):
- `track.needs_classification` starts True, set to False after the first successful classification
- Classification is attempted up to `MAX_CLASSIFICATION_ATTEMPTS=3` times, but **there is no voting** — the first successful call wins
- Once classified, `track.species` sticks for the track's lifetime ✓
- **No "best crop" concept** — the classifier runs on whatever crop is available on the frame where `needs_classification=True` happens to flip through the loop. That's usually the first frame the track exists, which is often the worst crop (bird just entered the scene, motion-blurred, partial)
- **The tracker's `stationary_regions` machinery exists** (spec'd in § tracker) and the motion gate / detector even accepts a `stationary_track_regions_fn` callback — but after the full-frame YOLO switch, the callback is **ignored** (bug M3 / dead code in §4). Stationary suppression is silently disabled. Runtime data confirms it: `stationary_tracks=0 ever` on both cameras.
- Post-classification re-entry is impossible: track expiration destroys the identity. Frigate handles this the same way — it's not a gap.

**The three concrete fixes, in priority order:**

- **(i) Vote-based classification** (accuracy win, biggest): maintain a ring of `(species, confidence)` tuples per track; classify up to 5 attempts across the first few frames; lock the species only when `max_conf ≥ 0.6` AND `≥ 3 votes` AND `≥ 60% agreement`. Matches Frigate's debounce. Direct answer to "tracking post classification."
- **(ii) Reinstate stationary suppression** (CPU win, biggest per-frame): add a fast-path check at the top of `BirdDetector.detect()` — if all motion regions are explained by stationary-track bboxes (IoU > 0.8), return `[]`. The `get_stationary` callback is already wired into the constructor. ~10 lines of code. This is also item A10 from §9.1.
- **(iii) Best-crop selection for classification**: rank crops by `bbox_area × sharpness_score` (Laplacian variance is cheap), only classify when a meaningfully better crop arrives. Combined with (i), this means the species ID is voted from the best 3–5 crops of a bird's visit, not from whatever frame happened to fire first.

Also touched by Bug P1 (§4): `track.confidence` is mutated every frame by the YOLO bbox score, so a separate `track.species_confidence` field is needed to surface "how sure are we about species?" in the UI or event store. That fix pairs naturally with fix (i) — the voting ring can store the per-vote confidence and expose the winning vote's confidence as `species_confidence`.

### 9.5 YOLO p100 spike — root cause (revised)

The original review said "the p99 calc is really p100 and the spike is probably hardware swap thrashing." The p99-calc-is-wrong part is still true (Bug I4). The swap-thrashing explanation is wrong. **The real cause is item A2: the 6.2 MB numpy allocation + copy per frame fragments the allocator, and periodically the allocator has to spend a very long time (hundreds of ms) finding a contiguous chunk.** This is exactly the kind of tail latency you'd expect from a Python process doing large buffered allocations in a tight loop while also running ONNX Runtime inferences.

**Fix:** Switch to pre-allocated frame buffers (ring of 4 × `height × width × 3` arrays), have the ffmpeg reader write directly into the next free buffer instead of `frombuffer().copy()`. The generation counter guarantees safety. This is a few dozen lines of code in `frame_capture.py` and should eliminate the p100 spike without touching YOLO, CoreML, or the model.

### 9.6 Amended recommendations (this supersedes § 7)

**The three moves that would fix ~80% of it** (if you only do three things):

1. **Delete the server-side annotator path and put `<video>` back on the dashboard** (R-A3 below). This is the architectural reset. A 1-day change that eliminates the biggest unforced error in the pipeline.
2. **Detect on a 640×360 @ 5 fps substream, not 1080p main** (R-A4 + R-A7). A ~2-hour config change that unlocks most of the remaining perf headroom.
3. **Add vote-based classification + reinstate stationary suppression** (R-A6 + R-A10). A ~6-hour change that is the single biggest accuracy improvement plus a massive per-frame CPU saver on quiet feeder scenes.

Everything else is detail after those three. Full ranked list:

**Tier 0 — architectural mistakes (do first):**
- **R-A3. Delete server-side annotator, restore `<video>`, emit SSE events.** ~1 day. Option 1 from §9.3 (Frigate-style: no live overlay, side panel shows tracks as text). Option 2 (buffered replay with overlay) can come later as a toggle.
- **R-A4. Configure go2rtc substream(s)**: add `feeder-sub`/`ground-sub` at 640×360 @ 5 fps. For the test video loop this is a one-liner in `go2rtc.yaml`. For the real UniFi camera on ground, use its low-quality RTSP alias. ~1 hour.
- **R-A5. Point pipeline at the substream**: change `bird_pipeline_v2.py:124-125` `width=1920, height=1080` → `width=640, height=360`, and change the rtsp URLs to the sub variants. ~30 min.
- **R-A6. Vote-based classification + best-crop selection** (matches Frigate's `≥0.8 conf`, `≥3 attempts`, `≥60% agree`). ~6 hours.
- **R-A10. Reinstate stationary suppression** (fast-path skip before YOLO when all motion regions are explained by stationary tracks). The wiring already exists, it just needs to be reconnected. ~2 hours.

**Tier 1 — critical correctness bugs from original review (still valid):**
- R1 fix `_restart()` `last_frame_ms` reset (Critical C3) — stops ground watchdog loop (one-line fix, do today)
- R2 fix p99 calculation (Important I4) — makes health endpoint truthful (one-line fix)
- R3 fix `BIRDNET_DB` path (Critical C1) (one-line fix) — but note R-A8 below: the audio cross-check may be moot after Frigate-style voting
- R4 fix `num_frames` in track summary (Important I2)

**Tier 2 — follow-on architectural fixes:**
- **R-A1. Add VideoToolbox hw decode** to the ffmpeg capture command. Meaningful CPU win but only after R-A4 lands (hw decode of a 640×360 stream is less impactful than hw decode of 1080p; the substream switch is the bigger lever). ~1 hour.
- **R-A2. Preallocated frame ring buffer** to eliminate per-frame `.copy()`. ~2 hours. Likely fixes the p100 spike. If you go straight to the full multiprocess/shm approach (R-A9) you can skip this.
- **R-A8. YUV420p instead of BGR24** in the ffmpeg pipe. ~1 hour + downstream color conversion changes. Cuts decode-pipe bandwidth in half.
- **R-A9. Full multiprocess split with POSIX shared memory** (Frigate's actual architecture). Significant refactor (~1 week) but is the proper version of A2 and unlocks multi-core usage. Do only if A1–A8 don't get us where we need to be.

**Tier 3 — answers to the "tracking post classification" question:**
- R-A6 (above) is the main answer. Also:
- Bug P1 fix: add `track.species_confidence` as a separate field from `track.confidence` (which is the YOLO bbox score). Pairs naturally with R-A6's voting ring.

**Tier 4 — audio cross-check decision:**
- **R-A11. Decide the fate of Path 4 (audio cross-check).** If R-A6 lands and voting gives us enough accuracy, the audio cross-check becomes optional. If we keep it, fix C1 (BIRDNET_DB path) AND C2 (SQL schema against `notes` table) AND integrate it properly. If we drop it, delete Path 4 from classifier.py and update the spec.

**Tier 5 — everything else from the original §7** (AIY confidence semantics, dead code deletion, spec doc updates, Coral lock decision, etc.), applied in its existing order.

### 9.7 What I missed and why

Writing this section honestly: my original review failed in a specific way. I treated the review as a *bug-finding exercise* (what's broken, what's dead code, what's wrong line-by-line) when David asked for a *state-of-the-system review with architectural assessment*. When David asked me to "weigh out the option of using a nano yolo or a different initial stage," I interpreted that as a model-selection question and recommended moving to a smaller model or tuning ONNX flags. I never asked the more fundamental question: **"what is this pipeline doing that it shouldn't be doing at all?"**

The telltale sign I should have caught: the spec literally says "decode-once, label-on-separate-path," the original architecture documents called for WebSocket + canvas overlay, and the post-merge fix commits silently abandoned all of it in favor of MJPEG. That's not a minor optimization — that's rolling the entire design back to a server-side-pixel-renderer architecture, which is exactly the wrong trade-off. I should have flagged that as the review's top finding. Instead I catalogued it under "spec deviation" in §3 and moved on. That was wrong.

The hardware-doom framing (original §1) was a secondary failure caused by the same blind spot. I saw a swap-thrashing machine with high load average, correctly noted the numbers, and incorrectly concluded that the hardware was the constraint. The correct conclusion was "the hardware is under stress *because* the pipeline is doing work it shouldn't be doing at all." A machine running Frigate with 7 cameras and no thrashing would have answered that question. I didn't even think to check.

**This is the kind of miss that the review process is supposed to catch.** I am correcting it in this amendment, but the miss itself is worth naming.

### 9.8 Source citations for § 9 claims

All specific Frigate claims in this section are sourced from a focused research pass done after the original review. Key references:

**Frigate architecture:**
- Frigate's role system (`detect` vs `record` with `-c copy` remux): https://docs.frigate.video/configuration/cameras/
- ffmpeg presets (`preset-vaapi`, `preset-intel-qsv-h264`, `preset-nvidia`): https://docs.frigate.video/configuration/ffmpeg_presets/ and https://github.com/blakeblackshear/frigate/blob/dev/frigate/ffmpeg_presets.py
- go2rtc restream pattern: https://docs.frigate.video/configuration/restream/ and https://docs.frigate.video/guides/configuring_go2rtc/

**Live view does not annotate:**
- LivePlayer.tsx / MSEPlayer / WebRTCPlayer / JSMpegPlayer code structure: https://deepwiki.com/blakeblackshear/frigate/5.2-live-camera-streaming
- "Live view has no boxes": https://github.com/blakeblackshear/frigate/issues/822, https://github.com/blakeblackshear/frigate/discussions/12570, https://github.com/blakeblackshear/frigate/discussions/15085
- `latest.webp?bbox=1` debug endpoint (on-demand still): `frigate/api/media.py`

**Zero-copy frame path:**
- `SharedMemoryFrameManager` + ring buffer + multiprocess queue: https://deepwiki.com/blakeblackshear/frigate/4.2-frame-processing-and-shared-memory
- Capture-to-tracker process split: https://deepwiki.com/blakeblackshear/frigate/4.1-camera-capture-and-ffmpeg-integration

**Tracking and classification:**
- Norfair-based tracker with scale-invariant distance, Kalman coasting, median-score confirmation: Frigate's `frigate/track/norfair_tracker.py`, https://github.com/tryolabs/norfair
- Sub-label debounce rule (`≥0.8 conf`, `≥3 attempts`, `≥60% agree`, then lock): https://docs.frigate.video/configuration/custom_classification/object_classification/
- Stationary objects stop being re-detected: https://docs.frigate.video/configuration/stationary_objects/

**Substream sizing and fps:**
- 640×360 @ 5 fps canonical recommendation: https://docs.frigate.video/frigate/camera_setup/
- Community consensus: https://github.com/blakeblackshear/frigate/issues/6039, https://github.com/blakeblackshear/frigate/discussions/5984

**Bandwidth comparison (our pipeline vs Frigate):**
- 1920×1080 BGR24 @ 30 fps = 186 MB/s per camera through the decode pipe
- 640×360 YUV420p @ 5 fps = 1.73 MB/s per camera through the decode pipe
- Ratio: **108× reduction** (or 17× if we stay at our current 5 fps but keep 1080p BGR24)
- Source: the research pass did the math directly from the pix_fmt bytes-per-pixel constants

---

## Appendix A — Subagent Reports

Three specialist subagents contributed to this review:

1. **Code reviewer** (`superpowers:code-reviewer`) — full spec compliance + code quality review of `pipeline/`, `bird_pipeline_v2.py`, and the v2 dashboard integration. Found Critical Bugs C1, C2, C4, all 8 important bugs, and most of the dead code list. Identified the AIY uint8/100 issue and the p99 calculation bug, both of which I had not anticipated. **Output: ~3500 words, returned in this conversation.**
2. **Detection-stage alternatives researcher** — weighed 9 options against current YOLOv8n+ONNX. Caught the false premise in my brief that `VNRecognizeAnimalsRequest` could detect birds (it cannot — cats and dogs only). Recommended native CoreML export, which I subsequently demoted after confirming the host is Intel, not Apple Silicon. **Output: ~2500 words.**
3. **Runtime anomaly investigator** (`Explore` subagent) — root-caused the AIY=0 silence (yard 14-class softmax peaks above 0.60) and confirmed the watchdog `_restart()` bug independently. Suggested both option A (lower CONFIDENT threshold) and option B (verify yard distribution) for the AIY question. **Output: ~1000 words.**

A fourth long-running background research agent ("Research Frigate smooth detection") was launched earlier in the session but its output was not needed for this review.

---

## Appendix B — Methodology Notes

- **Read-only review.** No code was modified during this review.
- **Tests were run once** to confirm baseline (50/50 passing).
- **Pipeline was running throughout** the review at PID 93573 (3.25 h uptime at start, ~4 h by completion). All runtime data is from a live healthy-but-degraded system, not a synthetic environment.
- **Hardware was confirmed via** `system_profiler SPHardwareDataType` and `top -l 1`. The Intel finding was a late-stage discovery that significantly reframed the alternatives analysis.
- **Subagents were dispatched in parallel** (one code-reviewer, one general-purpose researcher, one Explore investigator) to keep the controller's context clean and to parallelize wall-clock time.
- **DB inspection** was via direct `sqlite3` queries against `~/bird-snapshots/logs/pipeline.db`.
- **The prior conversation context was compacted mid-session**, which contributed to early-session disorientation about whether v2 was merged or still in worktree (it was merged, with 5 fix commits on top). This review was the recovery action.
