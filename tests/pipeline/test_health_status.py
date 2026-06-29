"""F3: the truthful reader_restarts_last_hour key is honored by the status logic.

The honesty contract (test_honesty_contract.py) stands: a sustained restart
storm -> broken. F3 only adds the truthful key name (the Pi uses PyAV, not an
ffmpeg subprocess); the legacy ffmpeg_restarts_last_hour key still works.
"""
from pipeline.health import HealthState


def _status(capture):
    hs = HealthState.__new__(HealthState)
    data = {"pipeline": {"feeder": {"capture": capture, "detector": {}, "classifier": {}}}}
    return hs._compute_status(data)


def test_reader_restart_storm_marks_broken():
    # The truthful key drives the same broken contract as the legacy key.
    assert _status({"last_frame_age_ms": 30, "reader_restarts_last_hour": 20}) == "broken"


def test_legacy_ffmpeg_key_still_honored():
    assert _status({"last_frame_age_ms": 30, "ffmpeg_restarts_last_hour": 20}) == "broken"


def test_no_storm_fresh_frames_is_ok():
    assert _status({"last_frame_age_ms": 30, "reader_restarts_last_hour": 0}) == "ok"
