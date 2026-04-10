"""Pipe saturation test — validates highest-risk assumption.

Runs FrameCapture against a real test video for 60 seconds with a slow
consumer (one frame pulled every 400ms). Verifies:
- No ffmpeg restart from pipe backpressure
- dropped_oldest counter increments correctly
- last_frame_ms stays recent (pipe never stalls)
"""
import queue
import time
from pathlib import Path

import pytest

TEST_VIDEO = Path("/Users/vives/docs/bird-observatory/training videos/lots of birds.mp4")


@pytest.mark.slow
@pytest.mark.skipif(not TEST_VIDEO.exists(), reason="test video not available")
def test_pipe_saturation_60s():
    """60-second run with slow consumer — no ffmpeg restarts allowed."""
    from pipeline.frame_capture import FrameCapture

    q = queue.Queue(maxsize=2)
    fc = FrameCapture(
        camera_name="saturation",
        rtsp_url=str(TEST_VIDEO),
        out_queue=q,
        width=1920, height=1080, fps=5,
    )

    try:
        fc.start()
        # Wait for first frame
        first = q.get(timeout=10)
        assert first is not None

        # Simulate slow consumer — pull 1 frame per 400ms for 60 seconds
        start = time.time()
        consumed = 0
        while time.time() - start < 60:
            try:
                q.get(timeout=1.0)
                consumed += 1
            except queue.Empty:
                pass
            time.sleep(0.4)  # slow consumer

        elapsed = time.time() - start
        print(f"\n=== Pipe Saturation Results ===")
        print(f"Elapsed: {elapsed:.1f}s")
        print(f"Consumed: {consumed} frames")
        print(f"Produced: {fc.stats['frames']}")
        print(f"Dropped oldest: {fc.stats['dropped_oldest']}")
        print(f"ffmpeg restarts: {fc.stats['ffmpeg_restarts']}")
        last_age_ms = (time.time() * 1000) - fc.stats['last_frame_ms']
        print(f"Last frame age: {last_age_ms:.0f}ms")

        # CRITICAL: zero ffmpeg restarts (proves no backpressure stall)
        assert fc.stats["ffmpeg_restarts"] == 0, \
            "ffmpeg restarted during saturation test — pipe backpressure deadlock"

        # dropped_oldest should be dominant (producer way faster than consumer)
        assert fc.stats["dropped_oldest"] > 100, \
            "Expected many drops — slow consumer should have backed up"

        # Last frame should be recent (< 1 second old)
        assert last_age_ms < 1000, \
            f"Pipe is stale, last frame {last_age_ms:.0f}ms old"

        # Should have produced roughly 60 * 5 = 300 frames at target fps
        assert fc.stats["frames"] >= 200, \
            f"Frame production too low: {fc.stats['frames']} (expected >=200)"
    finally:
        fc.stop()
