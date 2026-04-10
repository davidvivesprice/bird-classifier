# Live Detection Pipeline v2 — Frigate-Inspired Architecture

**Date:** 2026-04-10
**Status:** Approved
**Author:** Claude + David
**Supersedes:** (previous draft proposed 35s-delayed HLS with time-synced overlay — abandoned after Frigate research revealed a simpler approach)

## Problem

The current live detection pipeline (`bird_pipeline.py`) produces a poor user experience:

- Bounding boxes update in ~1-second batches, not smoothly
- Boxes appear shifted from birds (no relationship between detection events and video)
- Ghost boxes linger after birds leave
- "Unidentified bird" labels appear when either model is uncertain
- 3 FPS is too slow for smooth tracking
- JPEG keeper encoding blocks the hot path (50–150ms stalls)
- Yard model not integrated into pipeline (silent fallback to AIY-only)

**From the audit** (2026-04-09):
- P0: Batched SSE broadcasting → 1–2s perception delay
- P0: 60 FPS canvas redraw on 3 FPS data → wasted cycles, box jumps
- P0: Full-frame JPEG encoding on hot path → per-bird stalls
- P1: Coral TPU single-session contention
- P1: YardClassifier not integrated into pipeline
- P1: Classifying every detection on every frame (should be once per track)

**Live detection is the most critical part of the project.** This rewrite brings it to Frigate-quality.

## The Key Insight

Frigate solves smooth real-time detection by **not trying to align two independent streams**. Its overlay-with-bounding-boxes view is served from a stream that Frigate itself decodes — the same frames detection ran on. Zero sync problem.

The "pretty" go2rtc WebRTC/MSE stream without overlay is separate, and used when you don't need labels.

Our previous draft tried to align detection events from one pipeline to an HLS video from another. That's a painstaking solvable problem, but also an unnecessary one. **We do what Frigate does.**

## Solution Overview

Rebuild the pipeline with:

1. **ffmpeg subprocess → raw YUV → shared memory** frame decoding (instead of PyAV). Faster, more stable, enables hardware acceleration.
2. **Motion-gated detection with stationary-object skipping** — don't burn CPU on perched birds.
3. **Norfair tracker** (Kalman + distance) — smooth tracking at low FPS, tolerates dropped detections.
4. **Classification once per track** (Smart B chain: yard → AIY → audio → unlabeled).
5. **Debug WebSocket stream with server-drawn labels** — we decode, detect, annotate, and serve the same frames. No sync problem by construction.
6. **HLS for recording only** — go2rtc writes chunks to disk. Used for scrubbing, clip search, future auto-edits. Not used for live view.
7. **Time-indexed event store** — every detection and track summary written to `pipeline.db` for querying and future features.

## Glossary

- **Shared memory frame buffer**: POSIX shared memory region holding decoded YUV frames. Ring buffer of ~4 slots per camera. Avoids per-frame copies between threads.
- **ffmpeg subprocess**: A child ffmpeg process per camera. Reads RTSP, decodes with hardware acceleration, writes raw YUV420p frames to stdout. The pipeline reads the pipe and drops frames into shared memory.
- **jsmpeg-style stream**: Low-latency (~500ms-1s) MJPEG-over-WebSocket stream. Not true jsmpeg (which is MPEG-1), but the same concept: server decodes, annotates, re-encodes frames, ships to client. Simple, no browser codec magic needed.
- **Norfair**: Open-source multi-object tracker by tryolabs. Kalman filter + configurable distance function. Default distance is euclidean on centroid, but we'll use Frigate's "centroid-x + bottom-y + size-normalized" variant for stability under perspective.
- **Stationary skipping**: Once a tracked object hasn't moved N frames, run detection on it every M-th frame instead of every frame. Huge CPU win for perched birds.
- **Smart B classification**: yard → AIY → BirdNET audio → unlabeled decision tree. See § Classifier for pseudocode.
- **Track summary**: Row written when a track expires. `{species, start_time, end_time, peak_confidence, best_keeper_path, num_frames}`. The basis for clip search and auto-edits.
- **Motion gate**: OpenCV-style background subtraction. Runs on every frame. Detection runs only on motion regions plus periodic full-frame fallbacks.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                         Per Camera (×2)                             │
│                                                                     │
│  RTSP ──> ffmpeg subprocess ──> raw YUV ──> [SHM ring buffer]       │
│                                                 │                   │
│                                                 ▼                   │
│                                       [Motion Gate]                 │
│                                                 │                   │
│                                  motion? │      │ still             │
│                                           ▼      │                  │
│                                    [YOLO Detector] (skip)           │
│                                           │                         │
│                                           ▼                         │
│                                  [Norfair Tracker]                  │
│                                           │                         │
│                                           ▼                         │
│                      [Smart Classifier] (only new tracks)           │
│                                           │                         │
│                  ┌────────────────────────┼────────────────────────┐│
│                  ▼                        ▼                        ▼│
│         [Event Store]        [Debug Stream Encoder]     [Track Log] │
│         (pipeline.db)         (annotate + MJPEG/WS)                 │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
                  │                         │
                  ▼                         ▼
          GET /api/events          WS /debug-stream/{camera}
          (for scrubbing,          (dashboard connects here
           clip search)             for labeled live view)

