# Live Label Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make live labels measurable first, then PTS/video-clock aligned, then predictive enough to stay on moving birds.

**Architecture:** Keep WebRTC video and DOM labels as the live path. Add a browser-side clock bridge that observes video frame timing and incoming event PTS, buffers label events briefly, and renders the event state that corresponds to the frame being displayed. Use the HLS/PTS path for replay and snapshot truth, not as the live video transport.

**Tech Stack:** Python pipeline events over SSE/WebSocket, `dashboard/pi_dash.html` DOM overlay, browser `requestVideoFrameCallback`, existing Pi pytest environment, Playwright/browser probes for visual checks.

---

## File Map

- `dashboard/pi_dash.html`: live overlay rendering, diagnostics, clock bridge, label buffering, prediction.
- `tests/test_dashboard_sync_diagnostics.py`: static regression tests for dashboard sync instrumentation.
- `tests/test_dashboard_live_video_proxy.py`: existing video proxy/static dashboard tests; extend only if the video init path changes.
- `pipeline/sse_events.py`: event payload schema if server-side sync metadata is added.
- `pipeline/process_thread.py`: event payload source if track velocity or event sequence fields are added.
- `tools/sync_replay_assert.py`: replay/frame-accuracy harness for demo/annotation checks.
- `docs/working/progress/2026-05-12-codex-live-log.md`: running audit trail.

---

### Task 1: Add Browser Sync Telemetry

**Files:**
- Modify: `dashboard/pi_dash.html`
- Create: `tests/test_dashboard_sync_diagnostics.py`
- Modify: `docs/working/progress/2026-05-12-codex-live-log.md`

- [x] **Step 1: Write the failing test**

Create `tests/test_dashboard_sync_diagnostics.py`:

```python
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def test_sync_diag_exposes_video_frame_clock():
    html = (ROOT / "dashboard" / "pi_dash.html").read_text()

    assert "requestVideoFrameCallback" in html
    assert "videoFrameHz" in html
    assert "lastVideoMediaTime" in html
    assert "clockDeltaMs" in html
    assert "eventAgeMsRough" in html
    assert "get sync" in html
```

- [x] **Step 2: Run test to verify it fails**

Run on the Pi runtime:

```bash
ssh vives@pi5.local "cd /home/vives/bird-classifier && ./venv/bin/python -m pytest tests/test_dashboard_sync_diagnostics.py -q"
```

Expected: failure because the dashboard does not yet expose the new sync metrics.

- [x] **Step 3: Add diagnostic-only telemetry**

In `dashboard/pi_dash.html`, inside `setupLiveView()`:

```javascript
const syncStats = {
  lastVideoMediaTime: null,
  lastVideoCallbackAt: 0,
  lastVideoFrames: 0,
  videoFrameHz: 0,
  lastEventPts: null,
  lastEventWallTimeMs: null,
  lastEventArrivedAt: 0,
  clockDeltaMs: null,
  eventAgeMsRough: null,
};
```

Add a `requestVideoFrameCallback` loop against the inner `<video>` created by `<video-stream>`. Update `lastVideoMediaTime`, `videoFrameHz`, and `lastVideoCallbackAt`. In `handleTrackEvent()`, update `lastEventPts`, `lastEventWallTimeMs`, `lastEventArrivedAt`, `eventAgeMsRough`, and `clockDeltaMs = (evt.pts - lastVideoMediaTime) * 1000` when both clocks exist.

Expose these through:

```javascript
window.__overlayDebug = {
  // existing fields...
  get sync() { return { ...syncStats }; },
};
```

Add the same values to the `?syncdiag=1` chip without changing rendering behavior.

- [x] **Step 4: Run test to verify it passes**

Run:

```bash
ssh vives@pi5.local "cd /home/vives/bird-classifier && ./venv/bin/python -m pytest tests/test_dashboard_sync_diagnostics.py tests/test_dashboard_live_video_proxy.py -q"
```

Expected: all tests pass.

- [x] **Step 5: Browser smoke**

Open:

```text
https://pi5.vivessato.com/?syncdiag=1&cb=sync-telemetry
```

