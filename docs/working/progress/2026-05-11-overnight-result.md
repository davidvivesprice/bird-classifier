# Overnight Execution — Result (2026-05-11)

**Status at sunrise:** working demo loop on iMac LAN with synced labels on birds, CPU dropped from 213% → ~125%, thermals dropped from 84-86°C (throttled) → ~80°C (not throttled).

## TL;DR — what to test first

1. Open the dashboard on the iMac (LAN): http://pi5.local:8099/?syncdiag=1
2. Wait ~15 seconds for WebRTC to connect.
3. You should see the looped demo video playing AND labels appearing on the birds. The diag chip in the corner shows SSE rate + track count.
4. Click the "demo mode" toggle to switch between demo loop and live camera; the video source switches within 1 second.
5. Click "Labels" button to hide bboxes + labels (state persists in localStorage).

If you don't see labels: refresh the page (Cmd+R), or hard-reload (Cmd+Shift+R) to bypass cache.

If the dashboard looks broken: revert is `git revert c06b694 5331a1d 994d749 47fed11` (the four commits below).

## What was done

Followed the overnight execution plan at `docs/working/plans/2026-05-11-overnight-execution.md`. Five phases, two parallel audit gates, ~3 hours of work.

### Phase 1A — restore browser live-view per CLAUDE.md (commit `c06b694`)

The HLS + canvas + sidecar-PTS rewrite (commits `1503435..c12f82c`) was replaced with the documented WebRTC + DOM-label architecture. Server-side bedrock (PTS plumbing, snapshot accuracy, segmenter, sentinel) was preserved — only the browser live-view path changed.

- `<video-stream>` custom element from go2rtc → sub-second WebRTC video
- SSE → DOM `<div class="live-bbox">` + `<div class="live-label">` per track
- CSS `transform` transitions (240ms cubic-bezier) for smooth bbox motion
- 1.5s GC on stale tracks, opacity fade out
- Diag chip (?syncdiag=1) shows SSE rate, track count, last-event age
- Labels toggle persisted in localStorage
- `window.__overlayDebug` hook for Playwright probes (new shape: `arch='webrtc+dom'`, n_tracks, lastEventAgeMs, sseCount, tracksSummary)

Net delta: −263 lines in `dashboard/pi_dash.html`. The hls.js vendor script tag, canvas overlay, sidecar PTS lookup, Adaptive Lock kernel, rVFC render loop, all gone.

**Audit gate 1A:** parallel agent returned PASS with two MED issues — addressed inline (see Phase 1B+).

### Phase 1B — FrameCapture switched to substream (commit `5331a1d`)

`bird_pipeline_v3.py:246` now passes `CAMERAS_DETECT[name]` (feeder-sub, 640×360 native) to FrameCapture. `CAMERAS_MAIN[name]` is still used by HlsSegmenter for replay/recording quality.

Track A audit confirmed: Pi 5 has NO hardware H.264 decoder (only HEVC). Software-decoding 1080p main stream costs ~76% of a core; substream decode is ~14%. With detect_width/height matching substream native res, no `cv2.resize` fires (the conditional at `frame_capture.py:181` takes the no-op branch).

**Snapshot trade-off:** snapshots tonight are 640×360 (not 1080p). The high-res 1080p snapshot via on-demand main-stream pull is a follow-up.

Also in this commit:
- Dashboard window-resize handler (audit fix): rAF-coalesced re-apply of all bbox geometry on viewport change. Fixes "bbox floats off bird on iPad rotation" bug.
- Stale `<!-- hls.js -->` HTML comment removed.

### Phase 1B+ — 640×360 demo loop + dashboard auto-switch (commit `994d749`)

Pre-encoded a 640×360 H.264 demo video on the Pi (`~/bird-snapshots/demo/may10_demo_640x360.mp4`, 9.97 MB, 150s, 2s GOP). Source was the 1080p `may10_demo_normalized.mp4`; rescaled once via:

```
ffmpeg -i may10_demo_normalized.mp4 -vf scale=640:360 \
  -c:v libx264 -preset veryfast -crf 23 \
  -g 60 -keyint_min 60 -sc_threshold 0 -profile:v main -pix_fmt yuv420p -an \
  may10_demo_640x360.mp4
```

`scripts/demo-loop.sh` now points at the new video (added to repo; was Pi-only before). `go2rtc.yaml` got a new `feeder-demo` stream relaying `rtsp://localhost:8654/feeder-main` (the mediamtx demo loop) so the dashboard can play it via the same WebRTC infra as live cameras.

