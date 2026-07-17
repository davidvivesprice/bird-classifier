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

2026-07-03 EXTENSION — detection moves into the same child. After decode
isolation went live, the parent kept SEGV-ing: fresh cores put the fault in
libhailort 4.23 (the SECOND native crasher, distinct from libav — both fire
under sustained demo load). With hef_path set, the child also runs the
HailoDetector per decoded frame and ships detections through the ring
(fixed-size float32 block per slot); the parent uses PrecomputedDetector — a
shim honoring the BirdDetector interface — so process_thread is unchanged.
One supervised cage for BOTH native crashers; the parent process keeps only
pure-Python + CPU-onnx state (tracker, classifier, SSE) and survives.
Child respawn re-inits the Hailo VDevice (~2-4s) — within tracker coast.
NOTE: while the child owns the Hailo VDevice, Model-Lab HAILO classifiers
cannot load in the parent (CPU models like the active aiy_onnx are fine).
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

# meta layout per slot (float64): [pts, wall_ms, width, height, ndet, det_ms]
META_F64 = 6
# detections per slot: (MAX_DETS, 5) float32 = [x1, y1, x2, y2, confidence]
MAX_DETS = 16
# ctr layout (int64): [0] = write counter (child), [1] = idle flag (parent).
CTR_I64 = 2
# Idle-scan cadence (r4 thermal lever): when the parent reports no active
# tracks, the child still decodes every frame (H.264 needs it) but runs
# conversion + Hailo detection + ring publish only every Nth frame — the
# effective scan rate drops 30fps -> ~15fps at the default stride of 2.
# >99.9% of daylight frames are empty, so this halves the dominant heat
# source for most of the day; the instant a detection produces an active
# track the parent flips the flag and full rate resumes on the next frame.
IDLE_STRIDE = max(1, int(os.environ.get("PIPELINE_IDLE_STRIDE", "2")))


def _child_main(rtsp_url: str, shm_name: str, meta_name: str,
                ctr_name: str, det_name: str, width: int, height: int,
                hef_path: Optional[str], det_confidence: float,
                stop_ev, err_q, idle_stride: int = 1) -> None:
    """Decode (+ optionally Hailo-detect) loop, in the child process.
    Everything libav AND everything libhailort lives here."""
    # The av_log guard is proven NOT to stop the SEGV, but it still silences
    # log spam; import order kept for consistency with frame_capture.
    import pipeline.av_log_guard  # noqa: F401
    import av
    import cv2

    detector = None
    if hef_path:
        from pipeline.hailo_detector import HailoDetector
        detector = HailoDetector(hef_path=hef_path, confidence=det_confidence)

    # track=False: the parent owns (and unlinks) the segments. Without it,
    # Python 3.13's per-process resource tracker also registers them in the
    # child and complains/unlinks at child exit — noisy at best, racy at
    # worst. (kwarg exists only on 3.13+; Pi runs 3.13, dev Macs may not.)
    import sys as _sys
    _attach = ({"track": False} if _sys.version_info >= (3, 13) else {})
    shm = shared_memory.SharedMemory(name=shm_name, **_attach)
    msh = shared_memory.SharedMemory(name=meta_name, **_attach)
    csh = shared_memory.SharedMemory(name=ctr_name, **_attach)
    dsh = shared_memory.SharedMemory(name=det_name, **_attach)
    ring = np.ndarray((RING_SLOTS, height, width, 3), dtype=np.uint8, buffer=shm.buf)
    meta = np.ndarray((RING_SLOTS, META_F64), dtype=np.float64, buffer=msh.buf)
    ctr = np.ndarray((CTR_I64,), dtype=np.int64, buffer=csh.buf)
    dets = np.ndarray((RING_SLOTS, MAX_DETS, 5), dtype=np.float32, buffer=dsh.buf)

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

    frame_i = 0
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
                frame_i += 1
                # Idle-scan cadence: parent sets ctr[1]=1 when no tracks are
                # active; skip conversion+detect+publish on strided frames.
                # Decode itself must still run every frame (P-frames).
                if idle_stride > 1 and ctr[1] != 0 and frame_i % idle_stride:
                    continue
                img = av_frame.to_ndarray(format="bgr24")
                if img.shape[1] != width or img.shape[0] != height:
                    img = cv2.resize(img, (width, height), interpolation=cv2.INTER_AREA)
                pts = float(av_frame.time) if av_frame.time is not None else 0.0
                ndet, det_ms = 0, 0.0
                slot = int(ctr[0] + 1) % RING_SLOTS
                if detector is not None:
                    t0 = time.monotonic()
                    try:
                        found = detector.detect(img)
                    except Exception as e:   # Python-level errors: skip frame's dets
                        found = []
                        try:
                            err_q.put_nowait(f"detect error: {e}")
                        except Exception:
                            pass
                    det_ms = (time.monotonic() - t0) * 1000.0
                    ndet = min(len(found), MAX_DETS)
                    for i in range(ndet):
                        b = found[i].box
                        dets[slot, i] = (b[0], b[1], b[2], b[3], found[i].confidence)
                ring[slot] = img
                meta[slot] = (pts, time.time() * 1000.0, img.shape[1], img.shape[0],
                              float(ndet), det_ms)
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
    # Clean teardown: drop the numpy views first (close() raises BufferError
    # while exported views are alive), then close all FOUR segments (dsh was
    # previously omitted). Never unlink here — the parent owns the segments.
    del ring, meta, ctr, dets
    for s in (shm, msh, csh, dsh):
        try:
            s.close()
        except Exception:
            pass


