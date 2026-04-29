> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Live Detection Pipeline v3 — Floating-Label Design

**Date:** 2026-04-11
**Status:** Approved for implementation (brainstorming complete, section-by-section review skipped per David's direction; David unavailable during implementation)
**Author:** Claude (Opus 4.6) + David
**Supersedes:** [`2026-04-10-live-detection-v2-design.md`](./2026-04-10-live-detection-v2-design.md)

---

## 0. Why v3

The v2 pipeline shipped April 10 but diverged from its own spec in three load-bearing ways:

1. **Abandoned the two-stream architecture.** The v2 spec called for an unannotated smooth HD live stream plus a separate "debug view" with labels. Post-merge fixes replaced this with a single server-side-annotated MJPEG stream, which couples the visible video frame rate to the YOLO inference rate — on a feeder camera with 86 ms YOLO avg and a 1760 ms p100 tail, that produced a chopped, laggy live view.
2. **No voting.** v2 locked each track's species on the first confident classifier call. A single bad first-frame crop (motion blur, partial bird) permanently mislabels the track for its lifetime.
3. **Stationary suppression is dead.** The tracker tracks stationary regions, the detector accepts a `stationary_track_regions_fn` callback, but the full-frame YOLO switch severed the wiring. A bird perched for 10 seconds burns 50 YOLO calls for zero new information.

Plus multiple critical correctness bugs catalogued in [`docs/superpowers/reviews/2026-04-10-live-detection-v2-review.md`](../reviews/2026-04-10-live-detection-v2-review.md).

v3 is the Frigate-inspired design the v2 spec *tried* to be, plus David's specific "floating label at y=25% tracking x" aesthetic and a first-class **honesty contract** for every exposed metric.

## 1. Goal and Non-Goals

### Goal

Ship a live detection pipeline where the feeder cam dashboard view feels like watching a quiet nature camera with intelligent labels floating above the birds, and where the system's own self-report is honest:

- Smooth HD video, ≥ 25 fps sustained in the browser
- One species label per tracked bird, positioned at `y = 0.25 × video_height`, gliding with the bird's horizontal position
- Classification accurate enough to trust. Phase 1 uses first-confident-wins (same as v2); Phase 2 upgrades to vote-based confirmation (≥ 0.8 conf, ≥ 3 attempts, ≥ 60% agreement). The Phase 1 prototype is shippable without voting.
- Every metric exposed via `/health` is an accurate representation of actual system state, not a proxy that happens to look green
- Mission alignment: "If it says Cardinal, there better be a Cardinal."

### Non-Goals (explicit scope boundaries)

- **Polished ground-camera dashboard UX.** Ground cam runs on the same v3 pipeline (unified code path) and produces events into the DB, but its dashboard view is not a design focus. Collision-heavy flock handling, scene-tuned motion parameters — deferred.
- **Audio-visual cross-check (Smart B Path 4).** Dropped in favor of vote-based classification; saved as a forget-me-not with specific re-entry triggers.
- **VideoToolbox hardware decode, YUV420p pixel format, POSIX shared memory, multi-process split.** All real optimizations, but the Phase 1/2 plan meets its goals without them. Revisit only if empirical metrics justify.
- **Cross-expiration bird re-identification.** A bird that leaves the frame and returns is a new track with a new ID. Frigate doesn't solve this either.
- **Batch classifier changes.** `classify.py` continues processing keeper frames from `incoming/` unchanged.
- **Old `live_detector.py` and `bird_pipeline.py` (first unified pipeline).** v3 replaces both. The dashboard "Old Det / New Det" toggle is deleted. There is one live detection pipeline, and it is v3.

## 2. Architecture

### The two-stream story

```
              ┌──────────────────────────────┐
              │   Camera source (RTSP)       │
              │  (UniFi in prod, video loop  │
              │   in test)                   │
              └──────────────┬───────────────┘
                             │ (single RTSP pull into go2rtc)
                             ▼
                    ┌──────────────────┐
                    │     go2rtc       │
                    │  main + sub      │
                    │  (transcode sub  │
                    │   to 640×360@5)  │
                    └──┬────────────┬──┘
                       │            │
             main HD   │            │ sub 640×360@5
             via MSE/WS│            │ raw BGR via ffmpeg
                       │            │
                       ▼            ▼
              ┌─────────────┐  ┌───────────────────────┐
              │   Browser   │  │   bird_pipeline_v3    │
              │   <video>   │  │  motion → detect →    │
              │ plays HD    │  │  track → classify →   │
              │ at native   │  │  emit SSE events      │
              │ fps         │  └──────────┬────────────┘
              └──────┬──────┘             │
                     │                    │
                     │   JSON events      │
                     │   via SSE          │
                     └──────────┬─────────┘
                                │
                          ┌─────▼─────────┐
                          │   Dashboard   │
                          │  interpolate  │
                          │  + draw       │
                          │  floating     │
                          │  labels on    │
                          │  <canvas>     │
                          │  overlay      │
                          └───────────────┘
```

The main HD stream is **never decoded in Python**. The browser pulls it directly from go2rtc via MSE/WebSocket. The pipeline decodes only the substream. Labels are JSON events, drawn client-side on a canvas positioned over the video.

### Single-pipeline, per-camera-config

v3 is one Python process (`bird_pipeline_v3.py`) running one `CameraProcessThread` per camera, but each camera has its own:

- **Classifier configuration.** Feeder: `use_yard=True` → yard → AIY fallback. Ground: `use_yard=False` → AIY only.
- **Stats slot.** No shared global counters.
- **Event stream.** SSE events are tagged with `camera`, and the dashboard subscribes to the camera it's showing.

### Data flow per frame

```
1. ffmpeg subprocess drains 640×360 BGR frames from go2rtc sub stream (pipeline/frame_capture.py)
2. Motion gate (MOG2 on substream) emits motion regions (pipeline/motion_gate.py)
3. If all motion regions are explained by stationary tracks → skip (Phase 2)
4. Detector runs YOLO on the full substream frame when any motion is present (pipeline/detector.py)
5. Tracker updates with new detections (pipeline/tracker.py, Norfair scalar distance)
6. For each active track, classifier runs per per-camera config:
   - Phase 1: first confident classification wins (v2 behavior preserved for Phase 1)
   - Phase 2: vote-based lock (≥ 0.8 conf, ≥ 3 attempts, ≥ 60% agreement)
7. SSE broadcaster emits an event per frame that has active tracks:
   {camera, wall_time_ms, tracks: [{track_id, bbox, species, species_confidence,
                                    model_source, is_locked}]}
8. Event store writes per-frame events + per-track summary (on expiration)
9. Health state updates per camera from *this thread's* counters
```

No annotation step. No JPEG encode. No MJPEG push. No server-side pixel work on display frames.

## 3. Components

| Module | Responsibility | Phase |
|---|---|---|
| `pipeline/frame_capture.py` | ffmpeg subprocess per camera, pipe drain → bounded queue, watchdog with correct `last_frame_ms` reset on restart | 1 |
| `pipeline/motion_gate.py` | MOG2 on substream, emit motion regions (existing module, possibly minor tuning) | 1 |
| `pipeline/detector.py` | Full-substream YOLO when motion; stationary-suppression fast-path before YOLO | Phase 1: YOLO. Phase 2: stationary skip |
| `pipeline/tracker.py` | Norfair tracker with Frigate-inspired distance; per-track `frame_count`, `species_confidence`, `vote_history` | 1 (frame_count, species_confidence). 2 (vote_history) |
| `pipeline/classifier.py` | `SmartClassifier` with `camera_configs` dict; per-camera decision tree; per-camera stats | 1 (config + stats). Voting lives in process thread (see below). |
| `pipeline/vote_locker.py` **NEW** | Per-track voting ring, lock logic (conf ≥ 0.8, ≥ 3 attempts, ≥ 60% agreement) | 2 |
| `pipeline/sse_events.py` **NEW** | Frame-level SSE event emitter; HTTP server alongside health on port 8100 | 1 |
| `pipeline/event_store.py` | SQLite event + track summary writes; **track-level num_frames fix** | 1 |
| `pipeline/health.py` | Honest metrics per camera; honesty contract enforced via tests | 1 |
| `pipeline/process_thread.py` | Orchestrator: capture → motion → detect → track → classify → emit → record | 1 |
| `pipeline/hls_recorder.py` | Unchanged from v2; still `-c copy` remux | 1 |
| `bird_pipeline_v3.py` | Main orchestrator; instantiates per-camera stacks with per-camera configs | 1 |
| `dashboard/api.py` | Add `/api/pipeline/events/sse` proxy; delete MJPEG proxy | 1 |
| `dashboard/index.html` | Replace `<img>` MJPEG with `<video>` + `<canvas>` overlay; client-side label renderer + interpolator | 1 |
| `go2rtc.yaml` | Add `feeder-sub`, `ground-sub` transcoded streams | 1 |

**Deleted in v3:**

- `pipeline/annotator.py` (136 lines) — server-side labeling
- `pipeline/debug_stream.py` MJPEG broadcaster portion
- `dashboard/api.py` `/api/debug-stream*` proxies
- `dashboard/index.html` `connectDebugStreamV2`, `_v2Active`, `v2-mjpeg-img`, Old Det / New Det toggle, `connectPipelineSSE` dead code

## 4. Phase 1 — Foundation (the "fully working prototype" David sees)

**What Phase 1 delivers:**

A working Frigate-style live view for the feeder cam on the dashboard, with floating labels that track birds smoothly, running on the v3 pipeline against the test video loop.

### Phase 1 scope

1. **go2rtc substream configuration.** Add transcoded `feeder-sub` and `ground-sub` at 640×360 @ 5 fps to `go2rtc.yaml`. Concrete YAML (added alongside existing `feeder-main` / `ground-main`):

   ```yaml
   streams:
     feeder-main:
       - rtsp://127.0.0.1:8554/706907355fbd92f7cb5ec28f1ac605e9   # existing test loop
     feeder-sub:
       - "ffmpeg:feeder-main#video=h264#width=640#height=360#hardware"
     ground-main:
       - rtsp://192.168.4.9:7447/RTSnv0lLeUd8cJDw#tcp              # existing real camera
     ground-sub:
       - "ffmpeg:ground-main#video=h264#width=640#height=360#hardware"
   ```

   The `#hardware` flag lets go2rtc pick VideoToolbox on macOS automatically if available. If the transcode is too expensive, fall back to `#video=h264#width=640#height=360` (software), or drop the `height` to let ffmpeg maintain aspect ratio from the source.
2. **Pipeline source switch.** `bird_pipeline_v3.py` captures from `feeder-sub` / `ground-sub` instead of `feeder-main` / `ground-main`. `FrameCapture` width/height change to 640×360.
3. **Critical v2 bug fixes rolled into v3 from day one:**
   - `FrameCapture._restart()` resets `self.stats["last_frame_ms"] = time.time() * 1000`
   - `CameraProcessThread._update_health()` computes p99 correctly via `np.percentile(samples, 99)`
   - `yolo_ms_samples` only records frames where YOLO actually ran (`len(detections) > 0` OR `forced_full`)
   - `write_track_summary` takes per-track frame count from `track.frame_count`, not the global process counter
   - Delete `BIRDNET_DB` constant and `_audio_lookup` entirely (Path 4 is gone)
4. **Per-camera classifier config.** `SmartClassifier` takes `camera_configs: dict[str, CameraClassifierConfig]` in its constructor. Feeder: `use_yard=True`. Ground: `use_yard=False`. The decision tree branches on `camera` arg passed into `classify()`. Classifier stats dict is keyed by camera: `{feeder: {...}, ground: {...}}`.
5. **Delete `pipeline/annotator.py` and MJPEG path.** Remove imports, constructor wiring, the annotator thread, and all downstream MJPEG broadcast code. Update tests accordingly.
6. **New SSE endpoint (`pipeline/sse_events.py`).** Alongside the existing health HTTP server on port 8100, add:
   - `GET /events/sse` → SSE stream of per-frame track events
   - Event shape:
     ```json
     {
       "camera": "feeder",
       "wall_time_ms": 1775855942046,
       "tracks": [
         {
           "track_id": 67,
           "bbox": [1720, 344, 1918, 450],
           "bbox_center_x": 1819,
           "frame_width": 640,
           "frame_height": 360,
           "species": "House Finch",
           "species_confidence": 0.87,
           "model_source": "yard",
           "is_locked": false,
           "frame_count": 14
         }
       ]
     }
     ```
   - `frame_width` and `frame_height` are the substream dimensions so the client can scale coordinates to whatever the video element's display size is.
   - `bbox_center_x` is pre-computed for client convenience (same as `(bbox[0] + bbox[2]) / 2`).
   - `species` can be `null` if no species is assigned yet (tiny-dot state).
   - `is_locked` is `false` in Phase 1 (voting lives in Phase 2). In Phase 1 it's `true` the moment a species is set (first-confident-wins, same as v2).
7. **Dashboard client changes (`dashboard/index.html`):**
   - Delete `<img id="v2-mjpeg-img">`, restore `<video>` element, src = go2rtc WebSocket MSE endpoint
   - Delete `connectDebugStreamV2`, `_v2Active`, `_v2LastFrameMs`, the MJPEG-specific paths
   - Delete the Old Det / New Det toggle completely; there is one live detection pipeline
   - Add a transparent `<canvas id="label-overlay">` positioned directly over the video with CSS `position: absolute`
   - New `LabelRenderer` JS class (client-side):
     - Subscribes to `EventSource('/api/pipeline/events/sse?camera=feeder')`
     - Maintains `trackStates: Map<track_id, {last_t, last_x, prev_t, prev_x, species, fade_in_t}>`
     - On event: update the track state ring for each track in the event
     - On `requestAnimationFrame`: for each active track, compute interpolated `render_x` from last two known positions; render either a 4 px dot (no species yet) or a pill-shaped label (species locked) at `(render_x, 0.25 * canvas_height)`
     - Collision pass: if two labels overlap at base y, bump the lower-priority one down by `label_height + 8 px`. Priority = larger bbox area.
     - Edge clamp: `label_x = clamp(render_x, label_width/2 + 8, canvas_width - label_width/2 - 8)`
   - Interpolation math for the render loop:
     - `elapsed = now_ms - last_t`
     - `delta_t = last_t - prev_t`
     - `delta_x = last_x - prev_x`
     - If `delta_t > 0` and `elapsed < 500`: `render_x = last_x + delta_x * (elapsed / delta_t)` (extrapolation)
     - Else: `render_x = last_x` (fall back)
     - When a new event arrives: if `|new_x - render_x| > 10`, ease toward `new_x` over 150 ms rather than snap
   - Fade in for new tracks: start at 0 opacity, ease to 1.0 over 200 ms
   - Fade out when a track is expired (not seen in 2 s): ease to 0 over 300 ms, then remove from map
   - Track expiration timer: remove track state if no event has been received for that `track_id` in 3 s
8. **Dashboard API (`dashboard/api.py`):**
   - Add `/api/pipeline/events/sse` endpoint that proxies `http://127.0.0.1:8100/events/sse`
   - Delete `/api/debug-stream/*`, `/api/debug-stream-mjpeg/*`
9. **Honesty contract for Phase 1 metrics** (see Section 6).

### Phase 1 success criteria

All must hold before Phase 1 is considered "done":

- **Video:** `<video>` plays the go2rtc main stream at ≥ 25 fps steady in a real browser (Playwright-verified), no stalls ≥ 500 ms during a 60 s window
- **Labels:** Observed via Playwright screenshot, floating species labels appear at y=25% of video height, track bird x smoothly, fade in from dot to species, fade out when bird leaves
- **Pipeline runs:** 30+ minutes sustained against the test video loop with no error spam in the log
- **Metrics honest:** Every metric in the honesty contract passes its failure-injection test
- **No regression:** Detection count per 5-minute window within ±10% of v2 baseline on the same test loop
- **Tests:** All unit + integration + honesty-contract tests passing
- **Ground cam:** v3 pipeline processes ground cam frames without crashing or holding up feeder processing (no UX polish requirement)

## 5. Phase 2 — Accuracy Refinement

### Phase 2 scope

1. **Vote-based classification (`pipeline/vote_locker.py`).**
   - `Track` gains `vote_history: list[(species, confidence)]` and `species_confidence: float` (separate from `confidence` which is YOLO bbox score)
   - `process_thread._classify_tracks()` no longer locks species on first confident call
   - Instead, each classification call appends to `vote_history`, then checks lock condition:
     - `max_confidence in vote_history >= 0.8` AND
     - `len(vote_history) >= 3` AND
     - `>= 60% of votes agree on the top species`
   - When lock condition is met, `track.species` is set, `track.is_locked = True`, `track.species_confidence` = the winning vote's confidence, classification stops for this track.
   - If the track expires before lock, the track's best-so-far top vote becomes its final species (still stored, `is_locked = False`)
   - Max attempts cap stays at 5 (bumped from 3 since voting needs more samples)
2. **Stationary suppression revival.**
   - `BirdDetector.detect()` gets a fast-path at the top: if `self.get_stationary` callback returns any regions, and all `motion_regions` are explained by those stationary regions (IoU > 0.8), return `[]` without running YOLO.
   - `BirdTracker.stationary_regions()` already exists; verify it's producing correct output for tracks that have not moved > 10 px in the last 10 frames.
   - Phase 2 adds a stationary-cycle test that puts a fake stationary track in the tracker and asserts YOLO is not called for N frames.
3. **Best-crop classification.**
   - Each track accumulates a rolling "best crop" scored by `bbox_area * laplacian_variance(crop)`
   - Classification runs on the current best crop, not the first available
   - When a meaningfully better crop arrives (score > `1.2 × current_best_score`), re-classify and add the new result to the voting history
4. **Event store schema additions.** The `pipeline_events` table gains two new nullable columns — `species_confidence REAL` and `bbox_confidence REAL`. Phase 1 already writes to `bbox_confidence` (clearly labeled as the YOLO bbox score) and leaves `species_confidence` NULL. Phase 2 populates `species_confidence` with the winning vote's confidence. The legacy `confidence` column is kept for backward compatibility but marked deprecated in the schema comment; queries should prefer the two new columns. (Phase 2 migration: add columns via `ALTER TABLE pipeline_events ADD COLUMN species_confidence REAL; ALTER TABLE pipeline_events ADD COLUMN bbox_confidence REAL;` at startup, wrapped in a try/except for column-exists.)
5. **Expanded honesty contract** for Phase 2 metrics (vote agreement rate, stationary-skip rate, best-crop improvement count).

### Phase 2 success criteria

- **Voting works:** Confidence histogram on classified tracks shows median agreement ≥ 80% on tracks where voting triggered a lock
- **Stationary suppression fires:** On a test scene with a stationary bird, YOLO call count drops by ≥ 50% compared to Phase 1 baseline
- **Unlabeled rate drops:** Proportion of tracks that reach end-of-life without any species assignment drops from ~30% (v2 baseline) to ≤ 10%
- **No Phase 1 regression:** Phase 1 success criteria still hold
- **Phase 2 honesty contract tests pass**

## 6. Honesty Contract

> **Principle:** Every metric exposed by `/health` must measure what it claims to measure. A metric that looks green while the underlying feature is broken is a bug, not a metric. For each metric, we define: what it measures (the exact code path that updates it), what values indicate each health state, and a test that fabricates a broken state and verifies the metric responds correctly.

### Metrics inventory (Phase 1)

| Metric path | What it measures | Code path | Healthy | Degraded | Broken |
|---|---|---|---|---|---|
| `pipeline.<camera>.capture.frames_processed` | Count of frames successfully read from ffmpeg pipe | `FrameCapture._pipe_drain()` increments on every successful `put_nowait()` | > 0 and increasing | stalled < 10 s | stalled > 10 s |
| `pipeline.<camera>.capture.last_frame_age_ms` | Milliseconds since last frame was read from ffmpeg | `(time.time()*1000) - self.stats["last_frame_ms"]` | < 1000 | 1000–10000 | > 10000 (daytime) |
| `pipeline.<camera>.capture.dropped_oldest` | Count of queue-full drops | `FrameCapture._pipe_drain()` increments when queue is full | < 1% of frames_processed | 1–5% | > 5% |
| `pipeline.<camera>.capture.ffmpeg_restarts` | Count of watchdog-triggered ffmpeg restarts | `FrameCapture._restart()` increments | < 3 in last hour | 3–10 in last hour | > 10 in last hour |
| `pipeline.<camera>.detector.yolo_ms_avg` | Average YOLO inference time, **only for frames where YOLO actually ran** | `process_thread._update_health()`: `mean(self._yolo_samples_real)` | < 200 ms | 200–500 ms | > 500 ms |
| `pipeline.<camera>.detector.yolo_ms_p99` | True 99th percentile over last 100 samples where YOLO ran | `np.percentile(self._yolo_samples_real, 99)` | < 400 ms | 400–1000 ms | > 1000 ms |
| `pipeline.<camera>.detector.yolo_skipped_motion` | Count of frames skipped because no motion | `process_thread._process_frame()` increments when `motion_regions == []` | — | — | — (info only) |
| `pipeline.<camera>.detector.detections_total` | Count of detections returned by YOLO | sum of `len(detections)` | — | — | — (info only) |
| `pipeline.<camera>.tracker.active_tracks` | Count of currently-active tracks | `len(tracker.tracks)` at sample time | — | — | — (info only) |
| `pipeline.<camera>.classifier.yard` | Count of classifications resolved by yard model (Path 1) | `SmartClassifier.stats[camera]["yard"]` increments in Path 1 | — | — | — (info only, feeder only) |
| `pipeline.<camera>.classifier.aiy` | Count of classifications resolved by AIY | `SmartClassifier.stats[camera]["aiy"]` increments in Path 2 | — | — | — (info only) |
| `pipeline.<camera>.classifier.unlabeled_call` | Count of classifier calls that returned None species | `SmartClassifier.stats[camera]["unlabeled_call"]` | — | — | — (info only) |
| `pipeline.<camera>.classifier.lock_timeouts` | Count of Coral lock acquisition timeouts (feeder only, since only yard uses Coral) | `SmartClassifier.stats[camera]["lock_timeouts"]` | 0 | 1–5 per hour | > 5 per hour |
| `pipeline.<camera>.events_emitted` | Count of SSE events sent | `sse_events.py` increments | — | — | — (info only) |
| `pipeline.<camera>.sse_clients` | Current count of connected SSE clients | `sse_events.py` tracks connections | — | — | — (info only) |
| `overall` | Rolled-up status | `_compute_status()` applies rules below | `ok` | `degraded` | `broken` |

### `overall` health rules

`overall` is computed as the **worst** state across all of:

- **Daytime frame freshness:** any camera with `last_frame_age_ms > 60000` during daytime → `broken`
- **ffmpeg restart storm:** any camera with `ffmpeg_restarts > 10 in last hour` → `broken`
- **YOLO p99 tail:** any camera with `yolo_ms_p99 > 1000` → `degraded`
- **Coral lock storm:** `lock_timeouts > 5 per hour` on feeder → `degraded`
- **Dropped frame rate:** `dropped_oldest / frames_processed > 0.05` → `degraded`
- **SSE client never reconnected:** zero active SSE clients for > 10 minutes while frames are being processed → `degraded` (someone should be watching)

### Honesty contract tests

For every metric in the table, there is a test at `tests/pipeline/test_honesty_contract.py` that:

1. Stands up the minimal machinery needed (FrameCapture with a mocked process, or a fake CameraProcessThread)
2. Fabricates the broken state the metric should detect
3. Reads the metric
4. Asserts it reports the expected state

Example tests (non-exhaustive, actual list is one per row above):

- `test_last_frame_age_broken_when_daytime_stall` — mock `is_nighttime` False, `last_frame_ms` > 60s ago → assert `overall = broken`
- `test_yolo_p99_excludes_skipped_frames` — feed 90 samples of 2 ms (skip frames) + 10 samples of 500 ms (real runs) → assert `yolo_ms_p99` reads from the 500 ms distribution (~500 ms), not the whole thing
- `test_classifier_stats_are_per_camera` — call classifier with camera="feeder" 10 times and camera="ground" 3 times → assert feeder stats = 10, ground stats = 3
- `test_num_frames_is_per_track` — create two tracks, advance the process frame counter to 100 for one, 50 for the other → assert each track summary writes the per-track count, not the global
- `test_ffmpeg_restart_loop_is_broken` — trigger 11 restarts in 30 min → assert `overall = broken`
- `test_dropped_oldest_threshold` — force queue-full drops > 5% → assert `overall = degraded`
- `test_p99_is_actually_p99_not_p100` — feed a distribution where p99 and max differ by > 2×; assert `yolo_ms_p99` reads the 99th percentile, not the max
- `test_p99_requires_minimum_samples` — with fewer than 10 samples, `yolo_ms_p99` returns `null` (or 0 with a separate "insufficient_samples" flag), not a misleading max. Prevents one-sample p99s from looking like real data.

These tests are first-class members of the test suite, not optional. A Phase 1 implementation that doesn't pass them is not Phase 1 complete.

### v2 metrics audit (what we're fixing)

| v2 metric | What was wrong | v3 remedy |
|---|---|---|
| `yolo_ms_p99` | `sorted(samples)[-max(1, len(samples)//100)]` → p100 for any n < 200 | `np.percentile(samples, 99)` on a ring of ≥ 100 real samples |
| `yolo_ms_avg` (ground cam 7 ms) | Averaged across skip frames (0–2 ms) with real runs → meaningless | Separate ring of `yolo_samples_real`, only record when YOLO ran |
| Classifier stats | Global dict reported per-camera → identical numbers on both cameras | Per-camera nested dict, indexed at write time |
| `unlabeled=2` vs 323 None tracks | Counter only counted classifier calls that returned None, not tracks that never got a species | Renamed to `unlabeled_call` to reflect what it actually counts. Track-level "never classified" count added separately. |
| Events `confidence` field | Stored `track.confidence` which is mutated every frame by tracker with YOLO bbox score | Events store both `bbox_confidence` (YOLO) and `species_confidence` (classifier) in separate columns |
| `num_frames` in track summary | Global process frame counter, not per-track | Per-track `frame_count` on `Track`, incremented by tracker on each hit |
| `overall=degraded` missed ground stall | Only checked p99, not stall age | New rule: daytime `last_frame_age_ms > 60000` → broken |
| `stationary_tracks=0 ever` | Stationary feature silently disabled; counter honest but feature was dead | Phase 2 reinstates the feature; counter becomes meaningful |

## 7. Dashboard Rendering

### Layout

```
┌─────────────────────────────────────────────┐
│                                             │
│            ┌─ <video id="feeder"> ──┐       │
│            │  (go2rtc MSE, HD,     │       │
│            │   native fps)          │       │
│            │                        │       │
│            │       ┌──── label      │       │  ← y = 0.25 × video_h
│            │       │ Downy         │       │
│            │       │ Woodpecker    │       │
│            │       └────            │       │
│            │             🐦        │       │
│            │              bird     │       │
│            │                        │       │
│            └────────────────────────┘       │
│                                             │
│   <canvas id="label-overlay"> absolutely   │
│   positioned over <video>, same size       │
│                                             │
└─────────────────────────────────────────────┘
```

- Video element plays go2rtc's main stream via MSE over WebSocket
- Canvas is `position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none;`
- Canvas resolution matches video viewport resolution (updated on resize)
- Label coordinates scale from substream coords (640×360) to canvas coords via `scale_x = canvas_w / 640, scale_y = canvas_h / 360`

### Label styling

- **Dot state** (species not yet assigned): 4 px solid white circle, 0.8 opacity
- **Label state** (species assigned):
  - Pill-shaped rounded rectangle background, fill `rgba(0, 0, 0, 0.65)`, border `rgba(255, 255, 255, 0.15)` 1 px, corner radius 12 px
  - White text, 14 px, medium weight, species name only
  - Padding: 8 px horizontal, 4 px vertical
  - Drop shadow: 0 2 px 8 px rgba(0, 0, 0, 0.4)

### Render loop

```js
function renderFrame() {
  const now = performance.now();
  ctx.clearRect(0, 0, canvas.width, canvas.height);

  const basY = 0.25 * canvas.height;
  const placements = [];

  for (const [trackId, state] of trackStates) {
    const elapsed = now - state.last_t;
    if (elapsed > 3000) { trackStates.delete(trackId); continue; }

    // Interpolate x
    let renderX;
    const dt = state.last_t - state.prev_t;
    if (dt > 0 && elapsed < 500) {
      renderX = state.last_x + (state.last_x - state.prev_x) * (elapsed / dt);
    } else {
      renderX = state.last_x;
    }

    // Scale from substream coords to canvas
    const canvasX = renderX * (canvas.width / state.frame_width);

    // Compute opacity (fade in or out)
    let opacity = 1;
    if (!state.fadeOutAt) {
      const fadeInElapsed = now - state.first_seen_t;
      opacity = Math.min(1, fadeInElapsed / 200);
    } else {
      const fadeOutElapsed = now - state.fadeOutAt;
      opacity = Math.max(0, 1 - fadeOutElapsed / 300);
    }

    placements.push({trackId, x: canvasX, y: basY, state, opacity});
  }

  // Collision pass: sort by bbox area desc, place each, bump down if overlap
  placements.sort((a, b) => b.state.bbox_area - a.state.bbox_area);
  const placed = [];
  for (const p of placements) {
    let y = p.y;
    while (placed.some(q => overlaps(p.x, y, q))) y += LABEL_HEIGHT + 8;
    p.y = y;
    placed.push(p);
  }

  // Draw each
  for (const p of placed) drawLabel(ctx, p);

  requestAnimationFrame(renderFrame);
}
```

### SSE subscription

```js
const es = new EventSource(`/api/pipeline/events/sse?camera=feeder`);
es.onmessage = (msg) => {
  const ev = JSON.parse(msg.data);
  const now = performance.now();
  const seen = new Set();
  for (const t of ev.tracks) {
    seen.add(t.track_id);
    let state = trackStates.get(t.track_id);
    if (!state) {
      state = { first_seen_t: now, prev_t: now, prev_x: t.bbox_center_x };
      trackStates.set(t.track_id, state);
    }
    state.prev_t = state.last_t ?? now;
    state.prev_x = state.last_x ?? t.bbox_center_x;
    state.last_t = now;
    state.last_x = t.bbox_center_x;
    state.species = t.species;
    state.bbox_area = (t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]);
    state.frame_width = t.frame_width;
    state.frame_height = t.frame_height;
    state.fadeOutAt = null;
  }
  // Any track in state map but not in this event starts fading out
  for (const [trackId, state] of trackStates) {
    if (!seen.has(trackId) && !state.fadeOutAt) state.fadeOutAt = now;
  }
};
```

### go2rtc MSE stream URL

From dashboard JS:
```js
video.src = null;
// go2rtc's built-in stream.html does the MSE dance; we use the same endpoint
const videoWsUrl = `ws://${location.hostname}:1984/api/ws?src=feeder-main`;
// ... MSE MediaSource setup using go2rtc's documented protocol
```

(Exact MSE setup code is lifted from go2rtc's reference stream.html; this is a known-good snippet, not something we invent.)

## 8. Testing Strategy

### Unit tests

- Every existing `tests/pipeline/` test continues to pass (or is updated to reflect v3 contracts)
- New tests for `pipeline/vote_locker.py` (Phase 2)
- New tests for `pipeline/sse_events.py` (Phase 1): event shape, subscription handling, per-camera filtering

### Integration tests

- `test_pipeline_e2e.py` updated: v3 produces SSE events with correct shape, no MJPEG path
- Per-camera classifier routing: feeder uses yard, ground does not; confirmed via mock
- Stationary suppression (Phase 2): mock stationary regions, assert YOLO is not called
- Vote locking (Phase 2): feed 5 classification calls with varied species, assert lock behavior matches contract

### Honesty contract tests

- `tests/pipeline/test_honesty_contract.py` (NEW in Phase 1, expanded in Phase 2)
- One test per row in the metrics inventory
- Each test fabricates a broken state and asserts the metric detects it
- Each test also asserts the metric reads correctly in a healthy state

### End-to-end verification (pre-cutover)

- Launch v3 in the worktree against the running test video loop
- Open a headless browser (Playwright) to `https://birds.vivessato.com/` (or localhost for dev)
- Verify:
  1. `<video>` element is playing (non-zero currentTime, readyState ≥ 3)
  2. Browser frame rate ≥ 25 fps (via `requestAnimationFrame` timing)
  3. SSE connection active (check network panel)
  4. At least one floating label has appeared in the last 60 s of observation (screenshot evidence)
  5. Labels move horizontally on screen when bird positions change (screenshot delta)
  6. Metrics on `/api/pipeline/health` show sensible values (all fields populated, no NaN/None, no `unknown` status)
- Screenshots and raw metric snapshots archived in `docs/superpowers/progress/2026-04-11-v3-verification/` for David's review
- All of the above run automated — no manual "looks good" claims

## 9. Migration and Cutover

### Worktree setup

1. Create `.worktrees/pipeline-v3` via `git worktree add .worktrees/pipeline-v3 -b pipeline-v3`
2. Verify `.worktrees/` is in `.gitignore`
3. Verify baseline tests pass in the worktree before any changes
4. All Phase 1 and Phase 2 work happens in the worktree; main stays on v2 until cutover
5. The worktree path is the development working directory for the rest of this session

### Coral coordination during testing

- The currently-running v2 pipeline holds the Coral USB for yard classification
- Most Phase 1 unit tests don't need Coral (mock `YardClassifier`)
- Integration tests that need real Coral: stop v2 briefly (~10 s), run the integration test block, restart v2
- A dedicated `scripts/coral_borrow.sh` helper:
  - `stop` → `launchctl unload ~/Library/LaunchAgents/com.vives.bird-pipeline.plist`
  - `start` → `launchctl load ~/Library/LaunchAgents/com.vives.bird-pipeline.plist`
  - Test blocks wrap their coral-dependent tests in this stop/start

### Dev run ports (to avoid colliding with the running v2)

During development the v3 pipeline runs on a different port so v2 keeps serving production:

| Service | v2 (production) | v3 (dev in worktree) |
|---|---|---|
| Pipeline health + SSE | 8100 | 8102 |
| Dashboard API | 8099 | 8099 (dev proxy can target either backend via env var) |
| Debug stream (MJPEG) | 8101 (v2 only, deleted in v3) | — |

The dev dashboard reads `PIPELINE_BACKEND_URL` from environment; set to `http://127.0.0.1:8102` in dev, `http://127.0.0.1:8100` in prod.

### Production MSE access via Cloudflare tunnel

In production the dashboard is served at `https://birds.vivessato.com/` via Cloudflare tunnel. The browser must connect to go2rtc's MSE WebSocket at `:1984/api/ws?src=feeder-main`. Two options:

1. **Add a tunnel ingress rule** that proxies `/go2rtc/` on the same origin to `http://localhost:1984/`. Dashboard uses relative URL `wss://${location.host}/go2rtc/api/ws?src=feeder-main`. Single origin, simplest for CORS and auth.
2. **Dashboard API proxies the WebSocket** through uvicorn (`/api/go2rtc/ws`). More code, but keeps the Cloudflare tunnel ingress rules unchanged.

Option 1 is preferred. The spec does not block on this choice — the Phase 1 dev loop uses localhost directly, and the production proxy is a Cloudflare config change David can make at cutover time.

### Cutover procedure (for David to run when he approves)

1. Merge `pipeline-v3` branch to `main`: `git merge pipeline-v3 && git push` (or equivalent)
2. Reload LaunchAgent so it picks up the new `bird_pipeline_v3.py`:
   ```
   launchctl unload ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
   # (update plist to point at bird_pipeline_v3.py if filename differs, or symlink)
   launchctl load ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
   ```
3. Open dashboard, confirm smooth video + floating labels
4. Watch health endpoint for 5 minutes; confirm all metrics green
5. If anything is wrong: see rollback

### Rollback

v3 introduces no schema changes to `pipeline.db` beyond adding `species_confidence` column (Phase 2 only). Phase 1 rollback is purely code:

1. `git revert` the merge commit, or
2. Stop the LaunchAgent, swap back to `bird_pipeline_v2.py`, restart
3. Dashboard changes are additive (new `<video>` + canvas); old MJPEG `<img>` can be temporarily restored by reverting `dashboard/index.html`

For Phase 2, the new `species_confidence` column is nullable so v2 can read the table without issue.

## 10. Out of Scope (Explicit Forget-Me-Nots)

These are decisions consciously deferred, with specific re-entry triggers:

- **VideoToolbox hardware decode** — revisit if Phase 1 metrics show CPU headroom is tight
- **YUV420p pixel format** — revisit if memory bandwidth is a measured bottleneck
- **POSIX shared memory + multi-process split** — revisit if GIL contention shows up in profiling
- **Audio-visual cross-check (Path 4)** — revisit if voting gets stuck on visually-similar species pairs in production data
- **Ground camera polish (flock dedup, scene-tuned motion gate, per-camera substream size)** — revisit when feeder is top-notch
- **Re-identification across track expiration** — out of scope indefinitely; Frigate doesn't solve this either
- **Frigate-style "best snapshot" thumbnail per track** — Phase 2 adds best-crop for classification but not for snapshot archival
- **Per-event clip recording** — HLS recording continues as whole-stream, no per-event clipping
- **Dashboard event log / visit list** — the side-panel "right now" text list is out (user picked C2), but the event history browser in another tab is a separate feature and unchanged

---

## Appendix A: Module interface sketches

### `pipeline/sse_events.py` (new)

```python
class SSEEventServer:
    def __init__(self, port: int = 8100):
        self.port = port
        self.clients: dict[str, list[Queue]] = {}  # per-camera queues
        self.stats = {"events_emitted": 0, "clients_connected": 0}

    def start(self) -> None:
        """Start HTTP server on self.port with /events/sse route."""
        ...

    def emit(self, camera: str, wall_time_ms: int, tracks: list[dict]) -> None:
        """Push an event to all subscribed clients for this camera."""
        payload = json.dumps({"camera": camera, "wall_time_ms": wall_time_ms, "tracks": tracks})
        for q in self.clients.get(camera, []):
            try:
                q.put_nowait(payload)
            except Full:
                pass  # slow client, drop

    def stop(self) -> None:
        ...
```

### `pipeline/classifier.py` (modified)

```python
@dataclass
class CameraClassifierConfig:
    use_yard: bool
    confident_threshold: float = 0.6
    uncertain_low: float = 0.3

class SmartClassifier:
    def __init__(
        self,
        yard_model_path: str,
        yard_labels_path: str,
        aiy_model_path: str,
        aiy_labels_path: str,
        regional_species,
        camera_configs: dict[str, CameraClassifierConfig],
    ):
        ...
        self.stats = {
            camera: {
                "yard": 0, "aiy": 0, "both_agree": 0,
                "unlabeled_call": 0, "lock_timeouts": 0, "retries": 0,
            }
            for camera in camera_configs
        }

    def classify(self, crop_pil, frame_time_ms, camera) -> ClassificationResult:
        config = self.camera_configs[camera]
        if config.use_yard:
            # yard → AIY fallback decision tree
            ...
        else:
            # AIY only
            ...
```

### `pipeline/vote_locker.py` (new, Phase 2)

```python
class VoteLocker:
    """Accumulates classification votes per track, locks species when thresholds met."""

    MIN_CONFIDENCE = 0.8
    MIN_ATTEMPTS = 3
    MIN_AGREEMENT = 0.6

    def add_vote(self, track, species: str, confidence: float) -> bool:
        """Add a vote. Returns True if track is now locked."""
        track.vote_history.append((species, confidence))
        if self._should_lock(track):
            winner_species, winner_confidence = self._winning_vote(track)
            track.species = winner_species
            track.species_confidence = winner_confidence
            track.is_locked = True
            return True
        return False

    def _should_lock(self, track) -> bool:
        if len(track.vote_history) < self.MIN_ATTEMPTS:
            return False
        max_conf = max(c for _, c in track.vote_history)
        if max_conf < self.MIN_CONFIDENCE:
            return False
        species_counts = Counter(s for s, _ in track.vote_history)
        top_species, top_count = species_counts.most_common(1)[0]
        return (top_count / len(track.vote_history)) >= self.MIN_AGREEMENT

    def _winning_vote(self, track) -> tuple[str, float]:
        species_counts = Counter(s for s, _ in track.vote_history)
        top_species = species_counts.most_common(1)[0][0]
        top_confidence = max(c for s, c in track.vote_history if s == top_species)
        return top_species, top_confidence
```

## Appendix B: Decisions log

Every autonomous decision made during brainstorming and implementation is logged here so David can audit what I committed to without his explicit signoff.

1. **Live view philosophy: C2** (David chose) — floating labels always on, no replay toggle, client-side interpolation
2. **Label contents: species only** (David chose)
3. **Side panel: none** (David chose)
4. **Ground cam scope: B** (David chose) — shared pipeline, no UX polish
5. **Classifier routing: B** (David chose) — per-camera config, ground skips yard
6. **Unlabeled state: B** (David chose) — 4 px dot at bird x, y=25%
7. **Audio cross-check: A** (David chose) — dropped, saved as forget-me-not
8. **Scope phasing: (ii)** (David chose) — two phases, Phase 1 is the prototype
9. **Migration: (iii)** (David chose) — worktree + atomic cutover
10. **v2 spec fate: (i)** (David chose) — marked superseded, not deleted
11. **Substream resolution: 640×360 @ 5 fps** (Claude decided, Frigate canonical) — can be tuned later
12. **Voting thresholds: Frigate defaults** (Claude decided) — conf ≥ 0.8, ≥ 3 attempts, ≥ 60% agreement
13. **SSE port: 8100** (Claude decided) — same port as health server, new `/events/sse` path
14. **Interpolation: linear extrapolation with 500 ms cap, 150 ms ease on contradiction** (Claude decided)
15. **Label collision: vertical stack only, safety net** (David confirmed, Claude specified mechanics)
16. **Stationary suppression threshold: 10 frames / 10 px** (Claude decided, Phase 2)
17. **Max classification attempts: 5 in Phase 2** (Claude decided, up from 3)

---

*End of spec.*