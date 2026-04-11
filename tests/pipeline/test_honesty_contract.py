"""Honesty contract tests — every metric must respond correctly to fabricated broken state.

Each test fabricates a broken state and asserts the metric detects it.
Each test also asserts the metric reads correctly in a healthy state.

Spec: docs/superpowers/specs/2026-04-11-live-detection-v3-design.md §6
"""
import time

import pytest


def _make_health():
    from pipeline.health import HealthState
    return HealthState()


def _populate_healthy(h, camera="feeder"):
    """Populate a camera with a fully-healthy metric set."""
    h.update(camera, "capture", {
        "frames_processed": 100,
        "last_frame_age_ms": 500,
        "ffmpeg_restarts": 0,
        "ffmpeg_restarts_last_hour": 0,
        "dropped_oldest": 0,
    })
    h.update(camera, "detector", {
        "yolo_ms_avg": 50,
        "yolo_ms_p99": 100,
        "yolo_samples_count": 50,
        "detections_total": 10,
    })
    h.update(camera, "tracker", {
        "active_tracks": 0,
        "stationary_tracks": 0,
    })
    h.update(camera, "classifier", {
        "yard": 10, "aiy": 2, "both_agree": 0,
        "unlabeled_call": 0, "lock_timeouts": 0, "retries": 0,
    })


# --- Basic healthy state ---

def test_all_healthy_reports_ok():
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "ok", f"expected ok, got {snap['overall']}"


# --- Daytime stale frames ---

def test_last_frame_age_broken_when_daytime_stall():
    """last_frame_age_ms > 60000 during daytime → broken."""
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    h.update("feeder", "capture", {
        "frames_processed": 100,
        "last_frame_age_ms": 70000,  # stalled > 60s
        "ffmpeg_restarts": 0,
        "ffmpeg_restarts_last_hour": 0,
        "dropped_oldest": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "broken", f"expected broken, got {snap['overall']}"


def test_last_frame_age_ok_at_night():
    """Same stall is acceptable at night (pipeline paused)."""
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    h.update("feeder", "capture", {
        "frames_processed": 100,
        "last_frame_age_ms": 70000,
        "ffmpeg_restarts": 0,
        "ffmpeg_restarts_last_hour": 0,
        "dropped_oldest": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=True, create=True):
        snap = h.snapshot()
    # At night, stalled captures shouldn't be flagged as broken
    assert snap["overall"] in ("ok", "degraded"), f"got {snap['overall']} at night"


# --- ffmpeg restart storm ---

def test_ffmpeg_restart_storm_marks_broken():
    """ffmpeg_restarts_last_hour > 10 → broken."""
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    h.update("feeder", "capture", {
        "frames_processed": 100,
        "last_frame_age_ms": 500,
        "ffmpeg_restarts": 11,
        "ffmpeg_restarts_last_hour": 11,
        "dropped_oldest": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "broken"


# --- YOLO p99 tail ---

def test_yolo_p99_tail_degraded():
    """yolo_ms_p99 > 1000 → degraded."""
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    h.update("feeder", "detector", {
        "yolo_ms_avg": 200,
        "yolo_ms_p99": 1500,
        "yolo_samples_count": 50,
        "detections_total": 10,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "degraded"


def test_yolo_p99_none_does_not_crash_status():
    """yolo_ms_p99 = None (insufficient samples) must not trigger broken/degraded."""
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    h.update("feeder", "detector", {
        "yolo_ms_avg": 50,
        "yolo_ms_p99": None,  # insufficient samples
        "yolo_samples_count": 3,
        "detections_total": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    # None p99 should not be treated as > 1000
    assert snap["overall"] in ("ok", "degraded")  # depends on other checks, but NOT broken from p99


# --- Dropped frame rate ---

def test_dropped_oldest_threshold_degraded():
    """dropped_oldest / frames_processed > 5% → degraded."""
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    h.update("feeder", "capture", {
        "frames_processed": 100,
        "dropped_oldest": 10,  # 10% drop rate
        "last_frame_age_ms": 500,
        "ffmpeg_restarts": 0,
        "ffmpeg_restarts_last_hour": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "degraded"


# --- Coral lock storm ---

def test_lock_timeouts_degraded():
    """classifier.lock_timeouts > 5 → degraded."""
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    h.update("feeder", "classifier", {
        "yard": 10, "aiy": 2, "both_agree": 0,
        "unlabeled_call": 0, "lock_timeouts": 6, "retries": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "degraded"


# --- Multi-rule precedence ---

def test_worst_state_wins():
    """When multiple rules fire at different severities, the worst wins."""
    from unittest.mock import patch
    h = _make_health()
    _populate_healthy(h)
    # degraded: p99 tail
    h.update("feeder", "detector", {
        "yolo_ms_avg": 200, "yolo_ms_p99": 1500,
        "yolo_samples_count": 50, "detections_total": 10,
    })
    # broken: daytime stall
    h.update("feeder", "capture", {
        "frames_processed": 100, "last_frame_age_ms": 70000,
        "ffmpeg_restarts": 0, "ffmpeg_restarts_last_hour": 0, "dropped_oldest": 0,
    })
    with patch("pipeline.health.is_nighttime", return_value=False, create=True):
        snap = h.snapshot()
    assert snap["overall"] == "broken", f"broken should beat degraded, got {snap['overall']}"
