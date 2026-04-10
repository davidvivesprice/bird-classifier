# Live Detection Pipeline v2 — Frigate-Inspired Architecture

**Date:** 2026-04-10
**Status:** Approved (revised after three-reviewer audit)
**Author:** Claude + David
**Revisions:** Incorporates findings from architecture review, UX review, and implementation-risk review

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

The "pretty" go2rtc WebRTC/MSE stream without overlay is separate, used when you don't need labels.

**We do the same thing.**

## Solution Overview

Rebuild the pipeline with:

1. **ffmpeg subprocess per camera → numpy frames via bounded queue** (no shared memory — see § Design Decision: No SHM)
2. **Motion-gated region detection** (YOLO on motion crops, not full frames) with stationary-object skipping for perched birds
3. **Norfair tracker** (Kalman + distance) — smooth tracking at low FPS, tolerates dropped detections
4. **Classification once per track** (Smart B chain: yard → AIY → audio → unlabeled)
5. **Debug WebSocket stream with server-drawn labels** — we decode, detect, annotate, and serve the same frames
6. **Dedicated recording ffmpeg subprocess per camera** — writes HLS chunks to disk (go2rtc is NOT used for recording; see § Recording)
7. **Time-indexed event store** at `pipeline.db` for scrubbing + future clip search

## Glossary

- **ffmpeg subprocess**: Child ffmpeg process per camera. Reads RTSP, decodes to raw BGR frames, writes to stdout pipe. Pipeline owns a dedicated reader thread that drains the pipe into a bounded numpy frame queue.
- **Bounded frame queue**: `queue.Queue(maxsize=2)` of numpy BGR arrays. If the process thread stalls, the capture thread drops OLDEST frames to keep the pipe drained and prevent backpressure.
- **MJPEG-over-WebSocket**: Low-latency (~500ms-1s) stream. Server encodes annotated BGR frames as JPEG, pushes one frame per WebSocket message. Clients decode and paint to a `<canvas>`.
- **Norfair**: Open-source multi-object tracker by tryolabs. Kalman filter + configurable distance function. We use a Frigate-inspired distance: centroid-x + bottom-y normalized by object size.
- **Stationary skipping**: Once a tracked object's centroid hasn't moved >10px in 10 frames, detection on that region is skipped for N frames (default 30) unless motion is detected there.
- **Smart B classification**: yard → AIY → BirdNET audio → unlabeled decision tree. See § Classifier for pseudocode.
- **Track summary**: Row written when a track expires. `{species, start_time, end_time, peak_confidence, best_keeper_path, num_frames, motion_pct}`.
- **Motion gate**: OpenCV background subtraction. Runs on every frame. Emits motion regions `[(x1,y1,x2,y2), ...]`.
- **HLS recorder**: Dedicated ffmpeg subprocess per camera that re-muxes the RTSP stream into HLS segments on disk. Completely independent from the detection pipeline. Runs in copy mode (no re-encode) → ~1% CPU.

## Design Decision: No Shared Memory

Earlier drafts proposed POSIX shared memory via `multiprocessing.shared_memory` for zero-copy frame passing. **The implementation-risk reviewer caught critical issues:**

- `multiprocessing.shared_memory` on Python 3.9 + macOS has known bugs (bpo-39959, bpo-38119) that leak segments on crash
- SIGKILL-recovery of shared memory segments on macOS is fragile (no `/dev/shm`)
- POSIX SHM on macOS lives in `/private/var/folders/.../com.apple.shm.*` and requires explicit `shm_unlink`
- Frigate uses mmap over `/dev/shm` on Linux; that path doesn't exist on macOS
- Cost/benefit math: 3 MB/frame × 5 fps × 2 cameras = ~30 MB/sec of memcpy = ~10 ms/sec of extra CPU. Negligible compared to what SHM fragility would cost us.

**Decision:** Single process, threading, `queue.Queue(maxsize=2)` with numpy arrays. No shared memory anywhere.

If benchmarks later prove we need zero-copy at higher resolutions, we can revisit. Not now.

## Design Decision: Recording Is Its Own Process

Earlier drafts assumed go2rtc could be configured to record HLS chunks via API. **The implementation-risk reviewer caught this:**

- go2rtc serves HLS on-demand (`GET /api/stream.m3u8?src=feeder-main`)
- go2rtc is **not a DVR** — it does not write HLS chunks to disk as a persistent recording sink
- There is no API to enable HLS recording to a path

**Decision:** Each camera gets a **dedicated recording ffmpeg subprocess** that reads the RTSP stream in copy mode (no decode, no re-encode) and writes HLS segments:

```
ffmpeg -rtsp_transport tcp -i rtsp://... \
  -c copy -f hls \
  -hls_time 2 \
  -hls_list_size 0 \
  -hls_segment_filename /Users/vives/bird-snapshots/hls/feeder/seg_%Y%m%d-%H%M%S.ts \
  /Users/vives/bird-snapshots/hls/feeder/live.m3u8
```

This is ~1% CPU (pure re-mux), fully independent from detection, and the chunks on disk are real files playable by any HLS client. If the detection pipeline crashes, recording keeps going. If the recorder crashes, detection keeps going.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           Per Camera (×2)                               │
│                                                                         │
│  RTSP ──> ffmpeg-detect subprocess ──> stdout pipe                      │
│                                            │                            │
│                                            ▼                            │
│                                   [Capture Thread]                      │
│                                   drain pipe →                          │
│                                   numpy BGR array →                     │
│                                   Queue(maxsize=2)                      │
│                                   drop oldest if full                   │
│                                            │                            │
│                                            ▼                            │
│                                   [Process Thread]                      │
│                                   ┌────────────────┐                    │
│                                   │ Motion Gate    │                    │
│                                   └────────┬───────┘                    │
│                                            ▼                            │
│                                   ┌────────────────┐                    │
│                                   │ YOLO Detector  │                    │
│                                   │ (on motion     │                    │
│                                   │  regions)      │                    │
│                                   └────────┬───────┘                    │
│                                            ▼                            │
│                                   ┌────────────────┐                    │
│                                   │ Norfair Tracker│                    │
│                                   └────────┬───────┘                    │
│                                            ▼                            │
│                                   ┌────────────────┐                    │
│                                   │ Smart Classify │                    │
│                                   │ (new tracks    │                    │
│                                   │  only)         │                    │
│                                   └────────┬───────┘                    │
│                                            ▼                            │
│                      ┌─────────────────────┼─────────────────────┐      │
│                      ▼                     ▼                     ▼      │
│             [Event Store]      [Annotator + Encoder]    [Track Log]     │
│             (pipeline.db)       (JPEG to WS clients)                    │
│                                                                         │
│  RTSP ──> ffmpeg-record subprocess ──> HLS chunks on disk               │
│           (copy mode, independent process)                              │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘

