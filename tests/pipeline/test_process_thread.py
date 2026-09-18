"""Tests for CameraProcessThread — the per-camera orchestrator."""
import queue
import time
import numpy as np
from unittest.mock import MagicMock


def test_process_thread_reads_frame_and_calls_pipeline():
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame

    frame_q = queue.Queue(maxsize=2)

    motion_gate = MagicMock()
    motion_gate.regions = MagicMock(return_value=[(0, 0, 100, 100)])

    detector = MagicMock()
    from pipeline.detector import Detection
    detector.detect = MagicMock(return_value=[
        Detection(box=[10, 10, 50, 50], confidence=0.9)
    ])

    from pipeline.tracker import Track, TrackerOutput
    track = Track(track_id=1, created_at_ms=0, last_updated_ms=0,
                  bbox=[10, 10, 50, 50], confidence=0.9)
    tracker = MagicMock()
    tracker.update = MagicMock(return_value=TrackerOutput(
        active=[track], new=[track], expired=[], frame_time_ms=0
    ))
    tracker.stationary_regions = MagicMock(return_value=[])
    tracker.tracks = {1: track}

    classifier = MagicMock()
    from pipeline.classifier import ClassificationResult
    classifier.classify = MagicMock(return_value=ClassificationResult(
        species="Test Bird", confidence=0.9, model_source="yard", should_retry=False
    ))
    classifier.stats = {"yard": 0}

    event_store = MagicMock()
    health = MagicMock()

    thread = CameraProcessThread(
        name="feeder",
        frame_queue=frame_q,
        motion_gate=motion_gate,
        detector=detector,
        tracker=tracker,
        classifier=classifier,
        event_store=event_store,
        health=health,
    )
    thread.start()

    frame = Frame(
        bgr=np.ones((480, 640, 3), dtype=np.uint8) * 128,
        wall_time_ms=1000,
        camera="feeder",
        width=640,
        height=480,
    )
    frame_q.put(frame)

    # Give the thread a moment to process
    time.sleep(0.3)

    # Verify pipeline was called
    motion_gate.regions.assert_called()
    detector.detect.assert_called()
    tracker.update.assert_called()
    classifier.classify.assert_called()
    event_store.write_event.assert_called()

    thread.stop()


def test_process_thread_survives_detector_exception():
    """An exception in the detector should not crash the thread."""
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame

    frame_q = queue.Queue(maxsize=2)
    motion_gate = MagicMock()
    motion_gate.regions.return_value = [(0,0,10,10)]
    detector = MagicMock()
    detector.detect.side_effect = RuntimeError("boom")
    tracker = MagicMock()
    from pipeline.tracker import TrackerOutput
    tracker.update.return_value = TrackerOutput(active=[], new=[], expired=[], frame_time_ms=0)
    tracker.stationary_regions.return_value = []
    tracker.tracks = {}
    classifier = MagicMock()
    classifier.stats = {}
    event_store = MagicMock()
    health = MagicMock()

    thread = CameraProcessThread(
        name="feeder", frame_queue=frame_q, motion_gate=motion_gate,
        detector=detector, tracker=tracker, classifier=classifier,
        event_store=event_store, health=health,
    )
    thread.start()

    frame = Frame(bgr=np.zeros((10,10,3), dtype=np.uint8),
                  wall_time_ms=1000, camera="feeder", width=10, height=10)
    frame_q.put(frame)
    time.sleep(0.3)

    # Thread should still be alive
    assert thread.is_alive()
    thread.stop()


def test_process_thread_retries_track_when_coral_busy():
    """If classifier returns should_retry=True, track stays needs_classification=True."""
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    from pipeline.tracker import Track, TrackerOutput
    from pipeline.detector import Detection
    from pipeline.classifier import ClassificationResult

    frame_q = queue.Queue(maxsize=2)
    motion_gate = MagicMock()
    motion_gate.regions.return_value = [(0,0,100,100)]
    detector = MagicMock()
    detector.detect.return_value = [Detection(box=[10,10,50,50], confidence=0.9)]

    # One track that stays "needs_classification" across updates
    track = Track(track_id=1, created_at_ms=0, last_updated_ms=0,
                  bbox=[10,10,50,50], confidence=0.9)
    tracker = MagicMock()
    tracker.update.return_value = TrackerOutput(
        active=[track], new=[track], expired=[], frame_time_ms=0
    )
    tracker.stationary_regions.return_value = []
    tracker.tracks = {1: track}

    # Classifier returns should_retry=True (Coral busy)
    classifier = MagicMock()
    classifier.classify.return_value = ClassificationResult(
        species=None, confidence=0, model_source=None, should_retry=True
    )
    classifier.stats = {}

    event_store = MagicMock()
    health = MagicMock()

    thread = CameraProcessThread(
        name="feeder", frame_queue=frame_q, motion_gate=motion_gate,
        detector=detector, tracker=tracker, classifier=classifier,
        event_store=event_store, health=health,
    )
    thread.start()

    frame = Frame(bgr=np.ones((480,640,3), dtype=np.uint8)*128,
                  wall_time_ms=1000, camera="feeder", width=640, height=480)
    frame_q.put(frame)
    time.sleep(0.3)

    # Track should still be flagged for classification (retry next frame)
    assert track.needs_classification is True
    # Attempts counter incremented
    assert track.classification_attempts == 1
    thread.stop()


