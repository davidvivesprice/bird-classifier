"""Tests for bird_inference utility functions."""

import numpy as np
import pytest
from PIL import Image as PILImage

from bird_inference import (
    SPECIES_ALIASES,
    crop_bird,
    get_providers,
    normalize_species,
    parse_label,
)


# ── normalize_species ──────────────────────────────────────────────────────

class TestNormalizeSpecies:
    def test_slate_colored_junco(self):
        assert normalize_species("Slate-colored Junco") == "Dark-eyed Junco"

    def test_myrtle_warbler(self):
        assert normalize_species("Myrtle Warbler") == "Yellow-rumped Warbler"

    def test_feral_pigeon(self):
        assert normalize_species("Feral Pigeon") == "Rock Pigeon"

    def test_yellow_shafted_flicker(self):
        assert normalize_species("Yellow-shafted Flicker") == "Northern Flicker"

    def test_passthrough_unknown_species(self):
        assert normalize_species("American Robin") == "American Robin"

    def test_empty_string(self):
        assert normalize_species("") == ""

    def test_all_four_aliases_present(self):
        assert len(SPECIES_ALIASES) >= 4


# ── parse_label ────────────────────────────────────────────────────────────

class TestParseLabel:
    def test_standard_format(self):
        scientific, common = parse_label("Turdus migratorius (American Robin)")
        assert scientific == "Turdus migratorius"
        assert common == "American Robin"

    def test_nested_parens_bug_case(self):
        # The classify.py version (split("(")[0]) would return "Hawk " as scientific
        # and "Cooper" as common — wrong.  The correct version uses rindex.
        scientific, common = parse_label("Accipiter cooperii (Cooper's Hawk)")
        assert scientific == "Accipiter cooperii"
        assert common == "Cooper's Hawk"

    def test_label_with_parens_in_species_name(self):
        # Pathological case: paren in scientific portion
        # "Hawk (Cooper's) (Accipiter cooperii)" → splits on last "("
        scientific, common = parse_label("Hawk (Cooper's) (Accipiter cooperii)")
        assert scientific == "Hawk (Cooper's)"
        assert common == "Accipiter cooperii"

    def test_no_parens_returns_raw_twice(self):
        scientific, common = parse_label("UnknownLabel")
        assert scientific == "UnknownLabel"
        assert common == "UnknownLabel"

    def test_empty_string_returns_empty_twice(self):
        scientific, common = parse_label("")
        assert scientific == ""
        assert common == ""

    def test_strips_whitespace_from_scientific(self):
        scientific, common = parse_label("Parus major (Great Tit)")
        assert scientific == "Parus major"
        assert not scientific.endswith(" ")


# ── crop_bird ──────────────────────────────────────────────────────────────

class TestCropBird:
    def _make_pil(self, width=200, height=150):
        return PILImage.new("RGB", (width, height), color=(128, 64, 32))

    def _make_numpy(self, width=200, height=150):
        return np.zeros((height, width, 3), dtype=np.uint8)

    def test_crop_pil_image_returns_pil(self):
        img = self._make_pil()
        result = crop_bird(img, [50, 40, 100, 90])
        assert isinstance(result, PILImage.Image)

    def test_crop_numpy_array_returns_numpy(self):
        arr = self._make_numpy()
        result = crop_bird(arr, [50, 40, 100, 90])
        assert isinstance(result, np.ndarray)

    def test_crop_pil_with_padding(self):
        img = self._make_pil(200, 150)
        # box 50x50 centred in image, pad_ratio=0.1 → 5px pad each side
        result = crop_bird(img, [75, 50, 125, 100], pad_ratio=0.1)
        w, h = result.size
        assert w == 60   # 125+5 - (75-5) = 60
        assert h == 60

    def test_crop_numpy_with_padding(self):
        arr = self._make_numpy(200, 150)
        result = crop_bird(arr, [75, 50, 125, 100], pad_ratio=0.1)
        assert result.shape == (60, 60, 3)

    def test_clamps_to_image_bounds_pil(self):
        img = self._make_pil(100, 80)
        # Box at the very edge — padding would go negative / beyond size
        result = crop_bird(img, [0, 0, 20, 20], pad_ratio=0.5)
        w, h = result.size
        assert w >= 1 and h >= 1
        assert w <= 100 and h <= 80

    def test_clamps_to_image_bounds_numpy(self):
        arr = self._make_numpy(100, 80)
        result = crop_bird(arr, [90, 70, 100, 80], pad_ratio=0.5)
        assert result.shape[0] >= 1 and result.shape[1] >= 1
        assert result.shape[0] <= 80 and result.shape[1] <= 100


