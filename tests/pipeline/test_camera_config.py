"""Tests for CameraClassifierConfig dataclass."""
from pipeline.camera_config import CameraClassifierConfig


def test_feeder_config_default_thresholds():
    cfg = CameraClassifierConfig(use_yard=True)
    assert cfg.use_yard is True
    assert cfg.confident_threshold == 0.6
    assert cfg.uncertain_low == 0.3


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
