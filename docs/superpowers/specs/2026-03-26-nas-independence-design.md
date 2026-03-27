# NAS Independence — Design Spec

## Problem

The bird observatory depends on the NAS (VivesSyn) for video streaming (go2rtc), RTSP token refresh, and dashboard hosting. With the Cloudflare tunnel now serving the dashboard directly from the iMac, the NAS is an unnecessary middleman that adds latency, complexity, and a point of failure.

## Goal

Remove all NAS dependencies from the bird system. The iMac becomes fully self-contained. The only external dependency is the CloudKey Gen 2+ (192.168.4.9) which runs UniFi Protect and manages the cameras.

## Architecture After

```
CloudKey (192.168.4.9)
  └── UniFi Protect + cameras
        │
        ├── RTSP streams ──► iMac: go2rtc (port 1984) ──► dashboard video
        ├── RTSP streams ──► iMac: audio_analyzer.py ──► BirdNET detection
        └── Snapshot API ──► iMac: capture_snapshots.py ──► classification
                                    │
                              iMac: FastAPI (port 8099)
                                    │
                              Cloudflare tunnel
                                    │
                              birds.vivessato.com
```

## Changes

### 1. go2rtc on iMac

Install go2rtc binary (already done: `/usr/local/bin/go2rtc`).

Config at `~/bird-classifier/go2rtc.yaml`:
```yaml
streams:
  feeder-main:
    - rtsp://192.168.4.9:7447/{feeder_token}#tcp
  ground-main:
    - rtsp://192.168.4.9:7447/{ground_token}#tcp

api:
  listen: ":1984"

log:
  level: info
```

LaunchAgent: `com.vives.bird-go2rtc.plist` with KeepAlive.

### 2. refresh_rtsp.py on iMac

Simplified version of the NAS script. Only does:
- Fetch RTSP tokens from CloudKey Protect API
- Write `rtsp_urls.json` (for audio_analyzer, enhanced_audio)
- Update `go2rtc.yaml` with fresh tokens
- Restart go2rtc if tokens changed

No Frigate, BirdNET-Go, or NAS config updates.

Runs via LaunchAgent at 3:10 AM (replacing `sync_rtsp_urls.sh`).

### 3. WebSocket proxy → localhost

Change `GO2RTC_HOST` in api.py from Tailscale IP to `127.0.0.1`. No network hop for camera feeds.

### 4. Remove NAS sync

- Delete `sync_rtsp_urls.sh` (was syncing rtsp_urls.json FROM NAS)
- Update `bird-rtsp-sync` LaunchAgent to run `refresh_rtsp.py` instead
- Remove NAS SCP from `deploy.sh` (dashboard HTML no longer needs syncing to NAS)

### 5. NAS Docker status (for reference, no action)

- **go2rtc**: being moved to iMac, can be stopped on NAS after migration
- **Frigate**: dormant, leave as-is
- **BirdNET-Go**: dormant, leave as-is
- **UniFi containers**: dead, can be deleted (separate task)
- **nginx/birds-share**: replaced by Cloudflare tunnel
- **birdnet_sse.py**: replaced by iMac's built-in SSE endpoint

## Files Changed

| File | Action |
|------|--------|
| `go2rtc.yaml` | Create — go2rtc config for iMac |
| `refresh_rtsp.py` | Create — simplified token refresh from CloudKey |
| `dashboard/api.py` | Modify — GO2RTC_HOST → 127.0.0.1 |
| `sync_rtsp_urls.sh` | Delete — no longer needed |
| `scripts/deploy.sh` | Modify — remove NAS SCP |
| LaunchAgent `bird-go2rtc` | Create — go2rtc service |
| LaunchAgent `bird-rtsp-sync` | Modify — run refresh_rtsp.py instead of sync |

## Success Criteria

1. Camera feeds work at birds.vivessato.com with go2rtc on iMac
2. RTSP tokens refresh at 3:10 AM without NAS involvement
3. Audio analyzer still connects to camera streams
4. No SSH/SCP to NAS in any bird system process
5. NAS can be powered off without affecting bird system
