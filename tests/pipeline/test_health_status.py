"""F3: a lone reader-restart storm is degraded, not broken; truthful key honored."""
from pipeline.health import HealthState


def _status(capture):
    hs = HealthState.__new__(HealthState)
    data = {"pipeline": {"feeder": {"capture": capture, "detector": {}, "classifier": {}}}}
    return hs._compute_status(data)


def test_restart_storm_alone_is_degraded_not_broken():
    # Fresh frames but a restart storm -> degraded (frames may still flow).
    assert _status({"last_frame_age_ms": 30, "reader_restarts_last_hour": 20}) == "degraded"


def test_no_storm_fresh_frames_is_ok():
    assert _status({"last_frame_age_ms": 30, "reader_restarts_last_hour": 0}) == "ok"


def test_legacy_ffmpeg_key_still_honored():
    # Back-compat: the old key name still drives the (now degraded) path.
    assert _status({"last_frame_age_ms": 30, "ffmpeg_restarts_last_hour": 20}) == "degraded"
