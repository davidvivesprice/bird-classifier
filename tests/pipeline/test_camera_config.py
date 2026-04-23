"""Tests for CameraClassifierConfig dataclass."""
from pipeline.camera_config import CameraClassifierConfig


def test_feeder_config_default_thresholds():
    # 2026-04-18: thresholds recalibrated after yard-overconfidence fix
    # applied T=100 temperature scaling in yard_classifier.py. Post-T=100,
    # confident peaks sit ~0.45-0.54, uncertain band ~0.10-0.25, genuine
    # ties below 0.10. See project_forget_me_nots.md "YARD MODEL
    # OVERCONFIDENCE — PARTIALLY ADDRESSED" for the full trace.
    cfg = CameraClassifierConfig(use_yard=True)
    assert cfg.use_yard is True
    assert cfg.confident_threshold == 0.25
    assert cfg.uncertain_low == 0.1


def test_ground_config_skips_yard():
    cfg = CameraClassifierConfig(use_yard=False)
    assert cfg.use_yard is False


def test_config_is_frozen():
    import dataclasses
    cfg = CameraClassifierConfig(use_yard=True)
    try:
        cfg.use_yard = False
    except dataclasses.FrozenInstanceError:
        pass
    else:
        assert False, "CameraClassifierConfig should be frozen"
