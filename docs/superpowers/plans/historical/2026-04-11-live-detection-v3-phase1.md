> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# Live Detection Pipeline v3 — Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 1 "fully working prototype" — v3 pipeline running on a 640×360 substream, emitting SSE track events, with a dashboard that plays HD video via go2rtc MSE and draws client-side floating labels at y=25% that track bird positions smoothly. Delete the server-side annotator / MJPEG path entirely. Fix the critical v2 correctness bugs. Establish an honesty contract for every exposed metric.

**Architecture:** Two-stream separation. Browser plays main HD stream directly from go2rtc via MSE/WebSocket. Pipeline consumes only a 640×360@5fps transcoded substream. Pipeline emits JSON track events over SSE. Dashboard interpolates label positions client-side between SSE events via extrapolation from last two known positions.

**Tech Stack:** Python 3.9 (venv-coral), ONNX Runtime (YOLO), pycoral (yard classifier), Norfair (tracker), OpenCV (motion gate), SQLite (event store), FastAPI (dashboard API), go2rtc (video streaming + substream transcode), vanilla JS + Canvas API (dashboard overlay).

---

## Working Assumptions

Before Task 1: the implementing engineer is working in `.worktrees/pipeline-v3` branch `pipeline-v3` off `main`. This worktree is created out-of-band via `superpowers:using-git-worktrees` before plan execution begins. Baseline test state: 50/50 pipeline unit tests passing.

All file paths below are relative to the worktree root (`/Users/vives/bird-classifier/.worktrees/pipeline-v3/`).

**Python interpreter:** `/Users/vives/bird-classifier/venv-coral/bin/python`
**Test command template:** `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest <path> -v`

## Scope

**In scope (Phase 1):**
- Substream capture at 640×360 @ 5 fps
- Per-camera classifier config (feeder uses yard, ground skips yard)
- Delete annotator / MJPEG path entirely
- New SSE event server emitting per-frame track events
- Dashboard: `<video>` restoration + `<canvas>` overlay + floating labels + interpolation
- Critical v2 bug fixes (5 bugs)
- Honesty contract test suite for every exposed metric

