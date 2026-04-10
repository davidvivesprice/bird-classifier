"""Tests for HealthState."""
import json


def test_health_state_defaults_to_ok():
    from pipeline.health import HealthState
    h = HealthState()
    assert h.snapshot()["overall"] == "ok"


def test_update_component_reflects_in_snapshot():
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {"fps": 4.9, "dropped_oldest": 0})
    snap = h.snapshot()
    assert snap["pipeline"]["feeder"]["capture"]["fps"] == 4.9


def test_degraded_when_fps_low():
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {"fps": 4.0, "dropped_oldest": 0, "last_frame_age_ms": 200})
    h.update("ground", "capture", {"fps": 4.9, "dropped_oldest": 0, "last_frame_age_ms": 200})
    snap = h.snapshot()
    # 4.0 is below 4.5 threshold → degraded
    assert snap["overall"] == "degraded"


def test_broken_when_camera_stale():
    from pipeline.health import HealthState
    h = HealthState()
    # Stale frame > 60s
    h.update("feeder", "capture", {"fps": 0, "dropped_oldest": 0, "last_frame_age_ms": 120_000})
    h.update("ground", "capture", {"fps": 4.9, "dropped_oldest": 0, "last_frame_age_ms": 200})
    snap = h.snapshot()
    assert snap["overall"] == "broken"


def test_snapshot_serializable():
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {"fps": 4.9})
    json.dumps(h.snapshot())  # should not raise
