> **HISTORICAL** — This document describes a design, plan, or system state that has been superseded. Kept for decision-trail context.

# RTSP Stream Resilience Design

## Problem

The audio analysis pipeline depends on RTSP streams from UniFi Protect cameras. These streams use rotating tokens that expire nightly. When token refresh fails — as happened March 20-22 2026, causing 2+ days of silent audio downtime — the audio analyzer enters an infinite crash loop with no self-healing capability.

### Root Causes Identified

1. **Single point of failure**: Token sync runs once at 3:10 AM. If it fails, no retry for 24 hours.
2. **SCP port flag bug**: `sync_rtsp_urls.sh` used `-p` (SSH flag) instead of `-P` (SCP flag) — silently failing since macOS SCP tightened flag parsing. (Fixed March 22.)
3. **No fallback streams**: Audio analyzer only tries one camera. If that stream is dead, it loops forever.
4. **No self-healing**: Analyzer cannot trigger a URL refresh itself.
5. **No visibility**: Dashboard shows no indication that audio is degraded or down.
6. **Missing low-res URLs**: NAS-side `refresh_unifi_streams.py` fetches both high and low quality streams from Protect API but only writes high quality to `rtsp_urls.json`.

### Failure Inventory

| # | Failure Mode | Current Behavior | Downtime Risk |
|---|-------------|-----------------|---------------|
| 1 | RTSP token expires | Relies on 3:10 AM cron | Hours to days |
| 2 | Sync script fails | Silent, no retry until next night | 24h+ |
| 3 | NAS unreachable | Sync fails silently | Cascades to #1 |
| 4 | Protect RTSP server down | Infinite reconnect on dead URL | Until Protect restarts |
| 5 | Camera offline (reboot/firmware) | Same infinite loop | Until camera back |
| 6 | Network interruption | Same infinite loop | Until restored |
| 7 | PyAV/codec failure | Catches exception, reconnects | Brief (backoff works) |
| 8 | Audio stream missing | RuntimeError, reconnect loop | Until reconfigured |
| 9 | Process crash / OOM | LaunchAgent restarts, hits stale URL | Compounds with #1 |
| 10 | Disk full / DB locked | Writes fail silently | Silent data loss |
| 11 | rtsp_urls.json corrupted | Falls back to hardcoded stale URL | Permanent |
| 12 | rtsp_urls.json valid but missing stream name | KeyError / falls to hardcoded URL | Permanent |

## Architecture

### Token Refresh Flow (existing)

```
UniFi Protect API (192.168.4.9)
        │
        ▼
NAS cron 3:05 AM ── refresh_unifi_streams.py
        │               ├── rtsp_urls.json (high + low quality) ← NEW: include low-res
        │               ├── Frigate config (high + low)
        │               ├── go2rtc config
        │               └── BirdNET config
        │
        ├── NAS pushes rtsp_urls.json → iMac via SCP
        │
        ▼
iMac LaunchAgent 3:10 AM ── sync_rtsp_urls.sh (pull, redundant backup)
        │
        ▼
rtsp_urls.json on iMac ← read by audio services
```

### New: Self-Healing Layer

```
audio_analyzer.py / enhanced_audio_stream.py
        │
        ▼
rtsp_stream.py (RTSPStreamManager)
        ├── Read rtsp_urls.json (cached, re-read on failure)
        ├── Escalation ladder (5 levels)
        ├── On-demand sync trigger (subprocess → sync_rtsp_urls.sh)
        ├── Multi-stream fallback (ground high → ground low → feeder high → feeder low)
        ├── Recovery probes (try primary every 5 min while on fallback)
        └── Health status file → /tmp/audio-stream-health-{service}.json
                                        │
                                        ▼
                                api.py /api/audio-health (merges all health files)
                                        │
                                        ▼
                                index.html warning banner
```

## Component Design

### 1. `rtsp_stream.py` — Shared RTSP Stream Manager

New shared module following the pattern of `bird_inference.py` and `solar_utils.py`.

**Class: `RTSPStreamManager`**

```python
manager = RTSPStreamManager(
    service_name="analyzer",         # used for health file: /tmp/audio-stream-health-analyzer.json
    preferred_stream="ground",       # primary camera name
    fallback_stream="birds",         # last-resort camera name
    sync_script="sync_rtsp_urls.sh", # path to URL refresh script
    urls_file="rtsp_urls.json",      # path to URL config
)

container, audio_stream = manager.connect()  # handles all escalation
manager.report_success()                     # stream is flowing
manager.report_failure(exception)            # advances escalation
manager.get_health()                         # returns health dict
```

