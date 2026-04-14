#!/usr/bin/env python3
"""Refresh RTSP tokens from UniFi Protect CloudKey.

Fetches fresh RTSP stream URLs for all cameras, updates:
  - rtsp_urls.json (used by audio_analyzer, enhanced_audio_stream)
  - go2rtc.yaml (used by go2rtc for browser video streaming)

Restarts go2rtc if tokens changed.

Run daily at 3:10 AM via LaunchAgent, or on-demand.
"""

import json
import os
import pathlib
import ssl
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = pathlib.Path(__file__).parent
RTSP_URLS_FILE = BASE_DIR / "rtsp_urls.json"
GO2RTC_CONFIG = BASE_DIR / "go2rtc.yaml"

PROTECT_HOST = os.environ.get("PROTECT_HOST", "192.168.4.9")
API_KEY = os.environ.get("UNIFI_PROTECT_API_KEY", "")

CAMERAS = {
    "birds": "690e999401027503e400043b",
    "ground": "690e999400887503e4000439",
    "magnolia": "690e999400cd7503e400043a",
    "newbackyard": "690e99d000532203e4000433",
}

GO2RTC_STREAMS = {
    "feeder-main": ("birds", "high"),
    "feeder-sub": ("birds", "low"),
    "ground-main": ("ground", "high"),
    "ground-sub": ("ground", "low"),
}

ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}")


def fetch_streams(camera_id):
    """Fetch RTSP stream URLs from Protect API."""
    url = f"https://{PROTECT_HOST}/proxy/protect/integration/v1/cameras/{camera_id}/rtsps-stream"
    payload = json.dumps({"qualities": ["high", "low"]}).encode()
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"X-API-KEY": API_KEY, "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, context=ssl_ctx, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    result = {}
    for quality in ("high", "low"):
        parsed = urllib.parse.urlparse(data[quality])
        token = parsed.path.lstrip("/")
        host = parsed.hostname or PROTECT_HOST
        result[quality] = f"rtsp://{host}:7447/{token}"
    return result


def write_rtsp_urls(tokens):
    """Write rtsp_urls.json."""
    data = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "streams": {
            name: {"high": tokens[name]["high"], "low": tokens[name]["low"]}
            for name in CAMERAS
        },
    }
    tmp = RTSP_URLS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(RTSP_URLS_FILE)


def write_go2rtc_config(tokens):
    """Write go2rtc.yaml with fresh stream URLs."""
    lines = ["streams:"]
    for stream_name, (camera, quality) in GO2RTC_STREAMS.items():
        url = tokens[camera][quality]
        lines.append(f"  {stream_name}:")
        lines.append(f"    - {url}#tcp")
    lines.extend([
        "",
        "api:",
        '  listen: ":1984"',
        "",
        "log:",
        "  level: info",
        "",
    ])
    config = "\n".join(lines)
    tmp = GO2RTC_CONFIG.with_suffix(".tmp")
    tmp.write_text(config)
    tmp.rename(GO2RTC_CONFIG)
    return config


def restart_go2rtc():
    """Restart go2rtc by sending API restart command.

    go2rtc runs as a native binary (LaunchAgent com.vives.go2rtc),
    not in Docker. Its HTTP API at :1984 accepts POST /api/restart
    to reload the config file.
    """
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:1984/api/restart",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Restarted go2rtc via API (status {resp.status})")
    except Exception as e:
        log(f"Warning: could not restart go2rtc: {e}")


def main():
    if not API_KEY:
        log("ERROR: UNIFI_PROTECT_API_KEY not set")
        sys.exit(1)

    log(f"Fetching RTSP tokens from {PROTECT_HOST}...")

    tokens = {}
    failures = []
    for name, cam_id in CAMERAS.items():
        try:
            tokens[name] = fetch_streams(cam_id)
            log(f"  {name}: OK")
        except Exception as e:
            log(f"  {name}: FAILED — {e}")
            failures.append(name)

    if len(failures) == len(CAMERAS):
        log("ERROR: All cameras failed")
        sys.exit(1)
    if failures:
        log(f"Warning: {len(failures)} camera(s) failed, continuing with {len(tokens)}")

    # Compare streams only (not the timestamp) to detect real changes
    old_streams = {}
    if RTSP_URLS_FILE.exists():
        try:
            old_streams = json.loads(RTSP_URLS_FILE.read_text()).get("streams", {})
        except (json.JSONDecodeError, KeyError):
            pass

    new_streams = {
        name: {"high": tokens[name]["high"], "low": tokens[name]["low"]}
        for name in tokens
    }
    changed = old_streams != new_streams

    write_rtsp_urls(tokens)
    if changed:
        log("Tokens changed — updating go2rtc config")
        write_go2rtc_config(tokens)
        restart_go2rtc()
    else:
        log("Tokens unchanged")

    log("Done")


if __name__ == "__main__":
    main()
