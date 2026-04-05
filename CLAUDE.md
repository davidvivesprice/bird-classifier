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

Single machine: iMac runs everything. CloudKey Gen 2+ manages cameras.
SQLite is the sole data store (classifications.db for visual, birdnet_local.db for audio).
11 LaunchAgent services with KeepAlive. Cloudflare tunnel for external access.

Two detection systems run in parallel:
- **Old system**: `capture_snapshots.py` (polls CloudKey) -> `classify.py --watch` (batch) -> `live_detector.py` (SSE on port 8097)
- **New pipeline**: `bird_pipeline.py` — unified real-time detection. Decodes RTSP via go2rtc/PyAV at ~3 FPS. Motion gate -> YOLO -> species classification with yard prior -> IoU multi-bird tracking (`bird_tracker.py`) -> SSE broadcast on port 8100. Saves keeper frames to incoming/. Dashboard toggle ("New Det" / "Old Det") switches between the two SSE sources.

## Key Rules

- Read the mission above before every session
- Small modular changes, verify end-to-end before moving on
- Don't assume data structures — read the actual code
- Test wherever possible
- Honesty over optimism
