> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Unified Bird Pipeline — Design Spec (v3 — post-review + PoC validated)

## Problem

Three separate processes do overlapping work:
- `capture_snapshots.py` — polls CloudKey for frames, saves to disk
- `classify.py` — reads saved frames from disk, runs YOLO + AIY, saves to species folders
- `live_detector.py` — polls go2rtc for frames, runs YOLO + AIY, broadcasts SSE (broken/laggy)

This results in: 15+ second delay from bird landing to label showing, 84K wasted frames saved to disk, stale snapshot latency, and the live detector silently dying for 5 days.

## Goal

One process. Bird lands → bounding box with species label appears on the live feed in <500ms. Box tracks the bird as it moves. Multiple birds tracked simultaneously. Best frame per visit saved for review. No wasted frames on disk.

## PoC Results (validated April 3, 2026)

```
Source: go2rtc local RTSP restream (rtsp://127.0.0.1:8554/feeder-main)
Codec: H.264, 1920x1080
Decode FPS: 4.8 (plenty for 3 FPS target)
frame.to_image(): 12.8ms avg
PIL to numpy: 13.1ms avg
Total per-frame overhead: ~26ms
YOLO inference: ~100ms (CoreML)
Classification: ~5ms (ONNX)
End-to-end per frame: ~131ms
At 3 FPS: 393ms/sec compute — well within budget
```

## Architecture

```
go2rtc local RTSP restream (rtsp://127.0.0.1:8554/{stream})
    ↓
PyAV continuous decode → skip frames to achieve ~3 FPS (wall-clock timing)
    ↓
Motion gate (~1ms cv2.absdiff) — no change → skip frame
    ↓
Periodic forced YOLO every 10s even without motion (catch stationary birds)
    ↓
YOLO detection (~100ms CoreML) — no bird → skip frame
    ↓
For each bird detected:
    New bird? → Classify species (5ms ONNX) → create track
    Known bird? → IoU match → reuse species, update box position
    ↓
Broadcast ALL active tracks via SSE
    ↓
Best frame logic → save keeper to incoming/ for classify.py
```

## New File: `bird_pipeline.py`

Single file. Runs as one LaunchAgent on port 8100. Replaces three processes eventually, but runs parallel during testing.

### Frame Acquisition

**Source:** go2rtc's local RTSP restream at `rtsp://127.0.0.1:8554/{stream_name}`. This is critical:
- go2rtc holds ONE RTSP connection to the CloudKey per camera
- The pipeline connects to go2rtc LOCALLY — unlimited consumers, no CloudKey connection limit
- go2rtc handles token refresh via `refresh_rtsp.py` — pipeline doesn't need to manage tokens
- H.264 decode happens via PyAV — measured at 12.8ms per frame (not 5ms, not 100ms)

```python
container = av.open("rtsp://127.0.0.1:8554/feeder-main",
                    options={"rtsp_transport": "tcp", "stimeout": "5000000",
                             "fflags": "nobuffer", "flags": "low_delay"})
video_stream = container.streams.video[0]
video_stream.thread_type = "AUTO"  # multi-threaded H.264 decode

for frame in container.decode(video_stream):
    pil_image = frame.to_image()    # → PIL.Image (RGB), ~13ms
    np_array = np.array(pil_image)  # → numpy (RGB), ~13ms
```

**Frame rate control:** Wall-clock timing, NOT Nth-frame counting. Process a frame only if ≥333ms since last processed frame. Adapts to any camera FPS without configuration.

```python
last_process = 0
for frame in container.decode(video_stream):
    now = time.monotonic()
    if now - last_process < 0.333:  # 3 FPS target
        continue  # skip this frame
    last_process = now
    # process frame...
```

**Reconnection:** If go2rtc restream disconnects (go2rtc restarted, Docker restart), exponential backoff 5s → 10s → 30s → 60s. Re-read `rtsp_urls.json` on reconnect (tokens may have changed). No need to run `refresh_rtsp.py` — go2rtc handles that.

**Two cameras:** Feeder and ground run in **separate threads with separate model instances.** ONNX Runtime with CoreML releases the GIL during inference — shared sessions are NOT thread-safe. Each thread loads its own YOLO + classifier (~300MB RAM each). Total extra RAM: ~600MB on a 32GB+ iMac.

### Data Types at Each Stage

```
PyAV decode     → PIL.Image (RGB)
                → numpy.ndarray (RGB, for motion gate)
                    ↓ (convert RGB→BGR for motion gate: arr[:,:,::-1])
Motion gate     ← numpy.ndarray (BGR, matches cv2.COLOR_BGR2GRAY expectations)
YOLO detect     ← PIL.Image (RGB, as expected by YOLODetector.detect())
Crop bird       → PIL.Image (RGB crop)
Classifier      ← PIL.Image (RGB crop, as expected by SpeciesClassifier.classify())
```

### Motion Gate

Reuses existing `MotionGate` class from `motion_gate.py`. Threshold 1.5%.

