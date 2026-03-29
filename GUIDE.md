# Bird Classification & Dashboard System — Complete Guide

## Overview

A real-time bird observatory running on an iMac + VivesSyn NAS, combining live video detection, audio analysis, and batch classification into a unified dashboard. Two RTSP cameras (feeder + ground) provide both video frames for visual bird detection and audio streams for BirdNET analysis.

**Dashboard URL**: `https://birds.vivessyn.duckdns.org:9444/` (cookie-based auth)

---

## Architecture

```
                    ┌──────────────┐    ┌──────────────┐
                    │ Feeder Cam   │    │ Ground Cam   │
                    │   (RTSP)     │    │   (RTSP)     │
                    └──────┬───────┘    └──────┬───────┘
                           │                   │
        ┌──────────────────┼───────────────────┤
        │                  │                   │
        ▼                  ▼                   ▼
  ┌───────────┐   ┌────────────────┐   ┌───────────────┐
  │ VivesSyn  │   │ iMac           │   │ iMac          │
  │ go2rtc    │   │ live_detector  │   │ audio_analyzer│
  │ (WebRTC)  │   │ :8097 (SSE)   │   │ (BirdNET V2.4)│
  └─────┬─────┘   └────────┬───────┘   └───────┬───────┘
        │                  │                   │
        │           ┌──────▼───────┐   ┌───────▼───────┐
        │           │ Dashboard    │   │ Local SQLite  │
        │           │ bounding box │   │ birdnet_local │
        │           │ overlays +   │   │ .db + clips/  │
        │           │ Recent Sights│   └───────┬───────┘
        │           └──────────────┘           │
        │                                      │
  ┌─────▼──────────────────────────────────────▼──────┐
  │                  Dashboard SPA                     │
  │        birds.vivessyn.duckdns.org:9444             │
  │  Live feed │ Species chart │ Recent │ In the Yard  │
  └────────────────────────────────────────────────────┘

  Batch pipeline (background, 1-2h latency):
    NAS snapshots → sync_snapshots.sh (SCP 60s) → classify.py → JSONL → API
```

### Data Flows

1. **Real-time video** (~300ms): Camera RTSP → go2rtc frame API → `live_detector.py` (YOLO + classifier + temporal voting) → SSE → Dashboard bounding boxes + Recent Sightings
2. **Real-time audio** (~3s): Camera RTSP audio → `audio_analyzer.py` (bandpass + noisereduce + BirdNET V2.4) → local SQLite → FastAPI SSE → Dashboard "In the Yard"
3. **Batch visual** (~1-2h): Camera motion snapshots → NAS → SCP sync → `classify.py` (YOLO + AIY Birds) → JSONL → FastAPI → Dashboard species chart
4. **Enhanced audio** (live): Camera RTSP audio → `enhanced_audio_stream.py` (bandpass 300Hz-15kHz) → MP3 stream → Dashboard audio toggle

---

## Components

### 1. Live Bird Detector (iMac) — Real-Time

**Script**: `/Users/vives/bird-classifier/live_detector.py`
**LaunchAgent**: `com.vives.bird-livedetect` (KeepAlive)
**Venv**: `/Users/vives/bird-classifier/venv-coral/` (Python 3.9)
**Port**: 8097

Polls go2rtc's `/api/frame.jpeg` endpoint at 3fps for both cameras, runs YOLOv8n detection + AIY Birds V1 species classification, and pushes detection events via Server-Sent Events (SSE). The dashboard overlays bounding boxes and species labels on the live video feed, and injects detections directly into "Recent Sightings" for instant freshness.

**Endpoints**:

| Path | Description |
|------|-------------|
| `/events` | SSE stream of detection events |
| `/health` | JSON health check (stream status, client count) |

**Detection pipeline**:
1. Fetch JPEG frame from go2rtc via HTTPS (with auth cookie)
2. YOLOv8n bird detection (confidence ≥ 0.35, NMS IoU ≥ 0.45)
3. Crop detected bird region with 15% padding
4. AIY Birds V1 species classification with regional filter
5. Filter: skip "background"/"unidentified" and raw_score < 5
6. **Temporal voting** (SpeciesVoter): require consistent species ID before broadcasting

**Camera streams**:

| Camera | go2rtc stream | Description |
|--------|---------------|-------------|
| feeder | `feeder-main` | Bird feeder camera |
| ground | `ground-main` | Ground-level camera |

#### Temporal Voting (SpeciesVoter)

