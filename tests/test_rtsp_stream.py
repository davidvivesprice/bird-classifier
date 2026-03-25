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


class TestEscalation:
    """Test the escalation ladder state machine."""

    def _make_manager(self, urls_dir):
        from rtsp_stream import RTSPStreamManager
        urls_file = urls_dir / "rtsp_urls.json"
        _write_urls(urls_file, {
            "streams": {
                "ground": {"high": "rtsp://host/g-hi", "low": "rtsp://host/g-lo"},
                "birds": {"high": "rtsp://host/b-hi", "low": "rtsp://host/b-lo"},
            }
        })
        return RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(urls_file),
            sync_script="/bin/true",
            health_dir=str(urls_dir),
        )

    def test_initial_state(self, urls_dir):
        mgr = self._make_manager(urls_dir)
        assert mgr._level == 1
        assert mgr._current_stream == "ground"
        assert mgr._current_quality == "high"

    def test_level1_retries(self, urls_dir):
        """Failures 1-3 stay at level 1 with increasing backoff."""
        from rtsp_stream import RETRY_MAX
        mgr = self._make_manager(urls_dir)
        for i in range(RETRY_MAX):
            mgr.report_failure(Exception("test"))
            assert mgr._level == 1
            assert mgr._current_stream == "ground"
            assert mgr._current_quality == "high"

    def test_level2_refresh(self, urls_dir):
        """Failure RETRY_MAX+1 triggers URL refresh (level 2)."""
        from rtsp_stream import RETRY_MAX
        mgr = self._make_manager(urls_dir)
        for i in range(RETRY_MAX):
            mgr.report_failure(Exception("test"))
        mgr.report_failure(Exception("test"))
        assert mgr._level == 2

    def test_level3_lowres(self, urls_dir):
        """After refresh fails, try low-res."""
        from rtsp_stream import RETRY_MAX
        mgr = self._make_manager(urls_dir)
        for _ in range(RETRY_MAX):
            mgr.report_failure(Exception("test"))
        mgr.report_failure(Exception("test"))
        mgr.report_failure(Exception("test"))
        assert mgr._level == 3
        assert mgr._current_quality == "low"

    def test_level4_fallback(self, urls_dir):
        """After low-res fails, switch to fallback camera."""
        from rtsp_stream import RETRY_MAX, LOW_RES_MAX
        mgr = self._make_manager(urls_dir)
        # Level 1: RETRY_MAX retries
        for _ in range(RETRY_MAX):
            mgr.report_failure(Exception("test"))
        # Level 2: refresh
        mgr.report_failure(Exception("test"))
        # Level 3: LOW_RES_MAX attempts
        for _ in range(LOW_RES_MAX):
            mgr.report_failure(Exception("test"))
        assert mgr._level == 3  # still in low-res
        # One more triggers fallback
        mgr.report_failure(Exception("test"))
        assert mgr._level == 4
        assert mgr._current_stream == "birds"

    def test_success_resets_on_primary(self, urls_dir):
        """Successful connection on primary resets to level 1."""
        mgr = self._make_manager(urls_dir)
        mgr.report_failure(Exception("test"))
        mgr.report_failure(Exception("test"))
        assert mgr._failure_count == 2
        mgr.report_success()
        assert mgr._level == 1
        assert mgr._failure_count == 0
        assert mgr._current_stream == "ground"
        assert mgr._current_quality == "high"

    def test_success_preserves_fallback(self, urls_dir):
        """Successful connection on fallback stays at level 4, enables probes."""
        from rtsp_stream import RETRY_MAX, LOW_RES_MAX
        mgr = self._make_manager(urls_dir)
        for _ in range(RETRY_MAX + 1 + LOW_RES_MAX + 2):
            mgr.report_failure(Exception("test"))
        assert mgr._current_stream == "birds"
        mgr.report_success()
        assert mgr._level == 4
        assert mgr._current_stream == "birds"
        assert mgr._failure_count == 0

    def test_switch_to_primary(self, urls_dir):
        """switch_to_primary() resets everything back to preferred stream."""
        mgr = self._make_manager(urls_dir)
        mgr._level = 4
        mgr._current_stream = "birds"
        mgr.switch_to_primary()
        assert mgr._level == 1
        assert mgr._current_stream == "ground"
        assert mgr._current_quality == "high"

    def test_level6_down(self, urls_dir):
        """After fallback exhausts all levels, enter down state."""
        from rtsp_stream import RETRY_MAX, LOW_RES_MAX
        mgr = self._make_manager(urls_dir)
        for _ in range(RETRY_MAX + 1 + LOW_RES_MAX + 2):
            mgr.report_failure(Exception("test"))
        assert mgr._current_stream == "birds"
        for _ in range(RETRY_MAX + 1 + LOW_RES_MAX + 2):
            mgr.report_failure(Exception("test"))
        assert mgr._level == 6
        assert mgr._current_stream == "ground"

    def test_get_next_url_follows_escalation(self, urls_dir):
        """get_next_url returns the right URL for current escalation state."""
        mgr = self._make_manager(urls_dir)
        assert mgr.get_next_url() == "rtsp://host/g-hi"
        mgr._level = 3
        mgr._current_quality = "low"
        assert mgr.get_next_url() == "rtsp://host/g-lo"
        mgr._level = 4
        mgr._current_stream = "birds"
        mgr._current_quality = "high"
        assert mgr.get_next_url() == "rtsp://host/b-hi"