def test_yolo_p99_uses_np_percentile_and_returns_none_for_few_samples():
    """p99 must be computed via np.percentile (not sorted slice hack) AND
    must return None when fewer than 10 samples are available."""
    import numpy as np
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()

    # Case A: only 3 samples → p99 must be None
    t._stats = {
        "frames_processed": 3, "detections": 0,
        "yolo_ms_samples": [50.0, 60.0, 70.0],
    }
    t.tracker = MagicMock()
    t.tracker.tracks = []
    t.tracker.stationary_regions.return_value = []
    t.classifier = MagicMock()
    t.classifier.stats = {}
    t.health = MagicMock()
    captured = {}
    def capture(camera, section, payload):
        captured[section] = payload
    t.health.update = capture

    frame = Frame(bgr=np.zeros((360, 640, 3), dtype=np.uint8),
                  wall_time_ms=time.time() * 1000,
                  camera="test", width=640, height=360)
    t._update_health(frame, det_ms=50.0)

    assert captured["detector"]["yolo_ms_p99"] is None, (
        f"with 3 samples p99 must be None, got {captured['detector']['yolo_ms_p99']}"
    )

    # Case B: 100 samples with a clean distribution → np.percentile(samples, 99) exact
    samples = [10.0] * 50 + [20.0] * 40 + [1000.0] * 10  # top 10% = 1000
    t._stats["yolo_ms_samples"] = list(samples)
    captured.clear()
    t._last_stats_compute = 0  # reset throttle so this call runs immediately
    t._update_health(frame, det_ms=10.0)
    expected = round(float(np.percentile(samples, 99)))
    assert captured["detector"]["yolo_ms_p99"] == expected, (
        f"yolo_ms_p99={captured['detector']['yolo_ms_p99']}, expected {expected}"
    )


def test_yolo_samples_excludes_skip_frames():
    """When motion_regions is empty and not forced_full, YOLO is skipped and
    that zero-cost timing must NOT be recorded in yolo_ms_samples."""
    import queue, threading, time
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()
    t._dry_run = False
    t._stats = {
        "frames_processed": 0,
        "detections": 0,
        "yolo_ms_samples": [],
        "yolo_runs_total": 0,
        "yolo_skipped_motion": 0,
    }
    t._last_forced_full = time.time()  # not due for forced full

    motion_gate = MagicMock()
    motion_gate.regions.return_value = []  # no motion

    detector = MagicMock()
    detector.detect.return_value = []  # empty fast-path

    tracker_out = MagicMock()
    tracker_out.new = []
    tracker_out.active = []
    tracker_out.expired = []
    tracker = MagicMock()
    tracker.update.return_value = tracker_out
    tracker.tracks = []
    tracker.stationary_regions.return_value = []

    classifier = MagicMock(); classifier.stats = {}
    event_store = MagicMock()
    health = MagicMock()

    t.motion_gate = motion_gate
    t.detector = detector
    t.tracker = tracker
    t.classifier = classifier
    t.event_store = event_store
    t.health = health

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="test", width=640, height=360,
    )

    for _ in range(5):
        t._process_frame(frame)

    assert t._stats["yolo_ms_samples"] == [], (
        f"Expected empty samples, got {t._stats['yolo_ms_samples']}"
    )
    assert t._stats["yolo_skipped_motion"] == 5
    assert t._stats["yolo_runs_total"] == 0


