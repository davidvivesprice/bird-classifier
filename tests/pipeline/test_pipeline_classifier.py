"""Tests for SmartClassifier (Smart B decision tree)."""
import threading
from unittest.mock import MagicMock, patch
import pytest
from PIL import Image


def _make_pil():
    return Image.new("RGB", (224, 224), (128, 128, 128))


def _result(species, confidence):
    return MagicMock(species=species, confidence=confidence)


def test_yard_confident_returns_yard():
    """Path 1: Yard confidence >= 0.60 → immediate yard result."""
    from pipeline.classifier import SmartClassifier, ClassificationResult
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock()
    c.aiy = MagicMock()
    c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("Black-capped Chickadee", 0.82))
    c._run_aiy = MagicMock()
    c._audio_lookup = MagicMock()

    r = c.classify(_make_pil(), frame_time_ms=0, camera="feeder")
    assert r.species == "Black-capped Chickadee"
    assert r.model_source == "yard"
    assert r.should_retry is False
    c._run_aiy.assert_not_called()  # shortcut
    assert c.stats["yard"] == 1


def test_yard_useless_aiy_rescues():
    """Path 2: Yard <0.30, AIY confident → AIY result."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("noise", 0.10))
    c._run_aiy = MagicMock(return_value=_result("American Robin", 0.75))
    c._audio_lookup = MagicMock()

    r = c.classify(_make_pil(), 0, "feeder")
    assert r.species == "American Robin"
    assert r.model_source == "aiy"
    assert c.stats["aiy"] == 1


def test_yard_uncertain_both_agree():
    """Path 3: Yard 0.30-0.60, AIY same species → both_agree."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("Downy Woodpecker", 0.45))
    c._run_aiy = MagicMock(return_value=_result("Downy Woodpecker", 0.50))
    c._audio_lookup = MagicMock()

    r = c.classify(_make_pil(), 0, "feeder")
    assert r.species == "Downy Woodpecker"
    assert r.model_source == "both_agree"
    assert r.confidence == pytest.approx(0.50)
    assert c.stats["both_agree"] == 1


def test_disagreement_audio_confirms():
    """Path 4: Yard and AIY disagree, audio confirms one → audio_confirmed."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = "fake.db"
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("Downy Woodpecker", 0.50))
    c._run_aiy = MagicMock(return_value=_result("Hairy Woodpecker", 0.55))
    c._audio_lookup = MagicMock(return_value="Hairy Woodpecker")

    r = c.classify(_make_pil(), 0, "feeder")
    assert r.species == "Hairy Woodpecker"
    assert r.model_source == "audio_confirmed"
    assert c.stats["audio_confirmed"] == 1


def test_no_confident_answer_returns_unlabeled():
    """Nothing agrees, no audio → unlabeled, should_retry=False."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    c._coral_lock = threading.Lock()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock(return_value=_result("X", 0.40))
    c._run_aiy = MagicMock(return_value=_result("Y", 0.40))
    c._audio_lookup = MagicMock(return_value=None)

    r = c.classify(_make_pil(), 0, "feeder")
    assert r.species is None
    assert r.should_retry is False
    assert c.stats["unlabeled"] == 1


def test_coral_lock_timeout_returns_should_retry():
    """If another thread holds the Coral lock past timeout, return should_retry=True."""
    from pipeline.classifier import SmartClassifier
    c = SmartClassifier.__new__(SmartClassifier)
    # Hold the lock elsewhere
    c._coral_lock = threading.Lock()
    c._coral_lock.acquire()
    c.yard = MagicMock(); c.aiy = MagicMock(); c.audio_db_path = None
    c.stats = {k: 0 for k in ["yard", "aiy", "both_agree", "audio_confirmed",
                               "unlabeled", "lock_timeouts", "retries"]}
    c._run_yard = MagicMock()
    c._run_aiy = MagicMock()
    c._audio_lookup = MagicMock()

    # Shorten the timeout for test speed
    with patch("pipeline.classifier.CORAL_ACQUIRE_TIMEOUT", 0.2):
        r = c.classify(_make_pil(), 0, "feeder")
    assert r.should_retry is True
    assert r.species is None
    assert c.stats["lock_timeouts"] == 1
    c._coral_lock.release()
