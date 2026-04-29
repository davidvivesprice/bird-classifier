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
    ┌─────────────┴──────────────────────┐
    │                                    │
    ▼                                    ▼
 go2rtc (native binary, :1984)     audio_analyzer
    │                                   │
    ├── sub-streams (640×360)           ▼
    │      ↓                       birdnet_local.db
    │   bird_pipeline_v3            + audio clips
    │   (motion→YOLO→classify→track)
    │      ↓
    │   SSE :8105 + snapshot writer
    │      ↓
    │   classifications.db
    │
    └── main-streams (1080p)
           ↓
        HLS recorder → segments.json + .ts files
           ↓
        browser /live (hls.js, ~10s delay)
           │
           ▼
        WebRTC (real-time, <400ms)
           │
           └── canvas overlay (labels from SSE)

Everything routes through api.py :8099 → Cloudflare tunnel → birds.vivessato.com
```

Everything runs on the iMac (192.168.4.200). The CloudKey provides camera feeds only.

---

## Data Pipelines

**Live visual** (~5–7 fps on iMac): `bird_pipeline_v3.py` reads RTSP sub-streams from go2rtc via ffmpeg. Per camera: MotionGate (MOG2 + AOI polygon) → YOLO bird detection (full frame) → Norfair tracker → SmartClassifier (yard model on Coral for feeder; AIY-only for ground) → vote-lock (≥3 votes, ≥0.35 confidence, ≥60% agreement). On lock: SnapshotWriter saves a JPG, runs AIY authoritative relabel, writes a row to `classifications.db`. SSE events stream to the dashboard for live overlay.

**Audio** (~3s): `audio_analyzer.py` decodes RTSP audio via PyAV, applies bandpass + noisereduce, runs BirdNET V2.4 on 3-second overlapping windows, writes to SQLite (`birdnet_local.db`) + saves WAV clips.

**Enhanced audio** (live): `enhanced_audio_stream.py` applies bandpass filter (300Hz–15kHz) to RTSP audio and serves it as an MP3 stream for in-browser listening.

---

## Services

Eight services — seven macOS LaunchAgents plus one native binary LaunchAgent for go2rtc. No Docker.

| Label | Script | Details |
|-------|--------|---------|
| `com.vives.go2rtc` | `go2rtc` (native binary) | RTSP relay from CloudKey, WebRTC/HLS to browser, port 1984 |
| `com.vives.bird-pipeline` | `bird_pipeline_v3.py` | Detection + classification + snapshot writer; health :8100, SSE :8105 |
| `com.vives.bird-dashboard` | `uvicorn` (api.py) | FastAPI backend + dashboard SPA, port 8099 |
| `com.vives.bird-audio` | `audio_analyzer.py` | BirdNET audio analysis, port 8098 |
| `com.vives.bird-enhanced-audio` | `enhanced_audio_stream.py` | Bandpass MP3 audio stream, port 8096 |
| `com.vives.bird-integrity-audit` | integrity audit script | Periodic data integrity check (StartInterval) |
| `com.vives.bird-tunnel` | `cloudflared tunnel run` | Cloudflare tunnel: birds.vivessato.com → :8099 |
| `com.vives.bird-rtsp-sync` | `refresh_rtsp.py` | RTSP token refresh, daily at 3:10 AM (StartCalendarInterval) |

All services use KeepAlive except `bird-integrity-audit` (StartInterval) and `bird-rtsp-sync` (StartCalendarInterval).

**Deactivated** (plist files removed; scripts kept in repo):
- `com.vives.bird-classifier` (`classify.py --watch`) — Coral USB is single-session; the v3 pipeline holds it
- `com.vives.bird-capture` (`capture_snapshots.py`) — replaced by v3 pipeline's snapshot writer
- `bird-livedetect` (`live_detector.py`) — deleted; replaced by v3 pipeline

---

## Ports

| Port | Service |
|------|---------|
| 1984 | go2rtc (native) — WebRTC/HLS video streaming |
| 8096 | enhanced_audio_stream.py — bandpass MP3 stream |
| 8098 | audio_analyzer.py — BirdNET audio SSE + clips |
| 8099 | api.py (uvicorn) — dashboard API + frontend |
| 8100 | bird_pipeline_v3.py — health endpoint |
| 8105 | bird_pipeline_v3.py — SSE event stream |

External access: `birds.vivessato.com` routes through Cloudflare tunnel to port 8099.

---

## Key Configuration

### Detection Thresholds

**Visual (bird_pipeline_v3.py)**:
- YOLO bird confidence: 0.3 (`bird_pipeline_v3.py:260`)
- Vote-lock: ≥3 votes, top species ≥0.35 confidence, ≥60% vote share
- Nighttime pause: ~30 min after sunset to sunrise (NOAA solar, 41.35N 70.73W)
- After `MAX_CLASSIFICATION_ATTEMPTS = 5` without lock: takes plurality winner

**Feeder classifier (SmartClassifier, yard-first)**:
- Yard confident (≥0.25): accept yard answer, `model_source=YARD`
- Yard low (<0.10): skip to AIY alone
- Yard uncertain (0.10–0.25): cross-check with AIY; accept if they agree, else unlabeled
- AIY always runs as the authoritative relabel at snapshot time (overrides yard label in DB)

**Ground classifier**: AIY-only (no yard model for ground camera). Ground cam detection is currently disabled in the pipeline (`bird_pipeline_v3.py:34`) — the camera streams to go2rtc for live viewing but is not being processed for bird detection.

**Audio (audio_analyzer.py)**:
- MIN_CONFIDENCE: 0.50
- Overlap confirmation: min_confirmations=2 within a 6s flush window
- Sample rate: 48kHz mono, 3s analysis window
- Location: 41.35N, -70.73W (Chilmark, MA)

### Models

| Model | File | Purpose |
|-------|------|---------|
| YOLOv8n | `models/yolov8n_bird.onnx` | Bird detection (COCO class 14) |
| AIY Birds V1 | `models/aiy_birds_v1.onnx` | Species classification (965 classes) |
| Yard model | `models/yard_model.tflite` | 12 feeder-cam species (Coral Edge TPU) |
| BirdNET V2.4 | via `birdnetlib` | Audio species detection |

The yard model is a custom-trained TFLite EfficientNet-Lite0 compiled for Coral Edge TPU. AIY Birds V1 runs via ONNX Runtime (CoreML on iMac); it also has a Coral-compiled `_edgetpu.tflite` variant in `models/` for Pi deployment.

**12 yard-model species**: American Goldfinch, Black-capped Chickadee, Brown-headed Cowbird, Carolina Wren, Dark-eyed Junco, Downy Woodpecker, Hairy Woodpecker, House Finch, Northern Cardinal, Song Sparrow, Tufted Titmouse, White-breasted Nuthatch.

### Python Environments

- **`venv/`** (Python 3.12) — dashboard API, utility scripts. `onnxruntime==1.23.2` (last Intel Mac version; do not upgrade).
- **`venv-coral/`** (Python 3.9) — live pipeline, audio analyzer, enhanced audio. Includes pycoral, birdnetlib, PyAV, scipy, noisereduce.

### Coral TPU

Google Coral USB Accelerator connected to iMac. Used exclusively by the yard model (YardClassifier via pycoral). The pipeline holds the Coral lock throughout its lifetime — this is why `classify.py` is deactivated (it would compete for the same device).

---

## Directory Layout

```
/Users/vives/bird-snapshots/
  classified/         Organized by species subdirectories (JPGs from pipeline)
  annotated/          JPEGs with corner-bracket bounding boxes
  hls/
    feeder/           HLS .ts segments + segments.json sidecar
    (ground/ would appear here when ground cam detection is re-enabled)
  birdnet-audio/
    birdnet_local.db  Audio detections SQLite DB
    clips/            Saved WAV detection clips
  logs/               All service logs (pipeline.log, pipeline-stderr.log, etc.)

