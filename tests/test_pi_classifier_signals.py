"""PiClassifier: the no-bird signal and test-time augmentation, against a
fake registry (no ONNX)."""
from PIL import Image

from pipeline.pi_classifier import PiClassifier


class _Inst:
    """Fake model instance: returns (regional, unfiltered) per crop and counts calls."""
    def __init__(self, regional, unfiltered):
        self.regional, self.unfiltered, self.calls = regional, unfiltered, 0

    def classify_full(self, crop):
        self.calls += 1
        return list(self.regional), list(self.unfiltered)

    def classify(self, crop):
        self.calls += 1
        return list(self.regional) or list(self.unfiltered)


class _Registry:
    def __init__(self, inst):
        self._current_instance, self.current_name = inst, "aiy_onnx"

    def classify(self, crop):
        return self._current_instance.classify(crop)


def crop():
    return Image.new("RGB", (80, 60), (120, 120, 120))


def test_background_top1_flags_no_bird_without_a_vote():
    inst = _Inst(regional=[{"common_name": "House Finch", "raw_score": 9}],
                 unfiltered=[{"common_name": "background", "raw_score": 200}])
    r = PiClassifier(_Registry(inst), confident_threshold=0.16).classify(crop(), 0, "feeder")
    assert r.species is None and r.no_bird is True


def test_strong_offlist_with_weak_regional_flags_no_bird():
    inst = _Inst(regional=[{"common_name": "House Finch", "raw_score": 12}],
                 unfiltered=[{"common_name": "Least Grebe", "raw_score": 180}])
    r = PiClassifier(_Registry(inst), confident_threshold=0.16).classify(crop(), 0, "feeder")
    assert r.species is None and r.no_bird


def test_weak_regional_but_no_offlist_mass_is_plain_none():
    inst = _Inst(regional=[{"common_name": "House Finch", "raw_score": 12}],
                 unfiltered=[{"common_name": "House Finch", "raw_score": 12}])
    r = PiClassifier(_Registry(inst), confident_threshold=0.16).classify(crop(), 0, "feeder")
    assert r.species is None and not r.no_bird


def test_confident_regional_votes_normally():
    inst = _Inst(regional=[{"common_name": "Tufted Titmouse", "raw_score": 200}],
                 unfiltered=[{"common_name": "Tufted Titmouse", "raw_score": 200}])
    r = PiClassifier(_Registry(inst), confident_threshold=0.16).classify(crop(), 0, "feeder")
    assert r.species == "Tufted Titmouse" and not r.no_bird and r.confidence > 0.5


def test_tta_runs_four_views_and_averages():
    inst = _Inst(regional=[{"common_name": "Downy Woodpecker", "raw_score": 160}],
                 unfiltered=[{"common_name": "Downy Woodpecker", "raw_score": 160}])
    pc = PiClassifier(_Registry(inst), confident_threshold=0.16)
    r = pc.classify(crop(), 0, "feeder", views=4)
    assert inst.calls == 4 and r.species == "Downy Woodpecker"
    # single view for the cheap votes
    inst.calls = 0
    pc.classify(crop(), 0, "feeder", views=1)
    assert inst.calls == 1


def test_registry_without_classify_full_still_works():
    class Plain:
        def classify(self, crop):
            return [{"common_name": "Blue Jay", "raw_score": 220}]
    reg = _Registry(Plain())
    r = PiClassifier(reg, confident_threshold=0.16).classify(crop(), 0, "feeder", views=2)
    assert r.species == "Blue Jay" and not r.no_bird
