# Unified Bird Pipeline — Design Spec

## Problem

Three separate processes do overlapping work:
- `capture_snapshots.py` — polls CloudKey for frames, saves to disk
- `classify.py` — reads saved frames from disk, runs YOLO + AIY, saves to species folders
- `live_detector.py` — polls go2rtc for frames, runs YOLO + AIY, broadcasts SSE (broken/laggy)

This results in: 15+ second delay from bird landing to label showing, 84K wasted frames saved to disk, stale snapshot latency, and the live detector silently dying for 5 days.

## Goal

One process. Bird lands → bounding box with species label appears on the live feed in <500ms. Box tracks the bird as it moves. Multiple birds tracked simultaneously. Best frame per visit saved for review. No wasted frames on disk.

## Architecture

```
RTSP stream via PyAV (continuous decode, 3 FPS per camera)
    ↓
Motion gate (~1ms cv2.absdiff) — no change → skip frame
    ↓
YOLO detection (~100ms CoreML) — no bird → skip frame
    ↓
For each bird detected:
    New bird? → Classify species (5ms ONNX) → create track
    Known bird? → IoU match → reuse species, update box position
    ↓
Broadcast ALL active tracks via SSE
    ↓
Best frame logic → save keeper to disk for review
```

## New File: `bird_pipeline.py`

Single file. Runs as one LaunchAgent. Replaces three processes eventually, but runs parallel during testing.

### Frame Acquisition

PyAV decodes RTSP **video** stream continuously. Similar pattern to `audio_analyzer.py` but for video frames:

```python
container = av.open(rtsp_url, options={"rtsp_transport": "tcp", "stimeout": "10000000"})
video_stream = container.streams.video[0]
for frame in container.decode(video_stream):
    pil_image = frame.to_image()  # → PIL.Image (RGB)
    np_array = np.array(pil_image)  # → numpy for motion gate
```

**NOT reusing `RTSPStreamManager`** — that class is audio-specific. New `VideoStreamReader` class handles:
- RTSP connection with TCP transport
- Auto-reconnect with exponential backoff (5s → 10s → 30s → 60s)
- Health file writing (`/tmp/bird-pipeline-health.json`)
- Graceful shutdown

**Frame rate:** Decode at camera native rate but only process every Nth frame to achieve ~3 FPS detection rate. Skip decoded frames between processing slots (always use freshest available).

**Data types at each stage:**
- PyAV decode → `PIL.Image` (for YOLO, which takes PIL)
- PIL → `numpy.ndarray` (for motion gate, which takes numpy)
- YOLO returns `[{"box": [x1,y1,x2,y2], "confidence": float}]`
- Classifier takes `PIL.Image` crop, returns `[{"common_name", "raw_score"}]`

**Two cameras:** Feeder and ground run in separate threads. Each thread has its **own YOLO and classifier instances** — no sharing. ONNX Runtime with CoreML releases the GIL during inference, making shared sessions unsafe. The extra ~300MB RAM per camera is acceptable (iMac has 32GB+).

### Motion Gate

`cv2.absdiff` against previous frame, same as existing `MotionGate` class. ~1ms per frame. Threshold: 1.5% pixel change.

When no motion: zero inference cost.
When motion: proceed to YOLO.

### Detection (YOLO)

YOLOv8n at 640x640 via ONNX Runtime + CoreML. Same model as current system. Returns list of bounding boxes with confidence scores.

~100ms per frame. At 3 FPS = 300ms/sec of GPU time = 30% of one core.

### Multi-Bird Tracking

Simple IoU tracker. Not Norfair — overkill for static feeder cameras.

```python
class BirdTracker:
    """Track birds across frames by bounding box overlap."""

    tracks: dict[int, Track]  # track_id → Track
    next_id: int

    def update(self, detections, classifier):
        """Match new detections to existing tracks by IoU.

        Args:
            detections: list of YOLO detection dicts [{"box", "confidence"}]
            classifier: SpeciesClassifier instance (called only for new/stale tracks)

        For each detection:
            - If IoU > 0.3 with existing track → update position, keep species
            - If no match → run classifier on crop → create new track

        Returns list of TrackState:
            [{"track_id", "bbox", "species", "confidence", "is_new", "age_seconds"}]
        """
```

**Track lifecycle:**
- **Create:** New detection with no IoU match to existing track → classify species → assign track ID
- **Update:** Detection matches existing track (IoU > 0.3) → update bbox position, keep species
- **Re-classify:** Track age > 30s OR IoU match < 0.5 (bird moved significantly) → re-run classifier
- **Expire:** No matching detection for 3 seconds → remove track

**Multiple birds:** YOLO returns N boxes per frame. Each gets matched independently. 3 birds = 3 tracks, each with their own species label.

### Classification

AIY Birds V1 via ONNX (not Coral — avoids contention with batch classifier during parallel run).

**Classify once per track creation.** A chickadee sitting for 60 seconds gets classified once on arrival (~5ms), then tracked by IoU for the remaining frames (free). Re-classify only when:
- Track is new (first appearance)
- IoU match is weak (< 0.5 — bird moved significantly, might be different bird)
- Track age > 30 seconds (periodic recheck)

### SSE Broadcast

Every processed frame (after YOLO), broadcast ALL active tracks:

```json
{
  "type": "detections",
  "camera": "feeder",
  "timestamp": "2026-04-03T10:15:30.123",
  "tracks": [
    {
      "track_id": 1,
      "species": "Black-capped Chickadee",
      "confidence": 0.89,
      "bbox": [100, 200, 300, 400],
      "age_seconds": 12.5
    },
    {
      "track_id": 2,
      "species": "Northern Cardinal",
      "confidence": 0.95,
      "bbox": [400, 150, 600, 380],
      "age_seconds": 3.2
    }
  ],
  "frame_width": 1920,
  "frame_height": 1080
}
```