/Users/vives/bird-classifier/
  bird_pipeline_v3.py   Main detection + classification pipeline
  bird_tracker.py       IoU-based multi-bird tracker (Norfair)
  audio_analyzer.py     BirdNET audio analysis
  enhanced_audio_stream.py  Bandpass MP3 stream
  refresh_rtsp.py       RTSP token refresh
  reviews_db.py         Review/calibration DB layer
  classifications_db.py Classifications DB layer
  pipeline/             Pipeline modules (frame_capture, detector, classifier, etc.)
  dashboard/
    api.py              FastAPI backend
    index.html          Dashboard SPA (Review, Classify, Live tabs)
    live.html           Dedicated live view with adaptive lock overlay
  models/               ONNX + TFLite models + species labels
  venv/                 Python 3.12 environment
  venv-coral/           Python 3.9 environment
  tests/                pytest test suite
```

---

## Troubleshooting

### Check all services at once

```bash
launchctl list | grep -E 'vives\.(bird|go2rtc)'
```

A running service shows a PID in the first column. A `-` means it is not running.

### Check individual services

```bash
# Dashboard API
curl -s http://localhost:8099/api/health | python3 -m json.tool

# Pipeline (detection + classification)
curl -s http://localhost:8100/api/pipeline/health | python3 -m json.tool

