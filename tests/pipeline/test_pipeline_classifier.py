"""Tests for SmartClassifier (Smart B decision tree)."""
import threading
from unittest.mock import MagicMock, patch
import pytest
from PIL import Image

from pipeline.camera_config import CameraClassifierConfig


def _make_pil():
    return Image.new("RGB", (224, 224), (128, 128, 128))


def _result(species, confidence):
    return MagicMock(species=species, confidence=confidence)


def _make_classifier(camera="feeder"):
    """Helper: build a bare SmartClassifier with per-camera stats wired up."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock()
    c.aiy = MagicMock()
    configs = {
        camera: CameraClassifierConfig(use_yard=True),
    }
    c.camera_configs = configs
    c.stats = {
        camera: {k: 0 for k in ["yard", "aiy", "both_agree",
                                 "unlabeled_call", "lock_timeouts"]}
    }
    return c, camera


def test_yard_confident_returns_yard():
    """Path 1: Yard confidence >= 0.60 → immediate yard result."""
    c, cam = _make_classifier()
    c._run_yard = MagicMock(return_value=_result("Black-capped Chickadee", 0.82))
    c._run_aiy = MagicMock()

    r = c.classify(_make_pil(), frame_time_ms=0, camera=cam)
    assert r.species == "Black-capped Chickadee"
    assert r.model_source == "yard"
    assert r.should_retry is False
    c._run_aiy.assert_not_called()  # shortcut
    assert c.stats[cam]["yard"] == 1


def test_yard_useless_aiy_rescues():
    """Path 2: Yard <0.30, AIY confident → AIY result."""
    c, cam = _make_classifier()
    c._run_yard = MagicMock(return_value=_result("noise", 0.10))
    c._run_aiy = MagicMock(return_value=_result("American Robin", 0.75))

    r = c.classify(_make_pil(), 0, cam)
    assert r.species == "American Robin"
    assert r.model_source == "aiy"
    assert c.stats[cam]["aiy"] == 1


def test_yard_uncertain_both_agree():
    """Path 3: Yard 0.30-0.60, AIY same species → both_agree."""
    c, cam = _make_classifier()
    c._run_yard = MagicMock(return_value=_result("Downy Woodpecker", 0.45))
    c._run_aiy = MagicMock(return_value=_result("Downy Woodpecker", 0.50))

    r = c.classify(_make_pil(), 0, cam)
    assert r.species == "Downy Woodpecker"
    assert r.model_source == "both_agree"
    assert r.confidence == pytest.approx(0.50)
    assert c.stats[cam]["both_agree"] == 1


def test_disagreement_falls_through_to_unlabeled():
    """Path 4 removed: yard/AIY disagree → unlabeled (no audio tiebreaker)."""
    c, cam = _make_classifier()
    c._run_yard = MagicMock(return_value=_result("Downy Woodpecker", 0.50))
    c._run_aiy = MagicMock(return_value=_result("Hairy Woodpecker", 0.55))

    r = c.classify(_make_pil(), 0, cam)
    assert r.species is None
    assert r.model_source is None
    assert c.stats[cam]["unlabeled_call"] == 1


def test_no_confident_answer_returns_unlabeled():
    """Nothing agrees → unlabeled, should_retry=False."""
    c, cam = _make_classifier()
    c._run_yard = MagicMock(return_value=_result("X", 0.40))
    c._run_aiy = MagicMock(return_value=_result("Y", 0.40))

    r = c.classify(_make_pil(), 0, cam)
    assert r.species is None
    assert r.should_retry is False
    assert c.stats[cam]["unlabeled_call"] == 1


def test_coral_lock_timeout_returns_should_retry():
    """If another thread holds the Coral lock past timeout, return should_retry=True.

    With the narrowed lock scope (I2), the lock is acquired inside _run_yard.
    We hold the lock externally and let the real _run_yard try and fail to acquire it.
    """
    c, cam = _make_classifier()
    # Hold the lock elsewhere — real _run_yard will try to acquire and time out.
    c._coral_lock.acquire()

    # Shorten the timeout for test speed
    with patch("pipeline.classifier.CORAL_ACQUIRE_TIMEOUT", 0.2):
        r = c.classify(_make_pil(), 0, cam)
    assert r.should_retry is True
    assert r.species is None
    assert c.stats[cam]["lock_timeouts"] == 1
    c._coral_lock.release()


def test_classifier_has_no_audio_lookup_method():
    """Path 4 (audio cross-check) is dropped in v3. The method must not exist."""
    from pipeline.classifier import SmartClassifier
    assert not hasattr(SmartClassifier, "_audio_lookup"), (
        "SmartClassifier._audio_lookup was supposed to be deleted in v3"
    )


def test_classifier_has_no_audio_confirmed_stat():
    """audio_confirmed counter is removed along with Path 4."""
    from pipeline.classifier import SmartClassifier
    import inspect
    source = inspect.getsource(SmartClassifier)
    assert "audio_confirmed" not in source, (
        "audio_confirmed still referenced in SmartClassifier source"
    )


def test_ground_camera_skips_yard_entirely():
    """When use_yard=False, yard classifier must not be called; AIY runs alone."""
    from unittest.mock import MagicMock
    from pipeline.classifier import SmartClassifier
    from pipeline.camera_config import CameraClassifierConfig
    from PIL import Image

    configs = {
        "feeder": CameraClassifierConfig(use_yard=True),
        "ground": CameraClassifierConfig(use_yard=False),
    }

    classifier = SmartClassifier.__new__(SmartClassifier)
    classifier.camera_configs = configs
    classifier._coral_lock = __import__("threading").Lock()
    classifier.stats = {
        cam: {"yard": 0, "aiy": 0, "both_agree": 0,
              "unlabeled_call": 0, "lock_timeouts": 0}
        for cam in configs
    }

    classifier.yard = MagicMock()
    classifier.aiy = MagicMock()

    yard_called = [0]
    aiy_called = [0]

    def fake_yard(crop, cam_stats):
        yard_called[0] += 1
        return type("YR", (), {"species": "Northern Cardinal", "confidence": 0.9})()

    def fake_aiy(crop):
        aiy_called[0] += 1
        return type("AR", (), {"species": "Red-winged Blackbird", "confidence": 0.85})()

    classifier._run_yard = fake_yard
    classifier._run_aiy = fake_aiy

    dummy_img = Image.new("RGB", (100, 100))

    # Ground camera call
    result = classifier.classify(dummy_img, 0, "ground")
    assert yard_called[0] == 0, f"yard should NOT run for ground, ran {yard_called[0]} times"
    assert aiy_called[0] == 1
    assert result.species == "Red-winged Blackbird"
    assert result.model_source == "aiy"

    # Feeder camera call
    result2 = classifier.classify(dummy_img, 0, "feeder")
    assert yard_called[0] == 1, f"yard should run for feeder, ran {yard_called[0]} times"
    assert result2.species == "Northern Cardinal"
    assert result2.model_source == "yard"


def test_run_aiy_clamps_confidence_at_one():
    """AIY's raw_score is uint8 (0-255); historically `_run_aiy` returned
    raw_score/100.0 which could exceed 1.0 (max ~2.55). 1225 rows in
    classifications.db had auth.confidence > 1.0 before this clamp landed
    (2026-04-26). The clamp at 1.0 keeps the contract that confidence ∈ [0,1]
    so consumers (calibration UI buckets, RC2 threshold of 0.1, snapshot
    writer) can rely on it. Values <1.0 are unchanged so vote-lock 0.35
    stays calibrated.

    See pipeline/camera_config.py:17-34 docstring for the AIY scale-mismatch
    technical-debt note this clamp partially addresses.
    """
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    # Mock self.aiy.classify to return a high raw_score
    c.aiy = MagicMock()
    # filtered list with one entry; raw_score=200 → would be 2.0 unclamped
    c.aiy.classify.return_value = (
        [{"common_name": "Northern Cardinal", "raw_score": 200}],
        [{"common_name": "Northern Cardinal", "raw_score": 200}],
    )
    result = c._run_aiy(_make_pil())
    assert result is not None
    assert result.species == "Northern Cardinal"
    assert result.confidence == 1.0, f"expected clamped to 1.0, got {result.confidence}"

    # And a normal-range value (raw_score=75 → 0.75) passes through unchanged
    c.aiy.classify.return_value = (
        [{"common_name": "Tufted Titmouse", "raw_score": 75}],
        [{"common_name": "Tufted Titmouse", "raw_score": 75}],
    )
    result2 = c._run_aiy(_make_pil())
    assert result2.confidence == 0.75


def test_ground_path_does_not_hold_coral_lock():
    """With use_yard=False, classify() must never acquire the Coral lock,
    so a busy Coral on feeder doesn't block ground classification."""
    from unittest.mock import MagicMock
    from pipeline.classifier import SmartClassifier
    from pipeline.camera_config import CameraClassifierConfig
    from PIL import Image
    import threading

    configs = {
        "feeder": CameraClassifierConfig(use_yard=True),
        "ground": CameraClassifierConfig(use_yard=False),
    }

    classifier = SmartClassifier.__new__(SmartClassifier)
    classifier.camera_configs = configs
    classifier._coral_lock = threading.Lock()
    classifier.stats = {
        cam: {"yard": 0, "aiy": 0, "both_agree": 0,
              "unlabeled_call": 0, "lock_timeouts": 0}
        for cam in configs
    }

    # Grab the Coral lock — simulate feeder mid-yard-inference
    classifier._coral_lock.acquire()

    classifier._run_yard = MagicMock(return_value=None)
    classifier._run_aiy = MagicMock(return_value=type("R", (), {
        "species": "Mourning Dove", "confidence": 0.9})())

    # Ground call should NOT block on the held Coral lock
    result_holder = {}

    def call_ground():
        img = Image.new("RGB", (100, 100))
        result_holder["r"] = classifier.classify(img, 0, "ground")

    t = threading.Thread(target=call_ground)
    t.start()
    t.join(timeout=2.0)  # if we're blocked on the lock, this times out
    assert not t.is_alive(), "ground classify blocked on Coral lock while yard held it"
    assert result_holder["r"].species == "Mourning Dove"
    assert result_holder["r"].model_source == "aiy"

    classifier._coral_lock.release()
