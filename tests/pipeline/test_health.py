"""Tests for HealthState."""
import json
from unittest.mock import patch


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


def test_degraded_when_drop_rate_high():
    """dropped_oldest / frames_processed > 5% → degraded."""
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {
        "frames_processed": 100, "dropped_oldest": 10,
        "last_frame_age_ms": 200, "ffmpeg_restarts_last_hour": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "degraded"


def test_broken_when_camera_stale_daytime():
    """Stale frame > 60s during daytime → broken."""
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {
        "frames_processed": 0, "dropped_oldest": 0,
        "last_frame_age_ms": 120_000, "ffmpeg_restarts_last_hour": 0,
    })
    h.update("ground", "capture", {
        "frames_processed": 100, "dropped_oldest": 0,
        "last_frame_age_ms": 200, "ffmpeg_restarts_last_hour": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "broken"


def test_snapshot_serializable():
    from pipeline.health import HealthState
    h = HealthState()
    h.update("feeder", "capture", {"fps": 4.9})
    json.dumps(h.snapshot())  # should not raise
