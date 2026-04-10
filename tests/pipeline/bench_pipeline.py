"""Benchmark the full pipeline on a test video.

Realistic thresholds for iMac hardware (CoreML ONNX Runtime):
- YOLO avg < 350ms (steady state)
- YOLO p99 < 1500ms (includes CoreML warmup spike)
- Capture FPS >= 4.5 (target 5)
- Zero ffmpeg restarts in 60s
- Peak memory < 500 MB

Does NOT use the Coral classifier (uses a dummy) so it runs while
the main classifier LaunchAgent is active.
"""
import queue
import time
import tracemalloc
from pathlib import Path

import pytest

TEST_VIDEO = Path("/Users/vives/docs/bird-observatory/training videos/chickadee-finch-downy.mp4")


@pytest.mark.slow
@pytest.mark.skipif(not TEST_VIDEO.exists(), reason="test video not available")
def test_benchmark_60s_run():
    """Run the full pipeline (sans Coral classifier) against a test video."""
    from pipeline.frame_capture import FrameCapture
    from pipeline.motion_gate import MotionGate
    from pipeline.tracker import BirdTracker
    from pipeline.detector import BirdDetector
    from pipeline.classifier import ClassificationResult
    from pipeline.event_store import EventStore
    from pipeline.annotator import FrameAnnotator
    from pipeline.debug_stream import DebugStream
    from pipeline.health import HealthState
    from pipeline.process_thread import CameraProcessThread

    frame_q = queue.Queue(maxsize=2)
    capture = FrameCapture("bench", str(TEST_VIDEO), out_queue=frame_q,
                           width=1920, height=1080, fps=5)
    motion_gate = MotionGate()
    tracker = BirdTracker()
    detector = BirdDetector(
        yolo_model_path="/Users/vives/bird-classifier/models/yolov8n_bird.onnx",
        stationary_track_regions_fn=tracker.stationary_regions,
        confidence=0.3,
    )

    class FastClassifier:
        stats = {"yard": 0, "aiy": 0, "both_agree": 0, "audio_confirmed": 0,
                 "unlabeled": 0, "lock_timeouts": 0, "retries": 0}
        def classify(self, crop, frame_time_ms, camera):
            return ClassificationResult("Black-capped Chickadee", 0.9, "yard", False)

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    event_store = EventStore(str(tmp / "pipeline.db"))
    debug_stream = DebugStream(port=0)
    annotator = FrameAnnotator("bench", debug_stream, out_width=960, out_height=540)
    health = HealthState()

    process = CameraProcessThread(
        name="bench", frame_queue=frame_q, motion_gate=motion_gate,
        detector=detector, tracker=tracker, classifier=FastClassifier(),
        event_store=event_store, annotator=annotator, health=health,
    )

    tracemalloc.start()
    capture.start()
    annotator.start()
    process.start()
    t_start = time.time()
    time.sleep(60)
    capture.stop()
    process.stop()
    annotator.stop()
    event_store.shutdown()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    elapsed = time.time() - t_start

    snap = health.snapshot()
    cam = snap["pipeline"].get("bench", {})
    detector_stats = cam.get("detector", {})
    yolo_p99 = detector_stats.get("yolo_ms_p99", 0)
    yolo_avg = detector_stats.get("yolo_ms_avg", 0)
    frames = capture.stats["frames"]
    restarts = capture.stats["ffmpeg_restarts"]
    fps = frames / elapsed if elapsed > 0 else 0

    print("\n=== Pipeline v2 Benchmark ===")
    print(f"Elapsed: {elapsed:.1f}s")
    print(f"Frames captured: {frames}")
    print(f"Capture FPS: {fps:.2f}")
    print(f"ffmpeg restarts: {restarts}")
    print(f"YOLO ms avg: {yolo_avg}")
    print(f"YOLO ms p99: {yolo_p99}")
    print(f"Peak memory: {peak / 1024 / 1024:.0f} MB")

    # Realistic thresholds for this hardware (iMac + CoreML ONNX Runtime):
    # - YOLO avg ~230ms steady-state, p99 includes CoreML warmup spike (~950ms)
    # - Capture runs at 5 fps regardless; process thread drops oldest when full,
    #   giving effective tracking at ~4.3 fps (still smooth)
    # - Memory baseline is tiny (~50 MB) — 500 MB ceiling is generous
    assert restarts == 0, f"ffmpeg restarted {restarts} times"
    assert yolo_avg < 350, f"YOLO avg too slow: {yolo_avg} ms (steady-state target <350)"
    assert yolo_p99 < 1500, f"YOLO p99 too slow: {yolo_p99} ms (warmup-inclusive target <1500)"
    assert frames >= 150, f"Not enough frames captured: {frames}"
    assert fps >= 4.5, f"Capture FPS too low: {fps:.2f}"
    assert peak < 500 * 1024 * 1024, f"Peak memory too high: {peak / 1024 / 1024:.0f} MB"
