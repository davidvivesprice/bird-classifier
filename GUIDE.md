# Bird Observatory — Reference Guide

A real-time bird identification system running on a single iMac. Two cameras watch the yard, detecting and classifying birds by sight and sound, with results served on a live dashboard.

**Dashboard**: https://birds.vivessato.com/

---

## Architecture

```
              Feeder Cam        Ground Cam
                  │                  │
          CloudKey Gen 2+ (192.168.4.9)
          UniFi Protect manages cameras only
                  │
    ┌─────────────┼──────────────────┐
    │             │                  │
    ▼             ▼                  ▼
 capture      go2rtc            audio_analyzer
 snapshots    (Docker)          (BirdNET V2.4)
    │          :1984                 │
    ▼             │                  ▼
 classify.py  live_detector     birdnet_local.db
 (YOLO+AIY)   :8097 (SSE)      + audio clips
    │             │                  │
    ▼             ▼                  ▼
    └─────────── api.py ◄────────────┘
                 :8099
                   │
            Cloudflare tunnel
                   │
          birds.vivessato.com
```

Everything runs on the iMac (192.168.4.200). The CloudKey provides camera feeds only.

---

## Data Pipelines

**Batch visual** (~10s cycle): `capture_snapshots.py` polls CloudKey snapshot API with motion detection, saves to `incoming/`. `classify.py --watch` picks up new images, runs YOLO bird detection then AIY Birds V1 species classification, writes to `classified/{species}/` + JSONL log. Dashboard API reads the JSONL.

**Live visual** (~300ms): `live_detector.py` pulls frames from go2rtc at 3fps, runs YOLO + AIY with temporal voting (SpeciesVoter), pushes detections via SSE. Dashboard draws bounding box overlays on the live video feed.

**Audio** (~3s): `audio_analyzer.py` decodes RTSP audio via PyAV, applies bandpass + noisereduce, runs BirdNET V2.4 on 6-second overlapping windows, writes to SQLite (`birdnet_local.db`) + saves WAV clips. Dashboard receives audio detections via SSE.

**Enhanced audio** (live): `enhanced_audio_stream.py` applies bandpass filter (300Hz-15kHz) to RTSP audio and serves as MP3 stream for in-browser listening.

---

## Services

Nine macOS LaunchAgents + one Docker container:

| # | Label | Script | Details |
|---|-------|--------|---------|
| 1 | `com.vives.bird-audio` | `audio_analyzer.py` | BirdNET audio analysis, port 8098 |
| 2 | `com.vives.bird-capture` | `capture_snapshots.py` | Motion-triggered snapshots from CloudKey |
| 3 | `com.vives.bird-classifier` | `classify.py --watch` | Batch YOLO + AIY classification (venv-coral) |
| 4 | `com.vives.bird-dashboard` | `uvicorn` (api.py) | FastAPI dashboard backend, port 8099 |
| 5 | `com.vives.bird-enhanced-audio` | `enhanced_audio_stream.py` | Bandpass MP3 audio stream, port 8096 |
| 6 | `com.vives.bird-health-monitor` | `health_monitor.py` | Runs every 5 min, checks + restarts services |
| 7 | `com.vives.bird-livedetect` | `live_detector.py` | Real-time detection SSE, port 8097 |
| 8 | `com.vives.bird-rtsp-sync` | `refresh_rtsp.py` | RTSP token refresh, daily at 3:10 AM |
| 9 | `com.vives.bird-tunnel` | `cloudflared tunnel run` | Cloudflare tunnel to birds.vivessato.com |
| 10 | — (Docker) | `go2rtc` | RTSP to WebRTC/HLS, port 1984 |

All LaunchAgents use KeepAlive (except health-monitor which uses StartInterval, and rtsp-sync which uses StartCalendarInterval).

---

## Ports

| Port | Service |
|------|---------|
| 1984 | go2rtc (Docker) — WebRTC/HLS video streaming |
| 8096 | enhanced_audio_stream.py — bandpass MP3 stream |
| 8097 | live_detector.py — real-time detection SSE |
| 8098 | audio_analyzer.py — BirdNET audio SSE + clips |
| 8099 | api.py (uvicorn) — dashboard API + frontend |

External access: `birds.vivessato.com` routes through Cloudflare tunnel to port 8099.

---

## Key Configuration

### Detection Thresholds

**Visual (batch — classify.py)**:
- YOLO bird detection confidence: >= 0.3
- Regional species filter: `models/chilmark_feeder_species.txt` (230 species for batch, 61 for live)
- Nighttime pause: ~30 min after sunset to sunrise (NOAA solar, 41.39N 70.61W)

**Visual (live — live_detector.py)**:
- YOLO confidence: >= 0.35, NMS IoU >= 0.45
- SpeciesVoter: 2 agreeing frames out of 5, 5s cooldown, IoU 0.3 match threshold
- Polls go2rtc at 3fps per camera

**Audio (audio_analyzer.py)**:
- MIN_CONFIDENCE: 0.50
- Overlap confirmation: min_confirmations=2 within 6s flush window
- Sample rate: 48kHz mono, 6s window with 2s overlap
- Location: 41.35N, -70.73W (Chilmark, MA)