class FrameCaptureProc:
    """Drop-in FrameCapture with the decoder in a supervised child process."""

    def __init__(self, camera_name: str, rtsp_url: str,
                 out_queue: queue.Queue,
                 capture_width: int = 1920, capture_height: int = 1080,
                 detect_width: int = 640, detect_height: int = 360,
                 width: Optional[int] = None, height: Optional[int] = None,
                 fps: Optional[int] = None,
                 hef_path: Optional[str] = None,
                 det_confidence: float = 0.3):
        self.camera_name = camera_name
        self.rtsp_url = rtsp_url
        self.hef_path = hef_path
        self.det_confidence = det_confidence
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
        self._last_child_err_ms = 0.0
        # shm ring is allocated per start() cycle (_alloc_ring) and unlinked
        # in stop(). It used to be allocated ONCE here while stop() unlinked
        # it — so the nighttime pause destroyed the segments and the dawn
        # resume reused their dead names: the fresh child died with
        # FileNotFoundError('/psm_...') and the parent's reader SEGV'd on its
        # dangling view. That was the deterministic every-morning crash.
        self._shm = self._msh = self._csh = self._dsh = None
        self._ring = self._meta = self._ctr = self._dets = None
        self._consumed = -1

    def _alloc_ring(self):
        """Create the four shm segments + numpy views for one start/stop
        cycle (child writes at DETECT size — Pi single-stream path)."""
        w, h = self.detect_width, self.detect_height
        self._shm = shared_memory.SharedMemory(
            create=True, size=RING_SLOTS * h * w * 3)
        self._msh = shared_memory.SharedMemory(
            create=True, size=RING_SLOTS * META_F64 * 8)
        self._csh = shared_memory.SharedMemory(create=True, size=CTR_I64 * 8)
        self._dsh = shared_memory.SharedMemory(
            create=True, size=RING_SLOTS * MAX_DETS * 5 * 4)
        self._ring = np.ndarray((RING_SLOTS, h, w, 3), dtype=np.uint8,
                                buffer=self._shm.buf)
        self._meta = np.ndarray((RING_SLOTS, META_F64), dtype=np.float64,
                                buffer=self._msh.buf)
        self._ctr = np.ndarray((CTR_I64,), dtype=np.int64, buffer=self._csh.buf)
        self._dets = np.ndarray((RING_SLOTS, MAX_DETS, 5), dtype=np.float32,
                                buffer=self._dsh.buf)
        self._ctr[0] = -1
        self._ctr[1] = 0         # idle flag: parent-written, child-read
        self._consumed = -1

    def _dispose_ring(self):
        """Drop views, then close+unlink the segments. Views must go first:
        SharedMemory.close() raises BufferError while exports are alive."""
        self._ring = self._meta = self._ctr = self._dets = None
        for attr in ("_shm", "_msh", "_csh", "_dsh"):
            s = getattr(self, attr)
            if s is None:
                continue
            setattr(self, attr, None)
            try:
                s.close()
            except Exception:
                pass
            try:
                s.unlink()
            except Exception:
                pass

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self):
        self._stop_event.clear()
        self._alloc_ring()
        # Fresh stall reference: without this, the first supervisor tick after
        # a nighttime pause compares against the previous evening's last frame,
        # reads a >10s "stall", and kills the child it just spawned.
        self.stats["last_frame_ms"] = time.time() * 1000
        self._spawn_child()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"capring-{self.camera_name}", daemon=True)
        self._reader_thread.start()
        self._supervisor_thread = threading.Thread(
            target=self._supervise, name=f"capsup-{self.camera_name}", daemon=True)
        self._supervisor_thread.start()

    def stop(self):
        """Tear down child + threads AND this cycle's shm segments. The
        object itself stays reusable: start() allocates a fresh ring, so the
        nightly stop()/dawn start() cycle gets clean segments every time."""
        self._stop_event.set()
        self._child_stop.set()
        if self._child is not None and self._child.is_alive():
            self._child.join(timeout=3)
            if self._child.is_alive():
                self._child.kill()
                self._child.join(timeout=2)
        self._child = None
        for t in (self._reader_thread, self._supervisor_thread):
            if t is not None:
                t.join(timeout=3)
        self._reader_thread = self._supervisor_thread = None
        self._dispose_ring()

    def set_idle(self, idle: bool) -> None:
        """Parent hint from the process thread: True = no active tracks, the
        child may drop to idle-scan cadence (every IDLE_STRIDE-th frame);
        False = full rate. One shm int write; safe to call every frame."""
        ctr = self._ctr
        if ctr is None or IDLE_STRIDE <= 1:
            return
        val = 1 if idle else 0
        if int(ctr[1]) != val:
            ctr[1] = val
            log.debug("[%s] idle-scan %s (stride=%d)", self.camera_name,
                      "on" if idle else "off", IDLE_STRIDE)

    def _spawn_child(self):
        self._child_stop = self._mp.Event()
        self._child = self._mp.Process(
            target=_child_main,
            args=(self.rtsp_url, self._shm.name, self._msh.name, self._csh.name,
                  self._dsh.name, self.detect_width, self.detect_height,
                  self.hef_path, self.det_confidence,
                  self._child_stop, self._err_q, IDLE_STRIDE),
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
            pts, wall_ms, w, h, ndet, det_ms = self._meta[slot]
            frame = Frame(
                bgr=img,
                wall_time_ms=float(wall_ms),
                camera=self.camera_name,
                width=int(w), height=int(h),
                pts=float(pts),
                bgr_full=img,
                full_width=int(w), full_height=int(h),
            )
            if self.hef_path:
                # detections computed by the child ride the ring; the parent's
                # PrecomputedDetector shim hands them to process_thread.
                from pipeline.detector import Detection
                dets = self._dets[slot, :int(ndet)].copy()
                frame.precomputed_detections = [
                    Detection(box=[float(d[0]), float(d[1]), float(d[2]), float(d[3])],
                              confidence=float(d[4]))
                    for d in dets
                ]
                frame.det_ms = float(det_ms)
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
            # stop() may have run during the tick sleep — bail before the
            # dead-child check or every dusk pause logs a phantom "child died
            # — respawning" for the child stop() itself just tore down.
            if self._stop_event.is_set():
                return
            try:
                while True:
                    msg = self._err_q.get_nowait()
                    self.stats["decode_errors"] += 1
                    self._last_child_err_ms = time.time() * 1000
                    log.warning("[%s] %s", self.camera_name, msg)
            except Exception:
                pass
            dead = self._child is None or not self._child.is_alive()
            last = self.stats.get("last_frame_ms")
            stalled = (last is not None
                       and (time.time() * 1000 - last) / 1000.0 > CHILD_STALL_S)
            # "RTSP down" vs "decode wedged": a live child whose open/decode
            # loop is failing reports errors through err_q and already retries
            # av.open every 1s — SIGKILLing it just re-inits the Hailo VDevice
            # (~2-4s) every ~11s for zero benefit during a camera/go2rtc
            # outage. Only stall-kill a child that has gone SILENT (no frames
            # AND no error reports) — that's the truly wedged case.
            child_retrying = (not dead and
                              (time.time() * 1000 - self._last_child_err_ms)
                              / 1000.0 <= CHILD_STALL_S)
            if dead or (stalled and not child_retrying):
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
                # Re-check AFTER the backoff: a concurrent stop() may have
                # unlinked the segments during the sleep — spawning now would
                # hand the child dead shm names (FileNotFoundError loop).
                if self._stop_event.is_set() or self._shm is None:
                    return
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


class PrecomputedDetector:
    """Detector shim for process_thread: detections were already computed by
    the isolated child (riding the frame through the shared ring). Honors the
    BirdDetector interface; uses_motion_regions=False keeps the MOG2 bypass.
    NOTE: process_thread's own det-timing measures this no-op (~0ms); the true
    Hailo time is frame.det_ms (shipped from the child)."""
    uses_motion_regions = False

    def detect(self, frame, motion_regions=None, forced_full=False):
        return list(getattr(frame, "precomputed_detections", []) or [])


def make_frame_capture(*args, **kwargs):
    """Factory: process-isolated by default; PIPELINE_DECODE_INPROC=1 reverts
    (in-process decode; drops hef_path/det_confidence — the caller must then
    construct its own in-process detector)."""
    if os.environ.get("PIPELINE_DECODE_INPROC") == "1":
        from pipeline.frame_capture import FrameCapture
        kwargs.pop("hef_path", None)
        kwargs.pop("det_confidence", None)
        return FrameCapture(*args, **kwargs)
    return FrameCaptureProc(*args, **kwargs)