Shared services (across both cameras):
  - Smart Classifier (single instance, holds Coral lock)
  - Event Store (SQLite WAL)
  - Debug Stream WebSocket Server (port 8101)
  - Health Server (port 8100)
  - Prune loop (hourly: events, hls chunks)
```

**Three threads per camera:**

1. **Capture thread** — dedicated pipe drainer. Its ONLY job is to read `w*h*3` bytes from ffmpeg stdout and put the resulting numpy array into the frame queue. No decoding logic, no processing, no conditionals. This isolation is critical to prevent pipe backpressure from stalling ffmpeg.

2. **Process thread** — pulls from frame queue, runs motion gate → YOLO → tracker → classifier → event store → annotator. One frame at a time. Classification happens inline (not a separate thread) to avoid queue complexity.

3. **Annotator thread (per camera)** — dedicated JPEG encoder. Receives annotated BGR frames from the process thread via a tiny queue, encodes to JPEG, pushes to the debug stream server. Separate thread to keep encoding off the process hot path.

**Plus a fourth thread per camera:** the **recording ffmpeg subprocess monitor** — just a watchdog that restarts the recording ffmpeg if it dies.

## Why Processing Is Inline (Not Queued Stages)

The previous draft proposed detection → queue → classifier as separate threads. Two reviewers flagged this as a threading bomb due to Coral single-session constraints.

The Frigate approach runs detection and classification **in the same thread**. One per camera. Classification only runs on NEW tracks, so it's infrequent. This:

- Eliminates the Coral lock problem (single-thread execution serializes naturally)
- Removes the queue-between-stages complexity
- Keeps detection smooth because classification of new birds takes <30ms

Two separate process threads (one per camera) still contend on the Coral USB for yard/AIY inference — we handle this via a **shared classifier with a proper lock acquire pattern** (see § Classifier).

## Components

### `pipeline/frame_capture.py` (~200 lines)

Owns the ffmpeg subprocess and the pipe drainer thread.

```python
class FrameCapture:
    def __init__(self, camera_name, rtsp_url, width=1920, height=1080, fps=5,
                 out_queue: queue.Queue):
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.width = width
        self.height = height
        self.fps = fps
        self.out_queue = out_queue  # maxsize=2
        self.proc = None
        self.reader_thread = None
        self.watchdog_thread = None
        self.stats = {"frames": 0, "dropped_oldest": 0, "ffmpeg_restarts": 0,
                      "last_frame_ms": None}

    def start(self):
        self._spawn_ffmpeg()
        self.reader_thread = threading.Thread(target=self._pipe_drain, daemon=True)
        self.reader_thread.start()
        self.watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self.watchdog_thread.start()

    def _spawn_ffmpeg(self):
        # Start with SOFTWARE decode (software first, hwaccel is opt-in after
        # benchmarks — videotoolbox + UniFi has known issues per risk review)
        # Accepts either rtsp:// URLs or file paths (for integration tests)
        input_args = self._input_args(self.rtsp_url)
        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            *input_args,
            "-vf", f"fps={self.fps}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",  # Direct BGR — saves YUV→BGR conversion later
            "-"
        ]
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            bufsize=0  # unbuffered
        )

    def _input_args(self, url):
        if url.startswith("rtsp://"):
            return ["-rtsp_transport", "tcp", "-i", url]
        else:
            # File input for tests/video replay
            return ["-re", "-stream_loop", "-1", "-i", url]

    def _pipe_drain(self):
        """Dedicated pipe reader. ONLY job: read frames as fast as possible."""
        frame_bytes = self.width * self.height * 3
        while self.proc and self.proc.poll() is None:
            data = self.proc.stdout.read(frame_bytes)
            if len(data) != frame_bytes:
                break  # EOF or partial read — watchdog will restart
            arr = np.frombuffer(data, dtype=np.uint8).reshape(
                (self.height, self.width, 3))
            frame = Frame(
                bgr=arr,
                wall_time_ms=time.time() * 1000,
                camera=self.camera_name,
                width=self.width, height=self.height,
            )
            # Drop OLDEST if queue full (non-blocking)
            if self.out_queue.full():
                try:
                    self.out_queue.get_nowait()
                    self.stats["dropped_oldest"] += 1
                except queue.Empty:
                    pass
            try:
                self.out_queue.put_nowait(frame)
                self.stats["frames"] += 1
                self.stats["last_frame_ms"] = frame.wall_time_ms
            except queue.Full:
                pass

    def _watchdog(self):
        """Restart ffmpeg if no frame in 10s."""
        while True:
            time.sleep(2)
            if self.stats["last_frame_ms"] is None:
                continue
            age_ms = time.time() * 1000 - self.stats["last_frame_ms"]
            if age_ms > 10000:
                logging.warning("[%s] ffmpeg stalled, restarting",
                                self.camera_name)
                self._restart()

    def _restart(self):
        self.proc.kill()
        self.proc.wait(timeout=5)
        self._spawn_ffmpeg()
        # Reader thread will re-attach via self.proc.stdout on next iteration
        self.stats["ffmpeg_restarts"] += 1
```

**Key decisions:**
- Software decode first, hwaccel opt-in after benchmarks
- BGR output direct from ffmpeg (saves YUV→BGR conversion in Python)
- Dedicated pipe reader thread with NO conditionals or processing — prevents backpressure
- Drop OLDEST on queue full (keeps fresh frames flowing)
- File input support for integration tests (automatic detection)

### `pipeline/motion_gate.py` (~100 lines, refactored)

Refactor the existing `motion_gate.py` to emit regions instead of a bool:

```python
class MotionGate:
    def __init__(self, threshold_pct=1.5, min_region_area=400):
        self.bg_subtractor = cv2.createBackgroundSubtractorMOG2(
            history=500, varThreshold=16, detectShadows=False)

    def regions(self, bgr_frame) -> list[tuple[int, int, int, int]]:
        """Returns list of (x1,y1,x2,y2) motion regions at full-frame resolution."""
        mask = self.bg_subtractor.apply(bgr_frame)
        # Morphological close + find contours + bounding boxes
        # Filter by min_region_area
        # Return list of boxes
        ...
