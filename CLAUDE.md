# Bird Observatory — CLAUDE.md

## Mission

Build a bird identification system that is **delightful to use, deadly accurate, and tells beautiful stories with data**.

### Who it's for
- **Casual curious observers**: "What bird is that?" → instant, visual, fun answer
- **Obsessive birders**: Deep data, trends, rare species alerts, seasonal patterns
- **The system itself**: Data that feeds back to make identification more accurate over time

### What matters (in order)
1. **Accuracy** — If it says "Cardinal," there better be a Cardinal
2. **Experience** — Simple, fun, beautiful on a phone. Non-techy people get it instantly
3. **Reliability** — Just works. Self-heals. Never needs babysitting
4. **Rich data** — Stories, not just numbers. First arrivals, peak hours, rare visitors

### What we're NOT building
- A developer debugging tool (engineering stays invisible)
- A system that needs babysitting
- Something complicated

### Technical principles
- Light on the processor
- Modular and simple — each piece does one thing
- Self-healing — breaks fix themselves
- Ground truth — known-good data validates continuously
- Data feeds accuracy — reviews retrain the model

## Architecture

Single 2017 iMac (i5-7400, 8GB RAM). CloudKey Gen 2+ manages two UniFi cameras.
SQLite is the sole data store (classifications.db for visual, birdnet_local.db for audio, pipeline.db for v3 events).

### Services (7 active + 1 cron)

| Service | Port | What it does |
|---------|------|-------------|
| go2rtc (native binary) | 1984 | RTSP-in from CloudKey, WebRTC/MSE/HLS-out to browser |
| bird_pipeline_v3 | 8100 (health), 8105 (SSE) | Motion gate → YOLO → track → vote-classify → SSE events |
| dashboard (uvicorn) | 8099 | Serves HTML, proxies SSE/health, REST API for classifications |
| audio_analyzer | 8098 | BirdNET audio analysis |
| enhanced_audio | 8096 | Enhanced audio MP3 stream |
| bird-integrity-audit | — | Periodic data integrity check (StartInterval) |
| cloudflared | — | Tunnel: birds.vivessato.com → :8099, go2rtc.vivessato.com → :1984 |
| rtsp-sync (cron) | — | Refreshes RTSP tokens daily at 3:10 AM |

### Detection Pipeline (v3)

Camera → go2rtc (RTSP) → FrameCapture (native substream, 640×360, reads at YOLO rate ~5–7 fps) → MotionGate → BirdDetector (YOLO) → BirdTracker → SmartClassifier (yard model on Coral TPU → AIY fallback) → vote lock (≥3 votes, ≥0.35 conf, ≥60% agreement) → SSE broadcast → dashboard canvas overlay.

Per-camera classifier config: feeder uses yard model (Coral) + AIY fallback, ground uses AIY only.

### Video Path

- **Local**: Browser → WebRTC direct to go2rtc:1984 (UDP, real-time, smooth)
- **Remote**: Browser → MSE via wss://go2rtc.vivessato.com (TCP, buffered, auto-fallback)
- Labels rendered client-side on canvas overlay, synced via wall-clock time + SSE events

## Key Rules

- Read the mission above before every session
- Small modular changes, verify end-to-end before moving on
- Don't assume data structures — read the actual code
- Test wherever possible
- Honesty over optimism
