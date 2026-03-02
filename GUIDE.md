# Bird Classification & Dashboard System — Complete Guide

## Overview

A two-stage bird species classification pipeline running on an iMac, with a web dashboard served from VivesSyn NAS. Motion-triggered snapshots from a bird feeder camera are classified using computer vision, and the results are combined with BirdNET-Go audio detections into a unified bird observatory dashboard.

**Dashboard URL**: `https://birds.vivessyn.duckdns.org` (basic auth: user `birds`)

---

## Architecture

```
                                    ┌─────────────────┐
                                    │  Bird Feeder     │
                                    │  Camera (RTSP)   │
                                    └────────┬────────┘
                                             │
                   ┌─────────────────────────┤
                   │                         │
            ┌──────▼──────┐           ┌──────▼──────┐
            │  VivesSyn   │           │  VivesSyn   │
            │  Motion     │           │  BirdNET-Go │
            │  Detection  │           │  (audio)    │
            │  Snapshots  │           │  SQLite DB  │
            └──────┬──────┘           └──────┬──────┘
                   │ SCP (60s)               │ cron (5m)
            ┌──────▼──────┐           ┌──────▼──────┐
            │  iMac       │           │  export     │
            │  classify.py│           │  _birdnet.sh│
            │  (YOLO+AIY) │           │  →JSON      │
            └──────┬──────┘           └──────┬──────┘
                   │                         │
            ┌──────▼──────┐           ┌──────▼──────┐
            │  FastAPI     │           │  nginx      │
            │  :8099       │           │  birds-share│
            └──────┬──────┘           └──────┬──────┘
                   │                         │
                   └──────────┬──────────────┘
                              │
                    ┌─────────▼─────────┐
                    │  Dashboard SPA    │
                    │  birds.vivessyn   │
                    │  .duckdns.org     │
                    └───────────────────┘
```

---

## Components

### 1. Motion Detection Snapshots (VivesSyn)

**Location**: `/volume1/docker/scripts/bird_snapshots.py`
**Runs as**: `@reboot` entry in `/etc/crontab`
**Output**: JPEGs in `/volume1/docker/bird-snapshots/captures/`

Polls the UniFi Protect API for the bird feeder camera, uses PIL-based frame differencing to detect motion, saves JPEG snapshots at ~10s intervals during activity.

### 2. Snapshot Sync (iMac → VivesSyn)

**Script**: `/Users/vives/bird-classifier/sync_snapshots.sh`
**LaunchAgent**: `com.vives.bird-sync` (every 60 seconds)
**Plist**: `/Users/vives/Library/LaunchAgents/com.vives.bird-sync.plist`

Uses SSH+SCP (not rsync — macOS openrsync has SSH transport bugs) to pull new snapshots from VivesSyn to the iMac. Checks three directories (incoming, classified, failed) to avoid re-downloading already-processed files. Atomic downloads via `.tmp` rename.

**Key settings**:
- Remote: `vives@192.168.5.92:2000`
- Remote dir: `/volume1/docker/bird-snapshots/captures/`
- Local dir: `/Users/vives/bird-snapshots/incoming/`
- SSH key: `/Users/vives/.ssh/id_ed25519`
- Lock file: `/tmp/bird-sync.lock` (via `lockf`, not `flock`)

### 3. Bird Classifier (iMac)

**Script**: `/Users/vives/bird-classifier/classify.py`
**LaunchAgent**: `com.vives.bird-classifier` (watch mode, KeepAlive)
**Plist**: `/Users/vives/Library/LaunchAgents/com.vives.bird-classifier.plist`
**Venv**: `/Users/vives/bird-classifier/venv/` (Python 3.12.12)

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
- Regional filter: `models/cape_cod_species.txt` (230 species)
  - Filters out impossible species (Australian, African, etc.)
  - Falls back to next-best regional match if top-1 is filtered
  - Logs both raw and filtered predictions

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
└── logs/
    ├── classifications.jsonl   # All results (one JSON per line)
    ├── classifier.log          # Runtime log
    ├── classifier-stdout.log   # LaunchAgent stdout
    ├── classifier-stderr.log   # LaunchAgent stderr
    ├── dashboard-stdout.log    # Dashboard API stdout
    └── dashboard-stderr.log    # Dashboard API stderr
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