def test_write_track_summary_uses_per_track_frame_count():
    """write_track_summary must pass track.frame_count, not a global counter."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "test"
    t._stop = threading.Event()
    t._dry_run = False
    t._stats = {
        "frames_processed": 9999,  # deliberately big, not per-track
        "detections": 0,
        "yolo_ms_samples": [],
        "yolo_runs_total": 0,
        "yolo_skipped_motion": 0,
    }
    t._last_forced_full = time.time()

    fake_track = MagicMock()
    fake_track.frame_count = 42

    tracker_out = MagicMock()
    tracker_out.new = []
    tracker_out.active = []
    tracker_out.expired = [fake_track]

    motion_gate = MagicMock(); motion_gate.regions.return_value = []
    detector = MagicMock(); detector.detect.return_value = []
    tracker = MagicMock(); tracker.update.return_value = tracker_out
    tracker.tracks = []; tracker.stationary_regions.return_value = []
    classifier = MagicMock(); classifier.stats = {}
    event_store = MagicMock()
    health = MagicMock()

    t.motion_gate = motion_gate
    t.detector = detector
    t.tracker = tracker
    t.classifier = classifier
    t.event_store = event_store
    t.health = health

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="test", width=640, height=360,
    )
    t._process_frame(frame)

    event_store.write_track_summary.assert_called_once()
    call_kwargs = event_store.write_track_summary.call_args.kwargs
    assert call_kwargs["num_frames"] == 42, (
        f"expected num_frames=42 (per-track), got {call_kwargs['num_frames']}"
    )


def test_classifier_stats_reported_per_camera_not_global():
    """process_thread must pull only its own camera's slice of classifier stats."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._stats = {
        "frames_processed": 1, "detections": 0,
        "yolo_ms_samples": [10.0] * 15,  # ≥10 so p99 isn't None
        "yolo_runs_total": 15, "yolo_skipped_motion": 0,
    }

    classifier = MagicMock()
    classifier.stats = {
        "feeder": {"yard": 42, "aiy": 3, "unlabeled_call": 1,
                   "both_agree": 0, "lock_timeouts": 0},
        "ground": {"yard": 0, "aiy": 100, "unlabeled_call": 5,
                   "both_agree": 0, "lock_timeouts": 0},
    }
    t.classifier = classifier

    t.tracker = MagicMock()
    t.tracker.tracks = []
    t.tracker.stationary_regions.return_value = []

    captured = {}
    health = MagicMock()
    def fake_update(camera, section, payload):
        captured[(camera, section)] = payload
    health.update = fake_update
    t.health = health

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="feeder", width=640, height=360,
    )
    t._update_health(frame, det_ms=0.0)

    feeder_classifier_stats = captured[("feeder", "classifier")]
    assert feeder_classifier_stats["yard"] == 42
    assert feeder_classifier_stats["aiy"] == 3
    # Ground's aiy=100 must NOT leak into feeder's stats
    assert feeder_classifier_stats["aiy"] != 100
    # And ground's stats must NOT appear anywhere in the feeder update
    assert "ground" not in feeder_classifier_stats


def test_process_thread_emits_sse_event_for_active_tracks():
    """When a frame produces active tracks, process_thread must emit an SSE event
    with the expected shape: camera, wall_time_ms, tracks[] with bbox_center_x,
    frame_width, frame_height, species, model_source, is_locked, frame_count."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._dry_run = False
    t._stats = {
        "frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
        "yolo_runs_total": 0, "yolo_skipped_motion": 0,
    }
    t._last_forced_full = time.time() - 9999  # not forced

    # Fake track returned by tracker
    fake_track = MagicMock()
    fake_track.track_id = 7
    fake_track.bbox = [100, 50, 300, 200]
    fake_track.species = "Downy Woodpecker"
    fake_track.confidence = 0.9
    fake_track.model_source = "yard"
    fake_track.frame_count = 5
    fake_track.needs_classification = False
    fake_track.classification_attempts = 0
    fake_track.is_locked = True  # Phase 2: locked by vote consensus
    # pacing fields (2026-07-04 classification-lifecycle contract)
    fake_track.last_classify_fc = 5   # just verified — no re-verify this frame
    fake_track.no_vote_streak = 0
    fake_track.lock_disagreements = 0

    tracker_out = MagicMock()
    tracker_out.new = [fake_track]
    tracker_out.active = [fake_track]
    tracker_out.expired = []

    motion_gate = MagicMock()
    motion_gate.regions.return_value = [(0, 0, 640, 360)]
    detector = MagicMock()
    detector.detect.return_value = [MagicMock()]
    tracker = MagicMock()
    tracker.update.return_value = tracker_out
    tracker.tracks = [fake_track]
    tracker.stationary_regions.return_value = []
    classifier = MagicMock()
    classifier.stats = {"feeder": {"yard": 0, "aiy": 0}}
    event_store = MagicMock()
    health = MagicMock()

    sse_server = MagicMock()

    t.motion_gate = motion_gate
    t.detector = detector
    t.tracker = tracker
    t.classifier = classifier
    t.event_store = event_store
    t.health = health
    t.sse_server = sse_server
    t.frame_width = 640
    t.frame_height = 360

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=1_700_000_000_000,
        camera="feeder", width=640, height=360,
    )
    t._process_frame(frame)

    assert sse_server.emit.call_count == 1, (
        f"expected emit called once, got {sse_server.emit.call_count}"
    )
    call = sse_server.emit.call_args
    kwargs = call.kwargs
    assert kwargs["camera"] == "feeder"
    assert kwargs["wall_time_ms"] == 1_700_000_000_000
    tracks = kwargs["tracks"]
    assert len(tracks) == 1
    assert tracks[0]["track_id"] == 7
    assert tracks[0]["species"] == "Downy Woodpecker"
    assert tracks[0]["bbox"] == [100, 50, 300, 200]
    assert tracks[0]["bbox_center_x"] == 200  # (100 + 300) // 2
    assert tracks[0]["frame_width"] == 640
    assert tracks[0]["frame_height"] == 360
    assert tracks[0]["model_source"] == "yard"
    assert tracks[0]["is_locked"] is True  # Phase 2: locked by vote consensus
    assert tracks[0]["frame_count"] == 5


def test_process_thread_does_not_emit_when_no_active_tracks():
    """No active tracks → no SSE event (avoids spam)."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._dry_run = False
    t._stats = {
        "frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
        "yolo_runs_total": 0, "yolo_skipped_motion": 0,
    }
    t._last_forced_full = time.time()

    tracker_out = MagicMock()
    tracker_out.new = []
    tracker_out.active = []  # none
    tracker_out.expired = []

    motion_gate = MagicMock(); motion_gate.regions.return_value = []
    detector = MagicMock(); detector.detect.return_value = []
    tracker = MagicMock(); tracker.update.return_value = tracker_out
    tracker.tracks = []; tracker.stationary_regions.return_value = []
    classifier = MagicMock(); classifier.stats = {"feeder": {}}
    event_store = MagicMock()
    health = MagicMock()

    sse_server = MagicMock()

    t.motion_gate = motion_gate
    t.detector = detector
    t.tracker = tracker
    t.classifier = classifier
    t.event_store = event_store
    t.health = health
    t.sse_server = sse_server
    t.frame_width = 640
    t.frame_height = 360

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="feeder", width=640, height=360,
    )
    t._process_frame(frame)

    assert sse_server.emit.call_count == 0, (
        f"expected no emit for empty tracks, got {sse_server.emit.call_count}"
    )