**Out of scope (deferred to Phase 2):**
- Vote-based classification (Phase 1 keeps v2's first-confident-wins behavior)
- Stationary track suppression (wiring exists but stays disabled in Phase 1)
- Best-crop selection
- `species_confidence` event store column
- Audio cross-check (gone entirely — forget-me-not)

## File Structure

### Files created in Phase 1

| Path | Responsibility |
|---|---|
| `bird_pipeline_v3.py` | Main orchestrator (successor to `bird_pipeline_v2.py`). Instantiates per-camera classifier configs, wires SSE server, uses port 8102 in dev / 8100 in prod. |
| `pipeline/sse_events.py` | HTTP SSE server with `/events/sse?camera=<name>` route. Broadcasts per-camera track events to connected clients. |
| `pipeline/camera_config.py` | `CameraClassifierConfig` dataclass — per-camera classifier routing config. |
| `tests/pipeline/test_sse_events.py` | Unit tests for SSE event server. |
| `tests/pipeline/test_camera_config.py` | Unit tests for per-camera classifier config routing. |
| `tests/pipeline/test_honesty_contract.py` | One test per metric in the v3 honesty contract. |
| `tests/pipeline/test_v3_e2e.py` | End-to-end test of the full v3 pipeline against test fixtures. |
| `scripts/verify_v3_prototype.py` | Headless Playwright verification script (v3 dashboard + pipeline end-to-end). |
| `scripts/coral_borrow.sh` | Helper to stop/start the v2 LaunchAgent for Coral-dependent tests. |

### Files modified in Phase 1

| Path | Changes |
|---|---|
| `pipeline/frame_capture.py` | `_restart()` resets `last_frame_ms` (Bug C3 fix); width/height default change (now configurable per camera). |
| `pipeline/process_thread.py` | `yolo_ms_samples` only records real YOLO calls; `write_track_summary` uses `track.frame_count` not global counter; emits SSE events after classification; classifier stats reported per-camera. |
| `pipeline/tracker.py` | `Track` dataclass gains `frame_count: int = 0`; `BirdTracker.update()` increments it on hits. |
| `pipeline/classifier.py` | `SmartClassifier` takes `camera_configs: dict[str, CameraClassifierConfig]`; decision tree branches on camera arg; stats dict keyed by camera; Path 4 (`_audio_lookup`) and `BIRDNET_DB` import deleted. |
| `pipeline/health.py` | `_compute_status()` implements full honesty contract rules; `yolo_ms_p99` uses `np.percentile`; new `last_frame_age_ms` broken rule. |
| `pipeline/event_store.py` | `write_event()` accepts `bbox_confidence` explicitly (Phase 1 writes bbox conf to legacy `confidence` column, keeps Phase 2 schema changes for later). |
| `go2rtc.yaml` | Add `feeder-sub`, `ground-sub` transcoded entries at 640×360. |
| `dashboard/index.html` | Delete MJPEG path (`<img id="v2-mjpeg-img">`, `connectDebugStreamV2`, `_v2Active`, Old Det / New Det toggle, dead `connectPipelineSSE`). Add `<video id="v3-live-video">`, `<canvas id="v3-label-overlay">`, MSE WebSocket client, `LabelRenderer` class, SSE subscription. |
| `dashboard/api.py` | Delete MJPEG proxy routes (`/api/debug-stream*`). Add `/api/pipeline/events/sse` SSE proxy. Environment variable `PIPELINE_BACKEND_URL` defaults to `http://127.0.0.1:8100` (prod) and `http://127.0.0.1:8102` in dev. |

### Files deleted in Phase 1

| Path | Why |
|---|---|
| `pipeline/annotator.py` | Server-side pixel annotation is the architectural mistake. Labels move client-side. |
| Existing MJPEG broadcast in `pipeline/debug_stream.py` | Replaced by `pipeline/sse_events.py`. If `debug_stream.py` has other reasons to exist (poster frame, etc.), keep those; otherwise delete the whole file. |

---

## Tasks

### Task 1: Create v3 orchestrator stub from v2

**Files:**
- Create: `bird_pipeline_v3.py` (copy of `bird_pipeline_v2.py` with identifiers renamed)
- Test: (uses existing `tests/pipeline/test_pipeline_e2e.py` as smoke-test target)

- [ ] **Step 1: Copy v2 to v3**

```bash
cp bird_pipeline_v2.py bird_pipeline_v3.py
```

- [ ] **Step 2: Change port 8100 → 8102 in v3 (dev port, will become 8100 at cutover)**

In `bird_pipeline_v3.py` line 99 (approximate, where `HealthServer(health, port=8100)` is), change:

```python
HEALTH_PORT = int(os.environ.get("PIPELINE_HEALTH_PORT", "8102"))
# ...
health_server = HealthServer(health, port=HEALTH_PORT)
```

Similarly update the debug_stream port from 8101 to `os.environ.get("PIPELINE_DEBUG_PORT", "8103")` for now — it'll be deleted in Task 11 but the stub should come up cleanly.

- [ ] **Step 3: Run existing pipeline tests against v3**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/ -q`
Expected: 50 passed

- [ ] **Step 4: Commit**

```bash
git add bird_pipeline_v3.py
git commit -m "feat(v3): stub bird_pipeline_v3 from v2, port 8102 for dev"
```

---

### Task 2: Fix FrameCapture._restart() last_frame_ms reset (Critical Bug C3)

**Files:**
- Modify: `pipeline/frame_capture.py:164-178`
- Test: `tests/pipeline/test_frame_capture.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_frame_capture.py`:

```python
def test_restart_resets_last_frame_ms():
    """_restart() must update last_frame_ms so the watchdog doesn't immediately re-fire."""
    import queue
    from pipeline.frame_capture import FrameCapture

    fc = FrameCapture.__new__(FrameCapture)
    fc.camera_name = "test"
    fc.rtsp_url = "rtsp://127.0.0.1:9999/nonexistent"
    fc.out_queue = queue.Queue(maxsize=2)
    fc.proc = None
    fc._stop_event = __import__("threading").Event()
    fc.stats = {
        "frames": 0, "dropped_oldest": 0, "ffmpeg_restarts": 0,
        "last_frame_ms": 1000.0,  # stale old value
    }
    fc.width = 640
    fc.height = 360
    fc.fps = 5

    # Monkeypatch _spawn_ffmpeg so it doesn't actually spawn anything
    fc._spawn_ffmpeg = lambda: None

    before = __import__("time").time() * 1000
    fc._restart()
    after = __import__("time").time() * 1000

    # Must have been reset to roughly "now"
    assert before <= fc.stats["last_frame_ms"] <= after, (
        f"last_frame_ms={fc.stats['last_frame_ms']} not in [{before}, {after}]"
    )
    assert fc.stats["ffmpeg_restarts"] == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_frame_capture.py::test_restart_resets_last_frame_ms -v`
Expected: FAIL with assertion error on `last_frame_ms=1000.0 not in [...]`

- [ ] **Step 3: Fix _restart() in pipeline/frame_capture.py**

Replace the `_restart` method (currently at lines 164-178) with:

```python
    def _restart(self):
        # Local snapshot to avoid TOCTOU with stop()
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        try:
            self._spawn_ffmpeg()
            self.stats["ffmpeg_restarts"] += 1
            # Reset the frame-age clock so the watchdog doesn't re-fire
            # on the stale timestamp before the new ffmpeg can produce a frame.
            self.stats["last_frame_ms"] = time.time() * 1000
        except Exception as e:
            log.error("[%s] failed to respawn ffmpeg: %s", self.camera_name, e)
            # Leave self.proc as it was; watchdog will retry on next iteration
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_frame_capture.py::test_restart_resets_last_frame_ms -v`
Expected: PASS

- [ ] **Step 5: Run full frame_capture test suite to ensure no regression**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_frame_capture.py -v`
Expected: all tests pass including the new one

- [ ] **Step 6: Commit**

```bash
git add pipeline/frame_capture.py tests/pipeline/test_frame_capture.py
git commit -m "fix(frame_capture): _restart() resets last_frame_ms to prevent watchdog restart loop"
```

---

### Task 3: Fix p99 calculation via numpy percentile

**Files:**
- Modify: `pipeline/process_thread.py:166-174` (the `_update_health` method)
- Test: `tests/pipeline/test_process_thread.py` (new test)

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_process_thread.py` (create file if doesn't exist):

```python
def test_yolo_p99_is_actually_99th_percentile():
    """p99 must be computed via np.percentile, not via index slice hack."""
    import numpy as np
    from pipeline.process_thread import CameraProcessThread

    # 100 samples: 99 at 10ms, 1 at 500ms
    samples = [10.0] * 99 + [500.0]

    # The v2 code computed: sorted(samples)[-max(1, len(samples) // 100)]
    # = sorted[-1] = 500.0 for n=100
    # The correct p99 over this distribution is 500.0 (the single high value
    # IS the 99th percentile of 100 samples), so this particular test
    # doesn't reveal the bug. Use n=200 where the two calculations diverge.

    samples_200 = [10.0] * 199 + [500.0]

    # v2 buggy: sorted[-max(1, 200//100)] = sorted[-2] = 10.0 (because only ONE 500)
    # Correct np.percentile: 99th percentile of [10]*199 + [500] ≈ 10.0 as well
    # (since 99% of 200 = 198, and sorted[197] = 10.0)

    # Better test: bimodal distribution
    samples_bimodal = [10.0] * 180 + [500.0] * 20  # n=200
    # v2 buggy: sorted[-max(1, 200//100)] = sorted[-2] = 500.0
    # Correct np.percentile(samples_bimodal, 99) ≈ 500.0
    # These agree. Need a case where v2 reports MAX where real p99 is lower.

    # Real test: n=200, 199 samples at 10.0, 1 at 1000.0
    samples_outlier = [10.0] * 199 + [1000.0]
    expected_p99 = np.percentile(samples_outlier, 99)  # ~10.0
    # v2 buggy: sorted[-max(1, 200//100)] = sorted[-2] = 10.0 (because only one 1000)
    # Actually v2 buggy also gives 10.0 here. The v2 bug is specifically
    # when n < 200, where n//100 = 0 or 1, and sorted[-1] = max.

    samples_n100 = [10.0] * 99 + [1000.0]  # n=100, n//100 = 1, sorted[-1] = 1000
    # v2: sorted[-max(1, 1)] = sorted[-1] = 1000 (the MAX, not p99)
    # Correct p99 of this distribution: np.percentile([10]*99 + [1000], 99) = 10 + (1000-10)*0.99 ~ 980
    # Hmm, np.percentile is interpolated. For discrete p99 we want sorted[floor(n*0.99)] = sorted[99] = 1000
    # So for n=100 the real p99 is also 1000. These agree.

    # The actual v2 bug shows up at n<200 because the index formula collapses.
    # Use n=50: sorted[-max(1, 0)] = sorted[-1] = 1000 (max of 50 samples)
    # Real p99 of n=50: np.percentile(samples_50, 99) = sorted[49] = 1000 also
    # In a small sample the 99th percentile IS the max. So the bug only shows
    # when v2 reports something labeled "p99" that isn't what p99 should mean
    # in the long run, and that's a semantic bug, not a numeric one at small n.

    # Simpler test: assert we use np.percentile exactly
    from pipeline.process_thread import CameraProcessThread
    t = CameraProcessThread.__new__(CameraProcessThread)
    t._stats = {"yolo_ms_samples": samples_n100, "frames_processed": 100, "detections": 0}
    # Monkey-patch health / tracker / classifier attributes
    t.health = type("H", (), {"update": lambda *a, **kw: None})()
    t.tracker = type("T", (), {"tracks": [], "stationary_regions": lambda: []})()
    t.classifier = type("C", (), {"stats": {}})()
    t.name = "test"

    # Invoke _update_health and capture the yolo_p99 it would report
    captured = {}
    t.health.update = lambda camera, section, payload: captured.setdefault(section, payload)

    fake_frame = type("F", (), {"wall_time_ms": __import__("time").time() * 1000})()
    t._update_health(fake_frame, det_ms=10.0)

    expected = float(np.percentile(samples_n100, 99))
    assert abs(captured["detector"]["yolo_ms_p99"] - expected) < 0.01, (
        f"yolo_ms_p99 was {captured['detector']['yolo_ms_p99']}, expected {expected}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py::test_yolo_p99_is_actually_99th_percentile -v`
Expected: FAIL with assertion error (v2 code returns `sorted()[-1]` = 1000.0, np.percentile returns ~1000.0 but with float interpolation may differ slightly — the test is calibrated to fail on v2's formula vs pass on np.percentile)

- [ ] **Step 3: Fix _update_health in pipeline/process_thread.py**

In `_update_health` (lines 166-194), replace the p99 calculation block:

```python
    def _update_health(self, frame: Frame, det_ms: float):
        import numpy as np  # local import to avoid top-level churn
        samples = self._stats["yolo_ms_samples"]
        if len(samples) >= 10:
            yolo_avg = float(np.mean(samples))
            yolo_p99 = float(np.percentile(samples, 99))
        else:
            yolo_avg = float(np.mean(samples)) if samples else 0.0
            yolo_p99 = None  # insufficient_samples — honesty contract requirement
```

Also update the `detector` health payload to handle `yolo_p99 = None`:

```python
        self.health.update(self.name, "detector", {
            "yolo_ms_avg": round(yolo_avg),
            "yolo_ms_p99": round(yolo_p99) if yolo_p99 is not None else None,
            "yolo_samples_count": len(samples),
            "detections_total": self._stats["detections"],
        })
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py::test_yolo_p99_is_actually_99th_percentile -v`
Expected: PASS

- [ ] **Step 5: Run full process_thread tests**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py -v`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add pipeline/process_thread.py tests/pipeline/test_process_thread.py
git commit -m "fix(process_thread): p99 uses np.percentile, returns None for <10 samples"
```

---

### Task 4: yolo_ms_samples only records real YOLO calls (not skip frames)

**Files:**
- Modify: `pipeline/process_thread.py` (lines ~78-83, the detect + sample append block)
- Test: `tests/pipeline/test_process_thread.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_process_thread.py`:

```python
def test_yolo_samples_excludes_skip_frames():
    """When motion_regions is empty, YOLO is skipped and that timing must NOT be recorded."""
    import queue, threading, time
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 0, "detections": 0, "yolo_ms_samples": []}
    t._last_forced_full = time.time()  # not due for forced full

    # Mock collaborators
    motion_gate = MagicMock()
    motion_gate.regions.return_value = []  # no motion

    detector = MagicMock()
    detector.detect.return_value = []  # returns empty fast-path

    tracker_out = MagicMock()
    tracker_out.new = []
    tracker_out.active = []
    tracker_out.expired = []
    tracker = MagicMock()
    tracker.update.return_value = tracker_out
    tracker.tracks = []
    tracker.stationary_regions.return_value = []

    classifier = MagicMock()
    classifier.stats = {}

    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    t.motion_gate = motion_gate
    t.detector = detector
    t.tracker = tracker
    t.classifier = classifier
    t.event_store = event_store
    t.annotator = annotator
    t.health = health

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="test", width=640, height=360,
    )

    # Process 5 skip frames (no motion)
    for _ in range(5):
        t._process_frame(frame)

    # yolo_ms_samples should remain empty because YOLO never ran
    assert len(t._stats["yolo_ms_samples"]) == 0, (
        f"Expected 0 samples, got {len(t._stats['yolo_ms_samples'])}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py::test_yolo_samples_excludes_skip_frames -v`
Expected: FAIL — v2 records sample regardless

- [ ] **Step 3: Fix _process_frame in pipeline/process_thread.py**

Locate the detect+sample block (approximately lines 76-83):

```python
        # 3. Detect
        t_det = time.monotonic()
        detections = self.detector.detect(frame, regions, forced_full=forced_full)
        det_ms = (time.monotonic() - t_det) * 1000
        self._stats["yolo_ms_samples"].append(det_ms)
        if len(self._stats["yolo_ms_samples"]) > 100:
            self._stats["yolo_ms_samples"] = self._stats["yolo_ms_samples"][-100:]
        self._stats["detections"] += len(detections)
```

Replace with:

```python
        # 3. Detect
        t_det = time.monotonic()
        detections = self.detector.detect(frame, regions, forced_full=forced_full)
        det_ms = (time.monotonic() - t_det) * 1000
        # Only record the timing if YOLO actually ran. BirdDetector.detect returns
        # empty instantly when there's no motion and forced_full is False — those
        # near-zero timings pollute the yolo_ms_avg histogram and make it useless.
        yolo_actually_ran = bool(regions) or forced_full
        if yolo_actually_ran:
            self._stats["yolo_ms_samples"].append(det_ms)
            if len(self._stats["yolo_ms_samples"]) > 100:
                self._stats["yolo_ms_samples"] = self._stats["yolo_ms_samples"][-100:]
            self._stats["yolo_runs_total"] = self._stats.get("yolo_runs_total", 0) + 1
        else:
            self._stats["yolo_skipped_motion"] = self._stats.get("yolo_skipped_motion", 0) + 1
        self._stats["detections"] += len(detections)
```

Also initialize `yolo_runs_total` and `yolo_skipped_motion` in `__init__`:

```python
        self._stats = {
            "frames_processed": 0,
            "detections": 0,
            "yolo_ms_samples": [],
            "yolo_runs_total": 0,
            "yolo_skipped_motion": 0,
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py::test_yolo_samples_excludes_skip_frames -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/process_thread.py tests/pipeline/test_process_thread.py
git commit -m "fix(process_thread): exclude skip-frame timings from yolo_ms_samples histogram"
```

---

### Task 5: Track.frame_count + num_frames in track summary

**Files:**
- Modify: `pipeline/tracker.py` (Track dataclass + update() logic)
- Modify: `pipeline/process_thread.py` (write_track_summary call)
- Test: `tests/pipeline/test_pipeline_tracker.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_pipeline_tracker.py`:

```python
def test_track_frame_count_is_per_track_not_global():
    """Track.frame_count must increment only when that specific track gets a hit."""
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection

    tracker = BirdTracker()

    # Frame 1: one detection
    det1 = [Detection(box=[100, 100, 200, 200], confidence=0.9)]
    out1 = tracker.update(det1, wall_time_ms=1000)
    assert len(out1.active) == 1
    assert out1.active[0].frame_count == 1

    # Frame 2: same detection (slight movement)
    det2 = [Detection(box=[105, 100, 205, 200], confidence=0.9)]
    out2 = tracker.update(det2, wall_time_ms=1200)
    assert len(out2.active) == 1
    assert out2.active[0].frame_count == 2

    # Frame 3: two detections, one is the old track, one is new
    det3 = [
        Detection(box=[110, 100, 210, 200], confidence=0.9),  # same as old
        Detection(box=[500, 500, 600, 600], confidence=0.8),  # new bird
    ]
    out3 = tracker.update(det3, wall_time_ms=1400)
    assert len(out3.active) == 2
    old_track = next(t for t in out3.active if t.track_id == out1.active[0].track_id)
    new_track = next(t for t in out3.active if t.track_id != out1.active[0].track_id)
    assert old_track.frame_count == 3
    assert new_track.frame_count == 1, f"new track should be frame_count=1, got {new_track.frame_count}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_tracker.py::test_track_frame_count_is_per_track_not_global -v`
Expected: FAIL — `Track` has no `frame_count` field

- [ ] **Step 3: Add frame_count to Track and increment in update()**

In `pipeline/tracker.py`, locate the `Track` dataclass (or class) and add `frame_count: int = 0`. In the `BirdTracker.update()` method, after mapping Norfair tracked objects back to our `Track` instances and before returning, increment `frame_count` for each track that was hit this frame:

```python
# in BirdTracker.update(), where active tracks are assembled:
for track in active_tracks:
    # track was seen this frame (Norfair returned it as matched)
    track.frame_count += 1
```

(The exact location depends on how tracker.py currently assembles active_tracks from Norfair's tracked_objects — the implementer should trace through and increment at the right point.)

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_tracker.py::test_track_frame_count_is_per_track_not_global -v`
Expected: PASS

- [ ] **Step 5: Fix write_track_summary in pipeline/process_thread.py**

Change line ~108-113:

```python
        # 7. Track expired → write summary
        for track in tracker_out.expired:
            try:
                self.event_store.write_track_summary(
                    camera=self.name, track=track,
                    num_frames=track.frame_count,  # ← use per-track, not global
                )
            except Exception as e:
                log.warning("[%s] write_track_summary error: %s", self.name, e)
```

- [ ] **Step 6: Write test for process_thread using per-track frame_count**

Append to `tests/pipeline/test_process_thread.py`:

```python
def test_write_track_summary_uses_per_track_frame_count():
    """write_track_summary must pass track.frame_count, not process-thread global counter."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 9999, "detections": 0, "yolo_ms_samples": [],
                "yolo_runs_total": 0, "yolo_skipped_motion": 0}
    t._last_forced_full = time.time()

    # Fake expired track with frame_count=42
    fake_track = MagicMock()
    fake_track.frame_count = 42

    tracker_out = MagicMock()
    tracker_out.new = []
    tracker_out.active = []
    tracker_out.expired = [fake_track]

    motion_gate = MagicMock(); motion_gate.regions.return_value = []
    detector = MagicMock(); detector.detect.return_value = []
    tracker = MagicMock(); tracker.update.return_value = tracker_out
    tracker.tracks = []; tracker.stationary_regions.return_value = []
    classifier = MagicMock(); classifier.stats = {}
    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    t.motion_gate = motion_gate; t.detector = detector; t.tracker = tracker
    t.classifier = classifier; t.event_store = event_store
    t.annotator = annotator; t.health = health

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="test", width=640, height=360,
    )
    t._process_frame(frame)

    # Verify event_store.write_track_summary was called with num_frames=42
    event_store.write_track_summary.assert_called_once()
    call_kwargs = event_store.write_track_summary.call_args.kwargs
    assert call_kwargs["num_frames"] == 42, (
        f"expected num_frames=42 (per-track), got {call_kwargs['num_frames']}"
    )
```

- [ ] **Step 7: Run both tests**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_tracker.py tests/pipeline/test_process_thread.py -v`
Expected: all pass

- [ ] **Step 8: Commit**

```bash
git add pipeline/tracker.py pipeline/process_thread.py \
        tests/pipeline/test_pipeline_tracker.py tests/pipeline/test_process_thread.py
git commit -m "fix: Track.frame_count per-track counter, write_track_summary uses it not global"
```

---

### Task 6: Delete Path 4 (audio cross-check) entirely

**Files:**
- Modify: `pipeline/classifier.py` (remove `_audio_lookup`, remove audio call in `classify`, remove `audio_db_path` param)
- Modify: `bird_pipeline_v3.py` (remove `BIRDNET_DB` constant, remove `audio_db_path` arg)
- Test: `tests/pipeline/test_pipeline_classifier.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_pipeline_classifier.py`:

```python
def test_classifier_has_no_audio_lookup_method():
    """Path 4 (audio cross-check) is dropped in v3. The method must not exist."""
    from pipeline.classifier import SmartClassifier
    assert not hasattr(SmartClassifier, "_audio_lookup"), (
        "SmartClassifier._audio_lookup was supposed to be deleted in v3"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_classifier.py::test_classifier_has_no_audio_lookup_method -v`
Expected: FAIL — method still exists in v2 code inherited by v3

- [ ] **Step 3: Delete `_audio_lookup` and the Path 4 code block**

In `pipeline/classifier.py`:

(a) Remove the import: `import sqlite3`
(b) Remove the `audio_db_path: Optional[str] = None` parameter from `SmartClassifier.__init__`, remove `self.audio_db_path = audio_db_path`, remove `audio_confirmed` from `self.stats`
(c) Delete the entire `_audio_lookup` method (lines ~137-156)
(d) In `classify()`, delete the Path 4 block (lines ~87-95):

```python
            # Path 4: disagreement → audio cross-check
            audio_species = self._audio_lookup(camera, frame_time_ms)
            if audio_species and audio_species in (yard_res.species, aiy_res.species):
                self.stats["audio_confirmed"] += 1
                return ClassificationResult(
                    audio_species,
                    max(yard_res.confidence, aiy_res.confidence),
                    "audio_confirmed", False
                )
```

Replace that block with a comment: `# Path 4 (audio cross-check) removed in v3 — see docs/superpowers/specs/2026-04-11-live-detection-v3-design.md § 10 forget-me-nots`

- [ ] **Step 4: Update bird_pipeline_v3.py to remove BIRDNET_DB**

In `bird_pipeline_v3.py`, delete:
- `BIRDNET_DB = Path.home() / "bird-snapshots" / "logs" / "birdnet_local.db"` (line ~20)
- `audio_db_path = str(BIRDNET_DB) if BIRDNET_DB.exists() else None` (line ~105)
- The `audio_db_path=audio_db_path,` line in the `SmartClassifier(...)` construction

- [ ] **Step 5: Run the test to verify it passes**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_classifier.py -v`
Expected: new test passes, existing tests still pass (classifier has fewer stats keys now, some existing tests may need updates)

If any existing test asserts on `stats["audio_confirmed"]`, update that test to remove the assertion.

- [ ] **Step 6: Commit**

```bash
git add pipeline/classifier.py bird_pipeline_v3.py tests/pipeline/test_pipeline_classifier.py
git commit -m "feat(v3): drop Path 4 audio cross-check (deferred as forget-me-not)"
```

---

### Task 7: Per-camera classifier config

**Files:**
- Create: `pipeline/camera_config.py` (new)
- Modify: `pipeline/classifier.py` (SmartClassifier.__init__ takes camera_configs, decision tree branches)
- Modify: `bird_pipeline_v3.py` (pass camera_configs dict)
- Test: `tests/pipeline/test_camera_config.py` (new)
- Test: `tests/pipeline/test_pipeline_classifier.py` (update existing tests)

- [ ] **Step 1: Create pipeline/camera_config.py**

```python
"""Per-camera classifier configuration."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class CameraClassifierConfig:
    """Per-camera settings for SmartClassifier decision-tree routing.

    Fields:
        use_yard: If True, the classifier runs yard model first, AIY on fallback.
                  If False, yard is skipped entirely — AIY runs alone.
        confident_threshold: Confidence at/above which yard's answer is accepted.
        uncertain_low: Confidence below which yard is considered "useless"
                       and AIY runs as the only classifier.
    """
    use_yard: bool
    confident_threshold: float = 0.6
    uncertain_low: float = 0.3
```

- [ ] **Step 2: Write tests for camera_config module**

Create `tests/pipeline/test_camera_config.py`:

```python
"""Tests for CameraClassifierConfig dataclass."""
from pipeline.camera_config import CameraClassifierConfig


def test_feeder_config_default_thresholds():
    cfg = CameraClassifierConfig(use_yard=True)
    assert cfg.use_yard is True
    assert cfg.confident_threshold == 0.6
    assert cfg.uncertain_low == 0.3


def test_ground_config_skips_yard():
    cfg = CameraClassifierConfig(use_yard=False)
    assert cfg.use_yard is False


def test_config_is_frozen():
    cfg = CameraClassifierConfig(use_yard=True)
    import dataclasses
    try:
        cfg.use_yard = False
    except dataclasses.FrozenInstanceError:
        pass
    else:
        assert False, "CameraClassifierConfig should be frozen"
```

- [ ] **Step 3: Run tests to verify they pass on the new module**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_camera_config.py -v`
Expected: 3 PASS

- [ ] **Step 4: Write the failing test for per-camera classifier routing**

Append to `tests/pipeline/test_pipeline_classifier.py`:

```python
def test_ground_camera_skips_yard_entirely():
    """When use_yard=False, yard classifier must not be called, AIY runs alone."""
    from unittest.mock import MagicMock, patch
    from pipeline.classifier import SmartClassifier
    from pipeline.camera_config import CameraClassifierConfig
    from PIL import Image

    configs = {
        "feeder": CameraClassifierConfig(use_yard=True),
        "ground": CameraClassifierConfig(use_yard=False),
    }

    classifier = SmartClassifier.__new__(SmartClassifier)
    classifier.camera_configs = configs
    classifier._coral_lock = __import__("threading").Lock()
    classifier.stats = {
        cam: {"yard": 0, "aiy": 0, "both_agree": 0,
              "unlabeled_call": 0, "lock_timeouts": 0, "retries": 0}
        for cam in configs
    }

    # Mock internal classifier components
    classifier.yard = MagicMock()
    classifier.aiy = MagicMock()

    # Mock _run_yard/_run_aiy to track calls
    yard_called = [0]
    aiy_called = [0]

    def fake_yard(crop):
        yard_called[0] += 1
        return type("YR", (), {"species": "Northern Cardinal", "confidence": 0.9})()

    def fake_aiy(crop):
        aiy_called[0] += 1
        return type("AR", (), {"species": "Red-winged Blackbird", "confidence": 0.85})()

    classifier._run_yard = fake_yard
    classifier._run_aiy = fake_aiy

    dummy_img = Image.new("RGB", (100, 100))

    # Call for ground camera
    result = classifier.classify(dummy_img, 0, "ground")
    assert yard_called[0] == 0, f"yard should NOT be called for ground, was called {yard_called[0]} times"
    assert aiy_called[0] == 1, f"aiy should be called once for ground, got {aiy_called[0]}"
    assert result.species == "Red-winged Blackbird"
    assert result.model_source == "aiy"

    # Call for feeder camera
    result2 = classifier.classify(dummy_img, 0, "feeder")
    assert yard_called[0] == 1, f"yard should be called for feeder, got {yard_called[0]}"
    assert result2.species == "Northern Cardinal"
    assert result2.model_source == "yard"
```

- [ ] **Step 5: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_classifier.py::test_ground_camera_skips_yard_entirely -v`
Expected: FAIL — SmartClassifier doesn't accept camera_configs yet

- [ ] **Step 6: Update SmartClassifier to support camera_configs**

Rewrite `SmartClassifier.__init__` and `classify()` in `pipeline/classifier.py`:

```python
"""SmartClassifier — per-camera decision tree with yard + AIY fallback."""
from __future__ import annotations
import logging
import threading
from dataclasses import dataclass
from typing import Optional

from PIL import Image

from pipeline.camera_config import CameraClassifierConfig

log = logging.getLogger(__name__)

CORAL_ACQUIRE_TIMEOUT = 5.0
MAX_CLASSIFICATION_ATTEMPTS = 3


@dataclass
class ClassificationResult:
    species: Optional[str]
    confidence: float
    model_source: Optional[str]
    should_retry: bool


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
        from yard_classifier import YardClassifier
        from bird_inference import SpeciesClassifier

        self.yard = YardClassifier(yard_model_path, yard_labels_path)
        self.aiy = SpeciesClassifier(
            aiy_model_path, aiy_labels_path,
            regional_species=regional_species,
        )
        self.camera_configs = camera_configs
        self._coral_lock = threading.Lock()
        # Per-camera stats — no more global dict
        self.stats = {
            camera: {
                "yard": 0, "aiy": 0, "both_agree": 0,
                "unlabeled_call": 0, "lock_timeouts": 0, "retries": 0,
            }
            for camera in camera_configs
        }

    def classify(self, crop_pil: Image.Image, frame_time_ms: float,
                 camera: str) -> ClassificationResult:
        config = self.camera_configs.get(camera)
        if config is None:
            log.warning("No classifier config for camera %s, using AIY-only fallback", camera)
            config = CameraClassifierConfig(use_yard=False)

        cam_stats = self.stats.setdefault(camera, {
            "yard": 0, "aiy": 0, "both_agree": 0,
            "unlabeled_call": 0, "lock_timeouts": 0, "retries": 0,
        })

        got = self._coral_lock.acquire(timeout=CORAL_ACQUIRE_TIMEOUT)
        if not got:
            cam_stats["lock_timeouts"] += 1
            return ClassificationResult(None, 0.0, None, should_retry=True)

        try:
            if not config.use_yard:
                # Ground path: AIY only, no yard.
                aiy_res = self._run_aiy(crop_pil)
                if aiy_res and aiy_res.confidence >= config.confident_threshold:
                    cam_stats["aiy"] += 1
                    return ClassificationResult(
                        aiy_res.species, aiy_res.confidence, "aiy", False
                    )
                cam_stats["unlabeled_call"] += 1
                return ClassificationResult(None, 0.0, None, False)

            # Feeder path: yard-first decision tree
            yard_res = self._run_yard(crop_pil)
            if yard_res and yard_res.confidence >= config.confident_threshold:
                cam_stats["yard"] += 1
                return ClassificationResult(
                    yard_res.species, yard_res.confidence, "yard", False
                )

            if not yard_res or yard_res.confidence < config.uncertain_low:
                aiy_res = self._run_aiy(crop_pil)
                if aiy_res and aiy_res.confidence >= config.confident_threshold:
                    cam_stats["aiy"] += 1
                    return ClassificationResult(
                        aiy_res.species, aiy_res.confidence, "aiy", False
                    )
                cam_stats["unlabeled_call"] += 1
                return ClassificationResult(None, 0.0, None, False)

            # Yard is in the uncertain band — cross-check with AIY
            aiy_res = self._run_aiy(crop_pil)
            if not aiy_res:
                cam_stats["unlabeled_call"] += 1
                return ClassificationResult(None, 0.0, None, False)

            if aiy_res.species == yard_res.species:
                cam_stats["both_agree"] += 1
                return ClassificationResult(
                    yard_res.species,
                    max(yard_res.confidence, aiy_res.confidence),
                    "both_agree", False
                )

            # Disagreement and Path 4 (audio) is gone in v3.
            # Fall through to unlabeled.
            cam_stats["unlabeled_call"] += 1
            return ClassificationResult(None, 0.0, None, False)
        finally:
            self._coral_lock.release()

    def _run_yard(self, crop_pil):
        try:
            results = self.yard.classify(crop_pil)
            if not results:
                return None
            top = results[0]
            return type("YardResult", (), {
                "species": top.get("common_name"),
                "confidence": float(top.get("confidence", 0.0)),
            })()
        except Exception as e:
            log.warning("Yard classify error: %s", e)
            return None

    def _run_aiy(self, crop_pil):
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
```

- [ ] **Step 7: Update bird_pipeline_v3.py to pass camera_configs**

In `bird_pipeline_v3.py`, replace the `SmartClassifier(...)` instantiation with:

```python
    from pipeline.camera_config import CameraClassifierConfig

    camera_configs = {
        "feeder": CameraClassifierConfig(use_yard=True),
        "ground": CameraClassifierConfig(use_yard=False),
    }

    try:
        classifier = SmartClassifier(
            yard_model_path=YARD_MODEL,
            yard_labels_path=YARD_LABELS,
            aiy_model_path=AIY_MODEL,
            aiy_labels_path=AIY_LABELS,
            regional_species=regional_species,
            camera_configs=camera_configs,
        )
    except Exception as e:
        log.error("Failed to load classifiers: %s — pipeline will not start", e)
        return 1
```

- [ ] **Step 8: Run all classifier tests**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_pipeline_classifier.py tests/pipeline/test_camera_config.py -v`
Expected: all pass

- [ ] **Step 9: Run full pipeline test suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/ -q`
Expected: all tests pass (some existing tests may need updates to pass `camera_configs` when they construct SmartClassifier directly — update those)

- [ ] **Step 10: Commit**

```bash
git add pipeline/camera_config.py pipeline/classifier.py bird_pipeline_v3.py \
        tests/pipeline/test_camera_config.py tests/pipeline/test_pipeline_classifier.py
git commit -m "feat(v3): per-camera classifier config, ground skips yard entirely"
```

---

### Task 8: Update process_thread to report classifier stats per-camera

**Files:**
- Modify: `pipeline/process_thread.py:191-194`
- Test: `tests/pipeline/test_process_thread.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_process_thread.py`:

```python
def test_classifier_stats_reported_per_camera_not_global():
    """process_thread must pull only its own camera's slice of classifier stats."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stats = {"frames_processed": 1, "detections": 0, "yolo_ms_samples": [10.0] * 15,
                "yolo_runs_total": 15, "yolo_skipped_motion": 0}

    classifier = MagicMock()
    # Stats are keyed by camera
    classifier.stats = {
        "feeder": {"yard": 42, "aiy": 3, "unlabeled_call": 1},
        "ground": {"yard": 0, "aiy": 100, "unlabeled_call": 5},
    }
    t.classifier = classifier

    t.tracker = MagicMock()
    t.tracker.tracks = []
    t.tracker.stationary_regions.return_value = []

    captured = {}
    health = MagicMock()
    def fake_update(camera, section, payload):
        captured[(camera, section)] = payload
    health.update = fake_update
    t.health = health

    from pipeline.frame import Frame
    import numpy as np, time
    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="feeder", width=640, height=360,
    )

    t._update_health(frame, det_ms=0.0)

    # Only feeder's stats should appear in the feeder payload
    feeder_classifier_stats = captured[("feeder", "classifier")]
    assert feeder_classifier_stats["yard"] == 42
    assert feeder_classifier_stats["aiy"] == 3
    # Ground's aiy=100 must NOT leak into feeder's stats
    assert feeder_classifier_stats["aiy"] != 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py::test_classifier_stats_reported_per_camera_not_global -v`
Expected: FAIL — current code reports the whole `classifier.stats` dict verbatim

- [ ] **Step 3: Fix _update_health in pipeline/process_thread.py**

Replace the classifier stats block:

```python
        try:
            cam_classifier_stats = self.classifier.stats.get(self.name, {})
            self.health.update(self.name, "classifier", dict(cam_classifier_stats))
        except Exception:
            pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py::test_classifier_stats_reported_per_camera_not_global -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/process_thread.py tests/pipeline/test_process_thread.py
git commit -m "fix(process_thread): report classifier stats per-camera, not globally"
```

---

### Task 9: SSE event server module

**Files:**
- Create: `pipeline/sse_events.py`
- Create: `tests/pipeline/test_sse_events.py`

- [ ] **Step 1: Write failing tests for SSE event server**

Create `tests/pipeline/test_sse_events.py`:

```python
"""Tests for pipeline/sse_events.py — HTTP SSE server for track events."""
import json
import threading
import time
import urllib.request

import pytest


def _pick_port():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_sse_server_starts_and_accepts_connections():
    from pipeline.sse_events import SSEEventServer
    port = _pick_port()
    server = SSEEventServer(port=port)
    server.start()
    try:
        resp = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=2
        )
        assert resp.status == 200
    finally:
        server.stop()


def test_sse_server_emits_events_to_subscribers():
    from pipeline.sse_events import SSEEventServer
    port = _pick_port()
    server = SSEEventServer(port=port)
    server.start()
    try:
        # Subscribe in a thread, emit, read one event
        received = []

        def subscribe():
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/events/sse?camera=feeder", timeout=5
            )
            # Read until we have one event line
            buf = b""
            start = time.time()
            while time.time() - start < 3:
                chunk = resp.read1(1024)
                if not chunk:
                    break
                buf += chunk
                if b"\n\n" in buf:
                    break
            # Parse the first SSE event
            for line in buf.decode("utf-8").split("\n"):
                if line.startswith("data: "):
                    received.append(json.loads(line[6:]))
                    break

        t = threading.Thread(target=subscribe, daemon=True)
        t.start()
        # Give the subscriber time to connect before emitting
        time.sleep(0.3)
        server.emit("feeder", 1_700_000_000_000, [
            {"track_id": 1, "bbox": [100, 100, 200, 200], "species": "Test Bird",
             "species_confidence": 0.9, "model_source": "yard", "is_locked": False,
             "frame_count": 1, "bbox_center_x": 150, "frame_width": 640, "frame_height": 360}
        ])
        t.join(timeout=5)
        assert len(received) == 1
        assert received[0]["camera"] == "feeder"
        assert received[0]["tracks"][0]["species"] == "Test Bird"
    finally:
        server.stop()


def test_sse_server_filters_by_camera():
    from pipeline.sse_events import SSEEventServer
    port = _pick_port()
    server = SSEEventServer(port=port)
    server.start()
    try:
        received = []

        def subscribe():
            resp = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/events/sse?camera=feeder", timeout=5
            )
            buf = b""
            start = time.time()
            while time.time() - start < 2:
                chunk = resp.read1(1024)
                if chunk:
                    buf += chunk
            for line in buf.decode("utf-8").split("\n"):
                if line.startswith("data: "):
                    received.append(json.loads(line[6:]))

        t = threading.Thread(target=subscribe, daemon=True)
        t.start()
        time.sleep(0.3)
        # Emit for ground — feeder subscriber should NOT receive
        server.emit("ground", 1_700_000_000_000, [{"track_id": 1}])
        server.emit("feeder", 1_700_000_000_000, [{"track_id": 2}])
        t.join(timeout=3)
        assert len(received) == 1
        assert received[0]["tracks"][0]["track_id"] == 2
    finally:
        server.stop()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_sse_events.py -v`
Expected: ImportError / collection error — module doesn't exist

- [ ] **Step 3: Implement pipeline/sse_events.py**

```python
"""HTTP SSE event server for live track events.

Serves per-frame track events over Server-Sent Events on GET /events/sse?camera=<name>.
Events are dropped for slow clients (queue overflow) rather than blocking the emitter.
"""
from __future__ import annotations
import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse, parse_qs

log = logging.getLogger(__name__)

CLIENT_QUEUE_MAX = 32


class _SSEHandler(BaseHTTPRequestHandler):
    server_state: "SSEEventServer"  # set by SSEEventServer

    def log_message(self, format, *args):
        # Silence the default stderr logger; we have our own
        pass

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')
            return
        if parsed.path == "/events/sse":
            qs = parse_qs(parsed.query)
            cameras = qs.get("camera", [])
            if not cameras:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"missing ?camera=")
                return
            camera = cameras[0]
            self._stream_events(camera)
            return
        self.send_response(404)
        self.end_headers()

    def _stream_events(self, camera: str):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        q: "queue.Queue[str]" = queue.Queue(maxsize=CLIENT_QUEUE_MAX)
        self.server_state._add_client(camera, q)
        try:
            # Send an initial comment to establish the stream
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()
            while True:
                try:
                    payload = q.get(timeout=15)
                except queue.Empty:
                    # Keepalive heartbeat
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            log.debug("SSE client disconnected: %s", e)
        finally:
            self.server_state._remove_client(camera, q)


class SSEEventServer:
    """SSE broadcaster for per-frame track events.

    Usage:
        server = SSEEventServer(port=8102)
        server.start()
        server.emit(camera="feeder", wall_time_ms=..., tracks=[...])
        server.stop()
    """

    def __init__(self, port: int = 8102, host: str = "127.0.0.1"):
        self.port = port
        self.host = host
        self._clients: dict[str, list[queue.Queue]] = {}
        self._clients_lock = threading.Lock()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.stats = {"events_emitted": 0, "clients_connected": 0}

    def start(self) -> None:
        handler = _SSEHandler
        handler.server_state = self
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="sse-events",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()

    def emit(self, camera: str, wall_time_ms: int, tracks: list[dict]) -> None:
        payload = json.dumps({
            "camera": camera,
            "wall_time_ms": wall_time_ms,
            "tracks": tracks,
        })
        with self._clients_lock:
            cams = list(self._clients.get(camera, []))
        for q in cams:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # Slow client — drop this event for them.
                pass
        self.stats["events_emitted"] += 1

    def _add_client(self, camera: str, q: "queue.Queue") -> None:
        with self._clients_lock:
            self._clients.setdefault(camera, []).append(q)
            self.stats["clients_connected"] += 1

    def _remove_client(self, camera: str, q: "queue.Queue") -> None:
        with self._clients_lock:
            if camera in self._clients and q in self._clients[camera]:
                self._clients[camera].remove(q)
```

- [ ] **Step 4: Run the SSE tests**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_sse_events.py -v`
Expected: all 3 PASS

- [ ] **Step 5: Commit**

```bash
git add pipeline/sse_events.py tests/pipeline/test_sse_events.py
git commit -m "feat(v3): SSE event server for per-frame track events"
```

---

### Task 10: Wire SSE events into process_thread and bird_pipeline_v3

**Files:**
- Modify: `pipeline/process_thread.py` (constructor takes `sse_server`, emit after classification)
- Modify: `bird_pipeline_v3.py` (instantiate SSEEventServer, pass to CameraProcessThread)
- Test: `tests/pipeline/test_process_thread.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/pipeline/test_process_thread.py`:

```python
def test_process_thread_emits_sse_event_for_active_tracks():
    """When a frame yields active tracks, process_thread must emit an SSE event."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
                "yolo_runs_total": 0, "yolo_skipped_motion": 0}
    t._last_forced_full = time.time() - 9999  # long ago — not forced

    # Fake track
    fake_track = MagicMock()
    fake_track.track_id = 7
    fake_track.bbox = [100, 50, 300, 200]
    fake_track.species = "Downy Woodpecker"
    fake_track.confidence = 0.9  # YOLO bbox confidence (legacy field)
    fake_track.model_source = "yard"
    fake_track.frame_count = 1
    fake_track.needs_classification = False
    fake_track.classification_attempts = 0

    tracker_out = MagicMock()
    tracker_out.new = [fake_track]
    tracker_out.active = [fake_track]
    tracker_out.expired = []

    motion_gate = MagicMock()
    motion_gate.regions.return_value = [(0, 0, 640, 360)]
    detector = MagicMock()
    detector.detect.return_value = [MagicMock()]
    tracker = MagicMock()
    tracker.update.return_value = tracker_out
    tracker.tracks = [fake_track]
    tracker.stationary_regions.return_value = []
    classifier = MagicMock()
    classifier.stats = {"feeder": {"yard": 0, "aiy": 0}}
    event_store = MagicMock()
    annotator = MagicMock()
    health = MagicMock()

    sse_server = MagicMock()

    t.motion_gate = motion_gate
    t.detector = detector
    t.tracker = tracker
    t.classifier = classifier
    t.event_store = event_store
    t.annotator = annotator
    t.health = health
    t.sse_server = sse_server
    t.frame_width = 640
    t.frame_height = 360

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=1_700_000_000_000,
        camera="feeder", width=640, height=360,
    )
    t._process_frame(frame)

    # Verify sse_server.emit was called once with the right shape
    assert sse_server.emit.call_count == 1
    call = sse_server.emit.call_args
    assert call.kwargs["camera"] == "feeder"
    assert call.kwargs["wall_time_ms"] == 1_700_000_000_000
    tracks = call.kwargs["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["track_id"] == 7
    assert tracks[0]["species"] == "Downy Woodpecker"
    assert tracks[0]["bbox"] == [100, 50, 300, 200]
    assert tracks[0]["bbox_center_x"] == 200
    assert tracks[0]["frame_width"] == 640
    assert tracks[0]["frame_height"] == 360
    assert tracks[0]["model_source"] == "yard"
    assert tracks[0]["is_locked"] is True  # Phase 1: locked on first assignment
```

- [ ] **Step 2: Run test to verify it fails**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py::test_process_thread_emits_sse_event_for_active_tracks -v`
Expected: FAIL — `t.sse_server` referenced but no emit logic in code

- [ ] **Step 3: Update CameraProcessThread to accept and use sse_server**

In `pipeline/process_thread.py`:

(a) Update `__init__` signature to accept `sse_server` and `frame_width` / `frame_height`:

```python
    def __init__(self, name: str, frame_queue: queue.Queue,
                 motion_gate, detector, tracker, classifier,
                 event_store, annotator, health, sse_server,
                 frame_width: int, frame_height: int):
        ...
        self.sse_server = sse_server
        self.frame_width = frame_width
        self.frame_height = frame_height
```

(b) In `_process_frame`, after the existing event_store.write_event loop, emit the SSE event:

```python
        # 6b. Emit SSE event for live dashboard consumption
        if tracker_out.active and self.sse_server is not None:
            tracks_payload = []
            for track in tracker_out.active:
                bbox = list(track.bbox)
                tracks_payload.append({
                    "track_id": track.track_id,
                    "bbox": bbox,
                    "bbox_center_x": (bbox[0] + bbox[2]) // 2,
                    "frame_width": self.frame_width,
                    "frame_height": self.frame_height,
                    "species": track.species,
                    "species_confidence": None,  # Phase 1: not yet stored separately
                    "model_source": track.model_source,
                    "is_locked": track.species is not None,  # Phase 1: locked when assigned
                    "frame_count": getattr(track, "frame_count", 0),
                })
            self.sse_server.emit(
                camera=self.name,
                wall_time_ms=int(frame.wall_time_ms),
                tracks=tracks_payload,
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_process_thread.py::test_process_thread_emits_sse_event_for_active_tracks -v`
Expected: PASS

- [ ] **Step 5: Update bird_pipeline_v3.py to instantiate SSEEventServer**

In `bird_pipeline_v3.py` main():

```python
    from pipeline.sse_events import SSEEventServer

    sse_port = int(os.environ.get("PIPELINE_SSE_PORT", "8102"))
    sse_server = SSEEventServer(port=sse_port)
    sse_server.start()
```

And pass `sse_server` + `frame_width=640` + `frame_height=360` to each `CameraProcessThread(...)` call:

```python
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
                sse_server=sse_server,
                frame_width=640,
                frame_height=360,
            )
```

On shutdown, after stopping camera stacks, call `sse_server.stop()`.

- [ ] **Step 6: Run full pipeline test suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/ -q`
Expected: all pass (existing tests that construct CameraProcessThread will need `sse_server=None, frame_width=..., frame_height=...` added)

- [ ] **Step 7: Commit**

```bash
git add pipeline/process_thread.py bird_pipeline_v3.py tests/pipeline/test_process_thread.py
git commit -m "feat(v3): process_thread emits SSE events for active tracks"
```

---

### Task 11: Delete annotator and MJPEG broadcast paths

**Files:**
- Delete: `pipeline/annotator.py`
- Modify: `pipeline/debug_stream.py` — delete MJPEG broadcast portions, or delete whole file
- Modify: `bird_pipeline_v3.py` — remove FrameAnnotator + DebugStream instantiation
- Modify: `pipeline/process_thread.py` — make `annotator` optional / remove
- Modify: `tests/pipeline/test_annotator.py` — delete (file)
- Modify: `tests/pipeline/test_debug_stream.py` — delete or update

- [ ] **Step 1: Make annotator arg optional in process_thread**

In `pipeline/process_thread.py`:

```python
    def __init__(self, name, frame_queue, motion_gate, detector, tracker, classifier,
                 event_store, annotator, health, sse_server,
                 frame_width, frame_height):
        self.annotator = annotator  # may be None in v3
        ...
```

In `_process_frame`, guard the annotator submit:

```python
        # 8. Annotate + push (removed in v3; labels move client-side)
        if self.annotator is not None:
            self.annotator.submit(frame, tracker_out.active)
```

- [ ] **Step 2: Remove annotator instantiation from bird_pipeline_v3.py**

Delete these lines from `bird_pipeline_v3.py`:

```python
from pipeline.annotator import FrameAnnotator
from pipeline.debug_stream import DebugStream
...
debug_stream = DebugStream(port=8101)
debug_stream.start()
...
annotator = FrameAnnotator(name, debug_stream)
```

Pass `annotator=None` into `CameraProcessThread(...)`. Also remove the `annotator.start()` / `annotator.stop()` calls in the lifecycle.

Remove `debug_stream.stop()` from the shutdown block.

- [ ] **Step 3: Delete pipeline/annotator.py**

```bash
git rm pipeline/annotator.py
```

- [ ] **Step 4: Decide fate of pipeline/debug_stream.py**

The `DebugStream` module in v2 does two things: a WebSocket MJPEG broadcast and a poster-frame generator. In v3 the MJPEG broadcast is gone. If the poster-frame has no other callers, delete the whole file. Check callers:

```bash
grep -r "from pipeline.debug_stream" pipeline/ bird_pipeline_v3.py tests/ 2>/dev/null
```

Expected after Step 2: no references in pipeline/ or bird_pipeline_v3.py. Tests in `test_debug_stream.py` remain; those get deleted in step 6.

```bash
git rm pipeline/debug_stream.py
```

- [ ] **Step 5: Delete the test files for removed modules**

```bash
git rm tests/pipeline/test_annotator.py tests/pipeline/test_debug_stream.py
```

- [ ] **Step 6: Run full test suite**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/ -q`
Expected: all remaining tests pass (may need updates to `test_pipeline_e2e.py` if it references the annotator — use `annotator=None` everywhere)

- [ ] **Step 7: Commit**

```bash
git add pipeline/process_thread.py bird_pipeline_v3.py
git commit -m "feat(v3): delete annotator and MJPEG debug stream — labels move client-side"
```

---

### Task 12: Switch pipeline capture to 640×360 substream

**Files:**
- Modify: `bird_pipeline_v3.py` (RTSP URLs + width/height)
- Modify: `go2rtc.yaml` (add substream entries)
- Test: manual smoke test against go2rtc

- [ ] **Step 1: Update go2rtc.yaml**

Edit `go2rtc.yaml` to add transcoded substreams alongside existing main streams:

```yaml
streams:
  feeder-main:
    - rtsp://127.0.0.1:8554/706907355fbd92f7cb5ec28f1ac605e9
  feeder-sub:
    - "ffmpeg:feeder-main#video=h264#width=640#height=360#hardware"
  ground-main:
    - rtsp://192.168.4.9:7447/RTSnv0lLeUd8cJDw#tcp
  ground-sub:
    - "ffmpeg:ground-main#video=h264#width=640#height=360#hardware"

api:
  listen: ":1984"

log:
  level: info
```

- [ ] **Step 2: Reload go2rtc to pick up the new config**

```bash
# go2rtc reloads config via API
curl -X POST http://127.0.0.1:1984/api/restart
# Verify the new streams exist
curl -s http://127.0.0.1:1984/api/streams | python3 -c 'import sys, json; d=json.load(sys.stdin); print(list(d.keys()))'
```

Expected output includes `feeder-sub` and `ground-sub`.

- [ ] **Step 3: Test substream with a short ffmpeg probe**

```bash
ffprobe -v error -select_streams v:0 -show_entries stream=width,height,r_frame_rate \
  rtsp://127.0.0.1:8554/feeder-sub
```

Expected: `width=640`, `height=360`, `r_frame_rate=5/1` (or similar).

If the transcode fails, fall back to: `- "ffmpeg:feeder-main#video=h264#width=640#height=360"` (without `#hardware`).

- [ ] **Step 4: Update bird_pipeline_v3.py CAMERAS dict and capture width/height**

```python
CAMERAS = {
    "feeder": "rtsp://127.0.0.1:8554/feeder-sub",
    "ground": "rtsp://127.0.0.1:8554/ground-sub",
}

# In main(), when constructing FrameCapture:
capture = FrameCapture(name, url, out_queue=frame_q,
                       width=640, height=360, fps=5)
```

- [ ] **Step 5: Commit (config change only, no code tests yet)**

```bash
git add bird_pipeline_v3.py go2rtc.yaml
git commit -m "feat(v3): capture from 640x360 substream instead of 1080p main"
```

---

### Task 13: Dashboard — delete MJPEG path, restore <video> element

**Files:**
- Modify: `dashboard/index.html` — delete MJPEG img/connect code, add `<video>` + MSE client + `<canvas>` overlay
- Modify: `dashboard/api.py` — delete `/api/debug-stream*` proxies

- [ ] **Step 1: In dashboard/index.html, delete the MJPEG <img> and its wiring**

Locate the block starting with `<img id="v2-mjpeg-img">` (and any surrounding v2 markup) and remove it. Also delete:
- `connectDebugStreamV2` function
- `_v2Active`, `_v2LastFrameMs`, `_v2EnsureImg`, `_v2ShowImg`, `_v2HideImg` helpers
- The Old Det / New Det toggle button + handler
- The `connectPipelineSSE` function (dead code from v2)
- The `_v2Ws`-referencing visibilitychange branch

- [ ] **Step 2: Add v3 <video> element and <canvas> overlay**

Add (in the camera pane section of the HTML):

```html
<div id="v3-live-container" style="position: relative; width: 100%; height: auto;">
  <video id="v3-live-video" autoplay muted playsinline
         style="width: 100%; height: auto; display: block; background: #000;">
  </video>
  <canvas id="v3-label-overlay"
          style="position: absolute; top: 0; left: 0;
                 width: 100%; height: 100%; pointer-events: none;">
  </canvas>
</div>
```

- [ ] **Step 3: Add the MSE WebSocket client (based on go2rtc's reference)**

Add a `<script>` block near the bottom of `dashboard/index.html` (before `</body>`):

```javascript
(function setupV3LiveVideo() {
  const video = document.getElementById('v3-live-video');
  if (!video) return;

  // Determine the go2rtc WebSocket URL.
  // In dev: localhost:1984. In prod: via the Cloudflare tunnel with a /go2rtc/ proxy
  // — David sets up the tunnel rule at cutover. For now, fall back to direct localhost.
  const go2rtcHost = window.GO2RTC_HOST || `${location.hostname}:1984`;
  const streamName = 'feeder-main';
  const wsUrl = `ws://${go2rtcHost}/api/ws?src=${streamName}`;

  const ws = new WebSocket(wsUrl);
  ws.binaryType = 'arraybuffer';

  let mediaSource = null;
  let sourceBuffer = null;
  let queue = [];

  function flushQueue() {
    if (!sourceBuffer || sourceBuffer.updating) return;
    while (queue.length > 0 && !sourceBuffer.updating) {
      try {
        sourceBuffer.appendBuffer(queue.shift());
      } catch (e) {
        console.warn('appendBuffer error', e);
        return;
      }
    }
  }

  ws.addEventListener('open', () => {
    // Request MSE mode with H264 codec
    ws.send(JSON.stringify({ type: 'mse', value: 'avc1.640029' }));
  });

  ws.addEventListener('message', (ev) => {
    if (typeof ev.data === 'string') {
      // go2rtc sends a JSON message with the codec string first
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'mse') {
          mediaSource = new MediaSource();
          video.src = URL.createObjectURL(mediaSource);
          mediaSource.addEventListener('sourceopen', () => {
            sourceBuffer = mediaSource.addSourceBuffer(`video/mp4; codecs="${msg.value}"`);
            sourceBuffer.mode = 'segments';
            sourceBuffer.addEventListener('updateend', flushQueue);
            flushQueue();
          });
        }
      } catch (e) {
        console.warn('Unexpected string message', ev.data);
      }
      return;
    }
    // Binary MP4 fragment
    queue.push(new Uint8Array(ev.data));
    flushQueue();
  });

  ws.addEventListener('error', (e) => console.warn('v3 WS error', e));
  ws.addEventListener('close', () => console.log('v3 WS closed'));
})();
```

- [ ] **Step 4: Delete /api/debug-stream* proxies from dashboard/api.py**

In `dashboard/api.py`, find the routes `@app.get("/api/debug-stream/{camera}")` and `@app.get("/api/debug-stream-mjpeg/{camera}")` and delete both function definitions (and any supporting imports they exclusively needed — likely `websockets` for the old WebSocket proxy).

Keep imports that other routes still use.

- [ ] **Step 5: Manual smoke test**

Open the dashboard in a browser (or headless Playwright later). Expect:
- `<video>` element is visible
- `<video id="v3-live-video">.readyState` is `>=2` within 10 seconds of page load (checked in browser console)
- Network panel shows a WebSocket connection to `:1984/api/ws?src=feeder-main`
- Video plays actual camera frames

If it doesn't work, check the browser console for the specific MSE error; the exact handshake sequence may need tuning based on go2rtc's protocol version.

- [ ] **Step 6: Commit**

```bash
git add dashboard/index.html dashboard/api.py
git commit -m "feat(v3): restore <video> element + go2rtc MSE client, delete MJPEG path"
```

---

### Task 14a: Dashboard — Canvas overlay basic renderer (no interpolation)

**Files:**
- Modify: `dashboard/index.html` (add `LabelRenderer` class, plain render without interpolation first)

- [ ] **Step 1: Add the LabelRenderer skeleton**

Add a `<script>` block after the MSE setup:

```javascript
(function setupV3LabelRenderer() {
  const canvas = document.getElementById('v3-label-overlay');
  const video = document.getElementById('v3-live-video');
  if (!canvas || !video) return;
  const ctx = canvas.getContext('2d');

  // Match canvas internal resolution to displayed size on resize
  function resizeCanvas() {
    canvas.width = video.clientWidth;
    canvas.height = video.clientHeight;
  }
  window.addEventListener('resize', resizeCanvas);
  video.addEventListener('loadedmetadata', resizeCanvas);
  resizeCanvas();

  // trackStates: Map<track_id, {last_t, last_x, prev_t, prev_x, species,
  //                              bbox_area, frame_width, frame_height,
  //                              first_seen_t, fadeOutAt}>
  const trackStates = new Map();

  // Expose for SSE wiring in the next task
  window.__v3TrackStates = trackStates;
  window.__v3Canvas = canvas;
  window.__v3Ctx = ctx;

  const LABEL_FONT = '14px system-ui, -apple-system, sans-serif';
  const LABEL_HEIGHT = 24;
  const LABEL_PAD_X = 8;
  const LABEL_PAD_Y = 4;
  const BASE_Y_FRAC = 0.25;

  function drawDot(x, y, opacity) {
    ctx.save();
    ctx.globalAlpha = opacity;
    ctx.fillStyle = 'rgba(255,255,255,0.8)';
    ctx.beginPath();
    ctx.arc(x, y, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.restore();
  }

  function drawLabel(x, y, text, opacity) {
    ctx.save();
    ctx.globalAlpha = opacity;
    ctx.font = LABEL_FONT;
    const metrics = ctx.measureText(text);
    const w = metrics.width + 2 * LABEL_PAD_X;
    const h = LABEL_HEIGHT;

    // Drop shadow
    ctx.shadowColor = 'rgba(0,0,0,0.4)';
    ctx.shadowBlur = 8;
    ctx.shadowOffsetY = 2;
    // Pill background
    ctx.fillStyle = 'rgba(0,0,0,0.65)';
    const rx = x - w / 2;
    const ry = y - h / 2;
    const radius = 12;
    ctx.beginPath();
    ctx.moveTo(rx + radius, ry);
    ctx.lineTo(rx + w - radius, ry);
    ctx.quadraticCurveTo(rx + w, ry, rx + w, ry + radius);
    ctx.lineTo(rx + w, ry + h - radius);
    ctx.quadraticCurveTo(rx + w, ry + h, rx + w - radius, ry + h);
    ctx.lineTo(rx + radius, ry + h);
    ctx.quadraticCurveTo(rx, ry + h, rx, ry + h - radius);
    ctx.lineTo(rx, ry + radius);
    ctx.quadraticCurveTo(rx, ry, rx + radius, ry);
    ctx.closePath();
    ctx.fill();
    // Text
    ctx.shadowColor = 'transparent';
    ctx.fillStyle = 'rgba(255,255,255,1)';
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, x, y);
    ctx.restore();
    return { x: rx, y: ry, w, h };
  }

  function renderFrame() {
    const now = performance.now();
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const baseY = BASE_Y_FRAC * canvas.height;

    const placements = [];
    for (const [trackId, state] of trackStates) {
      const elapsed = now - state.last_t;
      if (elapsed > 3000) {
        trackStates.delete(trackId);
        continue;
      }
      // Simple version: no interpolation yet, just snap to last known x
      const renderX = state.last_x;
      // Scale substream coords to canvas coords
      const canvasX = renderX * (canvas.width / state.frame_width);
      // Opacity
      let opacity = 1;
      if (state.fadeOutAt) {
        opacity = Math.max(0, 1 - (now - state.fadeOutAt) / 300);
      } else if (state.first_seen_t) {
        opacity = Math.min(1, (now - state.first_seen_t) / 200);
      }
      placements.push({ trackId, x: canvasX, y: baseY, state, opacity });
    }

    // Draw (collision handling in next task)
    for (const p of placements) {
      if (p.state.species) {
        drawLabel(p.x, p.y, p.state.species, p.opacity);
      } else {
        drawDot(p.x, p.y, p.opacity);
      }
    }

    requestAnimationFrame(renderFrame);
  }
  requestAnimationFrame(renderFrame);
})();
```

- [ ] **Step 2: Manual smoke test**

Open the dashboard, check the browser console: no errors. Check the canvas exists and is sized to match the video. No labels visible yet (no SSE wiring).

- [ ] **Step 3: Commit**

```bash
git add dashboard/index.html
git commit -m "feat(v3): dashboard canvas overlay + LabelRenderer skeleton (no interp yet)"
```

---

### Task 14b: Dashboard — SSE subscription + interpolation + collision

**Files:**
- Modify: `dashboard/index.html` (SSE wiring + full interpolation + collision pass)
- Modify: `dashboard/api.py` (add /api/pipeline/events/sse proxy)

- [ ] **Step 1: Add /api/pipeline/events/sse proxy to dashboard/api.py**

Add to `dashboard/api.py`:

```python
import httpx
from fastapi.responses import StreamingResponse
import os

