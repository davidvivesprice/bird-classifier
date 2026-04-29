> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Live Detection Deep Research — April 1, 2026

## Current System

`live_detector.py` polls go2rtc `/api/frame.jpeg` at 3 FPS, runs YOLO + AIY classification, broadcasts via SSE to dashboard overlay. Uses ONNX (not Coral) to avoid contention with batch classifier.

### Known Issues
1. **Got stuck for 5 days (March 27 - April 1)** — frame errors loop forever with no recovery. No watchdog, no health file.
2. **go2rtc RTSP connection failures** — both Docker and native go2rtc binary timeout connecting to CloudKey (192.168.4.9:7447) while PyAV and urllib work fine with the same tokens. Suspected CloudKey RTSP client compatibility issue.
3. **Snapshot polling latency** — go2rtc `/api/frame.jpeg` blocks 1-2s waiting for keyframe. Real 3 FPS is impossible.

## Frigate's Architecture (Key Lessons)

### Reliability
- **CameraWatchdog thread** monitors each camera. If no frame for 20 seconds → restart FFmpeg. If FPS overflow → restart. If stale detection → increment counter.
- **Process isolation** — separate process per camera (CameraCapture + CameraTracker). One crash doesn't kill others.
- **Shared memory** for frames — zero-copy between processes, 1920x1080 YUV420 at ~3MB/frame.
- **Backpressure** — when detection can't keep up, frames are dropped. System always processes recent frames.

### Detection Optimization
- **Motion → Region → Detection** pipeline. Motion generates candidate regions. YOLO only runs on motion regions, not full frame.
- **Object tracking** (Norfair) — classify once on arrival, track by IoU thereafter. Re-classify only when track is weak or stale. Reduces Coral usage by ~95%.
- **Stationary objects** — bird sitting still for 10+ seconds stops triggering detection. Resumes only when motion near the object or at periodic intervals.
- **5 region types**: startup (full scan), tracked (predict position), motion (around motion boxes), grid (periodic full scan), stationary (interval re-check).
- **Zones** — polygon regions with object filtering. Only log visits when bird is in "perch zone."

### Frame Acquisition
- Frigate uses **FFmpeg to decode RTSP frame-by-frame** into shared memory. NOT go2rtc snapshot API.
- Consistent low-latency frame access (~33ms at 30fps source).
- Connection health is immediate (stream breaks = instant detection).

## Alternative Approaches

### Google Coral Smart Bird Feeder
- Single-stage MobileNet classification every frame (no detector)
- GStreamer pipeline, no motion gating, no tracking
- Simple but effective for single feeder

### Academic: Autonomous AI Bird Feeder (arxiv 2508.09398)
- Detectron2 (Mask R-CNN) + EfficientNet-B3 fine-tuned on 40 species
- 99.53% validation accuracy
- Motion-triggered, 1 FPS, all CPU inference
- **Two-phase EfficientNet training**: frozen backbone 5 epochs → fine-tune last blocks 30 epochs

### MegaDetector v6 (Microsoft)
- YOLOv9/v10, 2% of v5's parameters
- 3 classes: Animal, Human, Vehicle
- "MegaDetector for detection → crop → species classifier" pattern

### Specialized Bird YOLO Models
- YOLO-Bird: lightweight C2f-HLB for fine-grained bird features
- LDDm-YOLO: 127 FPS at mAP=0.96, only 6.25MB model

## go2rtc Snapshot Latency (Critical Finding)

go2rtc `/api/frame.jpeg` has 1-2 second latency because:
1. go2rtc is "lazy" — no decoded frame buffer
2. Must wait for next H.264 keyframe on each request
3. Keyframe interval typically 1-2 seconds

**Better approach**: MJPEG stream from go2rtc (`/api/stream.mjpeg?src={camera}`) — persistent connection, each JPEG boundary is a fresh frame, consistent ~33ms latency.

## Recommended Architecture

### Latency Budget

| Stage | Current | Optimized |
|-------|---------|-----------|
| Frame acquisition | 1000-2000ms (snapshot) | 33-100ms (MJPEG stream) |
| Motion check | 1ms | 1ms |
| YOLO detection | 100-200ms | 100-200ms |
| Classification | 5ms | 5ms (only new birds) |
| Tracking/voting | <1ms | <1ms |
| SSE broadcast | <1ms | <1ms |
| **Total** | **1100-2200ms** | **140-310ms** |

### Phase 1: Reliability (urgent)
- Watchdog thread in live_detector.py
- Health file with last_frame timestamp
- go2rtc connectivity check before retry loop
- Exponential backoff (5s → 10s → 30s → 60s)
- Health monitor integration

### Phase 2: MJPEG Streaming (high impact)
- Replace snapshot polling with continuous MJPEG stream
- `cv2.VideoCapture("http://go2rtc:1984/api/stream.mjpeg?src={stream}")` or raw HTTP multipart
- Auto-reconnect on stream break
- "Latest frame" buffer — detection always reads freshest frame

### Phase 3: Object Tracking (medium impact)
- Extend SpeciesVoter into BirdTracker with persistent track IDs
- Classify on first detection, track by IoU thereafter
- Re-classify when: new track, low confidence, age > 30s, significant position change
- Emit track_start/track_end SSE events

### Phase 4: Process Unification (long term)
- Merge live_detector.py and classify.py into one process
- Shared YOLO + AIY model instances
- Priority queue: live gets priority, batch fills idle time
- Unified visit model

## Coral TPU Strategy

**Option C (recommended for now):** Live detector uses ONNX only. The 3ms Coral advantage over ONNX classification is irrelevant when YOLO takes 100-200ms. Save Coral for batch classifier.

When yard model is retrained properly, revisit Coral sharing with time-division multiplexing.

## Key Insight

The #1 problem is reliability, not performance. A system at 1 FPS that never dies beats 10 FPS that silently breaks for 5 days.

The #2 problem is frame acquisition latency. Switching from snapshots to MJPEG streaming cuts end-to-end latency by 70-80%.