def test_species_confidence_separate_from_yolo_confidence():
    """After the first vote, track.species_confidence holds the classifier's
    score and track.confidence still holds the YOLO bbox score.

    Phase 2 note: a single vote shows the species immediately (before lock)
    but does NOT set needs_classification=False — that requires 3+ votes.
    """
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.classifier import ClassificationResult
    from pipeline.frame import Frame
    from PIL import Image
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
                "yolo_runs_total": 0, "yolo_skipped_motion": 0}

    fake_track = MagicMock()
    fake_track.needs_classification = True
    fake_track.classification_attempts = 0
    fake_track.vote_history = []
    fake_track.is_locked = False
    fake_track.bbox = [100, 100, 300, 300]
    fake_track.confidence = 0.85  # YOLO bbox score (the tracker mutates this)
    fake_track.species = None
    fake_track.species_confidence = None
    fake_track.model_source = None
    # pacing fields (2026-07-04 classification-lifecycle contract)
    fake_track.frame_count = 100
    fake_track.last_classify_fc = -1_000_000
    fake_track.no_vote_streak = 0
    fake_track.lock_disagreements = 0

    classifier = MagicMock()
    classifier.classify.return_value = ClassificationResult(
        species="Downy Woodpecker",
        confidence=0.92,  # classifier score
        model_source="yard",
        should_retry=False,
    )
    t.classifier = classifier

    frame = Frame(
        bgr=np.zeros((360, 640, 3), dtype=np.uint8),
        wall_time_ms=time.time() * 1000,
        camera="feeder", width=640, height=360,
    )

    t._classify_tracks(frame, [fake_track])

    # Species is shown immediately from first vote (before lock)
    assert fake_track.species == "Downy Woodpecker"
    assert fake_track.species_confidence == 0.92, (
        f"expected species_confidence=0.92, got {fake_track.species_confidence}"
    )
    # YOLO bbox confidence should NOT have been stomped
    assert fake_track.confidence == 0.85, (
        f"expected track.confidence=0.85 (YOLO, preserved), got {fake_track.confidence}"
    )
    assert fake_track.model_source == "yard"
    # One vote is not enough to lock — track stays open for more votes
    assert fake_track.needs_classification is True
    assert fake_track.is_locked is False
    assert len(fake_track.vote_history) == 1


