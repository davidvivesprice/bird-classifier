"""Tests for HlsRecorder."""
import os
import time
from pathlib import Path
import pytest


def test_ffmpeg_cmd_uses_copy_mode():
    """Recorder should use stream copy (no decode/re-encode)."""
    from pipeline.hls_recorder import HlsRecorder
    r = HlsRecorder("feeder", "rtsp://x/y", "/tmp/hls-test")
    cmd = r._build_cmd()
    assert "-c" in cmd
    assert cmd[cmd.index("-c") + 1] == "copy"
    assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "hls"
    assert "-hls_time" in cmd


def test_cleanup_old_chunks(tmp_path):
    """Files older than retention_days should be deleted."""
    from pipeline.hls_recorder import HlsRecorder
    hls_root = tmp_path / "hls"
    (hls_root / "feeder").mkdir(parents=True)
    old_file = hls_root / "feeder" / "old.ts"
    new_file = hls_root / "feeder" / "new.ts"
    old_file.write_bytes(b"old")
    new_file.write_bytes(b"new")
    # Make old_file old
    old_mtime = time.time() - 10 * 86400
    os.utime(old_file, (old_mtime, old_mtime))

    HlsRecorder.cleanup_old_chunks(hls_root, retention_days=7)

    assert not old_file.exists()
    assert new_file.exists()


def test_output_dir_is_created(tmp_path):
    from pipeline.hls_recorder import HlsRecorder
    out = tmp_path / "new_dir"
    r = HlsRecorder("feeder", "rtsp://x/y", str(out))
    assert out.exists()
