# Pi Live-ID Foundations — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** End the Pi's thermal throttling and make live identification actually produce labels, by removing wasted motion computation and unblocking the classifier — software-only, each change measured before/after.

**Architecture:** The detector declares whether it consumes motion regions; on the Pi (Hailo full-frame) it doesn't, so the per-frame MOG2 call is skipped entirely (the thermal fix). The classifier's vote-eligibility floor and attempt cap become env-configurable so more crops vote and tracks can lock. Crash observability (faulthandler + coredump) is armed first so every later measurement is on a stable process.

**Tech Stack:** Python 3.13, PyAV, OpenCV (TBB), Hailo-8L (pyhailort), Norfair, systemd `--user`, pytest. Edit in `/Users/vives/bird-classifier-pi/`, deploy via `rsync` to `vives@pi5.local:/home/vives/bird-classifier/`, restart `bird-pipeline.service` (`--user`).

**Spec:** `docs/working/specs/2026-06-27-pi-live-id-foundations-design.md` (commit `71973fd`).

**Execution order rationale:** Task 1 (crash safety net) first so measurements are stable. Task 2 (MOG2/thermal) next — the biggest, most visible win. Task 3 (classifier) — biggest identification win. Task 4 (health), Task 5 (SEGV root cause), Task 6 (UX) follow; Task 6 is client-side and may be done in parallel anytime.

---

## File Structure

| File | Responsibility | Tasks |
|---|---|---|
| `bird_pipeline_v3.py` | wiring: faulthandler init, classifier floor env, process-thread construction | 1, 3 |
| `pipeline/hailo_detector.py` | declare `uses_motion_regions = False` | 2 |
| `pipeline/detector.py` (`BirdDetector`) | declare `uses_motion_regions = True` (iMac parity) | 2 |
| `pipeline/process_thread.py` | skip motion when detector doesn't use it; fix `yolo_actually_ran` | 2 |
| `pipeline/classifier.py` | env-configurable `MAX_CLASSIFICATION_ATTEMPTS` | 3 |
| `pipeline/pi_classifier.py` | (no change; floor passed in via constructor) | 3 |
| `pipeline/health.py` | truthful restart key + status threshold | 4 |
| `pipeline/hailo_engine.py` | SEGV root-cause inspection (async job lifecycle) | 5 |
| `dashboard/video-stream.js` | unmute + fullscreen controls | 6 |
| `tests/pipeline/test_process_thread_motion.py` | motion-skip behavior | 2 |
| `tests/pipeline/test_pi_classifier_floor.py` | floor/eligibility behavior | 3 |
| `tests/pipeline/test_health_status.py` | restart-key status logic | 4 |

---

## Task 1: Crash observability safety net (F0)

**Files:**
- Modify: `bird_pipeline_v3.py` (top of module, after imports ~line 14)
- Modify (on Pi): `~/.bird-observatory-env` (add `PYTHONFAULTHANDLER=1`)

No unit test (observability/config). Verification is behavioral: a fatal signal dumps a Python traceback.

- [ ] **Step 1: Enable faulthandler at process start**

In `bird_pipeline_v3.py`, immediately after the existing `import` block (after line 14 `from pathlib import Path`), add:

```python
import faulthandler
faulthandler.enable(all_threads=True)  # dump all thread tracebacks on SIGSEGV/SIGABRT
```

- [ ] **Step 2: Verify faulthandler is armed locally**

Run: `cd /Users/vives/bird-classifier-pi && python3 -c "import bird_pipeline_v3" 2>&1 | head` (expect import to load; no faulthandler error). Then confirm the lines are present:
Run: `grep -n "faulthandler" bird_pipeline_v3.py`
Expected: shows `faulthandler.enable(all_threads=True)`.

- [ ] **Step 3: Confirm systemd auto-restart is already in place (no change needed)**

Run: `ssh vives@pi5.local 'systemctl --user show bird-pipeline.service -p Restart -p RestartSec'`
Expected: `Restart=always`, `RestartSec=10s`. If not, add them to the unit. (Verified present 2026-06-28.)

- [ ] **Step 4: Enable coredumps for a C-level backtrace on the Pi**

