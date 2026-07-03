"""Process-isolated FrameCapture — decode crashes can no longer kill the pipeline.

WHY (2026-07-02 investigation): the pipeline SEGV-crashes inside libav
(av_vlog <- av_log <- av_read_frame on the decode thread; ~80 crashes/3 days,
bursty). Proven NOT fixable via any av.logging configuration — four callback
states tested (PyAV no-op default, PyAV Python callback, libav C default,
NULL via ctypes) and ALL crash identically: the fault is inside libav's own
read path, below anything Python can configure. Every crash restarted the
whole pipeline and wiped every track — a top cause of the live view "losing
the bird."

THE FIX: run PyAV in a CHILD process. The child decodes and writes
(frame, pts, wall_ms) into a shared-memory ring; the parent never links libav.
A decode SEGV now kills only the child: the supervisor respawns it (~1-2s
frame gap) and the tracker — whose coast (hit_counter_max=150 ≈ 5s) was built
for exactly such gaps — carries every track straight through. Same public
surface as pipeline.frame_capture.FrameCapture (out_queue of Frame objects,
stats dict incl. ffmpeg_restarts, restarts_last_hour(), start/stop), so
process_thread / bird_pipeline_v3 are unchanged.

Ring: N slots of (HxWx3 uint8 + meta). Single writer (child), single reader
(parent). The writer bumps a shared write-counter after filling a slot; the
reader consumes any counter advance. Torn frames are impossible to hand out
twice because the reader copies the slot before pushing downstream; a slot
overwritten mid-copy can only happen if the reader lags a full ring (N-1
frames) — with N=6 at 30fps that is 200ms of reader stall, and the outcome is
one visually-torn frame fed to a detector, not corruption of state. Accepted.

Set PIPELINE_DECODE_INPROC=1 to fall back to the old in-process decoder.
"""
from __future__ import annotations

import collections
import logging
import multiprocessing as mp
import os
import queue
import threading
import time
from multiprocessing import shared_memory
from typing import Optional

import numpy as np

from pipeline.frame import Frame

log = logging.getLogger(__name__)

RING_SLOTS = 6
CHILD_STALL_S = 10.0     # no new frame for this long -> kill + respawn child
SUPERVISE_TICK_S = 1.0
RESPAWN_BACKOFF_S = 1.0

# meta layout per slot (float64): [pts, wall_ms, width, height]
META_F64 = 4


def _child_main(rtsp_url: str, shm_name: str, meta_name: str,
                ctr_name: str, width: int, height: int,
                stop_ev, err_q) -> None:
    """Decode loop, runs in the child process. Everything libav lives here."""
    # The av_log guard is proven NOT to stop the SEGV, but it still silences
    # log spam; import order kept for consistency with frame_capture.
    import pipeline.av_log_guard  # noqa: F401
    import av
    import cv2

    shm = shared_memory.SharedMemory(name=shm_name)
    msh = shared_memory.SharedMemory(name=meta_name)
    csh = shared_memory.SharedMemory(name=ctr_name)
    ring = np.ndarray((RING_SLOTS, height, width, 3), dtype=np.uint8, buffer=shm.buf)
    meta = np.ndarray((RING_SLOTS, META_F64), dtype=np.float64, buffer=msh.buf)
    ctr = np.ndarray((1,), dtype=np.int64, buffer=csh.buf)

    def open_container():
        if rtsp_url.startswith("rtsp://"):
            options = {
                "rtsp_transport": "tcp",
                "fflags": "nobuffer",
                "flags": "low_delay",
                "rtsp_flags": "prefer_tcp",
                "max_delay": "100000",
            }
        else:
            options = {}
        return av.open(rtsp_url, options=options)

    while not stop_ev.is_set():
        container = None
        try:
            container = open_container()
            stream = container.streams.video[0]
            try:
                stream.codec_context.thread_count = 1
                stream.codec_context.thread_type = "NONE"
            except Exception:
                pass
            for av_frame in container.decode(stream):
                if stop_ev.is_set():
                    break
                img = av_frame.to_ndarray(format="bgr24")
                if img.shape[1] != width or img.shape[0] != height:
                    img = cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)
                pts = float(av_frame.time) if av_frame.time is not None else 0.0
                slot = int(ctr[0] + 1) % RING_SLOTS
                ring[slot] = img
                meta[slot] = (pts, time.time() * 1000.0, img.shape[1], img.shape[0])
                ctr[0] += 1          # publish AFTER the slot is fully written
        except Exception as e:
            try:
                err_q.put_nowait(f"decode session ended: {e}")
            except Exception:
                pass
            time.sleep(1.0)
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:
                    pass
    shm.close(); msh.close(); csh.close()


