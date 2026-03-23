# RTSP Stream Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the RTSP audio pipeline self-healing with a 6-level escalation ladder, multi-stream fallback, on-demand URL refresh, and dashboard visibility.

**Architecture:** New shared `rtsp_stream.py` module provides `RTSPStreamManager` class that encapsulates all connection resilience. Both `audio_analyzer.py` and `enhanced_audio_stream.py` replace their inline RTSP/reconnect logic with this manager. Health status files enable a dashboard warning banner.

**Tech Stack:** Python 3.9, PyAV, SQLite, FastAPI, vanilla JS

**Spec:** `docs/superpowers/specs/2026-03-22-rtsp-resilience-design.md`

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `rtsp_stream.py` | Create | RTSPStreamManager: URL loading, escalation ladder, health file |
| `tests/test_rtsp_stream.py` | Create | Unit tests for URL parsing, escalation, health, sync triggering |
| `sync_rtsp_urls.sh` | Modify | Add retry, SSH fallback, lockfile, remove launchctl kickstart |
| `audio_analyzer.py` | Modify | Replace _get_rtsp_url/open_rtsp_audio/reconnect with manager |
| `enhanced_audio_stream.py` | Modify | Replace _get_rtsp_url/reconnect with manager |
| `dashboard/api.py` | Modify | Add /api/audio-health endpoint |
| `dashboard/index.html` | Modify | Add warning banner + polling |
| NAS: `refresh_unifi_streams.py` | Modify (SSH) | Include low-res URLs in rtsp_urls.json |

---

### Task 1: RTSPStreamManager — URL Loading & Parsing

**Files:**
- Create: `rtsp_stream.py`
- Create: `tests/test_rtsp_stream.py`

This task builds the URL loading layer only — no escalation yet.

- [ ] **Step 1: Write failing tests for URL parsing**

```python
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
        # Legacy URLs are treated as high quality
        assert mgr._get_url("ground", "high") == "rtsp://host/ground-token"
        # No low-res available in legacy format
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/bird-classifier && python -m pytest tests/test_rtsp_stream.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rtsp_stream'`

- [ ] **Step 3: Implement RTSPStreamManager URL loading**

```python
# rtsp_stream.py
"""rtsp_stream — Resilient RTSP stream manager with escalation ladder.

Provides RTSPStreamManager for audio services. Handles URL loading,
multi-stream fallback, on-demand URL refresh, and health reporting.

Used by:
  - audio_analyzer.py    (preferred=ground, fallback=birds)
  - enhanced_audio_stream.py (preferred=birds, fallback=ground)
"""

import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Default paths
_DEFAULT_URLS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "rtsp_urls.json"
)
_DEFAULT_SYNC_SCRIPT = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sync_rtsp_urls.sh"
)

# Escalation constants
RETRY_MAX = 3           # Level 1: retry attempts before escalating
BACKOFF_BASE = 5        # seconds
BACKOFF_MAX = 20        # max backoff within a level
REFRESH_COOLDOWN = 300  # seconds — min interval between sync triggers
LOW_RES_MAX = 2         # Level 3: low-res attempts before fallback
RECOVERY_INTERVAL = 300 # seconds between recovery probes
RECOVERY_REQUIRED = 2   # consecutive probe successes before switching back
DOWN_RETRY_INTERVAL = 300  # seconds between full-ladder retries when down

# PyAV connection options
PYAV_OPTIONS = {
    "rtsp_transport": "tcp",
    "stimeout": "10000000",     # 10s connection timeout (microseconds)
    "timeout": "10000000",      # 10s read timeout
    "reconnect": "1",
    "reconnect_streamed": "1",
    "reconnect_delay_max": "5",
}


class RTSPStreamManager:
    """Manages RTSP stream connections with escalation ladder and fallback.

    Escalation levels:
      1. Retry preferred stream (high) with backoff
      2. Trigger URL refresh via sync script
      3. Try preferred stream (low-res)
      4. Switch to fallback camera (high, then low)
      5. Recovery probes while on fallback
      6. Down — retry full ladder periodically
    """

    def __init__(self, service_name, preferred_stream, fallback_stream,
                 urls_file=_DEFAULT_URLS_FILE, sync_script=_DEFAULT_SYNC_SCRIPT,
                 health_dir="/tmp"):
        self.service_name = service_name
        self.preferred_stream = preferred_stream
        self.fallback_stream = fallback_stream
        self.urls_file = urls_file
        self.sync_script = sync_script
        self.health_file = os.path.join(
            health_dir, f"audio-stream-health-{service_name}.json"
        )

        # URL cache
        self._urls = {}  # stream_name -> {"high": url, "low": url_or_None}
        self._reload_urls()

        # Escalation state
        self._failure_count = 0
        self._level = 1
        self._current_stream = preferred_stream
        self._current_quality = "high"
        self._backoff = BACKOFF_BASE
        self._last_sync_time = 0
        self._recovery_successes = 0
        self._last_probe_time = 0
        self._connected_since = None
        self._last_error = None

    # ── URL Loading ──

    def _reload_urls(self):
        """Read rtsp_urls.json and cache parsed URLs."""
        self._urls = {}
        try:
            with open(self.urls_file) as f:
                data = json.load(f)
            streams = data.get("streams", {})
            for name, value in streams.items():
                if isinstance(value, dict):
                    # New format: {"high": "rtsp://...", "low": "rtsp://..."}
                    self._urls[name] = {
                        "high": value.get("high"),
                        "low": value.get("low"),
                    }
                elif isinstance(value, str):
                    # Legacy format: plain URL string (treated as high)
                    self._urls[name] = {"high": value, "low": None}
                else:
                    log.warning("Unexpected URL format for stream %s: %s", name, type(value))
        except FileNotFoundError:
            log.warning("RTSP URLs file not found: %s", self.urls_file)
        except json.JSONDecodeError as e:
            log.error("Corrupted RTSP URLs file %s: %s", self.urls_file, e)
        except Exception as e:
            log.error("Failed to read RTSP URLs: %s", e)

    def _get_url(self, stream_name, quality="high"):
        """Get a cached URL for a stream+quality. Returns None if not available."""
        entry = self._urls.get(stream_name)
        if entry is None:
            return None
        return entry.get(quality)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/bird-classifier && python -m pytest tests/test_rtsp_stream.py -v`
