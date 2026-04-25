"""HailoClassifier must use HailoEngine — no private VDevice.

This test asserts the cohabitation we need on Hailo-8L: a HailoDetector
and a HailoClassifier in the same process create exactly ONE VDevice.
That removes the constraint that previously forced
build_default_registry(exclude_hailo=True) on the live pipeline side.
"""
from __future__ import annotations

import sys

import numpy as np
import pytest

# Reuse the detector's fake hailo_platform installer — same shape works
# for both the detector and classifier.
from tests.pipeline.test_hailo_detector_engine import _install_fake_hailo_for_detector  # noqa: E402


def test_classifier_uses_shared_engine(monkeypatch):
    fake = _install_fake_hailo_for_detector(monkeypatch)
    from pipeline.hailo_engine import HailoEngine
    HailoEngine._reset_for_testing()

    from pipeline.hailo_detector import HailoDetector
    from pipeline.hailo_classifier import HailoClassifier

    det = HailoDetector("/yolov8s.hef")
    cls = HailoClassifier("/resnet50.hef")

    assert len(fake._created_vdevices) == 1, (
        "Detector + Classifier must share ONE VDevice on Hailo-8L; got "
        f"{len(fake._created_vdevices)}"
    )
    vd = fake._created_vdevices[0]
    assert len(vd._models) == 2


def test_classifier_input_shape_pulled_from_engine(monkeypatch):
    """HailoClassifier exposes a PIL-order (w, h) input shape derived
    from the engine's HailoModel input_shape() (which is (h, w, c))."""
    fake = _install_fake_hailo_for_detector(monkeypatch)
    from pipeline.hailo_engine import HailoEngine
    HailoEngine._reset_for_testing()

    from pipeline.hailo_classifier import HailoClassifier

    cls = HailoClassifier("/cls.hef")
    # Detector fake's input shape is (640, 640, 3) → PIL order is (640, 640)
    assert cls._input_shape == (640, 640)