```

### `pipeline/detector.py` (~150 lines)

Runs YOLO on motion regions (Frigate-style) with stationary object skipping.

```python
class BirdDetector:
    def __init__(self, yolo_model_path, stationary_track_regions_fn):
        self.yolo = YOLODetector(yolo_model_path, confidence=0.3)
        self.get_stationary = stationary_track_regions_fn

    def detect(self, frame: Frame, motion_regions: list[tuple],
               forced_full: bool) -> list[Detection]:
        """
        Run YOLO on motion crops. Skip regions that contain ONLY stationary tracks.
        Every N seconds (forced_full=True), run YOLO on the full frame as a sanity check.
        Returns detections with boxes in ORIGINAL full-frame coordinates.
        """
        if forced_full:
            return self._detect_full(frame)
        
        stationary = self.get_stationary()  # list of bounded boxes
        detections = []
        for region in motion_regions:
            if self._is_stationary_only(region, stationary):
                continue
            detections.extend(self._detect_region(frame, region))
        return detections

    def _detect_region(self, frame, region):
        x1, y1, x2, y2 = region
        # Crop, pad to YOLO input size, run inference
        crop = frame.bgr[y1:y2, x1:x2]
        raw = self.yolo.detect_numpy(crop)
        # CRITICAL: offset detection boxes back to full-frame coordinates
        return [Detection(
            box=[d.box[0] + x1, d.box[1] + y1,
                 d.box[2] + x1, d.box[3] + y1],
            confidence=d.confidence,
        ) for d in raw]
```

**Addresses arch review concern:** YOLO output coordinates are explicitly offset back to full-frame space. Unit tested.

### `pipeline/tracker.py` (~250 lines, Norfair wrapper)

Wraps `norfair.Tracker` with a Frigate-inspired distance function.

```python
import norfair
import numpy as np

class Track:
    def __init__(self, track_id, frame_time_ms):
        self.track_id = track_id
        self.species: Optional[str] = None
        self.confidence = 0.0
        self.model_source: Optional[str] = None
        self.bbox = [0, 0, 0, 0]
        self.created_at = frame_time_ms
        self.last_updated = frame_time_ms
        self.motion_history: deque = deque(maxlen=10)  # centroid positions
        self.stationary_frame_count = 0
        self.needs_classification = True
        self.classification_attempts = 0  # for retry logic

    @property
    def is_stationary(self) -> bool:
        if len(self.motion_history) < 10:
            return False
        xs = [p[0] for p in self.motion_history]
        ys = [p[1] for p in self.motion_history]
        return (max(xs) - min(xs)) < 10 and (max(ys) - min(ys)) < 10


def frigate_distance(detection: norfair.Detection,
                     tracked: norfair.TrackedObject) -> float:
    """Distance function inspired by Frigate's tracker.

    Uses centroid-x + bottom-y normalized by object size.
    Bottom-y is more stable under perspective (birds on perches).
    """
    det_w = detection.data["w"]
    det_h = detection.data["h"]
    trk_w = tracked.last_detection.data["w"]
    trk_h = tracked.last_detection.data["h"]
    
    # Centroid-x distance normalized by average width
    det_cx = detection.points[0][0]
    trk_cx = tracked.estimate[0][0]
    d_x = abs(det_cx - trk_cx) / max((det_w + trk_w) / 2, 1)
    
    # Bottom-y distance normalized by average height
    det_by = detection.points[0][1] + det_h / 2  # points[0] is centroid
    trk_by = tracked.estimate[0][1] + trk_h / 2
    d_y = abs(det_by - trk_by) / max((det_h + trk_h) / 2, 1)
    
    return d_x + d_y


class BirdTracker:
    def __init__(self):
        self.tracker = norfair.Tracker(
            distance_function=frigate_distance,
            distance_threshold=1.0,  # tuned per frigate_distance formula
            hit_counter_max=15,      # expire after 15 frames without match
            initialization_delay=1,
        )
        self.tracks: dict[int, Track] = {}

    def update(self, detections: list[Detection],
               frame_time_ms: float) -> TrackerOutput:
        norfair_dets = [
            norfair.Detection(
                points=np.array([[
                    (d.box[0] + d.box[2]) / 2,  # cx
                    (d.box[1] + d.box[3]) / 2,  # cy
                ]]),
                scores=np.array([d.confidence]),
                data={
                    "box": d.box,
                    "w": d.box[2] - d.box[0],
                    "h": d.box[3] - d.box[1],
                },
            )
            for d in detections
        ]
        tracked_objects = self.tracker.update(detections=norfair_dets)
        
        new_tracks = []
        active_tracks = []
        expired_track_ids = set(self.tracks.keys())
        
        for tobj in tracked_objects:
            tid = tobj.id
            if tid not in self.tracks:
                # New track
                track = Track(tid, frame_time_ms)
                track.bbox = tobj.last_detection.data["box"]
                track.confidence = float(tobj.last_detection.scores[0])
                self.tracks[tid] = track
                new_tracks.append(track)
            else:
                track = self.tracks[tid]
                track.bbox = tobj.last_detection.data["box"]
                track.confidence = float(tobj.last_detection.scores[0])
                track.last_updated = frame_time_ms
                expired_track_ids.discard(tid)
            
            # Update motion history for stationary detection
            cx = (track.bbox[0] + track.bbox[2]) / 2
            cy = (track.bbox[1] + track.bbox[3]) / 2
            track.motion_history.append((cx, cy))
            active_tracks.append(track)
        
        # Remove expired tracks from our dict
        expired = [self.tracks.pop(tid) for tid in expired_track_ids]
        
        return TrackerOutput(
            active=active_tracks,
            new=new_tracks,
            expired=expired,
            frame_time_ms=frame_time_ms,
        )

    def stationary_regions(self) -> list[tuple]:
        """Returns bboxes of tracks that are currently stationary."""
        return [tuple(t.bbox) for t in self.tracks.values() if t.is_stationary]
```

### `pipeline/classifier.py` (~300 lines) — Smart B with retry

Addresses ALL the classifier blockers from both reviews:

```python
CONFIDENT = 0.60
UNCERTAIN_LOW = 0.30
CORAL_ACQUIRE_TIMEOUT = 5.0  # how long to wait FOR the lock (not during invoke)
MAX_CLASSIFICATION_ATTEMPTS = 3  # retry unlabeled tracks this many times

class ClassificationResult:
    def __init__(self, species, confidence, model_source, should_retry):
        self.species = species
        self.confidence = confidence
        self.model_source = model_source
        self.should_retry = should_retry  # True if couldn't get Coral — retry on next frame