Expected: All 7 tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/bird-classifier
git add rtsp_stream.py tests/test_rtsp_stream.py
git commit -m "feat: add RTSPStreamManager URL loading with dual-format support"
```

---

### Task 2: RTSPStreamManager — Escalation State Machine

**Files:**
- Modify: `rtsp_stream.py`
- Modify: `tests/test_rtsp_stream.py`

This task adds the escalation ladder logic (state transitions on failure/success) without PyAV connections.

- [ ] **Step 1: Write failing tests for escalation**

Add to `tests/test_rtsp_stream.py`:

```python
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
            sync_script="/bin/true",  # always succeeds
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
        # Next failure should trigger refresh
        mgr.report_failure(Exception("test"))
        assert mgr._level == 2

    def test_level3_lowres(self, urls_dir):
        """After refresh fails, try low-res."""
        from rtsp_stream import RETRY_MAX
        mgr = self._make_manager(urls_dir)
        # Burn through level 1
        for _ in range(RETRY_MAX):
            mgr.report_failure(Exception("test"))
        # Level 2 — refresh
        mgr.report_failure(Exception("test"))
        # Level 3 — low-res
        mgr.report_failure(Exception("test"))
        assert mgr._level == 3
        assert mgr._current_quality == "low"

    def test_level4_fallback(self, urls_dir):
        """After low-res fails, switch to fallback camera."""
        from rtsp_stream import RETRY_MAX, LOW_RES_MAX
        mgr = self._make_manager(urls_dir)
        # Level 1: RETRY_MAX failures
        for _ in range(RETRY_MAX):
            mgr.report_failure(Exception("test"))
        # Level 2: refresh
        mgr.report_failure(Exception("test"))
        # Level 3: LOW_RES_MAX failures
        for _ in range(LOW_RES_MAX):
            mgr.report_failure(Exception("test"))
        # Should be on fallback
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
        # Escalate to fallback
        for _ in range(RETRY_MAX + 1 + LOW_RES_MAX + 1):
            mgr.report_failure(Exception("test"))
        assert mgr._current_stream == "birds"
        # Connect successfully on fallback
        mgr.report_success()
        assert mgr._level == 4  # stays on fallback level
        assert mgr._current_stream == "birds"  # NOT reset to ground
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
        # Exhaust preferred stream (level 1-4 -> switches to fallback)
        for _ in range(RETRY_MAX + 1 + LOW_RES_MAX + 1):
            mgr.report_failure(Exception("test"))
        assert mgr._current_stream == "birds"
        # Exhaust fallback stream (level 1-4 on fallback -> level 6 down)
        for _ in range(RETRY_MAX + 1 + LOW_RES_MAX + 1):
            mgr.report_failure(Exception("test"))
        assert mgr._level == 6
        # Should reset to preferred for next full-ladder retry
        assert mgr._current_stream == "ground"

    def test_get_next_url_follows_escalation(self, urls_dir):
        """get_next_url returns the right URL for current escalation state."""
        mgr = self._make_manager(urls_dir)
        assert mgr.get_next_url() == "rtsp://host/g-hi"
        # Simulate escalation to low-res
        mgr._level = 3
        mgr._current_quality = "low"
        assert mgr.get_next_url() == "rtsp://host/g-lo"
        # Simulate fallback
        mgr._level = 4
        mgr._current_stream = "birds"
        mgr._current_quality = "high"
        assert mgr.get_next_url() == "rtsp://host/b-hi"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/bird-classifier && python -m pytest tests/test_rtsp_stream.py::TestEscalation -v`
Expected: FAIL — `report_failure`, `report_success`, `get_next_url` don't exist

- [ ] **Step 3: Implement escalation state machine**

Add to `RTSPStreamManager` class in `rtsp_stream.py`:

```python
    # ── Escalation ──

    def get_next_url(self):
        """Get the RTSP URL for the current escalation state."""
        url = self._get_url(self._current_stream, self._current_quality)
        if url is None and self._current_quality == "low":
            # No low-res available, try high
            url = self._get_url(self._current_stream, "high")
        return url

    def report_success(self):
        """Stream is connected and flowing. Resets failure count but preserves
        current stream/quality — use switch_to_primary() to change back.

        This distinction is critical: when connected on the fallback stream,
        we must NOT reset to preferred, otherwise recovery probes never activate.
        """
        self._failure_count = 0
        self._backoff = BACKOFF_BASE
        if self._connected_since is None:
            self._connected_since = datetime.now().isoformat()
        self._last_error = None

        if self._current_stream == self.preferred_stream:
            # On primary — full reset
            self._level = 1
            self._recovery_successes = 0
            self._write_health("connected")
        else:
            # On fallback — stay at level 4, enable recovery probes
            self._level = 4
            self._write_health("fallback")

    def switch_to_primary(self):
        """Switch back to preferred stream after recovery probes confirm it's up."""
        self._level = 1
        self._current_stream = self.preferred_stream
        self._current_quality = "high"
        self._failure_count = 0
        self._backoff = BACKOFF_BASE
        self._recovery_successes = 0
        self._connected_since = None  # will be set on next report_success
        self._last_error = None
        self._write_health("connected")
        log.info("Switched back to primary stream '%s'", self.preferred_stream)

    def report_failure(self, error):
        """Stream failed. Advance escalation state."""
        self._failure_count += 1
        self._connected_since = None
        self._last_error = str(error)
        self._advance_escalation()

    def _advance_escalation(self):
        """Determine next escalation level based on failure count."""
        total = self._failure_count

        if total <= RETRY_MAX:
            # Level 1: retry with backoff
            self._level = 1
            self._backoff = min(BACKOFF_BASE * (2 ** (total - 1)), BACKOFF_MAX)
            log.info("Level 1: retry %d/%d, backoff %ds",
                     total, RETRY_MAX, self._backoff)
            self._write_health("reconnecting")

        elif total == RETRY_MAX + 1:
            # Level 2: trigger URL refresh
            self._level = 2
            self._trigger_sync()
            self._reload_urls()
            self._backoff = BACKOFF_BASE  # reset backoff after refresh
            log.warning("Level 2: URL refresh triggered, retrying with fresh URLs")
            self._write_health("refreshing_urls")

        elif total <= RETRY_MAX + 1 + LOW_RES_MAX:
            # Level 3: try low-res
            self._level = 3
            self._current_quality = "low"
            self._backoff = min(BACKOFF_BASE * (2 ** (total - RETRY_MAX - 2)), BACKOFF_MAX)
            log.warning("Level 3: trying low-res stream, attempt %d/%d",
                        total - RETRY_MAX - 1, LOW_RES_MAX)
            self._write_health("reconnecting")

        else:
            # Level 4: fallback camera
            if self._current_stream != self.fallback_stream:
                self._level = 4
                self._current_stream = self.fallback_stream
                self._current_quality = "high"
                self._backoff = BACKOFF_BASE
                # Reset failure count for fallback's own Level 1-2
                self._failure_count = 0
                log.error("Level 4: switching to fallback camera '%s'",
                          self.fallback_stream)
                self._write_health("fallback")
            else:
                # Fallback also exhausted — Level 6: down
                self._level = 6
                self._backoff = DOWN_RETRY_INTERVAL
                self._failure_count = 0  # reset for next full-ladder attempt
                self._current_stream = self.preferred_stream
                self._current_quality = "high"
                log.error("Level 6: all streams exhausted, entering DOWN state. "
                          "Retrying in %ds", DOWN_RETRY_INTERVAL)
                self._write_health("down")

    def get_backoff(self):
        """Get current backoff delay in seconds."""
        return self._backoff

    # ── Sync trigger ──

    def _trigger_sync(self):
        """Run sync_rtsp_urls.sh to refresh URLs. Rate-limited."""
        now = time.time()
        if now - self._last_sync_time < REFRESH_COOLDOWN:
            log.info("Sync rate-limited (last sync %.0fs ago)", now - self._last_sync_time)
            return False

        if not os.path.isfile(self.sync_script):
            log.warning("Sync script not found: %s", self.sync_script)
            return False

        self._last_sync_time = now
        try:
            result = subprocess.run(
                ["bash", self.sync_script],
                capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                log.info("Sync script succeeded: %s", result.stdout.strip())
                return True
            else:
                log.warning("Sync script failed (exit %d): %s",
                            result.returncode, result.stderr.strip())
                return False
        except subprocess.TimeoutExpired:
            log.error("Sync script timed out after 60s")
            return False
        except Exception as e:
            log.error("Failed to run sync script: %s", e)
            return False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/bird-classifier && python -m pytest tests/test_rtsp_stream.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/bird-classifier
git add rtsp_stream.py tests/test_rtsp_stream.py
git commit -m "feat: add escalation state machine to RTSPStreamManager"
```

---

### Task 3: RTSPStreamManager — Health File & Recovery Probes

**Files:**
- Modify: `rtsp_stream.py`
- Modify: `tests/test_rtsp_stream.py`

- [ ] **Step 1: Write failing tests for health file and recovery**

Add to `tests/test_rtsp_stream.py`:

```python
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
        # Escalate to fallback
        for _ in range(RETRY_MAX + 1 + LOW_RES_MAX + 1):
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
        # Simulate being on fallback
        mgr._level = 4
        mgr._current_stream = "birds"
        mgr._recovery_successes = 0
        # One probe success — not enough
        mgr.record_probe_success()
        assert mgr._recovery_successes == 1
        assert mgr.should_switch_to_primary() is False
        # Second probe success — switch back
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
        # Force last probe to be long ago
        mgr._last_probe_time = time.time() - rtsp_stream.RECOVERY_INTERVAL - 1
        assert mgr.should_probe() is True
        # Just probed
        mgr._last_probe_time = time.time()
        assert mgr.should_probe() is False

    def test_no_probe_when_on_primary(self, urls_dir):
        mgr = self._make_manager(urls_dir)
        mgr._level = 1
        assert mgr.should_probe() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/bird-classifier && python -m pytest tests/test_rtsp_stream.py::TestHealth tests/test_rtsp_stream.py::TestRecoveryProbe -v`
Expected: FAIL — `_write_health`, `get_health`, `record_probe_success`, etc. don't exist

- [ ] **Step 3: Implement health file and recovery probes**

Add to `RTSPStreamManager` class in `rtsp_stream.py`:

```python
    # ── Health status file ──

    def _write_health(self, status):
        """Write health status to per-service JSON file."""
        health = {
            "service": self.service_name,
            "stream": self._current_stream,
            "quality": self._current_quality,
            "status": status,
            "since": self._connected_since,
            "updated": datetime.now().isoformat(),
            "failures": self._failure_count,
            "level": self._level,
            "last_error": self._last_error,
        }
        try:
            tmp = self.health_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(health, f)
            os.replace(tmp, self.health_file)
        except Exception as e:
            log.warning("Failed to write health file: %s", e)

    def get_health(self):
        """Read and return health status from file."""
        try:
            with open(self.health_file) as f:
                return json.load(f)
        except Exception:
            return {"service": self.service_name, "status": "unknown"}

    # ── Recovery probes ──

    def should_probe(self):
        """Check if it's time for a recovery probe (only while on fallback)."""
        if self._current_stream == self.preferred_stream:
            return False
        if self._level not in (4, 5):
            return False
        return time.time() - self._last_probe_time >= RECOVERY_INTERVAL

    def record_probe_success(self):
        """Record a successful recovery probe."""
        self._recovery_successes += 1
        self._last_probe_time = time.time()
        log.info("Recovery probe succeeded (%d/%d)",
                 self._recovery_successes, RECOVERY_REQUIRED)

    def record_probe_failure(self):
        """Record a failed recovery probe."""
        self._recovery_successes = 0
        self._last_probe_time = time.time()
        log.info("Recovery probe failed, resetting count")

    def should_switch_to_primary(self):
        """Check if enough consecutive probes succeeded to switch back."""
        return self._recovery_successes >= RECOVERY_REQUIRED
```

Note: `_last_probe_time` and `_recovery_successes` are already initialized in `__init__` (Task 1).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/bird-classifier && python -m pytest tests/test_rtsp_stream.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
cd ~/bird-classifier
git add rtsp_stream.py tests/test_rtsp_stream.py
git commit -m "feat: add health file writing and recovery probes to RTSPStreamManager"
```

---

### Task 4: RTSPStreamManager — PyAV `connect()` Method

**Files:**
- Modify: `rtsp_stream.py`

This adds the actual PyAV connection method that ties escalation to real RTSP streams. No new tests — this calls PyAV which needs a live stream. We verify via the integration in Tasks 6-7.

- [ ] **Step 1: Add `connect()` and `probe_primary()` methods**

Add to `RTSPStreamManager` class in `rtsp_stream.py`:

```python
    # ── Connection ──

    def connect(self):
        """Open an RTSP audio stream using current escalation state.

        Returns (container, audio_stream) on success.
        Raises RuntimeError if no URL is available at current escalation level.
        """
        import av

        url = self.get_next_url()
        if url is None:
            raise RuntimeError(
                f"No RTSP URL available for {self._current_stream}/{self._current_quality}"
            )

        log.info("Connecting to %s/%s: %s",
                 self._current_stream, self._current_quality, url)

        container = av.open(url, options=PYAV_OPTIONS)

        # Find best audio stream (prefer highest sample rate)
        audio_stream = None
        for s in container.streams:
            if s.type == "audio":
                if audio_stream is None or s.rate > audio_stream.rate:
                    audio_stream = s

        if audio_stream is None:
            container.close()
            raise RuntimeError("No audio stream found in RTSP feed")

        log.info("Audio stream: %s %dHz %dch",
                 audio_stream.codec_context.name,
                 audio_stream.rate, audio_stream.channels)

        return container, audio_stream

    def probe_primary(self):
        """Test if the preferred stream is reachable. Non-destructive.

        Opens a short-lived connection, reads one frame, closes.
        Returns True if successful, False otherwise.
        """
        import av

        url = self._get_url(self.preferred_stream, "high")
        if url is None:
            self.record_probe_failure()
            return False

        try:
            container = av.open(url, options={
                "rtsp_transport": "tcp",
                "stimeout": "5000000",   # 5s timeout for probes
                "timeout": "5000000",
            })
            # Find audio stream and read one frame
            audio_stream = None
            for s in container.streams:
                if s.type == "audio":
                    audio_stream = s
                    break
            if audio_stream is None:
                container.close()
                self.record_probe_failure()
                return False

            # Read a single frame to confirm the stream is alive
            for frame in container.decode(audio_stream):
                break  # got one frame, enough
            container.close()
            self.record_probe_success()
            return True

        except Exception as e:
            log.debug("Recovery probe failed: %s", e)
            self.record_probe_failure()
            return False

    def wait_backoff(self, shutdown_event=None):
        """Wait for the current backoff duration. Interruptible via shutdown_event."""
        delay = self.get_backoff()
        log.info("Waiting %ds before next attempt...", delay)
        if shutdown_event:
            shutdown_event.wait(delay)
        else:
            time.sleep(delay)
```

- [ ] **Step 2: Run all tests**

Run: `cd ~/bird-classifier && python -m pytest tests/test_rtsp_stream.py -v`
Expected: All tests still PASS (no new tests, just new methods)

- [ ] **Step 3: Commit**

```bash
cd ~/bird-classifier
git add rtsp_stream.py
git commit -m "feat: add connect(), probe_primary(), wait_backoff() to RTSPStreamManager"
```

---

### Task 5: Harden `sync_rtsp_urls.sh`

**Files:**
- Modify: `sync_rtsp_urls.sh`

- [ ] **Step 1: Read current script**

Read: `sync_rtsp_urls.sh` — already read, contains SCP fix from earlier.

- [ ] **Step 2: Rewrite with retry, SSH fallback, lockfile, no kickstart**

```bash
#!/bin/bash
# Sync RTSP URLs from NAS after the nightly token refresh.
# Can be run by the 3:10 AM LaunchAgent or on-demand by RTSPStreamManager.
#
# Features:
#   - 3 SCP attempts with backoff (immediate, 5s, 10s)
#   - SSH cat fallback if all SCP attempts fail
#   - Lockfile prevents concurrent runs
#   - JSON validation before replacing
#
# Exit codes:
#   0 = success
#   1 = all transfer methods failed
set -eu

REMOTE_HOST="vives@192.168.5.92"
REMOTE_PORT=2000
SSH_KEY="/Users/vives/.ssh/id_ed25519"
SSH_OPTS="-i ${SSH_KEY} -o ConnectTimeout=10 -o BatchMode=yes"
REMOTE_FILE="/volume1/docker/birds-hls/rtsp_urls.json"
LOCAL_FILE="/Users/vives/bird-classifier/rtsp_urls.json"
LOCKFILE="/tmp/sync-rtsp-urls.lock"

log() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*"; }

# ── Lockfile ──
if [ -f "$LOCKFILE" ]; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null || true)
    if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
        log "Another sync is running (PID $LOCK_PID), exiting"
        exit 0
    fi
    log "Stale lockfile found, removing"
    rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# ── SCP with retry ──
SCP_DELAYS=(0 5 10)
FETCHED=false

for i in "${!SCP_DELAYS[@]}"; do
    delay=${SCP_DELAYS[$i]}
    attempt=$((i + 1))
    [ "$delay" -gt 0 ] && sleep "$delay"
    log "SCP attempt $attempt/3..."
    if scp -P ${REMOTE_PORT} ${SSH_OPTS} "${REMOTE_HOST}:${REMOTE_FILE}" "${LOCAL_FILE}.tmp" 2>/dev/null; then
        FETCHED=true
        log "SCP succeeded on attempt $attempt"
        break
    fi
    log "SCP attempt $attempt failed"
done

# ── SSH cat fallback ──
if [ "$FETCHED" = false ]; then
    log "All SCP attempts failed, trying SSH cat..."
    if ssh -p ${REMOTE_PORT} ${SSH_OPTS} "${REMOTE_HOST}" "cat ${REMOTE_FILE}" > "${LOCAL_FILE}.tmp" 2>/dev/null; then
        FETCHED=true
        log "SSH cat succeeded"
    else
        log "SSH cat also failed"
    fi
fi

if [ "$FETCHED" = false ]; then
    log "ERROR: All transfer methods failed"
    rm -f "${LOCAL_FILE}.tmp"
    exit 1
fi

# ── Validate JSON ──
if ! python3 -c "import json; json.load(open('${LOCAL_FILE}.tmp'))" 2>/dev/null; then
    log "ERROR: Invalid JSON in fetched file"
    rm -f "${LOCAL_FILE}.tmp"
    exit 1
fi

# ── Atomic replace ──
mv "${LOCAL_FILE}.tmp" "${LOCAL_FILE}"
log "Updated rtsp_urls.json"
```

- [ ] **Step 3: Verify script runs successfully**

Run: `bash ~/bird-classifier/sync_rtsp_urls.sh`
Expected: "SCP succeeded on attempt 1" + "Updated rtsp_urls.json"

- [ ] **Step 4: Commit**

```bash
cd ~/bird-classifier
git add sync_rtsp_urls.sh
git commit -m "feat: harden sync script with retry, SSH fallback, lockfile"
```

---

### Task 6: Integrate RTSPStreamManager into `audio_analyzer.py`

**Files:**
- Modify: `audio_analyzer.py:37-44` (remove RTSP URL config)
- Modify: `audio_analyzer.py:91-92` (remove RECONNECT constants)
- Modify: `audio_analyzer.py:174-185` (remove `_get_rtsp_url`)
- Modify: `audio_analyzer.py:417-452` (remove `open_rtsp_audio`)
- Modify: `audio_analyzer.py:502-517, 698-712` (replace reconnect loop)
- Modify: `audio_analyzer.py:767` (update startup log)

- [ ] **Step 1: Remove old RTSP constants and functions**

In `audio_analyzer.py`, remove:
- Lines 37-44: `_RTSP_URL_FALLBACK` and `RTSP_URLS_FILE` constants
- Lines 91-92: `RECONNECT_BASE` and `RECONNECT_MAX` constants
- Lines 174-185: `_get_rtsp_url()` function
- Lines 417-452: `open_rtsp_audio()` function

- [ ] **Step 2: Add RTSPStreamManager import and initialization**

Near the top of `audio_analyzer.py`, after the existing imports, add:

```python
from rtsp_stream import RTSPStreamManager
```

In the `run()` function, before the main loop, add:

```python
    stream_mgr = RTSPStreamManager(
        service_name="analyzer",
        preferred_stream="ground",
        fallback_stream="birds",
    )
```

- [ ] **Step 3: Replace the reconnect loop in `run()`**

Replace the main while loop structure (lines ~502-712) to use the manager:

```python
    while not _shutdown.is_set():
        # Sleep during nighttime
        if is_nighttime():
            log.info("Nighttime — pausing analysis until sunrise")
            while is_nighttime() and not _shutdown.is_set():
                _shutdown.wait(60)
            if _shutdown.is_set():
                break
            log.info("Sunrise — resuming analysis")

        container = None
        try:
            container, audio_stream = stream_mgr.connect()
            stream_mgr.report_success()

            pcm_buf = bytearray()

            for frame in container.decode(audio_stream):
                if _shutdown.is_set():
                    break
                if is_nighttime():
                    break

                # ... (all existing PCM decode + BirdNET analysis code stays unchanged) ...

                # Recovery probes while on fallback
                if stream_mgr.should_probe():
                    if stream_mgr.probe_primary():
                        if stream_mgr.should_switch_to_primary():
                            log.info("Primary stream recovered, switching back")
                            stream_mgr.switch_to_primary()
                            break  # exit decode loop to reconnect on primary

            log.warning("RTSP stream ended")

        except Exception as e:
            log.error("Stream error: %s", e)
            stream_mgr.report_failure(e)
        finally:
            if container:
                try:
                    container.close()
                except Exception:
                    pass

        if not _shutdown.is_set():
            stream_mgr.wait_backoff(_shutdown)
```

- [ ] **Step 4: Update startup log line**

Replace line 767:
```python
    log.info("  RTSP: %s (dynamic, fallback: %s)", _get_rtsp_url("ground"), _RTSP_URL_FALLBACK)
```
with:
```python
    log.info("  RTSP: managed (preferred=ground, fallback=birds)")
```

- [ ] **Step 5: Verify audio analyzer starts**

Run: `cd ~/bird-classifier && python audio_analyzer.py --test 2>&1 | head -20`
Expected: Should start, load model, connect to RTSP stream (or fail gracefully with escalation logs)

- [ ] **Step 6: Commit**

```bash
cd ~/bird-classifier
git add audio_analyzer.py
git commit -m "refactor: replace inline RTSP logic with RTSPStreamManager in audio_analyzer"
```

---

### Task 7: Integrate RTSPStreamManager into `enhanced_audio_stream.py`

**Files:**
- Modify: `enhanced_audio_stream.py:31-37` (remove RTSP URL config)
- Modify: `enhanced_audio_stream.py:51-52` (remove RECONNECT constants)
- Modify: `enhanced_audio_stream.py:72-84` (remove `_get_rtsp_url`)
- Modify: `enhanced_audio_stream.py:98-203` (replace `_rtsp_reader` reconnect logic)
- Modify: `enhanced_audio_stream.py:357-359` (update startup log)

- [ ] **Step 1: Remove old RTSP constants and functions**

In `enhanced_audio_stream.py`, remove:
- Lines 31-37: `_RTSP_URL_FALLBACK` and `RTSP_URLS_FILE`
- Lines 51-52: `RECONNECT_BASE` and `RECONNECT_MAX`
- Lines 72-84: `_get_rtsp_url()` function

- [ ] **Step 2: Add RTSPStreamManager import**

```python
from rtsp_stream import RTSPStreamManager
```

- [ ] **Step 3: Replace `_rtsp_reader()` reconnect logic**

The `_rtsp_reader()` function currently handles its own reconnection. Replace with manager:

```python
def _rtsp_reader():
    """Background thread: decode RTSP audio, apply bandpass, push to ring buffer."""
    global _ring_seq

    stream_mgr = RTSPStreamManager(
        service_name="enhanced",
        preferred_stream="birds",
        fallback_stream="ground",
    )

    while not _shutdown.is_set():
        container = None
        try:
            container, audio_stream = stream_mgr.connect()
            stream_mgr.report_success()

            # ... (existing resampler setup + frame decode loop unchanged) ...

            # Inside the frame decode loop, after chunk processing, add probe check:
            # (same pattern as audio_analyzer.py)

        except av.error.ExitError:
            log.warning("RTSP stream ended")
            stream_mgr.report_failure(Exception("Stream ended"))
        except Exception as e:
            log.error("RTSP error: %s", e)
            stream_mgr.report_failure(e)
        finally:
            if container:
                try:
                    container.close()
                except Exception:
                    pass

        if not _shutdown.is_set():
            stream_mgr.wait_backoff(_shutdown)
```

- [ ] **Step 4: Update startup log**

Replace line 359:
```python
    log.info("  RTSP: %s (dynamic, fallback: %s)", _get_rtsp_url("birds"), _RTSP_URL_FALLBACK)
```
with:
```python
    log.info("  RTSP: managed (preferred=birds, fallback=ground)")
```

- [ ] **Step 5: Verify enhanced audio stream starts**

Run: `cd ~/bird-classifier && timeout 10 python enhanced_audio_stream.py 2>&1 | head -20`
Expected: Should start, open RTSP stream (or escalate gracefully)

- [ ] **Step 6: Commit**

```bash
cd ~/bird-classifier
git add enhanced_audio_stream.py
git commit -m "refactor: replace inline RTSP logic with RTSPStreamManager in enhanced_audio_stream"
```

---

### Task 8: NAS — Include Low-Res URLs in `rtsp_urls.json`

**Files:**
- Modify (via SSH): NAS `/volume1/docker/scripts/refresh_unifi_streams.py`

- [ ] **Step 1: Read current `write_rtsp_urls_json` function on NAS**

```bash
ssh -p 2000 -i ~/.ssh/id_ed25519 vives@192.168.5.92 \
  "grep -n 'def write_rtsp_urls_json' -A 10 /volume1/docker/scripts/refresh_unifi_streams.py"
```

- [ ] **Step 2: Update function to include both high and low quality**

The current function:
```python
def write_rtsp_urls_json(tokens: dict) -> None:
    data = {
        'updated': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'streams': {name: tokens[name]['high']['url'] for name in CAMERAS},
    }
```

Change to:
```python
def write_rtsp_urls_json(tokens: dict) -> None:
    """Write RTSP URLs for all cameras (high + low quality) to JSON file."""
    data = {
        'updated': datetime.datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),
        'streams': {
            name: {
                'high': tokens[name]['high']['url'],
                'low': tokens[name]['low']['url'],
            }
            for name in CAMERAS
        },
    }
    tmp_path = RTSP_URLS_JSON.with_suffix('.tmp')
    tmp_path.write_text(json.dumps(data, indent=2) + '\n')
    tmp_path.rename(RTSP_URLS_JSON)
```

Apply via SSH:
```bash
ssh -p 2000 -i ~/.ssh/id_ed25519 vives@192.168.5.92 "
  cd /volume1/docker/scripts
  cp refresh_unifi_streams.py refresh_unifi_streams.py.bak
  sed -i \"s/'streams': {name: tokens\[name\]\['high'\]\['url'\] for name in CAMERAS},/'streams': {name: {'high': tokens[name]['high']['url'], 'low': tokens[name]['low']['url']} for name in CAMERAS},/\" refresh_unifi_streams.py
"
```

If sed is tricky, use a Python one-liner or create a small patch.

- [ ] **Step 3: Verify the change by running refresh manually**

```bash
ssh -p 2000 -i ~/.ssh/id_ed25519 vives@192.168.5.92 \
  "cd /volume1/docker && bash scripts/refresh_unifi_streams.sh"
```

Then check the output:
```bash
ssh -p 2000 -i ~/.ssh/id_ed25519 vives@192.168.5.92 \
  "cat /volume1/docker/birds-hls/rtsp_urls.json"
```

Expected: JSON with `{"high": "...", "low": "..."}` per stream.

- [ ] **Step 4: Sync fresh URLs to iMac**

```bash
bash ~/bird-classifier/sync_rtsp_urls.sh
cat ~/bird-classifier/rtsp_urls.json
```

Expected: New format with high+low URLs.

- [ ] **Step 5: Run unit tests to verify URL parsing handles new format**

```bash
cd ~/bird-classifier && python -m pytest tests/test_rtsp_stream.py -v
```

Expected: All tests PASS (TestURLLoading.test_load_new_format_high already covers this)

- [ ] **Step 6: Commit (local — NAS change is not in git)**

```bash
cd ~/bird-classifier
git add rtsp_urls.json
git commit -m "chore: update rtsp_urls.json to new high/low format"
```

---

### Task 9: Dashboard — Audio Health API Endpoint

**Files:**
- Modify: `dashboard/api.py`

- [ ] **Step 1: Add `/api/audio-health` endpoint**

In `dashboard/api.py`, add after the existing health check functions (around line 240):

```python
import glob as _glob

@app.get("/api/audio-health")
def get_audio_health():
    """Return health status of audio stream services.

    Reads per-service health files written by RTSPStreamManager.
    Returns status for each audio service (analyzer, enhanced).
    """
    services = {}
    health_files = _glob.glob("/tmp/audio-stream-health-*.json")
    for path in health_files:
        try:
            with open(path) as f:
                data = json.load(f)
            # Check staleness — if updated > 5 min ago, mark unknown
            updated = data.get("updated", "")
            if updated:
                try:
                    updated_dt = datetime.fromisoformat(updated)
                    age = (datetime.now() - updated_dt).total_seconds()
                    if age > 300:
                        data["status"] = "unknown"
                        data["stale"] = True
                except (ValueError, TypeError):
                    pass
            service_name = data.get("service", "unknown")
            services[service_name] = data
        except Exception:
            continue

    # If no health files exist, return unknown
    if not services:
        return {"analyzer": {"status": "unknown"}, "enhanced": {"status": "unknown"}}

    return services
```

- [ ] **Step 2: Verify endpoint works**

```bash
curl -s http://localhost:8099/api/audio-health | python3 -m json.tool
```

Expected: JSON with service health status (may show "unknown" if services haven't written health files yet)

- [ ] **Step 3: Commit**

```bash
cd ~/bird-classifier
git add dashboard/api.py
git commit -m "feat: add /api/audio-health endpoint for stream status"
```

---

### Task 10: Dashboard — Warning Banner

**Files:**
- Modify: `dashboard/index.html`

- [ ] **Step 1: Add warning banner CSS**

In the `<style>` section of `index.html`, add near the toast styles (~line 1215):

```css
  /* Audio health warning banner */
  .audio-warning {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    z-index: 1000;
    padding: 8px 16px;
    text-align: center;
    font-size: 14px;
    font-weight: 500;
    display: none;
    transition: opacity 0.3s;
  }
  .audio-warning.visible { display: block; }
  .audio-warning.warning { background: #92400e; color: #fef3c7; }
  .audio-warning.error { background: #991b1b; color: #fef2f2; }
  .audio-warning .dismiss {
    cursor: pointer;
    margin-left: 12px;
    opacity: 0.7;
  }
  .audio-warning .dismiss:hover { opacity: 1; }
```

- [ ] **Step 2: Add warning banner HTML**

Add after the toast div (~line 2150):

```html
<!-- Audio Health Warning -->
<div class="audio-warning" id="audioWarning">
  <span id="audioWarningText"></span>
  <span class="dismiss" onclick="dismissAudioWarning()">&times;</span>
</div>
```

- [ ] **Step 3: Add audio health polling JavaScript**

Add in the `<script>` section, near the other polling/update functions:

```javascript
  // ── Audio Health Warning ──
  let _audioWarningDismissed = null; // track dismissed message to avoid re-showing same

  function pollAudioHealth() {
    fetch('/api/audio-health')
      .then(r => r.json())
      .then(data => {
        // Check analyzer service (the critical one)
        const analyzer = data.analyzer || data.enhanced || {};
        const status = analyzer.status || 'unknown';
        const stream = analyzer.stream || '';

        const el = document.getElementById('audioWarning');
        const textEl = document.getElementById('audioWarningText');

        if (status === 'connected') {
          // All good — hide banner
          el.classList.remove('visible', 'warning', 'error');
          _audioWarningDismissed = null;
          return;
        }

        let msg = '';
        let severity = 'warning';
        if (status === 'fallback') {
          msg = 'Audio: using backup cam (primary cam down)';
        } else if (status === 'down') {
          msg = 'Audio: stream down, all retries exhausted';
          severity = 'error';
        } else if (status === 'reconnecting' || status === 'refreshing_urls') {
          msg = 'Audio: reconnecting to ' + stream + ' cam...';
        } else if (status === 'unknown') {
          msg = 'Audio: status unknown (service may be down)';
          severity = 'error';
        } else {
          return; // no warning needed
        }

        // Don't re-show if user dismissed this exact message
        if (msg === _audioWarningDismissed) return;

        textEl.textContent = msg;
        el.className = 'audio-warning visible ' + severity;
      })
      .catch(() => {}); // silent fail — don't spam console
  }

  function dismissAudioWarning() {
    const el = document.getElementById('audioWarning');
    const textEl = document.getElementById('audioWarningText');
    _audioWarningDismissed = textEl.textContent;
    el.classList.remove('visible');
  }

  // Poll every 60 seconds
  setInterval(pollAudioHealth, 60000);
  pollAudioHealth(); // initial check
```

- [ ] **Step 4: Verify banner appears**

Open the dashboard in a browser. If audio services are running with the manager, the banner should be hidden (connected state). To test the banner:
```bash
# Write a fake health file to simulate fallback
echo '{"service":"analyzer","stream":"birds","quality":"high","status":"fallback","since":"2026-03-22T16:00:00","updated":"'$(date -u +%Y-%m-%dT%H:%M:%S)'","failures":7,"level":4,"last_error":"token expired"}' > /tmp/audio-stream-health-analyzer.json
```
Refresh dashboard — should see yellow "Audio: using feeder cam (ground cam down)" banner.

Clean up: `rm /tmp/audio-stream-health-analyzer.json`

- [ ] **Step 5: Commit**

```bash
cd ~/bird-classifier
git add dashboard/index.html
git commit -m "feat: add audio health warning banner to dashboard"
```

---

### Task 11: Restart Services & Verify End-to-End

**Files:** None (operational verification)

- [ ] **Step 1: Run all tests**

```bash
cd ~/bird-classifier && python -m pytest tests/ -v
```

Expected: All tests pass (100 existing + new rtsp_stream tests)

- [ ] **Step 2: Restart audio analyzer**

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-audio"
sleep 5
tail -20 ~/bird-snapshots/logs/audio-analyzer-stderr.log
```

Expected: Logs show "RTSP: managed (preferred=ground, fallback=birds)" and "Audio stream: opus 48000Hz 2ch"

- [ ] **Step 3: Restart enhanced audio stream**

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-enhanced-audio"
sleep 5
tail -20 ~/bird-snapshots/logs/enhanced-audio-stderr.log 2>/dev/null || tail -20 /var/log/bird-enhanced-audio.log 2>/dev/null
```

Expected: Similar healthy startup

- [ ] **Step 4: Verify health files exist**

```bash
cat /tmp/audio-stream-health-analyzer.json
cat /tmp/audio-stream-health-enhanced.json
```

Expected: Both show `"status": "connected"`, `"stream"` matches preferred

- [ ] **Step 5: Verify dashboard endpoint**

```bash
curl -s http://localhost:8099/api/audio-health | python3 -m json.tool
```

Expected: Both services show connected

- [ ] **Step 6: Verify audio detections are flowing**

```bash
sqlite3 ~/bird-snapshots/birdnet-audio/birdnet_local.db \
  "SELECT COUNT(*), MAX(time) FROM notes WHERE date = '$(date +%Y-%m-%d)'"
```

Expected: Non-zero count, recent timestamp

- [ ] **Step 7: Final commit with tag**

```bash
cd ~/bird-classifier
git tag v0.9-rtsp-resilience
```