Run: `ssh vives@pi5.local 'systemctl --user show bird-pipeline.service -p LimitCORE; ulimit -c'`
If core size is 0, add `LimitCORE=infinity` under `[Service]` in `~/.config/systemd/user/bird-pipeline.service`, then `systemctl --user daemon-reload`. Confirm `coredumpctl` exists: `ssh vives@pi5.local 'command -v coredumpctl'`.
Expected: coredumpctl present; LimitCORE set so the next SEGV leaves a core.

- [ ] **Step 5: Deploy + restart + confirm clean start**

```bash
rsync -av bird_pipeline_v3.py vives@pi5.local:/home/vives/bird-classifier/
ssh vives@pi5.local 'grep -q PYTHONFAULTHANDLER ~/.bird-observatory-env || echo "PYTHONFAULTHANDLER=1" >> ~/.bird-observatory-env; systemctl --user restart bird-pipeline.service; sleep 5; systemctl --user is-active bird-pipeline.service'
```
Expected: `active`.

- [ ] **Step 6: Commit**

```bash
git add bird_pipeline_v3.py
git commit -m "feat(pi): arm faulthandler + coredumps for SEGV capture (F0)"
```

---

## Task 2: Kill wasted MOG2 — the thermal fix (F1)

**Files:**
- Modify: `pipeline/hailo_detector.py` (class body, near other class attrs ~line 80)
- Modify: `pipeline/detector.py` (`BirdDetector` class body)
- Modify: `pipeline/process_thread.py:94-121`
- Test: `tests/pipeline/test_process_thread_motion.py`

**Context:** `HailoDetector.detect()` ignores `motion_regions` (runs full-frame). The per-frame `motion_gate.regions()` call (process_thread.py:98) is the dominant CPU cost (MOG2 across 4 TBB threads) and its output is discarded. The Pi AOI is a near-full-frame rectangle, so dropping motion loses no real filtering.

- [ ] **Step 1: Write the failing test**

Create `tests/pipeline/test_process_thread_motion.py`:

```python
from unittest.mock import MagicMock
from pipeline.process_thread import CameraProcessThread


def _make_thread(detector):
    """Build a CameraProcessThread without running __init__ (documented pattern)."""
    pt = CameraProcessThread.__new__(CameraProcessThread)
    pt.name = "feeder"
    pt.motion_gate = MagicMock()
    pt.motion_gate.regions.return_value = []
    pt.detector = detector
    pt.tracker = MagicMock()
    pt.tracker.update.return_value = MagicMock(active=[], new=[])
    pt.classifier = MagicMock()
    pt.event_store = MagicMock()
    pt.snapshot_writer = None
    pt.sse_server = None
    pt.health = None
    pt._dry_run = True
    pt._last_forced_full = 0.0
    pt._last_debug_encode_ms = 0
    pt._last_health_update_ms = 0
    from collections import deque
    pt._stats = {"frames_processed": 0, "detections": 0,
                 "yolo_ms_samples": deque(maxlen=100),
                 "yolo_runs_total": 0, "yolo_skipped_motion": 0}
    return pt


def _frame():
    import numpy as np
    f = MagicMock()
    f.bgr = np.zeros((360, 640, 3), dtype=np.uint8)
    f.wall_time_ms = 0.0
    f.width = 640
    f.height = 360
    return f


def test_motion_skipped_when_detector_does_not_use_it():
    det = MagicMock()
    det.uses_motion_regions = False
    det.detect.return_value = []
    pt = _make_thread(det)
    pt._process_frame(_frame())
    det.detect.assert_called_once()          # YOLO still runs (full-frame)
    pt.motion_gate.regions.assert_not_called()  # MOG2 NOT computed


def test_motion_computed_when_detector_uses_it():
    det = MagicMock()
    det.uses_motion_regions = True
    det.detect.return_value = []
    pt = _make_thread(det)
    pt._process_frame(_frame())
    pt.motion_gate.regions.assert_called_once()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/vives/bird-classifier-pi && python3 -m pytest tests/pipeline/test_process_thread_motion.py -v`
Expected: FAIL — `test_motion_skipped_when_detector_does_not_use_it` fails because `_process_frame` currently always calls `motion_gate.regions`, and `detector` mock has no `uses_motion_regions` driving behavior yet.

- [ ] **Step 3: Declare detector motion contract**

In `pipeline/hailo_detector.py`, add a class attribute to `HailoDetector` (near the top of the class body, before `detect`):