class SmartClassifier:
    def __init__(self, yard_model_path, yard_labels_path,
                 aiy_model_path, aiy_labels_path,
                 regional_species, audio_db_path):
        self.yard = YardClassifier(yard_model_path, yard_labels_path)
        self.aiy = SpeciesClassifier(aiy_model_path, aiy_labels_path,
                                     regional_species=regional_species)
        self.audio_db_path = audio_db_path
        self._coral_lock = threading.Lock()
        self.stats = {"yard": 0, "aiy": 0, "both_agree": 0,
                      "audio_confirmed": 0, "unlabeled": 0,
                      "lock_timeouts": 0, "retries": 0}

    def classify(self, crop_pil, frame_time_ms, camera) -> ClassificationResult:
        # Try to acquire the lock — if we can't, return should_retry=True
        # (note: we CANNOT interrupt interpreter.invoke() once it starts —
        # the timeout is on lock acquisition, not on inference itself.
        # Inference is ~10-30ms so this is fine in practice.)
        got_lock = self._coral_lock.acquire(timeout=CORAL_ACQUIRE_TIMEOUT)
        if not got_lock:
            self.stats["lock_timeouts"] += 1
            return ClassificationResult(None, 0, None, should_retry=True)
        
        try:
            # Path 1: Yard model confident → use it immediately
            yard = self._run_yard(crop_pil)
            if yard and yard.confidence >= CONFIDENT:
                self.stats["yard"] += 1
                return ClassificationResult(
                    yard.species, yard.confidence, "yard", should_retry=False)

            # Path 2: Yard useless → AIY only
            if not yard or yard.confidence < UNCERTAIN_LOW:
                aiy = self._run_aiy(crop_pil)
                if aiy and aiy.confidence >= CONFIDENT:
                    self.stats["aiy"] += 1
                    return ClassificationResult(
                        aiy.species, aiy.confidence, "aiy", should_retry=False)
                self.stats["unlabeled"] += 1
                return ClassificationResult(None, 0, None, should_retry=False)

            # Path 3: Yard uncertain, run AIY for comparison
            aiy = self._run_aiy(crop_pil)
            if not aiy:
                self.stats["unlabeled"] += 1
                return ClassificationResult(None, 0, None, should_retry=False)

            # Agreement = same species, take higher confidence
            if aiy.species == yard.species:
                self.stats["both_agree"] += 1
                return ClassificationResult(
                    yard.species,
                    max(yard.confidence, aiy.confidence),
                    "both_agree", should_retry=False)

            # Disagreement → audio cross-check
            audio_hit = self._audio_lookup(camera, frame_time_ms)
            if audio_hit and audio_hit in (yard.species, aiy.species):
                self.stats["audio_confirmed"] += 1
                return ClassificationResult(
                    audio_hit,
                    max(yard.confidence, aiy.confidence),
                    "audio_confirmed", should_retry=False)

            # No confident answer → don't label
            self.stats["unlabeled"] += 1
            return ClassificationResult(None, 0, None, should_retry=False)
        finally:
            self._coral_lock.release()

    def _audio_lookup(self, camera, frame_time_ms) -> Optional[str]:
        """Query birdnet_local.db for detections within ±5s on that camera's location.
        Indexed query, <5ms. Returns species name or None.
        """
        # Fast indexed SQL — join on camera location and time window
        ...


# In the process thread, retry logic for tracks:
def classify_new_tracks(process_ctx, tracker_output):
    for track in tracker_output.new + [t for t in tracker_output.active
                                         if t.needs_classification]:
        if track.classification_attempts >= MAX_CLASSIFICATION_ATTEMPTS:
            track.needs_classification = False
            continue
        
        crop = crop_bird(process_ctx.current_frame.bgr, track.bbox)
        pil = Image.fromarray(crop)
        result = process_ctx.classifier.classify(
            pil, process_ctx.current_frame.wall_time_ms, process_ctx.camera)
        
        track.classification_attempts += 1
        if result.should_retry:
            # Coral busy — try again on next frame
            continue
        
        # Got a final answer (could be None = unlabeled)
        track.species = result.species
        track.confidence = result.confidence
        track.model_source = result.model_source
        track.needs_classification = False
```

**Key changes from earlier draft:**
- `acquire(timeout=...)` explicitly, not `with`
- `should_retry` field on result — distinguishes "Coral busy" from "actually unlabeled"
- Retry logic in the process thread: tracks that got `should_retry=True` stay `needs_classification=True` and get retried on next frame, up to `MAX_CLASSIFICATION_ATTEMPTS`
- Audio path uses indexed query on existing `birdnet_local.db`, NOT live BirdNET inference

### `pipeline/event_store.py` (~250 lines)

SQLite WAL with explicit pragmas and batch writes. **Source of truth is in-memory tracker state; DB is async audit log.**

```python
class EventStore:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=2000")
        self._init_schema()
        self._event_batch: list = []
        self._batch_lock = threading.Lock()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._flush_thread.start()

    def _flush_loop(self):
        while True:
            time.sleep(0.5)  # Flush every 500ms max
            self._flush()

    def _flush(self):
        with self._batch_lock:
            if not self._event_batch:
                return
            batch = self._event_batch
            self._event_batch = []
        self.conn.executemany(INSERT_EVENT_SQL, batch)
        self.conn.commit()

    def write_event(self, camera, frame_time_ms, track_id, species,
                    confidence, model_source, bbox, is_new):
        with self._batch_lock:
            self._event_batch.append((
                camera, int(frame_time_ms), track_id, species,
                confidence, model_source, json.dumps(bbox), int(is_new)
            ))
            if len(self._event_batch) >= 50:
                # Flush inline if batch is full
                pass  # Flush happens in background thread

    def write_track_summary(self, track: Track): ...
    def query_events(self, camera, start_ms, end_ms) -> list: ...
    def query_tracks(self, **filters) -> list: ...
    def daily_checkpoint(self):
        """Run once per day: PRAGMA wal_checkpoint(TRUNCATE)."""
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    def prune_events(self, older_than_ms): ...
```

**Schema (unchanged from previous revision, with explicit clarification):**

```sql
-- frame_time = the actual frame's wall-clock time (unix ms), NOT the DB write time
CREATE TABLE pipeline_events (
  camera TEXT NOT NULL,
  frame_time INTEGER NOT NULL,
  track_id INTEGER NOT NULL,
  species TEXT,
  confidence REAL,
  model_source TEXT,
  bbox_json TEXT NOT NULL,
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
  best_keeper_path TEXT,
  motion_pct REAL
);
CREATE INDEX idx_tracks_species ON pipeline_tracks(species, start_time);
CREATE INDEX idx_tracks_duration ON pipeline_tracks(camera, end_time, start_time);
```

**Retention (simplified per arch review):** flat 7-day retention on `pipeline_events`. `pipeline_tracks` kept indefinitely (small, <10 MB/year). No tiered retention in v1. Nice-to-have for v2.

### `pipeline/annotator.py` (~200 lines)

**Per-camera thread**. Receives annotated frame requests from the process thread, draws labels, encodes to JPEG, pushes to debug stream.

```python
class FrameAnnotator:
    def __init__(self, camera_name, debug_stream, out_width=960, out_height=540):
        self.camera_name = camera_name
        self.debug_stream = debug_stream
        self.out_width = out_width   # Default 960x540 (per risk review)
        self.out_height = out_height
        self.queue = queue.Queue(maxsize=2)  # drop oldest if annotator falls behind
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def submit(self, frame: Frame, tracks: list[Track]):
        """Called by process thread. Non-blocking."""
        if self.queue.full():
            try: self.queue.get_nowait()
            except queue.Empty: pass
        try:
            self.queue.put_nowait((frame, tracks))
        except queue.Full:
            pass

    def _loop(self):
        while True:
            frame, tracks = self.queue.get()
            jpeg_bytes = self._annotate(frame.bgr, tracks)
            self.debug_stream.push(self.camera_name, jpeg_bytes, frame.wall_time_ms)

    def _annotate(self, bgr, tracks):
        # 1. Downscale to out_width x out_height
        out = cv2.resize(bgr, (self.out_width, self.out_height),
                         interpolation=cv2.INTER_LINEAR)
        scale_x = self.out_width / bgr.shape[1]
        scale_y = self.out_height / bgr.shape[0]

        # 2. Draw labels PER TRACK — anchored above the bird (not fixed Y)
        for track in tracks:
            label = track.species or "·"  # muted chip for unlabeled
            if track.species is None:
                # Subtle muted marker
                color = (128, 128, 128)
            else:
                color = (128, 222, 74)  # green
            
            x1, y1, x2, y2 = [int(v * s) for v, s in zip(track.bbox,
                              [scale_x, scale_y, scale_x, scale_y])]
            cx = (x1 + x2) // 2
            label_y = max(20, y1 - 8)  # above the bird, clamped to visible
            
            # Draw rounded pill + text
            self._draw_label_pill(out, label, cx, label_y, color)
            
            # "both_agree" double-check badge
            if track.model_source == "both_agree":
                self._draw_checkmark(out, label, cx, label_y)

        # 3. Encode JPEG quality 75
        _, jpeg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 75])
        return jpeg.tobytes()

    def _draw_label_pill(self, img, text, cx, cy, color): ...
    def _draw_checkmark(self, img, text, cx, cy): ...