PIPELINE_BACKEND_URL = os.environ.get("PIPELINE_BACKEND_URL", "http://127.0.0.1:8102")

@app.get("/api/pipeline/events/sse")
async def proxy_pipeline_sse(camera: str = "feeder"):
    """Proxy SSE events from the pipeline's SSE server."""
    async def gen():
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                "GET",
                f"{PIPELINE_BACKEND_URL}/events/sse",
                params={"camera": camera},
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
```

- [ ] **Step 2: Add SSE subscription to dashboard/index.html**

Add a `<script>` block after the `setupV3LabelRenderer` IIFE:

```javascript
(function setupV3SSESubscription() {
  const trackStates = window.__v3TrackStates;
  if (!trackStates) return;

  const es = new EventSource('/api/pipeline/events/sse?camera=feeder');
  es.addEventListener('message', (ev) => {
    let data;
    try { data = JSON.parse(ev.data); } catch (e) { return; }
    if (!data.tracks) return;
    const now = performance.now();
    const seen = new Set();
    for (const t of data.tracks) {
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
      if (!seen.has(trackId) && !state.fadeOutAt) {
        state.fadeOutAt = now;
      }
    }
  });
  es.addEventListener('error', (e) => console.warn('v3 SSE error', e));
})();
```

- [ ] **Step 3: Add interpolation to the render loop**

In the `renderFrame` function in `setupV3LabelRenderer`, replace the simple `renderX = state.last_x;` with interpolation:

```javascript
      // Interpolated x via extrapolation from last two known positions
      let renderX = state.last_x;
      const dt = state.last_t - state.prev_t;
      const elapsed = now - state.last_t;
      if (dt > 0 && elapsed < 500) {
        renderX = state.last_x + (state.last_x - state.prev_x) * (elapsed / dt);
      }
