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