```

**Addresses UX blockers:**
- Labels anchored above each bird individually (not fixed Y)
- Muted "·" chip for unlabeled tracks (not invisible)
- `both_agree` gets a subtle checkmark badge
- Default 960x540 output (per risk review — cuts encoding CPU 4x)
- Queue drops oldest → never blocks process thread

### `pipeline/debug_stream.py` (~250 lines)

WebSocket server using `websockets.sync.server` (threading-compatible).

```python
from websockets.sync.server import serve, ServerConnection

class DebugStream:
    def __init__(self, port=8101):
        self.port = port
        self.clients: dict[str, list[ClientState]] = {"feeder": [], "ground": []}
        self._lock = threading.Lock()
        self.latest_frame: dict[str, bytes] = {}  # poster frames for new connections
        self.stats = {"active_clients": 0, "frames_sent": 0, "dropped_clients": 0}

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        with serve(self._handle_client, "0.0.0.0", self.port) as server:
            server.serve_forever()

    def _handle_client(self, websocket: ServerConnection):
        # Parse camera from path: /debug-stream/feeder or /debug-stream/ground
        path = websocket.request.path
        if "/feeder" in path:
            camera = "feeder"
        elif "/ground" in path:
            camera = "ground"
        else:
            websocket.close(1002, "Unknown camera")
            return
        
        client = ClientState(websocket, camera)
        with self._lock:
            self.clients[camera].append(client)
            self.stats["active_clients"] += 1

        # Send poster frame immediately if we have one
        poster = self.latest_frame.get(camera)
        if poster:
            try: websocket.send(poster)
            except Exception: pass

        try:
            # Keep connection alive, read any client messages (ping/pong handled)
            for _ in websocket:
                pass
        except Exception:
            pass
        finally:
            with self._lock:
                self.clients[camera].remove(client)
                self.stats["active_clients"] -= 1

    def push(self, camera: str, jpeg_bytes: bytes, frame_time_ms: float):
        """Called by annotator threads. Broadcasts to all clients for that camera."""
        self.latest_frame[camera] = jpeg_bytes  # always save as poster
        with self._lock:
            clients = list(self.clients[camera])
        
        for client in clients:
            try:
                # Non-blocking: if client is slow, drop this frame for them
                if client.is_slow():
                    continue
                client.send(jpeg_bytes)
                self.stats["frames_sent"] += 1
            except Exception:
                client.mark_failed()
                self.stats["dropped_clients"] += 1
```

**Addresses UX + arch concerns:**
- Poster frame sent on connect (no black square on first load)
- Per-client slow detection (dropped independently)
- Uses threading-compatible `websockets.sync.server` (not asyncio mismatch)
- `latest_frame` is always updated so reconnect is instant
- Debug stream ALWAYS runs, even if classifier/detection is degraded

### `pipeline/hls_recorder.py` (~120 lines)

**Completely rewritten — uses dedicated ffmpeg subprocess, not go2rtc.**

```python
class HlsRecorder:
    def __init__(self, camera_name, rtsp_url, output_dir):
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.proc = None
        self.watchdog_thread = None
        self.stats = {"chunks_written": 0, "restarts": 0, "last_chunk_ms": None}

    def start(self):
        self._spawn_ffmpeg()
        self.watchdog_thread = threading.Thread(target=self._watchdog, daemon=True)
        self.watchdog_thread.start()

    def _spawn_ffmpeg(self):
        playlist = self.output_dir / "live.m3u8"
        segment_pattern = self.output_dir / "seg_%Y%m%d-%H%M%S.ts"
        cmd = [
            "ffmpeg",
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-c", "copy",  # re-mux only, no decode
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "0",  # keep all segments in playlist
            "-hls_flags", "second_level_segment_index+append_list+program_date_time",
            "-strftime", "1",
            "-hls_segment_filename", str(segment_pattern),
            str(playlist),
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)

    def _watchdog(self):
        while True:
            time.sleep(5)
            if self.proc.poll() is not None:
                self.stats["restarts"] += 1
                time.sleep(2)
                self._spawn_ffmpeg()

    @staticmethod
    def cleanup_old_chunks(hls_root: Path, retention_days=7):
        cutoff = time.time() - retention_days * 86400
        for camera_dir in hls_root.iterdir():
            for seg in camera_dir.glob("seg_*.ts"):
                if seg.stat().st_mtime < cutoff:
                    seg.unlink()
