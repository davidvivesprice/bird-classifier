"""FrameCapture — PyAV decoder + downscaler + queue.

FrameCapture decodes the configured detection input and exposes:
  - bgr_full: the decoded input frame
  - bgr: the detector-sized frame, downscaled when needed
  - pts: stream timestamp in seconds, the canonical clock

On the Pi production path, the configured input is the camera's native
640×360 substream, so bgr_full is also 640×360. SnapshotWriter uses the PTS
to recover the matching 1920×1080 frame from the main-stream HLS segmenter.

We use PyAV instead of subprocess+ffmpeg+rawvideo because rawvideo discards
PTS. PyAV exposes per-frame PTS as `frame.time` (seconds) and `frame.pts`
(stream-units), which is what we need for sync. PyAV uses the same libav
backend as ffmpeg — no behavior loss.

Why no `-vf fps=N` filter: the fps filter is a pacing buffer that holds a
decoded frame until its scheduled emission slot arrives (up to 1/N seconds).
That's variable wall-clock latency relative to camera capture, which is the
exact bug we're eliminating. Decode at native rate; let the consumer queue
drop oldest on backpressure.
"""
import collections
import logging
import queue
import threading
import time
from typing import Optional

import numpy as np
import cv2
import av

from pipeline.frame import Frame

log = logging.getLogger(__name__)

WATCHDOG_STALL_S = 10.0
WATCHDOG_CHECK_S = 2.0


