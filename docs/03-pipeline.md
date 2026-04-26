# 03 · Pipeline — frame to labeled snapshot

End-to-end view of what `bird_pipeline_v3.py` does on the Pi. Code lives at `/Users/vives/bird-classifier-pi/pipeline/` and `bird_pipeline_v3.py`.

## Data flow

```
UniFi G3 Dome ──RTSP──► go2rtc :1984
                          │
            ┌─────────────┴─────────────┐
            ▼                           ▼
       feeder-sub (640×360)        feeder-main (1920×1080)
            │                           │
   ┌────────▼────────┐         ┌────────▼─────────┐
   │ FrameCapture    │         │ HiResCapture     │
   │ (one ffmpeg per │         │ (1080p ring)     │
   │  camera)        │         │ writes to        │
   │ wall_time_ms at │         │ HiResRingBuffer  │
   │ pipe-read       │         └────────┬─────────┘
   └────────┬────────┘                  │
            ▼                           │
   ┌─────────────────┐                  │
   │ MotionGate      │                  │
   │ (MOG2 + AOI)    │                  │
   └────────┬────────┘                  │
            ▼                           │
   ┌─────────────────┐                  │
   │ HailoDetector   │                  │
   │ (YOLOv8s on     │                  │
   │  Hailo-8L)      │                  │
   └────────┬────────┘                  │
            ▼                           │
   ┌─────────────────┐                  │
   │ BirdTracker     │                  │
   │ (Norfair +      │                  │
   │  Frigate dist)  │                  │
   └────────┬────────┘                  │
            ▼                           │
   ┌─────────────────┐                  │
   │ PiClassifier    │                  │
   │ (vote-locked    │                  │
   │  AIY ONNX)      │                  │
   └────────┬────────┘                  │
            │ track locks                │
            ▼                           │
   ┌─────────────────┐                  │
   │ SnapshotWriter  │◄─ pulls hi-res ──┘
   │ (background     │
   │  thread)        │
   └────────┬────────┘
            ▼
   ┌─────────────────────────────────────┐
   │ ~/bird-snapshots/classified/{sp}/   │
   │   feeder_*.jpg                      │
   │ classifications.db (DB row)         │
   └─────────────────────────────────────┘
```

SSE events stream out of the pipeline at `:8105/events/sse?camera=feeder` — one event per processed frame with active tracks. The dashboard's Live view subscribes to this.

## Stage-by-stage

### Frame capture (`pipeline/frame_capture.py`)

One ffmpeg subprocess per camera, output at 640×360 raw BGR. A pipe-drain thread reads frames into a bounded `out_queue` (drop-oldest on backpressure). Each frame is stamped with `wall_time_ms = time.time() * 1000` AT pipe-read.

**Watchdog** (`_watchdog`, lines 166-189): two checks per tick (`WATCHDOG_CHECK_S = 2.0`):

1. Process death — `if proc is not None and proc.poll() is not None: restart`. This catches ffmpeg dying before producing the first frame (otherwise `last_frame_ms` stays None and a stall-only check loops forever — see `historical/progress/2026-04-25-pi5-handoff.md` for the 5-hour-outage incident that prompted this).
2. Stall-age — if no frame in `WATCHDOG_STALL_MS = 10_000`, restart.

### Motion gate (`pipeline/motion_gate.py`)