class FrameCaptureProc:
    """Drop-in FrameCapture with the decoder in a supervised child process."""

    def __init__(self, camera_name: str, rtsp_url: str,
                 out_queue: queue.Queue,
                 capture_width: int = 1920, capture_height: int = 1080,
                 detect_width: int = 640, detect_height: int = 360,
                 width: Optional[int] = None, height: Optional[int] = None,
                 fps: Optional[int] = None):
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
        self.width = detect_width
        self.height = detect_height
        self.out_queue = out_queue
        self.stats = {
            "frames": 0,
            "dropped_oldest": 0,
            "ffmpeg_restarts": 0,     # name kept for health-endpoint compat
            "last_frame_ms": None,
            "last_pts": None,
            "decode_errors": 0,
            "child_respawns": 0,
        }
        self._restart_timestamps: collections.deque = collections.deque()
        self._stop_event = threading.Event()
        self._reader_thread: Optional[threading.Thread] = None
        self._supervisor_thread: Optional[threading.Thread] = None
        self._child: Optional[mp.Process] = None
        self._mp = mp.get_context("spawn")   # never fork a Hailo/onnx parent
        self._child_stop = self._mp.Event()
        self._err_q = self._mp.Queue(maxsize=64)
        # shared ring (child writes at DETECT size — Pi single-stream path)
        w, h = detect_width, detect_height
        self._shm = shared_memory.SharedMemory(
            create=True, size=RING_SLOTS * h * w * 3)
        self._msh = shared_memory.SharedMemory(
            create=True, size=RING_SLOTS * META_F64 * 8)
        self._csh = shared_memory.SharedMemory(create=True, size=8)
        self._ring = np.ndarray((RING_SLOTS, h, w, 3), dtype=np.uint8,
                                buffer=self._shm.buf)
        self._meta = np.ndarray((RING_SLOTS, META_F64), dtype=np.float64,
                                buffer=self._msh.buf)
        self._ctr = np.ndarray((1,), dtype=np.int64, buffer=self._csh.buf)
        self._ctr[0] = -1
        self._consumed = -1

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        self._stop_event.clear()
        self._spawn_child()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"capring-{self.camera_name}", daemon=True)
        self._reader_thread.start()
        self._supervisor_thread = threading.Thread(
            target=self._supervise, name=f"capsup-{self.camera_name}", daemon=True)
        self._supervisor_thread.start()

    def stop(self):
        self._stop_event.set()
        self._child_stop.set()
        if self._child is not None and self._child.is_alive():
            self._child.join(timeout=3)
            if self._child.is_alive():
                self._child.kill()
        for t in (self._reader_thread, self._supervisor_thread):
            if t is not None:
                t.join(timeout=3)
        for s in (self._shm, self._msh, self._csh):
            try:
                s.close(); s.unlink()
            except Exception:
                pass

    def _spawn_child(self):
        self._child_stop = self._mp.Event()
        self._child = self._mp.Process(
            target=_child_main,
            args=(self.rtsp_url, self._shm.name, self._msh.name, self._csh.name,
                  self.detect_width, self.detect_height,
                  self._child_stop, self._err_q),
            daemon=True,
            name=f"decode-{self.camera_name}",
        )
        self._child.start()
        log.info("[%s] decode child spawned (pid=%s)", self.camera_name, self._child.pid)

    # ── reader: ring -> Frame -> out_queue ───────────────────────────────
    def _reader_loop(self):
        while not self._stop_event.is_set():
            latest = int(self._ctr[0])
            if latest <= self._consumed:
                time.sleep(0.005)
                continue
            # always jump to the newest published slot (drop-oldest semantics)
            if latest - self._consumed > 1:
                self.stats["dropped_oldest"] += latest - self._consumed - 1
            self._consumed = latest
            slot = latest % RING_SLOTS
            img = self._ring[slot].copy()
            pts, wall_ms, w, h = self._meta[slot]
            frame = Frame(
                bgr=img,
                wall_time_ms=float(wall_ms),
                camera=self.camera_name,
                width=int(w), height=int(h),
                pts=float(pts),
                bgr_full=img,
                full_width=int(w), full_height=int(h),
            )
            if self.out_queue.full():
                try:
                    self.out_queue.get_nowait()
                    self.stats["dropped_oldest"] += 1
                except queue.Empty:
                    pass
            try:
                self.out_queue.put_nowait(frame)
                self.stats["frames"] += 1
                self.stats["last_frame_ms"] = float(wall_ms)
                self.stats["last_pts"] = float(pts)
            except queue.Full:
                pass

    # ── supervisor: child death / stall -> respawn ───────────────────────
    def _supervise(self):
        while not self._stop_event.is_set():
            time.sleep(SUPERVISE_TICK_S)
            try:
                while True:
                    msg = self._err_q.get_nowait()
                    self.stats["decode_errors"] += 1
                    log.warning("[%s] %s", self.camera_name, msg)
            except Exception:
                pass
            dead = self._child is None or not self._child.is_alive()
            last = self.stats.get("last_frame_ms")
            stalled = (last is not None
                       and (time.time() * 1000 - last) / 1000.0 > CHILD_STALL_S)
            if dead or stalled:
                why = "died" if dead else f"stalled >{CHILD_STALL_S:.0f}s"
                code = self._child.exitcode if self._child is not None else None
                log.warning("[%s] decode child %s (exitcode=%s) — respawning",
                            self.camera_name, why, code)
                try:
                    self._child_stop.set()
                    if self._child is not None and self._child.is_alive():
                        self._child.kill()
                        self._child.join(timeout=2)
                except Exception:
                    pass
                if self._stop_event.is_set():
                    return
                time.sleep(RESPAWN_BACKOFF_S)
                self._record_restart()
                self.stats["child_respawns"] += 1
                # reset stall reference so we don't instantly re-fire
                self.stats["last_frame_ms"] = time.time() * 1000
                self._spawn_child()

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


def make_frame_capture(*args, **kwargs):
    """Factory: process-isolated by default; PIPELINE_DECODE_INPROC=1 reverts."""
    if os.environ.get("PIPELINE_DECODE_INPROC") == "1":
        from pipeline.frame_capture import FrameCapture
        return FrameCapture(*args, **kwargs)
    return FrameCaptureProc(*args, **kwargs)