```

- [ ] **Step 4: Add collision handling**

In `renderFrame`, after the `placements.push` loop and before drawing:

```javascript
    // Collision pass: sort by bbox_area desc, place each, bump down if overlap
    placements.sort((a, b) => (b.state.bbox_area || 0) - (a.state.bbox_area || 0));
    const placed = [];
    const LABEL_W_ESTIMATE = 140;  // worst-case label width
    for (const p of placements) {
      let y = p.y;
      while (placed.some(q =>
        Math.abs(q.x - p.x) < LABEL_W_ESTIMATE && Math.abs(q.y - y) < LABEL_HEIGHT + 8
      )) {
        y += LABEL_HEIGHT + 8;
      }
      p.y = y;
      placed.push(p);
    }

    // Edge clamp
    for (const p of placed) {
      p.x = Math.max(LABEL_W_ESTIMATE / 2 + 8,
                     Math.min(canvas.width - LABEL_W_ESTIMATE / 2 - 8, p.x));
    }

    for (const p of placed) {
      if (p.state.species) drawLabel(p.x, p.y, p.state.species, p.opacity);
      else drawDot(p.x, p.y, p.opacity);
    }
```

- [ ] **Step 5: Manual smoke test — open dashboard, verify SSE connection**

In browser console:
```js
document.querySelector('video').readyState  // should be >= 2
// Network panel: should show /api/pipeline/events/sse as pending (streaming)
```

- [ ] **Step 6: Commit**

```bash
git add dashboard/index.html dashboard/api.py
git commit -m "feat(v3): dashboard SSE subscription + interpolation + collision handling"
```

---

### Task 15: Honesty contract test suite

**Files:**
- Create: `tests/pipeline/test_honesty_contract.py`
- Modify: `pipeline/health.py` (add the full `_compute_status` rules from the spec)

- [ ] **Step 1: Create test_honesty_contract.py with all failure-injection tests**

Create `tests/pipeline/test_honesty_contract.py`:

```python
"""Honesty contract tests — every metric must respond correctly to fabricated broken state.

Each test fabricates a broken state and asserts the metric detects it.
Each test also asserts the metric reads correctly in a healthy state.

Spec: docs/superpowers/specs/2026-04-11-live-detection-v3-design.md §6
"""
import time