### 4. Dashboard API (iMac)

**Script**: `/Users/vives/bird-classifier/dashboard/api.py`
**LaunchAgent**: `com.vives.bird-dashboard` (port 8099, KeepAlive)
**Plist**: `/Users/vives/Library/LaunchAgents/com.vives.bird-dashboard.plist`

FastAPI serving classifier data over REST:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/health` | GET | Uptime check |
| `/api/stats` | GET | Overall counts |
| `/api/species` | GET | All species with counts |
| `/api/species/{name}` | GET | Detail for one species |
| `/api/recent?limit=50` | GET | Recent detections |
| `/api/image/{filename}` | GET | Annotated JPEG |
| `/api/review/pending` | GET | Unreviewed items |
| `/api/review/{filename}` | POST | Submit verdict |
| `/api/regional-species` | GET | Species filter list |

### 5. BirdNET-Go Audio Detection (VivesSyn)

**Container**: `birdnet-go`
**Config**: `/volume1/docker/birdnet/config/config.yaml`
**DB**: Docker volume `35bfc1d5.../_data/birdnet.db`
**Web UI**: `birdnet.vivessyn.duckdns.org` (Authelia auth)

Listens to 4 RTSP camera microphones, detects bird songs using the BirdNET neural network. Location: 41.391°N, -70.613°W (Cape Cod, MA).

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

### 6. BirdNET Export (VivesSyn)

**Script**: `/volume1/docker/birds-hls/export_birdnet.sh`
**Schedule**: `/etc/crontab` — every 5 minutes
**Output**: `/volume1/docker/birds-hls/birdnet-data/summary.json`

Queries the BirdNET SQLite DB and exports a JSON summary with species counts, avg confidence, and last-seen timestamps.

### 7. Dashboard Frontend (VivesSyn)

**File**: `/volume1/docker/birds-hls/index.html`
**Container**: `birds-share` (nginx:alpine)
**URL**: `https://birds.vivessyn.duckdns.org`

Single-page app with two tabs:
- **Dashboard**: Live camera feed, species bar chart, species cards, recent sightings
- **Review**: Annotation GUI for confirming/rejecting classifications

**Nginx routing** (`/volume1/docker/birds-hls/nginx.conf`):

| Path | Target |
|------|--------|
| `/` | `index.html` (dashboard SPA) |
| `/api/` | `go2rtc:1984` (WebRTC/HLS live feed) |
| `/hls/` | `go2rtc:1984` (HLS fallback) |
| `/bird-api/` | `192.168.4.68:8099` (iMac classifier API) |
| `/birdnet-data/` | Static JSON files |

---

## What Worked

1. **YOLOv8n for bird detection**: Excellent at filtering no-bird frames. 82% of snapshots correctly skipped.
2. **ONNX Runtime on Intel Mac**: `onnxruntime==1.23.2` is the last version with x86_64 macOS wheels. Works perfectly.
3. **SSH+SCP for sync**: Completely avoids SMB mount fragility (`/Volumes/docker` vs `/Volumes/docker-1`).
4. **Regional species filter**: Eliminated all impossible classifications (Royal Spoonbill, Elegant Tern, etc.) with zero code complexity.
5. **BirdNET-Go audio data**: 1,100+ detections, 34 species — excellent complement to visual data.
6. **Static JSON export for BirdNET**: Simple cron + sqlite3 + python3 one-liner. No extra services needed.
7. **FastAPI on iMac**: Lightweight, fast, serves annotated images directly from disk.

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

---

## Key Configuration

### Network