Plus: go2rtc runs separately, handling both cameras:
  - Serves pretty HLS/WebRTC (no labels) for recording playback
  - Writes HLS chunks to disk for scrubbing + future auto-edits
```

**Three threads per camera:**

1. **Capture thread** — owns the ffmpeg subprocess, writes frames to shared memory
2. **Process thread** — runs motion gate → YOLO → tracker → classifier (inline, not in separate stages)
3. **Stream thread** — encodes annotated frames to MJPEG and pushes to WebSocket clients

**Shared services:**
- Event store (SQLite WAL, one DB for both cameras)
- Health server (HTTP endpoint)
- WebSocket server (MJPEG streams per camera)

## Why Processing Is Inline (Not Queued Stages)

The previous draft had detection → queue → classifier as separate threads with queues between. The reviewer flagged this as a threading bomb because of Coral single-session constraints.

The Frigate approach runs detection and classification **in the same thread**. One per camera. Classification only runs on NEW tracks, so it's infrequent. This:

- Eliminates the Coral lock problem (serialization happens naturally via single-thread execution)
- Removes the queue-between-stages complexity
- Keeps detection smooth because classification of new birds takes <30ms

Two separate process threads (one per camera) can still contend on the Coral USB for yard/AIY inference — we add a **per-classifier lock with 2-second timeout**. If contention exceeds the timeout, we skip classification for that frame (track stays unlabeled this iteration, will be classified on its next detection).

## Components

### `pipeline/frame_capture.py` (~180 lines)

Replaces PyAV-based `VideoStreamReader`. Owns the ffmpeg subprocess per camera.

```python
class FrameCapture:
    def __init__(self, camera_name, rtsp_url, width=1920, height=1080, fps=5):
        self.camera_name = camera_name
        self.width = width
        self.height = height
        self.fps = fps
        self.proc = None
        self.shm = SharedMemoryRingBuffer(camera_name, size=4, frame_bytes=width*height*3//2)
        self.last_pts = None
        self.stats = {"frames": 0, "dropped": 0, "restarts": 0}

    def start(self):
        # Spawn ffmpeg: -rtsp_transport tcp -i {url} -vf fps={fps} -f rawvideo -pix_fmt yuv420p -
        # Hardware accel via -hwaccel videotoolbox on macOS
        ...

    def read_frame(self) -> Optional[Frame]:
        # Read width*height*1.5 bytes from stdout into shared memory
        # Returns Frame reference (index into ring buffer + metadata)
        ...

    def stop(self): ...
    def restart(self): ...  # called by watchdog on stall
```

**Key decisions:**
- ffmpeg handles all decoding + hardware acceleration (`-hwaccel videotoolbox` on macOS)
- Raw YUV420 output is ~3MB per 1080p frame; shared memory avoids per-frame copies
- FPS enforcement via ffmpeg `-vf fps=5` filter (not Python-side rate limiting)
- Watchdog thread restarts ffmpeg if no frame received in 10s

### `pipeline/shm_buffer.py` (~80 lines)

Simple ring buffer over POSIX shared memory.

```python
class SharedMemoryRingBuffer:
    def __init__(self, name: str, size: int, frame_bytes: int):
        self.slots = [
            SharedMemory(create=True, name=f"{name}_f{i}", size=frame_bytes)
            for i in range(size)
        ]
        self.next_write = 0
        self._lock = threading.Lock()

    def write(self, data: bytes) -> int:
        # Returns slot index
        ...
    def read(self, slot: int) -> memoryview: ...
    def cleanup(self): ...  # unlink all slots on shutdown
```

### `pipeline/motion_gate.py` (keep existing, ~80 lines)

Already works. Existing `motion_gate.py` uses OpenCV background subtraction. Tune threshold per camera in config. Add output: list of `(x1, y1, x2, y2)` motion regions instead of just a bool.

### `pipeline/detector.py` (~120 lines)

Pull from `bird_inference.py`. Changes:

- Takes motion regions from motion_gate and runs YOLO on the **cropped region of the frame**, not the full frame (Frigate-style region detection). This cuts YOLO input from 640x640 downscale of 1080p to 640x640 of a ~300x300 motion region. Faster and more accurate for small birds.
- Still runs full-frame YOLO every 10 seconds as a sanity fallback.
- Supports stationary-object skipping — takes a set of "recently-stationary track regions" from the tracker and skips re-detection in them unless motion is detected there specifically.

### `pipeline/tracker.py` (~200 lines, new implementation using Norfair)

Replaces `bird_tracker.py`. Wraps `norfair.Tracker`.

```python
class BirdTracker:
    def __init__(self):
        self.tracker = norfair.Tracker(
            distance_function=self._distance,
            distance_threshold=50,
            hit_counter_max=15,      # expire after 15 frames without match
            initialization_delay=1,  # 1 hit to start a track
        )
        self.tracks: dict[int, Track] = {}
        self.stationary_frames = {}  # track_id -> frames without motion

    def _distance(self, detection, tracked_object):
        # Frigate-style: normalize by object size, use centroid-x + bottom-y
        ...

    def update(self, detections: list[Detection], frame_time: float) -> TrackerOutput:
        # Convert to norfair detections, call update, rebuild Track state
        # Mark tracks as stationary if they haven't moved > 10px in 10 frames
        ...
```

The existing `bird_tracker.py` stays in the repo; the new one lives at `pipeline/tracker.py` and is not imported by the old pipeline until flip-over. Tests are updated to cover the new tracker separately.

### `pipeline/classifier.py` (~280 lines) — Smart B

Explicit pseudocode:

```python
CONFIDENT = 0.60       # yard model "very confident" threshold
UNCERTAIN_LOW = 0.30   # below this, trust nothing from yard
AGREE_MARGIN = 0.15    # confidence within this = "agreeing"

class SmartClassifier:
    def classify(self, crop, frame_time_ms, camera) -> ClassificationResult:
        with self._coral_lock:  # 2s timeout
            yard = self._run_yard(crop)

            # Path 1: Yard is confident → use it (fast path, 95% case)
            if yard.confidence >= CONFIDENT:
                return ClassificationResult(
                    species=yard.species,
                    confidence=yard.confidence,
                    model_source="yard",
                )

            # Path 2: Yard not useful → AIY only
            if yard.confidence < UNCERTAIN_LOW:
                aiy = self._run_aiy(crop)
                if aiy.confidence >= CONFIDENT:
                    return ClassificationResult(
                        species=aiy.species,
                        confidence=aiy.confidence,
                        model_source="aiy",
                    )
                return ClassificationResult(None, 0, None)

            # Path 3: Yard uncertain — compare with AIY
            aiy = self._run_aiy(crop)

            if aiy.species == yard.species:
                # Both agree on species → confident even if individually uncertain
                return ClassificationResult(
                    species=yard.species,
                    confidence=max(yard.confidence, aiy.confidence),
                    model_source="both_agree",
                )

            # Disagreement → audio cross-check
            audio = self._audio_lookup(camera, frame_time_ms)
            if audio and audio.species in (yard.species, aiy.species):
                return ClassificationResult(
                    species=audio.species,
                    confidence=max(yard.confidence, aiy.confidence),
                    model_source="audio_confirmed",
                )

            # No confident answer → don't label
            return ClassificationResult(None, 0, None)

    def _audio_lookup(self, camera, frame_time_ms) -> Optional[AudioHit]:
        # Query birdnet_local.db for detections on that camera's location
        # within ±5s of frame_time_ms. Returns None if no match.
        # This is a fast indexed SQL query, <5ms.
        ...
```

**Key decisions:**
- Lock timeout: 2s. If contention, return `ClassificationResult(None, 0, None)` and the track stays unlabeled this iteration. Gets retried on next new-track creation for the same bird if we lose track.
- Audio query uses indexed lookup on existing `birdnet_local.db` — we do NOT query BirdNET live. We query already-stored detections.
- All thresholds in a config dict so you can tune without editing code.

### `pipeline/event_store.py` (~200 lines)

Same as previous draft — separate `pipeline.db`, batched writes, `pipeline_events` and `pipeline_tracks` tables. One addition: `track_summaries_with_hls` join helper for clip search.

```sql
CREATE TABLE pipeline_events (
  camera TEXT NOT NULL,
  frame_time INTEGER NOT NULL,    -- unix ms
  track_id INTEGER NOT NULL,
  species TEXT,                    -- NULL = unclassified (don't label)
  confidence REAL,
  model_source TEXT,               -- 'yard' | 'aiy' | 'both_agree' | 'audio_confirmed'
  bbox_json TEXT NOT NULL,         -- "[x1,y1,x2,y2]" in source resolution
  is_new INTEGER DEFAULT 0,
  PRIMARY KEY (camera, frame_time, track_id)
);
CREATE INDEX idx_events_track ON pipeline_events(camera, track_id);
CREATE INDEX idx_events_time ON pipeline_events(frame_time);
CREATE INDEX idx_events_species ON pipeline_events(species, frame_time);

CREATE TABLE pipeline_tracks (
  track_id INTEGER PRIMARY KEY AUTOINCREMENT,
  camera TEXT NOT NULL,
  species TEXT,
  start_time INTEGER NOT NULL,
  end_time INTEGER NOT NULL,
  peak_confidence REAL,
  num_frames INTEGER,
  model_source TEXT,
  best_keeper_path TEXT,           -- snapshot of best-confidence frame
  motion_pct REAL                  -- % of frames where bird moved >10px (filters stationary)
);
CREATE INDEX idx_tracks_species ON pipeline_tracks(species, start_time);
CREATE INDEX idx_tracks_duration ON pipeline_tracks(camera, end_time, start_time);
```

Retention: `pipeline_events` auto-pruned to 7 days. `pipeline_tracks` kept indefinitely (small).

### `pipeline/debug_stream.py` (~220 lines)

The WebSocket streamer that pushes annotated frames to the dashboard. MJPEG-over-WebSocket (one JPEG frame per WS message).

```python
class DebugStreamServer:
    def __init__(self, port=8101):
        self.port = port
        self.clients: dict[str, set[WebSocket]] = {"feeder": set(), "ground": set()}

    def push_frame(self, camera: str, jpeg_bytes: bytes):
        # Broadcast to all clients subscribed to this camera
        ...

class FrameAnnotator:
    def annotate(self, yuv_frame, tracks: list[Track]) -> bytes:
        # Convert YUV → BGR via OpenCV
        # Draw labels at centered-X, fixed Y 25% from top
        # NO bounding boxes (per earlier decision)
        # Encode as JPEG quality 75 (smaller payload for 5 FPS streaming)
        ...
```

**Key decisions:**
- 5 FPS output (matches detection rate, no interpolation needed)
- JPEG quality 75: ~100KB per frame × 5 FPS × 2 cameras = 1 MB/sec WebSocket throughput. Acceptable on a LAN, may be chunky on cellular.
- Optional downscaling: serve at 960x540 to cut bandwidth 4x (dashboard uses this by default, fullscreen mode requests 1920x1080)
- Annotated frames are NOT saved to disk — only sent to active clients
- If no clients are connected, annotator is skipped entirely (save CPU)

### `pipeline/hls_recorder.py` (~80 lines)

Minimal — just configures go2rtc and manages retention.

```python
class HlsRecorder:
    def configure_go2rtc(self):
        # Via go2rtc API: enable HLS output for each camera
        # Set segment length 2s, chunks_dir /Users/vives/bird-snapshots/hls/{camera}/
        ...

    def cleanup_old_chunks(self):
        # Delete chunks older than 7 days
        # EXCEPT: keep chunks that overlap pipeline_tracks with duration>10s AND peak_confidence>0.85 for 30 days
        ...

    def chunks_for_range(self, camera, start_ms, end_ms) -> list[Path]:
        # Used by future clip browser — not called in initial implementation
        ...
```

**HLS is recording only** — not the live view. The dashboard uses it for scrubbing back in time, or to playback a clip from the event store query results. If go2rtc crashes, we lose recording but debug_stream still works (separate data path).

### `bird_pipeline.py` (new orchestrator, ~150 lines)

Start ffmpeg per camera, start process thread per camera, start shared services.

```python
def main():
    event_store = EventStore(DB_PATH)
    debug_stream = DebugStreamServer(port=8101)
    classifier = SmartClassifier(...)

    cameras = []
    for name, url in CAMERAS.items():
        cap = FrameCapture(name, url)
        cap.start()
        cameras.append(cap)
        
        proc = CameraProcessThread(
            name=name,
            capture=cap,
            classifier=classifier,
            event_store=event_store,
            debug_stream=debug_stream,
        )
        proc.start()

    start_sse_server()      # /api/pipeline/events for scrubbing
    start_health_server()   # /api/pipeline/health
    start_prune_loop(event_store)
    start_watchdog(cameras)
    wait_for_shutdown()
```

### Dashboard changes (`dashboard/index.html`)

**Two stream modes via existing "New Det" / "Old Det" toggle:**

- **Old Det** (for now): unchanged, uses existing `/api/ws?src={camera}-main` from go2rtc, no overlay
- **New Det** (default after migration): uses `/api/debug-stream/{camera}` WebSocket → receives JPEG frames → paints to `<canvas>` element → labels are baked into the frames

**No separate overlay canvas.** No RAF loop drawing boxes on top of a video. The labels come pre-rendered in the frames. The dashboard just decodes JPEG → paints to canvas. This kills the entire class of time-sync bugs.

**Scrubbing:**
- User clicks "scrub back" → switches to HLS `<video>` element
- Dashboard queries `/api/pipeline/events?camera=feeder&start=T1&end=T2` and overlays labels as a separate canvas on top of HLS playback
- This is the time-sync case we originally wanted — but it only applies to historical playback, not live
- Precision doesn't matter as much for scrubbing; a 200ms tolerance is fine

**Smoothness:** labels follow the bird because the server draws them on frames as they're processed. ~200-500ms latency end-to-end.

## Data Flow (Worked Example, Revised)

1. Feeder camera captures frame at wall-time T=1712700000000 (unix ms)
2. `FrameCapture` reads ffmpeg stdout → writes YUV to SHM slot 2
3. `CameraProcessThread` reads SHM slot 2, stamps with `wall_time=1712700000000`
4. Motion gate: detects motion in region `(400,200)→(800,600)`
5. Detector runs YOLO on that region → finds bird at `(450,250)→(550,350)` (coordinates in original frame)
6. Tracker.update(): no matching existing track → creates new Track(id=42, species=None, needs_classify=True)
7. Classifier.classify(crop):
   - Yard model: Black-capped Chickadee, confidence 0.82 → returns immediately (Path 1)
8. Tracker sets track.species='Black-capped Chickadee', needs_classify=False
9. EventStore.write_event(camera='feeder', frame_time=1712700000000, track_id=42, species='Black-capped Chickadee', confidence=0.82, model_source='yard', bbox_json='[450,250,550,350]', is_new=1)
10. FrameAnnotator.annotate(yuv_frame, tracks=[track42]):
    - YUV → BGR
    - Draw "Black-capped Chickadee" label at centered-X (500), fixed Y (canvas_height * 0.25)
    - Encode as JPEG → ~80KB bytes
11. DebugStreamServer.push_frame('feeder', jpeg_bytes) → sent over WebSocket to connected clients
12. Dashboard receives message → decodes JPEG → paints to canvas → user sees the frame WITH the label
13. **End-to-end latency: ~500ms–1s from camera to browser**

## Failure Modes

- **ffmpeg crashes** → watchdog detects stall (>10s no frame), restarts ffmpeg. Dashboard sees brief freeze.
- **Pipeline process crashes** → LaunchAgent restarts it. Chunks continue via go2rtc (separate process). Dashboard reconnects WebSocket.
- **Coral USB disconnected** → yard + AIY both fail. Tracks stay unlabeled. Debug stream still shows frames + tracks without labels. Health marks `coral_available: false`.
- **go2rtc crashes** → debug stream unaffected (we decode directly from RTSP via ffmpeg, not via go2rtc restream). Recording stops until go2rtc recovers.
- **Audio DB unavailable** → Smart B skips audio path, uses yard/AIY only. Logged as degraded.
- **Coral lock timeout** → track unlabeled this iteration. Re-classified on next new-track.
- **WebSocket client slow** → per-client send queue with 3-frame max. Slow clients get dropped. Server logs warning. New client reconnects cleanly.
- **Disk full for HLS chunks** → cleanup runs hourly, retention 7d/30d enforced. Emergency prune at 95% disk. Detection continues regardless (HLS is only recording).

## Testing Strategy

### Unit tests

- `test_frame_capture.py`: ffmpeg spawn, SHM write, watchdog restart
- `test_shm_buffer.py`: ring buffer correctness, cleanup on shutdown
- `test_motion_gate.py`: extend existing with region output
- `test_detector.py`: YOLO + region cropping, stationary skip
- `test_tracker.py`: Norfair wrapper, Frigate-style distance, track lifecycle, stationary detection
- `test_classifier.py`: all four Smart B paths with mocked yard/AIY/audio, lock timeout behavior
- `test_event_store.py`: writes, queries, prune, track summaries
- `test_debug_stream.py`: WebSocket broadcast, client disconnect cleanup, annotator correctness
- `test_hls_recorder.py`: retention logic, chunks_for_range

### Integration tests

- `test_pipeline_e2e.py`: feed existing Protect videos through full stack
  - `1m-empty.mp4` → 0 events
  - `chickadee-finch-downy.mp4` → events for all 3 species, tracks persist
  - `hairy-chick-tufted.mp4` → 3 species, no track ID collisions
  - `lots of birds.mp4` → multiple concurrent tracks
  - Track summaries match manual counts within ±10%

### Visual test

- `test_dashboard_live.py`: Playwright. Starts pipeline with test video input via ffmpeg-loop. Opens dashboard. Verifies WebSocket receives frames at ~5 FPS, labels render within bbox area, no JavaScript errors, no stale labels after bird leaves.

### Benchmark test

- `bench_pipeline.py`: 60s test video through pipeline. Asserts:
  - YOLO ms/frame p50 < 80ms (was 100ms — region detection is faster)
  - Classifier ms/new-bird p50 < 25ms
  - Tracker ms/frame < 3ms (Norfair overhead is slightly higher than IoU)
  - Frame capture FPS ≥ 4.5 (target 5)
  - Debug stream latency capture-to-JPEG-ready: < 150ms
  - Peak memory < 500 MB
  - Zero ffmpeg restarts during clean 60s run

### Time-sync validation (nice-to-have)

Since the debug stream IS the frames we processed, time sync is tautological. But we can test frame continuity: inject a known timestamp overlay on the source video, verify the dashboard sees it at the expected position.

## Health Monitoring

### Layer 1: Per-component health dict

```python
health = {
  "pipeline": {
    "feeder": {
      "frame_capture":  {"fps": 4.9, "dropped": 0, "ffmpeg_restarts": 0, "last_frame_age_ms": 210},
      "detector":       {"yolo_ms_avg": 75, "yolo_ms_p99": 120, "detections_per_min": 47, "stationary_skipped_pct": 62},
      "tracker":        {"active_tracks": 3, "stationary_tracks": 1, "tracks_per_min": 8, "expired_per_min": 7},
      "classifier":     {"path_yard": 41, "path_aiy": 4, "path_both_agree": 2, "path_audio": 1, "path_unlabeled": 0, "coral_lock_timeouts": 0},
      "debug_stream":   {"active_clients": 1, "fps_out": 4.8, "dropped_slow_clients": 0},
      "status":         "ok"
    },
    "ground": { ... }
  },
  "event_store": {"events_written": 12847, "db_size_mb": 48, "last_write_age_ms": 23},
  "hls_recorder": {"chunks_on_disk": 860, "total_mb": 3200, "oldest_chunk_age_h": 168},
  "overall": "ok"
}
```

### Layer 2: Health endpoint

`GET /api/pipeline/health` returns above. Top-level `status`:
- **ok**: all green
- **degraded**: FPS 3–4, YOLO p99 > 150ms, event store lag > 500ms, coral lock timeouts > 5/min
- **broken**: camera down > 30s in daytime, FPS < 3, event store write errors, coral unavailable, ffmpeg restart loop

### Layer 3: Dashboard System Status panel

Compact at top of dashboard:
- **ok**: small ✓, no noise
- **degraded**: amber icon with one-line summary
- **broken**: red alert, auto-expands with details + action

### Pipeline event log

Structured JSON lines at `~/bird-snapshots/logs/pipeline-events.log`:

```json
{"ts":"2026-04-10T08:22:41.123","level":"info","event":"track_start","camera":"feeder","track_id":42,"species":"Black-capped Chickadee","confidence":0.82,"model_source":"yard"}
{"ts":"2026-04-10T08:22:45.456","level":"info","event":"track_end","camera":"feeder","track_id":42,"species":"Black-capped Chickadee","duration_s":4.3,"peak_confidence":0.89,"num_frames":22}
```

Daily rotation, 30-day retention.

## Migration Strategy

1. **Phase 1: Build alongside, no user-facing changes**
   - New files in `pipeline/` subdir
   - New DB at `pipeline.db`
   - `bird_pipeline.py` continues to work (old code unchanged)
   - Tests pass on new components in isolation

2. **Phase 2: Ground cam first (reduced risk)**
   - New pipeline runs ground cam, old runs feeder cam
   - Dashboard gets a hidden URL param to use new debug stream for ground only
   - Monitor for 48h of daytime operation
   - Watch: ffmpeg restarts, memory growth, Norfair track quality, classifier accuracy

3. **Phase 3: Full cutover**
   - New pipeline runs both cameras
   - Old `bird_pipeline.py` disabled (kept in repo)
   - Dashboard default changed: "New Det" uses new debug stream, "Old Det" removed from UI but kept in code for 1 week
   - Monitor 1 week

4. **Phase 4: Cleanup**
   - Old `bird_pipeline.py` and `bird_tracker.py` deleted
   - Dashboard "Old Det" mode removed
   - Audit logs and spec committed

**No dual-pipeline simultaneous operation on the same camera.** Only one camera gets the new pipeline at a time during transition.

## Success Criteria

Ships when ALL of these are true:

1. **Benchmarks pass**: YOLO p99 <150ms, classifier <25ms, capture FPS ≥4.5, memory <500MB, 0 ffmpeg restarts during 60s test
2. **e2e tests pass**: all 5 Protect test videos produce expected species counts ±10%
3. **Visual test passes**: Playwright confirms frames + labels arriving at ~5 FPS
4. **No "unidentified bird" labels** appear anywhere
5. **Track smoothness**: labels follow birds visually (subjective test, approved by David)
6. **Health shows green** for 1 hour of daytime live operation
7. **Clip query works**: manually querying `pipeline_tracks` for "Downy Woodpecker visits > 5s" returns playable HLS time ranges

## What We're NOT Building (YAGNI)

- **Auto-editor for highlight reels** — separate spec, builds on event store + HLS chunks
- **Clip browser UI** — event store enables it, UI is a separate task
- **Per-species confidence tuning UI** — v3
- **WebRTC live stream** — MJPEG-over-WS is enough for our needs
- **Multi-TPU support** — single Coral is fine
- **Prometheus/Grafana** — dashboard IS the alerting
- **Hardware upgrade planning** — current iMac is sufficient

## Dependencies & Constraints

- **Python 3.9** in `venv-coral` (pycoral)
- **numpy < 2.0**
- **ffmpeg** (already installed at `/usr/local/bin/ffmpeg`, Homebrew)
- **Norfair** (`pip install norfair`) — new dependency
- **OpenCV** (`pip install opencv-python-headless`) — may already be installed for motion gate
- **go2rtc** running for HLS recording (existing)
- **WebSocket library**: `websockets` (Python, add if missing)
- **No Docker for pipeline** — runs as LaunchAgent

## Out-of-Scope Items That Should Follow

- Auto-clip compilation ("show me best Downy visits this week, crossfaded")
- Pipeline-driven retraining (use `pipeline_events` as a labeled data source)
- Cloud backup of HLS chunks (optional, user preference)
- Alternate hardware detector backends (Hailo, RKNN) — interesting but not needed

## References

- Frigate source: https://github.com/blakeblackshear/frigate
- Frigate video pipeline: https://deepwiki.com/blakeblackshear/frigate/4-video-processing-pipeline
- Frigate live view docs: https://docs.frigate.video/configuration/live/
- Frigate stationary objects: https://docs.frigate.video/configuration/stationary_objects/
- Norfair tracker: https://github.com/tryolabs/norfair
- go2rtc HLS docs: https://github.com/AlexxIT/go2rtc#module-hls
- Current pipeline audit: embedded in this spec's Problem section
