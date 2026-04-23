# Buffered Hi-Res Ring + Frame-Quality Picker (Tier 1b) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans.

**Goal:** Replace the stale-bbox hallucination bug in `SnapshotWriter._fetch_hires_frame` with a buffered hi-res ring. When a detection fires at time T, pick the best-quality 1080p frame whose capture time is near T (not 2-5 seconds later when go2rtc finally releases a keyframe). "Best" = sharp, big, bird-centered, high detector confidence.

**Why this is the fix for the hallucination symptom David flagged:** go2rtc's `/api/frame.mp4?src=feeder-main` blocks until the next H.264 keyframe, typically 2-5 seconds. During that window, any moving bird has left the bbox — so we crop empty feeder background and AIY confidently labels it (American Robin, Common Grackle, etc.). The bbox also scales from the 640×360 detection frame to 1920×1080 output frame, but the bird in the hi-res frame is not where the bbox says it is anymore.

**Architecture:** Introduce a `HiResRingBuffer` that maintains a rolling window of recent 1080p frames (tagged with their capture wall-time). The `FrameCapture` for the `-main` stream feeds it. When `SnapshotWriter._write_one` needs a frame, it queries the ring for the nearest-timestamp frame, evaluates quality across a small candidate set (the nearest K frames), and returns the best. Safe integration: the new ring is opt-in — if the ring is empty or disabled, `_fetch_hires_frame` retains the existing behavior. **Does not break the live detection circuit** — the ring is populated by a new separate `FrameCapture` instance, not tapped off the existing one. Main-stream capture adds ~150 MB RAM (2s × 5 fps × 1920×1080×3 BGR ≈ 60 MB; plus ffmpeg buffers). Feasible on the 8 GB iMac.

