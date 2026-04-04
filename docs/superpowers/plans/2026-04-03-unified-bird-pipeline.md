# Unified Bird Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Real-time bird detection with <500ms latency — bird lands, bounding box + species label appears on the dashboard live feed. Multiple birds tracked simultaneously. Best frame per visit saved for review.

**Architecture:** Single process `bird_pipeline.py` decodes RTSP video from go2rtc's local restream via PyAV, runs motion gate + YOLO + species classification, tracks birds by IoU across frames, broadcasts detections via SSE. Runs parallel to existing system on port 8100.

**Tech Stack:** PyAV (RTSP decode), ONNX Runtime + CoreML (YOLO), ONNX (AIY classifier), OpenCV (motion gate), threading (per-camera), HTTP SSE (broadcast)

**Spec:** `docs/superpowers/specs/2026-04-03-unified-bird-pipeline-design.md`

**CRITICAL VERIFICATION GATE:** After every task:
1. Run `pytest tests/ -q` — all pass
2. For Tasks 4+: Take Playwright screenshot of the dashboard live feed
3. Read each screenshot and verify: boxes rendering, species labels visible, no stale boxes
4. Only then commit

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `bird_tracker.py` | Create | IoU-based multi-bird tracker with track lifecycle |
| `bird_pipeline.py` | Create | Main pipeline: frame decode, motion, YOLO, track, broadcast |
| `dashboard/api.py` | Modify | Add SSE proxy for pipeline on port 8100 |
| `dashboard/index.html` | Modify | Pipeline event handler + toggle |
| `com.vives.bird-pipeline.plist` | Create | LaunchAgent |
| `tests/test_bird_tracker.py` | Create | Tracker unit tests |

---

### Task 1: BirdTracker — IoU-based Multi-Bird Tracker

**Files:**
- Create: `bird_tracker.py`
- Create: `tests/test_bird_tracker.py`

The tracker is a standalone module with no dependencies on PyAV, YOLO, or SSE. It takes detection boxes + species labels and manages track lifecycle.

- [ ] **Step 1: Write tracker tests**

Create `tests/test_bird_tracker.py`:

```python
"""Tests for BirdTracker — IoU-based multi-bird tracking."""
import time
import pytest


class TestIoU:
    def test_perfect_overlap(self):
        from bird_tracker import _iou
        assert _iou([0,0,100,100], [0,0,100,100]) == 1.0

    def test_no_overlap(self):
        from bird_tracker import _iou
        assert _iou([0,0,50,50], [100,100,200,200]) == 0.0

    def test_partial_overlap(self):
        from bird_tracker import _iou
        iou = _iou([0,0,100,100], [50,50,150,150])
        assert 0.1 < iou < 0.2  # ~14% overlap


class TestBirdTracker:
    def test_new_detection_creates_track(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        detections = [{"box": [100,100,200,200], "confidence": 0.9}]
        species = ["Song Sparrow"]
        tracks = tracker.update(detections, species)
        assert len(tracks) == 1
        assert tracks[0]["species"] == "Song Sparrow"
        assert tracks[0]["is_new"] is True

    def test_same_position_reuses_track(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        det = [{"box": [100,100,200,200], "confidence": 0.9}]
        sp = ["Song Sparrow"]
        t1 = tracker.update(det, sp)
        t2 = tracker.update(det, sp)
        assert t1[0]["track_id"] == t2[0]["track_id"]
        assert t2[0]["is_new"] is False

    def test_different_position_creates_new_track(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        tracker.update([{"box": [0,0,50,50], "confidence": 0.9}], ["Sparrow"])
        tracks = tracker.update([{"box": [500,500,600,600], "confidence": 0.9}], ["Cardinal"])
        # Should have 2 tracks (old hasn't expired yet)
        assert len(tracks) >= 1
        assert any(t["species"] == "Cardinal" for t in tracks)

    def test_multiple_birds(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        dets = [
            {"box": [0,0,100,100], "confidence": 0.9},
            {"box": [300,300,400,400], "confidence": 0.8},
        ]
        tracks = tracker.update(dets, ["Sparrow", "Cardinal"])
        assert len(tracks) == 2
        species = {t["species"] for t in tracks}
        assert species == {"Sparrow", "Cardinal"}

    def test_track_expires_after_timeout(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker(expire_seconds=0.1)
        tracker.update([{"box": [100,100,200,200], "confidence": 0.9}], ["Sparrow"])
        time.sleep(0.2)
        expired = tracker.get_expired_tracks()
        assert len(expired) == 1
        assert expired[0]["species"] == "Sparrow"

    def test_keeper_frame_is_highest_confidence(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        tracker.update([{"box": [100,100,200,200], "confidence": 0.7}], ["Sparrow"],
                       frame_data=b"low_conf_frame")
        tracker.update([{"box": [100,100,200,200], "confidence": 0.95}], ["Sparrow"],
                       frame_data=b"high_conf_frame")
        tracker.update([{"box": [100,100,200,200], "confidence": 0.8}], ["Sparrow"],
                       frame_data=b"medium_conf_frame")
        # The keeper should be the high confidence frame
        tracks = tracker.get_active_tracks()
        assert tracks[0]["keeper_data"] == b"high_conf_frame"

    def test_max_tracks_evicts_oldest(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker(max_tracks=3)
        for i in range(5):
            tracker.update(
                [{"box": [i*100, 0, i*100+50, 50], "confidence": 0.9}],
                [f"Bird{i}"]
            )
        assert len(tracker.get_active_tracks()) <= 3

    def test_max_lifetime(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker(max_lifetime=0.1)
        tracker.update([{"box": [100,100,200,200], "confidence": 0.9}], ["Sparrow"])
        time.sleep(0.15)
        # Even with matching detection, track should expire
        expired = tracker.get_expired_tracks()
        assert len(expired) == 1

    def test_session_id_exists(self):
        from bird_tracker import BirdTracker
        tracker = BirdTracker()
        assert tracker.session_id is not None
        assert len(tracker.session_id) > 0
```