```

**~1% CPU (pure copy). Independent from detection pipeline. Can fail without affecting live view.**

### `bird_pipeline.py` (new orchestrator, ~200 lines)

Simplified top-level. Starts shared services, then capture + process + annotator + recorder per camera.

```python
def main():
    event_store = EventStore(DB_PATH)
    classifier = SmartClassifier(
        yard_model_path=MODELS_DIR / "yard_model.tflite",
        yard_labels_path=MODELS_DIR / "yard_model_labels.txt",
        aiy_model_path=MODELS_DIR / "aiy_birds_v1_edgetpu.tflite",
        aiy_labels_path=MODELS_DIR / "inat_bird_labels.txt",
        regional_species=load_regional_species(),
        audio_db_path=BIRDNET_DB_PATH,
    )
    debug_stream = DebugStream(port=8101)
    debug_stream.start()
    health_server = HealthServer(port=8100)
    health_server.start()

    camera_runners = []
    for name, url in CAMERAS.items():
        frame_q = queue.Queue(maxsize=2)
        capture = FrameCapture(name, url, out_queue=frame_q)
        annotator = FrameAnnotator(name, debug_stream)
        recorder = HlsRecorder(name, url, HLS_DIR / name)
        process = CameraProcessThread(
            name=name,
            frame_queue=frame_q,
            classifier=classifier,
            event_store=event_store,
            annotator=annotator,
            motion_gate=MotionGate(),
            detector=BirdDetector(YOLO_MODEL),
            tracker=BirdTracker(),
            health=health_server,
        )
        
        # Start everything, catch per-camera failures so one cam's issue
        # doesn't prevent the other from starting
        try:
            capture.start()
            annotator.start()
            process.start()
            recorder.start()
            camera_runners.append((capture, process, annotator, recorder))
        except Exception as e:
            logging.error("[%s] Failed to start: %s", name, e)

    # Daily maintenance + prune loop
    start_prune_loop(event_store, HLS_DIR)
    
    # Graceful shutdown
    signal.signal(signal.SIGTERM, lambda *_: shutdown(camera_runners))
    signal.signal(signal.SIGINT,  lambda *_: shutdown(camera_runners))

    # Block forever
    wait_for_shutdown()
