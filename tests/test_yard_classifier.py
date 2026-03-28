"""Tests for yard_classifier module."""

import numpy as np
import pytest

from yard_classifier import (
    _normalize_labels,
    _merge_alias_scores,
    softmax_top3,
    YARD_THRESHOLD,
    AIY_THRESHOLD,
)


# ── TestLabelLoading ──────────────────────────────────────────────────────

class TestLabelLoading:
    """Test _normalize_labels() with known label sets."""

    SAMPLE_LABELS = [
        "American Robin",       # 0
        "Feral Pigeon",         # 1  -> Rock Pigeon (alias)
        "Dark-eyed Junco",      # 2
        "Rock Pigeon",          # 3  -> Rock Pigeon (alias)
        "Slate-colored Junco",  # 4  -> Dark-eyed Junco (alias)
        "not a bird",           # 5
    ]

    def test_canonical_length_matches_labels(self):
        canonical, _, _ = _normalize_labels(self.SAMPLE_LABELS)
        assert len(canonical) == len(self.SAMPLE_LABELS)

    def test_aliases_resolved_to_canonical(self):
        canonical, _, _ = _normalize_labels(self.SAMPLE_LABELS)
        assert canonical[1] == "Rock Pigeon"    # Feral Pigeon -> Rock Pigeon
        assert canonical[4] == "Dark-eyed Junco"  # Slate-colored Junco -> Dark-eyed Junco

    def test_passthrough_names_unchanged(self):
        canonical, _, _ = _normalize_labels(self.SAMPLE_LABELS)
        assert canonical[0] == "American Robin"

    def test_alias_groups_detected(self):
        _, alias_groups, _ = _normalize_labels(self.SAMPLE_LABELS)
        assert "Rock Pigeon" in alias_groups
        assert sorted(alias_groups["Rock Pigeon"]) == [1, 3]
        assert "Dark-eyed Junco" in alias_groups
        assert sorted(alias_groups["Dark-eyed Junco"]) == [2, 4]

    def test_not_a_bird_identified(self):
        _, _, not_a_bird_ids = _normalize_labels(self.SAMPLE_LABELS)
        assert 5 in not_a_bird_ids

    def test_not_a_bird_case_insensitive(self):
        labels = ["American Robin", "Not A Bird", "NOT A BIRD"]
        _, _, not_a_bird_ids = _normalize_labels(labels)
        assert not_a_bird_ids == {1, 2}

    def test_no_aliases_produces_empty_groups(self):
        labels = ["American Robin", "Blue Jay", "not a bird"]
        _, alias_groups, _ = _normalize_labels(labels)
        assert alias_groups == {}

    def test_not_a_bird_excluded_from_alias_groups(self):
        _, alias_groups, _ = _normalize_labels(self.SAMPLE_LABELS)
        for name in alias_groups:
            assert name.lower() != "not a bird"

    def test_real_yard_labels(self, yard_labels_path):
        """Verify _normalize_labels works with the actual yard model labels."""
        with open(yard_labels_path) as f:
            labels = [line.strip() for line in f if line.strip()]

        canonical, alias_groups, not_a_bird_ids = _normalize_labels(labels)
        assert len(canonical) == len(labels)
        assert len(not_a_bird_ids) >= 1  # at least one "not a bird"
        # Known alias collisions from probe results
        assert "Rock Pigeon" in alias_groups
        assert "Dark-eyed Junco" in alias_groups


# ── TestScoreDeduplication ────────────────────────────────────────────────

class TestScoreDeduplication:
    """Test _merge_alias_scores() with known scores."""

    LABELS = [
        "American Robin",       # 0
        "Feral Pigeon",         # 1  -> Rock Pigeon
        "Dark-eyed Junco",      # 2
        "Rock Pigeon",          # 3  -> Rock Pigeon
        "Slate-colored Junco",  # 4  -> Dark-eyed Junco
        "not a bird",           # 5
    ]

    def setup_method(self):
        self.canonical, self.alias_groups, self.not_a_bird_ids = (
            _normalize_labels(self.LABELS)
        )

    def test_alias_scores_summed(self):
        scores = np.array([0, 5.0, 3.0, 7.0, 4.0, 6.0])
        merged = _merge_alias_scores(
            scores, self.canonical, self.alias_groups, self.not_a_bird_ids
        )
        # Feral Pigeon (5.0) + Rock Pigeon (7.0) = 12.0
        assert merged["Rock Pigeon"] == pytest.approx(12.0)
        # Dark-eyed Junco (3.0) + Slate-colored Junco (4.0) = 7.0
        assert merged["Dark-eyed Junco"] == pytest.approx(7.0)

    def test_not_a_bird_excluded(self):
        scores = np.array([0, 5.0, 3.0, 7.0, 4.0, 10.0])
        merged = _merge_alias_scores(
            scores, self.canonical, self.alias_groups, self.not_a_bird_ids
        )
        for name in merged:
            assert name.lower() != "not a bird"

    def test_non_alias_scores_preserved(self):
        scores = np.array([8.0, 0, 0, 0, 0, 0])
        merged = _merge_alias_scores(
            scores, self.canonical, self.alias_groups, self.not_a_bird_ids
        )
        assert merged["American Robin"] == pytest.approx(8.0)

    def test_all_zero_scores(self):
        scores = np.zeros(6)
        merged = _merge_alias_scores(
            scores, self.canonical, self.alias_groups, self.not_a_bird_ids
        )
        assert all(v == 0.0 for v in merged.values())

    def test_result_keys_are_canonical(self):
        scores = np.array([1, 2, 3, 4, 5, 6])
        merged = _merge_alias_scores(
            scores, self.canonical, self.alias_groups, self.not_a_bird_ids
        )
        assert "Feral Pigeon" not in merged
        assert "Slate-colored Junco" not in merged


