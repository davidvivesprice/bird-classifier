# tests/test_rtsp_stream.py
"""Tests for rtsp_stream.py — RTSP stream manager."""
import json
import time
import pytest
from pathlib import Path


@pytest.fixture
def urls_dir(tmp_path):
    """Create a temp directory with rtsp_urls.json files for testing."""
    return tmp_path


def _write_urls(path, data):
    path.write_text(json.dumps(data))


class TestURLLoading:
    """Test URL loading from rtsp_urls.json."""

    def test_load_new_format_high(self, urls_dir):
        """New format: streams are dicts with high/low keys."""
        from rtsp_stream import RTSPStreamManager
        urls_file = urls_dir / "rtsp_urls.json"
        _write_urls(urls_file, {
            "updated": "2026-03-22T03:05:02",
            "streams": {
                "ground": {"high": "rtsp://host/ground-hi", "low": "rtsp://host/ground-lo"},
                "birds": {"high": "rtsp://host/birds-hi", "low": "rtsp://host/birds-lo"},
            }
        })
        mgr = RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(urls_file),
            sync_script="/nonexistent",
        )
        assert mgr._get_url("ground", "high") == "rtsp://host/ground-hi"
        assert mgr._get_url("ground", "low") == "rtsp://host/ground-lo"
        assert mgr._get_url("birds", "high") == "rtsp://host/birds-hi"

    def test_load_legacy_format(self, urls_dir):
        """Legacy format: streams are plain URL strings."""
        from rtsp_stream import RTSPStreamManager
        urls_file = urls_dir / "rtsp_urls.json"
        _write_urls(urls_file, {
            "streams": {
                "ground": "rtsp://host/ground-token",
                "birds": "rtsp://host/birds-token",
            }
        })
        mgr = RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(urls_file),
            sync_script="/nonexistent",
        )
        assert mgr._get_url("ground", "high") == "rtsp://host/ground-token"
        assert mgr._get_url("ground", "low") is None

    def test_missing_stream_name(self, urls_dir):
        """Missing stream name returns None (no crash)."""
        from rtsp_stream import RTSPStreamManager
        urls_file = urls_dir / "rtsp_urls.json"
        _write_urls(urls_file, {"streams": {"birds": "rtsp://host/birds"}})
        mgr = RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(urls_file),
            sync_script="/nonexistent",
        )
        assert mgr._get_url("ground", "high") is None
        assert mgr._get_url("birds", "high") == "rtsp://host/birds"

    def test_missing_file(self, tmp_path):
        """Missing JSON file doesn't crash, returns None for all URLs."""
        from rtsp_stream import RTSPStreamManager
        mgr = RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(tmp_path / "nonexistent.json"),
            sync_script="/nonexistent",
        )
        assert mgr._get_url("ground", "high") is None

    def test_corrupted_file(self, urls_dir):
        """Corrupted JSON file doesn't crash."""
        from rtsp_stream import RTSPStreamManager
        urls_file = urls_dir / "rtsp_urls.json"
        urls_file.write_text("not json{{{")
        mgr = RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(urls_file),
            sync_script="/nonexistent",
        )
        assert mgr._get_url("ground", "high") is None

    def test_reload_urls(self, urls_dir):
        """URLs are re-read from file when reload_urls() is called."""
        from rtsp_stream import RTSPStreamManager
        urls_file = urls_dir / "rtsp_urls.json"
        _write_urls(urls_file, {"streams": {"ground": "rtsp://host/old"}})
        mgr = RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(urls_file),
            sync_script="/nonexistent",
        )
        assert mgr._get_url("ground", "high") == "rtsp://host/old"

        _write_urls(urls_file, {"streams": {"ground": "rtsp://host/new"}})
        mgr._reload_urls()
        assert mgr._get_url("ground", "high") == "rtsp://host/new"