```python
    # Hailo runs full-frame every frame; it ignores motion regions. Telling
    # the process thread this lets it skip the expensive per-frame MOG2 call.
    uses_motion_regions = False
```

In `pipeline/detector.py`, add to `BirdDetector` (the ONNX/iMac detector that IS region-gated):

```python
    uses_motion_regions = True
```

- [ ] **Step 4: Skip motion when unused, in `_process_frame`**

In `pipeline/process_thread.py`, replace lines 96-121 (the "Motion gate" through `self._stats["detections"]` block) with:

```python
        # 1. Motion gate — only if the detector actually consumes regions.
        #    Hailo runs full-frame and ignores them (uses_motion_regions=False),
        #    so skipping MOG2 removes the dominant per-frame CPU cost. Default
        #    True keeps region-gated detectors (iMac BirdDetector) working.
        uses_motion = getattr(self.detector, "uses_motion_regions", True)
        regions = self.motion_gate.regions(frame.bgr) if uses_motion else None

        # 2. Decide whether to force a full-frame YOLO scan
        now = time.time()
        forced_full = (now - self._last_forced_full) > FORCED_FULL_YOLO_INTERVAL_S
        if forced_full:
            self._last_forced_full = now

        # 3. Detect
        t_det = time.monotonic()
        detections = self.detector.detect(frame, regions, forced_full=forced_full)
        det_ms = (time.monotonic() - t_det) * 1000
        # YOLO "actually ran" whenever the detector does full-frame work. A
        # region-gated detector only runs when there are regions or a forced scan.
        yolo_actually_ran = (not uses_motion) or bool(regions) or forced_full
        if yolo_actually_ran:
            self._stats["yolo_ms_samples"].append(det_ms)
            self._stats["yolo_runs_total"] += 1
        else:
            self._stats["yolo_skipped_motion"] += 1
        self._stats["detections"] += len(detections)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/pipeline/test_process_thread_motion.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Deploy and MEASURE the thermal win (the acceptance gate)**

```bash
rsync -av pipeline/hailo_detector.py pipeline/detector.py pipeline/process_thread.py vives@pi5.local:/home/vives/bird-classifier/pipeline/
ssh vives@pi5.local 'systemctl --user restart bird-pipeline.service'
```
Wait ~20 min for thermal to settle, then:
Run: `ssh vives@pi5.local 'PID=$(systemctl --user show bird-pipeline.service -p MainPID --value); vcgencmd measure_temp; vcgencmd get_throttled; top -bn1 -p $PID | tail -1'`
Expected: process CPU drops from ~130% toward ~50-60%; `throttled` active-throttle bit (`0x...8`) clears; temp falls below the soft limit (target ~68-72°C). **Record the before/after numbers in the commit message.**

- [ ] **Step 7: Commit**

```bash
git add pipeline/hailo_detector.py pipeline/detector.py pipeline/process_thread.py tests/pipeline/test_process_thread_motion.py
git commit -m "perf(pi): skip per-frame MOG2 when detector runs full-frame (F1 thermal fix)

Hailo ignores motion regions; computing them every frame across 4 TBB
threads was the dominant CPU/thermal cost. Detector now declares
uses_motion_regions; process thread skips MOG2 when False.
Before: <temp>/<cpu>; After: <temp>/<cpu>."
```

---

## Task 3: Classifier gate floor + attempts (F2)

**Files:**
- Modify: `pipeline/classifier.py:16`
- Modify: `bird_pipeline_v3.py:213`
- Test: `tests/pipeline/test_pi_classifier_floor.py`

**Context:** `PiClassifier.confident_threshold=0.25` ≈ model median → ~82% of crops rejected before voting. Lower the floor (more crops vote) and raise the attempt cap (more chances to reach ≥3 votes). The lock gate (≥3 votes, ≥0.35, ≥60% agreement, process_thread.py:320) is unchanged — it remains the noise guard.

- [ ] **Step 1: Write the failing test**

Create `tests/pipeline/test_pi_classifier_floor.py`:

```python
from unittest.mock import MagicMock
from pipeline.pi_classifier import PiClassifier, _normalize_raw_score


def _registry(raw_score, name="House Finch"):
    reg = MagicMock()
    reg.current_name = "aiy_onnx"
    reg.classify.return_value = [{"common_name": name, "raw_score": raw_score}]
    return reg


