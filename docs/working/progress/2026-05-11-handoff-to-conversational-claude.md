# Handoff: Pi Bird Observatory — read this cold

**Intended reader:** a fresh Claude (Claude.ai web) helping David think through architecture/options for his bird-feeder observation system. You have no prior context with this project. This doc gives you enough to converse meaningfully about decisions, trade-offs, and next steps without asking dozens of basic questions.

**Author of this doc:** another Claude (Claude Code on David's iMac) who spent last night executing a planned overnight rebuild of the live-overlay system. Bias: I've been close to it for 4+ hours, so my framing may favor what I just shipped. Push back where appropriate.

**Where the code is:** `github.com/davidvivesprice/bird-classifier` (public). The branch with everything described here is `pi-overnight-2026-05-11` (or `main` if David pushed to main). The Pi-side codebase lives at `bird-classifier-pi` on David's iMac; the deployed copy is at `/home/vives/bird-classifier/` on the Pi 5 itself. Both share git history from a split point at commit `5773551` on 2026-04-25.

---

## The project in one paragraph

David is building a bird identification system for his backyard feeder. It's not a debugging tool, not a developer toy — it's meant to be **delightful, accurate, and reliable**. A UniFi G3 Dome camera watches the feeder; a Raspberry Pi 5 with a Hailo-8L NPU does motion gating → YOLO detection → bird tracking → species classification → snapshot storage → dashboard display. The user can be his wife on her phone ("what bird is that?"), or him obsessively reviewing patterns. Mission file: `CLAUDE.md` at repo root.

## The hardware

- **Pi 5 + Hailo-8L AI Hat** (NPU for YOLO + classifier inference)
- **Crucial P3 2 TB NVMe over USB-3** for storage (no SD-card writes)
- **UniFi G3 Dome IP camera** (outputs H.264; no H.265 support — relevant later)
- Boot config: SD card holds bootloader+kernel; NVMe holds rootfs (Pi 5 can't reliably boot from USB-NVMe enclosures per `~/.claude/projects/-Users-vives/memory/feedback_pi5_rtl9210_boot.md`).
- Pi 5 has hardware HEVC decode (`rpi-hevc-dec`) but **NO hardware H.264 decoder** and **no hardware encoder of either codec**. This matters a lot for architecture choices.
- Active fan attached to GPIO J17 header, kernel-controlled cooling driver, sits at ~75-78°C under load post-fix (was 84-86°C and throttling before last night).

## Architecture (services on Pi)

Four systemd-user services. Run unprivileged under user `vives`; `loginctl enable-linger vives` is set so they survive logout.

| Service | Port | Job |
|---|---|---|
| `go2rtc.service` | 1984 | RTSP → WebRTC/MSE relay. Pulls from UniFi camera + (now) the local demo-loop mediamtx. Vendored binary. Config: `go2rtc.yaml` (gitignored — fresh rotating tokens). |
| `bird-pipeline.service` | 8100/8105 | The actual pipeline. Reads RTSP, runs Hailo + AIY, broadcasts SSE. |
| `bird-dashboard.service` | 8099 | FastAPI app serving `dashboard/pi_dash.html` plus all API endpoints (snapshots, demo-mode, SSE proxy, reviews, etc.). |
| `cloudflared.service` | — | Tunnel: `pi5.vivessato.com → :8099` for remote access. |
| `bird-demo-loop.service` | 8654 | Optional helper: mediamtx + ffmpeg loops a pre-encoded demo MP4 as RTSP so the pipeline can be tested without live birds. Toggled by `/api/demo-mode`. |

Plus a daily timer `refresh-rtsp.timer` at 03:10 that runs `tools/refresh_rtsp.py` to renew UniFi tokens and rewrite `go2rtc.yaml`. (Mind this — it bit us last night, fix in commit `b71362b`.)

## Pipeline shape

```
go2rtc (RTSP relay)
    │
    ▼
FrameCapture           ──┐ shared decoded frame
(PyAV reads camera        ├──→ SnapshotWriter (saves JPEGs on lock)
 substream 640×360,       │
 30 fps native)           ├──→ MotionGate (MOG2 + AOI polygon)
                          │       │
                          │       ▼
                          │   HailoDetector (YOLOv8 on NPU; ~zero CPU)
                          │       │
                          │       ▼ tracks per bbox
                          │   BirdTracker (Norfair)
                          │       │
                          │       ▼ on track-lock (≥3 votes, ≥0.35 conf, ≥60% agreement)
                          │   PiClassifier (AIY-ONNX, runs on CPU, ~7 ms / crop)
                          │
                          └──→ HlsSegmenter (separate consumer, packet-passthrough mux for replay)

         │
         ▼
SSE broadcast → dashboard `<div class="live-bbox">` + `<div class="live-label">`
```

Key files:
- `bird_pipeline_v3.py` — orchestrator
- `pipeline/frame_capture.py` — PyAV reader, single-stream architecture
- `pipeline/motion_gate.py` — MOG2 + Area-of-Interest polygon
- `pipeline/hailo_detector.py` — YOLO inference on Hailo NPU
- `pipeline/tracker.py` — Norfair-based bird tracker
- `pipeline/pi_classifier.py` — AIY-ONNX species classifier
- `pipeline/snapshot_writer.py` — saves JPGs + writes to `classifications.db`
- `pipeline/hls_segmenter.py` — passthrough mpegts mux for the replay/harness path
- `pipeline/sse_events.py` — SSE broadcaster
- `dashboard/pi_dash.html` — single-file dashboard
- `dashboard/api.py` — all the HTTP endpoints

## Stream layout (this is the part everyone forgets)

go2rtc currently has five named streams:

- `feeder-main` → UniFi feeder 1080p
- `feeder-sub` → UniFi feeder 640×360 native substream
- `ground-main` / `ground-sub` → second camera (disabled in pipeline; reserved)
- `feeder-demo` → relay of `rtsp://localhost:8654/feeder-main` (the demo-loop mediamtx publishing a pre-encoded 640×360 MP4 on a loop)

Pipeline behavior:
- **Live mode** (default): `FrameCapture` reads `feeder-sub` (the 640×360 substream); `HlsSegmenter` reads `feeder-main` (1080p, for replay/recording quality).
- **Demo mode**: env var `PIPELINE_TEST_RTSP_URL` overrides BOTH dicts to point at the demo loop URL. Pipeline analyzes the demo video.

Dashboard behavior:
- Polls `GET /api/demo-mode` at boot + every 5s.
- If `enabled=true`, sets `<video-stream src="...?src=feeder-demo">` (matches what pipeline analyzes).
- If `enabled=false`, sets `<video-stream src="...?src=feeder-main">` (UniFi live camera).
- Toggle button at top of dashboard switches state; also calls `window.__refreshLiveSrc()` so the video source flips immediately.

## What changed overnight (2026-05-11)

Context: by end of the previous session the dashboard was broken — an HLS+canvas overlay rewrite had failed under thermal load (213% pipeline CPU, throttled), and labels weren't rendering. David asked for a comprehensive overnight rebuild with audits.

Nine commits on `main` branch (Pi-side):

```
b71362b  fix(refresh_rtsp): preserve non-managed go2rtc streams across token refresh
12dad55  docs: stability monitor results in overnight handoff
5c69707  docs: update overnight handoff with final verified state
82cdc30  fix(hailo): contiguous resize-temp for slice safety; ui: demo prefix in status
01f505f  docs: overnight execution result handoff
ea293f3  perf+audit: HailoDetector buffer pool, SnapshotWriter copy elim, dashboard hardening
994d749  perf+demo: PyAV threads=1, 640x360 demo loop, dashboard auto-switch source
5331a1d  perf(pipeline): FrameCapture → feeder-sub substream; dashboard resize fix
c06b694  fix(dashboard): restore WebRTC + DOM-label live view per CLAUDE.md
```

In order of what they actually do:

**A. Restored the documented live-view architecture (`c06b694`)** — replaced ~570 lines of HLS-player + canvas-overlay + sidecar-PTS in `setupLiveView()` with ~240 lines of WebRTC `<video-stream>` + DOM `<div class="live-bbox">` + CSS-transition smoothing. CLAUDE.md (line "Video Path") documents this as the intended architecture. The HLS+canvas rewrite had been a session-long deviation. Server-side bedrock from that rewrite (PTS plumbing, snapshot accuracy, sentinel) was preserved.

**B. Moved FrameCapture off the 1080p main stream (`5331a1d`)** — `bird_pipeline_v3.py:246` now passes `CAMERAS_DETECT[name]` (= `feeder-sub`, 640×360) instead of `CAMERAS_MAIN[name]` (1080p). Pi 5 has no HW H.264 decoder, so software-decoding 1080p was eating ~76% of one core. Substream decode is ~14%. The Pi has not been thermally throttled since.

**C. Made demo mode usable end-to-end (`994d749`)** — pre-encoded `may10_demo_640x360.mp4` so the demo loop matches the substream resolution path, added the `feeder-demo` stream to go2rtc, made the dashboard auto-switch its WebRTC src by polling `/api/demo-mode`. Also pinned libavcodec `thread_count=1` in `FrameCapture` to kill 3 auto-spawned slice workers that were costing ~80% of a core combined.

**D. Memory-bandwidth optimizations (`ea293f3`)** —
  - `HailoDetector`: preallocated `(1, 640, 640, 3)` uint8 input buffer in `__init__`, refilled in place each frame (was allocating ~3-4 MB/frame).
  - `SnapshotWriter`: removed two defensive `.copy()` calls on `frame_bgr` / `frame_bgr_full` (the producer doesn't mutate; copies were ~6 MB each per locked track).
  - `FrameCapture`: INTER_AREA → INTER_LINEAR in the fallback resize path (currently no-op since substream matches detect dims).
  - Dashboard hardening: in-flight guard on the demo-mode poll, BFCache-safe pause/resume on visibilitychange, instant `window.__refreshLiveSrc()` hook so user toggles propagate in <1s.

**E. Contiguity safety on the Hailo preallocated buffer (`82cdc30`)** — an audit caught that the slice into `_input_buf` is contiguous only when the inscribed-rect spans the full 640-px input width. Fix: resize into a contiguous `_resize_temp` instead, then memcpy into the slice. Robust across any future camera resolution.

**F. UI nudge (`82cdc30` also)** — live-status prefix says `demo · N tracks` when the active source is `feeder-demo`. Small but useful for telling at a glance which video the user is on.

**G. Morning hotfix (`b71362b`)** — `refresh-rtsp.timer` at 03:10 had wiped the new `feeder-demo` stream by rewriting `go2rtc.yaml` from a hardcoded canonical list. Patched `tools/refresh_rtsp.py` to read existing yaml with PyYAML, preserve any stream NOT in its managed set (`feeder-main`, `feeder-sub`, `ground-main`, `ground-sub`), and append them verbatim after writing the fresh-token canonical streams.

## Measured impact

| Metric | Pre-rebuild | Post-rebuild | Notes |
|---|---|---|---|
| `bird-pipeline` CPU | 213% (>2 cores) | 119-133% | Single-camera load |
| Temp under load | 84-86°C | 74-78°C | Throttle threshold = 85°C |
| `vcgencmd get_throttled` | `0xe0008` (bit 2 set = actively throttled) | `0xe0000` (historical only) | No active throttle event in 15-min soak |
| SSE delivery rate | ~30 Hz (when working) | ~30 Hz (sustained) | LAN only |
| Labels visible on demo loop | No | Yes | Verified via headless Playwright probe |
| Service restarts in 15-min soak | n/a (HLS broken) | 0 | Stability monitor evidence |

## Current state — what works, what doesn't

### Works
- Dashboard on LAN (`http://pi5.local:8099/`): WebRTC video plays, labels render on birds (DOM nodes with CSS transition smoothing), demo toggle works, labels toggle works, snapshots write to disk.
- Pipeline runs continuously without restart, thermally healthy, all four services stable.
- Detection accuracy is unchanged (same Hailo HEF, same AIY ONNX, same tracker config; we just feed it substream-resolution frames).
- Test/verification rig: 640×360 demo video loops on the Pi via the bird-demo-loop service; dashboard plays the SAME video so labels appear on the actual demo birds (not "labels for one source drawn over a different source").

### Known issues (deferred, not fixed)

1. **Snapshots are now 640×360 instead of 1080p.** Real regression. `frame.bgr_full` used to be the camera's main stream pixels; now it's the substream pixels. The proper fix is an on-demand 1080p ffmpeg-pull-per-lock-event so snapshots stay hi-res without the steady-state cost. Not implemented yet.

2. **Cloudflare tunnel buffers SSE.** Through `pi5.vivessato.com`: video plays (WebRTC negotiates fine), labels DON'T update (Cloudflare buffers `text/event-stream`). Three fix options on the table: switch SSE → WebSocket (CF doesn't buffer WS), switch to polling for label transport, or get a CF tunnel config option to disable buffering for that endpoint.

3. **Sync is "approximate," not "perfect."** WebRTC video (~100-300ms latency) + SSE delivery (~100-200ms) + CSS 240ms transitions = labels lag perched birds by ~200-500ms and visibly trail fast-flying birds. For true frame-accurate sync see Codex's spec at `docs/working/specs/2026-05-11-spatial-subtitle-overlay-architecture.md` (multi-day work; intentionally deferred).

4. **No N>1 camera testing.** Architecture is per-camera (FrameCapture/HlsSegmenter spin up per entry in `CAMERAS_DETECT`), but only feeder is enabled and we haven't load-tested at N=2-4. Track A audit's conclusion at scale: a single Pi 5 cannot do N=8 high-res streams regardless of architecture; the right N=4-8 answer is either camera-side H.265 + Pi `hevc_v4l2m2m` hardware decode, or federated Pis (one per camera) with central aggregation.

5. **Visual polish not done.** Functional, not beautiful — diag chip is monospace console-style, no labels-only-mode toggle separate from boxes-and-labels, no big "DEMO" badge other than small text prefix, no Adaptive Lock kernel port for smoother motion on fast birds.

## Architectural questions to think through with David

1. **Should the live path actually do frame-accurate sync?** Codex's spec argues yes — production subtitle/timed-graphics systems use a `ClockBridge` between detection clock and media clock, with a deliberate 10s display delay so the classifier has time to lock before the user sees the frame. Trade: complexity (3-4 implementation slices, weeks of work) for genuinely perfect sync that works on fly-throughs. Codex's spec is at `docs/working/specs/2026-05-11-spatial-subtitle-overlay-architecture.md` — read that for the full pitch.

2. **What does N=4 cameras look like?** Three options on the table:
   - **Single Pi + camera-side H.265**: requires camera upgrade (G4 Pro / G5 series support H.265; G3 Dome does not). Pi 5 can hardware-decode HEVC at 1080p60. Most elegant but capital cost + camera migration.
   - **Single Pi + further code optimization**: a stretch — already at 125% CPU per camera at N=1, would need to halve again to fit 4×.
   - **Federated Pis (one per camera) + central aggregation**: cheapest per stream ($80 + $90 Hailo HAT), no shared bottleneck. Dashboard becomes a multi-source aggregator. This is Track C's recommended N=8 answer.

3. **Live mode video quality vs detection accuracy.** Right now FrameCapture reads the camera substream (640×360) for inference. YOLO and AIY classifier inputs are 640×640 and ~224×224 respectively, so substream is plenty of resolution for them. But David might prefer detection to run on the full 1080p main stream for higher accuracy on small / distant birds. Trade: ~60% more CPU per camera for diminishing detection-quality gain.

4. **Is the snapshot regression acceptable until on-demand 1080p is built?** Snapshots at 640×360 are still useable for the review UI and species data, just lower-quality. The on-demand 1080p pull is ~50 lines of code in SnapshotWriter (spawn ffmpeg one-shot to grab a single frame from `feeder-main` at lock-time, scale into the same bbox coords). David could approve it as a quick follow-up or accept the lower-res state for a week.

5. **Tunnel architecture.** Right now: video goes through CF tunnel as WebRTC (works), label data goes through CF tunnel as SSE (broken — buffered). Easiest fix: a WebSocket endpoint mirroring the SSE one. Estimated 1-2 hours.

## Files conversational Claude should pull first

To understand the system shape:
- `CLAUDE.md` (repo root) — mission + service map + key rules
- `bird_pipeline_v3.py` — orchestrator
- `pipeline/frame_capture.py` — clearest single-file look at the I/O contract
- `dashboard/pi_dash.html` lines 1271-1510 — the rewritten `setupLiveView`

To understand the design decisions:
- `docs/working/specs/2026-05-11-spatial-subtitle-overlay-architecture.md` — Codex's bedrock proposal
- `docs/working/plans/2026-05-11-pipeline-cpu-audit-plan.md` — the audit plan that drove last night's work
- `docs/working/plans/2026-05-11-overnight-execution.md` — what last night's plan said it'd do
- `docs/working/progress/2026-05-11-overnight-result.md` — what it actually did

To understand the previous failure modes:
- `docs/working/progress/2026-05-10-overlay-sync-handoff.md` — the doc I wrote pre-compaction yesterday describing the HLS+canvas attempt that failed

## What I'm uncertain about / would value an outside read on

- **Is the WebRTC+DOM live path the right destination, or just a tactical stop on the way to Codex's spatial-subtitle spec?** I claimed the live view "doesn't need frame-accurate sync" — but Codex's spec argues that's exactly the assumption that makes labels look broken on fast birds. I might be optimizing for "ships tonight" at the cost of "right for the next year."

- **The snapshot regression.** I shipped without the on-demand 1080p path. David might rightly think that's not OK and want it before anything else. Or he might think hi-res snapshots aren't load-bearing for his use case. I don't know which.

- **The decision to pre-encode the demo to 640×360.** I did it to make the demo loop a clean test of the substream path. But it also means the demo video doesn't look as nice as the source. Could be either left alone (the goal is functional demo, not pretty demo) or revisited (encode at 640×360 from a *higher-quality source* to bird-feed-loop, since detection quality and visual quality aren't actually the same constraint).

- **Whether to push toward N=4-8 cameras now or keep N=1 polished.** Multi-camera is the original architectural intent. But the per-camera CPU envelope doesn't fit at N=4 today. Spending a week on Codex's spatial-subtitle architecture might or might not be the right priority vs spending that week on the camera upgrade + HW HEVC decode path.

I'd rather you push back on these than agree with me. I've been in the trenches for hours and my judgment is biased toward "what I just built."