def test_voting_locks_species_after_consensus():
    """Species locks after 3 agreeing votes with sufficient confidence."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.classifier import ClassificationResult, MAX_CLASSIFICATION_ATTEMPTS
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
                "yolo_runs_total": 0, "yolo_skipped_motion": 0}

    # Fake track that needs classification
    fake_track = MagicMock()
    fake_track.needs_classification = True
    fake_track.classification_attempts = 0
    fake_track.vote_history = []
    fake_track.is_locked = False
    fake_track.bbox = [100, 100, 300, 300]
    fake_track.species = None
    fake_track.species_confidence = None
    fake_track.model_source = None
    fake_track.confidence = 0.9
    # pacing fields (2026-07-04 classification-lifecycle contract)
    fake_track.frame_count = 100
    fake_track.last_classify_fc = -1_000_000
    fake_track.no_vote_streak = 0
    fake_track.lock_disagreements = 0

    # Classifier returns "Downy Woodpecker" 3 times
    call_count = [0]
    def fake_classify(crop, wall_t, camera):
        call_count[0] += 1
        return ClassificationResult(
            species="Downy Woodpecker", confidence=0.85,
            model_source="yard", should_retry=False
        )
    classifier = MagicMock()
    classifier.classify = fake_classify
    t.classifier = classifier

    frame = Frame(bgr=np.zeros((360, 640, 3), dtype=np.uint8),
                  wall_time_ms=time.time() * 1000,
                  camera="feeder", width=640, height=360)

    # Run classification 3 times (simulating 3 successive frames, advancing
    # frame_count past the CLASSIFY_EVERY cadence gate each time)
    for i in range(3):
        fake_track.needs_classification = True  # reset for each "frame"
        fake_track.classification_attempts = i  # simulate incremental attempts
        fake_track.frame_count = 100 + i * 10   # past the cadence gate
        t._classify_tracks(frame, [fake_track])

    assert fake_track.species == "Downy Woodpecker"
    assert fake_track.is_locked is True
    assert fake_track.species_confidence == 0.85
    assert len(fake_track.vote_history) == 3
    assert fake_track.needs_classification is False


def test_voting_takes_plurality_at_attempt_cap():
    """When a no-vote burst hits MAX_CLASSIFICATION_ATTEMPTS without consensus,
    take the plurality of the votes so far and enter cooldown (2026-07-04
    contract: cool down and retry later, never give up for the track's life)."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.classifier import ClassificationResult, MAX_CLASSIFICATION_ATTEMPTS
    from pipeline.frame import Frame
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
                "yolo_runs_total": 0, "yolo_skipped_motion": 0}

    fake_track = MagicMock()
    fake_track.needs_classification = True
    fake_track.classification_attempts = 5
    fake_track.vote_history = [
        ("Downy Woodpecker", 0.7),
        ("Hairy Woodpecker", 0.6),
        ("Downy Woodpecker", 0.8),
        ("House Finch", 0.5),
        ("Downy Woodpecker", 0.75),
    ]  # 3/5 Downy → 60% agreement, should be taken as plurality
    fake_track.is_locked = False
    fake_track.bbox = [100, 100, 300, 300]
    fake_track.species = None
    fake_track.species_confidence = None
    fake_track.model_source = None
    fake_track.frame_count = 100
    fake_track.last_classify_fc = -1_000_000
    fake_track.no_vote_streak = MAX_CLASSIFICATION_ATTEMPTS - 1  # one more no-vote hits the cap
    fake_track.lock_disagreements = 0

    # sub-floor crop: classifier returns no species
    classifier = MagicMock()
    classifier.classify.return_value = ClassificationResult(
        species=None, confidence=0.0, model_source="aiy_onnx", should_retry=False)
    t.classifier = classifier

    frame = Frame(bgr=np.zeros((360, 640, 3), dtype=np.uint8),
                  wall_time_ms=time.time() * 1000,
                  camera="feeder", width=640, height=360)

    t._classify_tracks(frame, [fake_track])

    assert fake_track.species == "Downy Woodpecker"
    assert fake_track.species_confidence == 0.8  # max confidence for the winning species
    assert fake_track.model_source == "vote_plurality"
    # cooldown entered, NOT permanent surrender
    assert fake_track.no_vote_streak >= MAX_CLASSIFICATION_ATTEMPTS
    assert fake_track.needs_classification is True