# Audio analyzer — check logs (no health endpoint)
tail -20 /Users/vives/bird-snapshots/logs/audio-analyzer-stdout.log

# Enhanced audio
curl -s http://localhost:8096/health | python3 -m json.tool

# Cloudflare tunnel
tail -20 /Users/vives/bird-snapshots/logs/cloudflare-tunnel-stdout.log
```

### Restart a service

```bash
# Any bird service (replace label as needed)
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-pipeline"
launchctl kickstart -k "gui/$(id -u)/com.vives.bird-dashboard"

# go2rtc
launchctl kickstart -k "gui/$(id -u)/com.vives.go2rtc"
```

### Check what classifications are coming in

```bash
sqlite3 ~/bird-snapshots/logs/classifications.db \
  "SELECT source_timestamp, common_name, confidence,
          json_extract(extra_json,'$.model_source')
   FROM classifications ORDER BY id DESC LIMIT 10;"
```

### go2rtc streams not working

Camera RTSP tokens expire. Check `bird-rtsp-sync` ran successfully, or manually run `refresh_rtsp.py`. Restart go2rtc after token refresh:

```bash
launchctl kickstart -k "gui/$(id -u)/com.vives.go2rtc"
```

### Nighttime — no new classifications

Expected. The pipeline pauses ~30 min after sunset and resumes at sunrise. Check `pipeline.log` for "night bypass" messages.

### macOS sleep kills everything

```bash
pmset -a sleep 0 && pmset -a disksleep 0
```

### onnxruntime upgrade breaks things

`onnxruntime==1.23.2` is the last version with Intel Mac (x86_64) wheels. Do not upgrade.

---

## Historical Note

**Before March 2026**: Two-machine architecture — a VivesSyn NAS hosted the dashboard (nginx + Traefik), go2rtc (Docker), BirdNET-Go, and snapshot sync. All services migrated to the iMac in March 2026. The NAS is no longer part of the system.

**Before April 2026**: The iMac ran a batch visual pipeline (`capture_snapshots.py` + `classify.py --watch`) alongside an older real-time detector (`live_detector.py`). The batch pipeline polled the CloudKey snapshot API; `live_detector.py` pulled frames from go2rtc and ran YOLO + AIY with temporal voting. Both were replaced by `bird_pipeline_v3.py`, which does motion-gated continuous RTSP detection. The batch scripts are kept in the repo for reference; their LaunchAgent plists have been removed.

**go2rtc**: Originally ran in Docker. Migrated to a native binary (`/usr/local/bin/go2rtc`) with its own LaunchAgent (`com.vives.go2rtc`) for cleaner process management and faster startup.
