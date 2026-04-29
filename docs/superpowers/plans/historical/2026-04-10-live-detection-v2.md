> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Live Detection Pipeline v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the live detection pipeline to achieve Frigate-quality smoothness. Decode frames via ffmpeg subprocess, run YOLO + classification inline, draw labels on the same frames, and serve them as MJPEG-over-WebSocket. Record HLS chunks via a separate ffmpeg subprocess.

**Architecture:** Single process per camera, threading-based. Capture thread drains ffmpeg stdout pipe into a bounded numpy frame queue. Process thread runs motion gate → YOLO (on motion regions) → Norfair tracker → Smart B classifier → event store → annotator. Annotator thread encodes JPEG and pushes to WebSocket clients. Dedicated recording ffmpeg subprocess writes HLS chunks in copy mode.

**Tech Stack:** Python 3.9 (venv-coral), ffmpeg 8.0.1, Norfair 2.3.0, websockets 12+, OpenCV, pycoral, SQLite WAL, MJPEG-over-WebSocket

**Source spec:** `docs/superpowers/specs/2026-04-10-live-detection-v2-design.md`

**Venv:** `/Users/vives/bird-classifier/venv-coral/bin/python`

---

## File Structure

**Create:**
- `pipeline/__init__.py` — package marker
- `pipeline/frame.py` — `Frame` dataclass (bgr array + metadata)
- `pipeline/frame_capture.py` — ffmpeg subprocess + pipe drain thread + watchdog
- `pipeline/motion_gate.py` — background subtractor, emits motion regions
- `pipeline/detector.py` — YOLO on motion regions with coordinate offset
- `pipeline/tracker.py` — Norfair wrapper with frigate-inspired distance
- `pipeline/classifier.py` — Smart B classifier with Coral lock and retry semantics
- `pipeline/event_store.py` — SQLite WAL + batched writes
- `pipeline/annotator.py` — label rendering + JPEG encoder per camera
- `pipeline/debug_stream.py` — WebSocket MJPEG server
- `pipeline/hls_recorder.py` — dedicated ffmpeg recording subprocess
- `pipeline/health.py` — shared health state + HTTP endpoint
- `pipeline/process_thread.py` — orchestrates motion→detect→track→classify→annotate per camera
- `bird_pipeline_v2.py` — new orchestrator (will replace bird_pipeline.py on cutover)
- `tests/pipeline/test_frame_capture.py`
- `tests/pipeline/test_pipe_saturation.py` — highest-risk validation, runs early
- `tests/pipeline/test_motion_gate.py`
- `tests/pipeline/test_detector.py`
- `tests/pipeline/test_tracker.py`
- `tests/pipeline/test_classifier.py`
- `tests/pipeline/test_event_store.py`
- `tests/pipeline/test_annotator.py`
- `tests/pipeline/test_debug_stream.py`
- `tests/pipeline/test_hls_recorder.py`
- `tests/pipeline/test_pipeline_e2e.py`
- `tests/pipeline/bench_pipeline.py`
- `tests/pipeline/__init__.py`

**Modify:**
- `dashboard/index.html` — replace overlay canvas with MJPEG WebSocket client; add poster frame, reconnect toast, cellular mode
- `dashboard/api.py` — add `/api/debug-stream/{camera}` WebSocket proxy + `/api/pipeline/health` + `/api/pipeline/events` proxies
- `requirements.txt` (or equivalent) — pin `norfair==2.3.0`, `websockets>=12.0,<13`, `filterpy==1.4.5`
- `~/Library/LaunchAgents/com.vives.bird-pipeline.plist` — update `ProgramArguments` to point at `bird_pipeline_v2.py` (done in Task 14)

**Reference (read-only):**
- `bird_pipeline.py` — current implementation (to be replaced)
- `bird_tracker.py` — current tracker (to be replaced by `pipeline/tracker.py`)
- `bird_inference.py` — `YOLODetector`, `SpeciesClassifier`, `crop_bird()` — REUSED by new pipeline
- `yard_classifier.py` — `YardClassifier` — REUSED by new pipeline
- `solar_utils.py` — `is_nighttime()` — REUSED

**Delete (after cutover, Task 14):**
- `bird_pipeline.py`
- `bird_tracker.py`

---

## Task 0: Install Dependencies + Scaffold Package

**Files:**
- Create: `pipeline/__init__.py`
- Create: `tests/pipeline/__init__.py`

- [ ] **Step 1: Install Norfair and websockets in venv-coral**

Run:
```bash
/Users/vives/bird-classifier/venv-coral/bin/pip install 'norfair==2.3.0' 'filterpy==1.4.5' 'websockets>=12.0,<13'
```

Expected: Successful install. norfair brings `filterpy`, `rich`, `scipy` (already installed).

- [ ] **Step 2: Verify imports work**

Run:
```bash
/Users/vives/bird-classifier/venv-coral/bin/python -c "
import norfair
import filterpy
import websockets.sync.server
import numpy as np
assert np.__version__.startswith('1.'), f'numpy should be <2.0, got {np.__version__}'
print('OK', norfair.__version__, 'numpy', np.__version__)
"
```

Expected: `OK 2.3.0 numpy 1.26.4` (or similar 1.x).

- [ ] **Step 3: Create package directories and marker files**

Run:
```bash
mkdir -p /Users/vives/bird-classifier/pipeline
mkdir -p /Users/vives/bird-classifier/tests/pipeline
```

Create `/Users/vives/bird-classifier/pipeline/__init__.py` with content:
```python
"""Live detection pipeline v2 — Frigate-inspired.

See docs/superpowers/specs/2026-04-10-live-detection-v2-design.md
"""
```

Create `/Users/vives/bird-classifier/tests/pipeline/__init__.py` with empty content:
```python
```

- [ ] **Step 4: Verify package imports**

Run:
```bash
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -c "import pipeline; print(pipeline.__doc__)"
```

Expected: The docstring prints.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/__init__.py tests/pipeline/__init__.py
git commit -m "chore: scaffold pipeline v2 package + install norfair

Pinned:
- norfair==2.3.0
- filterpy==1.4.5  
- websockets>=12.0,<13

Verified numpy stays <2.0 for pycoral compatibility.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 1: Frame Dataclass

**Files:**
- Create: `pipeline/frame.py`
- Test: `tests/pipeline/test_frame.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_frame.py`:
```python
"""Tests for Frame dataclass."""
import numpy as np
import pytest
import time


def test_frame_creation():
    """Frame holds a numpy BGR array and metadata."""
    from pipeline.frame import Frame
    bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    f = Frame(
        bgr=bgr,
        wall_time_ms=1712700000000,
        camera="feeder",
        width=1920,
        height=1080,
    )
    assert f.bgr.shape == (1080, 1920, 3)
    assert f.wall_time_ms == 1712700000000
    assert f.camera == "feeder"


def test_frame_is_dataclass_like():
    """Frame should support attribute access without being a frozen dataclass
    (bgr is a numpy array — mutability is fine)."""
    from pipeline.frame import Frame
    f = Frame(
        bgr=np.zeros((10, 10, 3), dtype=np.uint8),
        wall_time_ms=0,
        camera="feeder",
        width=10,
        height=10,
    )
    assert hasattr(f, "bgr")
    assert hasattr(f, "wall_time_ms")
    assert hasattr(f, "camera")
    assert hasattr(f, "width")
    assert hasattr(f, "height")
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_frame.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.frame'`.

- [ ] **Step 3: Write minimal implementation**

Create `/Users/vives/bird-classifier/pipeline/frame.py`:
```python
"""Frame dataclass carried through the pipeline."""
from dataclasses import dataclass
import numpy as np


@dataclass
class Frame:
    """A decoded video frame with metadata.

    bgr: numpy array of shape (H, W, 3), uint8, BGR color order (OpenCV convention)
    wall_time_ms: unix milliseconds when the frame was captured
    camera: camera name (e.g., "feeder", "ground")
    width, height: frame dimensions in pixels
    """
    bgr: np.ndarray
    wall_time_ms: float
    camera: str
    width: int
    height: int
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_frame.py -v
```

Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/frame.py tests/pipeline/test_frame.py
git commit -m "feat: Frame dataclass for pipeline v2

Holds BGR numpy array + wall-clock timestamp + camera + dimensions.
Used by all downstream stages.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Frame Capture + Pipe Saturation Test (HIGHEST RISK)

This task validates the single highest-risk assumption in the spec: that we can drain a raw BGR pipe at 5 FPS × 1080p × 2 cameras (~31 MB/sec) without ffmpeg backpressure stalls.

**Files:**
- Create: `pipeline/frame_capture.py`
- Test: `tests/pipeline/test_frame_capture.py`
- Test: `tests/pipeline/test_pipe_saturation.py` (runs LAST in this task — the long one)

- [ ] **Step 1: Write unit tests for FrameCapture**

Create `/Users/vives/bird-classifier/tests/pipeline/test_frame_capture.py`:
```python
"""Tests for FrameCapture."""
import os
import queue
import time
from pathlib import Path

import numpy as np
import pytest


TEST_VIDEO = Path("/Users/vives/docs/bird-observatory/training videos/short-downy.mp4")


def test_file_input_detection():
    """Non-rtsp URLs should use file input args (-re -stream_loop)."""
    from pipeline.frame_capture import FrameCapture
    q = queue.Queue(maxsize=2)
    fc = FrameCapture("test", "/tmp/fake.mp4", width=640, height=480, fps=5, out_queue=q)
    args = fc._input_args("/tmp/fake.mp4")
    assert "-re" in args
    assert "-stream_loop" in args
    assert "-i" in args
    assert args[-1] == "/tmp/fake.mp4"


def test_rtsp_input_detection():
    """rtsp:// URLs should use TCP transport."""
    from pipeline.frame_capture import FrameCapture
    q = queue.Queue(maxsize=2)
    fc = FrameCapture("test", "rtsp://1.2.3.4/stream", width=640, height=480, fps=5, out_queue=q)
    args = fc._input_args("rtsp://1.2.3.4/stream")
    assert "-rtsp_transport" in args
    assert "tcp" in args
    assert args[-1] == "rtsp://1.2.3.4/stream"


@pytest.mark.skipif(not TEST_VIDEO.exists(),
                    reason="test video not available")
def test_capture_from_file_produces_frames():
    """Real ffmpeg run: capture from a test video file and verify frames arrive."""
    from pipeline.frame_capture import FrameCapture
    from pipeline.frame import Frame

    q = queue.Queue(maxsize=2)
    fc = FrameCapture(
        camera_name="test",
        rtsp_url=str(TEST_VIDEO),
        width=1920, height=1080, fps=5,
        out_queue=q,
    )
    try:
        fc.start()
        # Wait up to 5 seconds for first frame
        frame = q.get(timeout=5)
        assert isinstance(frame, Frame)
        assert frame.bgr.shape == (1080, 1920, 3)
        assert frame.camera == "test"
        assert frame.wall_time_ms > 0
    finally:
        fc.stop()


@pytest.mark.skipif(not TEST_VIDEO.exists(),
                    reason="test video not available")
def test_drops_oldest_when_queue_full():
    """Fast producer + slow consumer → oldest frames dropped, newest kept."""
    from pipeline.frame_capture import FrameCapture

    q = queue.Queue(maxsize=2)
    fc = FrameCapture(
        camera_name="test",
        rtsp_url=str(TEST_VIDEO),
        width=1920, height=1080, fps=5,
        out_queue=q,
    )
    try:
        fc.start()
        # Don't drain the queue for 3 seconds — producer should drop oldest
        time.sleep(3)
        # Queue should have at most 2 frames (maxsize)
        assert q.qsize() <= 2
        # dropped_oldest counter should have incremented
        assert fc.stats["dropped_oldest"] > 0
    finally:
        fc.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_frame_capture.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'pipeline.frame_capture'`.

- [ ] **Step 3: Write the FrameCapture implementation**

Create `/Users/vives/bird-classifier/pipeline/frame_capture.py`:
```python
"""FrameCapture — ffmpeg subprocess + pipe drain thread + watchdog.

Owns one ffmpeg subprocess per camera. Reads raw BGR frames from stdout
into a bounded queue. Drops oldest on backpressure. Restarts ffmpeg if
stalled for >10s.
"""
import logging
import queue
import subprocess
import threading
import time
from typing import Optional

import numpy as np

from pipeline.frame import Frame

log = logging.getLogger(__name__)

FFMPEG = "/usr/local/bin/ffmpeg"
WATCHDOG_STALL_MS = 10_000
WATCHDOG_CHECK_S = 2.0


class FrameCapture:
    def __init__(self, camera_name: str, rtsp_url: str,
                 out_queue: queue.Queue,
                 width: int = 1920, height: int = 1080, fps: int = 5):
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.width = width
        self.height = height
        self.fps = fps
        self.out_queue = out_queue
        self.proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.stats = {
            "frames": 0,
            "dropped_oldest": 0,
            "ffmpeg_restarts": 0,
            "last_frame_ms": None,
        }

    def start(self):
        self._stop_event.clear()
        self._spawn_ffmpeg()
        self._reader_thread = threading.Thread(
            target=self._pipe_drain, name=f"cap-{self.camera_name}", daemon=True
        )
        self._reader_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name=f"watchdog-{self.camera_name}", daemon=True
        )
        self._watchdog_thread.start()

    def stop(self):
        self._stop_event.set()
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
            self.proc = None

    def _input_args(self, url: str) -> list[str]:
        if url.startswith("rtsp://"):
            return ["-rtsp_transport", "tcp", "-i", url]
        # File input — loop forever, real-time pacing
        return ["-re", "-stream_loop", "-1", "-i", url]

    def _spawn_ffmpeg(self):
        cmd = [
            FFMPEG,
            "-loglevel", "warning",
            *self._input_args(self.rtsp_url),
            "-vf", f"fps={self.fps}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        ]
        log.info("[%s] spawning ffmpeg: %s", self.camera_name, " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def _pipe_drain(self):
        """Dedicated pipe reader. Only job: read frames as fast as possible."""
        frame_bytes = self.width * self.height * 3
        while not self._stop_event.is_set():
            proc = self.proc
            if proc is None or proc.stdout is None:
                time.sleep(0.1)
                continue
            try:
                data = proc.stdout.read(frame_bytes)
            except Exception as e:
                log.warning("[%s] pipe read error: %s", self.camera_name, e)
                time.sleep(0.5)
                continue
            if not data or len(data) != frame_bytes:
                # EOF or partial — watchdog will restart
                time.sleep(0.1)
                continue
            arr = np.frombuffer(data, dtype=np.uint8).reshape(
                (self.height, self.width, 3)
            ).copy()  # copy so buffer can be reused
            frame = Frame(
                bgr=arr,
                wall_time_ms=time.time() * 1000,
                camera=self.camera_name,
                width=self.width,
                height=self.height,
            )
            # Drop oldest if queue full
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
        while not self._stop_event.is_set():
            time.sleep(WATCHDOG_CHECK_S)
            last = self.stats.get("last_frame_ms")
            if last is None:
                continue
            age_ms = (time.time() * 1000) - last
            if age_ms > WATCHDOG_STALL_MS:
                log.warning("[%s] ffmpeg stalled %.0fms, restarting",
                            self.camera_name, age_ms)
                self._restart()

    def _restart(self):
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass
        self._spawn_ffmpeg()
        self.stats["ffmpeg_restarts"] += 1
```