**RGB→BGR conversion:** MotionGate assumes BGR input (uses `cv2.COLOR_BGR2GRAY`). Pipeline feeds RGB from PIL. Convert with `arr[:,:,::-1]` before passing to motion gate.

**Forced periodic detection:** Every 10 seconds, run YOLO regardless of motion. Catches stationary birds that landed during a motion-gate pass (bird arrives, motion triggers detection, bird sits still — subsequent frames show no motion but bird is still there).

**False positive mitigation:** If motion gate fires continuously for >30 seconds (wind, shadows), throttle to 1 FPS instead of 3 FPS to reduce CPU waste. Reset to 3 FPS when motion stops then restarts.

### Detection (YOLO)

YOLOv8n at 640x640 via ONNX Runtime + CoreML. Same model as current system. Returns list of bounding boxes with confidence scores. ~100ms per frame.

### Multi-Bird Tracking

```python
class BirdTracker:
    """Track birds across frames by bounding box overlap."""

    tracks: dict[int, Track]
    next_id: int
    session_id: str  # random UUID, reset on restart

    def update(self, frame, detections, classifier):
        """Match detections to existing tracks. Classify new birds.

        Args:
            frame: PIL.Image (full frame, for cropping new detections)
            detections: list of YOLO dicts [{"box", "confidence"}]
            classifier: SpeciesClassifier instance

        Returns:
            list of TrackState dicts for SSE broadcast
        """
```

**Track lifecycle:**
- **Create:** Detection with no IoU match (>0.3) to existing track → crop bird → classify → assign track ID
- **Update:** Detection matches existing track by IoU → update bbox, keep species
- **Re-classify:** IoU match < 0.5 (bird moved significantly) OR track age > 30s → re-run classifier
- **Expire:** No matching detection for 3 seconds → save keeper frame → remove track
- **Hard max lifetime:** 10 minutes. No track lives forever (prevents memory leak from false persistent detections like feeder post being detected as bird).
- **Max concurrent tracks:** 20 per camera. Oldest evicted if exceeded.

**IoU threshold (0.3) at 3 FPS:** A bird hopping on the feeder moves ~50-100px between frames. At 640x640 YOLO input, bounding boxes are typically 80-200px wide. A 100px shift on a 150px box = ~0.33 IoU. The 0.3 threshold accommodates normal feeder movement. Fast flyovers (full frame traversal in one interval) will create new tracks — acceptable since the bird is leaving anyway.

### Classification

AIY Birds V1 via ONNX (not Coral). ~5ms per crop.

**Classify once per new track.** Re-classify only when track is stale or position shifted significantly. With 3 birds in frame, initial classification cost is 15ms total. Subsequent frames: 0ms classification cost.

**Species label is preliminary during parallel run.** The keeper frame goes through classify.py's full intelligence stack (yard prior, range filter, visit voting) which may produce a different species. This is expected and documented — the live label is "first impression," the DB record is "final answer."

### SSE Broadcast

Every processed frame with active tracks, broadcast:

```json
{
  "type": "detections",
  "session_id": "a1b2c3",
  "camera": "feeder",
  "timestamp": "2026-04-03T10:15:30.123",
  "tracks": [
    {
      "track_id": 1,
      "species": "Black-capped Chickadee",
      "confidence": 0.89,
      "bbox": [100, 200, 300, 400],
      "age_seconds": 12.5
    }
  ],
  "frame_width": 1920,
  "frame_height": 1080
}
```

**When no active tracks:** Don't broadcast (save bandwidth). Send keepalive every 15 seconds.

**Broadcast only on change:** Send when: new track appears, track position moved >10px, track expired. NOT every frame. Reduces event rate from 6/sec to ~1-2/sec while maintaining visual accuracy.

**`session_id`:** Random UUID generated on pipeline start. Dashboard detects session change and flushes stale tracks from previous session.

### SSE Server Threading

```
Threads:
- Main thread:      SSE HTTP server (ThreadingHTTPServer, one thread per client)
- Camera-feeder:    decode → motion → YOLO → track → classify
- Camera-ground:    same, independent
- Watchdog:         monitors camera threads, writes health file
```

**Slow client protection:** Per-client message queue with `maxsize=50`. On overflow, drop oldest messages. Dead clients (BrokenPipeError) removed immediately. Same pattern as existing `live_detector.py` SSE server (proven).

### Keeper Frame Logic

Per track, hold the frame with highest YOLO confidence in memory as PIL Image.

```
New frame with bird:
    confidence > current keeper's confidence?
        YES → close() old keeper, hold new one
        NO → discard

Track expires (bird left):
    Save keeper as {camera}_{timestamp}_{track_id}.jpg to incoming/
    classify.py picks it up and handles:
        - Yard prior, range filter, visit voting
        - DB record creation
        - File organization to classified/{species}/
```

**Filename:** `{camera}_{timestamp}_{track_id}.jpg` — includes track_id to prevent collisions when two cameras save keepers at the same moment. `classify.py` already handles camera-prefixed filenames.

**Memory management:** Explicitly `close()` old keeper PIL Image when replaced. Max ~6MB per keeper (1920x1080 RGB). With 20 max tracks × 2 cameras = max 240MB in extreme case (normal: 2-3 tracks, ~18MB).