Dashboard draws all boxes on the canvas overlay. Boxes update every ~333ms (3 FPS).

SSE server runs on a new port (8100) to avoid conflicting with existing services during parallel testing.

### Best Frame Selection

Per track, keep the frame with the highest YOLO detection confidence. When track expires (bird leaves):

1. Save the best frame to `incoming/` (or directly to `classified/` if we trust the species)
2. Create classification DB record
3. Create visit record

This replaces the current flow of saving every frame and classifying later. Only ~1 frame per visit gets saved.

### Keeper vs Discard Logic

```
Frame with bird detected:
    Is this the best frame so far for this track? (highest confidence)
        YES → hold in memory as candidate keeper (PIL Image + metadata)
        NO → discard (already broadcast via SSE, job done)

Track expires (bird left):
    Save keeper frame to incoming/ as {camera}_{timestamp}.jpg
    classify.py picks it up on its normal watch cycle and handles:
        - Yard prior correction
        - Range filter validation
        - Visit tracking
        - DB record creation
        - File organization to classified/{species}/
```

**Why write to incoming/ instead of classified/ directly:**
During parallel run, `classify.py` stays as the source of truth for file organization, DB records, and intelligence layers (yard prior, range filter, visit voting). The pipeline's job is detection + tracking + live broadcast. The keeper frame is just a handoff.

When we retire `classify.py`, we migrate that logic into the pipeline. But not now — one thing at a time.

**Disk impact:** Instead of saving 200+ frames per day and classifying all of them, save ~50-100 keeper frames (one per visit). Disk usage goes down.

### Watchdog

Built-in watchdog thread monitors each camera thread:
- No frame for 60 seconds → restart camera thread
- RTSP connection failed → exponential backoff (5s → 10s → 30s → 60s)
- Write health file to `/tmp/bird-pipeline-health.json` every 30 seconds
- Health monitor integration: detect stale timestamps, restart if stuck

### Nighttime Pause

Same solar calculation as current system. When nighttime:
- Close RTSP connections (saves camera bandwidth)
- Stop frame processing
- Keep SSE server running (sends empty keepalives)
- Resume at sunrise

## Dashboard Changes

### Canvas Overlay Update

The new SSE schema sends one event per frame with ALL tracks (vs old schema: one event per bird). The dashboard needs to handle both during parallel run.

```javascript
function _handleNewPipelineEvent(event) {
    var data = JSON.parse(event.data);
    if (data.type === 'detections' && data.tracks) {
        // New pipeline: replace ALL boxes with current tracks
        liveDetections = data.tracks.map(function(t) {
            return {
                species: t.species,
                bbox: t.bbox,
                confidence: t.confidence,
                camera: data.camera,
                track_id: t.track_id,
                _expireAt: Date.now() + 1000  // fade after 1s without update
            };
        });
    }
}
```

**Toggle:** Dashboard setting (localStorage) to switch between:
- Old SSE: `/live-detections/events` (port 8097 via proxy)
- New SSE: `/pipeline/events` (port 8100 via proxy)

Both use the same `drawDetections()` function — the conversion above normalizes the format.

### SSE Connection

Dashboard connects to `bird_pipeline.py` SSE on port 8100, proxied through api.py at `/pipeline/events`. Same EventSource pattern as current live detection.

## Parallel Running Strategy

During testing, both old and new systems run simultaneously:

| Process | Status | Port |
|---------|--------|------|
| `capture_snapshots.py` | Running (existing) | — |
| `classify.py --watch` | Running (existing) | — |
| `live_detector.py` | Running (existing) | 8097 |
| `bird_pipeline.py` | Running (NEW, parallel) | 8100 |

The dashboard can switch between old SSE (8097) and new SSE (8100) via a toggle. This lets you compare side-by-side.

Once confident the new pipeline works:
1. Stop `capture_snapshots.py`
2. Stop `live_detector.py`
3. Point `classify.py` at the new pipeline's output (or remove it entirely if the pipeline handles classification + saving)
4. Remove old LaunchAgents

## Performance Budget

| Stage | Time | Notes |
|-------|------|-------|
| Frame decode (PyAV) | ~5ms | Already decoded from stream |
| Motion gate | ~1ms | cv2.absdiff |
| YOLO detection | ~100ms | CoreML, GPU |
| Classification (new bird) | ~5ms | ONNX, once per track |
| IoU tracking | <1ms | Math only |
| SSE broadcast | <1ms | JSON serialize + send |
| **Total (motion, no bird)** | **~106ms** | |
| **Total (bird found, new)** | **~112ms** | |
| **Total (bird found, tracked)** | **~107ms** | |

At 3 FPS: 3 × 107ms = 321ms/sec of compute per camera. Two cameras = 642ms/sec. The iMac has plenty of headroom.

## Files

| File | Action |
|------|--------|
| `bird_pipeline.py` | Create — the unified pipeline |
| `dashboard/api.py` | Modify — add SSE proxy for port 8100 |
| `dashboard/index.html` | Modify — update drawDetections for multi-track |
| `com.vives.bird-pipeline.plist` | Create — LaunchAgent |
| `tests/test_bird_pipeline.py` | Create — tracker tests, motion gate tests |

## Success Criteria

1. Bird lands → box + species appears in <500ms on live feed
2. Multiple birds tracked simultaneously with independent labels
3. Box follows bird as it moves across feeder
4. Best frame per visit saved to disk (not every frame)
5. System recovers automatically from camera disconnects (watchdog)
6. No interference with existing pipeline during parallel run
7. CPU usage < 50% per camera at 3 FPS