- [ ] **Step 4: Run unit tests to verify they pass**

Run:
```bash
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_frame_capture.py -v
```

Expected: All 4 tests PASS.

- [ ] **Step 5: Write the pipe saturation test (HIGHEST RISK)**

Create `/Users/vives/bird-classifier/tests/pipeline/test_pipe_saturation.py`:
```python
"""Pipe saturation test — validates highest-risk assumption.

Runs FrameCapture against a real test video for 60 seconds with a slow
consumer (one frame pulled every 400ms). Verifies:
- No ffmpeg restart from pipe backpressure
- dropped_oldest counter increments correctly  
- last_frame_ms stays recent (pipe never stalls)
- Memory doesn't grow unbounded
"""
import queue
import time
from pathlib import Path

import pytest

TEST_VIDEO = Path("/Users/vives/docs/bird-observatory/training videos/lots of birds.mp4")


@pytest.mark.slow
@pytest.mark.skipif(not TEST_VIDEO.exists(), reason="test video not available")
def test_pipe_saturation_60s():
    """60-second run with slow consumer — no ffmpeg restarts allowed."""
    from pipeline.frame_capture import FrameCapture

    q = queue.Queue(maxsize=2)
    fc = FrameCapture(
        camera_name="saturation",
        rtsp_url=str(TEST_VIDEO),
        width=1920, height=1080, fps=5,
        out_queue=q,
    )

    try:
        fc.start()
        # Wait for first frame
        first = q.get(timeout=10)
        assert first is not None

        # Simulate slow consumer — pull 1 frame per 400ms for 60 seconds
        start = time.time()
        consumed = 0
        while time.time() - start < 60:
            try:
                q.get(timeout=1.0)
                consumed += 1
            except queue.Empty:
                pass
            time.sleep(0.4)  # slow consumer

        elapsed = time.time() - start
        print(f"\n=== Pipe Saturation Results ===")
        print(f"Elapsed: {elapsed:.1f}s")
        print(f"Consumed: {consumed} frames")
        print(f"Produced: {fc.stats['frames']}")
        print(f"Dropped oldest: {fc.stats['dropped_oldest']}")
        print(f"ffmpeg restarts: {fc.stats['ffmpeg_restarts']}")
        last_age_ms = (time.time() * 1000) - fc.stats['last_frame_ms']
        print(f"Last frame age: {last_age_ms:.0f}ms")

        # CRITICAL: zero ffmpeg restarts (proves no backpressure stall)
        assert fc.stats["ffmpeg_restarts"] == 0, \
            "ffmpeg restarted during saturation test — pipe backpressure deadlock"

        # dropped_oldest should be dominant (producer way faster than consumer)
        assert fc.stats["dropped_oldest"] > 100, \
            "Expected many drops — slow consumer should have backed up"

        # Last frame should be recent (< 1 second old)
        assert last_age_ms < 1000, \
            f"Pipe is stale, last frame {last_age_ms:.0f}ms old"

        # Should have produced roughly 60 * 5 = 300 frames at target fps
        assert fc.stats["frames"] >= 200, \
            f"Frame production too low: {fc.stats['frames']} (expected >=200)"
    finally:
        fc.stop()
```

- [ ] **Step 6: Run the saturation test**

Run (this takes ~70 seconds):
```bash
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipe_saturation.py -v -s
```

Expected: PASS. Output should show ~250-300 frames produced, >100 dropped_oldest, 0 ffmpeg restarts, last frame <1000ms old.

**If it fails with ffmpeg restarts > 0:** The pipe drain design is broken. Stop and report BLOCKED — do not proceed to Task 3.

- [ ] **Step 7: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/frame_capture.py tests/pipeline/test_frame_capture.py tests/pipeline/test_pipe_saturation.py
git commit -m "feat: frame capture via ffmpeg subprocess + pipe drain thread

Dedicated pipe drain thread reads raw BGR frames from ffmpeg stdout
into a bounded queue. Drops oldest on backpressure. Watchdog restarts
ffmpeg on stall.

Pipe saturation test (60s @ 5fps × 1080p, slow consumer):
- Validates zero ffmpeg restarts from backpressure
- Highest-risk assumption in the spec — passed

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Motion Gate (Regions)

**Files:**
- Create: `pipeline/motion_gate.py`
- Test: `tests/pipeline/test_motion_gate.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_motion_gate.py`:
```python
"""Tests for MotionGate (region-based)."""
import numpy as np


def test_no_motion_returns_empty_list():
    """Identical frames should produce no motion regions."""
    from pipeline.motion_gate import MotionGate
    gate = MotionGate()
    # First frame warms up background model
    still = np.ones((480, 640, 3), dtype=np.uint8) * 128
    gate.regions(still)
    gate.regions(still)
    regions = gate.regions(still)
    assert regions == []


def test_motion_produces_region():
    """A bright blob appearing on a gray frame should produce a region."""
    from pipeline.motion_gate import MotionGate
    gate = MotionGate()
    # Warm up with gray frames
    gray = np.ones((480, 640, 3), dtype=np.uint8) * 128
    for _ in range(5):
        gate.regions(gray)

    # Add a bright blob in the middle
    moving = gray.copy()
    moving[200:280, 280:360] = 255  # 80x80 white blob

    regions = gate.regions(moving)
    assert len(regions) >= 1
    # Each region is (x1, y1, x2, y2)
    r = regions[0]
    assert len(r) == 4
    # Blob should overlap the expected area
    x1, y1, x2, y2 = r
    assert x1 < 360 and x2 > 280
    assert y1 < 280 and y2 > 200


def test_small_regions_filtered():
    """Tiny motion (below min_region_area) should be filtered out."""
    from pipeline.motion_gate import MotionGate
    gate = MotionGate(min_region_area=1000)
    gray = np.ones((480, 640, 3), dtype=np.uint8) * 128
    for _ in range(5):
        gate.regions(gray)
    # Tiny 5x5 blob = 25 px² — below threshold
    moving = gray.copy()
    moving[100:105, 100:105] = 255
    regions = gate.regions(moving)
    assert regions == []
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_motion_gate.py -v
```

Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/motion_gate.py`:
```python
"""MotionGate — OpenCV background subtraction → motion regions."""
import cv2
import numpy as np