def test_disagreement_detector_stops_flipflopping_track_early():
    """When a track flip-flops on species, the disagreement detector stops
    classification early (before MAX_CLASSIFICATION_ATTEMPTS) and takes the
    plurality winner rather than leaving the track open indefinitely."""
    from unittest.mock import MagicMock
    from pipeline.process_thread import CameraProcessThread
    from pipeline.classifier import ClassificationResult
    from pipeline.track_disagreement_detector import TrackDisagreementDetector
    from pipeline.frame import Frame
    from pipeline.constants import ModelSource
    import numpy as np, threading, time

    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
                "yolo_runs_total": 0, "yolo_skipped_motion": 0}
    t.disagreement_detector = TrackDisagreementDetector(disagreement_threshold=0.6)

    fake_track = MagicMock()
    fake_track.track_id = 42
    fake_track.needs_classification = True
    fake_track.classification_attempts = 0
    fake_track.vote_history = []
    fake_track.is_locked = False
    fake_track.bbox = [100, 100, 300, 300]
    fake_track.confidence = 0.85
    fake_track.species = None
    fake_track.species_confidence = None
    fake_track.model_source = None
    # pacing fields (2026-07-04 classification-lifecycle contract)
    fake_track.frame_count = 100
    fake_track.last_classify_fc = -1_000_000
    fake_track.no_vote_streak = 0
    fake_track.lock_disagreements = 0

    species_sequence = ["Northern Cardinal", "Black-capped Chickadee", "House Wren"]
    call_index = [0]

    classifier = MagicMock()
    def flip_flop_classify(*args, **kwargs):
        sp = species_sequence[call_index[0] % len(species_sequence)]
        call_index[0] += 1
        return ClassificationResult(sp, 0.90, "aiy", False)
    classifier.classify.side_effect = flip_flop_classify
    t.classifier = classifier

    frame = Frame(bgr=np.zeros((360, 640, 3), dtype=np.uint8),
                  wall_time_ms=time.time() * 1000,
                  camera="feeder", width=640, height=360)

    # Three frames with three different species → 100% disagreement ratio → early stop
    for i in range(3):
        fake_track.needs_classification = True
        fake_track.classification_attempts = i
        fake_track.frame_count = 100 + i * 10   # past the cadence gate
        t._classify_tracks(frame, [fake_track])

    # 2026-07-04 contract: early-stop = enter cooldown (burst exhausted), not
    # permanent surrender; the label falls back to the plurality meanwhile.
    from pipeline.process_thread import MAX_CLASSIFICATION_ATTEMPTS
    assert fake_track.no_vote_streak >= MAX_CLASSIFICATION_ATTEMPTS, \
        "Disagreed track should enter cooldown early"
    assert fake_track.is_locked is False, "Disagreement early-stop is not a vote-lock"
    assert fake_track.species in species_sequence, "Should still emit a species (plurality)"
    assert fake_track.model_source == ModelSource.VOTE_PLURALITY


# ── identify-then-render (2026-09-18): label state, pts anchors, identity ops ──
def _bare_thread(tracker_out, sse_server=None):
    """CameraProcessThread via __new__ with just the attributes _process_frame
    reads; dry-run so no event_store writes."""
    import threading, time
    from pipeline.process_thread import CameraProcessThread
    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._dry_run = True
    t._stats = {"frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
                "yolo_runs_total": 0, "yolo_skipped_motion": 0}
    t._last_forced_full = time.time()
    t.motion_gate = MagicMock(); t.motion_gate.regions.return_value = []
    t.detector = MagicMock(); t.detector.detect.return_value = []
    t.tracker = MagicMock(); t.tracker.update.return_value = tracker_out
    t.tracker.tracks = {}; t.tracker.stationary_regions.return_value = []
    t.tracker.identity_log = []
    t.classifier = MagicMock(); t.classifier.stats = {}
    t.event_store = MagicMock(); t.health = MagicMock()
    t.sse_server = sse_server
    t.frame_width, t.frame_height = 640, 360
    return t


def _frame(pts=0.0):
    from pipeline.frame import Frame
    return Frame(bgr=np.zeros((360, 640, 3), dtype=np.uint8), wall_time_ms=1_700_000_000_000,
                 camera="feeder", width=640, height=360, pts=pts)


def _track(**kw):
    from pipeline.tracker_common import Track
    trk = Track(track_id=kw.pop("track_id", 1), created_at_ms=0, last_updated_ms=0,
                bbox=kw.pop("bbox", [100, 100, 300, 300]), confidence=0.9)
    for k, v in kw.items():
        setattr(trk, k, v)
    return trk


def test_tracker_update_receives_the_frame_pts():
    from pipeline.tracker_common import TrackerOutput
    t = _bare_thread(TrackerOutput(active=[], new=[], expired=[], frame_time_ms=0))
    t._process_frame(_frame(pts=12.5))
    assert t.tracker.update.call_args.kwargs["pts"] == 12.5


def test_label_state_has_exactly_four_states():
    from pipeline.process_thread import _label_state
    trk = _track()
    assert _label_state(trk) == "none"
    trk.species = "Blue Jay"
    assert _label_state(trk) == "candidate"
    trk.tentative = True
    assert _label_state(trk) == "tentative"
    trk.is_locked = True
    assert _label_state(trk) == "locked"
    trk.is_locked, trk.species = False, None
    assert _label_state(trk) == "none"                  # tentative with nothing to show is nothing