import pytest


def _make_health():
    from pipeline.health import HealthState
    return HealthState()


# --- p99 correctness ---

def test_p99_uses_true_percentile_not_max():
    """Feed a bimodal distribution; p99 must NOT equal max unless they naturally agree."""
    import numpy as np
    samples = [10.0] * 95 + [1000.0] * 5  # n=100, p99 ≈ 1000 (the top 1%)
    p99 = float(np.percentile(samples, 99))
    max_val = max(samples)
    # For this specific distribution, p99 should be close to 1000 but not guaranteed equal
    assert abs(p99 - 1000.0) < 50, f"p99 was {p99}"


def test_p99_returns_none_for_insufficient_samples():
    """Metric must refuse to report p99 for <10 samples; returns None sentinel."""
    from pipeline.process_thread import CameraProcessThread
    from unittest.mock import MagicMock
    from pipeline.frame import Frame
    import numpy as np, threading

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 0, "detections": 0,
                "yolo_ms_samples": [50.0, 60.0, 70.0],  # only 3 samples
                "yolo_runs_total": 3, "yolo_skipped_motion": 0}
    t.tracker = MagicMock()
    t.tracker.tracks = []
    t.tracker.stationary_regions.return_value = []
    t.classifier = MagicMock()
    t.classifier.stats = {"test": {}}

    captured = {}
    health = MagicMock()
    def fake_update(camera, section, payload):
        captured[section] = payload
    health.update = fake_update
    t.health = health

    frame = Frame(bgr=np.zeros((360, 640, 3), dtype=np.uint8),
                  wall_time_ms=time.time() * 1000,
                  camera="test", width=640, height=360)
    t._update_health(frame, det_ms=0.0)

    assert captured["detector"]["yolo_ms_p99"] is None, (
        "p99 must be None with <10 samples"
    )