Expected: video remains visible, labels still render, diagnostic chip now includes video frame clock and event/video delta values.

- [x] **Step 6: Commit**

```bash
git add dashboard/pi_dash.html tests/test_dashboard_sync_diagnostics.py docs/working/progress/2026-05-12-codex-live-log.md
git commit -m "feat(dashboard): expose live label sync telemetry"
git push origin pi-main
```

---

### Task 2: Add Event Buffer and Clock Bridge

**Files:**
- Modify: `dashboard/pi_dash.html`
- Create: `tests/test_dashboard_clock_bridge_static.py`

- [ ] **Step 1: Write a failing static test**

Assert the dashboard contains `eventBuffer`, `estimateClockOffset`, `selectRenderableEvent`, and a guarded feature flag such as `syncRender=1`.

- [ ] **Step 2: Implement buffer without changing default behavior**

Buffer raw events by PTS and keep the current immediate-render path as the default. Behind `?syncRender=1`, compute a rolling median offset between `event.pts` and video `mediaTime`, estimate current pipeline PTS from the video frame clock, and render the newest event at or before the target PTS.

- [ ] **Step 3: Verify**

Run static dashboard tests and browser smoke with both default mode and `?syncRender=1`.

---

### Task 3: Add Spatial Interpolation and Prediction

**Files:**
- Modify: `dashboard/pi_dash.html`
- Create/extend: `tests/test_dashboard_clock_bridge_static.py`

- [ ] **Step 1: Capture per-track history**

Store the last 3-5 bboxes per track with event PTS.

- [ ] **Step 2: Interpolate inside buffered history**

When two events bracket the render PTS, interpolate bbox coordinates.

- [ ] **Step 3: Predict short gaps**

When the render PTS is slightly newer than the latest event, extrapolate from velocity for a capped window, initially `150ms`, then tune from diagnostics.

- [ ] **Step 4: Clamp and degrade safely**

Clamp predicted bboxes to the frame, fall back to latest event if velocity is unstable, and fade labels out rather than jumping.

---

### Task 4: Replay and Demo Acceptance Harness

**Files:**
- Modify: `tools/sync_replay_assert.py`
- Add tests only if existing harness lacks assertions for the new debug metrics.

- [ ] **Step 1: Confirm demo annotation path**

Use the existing annotated demo footage to prove label timing and spatial error against known entry/exit windows.

- [ ] **Step 2: Add metric output**

Report median event/video offset, p95 spatial error, missed-label frames, and late-label frames.

- [ ] **Step 3: Define pass gates**

Initial gates: labels visible for locked tracks, no sustained drift, no empty-video overlay, p95 spatial error within the annotated tolerance.

---

### Task 5: Tune Label Confidence and UX

**Files:**
- Modify: `dashboard/pi_dash.html`
- Modify only if needed: `pipeline/process_thread.py`

- [ ] **Step 1: Separate pending from locked visual state**

Keep `identifying...` light and non-authoritative; make locked species labels stable.

- [ ] **Step 2: Remove boxes behind a toggle**

Keep bounding boxes available as a debug toggle; make label-only the default once sync telemetry is healthy.

- [ ] **Step 3: Add production-safe controls**

Expose labels, boxes, sync diagnostics, and sync-render mode as URL/localStorage toggles so testing does not require code edits.

---

### Task 6: Production Acceptance

**Files:**
- Modify: `docs/working/progress/2026-05-12-codex-live-log.md`

- [ ] **Step 1: LAN browser check**

Use `http://pi5.local:8099/?syncdiag=1` for low-latency baseline.

- [ ] **Step 2: Cloudflare browser check**

Use `https://pi5.vivessato.com/?syncdiag=1` for authenticated remote path.

- [ ] **Step 3: Real-camera lock check**

Wait for real birds. Confirm labels appear, stay attached, and no high-res snapshot regression appears.

- [ ] **Step 4: Demo check**

Confirm demo remains useful for label timing and transport checks, while documenting that it is low-resolution and cannot validate high-res snapshot capture.