def test_payload_carries_label_fields_beside_the_legacy_keys_and_identity_none():
    from pipeline.tracker_common import TrackerOutput
    trk = _track(track_id=3, bbox=[10, 10, 50, 50], species="Blue Jay", is_locked=True,
                 label_epoch=1, lock_pts=133.1667, seg_pts=132.4333)
    sse = MagicMock()
    t = _bare_thread(TrackerOutput(active=[trk], new=[trk], expired=[], frame_time_ms=0), sse)
    t._process_frame(_frame(pts=134.0))
    kw = sse.emit.call_args.kwargs
    assert kw["pts"] == 134.0 and kw["identity"] is None
    p = kw["tracks"][0]
    assert p["label_state"] == "locked" and p["label_epoch"] == 1
    assert p["lock_pts"] == 133.1667 and p["seg_pts"] == 132.4333
    for k in ("track_id", "bbox", "bbox_center_x", "frame_width", "frame_height", "species",
              "species_confidence", "model_source", "is_locked", "frame_count", "coasting"):
        assert k in p, k


def test_identity_ops_ride_ten_emitted_events_then_drain():
    from pipeline.tracker_common import TrackerOutput
    trk = _track(bbox=[10, 10, 50, 50])
    busy = TrackerOutput(active=[trk], new=[], expired=[], frame_time_ms=0)
    idle = TrackerOutput(active=[], new=[], expired=[], frame_time_ms=0)
    sse = MagicMock()
    t = _bare_thread(busy, sse)
    log = t.tracker.identity_log

    def emitted_identity():
        return sse.emit.call_args.kwargs["identity"]

    t._process_frame(_frame())
    assert emitted_identity() is None
    op1 = {"id": 1, "op": "split", "from": 8, "to": 9, "at_pts": 137.0667}
    log.append(op1)
    # frames with no active tracks emit nothing and must not consume repeats
    t.tracker.update.return_value = idle
    for _ in range(3):
        t._process_frame(_frame())
    assert sse.emit.call_count == 1
    t.tracker.update.return_value = busy
    for _ in range(5):
        t._process_frame(_frame())
        assert emitted_identity() == [op1]
    op2 = {"id": 2, "op": "merge", "from": 12, "into": 4, "at_pts": 300.1}
    log.append(op2)
    for _ in range(5):
        t._process_frame(_frame())
        assert emitted_identity() == [op1, op2]          # op1 events 6..10
    for _ in range(5):
        t._process_frame(_frame())
        assert emitted_identity() == [op2]               # op1 drained after its 10th event
    t._process_frame(_frame())
    assert emitted_identity() is None
    assert log == [op1, op2]                             # the tracker's log is never mutated


def test_identity_drain_reads_by_op_id_so_a_rotated_log_never_resends_or_skips():
    """The tracker's identity_log is a bounded deque. Ops that rotated out
    before the drain are simply gone; ops already riding are not re-sent when
    the entries beneath them disappear, and nothing newer is skipped."""
    from collections import deque
    from pipeline.tracker_common import TrackerOutput
    trk = _track(bbox=[10, 10, 50, 50])
    sse = MagicMock()
    t = _bare_thread(TrackerOutput(active=[trk], new=[], expired=[], frame_time_ms=0), sse)
    log = deque(maxlen=2)
    t.tracker.identity_log = log
    ops = [{"id": i, "op": "merge", "from": 10 + i, "into": 1, "at_pts": float(i)} for i in range(1, 5)]
    log.extend(ops[:3])                                  # op1 rotated out before any emit
    t._process_frame(_frame())
    assert sse.emit.call_args.kwargs["identity"] == [ops[1], ops[2]]
    log.append(ops[3])                                   # pushes op2 out of the log
    t._process_frame(_frame())
    assert sse.emit.call_args.kwargs["identity"] == [ops[1], ops[2], ops[3]]   # op2 still rides
    t._process_frame(_frame())
    assert sse.emit.call_args.kwargs["identity"] == [ops[1], ops[2], ops[3]]   # nothing re-sent


def _classify_only_thread(result):
    import threading
    from pipeline.process_thread import CameraProcessThread
    t = CameraProcessThread.__new__(CameraProcessThread)
    t.name = "feeder"
    t._stop = threading.Event()
    t._stats = {"frames_processed": 0, "detections": 0, "yolo_ms_samples": [],
                "yolo_runs_total": 0, "yolo_skipped_motion": 0}
    t.classifier = MagicMock()
    t.classifier.classify.return_value = result
    return t