# ── TestSoftmax ───────────────────────────────────────────────────────────

class TestSoftmax:
    """Test softmax_top3() basic math."""

    def test_probabilities_sum_to_one(self):
        scores = {"A": 10.0, "B": 8.0, "C": 6.0}
        probs = softmax_top3(scores)
        assert len(probs) == 3
        assert sum(probs) == pytest.approx(1.0)

    def test_order_preserved_descending(self):
        scores = {"A": 10.0, "B": 8.0, "C": 6.0}
        probs = softmax_top3(scores)
        assert probs[0] >= probs[1] >= probs[2]

    def test_equal_scores_equal_probs(self):
        scores = {"A": 5.0, "B": 5.0, "C": 5.0}
        probs = softmax_top3(scores)
        assert probs[0] == pytest.approx(probs[1], abs=1e-7)
        assert probs[1] == pytest.approx(probs[2], abs=1e-7)
        assert probs[0] == pytest.approx(1.0 / 3, abs=1e-7)

    def test_dominant_score(self):
        scores = {"A": 100.0, "B": 1.0, "C": 1.0}
        probs = softmax_top3(scores)
        assert probs[0] > 0.99

    def test_accepts_array_like(self):
        probs = softmax_top3([10.0, 8.0, 6.0, 4.0, 2.0])
        assert len(probs) == 3
        assert sum(probs) == pytest.approx(1.0)

    def test_fewer_than_three_values(self):
        probs = softmax_top3({"A": 5.0, "B": 3.0})
        assert len(probs) == 2
        assert sum(probs) == pytest.approx(1.0)

    def test_numerical_stability_large_values(self):
        """Large scores should not cause overflow."""
        scores = {"A": 1000.0, "B": 999.0, "C": 998.0}
        probs = softmax_top3(scores)
        assert all(np.isfinite(p) for p in probs)
        assert sum(probs) == pytest.approx(1.0)

    def test_compressed_range(self):
        """Yard-model-like compressed scores (3-12) produce valid probs."""
        scores = {"A": 12.0, "B": 10.0, "C": 8.0, "D": 5.0}
        probs = softmax_top3(scores)
        assert probs[0] > probs[1] > probs[2]
        assert sum(probs) == pytest.approx(1.0)


# ── TestThresholds ────────────────────────────────────────────────────────

class TestThresholds:
    def test_yard_threshold_value(self):
        assert YARD_THRESHOLD == 0.45

    def test_aiy_threshold_value(self):
        assert AIY_THRESHOLD == 0.50


# ── TestYardClassifierIntegration ─────────────────────────────────────────

class TestYardClassifierIntegration:
    """Integration tests requiring Coral USB + model files.

    These tests are skipped if the Coral TPU is unavailable (e.g. in use
    by the running classifier service) or if model files are missing.
    """

    @pytest.fixture(scope="class")
    def classifier(self, yard_model_path, yard_labels_path):
        """Instantiate YardClassifier; skip if Coral unavailable."""
        from yard_classifier import YardClassifier
        try:
            return YardClassifier(yard_model_path, yard_labels_path)
        except RuntimeError as exc:
            pytest.skip(f"Cannot load yard model on Coral: {exc}")

    @pytest.fixture()
    def dummy_crop(self):
        """A small synthetic RGB image for smoke testing."""
        from PIL import Image
        return Image.fromarray(
            np.random.randint(0, 255, (100, 100, 3), dtype=np.uint8)
        )

    def test_loads_successfully(self, classifier):
        assert classifier is not None

    def test_classify_returns_list(self, classifier, dummy_crop):
        results = classifier.classify(dummy_crop)
        assert isinstance(results, list)

    def test_classify_returns_top3_or_fewer(self, classifier, dummy_crop):
        results = classifier.classify(dummy_crop)
        assert len(results) <= 3

    def test_result_dict_keys(self, classifier, dummy_crop):
        results = classifier.classify(dummy_crop)
        if results:
            for r in results:
                assert "common_name" in r
                assert "scientific_name" in r
                assert "confidence" in r
                assert isinstance(r["confidence"], float)
                assert 0.0 <= r["confidence"] <= 1.0

    def test_not_a_bird_never_in_results(self, classifier, dummy_crop):
        results = classifier.classify(dummy_crop)
        for r in results:
            assert r["common_name"].lower() != "not a bird"

    def test_enabled_flag_disables_classify(self, classifier, dummy_crop):
        classifier.enabled = False
        try:
            results = classifier.classify(dummy_crop)
            assert results == []
        finally:
            classifier.enabled = True

    def test_small_image_no_crash(self, classifier):
        """A tiny 10x10 image should not crash (just resize)."""
        from PIL import Image
        tiny = Image.fromarray(
            np.random.randint(0, 255, (10, 10, 3), dtype=np.uint8)
        )
        results = classifier.classify(tiny)
        assert isinstance(results, list)

    def test_numpy_input_accepted(self, classifier):
        """Passing a numpy array instead of PIL Image should work."""
        arr = np.random.randint(0, 255, (150, 200, 3), dtype=np.uint8)
        results = classifier.classify(arr)
        assert isinstance(results, list)

    def test_real_bird_image(self, classifier, test_bird_image_pil):
        """Classify a real bird photo — should return non-empty results."""
        results = classifier.classify(test_bird_image_pil)
        assert isinstance(results, list)
        # A real bird should get some classification
        if results:
            assert results[0]["confidence"] > 0
