# Bird Observatory — CLAUDE.md (Pi-side repo)

> **You are in the Pi-side repo at `/Users/vives/bird-classifier-pi/`.**
> Pi-Claude commits live here. Edit files here and rsync to Pi (`vives@pi5.local:/home/vives/bird-classifier/`) for deployment.
>
> The iMac-side repo at `/Users/vives/bird-classifier/` is iMac-Claude's territory — don't push to it.
>
> Cross-cutting fixes (anything that benefits both sides) flow via patches in `docs/working/progress/cross-claude-comms.md`. David relays.
>
> Both repos share git history through commit `5773551` (split point, 2026-04-25). They diverge from there.
>
> See `docs/working/progress/2026-04-25-pi-repo-split.md` for the full split context.
> The Pi reference book (chapters 00-08) lives at `~/docs/bird-observatory-pi/`, parallel to iMac's at `~/docs/bird-observatory/`.
>
> **Remote branch model (added 2026-05-11):**
> - iMac repo pushes to `imac-origin/main` (long-lived).
> - **Pi repo (this one) pushes to `imac-origin/pi-main`** (long-lived). Local `main` is configured to track it (`git status` should show `main...imac-origin/pi-main`).
> - Ad-hoc snapshot branches for outside review (`pi-snapshot-YYYY-MM-DD`, `pi-overnight-YYYY-MM-DD`) are fine; delete when done.
> - **Do NOT push the Pi-repo's `main` to `imac-origin/main`** — that's the iMac repo's branch and the histories have diverged since the April 25 split.
> - Cross-repo merges happen via cherry-pick or PR, never a forced push.

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

## Architecture (Pi-side)

Raspberry Pi 5 + Hailo-8L AI Hat + Crucial P3 2 TB NVMe (USB-3) + UniFi G3 Dome over LAN. Raspberry Pi OS Lite (Debian Trixie, Python 3.13).
SQLite is the sole data store: `~/bird-snapshots/logs/classifications.db` (per-classification rows), `pipeline.db` (event store), `pi_reviews.db` (Pi-native ✓/✗ verdicts).

For the full reference, see the chapter book at `~/docs/bird-observatory-pi/` (00-overview through 08-deployment).

### Services (4 systemd-user units + 1 timer)

| Service | Port | What it does |
|---------|------|-------------|
| `go2rtc.service` | 1984 | RTSP-in from UniFi → WebRTC/MSE/HLS-out (native binary, not Docker) |
| `bird-pipeline.service` | 8100 (health), 8105 (SSE) | Motion gate → Hailo YOLO → tracker → AIY classifier → snapshot writer |
| `bird-dashboard.service` | 8099 | uvicorn `dashboard.api:app` — Pi-native dashboard, Live view, Model Lab, Pi-review |
| `cloudflared.service` | — | Tunnel: pi5.vivessato.com → :8099 |
| `pi5-thermal-watch.timer` | — | Fires every 60s — appends one row to `~/logs/pi5-thermal-watch.csv` |

`loginctl enable-linger vives` is set so all start without a login session.

### Detection Pipeline (v3 on Pi)

go2rtc (RTSP) → FrameCapture (`feeder-sub` 640×360, native ~30 fps, no fps filter) → HailoDetector (YOLOv8s on Hailo-8L, **full-frame every frame**) → BirdTracker (Norfair + Frigate-distance) → PiClassifier (AIY ONNX on CPU, ~7.4 ms / crop) → vote lock → SnapshotWriter (1080p frame extracted from the HLS passthrough-mux segments by PTS at lock; AIY rerun) → SSE broadcast → dashboard.

> **2026-06-29 foundations changes:** MotionGate/MOG2 is **bypassed on the Hailo path** — HailoDetector runs full-frame and ignores motion regions, so computing MOG2 every frame was wasted CPU and the thermal hog (F1). Tracker `hit_counter_max` 90→**150** (~5s coast through detection gaps; S1); `distance_threshold` 2.5; all tracker params env-tunable (`PIPELINE_TRACK_*`). Classifier vote-eligibility floor 0.25→**0.16** (`PIPELINE_CLASSIFIER_FLOOR`), `MAX_CLASSIFICATION_ATTEMPTS` 5→12 (F2); lock gate unchanged (≥3 votes, ≥0.35 conf, ≥60% agreement). The hi-res ring buffer is legacy (None); snapshots come from the HLS-by-PTS path. See `docs/working/specs/2026-06-27-pi-live-id-foundations-design.md`.

Per-camera classifier config: only feeder camera enabled (ground commented out in `bird_pipeline_v3.py:CAMERAS_DETECT`). Pi has no Coral; AIY runs on CPU. Hailo classifiers (ResNet50, YOLOv8s, YOLOv6n) cohabit with the YOLOv8 detector on a single shared `VDevice` via the HailoRT scheduler — see `docs/04-hailo-engine.md` and `docs/working/specs/2026-04-25-hailo-playbook.md`.

### Video Path

- **Local**: Browser → `<video-stream>` element → WebRTC direct to go2rtc:1984 (UDP, real-time, sub-second)
- **Remote**: Browser → MSE via wss://go2rtc.vivessato.com (TCP, buffered, auto-fallback)
- Labels rendered client-side as DOM elements positioned per displayed video frame by the **video-clock overlay engine** (2026-07-02): rVFC `rtpTimestamp` (WebRTC) / `mediaTime` (MSE) anchor the camera's 90 kHz clock; SSE events (canonical `pts`, same crystal) are BUFFERED and played out at the video's pace — never applied at arrival. Epoch offset C auto-calibrated (rolling median + measured jitter-buffer depth); Smooth mode deepens the WebRTC jitter buffer to ~0.8s (`jitterBufferTarget`) for guaranteed lookahead, Realtime toggle for minimum delay. Coasting tracks render dashed/held. Spec: `docs/working/specs/2026-07-02-overlay-video-clock-sync-design.md`

## Key Rules

- Read the mission above before every session
- Small modular changes, verify end-to-end before moving on
- Don't assume data structures — read the actual code
- Test wherever possible
- Honesty over optimism
