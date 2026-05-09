#!/usr/bin/env python3
"""Refresh RTSP tokens from UniFi Protect CloudKey (Pi-side).

Ported from iMac on 2026-04-30 after discovering Pi tokens DO rotate
(contrary to the earlier assumption). The Pi had been running a stub
that only restarted go2rtc — when UniFi rotated tokens (somewhere
between 2026-04-24 and 2026-04-27), the Pi's go2rtc.yaml went stale
and the pipeline silently crash-looped for 4 days.

Fetches fresh RTSP stream URLs for all cameras, updates:
  - rtsp_urls.json (used by audio_analyzer, enhanced_audio_stream
    when those services are eventually ported to Pi)
  - go2rtc.yaml (used by go2rtc for browser video streaming)

Restarts go2rtc via its HTTP API (`POST /api/restart`) if tokens changed.
go2rtc runs as a systemd-user service on Pi; the API restart triggers
config reload without needing systemctl access.

Runs daily at 03:10 via systemd-user timer (refresh-rtsp.timer), or
on-demand: `/home/vives/bird-classifier/tools/refresh_rtsp.py`
"""

import json
import os
import pathlib
import ssl
import sys
import time
import urllib.parse
import urllib.request

BASE_DIR = pathlib.Path(__file__).parent.parent  # tools/ → repo root
RTSP_URLS_FILE = BASE_DIR / "rtsp_urls.json"
GO2RTC_CONFIG = BASE_DIR / "go2rtc.yaml"

PROTECT_HOST = os.environ.get("PROTECT_HOST", "192.168.4.9")
# Pi env file uses UNIFI_API_KEY (set in ~/.bird-observatory-env).
# Fall back to UNIFI_PROTECT_API_KEY for compatibility with iMac shell envs.
API_KEY = os.environ.get("UNIFI_API_KEY") or os.environ.get("UNIFI_PROTECT_API_KEY", "")

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
    """Fetch RTSP stream URLs from Protect API.

    POST with {"qualities": ["high", "low"]} body — that's how the iMac
    side talks to it; copying verbatim. Response shape:
    {"high": "rtsps://host:7441/<tok>?enableSrtp", "medium": null,
     "low": "rtsps://host:7441/<tok>?enableSrtp", "package": null}

    Returns dict {quality: rtsp_url} normalized to plain RTSP-over-TCP
    on port 7447 (drops the rtsps:// + ?enableSrtp + 7441 form).
    """
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
        if not data.get(quality):
            continue
        parsed = urllib.parse.urlparse(data[quality])
        token = parsed.path.lstrip("/")
        host = parsed.hostname or PROTECT_HOST
        result[quality] = f"rtsp://{host}:7447/{token}"
    if not result:
        raise RuntimeError(f"No streams returned for camera {camera_id}")
    return result


def write_rtsp_urls(tokens):
    """Write rtsp_urls.json for downstream consumers (audio_analyzer,
    enhanced_audio_stream when those services are eventually ported to Pi).
    Atomic write via .tmp + rename. Partial-failure safe: only writes
    cameras whose fetch succeeded."""
    data = {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "streams": {
            name: {"high": tokens[name].get("high"), "low": tokens[name].get("low")}
            for name in tokens
        },
    }
    tmp = RTSP_URLS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.rename(RTSP_URLS_FILE)
    log(f"Wrote {RTSP_URLS_FILE}")


