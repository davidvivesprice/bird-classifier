"""Tests for DebugStream WebSocket server."""
import time
import threading
import pytest


@pytest.fixture
def stream():
    from pipeline.debug_stream import DebugStream
    s = DebugStream(port=0)  # port 0 = OS-assigned for tests
    yield s
    try:
        s.stop()
    except Exception:
        pass


def test_push_updates_latest_frame(stream):
    """Pushing a frame stores it as the poster for that camera."""
    stream.push("feeder", b"\xff\xd8fake", 0)
    assert stream.latest_frame.get("feeder") == b"\xff\xd8fake"


def test_push_with_no_clients_no_errors(stream):
    """Pushing to a camera with no subscribed clients should not raise."""
    stream.push("ground", b"\xff\xd8x", 0)  # no clients connected
    assert stream.stats.get("frames_sent", 0) == 0


def test_stats_counters_exist(stream):
    assert "active_clients" in stream.stats
    assert "frames_sent" in stream.stats
    assert "dropped_clients" in stream.stats


def test_push_ignores_unknown_camera(stream):
    """Pushing to a camera not in the clients dict should not crash."""
    stream.push("unknown", b"\xff\xd8x", 0)
    # Should still store as latest_frame
    assert stream.latest_frame.get("unknown") == b"\xff\xd8x"