Prevents "species flickering" — the problem where the same stationary bird gets classified as 6 different species across consecutive frames because the classifier processes each frame independently with no temporal memory.

**How it works**:
- Tracks bird positions across frames using IoU matching (threshold 0.3)
- Each tracked position accumulates a sliding window of species votes from the last 5 classifications
- A detection is only broadcast when the top-voted species has ≥ 2 agreeing frames
- 5-second cooldown prevents re-reporting the same species at the same position
- Stale slots expire after 3 seconds of no detections
- Maximum 20 tracked slots per camera

**Constants**:
```python
VOTE_MIN_HITS = 2           # need ≥2 agreeing frames
VOTE_WINDOW = 5             # out of the last 5 classifications
VOTE_IOU_MATCH = 0.3        # IoU threshold for "same bird"
VOTE_COOLDOWN_SEC = 5.0     # don't re-report same species/position within 5s
```

**Effect**: ~0.6s delay (2 frames at 3fps) before first report, then stable species labels. Dramatically reduces false species IDs from shadow detections and ambiguous angles.

#### SSE Event Schema

```json
{
  "camera": "ground",
  "species": "Dark-eyed Junco",
  "scientific_name": "Junco hyemalis",
  "confidence": 0.86,
  "raw_score": 110,
  "bbox": [x1, y1, x2, y2],
  "frame_width": 1920,
  "frame_height": 1080,
  "timestamp": "2026-03-17T12:58:06.123456",
  "inference_ms": 176.3
}
```