**Atomic write:** Save as `.tmp` then rename. `classify.py` already skips `.tmp` files.

### Watchdog

Built-in watchdog thread:
- Checks each camera thread's `last_frame` timestamp every 15 seconds
- If no frame for 60 seconds → restart that camera thread
- If go2rtc is down (RTSP connect fails) → log warning, backoff, retry
- Write health file to `/tmp/bird-pipeline-health.json` every 30 seconds
- Health monitor can detect stale timestamps and restart the LaunchAgent

### Nighttime Pause

Same `solar_utils.is_nighttime()` as current system.
- Close RTSP connections (saves go2rtc resources)
- Stop frame processing, keep SSE server running (keepalives)
- On sunrise: reconnect to go2rtc RTSP restream with fresh frame

**Token handling on wake:** Re-read `rtsp_urls.json` before connecting. Tokens may have been refreshed by `refresh_rtsp.py` overnight. go2rtc.yaml may have been updated. No need for pipeline to manage tokens — just reconnect to go2rtc.

## Dashboard Changes

### Canvas Overlay

```javascript
function _handlePipelineEvent(event) {
    var data = JSON.parse(event.data);

    // Detect pipeline restart — flush stale tracks
    if (data.session_id && data.session_id !== _pipelineSessionId) {
        _pipelineSessionId = data.session_id;
        liveDetections = [];
    }

    if (data.type === 'detections' && data.tracks) {
        liveDetections = data.tracks.map(function(t) {
            return {
                species: t.species,
                bbox: t.bbox,
                confidence: t.confidence,
                camera: data.camera,
                track_id: t.track_id,
                _expireAt: Date.now() + 1500
            };
        });
    }
}
```

**Toggle:** localStorage setting to switch between:
- Old SSE: `/live-detections/events` (port 8097)
- New SSE: `/pipeline/events` (port 8100 via api.py proxy)

Default to old during parallel run. Switch to new when confirmed working.

### SSE Proxy

Add to `dashboard/api.py`:
```python
@app.get("/pipeline/events")
async def proxy_pipeline_sse():
    """Proxy SSE from bird_pipeline (port 8100)."""
    # Same pattern as /live-detections/events proxy
```

## Parallel Running Strategy

| Process | Status | Port | Role |
|---------|--------|------|------|
| `capture_snapshots.py` | Running (existing) | — | Saves frames to incoming/ |
| `classify.py --watch` | Running (existing) | — | Classifies from incoming/ |
| `live_detector.py` | Running (existing) | 8097 | Old SSE overlay |
| **`bird_pipeline.py`** | **Running (NEW)** | **8100** | **New SSE overlay + keeper frames** |

Both pipeline and old system write keepers to `incoming/`. `classify.py` processes whatever arrives — it doesn't care who wrote it. No conflict.

**Cutover sequence (after confidence):**
1. Switch dashboard default SSE to new pipeline
2. Stop `live_detector.py`
3. Stop `capture_snapshots.py` (pipeline's keepers replace its output)
4. Eventually: migrate classify.py logic into pipeline

## Performance Budget (validated)

| Stage | Time | Notes |
|-------|------|-------|
| PyAV decode + to_image | ~13ms | Validated via PoC |
| PIL to numpy | ~13ms | Validated via PoC |
| RGB→BGR conversion | <1ms | Array slice |
| Motion gate | ~1ms | cv2.absdiff |
| YOLO detection | ~100ms | CoreML GPU |
| Classification (new bird) | ~5ms | ONNX, once per track |
| IoU tracking | <1ms | Math only |
| SSE broadcast | <1ms | JSON + send |
| **Total (motion, no bird)** | **~128ms** | |
| **Total (bird, new track)** | **~133ms** | |
| **Total (bird, tracked)** | **~128ms** | |

At 3 FPS per camera: 384ms/sec. Two cameras: 768ms/sec. With GIL contention between threads, effective per-camera rate may drop to ~2.5 FPS under simultaneous motion. Still well under 500ms latency target.

## Files

| File | Action |
|------|--------|
| `bird_pipeline.py` | Create — the unified pipeline |
| `dashboard/api.py` | Modify — add SSE proxy for port 8100 |
| `dashboard/index.html` | Modify — add pipeline event handler + toggle |
| `com.vives.bird-pipeline.plist` | Create — LaunchAgent |
| `tests/test_bird_tracker.py` | Create — IoU tracker tests |
| `tests/test_pipeline_integration.py` | Create — frame decode + motion + detect tests |

## Success Criteria

1. Bird lands → box + species appears in <500ms on live feed
2. Multiple birds tracked simultaneously with independent labels
3. Box follows bird as it moves across feeder
4. Best frame per visit saved to incoming/ for classify.py
5. System recovers automatically from go2rtc disconnects (watchdog)
6. No interference with existing pipeline during parallel run
7. CPU usage < 50% per camera at 3 FPS
8. No RTSP connection limit issues (uses go2rtc local restream)
9. Dashboard can toggle between old and new SSE sources