def test_lock_stamps_lock_pts_and_bumps_epoch_once_not_on_candidate_churn():
    from pipeline.classifier import ClassificationResult
    t = _classify_only_thread(ClassificationResult("Downy Woodpecker", 0.85, "yard", False))
    trk = _track()
    for i in range(3):
        trk.frame_count = 100 + i * 10                   # past the CLASSIFY_EVERY gate
        t._classify_tracks(_frame(pts=41.7), [trk])
        if i < 2:
            assert trk.species == "Downy Woodpecker" and not trk.is_locked
            assert trk.label_epoch == 0 and trk.lock_pts is None
    assert trk.is_locked and trk.tentative is False
    assert trk.label_epoch == 1 and trk.lock_pts == 41.7


def test_unverifiable_unlock_demotes_to_tentative_keeps_species_and_lock_pts_then_relocks():
    from pipeline.classifier import ClassificationResult
    from pipeline.process_thread import LOCK_UNVERIFIED_N, _label_state
    t = _classify_only_thread(ClassificationResult(None, 0.0, "aiy_onnx", False))
    trk = _track(species="Blue Jay", species_confidence=0.9, is_locked=True, lock_pts=10.0,
                 label_epoch=1, frame_count=100, last_classify_fc=0,
                 no_vote_streak=LOCK_UNVERIFIED_N - 1)
    t._classify_tracks(_frame(pts=20.0), [trk])
    assert not trk.is_locked and trk.tentative is True
    assert trk.species == "Blue Jay" and trk.lock_pts == 10.0
    assert trk.label_epoch == 2 and _label_state(trk) == "tentative"
    # votes resume and agree: back to locked, fresh lock_pts, one more epoch
    t.classifier.classify.return_value = ClassificationResult("Blue Jay", 0.85, "yard", False)
    for i in range(3):
        trk.frame_count += 10
        t._classify_tracks(_frame(pts=23.0), [trk])
    assert trk.is_locked and trk.tentative is False
    assert trk.lock_pts == 23.0 and trk.label_epoch == 3


def test_relabel_while_tentative_demotes_to_candidate_and_bumps_epoch():
    """A tentative label (kept from a demoted lock) is verified text on glass.
    Once the votes since the demotion favour ANOTHER species it is no longer
    verified: candidate (no text), lock_pts null, epoch +1 — never a one-vote
    species rendered as tentative. An agreeing vote leaves it untouched."""
    from pipeline.classifier import ClassificationResult
    from pipeline.process_thread import LOCK_CONF_THRESHOLD, _label_state
    sub = LOCK_CONF_THRESHOLD - 0.05                      # votes that can never complete a lock
    trk = _track(species="Blue Jay", species_confidence=0.9, is_locked=False, tentative=True,
                 lock_pts=10.0, label_epoch=2, frame_count=100, last_classify_fc=0)
    t = _classify_only_thread(ClassificationResult("Blue Jay", sub, "aiy_onnx", False))
    t._classify_tracks(_frame(pts=20.0), [trk])
    assert trk.species == "Blue Jay" and _label_state(trk) == "tentative"
    assert trk.tentative is True and trk.lock_pts == 10.0 and trk.label_epoch == 2
    t.classifier.classify.return_value = ClassificationResult("Tufted Titmouse", sub, "aiy_onnx", False)
    trk.frame_count += 10
    t._classify_tracks(_frame(pts=21.0), [trk])          # 1 jay / 1 titmouse: plurality still jay
    assert trk.species == "Blue Jay" and trk.tentative is True and trk.label_epoch == 2
    trk.frame_count += 10
    t._classify_tracks(_frame(pts=22.0), [trk])          # 1 jay / 2 titmouse: the kept label is contradicted
    assert trk.species == "Tufted Titmouse" and not trk.is_locked
    assert trk.tentative is False and trk.lock_pts is None and trk.label_epoch == 3
    assert _label_state(trk) == "candidate"


def test_contradiction_unlock_clears_lock_pts_and_tentative_and_bumps_epoch():
    from pipeline.classifier import ClassificationResult
    from pipeline.process_thread import LOCK_UNLOCK_DISAGREEMENTS, _label_state
    t = _classify_only_thread(ClassificationResult("Tufted Titmouse", 0.9, "aiy_onnx", False))
    trk = _track(species="Blue Jay", species_confidence=0.9, is_locked=True, tentative=False,
                 lock_pts=10.0, label_epoch=1, frame_count=100, last_classify_fc=0,
                 lock_disagreements=LOCK_UNLOCK_DISAGREEMENTS - 1)
    t._classify_tracks(_frame(pts=20.0), [trk])
    assert not trk.is_locked and trk.tentative is False and trk.lock_pts is None
    assert trk.species == "Tufted Titmouse" and trk.label_epoch == 2
    assert _label_state(trk) == "candidate"
