"""rtsp_stream — Resilient RTSP stream manager with escalation ladder.

Provides RTSPStreamManager for audio services. Handles URL loading,
multi-stream fallback, on-demand URL refresh, and health reporting.
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
        self._last_heartbeat = 0
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

    # ── Escalation ──

    def get_next_url(self):
        """Get the RTSP URL for the current escalation state."""
        url = self._get_url(self._current_stream, self._current_quality)
        if url is None and self._current_quality == "low":
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
            self._level = 1
            self._current_quality = "high"  # reset from low-res if that's how we connected
            self._recovery_successes = 0
            self._write_health("connected")
        else:
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
        self._connected_since = None
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

        # Stay in down state until a successful connection
        if self._level == 6:
            self._backoff = DOWN_RETRY_INTERVAL
            log.error("Level 6: still down, retrying in %ds", DOWN_RETRY_INTERVAL)
            self._write_health("down")
            return

        # Stay in fallback state — re-run fallback escalation independently
        if self._current_stream == self.fallback_stream:
            if total <= RETRY_MAX:
                self._level = 4
                self._backoff = min(BACKOFF_BASE * (2 ** (total - 1)), BACKOFF_MAX)
                log.warning("Level 4 retry %d/%d on fallback '%s', backoff %ds",
                            total, RETRY_MAX, self._current_stream, self._backoff)
                self._write_health("fallback")
            else:
                self._level = 6
                self._backoff = DOWN_RETRY_INTERVAL
                self._failure_count = 0
                self._current_stream = self.preferred_stream
                self._current_quality = "high"
                log.error("Level 6: all streams exhausted, entering DOWN state. Retrying in %ds",
                          DOWN_RETRY_INTERVAL)
                self._write_health("down")
            return

        if total <= RETRY_MAX:
            self._level = 1
            self._backoff = min(BACKOFF_BASE * (2 ** (total - 1)), BACKOFF_MAX)
            log.info("Level 1: retry %d/%d, backoff %ds", total, RETRY_MAX, self._backoff)
            self._write_health("reconnecting")

        elif total == RETRY_MAX + 1:
            self._level = 2
            self._trigger_sync()
            self._reload_urls()
            self._backoff = BACKOFF_BASE
            log.warning("Level 2: URL refresh triggered, retrying with fresh URLs")
            self._write_health("refreshing_urls")

        elif total <= RETRY_MAX + 1 + LOW_RES_MAX:
            self._level = 3
            self._current_quality = "low"
            self._backoff = min(BACKOFF_BASE * (2 ** (total - RETRY_MAX - 2)), BACKOFF_MAX)
            log.warning("Level 3: trying low-res stream, attempt %d/%d",
                        total - RETRY_MAX - 1, LOW_RES_MAX)
            self._write_health("reconnecting")

        else:
            if self._current_stream != self.fallback_stream:
                self._level = 4
                self._current_stream = self.fallback_stream
                self._current_quality = "high"
                self._backoff = BACKOFF_BASE
                self._failure_count = 0
                log.error("Level 4: switching to fallback camera '%s'", self.fallback_stream)
                self._write_health("fallback")
            else:
                self._level = 6
                self._backoff = DOWN_RETRY_INTERVAL
                self._failure_count = 0
                self._current_stream = self.preferred_stream
                self._current_quality = "high"
                log.error("Level 6: all streams exhausted, entering DOWN state. Retrying in %ds", DOWN_RETRY_INTERVAL)
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
                log.warning("Sync script failed (exit %d): %s", result.returncode, result.stderr.strip())
                return False
        except subprocess.TimeoutExpired:
            log.error("Sync script timed out after 60s")
            return False
        except Exception as e:
            log.error("Failed to run sync script: %s", e)
            return False

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

    def heartbeat(self):
        """Periodically update the health file timestamp. Call from analysis loop.
        Rate-limited to once per 60 seconds to avoid disk thrash."""
        now = time.time()
        if now - self._last_heartbeat < 60:
            return
        self._last_heartbeat = now
        status = "connected" if self._current_stream == self.preferred_stream else "fallback"
        if self._level == 6:
            status = "down"
        self._write_health(status)

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
        log.info("Recovery probe succeeded (%d/%d)", self._recovery_successes, RECOVERY_REQUIRED)

    def record_probe_failure(self):
        """Record a failed recovery probe."""
        self._recovery_successes = 0
        self._last_probe_time = time.time()
        log.info("Recovery probe failed, resetting count")

    def should_switch_to_primary(self):
        """Check if enough consecutive probes succeeded to switch back."""
        return self._recovery_successes >= RECOVERY_REQUIRED

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

        try:
            # Find best audio stream (prefer highest sample rate)
            audio_stream = None
            for s in container.streams:
                if s.type == "audio":
                    if audio_stream is None or s.rate > audio_stream.rate:
                        audio_stream = s

            if audio_stream is None:
                raise RuntimeError("No audio stream found in RTSP feed")

            log.info("Audio stream: %s %dHz %dch",
                     audio_stream.codec_context.name,
                     audio_stream.rate, audio_stream.channels)
        except Exception:
            container.close()
            raise

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

        container = None
        try:
            container = av.open(url, options={
                "rtsp_transport": "tcp",
                "stimeout": "5000000",
                "timeout": "5000000",
            })
            audio_stream = None
            for s in container.streams:
                if s.type == "audio":
                    audio_stream = s
                    break
            if audio_stream is None:
                self.record_probe_failure()
                return False

            for frame in container.decode(audio_stream):
                break  # got one frame, enough
            self.record_probe_success()
            return True

        except Exception as e:
            log.debug("Recovery probe failed: %s", e)
            self.record_probe_failure()
            return False
        finally:
            if container:
                try:
                    container.close()
                except Exception:
                    pass

    def wait_backoff(self, shutdown_event=None):
        """Wait for the current backoff duration. Interruptible via shutdown_event."""
        delay = self.get_backoff()
        log.info("Waiting %ds before next attempt...", delay)
        if shutdown_event:
            shutdown_event.wait(delay)
        else:
            time.sleep(delay)