def write_go2rtc_config(tokens):
    """Write go2rtc.yaml with fresh stream URLs. Atomic write via .tmp + rename.

    Partial-failure safe: cameras whose token fetch failed upstream are
    omitted from the config rather than KeyError'ing. api.listen +
    origin "*" must be present so the dashboard WebSocket from
    pi5.vivessato.com can talk to go2rtc.
    """
    lines = ["streams:"]
    for stream_name, (camera, quality) in GO2RTC_STREAMS.items():
        if camera not in tokens:
            continue
        url = tokens[camera].get(quality)
        if not url:
            continue
        lines.append(f"  {stream_name}:")
        lines.append(f"    - {url}#tcp")
    lines.extend([
        "",
        "api:",
        '  listen: ":1984"',
        '  origin: "*"',
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
    """Restart go2rtc by hitting its HTTP API.

    go2rtc runs as a systemd-user service on Pi (go2rtc.service). Its
    HTTP API at :1984 accepts POST /api/restart to reload the config
    file without needing systemctl. This works the same way on the iMac
    where it runs as a LaunchAgent.
    """
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:1984/api/restart",
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log(f"Restarted go2rtc via API (status {resp.status})")
    except Exception as e:
        log(f"Warning: could not restart go2rtc via API: {e}")


# Retry config for transient CloudKey unavailability (HDD swap, reboot, network blip).
# A single missed window otherwise leaves go2rtc with stale tokens for 24 h until the
# next 03:10 run. Keep retrying as long as ALL cameras are failing (i.e. CloudKey is
# fully down). Stop the moment we get even one camera back -- partial success is
# better than waiting longer.
MAX_FETCH_ATTEMPTS = 20      # 20 x 30s = 10 minutes total budget
FETCH_BACKOFF_S = 30         # constant backoff; the failure mode is "CloudKey gone"
                              # not "transient blip", so exponential doesn't help much


def fetch_all_cameras_once():
    """Fetch every camera's streams in one pass. Returns (tokens_dict, failures_list).

    Partial success is fine -- the caller decides whether to retry."""
    tokens = {}
    failures = []
    for name, cam_id in CAMERAS.items():
        try:
            tokens[name] = fetch_streams(cam_id)
            log(f"  {name}: OK")
        except Exception as e:
            log(f"  {name}: FAILED -- {e}")
            failures.append(name)
    return tokens, failures


def fetch_with_retry():
    """Run fetch_all_cameras_once with a retry loop while ALL cameras are down.

    Stop retrying as soon as any camera comes back -- write what we have and
    let the next scheduled run pick up the rest. This handles the CloudKey-
    rebooting case (all cameras down for 1-5 min) without making a network
    blip on one camera block the others."""
    for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
        if attempt > 1:
            log(f"Retry attempt {attempt}/{MAX_FETCH_ATTEMPTS} (waiting {FETCH_BACKOFF_S}s)...")
            time.sleep(FETCH_BACKOFF_S)
        log(f"Fetching RTSP tokens from {PROTECT_HOST}...")
        tokens, failures = fetch_all_cameras_once()
        if tokens:
            return tokens, failures
        log(f"All {len(CAMERAS)} cameras failed -- CloudKey may be unreachable")
    log(f"ERROR: All cameras failed after {MAX_FETCH_ATTEMPTS} attempts (~{MAX_FETCH_ATTEMPTS * FETCH_BACKOFF_S // 60} min)")
    return {}, list(CAMERAS.keys())


def main():
    if not API_KEY:
        log("ERROR: UNIFI_API_KEY (or UNIFI_PROTECT_API_KEY) not set")
        sys.exit(1)

    tokens, failures = fetch_with_retry()

    if failures:
        log(f"Token fetch failed for {len(failures)} camera(s): {failures}")
        # Continue with what we got -- better partial than zero

    if not tokens:
        log("ERROR: no tokens fetched, aborting")
        sys.exit(1)

    # Compare with previous tokens to skip restart when no rotation happened
    previous = ""
    if RTSP_URLS_FILE.exists():
        try:
            previous = RTSP_URLS_FILE.read_text()
        except Exception:
            pass

    write_rtsp_urls(tokens)

    new_contents = RTSP_URLS_FILE.read_text()
    tokens_changed = (previous != new_contents)

    write_go2rtc_config(tokens)
    log(f"Wrote {GO2RTC_CONFIG}")

    if tokens_changed:
        log("Tokens rotated -- restarting go2rtc")
        restart_go2rtc()
    else:
        log("Tokens unchanged -- no restart needed")

    log("Done.")
    sys.exit(0)


if __name__ == "__main__":
    main()
