"""Rolling buffer of recent 1080p frames for nearest-timestamp lookup.

Used by SnapshotWriter to find the hi-res frame whose capture time matches
the detection's wall_time_ms, instead of waiting 2-5s for go2rtc to emit
the next keyframe (which lets the bird leave the bbox → stale-bbox
hallucination).

Plan: docs/superpowers/plans/2026-04-22-hires-ring-buffer.md
"""
from __future__ import annotations

import bisect
import threading
from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass
class RingFrame:
    frame: np.ndarray   # BGR, typically 1920x1080
    wall_ms: float


class HiResRingBuffer:
    """Thread-safe rolling buffer indexed by wall-clock ms.

    Eviction: any frame older than ``max_seconds`` behind the newest is dropped.
    Hard cap: ``max_seconds * expected_fps * 2`` — 2x headroom for clock jitter.
    """

    def __init__(self, max_seconds: float = 2.0, expected_fps: float = 5.0):
        self.max_ms = float(max_seconds * 1000.0)
        self.cap = max(4, int(max_seconds * expected_fps * 2))
        # Default tolerance for find_nearest: 2 frame-intervals at expected_fps.
        # At 5 fps that's 400 ms — snapshot crops from ±400 ms of the detection
        # are close enough; anything farther and we should fall through to the
        # go2rtc /api/frame.mp4 path rather than crop a far-off frame.
        self.default_tolerance_ms = 2.0 * (1000.0 / max(1.0, expected_fps))
        self._frames: list[RingFrame] = []  # sorted by wall_ms ascending
        self._times: list[float] = []        # parallel list for bisect
        self._lock = threading.Lock()

    def push(self, frame: np.ndarray, wall_ms: float) -> None:
        """Insert a frame. Frame is COPIED — caller may reuse its buffer."""
        with self._lock:
            # New frames arrive monotonically in practice; handle out-of-order too.
            if self._times and wall_ms < self._times[-1]:
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
            # Hard cap
            while len(self._times) > self.cap:
                self._times.pop(0)
                self._frames.pop(0)

    def find_nearest(self, wall_ms: float,
                     tolerance_ms: Optional[float] = None) -> Optional[RingFrame]:
        """Return the frame whose wall_ms is closest to the target.

        Returns None if the closest frame is more than ``tolerance_ms`` away,
        defaulting to the ring's ``max_ms`` (i.e., queries within the ring's
        time window return something; queries outside return None). The
        default matches the intent that "evicted = gone" — queries for an
        evicted timestamp shouldn't silently return a distant surviving frame.
        """
        if tolerance_ms is None:
            tolerance_ms = self.default_tolerance_ms
        with self._lock:
            if not self._times:
                return None
            idx = bisect.bisect_left(self._times, wall_ms)
            candidates = []
            if idx < len(self._times):
                candidates.append(idx)
            if idx > 0:
                candidates.append(idx - 1)
            best = min(candidates, key=lambda i: abs(self._times[i] - wall_ms))
            if abs(self._times[best] - wall_ms) > tolerance_ms:
                return None
            return self._frames[best]

    def find_candidates(self, wall_ms: float, k: int = 3) -> list[RingFrame]:
        """Return up to K frames closest in time to wall_ms. Unordered.

        Used by SnapshotWriter to score multiple candidates and pick the
        best-quality one, per the frame-quality-picker spec.
        """
        with self._lock:
            if not self._times:
                return []
            scored_idx = sorted(
                range(len(self._times)),
                key=lambda i: abs(self._times[i] - wall_ms),
            )
            return [self._frames[i] for i in scored_idx[:k]]

    def __len__(self) -> int:
        with self._lock:
            return len(self._frames)


# ── HiResCapture: fps-throttled ffmpeg → ring ─────────────────────────────


import logging  # noqa: E402
import subprocess  # noqa: E402
import time  # noqa: E402
import cv2  # noqa: E402

_log = logging.getLogger(__name__)
_FFMPEG = "/usr/local/bin/ffmpeg"
_WATCHDOG_STALL_MS = 15_000
_WATCHDOG_CHECK_S = 3.0


