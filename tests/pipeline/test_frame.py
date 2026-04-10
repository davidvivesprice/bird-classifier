"""Tests for Frame dataclass."""
import numpy as np
import pytest
import time


def test_frame_creation():
    """Frame holds a numpy BGR array and metadata."""
    from pipeline.frame import Frame
    bgr = np.zeros((1080, 1920, 3), dtype=np.uint8)
    f = Frame(
        bgr=bgr,
        wall_time_ms=1712700000000,
        camera="feeder",
        width=1920,
        height=1080,
    )
    assert f.bgr.shape == (1080, 1920, 3)
    assert f.wall_time_ms == 1712700000000
    assert f.camera == "feeder"


def test_frame_is_dataclass_like():
    """Frame should support attribute access without being a frozen dataclass
    (bgr is a numpy array — mutability is fine)."""
    from pipeline.frame import Frame
    f = Frame(
        bgr=np.zeros((10, 10, 3), dtype=np.uint8),
        wall_time_ms=0,
        camera="feeder",
        width=10,
        height=10,
    )
    assert hasattr(f, "bgr")
    assert hasattr(f, "wall_time_ms")
    assert hasattr(f, "camera")
    assert hasattr(f, "width")
    assert hasattr(f, "height")
