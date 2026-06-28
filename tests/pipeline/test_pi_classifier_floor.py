"""F2: lowering the vote floor lets mid-confidence crops vote."""
from unittest.mock import MagicMock

from pipeline.pi_classifier import PiClassifier, _normalize_raw_score


def _registry(raw_score, name="House Finch"):
    reg = MagicMock()
    reg.current_name = "aiy_onnx"
    reg.classify.return_value = [{"common_name": name, "raw_score": raw_score}]
    return reg


def test_crop_between_old_and_new_floor_now_votes():
    # A raw_score whose normalized confidence is in [0.16, 0.25): rejected at the
    # old 0.25 floor, eligible at the new 0.16 floor.
    raw = next(r for r in range(256) if 0.16 <= _normalize_raw_score(r) < 0.25)
    clf = PiClassifier(_registry(raw), confident_threshold=0.16)
    res = clf.classify(MagicMock(), 0.0, "feeder")
    assert res.species == "House Finch"
    assert res.confidence >= 0.16


def test_crop_below_new_floor_still_rejected():
    raw = next(r for r in range(256) if _normalize_raw_score(r) < 0.16)
    clf = PiClassifier(_registry(raw), confident_threshold=0.16)
    res = clf.classify(MagicMock(), 0.0, "feeder")
    assert res.species is None


def test_old_floor_would_have_rejected_the_mid_crop():
    # Same mid crop, old 0.25 floor -> rejected (documents the regression we fix).
    raw = next(r for r in range(256) if 0.16 <= _normalize_raw_score(r) < 0.25)
    clf = PiClassifier(_registry(raw), confident_threshold=0.25)
    res = clf.classify(MagicMock(), 0.0, "feeder")
    assert res.species is None