**URL Loading:**
- Reads `rtsp_urls.json` on init and on each reconnect attempt
- Supports both formats:
  - Legacy: `{"streams": {"ground": "rtsp://..."}}`
  - New: `{"streams": {"ground": {"high": "rtsp://...", "low": "rtsp://..."}}}`
- Caches parsed URLs; re-reads file when escalation advances or after sync trigger
- If a stream name is missing from the JSON, logs a warning and skips to the next escalation level

**Escalation Ladder:**

| Level | Trigger | Action | Backoff |
|-------|---------|--------|---------|
| 1 — Retry | Failures 1-3 | Re-read URLs, try preferred stream (high) | 5s → 10s → 20s |
| 2 — Refresh | Failure 4 | Run `sync_rtsp_urls.sh`, reset backoff, try fresh URL | Reset to 5s |
| 3 — Low-res | Failures 5-6 | Try low-res stream of preferred camera | 5s → 10s |
| 4 — Fallback cam | Failure 7+ | Switch to fallback camera (high, then low) | Reset to 5s |
| 5 — Recovery probe | Every 5 min while on fallback | Open short-lived test connection to preferred cam; require 2 consecutive successes before switching back | — |
| 6 — Down | Fallback also exhausts Level 2 | Enter `"down"` status, retry full ladder from Level 1 every 5 minutes | 5 min fixed |

**Behaviors:**
- Successful connection at any level resets escalation to Level 1
- URL refresh rate-limited: at most once per 5 minutes
- Fallback camera also goes through Levels 1-2 if it fails (but never falls back to itself — avoids infinite loop)
- After fallback exhausts Level 2, status becomes `"down"`. Full ladder retried every 5 minutes until any stream connects.
- Recovery probe: opens a separate short-lived PyAV connection (connect, read one frame, close). Requires 2 consecutive successful probes (5 min apart = 10 min confirmed availability) before switching back to primary. Prevents rapid bouncing between streams.
- Logs clearly at each level: INFO for normal retries, WARNING for refresh/low-res, ERROR for fallback/down
- PyAV connection options: TCP transport, 10s timeout, auto-reconnect

**Health Status File:**

Each `RTSPStreamManager` instance writes to its own file based on `service_name`:
- `/tmp/audio-stream-health-analyzer.json` (audio_analyzer.py)
- `/tmp/audio-stream-health-enhanced.json` (enhanced_audio_stream.py)

This prevents race conditions between the two services. The API merges both files.

```json
{
  "service": "analyzer",
  "stream": "ground",
  "quality": "high",
  "status": "connected",
  "since": "2026-03-22T16:39:48",
  "updated": "2026-03-22T16:45:12",
  "failures": 0,
  "level": 1,
  "last_error": null
}
```

Possible `status` values: `"connected"`, `"reconnecting"`, `"refreshing_urls"`, `"fallback"`, `"down"`

### 2. `sync_rtsp_urls.sh` — Hardened Sync Script

Changes to existing script:

- **Retry with backoff**: 3 SCP attempts (immediate, then 5s, then 10s delay)
- **SSH cat fallback**: If all SCP attempts fail, try `ssh cat remote_file > local_file`
- **Lockfile**: Uses `/tmp/sync-rtsp-urls.lock` to prevent concurrent runs (two managers could both trigger sync)
- **Clear exit codes**: 0 = success, 1 = all methods failed
- **Timestamped logging** at each attempt
- **Remove `launchctl kickstart`**: The stream managers re-read URLs on reconnect, so force-restarting healthy services is no longer needed. Avoids unnecessary 5-10s outage on nightly sync.

No fundamental changes — same script, more resilient.

### 3. NAS `refresh_unifi_streams.py` — Include Low-Res URLs

Change `write_rtsp_urls_json()` to include both qualities:

```json
{
  "updated": "2026-03-22T03:05:02",
  "streams": {
    "birds": {"high": "rtsp://192.168.4.9:7447/token1", "low": "rtsp://192.168.4.9:7447/token2"},
    "ground": {"high": "rtsp://192.168.4.9:7447/token3", "low": "rtsp://192.168.4.9:7447/token4"},
    "newbackyard": {"high": "rtsp://...", "low": "rtsp://..."},
    "magnolia": {"high": "rtsp://...", "low": "rtsp://..."}
  }
}
```