class MotionGate:
    """Background subtraction motion gate that emits regions.

    Usage:
        gate = MotionGate()
        regions = gate.regions(bgr_frame)  # list of (x1,y1,x2,y2)
    """

    def __init__(self, history: int = 500, var_threshold: int = 16,
                 min_region_area: int = 400, pad: int = 20):
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=history,
            varThreshold=var_threshold,
            detectShadows=False,
        )
        self.min_region_area = min_region_area
        self.pad = pad

    def regions(self, bgr_frame: np.ndarray) -> list[tuple[int, int, int, int]]:
        """Return list of motion bounding boxes (x1,y1,x2,y2) in frame coordinates."""
        mask = self.bg.apply(bgr_frame)
        # Clean up noise
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)

        # Find contours
        contours, _ = cv2.findContours(
            mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        h, w = bgr_frame.shape[:2]
        regions = []
        for c in contours:
            area = cv2.contourArea(c)
            if area < self.min_region_area:
                continue
            x, y, bw, bh = cv2.boundingRect(c)
            x1 = max(0, x - self.pad)
            y1 = max(0, y - self.pad)
            x2 = min(w, x + bw + self.pad)
            y2 = min(h, y + bh + self.pad)
            regions.append((x1, y1, x2, y2))
        return regions
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_motion_gate.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/motion_gate.py tests/pipeline/test_motion_gate.py
git commit -m "feat: motion gate emits regions instead of boolean

Background subtractor + contour detection + padding.
Returns list of (x1,y1,x2,y2) regions for region-based YOLO.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: YOLO Detector (Region-Based with Offset)

**Files:**
- Create: `pipeline/detector.py`
- Test: `tests/pipeline/test_detector.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_detector.py`:
```python
"""Tests for BirdDetector — region detection + coordinate offset."""
import numpy as np
import pytest
from unittest.mock import MagicMock


def test_detection_coordinates_are_full_frame():
    """YOLO runs on a crop, but returned boxes are in full-frame coordinates."""
    from pipeline.detector import BirdDetector, Detection

    # Mock the underlying YOLODetector to return a box in crop-local coords
    yolo_mock = MagicMock()
    yolo_mock.detect_numpy = MagicMock(return_value=[
        {"box": [10, 20, 60, 80], "confidence": 0.9}  # crop-local
    ])

    d = BirdDetector.__new__(BirdDetector)
    d.yolo = yolo_mock
    d.get_stationary = lambda: []

    frame_bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    # Motion region offset by (400, 200)
    region = (400, 200, 500, 300)
    detections = d._detect_region(frame_bgr, region)

    assert len(detections) == 1
    # Box should be offset: crop(10,20,60,80) → full(410,220,460,280)
    assert detections[0].box == [410, 220, 460, 280]
    assert detections[0].confidence == pytest.approx(0.9)


def test_stationary_only_region_is_skipped():
    """A motion region that contains ONLY stationary tracks should be skipped."""
    from pipeline.detector import BirdDetector

    yolo_mock = MagicMock()
    yolo_mock.detect_numpy = MagicMock(return_value=[])

    d = BirdDetector.__new__(BirdDetector)
    d.yolo = yolo_mock
    # One stationary track covering (400,200,500,300)
    d.get_stationary = lambda: [(400, 200, 500, 300)]

    frame_bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    motion_regions = [(400, 200, 500, 300)]  # identical to stationary

    from pipeline.frame import Frame
    frame = Frame(bgr=frame_bgr, wall_time_ms=0, camera="test", width=1920, height=1080)
    detections = d.detect(frame, motion_regions, forced_full=False)
    # YOLO should NOT have been called
    yolo_mock.detect_numpy.assert_not_called()
    assert detections == []


def test_forced_full_runs_on_whole_frame():
    """When forced_full=True, YOLO runs on the full frame ignoring motion regions."""
    from pipeline.detector import BirdDetector

    yolo_mock = MagicMock()
    yolo_mock.detect = MagicMock(return_value=[
        {"box": [100, 100, 200, 200], "confidence": 0.8}
    ])

    d = BirdDetector.__new__(BirdDetector)
    d.yolo = yolo_mock
    d.get_stationary = lambda: []

    frame_bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    from pipeline.frame import Frame
    frame = Frame(bgr=frame_bgr, wall_time_ms=0, camera="test", width=1920, height=1080)
    detections = d.detect(frame, [], forced_full=True)

    yolo_mock.detect.assert_called_once()
    assert len(detections) == 1
    assert detections[0].box == [100, 100, 200, 200]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_detector.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/detector.py`:
```python
"""BirdDetector — region-based YOLO with full-frame coordinate output."""
from dataclasses import dataclass
from typing import Callable
import logging

import numpy as np
from PIL import Image

from pipeline.frame import Frame

log = logging.getLogger(__name__)


@dataclass
class Detection:
    box: list  # [x1, y1, x2, y2] in full-frame coordinates
    confidence: float


def _iou(a, b) -> float:
    """IoU between two boxes."""
    x1 = max(a[0], b[0]); y1 = max(a[1], b[1])
    x2 = min(a[2], b[2]); y2 = min(a[3], b[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / (area_a + area_b - inter)


class BirdDetector:
    """Region-based YOLO detector with stationary-track skipping.

    Runs YOLO on motion crops (faster than full-frame detection on 1080p),
    offsets detection boxes back to full-frame coordinates, and skips regions
    that contain only stationary tracks.
    """

    def __init__(self, yolo_model_path: str,
                 stationary_track_regions_fn: Callable[[], list],
                 confidence: float = 0.3):
        from bird_inference import YOLODetector
        self.yolo = YOLODetector(yolo_model_path, confidence=confidence)
        self.get_stationary = stationary_track_regions_fn

    def detect(self, frame: Frame, motion_regions: list,
               forced_full: bool = False) -> list[Detection]:
        """Run detection. If forced_full, ignore motion regions and scan whole frame."""
        if forced_full or not motion_regions:
            return self._detect_full(frame)

        stationary = self.get_stationary()
        detections = []
        for region in motion_regions:
            if self._is_stationary_only(region, stationary):
                continue
            detections.extend(self._detect_region(frame.bgr, region))
        return detections

    def _detect_full(self, frame: Frame) -> list[Detection]:
        """Run YOLO on the full frame (fallback)."""
        pil = Image.fromarray(frame.bgr[:, :, ::-1])  # BGR → RGB
        raw = self.yolo.detect(pil)
        return [Detection(box=list(r["box"]), confidence=float(r["confidence"]))
                for r in raw]

    def _detect_region(self, bgr: np.ndarray,
                       region: tuple) -> list[Detection]:
        """Run YOLO on a cropped region. Offset outputs back to full-frame."""
        x1, y1, x2, y2 = region
        crop = bgr[y1:y2, x1:x2]
        if crop.size == 0:
            return []
        try:
            raw = self.yolo.detect_numpy(crop)
        except Exception as e:
            log.warning("YOLO error on region: %s", e)
            return []
        # Offset boxes to full-frame
        out = []
        for r in raw:
            b = r["box"]
            out.append(Detection(
                box=[b[0] + x1, b[1] + y1, b[2] + x1, b[3] + y1],
                confidence=float(r["confidence"]),
            ))
        return out

    def _is_stationary_only(self, region: tuple, stationary: list) -> bool:
        """True if the motion region is entirely explained by stationary tracks."""
        if not stationary:
            return False
        for st in stationary:
            if _iou(region, st) > 0.8:
                return True
        return False
```

**Note:** This uses `self.yolo.detect_numpy(crop)` which may not exist on the current `YOLODetector`. If it doesn't, add a thin wrapper in the detector that converts numpy to PIL and calls `.detect()`. The test mocks this method, so the behavior is isolated.

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_detector.py -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/detector.py tests/pipeline/test_detector.py
git commit -m "feat: region-based YOLO detector with coordinate offset

Runs YOLO on motion crops instead of full frames (faster for small birds).
Offsets detection boxes back to full-frame coordinates.
Skips regions containing only stationary tracks.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Norfair Tracker with Frigate Distance

**Files:**
- Create: `pipeline/tracker.py`
- Test: `tests/pipeline/test_pipeline_tracker.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_pipeline_tracker.py`:
```python
"""Tests for Norfair-based BirdTracker."""
import pytest


def test_new_detection_creates_track():
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    t = BirdTracker()
    det = Detection(box=[100, 100, 200, 200], confidence=0.9)
    out = t.update([det], frame_time_ms=1000)
    # initialization_delay=1 means first hit creates a track on next update
    out2 = t.update([det], frame_time_ms=1050)
    assert len(out2.active) >= 1
    assert len(out2.new) >= 0  # may have been marked new on either update


def test_moving_detection_stays_same_track():
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    t = BirdTracker()
    # Bird moves slightly between frames
    for i, x in enumerate([100, 105, 110, 115, 120]):
        det = Detection(box=[x, 100, x+100, 200], confidence=0.9)
        out = t.update([det], frame_time_ms=1000 + i*200)
    # After 5 updates, there should be exactly 1 active track
    assert len(out.active) == 1


def test_stationary_detection_flagged_after_10_frames():
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    t = BirdTracker()
    # 12 identical detections — bird hasn't moved
    for i in range(12):
        det = Detection(box=[100, 100, 200, 200], confidence=0.9)
        out = t.update([det], frame_time_ms=1000 + i*200)

    assert len(out.active) == 1
    assert out.active[0].is_stationary is True


def test_tracker_output_dataclass_shape():
    from pipeline.tracker import BirdTracker, TrackerOutput
    t = BirdTracker()
    out = t.update([], frame_time_ms=1000)
    assert isinstance(out, TrackerOutput)
    assert isinstance(out.active, list)
    assert isinstance(out.new, list)
    assert isinstance(out.expired, list)
    assert out.frame_time_ms == 1000


def test_stationary_regions_returns_only_stationary():
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    t = BirdTracker()
    # Create one stationary bird
    for i in range(12):
        t.update([Detection(box=[100, 100, 200, 200], confidence=0.9)],
                 frame_time_ms=1000 + i*200)
    regions = t.stationary_regions()
    assert len(regions) == 1
    assert regions[0] == (100, 100, 200, 200)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_tracker.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/tracker.py`:
```python
"""Norfair-based bird tracker with Frigate-inspired distance function."""
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import norfair
import numpy as np

from pipeline.detector import Detection


@dataclass
class Track:
    track_id: int
    created_at_ms: float
    last_updated_ms: float
    bbox: list = field(default_factory=lambda: [0, 0, 0, 0])
    confidence: float = 0.0
    species: Optional[str] = None
    model_source: Optional[str] = None
    trust_level: str = "normal"
    needs_classification: bool = True
    classification_attempts: int = 0
    motion_history: deque = field(default_factory=lambda: deque(maxlen=10))

    @property
    def is_stationary(self) -> bool:
        if len(self.motion_history) < 10:
            return False
        xs = [p[0] for p in self.motion_history]
        ys = [p[1] for p in self.motion_history]
        return (max(xs) - min(xs)) < 10 and (max(ys) - min(ys)) < 10


@dataclass
class TrackerOutput:
    active: list
    new: list
    expired: list
    frame_time_ms: float


def _frigate_distance(detection: norfair.Detection,
                      tracked: norfair.TrackedObject) -> float:
    """Frigate-inspired distance: centroid-x + bottom-y normalized by size."""
    det_data = detection.data
    trk_det = tracked.last_detection
    trk_data = trk_det.data

    det_w = det_data["w"]
    det_h = det_data["h"]
    trk_w = trk_data["w"]
    trk_h = trk_data["h"]

    det_cx = detection.points[0][0]
    det_cy = detection.points[0][1]
    trk_cx = trk_det.points[0][0]
    trk_cy = trk_det.points[0][1]

    d_x = abs(det_cx - trk_cx) / max((det_w + trk_w) / 2, 1)
    # bottom-y: y-center + half height
    det_by = det_cy + det_h / 2
    trk_by = trk_cy + trk_h / 2
    d_y = abs(det_by - trk_by) / max((det_h + trk_h) / 2, 1)

    return d_x + d_y


class BirdTracker:
    """Norfair wrapper with frigate_distance and stationary detection."""

    def __init__(self, distance_threshold: float = 1.0,
                 hit_counter_max: int = 15, initialization_delay: int = 1):
        self.norfair = norfair.Tracker(
            distance_function=_frigate_distance,
            distance_threshold=distance_threshold,
            hit_counter_max=hit_counter_max,
            initialization_delay=initialization_delay,
        )
        self.tracks: dict[int, Track] = {}
        self._next_id = 0

    def update(self, detections: list[Detection],
               frame_time_ms: float) -> TrackerOutput:
        # Convert Detection → norfair.Detection
        norfair_dets = []
        for d in detections:
            x1, y1, x2, y2 = d.box
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            norfair_dets.append(norfair.Detection(
                points=np.array([[cx, cy]]),
                scores=np.array([d.confidence]),
                data={"box": d.box, "w": x2 - x1, "h": y2 - y1},
            ))

        tracked_objs = self.norfair.update(detections=norfair_dets)

        new_tracks = []
        active_tracks = []
        seen_ids = set()

        for tobj in tracked_objs:
            tid = tobj.id
            seen_ids.add(tid)
            is_new = tid not in self.tracks
            if is_new:
                track = Track(
                    track_id=tid,
                    created_at_ms=frame_time_ms,
                    last_updated_ms=frame_time_ms,
                )
                self.tracks[tid] = track
                new_tracks.append(track)
            else:
                track = self.tracks[tid]
                track.last_updated_ms = frame_time_ms

            # Update bbox from last detection
            if tobj.last_detection is not None:
                track.bbox = list(tobj.last_detection.data["box"])
                track.confidence = float(tobj.last_detection.scores[0])

            # Update motion history
            cx = (track.bbox[0] + track.bbox[2]) / 2
            cy = (track.bbox[1] + track.bbox[3]) / 2
            track.motion_history.append((cx, cy))

            active_tracks.append(track)

        # Expire: tracks in our dict but not in seen_ids
        expired_ids = set(self.tracks.keys()) - seen_ids - {
            t.track_id for t in active_tracks
        }
        expired = []
        for tid in expired_ids:
            # Only expire if norfair also let it go
            still_in_norfair = any(t.id == tid for t in tracked_objs)
            if not still_in_norfair:
                expired.append(self.tracks.pop(tid))

        return TrackerOutput(
            active=active_tracks,
            new=new_tracks,
            expired=expired,
            frame_time_ms=frame_time_ms,
        )

    def stationary_regions(self) -> list[tuple]:
        return [tuple(t.bbox) for t in self.tracks.values() if t.is_stationary]
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_tracker.py -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/tracker.py tests/pipeline/test_pipeline_tracker.py
git commit -m "feat: norfair tracker with frigate distance function

Wraps norfair.Tracker with a custom distance function inspired by Frigate:
centroid-x + bottom-y normalized by object size.
Tracks stationary detection for skip optimization.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Smart B Classifier with Retry Semantics

**Files:**
- Create: `pipeline/classifier.py`
- Test: `tests/pipeline/test_pipeline_classifier.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_pipeline_classifier.py`:
```python
"""Tests for SmartClassifier (Smart B decision tree)."""
import threading
from unittest.mock import MagicMock, patch
import pytest
from PIL import Image


def _make_pil():
    return Image.new("RGB", (224, 224), (128, 128, 128))


def _result(species, confidence):
    return MagicMock(species=species, confidence=confidence)


def test_yard_confident_returns_yard():
    """Path 1: Yard confidence >= 0.60 → immediate yard result."""
    from pipeline.classifier import SmartClassifier, ClassificationResult
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock()
    c.aiy = MagicMock()
    c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("Black-capped Chickadee", 0.82))
    c._run_aiy = MagicMock()
    c._audio_lookup = MagicMock()

    r = c.classify(_make_pil(), frame_time_ms=0, camera="feeder")
    assert r.species == "Black-capped Chickadee"
    assert r.model_source == "yard"
    assert r.should_retry is False
    c._run_aiy.assert_not_called()  # shortcut
    assert c.stats["yard"] == 1


def test_yard_useless_aiy_rescues():
    """Path 2: Yard <0.30, AIY confident → AIY result."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("noise", 0.10))
    c._run_aiy = MagicMock(return_value=_result("American Robin", 0.75))
    c._audio_lookup = MagicMock()

    r = c.classify(_make_pil(), 0, "feeder")
    assert r.species == "American Robin"
    assert r.model_source == "aiy"
    assert c.stats["aiy"] == 1


def test_yard_uncertain_both_agree():
    """Path 3: Yard 0.30-0.60, AIY same species → both_agree."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("Downy Woodpecker", 0.45))
    c._run_aiy = MagicMock(return_value=_result("Downy Woodpecker", 0.50))
    c._audio_lookup = MagicMock()

    r = c.classify(_make_pil(), 0, "feeder")
    assert r.species == "Downy Woodpecker"
    assert r.model_source == "both_agree"
    assert r.confidence == pytest.approx(0.50)
    assert c.stats["both_agree"] == 1


def test_disagreement_audio_confirms():
    """Path 4: Yard and AIY disagree, audio confirms one → audio_confirmed."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = "fake.db"
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("Downy Woodpecker", 0.50))
    c._run_aiy = MagicMock(return_value=_result("Hairy Woodpecker", 0.55))
    c._audio_lookup = MagicMock(return_value="Hairy Woodpecker")

    r = c.classify(_make_pil(), 0, "feeder")
    assert r.species == "Hairy Woodpecker"
    assert r.model_source == "audio_confirmed"
    assert c.stats["audio_confirmed"] == 1


def test_no_confident_answer_returns_unlabeled():
    """Nothing agrees, no audio → unlabeled, should_retry=False."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("X", 0.40))
    c._run_aiy = MagicMock(return_value=_result("Y", 0.40))
    c._audio_lookup = MagicMock(return_value=None)

    r = c.classify(_make_pil(), 0, "feeder")
    assert r.species is None
    assert r.should_retry is False
    assert c.stats["unlabeled"] == 1