| Host | IP | SSH Port | Role |
|------|----|----------|------|
| vivess-iMac | 192.168.4.68 | — | Classifier + API |
| VivesSyn | 192.168.5.92 | 2000 | Camera, BirdNET, Dashboard |
| VivesNAS | 192.168.4.243 | 2000 | Storage (not used by bird system) |

### LaunchAgents (iMac)

| Label | What | Log |
|-------|------|-----|
| `com.vives.bird-sync` | SCP snapshot sync (60s) | `bird-snapshots/sync.log` |
| `com.vives.bird-classifier` | classify.py --watch | `bird-snapshots/logs/classifier-*.log` |
| `com.vives.bird-dashboard` | uvicorn :8099 | `bird-snapshots/logs/dashboard-*.log` |

### Docker Containers (VivesSyn)

| Container | Image | Purpose |
|-----------|-------|---------|
| `birds-share` | nginx:alpine | Dashboard + API proxy |
| `go2rtc` | alexxit/go2rtc | WebRTC/HLS live feed |
| `birdnet-go` | tphakala/birdnet-go | Audio bird detection |

### Models

| Model | File | Size | Input | Purpose |
|-------|------|------|-------|---------|
| YOLOv8n | `yolov8n.onnx` | 12MB | [1,3,640,640] float32 | Bird detection (COCO class 14) |
| AIY Birds V1 | `aiy_birds_v1.onnx` | 3.4MB | [1,224,224,3] uint8 | Species classification (965 classes) |

### Python Dependencies

```
onnxruntime==1.23.2    # CRITICAL: last version with Intel Mac wheels
numpy==1.26.4
pillow==12.1.1
fastapi==0.135.1
uvicorn==0.41.0
```

---

## Operational Commands

### Check classifier status
```bash
launchctl list | grep bird
tail -20 /Users/vives/bird-snapshots/logs/classifier.log
```

### Reprocess all images
```bash
launchctl unload ~/Library/LaunchAgents/com.vives.bird-classifier.plist
cd /Users/vives/bird-classifier
./venv/bin/python classify.py --reprocess
launchctl load ~/Library/LaunchAgents/com.vives.bird-classifier.plist
```

### View summary
```bash
cd /Users/vives/bird-classifier && ./venv/bin/python classify.py --summary
```

### Check dashboard API
```bash
curl http://localhost:8099/api/stats
curl http://localhost:8099/api/species
```

### Check BirdNET data
```bash
ssh -p 2000 vives@192.168.5.92 "cat /volume1/docker/birds-hls/birdnet-data/summary.json | python3 -m json.tool | head -20"
```

### Restart dashboard
```bash
launchctl unload ~/Library/LaunchAgents/com.vives.bird-dashboard.plist
launchctl load ~/Library/LaunchAgents/com.vives.bird-dashboard.plist
```

### Restart birds-share on VivesSyn
```bash
ssh -p 2000 vives@192.168.5.92 "cd /volume1/docker && sudo /usr/local/bin/docker compose restart birds-share"
```

---

## Git History

```
0366d81 Initial commit: two-stage bird classifier pipeline
cd1b7d7 Add regional species filter for Cape Cod, MA
f664e64 Add FastAPI dashboard backend
8da6edd Add bird observatory dashboard and BirdNET export
```

## Current Stats (as of 2026-03-02)

- **Visual classifier**: 250+ birds classified across 31 species from 1,400+ images
- **Audio (BirdNET-Go)**: 1,100+ detections across 34 species
- **Top species (visual)**: Song Sparrow (84), Northern Cardinal (37), Black-capped Chickadee (29)
- **Top species (audio)**: Black-capped Chickadee (238), Blue Jay (189), White-breasted Nuthatch (135)
- **Processing speed**: ~130ms per image (detection + classification)

## Future Improvements

1. **Better species model**: Swap AIY Birds V1 (2017) for a modern model with better accuracy
2. **Species info cards**: Fetch descriptions and photos from Nuthatch API or Wikipedia
3. **Fine-tuning**: Use reviewed annotations as training data for a custom model
4. **Real-time SSE**: Stream new detections to the dashboard without polling