**Note**: `confidence` is the YOLO detection confidence (how sure it is there's *a bird*), while `raw_score` is the classifier's species confidence (0-255 uint8, how sure it is *which* bird). The SSE greeting `{"type":"connected"}` is sent immediately on client connect to prime HTTP/2 streams through Traefik/nginx.

#### Species Aliases

Subspecies and regional forms are mapped to canonical parent species:
```python
SPECIES_ALIASES = {
    "Slate-colored Junco": "Dark-eyed Junco",
    "Myrtle Warbler": "Yellow-rumped Warbler",
    "Feral Pigeon": "Rock Pigeon",
}
```

### 2. Audio Analyzer (iMac) — Real-Time

**Script**: `/Users/vives/bird-classifier/audio_analyzer.py`
**LaunchAgent**: `com.vives.bird-audio` (KeepAlive)
**DB**: `~/bird-snapshots/birdnet-audio/birdnet_local.db`
**Clips**: `~/bird-snapshots/birdnet-audio/clips/`

Pulls audio from RTSP camera stream via FFmpeg, preprocesses with bandpass + noisereduce, analyzes 6-second overlapping windows with BirdNET V2.4, saves detected clips as WAV files, and writes to a local SQLite database. Replaces the NAS-hosted BirdNET-Go with a faster, fully-controlled solution running on the iMac.

**Audio preprocessing pipeline**:
1. **Bandpass filter** (300Hz-15kHz, 4th order Butterworth) — removes wind rumble and ultrasonic noise
2. **noisereduce spectral gating** — suppresses broadband noise within the passband
3. **RMS normalization** — restores original signal level so BirdNET sees audio at trained amplitude

**Detection thresholds** (tuned to match BirdNET-Go sensitivity):
- `MIN_CONFIDENCE`: 0.50 (minimum confidence to consider a detection)
- `DYNAMIC_THRESHOLD_MIN`: 0.25 (lowest a dynamic threshold can go for known species)
- `DEEP_DETECTION_INSTANT`: 0.65 (single detection above this = instant accept)
- `DEEP_DETECTION_MIN_HITS`: 2 (require 2 detections within 15s window for confirmation)
- `DEEP_DETECTION_COOLDOWN`: 10s (prevent re-accepting same species too quickly)

**Analysis parameters**:
- Sample rate: 48kHz mono
- Analysis window: 6 seconds with 2.0s overlap
- Buffer advance: 3 seconds per step
- Location: 41.35°N, -70.73°W (Chilmark, MA)

### 3. Enhanced Audio Stream (iMac) — Live Playback

**Script**: `/Users/vives/bird-classifier/enhanced_audio_stream.py`
**LaunchAgent**: `com.vives.bird-enhanced-audio` (KeepAlive)
**Port**: 8096

Captures RTSP audio via python-av, applies a bandpass filter (300Hz-15kHz) to isolate bird call frequencies, encodes to MP3 via ffmpeg, and serves as a streaming HTTP endpoint. The dashboard toggles between this enhanced stream and the raw camera audio.

**Architecture**: `RTSP → python-av (decode) → bandpass filter → ring buffer → ffmpeg (encode) → HTTP`

**Endpoints**:

| Path | Description |
|------|-------------|
| `/stream.mp3` | MP3 audio stream (192kbps) |
| `/health` | JSON health check |

**Key design decisions**:
- **Bandpass-only** — no noisereduce, no RMS normalization, no crossfade. Earlier versions with noisereduce spectral gating produced audible clicking/distortion artifacts during wind gusts. The bandpass-only approach sounds natural with no artifacts.
- **Ring buffer** (30 chunks × 1s = ~30s) with condition variable notification for low-latency client serving
- **Per-client ffmpeg encoder** — each connected client gets its own PCM→MP3 encoder process
- **Threaded HTTP server** — handles multiple concurrent audio clients

**Dependencies**: `av` (python-av), `numpy`, `scipy` (for bandpass filter)

### 4. Motion Detection Snapshots (VivesSyn)

**Location**: `/volume1/docker/scripts/bird_snapshots.py`
**Runs as**: `@reboot` entry in `/etc/crontab`
**Output**: JPEGs in `/volume1/docker/bird-snapshots/captures/`

Polls the UniFi Protect API for the bird feeder camera, uses PIL-based frame differencing to detect motion, saves JPEG snapshots at ~10s intervals during activity.

### 5. Snapshot Sync (VivesSyn → iMac)

**Script**: `/Users/vives/bird-classifier/sync_snapshots.sh`
**LaunchAgent**: `com.vives.bird-sync` (every 60 seconds)

Uses SSH+SCP (not rsync — macOS openrsync has SSH transport bugs) to pull new snapshots from VivesSyn to the iMac. Checks three directories (incoming, classified, failed) to avoid re-downloading already-processed files. Atomic downloads via `.tmp` rename.

**Key settings**:
- Remote: `vives@192.168.5.92:2000`
- Remote dir: `/volume1/docker/bird-snapshots/captures/`
- Local dir: `/Users/vives/bird-snapshots/incoming/`
- SSH key: `/Users/vives/.ssh/id_ed25519`
- Lock file: `/tmp/bird-sync.lock` (via `lockf`, not `flock`)

**Note**: Must use bash 3.2-compatible syntax (no `declare -A`). Uses temp file + `grep -qxF` for lookups.

### 6. Batch Classifier (iMac)

**Script**: `/Users/vives/bird-classifier/classify.py`
**LaunchAgent**: `com.vives.bird-classifier` (watch mode, KeepAlive)
**Venv**: `/Users/vives/bird-classifier/venv/` (Python 3.12.12)

#### Nighttime Scheduling

Automatically pauses ~30 minutes after sunset and resumes at sunrise. Uses NOAA solar algorithm for Cape Cod coordinates (41.39°N, 70.61°W). Night vision confuses the species model. In watch mode, polls every 5 minutes during nighttime instead of every 10 seconds.

#### Two-Stage Pipeline

**Stage 1 — Bird Detection (YOLOv8n)**:
- Model: `models/yolov8n.onnx` (12MB, COCO 80-class)
- Input: 640×640 float32 (letterbox preprocessing)
- Detects COCO class 14 ("bird") with confidence ≥ 0.3
- Pure-numpy NMS (no OpenCV dependency)
- If no bird detected → image moved to `skipped/`

**Stage 2 — Species Classification (AIY Birds V1)**:
- Model: `models/aiy_birds_v1.onnx` (3.4MB, MobileNetV2)
- Input: 224×224 uint8 (cropped bird region with 15% padding)
- Output: 965 classes (964 species + background)
- Labels: `models/inat_bird_labels.txt`
- Regional filter: `models/chilmark_feeder_species.txt` (61 species for live detector, 230 for batch)
  - Filters out impossible species (Australian, African, etc.)
  - Falls back to next-best regional match if top-1 is filtered

#### Output Directories

```
/Users/vives/bird-snapshots/
├── incoming/          # New snapshots from sync (input)
├── classified/        # Organized by species subdirectories
│   ├── Song_Sparrow/
│   ├── Northern_Cardinal/
│   └── ...
├── skipped/           # No bird detected
├── failed/            # Corrupt or unreadable images
├── annotated/         # JPEGs with bounding boxes + labels
├── birdnet-audio/     # Audio analyzer output
│   ├── birdnet_local.db  # Local BirdNET SQLite DB
│   └── clips/            # Saved WAV detection clips
└── logs/
    ├── classifications.jsonl     # All visual classification results
    ├── classifier.log            # Batch classifier runtime log
    ├── live_detector.log         # Live detector runtime log
    ├── live_detector_stdout.log  # Live detector LaunchAgent stdout
    ├── live_detector_stderr.log  # Live detector LaunchAgent stderr
    ├── classifier-stdout.log     # Batch classifier LaunchAgent stdout
    ├── classifier-stderr.log     # Batch classifier LaunchAgent stderr
    ├── dashboard-stdout.log      # Dashboard API stdout
    ├── dashboard-stderr.log      # Dashboard API stderr
    ├── enhanced-audio-stdout.log # Enhanced audio LaunchAgent stdout
    ├── enhanced-audio-stderr.log # Enhanced audio LaunchAgent stderr
    ├── audio-analyzer-stdout.log # Audio analyzer LaunchAgent stdout
    └── audio-analyzer-stderr.log # Audio analyzer LaunchAgent stderr
```

#### JSONL Schema (per entry)

```json
{
  "file": "2026-03-02_10-47-50.jpg",
  "timestamp": "2026-03-02T11:14:00.123456",
  "source_timestamp": "2026-03-02 10-47-50",
  "action": "classified",
  "detect_ms": 130.5,
  "classify_ms": 12.3,
  "total_ms": 143.1,
  "detections": 1,
  "best_detection": {"box": [120, 200, 450, 580], "confidence": 0.901},
  "top_prediction": {"common_name": "Song Sparrow", "scientific_name": "Melospiza melodia", "raw_score": 186},
  "top3": [...],
  "raw_top3": [...]
}
```

### 7. Dashboard API (iMac)

**Script**: `/Users/vives/bird-classifier/dashboard/api.py`
**LaunchAgent**: `com.vives.bird-dashboard` (port 8099, KeepAlive)

FastAPI serving classifier data, species images, and real-time SSE over REST:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Uptime check |
| `/api/stats` | GET | Overall counts (filterable by `?date=YYYY-MM-DD`) |
| `/api/species` | GET | All species with counts (filterable by date) |
| `/api/species/{name}` | GET | Detail for one species |
| `/api/recent?limit=50` | GET | Recent detections |
| `/api/dates` | GET | List of dates with classified detections |
| `/api/image/{filename}` | GET | Annotated JPEG |
| `/api/species-image/{name}` | GET | Cached species photo (Wikipedia REST API fallback) |
| `/api/review/pending` | GET | Unreviewed items |
| `/api/review/{filename}` | POST | Submit verdict (correct/wrong/skip/trash) |
| `/api/regional-species` | GET | Species filter list |

**Species images**: 224 locally-cached bird photos in `dashboard/species_images/`, served via FastAPI with 24h cache headers. Falls back to on-demand Wikipedia REST API download if not cached.

### 8. BirdNET-Go Audio Detection (VivesSyn)

**Container**: `birdnet-go`
**Config**: `/volume1/docker/birdnet/config/config.yaml`
**DB**: Docker volume `35bfc1d5.../_data/birdnet.db`
**Web UI**: `birdnet.vivessyn.duckdns.org` (Authelia auth)

Listens to 4 RTSP camera microphones, detects bird songs using the BirdNET neural network. Location: 41.391°N, -70.613°W (Cape Cod, MA). Being phased out in favor of the iMac-based `audio_analyzer.py`.

**DB Schema** (key table):
```sql
CREATE TABLE notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_node TEXT, date TEXT, time TEXT, source TEXT,
  begin_time DATETIME, end_time DATETIME,
  species_code TEXT, scientific_name TEXT, common_name TEXT,
  confidence REAL, latitude REAL, longitude REAL,
  threshold REAL, sensitivity REAL,
  clip_name TEXT, processing_time INTEGER
);
```

### 9. BirdNET SSE Server (VivesSyn)

**Script**: `/volume1/docker/birds-hls/birdnet_sse.py`
**Port**: 8098
**Schedule**: `@reboot` + healthcheck every 5 min in `/etc/crontab`

Python 3.8 stdlib HTTP server that polls the BirdNET-Go SQLite DB every 3 seconds and pushes new detections as SSE events to connected dashboard clients. Also serves audio clips.

**Endpoints**:

| Path | Description |
|------|-------------|
| `/events` | SSE stream of BirdNET detection events |
| `/clips/<year>/<month>/<file>.wav` | Audio clip playback |
| `/health` | Health check |

### 10. BirdNET Export (VivesSyn)

**Script**: `/volume1/docker/birds-hls/export_birdnet.sh`
**Schedule**: `/etc/crontab` — every 5 minutes
**Output**: `/volume1/docker/birds-hls/birdnet-data/summary.json`

Queries the BirdNET SQLite DB and exports a JSON summary with species counts, avg confidence, and last-seen timestamps. Structure: `{ species: [...], by_date: { "YYYY-MM-DD": { species: [...] } }, dates: [...] }`

### 11. Dashboard Frontend (VivesSyn)

**File**: `/volume1/docker/birds-hls/index.html` (~2000+ lines)
**Source**: `/Users/vives/bird-classifier/dashboard/index.html`
**Container**: `birds-share` (nginx:alpine)
**URL**: `https://birds.vivessyn.duckdns.org:9444/`

Single-page app with:
- **Live camera feed**: WebRTC via go2rtc with detection bounding box overlays
- **Audio toggle**: Raw camera audio vs Enhanced (bandpass-filtered) audio
- **Species Detection Counts chart**: Horizontal bar chart (Chart.js v4) with rainbow palette — solid bars for camera (📷), faded/dashed bars for audio BirdNET (🎙️). Y-axis labels are clickable → species info popup.
- **Recent Sightings**: Thumbnails with species name, confidence %, time ago. **Live detections** from the live detector SSE are injected directly with a ⚡ LIVE badge, bypassing the slow batch pipeline.
- **Currently in Yard** (topbar): BirdNET species heard in last 10 minutes with real-time SSE updates and pulse animations. 🔊 speaker button plays detection clips.
- **Date selector**: Today (default) | Yesterday | "More…" dropdown (All Time + older dates)
- **Species info popup**: Wikipedia summary + Xeno-canto audio player + locally-cached species image
- **Review tab**: Annotation GUI for confirming/rejecting/trashing classifications

**Dashboard SSE connections**:
1. **Live detection SSE** (`/live-detections/events`): Receives bounding box + species data, draws canvas overlay, injects into Recent Sightings (with 30s species+camera dedup)
2. **BirdNET SSE** (`/birdnet-sse/events`): Receives audio detections, updates "In the Yard" bar with pulse animation

**Deploy command**:
```bash
scp -P 2000 -i ~/.ssh/id_ed25519 /Users/vives/bird-classifier/dashboard/index.html vives@192.168.5.92:/volume1/docker/birds-hls/index.html
```

---

## Nginx Proxy Routing

**Config**: `/volume1/docker/birds-hls/nginx.conf`
**Container**: `birds-share` (nginx:alpine)
**Auth**: Cookie-based (`birdauth` cookie checked against allowed tokens)

| Path | Target | Description |
|------|--------|-------------|
| `/` | `index.html` | Dashboard SPA |
| `/login` | `login.html` | Login page (no auth required) |
| `/stream.html` | `go2rtc:1984` | WebRTC streaming page |
| `/api/` | `go2rtc:1984` | WebRTC/HLS API (video streams) |
| `/hls/` | `go2rtc:1984` | HLS fallback |
| `/bird-api/` | `192.168.4.200:8099` | iMac classifier/dashboard API |
| `/live-detections/` | `192.168.4.200:8097` | iMac live detector SSE |
| `/enhanced-audio/` | `192.168.4.200:8096` | iMac enhanced audio MP3 stream |
| `/birdnet-sse/` | `172.22.0.1:8098` | BirdNET SSE server (Docker host) |
| `/birdnet-clips/` | `172.22.0.1:8098/clips/` | BirdNET audio clips |
| `/birdnet-data/` | Static files | BirdNET summary JSON |

**SSE proxy settings** (critical for streaming):
```nginx
proxy_http_version 1.1;
proxy_set_header Connection '';
proxy_buffering off;
proxy_cache off;
proxy_read_timeout 86400;
chunked_transfer_encoding off;
```

**Auth flow**: All paths except `/login` and `/healthz` require a valid `birdauth` cookie. Invalid/missing cookies get a `302` redirect to `/login`. Note: `absolute_redirect off` is required — without it, 302 redirects lose the `:9444` port behind Traefik.

---

## What Worked

1. **YOLOv8n for bird detection**: Excellent at filtering no-bird frames. 82% of snapshots correctly skipped.
2. **ONNX Runtime on Intel Mac**: `onnxruntime==1.23.2` is the last version with x86_64 macOS wheels. Works perfectly.
3. **SSH+SCP for sync**: Completely avoids SMB mount fragility (`/Volumes/docker` vs `/Volumes/docker-1`).
4. **Regional species filter**: Eliminated all impossible classifications (Royal Spoonbill, Elegant Tern, etc.) with zero code complexity.
5. **BirdNET-Go audio data**: 1,100+ detections, 34 species — excellent complement to visual data.
6. **Static JSON export for BirdNET**: Simple cron + sqlite3 + python3 one-liner. No extra services needed.
7. **FastAPI on iMac**: Lightweight, fast, serves annotated images directly from disk.
8. **Temporal voting (SpeciesVoter)**: Eliminated species flickering with minimal latency (~0.6s). IoU-based position tracking + vote accumulation is simple and effective.
9. **Bandpass-only enhanced audio**: Clean, artifact-free audio with just a frequency filter. No processing that can introduce distortion.
10. **SSE for real-time updates**: Live detections appear on dashboard within ~1s, vs 1-2h for the batch pipeline.
11. **Local species images**: Wikipedia REST API downloads cached locally, eliminating Wikimedia Commons 429 rate-limiting.

## What Didn't Work

1. **tflite-runtime on macOS**: No wheels exist for any macOS architecture. Dead end.
2. **ai-edge-litert**: Only has macOS ARM wheels, not Intel x86_64.
3. **tensorflow on Intel Mac**: `tensorflow==2.16.2` is the last version with x86_64 wheels but has AVX-512 segfault risk on i5-7400. Only used for one-time model conversion, not runtime.
4. **tflite2onnx**: Produced malformed ONNX with Gemm rank errors. `tf2onnx` worked correctly.
5. **macOS openrsync**: Has SSH transport bugs. `rsync -e "ssh -p 2000"` fails with "Permission denied" even though standalone SSH works fine. Fell back to SCP.
6. **Classifier-only approach (no YOLO)**: ALL images classified as American Goldfinch because the model classifies the entire frame. The yellow tape measure triggered goldfinch every time. Two-stage detect→classify was essential.
7. **BirdNET-Go REST API**: HTMX-based, returns HTML fragments not JSON. Unusable for programmatic access. Direct SQLite queries work great though.
8. **SCP file permissions from NAS**: Files come with `dr-xr-xr-x` permissions. Need `chmod u+rw` before moving.
9. **`flock` on macOS**: Doesn't exist. Use `lockf` instead.
10. **noisereduce on live audio stream**: Spectral gating creates audible clicking/distortion artifacts during wind gusts, amplified by RMS normalization. Gate-switching artifacts are the fundamental problem. Bandpass-only is the right approach for the live listening stream.
11. **Wiener spectral subtraction**: STFT-based soft power-domain mask. A/B tested against bandpass+noisereduce — user heard "distracting artifacting." Removed entirely.
12. **EventSource `onmessage` property**: Silently fails inside IIFE closures in the dashboard JS. Must use `addEventListener('message', fn)` instead.
13. **HTTP/2 SSE without priming**: Traefik/nginx buffer the SSE connection until the first data frame. An initial `data: {"type":"connected"}\n\n` greeting is required to prime the stream.

---

## Key Configuration

### Network

| Host | IP | SSH Port | Role |
|------|----|----------|------|
| vivess-iMac | 192.168.4.200 | — | Classifier, API, live detector, audio analyzer, enhanced audio |
| VivesSyn | 192.168.5.92 | 2000 | Cameras, go2rtc, BirdNET-Go, Dashboard (nginx), Traefik |
| VivesNAS | 192.168.4.243 | 2000 | Storage (not used by bird system) |

### Ports (iMac)

| Port | Service | Description |
|------|---------|-------------|
| 8096 | `enhanced_audio_stream.py` | Bandpass-filtered MP3 audio stream |
| 8097 | `live_detector.py` | Real-time detection SSE server |
| 8098 | `birdnet_sse.py` (NAS) | BirdNET detection SSE server |
| 8099 | `api.py` (uvicorn) | FastAPI dashboard backend |

### LaunchAgents (iMac)

| Label | What | Python | Log |
|-------|------|--------|-----|
| `com.vives.bird-sync` | SCP snapshot sync (60s) | system | `bird-snapshots/sync.log` |
| `com.vives.bird-classifier` | classify.py --watch | `venv/` | `logs/classifier-*.log` |
| `com.vives.bird-dashboard` | uvicorn :8099 | `venv/` | `logs/dashboard-*.log` |
| `com.vives.bird-livedetect` | live_detector.py --fps 3 | `venv-coral/` | `logs/live_detector*.log` |
| `com.vives.bird-enhanced-audio` | enhanced_audio_stream.py | system + `venv-coral` PYTHONPATH | `logs/enhanced-audio-*.log` |
| `com.vives.bird-audio` | audio_analyzer.py | system + `venv-coral` PYTHONPATH | `logs/audio-analyzer-*.log` |

### Crontab Entries (VivesSyn NAS `/etc/crontab`)

**NOTE: NAS (VivesSyn) was decommissioned March 2026. All services now run on the iMac.**

| Schedule | Command | Purpose |
|----------|---------|---------|
| `@reboot` | `birdnet_sse.py` | Start BirdNET SSE server |
| `*/5 * * * *` | `export_birdnet.sh` | Export BirdNET summary JSON |
| `*/5 * * * *` | `curl healthcheck` | Restart birdnet_sse.py if dead |

### Docker Containers (VivesSyn)

| Container | Image | Purpose |
|-----------|-------|---------|
| `birds-share` | nginx:alpine | Dashboard + API proxy + auth |
| `go2rtc` | alexxit/go2rtc | WebRTC/HLS live camera feed |
| `birdnet-go` | tphakala/birdnet-go | Audio bird detection |

### Models

| Model | File | Size | Input | Purpose |
|-------|------|------|-------|---------|
| YOLOv8n (bird) | `yolov8n_bird.onnx` | 12MB | [1,3,640,640] float32 | Bird detection |
| AIY Birds V1 | `aiy_birds_v1.onnx` | 3.4MB | [1,224,224,3] uint8 | Species classification (965 classes) |
| BirdNET V2.4 | via `birdnetlib` | — | 48kHz float32 audio | Audio species detection |

### Python Environments

**`venv/`** (Python 3.12.12) — batch classifier + dashboard API:
```
onnxruntime==1.23.2    # CRITICAL: last version with Intel Mac wheels
numpy==1.26.4
pillow==12.1.1
fastapi==0.135.1
uvicorn==0.41.0
```

**`venv-coral/`** (Python 3.9) — live detector + audio:
```
onnxruntime            # with CoreML provider
numpy
pillow
scipy                  # bandpass filter (butter, sosfilt)
noisereduce            # spectral gating for audio analyzer
av                     # python-av for RTSP audio decoding
birdnetlib             # BirdNET V2.4 Python wrapper
```

---

## Operational Commands

### Check all services
```bash
launchctl list | grep bird
```

### Check live detector
```bash
curl -s http://localhost:8097/health | python3 -m json.tool
tail -30 /Users/vives/bird-snapshots/logs/live_detector.log
```

### Restart live detector
```bash
launchctl unload ~/Library/LaunchAgents/com.vives.bird-livedetect.plist
launchctl load ~/Library/LaunchAgents/com.vives.bird-livedetect.plist
```

### Check enhanced audio
```bash
curl -s http://localhost:8096/health | python3 -m json.tool
```

### Check batch classifier
```bash
tail -20 /Users/vives/bird-snapshots/logs/classifier.log
cd /Users/vives/bird-classifier && ./venv/bin/python classify.py --summary
```

### Check dashboard API
```bash
curl -s http://localhost:8099/api/stats | python3 -m json.tool
curl -s http://localhost:8099/api/species | python3 -m json.tool
```

### Check BirdNET SSE (NAS)
```bash
ssh -p 2000 vives@192.168.5.92 "curl -s http://localhost:8098/health"
```

### Reprocess all images
```bash
launchctl unload ~/Library/LaunchAgents/com.vives.bird-classifier.plist
cd /Users/vives/bird-classifier
./venv/bin/python classify.py --reprocess
launchctl load ~/Library/LaunchAgents/com.vives.bird-classifier.plist
```

### Deploy dashboard
```bash
scp -P 2000 -i ~/.ssh/id_ed25519 /Users/vives/bird-classifier/dashboard/index.html vives@192.168.5.92:/volume1/docker/birds-hls/index.html
```

### Restart nginx on VivesSyn
```bash
ssh -p 2000 vives@192.168.5.92 "cd /volume1/docker && sudo /usr/local/bin/docker compose restart birds-share"
```

---

## Important Technical Notes

- **macOS bash is v3.2.57** — no `declare -A` (associative arrays). Use temp files + `grep -qxF`.
- **Chart.js onClick** doesn't fire for Y-axis label clicks. Use native `canvas.addEventListener('click')`.
- **Chart.js uses CSS pixels** for `chartArea`, `getValueForPixel()`. Do NOT multiply by DPI ratio on retina.
- **macOS sleep tears down ALL networking** (WiFi and Ethernet) — must disable sleep via `pmset -a sleep 0 && pmset -a disksleep 0`.
- **ThreadingHTTPServer needs `allow_reuse_address = True`** set on the class before construction to avoid "Address already in use" on restart.
- **`onnxruntime==1.23.2`** is the last version with Intel Mac (x86_64) wheels. Do not upgrade.
- **SSE through Traefik/nginx requires**: `proxy_buffering off`, `Connection ''`, `chunked_transfer_encoding off`, and an initial greeting message to prime HTTP/2 streams.
- **nginx `absolute_redirect off`** required — without it, 302 redirects lose the `:9444` port behind Traefik.

---

## Quality Audit Log

### Phase 7 — Production Hardening (2026-03-17)
13 issues found across 3 backend services, all fixed:
- **enhanced_audio_stream.py**: ffmpeg stderr deadlock (→DEVNULL), O(n²) numpy concat (→list accumulation), RTSP container leak (→try/finally), bandpass filter state reset (→sosfilt_zi), zombie ffmpeg (→kill fallback), reader thread not joined (→join on shutdown), fixed reconnect delay (→exponential backoff 3→30s)
- **live_detector.py**: YOLO/classifier crash kills camera thread (→try/except), label parsing breaks on nested parentheses (→rindex)
- **audio_analyzer.py**: per-insert SQLite connection (→persistent conn + lock), no inference timeout (→30s thread watchdog), no reconnect backoff (→exponential 5→30s), clip save crash on disk full (→try/except)

### Phase 8 — Deep Audit Round 2 (2026-03-17)
Second-pass audit covering api.py, index.html, classify.py, sync_snapshots.sh. ~80 raw findings triaged → 10 confirmed bugs, 16 false positives rejected:
- **index.html**: Canvas event listeners stack on every chart re-render (→remove before add)
- **api.py**: Corrupt species images cached permanently on partial download (→atomic temp+rename), cull_trash_species crashes with 500 (→try/except with counts), rerun_missed stale cache (→force invalidation)
- **live_detector.py**: daemon thread vs block_on_close contradiction (→remove daemon flag)
- **audio_analyzer.py**: Inference timeout thread leak warning
- **enhanced_audio_stream.py**: encoder.stdout.read blocks indefinitely (→select with timeout)
- **classify.py**: DST boundary off by 2h (→timezone-aware datetime), auto-cull missing try/except, partial JPEG race with sync

Key false positives rejected: SpeciesVoter logic is intentional (not broken), int(scores) correct for uint8 model output, escAttr XSS protection is sound, SSE reconnect properly managed, CPython GIL makes dict/deque ops atomic.

### What This Means for Feature Development

The Phase 7+8 audit eliminated a class of problems that made feature work unpredictable:

**Before the audit:**
- Adding a new chart feature could trigger the canvas listener stacking bug (8A) — popups open N times after N refreshes
- Any RTSP interruption (camera reboot, network blip) leaked TCP connections (7C) and hammered the camera with reconnects every 3s (7J) until it locked out
- A single ONNX inference crash killed the entire camera thread with no recovery (7D) — new detection features were untestable under real conditions
- The ffmpeg audio encoder could silently deadlock (7A) or hang forever (8F), making enhanced audio unreliable as a platform for new processing modes
- Species image downloads could corrupt the cache permanently (8B), breaking any feature that shows bird photos
- The numpy O(n²) concat (7B) meant audio processing degraded over time — new audio features would inherit this performance cliff

**After the audit:**
- All three services recover gracefully from errors — try/except around every external call, exponential backoff on reconnects, proper resource cleanup
- Dashboard re-renders are idempotent — no listener accumulation, no state leaks
- File operations are atomic where needed and error-handled everywhere else
- The audio pipeline is O(n) with proper filter state continuity — ready for new processing stages
- New features can assume the infrastructure won't silently fail underneath them

**Practical impact:** You can now safely add new dashboard panels, new detection models, new audio processing stages, or new API endpoints without worrying about the foundation crumbling. Errors are logged, resources are cleaned up, and services self-heal.