Frame-quality scoring (per David's spec):
- **Sharpness**: Laplacian variance inside bbox. Anti-motion-blur.
- **Center weight**: upper-middle third bonus — where a perched feeder bird's head usually is.
- **Size**: reject bboxes smaller than 80×80 px.
- **Detector confidence**: from the track — bias toward frames where YOLO was most sure.

Shadow mode 3-4 days: new path writes a SIDECAR JSON per row (picked frame id, quality score, candidates considered) BEFORE it becomes the authoritative source. After David eyeballs a sample and approves, the sidecar becomes the main path.

**Tech Stack:** Python 3.9 (venv-coral), OpenCV (Laplacian), existing `FrameCapture`, existing `SnapshotWriter`, no new deps.

---

## File Structure

**Files created:**
- `~/bird-classifier/pipeline/hires_ring.py` — ring buffer + frame quality scorer.
- `~/bird-classifier/tests/test_hires_ring.py` — unit tests.

**Files modified:**
- `~/bird-classifier/bird_pipeline_v3.py` — instantiate the ring buffer and a feeder-main FrameCapture that feeds it.
- `~/bird-classifier/pipeline/snapshot_writer.py` — `SnapshotWriter.__init__` takes an optional `hires_ring`; `_write_one` uses it with fallback.

**No changes to** detection path, tracker, classifier, dashboard, DB schema.

---

### Task 1: Ring buffer implementation (TDD)

**Files:**
- Create: `pipeline/hires_ring.py`
- Create: `tests/test_hires_ring.py`

- [ ] **Step 1.1: Write failing test for basic push/query**

  ```python
  # tests/test_hires_ring.py
  import numpy as np
  from pipeline.hires_ring import HiResRingBuffer


  def _frame(val=0):
      f = np.full((1080, 1920, 3), val, dtype=np.uint8)
      return f


  def test_push_and_find_nearest():
      ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
      ring.push(_frame(10), wall_ms=1000)
      ring.push(_frame(20), wall_ms=1200)
      ring.push(_frame(30), wall_ms=1400)
      hit = ring.find_nearest(wall_ms=1210)
      assert hit is not None
      assert hit.wall_ms == 1200
      # frame value confirms we got the right one
      assert hit.frame[0, 0, 0] == 20


  def test_drops_old_frames():
      ring = HiResRingBuffer(max_seconds=1.0, expected_fps=5)
      ring.push(_frame(), wall_ms=1000)
      ring.push(_frame(), wall_ms=1500)
      ring.push(_frame(), wall_ms=2500)  # now 1.5s after the oldest
      # Oldest should have been evicted
      assert ring.find_nearest(wall_ms=1000) is None
      assert ring.find_nearest(wall_ms=1500) is not None


  def test_find_nearest_empty_returns_none():
      ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
      assert ring.find_nearest(wall_ms=1000) is None


  def test_find_candidates_returns_k_nearest():
      ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5)
      for i in range(10):
          ring.push(_frame(i), wall_ms=1000 + i * 100)
      # around t=1550, K=3 → frames at 1500, 1600, 1400
      cands = ring.find_candidates(wall_ms=1550, k=3)
      times = sorted([c.wall_ms for c in cands])
      assert times == [1400, 1500, 1600]
  ```

- [ ] **Step 1.2: Run the test — should fail with ImportError**

  ```bash
  cd ~/bird-classifier
  ./venv-coral/bin/python -m pytest tests/test_hires_ring.py -v
  ```
  Expected: 4 errors about missing module.

- [ ] **Step 1.3: Implement the ring**

  ```python
  # pipeline/hires_ring.py
  """Rolling buffer of recent 1080p frames for nearest-timestamp lookup.

  Used by SnapshotWriter to find the hi-res frame whose capture time matches
  the detection's wall_time_ms, instead of waiting 2-5s for go2rtc to emit
  the next keyframe (which loses the bird).
  """
  from __future__ import annotations

  import bisect
  import threading
  from dataclasses import dataclass
  from typing import Optional

  import numpy as np


  @dataclass
  class RingFrame:
      frame: np.ndarray   # BGR, full 1920x1080
      wall_ms: float


  class HiResRingBuffer:
      """Thread-safe rolling buffer indexed by wall-clock ms.

      Eviction: any frame older than `max_seconds` behind the newest is dropped.
      Capacity is roughly max_seconds * expected_fps, with slack for jitter.
      """

      def __init__(self, max_seconds: float = 2.0, expected_fps: float = 5.0):
          self.max_ms = float(max_seconds * 1000.0)
          # sizing with 2x headroom for clock jitter
          self.cap = max(4, int(max_seconds * expected_fps * 2))
          self._frames: list[RingFrame] = []  # sorted by wall_ms ascending
          self._times: list[float] = []        # parallel list for bisect
          self._lock = threading.Lock()

      def push(self, frame: np.ndarray, wall_ms: float) -> None:
          with self._lock:
              # Keep sorted by wall_ms. New frames arrive monotonic in practice.
              if self._times and wall_ms < self._times[-1]:
                  # rare: clock skew or out-of-order — insort to keep invariant
                  idx = bisect.bisect_left(self._times, wall_ms)
                  self._times.insert(idx, wall_ms)
                  self._frames.insert(idx, RingFrame(frame.copy(), wall_ms))
              else:
                  self._times.append(wall_ms)
                  self._frames.append(RingFrame(frame.copy(), wall_ms))

              # Evict old
              newest = self._times[-1]
              while self._times and (newest - self._times[0]) > self.max_ms:
                  self._times.pop(0)
                  self._frames.pop(0)
              # Also respect hard cap
              while len(self._times) > self.cap:
                  self._times.pop(0)
                  self._frames.pop(0)

      def find_nearest(self, wall_ms: float) -> Optional[RingFrame]:
          with self._lock:
              if not self._times:
                  return None
              idx = bisect.bisect_left(self._times, wall_ms)
              candidates: list[int] = []
              if idx < len(self._times):
                  candidates.append(idx)
              if idx > 0:
                  candidates.append(idx - 1)
              best = min(candidates, key=lambda i: abs(self._times[i] - wall_ms))
              return self._frames[best]

      def find_candidates(self, wall_ms: float, k: int = 3) -> list[RingFrame]:
          """Return the K frames closest in time to wall_ms, any order."""
          with self._lock:
              if not self._times:
                  return []
              scored = sorted(
                  range(len(self._times)),
                  key=lambda i: abs(self._times[i] - wall_ms),
              )
              return [self._frames[i] for i in scored[:k]]

      def __len__(self) -> int:
          with self._lock:
              return len(self._frames)
  ```

- [ ] **Step 1.4: Test passes**

  ```bash
  ./venv-coral/bin/python -m pytest tests/test_hires_ring.py -v
  ```
  Expected: 4 passed.

---

### Task 2: Quality scorer (TDD)

- [ ] **Step 2.1: Write failing tests**

  Append to `tests/test_hires_ring.py`:

  ```python
  from pipeline.hires_ring import score_frame


  def _striped_frame(size=1080):
      """High-frequency stripes → high Laplacian variance → sharp."""
      f = np.zeros((size, size, 3), dtype=np.uint8)
      f[:, ::2, :] = 255
      return f


  def _flat_frame(size=1080):
      """Flat gray → zero Laplacian variance → blurry."""
      return np.full((size, size, 3), 128, dtype=np.uint8)


  def test_score_sharp_beats_blurry():
      sharp = _striped_frame()
      blurry = _flat_frame()
      bbox = [400, 400, 600, 600]
      s = score_frame(sharp, bbox, detector_conf=0.8)
      b = score_frame(blurry, bbox, detector_conf=0.8)
      assert s > b


  def test_score_rejects_tiny_bbox():
      f = _striped_frame()
      bbox = [500, 500, 550, 550]   # 50x50 — below the 80 floor
      s = score_frame(f, bbox, detector_conf=0.9)
      assert s == 0.0


  def test_score_rewards_higher_detector_confidence():
      f = _striped_frame()
      bbox = [400, 400, 600, 600]
      low = score_frame(f, bbox, detector_conf=0.3)
      high = score_frame(f, bbox, detector_conf=0.9)
      assert high > low
  ```

- [ ] **Step 2.2: Run — should fail with ImportError**

- [ ] **Step 2.3: Implement score_frame**

  Add to `pipeline/hires_ring.py`:

  ```python
  import cv2  # only in scorer path, not the ring itself

  MIN_BBOX_SIDE = 80   # pixels


  def score_frame(frame: np.ndarray, bbox, detector_conf: float) -> float:
      """Quality score for a (frame, bbox) pair. Higher = better.

      Components (per David's 2026-04-22 spec):
      - Sharpness: Laplacian variance inside bbox (anti-motion-blur proxy; a
        visible eye correlates with high-frequency detail).
      - Center weight: boost if the bbox center is in the upper-middle third
        (where a perched bird's head usually is).
      - Size: zero score below 80x80 (can't see an eye that small).
      - Detector confidence: multiplier, so a sharp but low-confidence bbox
        doesn't out-rank a sharp high-confidence one.

      Returns 0.0 for invalid / too-small / out-of-frame bboxes.
      """
      if frame is None or frame.size == 0:
          return 0.0
      x1, y1, x2, y2 = [int(v) for v in bbox]
      x1 = max(0, x1); y1 = max(0, y1)
      x2 = min(frame.shape[1], x2); y2 = min(frame.shape[0], y2)
      bw, bh = x2 - x1, y2 - y1
      if bw < MIN_BBOX_SIDE or bh < MIN_BBOX_SIDE:
          return 0.0

      crop = frame[y1:y2, x1:x2]
      gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
      lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

      # Center-weight: 1.0 if bbox center is in upper-middle third of frame, 0.8 otherwise
      cx = (x1 + x2) / 2.0
      cy = (y1 + y2) / 2.0
      fh, fw = frame.shape[:2]
      in_upper_middle = (fw * 0.25 < cx < fw * 0.75) and (fh * 0.15 < cy < fh * 0.55)
      center_boost = 1.0 if in_upper_middle else 0.8

      # Size boost — larger bbox linearly better up to 300px
      size_boost = min(1.0, min(bw, bh) / 300.0)

      conf = max(0.1, float(detector_conf or 0))  # floor so conf=0 doesn't zero everything

      return lap_var * center_boost * (0.5 + 0.5 * size_boost) * conf
  ```

- [ ] **Step 2.4: Tests pass**

  ```bash
  ./venv-coral/bin/python -m pytest tests/test_hires_ring.py -v
  ```
  Expected: 7 passed.

---

### Task 3: Wire the ring into pipeline

**Files:**
- Modify: `~/bird-classifier/bird_pipeline_v3.py` — add a FrameCapture for `feeder-main` and a HiResRingBuffer; pass buffer into SnapshotWriter.
- Modify: `~/bird-classifier/pipeline/snapshot_writer.py` — accept `hires_ring` kwarg; in `_write_one`, prefer ring over `_fetch_hires_frame`.

- [ ] **Step 3.1: Thread the ring through SnapshotWriter**

  In `pipeline/snapshot_writer.py`, update `__init__`:
  ```python
      def __init__(self, maxsize: int = 32, classifier=None,
                   go2rtc_url: str = "http://localhost:1984",
                   hires_ring=None,
                   shadow_mode: bool = True):
          # ... existing code ...
          self.hires_ring = hires_ring
          self.shadow_mode = shadow_mode  # if True, ring pick goes to sidecar only
          self.stats.update({
              "ring_pick_ok": 0,
              "ring_pick_empty": 0,
              "shadow_disagree": 0,
          })
  ```

  Add a helper:
  ```python
      def _pick_from_ring(self, camera, wall_time_ms, bbox_scaled, detector_conf):
          """Return (frame, pick_meta) or (None, reason).

          Queries the ring for K nearest-timestamp frames, scores each against
          the bbox (scaled to hi-res coords), returns the best.
          """
          if self.hires_ring is None:
              return None, "no_ring"
          from pipeline.hires_ring import score_frame
          cands = self.hires_ring.find_candidates(wall_time_ms, k=5)
          if not cands:
              return None, "ring_empty"
          scored = []
          for c in cands:
              s = score_frame(c.frame, bbox_scaled, detector_conf)
              scored.append((s, c))
          scored.sort(key=lambda sc: sc[0], reverse=True)
          best_score, best = scored[0]
          meta = {
              "picked_wall_ms": best.wall_ms,
              "picked_score": best_score,
              "candidates": [{"wall_ms": c.wall_ms, "score": s}
                             for s, c in scored],
              "requested_wall_ms": wall_time_ms,
          }
          if best_score <= 0:
              return None, "all_zero_score"
          return best.frame, meta
  ```

  In `_write_one`, replace the single `_fetch_hires_frame` call with:
  ```python
      # Scale bbox from detector (640x360) coords to hi-res (1920x1080) space.
      # We do this BEFORE picking from the ring because the scorer needs the
      # bbox in ring-frame coords.
      low_w = p["frame"].shape[1] or 1
      low_h = p["frame"].shape[0] or 1
      # Assume ring frames are 1920x1080. If ring is empty, fall back to the
      # existing fetch which produces whatever resolution go2rtc emits.
      HIRES_W, HIRES_H = 1920, 1080
      sx, sy = HIRES_W / low_w, HIRES_H / low_h
      bbox_scaled = [p["bbox"][0] * sx, p["bbox"][1] * sy,
                     p["bbox"][2] * sx, p["bbox"][3] * sy]

      ring_frame, ring_meta = self._pick_from_ring(
          p["camera"], p["wall_time_ms"], bbox_scaled,
          p.get("confidence", 0.0),
      )

      if ring_frame is not None and not self.shadow_mode:
          p["frame"] = ring_frame
          p["bbox"] = bbox_scaled
          self.stats["hires_ok"] += 1
          self.stats["ring_pick_ok"] += 1
      else:
          # Shadow mode OR ring empty: fall back to existing behavior.
          hires = self._fetch_hires_frame(p["camera"])
          if hires is not None:
              sx = hires.shape[1] / low_w
              sy = hires.shape[0] / low_h
              p["bbox"] = [p["bbox"][0] * sx, p["bbox"][1] * sy,
                           p["bbox"][2] * sx, p["bbox"][3] * sy]
              p["frame"] = hires
              self.stats["hires_ok"] += 1
          else:
              self.stats["hires_fail"] += 1
          if ring_frame is None:
              self.stats["ring_pick_empty"] += 1

      # Shadow-mode sidecar: record ring's pick next to the JPG, regardless.
      if ring_meta and self.shadow_mode:
          p["_ring_sidecar"] = ring_meta
  ```

  And at the end of `_write_one`, after `cdb.insert_classification(entry)` succeeds, write the sidecar:
  ```python
      # Shadow sidecar — records what the new ring path WOULD have picked,
      # so David can eyeball against the actual written JPG to build trust
      # before we flip shadow_mode off.
      if p.get("_ring_sidecar"):
          import json
          sidecar = out_path.with_suffix(".ring.json")
          try:
              sidecar.write_text(json.dumps(p["_ring_sidecar"], indent=2))
          except Exception as e:
              log.debug("ring sidecar write failed: %s", e)
  ```

- [ ] **Step 3.2: Wire up in bird_pipeline_v3.py**

  Find where `SnapshotWriter` is instantiated (grep `SnapshotWriter(`). Before it:
  ```python
      from pipeline.hires_ring import HiResRingBuffer
      hires_ring = HiResRingBuffer(max_seconds=2.0, expected_fps=5.0)
  ```

  Spawn a new `FrameCapture` for `feeder-main`:
  ```python
      from pipeline.frame_capture import FrameCapture   # adjust import to codebase
      hires_capture = FrameCapture(
          stream="feeder-main",
          fps=5,
          size=(1920, 1080),   # confirm arg names match FrameCapture signature
          on_frame=lambda frame, wall_ms: hires_ring.push(frame, wall_ms),
      )
      hires_capture.start()
  ```

  Pass to SnapshotWriter:
  ```python
      snapshot_writer = SnapshotWriter(
          classifier=smart_classifier,
          hires_ring=hires_ring,
          shadow_mode=True,   # 3-4 days of sidecar, then flip
      )
  ```

  > Note: the exact `FrameCapture` signature may differ from the guessed one above. Before running, grep `class FrameCapture` in the codebase and adjust `on_frame` / `size` / `fps` kwargs to the real ones. If `FrameCapture` does not expose a frame callback, add one (smallest change: emit to a queue the ring consumes in its own thread). Capture the exact signature in the commit message so the behavior is traceable.

- [ ] **Step 3.3: Restart pipeline, verify shadow mode populates sidecars**

  ```bash
  launchctl unload ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
  launchctl load ~/Library/LaunchAgents/com.vives.bird-pipeline.plist
  sleep 30
  # Check that ring is accumulating
  curl -sS http://localhost:8100/api/pipeline/health | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('shared',{}).get('snapshot_writer',{}))"
  # After a classification lands, check for sidecar
  ls ~/bird-snapshots/classified/*/*.ring.json 2>/dev/null | head -5
  ```
  Expected: `ring_pick_ok` climbing, sidecar JSONs appearing. If `ring_pick_empty` is dominant, the FrameCapture wiring isn't feeding the ring.

---

### Task 4: Soak for 3-4 days in shadow mode

- [ ] **Step 4.1: Build a spot-check tool**

  Create `~/bird-classifier/tools/compare_ring_pick.py` that for a random sample of 20 recent (row + sidecar) pairs:
  - Pulls the written JPG (what the old path produced)
  - Pulls the sidecar's `picked_wall_ms` vs `requested_wall_ms` delta
  - Prints a side-by-side summary David can eyeball

  Minimal version:
  ```python
  #!/usr/bin/env python3
  import json
  import random
  from pathlib import Path

  CLASSIFIED = Path.home() / "bird-snapshots" / "classified"
  sidecars = list(CLASSIFIED.rglob("*.ring.json"))
  print(f"Found {len(sidecars)} sidecars")
  sample = random.sample(sidecars, min(20, len(sidecars)))
  for s in sample:
      meta = json.loads(s.read_text())
      delta = meta["picked_wall_ms"] - meta["requested_wall_ms"]
      print(f"{s.name:50s}  Δt={delta:+7.0f}ms  score={meta['picked_score']:.1f}")
  ```

- [ ] **Step 4.2: David reviews a sample at day 2**

  Ask David to open the lightbox on 5-10 recent rows AND open the sidecar JSON next to each. Confirm: does the ring's picked frame (if ring had been authoritative) look better than what's actually displayed?

  Evidence gate: David's subjective confirmation that the ring pick is usually equal-or-better than the current written JPG. If the ring is worse in some pattern, diagnose before flipping.

---

### Task 5: Flip shadow_mode off

- [ ] **Step 5.1: Change `shadow_mode=True` to `shadow_mode=False`** in bird_pipeline_v3.py.

- [ ] **Step 5.2: Restart pipeline, verify `ring_pick_ok` now drives `hires_ok`.**

- [ ] **Step 5.3: Remove the `_fetch_hires_frame` fallback ONLY after 7 more days** of ring-authoritative flight. Until then, keep it as a safety net for empty-ring edge cases.

- [ ] **Step 5.4: Commit (done in stages)**

  Commit 1 (after Task 3): add ring in shadow mode.
  Commit 2 (after Task 4): spot-check tool.
  Commit 3 (after Task 5): flip shadow mode off.

---

## Self-review notes

- **Spec coverage:** ring (Task 1), scorer (Task 2), integration (Task 3), shadow (Task 4), flip (Task 5). Every requirement David named.
- **Placeholder scan:** Task 3.2 has an explicit note that FrameCapture's signature must be verified before running — that's a recognized uncertainty, flagged with action ("grep the signature; adjust kwargs"), not a TBD hiding rot.
- **Type consistency:** `RingFrame`, `score_frame`, `HiResRingBuffer.find_nearest/find_candidates`, `shadow_mode`, `ring_pick_ok/ring_pick_empty` stats names — all reused consistently.
- **Safety:** shadow mode for 3-4 days; sidecar audit trail; fallback to existing `_fetch_hires_frame` retained throughout initial rollout; old path removed only after second soak.
- **RAM budget:** 2s × 5 fps × 1920×1080×3 BGR ≈ 60 MB for frames + ffmpeg buffers ≈ 150 MB total. On an 8 GB iMac running pipeline + dashboard + go2rtc, that's ~2% of RAM.
