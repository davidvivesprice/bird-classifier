"""Tests for FrameAnnotator."""
import queue
import time
import numpy as np
from unittest.mock import MagicMock


def test_annotator_downscales_and_encodes():
    from pipeline.annotator import FrameAnnotator
    from pipeline.frame import Frame

    debug_stream = MagicMock()
    a = FrameAnnotator("feeder", debug_stream, out_width=960, out_height=540)
    a.start()

    frame = Frame(
        bgr=np.ones((1080, 1920, 3), dtype=np.uint8) * 128,
        wall_time_ms=0, camera="feeder", width=1920, height=1080,
    )
    a.submit(frame, tracks=[])
    time.sleep(0.3)  # let annotator thread run

    debug_stream.push.assert_called()
    args, kwargs = debug_stream.push.call_args
    camera, jpeg_bytes, _ = args
    assert camera == "feeder"
    assert isinstance(jpeg_bytes, bytes)
    assert jpeg_bytes.startswith(b"\xff\xd8")  # JPEG magic
    a.stop()


def test_annotator_drops_oldest_when_full():
    from pipeline.annotator import FrameAnnotator
    from pipeline.frame import Frame

    debug_stream = MagicMock()
    # Make push slow to fill the queue
    debug_stream.push.side_effect = lambda *a, **k: time.sleep(0.3)

    a = FrameAnnotator("feeder", debug_stream, out_width=320, out_height=180)
    a.start()

    frame = Frame(
        bgr=np.ones((1080, 1920, 3), dtype=np.uint8) * 128,
        wall_time_ms=0, camera="feeder", width=1920, height=1080,
    )
    for _ in range(10):
        a.submit(frame, [])
    # Queue should never exceed maxsize
    assert a.queue.qsize() <= 2
    a.stop()


def test_muted_chip_for_unlabeled_track():
    """An unlabeled (species=None) track gets drawn with muted color, not skipped."""
    from pipeline.annotator import FrameAnnotator
    from pipeline.frame import Frame
    from pipeline.tracker import Track

    debug_stream = MagicMock()
    a = FrameAnnotator("feeder", debug_stream)
    bgr = np.ones((1080, 1920, 3), dtype=np.uint8) * 50
    frame = Frame(bgr=bgr, wall_time_ms=0, camera="feeder", width=1920, height=1080)
    unlabeled = Track(track_id=1, created_at_ms=0, last_updated_ms=0,
                      bbox=[400, 300, 500, 400], confidence=0.2, species=None)
    out_jpeg = a._annotate(frame.bgr, [unlabeled])
    assert isinstance(out_jpeg, bytes)
    assert len(out_jpeg) > 100  # non-trivial JPEG