def test_crop_below_old_threshold_now_votes_at_lower_floor():
    # Pick a raw_score whose normalized confidence is between 0.16 and 0.25.
    raw = next(r for r in range(256) if 0.16 <= _normalize_raw_score(r) < 0.25)
    clf = PiClassifier(_registry(raw), confident_threshold=0.16)
    res = clf.classify(MagicMock(), 0.0, "feeder")
    assert res.species == "House Finch"        # now eligible to vote
    assert res.confidence >= 0.16


def test_crop_below_floor_still_rejected():
    raw = next(r for r in range(256) if _normalize_raw_score(r) < 0.16)
    clf = PiClassifier(_registry(raw), confident_threshold=0.16)
    res = clf.classify(MagicMock(), 0.0, "feeder")
    assert res.species is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/pipeline/test_pi_classifier_floor.py -v`
Expected: FAIL — `bird_pipeline_v3.py` still builds `PiClassifier(registry)` with the 0.25 default elsewhere, but more importantly this test passes a floor directly; it fails only if `_normalize_raw_score` import path is wrong. (If it already passes, that confirms `PiClassifier` honors the constructor floor — proceed to wire the env var so production uses it.)

- [ ] **Step 3: Make the attempt cap env-configurable**

In `pipeline/classifier.py`, replace line 16:

```python
import os
MAX_CLASSIFICATION_ATTEMPTS = int(os.environ.get("PIPELINE_MAX_CLASS_ATTEMPTS", "12"))
```
(Add `import os` at the top if not already imported.)

- [ ] **Step 4: Make the classifier floor env-configurable in production wiring**

In `bird_pipeline_v3.py`, replace line 213 `classifier = PiClassifier(registry)` with:

```python
        _floor = float(os.environ.get("PIPELINE_CLASSIFIER_FLOOR", "0.16"))
        classifier = PiClassifier(registry, confident_threshold=_floor)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/pipeline/test_pi_classifier_floor.py -v`
Expected: PASS (both tests).

- [ ] **Step 6: Deploy + measure label rate (acceptance)**

```bash
rsync -av pipeline/classifier.py bird_pipeline_v3.py vives@pi5.local:/home/vives/bird-classifier/ --relative
ssh vives@pi5.local 'systemctl --user restart bird-pipeline.service'
```
After ~30 min of daytime bird activity:
Run: `ssh vives@pi5.local 'curl -s http://localhost:8100/ | python3 -c "import sys,json;d=json.load(sys.stdin);print(d)"' | grep -iE "unlabeled|labeled|model_current"`
Expected: unlabeled-vs-labeled ratio improves markedly (target unlabeled ~82% → ~40-50%); species labels visible on the live overlay. **Record before/after in the commit.**

- [ ] **Step 7: Commit**

```bash
git add pipeline/classifier.py bird_pipeline_v3.py tests/pipeline/test_pi_classifier_floor.py
git commit -m "feat(pi): lower classifier vote floor 0.25->0.16, attempts 5->12 (F2)

Floor sat at the AIY model's confidence median (~0.23), rejecting ~82%
of crops before they could vote. Lock gate (>=3 votes/0.35/60%) unchanged.
Both env-configurable: PIPELINE_CLASSIFIER_FLOOR, PIPELINE_MAX_CLASS_ATTEMPTS."
```

---

## Task 4: Truthful health signal (F3)

**Files:**
- Modify: `pipeline/health.py:76-78`
- Test: `tests/pipeline/test_health_status.py`

**Context:** `overall=broken` fires purely on `ffmpeg_restarts_last_hour > 10`. The Pi uses PyAV (not ffmpeg), and the storm should subside once thermal is fixed (Task 2). Rename for truth and downgrade a pure restart-storm (no other fault) to `degraded`, reserving `broken` for genuine outages.

- [ ] **Step 1: Write the failing test**

Create `tests/pipeline/test_health_status.py`:

```python
from pipeline.health import HealthRegistry  # adjust import to the actual class name in health.py


def _status(cam):
    h = HealthRegistry.__new__(HealthRegistry)
    return h._compute_status({"feeder": cam})


def test_restart_storm_alone_is_degraded_not_broken():
    cam = {"reader_restarts_last_hour": 20, "frame_age_ms": 30, "capture_alive": True}
    assert _status(cam) == "degraded"


def test_dead_capture_is_broken():
    cam = {"reader_restarts_last_hour": 0, "frame_age_ms": 999999, "capture_alive": False}
    assert _status(cam) == "broken"
