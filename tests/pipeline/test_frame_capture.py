"""Tests for FrameCapture."""
import os
import queue
import time
from pathlib import Path

import numpy as np
import pytest


TEST_VIDEO = Path("/Users/vives/docs/bird-observatory/training videos/short-downy.mp4")


def test_file_input_detection():
    """Non-rtsp URLs should use file input args (-re -stream_loop)."""
    from pipeline.frame_capture import FrameCapture
    q = queue.Queue(maxsize=2)
    fc = FrameCapture("test", "/tmp/fake.mp4", out_queue=q, width=640, height=480, fps=5)
    args = fc._input_args("/tmp/fake.mp4")
    assert "-re" in args
    assert "-stream_loop" in args
    assert "-i" in args
    assert args[-1] == "/tmp/fake.mp4"


def test_rtsp_input_detection():
    """rtsp:// URLs should use TCP transport."""
    from pipeline.frame_capture import FrameCapture
    q = queue.Queue(maxsize=2)
    fc = FrameCapture("test", "rtsp://1.2.3.4/stream", out_queue=q, width=640, height=480, fps=5)
    args = fc._input_args("rtsp://1.2.3.4/stream")
    assert "-rtsp_transport" in args
    assert "tcp" in args
    assert args[-1] == "rtsp://1.2.3.4/stream"


@pytest.mark.skipif(not TEST_VIDEO.exists(),
                    reason="test video not available")
def test_capture_from_file_produces_frames():
    """Real ffmpeg run: capture from a test video file and verify frames arrive."""
    from pipeline.frame_capture import FrameCapture
    from pipeline.frame import Frame

    q = queue.Queue(maxsize=2)
    fc = FrameCapture(
        camera_name="test",
        rtsp_url=str(TEST_VIDEO),
        out_queue=q,
        width=1920, height=1080, fps=5,
    )
    try:
        fc.start()
        # Wait up to 5 seconds for first frame
        frame = q.get(timeout=5)
        assert isinstance(frame, Frame)
        assert frame.bgr.shape == (1080, 1920, 3)
        assert frame.camera == "test"
        assert frame.wall_time_ms > 0
    finally:
        fc.stop()


@pytest.mark.skipif(not TEST_VIDEO.exists(),
                    reason="test video not available")
def test_drops_oldest_when_queue_full():
    """Fast producer + slow consumer → oldest frames dropped, newest kept."""
    from pipeline.frame_capture import FrameCapture

    q = queue.Queue(maxsize=2)
    fc = FrameCapture(
        camera_name="test",
        rtsp_url=str(TEST_VIDEO),
        out_queue=q,
        width=1920, height=1080, fps=5,
    )
    try:
        fc.start()
        # Don't drain the queue for 3 seconds — producer should drop oldest
        time.sleep(3)
        # Queue should have at most 2 frames (maxsize)
        assert q.qsize() <= 2
        # dropped_oldest counter should have incremented
        assert fc.stats["dropped_oldest"] > 0
    finally:
        fc.stop()


def test_watchdog_detects_dead_ffmpeg_before_first_frame(monkeypatch):
    """Regression: watchdog must restart ffmpeg even when it dies before
    the first frame is read.

    Bug observed 2026-04-25 on Pi 5: post-startup at 05:45 EDT, both the
    sub-stream and hi-res ffmpegs died ~5s after spawn (same window in
    which the HLS recorder also died and successfully respawned). The
    watchdog only fired on last_frame_ms-based stalls, so with
    last_frame_ms still None the loop skipped every iteration and the
    pipeline silently stalled for ~5 hours. Comparison case:
    pipeline.hls_recorder._watchdog_loop uses ``proc.poll() is not None``
    and recovered correctly.
    """
    import threading
    from unittest.mock import MagicMock

    from pipeline import frame_capture as fc_module

    # Speed up the watchdog so the test runs in <1s.
    monkeypatch.setattr(fc_module, "WATCHDOG_CHECK_S", 0.1)

    spawn_count = {"n": 0}

    def fake_spawn(self):
        spawn_count["n"] += 1
        proc = MagicMock()
        # Simulate ffmpeg that exited immediately (dead before first frame)
        proc.poll.return_value = 1
        proc.returncode = 1
        proc.stdout.read.return_value = b""
        proc.wait.return_value = 1
        self.proc = proc

    monkeypatch.setattr(fc_module.FrameCapture, "_spawn_ffmpeg", fake_spawn)

    q = queue.Queue(maxsize=2)
    fc = fc_module.FrameCapture("test", "rtsp://fake/", out_queue=q,
                                 width=64, height=64, fps=5)
    try:
        fc.start()
        # Allow at least 3 watchdog ticks (0.1s each + start lag)
        time.sleep(0.6)
    finally:
        fc.stop()

    assert spawn_count["n"] >= 2, (
        f"Watchdog must respawn ffmpeg that died before first frame; "
        f"got spawn_count={spawn_count['n']}, ffmpeg_restarts="
        f"{fc.stats['ffmpeg_restarts']}"
    )
    assert fc.stats["ffmpeg_restarts"] >= 1


def test_restart_resets_last_frame_ms():
    """_restart() must update last_frame_ms so the watchdog doesn't immediately re-fire."""
    import queue
    from pipeline.frame_capture import FrameCapture

    fc = FrameCapture.__new__(FrameCapture)
    fc.camera_name = "test"
    fc.rtsp_url = "rtsp://127.0.0.1:9999/nonexistent"
    fc.out_queue = queue.Queue(maxsize=2)
    fc.proc = None
    fc._stop_event = __import__("threading").Event()
    fc.stats = {
        "frames": 0, "dropped_oldest": 0, "ffmpeg_restarts": 0,
        "last_frame_ms": 1000.0,  # stale old value
    }
    fc.width = 640
    fc.height = 360
    fc.fps = 5

    # Monkeypatch _spawn_ffmpeg so it doesn't actually spawn anything
    fc._spawn_ffmpeg = lambda: None

    before = __import__("time").time() * 1000
    fc._restart()
    after = __import__("time").time() * 1000

    # Must have been reset to roughly "now"
    assert before <= fc.stats["last_frame_ms"] <= after, (
        f"last_frame_ms={fc.stats['last_frame_ms']} not in [{before}, {after}]"
    )
    assert fc.stats["ffmpeg_restarts"] == 1