MOG2 background subtraction on a 4-point AOI polygon (the feeder area; sky / branches / grass are masked off so YOLO doesn't run on irrelevant motion). Returns the list of bbox motion regions per frame.

### Hailo detector (`pipeline/hailo_detector.py`)

YOLOv8s on Hailo from `/usr/share/hailo-models/yolov8s_h8l.hef` (override via `PI_YOLO_HEF` env). Acquires an `InferModel` from the shared `HailoEngine` (see `04-hailo-engine.md`); calls `model.infer({input_name: bgr})` — the engine hides the run_async / wait round-trip.

Output is a flat FLOAT32 ndarray of shape `(40080,)` — densely-packed per-class blocks in the form `[count_c0, det0_5fl, ..., countN_cK_5fl, count_c1, ...]` for 80 COCO classes. `_parse_yolo_flat_output` walks the variable-length blocks and emits `Detection(box=[x1,y1,x2,y2], confidence)` for class 14 (bird).

Per-frame budget on Hailo-8L: ~17 ms isolated, ~22 ms when co-scheduled with a Hailo classifier (measured 2026-04-25, see playbook §12).

### Tracker (`pipeline/tracker.py` / `pipeline/bird_tracker.py`)

Norfair with a `_frigate_distance` distance function. Distance threshold 2.0 (raised from 1.0 on 2026-04-17 because fast-moving birds were losing track ID mid-flight). Returns `TrackerOutput(active, new, expired)`.

### Classifier (`pipeline/pi_classifier.py` + `pipeline/model_registry.py`)

`PiClassifier` wraps a `ModelRegistry` of candidate classifiers. The active candidate is picked via the `PI_CLASSIFIER` env var; default is `aiy_onnx` (Google AIY Birds V1, 965 species, ONNX runtime on Pi CPU at ~7.4 ms / crop). Other candidates (registered but `is_classifier=False` → not selectable for the live slot): `resnet50_hailo`, `yolov8s_hailo`, `yolov6n_hailo`. Plus a `flagship_pending` placeholder for the upcoming Tier 2 model.

Vote-lock semantics (in `pipeline/process_thread.py`):

- Append `(species, confidence)` to `track.vote_history` per classification.
- Set `track.species` to the current top-voted species so the live label appears immediately.
- Lock when ≥3 votes AND top species ≥ 0.35 conf AND top species holds ≥ 60 % of votes.
- After `MAX_CLASSIFICATION_ATTEMPTS = 5` without lock: take plurality winner (or leave unlabeled).

### Hi-res ring buffer (`pipeline/hires_ring.py`)

`HiResRingBuffer` is a thread-safe rolling buffer of `RingFrame(frame_bgr, wall_ms)` for the main-stream camera. `HiResCapture` is a dedicated ffmpeg pipe-drain that pushes 1920×1080 frames into the ring at 5 fps.

On the Pi we run with `PIPELINE_HIRES_RING=authoritative`, meaning the snapshot writer prefers a ring frame (matched by `wall_time_ms`) over the substream frame. This is the path that delivers the 1920×1080 `feeder_*.jpg` output.

### Snapshot writer (`pipeline/snapshot_writer.py`)

Background thread, queue-fed (maxsize=32, drop-oldest). For each track that locks: pulls the matching hi-res frame from the ring (by `wall_time_ms` ± tolerance), runs `classifier.authoritative_classify()` for a final AIY label on the hi-res crop, writes:

- `~/bird-snapshots/classified/{species}/feeder_YYYY-MM-DD_HH-MM-SS_{track_id}.jpg`
- corner-bracketed copy at `~/bird-snapshots/annotated/...`
- a row in `classifications.db` with `extra_json.model_source = <active classifier name>`

Health counters (visible at `/api/pipeline/health`):
`submitted, written, dropped_full, errors, hires_ok, hires_fail, hires_skipped, aiy_relabel, aiy_none, ring_pick_ok, ring_pick_empty, shadow_sidecar_written`.

## Two timestamps that matter

(Same architecture as iMac's, see `historical/specs/2026-04-25-imac-live-classify-as-built.md` §2 for the long form.)

- **SSE `wall_time_ms`** — stamped at pipe-read in `frame_capture._pipe_drain` (line 147). The "what wall-clock did Python see this frame at" timestamp; drives label sync in the Live view.
- **HLS `completed_ms`** — stamped at `.ts` segment file mtime (in `hls_recorder._manifest_loop`). Currently unused by the Pi Live view (which uses WebRTC instead) but the `segments.json` sidecar is still written.

The Pi's Live view uses go2rtc WebRTC + SSE labels with CSS-driven visual smoothing — see `05-dashboard.md`. No PROGRAM-DATE-TIME math, no two-Gaussian-kernel smoothing — different tradeoff than iMac's `/live.html`, by design.

## What's intentionally absent on the Pi

- No yard model (the iMac's per-camera yard-first decision tree). On the Pi only AIY runs, so the per-camera config in `camera_config.py` is mostly inert.
- No Coral lock. The Pi has no Coral USB; the lock infra is silently bypassed because the classifier runs on CPU.
- No second camera. `CAMERAS_DETECT` is `[feeder]` only on Pi (the ground camera entry is commented out — see `bird_pipeline_v3.py`).