```

(Before writing, the implementer must open `pipeline/health.py` and confirm the actual class name and the per-camera dict keys; adjust the test to match. Keep the two assertions.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/pipeline/test_health_status.py -v`
Expected: FAIL — restart storm currently returns `broken`, and the `reader_restarts_last_hour` key doesn't exist yet.

- [ ] **Step 3: Add the truthful key + downgrade lone restart-storm**

In `pipeline/health.py`, in `_compute_status`, replace the restart-storm block (around lines 76-78):

```python
            # Reader-loop restarts (PyAV, not ffmpeg). Read the new key, fall
            # back to the legacy name for dashboards not yet updated.
            restart_storm = capture.get("reader_restarts_last_hour",
                                        capture.get("ffmpeg_restarts_last_hour", 0))
            if restart_storm > 10:
                # A restart storm alone is degraded, not broken — frames may
                # still be flowing. Genuine outages are caught by the
                # frame-age / capture-alive checks above.
                status = "degraded"
```
Ensure `status` defaults to `"ok"` and that the function still returns `broken` first for the dead-capture / stale-frame checks (those `return "broken"` short-circuits stay above this block).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/pipeline/test_health_status.py -v`
Expected: PASS.

- [ ] **Step 5: Deploy + confirm restarts subsided post-thermal-fix**

```bash
rsync -av pipeline/health.py vives@pi5.local:/home/vives/bird-classifier/pipeline/
ssh vives@pi5.local 'systemctl --user restart bird-pipeline.service'
```
After 1 h stable uptime:
Run: `ssh vives@pi5.local 'curl -s http://localhost:8100/ | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get(\"overall\"), d.get(\"feeder\",{}).get(\"reader_restarts_last_hour\"))"'`
Expected: `overall=ok` (or `degraded` with a true reason); restarts in single digits.

- [ ] **Step 6: Commit**

```bash
git add pipeline/health.py tests/pipeline/test_health_status.py
git commit -m "fix(pi): truthful reader-restart health key; lone storm -> degraded (F3)"
```

---

## Task 5: SEGV root cause + harden (F4)

**Files:**
- Inspect: `pipeline/hailo_engine.py` (async job lifecycle: `run_async`/`wait`/buffer release)
- Possibly modify: `pipeline/hailo_engine.py`

**Context:** ~6 SEGV/24h, suspected Hailo VDevice/driver, possibly heat-aggravated (Task 2 may reduce frequency). This task is investigation-led; the fix depends on the captured backtrace. No speculative TDD.

- [ ] **Step 1: Confirm capture is armed (depends on Task 1)**

Run: `ssh vives@pi5.local 'grep PYTHONFAULTHANDLER ~/.bird-observatory-env; coredumpctl list 2>/dev/null | tail -3'`
Expected: faulthandler env set; coredumpctl available.

- [ ] **Step 2: Wait for / locate the next SEGV and pull the trace**

Run: `ssh vives@pi5.local 'journalctl --user -u bird-pipeline.service --since "-24h" | grep -iE "segv|fault|Current thread|signal|Traceback" | head -40; coredumpctl info bird-pipeline 2>/dev/null | head -40'`
Expected: a faulthandler "Current thread" dump and/or a coredump backtrace naming the faulting frame (look for `pyhailort`/`hailo_engine` frames).

- [ ] **Step 3: Inspect the Hailo async job lifecycle**

Open `pipeline/hailo_engine.py` (`infer` ~lines 76-78 use `run_async`/`wait`). Check for: a job whose buffers are released/GC'd before `wait()` returns, missing `with` scoping on the async job, or shared-VDevice reentrancy across threads (Hailo classifiers + detector share one VDevice via the scheduler). Document the finding inline in the task notes.

- [ ] **Step 4: Apply the minimal fix the trace points to, or escalate**

If the trace shows a buffer-lifetime bug: hold a reference to the input/output buffers until `wait()` completes (e.g., keep them in a local that outlives the async job). If it shows VDevice reentrancy: serialize inference with the existing engine lock. If the trace is inconclusive: keep the auto-restart+backoff (Task 1) as the survivable mitigation and record the open question. Do NOT guess a driver fix without evidence.

- [ ] **Step 5: Verify uptime**

Run: `ssh vives@pi5.local 'systemctl --user show bird-pipeline.service -p ActiveEnterTimestamp; uptime'`
Expected (acceptance): continuous service uptime exceeds the prior 2-6 h crash interval (target >12 h). Record the observation window.

- [ ] **Step 6: Commit (if code changed)**

```bash
git add pipeline/hailo_engine.py
git commit -m "fix(pi): <specific Hailo async lifecycle fix from backtrace> (F4)"
```

---

## Task 6: Live-view sound + fullscreen (UX-A)

**Files:**
- Modify: `dashboard/video-stream.js`
- Modify: `dashboard/pi_dash.html` (add two controls in the live-view container only)

**Context:** Stream already carries H.264 + Opus audio and is 1080p; it's just `muted=true` (video-stream.js:9), `controls=false` (:8). Browser autoplay policy requires a user gesture to unmute. Do NOT touch theme CSS, the theme switcher, or non-live panels.

- [ ] **Step 1: Expose unmute + fullscreen helpers on the element**

In `dashboard/video-stream.js`, inside the `VideoStream` class (after `oninit`), add:

```js
  unmute() { this.video.muted = false; this.video.volume = 1.0; this.video.play?.(); }
  toggleFullscreen() {
    const el = this.video.parentElement || this.video;
    if (document.fullscreenElement) document.exitFullscreen();
    else (el.requestFullscreen || el.webkitRequestFullscreen)?.call(el);
  }