# ── get_providers ──────────────────────────────────────────────────────────

class TestGetProviders:
    def test_returns_list(self):
        providers = get_providers()
        assert isinstance(providers, list)

    def test_always_includes_cpu(self):
        providers = get_providers()
        assert "CPUExecutionProvider" in providers

    def test_cpu_is_last_or_only(self):
        providers = get_providers()
        # CPUExecutionProvider should be the fallback (last element or only element)
        assert providers[-1] == "CPUExecutionProvider"

    def test_non_empty(self):
        providers = get_providers()
        assert len(providers) >= 1


# ── YOLODetector ──────────────────────────────────────────────────────────

class TestYOLODetector:
    def test_init(self, yolo_model_path):
        from bird_inference import YOLODetector
        detector = YOLODetector(yolo_model_path)
        assert detector is not None

    def test_detect_returns_list(self, yolo_model_path, test_bird_image_pil):
        from bird_inference import YOLODetector
        detector = YOLODetector(yolo_model_path)
        detections = detector.detect(test_bird_image_pil)
        assert isinstance(detections, list)

    def test_detection_has_required_fields(self, yolo_model_path, test_bird_image_pil):
        from bird_inference import YOLODetector
        detector = YOLODetector(yolo_model_path)
        detections = detector.detect(test_bird_image_pil)
        if len(detections) > 0:
            det = detections[0]
            assert "box" in det
            assert "confidence" in det
            assert len(det["box"]) == 4
            assert 0 < det["confidence"] <= 1.0

    def test_detect_empty_image(self, yolo_model_path):
        from PIL import Image
        from bird_inference import YOLODetector
        detector = YOLODetector(yolo_model_path)
        black = Image.new("RGB", (640, 640), (0, 0, 0))
        detections = detector.detect(black)
        assert isinstance(detections, list)

    def test_confidence_threshold(self, yolo_model_path, test_bird_image_pil):
        from bird_inference import YOLODetector
        det_low = YOLODetector(yolo_model_path, confidence=0.1).detect(test_bird_image_pil)
        det_high = YOLODetector(yolo_model_path, confidence=0.9).detect(test_bird_image_pil)
        assert len(det_low) >= len(det_high)


# ── SpeciesClassifier ─────────────────────────────────────────────────────

class TestSpeciesClassifier:
    def test_init(self, species_model_path, labels_path):
        from bird_inference import SpeciesClassifier
        classifier = SpeciesClassifier(species_model_path, labels_path)
        assert classifier is not None
        assert len(classifier.labels) > 900

    def test_classify_returns_tuple(self, species_model_path, labels_path, regional_species,
                                     yolo_model_path, test_bird_image_pil):
        from bird_inference import SpeciesClassifier, YOLODetector, crop_bird
        detector = YOLODetector(yolo_model_path)
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)
        detections = detector.detect(test_bird_image_pil)
        if not detections:
            pytest.skip("No birds detected in test image")
        crop = crop_bird(test_bird_image_pil, detections[0]["box"])
        filtered, raw = classifier.classify(crop)
        assert isinstance(filtered, list)
        assert isinstance(raw, list)
        assert len(raw) > 0

    def test_prediction_has_fields(self, species_model_path, labels_path, regional_species,
                                    yolo_model_path, test_bird_image_pil):
        from bird_inference import SpeciesClassifier, YOLODetector, crop_bird
        detector = YOLODetector(yolo_model_path)
        classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)
        detections = detector.detect(test_bird_image_pil)
        if not detections:
            pytest.skip("No birds detected in test image")
        crop = crop_bird(test_bird_image_pil, detections[0]["box"])
        filtered, raw = classifier.classify(crop)
        if filtered:
            pred = filtered[0]
            assert "common_name" in pred
            assert "scientific_name" in pred
            assert "raw_score" in pred
            assert "index" in pred  # Errata E5
            assert "label" in pred  # Errata E5

    def test_classify_uses_uint8_input(self, species_model_path, labels_path):
        import numpy as np
        from bird_inference import SpeciesClassifier
        classifier = SpeciesClassifier(species_model_path, labels_path)
        dummy = np.random.randint(0, 255, (224, 224, 3), dtype=np.uint8)
        filtered, raw = classifier.classify(dummy)
        assert isinstance(raw, list)