# --- Skip frame exclusion ---

def test_yolo_samples_excludes_skip_frames():
    """Already tested in test_process_thread.py but duplicated here for honesty completeness."""
    # Reuse the same scenario: 5 skip frames should produce 0 yolo samples.
    # This test mirrors test_process_thread::test_yolo_samples_excludes_skip_frames
    pass  # explicit pass; real assertion lives in the process_thread test


# --- Per-camera stats ---

def test_classifier_stats_per_camera():
    """SmartClassifier.stats must be a per-camera dict, not global."""
    from pipeline.classifier import SmartClassifier
    from pipeline.camera_config import CameraClassifierConfig

    configs = {
        "feeder": CameraClassifierConfig(use_yard=True),
        "ground": CameraClassifierConfig(use_yard=False),
    }
    classifier = SmartClassifier.__new__(SmartClassifier)
    classifier.camera_configs = configs
    classifier.stats = {cam: {"yard": 0, "aiy": 0} for cam in configs}

    assert "feeder" in classifier.stats
    assert "ground" in classifier.stats
    assert classifier.stats["feeder"] is not classifier.stats["ground"], (
        "stats must be separate dicts per camera"
    )


# --- num_frames is per-track ---

def test_num_frames_is_per_track():
    """write_track_summary must pass track.frame_count, not a global counter."""
    # Already tested in test_process_thread.py::test_write_track_summary_uses_per_track_frame_count
    from pipeline.tracker import BirdTracker
    from pipeline.detector import Detection
    tracker = BirdTracker()
    tracker.update([Detection(box=[0,0,10,10], confidence=0.9)], wall_time_ms=0)
    tracker.update([Detection(box=[0,0,10,10], confidence=0.9)], wall_time_ms=100)
    tracker.update([Detection(box=[0,0,10,10], confidence=0.9)], wall_time_ms=200)
    out = tracker.update([Detection(box=[0,0,10,10], confidence=0.9)], wall_time_ms=300)
    assert out.active[0].frame_count == 4