class FrameCapture:
    def __init__(self, camera_name: str, rtsp_url: str,
                 out_queue: queue.Queue,
                 # Capture (full-res) dimensions — driven by the camera's main
                 # stream. We don't request a specific size; we accept whatever
                 # the stream delivers and record it for downscale targeting.
                 capture_width: int = 1920, capture_height: int = 1080,
                 # Detect dimensions — what motion gate / YOLO see. The
                 # downscale is done in-process via cv2.resize.
                 detect_width: int = 640, detect_height: int = 360,
                 # Legacy compatibility: callers used to pass `width=, height=`.
                 # Map those to capture dims for now.
                 width: Optional[int] = None, height: Optional[int] = None,
                 fps: Optional[int] = None):  # fps ignored — kept for compat
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        if width is not None:
            capture_width = width
        if height is not None:
            capture_height = height
        self.capture_width = capture_width
        self.capture_height = capture_height
        self.detect_width = detect_width
        self.detect_height = detect_height
        # Legacy aliases used elsewhere in the codebase (process_thread reads
        # capture.stats, MotionGate is told frame_width/height, etc.)
        self.width = detect_width
        self.height = detect_height
        self.out_queue = out_queue
        self._container: Optional[av.container.InputContainer] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._watchdog_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self.stats = {
            "frames": 0,
            "dropped_oldest": 0,
            "ffmpeg_restarts": 0,    # name kept for health-endpoint compat
            "last_frame_ms": None,
            "last_pts": None,
            "decode_errors": 0,
        }
        self._restart_timestamps: collections.deque = collections.deque()

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self):
        self._stop_event.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"cap-{self.camera_name}", daemon=True,
        )
        self._reader_thread.start()
        self._watchdog_thread = threading.Thread(
            target=self._watchdog, name=f"watchdog-{self.camera_name}", daemon=True,
        )
        self._watchdog_thread.start()

    def stop(self):
        self._stop_event.set()
        self._close_container()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3)
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=3)

    def _close_container(self):
        c = self._container
        self._container = None
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    # ── input args (also used by tests for file inputs) ─────────────────────

    def _open_container(self) -> "av.container.InputContainer":
        if self.rtsp_url.startswith("rtsp://"):
            options = {
                "rtsp_transport": "tcp",
                "fflags": "nobuffer",
                "flags": "low_delay",
                "rtsp_flags": "prefer_tcp",
                "max_delay": "100000",  # μs
            }
        else:
            # Local file — let PyAV pick defaults
            options = {}
        return av.open(self.rtsp_url, options=options)

    # ── main loop ───────────────────────────────────────────────────────────

    def _reader_loop(self):
        """Open the stream, decode forever, push Frames to the out_queue.

        On any container/decoder error: close, sleep, watchdog will restart us.
        Restart is implemented by exiting the loop and being re-spawned by
        watchdog (simpler than nesting another loop here).
        """
        while not self._stop_event.is_set():
            try:
                self._decode_session()
            except Exception as e:
                self.stats["decode_errors"] += 1
                log.warning("[%s] decode session ended: %s", self.camera_name, e)
            self._close_container()
            if self._stop_event.is_set():
                break
            # Brief backoff before reopening; watchdog will also nudge.
            time.sleep(1.0)
            self._record_restart()

    def _decode_session(self):
        container = self._open_container()
        self._container = container
        try:
            stream = container.streams.video[0]
            # Pin libavcodec to a single decode thread. Default `thread_count=auto`
            # spawns 3-4 slice-decoder workers per container (Track B audit
            # 2026-05-11 caught them at 20-30% of a core each, ~80% of a core
            # total). At substream resolution (640×360) one thread is plenty
            # for 30fps. Set BEFORE the first decode() call.
            try:
                stream.codec_context.thread_count = 1
                stream.codec_context.thread_type = "NONE"
            except Exception as e:
                log.warning("[%s] could not pin decoder threads=1: %s", self.camera_name, e)
            log.info(
                "[%s] PyAV stream open: %sx%s codec=%s rate=%s time_base=%s threads=%s",
                self.camera_name, stream.width, stream.height,
                stream.codec_context.name, stream.average_rate, stream.time_base,
                stream.codec_context.thread_count,
            )
            for av_frame in container.decode(stream):
                if self._stop_event.is_set():
                    break
                self._handle_frame(av_frame)
        finally:
            self._close_container()

    def _handle_frame(self, av_frame):
        # Convert to BGR ndarray. PyAV's `to_ndarray(format='bgr24')` performs
        # the YUV→BGR conversion in libav (fast, vectorized).
        try:
            bgr_full = av_frame.to_ndarray(format="bgr24")
        except Exception as e:
            self.stats["decode_errors"] += 1
            log.debug("[%s] frame convert failed: %s", self.camera_name, e)
            return

        h, w = bgr_full.shape[:2]
        # Downscale for the detect path. With the substream/main streams
        # already matching detect res (640×360), this branch is a no-op
        # in normal operation — the `else` returns bgr_full directly.
        # The resize remains as a fallback for demo sources / future
        # configs that don't match. INTER_LINEAR > INTER_AREA on CPU
        # cost; quality difference is invisible to YOLO at this scale.
        if (w, h) != (self.detect_width, self.detect_height):
            bgr_detect = cv2.resize(
                bgr_full, (self.detect_width, self.detect_height),
                interpolation=cv2.INTER_LINEAR,
            )
        else:
            bgr_detect = bgr_full

        # PTS in seconds. `av_frame.time` already accounts for time_base.
        # Falls back to 0 only if pts is None (rare, malformed stream).
        pts_s = float(av_frame.time) if av_frame.time is not None else 0.0
        wall_ms = time.time() * 1000

        frame = Frame(
            bgr=bgr_detect,
            wall_time_ms=wall_ms,
            camera=self.camera_name,
            width=self.detect_width,
            height=self.detect_height,
            pts=pts_s,
            bgr_full=bgr_full,
            full_width=w,
            full_height=h,
        )

        # Drop oldest if queue full. The processor sets the pace; capture
        # never blocks on it.
        if self.out_queue.full():
            try:
                self.out_queue.get_nowait()
                self.stats["dropped_oldest"] += 1
            except queue.Empty:
                pass
        try:
            self.out_queue.put_nowait(frame)
            self.stats["frames"] += 1
            self.stats["last_frame_ms"] = wall_ms
            self.stats["last_pts"] = pts_s
        except queue.Full:
            pass

    # ── watchdog (compat with prior FrameCapture API) ───────────────────────

    def _watchdog(self):
        while not self._stop_event.is_set():
            try:
                time.sleep(WATCHDOG_CHECK_S)
                if self._stop_event.is_set():
                    break
                last = self.stats.get("last_frame_ms")
                if last is None:
                    continue
                age_s = (time.time() * 1000 - last) / 1000.0
                if age_s > WATCHDOG_STALL_S:
                    log.warning(
                        "[%s] decoder stalled %.1fs, forcing reopen",
                        self.camera_name, age_s,
                    )
                    # Close container; the reader loop will exit its decode
                    # iterator with an error, sleep, and re-open.
                    self._close_container()
                    # Reset the age so we don't re-fire immediately.
                    self.stats["last_frame_ms"] = time.time() * 1000
            except Exception:
                log.exception("[%s] watchdog error", self.camera_name)
                time.sleep(1.0)

    def _record_restart(self):
        now = time.time()
        self._restart_timestamps.append(now)
        self.stats["ffmpeg_restarts"] += 1
        self._prune_restart_window(now)

    def _prune_restart_window(self, now_s: float) -> None:
        cutoff = now_s - 3600
        while self._restart_timestamps and self._restart_timestamps[0] < cutoff:
            self._restart_timestamps.popleft()

    def restarts_last_hour(self) -> int:
        self._prune_restart_window(time.time())
        return len(self._restart_timestamps)
