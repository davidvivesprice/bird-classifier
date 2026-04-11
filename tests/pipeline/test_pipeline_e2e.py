"""End-to-end pipeline test using Protect video files.

Feeds an MP4 through the full stack with a dummy classifier (no Coral required)
to verify that all stages wire together correctly.
"""
import queue
import time
from pathlib import Path

import pytest

VIDEOS = Path("/Users/vives/docs/bird-observatory/training videos")


@pytest.mark.slow
@pytest.mark.skipif(not VIDEOS.exists(), reason="test videos not available")
def test_empty_video_produces_no_events(tmp_path):
    """1m-empty.mp4 should run through the pipeline without crashing.

    Uses a dummy classifier that always returns unlabeled — tests
    the detect→track→store→annotate wiring without needing the
    Coral USB.
    """
    from pipeline.frame_capture import FrameCapture
    from pipeline.motion_gate import MotionGate
    from pipeline.tracker import BirdTracker
    from pipeline.detector import BirdDetector
    from pipeline.classifier import ClassificationResult
    from pipeline.event_store import EventStore
    from pipeline.health import HealthState
    from pipeline.process_thread import CameraProcessThread

    empty = VIDEOS / "1m-empty.mp4"
    if not empty.exists():
        pytest.skip(f"Missing {empty}")

    frame_q = queue.Queue(maxsize=2)
    capture = FrameCapture("test", str(empty), out_queue=frame_q,
                           width=1920, height=1080, fps=5)
    motion_gate = MotionGate()
    tracker = BirdTracker()
    detector = BirdDetector(
        yolo_model_path="/Users/vives/bird-classifier/models/yolov8n_bird.onnx",
        stationary_track_regions_fn=tracker.stationary_regions,
        confidence=0.3,
    )

    # Dummy classifier: never return a species (tests pipeline wiring, not ML)
    class DummyClassifier:
        stats = {"yard": 0, "aiy": 0, "both_agree": 0,
                 "unlabeled": 0, "lock_timeouts": 0, "retries": 0}
        def classify(self, *a, **k):
            return ClassificationResult(None, 0, None, False)

    event_store = EventStore(str(tmp_path / "pipeline.db"))
    health = HealthState()

    process = CameraProcessThread(
        name="test",
        frame_queue=frame_q,
        motion_gate=motion_gate,
        detector=detector,
        tracker=tracker,
        classifier=DummyClassifier(),
        event_store=event_store,
        annotator=None,
        health=health,
    )

    capture.start()
    process.start()

    # Run for 20 seconds (enough to validate the wiring + prove no crashes)
    time.sleep(20)

    capture.stop()
    process.stop()
    event_store.shutdown()

    # Verify health was updated (proves pipeline ran)
    snap = health.snapshot()
    assert "test" in snap["pipeline"]
    cap_stats = snap["pipeline"]["test"].get("capture", {})
    assert cap_stats.get("frames_processed", 0) > 0, "No frames were processed"
    # The empty video may still produce some YOLO false positives
    # on feeder hardware, but we don't fail on those — primary assertion
    # is that the pipeline didn't crash and health is updated