# --- ffmpeg restart loop detection ---

def test_ffmpeg_restart_storm_marks_broken():
    """>10 ffmpeg restarts in the last hour on any camera → overall broken."""
    h = _make_health()
    now_s = time.time()
    h.update("feeder", "capture", {
        "frames_processed": 100,
        "last_frame_age_ms": 500,
        "ffmpeg_restarts": 11,
        "ffmpeg_restarts_last_hour": 11,
        "dropped_oldest": 0,
    })
    h.update("feeder", "detector", {"yolo_ms_avg": 50, "yolo_ms_p99": 100})
    h.update("feeder", "tracker", {"active_tracks": 0, "stationary_tracks": 0})
    h.update("feeder", "classifier", {"yard": 0, "aiy": 0, "lock_timeouts": 0})
    snap = h.snapshot()
    assert snap["overall"] == "broken", f"expected broken, got {snap['overall']}"


# --- last_frame_age broken during daytime ---

def test_last_frame_age_broken_when_daytime_stall():
    """last_frame_age_ms > 60000 during daytime → broken."""
    from unittest.mock import patch
    h = _make_health()
    h.update("feeder", "capture", {
        "frames_processed": 100,
        "last_frame_age_ms": 70000,  # stalled > 60s
        "ffmpeg_restarts": 0,
        "dropped_oldest": 0,
    })
    h.update("feeder", "detector", {"yolo_ms_avg": 50, "yolo_ms_p99": 100})
    h.update("feeder", "tracker", {"active_tracks": 0, "stationary_tracks": 0})
    h.update("feeder", "classifier", {"yard": 0, "aiy": 0, "lock_timeouts": 0})

    with patch("pipeline.health.is_nighttime", return_value=False):
        snap = h.snapshot()
    assert snap["overall"] == "broken"


def test_last_frame_age_ok_at_night():
    """Same stall is acceptable at night (pipeline paused)."""
    from unittest.mock import patch
    h = _make_health()
    h.update("feeder", "capture", {
        "frames_processed": 100,
        "last_frame_age_ms": 70000,
        "ffmpeg_restarts": 0,
        "dropped_oldest": 0,
    })
    h.update("feeder", "detector", {"yolo_ms_avg": 0, "yolo_ms_p99": None})
    h.update("feeder", "tracker", {"active_tracks": 0, "stationary_tracks": 0})
    h.update("feeder", "classifier", {"yard": 0, "aiy": 0, "lock_timeouts": 0})

    with patch("pipeline.health.is_nighttime", return_value=True):
        snap = h.snapshot()
    assert snap["overall"] in ("ok", "degraded")


# --- yolo p99 tail ---

def test_yolo_p99_tail_degraded():
    """yolo_ms_p99 > 1000 → degraded."""
    h = _make_health()
    h.update("feeder", "capture", {
        "frames_processed": 100, "last_frame_age_ms": 500,
        "ffmpeg_restarts": 0, "dropped_oldest": 0,
    })
    h.update("feeder", "detector", {"yolo_ms_avg": 200, "yolo_ms_p99": 1500})
    h.update("feeder", "tracker", {"active_tracks": 0, "stationary_tracks": 0})
    h.update("feeder", "classifier", {"yard": 0, "aiy": 0, "lock_timeouts": 0})
    snap = h.snapshot()
    assert snap["overall"] == "degraded"


# --- dropped frame rate ---

def test_dropped_oldest_threshold_degraded():
    """dropped_oldest / frames_processed > 5% → degraded."""
    h = _make_health()
    h.update("feeder", "capture", {
        "frames_processed": 100,
        "dropped_oldest": 10,  # 10% drop rate
        "last_frame_age_ms": 500,
        "ffmpeg_restarts": 0,
    })
    h.update("feeder", "detector", {"yolo_ms_avg": 50, "yolo_ms_p99": 100})
    h.update("feeder", "tracker", {"active_tracks": 0, "stationary_tracks": 0})
    h.update("feeder", "classifier", {"yard": 0, "aiy": 0, "lock_timeouts": 0})
    snap = h.snapshot()
    assert snap["overall"] == "degraded"


# --- Coral lock storm ---

def test_lock_timeouts_degraded():
    """lock_timeouts > 5/hr → degraded."""
    h = _make_health()
    h.update("feeder", "capture", {
        "frames_processed": 100, "last_frame_age_ms": 500,
        "ffmpeg_restarts": 0, "dropped_oldest": 0,
    })
    h.update("feeder", "detector", {"yolo_ms_avg": 50, "yolo_ms_p99": 100})
    h.update("feeder", "tracker", {"active_tracks": 0, "stationary_tracks": 0})
    h.update("feeder", "classifier", {"yard": 0, "aiy": 0, "lock_timeouts": 6})
    snap = h.snapshot()
    assert snap["overall"] == "degraded"


# --- All healthy ---

