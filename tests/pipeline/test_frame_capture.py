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
