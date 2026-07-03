"""FrameCaptureProc: decode crashes must not kill the pipeline.

The real failure this guards: libav SEGVs inside av_read_frame (~80x/3days,
proven unfixable via av.logging config). With the decoder in a child process,
that SEGV becomes a ~2s frame gap the tracker coasts through — the parent,
tracker state, and all locks survive.
"""
import os
import queue
import signal
import time

import numpy as np
import pytest


@pytest.fixture(scope="module")
def tiny_video(tmp_path_factory):
    """~3s of 64x36 synthetic video (moving square) — loops in the child."""
    import av
    path = str(tmp_path_factory.mktemp("vid") / "tiny.mp4")
    out = av.open(path, "w")
    vs = out.add_stream("h264", rate=30)
    vs.width, vs.height = 64, 36
    vs.pix_fmt = "yuv420p"
    for i in range(90):
        img = np.zeros((36, 64, 3), dtype=np.uint8)
        x = (i * 2) % 56
        img[10:20, x:x + 8] = 255
        frame = av.VideoFrame.from_ndarray(img, format="bgr24")
        for pkt in vs.encode(frame):
            out.mux(pkt)
    for pkt in vs.encode():
        out.mux(pkt)
    out.close()
    return path


def _drain_one(q, timeout):
    return q.get(timeout=timeout)


def test_frames_flow_through_the_ring(tiny_video):
    from pipeline.frame_capture_proc import FrameCaptureProc
    q = queue.Queue(maxsize=2)
    cap = FrameCaptureProc("t", tiny_video, out_queue=q,
                           detect_width=64, detect_height=36)
    cap.start()
    try:
        f = _drain_one(q, timeout=20)
        assert f.bgr.shape == (36, 64, 3)
        assert f.camera == "t"
        assert isinstance(f.pts, float)
        f2 = _drain_one(q, timeout=10)
        assert f2.pts != f.pts or f2.wall_time_ms >= f.wall_time_ms
    finally:
        cap.stop()


def test_decode_child_segv_recovers(tiny_video):
    """Kill the decode child with SIGSEGV (the real crash signal): the parent
    must respawn it and frames must resume, without the parent dying."""
    from pipeline.frame_capture_proc import FrameCaptureProc
    q = queue.Queue(maxsize=2)
    cap = FrameCaptureProc("t", tiny_video, out_queue=q,
                           detect_width=64, detect_height=36)
    cap.start()
    try:
        _drain_one(q, timeout=20)                 # flowing
        pid = cap._child.pid
        os.kill(pid, signal.SIGSEGV)              # the libav failure mode
        kill_ms = time.time() * 1000

        # frames must RESUME (new child) within the supervise+backoff window
        deadline = time.time() + 25
        resumed = False
        while time.time() < deadline:
            try:
                f = _drain_one(q, timeout=2)
            except queue.Empty:
                continue
            if f.wall_time_ms > kill_ms + 200:    # decoded after the crash
                resumed = True
                break
        assert resumed, "frames did not resume after decode-child SEGV"
        assert cap.stats["child_respawns"] >= 1
        assert cap._child.pid != pid, "child was not respawned"
        assert cap.restarts_last_hour() >= 1
    finally:
        cap.stop()
