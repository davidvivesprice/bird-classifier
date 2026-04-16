"""FrameCapture — ffmpeg subprocess + pipe drain thread + watchdog.

Owns one ffmpeg subprocess per camera. Reads raw BGR frames from stdout
into a bounded queue. Drops oldest on backpressure. Restarts ffmpeg if
stalled for >10s.
"""
import collections
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
        self._restart_timestamps: collections.deque = collections.deque()

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
        # Wait for threads to exit their loops before killing the process.
        # They check _stop_event at the top of each iteration.
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=3)
        if self._watchdog_thread is not None:
            self._watchdog_thread.join(timeout=3)
        # Now kill whatever subprocess is current
        proc = self.proc
        if proc is not None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass
        self.proc = None

    def _input_args(self, url: str) -> list:
        if url.startswith("rtsp://"):
            return ["-rtsp_transport", "tcp", "-i", url]
        # File input — loop forever, real-time pacing
        return ["-re", "-stream_loop", "-1", "-i", url]

    def _spawn_ffmpeg(self):
        # IMPORTANT: we deliberately do NOT use a `-vf fps=N` filter here.
        # The fps filter paces output to N frames/sec — ffmpeg holds a decoded
        # frame until its scheduled emission slot arrives (up to 1/N seconds).
        # That wait shows up as wall-clock latency between camera capture and
        # pipe-read, which in turn makes SSE event wall_time_ms lag behind the
        # main-stream HLS frames for the same physical moment → overlay appears
        # behind the bird.
        #
        # Instead, ffmpeg outputs the native ~30fps here, and Python reads as
        # fast as YOLO/classification allows. The bounded output queue drops
        # oldest frames when full, so processing rate is determined by the
        # downstream consumer, not by artificial pacing. Every frame that
        # reaches Python gets wall_time_ms stamped at pipe-read = close to the
        # moment the camera's frame actually landed on the iMac.
        cmd = [
            FFMPEG,
            "-loglevel", "warning",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-rtsp_flags", "prefer_tcp",
            "-max_delay", "100000",  # μs; cap rtsp reorder buffer at 100ms
            *self._input_args(self.rtsp_url),
            "-vf", f"scale={self.width}:{self.height}",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "-",
        ]
        log.info("[%s] spawning ffmpeg: %s", self.camera_name, " ".join(cmd))
        self.proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def _pipe_drain(self):
        """Dedicated pipe reader. Only job: read frames as fast as possible.

        DO NOT add conditionals, processing, or blocking calls here.
        Any stall will cause ffmpeg pipe backpressure and RTSP disconnect.
        """
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
            try:
                time.sleep(WATCHDOG_CHECK_S)
                if self._stop_event.is_set():
                    break
                last = self.stats.get("last_frame_ms")
                if last is None:
                    continue
                age_ms = (time.time() * 1000) - last
                if age_ms > WATCHDOG_STALL_MS:
                    log.warning("[%s] ffmpeg stalled %.0fms, restarting",
                                self.camera_name, age_ms)
                    self._restart()
            except Exception:
                log.exception("[%s] watchdog error", self.camera_name)
                # Brief delay to avoid tight error loops, then continue
                time.sleep(1.0)

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
            now = time.time()
            self._restart_timestamps.append(now)
            self._prune_restart_window(now)
        except Exception as e:
            log.error("[%s] failed to respawn ffmpeg: %s", self.camera_name, e)
            # Leave self.proc as it was; watchdog will retry on next iteration

    def _prune_restart_window(self, now_s: float) -> None:
        cutoff = now_s - 3600
        while self._restart_timestamps and self._restart_timestamps[0] < cutoff:
            self._restart_timestamps.popleft()

    def restarts_last_hour(self) -> int:
        self._prune_restart_window(time.time())
        return len(self._restart_timestamps)