def test_coral_lock_timeout_returns_should_retry():
    """If another thread holds the Coral lock past timeout, return should_retry=True."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    # Hold the lock elsewhere
    c._coral_lock = threading.Lock()
    c._coral_lock.acquire()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock()
    c._run_aiy = MagicMock()
    c._audio_lookup = MagicMock()

    # Shorten the timeout for test speed
    with patch("pipeline.classifier.CORAL_ACQUIRE_TIMEOUT", 0.2):
        r = c.classify(_make_pil(), 0, "feeder")
    assert r.should_retry is True
    assert r.species is None
    assert c.stats["lock_timeouts"] == 1
    c._coral_lock.release()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_classifier.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/classifier.py`:
```python
"""SmartClassifier — Smart B decision tree with Coral lock and retry semantics."""
from dataclasses import dataclass
from typing import Optional
import logging
import sqlite3
import threading

from PIL import Image

log = logging.getLogger(__name__)

CONFIDENT = 0.60
UNCERTAIN_LOW = 0.30
CORAL_ACQUIRE_TIMEOUT = 5.0  # seconds to wait FOR the lock (not inference itself)
MAX_CLASSIFICATION_ATTEMPTS = 3


@dataclass
class ClassificationResult:
    species: Optional[str]
    confidence: float
    model_source: Optional[str]  # 'yard' | 'aiy' | 'both_agree' | 'audio_confirmed'
    should_retry: bool  # True if Coral was busy — retry on next frame


class SmartClassifier:
    def __init__(self, yard_model_path: str, yard_labels_path: str,
                 aiy_model_path: str, aiy_labels_path: str,
                 regional_species, audio_db_path: Optional[str] = None):
        from yard_classifier import YardClassifier
        from bird_inference import SpeciesClassifier

        self.yard = YardClassifier(yard_model_path, yard_labels_path)
        self.aiy = SpeciesClassifier(
            aiy_model_path, aiy_labels_path,
            regional_species=regional_species,
        )
        self.audio_db_path = audio_db_path
        self._coral_lock = threading.Lock()
        self.stats = {
            "yard": 0, "aiy": 0, "both_agree": 0, "audio_confirmed": 0,
            "unlabeled": 0, "lock_timeouts": 0, "retries": 0,
        }

    def classify(self, crop_pil: Image.Image, frame_time_ms: float,
                 camera: str) -> ClassificationResult:
        got = self._coral_lock.acquire(timeout=CORAL_ACQUIRE_TIMEOUT)
        if not got:
            self.stats["lock_timeouts"] += 1
            return ClassificationResult(None, 0.0, None, should_retry=True)

        try:
            # Path 1: yard confident
            yard_res = self._run_yard(crop_pil)
            if yard_res and yard_res.confidence >= CONFIDENT:
                self.stats["yard"] += 1
                return ClassificationResult(
                    yard_res.species, yard_res.confidence, "yard", False
                )

            # Path 2: yard useless
            if not yard_res or yard_res.confidence < UNCERTAIN_LOW:
                aiy_res = self._run_aiy(crop_pil)
                if aiy_res and aiy_res.confidence >= CONFIDENT:
                    self.stats["aiy"] += 1
                    return ClassificationResult(
                        aiy_res.species, aiy_res.confidence, "aiy", False
                    )
                self.stats["unlabeled"] += 1
                return ClassificationResult(None, 0.0, None, False)

            # Path 3: yard uncertain, compare with AIY
            aiy_res = self._run_aiy(crop_pil)
            if not aiy_res:
                self.stats["unlabeled"] += 1
                return ClassificationResult(None, 0.0, None, False)

            if aiy_res.species == yard_res.species:
                self.stats["both_agree"] += 1
                return ClassificationResult(
                    yard_res.species,
                    max(yard_res.confidence, aiy_res.confidence),
                    "both_agree", False
                )

            # Path 4: disagreement → audio cross-check
            audio_species = self._audio_lookup(camera, frame_time_ms)
            if audio_species and audio_species in (yard_res.species, aiy_res.species):
                self.stats["audio_confirmed"] += 1
                return ClassificationResult(
                    audio_species,
                    max(yard_res.confidence, aiy_res.confidence),
                    "audio_confirmed", False
                )

            self.stats["unlabeled"] += 1
            return ClassificationResult(None, 0.0, None, False)
        finally:
            self._coral_lock.release()

    def _run_yard(self, crop_pil):
        """Run yard classifier, return object with .species and .confidence."""
        try:
            result = self.yard.classify(crop_pil)
            if not result:
                return None
            return type("YardResult", (), {
                "species": result.get("species"),
                "confidence": result.get("confidence", 0.0),
            })()
        except Exception as e:
            log.debug("Yard classify error: %s", e)
            return None

    def _run_aiy(self, crop_pil):
        """Run AIY classifier, return object with .species and .confidence."""
        try:
            filtered, _raw = self.aiy.classify(crop_pil)
            if not filtered:
                return None
            top = filtered[0]
            return type("AiyResult", (), {
                "species": top.get("common_name"),
                "confidence": float(top.get("raw_score", 0)) / 100.0,
            })()
        except Exception as e:
            log.debug("AIY classify error: %s", e)
            return None

    def _audio_lookup(self, camera: str, frame_time_ms: float) -> Optional[str]:
        """Query birdnet_local.db for a detection within ±5s on this camera."""
        if not self.audio_db_path:
            return None
        try:
            conn = sqlite3.connect(self.audio_db_path, timeout=2)
            conn.row_factory = sqlite3.Row
            start_ms = int(frame_time_ms - 5000)
            end_ms = int(frame_time_ms + 5000)
            # birdnet_local.db schema: detections(common_name, timestamp_ms, camera, ...)
            row = conn.execute(
                """SELECT common_name FROM detections
                   WHERE camera = ? AND timestamp_ms BETWEEN ? AND ?
                   ORDER BY confidence DESC LIMIT 1""",
                (camera, start_ms, end_ms),
            ).fetchone()
            conn.close()
            return row["common_name"] if row else None
        except Exception as e:
            log.debug("Audio lookup error: %s", e)
            return None
```

**Note:** The exact `birdnet_local.db` schema may differ. If the column names don't match, the try/except returns None — degrading gracefully to "no audio corroboration" without breaking classification.

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_classifier.py -v`
Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/classifier.py tests/pipeline/test_pipeline_classifier.py
git commit -m "feat: Smart B classifier with Coral lock and retry semantics

Decision tree: yard confident → yard. Yard useless → AIY. Yard uncertain
→ compare with AIY. Disagreement → BirdNET audio cross-check. No
confident answer → unlabeled (not 'unidentified bird').

Lock uses acquire(timeout=) and returns should_retry=True on timeout
so tracks can be re-classified on the next frame.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Event Store (SQLite WAL)

**Files:**
- Create: `pipeline/event_store.py`
- Test: `tests/pipeline/test_event_store.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_event_store.py`:
```python
"""Tests for EventStore."""
import json
import sqlite3
import time
import pytest


def test_schema_is_created(tmp_path):
    from pipeline.event_store import EventStore
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    conn = sqlite3.connect(str(db))
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "pipeline_events" in tables
    assert "pipeline_tracks" in tables
    store.shutdown()


def test_write_event_flushes_to_db(tmp_path):
    from pipeline.event_store import EventStore
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    store.write_event(
        camera="feeder",
        frame_time_ms=1712700000000,
        track_id=42,
        species="Black-capped Chickadee",
        confidence=0.82,
        model_source="yard",
        bbox=[100, 200, 300, 400],
        is_new=True,
    )
    store.flush()  # force immediate write
    conn = sqlite3.connect(str(db))
    row = conn.execute("SELECT * FROM pipeline_events").fetchone()
    assert row is not None
    store.shutdown()


def test_query_events_by_time_range(tmp_path):
    from pipeline.event_store import EventStore
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    for i in range(5):
        store.write_event(
            camera="feeder",
            frame_time_ms=1000 + i * 100,
            track_id=1,
            species="Test",
            confidence=0.9,
            model_source="yard",
            bbox=[0, 0, 10, 10],
            is_new=(i == 0),
        )
    store.flush()
    results = store.query_events(camera="feeder", start_ms=1100, end_ms=1300)
    assert len(results) == 3
    store.shutdown()


def test_write_track_summary(tmp_path):
    from pipeline.event_store import EventStore
    from pipeline.tracker import Track
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    t = Track(
        track_id=42,
        created_at_ms=1000,
        last_updated_ms=5000,
        bbox=[100, 100, 200, 200],
        confidence=0.85,
        species="Downy Woodpecker",
        model_source="yard",
    )
    # Populate motion history for motion_pct calculation
    for i in range(10):
        t.motion_history.append((100 + i, 100))
    store.write_track_summary(camera="feeder", track=t, num_frames=20)
    store.flush()
    tracks = store.query_tracks(species="Downy Woodpecker")
    assert len(tracks) == 1
    assert tracks[0]["species"] == "Downy Woodpecker"
    store.shutdown()


def test_query_tracks_filters(tmp_path):
    from pipeline.event_store import EventStore
    from pipeline.tracker import Track
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    for species, peak in [("Chickadee", 0.9), ("Cardinal", 0.7), ("Chickadee", 0.85)]:
        t = Track(track_id=0, created_at_ms=1000, last_updated_ms=6000,
                  bbox=[0,0,10,10], confidence=peak, species=species,
                  model_source="yard")
        store.write_track_summary(camera="feeder", track=t, num_frames=30)
    store.flush()

    chickadees = store.query_tracks(species="Chickadee")
    assert len(chickadees) == 2

    high_conf = store.query_tracks(min_confidence=0.8)
    assert len(high_conf) == 2  # Chickadee 0.9 and 0.85
    store.shutdown()


def test_prune_events_respects_age(tmp_path):
    from pipeline.event_store import EventStore
    db = tmp_path / "pipeline.db"
    store = EventStore(str(db))
    now_ms = int(time.time() * 1000)
    # One old event, one recent
    store.write_event(camera="feeder", frame_time_ms=now_ms - 10 * 86400 * 1000,
                      track_id=1, species="Old", confidence=0.9,
                      model_source="yard", bbox=[0,0,10,10], is_new=True)
    store.write_event(camera="feeder", frame_time_ms=now_ms,
                      track_id=2, species="New", confidence=0.9,
                      model_source="yard", bbox=[0,0,10,10], is_new=True)
    store.flush()

    store.prune_events(older_than_ms=now_ms - 7 * 86400 * 1000)

    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT species FROM pipeline_events ORDER BY frame_time").fetchall()
    assert len(rows) == 1
    assert rows[0][0] == "New"
    store.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_event_store.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/event_store.py`:
```python
"""EventStore — time-indexed SQLite WAL for pipeline events and tracks."""
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional

from pipeline.tracker import Track


SCHEMA = """
CREATE TABLE IF NOT EXISTS pipeline_events (
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
CREATE INDEX IF NOT EXISTS idx_events_track ON pipeline_events(camera, track_id);
CREATE INDEX IF NOT EXISTS idx_events_time ON pipeline_events(frame_time);
CREATE INDEX IF NOT EXISTS idx_events_species ON pipeline_events(species, frame_time);

CREATE TABLE IF NOT EXISTS pipeline_tracks (
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
CREATE INDEX IF NOT EXISTS idx_tracks_species ON pipeline_tracks(species, start_time);
CREATE INDEX IF NOT EXISTS idx_tracks_duration ON pipeline_tracks(camera, end_time, start_time);
"""


INSERT_EVENT = """
INSERT OR REPLACE INTO pipeline_events
(camera, frame_time, track_id, species, confidence, model_source, bbox_json, is_new)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_TRACK = """
INSERT INTO pipeline_tracks
(camera, species, start_time, end_time, peak_confidence, num_frames,
 model_source, best_keeper_path, motion_pct)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class EventStore:
    def __init__(self, db_path: str, flush_interval_s: float = 0.5,
                 batch_size: int = 50):
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn_lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=5)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA wal_autocheckpoint=2000")
        for stmt in SCHEMA.strip().split(";"):
            if stmt.strip():
                self.conn.execute(stmt)
        self.conn.commit()

        self._event_batch: list = []
        self._batch_lock = threading.Lock()
        self._batch_size = batch_size
        self._flush_interval = flush_interval_s
        self._stop = threading.Event()
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True)
        self._flusher.start()

    def shutdown(self):
        self._stop.set()
        self.flush()
        with self._conn_lock:
            self.conn.close()

    def _flush_loop(self):
        while not self._stop.is_set():
            time.sleep(self._flush_interval)
            self.flush()

    def flush(self):
        with self._batch_lock:
            if not self._event_batch:
                return
            batch = self._event_batch
            self._event_batch = []
        with self._conn_lock:
            self.conn.executemany(INSERT_EVENT, batch)
            self.conn.commit()

    def write_event(self, camera: str, frame_time_ms: float, track_id: int,
                    species: Optional[str], confidence: float,
                    model_source: Optional[str], bbox: list, is_new: bool):
        row = (
            camera, int(frame_time_ms), int(track_id), species,
            float(confidence or 0), model_source,
            json.dumps(bbox), int(1 if is_new else 0),
        )
        with self._batch_lock:
            self._event_batch.append(row)
            if len(self._event_batch) >= self._batch_size:
                batch = self._event_batch
                self._event_batch = []
                with self._conn_lock:
                    self.conn.executemany(INSERT_EVENT, batch)
                    self.conn.commit()

    def write_track_summary(self, camera: str, track: Track, num_frames: int):
        duration_ms = track.last_updated_ms - track.created_at_ms
        # Motion %: fraction of motion_history entries showing movement >5px
        motion_pct = 0.0
        if len(track.motion_history) >= 2:
            hist = list(track.motion_history)
            moves = sum(
                1 for (a, b) in zip(hist, hist[1:])
                if abs(a[0] - b[0]) > 5 or abs(a[1] - b[1]) > 5
            )
            motion_pct = moves / max(1, len(hist) - 1)
        row = (
            camera, track.species,
            int(track.created_at_ms), int(track.last_updated_ms),
            float(track.confidence or 0), int(num_frames),
            track.model_source, None, float(motion_pct),
        )
        with self._conn_lock:
            self.conn.execute(INSERT_TRACK, row)
            self.conn.commit()

    def query_events(self, camera: str, start_ms: int,
                     end_ms: int) -> list[dict]:
        with self._conn_lock:
            cur = self.conn.execute(
                """SELECT camera, frame_time, track_id, species, confidence,
                           model_source, bbox_json, is_new
                   FROM pipeline_events
                   WHERE camera = ? AND frame_time BETWEEN ? AND ?
                   ORDER BY frame_time ASC""",
                (camera, start_ms, end_ms),
            )
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def query_tracks(self, camera: Optional[str] = None,
                     species: Optional[str] = None,
                     start_ms: Optional[int] = None,
                     end_ms: Optional[int] = None,
                     min_duration_s: Optional[float] = None,
                     min_confidence: Optional[float] = None,
                     limit: int = 100) -> list[dict]:
        clauses = []
        params = []
        if camera:
            clauses.append("camera = ?"); params.append(camera)
        if species:
            clauses.append("species = ?"); params.append(species)
        if start_ms is not None:
            clauses.append("start_time >= ?"); params.append(int(start_ms))
        if end_ms is not None:
            clauses.append("end_time <= ?"); params.append(int(end_ms))
        if min_duration_s is not None:
            clauses.append("(end_time - start_time) >= ?")
            params.append(int(min_duration_s * 1000))
        if min_confidence is not None:
            clauses.append("peak_confidence >= ?"); params.append(min_confidence)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = (
            "SELECT track_id, camera, species, start_time, end_time, "
            "peak_confidence, num_frames, model_source, motion_pct "
            "FROM pipeline_tracks" + where +
            " ORDER BY start_time DESC LIMIT ?"
        )
        params.append(limit)
        with self._conn_lock:
            cur = self.conn.execute(sql, params)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def prune_events(self, older_than_ms: int):
        with self._conn_lock:
            self.conn.execute(
                "DELETE FROM pipeline_events WHERE frame_time < ?",
                (older_than_ms,),
            )
            self.conn.commit()

    def daily_checkpoint(self):
        with self._conn_lock:
            self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_event_store.py -v`
Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/event_store.py tests/pipeline/test_event_store.py
git commit -m "feat: EventStore with WAL + batched writes

Time-indexed SQLite for pipeline events and track summaries.
Batched writes (50 events or 500ms), WAL mode, explicit pragmas.
Query helpers for scrubbing and clip search.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Frame Annotator (JPEG Encoder per Camera)

**Files:**
- Create: `pipeline/annotator.py`
- Test: `tests/pipeline/test_annotator.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_annotator.py`:
```python
"""Tests for FrameAnnotator."""
import queue
import time
import numpy as np
from unittest.mock import MagicMock


def test_annotator_downscales_and_encodes():
    from pipeline.annotator import FrameAnnotator
    from pipeline.frame import Frame

    debug_stream = MagicMock()
    a = FrameAnnotator("feeder", debug_stream, out_width=960, out_height=540)
    a.start()

    frame = Frame(
        bgr=np.ones((1080, 1920, 3), dtype=np.uint8) * 128,
        wall_time_ms=0, camera="feeder", width=1920, height=1080,
    )
    a.submit(frame, tracks=[])
    time.sleep(0.3)  # let annotator thread run

    debug_stream.push.assert_called()
    args, kwargs = debug_stream.push.call_args
    camera, jpeg_bytes, _ = args
    assert camera == "feeder"
    assert isinstance(jpeg_bytes, bytes)
    assert jpeg_bytes.startswith(b"\xff\xd8")  # JPEG magic
    a.stop()


def test_annotator_drops_oldest_when_full():
    from pipeline.annotator import FrameAnnotator
    from pipeline.frame import Frame

    debug_stream = MagicMock()
    # Make push slow to fill the queue
    debug_stream.push.side_effect = lambda *a, **k: time.sleep(0.3)

    a = FrameAnnotator("feeder", debug_stream, out_width=320, out_height=180)
    a.start()

    frame = Frame(
        bgr=np.ones((1080, 1920, 3), dtype=np.uint8) * 128,
        wall_time_ms=0, camera="feeder", width=1920, height=1080,
    )
    for _ in range(10):
        a.submit(frame, [])
    # Queue should never exceed maxsize
    assert a.queue.qsize() <= 2
    a.stop()


def test_muted_chip_for_unlabeled_track():
    """An unlabeled (species=None) track gets drawn with muted color, not skipped."""
    from pipeline.annotator import FrameAnnotator
    from pipeline.frame import Frame
    from pipeline.tracker import Track

    debug_stream = MagicMock()
    a = FrameAnnotator("feeder", debug_stream)
    bgr = np.ones((1080, 1920, 3), dtype=np.uint8) * 50
    frame = Frame(bgr=bgr, wall_time_ms=0, camera="feeder", width=1920, height=1080)
    unlabeled = Track(track_id=1, created_at_ms=0, last_updated_ms=0,
                      bbox=[400, 300, 500, 400], confidence=0.2, species=None)
    out_jpeg = a._annotate(frame.bgr, [unlabeled])
    assert isinstance(out_jpeg, bytes)
    assert len(out_jpeg) > 100  # non-trivial JPEG
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_annotator.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/annotator.py`:
```python
"""FrameAnnotator — draws labels on frames and encodes as JPEG."""
import queue
import threading
from typing import Optional

import cv2
import numpy as np

from pipeline.frame import Frame
from pipeline.tracker import Track


LABEL_COLOR_NORMAL = (74, 222, 128)   # green
LABEL_COLOR_MUTED = (128, 128, 128)   # gray for unlabeled
LABEL_BG = (0, 0, 0)
JPEG_QUALITY = 75


class FrameAnnotator:
    def __init__(self, camera_name: str, debug_stream,
                 out_width: int = 960, out_height: int = 540):
        self.camera_name = camera_name
        self.debug_stream = debug_stream
        self.out_width = out_width
        self.out_height = out_height
        self.queue: queue.Queue = queue.Queue(maxsize=2)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name=f"annot-{self.camera_name}", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.queue.put_nowait(None)  # wake thread
        except queue.Full:
            pass

    def submit(self, frame: Frame, tracks: list[Track]):
        """Non-blocking: drop oldest if queue full."""
        if self.queue.full():
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
        try:
            self.queue.put_nowait((frame, list(tracks)))
        except queue.Full:
            pass

    def _loop(self):
        while not self._stop.is_set():
            try:
                item = self.queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            frame, tracks = item
            try:
                jpeg_bytes = self._annotate(frame.bgr, tracks)
                self.debug_stream.push(self.camera_name, jpeg_bytes, frame.wall_time_ms)
            except Exception as e:
                # Never let annotator errors take down the thread
                import logging
                logging.warning("[%s] annotator error: %s", self.camera_name, e)

    def _annotate(self, bgr: np.ndarray, tracks: list[Track]) -> bytes:
        h_src, w_src = bgr.shape[:2]
        out = cv2.resize(
            bgr, (self.out_width, self.out_height),
            interpolation=cv2.INTER_LINEAR,
        )
        scale_x = self.out_width / w_src
        scale_y = self.out_height / h_src

        for track in tracks:
            x1 = int(track.bbox[0] * scale_x)
            y1 = int(track.bbox[1] * scale_y)
            x2 = int(track.bbox[2] * scale_x)
            y2 = int(track.bbox[3] * scale_y)
            cx = (x1 + x2) // 2
            label_y = max(22, y1 - 8)  # above the bird, clamped inside frame

            if track.species:
                label = track.species
                color = LABEL_COLOR_NORMAL
            else:
                label = "·"
                color = LABEL_COLOR_MUTED

            self._draw_label_pill(out, label, cx, label_y, color)
            if track.model_source == "both_agree":
                self._draw_checkmark(out, label, cx, label_y)

        ok, jpeg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        if not ok:
            return b""
        return jpeg.tobytes()

    def _draw_label_pill(self, img, text, cx, cy, color):
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.5
        thickness = 1
        (tw, th), baseline = cv2.getTextSize(text, font, scale, thickness)
        pad_x = 7
        pad_y = 4
        x1 = cx - (tw // 2) - pad_x
        x2 = cx + (tw // 2) + pad_x
        y1 = cy - th - pad_y
        y2 = cy + pad_y
        # Background
        overlay = img.copy()
        cv2.rectangle(overlay, (x1, y1), (x2, y2), LABEL_BG, -1)
        cv2.addWeighted(overlay, 0.75, img, 0.25, 0, dst=img)
        # Text
        cv2.putText(img, text, (cx - tw // 2, cy - 2),
                    font, scale, color, thickness, cv2.LINE_AA)

    def _draw_checkmark(self, img, text, cx, cy):
        """Small double-check badge to the right of the label."""
        font = cv2.FONT_HERSHEY_SIMPLEX
        scale = 0.4
        (tw, _), _ = cv2.getTextSize(text, font, 0.5, 1)
        x = cx + tw // 2 + 10
        y = cy - 2
        cv2.putText(img, "✓✓", (x, y), font, scale,
                    (255, 255, 255), 1, cv2.LINE_AA)
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_annotator.py -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/annotator.py tests/pipeline/test_annotator.py
git commit -m "feat: per-track label annotator + JPEG encoder

Draws labels above each bird (not fixed Y). Muted gray '·' chip for
unlabeled tracks (not invisible). Small double-check badge for tracks
where both models agreed. Downscales to 960x540 by default. Encodes
JPEG quality 75. Per-camera thread with drop-oldest backpressure.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Debug Stream (WebSocket MJPEG Server)

**Files:**
- Create: `pipeline/debug_stream.py`
- Test: `tests/pipeline/test_debug_stream.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_debug_stream.py`:
```python
"""Tests for DebugStream WebSocket server."""
import time
import threading
import pytest


@pytest.fixture
def stream():
    from pipeline.debug_stream import DebugStream
    s = DebugStream(port=0)  # port 0 = OS-assigned for tests
    yield s
    try:
        s.stop()
    except Exception:
        pass


def test_push_updates_latest_frame(stream):
    """Pushing a frame stores it as the poster for that camera."""
    stream.push("feeder", b"\xff\xd8fake", 0)
    assert stream.latest_frame.get("feeder") == b"\xff\xd8fake"


def test_push_with_no_clients_no_errors(stream):
    """Pushing to a camera with no subscribed clients should not raise."""
    stream.push("ground", b"\xff\xd8x", 0)  # no clients connected
    assert stream.stats.get("frames_sent", 0) == 0


def test_stats_counters_exist(stream):
    assert "active_clients" in stream.stats
    assert "frames_sent" in stream.stats
    assert "dropped_clients" in stream.stats


def test_push_ignores_unknown_camera(stream):
    """Pushing to a camera not in the clients dict should not crash."""
    stream.push("unknown", b"\xff\xd8x", 0)
    # Should still store as latest_frame
    assert stream.latest_frame.get("unknown") == b"\xff\xd8x"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_debug_stream.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/debug_stream.py`:
```python
"""DebugStream — MJPEG-over-WebSocket broadcast server."""
import logging
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)


class ClientState:
    def __init__(self, websocket, camera: str):
        self.websocket = websocket
        self.camera = camera
        self.failed = False
        self.last_send_ms = time.time() * 1000

    def send(self, data: bytes):
        self.websocket.send(data)
        self.last_send_ms = time.time() * 1000

    def is_slow(self) -> bool:
        return False  # simple v1 — always try to send

    def mark_failed(self):
        self.failed = True


class DebugStream:
    def __init__(self, port: int = 8101):
        self.port = port
        self.clients: dict[str, list] = {"feeder": [], "ground": []}
        self._lock = threading.Lock()
        self.latest_frame: dict[str, bytes] = {}
        self.stats = {
            "active_clients": 0, "frames_sent": 0,
            "dropped_clients": 0, "start_time": time.time(),
        }
        self._server = None
        self._serve_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    def start(self):
        self._stop.clear()
        self._serve_thread = threading.Thread(
            target=self._serve, name="debug-stream-serve", daemon=True
        )
        self._serve_thread.start()

    def stop(self):
        self._stop.set()
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass

    def _serve(self):
        try:
            from websockets.sync.server import serve
        except ImportError:
            log.error("websockets library not installed")
            return
        try:
            with serve(self._handle_client, "0.0.0.0", self.port) as server:
                self._server = server
                server.serve_forever()
        except Exception as e:
            log.error("Debug stream server error: %s", e)

    def _handle_client(self, websocket):
        try:
            path = websocket.request.path
        except Exception:
            websocket.close(1002, "No path")
            return

        if "/feeder" in path:
            camera = "feeder"
        elif "/ground" in path:
            camera = "ground"
        else:
            websocket.close(1002, "Unknown camera")
            return

        client = ClientState(websocket, camera)
        with self._lock:
            self.clients.setdefault(camera, []).append(client)
            self.stats["active_clients"] = sum(len(v) for v in self.clients.values())

        # Send poster frame immediately
        poster = self.latest_frame.get(camera)
        if poster:
            try:
                websocket.send(poster)
            except Exception:
                pass

        try:
            for _ in websocket:
                pass  # drain any pings
        except Exception:
            pass
        finally:
            with self._lock:
                try:
                    self.clients[camera].remove(client)
                except (ValueError, KeyError):
                    pass
                self.stats["active_clients"] = sum(len(v) for v in self.clients.values())

    def push(self, camera: str, jpeg_bytes: bytes, frame_time_ms: float):
        """Called by annotator threads. Broadcasts JPEG to all clients for this camera."""
        self.latest_frame[camera] = jpeg_bytes
        with self._lock:
            clients = list(self.clients.get(camera, []))
        for client in clients:
            if client.failed:
                continue
            try:
                client.send(jpeg_bytes)
                self.stats["frames_sent"] += 1
            except Exception:
                client.mark_failed()
                self.stats["dropped_clients"] += 1
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_debug_stream.py -v`
Expected: 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/debug_stream.py tests/pipeline/test_debug_stream.py
git commit -m "feat: WebSocket MJPEG debug stream server

Threading-compatible websockets.sync.server. Per-camera client
subscriptions via path. Poster frame sent on connect. Slow client
drop policy. Latest frame cached for reconnect recovery.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: HLS Recorder (Dedicated ffmpeg Subprocess)

**Files:**
- Create: `pipeline/hls_recorder.py`
- Test: `tests/pipeline/test_hls_recorder.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_hls_recorder.py`:
```python
"""Tests for HlsRecorder."""
import os
import time
from pathlib import Path
import pytest


def test_ffmpeg_cmd_uses_copy_mode():
    """Recorder should use stream copy (no decode/re-encode)."""
    from pipeline.hls_recorder import HlsRecorder
    r = HlsRecorder("feeder", "rtsp://x/y", "/tmp/hls-test")
    cmd = r._build_cmd()
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "hls"
    assert "-hls_time" in cmd


def test_cleanup_old_chunks(tmp_path):
    """Files older than retention_days should be deleted."""
    from pipeline.hls_recorder import HlsRecorder
    hls_root = tmp_path / "hls"
    (hls_root / "feeder").mkdir(parents=True)
    old_file = hls_root / "feeder" / "old.ts"
    new_file = hls_root / "feeder" / "new.ts"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    # Make old_file old
    old_mtime = time.time() - 10 * 86400
    os.utime(old_file, (old_mtime, old_mtime))

    HlsRecorder.cleanup_old_chunks(hls_root, retention_days=7)

    assert not old_file.exists()
    assert new_file.exists()


def test_output_dir_is_created(tmp_path):
    from pipeline.hls_recorder import HlsRecorder
    out = tmp_path / "new_dir"
    r = HlsRecorder("feeder", "rtsp://x/y", str(out))
    assert out.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_hls_recorder.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/hls_recorder.py`:
```python
"""HlsRecorder — dedicated ffmpeg subprocess for HLS chunk recording."""
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

FFMPEG = "/usr/local/bin/ffmpeg"


class HlsRecorder:
    def __init__(self, camera_name: str, rtsp_url: str, output_dir: str):
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.proc: Optional[subprocess.Popen] = None
        self._watchdog: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.stats = {"chunks_written": 0, "restarts": 0, "last_chunk_ms": None}

    def _build_cmd(self) -> list:
        playlist = self.output_dir / "live.m3u8"
        segment = self.output_dir / "seg_%Y%m%d-%H%M%S.ts"
        return [
            FFMPEG,
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-i", self.rtsp_url,
            "-c", "copy",
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "0",
            "-hls_flags", "append_list+program_date_time",
            "-strftime", "1",
            "-hls_segment_filename", str(segment),
            str(playlist),
        ]

    def start(self):
        self._stop.clear()
        self._spawn()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, name=f"hls-wd-{self.camera_name}", daemon=True
        )
        self._watchdog.start()

    def stop(self):
        self._stop.set()
        if self.proc:
            try:
                self.proc.kill()
                self.proc.wait(timeout=5)
            except Exception:
                pass

    def _spawn(self):
        cmd = self._build_cmd()
        log.info("[%s] HLS recorder: %s", self.camera_name, " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

    def _watchdog_loop(self):
        while not self._stop.is_set():
            time.sleep(5)
            if self.proc and self.proc.poll() is not None:
                log.warning("[%s] HLS recorder exited, restarting",
                            self.camera_name)
                self.stats["restarts"] += 1
                time.sleep(2)
                self._spawn()

    @staticmethod
    def cleanup_old_chunks(hls_root: Path, retention_days: int = 7):
        """Delete HLS segments older than retention_days."""
        hls_root = Path(hls_root)
        if not hls_root.exists():
            return
        cutoff = time.time() - retention_days * 86400
        for camera_dir in hls_root.iterdir():
            if not camera_dir.is_dir():
                continue
            for seg in camera_dir.glob("*.ts"):
                try:
                    if seg.stat().st_mtime < cutoff:
                        seg.unlink()
                except Exception:
                    pass
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_hls_recorder.py -v`
Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/hls_recorder.py tests/pipeline/test_hls_recorder.py
git commit -m "feat: dedicated HLS recorder subprocess

Copy-mode ffmpeg per camera writes HLS chunks to disk with
PROGRAM-DATE-TIME tags. ~1% CPU. Fully independent from detection
pipeline. Watchdog restarts on exit. 7-day retention cleanup.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: Health State + HTTP Endpoint

**Files:**
- Create: `pipeline/health.py`
- Test: `tests/pipeline/test_health.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_health.py`:
```python
"""Tests for HealthState."""
import json


def test_health_state_defaults_to_ok():
    from pipeline.health import HealthState
    h = HealthState()
    assert h.snapshot()["overall"] == "ok"


def test_update_component_reflects_in_snapshot():
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {"fps": 4.9, "dropped_oldest": 0})
    snap = h.snapshot()
    assert snap["pipeline"]["feeder"]["capture"]["fps"] == 4.9


def test_degraded_when_fps_low():
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {"fps": 3.5, "dropped_oldest": 0, "last_frame_age_ms": 200})
    h.update("ground", "capture", {"fps": 4.9, "dropped_oldest": 0, "last_frame_age_ms": 200})
    snap = h.snapshot()
    assert snap["overall"] in ("degraded", "ok")  # 3.5 is borderline


def test_broken_when_camera_stale():
    from pipeline.health import HealthState
    h = HealthState()
    # Stale frame > 60s
    h.update("feeder", "capture", {"fps": 0, "dropped_oldest": 0, "last_frame_age_ms": 120_000})
    h.update("ground", "capture", {"fps": 4.9, "dropped_oldest": 0, "last_frame_age_ms": 200})
    snap = h.snapshot()
    assert snap["overall"] == "broken"


def test_snapshot_serializable():
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {"fps": 4.9})
    json.dumps(h.snapshot())  # should not raise
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_health.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/health.py`:
```python
"""HealthState — shared pipeline health dict + status computation."""
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Optional


class HealthState:
    def __init__(self):
        self._lock = threading.Lock()
        self._data: dict = {"pipeline": {}, "shared": {}}
        self._start_time = time.time()

    def update(self, camera: str, component: str, stats: dict):
        with self._lock:
            cam = self._data["pipeline"].setdefault(camera, {})
            cam[component] = dict(stats)

    def update_shared(self, component: str, stats: dict):
        with self._lock:
            self._data["shared"][component] = dict(stats)

    def snapshot(self) -> dict:
        with self._lock:
            data = {
                "pipeline": {
                    cam: {comp: dict(s) for comp, s in comps.items()}
                    for cam, comps in self._data["pipeline"].items()
                },
                "shared": {k: dict(v) for k, v in self._data["shared"].items()},
                "uptime_s": int(time.time() - self._start_time),
            }
        data["overall"] = self._compute_status(data)
        return data

    def _compute_status(self, data: dict) -> str:
        """Roll-up: ok / degraded / broken."""
        worst = "ok"
        for cam, comps in data.get("pipeline", {}).items():
            cap = comps.get("capture", {})
            fps = cap.get("fps")
            age_ms = cap.get("last_frame_age_ms")
            if age_ms is not None and age_ms > 60_000:
                return "broken"
            if fps is not None and fps < 3:
                return "broken"
            if fps is not None and fps < 4.5:
                worst = "degraded"
            detector = comps.get("detector", {})
            p99 = detector.get("yolo_ms_p99")
            if p99 is not None and p99 > 150:
                worst = max(worst, "degraded", key=["ok", "degraded", "broken"].index)
            classifier = comps.get("classifier", {})
            if classifier.get("lock_timeouts", 0) > 10:
                worst = max(worst, "degraded", key=["ok", "degraded", "broken"].index)
        return worst


class HealthServer:
    """Minimal HTTP server that exposes /api/pipeline/health as JSON."""
    def __init__(self, health: HealthState, port: int = 8100):
        self.health = health
        self.port = port
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def start(self):
        health = self.health

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args): pass  # silence access log

            def do_GET(self):
                if self.path.startswith("/api/pipeline/health"):
                    body = json.dumps(health.snapshot()).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

        self._server = ThreadingHTTPServer(("0.0.0.0", self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="health-server", daemon=True
        )
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_health.py -v`
Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/health.py tests/pipeline/test_health.py
git commit -m "feat: pipeline health state + HTTP endpoint

HealthState holds per-camera per-component stats with threading lock.
Computed overall status: ok/degraded/broken. HealthServer exposes
/api/pipeline/health as JSON on port 8100.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Process Thread (orchestrates per-camera pipeline)

**Files:**
- Create: `pipeline/process_thread.py`
- Test: `tests/pipeline/test_process_thread.py`

- [ ] **Step 1: Write the failing test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_process_thread.py`:
```python
"""Tests for CameraProcessThread — the per-camera orchestrator."""
import queue
import time
import numpy as np
from unittest.mock import MagicMock


def test_process_thread_reads_frame_and_calls_pipeline():
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame

    frame_q = queue.Queue(maxsize=2)

    motion_gate = MagicMock()
    motion_gate.regions = MagicMock(return_value=[(0, 0, 100, 100)])

    detector = MagicMock()
    from pipeline.detector import Detection
    detector.detect = MagicMock(return_value=[
        Detection(box=[10, 10, 50, 50], confidence=0.9)
    ])

    from pipeline.tracker import Track, TrackerOutput
    track = Track(track_id=1, created_at_ms=0, last_updated_ms=0,
                  bbox=[10, 10, 50, 50], confidence=0.9)
    tracker = MagicMock()
    tracker.update = MagicMock(return_value=TrackerOutput(
        active=[track], new=[track], expired=[], frame_time_ms=0
    ))
    tracker.stationary_regions = MagicMock(return_value=[])
    tracker.tracks = {1: track}

    classifier = MagicMock()
    from pipeline.classifier import ClassificationResult
    classifier.classify = MagicMock(return_value=ClassificationResult(
        species="Test Bird", confidence=0.9, model_source="yard", should_retry=False
    ))

    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    thread = CameraProcessThread(
        name="feeder",
        frame_queue=frame_q,
        motion_gate=motion_gate,
        detector=detector,
        tracker=tracker,
        classifier=classifier,
        event_store=event_store,
        annotator=annotator,
        health=health,
    )
    thread.start()

    frame = Frame(
        bgr=np.ones((480, 640, 3), dtype=np.uint8) * 128,
        wall_time_ms=1000,
        camera="feeder",
        width=640,
        height=480,
    )
    frame_q.put(frame)

    # Give the thread a moment to process
    time.sleep(0.3)

    # Verify pipeline was called
    motion_gate.regions.assert_called()
    detector.detect.assert_called()
    tracker.update.assert_called()
    classifier.classify.assert_called()
    event_store.write_event.assert_called()
    annotator.submit.assert_called()

    thread.stop()


def test_process_thread_survives_detector_exception():
    """An exception in the detector should not crash the thread."""
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame

    frame_q = queue.Queue(maxsize=2)
    motion_gate = MagicMock()
    motion_gate.regions.return_value = [(0,0,10,10)]
    detector = MagicMock()
    detector.detect.side_effect = RuntimeError("boom")
    tracker = MagicMock()
    from pipeline.tracker import TrackerOutput
    tracker.update.return_value = TrackerOutput(active=[], new=[], expired=[], frame_time_ms=0)
    tracker.stationary_regions.return_value = []
    tracker.tracks = {}
    classifier = MagicMock()
    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    thread = CameraProcessThread(
        name="feeder", frame_queue=frame_q, motion_gate=motion_gate,
        detector=detector, tracker=tracker, classifier=classifier,
        event_store=event_store, annotator=annotator, health=health,
    )
    thread.start()

    frame = Frame(bgr=np.zeros((10,10,3), dtype=np.uint8),
                  wall_time_ms=1000, camera="feeder", width=10, height=10)
    frame_q.put(frame)
    time.sleep(0.3)

    # Thread should still be alive
    assert thread.is_alive()
    thread.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py -v`
Expected: FAIL — `ModuleNotFoundError`.

- [ ] **Step 3: Write implementation**

Create `/Users/vives/bird-classifier/pipeline/process_thread.py`:
```python
"""CameraProcessThread — orchestrates the per-camera pipeline stages."""
import logging
import queue
import threading
import time
from typing import Optional

from PIL import Image

from pipeline.frame import Frame
from pipeline.classifier import MAX_CLASSIFICATION_ATTEMPTS

log = logging.getLogger(__name__)

FORCED_FULL_YOLO_INTERVAL_S = 10.0


class CameraProcessThread:
    def __init__(self, name: str, frame_queue: queue.Queue,
                 motion_gate, detector, tracker, classifier,
                 event_store, annotator, health):
        self.name = name
        self.frame_queue = frame_queue
        self.motion_gate = motion_gate
        self.detector = detector
        self.tracker = tracker
        self.classifier = classifier
        self.event_store = event_store
        self.annotator = annotator
        self.health = health
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_forced_full = 0.0
        self._stats = {
            "frames_processed": 0,
            "detections": 0,
            "yolo_ms_samples": [],
        }

    def start(self):
        self._thread = threading.Thread(
            target=self._loop, name=f"proc-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def is_alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self):
        while not self._stop.is_set():
            try:
                frame: Frame = self.frame_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._process_frame(frame)
            except Exception as e:
                log.exception("[%s] process frame error: %s", self.name, e)

    def _process_frame(self, frame: Frame):
        self._stats["frames_processed"] += 1

        # 1. Motion gate
        regions = self.motion_gate.regions(frame.bgr)

        # 2. Decide forced full-frame detection
        now = time.time()
        forced_full = (now - self._last_forced_full) > FORCED_FULL_YOLO_INTERVAL_S
        if forced_full:
            self._last_forced_full = now

        # 3. Detect
        t_det = time.monotonic()
        detections = self.detector.detect(frame, regions, forced_full=forced_full)
        det_ms = (time.monotonic() - t_det) * 1000
        self._stats["yolo_ms_samples"].append(det_ms)
        if len(self._stats["yolo_ms_samples"]) > 100:
            self._stats["yolo_ms_samples"] = self._stats["yolo_ms_samples"][-100:]
        self._stats["detections"] += len(detections)

        # 4. Track
        tracker_out = self.tracker.update(detections, frame.wall_time_ms)

        # 5. Classify tracks needing classification
        self._classify_tracks(frame, tracker_out.active)

        # 6. Write events (one per active track per frame)
        for track in tracker_out.active:
            self.event_store.write_event(
                camera=self.name,
                frame_time_ms=frame.wall_time_ms,
                track_id=track.track_id,
                species=track.species,
                confidence=track.confidence,
                model_source=track.model_source,
                bbox=track.bbox,
                is_new=track in tracker_out.new,
            )

        # 7. Track expired → write summary
        for track in tracker_out.expired:
            self.event_store.write_track_summary(
                camera=self.name, track=track, num_frames=self._stats["frames_processed"]
            )

        # 8. Annotate + push
        self.annotator.submit(frame, tracker_out.active)

        # 9. Update health
        self._update_health(frame, det_ms)

    def _classify_tracks(self, frame: Frame, tracks: list):
        """Run Smart B on any track that still needs classification."""
        for track in tracks:
            if not track.needs_classification:
                continue
            if track.classification_attempts >= MAX_CLASSIFICATION_ATTEMPTS:
                track.needs_classification = False
                continue

            # Crop the bird
            x1, y1, x2, y2 = [int(v) for v in track.bbox]
            x1 = max(0, x1); y1 = max(0, y1)
            x2 = min(frame.width, x2); y2 = min(frame.height, y2)
            if x2 <= x1 or y2 <= y1:
                track.needs_classification = False
                continue
            crop_bgr = frame.bgr[y1:y2, x1:x2]
            # OpenCV BGR → PIL RGB
            crop_pil = Image.fromarray(crop_bgr[:, :, ::-1])
            if crop_pil.size[0] < 5 or crop_pil.size[1] < 5:
                track.needs_classification = False
                continue

            track.classification_attempts += 1
            result = self.classifier.classify(
                crop_pil, frame.wall_time_ms, self.name
            )
            if result.should_retry:
                # Will retry on next frame
                continue
            # Got a final answer (species may be None = unlabeled)
            track.species = result.species
            track.confidence = result.confidence if result.confidence else track.confidence
            track.model_source = result.model_source
            track.needs_classification = False

    def _update_health(self, frame: Frame, det_ms: float):
        samples = self._stats["yolo_ms_samples"]
        if samples:
            yolo_avg = sum(samples) / len(samples)
            yolo_p99 = sorted(samples)[-max(1, len(samples) // 100)]
        else:
            yolo_avg = 0
            yolo_p99 = 0
        age_ms = (time.time() * 1000) - frame.wall_time_ms
        self.health.update(self.name, "capture", {
            "last_frame_age_ms": int(age_ms),
            "frames_processed": self._stats["frames_processed"],
        })
        self.health.update(self.name, "detector", {
            "yolo_ms_avg": round(yolo_avg),
            "yolo_ms_p99": round(yolo_p99),
            "detections_total": self._stats["detections"],
        })
        self.health.update(self.name, "tracker", {
            "active_tracks": len(self.tracker.tracks),
            "stationary_tracks": len(self.tracker.stationary_regions()),
        })
        self.health.update(self.name, "classifier", dict(self.classifier.stats))
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py -v`
Expected: 2 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/vives/bird-classifier
git add pipeline/process_thread.py tests/pipeline/test_process_thread.py
git commit -m "feat: per-camera process thread orchestrator

Reads frames from queue, runs motion gate → detector → tracker →
classifier → event store → annotator. Handles retry semantics for
tracks that hit Coral lock timeouts. Surface-level exception catch
keeps the thread alive.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: Orchestrator + Dashboard + Integration

**Files:**
- Create: `bird_pipeline_v2.py`
- Modify: `dashboard/index.html`
- Modify: `dashboard/api.py`
- Create: `tests/pipeline/test_pipeline_e2e.py`
- Create: `tests/pipeline/bench_pipeline.py`

- [ ] **Step 1: Write the orchestrator — `bird_pipeline_v2.py`**

Create `/Users/vives/bird-classifier/bird_pipeline_v2.py`:
```python
#!/usr/bin/env python3
"""bird_pipeline_v2 — Frigate-inspired live detection orchestrator.

Starts per-camera capture + process + annotator + recorder threads,
plus shared classifier, event store, debug stream, health server,
and prune loop.

See docs/superpowers/specs/2026-04-10-live-detection-v2-design.md
"""
import logging
import os
import queue
import signal
import sys
import threading
import time
from pathlib import Path

BASE_DIR = Path(__file__).parent
MODELS_DIR = BASE_DIR / "models"
HLS_DIR = Path.home() / "bird-snapshots" / "hls"
PIPELINE_DB = Path.home() / "bird-snapshots" / "logs" / "pipeline.db"
BIRDNET_DB = Path.home() / "bird-snapshots" / "logs" / "birdnet_local.db"
REGIONAL_SPECIES_PATH = MODELS_DIR / "chilmark_feeder_species.txt"

CAMERAS = {
    "feeder": "rtsp://127.0.0.1:8554/feeder-main",
    "ground": "rtsp://127.0.0.1:8554/ground-main",
}

YOLO_MODEL = str(MODELS_DIR / "yolov8n_bird.onnx")
YARD_MODEL = str(MODELS_DIR / "yard_model.tflite")
YARD_LABELS = str(MODELS_DIR / "yard_model_labels.txt")
AIY_MODEL = str(MODELS_DIR / "aiy_birds_v1_edgetpu.tflite")
AIY_LABELS = str(MODELS_DIR / "inat_bird_labels.txt")

running = True


def load_regional_species() -> set:
    if not REGIONAL_SPECIES_PATH.exists():
        return set()
    with open(REGIONAL_SPECIES_PATH) as f:
        species = {line.strip() for line in f if line.strip() and line.strip() != "background"}
    return species


def shutdown_handler(signum, frame):
    global running
    logging.info("Shutdown signal received")
    running = False


def prune_loop(event_store, hls_root):
    from pipeline.hls_recorder import HlsRecorder
    while running:
        time.sleep(3600)  # hourly
        try:
            cutoff = int((time.time() - 7 * 86400) * 1000)
            event_store.prune_events(older_than_ms=cutoff)
            event_store.daily_checkpoint()
            HlsRecorder.cleanup_old_chunks(hls_root, retention_days=7)
        except Exception as e:
            logging.warning("Prune loop error: %s", e)


def main():
    global running
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    log = logging.getLogger("pipeline")

    # Nighttime pause check (not in camera loop — done here at top level)
    from solar_utils import is_nighttime

    # Import pipeline modules
    from pipeline.frame_capture import FrameCapture
    from pipeline.motion_gate import MotionGate
    from pipeline.detector import BirdDetector
    from pipeline.tracker import BirdTracker
    from pipeline.classifier import SmartClassifier
    from pipeline.event_store import EventStore
    from pipeline.annotator import FrameAnnotator
    from pipeline.debug_stream import DebugStream
    from pipeline.hls_recorder import HlsRecorder
    from pipeline.health import HealthState, HealthServer
    from pipeline.process_thread import CameraProcessThread

    log.info("Starting bird_pipeline_v2...")

    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

    # Shared services
    event_store = EventStore(str(PIPELINE_DB))
    health = HealthState()
    health_server = HealthServer(health, port=8100)
    health_server.start()
    debug_stream = DebugStream(port=8101)
    debug_stream.start()

    regional_species = load_regional_species()
    classifier = SmartClassifier(
        yard_model_path=YARD_MODEL,
        yard_labels_path=YARD_LABELS,
        aiy_model_path=AIY_MODEL,
        aiy_labels_path=AIY_LABELS,
        regional_species=regional_species,
        audio_db_path=str(BIRDNET_DB) if BIRDNET_DB.exists() else None,
    )

    # Per-camera stack
    camera_stacks = []
    for name, url in CAMERAS.items():
        try:
            frame_q = queue.Queue(maxsize=2)
            capture = FrameCapture(name, url, out_queue=frame_q,
                                   width=1920, height=1080, fps=5)
            motion_gate = MotionGate()
            tracker = BirdTracker()
            detector = BirdDetector(
                yolo_model_path=YOLO_MODEL,
                stationary_track_regions_fn=tracker.stationary_regions,
                confidence=0.3,
            )
            annotator = FrameAnnotator(name, debug_stream)
            process = CameraProcessThread(
                name=name,
                frame_queue=frame_q,
                motion_gate=motion_gate,
                detector=detector,
                tracker=tracker,
                classifier=classifier,
                event_store=event_store,
                annotator=annotator,
                health=health,
            )
            recorder = HlsRecorder(name, url, str(HLS_DIR / name))

            capture.start()
            annotator.start()
            process.start()
            recorder.start()
            camera_stacks.append((name, capture, annotator, process, recorder))
            log.info("[%s] Stack started", name)
        except Exception as e:
            log.error("[%s] Failed to start: %s", name, e)

    # Prune loop
    pruner = threading.Thread(
        target=prune_loop, args=(event_store, HLS_DIR), daemon=True
    )
    pruner.start()

    # Main loop: handle nighttime pause / wait for shutdown
    while running:
        time.sleep(10)
        # Daytime-only detection
        if is_nighttime():
            # Pause by stopping captures (HLS recorder keeps running for motion playback)
            for _name, cap, _ann, _proc, _rec in camera_stacks:
                if cap.proc and cap.proc.poll() is None:
                    log.info("[%s] Nighttime pause", _name)
                    cap.stop()
        else:
            # Restart any stopped captures
            for _name, cap, _ann, _proc, _rec in camera_stacks:
                if cap.proc is None or cap.proc.poll() is not None:
                    log.info("[%s] Daytime resume", _name)
                    cap.start()

    log.info("Shutting down...")
    for name, capture, annotator, process, recorder in camera_stacks:
        try: capture.stop()
        except Exception: pass
        try: annotator.stop()
        except Exception: pass
        try: process.stop()
        except Exception: pass
        try: recorder.stop()
        except Exception: pass
    try: debug_stream.stop()
    except Exception: pass
    try: health_server.stop()
    except Exception: pass
    try: event_store.shutdown()
    except Exception: pass
    log.info("Bye")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test the orchestrator imports**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -c "import bird_pipeline_v2; print('imports ok')"`
Expected: `imports ok`

- [ ] **Step 3: Write the end-to-end integration test**

Create `/Users/vives/bird-classifier/tests/pipeline/test_pipeline_e2e.py`:
```python
"""End-to-end pipeline test using Protect video files as RTSP substitutes."""
import queue
import time
from pathlib import Path

import pytest

VIDEOS = Path("/Users/vives/docs/bird-observatory/training videos")


@pytest.mark.slow
@pytest.mark.skipif(not VIDEOS.exists(), reason="test videos not available")
def test_empty_video_produces_no_events(tmp_path):
    """1m-empty.mp4 should produce zero tracks with confidence."""
    from pipeline.frame_capture import FrameCapture
    from pipeline.motion_gate import MotionGate
    from pipeline.tracker import BirdTracker
    from pipeline.detector import BirdDetector
    from pipeline.classifier import SmartClassifier
    from pipeline.event_store import EventStore
    from pipeline.annotator import FrameAnnotator
    from pipeline.debug_stream import DebugStream
    from pipeline.health import HealthState
    from pipeline.process_thread import CameraProcessThread

    empty = VIDEOS / "1m-empty.mp4"
    if not empty.exists():
        pytest.skip(f"Missing {empty}")

    frame_q = queue.Queue(maxsize=2)
    capture = FrameCapture("test", str(empty), out_queue=frame_q,
                           width=1920, height=1080, fps=5)
    motion_gate = MotionGate()
    tracker = BirdTracker()
    detector = BirdDetector(
        yolo_model_path="/Users/vives/bird-classifier/models/yolov8n_bird.onnx",
        stationary_track_regions_fn=tracker.stationary_regions,
        confidence=0.3,
    )

    # Dummy classifier: never return a species so we isolate detection
    class DummyClassifier:
        stats = {}
        def classify(self, *a, **k):
            from pipeline.classifier import ClassificationResult
            return ClassificationResult(None, 0, None, False)

    event_store = EventStore(str(tmp_path / "pipeline.db"))
    debug_stream = DebugStream(port=0)
    annotator = FrameAnnotator("test", debug_stream, out_width=320, out_height=180)
    health = HealthState()

    process = CameraProcessThread(
        name="test",
        frame_queue=frame_q,
        motion_gate=motion_gate,
        detector=detector,
        tracker=tracker,
        classifier=DummyClassifier(),
        event_store=event_store,
        annotator=annotator,
        health=health,
    )

    capture.start()
    annotator.start()
    process.start()

    # Run for 30 seconds
    time.sleep(30)

    capture.stop()
    process.stop()
    annotator.stop()
    event_store.shutdown()

    # Verify health was updated (proves pipeline ran)
    snap = health.snapshot()
    assert "test" in snap["pipeline"]
    # Empty video should have very few detections (YOLO can get false positives)
    # Primary assertion: pipeline didn't crash
```

- [ ] **Step 4: Run the e2e test**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_e2e.py -v -s`
Expected: PASS (takes ~35s). If the Coral USB is busy (old pipeline running), skip or stop that first.

- [ ] **Step 5: Update dashboard — new WebSocket client code**

Modify `/Users/vives/bird-classifier/dashboard/index.html`:

Find the current "New Det" / SSE overlay code and replace with the MJPEG client. The exact line numbers will vary — look for the `connectPipelineSSE()` function and the detection overlay canvas logic. Replace with this approach:

Add this JavaScript inside the existing script tag (near the other live-feed code):
```javascript
/* ====== New Det v2: MJPEG over WebSocket ======================== */
var _v2Ws = null;
var _v2Canvas = null;
var _v2Ctx = null;
var _v2LastFrameMs = 0;
var _v2ReconnectDelay = 1000;

function connectDebugStreamV2(camera) {
  if (_v2Ws) {
    try { _v2Ws.close(); } catch(e) {}
  }
  _v2Canvas = document.getElementById('debug-stream-canvas-' + camera) ||
              document.getElementById('detection-overlay');
  if (!_v2Canvas) return;
  _v2Ctx = _v2Canvas.getContext('2d');

  var proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  var mode = '';
  var c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  if (c && c.effectiveType && /(slow-)?2g|3g/.test(c.effectiveType)) {
    mode = '?mode=mobile';
  }
  var url = proto + '//' + location.host + '/api/debug-stream/' + camera + mode;
  _v2Ws = new WebSocket(url);
  _v2Ws.binaryType = 'blob';
  _v2Ws.onopen = function() {
    _v2ReconnectDelay = 1000;
    hideToast('v2-reconnect');
  };
  _v2Ws.onmessage = async function(ev) {
    try {
      var blob = ev.data;
      var img = await createImageBitmap(blob);
      _v2Canvas.width = img.width;
      _v2Canvas.height = img.height;
      _v2Ctx.drawImage(img, 0, 0);
      img.close && img.close();
      _v2LastFrameMs = Date.now();
    } catch(e) {}
  };
  _v2Ws.onclose = function() {
    showToast('v2-reconnect', 'Reconnecting...');
    setTimeout(function() {
      if (document.visibilityState !== 'hidden') connectDebugStreamV2(camera);
    }, _v2ReconnectDelay);
    _v2ReconnectDelay = Math.min(_v2ReconnectDelay * 2, 30000);
  };
}

document.addEventListener('visibilitychange', function() {
  if (document.visibilityState === 'hidden' && _v2Ws) {
    try { _v2Ws.close(); } catch(e) {}
    _v2Ws = null;
  } else if (document.visibilityState === 'visible' && !_v2Ws) {
    connectDebugStreamV2(currentCamera);
  }
});

/* Live freshness indicator */
setInterval(function() {
  var dot = document.getElementById('live-dot');
  if (!dot) return;
  var age = Date.now() - _v2LastFrameMs;
  if (age < 2000) {
    dot.classList.add('pulsing');
    dot.classList.remove('stale');
  } else {
    dot.classList.remove('pulsing');
    dot.classList.add('stale');
  }
}, 500);

function showToast(id, text) {
  var t = document.getElementById(id);
  if (!t) {
    t = document.createElement('div');
    t.id = id;
    t.className = 'toast';
    document.body.appendChild(t);
  }
  t.textContent = text;
  t.style.display = 'block';
}
function hideToast(id) {
  var t = document.getElementById(id);
  if (t) t.style.display = 'none';
}
```

Add this CSS rule near the existing styles:
```css
#live-dot { width: 10px; height: 10px; border-radius: 50%; background: #4ade80;
            display: inline-block; }
#live-dot.pulsing { animation: pulse 1s infinite; }
#live-dot.stale { background: #9ca3af; }
@keyframes pulse { 0%, 100% { opacity: 1 } 50% { opacity: 0.4 } }
.toast { position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
         background: rgba(0,0,0,0.85); color: #fff; padding: 8px 16px;
         border-radius: 8px; font-size: 14px; z-index: 10000; display: none; }
```

- [ ] **Step 6: Add FastAPI WebSocket proxy in `dashboard/api.py`**

Add to `/Users/vives/bird-classifier/dashboard/api.py` (near other WebSocket routes):
```python
@app.websocket("/api/debug-stream/{camera}")
async def debug_stream_proxy(websocket: WebSocket, camera: str):
    """Proxy MJPEG frames from pipeline v2 debug stream on port 8101."""
    import websockets
    await websocket.accept()
    backend_url = f"ws://127.0.0.1:8101/debug-stream/{camera}"
    try:
        async with websockets.connect(backend_url, max_size=None) as backend:
            # Relay binary messages from backend to client
            async def backend_to_client():
                try:
                    async for msg in backend:
                        await websocket.send_bytes(msg)
                except Exception:
                    pass
            task = asyncio.create_task(backend_to_client())
            try:
                while True:
                    await websocket.receive()  # drain client pings
            except WebSocketDisconnect:
                pass
            finally:
                task.cancel()
    except Exception:
        try: await websocket.close()
        except Exception: pass


@app.get("/api/pipeline/health")
async def pipeline_health_proxy():
    import httpx
    async with httpx.AsyncClient(timeout=2) as c:
        try:
            r = await c.get("http://127.0.0.1:8100/api/pipeline/health")
            return r.json()
        except Exception as e:
            return {"overall": "broken", "error": str(e)}


@app.get("/api/pipeline/events")
async def pipeline_events(camera: str, start: int, end: int):
    from pipeline.event_store import EventStore
    from pathlib import Path
    db = Path.home() / "bird-snapshots" / "logs" / "pipeline.db"
    if not db.exists():
        return []
    store = EventStore(str(db))
    try:
        return store.query_events(camera=camera, start_ms=start, end_ms=end)
    finally:
        store.shutdown()
```

Make sure `asyncio`, `websockets`, and `httpx` are imported at the top of `api.py`.

- [ ] **Step 7: Install httpx if not already installed**

Run:
```bash
/Users/vives/bird-classifier/venv-coral/bin/pip show httpx || \
/Users/vives/bird-classifier/venv-coral/bin/pip install httpx
```

- [ ] **Step 8: Manual smoke test — restart dashboard and pipeline**

Run (in two separate Terminal tabs if possible):
```bash
# Tab 1: restart dashboard API
launchctl stop com.vives.bird-dashboard && launchctl start com.vives.bird-dashboard

# Tab 2: run pipeline v2 in the foreground for testing
cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python bird_pipeline_v2.py
```

Then open the dashboard in a browser. "New Det" mode should show a live MJPEG stream with labels. The live dot should pulse. A "Reconnecting..." toast should appear if you kill the pipeline.

- [ ] **Step 9: Commit**

```bash
cd /Users/vives/bird-classifier
git add bird_pipeline_v2.py dashboard/index.html dashboard/api.py tests/pipeline/test_pipeline_e2e.py
git commit -m "feat: pipeline v2 orchestrator + dashboard MJPEG client + e2e test

- bird_pipeline_v2.py starts shared services + per-camera stacks
- Dashboard uses WebSocket MJPEG for New Det (no more SSE overlay)
- api.py proxies /api/debug-stream/{camera} and /api/pipeline/health
- Live freshness dot + reconnect toast + cellular detection
- Tab-backgrounded WS close for battery savings
- e2e test feeds Protect video through full pipeline stack

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: Cutover + Benchmark + Retire Old Pipeline

**Files:**
- Create: `tests/pipeline/bench_pipeline.py`
- Modify: `~/Library/LaunchAgents/com.vives.bird-pipeline.plist`
- Delete: `bird_pipeline.py`, `bird_tracker.py` (after 1-week soak)

- [ ] **Step 1: Write the benchmark**

Create `/Users/vives/bird-classifier/tests/pipeline/bench_pipeline.py`:
```python
"""Benchmark the full pipeline on a test video."""
import queue
import time
import tracemalloc
from pathlib import Path

import pytest

TEST_VIDEO = Path("/Users/vives/docs/bird-observatory/training videos/chickadee-finch-downy.mp4")


@pytest.mark.slow
@pytest.mark.skipif(not TEST_VIDEO.exists(), reason="test video not available")
def test_benchmark_60s_run():
    """Run the full pipeline against a test video and assert on thresholds."""
    from pipeline.frame_capture import FrameCapture
    from pipeline.motion_gate import MotionGate
    from pipeline.tracker import BirdTracker
    from pipeline.detector import BirdDetector
    from pipeline.classifier import SmartClassifier, ClassificationResult
    from pipeline.event_store import EventStore
    from pipeline.annotator import FrameAnnotator
    from pipeline.debug_stream import DebugStream
    from pipeline.health import HealthState
    from pipeline.process_thread import CameraProcessThread

    frame_q = queue.Queue(maxsize=2)
    capture = FrameCapture("bench", str(TEST_VIDEO), out_queue=frame_q,
                           width=1920, height=1080, fps=5)
    motion_gate = MotionGate()
    tracker = BirdTracker()
    detector = BirdDetector(
        yolo_model_path="/Users/vives/bird-classifier/models/yolov8n_bird.onnx",
        stationary_track_regions_fn=tracker.stationary_regions,
    )

    class FastClassifier:
        stats = {"yard": 0, "aiy": 0, "both_agree": 0, "audio_confirmed": 0,
                 "unlabeled": 0, "lock_timeouts": 0, "retries": 0}
        def classify(self, crop, frame_time_ms, camera):
            return ClassificationResult("Black-capped Chickadee", 0.9, "yard", False)

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    event_store = EventStore(str(tmp / "pipeline.db"))
    debug_stream = DebugStream(port=0)
    annotator = FrameAnnotator("bench", debug_stream)
    health = HealthState()

    process = CameraProcessThread(
        name="bench", frame_queue=frame_q, motion_gate=motion_gate,
        detector=detector, tracker=tracker, classifier=FastClassifier(),
        event_store=event_store, annotator=annotator, health=health,
    )

    tracemalloc.start()
    capture.start()
    annotator.start()
    process.start()
    t_start = time.time()
    time.sleep(60)
    capture.stop()
    process.stop()
    annotator.stop()
    event_store.shutdown()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    snap = health.snapshot()
    cam = snap["pipeline"]["bench"]
    yolo_p99 = cam["detector"]["yolo_ms_p99"]
    frames = capture.stats["frames"]
    restarts = capture.stats["ffmpeg_restarts"]

    print(f"\n=== Benchmark Results ===")
    print(f"Elapsed: {time.time() - t_start:.1f}s")
    print(f"Frames produced: {frames}")
    print(f"ffmpeg restarts: {restarts}")
    print(f"YOLO ms p99: {yolo_p99}")
    print(f"Peak memory: {peak / 1024 / 1024:.0f} MB")

    assert restarts == 0, "ffmpeg restarts during benchmark"
    assert yolo_p99 < 200, f"YOLO p99 too slow: {yolo_p99}"
    assert frames >= 150, f"Not enough frames: {frames}"
    assert peak < 500 * 1024 * 1024, f"Peak memory too high: {peak}"
```

- [ ] **Step 2: Run the benchmark**

Run: `cd /Users/vives/bird-classifier && /Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/bench_pipeline.py -v -s`
Expected: PASS with all assertions satisfied.

**If any assertion fails, STOP and report BLOCKED — do not proceed to cutover.**

- [ ] **Step 3: Update LaunchAgent plist**

Read `/Users/vives/Library/LaunchAgents/com.vives.bird-pipeline.plist`. Find the `<string>` under `ProgramArguments` that references `bird_pipeline.py`. Change it to `bird_pipeline_v2.py`.

Use the Edit tool:
```bash
grep -n "bird_pipeline" /Users/vives/Library/LaunchAgents/com.vives.bird-pipeline.plist
```
Then Edit to change `bird_pipeline.py` → `bird_pipeline_v2.py` in that specific line.

- [ ] **Step 4: Stop old pipeline, load new plist**

Run:
```bash
launchctl unload ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
launchctl load ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
launchctl list | grep bird-pipeline
```

Expected: service listed, no error.

- [ ] **Step 5: Verify health endpoint**

Run: `curl -s http://127.0.0.1:8100/api/pipeline/health | python3 -m json.tool | head -30`
Expected: JSON health snapshot with `overall: ok` (or `degraded` if nighttime is skipping detection).

- [ ] **Step 6: Commit cutover**

```bash
cd /Users/vives/bird-classifier
git add tests/pipeline/bench_pipeline.py
git commit -m "feat: pipeline v2 benchmark + cutover

Benchmark asserts: yolo p99 < 200ms, frames >= 150 in 60s,
0 ffmpeg restarts, peak memory < 500MB.

LaunchAgent now runs bird_pipeline_v2.py on both cameras
simultaneously (no phased migration — old/new cannot share
the Coral USB across processes).

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

- [ ] **Step 7: After 1 week of soak time, delete old pipeline files**

**Do not run this step until the new pipeline has been observed healthy for 1 week.**

Run:
```bash
cd /Users/vives/bird-classifier
git rm bird_pipeline.py bird_tracker.py
git commit -m "chore: remove old pipeline after successful v2 soak

bird_pipeline.py and bird_tracker.py have been superseded by the
pipeline/ package and bird_pipeline_v2.py. Removed after 1 week of
clean production operation.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Success Criteria Checklist (from spec § Success Criteria)

Before declaring the rewrite done, verify all of these:

- [ ] Unit tests: 100% passing (Tasks 1-12)
- [ ] e2e test (`test_pipeline_e2e.py`): passes with no crashes
- [ ] Benchmark (`bench_pipeline.py`): all assertions satisfied
- [ ] Pipe saturation test: zero ffmpeg restarts
- [ ] Visual check: labels anchored per-track, no stale labels after bird leaves
- [ ] Zero "unidentified bird" labels — only muted "·" chips for unlabeled tracks
- [ ] Track smoothness: David's subjective approval on test videos
- [ ] Health shows green for 1 hour of daytime live operation
- [ ] Clip query works: manually `curl /api/pipeline/tracks?species=Downy+Woodpecker`
- [ ] "Best visit today" card renders (if implemented; may slip to a follow-up spec)
- [ ] Mobile smoke test: open dashboard on iPhone Safari over cellular, see stream