class HiResCapture:
    """Dedicated ffmpeg → HiResRingBuffer feeder for a main-stream camera.

    Differs from pipeline.FrameCapture in TWO ways:
    (1) Applies `-vf fps=N` to cap decode rate at the ring's expected fps.
        Native-rate decode of 1920x1080 at 30 fps is ~90% of a core on this
        system; fps-throttling to 5 drops that 6x. The `fps` filter's
        ≤200 ms pacing latency is harmless for the ring because the
        wall_time_ms is stamped at pipe-read (same cadence as the main
        pipeline's sub-stream capture), so nearest-timestamp lookup is
        self-consistent.
    (2) Pushes directly to a HiResRingBuffer instead of a Queue. No
        intermediate consumer thread needed.
    """

    def __init__(self, camera_name: str, rtsp_url: str,
                 ring: HiResRingBuffer,
                 width: int = 1920, height: int = 1080, fps: int = 5):
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.ring = ring
        self.width = width
        self.height = height
        self.fps = fps
        self.proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.stats = {
            "frames": 0,
            "ffmpeg_restarts": 0,
            "last_frame_ms": None,
        }

    def start(self):
        self._stop.clear()
        self._spawn_ffmpeg()
        self._reader_thread = threading.Thread(
            target=self._pipe_drain, name=f"hires-cap-{self.camera_name}",
            daemon=True,
        )
        self._reader_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name=f"hires-wd-{self.camera_name}",
            daemon=True,
        )
        self._watchdog_thread.start()

    def stop(self):
        self._stop.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3)
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=3)
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        self.proc = None

    def _spawn_ffmpeg(self):
        cmd = [
            _FFMPEG,
            "-loglevel", "warning",
            "-rtsp_transport", "tcp",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-rtsp_flags", "prefer_tcp",
            "-max_delay", "200000",   # μs; tighter than sub-stream since we don't need ultra-low latency
            "-i", self.rtsp_url,
            "-vf", f"scale={self.width}:{self.height},fps={self.fps}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        ]
        _log.info("[%s-hires] spawning ffmpeg: %s", self.camera_name, " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _pipe_drain(self):
        frame_bytes = self.width * self.height * 3
        while not self._stop.is_set():
            proc = self.proc
            if proc is None or proc.stdout is None:
                time.sleep(0.1)
                continue
            try:
                data = proc.stdout.read(frame_bytes)
            except Exception:
                time.sleep(0.5)
                continue
            if not data or len(data) != frame_bytes:
                time.sleep(0.1)
                continue
            arr = np.frombuffer(data, dtype=np.uint8).reshape(
                (self.height, self.width, 3)
            ).copy()
            wall_ms = time.time() * 1000
            self.ring.push(arr, wall_ms)
            self.stats["frames"] += 1
            self.stats["last_frame_ms"] = wall_ms

    def _watchdog(self):
        while not self._stop.is_set():
            try:
                time.sleep(_WATCHDOG_CHECK_S)
                if self._stop.is_set():
                    break
                last = self.stats.get("last_frame_ms")
                if last is None:
                    continue
                age_ms = (time.time() * 1000) - last
                if age_ms > _WATCHDOG_STALL_MS:
                    _log.warning("[%s-hires] stalled %.0fms, restarting",
                                 self.camera_name, age_ms)
                    proc = self.proc
                    if proc is not None:
                        try:
                            proc.kill()
                            proc.wait(timeout=3)
                        except Exception:
                            pass
                    self._spawn_ffmpeg()
                    self.stats["ffmpeg_restarts"] += 1
                    self.stats["last_frame_ms"] = time.time() * 1000
            except Exception:
                _log.exception("[%s-hires] watchdog error", self.camera_name)
                time.sleep(1.0)


# ── Quality scorer ─────────────────────────────────────────────────────────

MIN_BBOX_SIDE = 80   # px floor — smaller than this, can't see an eye


def score_frame(frame, bbox, detector_conf: float) -> float:
    """Quality score for a (frame, bbox) pair. Higher = better.

    Components (per David's 2026-04-22 spec):
    - Sharpness: Laplacian variance inside bbox. Anti-motion-blur proxy; a
      visible eye correlates with high-frequency detail.
    - Center weight: +20% if the bbox center is in the upper-middle third
      of the frame (where a perched bird's head usually is on this feeder).
    - Size: reject bboxes below 80x80 (can't see an eye that small). Above
      the floor, linear up to 300 px.
    - Detector confidence: multiplier. A sharp but low-confidence bbox
      shouldn't outrank a sharp high-confidence one.

    Returns 0.0 for invalid / too-small / out-of-frame bboxes.
    """
    if frame is None:
        return 0.0
    if not hasattr(frame, "size") or frame.size == 0:
        return 0.0
    if not hasattr(frame, "shape") or len(frame.shape) < 2:
        return 0.0

    x1, y1, x2, y2 = [int(v) for v in bbox]
    x1 = max(0, x1); y1 = max(0, y1)
    x2 = min(frame.shape[1], x2); y2 = min(frame.shape[0], y2)
    bw, bh = x2 - x1, y2 - y1
    if bw < MIN_BBOX_SIDE or bh < MIN_BBOX_SIDE:
        return 0.0

    crop = frame[y1:y2, x1:x2]
    if crop.size == 0:
        return 0.0
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    lap_var = float(cv2.Laplacian(gray, cv2.CV_64F).var())

    # Center weight: 1.0 if bbox center is in upper-middle third, else 0.8
    cx = (x1 + x2) / 2.0
    cy = (y1 + y2) / 2.0
    fh, fw = frame.shape[:2]
    in_upper_middle = (fw * 0.25 < cx < fw * 0.75) and (fh * 0.15 < cy < fh * 0.55)
    center_boost = 1.0 if in_upper_middle else 0.8

    # Size boost: linear up to 300 px
    size_boost = min(1.0, min(bw, bh) / 300.0)

    # Confidence floor so detector_conf=0 doesn't zero everything out
    conf = max(0.1, float(detector_conf or 0))

    return lap_var * center_boost * (0.5 + 0.5 * size_boost) * conf
