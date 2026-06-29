"""Tests for FrameCapture (PyAV-based).

Rewritten 2026-06-29: the old tests targeted the removed ffmpeg-subprocess
implementation (_input_args / _spawn_ffmpeg / _restart). FrameCapture now
decodes via PyAV (av.open + a reader/watchdog loop). These cover the behaviors
that still matter: RTSP transport selection and the restart-rate counter.
"""
import queue
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


TEST_VIDEO = Path("/Users/vives/docs/bird-observatory/training videos/short-downy.mp4")


def _fc(url):
    from pipeline.frame_capture import FrameCapture
    return FrameCapture("test", url, out_queue=queue.Queue(maxsize=2),
                        capture_width=640, capture_height=360,
                        detect_width=640, detect_height=360)


def test_rtsp_open_uses_tcp_transport(monkeypatch):
    """rtsp:// inputs must open over TCP (UDP loses packets on this LAN)."""
    import pipeline.frame_capture as fc_mod
    captured = {}

    def fake_open(url, options=None, **kw):
        captured["url"] = url
        captured["options"] = options or {}
        return MagicMock()

    monkeypatch.setattr(fc_mod.av, "open", fake_open)
    _fc("rtsp://1.2.3.4/stream")._open_container()
    assert captured["url"] == "rtsp://1.2.3.4/stream"
    assert captured["options"].get("rtsp_transport") == "tcp"


def test_file_open_has_no_rtsp_options(monkeypatch):
    """Non-rtsp (file) inputs should not carry rtsp_transport options."""
    import pipeline.frame_capture as fc_mod

    def fake_open(url, options=None, **kw):
        fake_open.options = options or {}
        return MagicMock()

    monkeypatch.setattr(fc_mod.av, "open", fake_open)
    _fc("/tmp/fake.mp4")._open_container()
    assert "rtsp_transport" not in fake_open.options


def test_record_restart_counts_within_the_hour():
    """_record_restart increments the counter and restarts_last_hour reflects it."""
    fc = _fc("rtsp://1.2.3.4/stream")
    assert fc.restarts_last_hour() == 0
    fc._record_restart()
    fc._record_restart()
    assert fc.stats["ffmpeg_restarts"] == 2
    assert fc.restarts_last_hour() == 2


def test_record_restart_prunes_old_timestamps():
    """Restarts older than 1 hour drop out of the rolling window."""
    fc = _fc("rtsp://1.2.3.4/stream")
    fc._restart_timestamps.append(time.time() - 4000)  # >1h ago
    fc._record_restart()                                # now
    assert fc.restarts_last_hour() == 1                 # old one pruned


@pytest.mark.skipif(not TEST_VIDEO.exists(), reason="test video not available")
def test_capture_from_file_produces_frames():
    """Real PyAV run: capture from a test video file and verify frames arrive."""
    from pipeline.frame_capture import FrameCapture
    from pipeline.frame import Frame

    q = queue.Queue(maxsize=2)
    fc = FrameCapture("test", str(TEST_VIDEO), out_queue=q,
                      capture_width=1920, capture_height=1080,
                      detect_width=640, detect_height=360)
    try:
        fc.start()
        frame = q.get(timeout=5)
        assert isinstance(frame, Frame)
        assert frame.camera == "test"
        assert frame.wall_time_ms > 0
    finally:
        fc.stop()
