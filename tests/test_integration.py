"""Integration tests for shared inference pipeline."""
import pytest
import numpy as np


def test_detector_consistency(yolo_model_path, test_bird_image_pil):
    """YOLODetector produces consistent results on same input."""
    from bird_inference import YOLODetector
    detector = YOLODetector(yolo_model_path)
    d1 = detector.detect(test_bird_image_pil)
    d2 = detector.detect(test_bird_image_pil)
    assert len(d1) == len(d2)
    for a, b in zip(d1, d2):
        assert a["box"] == b["box"]
        assert abs(a["confidence"] - b["confidence"]) < 0.001


def test_full_pipeline(yolo_model_path, species_model_path, labels_path,
                       regional_species, test_bird_image_pil):
    """Full detection + classification pipeline produces valid output."""
    from bird_inference import YOLODetector, SpeciesClassifier, crop_bird
    detector = YOLODetector(yolo_model_path)
    classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)
    detections = detector.detect(test_bird_image_pil)
    if not detections:
        pytest.skip("No birds in test image")
    img_np = np.array(test_bird_image_pil)
    for det in detections:
        crop = crop_bird(img_np, det["box"])
        filtered, raw = classifier.classify(crop)
        assert len(raw) > 0
        assert raw[0]["common_name"]
        assert raw[0]["raw_score"] >= 0


def test_normalize_in_pipeline(yolo_model_path, species_model_path, labels_path,
                                regional_species, test_bird_image_pil):
    """Species aliases are applied during classification."""
    from bird_inference import SpeciesClassifier, YOLODetector, crop_bird, SPECIES_ALIASES
    detector = YOLODetector(yolo_model_path)
    classifier = SpeciesClassifier(species_model_path, labels_path, regional_species)
    detections = detector.detect(test_bird_image_pil)
    if not detections:
        pytest.skip("No birds in test image")
    crop = crop_bird(test_bird_image_pil, detections[0]["box"])
    filtered, raw = classifier.classify(crop)
    # No prediction should use an alias name (they should all be normalized)
    alias_names = set(SPECIES_ALIASES.keys())
    for pred in filtered + raw:
        assert pred["common_name"] not in alias_names, \
            f"Alias '{pred['common_name']}' should be normalized"


def test_crop_bird_pil_and_numpy(test_bird_image_pil):
    """crop_bird works with both PIL and numpy input."""
    from bird_inference import crop_bird
    import numpy as np
    box = [10, 10, 100, 100]
    # PIL path
    pil_crop = crop_bird(test_bird_image_pil, box)
    from PIL import Image
    assert isinstance(pil_crop, Image.Image)
    # numpy path
    np_img = np.array(test_bird_image_pil)
    np_crop = crop_bird(np_img, box)
    assert isinstance(np_crop, np.ndarray)
    # Both should produce same dimensions
    assert pil_crop.size[0] == np_crop.shape[1]  # width
    assert pil_crop.size[1] == np_crop.shape[0]  # height
