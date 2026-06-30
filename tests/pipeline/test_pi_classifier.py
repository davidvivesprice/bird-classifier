import pytest
from PIL import Image


class FakeRegistry:
    current_name = "aiy_onnx"

    def __init__(self, predictions):
        self.predictions = predictions

    def classify(self, crop_pil):
        return list(self.predictions)


def _crop():
    return Image.new("RGB", (64, 64), (128, 128, 128))


def test_aiy_raw_score_one_is_not_perfect_confidence():
    from pipeline.pi_classifier import PiClassifier

    classifier = PiClassifier(
        FakeRegistry([
            {"common_name": "Northern Flicker", "raw_score": 1},
        ]),
        confident_threshold=0.25,
    )

    result = classifier.classify(_crop(), frame_time_ms=0, camera="feeder")

    assert result.species is None
    assert result.confidence == 0.0
    assert classifier.stats["feeder"]["unlabeled_call"] == 1


def test_authoritative_returns_calibrated_confidence_not_overconfident():
    from pipeline.pi_classifier import PiClassifier
    from pipeline.calibration import calibrate

    classifier = PiClassifier(
        FakeRegistry([
            {"common_name": "Northern Flicker", "raw_score": 1},
        ])
    )

    result = classifier.authoritative_classify(_crop())

    assert result is not None
    assert result.species == "Northern Flicker"
    # Confidence is now the calibrated P(correct), not raw/255. raw=1 must NOT
    # become perfect confidence (the old normalization-bug guard still holds).
    assert result.confidence == pytest.approx(calibrate(1))
    assert result.confidence < 0.5


def test_aiy_above_threshold_returns_calibrated_confidence():
    from pipeline.pi_classifier import PiClassifier
    from pipeline.calibration import calibrate

    classifier = PiClassifier(
        FakeRegistry([
            {"common_name": "House Finch", "raw_score": 128},
        ]),
        confident_threshold=0.25,
    )

    result = classifier.classify(_crop(), frame_time_ms=0, camera="feeder")

    assert result.species == "House Finch"
    # Eligibility floor stays on raw (128/255=0.50 >= 0.25); returned confidence
    # is the calibrated P(correct), not raw/255.
    assert result.confidence == pytest.approx(calibrate(128))
    assert result.model_source == "aiy_onnx"
