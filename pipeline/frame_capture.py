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

    def _input_args(self, url: str) -> list:
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