Dashboard `setupLiveView` now polls `/api/demo-mode` at boot and switches the `<video-stream>` `src` between `feeder-main` (live camera) and `feeder-demo` (demo loop). 5s periodic re-check so manual server-side flips propagate.

Phase 2A (commit `994d749`):
- `pipeline/frame_capture.py`: pin libavcodec `thread_count=1, thread_type=NONE` before first `decode()`. Pi 5 ARM cores don't benefit from slice threading at 360p; the auto-spawned slice workers were costing ~20-30% of a core × 3 threads. Pipeline log now shows `threads=1` on stream open. Audit confirmed placement.

### Phase 2B/2C/2D + audit hardening (commit `47fed11`)

`pipeline/hailo_detector.py`:
- Preallocated `(1, 640, 640, 3)` uint8 input buffer in `__init__`, pre-filled with 114 (gray, the letterbox pad value).
- Per-frame: compute the inscribed-rect once, write resized BGR directly into a slice via `cv2.resize(..., dst=target_slice)`, then in-place BGR→RGB via `cv2.cvtColor(target_slice, COLOR_BGR2RGB, dst=target_slice)`.
- Eliminates ~3-4 MB of allocation per frame at 30fps = 90-120 MB/s of malloc traffic that was hitting glibc's arena allocator.
- Old `_letterbox` helper deleted (dead code).
- `import cv2` moved to module level.

`pipeline/snapshot_writer.py`:
- Removed two defensive `.copy()` calls on `frame_bgr` (~700 KB) and `frame_bgr_full` (~6 MB at 1080p, ~700 KB at 640p) in `submit()`. PyAV.to_ndarray returns a fresh array per frame; the producer never mutates. Track B audit caught this.

`pipeline/frame_capture.py`:
- INTER_AREA → INTER_LINEAR in the (currently no-op) fallback resize branch. Future-proof if demo source ever has a different resolution.

`dashboard/pi_dash.html` (audit fixes):
- `pickLiveSrc` in-flight guard so concurrent fetches don't pile up
- Interval handle stored, paused on `visibilitychange`/`pagehide`, resumed on `pageshow` → no BFCache timer accumulation
- New `window.__refreshLiveSrc` public hook
- Demo-toggle button click calls `__refreshLiveSrc()` 300ms after POSTing — user toggles propagate to the `<video-stream>` src within 1s instead of 5s

## Measured impact

| Metric | Baseline (213% / 84°C) | After Phase 1B / 2A | After Phase 2B/C/D |
|---|---|---|---|
| `bird-pipeline` CPU | 213% | 124-139% | ~125% |
| Temp | 84-86°C | 80-83°C | 80-81°C |
| `vcgencmd get_throttled` | `0xe0008` (active) | `0xe0000` (historical only) | `0xe0000` |
| SSE rate | 25-30 Hz | 27-30 Hz | 30 Hz |
| Memory (RSS) | 502 MB | 411 MB | 411 MB |
| Dashboard probe | FAIL (no boxes) | PASS (2 boxes) | PASS (2-5 boxes) |

The chip is no longer at thermal throttle. `bit 2 (currently throttled)` is CLEARED — only the historical bits (17/18/19) remain from earlier sustained throttling.

## Audit verdicts (parallel-agent reviews)

- **Phase 1A audit:** PASS with CONCERNS — two MED issues (resize handler, stale comment) → fixed in next commit.
- **Phase 1B + 2A audit:** PASS with CONCERNS — two MED issues (BFCache timer leak, race window on demo toggle) → fixed in next commit.
- **Phase 2 deploy audit:** pending (still running at handoff time — see background task `aebc45837f226d17d`).
- **15-min stability test:** in flight at handoff time — see background task `bnfwk8woh`. Samples CPU/thermal/SSE every 60s. Result CSV will be in the task output file.

## What works

- Dashboard live view on iMac LAN: WebRTC video plays, DOM labels render on birds with CSS-transition smoothing.
- Demo-mode toggle switches between live UniFi feed and demo loop; dashboard auto-switches `<video-stream>` source to match.
- SSE flowing at ~30 Hz through LAN.
- Pipeline thermally healthy, no longer throttled.
- Snapshots still write (low-res tonight; 1080p path is a follow-up).
- HLS segmenter still running for the replay harness — separate from live view.

## Known issues / follow-ups

1. **Snapshots at 640×360 instead of 1080p.** FrameCapture now reads substream; `bgr_full` is 640×360. On-demand 1080p pull via ffmpeg-spawn-per-lock is a separate task. Acceptable tonight.