class TestHealth:
    """Test health file writing."""

    def _make_manager(self, urls_dir):
        from rtsp_stream import RTSPStreamManager
        urls_file = urls_dir / "rtsp_urls.json"
        _write_urls(urls_file, {
            "streams": {
                "ground": {"high": "rtsp://host/g-hi", "low": "rtsp://host/g-lo"},
                "birds": {"high": "rtsp://host/b-hi", "low": "rtsp://host/b-lo"},
            }
        })
        return RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(urls_file),
            sync_script="/bin/true",
            health_dir=str(urls_dir),
        )

    def test_health_file_created_on_success(self, urls_dir):
        mgr = self._make_manager(urls_dir)
        mgr.report_success()
        health_path = urls_dir / "audio-stream-health-test.json"
        assert health_path.exists()
        data = json.loads(health_path.read_text())
        assert data["service"] == "test"
        assert data["status"] == "connected"
        assert data["stream"] == "ground"
        assert data["quality"] == "high"
        assert "updated" in data

    def test_health_file_on_failure(self, urls_dir):
        mgr = self._make_manager(urls_dir)
        mgr.report_failure(Exception("stream died"))
        health_path = urls_dir / "audio-stream-health-test.json"
        data = json.loads(health_path.read_text())
        assert data["status"] == "reconnecting"
        assert data["last_error"] == "stream died"
        assert data["failures"] == 1

    def test_health_file_on_fallback(self, urls_dir):
        from rtsp_stream import RETRY_MAX, LOW_RES_MAX
        mgr = self._make_manager(urls_dir)
        for _ in range(RETRY_MAX + 1 + LOW_RES_MAX + 2):
            mgr.report_failure(Exception("test"))
        health_path = urls_dir / "audio-stream-health-test.json"
        data = json.loads(health_path.read_text())
        assert data["status"] == "fallback"
        assert data["stream"] == "birds"

    def test_get_health_returns_dict(self, urls_dir):
        mgr = self._make_manager(urls_dir)
        mgr.report_success()
        health = mgr.get_health()
        assert health["status"] == "connected"
        assert health["service"] == "test"


class TestRecoveryProbe:
    """Test recovery probe logic."""

    def _make_manager(self, urls_dir):
        from rtsp_stream import RTSPStreamManager
        urls_file = urls_dir / "rtsp_urls.json"
        _write_urls(urls_file, {
            "streams": {
                "ground": {"high": "rtsp://host/g-hi", "low": "rtsp://host/g-lo"},
                "birds": {"high": "rtsp://host/b-hi", "low": "rtsp://host/b-lo"},
            }
        })
        return RTSPStreamManager(
            service_name="test",
            preferred_stream="ground",
            fallback_stream="birds",
            urls_file=str(urls_file),
            sync_script="/bin/true",
            health_dir=str(urls_dir),
        )

    def test_probe_needs_two_successes(self, urls_dir):
        mgr = self._make_manager(urls_dir)
        mgr._level = 4
        mgr._current_stream = "birds"
        mgr._recovery_successes = 0
        mgr.record_probe_success()
        assert mgr._recovery_successes == 1
        assert mgr.should_switch_to_primary() is False
        mgr.record_probe_success()
        assert mgr._recovery_successes == 2
        assert mgr.should_switch_to_primary() is True

    def test_probe_failure_resets_count(self, urls_dir):
        mgr = self._make_manager(urls_dir)
        mgr._level = 4
        mgr._current_stream = "birds"
        mgr.record_probe_success()
        assert mgr._recovery_successes == 1
        mgr.record_probe_failure()
        assert mgr._recovery_successes == 0

    def test_should_probe_timing(self, urls_dir):
        import rtsp_stream
        mgr = self._make_manager(urls_dir)
        mgr._level = 4
        mgr._current_stream = "birds"
        mgr._last_probe_time = time.time() - rtsp_stream.RECOVERY_INTERVAL - 1
        assert mgr.should_probe() is True
        mgr._last_probe_time = time.time()
        assert mgr.should_probe() is False

    def test_no_probe_when_on_primary(self, urls_dir):
        mgr = self._make_manager(urls_dir)
        mgr._level = 1
        assert mgr.should_probe() is False