```

### Dashboard changes (`dashboard/index.html`)

**Replace the overlay canvas approach entirely.**

1. **New Det mode** uses the debug WebSocket:
   ```js
   const ws = new WebSocket(`ws://${location.host}/api/debug-stream/${camera}`);
   ws.binaryType = "blob";
   ws.onmessage = async (ev) => {
     const blob = ev.data;
     const img = await createImageBitmap(blob);
     ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
     img.close();
     lastFrameTime = performance.now();
   };
   ```

2. **Live freshness indicator**: pulsing "LIVE" dot. Stops pulsing if `lastFrameTime` is >2s old. Shows "Reconnecting..." toast after 3s.

3. **Auto-reconnect with exponential backoff** (1s → 2s → 4s → 30s cap).

4. **Cellular mode detection**: check `navigator.connection.effectiveType` if available. If `'2g' | '3g' | 'slow-2g'`, append `?mode=mobile` to the WS URL. The server-side annotator can downscale further (480x270) and lower quality when the URL has `mode=mobile`.

5. **Tab backgrounded**: close the WebSocket when `document.visibilityState === 'hidden'`. Reopen on visibility return. Saves data + battery.

6. **Remove "Old Det" / "New Det" toggle** — during Phase 3 cutover, the toggle is hidden from the UI. Developer rollback via `?old=1` URL param for 1 week, then the old path is deleted in Phase 4.

7. **Scrubbing** (historical playback) uses an HTML5 `<video>` element with the HLS playlist. Labels are overlaid from the event store via a separate SSE or REST call. Precision isn't critical for scrubbing (200ms tolerance is fine).

8. **"Best visit today" card** (new, UX review): on dashboard load, query `/api/pipeline/tracks?min_duration=5&sort=confidence&limit=1` and show a single HLS clip inline. Minimal UI, zero extra spec burden, addresses the "recording with no user surface" blocker.

## Data Flow (Worked Example)

1. Feeder camera captures frame at wall-time T=1712700000000 (unix ms)
2. `FrameCapture._pipe_drain()` reads `1920*1080*3` bytes from ffmpeg stdout → numpy BGR array
3. Capture thread puts `Frame(bgr, wall_time=T, camera='feeder')` into frame_queue
4. `CameraProcessThread` reads from queue, passes to motion_gate
5. Motion gate detects motion region `(400,200)→(800,600)`
6. Detector runs YOLO on the CROPPED region → finds bird at `(50,50)→(150,150)` in crop-space → offsets to `(450,250)→(550,350)` in full-frame space
7. Tracker.update(): no matching existing track → creates new Track(id=42, species=None, needs_classification=True)
8. classify_new_tracks(): acquires Coral lock (succeeds), runs yard model → Black-capped Chickadee, 0.82 → Path 1 → returns ClassificationResult('Black-capped Chickadee', 0.82, 'yard', should_retry=False)
9. Tracker sets track.species, needs_classification=False
10. EventStore.write_event(...) — appended to batch, flushed within 500ms
11. Annotator.submit(frame, [track42]) — non-blocking, goes into annotator's queue
12. Annotator thread: downscales frame to 960x540, draws "Black-capped Chickadee" pill ABOVE the bird (at adjusted coordinates), encodes JPEG quality 75
13. DebugStream.push('feeder', jpeg_bytes) — broadcasts to all connected clients for 'feeder' camera
14. Dashboard WebSocket message handler: decodes blob → `createImageBitmap` → `ctx.drawImage` → user sees the frame WITH the label in the right place
15. **End-to-end latency: ~500-800ms from camera to browser**

## Failure Modes & Recovery

### Failures that keep the system running

- **ffmpeg-detect crashes** → capture thread's watchdog restarts ffmpeg. Brief freeze on dashboard (<10s). Debug stream poster frame is shown during reconnect.
- **ffmpeg-record crashes** → recorder watchdog restarts. Detection continues normally. Brief gap in HLS chunks.
- **go2rtc crashes** → no effect on either detection or recording (we don't use go2rtc at all in this architecture — it was in earlier drafts).
- **Coral USB busy** → classify returns `should_retry=True`. Track stays `needs_classification=True`, retries on next frame, up to `MAX_CLASSIFICATION_ATTEMPTS`. User sees the bird as unlabeled "·" chip until classification succeeds.
- **Coral USB disconnected** → both yard and AIY fail. All tracks get `·` chips. Health shows `coral_available: false`. Debug stream still flowing.
- **Audio DB unavailable** → Smart B skips Path 4 (audio cross-check), falls back to Path 3's disagreement as unlabeled. Logged as degraded.
- **WebSocket client slow/disconnected** → drops only that client's frame. Other clients unaffected. Server pushes to everyone else normally.
- **Dashboard loses connection** → auto-reconnect with backoff + "Reconnecting..." toast. Poster frame shows on reconnect until fresh frame arrives.
- **Tab backgrounded** → dashboard closes WS to save battery; reconnects on foreground.
- **Disk full for HLS chunks** → recorder ffmpeg fails, watchdog keeps restarting. Health shows `hls_disk_full: true`. Cleanup runs hourly + emergency prune at 95% disk. Detection unaffected.

### Failures that need intervention

- **Camera unreachable >60s in daytime** → health marks camera as `broken`. Dashboard shows red alert. User action required.
- **Pipeline process crash** → LaunchAgent restarts. Clean startup (no SHM cleanup needed since we use threading only).
- **SQLite DB corruption** → pipeline logs error, exits. LaunchAgent restarts. If corruption persists, manual DB repair required. Detection continues writing to in-memory tracker until DB recovers (events lost during outage, tracks not lost).

## Testing Strategy

### Unit tests

- `test_frame_capture.py`: ffmpeg spawn, pipe drain with simulated slow consumer, watchdog restart, file vs RTSP input detection
- `test_motion_gate.py`: extend existing with region output assertions
- `test_detector.py`: region detection + coordinate offset correctness, stationary skip logic
- `test_tracker.py`: Norfair wrapper, frigate_distance correctness, stationary detection, track lifecycle
- `test_classifier.py`: all four Smart B paths, lock acquisition timeout → should_retry=True, retry counter exhaustion
- `test_event_store.py`: batched writes, query correctness, prune retention, WAL checkpoint
- `test_annotator.py`: label position per track, downscaling, muted chip for unlabeled, backpressure drops oldest
- `test_debug_stream.py`: WebSocket broadcast, poster frame, slow client drop, reconnect
- `test_hls_recorder.py`: ffmpeg spawn, watchdog restart, cleanup_old_chunks retention

### Integration tests

- `test_pipeline_e2e.py`: feed test videos via ffmpeg file input through full stack
  - `1m-empty.mp4` → 0 events
  - `chickadee-finch-downy.mp4` → expected species counts
  - `hairy-chick-tufted.mp4` → three species, no track collisions
  - `lots of birds.mp4` → multiple concurrent tracks with distinct track_ids
  - All tests assert: no crashes, no leaked subprocesses, no growing memory

### Visual test

- `test_dashboard_live.py`: Playwright. Starts pipeline with test video input. Opens dashboard New Det view. Verifies:
  - WebSocket receives frames at ≥4 FPS
  - Frames render correctly
  - Labels anchor above birds (sanity check via image diff)
  - No JavaScript errors
  - Poster frame appears on initial load
  - Reconnect works after simulated WS close

### Benchmark test

- `bench_pipeline.py`: 60s test video through pipeline. Asserts:
  - YOLO ms/frame p50 < 80ms, p99 < 150ms
  - Classifier ms/new-bird p50 < 30ms
  - Tracker ms/frame < 5ms
  - Capture FPS ≥ 4.5 (target 5)
  - Annotator ms/frame < 50ms at 960x540
  - Debug stream end-to-end latency < 1000ms
  - Peak memory < 500 MB
  - Zero ffmpeg restarts
  - Zero uncaught exceptions

### Pipe saturation test (new — per risk review)

- `test_pipe_saturation.py`: Run FrameCapture alone for 10 minutes with a fake slow consumer. Verify:
  - No pipe backpressure (ffmpeg does not stall)
  - `dropped_oldest` stat increments correctly
  - `last_frame_ms` stays within 300ms of current time
  - Zero ffmpeg restarts from backpressure stalls

## Health Monitoring

### Layer 1: Per-component health dict

```python
health = {
  "pipeline": {
    "feeder": {
      "capture":        {"fps": 4.9, "dropped_oldest": 0, "ffmpeg_restarts": 0, "last_frame_age_ms": 210},
      "detector":       {"yolo_ms_avg": 75, "yolo_ms_p99": 120, "dets_per_min": 47, "stationary_skipped_pct": 62},
      "tracker":        {"active_tracks": 3, "stationary_tracks": 1, "tracks_per_min": 8},
      "classifier":     {"yard": 41, "aiy": 4, "both_agree": 2, "audio": 1, "unlabeled": 0, "lock_timeouts": 0, "retries": 0},
      "annotator":      {"fps_out": 4.8, "avg_encode_ms": 32, "dropped_oldest": 0},
      "recorder":       {"chunks_written": 1800, "last_chunk_age_s": 1.8, "restarts": 0, "disk_free_gb": 180},
      "status":         "ok"
    },
    "ground": { ... }
  },
  "shared": {
    "debug_stream": {"active_clients": 1, "frames_sent": 4320, "dropped_clients": 0},
    "event_store":  {"events_written": 12847, "db_size_mb": 48, "batch_queue_depth": 3, "wal_size_mb": 8},
  },
  "overall": "ok"
}
```

### Layer 2: Health endpoint

`GET /api/pipeline/health` returns the above dict. Top-level `status`:

- **ok**: all green
- **degraded**: FPS 3–4, YOLO p99 > 150ms, classifier lock timeouts > 5/min, recorder restarts > 3/h
- **broken**: camera unreachable > 60s in daytime, FPS < 3 for > 2min, classifier retry exhaustion on > 20% of tracks, coral unavailable > 5min, event_store write errors

### Layer 3: Dashboard System Status panel

Two modes:

- **Friendly** (default for Mom): single line chip — "All cameras good" / "Running a bit slow" / "Feeder offline, reconnecting..."
- **Technical** (click to expand): full health dict as formatted JSON

Compact at top of dashboard. Green = invisible (just a small ✓ icon). Degraded = amber with summary. Broken = red alert, auto-expands.

### Pipeline event log

Structured JSON lines at `~/bird-snapshots/logs/pipeline-events.log`, rotated daily, 30-day retention:

```json
{"ts":"2026-04-10T08:22:41.123","level":"info","event":"track_start","camera":"feeder","track_id":42,"species":"Black-capped Chickadee","confidence":0.82,"model_source":"yard"}
{"ts":"2026-04-10T08:22:45.456","level":"info","event":"track_end","camera":"feeder","track_id":42,"species":"Black-capped Chickadee","duration_s":4.3,"peak_confidence":0.89,"num_frames":22}
{"ts":"2026-04-10T08:23:01.789","level":"warn","event":"coral_lock_timeout","camera":"ground","track_id":51,"attempts":2}
```

## Migration Strategy

**Two phases, no simultaneous dual-pipeline operation on either camera.** Old pipeline and new pipeline cannot share Coral USB across processes.

**Phase 1: Build and validate in isolation**
- New files in `pipeline/` subdir
- New DB at `pipeline.db`
- All unit and integration tests pass
- Benchmark suite passes on all assertions
- `bird_pipeline.py` continues to work unchanged
- Pipe saturation test passes

**Phase 2: Cutover (both cameras at once)**
- Disable old `com.vives.bird-pipeline.plist` LaunchAgent
- Install new `com.vives.bird-pipeline.plist` pointing at the new orchestrator
- Enable "New Det" mode as default on the dashboard
- Keep old code path accessible via `?old=1` URL param for 1 week as developer rollback
- Monitor health for 48h

**Phase 3: Cleanup**
- Old `bird_pipeline.py`, `bird_tracker.py`, `yard_classifier.py` (if fully absorbed) deleted
- Dashboard `?old=1` path removed
- Documentation updated

**No "ground cam new, feeder cam old" phase.** The risk review correctly identified that the old and new pipelines would deadlock on the Coral USB across processes.

## Success Criteria

The new pipeline ships when ALL of these are true:

1. **Unit tests**: 100% passing
2. **Integration tests**: all 5 Protect test videos produce expected species counts within ±10%
3. **Benchmarks**: YOLO p99 < 150ms, classifier < 30ms, capture FPS ≥ 4.5, memory < 500 MB, zero ffmpeg restarts in 60s, zero exceptions
4. **Pipe saturation test**: passes 10-minute run with zero backpressure-triggered restarts
5. **Visual test**: Playwright confirms labels anchored per-track above birds, no stale labels, reconnect works
6. **Zero "unidentified bird" labels** anywhere (uncertain tracks show "·" chip)
7. **Track smoothness**: subjective approval from David on test videos
8. **Health shows green** for 1 hour of daytime live operation
9. **Clip query works**: `pipeline_tracks` query for "Downy Woodpecker > 5s" returns real chunk paths
10. **Best visit today card** renders on the dashboard
11. **SHM lifecycle test**: 10 kill -9 restarts with zero leaked resources (trivially passes — we use no SHM)
12. **Mobile smoke test**: dashboard loads and plays debug stream on an iPhone Safari browser over cellular

## What We're NOT Building (YAGNI)

- **Auto-editor for highlight reels** — separate spec, builds on event store + HLS chunks
- **Clip browser UI** — enabled by event store, deliberately scoped out (minimal "best visit today" card included as a hook)
- **Per-species confidence tuning UI** — v3
- **WebRTC live stream** — MJPEG-over-WS is enough
- **Hardware-accelerated ffmpeg by default** — software decode first, hwaccel opt-in after benchmarks prove it's worth the risk
- **Multi-TPU support** — single Coral is fine
- **Shared memory** — explicitly rejected
- **Prometheus/Grafana** — dashboard IS the alerting
- **Cloud backup of chunks** — local only
- **Tiered HLS retention** — flat 7-day retention in v1, tiered is v2 if needed

## Dependencies & Constraints

- **Python 3.9** in `venv-coral` (pycoral)
- **numpy < 2.0** (pycoral compiled against numpy 1.x)
- **ffmpeg 8.0.1** (already installed at `/usr/local/bin/ffmpeg`)
- **norfair 2.3.0** (new dep — verified Python 3.9 + numpy < 2.0 compatible)
  - Pulls: `filterpy 1.4.5`, `rich >= 9.10 < 15.0`, `scipy >= 1.5.4`
  - Verified: no torch/tensorflow transitive imports
- **websockets >= 12.0** (new dep — use `websockets.sync.server.serve()` for threading compatibility)
- **opencv-python 4.13** (already installed)
- **go2rtc remains installed** for on-demand HLS playback during scrubbing (unchanged from today)
- **No Docker for the pipeline itself** — runs as LaunchAgent

## UX Details (from review — explicit inclusions)

- **Label anchoring per-track**: labels are drawn above each track individually, not at a fixed Y. Collision avoidance: if two tracks are close together vertically, offset labels vertically.
- **Muted chip for unlabeled tracks**: `·` symbol in gray (128,128,128) color. "Something is there, we don't know what" is better than nothing.
- **"Both agree" checkmark**: subtle double-check badge next to labels where `model_source == 'both_agree'`.
- **Live freshness indicator**: pulsing "LIVE" dot that stops pulsing if `lastFrameTime > 2s` ago.
- **Reconnecting toast**: appears after 3s of no frames with exponential backoff reconnect.
- **Poster frame on connect**: server sends last frame immediately so first-load is never a black square.
- **Cellular mode**: auto-detect via `navigator.connection.effectiveType`; server downscales to 480x270 at quality 60 when mobile mode is requested.
- **Tab backgrounded**: close WebSocket to save battery; reconnect on foreground.
- **"Best visit today" card**: simple dashboard component that queries the event store for the longest high-confidence track of the day and plays the corresponding HLS clip inline.
- **Old/New Det toggle removed from UI** on cutover; `?old=1` URL param available for 1 week rollback.

## Out-of-Scope Items That Should Follow (next specs)

- Auto-clip compilation ("show me best Downy visits this week, crossfaded")
- Full clip browser UI with filters
- Pipeline-driven retraining (use `pipeline_events` as a labeled data source)
- Hardware-accelerated ffmpeg benchmark + opt-in config
- Multi-client bandwidth adaptation beyond the simple cellular detection

## References

- Frigate source: https://github.com/blakeblackshear/frigate
- Frigate video pipeline: https://deepwiki.com/blakeblackshear/frigate/4-video-processing-pipeline
- Norfair tracker: https://github.com/tryolabs/norfair
- ffmpeg HLS muxer: https://ffmpeg.org/ffmpeg-formats.html#hls
- websockets sync.server: https://websockets.readthedocs.io/en/stable/reference/sync/server.html
- Current pipeline (to be replaced): `bird_pipeline.py`
- Current tracker (to be replaced): `bird_tracker.py`
- Current inference library (reused): `bird_inference.py`
- Current classifier wrappers (reused): `yard_classifier.py`

## Review Audit Trail

This spec incorporates findings from three parallel review cycles on 2026-04-10:

1. **Architecture review** (thread safety, data flow, shared memory, error propagation)
2. **UX / Product review** (mom test, mobile experience, label anchoring, delight opportunities)
3. **Implementation risk review** (macOS gotchas, Norfair compatibility, ffmpeg pipe throughput, go2rtc reality check)

**Key revisions applied:**
- Abandoned shared memory entirely (blocker #1, #2 from arch; #5 from risk)
- Rewrote HLS recording to use dedicated ffmpeg subprocess (blocker #10 from risk)
- Fixed Coral lock semantics with explicit `acquire(timeout=...)` and `should_retry` (blocker #4 from risk; #2 from arch)
- Removed Phase 2 "ground new, feeder old" migration (blocker #6 from risk; #3 from arch)
- Added explicit YOLO coordinate offset specification (blocker #8 from arch)
- Added `·` muted chip for unlabeled tracks (UX blocker #6)
- Changed labels from "fixed Y" to "per-track anchored above bird" (UX blocker #2)
- Added cellular mode, poster frame, reconnect toast, tab-background pause (UX improvements #5, #10)
- Added "best visit today" card to give recording a user surface (UX blocker #4)
- Added dedicated pipe drain thread spec (blocker #4 from risk)
- Added software-decode-first, hwaccel-opt-in guidance (issue #5 from risk)
- Added explicit SQLite WAL pragmas (issue #9 from risk)
- Added pipe saturation test (new requirement from risk review)
- Pinned all dependency versions (per risk review #6, #7)
- Clarified event store is async audit log; in-memory tracker is source of truth (arch concern #5)
- Added explicit `frame_time_ms` definition (event wall-clock, not write time) (risk #9)