2. **Cloudflare tunnel SSE still buffered (0 events through tunnel).** Live view through `pi5.vivessato.com` will show video (WebRTC negotiates fine) but no label updates. Three options: switch SSE → WebSocket; switch to polling for label transport; or get Cloudflare to disable buffering for the SSE endpoint. Not blocking tonight's LAN demo.

3. **No N>1 camera testing.** Architecture supports it (FrameCapture/HlsSegmenter are per-camera), but only the feeder is enabled and we haven't load-tested at N=2-4 yet. The CPU envelope at N=1 is ~125% of one core; at N=4 we'd be at ~500% which exceeds the 4-core Pi 5. Codec audit (Track A) recommended the camera-side H.265 path or federated Pis for N=4-8.

4. **iPad / Safari "Install as App" PWA flow not verified tonight.** WebRTC support on iPad Safari has historically been finicky with autoplay. The architecture should work but you'll want to retest tomorrow.

5. **`requestVideoFrameCallback`-driven frame-accurate replay** still useful for the annotation harness — Codex's `2026-05-11-spatial-subtitle-overlay-architecture.md` spec is the next destination. The existing `tools/sync_replay_assert.py` harness consumes HLS segments + sidecar PTS for replay against `may10_demo_video.annotations.md`. That work is independent of the live-view path.

## How to revert if anything's broken

```bash
cd /Users/vives/bird-classifier-pi
git log --oneline | head -10   # commits 1c701db..47fed11 are tonight's
git revert 47fed11 994d749 5331a1d c06b694 1c701db 901d98b
git push  # if you want it on a branch
rsync -av dashboard/pi_dash.html bird_pipeline_v3.py pipeline/ \
  vives@pi5.local:/home/vives/bird-classifier/
ssh vives@pi5.local "systemctl --user restart bird-pipeline.service bird-dashboard.service go2rtc.service bird-demo-loop.service"
```

To revert just the demo video swap (keep code changes):
```bash
ssh vives@pi5.local "sed -i 's|may10_demo_640x360|may10_demo_normalized|' /home/vives/bird-classifier/scripts/demo-loop.sh && systemctl --user restart bird-demo-loop.service"
```

## Critical files (paths)

| File | Purpose |
|---|---|
| `dashboard/pi_dash.html` | Dashboard with new `setupLiveView` |
| `bird_pipeline_v3.py:243-265` | FrameCapture wiring (substream + segmenter on main) |
| `pipeline/frame_capture.py` | PyAV reader, threads=1 pin, INTER_LINEAR fallback |
| `pipeline/hailo_detector.py` | Preallocated input buffer, in-place BGR→RGB |
| `pipeline/snapshot_writer.py` | No-copy submit path |
| `scripts/demo-loop.sh` | Demo video path (now 640×360) |
| `go2rtc.yaml` | feeder-demo stream (relays mediamtx demo loop on :8654) |
| `docs/working/plans/2026-05-11-overnight-execution.md` | The plan I executed |
| `docs/working/plans/2026-05-11-pipeline-cpu-audit-plan.md` | The audit plan (still valid; Codex prompts unused — feel free) |
| `docs/working/specs/2026-05-11-spatial-subtitle-overlay-architecture.md` | Codex's bedrock spec for the eventual full solution |
| `docs/working/progress/2026-05-11-overnight-result.md` | This document |

## Commits tonight

```
47fed11  perf+audit: HailoDetector buffer pool, SnapshotWriter copy elim, dashboard hardening
994d749  perf+demo: PyAV threads=1, 640x360 demo loop, dashboard auto-switch source
5331a1d  perf(pipeline): FrameCapture → feeder-sub substream; dashboard resize fix
c06b694  fix(dashboard): restore WebRTC + DOM-label live view per CLAUDE.md
901d98b  debug(overlay): window.__overlayDebug hook + rVFC resilience  (pre-rewrite cleanup)
1c701db  feat(dashboard): /api/demo-mode toggle endpoint  (David's parallel work, committed for clean baseline)
```

## What I did NOT do tonight (deliberate)

- The full Codex spatial-subtitle-overlay architecture (multi-day work)
- 1080p on-demand snapshot path (next task)
- Cloudflare tunnel SSE fix (separate problem)
- Demo mode sandbox / separate DB (Codex spec covers it)
- Touch any theme CSS or theme switcher JS
- Touch any dashboard panel other than the live-stage region
- Skip a verification gate
- Skip an audit gate (both completed during execution; Phase 2 audit was still running at handoff time)