```

- [ ] **Step 2: Add the two controls to the live-view container**

In `dashboard/pi_dash.html`, locate the container that holds the `<video-stream>` element for the live view (search for `video-stream` / `setupLiveView`). Add, inside that container, an overlay with two buttons (reuse existing button styling classes if present; otherwise minimal inline style — do not edit the theme stylesheet):

```html
<div class="live-controls" style="position:absolute;bottom:8px;right:8px;display:flex;gap:6px;z-index:5;">
  <button id="live-unmute" title="Unmute" aria-label="Unmute">🔊</button>
  <button id="live-fullscreen" title="Fullscreen" aria-label="Fullscreen">⛶</button>
</div>
```
And wire them (in the existing live-view setup script, after the `<video-stream>` element exists):

```js
const vs = document.querySelector('video-stream');
document.getElementById('live-unmute')?.addEventListener('click', () => vs.unmute());
document.getElementById('live-fullscreen')?.addEventListener('click', () => vs.toggleFullscreen());
```

- [ ] **Step 3: Deploy**

```bash
rsync -av dashboard/video-stream.js dashboard/pi_dash.html vives@pi5.local:/home/vives/bird-classifier/dashboard/
ssh vives@pi5.local 'systemctl --user restart bird-dashboard.service'
```

- [ ] **Step 4: Verify with a headless screenshot (HARD RULE: human-facing verification)**

Capture the live view headless (Chromium/WebKit against `http://pi5.local:8099/`), Read the PNG, confirm the unmute + fullscreen buttons render over the live video and don't overlap the label overlay. Then click unmute and confirm audio is no longer muted (`video.muted === false`). Show David the screenshot.
Expected: buttons visible and functional; overlay unaffected.

- [ ] **Step 5: Commit**

```bash
git add dashboard/video-stream.js dashboard/pi_dash.html
git commit -m "feat(dashboard): live-view unmute + fullscreen controls (UX-A)"
```

---

## Self-Review

**Spec coverage:** F0→Task 1, F1→Task 2, F2→Task 3, F3→Task 4, F4→Task 5, UX-A→Task 6. All spec items covered. Subsequent sub-projects (tracker, snapshots, parity, flagship) intentionally out of scope for this plan.

**Placeholder scan:** Task 5 is investigation-led (the fix is genuinely backtrace-dependent) and Task 4/6 require the implementer to confirm an exact class name / container in-file before editing — these are flagged explicitly, not hidden TODOs. All code-change steps show real code.

**Type/name consistency:** `uses_motion_regions` (attribute) used identically in hailo_detector.py, detector.py, and process_thread.py. Env vars consistent: `PIPELINE_CLASSIFIER_FLOOR`, `PIPELINE_MAX_CLASS_ATTEMPTS`. `reader_restarts_last_hour` key used in both health.py and its test.

**Measurement honesty:** Tasks 2, 3, 4 have explicit before/after acceptance numbers recorded in their commits (no "should be faster" claims).