### Models

| Model | File | Purpose |
|-------|------|---------|
| YOLOv8n | `models/yolov8n_bird.onnx` (12MB) | Bird detection (COCO class 14) |
| AIY Birds V1 | `models/aiy_birds_v1.onnx` (3.4MB) | Species classification (965 classes) |
| BirdNET V2.4 | via `birdnetlib` | Audio species detection |

### Python Environments

- **`venv/`** (Python 3.12) — batch classifier + dashboard API. `onnxruntime==1.23.2` (last Intel Mac version).
- **`venv-coral/`** (Python 3.9) — live detector, audio analyzer, enhanced audio. ONNX with CoreML, birdnetlib, PyAV, scipy, noisereduce.

### Coral TPU

Google Coral USB Accelerator connected to iMac. Used by AIY Birds V1 for species classification (~5ms inference). Edge TPU runtime 2022-10-24.

---

## Directory Layout

```
/Users/vives/bird-snapshots/
  incoming/           Snapshots awaiting classification
  classified/         Organized by species subdirectories
  skipped/            No bird detected
  failed/             Corrupt images
  annotated/          JPEGs with bounding boxes + labels
  birdnet-audio/
    birdnet_local.db  Audio detections SQLite DB
    clips/            Saved WAV detection clips
  logs/               All service logs

/Users/vives/bird-classifier/
  classify.py         Batch classification pipeline
  capture_snapshots.py  Motion-triggered snapshot capture
  live_detector.py    Real-time detection + SSE
  audio_analyzer.py   BirdNET audio analysis
  enhanced_audio_stream.py  Bandpass MP3 stream
  health_monitor.py   Service health checker
  refresh_rtsp.py     RTSP token refresh
  dashboard/
    api.py            FastAPI backend
    index.html        Dashboard SPA
    species_images/   Cached bird photos (224 species)
  models/             ONNX models + species labels
  venv/               Python 3.12 environment
  venv-coral/         Python 3.9 environment
```

---

## Troubleshooting

### Check all services at once

```bash
launchctl list | grep bird
```

A running service shows a PID in the first column. A `-` means it is not running.

### Check Docker go2rtc

```bash
docker ps | grep go2rtc
# Health check:
curl -s http://localhost:1984/api/streams
```

### Check individual services

```bash
# Dashboard API
curl -s http://localhost:8099/api/health | python3 -m json.tool

# Live detector
curl -s http://localhost:8097/health | python3 -m json.tool

# Audio analyzer — check logs (no health endpoint)
tail -20 /Users/vives/bird-snapshots/logs/audio-analyzer-stdout.log

# Enhanced audio
curl -s http://localhost:8096/health | python3 -m json.tool

# Cloudflare tunnel
tail -20 /Users/vives/bird-snapshots/logs/cloudflare-tunnel-stdout.log

# Batch classifier
tail -20 /Users/vives/bird-snapshots/logs/classifier-stdout.log

# Capture snapshots
tail -20 /Users/vives/bird-snapshots/capture.log

# Health monitor
tail -20 /Users/vives/bird-snapshots/logs/health-monitor-stdout.log
```

### Restart a service

```bash
# Unload then reload the LaunchAgent
launchctl unload ~/Library/LaunchAgents/com.vives.bird-livedetect.plist
launchctl load ~/Library/LaunchAgents/com.vives.bird-livedetect.plist
```

Replace the label for any service. For go2rtc:

```bash
docker restart go2rtc
```

### Reprocess all images

```bash
launchctl unload ~/Library/LaunchAgents/com.vives.bird-classifier.plist
cd /Users/vives/bird-classifier && ./venv/bin/python classify.py --reprocess
launchctl load ~/Library/LaunchAgents/com.vives.bird-classifier.plist
```

### Common Issues

**Service keeps crashing (exit code != 0)**: Check stderr log for the service. Most common causes: RTSP stream unavailable (camera rebooting), port already in use, Python import error.

**No new classifications**: Check that `bird-capture` is running (produces snapshots) and `bird-classifier` is running (processes them). During nighttime, the classifier pauses automatically.

**Dashboard not loading externally**: Check `bird-tunnel` is running. Verify with `curl -s http://localhost:8099/api/health` that the API is up locally.

**go2rtc streams not working**: Camera RTSP tokens expire. Check `bird-rtsp-sync` ran successfully, or manually run `refresh_rtsp.py`. Restart go2rtc after token refresh.

**macOS sleep kills everything**: Ensure sleep is disabled: `pmset -a sleep 0 && pmset -a disksleep 0`.

**onnxruntime upgrade breaks things**: `onnxruntime==1.23.2` is the last version with Intel Mac (x86_64) wheels. Do not upgrade.

---

## Historical Note

Until March 2026, the system used a two-machine architecture with a VivesSyn NAS hosting the dashboard (nginx + Traefik), go2rtc, BirdNET-Go, and snapshot sync. All services were migrated to the iMac in March 2026. The NAS is no longer part of the system.