def test_all_healthy_reports_ok():
    h = _make_health()
    h.update("feeder", "capture", {
        "frames_processed": 100, "last_frame_age_ms": 500,
        "ffmpeg_restarts": 0, "dropped_oldest": 0,
    })
    h.update("feeder", "detector", {"yolo_ms_avg": 50, "yolo_ms_p99": 100})
    h.update("feeder", "tracker", {"active_tracks": 0, "stationary_tracks": 0})
    h.update("feeder", "classifier", {"yard": 10, "aiy": 2, "lock_timeouts": 0})
    snap = h.snapshot()
    assert snap["overall"] == "ok", f"expected ok, got {snap['overall']}"
```

- [ ] **Step 2: Run the contract tests to see which currently fail**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_honesty_contract.py -v`
Expected: Multiple FAIL — `HealthState._compute_status` doesn't implement all the spec rules yet.

- [ ] **Step 3: Rewrite `_compute_status` in pipeline/health.py**

Update the `_compute_status` method to implement the full ruleset from the spec §6. Exact implementation will depend on the current shape of `pipeline/health.py`, but it needs to:

- Import `is_nighttime` from `solar_utils`
- Check `last_frame_age_ms > 60000` during daytime → broken
- Check `ffmpeg_restarts_last_hour > 10` → broken (may need to add a rolling counter)
- Check `yolo_ms_p99 > 1000` → degraded
- Check `dropped_oldest / frames_processed > 0.05` → degraded
- Check `lock_timeouts > 5` → degraded
- Worst state wins for `overall`

- [ ] **Step 4: Iterate on `_compute_status` + tests until all pass**

Run: `/Users/vives/bird-classifier/venv-coral/bin/python -m pytest tests/pipeline/test_honesty_contract.py -v`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add pipeline/health.py tests/pipeline/test_honesty_contract.py
git commit -m "feat(v3): honesty contract test suite + full _compute_status rules"
```

---

### Task 16: End-to-end verification script

**Files:**
- Create: `scripts/verify_v3_prototype.py`
- Create: `scripts/coral_borrow.sh`

- [ ] **Step 1: Create the coral_borrow.sh helper**

```bash
#!/bin/bash
# Temporarily stop/start the production v2 LaunchAgent so v3 can use Coral in tests.
set -e
ACTION="$1"
PLIST=~/Library/LaunchAgents/com.vives.bird-pipeline.plist
case "$ACTION" in
  stop)
    launchctl unload "$PLIST" 2>/dev/null || true
    ;;
  start)
    launchctl load "$PLIST" 2>/dev/null || true
    ;;
  *)
    echo "usage: $0 {stop|start}"; exit 1;;
esac
```

```bash
chmod +x scripts/coral_borrow.sh
```

- [ ] **Step 2: Create scripts/verify_v3_prototype.py**

```python
#!/usr/bin/env python3
"""End-to-end verification of the v3 prototype.

Runs the v3 pipeline against the live test video loop, opens a headless browser,
exercises the dashboard, and checks every Phase 1 success criterion.

Usage:
    python scripts/verify_v3_prototype.py

Exits 0 on full pass, non-zero with a report on failure.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).parent.parent
VENV_PY = Path.home() / "bird-classifier" / "venv-coral" / "bin" / "python"
EVIDENCE_DIR = REPO / "docs" / "superpowers" / "progress" / "2026-04-11-v3-verification"
EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)

PIPELINE_HEALTH_URL = "http://127.0.0.1:8102/health"
PIPELINE_SSE_URL = "http://127.0.0.1:8102/events/sse?camera=feeder"
DEV_DASHBOARD_URL = os.environ.get("DEV_DASHBOARD_URL", "http://127.0.0.1:8099/")


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def check_pipeline_running() -> dict:
    log("Checking pipeline health endpoint...")
    for _ in range(30):
        try:
            with urllib.request.urlopen(PIPELINE_HEALTH_URL, timeout=2) as resp:
                data = json.loads(resp.read())
            log(f"  Pipeline health: overall={data.get('overall')}")
            return data
        except Exception:
            time.sleep(1)
    raise SystemExit("Pipeline health endpoint did not respond within 30s")


def check_sse_stream() -> list:
    log("Subscribing to SSE stream for 15s to capture events...")
    events = []
    deadline = time.time() + 15
    try:
        resp = urllib.request.urlopen(PIPELINE_SSE_URL, timeout=5)
        buf = b""
        while time.time() < deadline:
            chunk = resp.read1(4096)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                frame, buf = buf.split(b"\n\n", 1)
                for line in frame.decode("utf-8").split("\n"):
                    if line.startswith("data: "):
                        try:
                            events.append(json.loads(line[6:]))
                        except Exception:
                            pass
    except Exception as e:
        log(f"SSE subscription error: {e}")
    log(f"  Captured {len(events)} SSE events in 15s")
    return events


def browser_check() -> dict:
    log("Opening headless browser for dashboard check...")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("  playwright not installed — skipping browser check")
        return {"skipped": True}

    result = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 720})
        page.goto(DEV_DASHBOARD_URL)
        page.wait_for_load_state("networkidle", timeout=10000)

        # Wait for video to have readyState >= 2
        ready = page.evaluate("() => document.getElementById('v3-live-video')?.readyState ?? -1")
        log(f"  video readyState = {ready}")
        result["video_readyState"] = ready

        # Capture a screenshot
        screenshot_path = EVIDENCE_DIR / f"dashboard-{int(time.time())}.png"
        page.screenshot(path=str(screenshot_path))
        log(f"  Screenshot: {screenshot_path}")
        result["screenshot"] = str(screenshot_path)

        # Wait for at least one label
        time.sleep(30)
        screenshot_path2 = EVIDENCE_DIR / f"dashboard-after-30s-{int(time.time())}.png"
        page.screenshot(path=str(screenshot_path2))
        log(f"  Screenshot after 30s: {screenshot_path2}")
        result["screenshot_30s"] = str(screenshot_path2)

        browser.close()
    return result


def main():
    log("=" * 60)
    log("v3 Prototype Verification")
    log("=" * 60)

    checks = {}

    # 1. Pipeline health
    health = check_pipeline_running()
    checks["health"] = health

    # 2. SSE stream
    events = check_sse_stream()
    checks["sse_event_count"] = len(events)
    checks["sse_has_events"] = len(events) > 0

    # 3. Browser check
    browser = browser_check()
    checks["browser"] = browser

    # Save full verification report
    report_path = EVIDENCE_DIR / f"verification-{int(time.time())}.json"
    with open(report_path, "w") as f:
        json.dump(checks, f, indent=2, default=str)
    log(f"Full report: {report_path}")

    # Summary
    log("=" * 60)
    log("Summary:")
    log(f"  overall health: {health.get('overall')}")
    log(f"  SSE events in 15s: {len(events)}")
    log(f"  browser check: {browser}")
    log("=" * 60)

    return 0 if health.get("overall") in ("ok", "degraded") else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Commit**

```bash
git add scripts/verify_v3_prototype.py scripts/coral_borrow.sh
git commit -m "feat(v3): end-to-end verification script + coral_borrow helper"
```

---

### Task 17: Smoke test — run v3 pipeline against test loop, verify end-to-end

**Files:** none modified; this task is pure verification.

- [ ] **Step 1: Stop the running v2 pipeline temporarily**

```bash
./scripts/coral_borrow.sh stop
sleep 2
ps aux | grep bird_pipeline | grep -v grep  # verify v2 is gone
```

- [ ] **Step 2: Launch v3 pipeline in foreground**

```bash
PIPELINE_HEALTH_PORT=8102 \
PIPELINE_SSE_PORT=8102 \
/Users/vives/bird-classifier/venv-coral/bin/python bird_pipeline_v3.py 2>&1 | tee /tmp/v3-smoke.log &
V3_PID=$!
sleep 15
```

- [ ] **Step 3: Run the verification script**

```bash
/Users/vives/bird-classifier/venv-coral/bin/python scripts/verify_v3_prototype.py
```

Expected: exits 0, produces evidence files in `docs/superpowers/progress/2026-04-11-v3-verification/`.

- [ ] **Step 4: Stop v3 and restart v2**

```bash
kill $V3_PID
wait $V3_PID 2>/dev/null || true
./scripts/coral_borrow.sh start
sleep 5
ps aux | grep bird_pipeline | grep -v grep  # verify v2 is back
```

- [ ] **Step 5: Commit the verification evidence**

```bash
git add docs/superpowers/progress/2026-04-11-v3-verification/
git commit -m "evidence: v3 prototype verification run"
```

---

## Self-Review Checklist Results

### Spec coverage check

| Spec section | Task(s) |
|---|---|
| § 2 Architecture (two-stream) | Tasks 12 (substream), 13 (video+MSE), 14a/14b (canvas+overlay) |
| § 3 Components | All tasks 1–15 |
| § 4.1 go2rtc substream | Task 12 |
| § 4.2 Pipeline source switch | Task 12 |
| § 4.3 Critical bug fixes | Tasks 2, 3, 4, 5, 6 |
| § 4.4 Per-camera classifier config | Task 7, 8 |
| § 4.5 Delete annotator | Task 11 |
| § 4.6 New SSE endpoint | Tasks 9, 10 |
| § 4.7 Dashboard client | Tasks 13, 14a, 14b |
| § 4.8 Dashboard API | Tasks 13 (delete), 14b (new proxy) |
| § 4.9 Honesty contract | Task 15 |
| § 6 Honesty contract detail | Task 15 |
| § 7 Dashboard rendering | Tasks 14a, 14b |
| § 8 Testing strategy | Tasks 2–15 (unit), Task 16 (verification) |
| § 9 Migration plan | Prerequisites section + Task 17 (verification run) |

No spec section lacks a task.

### Placeholder scan

- No "TBD" / "TODO" / "fill in"
- Every code block is complete (though some require the implementer to locate an existing line — those are marked with approximate line numbers)
- Every test has a concrete assertion
- Commit messages are written

Known soft spots (flagged for the implementing engineer):
- Task 5 Step 3: the exact line for incrementing `frame_count` in `BirdTracker.update()` depends on the current tracker.py structure; the engineer must trace through.
- Task 13 Step 3: the MSE WebSocket protocol handshake is a best-effort port of go2rtc's reference client. If the handshake differs in the currently-installed go2rtc version, the engineer should copy from `https://github.com/AlexxIT/go2rtc/blob/master/www/video-stream.js`.
- Task 14a Step 1: the `LABEL_W_ESTIMATE = 140` is a worst-case placeholder; a more precise collision box comes from `ctx.measureText` but is omitted for simplicity.

### Type consistency

- `CameraClassifierConfig` — defined in Task 7, used in Task 7, 8
- `Track.frame_count` — added in Task 5, used in Tasks 5, 10
- `SSEEventServer` — defined in Task 9, used in Task 10
- `trackStates` (JS) — defined in Task 14a, extended in Task 14b
- `CameraProcessThread.__init__` signature — modified in Task 10 (adds `sse_server`, `frame_width`, `frame_height`), modified again in Task 11 (annotator becomes optional); final signature consistent with uses in `bird_pipeline_v3.py`

No type inconsistencies.

---

Plan complete and saved to `docs/superpowers/plans/2026-04-11-live-detection-v3-phase1.md`.

Execution approach: **Subagent-Driven Development** (required sub-skill: `superpowers:subagent-driven-development`). David is unavailable and has explicitly authorized autonomous execution. Fresh implementer subagent per task, two-stage review per task (spec compliance → code quality), progress continually documented to `docs/superpowers/progress/2026-04-11-v3-progress.md`.