**Note on consumers:** `rtsp_urls.json` is only read by iMac audio services (`audio_analyzer.py`, `enhanced_audio_stream.py`). The NAS-side Frigate, go2rtc, and BirdNET configs are generated separately by `refresh_unifi_streams.py` from its internal `tokens` dict — they do not read `rtsp_urls.json`. So this format change only affects the iMac side, where `rtsp_stream.py` handles both old (string) and new (dict) formats.

### 4. `audio_analyzer.py` — Use Stream Manager

Replace:
- `_get_rtsp_url()` function → removed
- `open_rtsp_audio()` function → delegates to `RTSPStreamManager.connect()`
- Reconnect loop (backoff, delay) → handled by manager
- `RECONNECT_BASE`, `RECONNECT_MAX` constants → moved to `rtsp_stream.py`
- `_RTSP_URL_FALLBACK` → removed (manager handles fallback)

The main `run()` loop simplifies to:
```python
manager = RTSPStreamManager(preferred_stream="ground", fallback_stream="birds", ...)
while not shutdown:
    try:
        container, audio_stream = manager.connect()
        manager.report_success()
        for frame in container.decode(audio_stream):
            # ... analysis logic unchanged ...
    except Exception as e:
        manager.report_failure(e)
        # manager handles backoff internally
```

### 5. `enhanced_audio_stream.py` — Use Stream Manager

Same pattern. Replace `_get_rtsp_url()` and reconnect logic with `RTSPStreamManager`. This service uses feeder cam (`"birds"`) as primary with ground as fallback (opposite of audio analyzer).

### 6. Dashboard Warning Banner

**`api.py`** — New endpoint:
```
GET /api/audio-health → merges /tmp/audio-stream-health-*.json files
```

Returns a dict with each service's health status. Returns `{"status": "unknown"}` for a service if its file doesn't exist or `updated` timestamp is stale (>5 min old).

**`index.html`** — Warning banner:
- Polls `/api/audio-health` every 60 seconds
- Shows a yellow/orange banner at the top of the dashboard when:
  - `status` is not `"connected"` — e.g. "Audio: reconnecting to ground cam..."
  - `stream` is the fallback — e.g. "Audio: using feeder cam (ground cam down)"
  - `status` is `"down"` — e.g. "Audio: stream down, all retries exhausted"
- Banner auto-dismisses when status returns to `"connected"` on primary stream
- No banner when everything is healthy

## Files Changed

| File | Location | Change |
|------|----------|--------|
| `rtsp_stream.py` | iMac (new) | Shared RTSP stream manager |
| `audio_analyzer.py` | iMac (modify) | Use RTSPStreamManager, remove RTSP/reconnect logic |
| `enhanced_audio_stream.py` | iMac (modify) | Use RTSPStreamManager, remove RTSP/reconnect logic |
| `sync_rtsp_urls.sh` | iMac (modify) | Retry + SSH fallback |
| `dashboard/api.py` | iMac (modify) | Add `/api/audio-health` endpoint |
| `dashboard/index.html` | iMac (modify) | Add warning banner + polling |
| `refresh_unifi_streams.py` | NAS (modify) | Include low-res URLs in rtsp_urls.json |
| `tests/test_rtsp_stream.py` | iMac (new) | Unit tests for escalation ladder, URL parsing, health status |

## Testing Strategy

- **Unit tests** for `rtsp_stream.py`: URL parsing (both formats, including missing stream names), escalation state machine, health file writing (per-service isolation), sync rate limiting, recovery probe logic (2-consecutive-success requirement)
- **Integration test**: Mock RTSP failure sequence, verify escalation progresses correctly through all 6 levels
- **Concurrent health files**: Verify two managers with different service names write to separate files without interference
- **Manual verification**: Restart audio services, confirm dashboard shows healthy; kill RTSP stream, confirm warning appears and self-healing kicks in

## Design Notes

**Dual-path URL sync is intentional redundancy.** The NAS pushes `rtsp_urls.json` to the iMac (SCP in `refresh_unifi_streams.sh`) and the iMac also pulls it (LaunchAgent `sync_rtsp_urls.sh`). Either path succeeding is sufficient. The analyzer's on-demand sync trigger is a third line of defense. All three paths write to the same file atomically.

## What This Does NOT Cover

- Moving the Protect API refresh to the iMac (stays on NAS — it also updates Frigate/go2rtc/BirdNET configs there)
- Changes to `capture_snapshots.py` or `live_detector.py` (they use Protect snapshot API / go2rtc, not direct RTSP)
- Phase 8 audio analysis improvements (B17/B18/B19) — separate work