- [ ] **Step 2: Run tests — verify they fail**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_bird_tracker.py -v`

- [ ] **Step 3: Implement `bird_tracker.py`**

Create `bird_tracker.py`:

```python
"""bird_tracker — IoU-based multi-bird tracker for real-time detection.

Tracks birds across video frames by matching bounding box overlap (IoU).
Each track has a species label, bounding box, confidence, and keeper frame.
Tracks expire when no matching detection appears for expire_seconds.

Used by bird_pipeline.py for the live detection overlay.
"""

import time
import uuid


def _iou(box_a, box_b):
    """Compute Intersection over Union between two boxes [x1,y1,x2,y2]."""
    x1 = max(box_a[0], box_b[0])
    y1 = max(box_a[1], box_b[1])
    x2 = min(box_a[2], box_b[2])
    y2 = min(box_a[3], box_b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    area_a = (box_a[2] - box_a[0]) * (box_a[3] - box_a[1])
    area_b = (box_b[2] - box_b[0]) * (box_b[3] - box_b[1])
    return inter / (area_a + area_b - inter + 1e-6)


class Track:
    __slots__ = ("track_id", "species", "bbox", "confidence",
                 "created", "updated", "keeper_data", "keeper_confidence")

    def __init__(self, track_id, species, bbox, confidence, frame_data=None):
        self.track_id = track_id
        self.species = species
        self.bbox = bbox
        self.confidence = confidence
        self.created = time.monotonic()
        self.updated = time.monotonic()
        self.keeper_data = frame_data
        self.keeper_confidence = confidence


class BirdTracker:
    """Track birds across frames by bounding box IoU overlap.

    Args:
        iou_threshold: Minimum IoU to match a detection to existing track (default 0.3)
        expire_seconds: Seconds with no match before track expires (default 3.0)
        max_tracks: Maximum concurrent tracks per camera (default 20)
        max_lifetime: Hard max track lifetime in seconds (default 600 = 10 min)
    """

    def __init__(self, iou_threshold=0.3, expire_seconds=3.0,
                 max_tracks=20, max_lifetime=600):
        self.iou_threshold = iou_threshold
        self.expire_seconds = expire_seconds
        self.max_tracks = max_tracks
        self.max_lifetime = max_lifetime
        self.tracks: dict[int, Track] = {}
        self._next_id = 0
        self.session_id = uuid.uuid4().hex[:8]

    def _new_id(self):
        tid = self._next_id
        self._next_id += 1
        return tid

    def update(self, detections, species_list, frame_data=None):
        """Match detections to existing tracks. Create new tracks for unmatched.

        Args:
            detections: list of {"box": [x1,y1,x2,y2], "confidence": float}
            species_list: list of species names, parallel to detections
            frame_data: optional bytes/object to store as keeper frame

        Returns:
            list of track state dicts for SSE broadcast
        """
        now = time.monotonic()
        matched_track_ids = set()

        for det, species in zip(detections, species_list):
            box = det["box"]
            conf = det["confidence"]

            # Find best IoU match among existing tracks
            best_iou = 0
            best_track = None
            for track in self.tracks.values():
                iou = _iou(box, track.bbox)
                if iou > best_iou:
                    best_iou = iou
                    best_track = track

            if best_track and best_iou >= self.iou_threshold:
                # Update existing track
                best_track.bbox = box
                best_track.confidence = conf
                best_track.updated = now
                matched_track_ids.add(best_track.track_id)
                # Update keeper if this frame is better
                if frame_data and conf > best_track.keeper_confidence:
                    best_track.keeper_data = frame_data
                    best_track.keeper_confidence = conf
            else:
                # New track
                tid = self._new_id()
                track = Track(tid, species, box, conf, frame_data)
                self.tracks[tid] = track
                matched_track_ids.add(tid)

        # Evict if over max
        while len(self.tracks) > self.max_tracks:
            oldest_id = min(self.tracks, key=lambda k: self.tracks[k].created)
            del self.tracks[oldest_id]

        return self._build_states(now)

    def get_expired_tracks(self):
        """Remove and return tracks that have expired."""
        now = time.monotonic()
        expired = []
        to_remove = []
        for tid, track in self.tracks.items():
            age = now - track.updated
            lifetime = now - track.created
            if age > self.expire_seconds or lifetime > self.max_lifetime:
                expired.append({
                    "track_id": track.track_id,
                    "species": track.species,
                    "bbox": track.bbox,
                    "keeper_data": track.keeper_data,
                    "keeper_confidence": track.keeper_confidence,
                    "duration": now - track.created,
                })
                to_remove.append(tid)
        for tid in to_remove:
            del self.tracks[tid]
        return expired

    def get_active_tracks(self):
        """Return current active track states."""
        return self._build_states(time.monotonic())

    def _build_states(self, now):
        return [
            {
                "track_id": t.track_id,
                "species": t.species,
                "bbox": t.bbox,
                "confidence": t.confidence,
                "is_new": (now - t.created) < 0.5,
                "age_seconds": round(now - t.created, 1),
                "keeper_data": t.keeper_data,
            }
            for t in self.tracks.values()
        ]
```

- [ ] **Step 4: Run tests — all pass**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/test_bird_tracker.py -v`

- [ ] **Step 5: Run full suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -q`

- [ ] **Step 6: Commit**

```bash
git add bird_tracker.py tests/test_bird_tracker.py
git commit -m "feat: BirdTracker — IoU-based multi-bird tracker with keeper frames"
```

---

### Task 2: Bird Pipeline — Core Frame Loop

**Files:**
- Create: `bird_pipeline.py`

This is the main pipeline. It connects to go2rtc's RTSP restream, decodes frames, runs motion gate + YOLO + classification + tracking, and broadcasts via SSE. This task creates the file with all components wired together.

**IMPORTANT:** The implementer must READ these files first to understand the patterns:
- `live_detector.py` — SSE server pattern (lines 319-388), camera loop pattern (lines 410-579)
- `bird_inference.py` — `YOLODetector` and `SpeciesClassifier` classes
- `motion_gate.py` — `MotionGate` class
- `bird_tracker.py` — the tracker from Task 1

- [ ] **Step 1: Create `bird_pipeline.py` — frame reader + motion gate + YOLO + tracker + SSE**

The file should contain:

1. **Imports and config** — PyAV, bird_inference, motion_gate, bird_tracker, threading, signal handling
2. **`VideoStreamReader` class** — connects to go2rtc RTSP restream, decodes video frames, exponential backoff on disconnect, re-reads `rtsp_urls.json` on reconnect failure
3. **`camera_loop()` function** — main loop per camera: decode frames at 3 FPS (wall-clock), motion gate, YOLO, classify new tracks, update tracker, broadcast, save keepers
4. **SSE server** — same `ThreadingHTTPServer` + `SSEHandler` pattern as `live_detector.py`, port 8100. Endpoints: `/events` (SSE), `/health`, `/metrics`
5. **`broadcast_tracks()`** — send all active tracks as one SSE event with session_id
6. **Watchdog thread** — monitors camera threads, restarts if no frame for 60s, writes health file
7. **Nighttime pause** — uses `solar_utils.is_nighttime()`, closes RTSP on pause, reconnects on sunrise
8. **Keeper frame saving** — on track expiry, save best frame to `incoming/` as `{camera}_{timestamp}_{track_id}.jpg` with atomic .tmp + rename
9. **`main()` function** — parse args, load models (per-camera instances), start threads, start SSE server

Key implementation details:

**Frame rate control (wall-clock, not Nth-frame):**
```python
last_process = 0
for frame in container.decode(video_stream):
    now = time.monotonic()
    if now - last_process < 0.333:
        continue
    last_process = now
```

**RGB→BGR for motion gate:**
```python
np_bgr = np.array(pil_image)[:, :, ::-1]  # RGB to BGR
```

**Per-camera model instances (NOT shared):**
```python
def camera_loop(camera_name, stream_name):
    detector = YOLODetector(str(YOLO_MODEL_PATH), confidence=0.3)
    classifier = SpeciesClassifier(str(SPECIES_MODEL_PATH), str(LABELS_PATH),
                                   regional_species=regional)
    tracker = BirdTracker()
    gate = MotionGate(threshold_pct=1.5, resize_width=320)
    # ... loop
```

**Forced periodic YOLO (catch stationary birds):**
```python
last_forced_yolo = 0
# In the frame loop:
force_yolo = (now - last_forced_yolo) > 10.0
if gate.has_motion(np_bgr, camera=camera_name) or force_yolo:
    if force_yolo:
        last_forced_yolo = now
    detections = detector.detect(pil_image)
    # ...
```

**Keeper frame saving on track expiry:**
```python
expired = tracker.get_expired_tracks()
for track in expired:
    if track["keeper_data"]:
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        fname = f"{camera_name}_{ts}_{track['track_id']}.jpg"
        tmp = INCOMING_DIR / (fname + ".tmp")
        tmp.write_bytes(track["keeper_data"])
        tmp.rename(INCOMING_DIR / fname)
        logging.info("[%s] Keeper saved: %s (%s, %.0f%% conf, %.1fs visit)",
                     camera_name, fname, track["species"],
                     track["keeper_confidence"] * 100, track["duration"])
```

**Broadcast only on track changes:**
```python
prev_track_state = None
# After tracker.update():
current_state = tracker.get_active_tracks()
state_key = [(t["track_id"], t["bbox"]) for t in current_state]
if state_key != prev_track_state:
    broadcast_tracks(camera_name, current_state, frame_width, frame_height)
    prev_track_state = state_key
```

- [ ] **Step 2: Test manually — start pipeline, verify it connects and detects**

```bash
/Users/vives/bird-classifier/venv-coral/bin/python bird_pipeline.py
```

Expected output:
```
[feeder] Connecting to rtsp://127.0.0.1:8554/feeder-main
[feeder] Connected: 1920x1080 h264
[ground] Connecting to rtsp://127.0.0.1:8554/ground-main
[ground] Connected: 1920x1080 h264
SSE server listening on port 8100
```

Wait for a bird to appear, verify detection log line:
```
[feeder] Track 0: Song Sparrow (89% det, 5ms cls)
```

- [ ] **Step 3: Test SSE endpoint**

```bash
curl -s http://localhost:8100/events --max-time 30 | head -20
curl -s http://localhost:8100/health | python3 -m json.tool
```

- [ ] **Step 4: Run full test suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/ -q`

- [ ] **Step 5: Commit**

```bash
git add bird_pipeline.py
git commit -m "feat: bird_pipeline.py — unified real-time detection with tracking"
```

---

### Task 3: Dashboard Integration — SSE Proxy + Overlay Toggle

**Files:**
- Modify: `dashboard/api.py`
- Modify: `dashboard/index.html`

- [ ] **Step 1: Add SSE proxy to api.py**

Add after the existing `/live-detections/events` proxy:

```python
@app.get("/pipeline/events")
async def proxy_pipeline_sse():
    """Proxy SSE from bird_pipeline (port 8100)."""
    import httpx
    from starlette.responses import StreamingResponse

    async def stream():
        async with httpx.AsyncClient() as client:
            async with client.stream("GET", "http://127.0.0.1:8100/events", timeout=None) as resp:
                async for line in resp.aiter_lines():
                    yield line + "\n"

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive",
                                      "X-Accel-Buffering": "no"})

@app.get("/pipeline/health")
def pipeline_health():
    """Proxy health from bird_pipeline."""
    import httpx
    try:
        resp = httpx.get("http://127.0.0.1:8100/health", timeout=3)
        return resp.json()
    except Exception as e:
        return {"status": "down", "detail": str(e)}
```

- [ ] **Step 2: Add pipeline event handler + toggle to index.html**

Find the `connectLiveDetectionSSE()` function. Add a parallel function for the new pipeline:

```javascript
var _pipelineSessionId = null;
var _usePipeline = localStorage.getItem('usePipeline') === 'true';

function connectPipelineSSE() {
    var es = new EventSource('/pipeline/events');
    es.addEventListener('message', function(event) {
        try {
            var data = JSON.parse(event.data);
            if (data.type === 'connected') return;
            if (data.session_id && data.session_id !== _pipelineSessionId) {
                _pipelineSessionId = data.session_id;
                liveDetections = [];
            }
            if (data.tracks) {
                var cam = currentStream.replace('-main', '');
                liveDetections = data.tracks.filter(function(t) {
                    return !data.camera || data.camera === cam;
                }).map(function(t) {
                    return {
                        species: t.species,
                        bbox: t.bbox,
                        confidence: t.confidence,
                        camera: data.camera,
                        track_id: t.track_id,
                        _expireAt: Date.now() + 1500
                    };
                });
                // Flash status dot
                var dot = document.getElementById('live-detect-status');
                if (dot && data.tracks.length > 0) {
                    dot.style.color = '#4a9eff';
                    setTimeout(function() { dot.style.color = '#22c55e'; }, 500);
                }
                // Log
                data.tracks.forEach(function(t) {
                    console.log('[Pipeline] ' + t.species + ' (' + data.camera + ', track ' + t.track_id + ')');
                });
            }
        } catch(e) {}
    });
    es.addEventListener('open', function() {
        var dot = document.getElementById('live-detect-status');
        if (dot) { dot.style.color = '#22c55e'; dot.title = 'Pipeline: connected'; }
    });
    es.addEventListener('error', function() {
        var dot = document.getElementById('live-detect-status');
        if (dot) { dot.style.color = '#ef4444'; dot.title = 'Pipeline: disconnected'; }
    });
}

// Connect to whichever SSE source is selected
if (_usePipeline) {
    connectPipelineSSE();
} else {
    connectLiveDetectionSSE();
}
```

Add a toggle button near the detection overlay toggle:

```html
<button class="live-enhance-btn" onclick="togglePipelineSSE()" id="pipeline-toggle"
        style="left:260px;display:none;" title="Switch between old and new detection pipeline">
    Old Det
</button>
```

```javascript
window.togglePipelineSSE = function() {
    _usePipeline = !_usePipeline;
    localStorage.setItem('usePipeline', _usePipeline);
    location.reload(); // simplest way to switch SSE source
};
```

- [ ] **Step 3: Run tests**

- [ ] **Step 4: Restart dashboard. Verify both SSE sources work**

Take Playwright screenshots with both old and new SSE connected.

- [ ] **Step 5: Commit**

```bash
git add dashboard/api.py dashboard/index.html
git commit -m "feat: dashboard pipeline SSE integration with toggle"
```

---

### Task 4: LaunchAgent + Smoke Test

**Files:**
- Create: `com.vives.bird-pipeline.plist`

- [ ] **Step 1: Create LaunchAgent plist**

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.vives.bird-pipeline</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/vives/bird-classifier/venv-coral/bin/python3</string>
        <string>-u</string>
        <string>/Users/vives/bird-classifier/bird_pipeline.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/Users/vives/bird-classifier</string>
    <key>KeepAlive</key>
    <true/>
    <key>ThrottleInterval</key>
    <integer>10</integer>
    <key>StandardOutPath</key>
    <string>/Users/vives/bird-snapshots/logs/pipeline.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/vives/bird-snapshots/logs/pipeline-stderr.log</string>
</dict>
</plist>
```

- [ ] **Step 2: Install and start**

```bash
cp com.vives.bird-pipeline.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
sleep 15
launchctl list | grep pipeline
```

- [ ] **Step 3: Verify pipeline is running**

```bash
curl -s http://localhost:8100/health | python3 -m json.tool
tail -20 /Users/vives/bird-snapshots/logs/pipeline.log
```

- [ ] **Step 4: Take Playwright screenshot of dashboard with pipeline SSE**

```python
from playwright.sync_api import sync_playwright
import time
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    page.goto("http://localhost:8099/")
    # Enable pipeline SSE
    page.evaluate("localStorage.setItem('usePipeline', 'true')")
    page.reload()
    time.sleep(15)  # wait for birds
    page.screenshot(path="/tmp/ss_pipeline_live.png")
    browser.close()
```

Read the screenshot. Verify:
- Green status dot (pipeline connected)
- If a bird is present: bounding box + species label visible on the canvas
- No stale boxes from old SSE

- [ ] **Step 5: Check keeper frames being saved**

```bash
ls -lt /Users/vives/bird-snapshots/incoming/*_*.jpg | head -5
```

Verify files with track IDs in the filename (e.g., `feeder_2026-04-03_12-30-45_3.jpg`).

- [ ] **Step 6: Commit**

```bash
git add com.vives.bird-pipeline.plist
git commit -m "feat: bird pipeline LaunchAgent — real-time detection running"
```

---

### Task 5: End-to-End Verification

No new code. Full system validation.

- [ ] **Step 1: Verify parallel run — old system still works**

```bash
# Old classifier still processing
tail -5 /Users/vives/bird-snapshots/logs/classifier-stdout.log | grep BIRD
# Old live detector still running
curl -s http://localhost:8097/health | python3 -m json.tool
# New pipeline running
curl -s http://localhost:8100/health | python3 -m json.tool
```

- [ ] **Step 2: Verify keeper frames reach classify.py**

Wait for a bird visit. Check:
```bash
# Pipeline saves keeper to incoming/
ls -lt /Users/vives/bird-snapshots/incoming/ | head -3
# classify.py picks it up and classifies
# (wait 10 seconds for watch interval)
ls -lt /Users/vives/bird-snapshots/classified/*/ | head -3
```

- [ ] **Step 3: Verify through Cloudflare tunnel**

```bash
curl -s https://birds.vivessato.com/pipeline/health
```

- [ ] **Step 4: Take screenshots on mobile viewport**

```python
from playwright.sync_api import sync_playwright
import time
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 390, "height": 844})
    page.goto("http://localhost:8099/")
    page.evaluate("localStorage.setItem('usePipeline', 'true')")
    page.reload()
    time.sleep(15)
    page.screenshot(path="/tmp/ss_pipeline_mobile.png")
    browser.close()
```

Read screenshot. Verify overlay renders correctly on mobile.

- [ ] **Step 5: Final commit with all verification notes**

```bash
git commit --allow-empty -m "verified: bird pipeline running parallel, keeper frames flowing, SSE overlay working"
```
